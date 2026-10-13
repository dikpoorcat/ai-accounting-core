"""Gross labor accrual cannot become a future net bank instruction without tax proof."""

import pytest
from test_exports import inventory
from test_exports import setup as setup

from ai_accounting.kernel.contracts import Context, NeedsInformation
from ai_accounting.kernel.domains.labor_assets import LaborProjectCost
from ai_accounting.kernel.domains.payroll import LaborAccrual
from ai_accounting.kernel.domains.transactions import Allocation, Payment
from ai_accounting.kernel.labor_result_presentation import labor_withholding_presentation


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


@pytest.mark.parametrize("kind", ["labor_accrual", "labor_project_cost"])
@pytest.mark.parametrize("tax", [0, 8_000])
def test_confirmed_accrual_tax_reuses_cost_voucher_and_exports_only_unpaid_net(setup, kind, tax):
    company, export, template = setup
    add_gross_labor(company, export, kind)
    original = company.current("gross-labor", kind)
    before_vouchers = company.count("voucher")
    with company.engine.store.connection(read_only=True) as connection:
        number = connection.execute(
            "SELECT number FROM voucher WHERE id='v:gross-labor'"
        ).fetchone()[0]
    saved = company.tax_basis("gross-labor", tax)
    assert saved["pending"] == ["gross-labor"]
    _, published = company.publish("gross-labor")
    current = company.current("gross-labor", kind)
    assert current.fact_id == original.fact_id
    assert published["gross-labor"]["voucher_number"] == number
    assert company.count("voucher") == before_vouchers
    assert (current.values["gross_fen"], current.values["net_fen"], current.values["tax_fen"]) == (
        100_000, 100_000 - tax, tax,
    )
    assert {item["name"]: item["amount_fen"] for item in current.values["obligations"]} == {
        "net": 100_000 - tax, "tax": tax,
    }
    assert current.values["tax_assessed"] is True
    assert current.values["tax_review_required"] is False
    assert current.values["tax_basis"]["cash_payment_recorded"] is False
    presentation = labor_withholding_presentation(
        current.values, LaborAccrual if kind == "labor_accrual" else LaborProjectCost,
        fact_id=current.fact_id, subject_id="gross-labor", period="2026-01",
    )
    assert presentation == {
        "withholding_method": "confirmed_tax_basis",
        "withholding_label": "按最终报酬表确认扣税金额",
    }
    inventory(company)
    preview = export.preview(
        "2026-01", template_evidence_digest=template, source_ids=["gross-labor"],
    )
    assert preview["total_fen"] == 100_000 - tax
    assert [source["subject_id"] for row in preview["rows"] for source in row["sources"]] == [
        "gross-labor",
    ]
    company.save(
        Payment(
            period="2026-01", actual_date="2026-01-31", direction="outflow",
            bank_account_id="company-bank", counterparty_id="contractor",
            amount_fen=100_000 - tax,
            allocations=(Allocation(
                source_kind=kind, source_id="gross-labor", obligation="net",
                amount_fen=100_000 - tax,
            ),),
        ),
        "actual-net-payment",
    )
    company.publish("actual-net-payment")
    with company.engine.store.connection(read_only=True) as connection:
        remaining = {row[0]: row[1] for row in connection.execute(
            "SELECT balance_key,amount FROM balance WHERE category='payable' "
            "AND balance_key IN (?,?)",
            (kind + ":gross-labor:net", kind + ":gross-labor:tax"),
        )}
        assert remaining.get(kind + ":gross-labor:net", 0) == 0
        assert remaining.get(kind + ":gross-labor:tax", 0) == tax
        cost_account = "540104" if kind == "labor_accrual" else "189901"
        assert connection.execute(
            "SELECT sum(l.debit) FROM voucher_current c JOIN voucher_line l "
            "ON l.version_id=c.version_id WHERE l.account=?", (cost_account,),
        ).fetchone()[0] == 100_000


@pytest.mark.parametrize("kind", ["labor_accrual", "labor_project_cost"])
def test_first_tax_basis_invalidates_gross_calculation_without_saved_tax_reads_and_descendants(
    setup, kind, monkeypatch,
):
    company, export, template = setup
    registry = company.engine.store.registry
    calculator = registry.evaluators[kind]
    # Publish the old program's result without recording a tax-scope dependency.
    def gross_calculator(version, context):
        detached = Context({read: () for read in version.fact.reads_for(version.subject_id)})
        return calculator(version, detached)

    with monkeypatch.context() as previous_program:
        previous_program.setitem(registry.evaluators, kind, gross_calculator)
        add_gross_labor(company, export, kind)
    original = company.current("gross-labor", kind)
    with company.engine.store.connection(read_only=True) as connection:
        assert connection.execute(
            "SELECT count(*) FROM dependency_scope WHERE calculation_id=? "
            "AND kind='remuneration_tax_basis'", (original.id,),
        ).fetchone()[0] == 0
    company.save(
        Payment(
            period="2026-01", actual_date="2026-01-31", direction="outflow",
            bank_account_id="company-bank", counterparty_id="contractor", amount_fen=10_000,
            allocations=(Allocation(
                source_kind=kind, source_id="gross-labor", obligation="net", amount_fen=10_000,
            ),),
        ),
        "partial-payment",
    )
    company.publish("partial-payment")
    saved = company.tax_basis("gross-labor", 8_000)
    assert set(saved["pending"]) == company.pending() == {"gross-labor", "partial-payment"}
    company.publish("gross-labor")
    assert company.pending() == set()
    inventory(company)
    preview = export.preview(
        "2026-01", template_evidence_digest=template, source_ids=["gross-labor"],
    )
    assert preview["total_fen"] == 82_000
    # Later changes use the newly saved dependency and continue to include the payment.
    changed = company.tax_basis("gross-labor", 0, revision=1)
    assert set(changed["pending"]) == {"gross-labor", "partial-payment"}


@pytest.mark.parametrize("kind", ["labor_accrual", "labor_project_cost"])
def test_changed_accrual_amount_cannot_reuse_stale_deduction_or_replace_voucher(setup, kind):
    company, export, _template = setup
    add_gross_labor(company, export, kind)
    company.tax_basis("gross-labor", 8_000)
    company.publish("gross-labor")
    adopted = company.current("gross-labor", kind)
    with company.engine.store.connection(read_only=True) as connection:
        fact = company.engine.store.current_fact(connection, "gross-labor").fact
    company.save(type(fact)(**(fact.model_dump(mode="json") | {"gross_fee_fen": 120_000})),
                 "gross-labor", revision=1)
    before = {table: company.count(table) for table in ("calculation", "voucher_version", "audit")}
    with pytest.raises(NeedsInformation) as missing:
        company.publish("gross-labor")
    assert missing.value.issues[0]["field"] == "remuneration_tax_basis.source_fact_id"
    assert company.current("gross-labor", kind) == adopted
    assert {table: company.count(table) for table in before} == before
