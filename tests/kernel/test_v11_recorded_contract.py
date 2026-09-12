"""V11 accepts one recorded complete v10 shape without rewriting business data."""

import hashlib
import json
from contextlib import closing
from pathlib import Path

import pytest

import ai_accounting
from ai_accounting.kernel import read_indexes
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.runtime import connect
from ai_accounting.kernel.schema import initialize, schema_sql
from ai_accounting.kernel.service import default_registry
from ai_accounting.kernel.versions import (
    contract,
    current_version,
    fingerprint,
    known_contracts,
    objects,
    recorded_business_v10_variant,
    upgrade,
    verify_schema,
)

CANDIDATE = Path(__file__).resolve().parents[2]
assert Path(ai_accounting.__file__).resolve().is_relative_to(CANDIDATE / "src"), (
    "Run with candidate/src first; the production editable installation is not a test target"
)
MISSING_INDEXES = {
    "calculation_obligations",
    "dependency_scope_any_kind",
    "voucher_line_account",
    "voucher_version_reverses",
}


def create_recorded_file(path, *, variant=True, alteration=None, taxpayer_id="synthetic-taxpayer"):
    """Synthetic exact source fixture; never discover or open an installed company."""
    released = known_contracts("business")[10]
    snapshot = recorded_business_v10_variant() if variant else released
    selected = list(snapshot["objects"])
    if alteration == "one_missing":
        selected = [item for item in selected if item["name"] != "voucher_line_account"]
    elif alteration == "partial_repair":
        selected += [item for item in released["objects"] if item["name"] == "voucher_line_account"]
    elif alteration == "extra_missing":
        selected = [item for item in selected if item["name"] != "audit_reference_lookup"]
    elif alteration == "missing_trigger":
        selected = [item for item in selected if item["name"] != "immutable_identity_UPDATE"]
    elif alteration == "changed_sql":
        selected = [
            dict(item, sql=item["sql"].replace("(source_type,", "(source_type DESC,"))
            if item["name"] == "audit_reference_lookup"
            else item
            for item in selected
        ]
    with closing(connect(path)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        for typ in ("table", "index", "view", "trigger"):
            for item in selected:
                if item["type"] == typ:
                    connection.execute(item["sql"])
        connection.execute(
            "INSERT INTO identity VALUES(1,'synthetic-company',?,'synthetic-db',?)",
            (taxpayer_id, 9 if alteration == "identity" else 10),
        )
        connection.execute("INSERT INTO state VALUES(1,7,3,2,1)")
        content = b"retained synthetic evidence"
        connection.execute(
            "INSERT INTO evidence VALUES(?,?,?,?)",
            (hashlib.sha256(content).digest(), content, "text/plain", "synthetic source"),
        )
        manifest = '{"period":"2026-01","calculations":[],"vouchers":[]}'
        connection.execute(
            "INSERT INTO period_close VALUES(?,?,?)",
            (24300, manifest, hashlib.sha256(manifest.encode()).digest()),
        )
        read_indexes.sync_close(connection, 24300)
        if alteration != "missing_history":
            connection.execute(
                "INSERT INTO schema_history(version,fingerprint) VALUES(10,?)",
                (bytes(32) if alteration == "history" else bytes.fromhex(snapshot["sha256"]),),
            )
        connection.execute("PRAGMA user_version=10")
        connection.commit()


def table_digest(connection, *, include_metadata=False):
    retained = {}
    for item in objects(connection):
        if item["type"] != "table":
            continue
        if not include_metadata and item["name"] in {"identity", "schema_history"}:
            continue
        rows = connection.execute('SELECT * FROM "' + item["name"] + '"').fetchall()
        retained[item["name"]] = sorted(repr(tuple(row)) for row in rows)
    return hashlib.sha256(json.dumps(retained, sort_keys=True).encode()).hexdigest()


def test_frozen_contracts_and_recorded_source_are_exact():
    standard = known_contracts("business")[10]
    recorded = recorded_business_v10_variant()
    assert current_version("business") == 11
    assert current_version("catalog") == 3
    assert standard["objects"] == known_contracts("business")[11]["objects"]
    assert contract(schema_sql(default_registry())) == standard["objects"]
    assert recorded["sha256"] == "45dd0bba8f9668b552859ff7beea612a8c51fd9838f1a3d9785c76771bc08266"
    assert fingerprint(recorded["objects"]).hex() == recorded["sha256"]
    assert recorded["objects"] == [
        item
        for item in standard["objects"]
        if not (item["type"] == "index" and item["name"] in MISSING_INDEXES)
    ]
    assert set(recorded["missing_indexes"]) == MISSING_INDEXES
    assert recorded["source"]["field"] == "pre_account_index_contract"


@pytest.mark.parametrize("variant", [False, True])
def test_v10_upgrade_keeps_business_tables_and_original_history(tmp_path, monkeypatch, variant):
    path = tmp_path / "source.sqlite"
    create_recorded_file(path, variant=variant)

    def must_not_rebuild(*args):
        pytest.fail("v10 already owns reference directories; v11 must not rebuild them")

    monkeypatch.setattr(read_indexes, "backfill_read_indexes", must_not_rebuild)
    with closing(connect(path)) as connection:
        before = table_digest(connection)
        history = tuple(
            connection.execute("SELECT * FROM schema_history WHERE version=10").fetchone()
        )
        identity = tuple(connection.execute("SELECT * FROM identity").fetchone())
        previous_objects = objects(connection)
        assert verify_schema(connection, allow_previous=True) == 10
        with pytest.raises(KernelError, match="数据库版本"):
            verify_schema(connection)
        assert upgrade(connection, registry=default_registry())
        assert verify_schema(connection) == 11
        assert table_digest(connection) == before
        assert (
            tuple(connection.execute("SELECT * FROM schema_history WHERE version=10").fetchone())
            == history
        )
        assert tuple(connection.execute("SELECT * FROM identity").fetchone()) == (
            *identity[:-1],
            11,
        )
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 11
        assert [
            row[0]
            for row in connection.execute("SELECT version FROM schema_history ORDER BY version")
        ] == [10, 11]
        added = {item["name"] for item in objects(connection) if item not in previous_objects}
        assert added == (MISSING_INDEXES if variant else set())
        assert not upgrade(connection)
        assert table_digest(connection) == before


@pytest.mark.parametrize("variant", [False, True])
@pytest.mark.parametrize("stage", ["after_ddl", "before_commit"])
def test_v11_failure_rolls_back_ddl_identity_and_history(tmp_path, variant, stage):
    path = tmp_path / "rollback.sqlite"
    create_recorded_file(path, variant=variant)
    with closing(connect(path)) as connection:
        before = table_digest(connection, include_metadata=True)
        before_objects = objects(connection)

        def fail(point):
            if point == stage:
                raise RuntimeError("synthetic v11 interruption")

        with pytest.raises(RuntimeError, match="synthetic v11 interruption"):
            upgrade(connection, fault=fail)
        assert objects(connection) == before_objects
        assert table_digest(connection, include_metadata=True) == before
        assert verify_schema(connection, allow_previous=True) == 10
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 10
        assert not connection.in_transaction


@pytest.mark.parametrize(
    ("variant", "alteration", "code"),
    [
        (False, "one_missing", "schema_fingerprint_mismatch"),
        (True, "partial_repair", "schema_fingerprint_mismatch"),
        (True, "extra_missing", "schema_fingerprint_mismatch"),
        (True, "missing_trigger", "schema_fingerprint_mismatch"),
        (True, "changed_sql", "schema_fingerprint_mismatch"),
        (True, "identity", "schema_version_mismatch"),
        (True, "history", "schema_history_mismatch"),
        (True, "missing_history", "schema_history_mismatch"),
    ],
)
def test_nearby_or_misregistered_v10_is_rejected_atomically(tmp_path, variant, alteration, code):
    path = tmp_path / "rejected.sqlite"
    create_recorded_file(path, variant=variant, alteration=alteration)
    with closing(connect(path)) as connection:
        before = table_digest(connection, include_metadata=True)
        before_objects = objects(connection)
        with pytest.raises(KernelError) as error:
            upgrade(connection)
        assert error.value.code == code
        assert objects(connection) == before_objects
        assert table_digest(connection, include_metadata=True) == before
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 10


def test_new_v11_is_strict_and_has_only_v11_history(tmp_path):
    with closing(connect(tmp_path / "new.sqlite")) as connection:
        initialize(
            connection,
            default_registry(),
            "synthetic-company",
            "synthetic-taxpayer",
            "synthetic-db",
        )
        assert verify_schema(connection) == 11
        assert [row[0] for row in connection.execute("SELECT version FROM schema_history")] == [11]
        for name in MISSING_INDEXES:
            connection.execute('DROP INDEX "' + name + '"')
        for allow_previous in (False, True):
            with pytest.raises(KernelError) as error:
                verify_schema(connection, allow_previous=allow_previous)
            assert error.value.code == "schema_fingerprint_mismatch"
