"""Small, self-contained exact SOURCE24 history for ordinary regression tests.

The private installation registry writes declared old bodies into their declared
old SQL. Two explicitly chosen results are literals, never retired calculators.
All installation overrides end before current close, backup and restore run.
"""

import copy
from collections import defaultdict
from dataclasses import replace

import entity_fixture
from close_storage_fixture import use_v4_close_manifest
from opening_fixture import adopt_empty_opening
from test_adjust_stage9_samples import original_file
from test_payroll import contribution_policy, income_tax_policy, profile
from test_payroll_corrections import Company

from ai_accounting.kernel.contracts import KernelError, Line
from ai_accounting.kernel.domains.assets import LoanAgreement, LoanDrawdown
from ai_accounting.kernel.domains.payroll import _remuneration_outcome
from ai_accounting.kernel.domains.transactions import obligation, outcome
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.entity_profile_history import PreEventProfile
from ai_accounting.kernel.materials import Materials
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.source24_history import (
    HISTORICAL_BODIES,
    LoanInterestPreReference,
    MaterialResolutionPreReference,
    PayrollPlanPreRevision,
    PayrollPreRevision,
    resolve_pre_revision_confirmation,
)
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.types import canonical
from scripts import stage9_history_adjustment as adjustment

MONTHS = ("2026-01", "2026-02")
HISTORY_TABLES = (
    "fact_payroll_pre_revision",
    "fact_payroll_plan_v2_pre_revision",
    "fact_material_resolution_v2_pre_reference",
    "fact_material_resolution_v2_pre_reference_links",
    "fact_loan_interest_pre_reference",
)


def literal_wage(version, context):
    assert type(version.fact) is PayrollPreRevision
    assert version.fact.accounting_gross_salary_fen == 600_000
    wage = version.fact
    employee_profile = context.one("payroll_profile", "@" + wage.profile_id)
    contributions = context.one("payroll_contribution_policy", "@" + wage.contribution_policy_id)
    tax_policy = context.one("payroll_income_tax_policy", "@" + wage.income_tax_policy_id)
    confirmation = resolve_pre_revision_confirmation(
        version,
        context,
        profile_version=employee_profile,
        contribution_policy_version=contributions,
        income_tax_policy_version=tax_policy,
    )
    return _remuneration_outcome(
        version,
        "560201",
        600_000,
        (("net", "221101", 597_000, wage.employee_id), ("tax", "222103", 3_000, None)),
        {
            "employee_id": wage.employee_id,
            "gross_fen": 600_000,
            "net_fen": 597_000,
            "tax_fen": 3_000,
            "employee_contributions_fen": 0,
            "employer_contributions_fen": 0,
            "source_versions": [employee_profile.id],
            "rule_versions": [contributions.id, tax_policy.id],
            "payroll_confirmation": confirmation,
        },
        (
            {
                "step": "literal_archived_fixture_result",
                "values": {"gross_fen": 600_000, "tax_fen": 3_000, "net_fen": 597_000},
            },
        ),
    )


def literal_interest(version, context):
    assert type(version.fact) is LoanInterestPreReference
    assert version.fact.period_start == version.fact.period + "-01"
    agreement = context.one("loan_agreement", "@" + version.fact.agreement_id)
    principal = context.one("loan_drawdown", "@" + version.fact.drawdown_id)
    assert principal.fact.principal_fen == 1_000_000
    # These amounts are chosen historical fixture results, not a recomputation.
    amount, days = {"2026-01": (5_167, 31), "2026-02": (4_667, 28)}[str(version.fact.period)]
    payable = obligation(
        version,
        name="interest",
        amount=amount,
        account="2231",
        normal="credit",
        counterparty=agreement.fact.lender_id,
        cashflow="interest_payments",
    )
    return outcome(
        [Line("5603", debit=amount), Line("2231", credit=amount)],
        {
            "interest_fen": amount,
            "principal_fen": 1_000_000,
            "actual_days": days,
            "annual_rate_percent": "6",
            "unrounded_interest_fen": {
                "2026-01": "5166.666666666666666666666667",
                "2026-02": "4666.666666666666666666666667",
            }[str(version.fact.period)],
        },
        [payable],
    )


