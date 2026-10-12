"""Month head discovery retains exact authority across history and reader scopes."""

from contextlib import contextmanager, nullcontext
from unittest.mock import patch

import pytest
from stage9_metrics import measure_work
from test_dashboard_model_transactions import raw_read
from test_employee_roster_scope import current_zero_company
from test_integrity_content import damage
from test_labor_accrual import accrual
from test_payroll import actual, contribution_policy, payroll, profile
from test_payroll_corrections import Company
from test_payroll_corrections import company as company
from test_settlement_late_reviews import prepared, review

from ai_accounting.kernel import dashboard_reads
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import LABOR_KINDS, PAYROLL_KINDS, Dashboard


@contextmanager
def unowned_candidate_scope():
    original = dashboard_reads._adopted_heads_sql

    def unowned(*args, **kwargs):
        # Exercise candidate construction without owned-snapshot eligibility.
        # The public consumers retain their real complete proofs afterwards.
        with patch.object(dashboard_reads, "_owns_current_selector_snapshot", return_value=False):
            return original(*args, **kwargs)

    with patch.object(dashboard_reads, "_adopted_heads_sql", unowned):
        yield


def heads(engine, period, *, kinds=PAYROLL_KINDS):
    with Dashboard(engine)._snapshot(period) as snap:
        return dashboard_reads.adopted_head_metadata(
            snap,
            kinds,
            posting_period=period,
            read_line_counts=False,
        )


def candidate_work(work):
    return [
        row
        for row in work["sql"]
        if "selection(month,posting_period) AS" in row["statement"]
        and row["statement"].endswith("WHERE h.rn=1")
    ]


def normalized_brief(engine, period):
    # This suite compares the real native candidate query under owned and
    # unowned snapshots. Persisted model-hit proofs have their own matrix.
    with raw_read():
        value = Dashboard(engine).brief(period)
    value["data"].pop("generated_at")
    return value


def test_public_month_heads_preserve_outputs_with_unpublished_history(company, record_property):
    company.publish("january", "february")
    measured = []
    for count in (0, 12, 36):
        if count:
            for index in range(count - (measured[-1][0] if measured else 0)):
                company.save(payroll(period="2025-12"), f"unpublished-{count}-{index}")
        actual_work, actual = measure_work(
            company.engine,
            lambda: normalized_brief(company.engine, "2026-02"),
        )
        with unowned_candidate_scope():
            unowned_work, expected = measure_work(
                company.engine,
                lambda: normalized_brief(company.engine, "2026-02"),
            )
        assert actual == expected
        owned, unowned = candidate_work(actual_work), candidate_work(unowned_work)
        assert (
            sum(row.get("returned_rows", 0) for row in owned)
            == sum(row.get("returned_rows", 0) for row in unowned)
            == 1
        )
        owned_vm = sum(row.get("sqlite_vm_steps", 0) for row in owned)
        unowned_vm = sum(row.get("sqlite_vm_steps", 0) for row in unowned)
        measured.append((count, owned_vm, unowned_vm))
        record_property(
            f"heads_{count}",
            {
                "owned_vm": owned_vm,
                "unowned_vm": unowned_vm,
                "required_rows": 1,
            },
        )


def test_public_closed_month_heads_keep_complete_proofs_with_history_growth(
    tmp_path,
    record_property,
):
    book = Company(tmp_path / "closed-months.sqlite")
    for fact, subject in (
        (
            profile(
                effective_to="2026-12",
                social_insurance_participating=False,
                social_insurance_base_fen=None,
            ),
            "profile",
        ),
        (contribution_policy(), "contributions"),
    ):
        book.save(fact, subject)
    measurements = []
    for index in range(6):
        period = f"2026-{index + 1:02d}"
        subject = f"wage-{index}"
        book.save(payroll(period=period, accounting_gross_salary_fen=0), subject)
        book.confirm_payroll(subject)
        book.tax_basis(subject, 0)
        book.publish(subject)
        book.close(period)
        if index not in (0, 5):
            continue
        owned_work, actual = measure_work(
            book.engine,
            lambda period=period: normalized_brief(book.engine, period),
        )
        with unowned_candidate_scope():
            unowned_work, expected = measure_work(
                book.engine,
                lambda period=period: normalized_brief(book.engine, period),
            )
        assert actual == expected
        owned, unowned = candidate_work(owned_work), candidate_work(unowned_work)
        assert sum(row.get("returned_rows", 0) for row in owned) == sum(
            row.get("returned_rows", 0) for row in unowned
        )
        measurements.append(
            (
                sum(row.get("sqlite_vm_steps", 0) for row in owned),
                sum(row.get("sqlite_vm_steps", 0) for row in unowned),
            )
        )
    record_property("closed_head_sql_vm", measurements)


