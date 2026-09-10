"""Independent small-graph audits of the generic SQLite kernel invariants."""

from __future__ import annotations

import sqlite3
from typing import ClassVar, Literal

import pytest
from pydantic import BaseModel, ConfigDict

from ai_accounting.kernel.contracts import (
    BalanceEffect,
    Fact,
    KernelError,
    Line,
    Outcome,
    Read,
    Registry,
)
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.types import Fen, YearMonth, digest


class AuditSource(Fact):
    kind: ClassVar[str] = "audit_source"
    amount: Fen
    bucket: str

    def scopes(self):
        return (self.bucket,)


class AuditEntry(Fact):
    kind: ClassVar[str] = "audit_entry"
    amount: Fen
    bucket: str
    expense_class: Literal["management", "selling"]
    use_source: bool

    def reads(self):
        return (Read("fact", "audit_source", self.bucket),) if self.use_source else ()


class AuditFollow(Fact):
    kind: ClassVar[str] = "audit_follow"
    upstream: str

    def reads(self):
        return (Read("calculation", "audit_entry", "@" + self.upstream),)


class AuditItem(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    label: str
    amount: Fen


class AuditBatch(Fact):
    kind: ClassVar[str] = "audit_batch"
    items: tuple[AuditItem, ...]


def entry_calculation(version, context):
    fact = version.fact
    amount = fact.amount
    if fact.use_source:
        amount += sum(item.fact.amount for item in context.facts("audit_source", fact.bucket))
    expense = {"management": "5602", "selling": "5601"}[fact.expense_class]
    lines = (
        (
            Line(expense, debit=amount),
            Line("1002", credit=amount, cashflow="operating_expense"),
        )
        if amount
        else ()
    )
    return Outcome(lines, {"amount": amount}, (BalanceEffect("owner", amount),) if amount else ())


def follow_calculation(version, context):
    selected = context.calculations("audit_entry", "@" + version.fact.upstream)
    amount = sum(item.values["amount"] for item in selected)
    return Outcome(
        (Line("5602", debit=amount), Line("2202", credit=amount)) if amount else (),
        {"amount": amount},
    )


@pytest.fixture
def audit(tmp_path):
    registry = Registry()
    registry.register(AuditSource)
    registry.register(AuditEntry, entry_calculation)
    registry.register(AuditFollow, follow_calculation)
    registry.register(AuditBatch)
    engine = Engine(
        Store.create(tmp_path / "audit.sqlite", registry, "audit", "91310000123456789A", "audit-db")
    )
    proof = engine.register_evidence(
        b"isolated audit fixture", "text/plain", "proof", request_id="proof"
    )["digest"]
    return engine, proof


def save_entry(
    audit,
    *,
    subject="entry",
    amount=100,
    revision=0,
    period="2026-01",
    expense="management",
    use_source=False,
):
    engine, proof = audit
    return engine.save_fact(
        "audit_entry",
        subject,
        {
            "period": period,
            "amount": amount,
            "bucket": "owner",
            "expense_class": expense,
            "use_source": use_source,
        },
        evidence=(proof,),
        expected_revision=revision,
        request_id=f"{subject}-fact-{revision}",
    )


def publish(engine, subjects=("entry",), *, request="publish", correction_period=None):
    preview = engine.preview(list(subjects), correction_period=correction_period)
    result = engine.confirm(
        list(subjects),
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id=request,
        correction_period=correction_period,
    )
    return preview, result


def inventory(audit, period):
    engine, proof = audit
    for category in MATERIAL_CATEGORIES:
        Periods(engine).inventory(
            period,
            category,
            evidence=[],
            expected=0,
            no_business=True,
            confirmation_evidence=proof,
            request_id=f"inventory-{period}-{category}",
        )


def close(audit, period):
    engine, proof = audit
    inventory(audit, period)
    periods = Periods(engine)
    preview = periods.preview_close(period, owner_confirmation=proof)
    periods.close(
        period,
        owner_confirmation=proof,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id=f"close-{period}",
    )
    return periods.closed_report(period)


def projections(engine):
    with engine.store.connection(read_only=True) as connection:
        return {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1,2")]
            for table in ("monthly_account", "monthly_cashflow", "balance")
        }


def test_incremental_projections_equal_rebuild_after_account_switch_zero_and_delete(audit):
    engine, _ = audit
    for revision, amount, expense in (
        (0, 100, "management"),
        (1, 125, "selling"),
        (2, 0, "selling"),
        (3, 80, "management"),
    ):
        save_entry(audit, amount=amount, revision=revision, expense=expense)
        publish(engine, request=f"publish-{revision}")
        before = projections(engine)
        engine.rebuild_projections(request_id=f"rebuild-{revision}")
        assert projections(engine) == before
    preview = engine.preview_delete("entry")
    engine.delete(
        "entry", preview_digest=preview["digest"], epochs=preview["epochs"], request_id="delete"
    )
    before = projections(engine)
    engine.rebuild_projections(request_id="rebuild-after-delete")
    assert projections(engine) == before


def test_old_dependency_history_does_not_block_source_deletion(audit):
    engine, proof = audit
    source = engine.save_fact(
        "audit_source",
        "source",
        {"period": "2026-01", "amount": 25, "bucket": "owner"},
        evidence=(proof,),
        expected_revision=0,
        request_id="source",
    )
    save_entry(audit, use_source=True)
    _, old = publish(engine)
    with pytest.raises(KernelError) as error:
        engine.preview_delete("source")
    assert error.value.code == "has_dependents"
    save_entry(audit, revision=1, use_source=False)
    publish(engine, request="detach")
    preview = engine.preview_delete("source")
    engine.delete(
        "source",
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="delete-source",
    )
    old_trace = engine.trace(old["results"][0]["calculation_id"])
    assert source["fact_id"] in {fact["id"] for fact in old_trace["facts"]}
    assert engine.overview("2026-01")["pending"] == []


def test_latest_fact_is_separate_from_active_calculation_and_overlay_replaces_stale_result(audit):
    engine, proof = audit
    save_entry(audit)
    publish(engine)
    engine.save_fact(
        "audit_follow",
        "follow",
        {"period": "2026-02", "upstream": "entry"},
        evidence=(proof,),
        expected_revision=0,
        request_id="follow-fact",
    )
    publish(engine, ("follow",), request="follow-post")
    save_entry(audit, amount=250, revision=1)
    with engine.store.connection(read_only=True) as connection:
        assert engine.store.current_fact(connection, "entry").fact.amount == 250
        active = engine.store.select(connection, Read("calculation", "audit_entry", "@entry"))
        assert active[0].values["amount"] == 100
    with pytest.raises(KernelError) as error:
        engine.preview(["follow"])
    assert error.value.code == "pending_upstream"
    preview, _ = publish(engine, request="recalculate")
    assert {row["subject_id"] for row in preview["results"]} == {"entry", "follow"}
    assert all(row["values"]["amount"] == 250 for row in preview["results"])


def test_accounting_change_between_detached_prepare_and_writer_lock_expires_plan(
    audit, monkeypatch
):
    engine, proof = audit
    save_entry(audit)
    preview = engine.preview(["entry"])
    original = engine._prepare

    def raced_prepare(*args, **kwargs):
        prepared = original(*args, **kwargs)
        other = Engine(engine.store)
        other.save_fact(
            "audit_source",
            "racing-source",
            {"period": "2026-01", "amount": 5, "bucket": "owner"},
            evidence=(proof,),
            expected_revision=0,
            request_id="racing-source",
        )
        return prepared

    monkeypatch.setattr(engine, "_prepare", raced_prepare)
    with pytest.raises(KernelError) as error:
        engine.confirm(
            ["entry"],
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id="stale-confirm",
        )
    assert error.value.code == "preview_expired"
    assert engine.ledger("2026-01") == []
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute("SELECT 1 FROM request WHERE id='stale-confirm'").fetchone() is None
        )


