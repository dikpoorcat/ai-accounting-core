"""Exercise the recorded v10 portable path using only an isolated synthetic root."""

from contextlib import closing

from test_v11_recorded_contract import create_recorded_file, table_digest

from ai_accounting.kernel.backup import create_portable, verify_file, verify_portable
from ai_accounting.kernel.catalog import Catalog
from ai_accounting.kernel.runtime import connect
from ai_accounting.kernel.service import default_registry
from ai_accounting.kernel.versions import verify_schema


def test_recorded_v10_portable_restores_through_catalog_to_v11(tmp_path):
    taxpayer = "91330100MA00000001"
    source = tmp_path / "recorded-v10.sqlite"
    create_recorded_file(source, taxpayer_id=taxpayer)
    with closing(connect(source, read_only=True)) as connection:
        assert verify_schema(connection, allow_previous=True) == 10
        before = table_digest(connection)
        history = tuple(
            connection.execute("SELECT * FROM schema_history WHERE version=10").fetchone()
        )
        source_metadata = table_digest(connection, include_metadata=True)

    archive = create_portable(source, tmp_path / "portable", request_id="isolated-v11-restore")
    verified = verify_portable(archive["path"], expected_taxpayer_id=taxpayer)
    assert verified["identity"]["schema_version"] == 10
    assert verified["evidence_count"] == 1
    assert verified["latest_closed_period"] == 24300

    catalog = Catalog(tmp_path / "restored-root", default_registry())
    company = catalog.restore_company(
        archive["path"], taxpayer_id=taxpayer, name="合成旧版本恢复验证"
    )
    store = catalog.bind(company["id"])
    with store.connection(read_only=True) as connection:
        assert verify_schema(connection) == 11
        assert table_digest(connection) == before
        assert (
            tuple(connection.execute("SELECT * FROM schema_history WHERE version=10").fetchone())
            == history
        )
        assert [
            row[0]
            for row in connection.execute("SELECT version FROM schema_history ORDER BY version")
        ] == [10, 11]
    assert verify_file(store.path)["identity"]["schema_version"] == 11

    # Backup and restore must not upgrade the protected source or rewrite its original archive.
    with closing(connect(source, read_only=True)) as connection:
        assert verify_schema(connection, allow_previous=True) == 10
        assert table_digest(connection, include_metadata=True) == source_metadata
    assert verify_portable(archive["path"])["identity"]["schema_version"] == 10
