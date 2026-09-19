"""Asset batches upgrade a synthetic v11 without adopting its single-card history."""

from contextlib import closing

import pytest
from test_close_batch_migrations import previous_file
from test_v11_recorded_contract import table_digest

from ai_accounting.kernel import read_indexes
from ai_accounting.kernel.contracts import FactVersion, KernelError
from ai_accounting.kernel.domains.assets import AssetConsumption
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.runtime import connect
from ai_accounting.kernel.schema import VERSION
from ai_accounting.kernel.service import default_registry
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.types import YearMonth, canonical, digest
from ai_accounting.kernel.versions import objects, upgrade, verify_schema


def legacy_card_file(path):
    """Seed actual v11 row shapes; no new evaluator is used to rewrite history."""
    previous_file(path, "business", 11)
    store = Store(path, default_registry(), "synthetic-company", "synthetic-db")
    fact = AssetConsumption.model_validate_json(
        canonical({"period": "2026-01", "asset_id": "legacy-card"})
    )
    version = FactVersion("legacy-fact", "legacy-consumption", 1, fact, ("61" * 32,))
    month = YearMonth("2026-01").ordinal
    outcome = {
        "lines": [
            {"account": "5602", "debit": 100, "credit": 0, "cashflow": None},
            {"account": "1602", "debit": 0, "credit": 100, "cashflow": None},
        ],
        "values": {
            "asset_id": "legacy-card",
            "consumption_fen": 100,
            "completed_months": 1,
            "closing_accumulated_fen": 100,
            "carrying_fen": 1100,
            "rounding_policy": "floor_final_remainder",
        },
        "balances": [{"category": "asset", "key": "asset:legacy-card:carrying", "amount": -100}],
        "explanation": [],
        "opening_lines": [],
        "opening": False,
    }
    with closing(connect(path)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        store.write_fact(connection, version, digest(fact.model_dump(mode="json")))
        connection.execute(
            "INSERT INTO calculation VALUES(?,?,?,?,?,?,?,?)",
            (
                "legacy-calculation",
                version.subject_id,
                version.id,
                fact.kind,
                month,
                canonical(outcome),
                digest(outcome),
                "local-kernel-2",
            ),
        )
        connection.execute("INSERT INTO dependency_fact VALUES('legacy-calculation','legacy-fact')")
        connection.execute("INSERT INTO voucher VALUES('legacy-voucher',1)")
        connection.executemany(
            "INSERT INTO voucher_line VALUES('legacy-version',?,?,?,?,NULL)",
            [(1, "5602", 100, 0), (2, "1602", 0, 100)],
        )
        connection.execute(
            "INSERT INTO voucher_version VALUES"
            "('legacy-version','legacy-voucher','legacy-calculation',?,NULL,100)",
            (month,),
        )
        connection.execute("INSERT INTO voucher_current VALUES('legacy-voucher','legacy-version')")
        connection.execute(
            "INSERT INTO calculation_publication VALUES('legacy-calculation',?,'legacy-voucher')",
            (month,),
        )
        connection.execute("INSERT INTO calculation_seal VALUES('legacy-calculation')")
        connection.execute(
            "INSERT INTO calculation_current VALUES('legacy-consumption','legacy-calculation')"
        )
        connection.execute("UPDATE state SET next_number=2 WHERE id=1")
        manifest = {
            "period": "2026-01",
            "calculations": ["legacy-calculation"],
            "facts": ["legacy-fact"],
            "vouchers": [{"id": "legacy-version", "calculation_id": "legacy-calculation"}],
        }
        connection.execute(
            "INSERT INTO period_close VALUES(?,?,?)", (month, canonical(manifest), digest(manifest))
        )
        read_indexes.sync_close(connection, month)
        connection.commit()
    return store, outcome


def test_v11_upgrade_preserves_single_card_voucher_and_close_without_backfill(
    tmp_path, monkeypatch
):
    store, outcome = legacy_card_file(tmp_path / "legacy.sqlite")

    def no_backfill(*args):
        pytest.fail("v11 reference directories must not be rebuilt by the asset migration")

    monkeypatch.setattr(read_indexes, "backfill_read_indexes", no_backfill)
    with closing(connect(store.path)) as connection:
        assert VERSION == 13
        before_tables = {item["name"] for item in objects(connection) if item["type"] == "table"}
        before = table_digest(connection)
        history = tuple(
            connection.execute("SELECT * FROM schema_history WHERE version=11").fetchone()
        )
        assert upgrade(connection, registry=store.registry)
        assert verify_schema(connection, registry=store.registry) == VERSION
        assert table_digest(connection, table_names=before_tables) == before
        assert (
            tuple(connection.execute("SELECT * FROM schema_history WHERE version=11").fetchone())
            == history
        )
        assert [
            row[0]
            for row in connection.execute("SELECT version FROM schema_history ORDER BY version")
        ] == [11, 12, VERSION]
        added_tables = {
            item["name"] for item in objects(connection) if item["type"] == "table"
        } - before_tables
        assert {
            "asset_batch_member",
            "fact_asset_activation_batch",
            "fact_asset_consumption_month",
        } <= added_tables
        for table in added_tables:
            assert connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] == 0
        assert not upgrade(connection, registry=store.registry)
    engine = Engine(store)
    assert engine.ledger("2026-01")[0]["kind"] == "asset_consumption"
    trace = engine.trace(voucher_version_id="legacy-version")
    assert trace["calculation"]["outcome"] == outcome
    assert trace["voucher"]["number"] == 1
    assert [row["debit"] for row in trace["voucher"]["lines"]] == [100, 0]


@pytest.mark.parametrize("stage", ["after_ddl", "before_commit"])
def test_v12_migration_failure_retains_v11_ddl_history_and_business_rows(tmp_path, stage):
    store, _ = legacy_card_file(tmp_path / "rollback.sqlite")
    with closing(connect(store.path)) as connection:
        before = table_digest(connection, include_metadata=True)
        before_ddl = objects(connection)

        def fail(point):
            if point == stage:
                raise RuntimeError("asset migration interrupted")

        with pytest.raises(RuntimeError, match="asset migration interrupted"):
            upgrade(connection, registry=store.registry, fault=fail)
        assert objects(connection) == before_ddl
        assert table_digest(connection, include_metadata=True) == before
        assert verify_schema(connection, registry=store.registry, allow_previous=True) == 11
        assert not connection.in_transaction


def test_v12_does_not_accept_nearby_v11_shape(tmp_path):
    store, _ = legacy_card_file(tmp_path / "unknown.sqlite")
    with closing(connect(store.path)) as connection:
        connection.execute("DROP TRIGGER sealed_line")
        before = list(connection.iterdump())
        with pytest.raises(KernelError) as error:
            upgrade(connection, registry=store.registry)
        assert error.value.code == "schema_fingerprint_mismatch"
        assert list(connection.iterdump()) == before