def test_unsealed_fact_at_commit_rolls_back_every_publication_and_allows_same_request_retry(audit):
    engine, _ = audit
    save_entry(audit)
    preview = engine.preview(["entry"])

    def break_commit(stage, connection):
        if stage == "commit":
            connection.execute("INSERT INTO subject VALUES('orphan','audit_source')")
            connection.execute(
                "INSERT INTO fact_revision VALUES('unsealed','orphan',1,?,?)",
                (YearMonth("2026-01").ordinal, digest({"orphan": True})),
            )

    engine.fault = break_commit
    with pytest.raises(sqlite3.IntegrityError):
        engine.confirm(
            ["entry"],
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id="retry",
        )
    assert engine.ledger("2026-01") == []
    assert projections(engine) == {"monthly_account": [], "monthly_cashflow": [], "balance": []}
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT 1 FROM subject WHERE id='orphan'").fetchone() is None
        assert engine.store.epochs(connection)["accounting"] == preview["epochs"]["accounting"]
    engine.fault = lambda stage, connection: None
    result = engine.confirm(
        ["entry"], preview_digest=preview["digest"], epochs=preview["epochs"], request_id="retry"
    )
    assert result["results"][0]["voucher_number"] == 1


def test_typed_repeated_rows_and_dependency_relations_cannot_be_appended_after_seal(audit):
    engine, proof = audit
    saved = engine.save_fact(
        "audit_batch",
        "batch",
        {"period": "2026-01", "items": [{"label": "a", "amount": 10}]},
        evidence=(proof,),
        expected_revision=0,
        request_id="batch",
    )
    with engine.store.connection() as connection:
        for sql, values in (
            ("INSERT INTO fact_audit_batch_items VALUES(?,1,'b',20)", (saved["fact_id"],)),
            ("UPDATE fact_audit_batch_items SET amount=20", ()),
            (
                "INSERT OR REPLACE INTO fact_audit_batch_items "
                "SELECT * FROM fact_audit_batch_items",
                (),
            ),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(sql, values)
    saved = save_entry(audit)
    _, posted = publish(engine)
    cid = posted["results"][0]["calculation_id"]
    with engine.store.connection() as connection:
        for sql, values in (
            ("INSERT INTO fact_scope VALUES(?,'audit_entry','late')", (saved["fact_id"],)),
            ("INSERT INTO calculation_scope VALUES(?,'audit_entry','late')", (cid,)),
            ("INSERT INTO dependency_fact VALUES(?,?)", (cid, saved["fact_id"])),
            ("INSERT INTO dependency_calculation VALUES(?,?)", (cid, cid)),
            (
                "INSERT OR REPLACE INTO calculation_publication "
                "SELECT * FROM calculation_publication",
                (),
            ),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(sql, values)


def test_close_cannot_skip_an_earlier_unpublished_fact(audit):
    engine, proof = audit
    save_entry(audit)
    inventory(audit, "2026-02")
    with pytest.raises(KernelError):
        Periods(engine).preview_close("2026-02", owner_confirmation=proof)


def test_cumulative_closed_snapshot_cannot_be_changed_by_posting_to_an_earlier_month(audit):
    engine, _ = audit
    snapshot = close(audit, "2026-02")
    save_entry(audit, period="2026-01")
    with pytest.raises(KernelError):
        publish(engine)
    assert Periods(engine).closed_report("2026-02") == snapshot


def test_raw_typed_fact_cannot_claim_another_fact_kind(audit):
    engine, _ = audit
    with engine.store.connection() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT INTO subject VALUES('wrong-kind','audit_source')")
            connection.execute(
                "INSERT INTO fact_revision VALUES('wrong-kind','wrong-kind',1,?,?)",
                (YearMonth("2026-01").ordinal, digest({"wrong": True})),
            )
            connection.execute(
                "INSERT INTO fact_audit_entry VALUES(?,?,100,'owner','management',0)",
                ("wrong-kind", YearMonth("2026-01").ordinal),
            )
            connection.execute("INSERT INTO fact_seal VALUES('wrong-kind')")
            connection.execute("INSERT INTO fact_current VALUES('wrong-kind','wrong-kind')")
            connection.commit()


def test_raw_calculation_cannot_claim_a_different_subjects_fact(audit):
    engine, proof = audit
    saved = save_entry(audit)
    engine.save_fact(
        "audit_source",
        "other",
        {"period": "2026-01", "amount": 10, "bucket": "owner"},
        evidence=(proof,),
        expected_revision=0,
        request_id="other",
    )
    with engine.store.connection() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO calculation "
                "VALUES('mismatch','other',?,'audit_entry',?,'{}',?,'test')",
                (saved["fact_id"], YearMonth("2026-01").ordinal, digest({})),
            )
            connection.execute("INSERT INTO calculation_seal VALUES('mismatch')")
            connection.execute("INSERT INTO calculation_current VALUES('other','mismatch')")
            connection.commit()


def test_multiple_closed_corrections_preserve_snapshots_through_zero_results(audit):
    engine, _ = audit
    save_entry(audit)
    publish(engine)
    january = close(audit, "2026-01")
    save_entry(audit, amount=0, revision=1)
    publish(engine, request="zero-in-february", correction_period="2026-02")
    february = close(audit, "2026-02")
    save_entry(audit, amount=80, revision=2)
    _, initial_march = publish(engine, request="restore-in-march", correction_period="2026-03")
    save_entry(audit, amount=0, revision=3)
    publish(engine, request="zero-again")
    save_entry(audit, amount=50, revision=4)
    _, restored_march = publish(engine, request="restore-again")
    assert (
        restored_march["results"][0]["voucher_number"]
        == initial_march["results"][0]["voucher_number"]
    )
    assert Periods(engine).closed_report("2026-01") == january
    assert Periods(engine).closed_report("2026-02") == february
    assert (
        len(engine.ledger("2026-01"))
        == len(engine.ledger("2026-02"))
        == len(engine.ledger("2026-03"))
        == 1
    )
    before = projections(engine)
    engine.rebuild_projections(request_id="rebuild-closed-series")
    assert projections(engine) == before


def test_material_change_after_close_snapshot_but_before_commit_expires_plan(audit, monkeypatch):
    engine, proof = audit
    save_entry(audit)
    publish(engine)
    inventory(audit, "2026-01")
    periods = Periods(engine)
    preview = periods.preview_close("2026-01", owner_confirmation=proof)
    original = periods.preview_close

    def raced_preview(*args, **kwargs):
        prepared = original(*args, **kwargs)
        engine.register_evidence(b"late material", "text/plain", "late", request_id="late-material")
        return prepared

    monkeypatch.setattr(periods, "preview_close", raced_preview)
    with pytest.raises(KernelError) as error:
        periods.close(
            "2026-01",
            owner_confirmation=proof,
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id="raced-close",
        )
    assert error.value.code == "preview_expired"
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM period_close").fetchone()[0] == 0
        assert connection.execute("SELECT 1 FROM request WHERE id='raced-close'").fetchone() is None


def test_closed_voucher_pointer_cannot_be_deleted_or_replaced_with_raw_sql(audit):
    engine, _ = audit
    save_entry(audit)
    publish(engine)
    close(audit, "2026-01")
    with engine.store.connection() as connection:
        for statement in (
            "DELETE FROM voucher_current",
            "UPDATE voucher_current SET version_id=version_id",
            "INSERT OR REPLACE INTO voucher_current SELECT * FROM voucher_current",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="closed"):
                connection.execute(statement)


def test_jobs_keep_request_identity_and_payload_immutable_but_allow_worker_progress(audit):
    engine, _ = audit
    with engine.store.connection() as connection:
        connection.execute(
            "INSERT INTO jobs(id,kind,payload,status) "
            "VALUES('backup','portable_backup','{}','pending')"
        )
        connection.execute("UPDATE jobs SET status='running',attempts=attempts+1 WHERE id='backup'")
        for statement in (
            "UPDATE jobs SET id='another' WHERE id='backup'",
            "UPDATE jobs SET kind='other' WHERE id='backup'",
            "UPDATE jobs SET payload='{\"directory\":\"elsewhere\"}' WHERE id='backup'",
            "DELETE FROM jobs WHERE id='backup'",
            "INSERT OR REPLACE INTO jobs SELECT * FROM jobs",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement)
        connection.execute("UPDATE jobs SET status='succeeded',result='{}' WHERE id='backup'")


def test_net_zero_cashflow_is_absent_but_equal_debit_credit_turnover_is_preserved(audit):
    engine, _ = audit
    save_entry(audit)
    publish(engine)
    close(audit, "2026-01")
    save_entry(audit, revision=1, expense="selling")
    publish(engine, request="same-amount-reclassified", correction_period="2026-02")
    before = projections(engine)
    february = YearMonth("2026-02").ordinal
    assert [row for row in before["monthly_cashflow"] if row[0] == february] == []
    assert (february, "1002", 100, 100) in before["monthly_account"]
    engine.rebuild_projections(request_id="rebuild-net-zero")
    assert projections(engine) == before


@pytest.mark.parametrize("wrong_period", [False, True])
def test_fact_seal_requires_typed_root_with_matching_period(audit, wrong_period):
    engine, _ = audit
    with engine.store.connection() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT INTO subject VALUES('broken','audit_source')")
            january = YearMonth("2026-01").ordinal
            connection.execute(
                "INSERT INTO fact_revision VALUES('broken','broken',1,?,?)",
                (january, digest({"broken": True})),
            )
            if wrong_period:
                connection.execute(
                    "INSERT INTO fact_audit_source VALUES('broken',?,1,'owner')", (january + 1,)
                )
            connection.execute("INSERT INTO fact_seal VALUES('broken')")
            connection.commit()
