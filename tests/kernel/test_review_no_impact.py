"""Review new evidence without rewriting an unchanged accounting result."""

import pytest
from test_engine import close, publish, save
from test_engine import engine as engine_fixture

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.periods import Periods

engine = engine_fixture


def test_open_review_keeps_sealed_voucher_and_refreshes_dependencies(engine):
    save(engine)
    _, original = publish(engine)
    with engine.store.connection(read_only=True) as connection:
        voucher = tuple(connection.execute("SELECT * FROM voucher_current").fetchone())
    revised = save(engine, revision=1, request="new-evidence")
    _, reviewed = publish(engine, request="review")
    assert reviewed["results"][0]["voucher_number"] == original["results"][0]["voucher_number"]
    assert reviewed["results"][0]["calculation_id"] != original["results"][0]["calculation_id"]
    with engine.store.connection(read_only=True) as connection:
        assert tuple(connection.execute("SELECT * FROM voucher_current").fetchone()) == voucher
        assert connection.execute("SELECT count(*) FROM voucher_version").fetchone()[0] == 1
        assert connection.execute(
            "SELECT action FROM disposition WHERE cause_id=?", (revised["fact_id"],)
        ).fetchone()[0] == "review_no_impact"
    assert engine.overview("2026-01")["pending"] == []
    save(engine, amount=150, revision=2, request="actual-change")
    _, corrected = publish(engine, request="correct-after-review")
    assert corrected["results"][0]["voucher_number"] == original["results"][0]["voucher_number"]
    assert engine.overview("2026-01")["accounts"][0]["credit"] == 150


def test_closed_review_needs_no_reversal_but_later_real_change_reverses_original(engine):
    save(engine)
    save(engine, "source", "test_source", 25, request="source")
    _, original = publish(engine)
    frozen = close(engine)
    source = save(engine, "source", "test_source", 25, revision=1, request="source-reviewed")
    _, reviewed = publish(engine, ["source"], request="review-closed")
    reviewed_id = reviewed["results"][0]["calculation_id"]
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM voucher_version").fetchone()[0] == 1
        assert connection.execute(
            "SELECT 1 FROM dependency_fact WHERE calculation_id=? AND fact_id=?",
            (reviewed_id, source["fact_id"]),
        ).fetchone()
        assert connection.execute(
            "SELECT action FROM disposition WHERE cause_id=?", (source["fact_id"],)
        ).fetchone()[0] == "review_no_impact"
    assert reviewed["results"][0]["voucher_number"] == original["results"][0]["voucher_number"]
    assert Periods(engine).closed_report("2026-01") == frozen
    assert engine.overview("2026-01")["pending"] == []

    save(engine, "source", "test_source", 50, revision=2, request="source-changed")
    with pytest.raises(KernelError) as failure:
        publish(engine, ["source"], request="missing-correction-period")
    assert failure.value.code == "closed_correction_required"
    publish(engine, ["source"], request="closed-change", correction_period="2026-02")
    with engine.store.connection(read_only=True) as connection:
        vouchers = connection.execute(
            "SELECT total,reverses_id FROM voucher_version ORDER BY rowid"
        ).fetchall()
        assert [row["total"] for row in vouchers] == [125, 125, 150]
        assert vouchers[1]["reverses_id"] is not None
    assert Periods(engine).closed_report("2026-01") == frozen
    before = engine.overview("2026-02")
    engine.rebuild_projections(request_id="rebuild-reviewed")
    assert engine.overview("2026-02") == before


def test_same_amount_in_a_different_month_is_an_accounting_change(engine):
    save(engine)
    _, original = publish(engine)
    save(engine, period="2026-02", revision=1, request="correct-month")
    _, corrected = publish(engine, request="publish-correct-month")
    assert corrected["results"][0]["voucher_number"] == original["results"][0]["voucher_number"]
    assert engine.overview("2026-01")["accounts"] == []
    assert engine.overview("2026-02")["accounts"][0]["credit"] == 100
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute(
            "SELECT action FROM disposition ORDER BY id DESC LIMIT 1"
        ).fetchone()[0] == "recalculated"
