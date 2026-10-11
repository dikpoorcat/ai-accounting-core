"""Authenticated close management sections allow bounded report readiness reads."""

import hashlib
import json

import pytest
from close_storage_fixture import replace_stored_manifest
from test_dashboard_model_integrity import raw_read
from test_engine import close as close_custom
from test_engine import engine as custom_engine_fixture
from test_integrity_content import damage
from test_reports import book as book_fixture
from test_reports import scenario

from ai_accounting.kernel import close_storage, close_storage_v1
from ai_accounting.kernel.backup import create_portable, restore_portable, verify_portable
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.integrity import verify_integrity
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.reports import _closed_report_fact_sources
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.types import YearMonth, canonical

book = book_fixture
custom_engine = custom_engine_fixture


def _close_report_book(book, *, codec_version=5):
    scenario(book)
    book[3]("2026-01")
    if codec_version == 4:
        # Finish the current producer first. Only isolated storage-codec reads
        # below consume this historical logical shape, never current services.
        with book[0].store.connection(read_only=True) as connection:
            row = connection.execute("SELECT * FROM period_close").fetchone()
            historical = close_storage.decode_close(connection, row)
        historical.pop("fund_details")
        historical["format_version"] = 4
        replace_stored_manifest(book[0], historical)
    return book[0], YearMonth("2026-01").ordinal


def test_current_portable_backup_preserves_nonempty_history_and_frozen_position(book, tmp_path):
    engine, save, _, close = book
    scenario(book)
    with engine.store.connection(read_only=True) as connection:
        voucher_id, line_no = connection.execute(
            "SELECT v.id,l.line_no FROM voucher_version v "
            "JOIN calculation c ON c.id=v.calculation_id "
            "JOIN voucher_line l ON l.version_id=v.id "
            "WHERE c.subject_id='cost' AND l.account='2202'"
        ).fetchone()
    save(
        "report_classification",
        "class-" + voucher_id,
        {
            "period": "2026-02",
            "voucher_version_id": voucher_id,
            "profit_details": [
                {"line_no": 1, "detail_code": "management_entertainment", "amount_fen": 10000}
            ],
            "counterparties": [{"line_no": line_no, "counterparty_id": "supplier"}],
        },
        revision=1,
    )
    for period in ("2026-01", "2026-02", "2026-03"):
        close(period)

    def frozen_sources(selected):
        with selected.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            assert verify_integrity(selected, connection)["status"] == "verified"
            rows = connection.execute("SELECT * FROM period_close ORDER BY period").fetchall()
            assert len(rows) == 3
            manifests = tuple(close_storage.decode_close(connection, row) for row in rows)
            assert all(item["format_version"] == 5 for item in manifests)
            assert all(item["fund_details"]["definitions"] for item in manifests)
            assert manifests[1]["readiness"]["financial_reports"]["facts"]
            return tuple(connection.iterdump()), manifests

    source_before, frozen_before = frozen_sources(engine)
    with raw_read():
        report_before = Dashboard(engine).quarterly_report(2026, 1)
    assert report_before["close_state"] == "closed"
    assert report_before["export"]["available"]
    assert report_before["summary"]["ending_cash_fen"] == 40000
    assert report_before["summary"]["assets_total_fen"] == 40000
    assert report_before["summary"]["year_to_date_net_profit_fen"] == -10000

    created = create_portable(
        engine.store.path, tmp_path / "backups", _bundle=engine.store.bundle
    )
    verified = verify_portable(created["path"], _bundle=engine.store.bundle)
    assert verified["database_format"]["status"] == "draft"
    assert verified["database_format"]["version"] == 0
    assert verified["verification"]["status"] == "verified"
    target = tmp_path / "independent-restored.sqlite"
    assert not target.exists()
    restored = restore_portable(
        created["path"], target,
        expected_company_id=engine.store.company_id,
        expected_database_id=engine.store.database_id,
        _bundle=engine.store.bundle,
    )
    assert restored["verification"]["status"] == "verified"
    restored_engine = Engine(Store(
        target, engine.store.bundle, engine.store.company_id, engine.store.database_id
    ))
    restored_sources, restored_frozen = frozen_sources(restored_engine)
    assert restored_sources == source_before
    assert restored_frozen == frozen_before
    with raw_read():
        report_after = Dashboard(restored_engine).quarterly_report(2026, 1)
    assert report_after["summary"] == report_before["summary"]
    assert report_after["statements"] == report_before["statements"]
    assert frozen_sources(engine) == (source_before, frozen_before)

    # Corrupt only the independent copy; all original source bytes and the
    # verified portable archive remain available for a clean restore.
    damage(
        restored_engine,
        "close_storage_block",
        "UPDATE close_storage_block SET content='{}' "
        "WHERE period=(SELECT max(period) FROM period_close) "
        "AND field='management_snapshot'",
    )
    with restored_engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        with pytest.raises(KernelError):
            verify_integrity(restored_engine, connection)
    assert frozen_sources(engine) == (source_before, frozen_before)
    assert verify_portable(
        created["path"], _bundle=engine.store.bundle
    )["verification"]["status"] == "verified"


