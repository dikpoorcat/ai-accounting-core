"""Company v9 preserves historical commentary without inventing its content basis."""

import sqlite3
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
from ai_accounting.kernel.versions import (
    current_version,
    known_contracts,
    objects,
    upgrade,
    verify_schema,
)


@pytest.mark.parametrize("stage", ["after_ddl", "before_commit"])
@pytest.mark.parametrize("previous", [7, 8])
def test_forward_upgrade_rolls_back_and_keeps_legacy_close_digest(tmp_path, stage, previous):
    path = tmp_path / "legacy.sqlite"
    previous_file(path, "business", previous)
    old_manifest = {"period": "2026-01", "facts": [], "vouchers": [], "trial_balance": []}
    with closing(connect(path)) as connection:
        connection.execute(
            "INSERT INTO period_close VALUES(?,?,?)",
            (YearMonth("2026-01").ordinal, canonical(old_manifest), digest(old_manifest)),
        )
        old_commentary = None
        if previous == 8:
            connection.execute(
                "INSERT INTO period_commentary_revision VALUES(?,?,?,?,?,?,?,?,?)",
                commentary_values("historical"),
            )
            old_commentary = tuple(
                connection.execute("SELECT * FROM period_commentary_revision").fetchone()
            )
        before = objects(connection)

        def fail(point):
            if point == stage:
                raise RuntimeError("synthetic migration interruption")

        with pytest.raises(RuntimeError):
            upgrade(connection, registry=default_registry(), fault=fail)
        assert objects(connection) == before
        assert (
            verify_schema(connection, registry=default_registry(), allow_previous=True) == previous
        )
        if old_commentary:
            assert (
                tuple(connection.execute("SELECT * FROM period_commentary_revision").fetchone())
                == old_commentary
            )
        assert upgrade(connection, registry=default_registry())
        assert verify_schema(connection, registry=default_registry()) == current_version("business")
        assert [
            row[0]
            for row in connection.execute("SELECT version FROM schema_history ORDER BY version")
        ] == [previous, current_version("business")]
        assert dict(connection.execute("SELECT manifest,digest FROM period_close").fetchone()) == {
            "manifest": canonical(old_manifest),
            "digest": digest(old_manifest),
        }
        assert (
            connection.execute("SELECT count(*) FROM display_profile_revision").fetchone()[0] == 0
        )
        if old_commentary:
            assert (
                tuple(connection.execute("SELECT * FROM period_commentary_revision").fetchone())
                == old_commentary
            )
            assert tuple(
                connection.execute("SELECT * FROM period_commentary_basis").fetchone()
            ) == ("historical", "legacy-context-v8", None, None)
            for sql, args in (
                ("UPDATE period_commentary_basis SET contract=?", ("commentary-content-v1",)),
                ("DELETE FROM period_commentary_basis", ()),
                (
                    "INSERT INTO period_commentary_basis VALUES(?,?,?,?)",
                    basis_values("historical"),
                ),
            ):
                with pytest.raises(sqlite3.IntegrityError):
                    connection.execute(sql, args)
        else:
            assert (
                connection.execute("SELECT count(*) FROM period_commentary_basis").fetchone()[0]
                == 0
            )
            assert (
                connection.execute("SELECT count(*) FROM period_commentary_revision").fetchone()[0]
                == 0
            )
        assert not upgrade(connection, registry=default_registry())
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


def test_v9_profiles_commentary_basis_and_audit_survive_verified_portable_restore(book, tmp_path):
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
        for table in (
            "display_profile_revision",
            "period_commentary_revision",
            "period_commentary_basis",
            "audit",
            "request",
        ):
            assert list(map(tuple, source.execute(f"SELECT * FROM {table}"))) == list(
                map(tuple, target.execute(f"SELECT * FROM {table}"))
            )
        assert verify_schema(target, registry=default_registry()) == current_version("business")


