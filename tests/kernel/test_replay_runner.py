import sqlite3
from pathlib import Path
from types import SimpleNamespace
from contextlib import contextmanager

import pytest

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.replay_contracts import ReplayStep
from ai_accounting.kernel.replay_runner import (
    after_replay_write,
    before_replay_write,
    compile_replay_steps,
    replay_write_scope,
    validate_replay_sequence,
)
from ai_accounting.kernel.report_classification_prepare import resolve_unique_voucher_line
from ai_accounting.kernel.report_classification_prepare import prepare_typed_report_classifications
from ai_accounting.kernel.types import canonical
from ai_accounting.kernel.types import YearMonth


def test_dependency_compiler_orders_dynamic_output_references_and_rejects_cycles():
    member = ReplayStep(
        step_id="member", command="save_facts",
        payload={"source": {"$replay_ref": "source", "field": "fact_id"}},
    )
    source = ReplayStep(
        step_id="source", command="receive_material", payload={},
        output_ref="source", output_fields=("fact_id",),
    )
    ordered, edges = compile_replay_steps((member, source))
    assert [item.step_id for item in ordered] == ["source", "member"]
    assert edges["member"] == ("source",)

    cycle = (
        ReplayStep(step_id="a", command="save_facts", payload={}, depends_on=("b",)),
        ReplayStep(step_id="b", command="save_facts", payload={}, depends_on=("a",)),
    )
    with pytest.raises(KernelError, match="循环"):
        compile_replay_steps(cycle)


def test_replay_sequence_rejects_material_before_receipt_and_missing_month_review():
    receive = ReplayStep(
        step_id="receive", command="receive_material", payload={},
        output_ref="received", output_fields=("source_fact_id",),
    )
    resolve = ReplayStep(
        step_id="resolve", command="resolve_material",
        payload={"data": {"source_fact_id": {"$replay_ref": "received", "field": "source_fact_id"}}},
    )
    preview = ReplayStep(step_id="review", command="preview_close", payload={"period": "2025-01"})
    close = ReplayStep(step_id="close", command="close", payload={"period": "2025-01"}, depends_on=("review",))
    ordered, deps = compile_replay_steps((resolve, receive, preview, close))
    validate_replay_sequence(ordered, deps, YearMonth("2025-01"), YearMonth("2025-01"))

    broken = (
        ReplayStep(step_id="resolve", command="resolve_material", payload={"data": {"treatment": "recognize"}}),
        preview,
        close,
    )
    broken_order, broken_deps = compile_replay_steps(broken)
    with pytest.raises(KernelError, match="接收"):
        validate_replay_sequence(broken_order, broken_deps, YearMonth("2025-01"), YearMonth("2025-01"))


def test_continuous_month_plan_requires_prior_close_before_next_preview():
    steps = (
        ReplayStep(step_id="p1", command="preview_close", payload={"period": "2025-01"}),
        ReplayStep(step_id="c1", command="close", payload={"period": "2025-01"}, depends_on=("p1",)),
        ReplayStep(step_id="p2", command="preview_close", payload={"period": "2025-02"}),
        ReplayStep(step_id="c2", command="close", payload={"period": "2025-02"}, depends_on=("p2",)),
    )
    ordered, dependencies = compile_replay_steps(steps)
    with pytest.raises(KernelError, match="前月关账"):
        validate_replay_sequence(ordered, dependencies, YearMonth("2025-01"), YearMonth("2025-02"))


def test_duplicate_material_group_requires_each_member_copy_before_final_group():
    receive = ReplayStep(step_id="receive", command="receive_material", payload={})
    master = ReplayStep(
        step_id="master", command="resolve_material_group",
        payload={"data": {"source_id": "source", "members": []}}, depends_on=("receive",),
    )
    first = ReplayStep(
        step_id="copy-a", command="resolve_material",
        payload={"data": {"source_id": "source", "location": "a", "treatment": "duplicate"}},
        depends_on=("master", "receive"),
    )
    second = ReplayStep(
        step_id="copy-b", command="resolve_material",
        payload={"data": {"source_id": "source", "location": "b", "treatment": "duplicate"}},
        depends_on=("master", "receive"),
    )
    final = ReplayStep(
        step_id="final", command="resolve_material_group",
        payload={"data": {"source_id": "source", "members": [{"location": "a"}, {"location": "b"}]}},
        depends_on=("copy-a", "copy-b", "receive"),
    )
    preview = ReplayStep(step_id="preview", command="preview_close", payload={"period": "2025-01"})
    close = ReplayStep(step_id="close", command="close", payload={"period": "2025-01"}, depends_on=("preview",))
    steps = (receive, master, first, second, final, preview, close)
    ordered, dependencies = compile_replay_steps(steps)
    validate_replay_sequence(ordered, dependencies, YearMonth("2025-01"), YearMonth("2025-01"))

    incomplete = tuple(
        item.model_copy(update={"depends_on": ("copy-a", "receive")})
        if item.step_id == "final" else item
        for item in steps if item.step_id != "copy-b"
    )
    ordered, dependencies = compile_replay_steps(incomplete)
    with pytest.raises(KernelError, match="每个成员"):
        validate_replay_sequence(ordered, dependencies, YearMonth("2025-01"), YearMonth("2025-01"))