@pytest.mark.parametrize("close_version", [4, 5])
def test_readiness_section_is_bounded_and_full_logical_close_is_unchanged(
    book, monkeypatch, close_version
):
    engine, period = _close_report_book(book, codec_version=close_version)
    with engine.store.connection(read_only=True) as connection:
        row = connection.execute("SELECT * FROM period_close WHERE period=?", (period,)).fetchone()
        current = close_storage.decode_close(connection, row)
        assert current["format_version"] == close_version
        if close_version == 4:
            historical = close_storage_v1.decode_close(connection, row)
            assert current == historical
            assert "fund_details" not in current
        else:
            assert "fund_details" in current
            with pytest.raises(KernelError):
                close_storage_v1.decode_close(connection, row)
        assert hashlib.sha256(canonical(current).encode("utf-8")).digest() == row["digest"]
        assert current["readiness"]["financial_reports"]["facts"]

    fields = []
    original = close_storage._bucket_rows

    def counted(connection, header, subroot, field, bucket, *, _stored=None):
        fields.append(field)
        return original(connection, header, subroot, field, bucket, _stored=_stored)

    monkeypatch.setattr(close_storage, "_bucket_rows", counted)
    with QueryReads.snapshot(engine) as reads:
        row = reads.authoritative_close_rows(periods=[period])[0]
        financial = reads.close_readiness_check(row, "financial_reports")
        assert financial == current["readiness"]["financial_reports"]
        assert reads.close_readiness_check(row, "financial_reports") is financial
        assert _closed_report_fact_sources(
            reads.connection, YearMonth("2026-01"), reads, closes=[row]
        ) == [{"close_period": period, "reference_id": ident} for ident in financial["facts"]]
        assert fields == ["readiness:financial_reports"]
        header = reads.close_header(row)
        assert close_storage.reference_leaf(
            reads.connection,
            header,
            "readiness.financial_reports.facts[*]",
            0,
            financial["facts"][0],
        ) == (financial["facts"][0], None)
        assert fields == ["readiness:financial_reports"] * 2
        assert (
            close_storage_v1.read_readiness_check(reads.connection, header, "financial_reports")
            == financial
        )


@pytest.mark.parametrize("close_version", [4, 5])
def test_full_close_decode_reads_each_authenticated_family_and_adoption_once(
    book, monkeypatch, close_version
):
    engine, period = _close_report_book(book, codec_version=close_version)
    with engine.store.connection(read_only=True) as connection:
        row = connection.execute("SELECT * FROM period_close WHERE period=?", (period,)).fetchone()
        results = []
        readers = (close_storage, close_storage_v1) if close_version == 4 else (close_storage,)
        for reader in readers:
            families = []
            adopted = []
            original_family = reader._family
            original_field_values = reader._field_values

            def counted_family(
                connection, header, family, *, _seen=families, _read=original_family
            ):
                _seen.append(family)
                return _read(connection, header, family)

            def counted_field_values(
                connection, header, subroot, field, *, _seen=adopted, _read=original_field_values
            ):
                if field == "adopted_results":
                    _seen.append(field)
                return _read(connection, header, subroot, field)

            with monkeypatch.context() as patch:
                patch.setattr(reader, "_family", counted_family)
                patch.setattr(reader, "_field_values", counted_field_values)
                manifest = reader.decode_close(connection, row)
            assert sorted(families) == sorted(reader.FAMILIES)
            assert adopted == ["adopted_results"]
            assert hashlib.sha256(canonical(manifest).encode("utf-8")).digest() == row["digest"]
            results.append(manifest)
        if close_version == 4:
            assert results[0] == results[1]


