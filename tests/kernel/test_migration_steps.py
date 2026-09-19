"""Private contracts exercise the production executor without releasing a version."""

import sqlite3
from contextlib import closing

import pytest

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.migration_steps import MigrationStep, TableReplacement
from ai_accounting.kernel.versions import (
    HISTORY_DDL,
    _execute_steps,
    contract,
    contract_fingerprint,
    execute_statements,
    objects,
    record_version,
)

COMMON = """CREATE TABLE refs(id INTEGER PRIMARY KEY,ledger_id INTEGER NOT NULL
 REFERENCES ledger(id) ON DELETE CASCADE) STRICT;
"""
OLD_TABLE = (
    "CREATE TABLE ledger(id INTEGER PRIMARY KEY,label TEXT NOT NULL,"
    "amount INTEGER NOT NULL CHECK(amount>=0)) STRICT;"
)
MIDDLE_TABLE = (
    "CREATE TABLE ledger(id INTEGER PRIMARY KEY,memo TEXT NOT NULL,"
    "amount INTEGER NOT NULL CHECK(amount>=0), note TEXT NOT NULL DEFAULT '') STRICT;"
)
FINAL_TABLE = (
    'CREATE TABLE "ledger"(id INTEGER PRIMARY KEY,memo TEXT NOT NULL,'
    "amount INTEGER NOT NULL CHECK(amount>=1),note TEXT NOT NULL) STRICT;"
)
OLD_INDEX = "CREATE INDEX ledger_search ON ledger(label)"
INDEX = "CREATE INDEX ledger_search ON ledger(memo,amount)"
OLD_TRIGGER = """CREATE TRIGGER ledger_guard BEFORE INSERT ON ledger
 BEGIN SELECT CASE WHEN NEW.label='' THEN RAISE(ABORT,'empty label') END; END"""
TRIGGER = """CREATE TRIGGER ledger_guard BEFORE INSERT ON ledger
 BEGIN SELECT CASE WHEN NEW.memo='' THEN RAISE(ABORT,'empty memo') END; END"""
SCRIPTS = {
    1: HISTORY_DDL + OLD_TABLE + COMMON + OLD_INDEX + ";" + OLD_TRIGGER + ";",
    2: HISTORY_DDL + MIDDLE_TABLE + COMMON + INDEX + ";" + TRIGGER + ";",
    3: HISTORY_DDL + FINAL_TABLE + COMMON + INDEX + ";" + TRIGGER + ";",
}


def source(connection):
    execute_statements(connection, SCRIPTS[1])
    connection.execute("PRAGMA user_version=1")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("INSERT INTO ledger VALUES(7,'kept',25)")
    connection.execute("INSERT INTO refs VALUES(4,7)")
    record_version(connection, 1)
    connection.commit()


def verify_source(connection):
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def finish(connection, step):
    connection.execute(f"PRAGMA user_version={step.target_version}")
    record_version(connection, step.target_version)


def declarations(
    *, fault=None, validate=None, bad_copy=False, corrupt_copy=False, omit_object=False
):
    def alter(connection):
        connection.execute("ALTER TABLE ledger ADD COLUMN note TEXT NOT NULL DEFAULT ''")
        connection.execute("ALTER TABLE ledger RENAME COLUMN label TO memo")
        connection.execute("DROP INDEX ledger_search")
        connection.execute(INDEX)
        connection.execute("DROP TRIGGER ledger_guard")
        connection.execute(TRIGGER)

    replacement = TableReplacement(
        "ledger",
        "ledger_new",
        FINAL_TABLE.replace('"ledger"', "ledger_new").rstrip(";"),
        "INSERT INTO ledger_new(id,memo,amount,note) "
        + (
            "SELECT id,memo,0,note FROM ledger"
            if bad_copy
            else "SELECT id,memo,amount+1,note FROM ledger"
            if corrupt_copy
            else "SELECT id,memo,amount,note FROM ledger"
        ),
        (("index", "ledger_search", INDEX),)
        if omit_object
        else (("index", "ledger_search", INDEX), ("trigger", "ledger_guard", TRIGGER)),
    )

    def check_data(connection):
        assert connection.execute("SELECT * FROM ledger").fetchall() == [(7, "kept", 25, "")]
        assert connection.execute("SELECT * FROM refs").fetchall() == [(4, 7)]
        if validate:
            validate(connection)

    return (
        MigrationStep(
            "business",
            1,
            contract_fingerprint(SCRIPTS[1]).hex(),
            2,
            contract_fingerprint(SCRIPTS[2]).hex(),
            alter,
            check_data,
        ),
        MigrationStep(
            "business",
            2,
            contract_fingerprint(SCRIPTS[2]).hex(),
            3,
            contract_fingerprint(SCRIPTS[3]).hex(),
            lambda connection: replacement.apply(connection, fault=fault),
            check_data,
            requires_fk_off=True,
        ),
    )


