from __future__ import annotations

import sqlite3
from typing import ClassVar

import pytest

from ai_accounting.kernel.contracts import Context, Fact, KernelError, Line, Outcome, Read, Registry
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.types import PositiveFen, YearMonth


class Source(Fact):
    kind: ClassVar[str] = "test_source"
    amount: PositiveFen


class Charge(Fact):
    kind: ClassVar[str] = "test_charge"
    amount: PositiveFen
    suppress_posting: bool = False

    def reads(self):
        return (Read("fact", "test_source", str(self.period)),)


def calculate(version, context: Context):
    sources = context.facts("test_source", str(version.fact.period))
    amount = version.fact.amount + sum(v.fact.amount for v in sources)
    if version.fact.suppress_posting:
        return Outcome((), {"amount": amount, "suppressed": True})
    return Outcome((Line("5602", debit=amount), Line("2202", credit=amount)), {"amount": amount})


@pytest.fixture
def engine(tmp_path):
    registry = Registry()
    registry.register(Source)
    registry.register(Charge, calculate)
    return Engine(
        Store.create(
            tmp_path / "company.sqlite", registry, "company-a", "91310000123456789A", "db-a"
        )
    )


def evidence(engine):
    return engine.register_evidence(
        b"business evidence", "text/plain", "evidence", request_id="evidence"
    )["digest"]


def save(
    engine,
    subject="charge",
    kind="test_charge",
    amount=100,
    revision=0,
    request="save",
    period="2026-01",
):
    return engine.save_fact(
        kind,
        subject,
        {"period": period, "amount": amount},
        evidence=(evidence(engine),),
        expected_revision=revision,
        request_id=request,
    )


def publish(engine, subjects=None, request="publish", correction_period=None):
    subjects = subjects or ["charge"]
    preview = engine.preview(subjects, correction_period=correction_period)
    result = engine.confirm(
        subjects,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id=request,
        correction_period=correction_period,
    )
    return preview, result


def test_fact_versions_empty_dependencies_and_idempotency(engine):
    save(engine)
    preview, first = publish(engine)
    assert (
        engine.confirm(
            ["charge"],
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id="publish",
        )
        == first
    )
    assert engine.overview("2026-01")["accounts"][0]["credit"] == 100
    saved = save(engine, "source", "test_source", 25, request="source")
    assert saved["pending"] == ["charge"]
    _, second = publish(engine, request="correction")
    assert first["results"][0]["voucher_number"] == second["results"][0]["voucher_number"]
    assert engine.overview("2026-01")["accounts"][0]["credit"] == 125
    assert engine.overview("2026-01")["pending"] == []
    assert (
        engine.trace(first["results"][0]["calculation_id"])["calculation"]["outcome"]["values"][
            "amount"
        ]
        == 100
    )


@pytest.mark.parametrize("stage", ["begin", "lines", "calculation", "published", "commit"])
def test_failure_is_atomic(engine, stage):
    save(engine)
    preview = engine.preview(["charge"])

    def fail(current, connection):
        if current == stage:
            raise RuntimeError("injected failure")

    engine.fault = fail
    with pytest.raises(RuntimeError):
        engine.confirm(
            ["charge"],
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id="failed",
        )
    with engine.store.connection(read_only=True) as connection:
        for table in (
            "calculation",
            "voucher",
            "voucher_line",
            "voucher_version",
            "monthly_account",
        ):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        assert connection.execute("SELECT 1 FROM request WHERE id='failed'").fetchone() is None
    engine.fault = lambda stage, connection: None
    publish(engine)


def test_commit_failure_rolls_back_deferred_orphan(engine):
    save(engine)

    def fail(stage, connection):
        if stage == "commit":
            connection.execute("INSERT INTO voucher_line VALUES('orphan',1,'1002',1,0,NULL)")

    engine.fault = fail
    with pytest.raises(sqlite3.IntegrityError):
        publish(engine)
    with engine.store.connection() as connection:
        assert connection.execute("SELECT count(*) FROM voucher_line").fetchone()[0] == 0


def test_preview_expiration_lanes(engine):
    save(engine)
    preview = engine.preview(["charge"])
    Periods(engine).management(
        "charge",
        note="说明后补",
        payment_period=None,
        payment_category=None,
        expected_revision=0,
        request_id="management",
    )
    engine.confirm(
        ["charge"], preview_digest=preview["digest"], epochs=preview["epochs"], request_id="publish"
    )
    preview = engine.preview(["charge"])
    save(engine, "source", "test_source", 10, request="source")
    with pytest.raises(KernelError, match="预览"):
        engine.confirm(
            ["charge"],
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id="stale",
        )


def close(engine, period="2026-01"):
    periods = Periods(engine)
    ev = evidence(engine)
    for category in MATERIAL_CATEGORIES:
        periods.inventory(
            period,
            category,
            evidence=[],
            expected=0,
            no_business=True,
            confirmation_evidence=ev,
            request_id="inventory-" + category,
        )
    preview = periods.preview_close(period, owner_confirmation=ev)
    periods.close(
        period,
        owner_confirmation=ev,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="close",
    )
    return periods.closed_report(period)


