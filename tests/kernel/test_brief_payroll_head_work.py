"""Payroll state discovery keeps every adopted body proof with bounded counts."""

import json
from unittest.mock import patch

import pytest
from stage9_metrics import measure_work
from test_integrity_content import damage
from test_payroll import payroll, profile
from test_payroll_corrections import company as _company

from ai_accounting.kernel import dashboard as module
from ai_accounting.kernel import dashboard_reads
from ai_accounting.kernel import engine as engine_module
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard, _brief_workforce_cost, _Snapshot
from ai_accounting.kernel.query_reads import QueryReads

company = _company


def workforce(engine):
    with Dashboard(engine)._snapshot("2026-01") as snap:
        return _brief_workforce_cost(snap)


def line_count_work(work):
    return [row for row in work["sql"] if row["statement"].startswith(
        "SELECT c.id,json_array_length(c.outcome,'$.lines') line_count "
    )]


@pytest.mark.parametrize("zero", [False, True])
def test_only_unposted_payroll_states_read_line_counts_with_equal_amounts(company, zero):
    if zero:
        company.save(profile(social_insurance_participating=False,
                             social_insurance_base_fen=None), "profile", revision=1)
        company.save(payroll(accounting_gross_salary_fen=0, tax_reported_salary_fen=0),
                     "january", revision=1)
        company.confirm_payroll("january")
    company.publish("january")
    original = module.adopted_head_metadata
    original_line_counts = dashboard_reads._head_line_counts

    def full_counts(*args, **options):
        options.pop("read_line_counts", None)
        return original(*args, **options)

    def once_counted(snapshot, heads, **options):
        heads = list(heads)
        if heads and all(head.get("line_count") is not None for head in heads):
            return heads
        return original_line_counts(snapshot, heads, **options)

    # The control's complete head read already supplies state counts; avoid
    # charging it again for the optimized consumer's separate state phase.
    with patch.object(module, "adopted_head_metadata", full_counts), patch.object(
        dashboard_reads, "_head_line_counts", once_counted,
    ):
        before_work, before = measure_work(company.engine, lambda: workforce(company.engine))
    after_work, after = measure_work(company.engine, lambda: workforce(company.engine))
    assert after == before
    assert after["employee"]["has_activity"]
    assert after["employee"]["gross_salary_fen"] == (0 if zero else 1_000_000)
    assert line_count_work(before_work)
    assert sum(row.get("returned_rows", 0) for row in line_count_work(after_work)) == int(zero)
    # Whole adopted result content remains checked in both paths. This change
    # removes a redundant scalar query, not the body or source trust boundary.
    for counter in ("calculation_result_rows_loaded", "calculation_result_json_decodes"):
        assert after_work["counters"][counter] == before_work["counters"][counter]
    print(json.dumps({"zero": zero, "full_counts": before_work["counters"],
                      "state_counts": after_work["counters"]}))


@pytest.mark.parametrize("closed", [False, True])
def test_posted_payroll_no_impact_adopted_body_still_rejects_damage(company, monkeypatch, closed):
    company.publish("january")
    with monkeypatch.context() as scope:
        scope.setattr(engine_module, "PROGRAM_VERSION", "synthetic-brief-payroll-review")
        _, result = company.publish("january")
    reviewed = result["january"]
    assert reviewed["impact"] == "review_no_impact"
    if closed:
        company.close("2026-01")
    assert workforce(company.engine)["employee"]["gross_salary_fen"] == 1_000_000
    damage(company.engine, "calculation",
           "UPDATE calculation SET outcome=json_set(outcome,'$.values.gross_fen',7) WHERE id=?",
           (reviewed["calculation_id"],))
    for _ in range(2):
        with pytest.raises(KernelError):
            workforce(company.engine)


@pytest.mark.parametrize("reader", ["unowned", "fixed_v1"])
def test_payroll_state_line_counts_keep_full_reader_fallback(company, reader):
    company.publish("january")
    expected = workforce(company.engine)
    if reader == "fixed_v1":
        with historical_content(1):
            assert workforce(company.engine) == expected
    else:
        with company.engine.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            snap = _Snapshot(company.engine, connection, "2026-01",
                             reads=QueryReads(company.engine, connection))
            try:
                assert _brief_workforce_cost(snap) == expected
            finally:
                snap.release()
