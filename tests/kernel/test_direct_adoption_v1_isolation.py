"""Fixed v1 verification and directory repair do not use dashboard candidates."""

from test_v1_stored_json_isolation import released_book  # noqa: F401

from ai_accounting.kernel import dashboard_reads
from ai_accounting.kernel.backup import create_portable, verify_file, verify_portable
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.read_indexes import repair_read_indexes


def test_fixed_v1_verification_backup_and_repair_do_not_call_current_head_sql(
    released_book, tmp_path, monkeypatch,
):
    book, bundle = released_book

    def blocked(*_args, **_kwargs):
        raise AssertionError("fixed v1 used current dashboard adopted-head SQL")

    monkeypatch.setattr(dashboard_reads, "_adopted_heads_sql", blocked)
    assert verify_file(book.engine.store.path, _bundle=bundle)["verification"]["status"] == "verified"
    archive = create_portable(book.engine.store.path, tmp_path / "backups", _bundle=bundle, request_id="v1")
    assert verify_portable(archive["path"], _bundle=bundle)["verification"]["status"] == "verified"
    with book.engine.store.connection() as connection, historical_content(1):
        connection.execute("BEGIN IMMEDIATE")
        trigger = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name='immutable_close_reference_DELETE'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER immutable_close_reference_DELETE")
        connection.execute("DELETE FROM close_reference")
        connection.execute(trigger)
        assert repair_read_indexes(connection, bundle=bundle)["changed"] is True
        connection.commit()
    assert verify_file(book.engine.store.path, _bundle=bundle)["verification"]["status"] == "verified"