def run(connection, steps, **kwargs):
    return _execute_steps(
        connection,
        steps,
        kind="business",
        verify_source=verify_source,
        finish_step=finish,
        **kwargs,
    )


def snapshot(connection):
    return (
        objects(connection),
        connection.execute("SELECT * FROM ledger").fetchall(),
        connection.execute("SELECT * FROM refs").fetchall(),
        connection.execute("SELECT * FROM schema_history").fetchall(),
        connection.execute("PRAGMA user_version").fetchone(),
    )


def test_explicit_alter_and_table_replacement_match_independent_target():
    with closing(sqlite3.connect(":memory:")) as connection:
        source(connection)
        history = connection.execute("SELECT * FROM schema_history").fetchall()
        trace = []
        connection.set_trace_callback(trace.append)
        assert run(connection, declarations())
        assert objects(connection) == contract(SCRIPTS[3])
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            connection.execute("SELECT * FROM schema_history WHERE version=1").fetchall() == history
        )
        assert connection.execute("SELECT version FROM schema_history").fetchall() == [
            (1,),
            (2,),
            (3,),
        ]
        assert trace.index("PRAGMA foreign_keys=OFF") < trace.index("BEGIN IMMEDIATE")
        assert sum(sql == "BEGIN IMMEDIATE" for sql in trace) == 1
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO ledger VALUES(8,'valid',0,'')")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO ledger VALUES(8,'',5,'')")
        # A referenced parent survived DROP TABLE because FK actions were disabled.
        assert connection.execute("SELECT * FROM refs").fetchall() == [(4, 7)]


@pytest.mark.parametrize(
    "stage",
    [
        "after_create",
        "after_copy",
        "after_drop",
        "after_rename",
        "after_restore",
        "after_ddl",
        "after_validate",
        "before_commit",
    ],
)
def test_every_failure_rolls_back_all_steps_data_and_history(stage):
    def fault(actual):
        if actual == stage:
            raise RuntimeError(stage)

    with closing(sqlite3.connect(":memory:")) as connection:
        source(connection)
        before = snapshot(connection)
        with pytest.raises(RuntimeError, match=stage):
            run(connection, declarations(fault=fault), fault=fault)
        assert snapshot(connection) == before
        assert not connection.in_transaction
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


@pytest.mark.parametrize(
    "mode", ["copy", "corrupt_copy", "undeclared_object", "data", "foreign_key", "target"]
)
def test_validation_and_declared_copy_failures_are_atomic(mode):
    def validate(connection):
        if mode == "data":
            raise RuntimeError("bad retained data")
        if mode == "foreign_key":
            connection.execute("INSERT INTO refs VALUES(99,999)")

    with closing(sqlite3.connect(":memory:")) as connection:
        source(connection)
        before = snapshot(connection)
        steps = declarations(
            validate=validate,
            bad_copy=mode == "copy",
            corrupt_copy=mode == "corrupt_copy",
            omit_object=mode == "undeclared_object",
        )
        if mode == "target":
            from dataclasses import replace

            steps = (steps[0], replace(steps[1], target_sha256="0" * 64))
        with pytest.raises((AssertionError, RuntimeError, KernelError, sqlite3.IntegrityError)):
            run(connection, steps)
        assert snapshot(connection) == before
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_unknown_nearby_source_is_rejected_before_any_migration():
    with closing(sqlite3.connect(":memory:")) as connection:
        source(connection)
        connection.execute("DROP INDEX ledger_search")
        connection.execute("CREATE INDEX ledger_search ON ledger(amount)")
        before = snapshot(connection)
        with pytest.raises(KernelError, match="迁移步骤"):
            run(connection, declarations())
        assert snapshot(connection) == before