def test_unread_management_damage_is_rejected_by_full_decode(book):
    engine, period = _close_report_book(book)
    damage(
        engine,
        "close_storage_block",
        "UPDATE close_storage_block SET content='{}' "
        "WHERE period=? AND field='management_snapshot'",
        (period,),
    )
    with engine.store.connection(read_only=True) as connection:
        row = connection.execute("SELECT * FROM period_close WHERE period=?", (period,)).fetchone()
        header = close_storage.verified_header(connection, row)
        assert close_storage.read_readiness_check(connection, header, "financial_reports")
        with pytest.raises(KernelError, match="关账私有存储"):
            close_storage.decode_close(connection, row)
        connection.execute("BEGIN")
        with pytest.raises(KernelError):
            verify_integrity(engine, connection)


def test_missing_named_readiness_block_is_rejected_by_narrow_read(book):
    engine, period = _close_report_book(book)
    damage(
        engine,
        "close_storage_block",
        "DELETE FROM close_storage_block WHERE period=? AND field='readiness:financial_reports'",
        (period,),
    )
    with engine.store.connection(read_only=True) as connection:
        row = connection.execute("SELECT * FROM period_close WHERE period=?", (period,)).fetchone()
        header = close_storage.verified_header(connection, row)
        with pytest.raises(KernelError, match="关账私有存储"):
            close_storage.read_readiness_check(connection, header, "financial_reports")
        with pytest.raises(KernelError, match="关账私有存储"):
            close_storage_v1.read_readiness_check(connection, header, "financial_reports")


def test_forged_management_name_is_rejected_even_with_resealed_private_root(book):
    engine, period = _close_report_book(book)
    with engine.store.connection(read_only=True) as connection:
        subroot = json.loads(
            connection.execute(
                "SELECT content FROM close_storage_subroot WHERE period=? AND family='management'",
                (period,),
            ).fetchone()[0]
        )
        root = json.loads(
            connection.execute(
                "SELECT manifest FROM period_close WHERE period=?", (period,)
            ).fetchone()[0]
        )
    subroot["directories"]["forged"] = subroot["directories"]["management_snapshot"]

    def sha(value):
        return hashlib.sha256(canonical(value).encode("utf-8")).digest()

    subroot_digest = sha(subroot)
    damage(
        engine,
        "close_storage_subroot",
        "UPDATE close_storage_subroot SET content=?,digest=? "
        "WHERE period=? AND family='management'",
        (canonical(subroot), subroot_digest, period),
    )
    root["subroots"]["management"] = subroot_digest.hex()
    damage(
        engine,
        "period_close",
        "UPDATE period_close SET manifest=? WHERE period=?",
        (canonical(root), period),
    )
    damage(
        engine,
        "close_storage_root",
        "UPDATE close_storage_root SET storage_digest=? WHERE period=?",
        (sha(root), period),
    )
    with engine.store.connection(read_only=True) as connection:
        row = connection.execute("SELECT * FROM period_close WHERE period=?", (period,)).fetchone()
        header = close_storage.verified_header(connection, row)
        with pytest.raises(KernelError, match="关账私有存储"):
            close_storage.read_readiness_check(connection, header, "financial_reports")


def test_unregistered_checker_is_authentically_absent(custom_engine):
    close_custom(custom_engine)
    with QueryReads.snapshot(custom_engine) as reads:
        row = reads.authoritative_close_rows(periods=[YearMonth("2026-01").ordinal])[0]
        assert reads.close_readiness_check(row, "financial_reports") == {}
        assert "financial_reports" not in reads.close_section(row, "readiness")
