"""Whole-month activity reuses proved rows without narrowing its source scope."""

import json

import pytest
import test_cash as cash
from stage9_metrics import measure_work
from test_integrity_content import damage

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_reads import Journal
from ai_accounting.kernel.types import canonical, digest

book = cash.book


@pytest.fixture
def paid_month(book):
    engine, save, publish, _ = book
    cash.fund(save, amount=1000)
    publish("funding")
    subjects = []
    for index in range(24):
        subject = f"expense-{index:02}"
        save("expense", subject, {
            "period": "2026-08", "counterparty_id": "supplier", "amount_fen": 1,
            "expense_class": "administration", "creditor_kind": "supplier",
        })
        subjects.append(subject)
    publish(*subjects)
    payments = []
    for index, subject in enumerate(subjects):
        payment = f"payment-{index:02}"
        cash.payment(save, amount=1, source_id=subject, subject=payment)
        payments.append(payment)
    publish(*payments)
    with engine.store.connection(read_only=True) as connection:
        sources = {
            row["subject_id"]: dict(row) for row in connection.execute(
                "SELECT c.*,p.id publication_id FROM calculation c "
                "JOIN calculation_publication p ON p.calculation_id=c.id WHERE c.kind='expense'"
            )
        }
    return engine, sources


def classify(engine, period, *, decoded=False):
    with Dashboard(engine)._snapshot(period) as snapshot:
        snapshot.month_journal.account_amounts()
        if decoded:
            identifiers = {
                row[0] for row in snapshot.connection.execute(
                    "SELECT id FROM calculation WHERE kind='expense'"
                )
            }
            snapshot.reads.verify_selected_content(identifiers)
        return snapshot.activity_classification


@pytest.mark.parametrize("decoded", [False, True])
def test_actual_month_work_removes_repeated_selection_and_settlement_expansion(
    paid_month, monkeypatch, record_property, decoded
):
    engine, _ = paid_month
    current_work, current = measure_work(
        engine, lambda: classify(engine, "2026-09", decoded=decoded)
    )
    with monkeypatch.context() as patch:
        patch.setattr(Journal, "verified_rows", lambda self: None)
        old_work, old = measure_work(engine, lambda: classify(engine, "2026-09", decoded=decoded))
    assert current == old
    assert current[1] == {
        ("financing_owner", "cash_funding"): 1, ("expense_supplier", "cash_payment"): 24,
    }
    for key in ("returned_rows", "returned_value_bytes", "sqlite_vm_steps"):
        assert current_work["counters"][key] < old_work["counters"][key]
        record_property(f"current_{key}", current_work["counters"][key])
        record_property(f"sql_fallback_{key}", old_work["counters"][key])
    assert current_work["counters"]["calculation_result_json_decodes"] == (
        old_work["counters"]["calculation_result_json_decodes"]
    )


@pytest.mark.parametrize("corruption", [
    "anchor", "both_authorities", "kind", "fact_binding", "fact_seal", "content_and_digest",
])
def test_default_brief_rejects_prior_month_source_outside_first_page(paid_month, corruption):
    engine, sources = paid_month
    dashboard = Dashboard(engine)
    baseline = dashboard.brief("2026-09")
    groups = baseline["data"]["collections"]["activity"]["items"]
    assert len(groups) == 2
    payment_group = next(group for group in groups if group["kind"] == "cash_payment")
    assert payment_group["member_count"] == 24
    members = dashboard.brief_group(
        "2026-09", section="activity", group_key=payment_group["group_key"],
        expected_version=baseline["snapshot_version"],
    )["data"]["collections"]["members"]
    assert len(members["items"]) == 20 and members["page"]["has_more"]
    loaded = {row["subject_id"] for row in members["items"]}
    index = next(index for index in range(24) if f"payment-{index:02}" not in loaded)
    assert baseline["data"]["funds_overview"]["total_fen"] == 976
    row = sources[f"expense-{index:02}"]
    if corruption in {"anchor", "both_authorities"}:
        damage(engine, "report_open_contribution_anchor",
               "DELETE FROM report_open_contribution_anchor WHERE publication_id=?",
               (row["publication_id"],), foreign_keys=False)
        if corruption == "both_authorities":
            damage(engine, "calculation_publication",
                   "DELETE FROM calculation_publication WHERE id=?",
                   (row["publication_id"],), foreign_keys=False)
    elif corruption == "kind":
        damage(engine, "calculation", "UPDATE calculation SET kind='employee_advance' WHERE id=?",
               (row["id"],))
    elif corruption == "fact_binding":
        damage(engine, "calculation", "UPDATE calculation SET fact_id=? WHERE id=?",
               (sources[f"expense-{(index + 1) % 24:02}"]["fact_id"], row["id"]))
    elif corruption == "fact_seal":
        damage(engine, "fact_seal", "DELETE FROM fact_seal WHERE fact_id=?", (row["fact_id"],),
               foreign_keys=False)
    else:
        outcome = json.loads(row["outcome"])
        outcome["values"]["creditor_kind"] = "employee"
        damage(engine, "calculation", "UPDATE calculation SET outcome=?,digest=? WHERE id=?",
               (canonical(outcome), digest(outcome), row["id"]))
    # A fresh request must re-prove the off-page direct source; a successful
    # earlier month/page read cannot mask the damage or turn it into "other".
    with pytest.raises(KernelError) as failure:
        Dashboard(engine).brief("2026-09")
    assert failure.value.code == "content_integrity_failed"