@pytest.mark.parametrize("change", ["publication", "fact_reference", "calculation", "type"])
def test_month_frozen_fact_and_calculation_lanes_keep_damage_visible(tmp_path, change):
    company = prepared(tmp_path, zero=True)
    table, sql = {
        "publication": (
            "calculation_publication",
            "DELETE FROM calculation_publication WHERE subject_id='january'",
        ),
        "fact_reference": (
            "close_reference",
            "UPDATE close_reference SET reference_id='missing' "
            "WHERE path='adopted_results[*].fact_id'",
        ),
        "calculation": ("calculation", "DELETE FROM calculation WHERE subject_id='january'"),
        "type": (
            "close_reference",
            "UPDATE close_reference SET reference_type='damaged-type' "
            "WHERE path='adopted_results[*].calculation_id'",
        ),
    }[change]
    damage(company.engine, table, sql, foreign_keys=False)
    for unowned in (False, True):
        with unowned_candidate_scope() if unowned else nullcontext():
            if change == "publication":
                assert [row["subject_id"] for row in heads(company.engine, "2026-01")] == [
                    "january",
                ]
            with pytest.raises(KernelError):
                if change == "publication":
                    with raw_read():
                        Dashboard(company.engine).brief("2026-01")
                else:
                    heads(company.engine, "2026-01")


def test_earlier_missing_publication_current_is_not_hidden_by_month_scope(company):
    company.publish("january", "february")
    damage(
        company.engine,
        "calculation_publication",
        "DELETE FROM calculation_publication WHERE subject_id='january'",
        foreign_keys=False,
    )
    with pytest.raises(KernelError):
        heads(company.engine, "2026-02")
    with unowned_candidate_scope(), pytest.raises(KernelError):
        heads(company.engine, "2026-02")


def test_month_subject_kind_filter_retains_historical_calculation_kind(tmp_path, monkeypatch):
    company = current_zero_company(tmp_path)
    current = review(company, monkeypatch, 1)
    damage(company.engine, "subject", "UPDATE subject SET kind='expense' WHERE id='january'")
    damage(
        company.engine,
        "calculation",
        "UPDATE calculation SET kind='expense' WHERE id=?",
        (current,),
    )

    def raw_heads():
        with Dashboard(company.engine)._snapshot("2026-01") as snap:
            sql, parameters = dashboard_reads._adopted_heads_sql(
                snap,
                PAYROLL_KINDS,
                posting_period="2026-01",
            )
            return [
                tuple(row)
                for row in snap.connection.execute(
                    sql + "SELECT subject_id,calculation_id,kind FROM heads WHERE rn=1",
                    parameters,
                )
            ]

    actual = raw_heads()
    with unowned_candidate_scope():
        assert raw_heads() == actual
    assert actual == [("january", current, "expense")]


def test_month_closed_correction_preserves_original_ledger_and_posted_scope(company):
    company.publish("january", "february")
    company.close("2026-01")
    frozen = company.engine.ledger("2026-01")
    company.save(actual(), "actual")
    company.publish("actual", posting_period="2026-03")
    after = normalized_brief(company.engine, "2026-03")
    with unowned_candidate_scope():
        assert normalized_brief(company.engine, "2026-03") == after
    assert company.engine.ledger("2026-01") == frozen
    assert any(item["reverses_id"] for item in company.engine.ledger("2026-03"))


def test_month_labor_withdrawal_keeps_original_absence(tmp_path):
    company = Company(tmp_path / "withdrawn-labor.sqlite")
    company.save(accrual(), "labor")
    company.publish("labor")
    assert [
        row["subject_id"]
        for row in heads(
            company.engine,
            "2026-01",
            kinds=LABOR_KINDS,
        )
    ] == ["labor"]
    preview = company.engine.preview_delete("labor")
    company.engine.delete(
        "labor",
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id=company.request(),
    )
    assert heads(company.engine, "2026-01", kinds=LABOR_KINDS) == []
    with unowned_candidate_scope():
        assert heads(company.engine, "2026-01", kinds=LABOR_KINDS) == []