def historical_book(tmp_path, monkeypatch):
    path = tmp_path / "historical-book.sqlite"
    source = original_file(path, "company")
    registry = copy.copy(source.registry)
    registry.historical_only = source.registry.historical_only - {
        *HISTORICAL_BODIES,
        "payroll_profile",
        "payroll_income_tax_policy",
    }
    registry.evaluators = dict(
        source.registry.evaluators, payroll=literal_wage, loan_interest=literal_interest
    )
    fixture = replace(source, registry=registry)
    company = Company.__new__(Company)
    company.engine = Engine(Store(path, fixture, "synthetic-company", "synthetic-database"))
    company.sequence = 0
    company.materials = defaultdict(list)
    company.owner_confirmation = company.engine.register_evidence(
        b"synthetic owner: all January and February documents explicitly complete",
        "text/plain",
        "synthetic completeness confirmation",
        request_id=company.request(),
    )["digest"]
    with monkeypatch.context() as installation:
        current_models = entity_fixture.entity_profile_models()
        installation.setattr(
            entity_fixture,
            "entity_profile_models",
            lambda: {**current_models, "person": PreEventProfile},
        )
        adopt_empty_opening(company.engine, period=MONTHS[0])
        old_profile = registry.models["payroll_profile"].model_validate_json(
            canonical(
                profile(
                    social_insurance_participating=False, social_insurance_base_fen=None
                ).model_dump(mode="json")
                | {"withholding_start_date": "2026-01"}
            )
        )
        company.save(old_profile, "profile")
        company.save(contribution_policy(), "contributions")
        old_tax = registry.models["payroll_income_tax_policy"].model_validate_json(
            canonical(income_tax_policy().model_dump(mode="json"))
        )
        company.save(old_tax, "income-tax")
        company.save(
            LoanAgreement(
                period=MONTHS[0],
                lender_id="lender",
                lender_is_licensed=True,
                currency="CNY",
                annual_rate_percent="6",
                day_count_basis="actual_360",
                maturity_date="2027-01-01",
                loan_term="short_term",
            ),
            "agreement",
        )
        company.save(
            registry.models["bank_opening"].model_validate_json(
                canonical(
                    {
                        "period": MONTHS[0],
                        "bank_account_id": "bank",
                        "opening_fen": 0,
                        "basis": "new_account",
                    }
                )
            ),
            "bank-opening",
        )
        company.publish("bank-opening")
        company.save(
            LoanDrawdown(
                period=MONTHS[0],
                agreement_id="agreement",
                principal_fen=1_000_000,
                actual_date="2026-01-01",
                bank_account_id="bank",
            ),
            "drawdown",
        )
        company.publish("drawdown")
        for month, end in zip(MONTHS, ("2026-02-01", "2026-03-01"), strict=True):
            wage = PayrollPreRevision(
                period=month,
                employee_id="employee",
                profile_id="profile",
                contribution_policy_id="contributions",
                income_tax_policy_id="income-tax",
                accounting_gross_salary_fen=600_000,
                tax_reported_salary_fen=600_000,
                tax_exempt_income_fen=0,
                special_additional_deduction_fen=0,
                other_legal_deduction_fen=0,
                tax_relief_fen=0,
                expense_class="management",
                contribution_basis="policy_until_actual",
            )
            company.save(
                PayrollPlanPreRevision(
                    period=month,
                    employee_id="employee",
                    payroll=wage,
                    profile_revision={"subject_id": "profile", "revision": 1},
                    contribution_policy_revision={"subject_id": "contributions", "revision": 1},
                    income_tax_policy_revision={"subject_id": "income-tax", "revision": 1},
                    change_notice_revisions=(),
                ),
                "plan-" + month,
            )
            wage_subject = "wage-" + month
            company.save(wage, wage_subject)
            company.save(
                LoanInterestPreReference(
                    period=month,
                    drawdown_id="drawdown",
                    agreement_id="agreement",
                    period_start=month + "-01",
                    period_end_exclusive=end,
                ),
                "interest-" + month,
            )
            company.publish(wage_subject, "interest-" + month)
            add_original_wage_material(company, month, wage_subject)
            entries = (
                [
                    {
                        "reference": "original-drawdown",
                        "actual_date": "2026-01-01",
                        "signed_fen": 1_000_000,
                    }
                ]
                if month == MONTHS[0]
                else []
            )
            company.save(
                registry.models["bank_statement"].model_validate_json(
                    canonical(
                        {
                            "period": month,
                            "bank_account_id": "bank",
                            "opening_fen": 0 if month == MONTHS[0] else 1_000_000,
                            "closing_fen": 1_000_000,
                            "entries": entries,
                        }
                    )
                ),
                "statement-" + month,
            )
            company.publish("statement-" + month)
            company.save(
                registry.models["bank_reconciliation"].model_validate_json(
                    canonical(
                        {
                            "period": month,
                            "bank_account_id": "bank",
                            "statement_id": "statement-" + month,
                            "matches": (
                                [
                                    {
                                        "reference": "original-drawdown",
                                        "source_kind": "loan_drawdown",
                                        "source_id": "drawdown",
                                    }
                                ]
                                if month == MONTHS[0]
                                else []
                            ),
                        }
                    )
                ),
                "reconciliation-" + month,
            )
            company.publish("reconciliation-" + month)
        # Install a declared historical freeze while all sources are still in
        # the exact old schema; this does not modify a current frozen month.
        use_v4_close_manifest(installation)
        try:
            january = company.close(MONTHS[0])
        except KernelError as error:
            raise AssertionError(error.response()) from error
    with company.engine.store.connection() as connection:
        originals = {
            target: tuple(
                map(tuple, connection.execute(f"SELECT * FROM {table} ORDER BY revision_id"))
            )
            for table, target in adjustment.PRESERVED_BODIES.items()
        }
        frozen_tables = (
            "period_close",
            "close_storage_root",
            "close_storage_subroot",
            "close_storage_directory",
            "close_storage_block",
        )
        frozen = {
            table: tuple(map(tuple, connection.execute(f"SELECT * FROM {table}")))
            for table in frozen_tables
        }
        result = adjustment.adjust_connection(
            connection,
            kind="company",
            first_fund_period=MONTHS[1],
            fund_accounts=[
                {
                    "entity_id": "bank",
                    "data": {
                        "bank_name": "Synthetic fixture bank",
                        "bank_account_number": "1234567890123456",
                        "account_holder_name": "Synthetic fixture company",
                    },
                    "source": "new explicit synthetic fund definition; not historical confirmation",
                }
            ],
        )
        assert result["status"] == "adjusted"
        for table, previous in originals.items():
            assert (
                tuple(map(tuple, connection.execute(f"SELECT * FROM {table} ORDER BY revision_id")))
                == previous
            )
        for table, previous in frozen.items():
            assert tuple(map(tuple, connection.execute(f"SELECT * FROM {table}"))) == previous
        from ai_accounting.kernel.offline_development_upgrade import upgrade_company

        assert upgrade_company(connection, production_bundle())["status"] == "upgraded"
    company.engine = Engine(
        Store(path, production_bundle(), "synthetic-company", "synthetic-database")
    )
    return company, january


