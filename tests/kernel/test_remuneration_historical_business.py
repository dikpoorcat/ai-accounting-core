"""Declared source draft wages remain auditable through current-format corrections."""

import copy
from collections import defaultdict
from dataclasses import replace

import pytest
from opening_fixture import adopt_empty_opening
from test_payroll import contribution_policy, income_tax_policy, payroll, profile
from test_payroll_corrections import Company
from test_remuneration_development_upgrade import create_archived_company

from ai_accounting.kernel import offline_development_upgrade as development
from ai_accounting.kernel.backup import verify_file
from ai_accounting.kernel.contracts import NeedsInformation
from ai_accounting.kernel.development_contracts import REMUNERATION_SOURCE_FINGERPRINT
from ai_accounting.kernel.domains.cash import CashFunding, CashPayment
from ai_accounting.kernel.domains.payroll import _remuneration_outcome
from ai_accounting.kernel.domains.transactions import Allocation
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.payroll_confirmation import resolve_payroll_confirmation
from ai_accounting.kernel.payroll_history import HistoricalPayroll, HistoricalPayrollProfile
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.storage import Store


def literal_archived_wage(version, context):
    """Install an explicitly known synthetic old result, never calculate a tax."""
    assert type(version.fact) is HistoricalPayroll
    assert version.fact.accounting_gross_salary_fen == 4_000_000
    wage = version.fact
    employee_profile = context.one("payroll_profile", "@" + wage.profile_revision.subject_id)
    contributions = context.one(
        "payroll_contribution_policy", "@" + wage.contribution_policy_revision.subject_id
    )
    tax_policy = context.one(
        "payroll_income_tax_policy", "@" + wage.income_tax_policy_revision.subject_id
    )
    confirmation = resolve_payroll_confirmation(
        version,
        context,
        profile_version=employee_profile,
        contribution_policy_version=contributions,
    ) | {"income_tax_policy_fact_id": tax_policy.id}
    return _remuneration_outcome(
        version,
        "560201",
        4_000_000,
        (("net", "221101", 3_970_000, wage.employee_id), ("tax", "222103", 30_000, None)),
        {
            "employee_id": wage.employee_id,
            "gross_fen": 4_000_000,
            "net_fen": 3_970_000,
            "tax_fen": 30_000,
            "employee_contributions_fen": 0,
            "employer_contributions_fen": 0,
            "source_versions": [employee_profile.id],
            "rule_versions": [contributions.id, tax_policy.id],
            "payroll_confirmation": confirmation,
        },
        (
            {
                "step": "explicit_archived_fixture_result",
                "values": {"gross_fen": 4_000_000, "tax_fen": 30_000, "net_fen": 3_970_000},
            },
        ),
    )


def archived_wage_company(tmp_path):
    target = production_bundle()
    source = development.source_bundle(target, REMUNERATION_SOURCE_FINGERPRINT)
    path = tmp_path / "archived-wage.sqlite"
    create_archived_company(path, source)
    # The private installation registry is restricted to this synthetic fixture.
    # Public runtime registration still excludes every original tax-only grammar.
    registry = copy.copy(source.registry)
    registry.historical_only = source.registry.historical_only - {
        "payroll",
        "payroll_profile",
        "payroll_income_tax_policy",
    }
    registry.evaluators = dict(source.registry.evaluators, payroll=literal_archived_wage)
    fixture_bundle = replace(source, registry=registry)
    company = Company.__new__(Company)
    company.engine = Engine(Store(path, fixture_bundle, "synthetic-co", "synthetic-db"))
    company.sequence = 0
    company.materials = defaultdict(list)
    company.owner_confirmation = company.engine.register_evidence(
        b"explicit synthetic complete source documents",
        "text/plain",
        "owner confirmation",
        request_id=company.request(),
    )["digest"]
    adopt_empty_opening(company.engine, period="2026-08")
    original_profile = HistoricalPayrollProfile(
        **profile(
            period="2026-08",
            effective_from="2026-08",
            effective_to="2026-08",
            social_insurance_base_fen=None,
            social_insurance_participating=False,
        ).model_dump(),
        withholding_start_date="2026-03",
    )
    company.save(original_profile, "profile")
    company.save(contribution_policy(), "contributions")
    company.save(income_tax_policy(), "income-tax")
    original_wage = HistoricalPayroll(
        **payroll(period="2026-08", accounting_gross_salary_fen=4_000_000).model_dump(),
        income_tax_policy_revision={"subject_id": "income-tax", "revision": 1},
        tax_reported_salary_fen=4_000_000,
        tax_exempt_income_fen=0,
        special_additional_deduction_fen=0,
        other_legal_deduction_fen=0,
        tax_relief_fen=0,
    )
    company.save(original_wage, "wage")
    company.confirm_payroll("wage")
    _, publication = company.publish("wage")
    return company, source, target, publication["wage"]


