"""A registered company survives a runtime upgrade without accepting unknown files."""

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from ai_accounting.kernel import schema, versions
from ai_accounting.kernel.backup import create_portable
from ai_accounting.kernel.catalog import Catalog
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.runtime import connect
from ai_accounting.kernel.service import default_registry
from ai_accounting.kernel.types import canonical


def frozen_v2_file(path, *, taxpayer_id="taxpayer"):
    path.parent.mkdir(parents=True, exist_ok=True)
    released = versions.known_contracts("business")[2]
    with closing(connect(path)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        for typ in ("table", "index", "view", "trigger"):
            for item in released["objects"]:
                if item["type"] == typ:
                    connection.execute(item["sql"])
        connection.execute(
            "INSERT INTO identity VALUES(1,'company',?,'database',2)", (taxpayer_id,)
        )
        connection.execute("INSERT INTO state VALUES(1,0,0,0,1)")
        content = b"original synthetic evidence survives upgrade"
        connection.execute(
            "INSERT INTO evidence VALUES(?,?,?,?)",
            (hashlib.sha256(content).digest(), content, "text/plain", "original"),
        )
        connection.execute("PRAGMA user_version=2")
        versions.record_version(connection, 2)
        connection.commit()
    return content


def registered_v2(tmp_path):
    catalog = Catalog(tmp_path, default_registry())
    path = tmp_path / "company.sqlite"
    content = frozen_v2_file(path)
    with catalog.connection() as connection:
        connection.execute(
            "INSERT INTO company VALUES(?,?,?,?,?)",
            ("company", "taxpayer", "synthetic registered company", str(path), "database"),
        )
    return catalog, path, content


def test_registered_v2_upgrades_once_and_preserves_original_content(tmp_path):
    catalog, path, content = registered_v2(tmp_path)
    catalog_hash = hashlib.sha256(catalog.path.read_bytes()).digest()
    for _ in range(2):
        with catalog.bind("company").connection(read_only=True) as connection:
            assert versions.verify_schema(connection) == schema.VERSION
            assert connection.execute("SELECT content FROM evidence").fetchone()[0] == content
            assert tuple(connection.execute("SELECT * FROM identity").fetchone()) == (
                1,
                "company",
                "taxpayer",
                "database",
                schema.VERSION,
            )
            assert [
                r[0]
                for r in connection.execute("SELECT version FROM schema_history ORDER BY version")
            ] == [2, schema.VERSION]
    assert hashlib.sha256(catalog.path.read_bytes()).digest() == catalog_hash


def test_current_registered_reads_succeed_while_catalog_and_company_writers_are_active(tmp_path):
    catalog, path, content = registered_v2(tmp_path)
    catalog.bind("company")  # Complete the one required upgrade before taking write locks.
    with closing(connect(path)) as business_writer, catalog.connection() as catalog_writer:
        business_writer.execute("BEGIN IMMEDIATE")
        catalog_writer.execute("BEGIN IMMEDIATE")
        try:
            with catalog.bind("company").connection(read_only=True) as reader:
                assert versions.verify_schema(reader) == schema.VERSION
                assert reader.execute("PRAGMA query_only").fetchone()[0] == 1
                assert reader.execute("SELECT content FROM evidence").fetchone()[0] == content
                assert [
                    row[0]
                    for row in reader.execute("SELECT version FROM schema_history ORDER BY version")
                ] == [2, schema.VERSION]
        finally:
            business_writer.rollback()
            catalog_writer.rollback()


@pytest.mark.parametrize("kind", ["create", "restore"])
def test_published_v2_operation_recovers_after_runtime_upgrade_without_original_zip(tmp_path, kind):
    catalog = Catalog(tmp_path, default_registry())
    taxpayer_id = "91310000123456789A"
    path = tmp_path / taxpayer_id / "company.sqlite"
    content = frozen_v2_file(path, taxpayer_id=taxpayer_id)
    archive, source_hash = None, None
    if kind == "restore":
        archive = Path(create_portable(path, tmp_path / "backups")["path"])
        source_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
        archive.unlink()
        assert not archive.exists()
    company = {
        "id": "company",
        "taxpayer_id": taxpayer_id,
        "name": "synthetic interrupted v2 company",
        "path": str(path),
        "database_id": "database",
    }
    payload = canonical(
        {
            **company,
            "archive": str(archive) if archive is not None else None,
            "source_hash": source_hash,
        }
    )
    # This is the durable v2 file_published checkpoint: the exact released file
    # exists, but the catalog registration transaction has not committed.
    with catalog.connection() as connection:
        connection.execute(
            "INSERT INTO company_operation(id,taxpayer_id,kind,payload,status) "
            "VALUES('interrupted',?,?,?,'pending')",
            (taxpayer_id, kind, payload),
        )
    assert catalog.companies() == []
    with closing(connect(path, read_only=True)) as connection:
        assert versions.verify_schema(connection, allow_previous=True) == 2

    for _ in range(2):
        recovered = Catalog(tmp_path, default_registry())
        assert recovered.companies() == [company]
        assert recovered.operations() == [
            {
                "id": "interrupted",
                "taxpayer_id": taxpayer_id,
                "kind": kind,
                "status": "succeeded",
                "attempts": 1,
                "last_error": None,
            }
        ]
        with recovered.bind("company").connection(read_only=True) as connection:
            assert versions.verify_schema(connection) == schema.VERSION
            assert tuple(connection.execute("SELECT * FROM identity").fetchone()) == (
                1,
                "company",
                taxpayer_id,
                "database",
                schema.VERSION,
            )
            assert [
                tuple(row) for row in connection.execute("SELECT digest,content FROM evidence")
            ] == [(hashlib.sha256(content).digest(), content)]
            assert [
                row[0]
                for row in connection.execute("SELECT version FROM schema_history ORDER BY version")
            ] == [2, schema.VERSION]
        with recovered.connection(read_only=True) as connection:
            assert (
                connection.execute("SELECT payload FROM company_operation").fetchone()[0] == payload
            )


def test_registered_upgrade_failure_rolls_back_and_retry_recovers(tmp_path):
    catalog, path, content = registered_v2(tmp_path)

    def fail(point):
        if point == "before_commit":
            raise OSError("synthetic upgrade interruption")

    catalog.fault = fail
    with pytest.raises(OSError, match="synthetic upgrade"):
        catalog.bind("company")
    with closing(connect(path, read_only=True)) as connection:
        assert versions.verify_schema(connection, allow_previous=True) == 2
        assert connection.execute("SELECT content FROM evidence").fetchone()[0] == content
    catalog.fault = None
    with catalog.bind("company").connection(read_only=True) as connection:
        assert versions.verify_schema(connection) == schema.VERSION


@pytest.mark.parametrize(
    "mutation",
    [
        "DROP TRIGGER seal_voucher",
        "DROP TRIGGER immutable_identity_UPDATE; UPDATE identity SET company_id='other' WHERE id=1",
        "PRAGMA user_version=99",
    ],
)
def test_registered_unknown_file_rejected_before_journal_mode_changes(tmp_path, mutation):
    catalog, path, _ = registered_v2(tmp_path)
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(mutation)
        connection.execute("PRAGMA journal_mode=DELETE")
    before = hashlib.sha256(path.read_bytes()).digest()
    with pytest.raises(KernelError):
        catalog.bind("company")
    assert hashlib.sha256(path.read_bytes()).digest() == before
    assert not path.with_name(path.name + "-wal").exists()


@pytest.mark.parametrize("field", ["company_id", "database_id", "taxpayer_id"])
def test_known_v2_wrong_identity_is_never_upgraded(tmp_path, field):
    catalog, path, _ = registered_v2(tmp_path)
    with closing(sqlite3.connect(path)) as connection:
        trigger = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name='immutable_identity_UPDATE'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER immutable_identity_UPDATE")
        connection.execute(f"UPDATE identity SET {field}='other' WHERE id=1")
        connection.execute(trigger)
        connection.commit()
        connection.execute("PRAGMA journal_mode=DELETE")
    before = hashlib.sha256(path.read_bytes()).digest()
    with pytest.raises(KernelError) as error:
        catalog.bind("company")
    assert error.value.code == "company_mismatch"
    assert hashlib.sha256(path.read_bytes()).digest() == before
    assert not path.with_name(path.name + "-wal").exists()
