"""Current commentary bases remain immutable and survive portable restore."""

import sqlite3

import pytest
from entity_fixture import save_entity_display_profile
from test_opening_continuation import book as book  # noqa: F401

from ai_accounting.kernel.backup import create_portable, restore_portable, verify_portable
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.display import Display
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.types import YearMonth, canonical, digest


def commentary_values(identifier):
    return (
        identifier,
        YearMonth("2026-01").ordinal,
        1,
        "合成历史经营说明",
        digest("original context"),
        None,
        "合成负责人依据",
        None,
        digest("original commentary"),
    )


def basis_values(identifier):
    content = {
        "identity": {"company_id": "synthetic-company", "database_id": "synthetic-db"},
        "period": "2026-01",
        "close_digest": None,
    }
    basis = {
        "content": content,
        "adoption": {
            "commentary_id": identifier,
            "context_digest": digest("original context").hex(),
            "revision": 1,
        },
    }
    return identifier, "commentary-content-v1", canonical(basis), digest(content)


def test_new_commentary_requires_immutable_basis_in_the_same_transaction(book):
    engine, *_ = book
    with engine.store.connection() as connection:
        assert connection.execute("SELECT count(*) FROM period_commentary_basis").fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError, match="requires content basis"):
            connection.execute(
                "INSERT INTO period_commentary_revision VALUES(?,?,?,?,?,?,?,?,?)",
                commentary_values("new"),
            )
        with pytest.raises(sqlite3.IntegrityError, match="must precede new commentary"):
            connection.execute(
                "INSERT INTO period_commentary_basis VALUES(?,?,?,?)",
                ("new", "legacy-context-v8", None, None),
            )
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO period_commentary_basis VALUES(?,?,?,?)", basis_values("new")
        )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.commit()
        connection.rollback()
        assert connection.execute("SELECT count(*) FROM period_commentary_basis").fetchone()[0] == 0
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO period_commentary_basis VALUES(?,?,?,?)", basis_values("new")
        )
        connection.execute(
            "INSERT INTO period_commentary_revision VALUES(?,?,?,?,?,?,?,?,?)",
            commentary_values("new"),
        )
        connection.commit()
        for sql in (
            "UPDATE period_commentary_basis SET content_digest=zeroblob(32)",
            "DELETE FROM period_commentary_basis",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                connection.execute(sql)
        with pytest.raises(sqlite3.IntegrityError, match="must precede new commentary"):
            connection.execute(
                "INSERT OR REPLACE INTO period_commentary_basis VALUES(?,?,?,?)",
                basis_values("new"),
            )
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None


def test_profiles_commentary_basis_and_audit_survive_verified_portable_restore(book, tmp_path):
    engine, *_ = book
    display = Display(engine)
    save_entity_display_profile(
        engine,
        {
            "kind": "asset",
            "entity_id": "asset-1",
            "purpose": "明确业务用途",
            "source": "负责人确认",
        },
        expected_revision=0,
        request_id="asset-profile",
    )
    with pytest.raises(KernelError) as stale:
        save_entity_display_profile(
            engine,
            {
                "kind": "asset",
                "entity_id": "asset-1",
                "purpose": "过期修改",
                "source": "负责人确认",
            },
            expected_revision=0,
            request_id="stale-asset-profile",
        )
    assert stale.value.code == "entity_profile_version_conflict"
    preview = display.preview_period_commentary("2026-01")
    display.update_period_commentary(
        "2026-01",
        "已核对本期资料",
        context_digest=preview["context_digest"],
        source="人工核对",
        expected_revision=0,
        request_id="commentary",
    )
    package = create_portable(engine.store.path, tmp_path / "backups", _bundle=engine.store.bundle)
    verify_portable(package["path"], _bundle=engine.store.bundle)
    restored_path = tmp_path / "restored.sqlite"
    restore_portable(package["path"], restored_path, _bundle=engine.store.bundle)
    restored = Store(
        restored_path, engine.store.bundle, engine.store.company_id, engine.store.database_id
    )
    with (
        engine.store.connection(read_only=True) as source,
        restored.connection(read_only=True) as target,
    ):
        for table in (
            "entity",
            "entity_profile_revision",
            "display_profile_revision",
            "period_commentary_revision",
            "period_commentary_basis",
            "audit",
            "request",
        ):
            assert list(map(tuple, source.execute(f"SELECT * FROM {table}"))) == list(
                map(tuple, target.execute(f"SELECT * FROM {table}"))
            )