def test_report_classification_requires_one_line_from_all_three_typed_relations():
    line = {"version_id": "voucher-v1", "line_no": 3}
    assert resolve_unique_voucher_line(
        source=[line], obligation=[line], payment_allocation=[line]
    ) == {"status": "prepared", "line": line, "issues": []}
    conflict = resolve_unique_voucher_line(
        source=[line], obligation=[line],
        payment_allocation=[{"version_id": "voucher-v1", "line_no": 4}],
    )
    assert conflict["status"] == "needs_information"
    assert conflict["line"] is None


def test_classification_resolver_derives_voucher_line_from_server_reads(monkeypatch):
    from ai_accounting.kernel import publication, query_reads

    item = {
        "source_id": "source-1",
        "source_subject_id": "source-subject",
        "source_calculation_id": "source-calc",
        "obligation_key": "obligation-1",
        "payment_revision_id": "payment-revision",
        "payment_allocation_scope": "payment:payment:payment-subject:obligation-1",
    }
    allocation = SimpleNamespace(
        source_id="source-subject", obligation="obligation-1",
        scope=item["payment_allocation_scope"],
    )

    class Result:
        def __init__(self, value):
            self.value = value

        def fetchone(self):
            return self.value

        def __iter__(self):
            return iter([self.value] if self.value is not None else [])

    class Connection:
        def execute(self, sql, params=()):
            if "SELECT c.id FROM calculation_current" in sql:
                return Result(("payment-calc",))
            if "FROM settlement_change" in sql:
                return Result((1,))
            if "SELECT h.version_id FROM json_each" in sql:
                return Result(("voucher-version",))
            raise AssertionError(sql)

    class Reads:
        connection = Connection()

        def raw_calculations(self, ids):
            if "source-calc" in ids:
                return {"source-calc": {
                    "subject_id": "source-subject", "fact_id": "source-fact",
                    "outcome": {"values": {"obligations": [
                        {"key": "obligation-1", "account": "accounts_payable"}
                    ]}},
                }}
            return {"payment-calc": {
                "subject_id": "payment-subject", "fact_id": "payment-revision",
                "outcome": {"values": {"settlements": [{
                    "source_calculation": "source-calc", "obligation": "obligation-1"
                }]}},
            }}

        def fact_version(self, revision_id):
            assert revision_id == "payment-revision"
            return SimpleNamespace(
                subject_id="payment-subject",
                fact=SimpleNamespace(actual_payment=True, allocations=(allocation,)),
            )

        def verify_publication_records(self, rows):
            return None

        def vouchers(self, version_ids):
            return {"voucher-version": {"calculation_id": "source-calc"}}

        def voucher_lines(self, version_ids):
            return {"voucher-version": [{"line_no": 2, "account": "accounts_payable"}]}

    class Snapshot:
        def __enter__(self):
            return Reads()

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(query_reads.QueryReads, "snapshot", staticmethod(lambda engine: Snapshot()))
    monkeypatch.setattr(publication, "verify_publication_chain", lambda connection, subjects: None)
    monkeypatch.setattr(publication, "active_tranches", lambda connection, subject_ids: [
        {"calculation_id": "source-calc", "voucher_id": "voucher-1"}
    ])
    monkeypatch.setattr(query_reads, "verify_current_publication_voucher_heads", lambda *args: None)

    assert prepare_typed_report_classifications(object(), [item]) == [{
        "source_id": "source-1", "status": "prepared",
        "line": {"version_id": "voucher-version", "line_no": 2}, "issues": [],
    }]
    with pytest.raises(ValueError, match="typed source"):
        prepare_typed_report_classifications(object(), [{**item, "source_lines": [("fake", 1)]}])