def test_migrated_archived_wage_remains_settleable_without_recalculation(tmp_path):
    company, _source, target, _publication = archived_wage_company(tmp_path)
    old = company.current("wage")
    frozen = company.close("2026-08")
    with company.engine.store.connection() as connection:
        development.upgrade_company(connection, target)
    company.engine = Engine(
        Store(company.engine.store.path, target, "synthetic-co", "synthetic-db")
    )
    company.save(
        CashFunding(
            period="2026-09",
            actual_date="2026-09-01",
            owner_id="owner",
            amount_fen=1_000_000,
            funding_kind="loan",
            cash_account_id="cash",
        ),
        "funds",
    )
    company.save(
        CashPayment(
            period="2026-09",
            actual_date="2026-09-02",
            direction="outflow",
            cash_account_id="cash",
            counterparty_id="employee",
            amount_fen=1_000_000,
            allocations=(
                Allocation(
                    source_kind="payroll", source_id="wage", obligation="net", amount_fen=1_000_000
                ),
            ),
        ),
        "historical-wage-payment",
    )
    company.publish("funds", "historical-wage-payment")
    assert company.current("wage") == old
    assert company.count("fact_payroll_confirmed") == 0
    assert company.count("fact_remuneration_tax_basis") == 0
    with company.engine.store.connection(read_only=True) as connection:
        outstanding = connection.execute(
            "SELECT amount FROM balance WHERE category='payable' AND balance_key='payroll:wage:net'"
        ).fetchone()[0]
        assert outstanding == 2_970_000
    from ai_accounting.kernel.periods import Periods

    assert Periods(company.engine).closed_report("2026-08") == frozen
    assert (
        verify_file(company.engine.store.path, _bundle=target)["verification"]["status"]
        == "verified"
    )


