"""Gross labor accrual cannot become a future net bank instruction without tax proof."""

import pytest
from test_exports import inventory
from test_exports import setup as setup

from ai_accounting.kernel.contracts import NeedsInformation
from ai_accounting.kernel.domains.labor_assets import LaborProjectCost
from ai_accounting.kernel.domains.payroll import LaborAccrual
from ai_accounting.kernel.domains.transactions import Allocation, Payment


def add_gross_labor(company, export, kind):
    fields = dict(
        period="2026-01",
        person_id="contractor",
        gross_fee_fen=100_000,
        tax_treatment="not_withheld_not_filed",
    )
    fact = (
        LaborAccrual(expense_class="service", **fields)
        if kind == "labor_accrual"
        else LaborProjectCost(
            project_id="accepted-project",
            capitalization_conditions_confirmed=True,
            **fields,
        )
    )
    company.save(fact, "gross-labor")
    company.publish("gross-labor")
    export.save_payee(
        "contractor",
        name="劳务人员",
        account="001234567891",
        evidence_digest=company.owner_confirmation,
        expected_revision=0,
        request_id=company.request(),
    )
    inventory(company)


@pytest.mark.parametrize("kind", ["labor_accrual", "labor_project_cost"])
@pytest.mark.parametrize("selected", [False, True])
def test_positive_unconfirmed_labor_blocks_complete_and_explicit_net_export(setup, kind, selected):
    company, export, template = setup
    add_gross_labor(company, export, kind)
    sources = ["gross-labor"] if selected else None
    before = {table: company.count(table) for table in ("voucher", "jobs", "audit")}
    with pytest.raises(NeedsInformation) as missing:
        export.preview("2026-01", template_evidence_digest=template, source_ids=sources)
    issue = missing.value.response()["fact_issues"][0]
    assert issue["field"] == "net_disbursement_tax"
    assert issue["semantics"] == "accounting"
    assert issue["allowed_precision"] == ["integer_fen"]
    assert "gross-labor" in issue["reusable_sources"]
    assert {table: company.count(table) for table in before} == before
    assert company.current("gross-labor", kind).values["gross_fen"] == 100_000


@pytest.mark.parametrize("kind", ["labor_accrual", "labor_project_cost"])
def test_actual_full_clearing_keeps_gross_fact_and_does_not_block_other_net_export(setup, kind):
    company, export, template = setup
    add_gross_labor(company, export, kind)
    original = company.current("gross-labor", kind)
    company.save(
        Payment(
            period="2026-01",
            actual_date="2026-01-31",
            direction="outflow",
            bank_account_id="company-bank",
            counterparty_id="contractor",
            amount_fen=100_000,
            allocations=(
                Allocation(
                    source_kind=kind, source_id="gross-labor", obligation="net", amount_fen=100_000
                ),
            ),
        ),
        "actual-clearing",
    )
    company.publish("actual-clearing")
    inventory(company)
    assert company.current("gross-labor", kind) == original
    assert original.values["tax_assessed"] is False
    with company.engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT amount FROM balance WHERE category='payable' AND balance_key=?",
                (kind + ":gross-labor:net",),
            ).fetchone()
            is None
        )
    preview = export.preview("2026-01", template_evidence_digest=template)
    assert preview["total_fen"] == company.current("january").values["net_fen"]
    assert {source["subject_id"] for row in preview["rows"] for source in row["sources"]} == {
        "january",
    }


@pytest.mark.parametrize("kind", ["labor_accrual", "labor_project_cost"])
def test_export_confirmation_rechecks_new_unconfirmed_labor_without_queueing_a_job(
    setup,
    tmp_path,
    kind,
):
    company, export, template = setup
    ready = export.preview("2026-01", template_evidence_digest=template)
    add_gross_labor(company, export, kind)
    before = {table: company.count(table) for table in ("jobs", "audit", "voucher")}
    with pytest.raises(NeedsInformation):
        export.confirm(
            "2026-01",
            template_evidence_digest=template,
            preview_digest=ready["digest"],
            epochs=ready["epochs"],
            output_directory=str(tmp_path / "net-export"),
            request_id=company.request(),
        )
    assert {table: company.count(table) for table in before} == before
    assert not (tmp_path / "net-export").exists()


@pytest.mark.parametrize("kind", ["labor_accrual", "labor_project_cost"])
def test_archived_missing_tax_marker_is_not_confirmed_net_but_explicit_proof_is_retained(kind):
    from ai_accounting.kernel.exports import _require_labor_net_tax

    old_gross_values = {"gross_fen": 100_000, "net_fen": 100_000, "tax_fen": 0}
    with pytest.raises(NeedsInformation):
        _require_labor_net_tax(kind, old_gross_values, 100_000, "archived-source")
    # A settled historical claim is not a proposed payment. An explicit retained
    # confirmation may be used; absence of a marker cannot establish one.
    _require_labor_net_tax(kind, old_gross_values, 0, "archived-source")
    _require_labor_net_tax(
        kind, old_gross_values | {"tax_assessed": True}, 100_000, "archived-source"
    )
    with pytest.raises(NeedsInformation):
        _require_labor_net_tax(
            kind,
            old_gross_values
            | {
                "tax_assessed": True,
                "tax_review_required": True,
            },
            100_000,
            "archived-source",
        )
