"""Company v8 adds metadata without rewriting released schemas or closed records."""

from contextlib import closing

import pytest
from test_close_batch_migrations import previous_file
from test_opening_continuation import book as book  # noqa: F401

from ai_accounting.kernel.backup import create_portable, restore_portable, verify_portable
from ai_accounting.kernel.display import Display
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.runtime import connect
from ai_accounting.kernel.service import default_registry
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.types import YearMonth, canonical, digest
from ai_accounting.kernel.versions import known_contracts, objects, upgrade, verify_schema


@pytest.mark.parametrize("stage", ["after_ddl", "before_commit"])
def test_v7_forward_upgrade_rolls_back_and_keeps_legacy_close_digest(tmp_path, stage):
    path = tmp_path / "legacy.sqlite"
    previous_file(path, "business", 7)
    old_manifest = {"period": "2026-01", "facts": [], "vouchers": [], "trial_balance": []}
    with closing(connect(path)) as connection:
        connection.execute(
            "INSERT INTO period_close VALUES(?,?,?)",
            (YearMonth("2026-01").ordinal, canonical(old_manifest), digest(old_manifest)),
        )
        before = objects(connection)

        def fail(point):
            if point == stage:
                raise RuntimeError("synthetic migration interruption")

        with pytest.raises(RuntimeError):
            upgrade(connection, registry=default_registry(), fault=fail)
        assert objects(connection) == before
        assert verify_schema(connection, registry=default_registry(), allow_previous=True) == 7
        assert upgrade(connection, registry=default_registry())
        assert verify_schema(connection, registry=default_registry()) == 8
        assert [
            row[0]
            for row in connection.execute("SELECT version FROM schema_history ORDER BY version")
        ] == [7, 8]
        assert dict(connection.execute("SELECT manifest,digest FROM period_close").fetchone()) == {
            "manifest": canonical(old_manifest),
            "digest": digest(old_manifest),
        }
        assert (
            connection.execute("SELECT count(*) FROM display_profile_revision").fetchone()[0] == 0
        )
        assert (
            connection.execute("SELECT count(*) FROM period_commentary_revision").fetchone()[0] == 0
        )
    store = Store(path, default_registry(), "synthetic-company", "synthetic-db")
    display = Display(Engine(store))
    display.save_display_profile(
        {
            "kind": "employee",
            "entity_id": "person",
            "display_name": "后来确认姓名",
            "source": "后补",
        },
        expected_revision=0,
        request_id="profile",
    )
    assert display.display_profiles(period="2026-01")["profiles"]["employee"] == {}
    assert max(known_contracts("catalog")) == 3


def test_v8_profiles_commentary_and_audit_survive_verified_portable_restore(book, tmp_path):
    engine, *_ = book
    display = Display(engine)
    display.save_display_profile(
        {
            "kind": "asset",
            "entity_id": "asset-1",
            "purpose": "明确业务用途",
            "source": "负责人确认",
        },
        expected_revision=0,
        request_id="asset-profile",
    )
    preview = display.preview_period_commentary("2026-01")
    display.update_period_commentary(
        "2026-01",
        "已核对本期资料",
        context_digest=preview["context_digest"],
        source="人工核对",
        expected_revision=0,
        request_id="commentary",
    )
    package = create_portable(engine.store.path, tmp_path / "backups")
    verify_portable(package["path"])
    restored_path = tmp_path / "restored.sqlite"
    restore_portable(package["path"], restored_path)
    restored = Store(
        restored_path, default_registry(), engine.store.company_id, engine.store.database_id
    )
    with (
        engine.store.connection(read_only=True) as source,
        restored.connection(read_only=True) as target,
    ):
        for table in ("display_profile_revision", "period_commentary_revision", "audit", "request"):
            assert list(map(tuple, source.execute(f"SELECT * FROM {table}"))) == list(
                map(tuple, target.execute(f"SELECT * FROM {table}"))
            )
        assert verify_schema(target, registry=default_registry()) == 8