def _connection(path):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript("""
      CREATE TABLE state(id INTEGER PRIMARY KEY, accounting INTEGER, material INTEGER,
        management INTEGER, read_repair_revision INTEGER);
      INSERT INTO state VALUES(1,0,0,0,0);
      CREATE TABLE period_close(period INTEGER PRIMARY KEY, logical_digest BLOB);
      CREATE TABLE replay_run(id TEXT PRIMARY KEY,status TEXT,checkpoint TEXT,current_step TEXT);
      CREATE TABLE replay_step(run_id TEXT,step_id TEXT,command TEXT,request_id TEXT UNIQUE,
        request_digest BLOB,status TEXT,response TEXT,error_code TEXT,last_error TEXT,
        PRIMARY KEY(run_id,step_id));
      CREATE TABLE replay_binding(run_id TEXT,reference TEXT,field TEXT,value TEXT,source_step TEXT,
        PRIMARY KEY(run_id,reference,field));
      CREATE TABLE business_write(value TEXT);
    """)
    return connection


class _Store:
    company_id = "company-1"
    database_id = "database-1"

    def __init__(self, path):
        self.path = Path(path)

    @staticmethod
    def epochs(connection):
        row = connection.execute("SELECT accounting,material,management FROM state WHERE id=1").fetchone()
        return dict(zip(("accounting", "material", "management"), row))


def test_engine_write_hook_commits_business_result_and_binding_together(tmp_path):
    path = tmp_path / "company.sqlite"
    engine = SimpleNamespace(store=_Store(path))
    request_id = "stable-step-key"
    request_hash = b"r" * 32
    step = {
        "step_id": "register-source", "output_ref": "source",
        "output_fields": ("fact_id",),
    }
    connection = _connection(path)
    connection.execute(
        "INSERT INTO replay_run(id,status,checkpoint) VALUES(?,?,?)",
        ("run-1", "pending", canonical({
            "epochs": {"accounting": 0, "material": 0, "management": 0},
            "read_repair_revision": 0, "prefix": None,
        })),
    )
    connection.execute(
        "INSERT INTO replay_step(run_id,step_id,command,request_id,status) VALUES(?,?,?,?,?)",
        ("run-1", step["step_id"], "evidence", request_id, "pending"),
    )
    connection.commit()

    with replay_write_scope(
        engine, run_id="run-1", step=step,
        request_id=request_id, request_digest=None,
    ):
        before_replay_write(
            engine, connection, request_id=request_id, action="evidence", request_hash=request_hash
        )
        connection.execute("INSERT INTO business_write VALUES('typed-domain-result')")
        after_replay_write(
            engine, connection, request_id=request_id, action="evidence",
            request_hash=request_hash, result={"fact_id": "fact-1"},
        )
    connection.commit()

    receipt = connection.execute(
        "SELECT status,response FROM replay_step WHERE run_id='run-1' AND step_id='register-source'"
    ).fetchone()
    binding = connection.execute("SELECT value FROM replay_binding").fetchone()[0]
    assert receipt["status"] == "completed"
    assert receipt["response"] == '{"fact_id":"fact-1"}'
    assert binding == '"fact-1"'
    assert connection.execute("SELECT value FROM business_write").fetchone()[0] == "typed-domain-result"
    connection.close()


def test_engine_write_hook_rollback_discards_domain_and_replay_receipts(tmp_path):
    path = tmp_path / "company.sqlite"
    engine = SimpleNamespace(store=_Store(path))
    connection = _connection(path)
    checkpoint = {
        "epochs": {"accounting": 0, "material": 0, "management": 0},
        "read_repair_revision": 0, "prefix": None,
    }
    connection.execute("INSERT INTO replay_run VALUES('run-1','pending',?,NULL)", (canonical(checkpoint),))
    connection.execute(
        "INSERT INTO replay_step(run_id,step_id,command,request_id,status) VALUES('run-1','step-1','evidence','key-1','pending')"
    )
    connection.commit()
    try:
        with replay_write_scope(
            engine, run_id="run-1", step={"step_id": "step-1"},
            request_id="key-1", request_digest=None,
        ):
            connection.execute("BEGIN IMMEDIATE")
            before_replay_write(engine, connection, request_id="key-1", action="evidence", request_hash=b"x" * 32)
            connection.execute("INSERT INTO business_write VALUES('uncommitted')")
            after_replay_write(engine, connection, request_id="key-1", action="evidence", request_hash=b"x" * 32, result={})
            raise RuntimeError("commit fault")
    except RuntimeError:
        connection.rollback()
    assert connection.execute("SELECT count(*) FROM business_write").fetchone()[0] == 0
    assert connection.execute("SELECT status FROM replay_step").fetchone()[0] == "pending"
    assert connection.execute("SELECT count(*) FROM replay_binding").fetchone()[0] == 0
    connection.close()