def test_source_revalidated_after_lock_and_fk_restored_on_failure():
    with closing(sqlite3.connect(":memory:")) as connection:
        source(connection)
        calls = []

        def changed_after_lock(connection):
            calls.append(
                (connection.in_transaction, connection.execute("PRAGMA foreign_keys").fetchone()[0])
            )
            if connection.in_transaction:
                raise RuntimeError("source changed")

        before = snapshot(connection)
        with pytest.raises(RuntimeError, match="source changed"):
            _execute_steps(
                connection,
                declarations(),
                kind="business",
                verify_source=changed_after_lock,
                finish_step=finish,
            )
        assert calls == [(False, 1), (True, 0)]
        assert snapshot(connection) == before
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_competing_schema_change_between_probe_and_write_lock_is_rejected(tmp_path):
    path = tmp_path / "isolated.sqlite"
    changed = []

    class Connection(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql == "BEGIN IMMEDIATE":
                # A distinct connection wins the write lock after the initial probe.
                with closing(sqlite3.connect(path)) as writer:
                    writer.execute("DROP INDEX ledger_search")
                    writer.execute("CREATE INDEX ledger_search ON ledger(amount)")
                    writer.commit()
                    changed.append(snapshot(writer))
            return super().execute(sql, *args)

    with closing(sqlite3.connect(path, factory=Connection)) as connection:
        source(connection)
        with pytest.raises(KernelError, match="精确结构合同"):
            run(connection, declarations())
        assert snapshot(connection) == changed[0]
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert not connection.in_transaction


def test_existing_transaction_is_never_committed_or_rolled_back():
    with closing(sqlite3.connect(":memory:")) as connection:
        source(connection)
        connection.execute("INSERT INTO refs VALUES(5,7)")
        with pytest.raises(KernelError, match="无事务"):
            run(connection, declarations())
        assert connection.in_transaction
        assert connection.execute("SELECT count(*) FROM refs").fetchone()[0] == 2


@pytest.mark.parametrize("restore_failure", ["execute", "readback"])
def test_failed_fk_restoration_closes_connection(restore_failure):
    class Connection(sqlite3.Connection):
        restoring = False
        was_closed = False

        def execute(self, sql, *args):
            if self.restoring and sql == "PRAGMA foreign_keys=ON":
                if restore_failure == "execute":
                    raise sqlite3.OperationalError("cannot restore")
                return super().execute("PRAGMA foreign_keys=OFF")
            return super().execute(sql, *args)

        def close(self):
            self.was_closed = True
            return super().close()

    connection = sqlite3.connect(":memory:", factory=Connection)
    source(connection)

    def fault(stage):
        if stage == "after_ddl":
            connection.restoring = True
            raise RuntimeError("rollback first")

    with pytest.raises((sqlite3.OperationalError, KernelError)):
        run(connection, declarations(), fault=fault)
    assert connection.was_closed
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


@pytest.mark.parametrize("damage", [None, "missing", "ambiguous", "wrong_hash", "wrong_kind"])
def test_packaged_plan_requires_one_exact_declared_path(monkeypatch, damage):
    from dataclasses import replace

    import ai_accounting.kernel.versions as versions

    # These versions exist only in this private in-memory test mapping.
    source_contract = {
        "version": 100,
        "objects": contract(SCRIPTS[1]),
        "sha256": contract_fingerprint(SCRIPTS[1]).hex(),
    }
    target_contract = {
        "version": 101,
        "objects": contract(SCRIPTS[2]),
        "sha256": contract_fingerprint(SCRIPTS[2]).hex(),
    }
    step = replace(declarations()[0], source_version=100, target_version=101)
    if damage == "wrong_hash":
        step = replace(step, target_sha256="0" * 64)
    elif damage == "wrong_kind":
        step = replace(step, kind="catalog")
    declared = () if damage == "missing" else ((step, step) if damage == "ambiguous" else (step,))
    monkeypatch.setattr(versions, "_DECLARED_STEPS", declared)
    monkeypatch.setattr(versions, "known_contracts", lambda kind: {101: target_contract})
    if damage:
        with pytest.raises(KernelError, match="迁移"):
            versions._migration_plan("business", source_contract, target_contract)
    else:
        assert versions._migration_plan("business", source_contract, target_contract) == (step,)