def test_legacy_strict_commentary_remains_historical_when_new_basis_is_adopted(tmp_path):
    path = tmp_path / "legacy-commentary.sqlite"
    previous_file(path, "business", 8)
    period, text, source = "2026-01", "升级前已核对的经营说明", "合成负责人确认"
    with closing(connect(path)) as connection:
        old_context = Display._legacy_context(connection, period)["context_digest"]
        data = [period, text, old_context, source, 0, None]
        historical = (
            "legacy-commentary",
            YearMonth(period).ordinal,
            1,
            text,
            bytes.fromhex(old_context),
            None,
            source,
            None,
            digest([data, None]),
        )
        connection.execute(
            "INSERT INTO period_commentary_revision VALUES(?,?,?,?,?,?,?,?,?)", historical
        )
        assert upgrade(connection, registry=default_registry())
        marker = tuple(connection.execute("SELECT * FROM period_commentary_basis").fetchone())
        assert marker == ("legacy-commentary", "legacy-context-v8", None, None)

    engine = Engine(Store(path, default_registry(), "synthetic-company", "synthetic-db"))
    display = Display(engine)
    initial = display.preview_period_commentary(period)
    assert initial["current"]["id"] == "legacy-commentary"
    assert initial["current"]["content_validity"] == {
        "status": "current",
        "contract": "legacy-context-v8",
        "method": "legacy_strict",
    }
    with engine.store.connection(read_only=True) as connection:
        original = initial["current"]
        for frozen_digest in (None, original["digest"]):
            assert Display._validity(connection, original, initial, frozen_digest=frozen_digest)[
                "status"
            ] == ("frozen" if frozen_digest else "current")
            for field, value in {
                "text": "读取到的正文与保存摘要不一致",
                "source": "其他来源",
                "context_digest": digest("other context").hex(),
                "revision": 2,
                "evidence_digest": (b"a" * 32).hex(),
                "period": "2026-02",
                "close_digest": digest("other close").hex(),
            }.items():
                damaged = {**original, field: value}
                validity = Display._validity(
                    connection, damaged, initial, frozen_digest=frozen_digest
                )
                assert validity == {
                    "status": "unverifiable",
                    "contract": "legacy-context-v8",
                    "reason": "commentary_digest_mismatch",
                }
                assert damaged[field] == value  # Keep the read original for inspection.
        assert engine.ledger(period) == []
    engine.save_fact(
        "expense",
        "future-expense",
        {
            "period": "2026-02",
            "amount_fen": 100,
            "counterparty_id": "supplier",
            "creditor_kind": "supplier",
            "expense_class": "administration",
        },
        evidence=((b"a" * 32).hex(),),
        expected_revision=0,
        request_id="future-expense",
    )
    later = display.preview_period_commentary(period)
    assert later["current"] is None and later["latest"]["text"] == text
    assert later["latest"]["content_validity"] == {
        "status": "unverifiable",
        "contract": "legacy-context-v8",
        "method": "legacy_strict",
    }
    assert later["content_digest"] == initial["content_digest"]
    saved = display.update_period_commentary(
        period,
        "重新核对后独立采用当前内容",
        context_digest=later["context_digest"],
        source=source,
        expected_revision=1,
        request_id="new-commentary",
    )
    current = display.preview_period_commentary(period)["current"]
    assert current["id"] == saved["id"]
    assert current["content_validity"] == {
        "status": "current",
        "contract": "commentary-content-v1",
    }
    with engine.store.connection(read_only=True) as connection:
        assert (
            tuple(
                connection.execute(
                    "SELECT * FROM period_commentary_revision WHERE id='legacy-commentary'"
                ).fetchone()
            )
            == historical
        )
        assert (
            tuple(
                connection.execute(
                    "SELECT * FROM period_commentary_basis WHERE commentary_id='legacy-commentary'"
                ).fetchone()
            )
            == marker
        )
        basis = connection.execute(
            "SELECT contract,basis,content_digest FROM period_commentary_basis "
            "WHERE commentary_id=?",
            (saved["id"],),
        ).fetchone()
        assert basis["contract"] == "commentary-content-v1"
        assert basis["basis"] is not None and basis["content_digest"] is not None