def add_original_wage_material(company, month, subject):
    materials = Materials(company.engine)
    text = f"Synthetic original wage statement {month}: gross 600000 fen."
    proof = company.engine.register_evidence(
        text.encode(), "text/plain", "synthetic wage statement", request_id=company.request()
    )["digest"]
    source = materials.receive(
        "source-" + month,
        {
            "period": month,
            "evidence_digest": proof,
            "category": "payroll",
            "purpose": "business",
            "specification": {
                "format": "text",
                "all_pages_reviewed": True,
                "passages": [
                    {
                        "location": "gross",
                        "page": 1,
                        "excerpt": text,
                        "amount_fen": 600_000,
                        "recognition_period": month,
                    }
                ],
            },
        },
        evidence=(proof,),
        expected_revision=0,
        request_id=company.request(),
    )
    with company.engine.store.connection(read_only=True) as connection:
        fact = company.engine.store.current_fact(connection, subject)
        result = company.current(subject)
    old_resolution = MaterialResolutionPreReference(
        period=month,
        source_id=source["subject_id"],
        source_fact_id=source["fact_id"],
        location="gross",
        treatment="recognize",
        amount_fen=600_000,
        recognition_period=month,
        links=(
            {
                "subject_id": subject,
                "fact_kind": "payroll",
                "fact_id": fact.id,
                "calculation_id": result.id,
                "amount_field": "result.gross_fen",
                "amount_fen": 600_000,
                "recognition_period": month,
            },
        ),
    )
    company.engine.save_fact(
        old_resolution.kind,
        "resolution-" + month,
        old_resolution.model_dump(mode="json"),
        evidence=(proof,),
        expected_revision=0,
        request_id=company.request(),
    )
    company.materials["payroll"].append((month, proof))