@pytest.mark.parametrize("closed", [False, True], ids=["open-replacement", "closed-reversal"])
def test_original_wage_corrects_with_current_body_and_confirmed_tax_then_settles(tmp_path, closed):
    company, source, target, original_publication = archived_wage_company(tmp_path)
    original_calculation = company.current("wage")
    with company.engine.store.connection(read_only=True) as connection:
        old_fact = company.engine.store.current_fact(connection, "wage")
        old_body = company.engine.store.fact_data(connection, old_fact.id)
        old_row = tuple(
            connection.execute(
                "SELECT * FROM fact_revision WHERE id=?",
                (old_fact.id,),
            ).fetchone()
        )
        calculation_row = tuple(
            connection.execute(
                "SELECT * FROM calculation WHERE id=?",
                (original_calculation.id,),
            ).fetchone()
        )
    frozen = company.close("2026-08") if closed else None
    original_ledger = company.engine.ledger("2026-08")
    assert (
        verify_file(company.engine.store.path, _bundle=source)["verification"]["status"]
        == "verified"
    )
    with company.engine.store.connection() as connection:
        result = development.upgrade_company(connection, target)
        assert result["status"] == "upgraded"
    company.engine = Engine(
        Store(company.engine.store.path, target, "synthetic-co", "synthetic-db")
    )
    with pytest.raises(NeedsInformation) as archived_recalculation:
        company.engine.preview(["wage"], posting_period="2026-09" if closed else None)
    assert archived_recalculation.value.response()["fact_issues"][0]["field"] == (
        "remuneration_revision"
    )
    current_profile = profile(
        period="2026-08",
        effective_from="2026-08",
        effective_to="2026-08",
        social_insurance_base_fen=None,
        social_insurance_participating=False,
    )
    company.save(current_profile, "profile", revision=1)
    company.save(
        payroll(
            period="2026-08",
            accounting_gross_salary_fen=4_000_000,
            profile_revision={"subject_id": "profile", "revision": 2},
        ),
        "wage",
        revision=1,
    )
    company.confirm_payroll("wage")
    # Missing real tax blocks preview, even though the old sealed result had 300.
    before_missing_tax = {
        table: company.count(table) for table in ("calculation", "voucher_version", "voucher_line")
    }
    with pytest.raises(NeedsInformation) as missing:
        company.engine.preview(["wage"], posting_period="2026-09" if closed else None)
    assert missing.value.response()["status"] == "needs_information"
    assert company.current("wage") == original_calculation
    assert {table: company.count(table) for table in before_missing_tax} == before_missing_tax
    company.tax_basis("wage", 90_000)
    _, corrected = company.publish("wage", posting_period="2026-09" if closed else None)
    current = company.current("wage")
    assert current.values["gross_fen"] == 4_000_000
    assert current.values["net_fen"] == 3_910_000
    assert current.values["tax_fen"] == 90_000
    assert current.values["tax_basis"]["source_id"] == "wage"
    assert corrected["wage"]["mode"] == ("closed_correction" if closed else "open_replace")
    if closed:
        assert corrected["wage"]["posting_period"] == "2026-09"
        assert corrected["wage"]["voucher_number"] != original_publication["voucher_number"]
        assert company.engine.ledger("2026-08") == original_ledger
        correction_ledger = company.engine.ledger("2026-09")
        assert len(correction_ledger) == 2
        reversal = next(item for item in correction_ledger if item["reverses_id"])
        replacement = next(item for item in correction_ledger if not item["reverses_id"])
        assert reversal["reverses_id"] == original_ledger[0]["id"]
        assert replacement["number"] == corrected["wage"]["voucher_number"]
        from ai_accounting.kernel.periods import Periods

        assert Periods(company.engine).closed_report("2026-08") == frozen
    else:
        assert corrected["wage"]["voucher_number"] == original_publication["voucher_number"]
    with company.engine.store.connection(read_only=True) as connection:
        assert company.engine.store.fact_data(connection, old_fact.id) == old_body
        assert type(company.engine.store.fact(connection, old_fact.id).fact) is HistoricalPayroll
        assert (
            tuple(
                connection.execute(
                    "SELECT * FROM fact_revision WHERE id=?",
                    (old_fact.id,),
                ).fetchone()
            )
            == old_row
        )
        assert (
            tuple(
                connection.execute(
                    "SELECT * FROM calculation WHERE id=?",
                    (original_calculation.id,),
                ).fetchone()
            )
            == calculation_row
        )
        assert company.engine.store.current_fact(connection, "wage").revision == 2
    company.save(
        CashFunding(
            period="2026-09",
            actual_date="2026-09-02",
            owner_id="owner",
            cash_account_id="cash",
            amount_fen=4_000_000,
            funding_kind="loan",
        ),
        "funds",
    )
    for subject, amount, obligation, party in (
        ("partial-wage-payment", 1_000_000, "net", "employee"),
        ("september-tax-payment", 90_000, "tax", "tax-authority"),
    ):
        company.save(
            CashPayment(
                period="2026-09",
                actual_date="2026-09-10",
                direction="outflow",
                cash_account_id="cash",
                counterparty_id=party,
                amount_fen=amount,
                allocations=(
                    Allocation(
                        source_kind="payroll",
                        source_id="wage",
                        obligation=obligation,
                        amount_fen=amount,
                    ),
                ),
            ),
            subject,
        )
    company.publish("funds", "partial-wage-payment", "september-tax-payment")
    with company.engine.store.connection(read_only=True) as connection:
        net = connection.execute(
            "SELECT amount FROM balance WHERE category='payable' AND balance_key='payroll:wage:net'"
        ).fetchone()[0]
        tax = connection.execute(
            "SELECT coalesce((SELECT amount FROM balance WHERE category='payable' "
            "AND balance_key='payroll:wage:tax'),0)"
        ).fetchone()[0]
        assert net == 2_910_000
        assert tax == 0
        assert company.engine.store.fact_data(connection, old_fact.id) == old_body
        assert (
            tuple(
                connection.execute(
                    "SELECT * FROM calculation WHERE id=?",
                    (original_calculation.id,),
                ).fetchone()
            )
            == calculation_row
        )
    if closed:
        assert Periods(company.engine).closed_report("2026-08") == frozen
    assert (
        verify_file(company.engine.store.path, _bundle=target)["verification"]["status"]
        == "verified"
    )