def test_closed_reversal_frozen_history_and_projection_rebuild(engine):
    save(engine)
    publish(engine)
    before = close(engine)
    save(engine, amount=125, revision=1, request="change")
    with pytest.raises(KernelError, match="冲正"):
        publish(engine, request="bad-correction")
    publish(engine, request="closed-correction", correction_period="2026-02")
    assert Periods(engine).closed_report("2026-01") == before
    lines = engine.ledger("2026-02")
    assert len(lines) == 2 and sum(row["reverses_id"] is not None for row in lines) == 1
    snapshots = [engine.overview(period)["accounts"] for period in ("2026-01", "2026-02")]
    engine.rebuild_projections(request_id="rebuild")
    assert [engine.overview(period)["accounts"] for period in ("2026-01", "2026-02")] == snapshots


def test_database_seal_and_replace_protection(engine):
    save(engine)
    publish(engine)
    with engine.store.connection() as connection:
        row = connection.execute("SELECT * FROM voucher_line LIMIT 1").fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="sealed"):
            connection.execute(
                "INSERT INTO voucher_line VALUES(?,99,'1002',1,0,NULL)", (row["version_id"],)
            )
        for sql in (
            "UPDATE voucher_line SET debit=2",
            "DELETE FROM voucher_line",
            "INSERT OR REPLACE INTO voucher_line SELECT * FROM voucher_line",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(sql)
        with pytest.raises(sqlite3.IntegrityError, match="unbalanced"):
            connection.execute(
                "INSERT INTO voucher_version SELECT "
                "'empty',voucher_id,calculation_id,period,NULL,1 FROM "
                "voucher_version LIMIT 1"
            )


def test_closed_correction_can_pass_through_zero_and_be_corrected_again(engine):
    save(engine)
    publish(engine)
    close(engine)
    engine.save_fact(
        "test_charge",
        "charge",
        {"period": "2026-01", "amount": 100, "suppress_posting": True},
        evidence=(evidence(engine),),
        expected_revision=1,
        request_id="zero",
    )
    publish(engine, request="zero-result", correction_period="2026-02")
    save(engine, amount=50, revision=2, request="nonzero")
    _, restored = publish(engine, request="restore-result")
    assert len(engine.ledger("2026-01")) == 1
    assert len(engine.ledger("2026-02")) == 2
    save(engine, amount=75, revision=3, request="again")
    _, again = publish(engine, request="again-result")
    assert restored["results"][0]["voucher_number"] == again["results"][0]["voucher_number"]


def test_fact_and_calculation_relationships_seal(engine):
    saved = save(engine)
    _, result = publish(engine)
    calc = result["results"][0]["calculation_id"]
    ev = engine.register_evidence(
        b"late evidence", "text/plain", "late", request_id="late-evidence"
    )["digest"]
    with engine.store.connection() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="sealed"):
            connection.execute(
                "INSERT INTO fact_evidence VALUES(?,?)", (saved["fact_id"], bytes.fromhex(ev))
            )
        with pytest.raises(sqlite3.IntegrityError, match="sealed"):
            connection.execute(
                "INSERT INTO dependency_scope VALUES(?,'fact','test_source','other',119988)",
                (calc,),
            )


def test_delete_preserves_history_and_number(engine):
    save(engine)
    _, result = publish(engine)
    preview = engine.preview_delete("charge")
    engine.delete(
        "charge", preview_digest=preview["digest"], epochs=preview["epochs"], request_id="delete"
    )
    assert engine.ledger("2026-01") == []
    assert (
        engine.trace(result["results"][0]["calculation_id"])["calculation"]["outcome"]["values"][
            "amount"
        ]
        == 100
    )
    with pytest.raises(KernelError, match="新身份"):
        save(engine, request="revive")


def test_strict_money_month_and_company_binding(engine):
    for bad in (True, 1.0, 2**63):
        with pytest.raises(KernelError):
            save(engine, amount=bad)
    for month in ("2026-13", "0000-01", "2026-1"):
        with pytest.raises(ValueError):
            YearMonth(month)
    other = Store(engine.store.path, engine.store.registry, "other", "db-a")
    with pytest.raises(KernelError, match="company"):
        with other.connection():
            pass


def test_no_business_cannot_hide_received_materials(engine):
    ev = evidence(engine)
    periods = Periods(engine)
    periods.inventory(
        "2026-01",
        "bank",
        evidence=[ev],
        expected=1,
        no_business=False,
        confirmation_evidence=ev,
        request_id="received",
    )
    with pytest.raises(KernelError, match="撤去"):
        periods.inventory(
            "2026-01",
            "bank",
            evidence=[],
            expected=0,
            no_business=True,
            confirmation_evidence=ev,
            request_id="hide",
        )
