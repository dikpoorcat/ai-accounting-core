"""Per-month adopted scopes keep one batch and independent publication authority."""

import pytest
from stage9_metrics import measure_work
from test_close_accounting_filter import _close_empty_months, _closed_charge
from test_employee_adopted_result_reads import close_rows
from test_engine import engine as engine_fixture
from test_engine import publish, save
from test_integrity_content import damage

from ai_accounting.kernel import close_storage, close_storage_v1
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.types import YearMonth

engine = engine_fixture


def prepare(engine):
    january = _closed_charge(engine)
    save(engine, subject="february", period="2026-02", request="february")
    publish(engine, ["february"], request="publish-february")
    _close_empty_months(engine, ["2026-02", "2026-03"])
    return {january: {"included"}, january + 1: {"february"}, january + 2: set()}


def selected(engine, scopes, *, narrow):
    subjects = set().union(*scopes.values())
    with QueryReads.snapshot(engine) as reads:
        return reads.close_adopted_results_many(
            close_rows(reads), subjects=subjects,
            **({"subjects_by_period": scopes} if narrow else {}),
        )


def test_period_scopes_preserve_leaves_without_repeated_month_authority(engine, monkeypatch):
    scopes = prepare(engine)
    calls = []
    original = close_storage._may_contain_with_positions

    def observed(*args):
        calls.append(args[1])
        return original(*args)

    monkeypatch.setattr(close_storage, "_may_contain_with_positions", observed)
    full_work, full = measure_work(engine, lambda: selected(engine, scopes, narrow=False))
    assert len(calls) == 6
    calls.clear()
    narrow_work, narrow = measure_work(engine, lambda: selected(engine, scopes, narrow=True))
    assert len(calls) == 2
    assert [part.adopted_results for part in narrow] == [part.adopted_results for part in full]
    assert [part.subjects for part in narrow] == [frozenset(scopes[p.period]) for p in narrow]
    # The new scope must not reissue authority selections once per close.
    assert narrow_work["counters"]["sql_calls"] <= full_work["counters"]["sql_calls"]
    assert narrow_work["counters"]["adoption_accounting_rows"] == 2


@pytest.mark.parametrize("change", ["missing_publication", "missing_block", "bad_block"])
def test_exact_period_scope_failed_batch_never_caches_valid_prefix(engine, change):
    scopes = prepare(engine)
    period = YearMonth("2026-01").ordinal
    if change == "missing_publication":
        damage(engine, "calculation_publication",
               "DELETE FROM calculation_publication WHERE posting_period=?", (period,),
               foreign_keys=False)
    else:
        statement = (
            "DELETE FROM close_storage_block WHERE period=? AND field='adopted_results'"
            if change == "missing_block" else
            "UPDATE close_storage_block SET content='[]' "
            "WHERE period=? AND field='adopted_results'"
        )
        damage(engine, "close_storage_block", statement, (period,))
    with QueryReads.snapshot(engine) as reads:
        rows = list(reversed(close_rows(reads)))
        for _ in range(2):
            with pytest.raises(KernelError) as failure:
                reads.close_adopted_results_many(
                    rows, subjects={"included", "february"}, subjects_by_period=scopes,
                )
            assert failure.value.code == "content_integrity_failed"
            assert not reads._close_adopted_result_slices
            assert not reads._verified_close_storage_parts
            assert not reads._close_accounting_positions


def test_period_cache_keys_do_not_replace_wider_scope_or_new_transaction(engine, monkeypatch):
    scopes = prepare(engine)
    original = close_storage.read_adopted_results_many
    calls = []

    def observed(*args, **kwargs):
        calls.append(kwargs.get("subjects_by_period"))
        return original(*args, **kwargs)

    monkeypatch.setattr(close_storage, "read_adopted_results_many", observed)
    with QueryReads.snapshot(engine) as reads:
        rows = close_rows(reads)
        small = reads.close_adopted_results_many(
            rows, subjects={"included", "february"}, subjects_by_period=scopes,
        )
        assert reads.close_adopted_results_many(
            rows, subjects={"included", "february"}, subjects_by_period=scopes,
        ) == small
        assert len(calls) == 1
        wider = reads.close_adopted_results_many(rows, subjects={"included", "february"})
        assert len(calls) == 2
        assert all(part.subjects == {"included", "february"} for part in wider)
        assert not reads._close_accounting_slices
    assert selected(engine, scopes, narrow=True) == small
    assert len(calls) == 3


def test_fixed_v1_period_scope_retains_union_complete_proof(engine, monkeypatch):
    scopes = prepare(engine)
    original = close_storage_v1.read_accounting
    calls = []

    def observed(connection, header, subjects, **kwargs):
        calls.append(frozenset(subjects))
        return original(connection, header, subjects, **kwargs)

    monkeypatch.setattr(close_storage_v1, "read_accounting", observed)
    with historical_content(1):
        result = selected(engine, scopes, narrow=True)
    assert calls == [frozenset({"included", "february"})] * 3
    assert result[0].vouchers and result[1].vouchers


@pytest.mark.parametrize("scopes", [{}, {123: {"included"}}])
def test_period_scope_cannot_silently_omit_requested_close(engine, scopes):
    _closed_charge(engine)
    with QueryReads.snapshot(engine) as reads, pytest.raises(ValueError):
        reads.close_adopted_results_many(
            close_rows(reads), subjects={"included"}, subjects_by_period=scopes,
        )
