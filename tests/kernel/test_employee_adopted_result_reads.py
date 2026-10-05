"""Employee head identity uses adopted leaves; full accounting retains vouchers."""

import pytest
from stage9_metrics import measure_work
from test_close_accounting_filter import _close_empty_months, _closed_charge
from test_engine import engine as engine_fixture
from test_integrity_content import damage

from ai_accounting.kernel import close_storage, close_storage_v1
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.query_reads import QueryReads

engine = engine_fixture


def close_rows(reads):
    periods = {row[0] for row in reads.connection.execute("SELECT period FROM period_close")}
    return reads.authoritative_close_rows(periods=periods)


def selected(engine, *, adopted_only):
    with QueryReads.snapshot(engine) as reads:
        rows = close_rows(reads)
        method = reads.close_adopted_results_many if adopted_only else reads.close_accounting_many
        return method(rows, subjects={"included", "unrelated"})


@pytest.mark.parametrize("months", [1, 3])
def test_adopted_leaves_match_full_proof_with_less_actual_payload(engine, months):
    _closed_charge(engine)
    if months == 3:
        _close_empty_months(engine, ["2026-02", "2026-03"])
    full_work, full = measure_work(engine, lambda: selected(engine, adopted_only=False))
    narrow_work, narrow = measure_work(engine, lambda: selected(engine, adopted_only=True))
    assert [part.adopted_results for part in narrow] == [part.adopted_results for part in full]
    assert full[0].vouchers
    assert not hasattr(narrow[0], "vouchers")
    for counter in ("returned_value_bytes", "stdlib_json_loads", "returned_rows"):
        assert narrow_work["counters"][counter] < full_work["counters"][counter]
    assert narrow_work["counters"]["adoption_accounting_rows"] == 1
    assert narrow_work["counters"]["adoption_accounting_slice_reads"] == months


def test_adopted_cache_does_not_stand_in_for_full_voucher_proof(engine, monkeypatch):
    _closed_charge(engine)
    fields = []
    original = close_storage._buckets_rows

    def observed(connection, header, subroot, field, buckets):
        fields.append(field)
        return original(connection, header, subroot, field, buckets)

    monkeypatch.setattr(close_storage, "_buckets_rows", observed)
    with QueryReads.snapshot(engine) as reads:
        rows = close_rows(reads)
        narrow = reads.close_adopted_results_many(rows, subjects={"included"})
        assert fields == ["adopted_results"]
        assert reads.close_adopted_results_many(rows, subjects={"included"}) == narrow
        assert fields == ["adopted_results"]
        assert not reads._close_accounting_slices
        full = reads.close_accounting_many(rows, subjects={"included"})
        assert fields[-1] == "vouchers"
        assert full[0].adopted_results == narrow[0].adopted_results
        assert full[0].vouchers


@pytest.mark.parametrize("change", ["missing_block", "bad_block", "missing_publication"])
def test_failed_adopted_batch_retains_no_successful_prefix(engine, change):
    period = _closed_charge(engine)
    _close_empty_months(engine, ["2026-02"])
    if change == "missing_publication":
        damage(
            engine,
            "calculation_publication",
            "DELETE FROM calculation_publication WHERE posting_period=?",
            (period,),
            foreign_keys=False,
        )
    else:
        statement = (
            "DELETE FROM close_storage_block WHERE period=? AND field='adopted_results'"
            if change == "missing_block"
            else "UPDATE close_storage_block SET content='[]' "
            "WHERE period=? AND field='adopted_results'"
        )
        damage(engine, "close_storage_block", statement, (period,))
    with QueryReads.snapshot(engine) as reads:
        rows = close_rows(reads)
        rows.reverse()  # A valid empty later close must not publish a successful prefix.
        with pytest.raises(KernelError) as failure:
            reads.close_adopted_results_many(rows, subjects={"included"})
        assert failure.value.code == "content_integrity_failed"
        assert not reads._close_adopted_result_slices
        assert not reads._verified_close_storage_parts
        assert not reads._close_accounting_positions
        with pytest.raises(KernelError):
            reads.close_adopted_results_many(rows, subjects={"included"})


def test_adopted_proof_leaves_full_voucher_corruption_rejection_intact(engine):
    period = _closed_charge(engine)
    damage(
        engine,
        "close_storage_block",
        "UPDATE close_storage_block SET content='[]' WHERE period=? AND field='vouchers'",
        (period,),
    )
    assert selected(engine, adopted_only=True)[0].adopted_results
    with pytest.raises(KernelError) as failure:
        selected(engine, adopted_only=False)
    assert failure.value.code == "content_integrity_failed"


def test_adopted_proof_rechecks_a_new_snapshot(engine):
    period = _closed_charge(engine)
    assert selected(engine, adopted_only=True)[0].adopted_results
    damage(
        engine,
        "close_storage_block",
        "UPDATE close_storage_block SET content='[]' WHERE period=? AND field='adopted_results'",
        (period,),
    )
    with pytest.raises(KernelError):
        selected(engine, adopted_only=True)


def test_fixed_v1_adopted_request_uses_original_complete_reader(engine, monkeypatch):
    _closed_charge(engine)
    original = close_storage_v1.read_accounting
    calls = []

    def observed(*args, **kwargs):
        calls.append(args[1].period)
        return original(*args, **kwargs)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("fixed v1 must retain its original complete accounting proof")

    monkeypatch.setattr(close_storage_v1, "read_accounting", observed)
    monkeypatch.setattr(close_storage, "read_adopted_results_many", forbidden)
    with historical_content(1):
        result = selected(engine, adopted_only=True)
    assert len(calls) == 1
    assert result[0].adopted_results and result[0].vouchers
