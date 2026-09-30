"""Authenticated close management sections allow bounded report readiness reads."""

import hashlib
import json

import pytest
from schema_fixture import TEST_FAMILY, full_contract, write_contract
from test_engine import close as close_custom
from test_engine import engine as custom_engine_fixture
from test_integrity_content import damage
from test_reports import book as book_fixture
from test_reports import scenario

from ai_accounting.kernel import close_storage, close_storage_v1, content_v1, service
from ai_accounting.kernel.backup import create_portable, verify_portable
from ai_accounting.kernel.catalog import catalog_sql
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.integrity import verify_integrity
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.reports import _closed_report_fact_sources
from ai_accounting.kernel.schema import schema_sql
from ai_accounting.kernel.schema_bundle import APPLICATION_ID, load_bundle
from ai_accounting.kernel.types import YearMonth, canonical

book = book_fixture
custom_engine = custom_engine_fixture


def _close_report_book(book):
    scenario(book)
    book[3]("2026-01")
    return book[0], YearMonth("2026-01").ordinal


def test_released_v1_portable_backup_replays_nonempty_history_position(
    request, tmp_path, monkeypatch
):
    import test_reports

    registry = service.default_registry()
    contract_dir = tmp_path / "schema_contracts"
    write_contract(
        contract_dir,
        full_contract(
            schema_sql(registry), kind="company", status="released", version=1
        ),
    )
    write_contract(
        contract_dir,
        full_contract(catalog_sql(), kind="catalog", status="released", version=1),
    )
    (contract_dir / "content-v1.json").write_text(
        json.dumps(content_v1.content_contract(registry), ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(content_v1, "__file__", str(tmp_path / "content_v1.py"))
    content_v1.v1_registry.cache_clear()
    bundle = load_bundle(
        registry,
        contract_dir,
        family=TEST_FAMILY,
        application_id=APPLICATION_ID,
        status="released",
        current_versions={"company": 1, "catalog": 1},
        company_verifiers={1: content_v1.verify_v1_company},
    )
    monkeypatch.setattr(test_reports, "production_bundle", lambda: bundle)
    book = request.getfixturevalue("book")
    engine, _, _, close = book
    try:
        scenario(book)
        with engine.store.connection(read_only=True) as connection:
            voucher_id, line_no = connection.execute(
                "SELECT v.id,l.line_no FROM voucher_version v "
                "JOIN calculation c ON c.id=v.calculation_id "
                "JOIN voucher_line l ON l.version_id=v.id "
                "WHERE c.subject_id='cost' AND l.account='2202'"
            ).fetchone()
        book[1](
            "report_classification",
            "class-" + voucher_id,
            {
                "period": "2026-02",
                "voucher_version_id": voucher_id,
                "profit_details": [
                    {"line_no": 1, "detail_code": "management_entertainment", "amount_fen": 10000}
                ],
                "counterparties": [
                    {"line_no": line_no, "counterparty_id": "supplier"}
                ],
            },
            revision=1,
        )
        for period in ("2026-01", "2026-02", "2026-03"):
            close(period)
        from ai_accounting.kernel import position_v1, report_projection_v1
        from ai_accounting.kernel.history_reads_v1 import V1Reads

        selected = []
        decode_callers = []
        line_batches = []
        calculation_batches = []
        selected_scopes = []
        original_lines = V1Reads.voucher_lines
        original_calculations = V1Reads.calculations
        original_selected_sql = report_projection_v1.selected_voucher_sql

        def counted_lines(reader, identifiers):
            identifiers = tuple(identifiers)
            line_batches.append(len(set(identifiers)))
            return original_lines(reader, identifiers)

        def counted_calculations(reader, identifiers):
            identifiers = tuple(identifiers)
            calculation_batches.append(len(set(identifiers)))
            return original_calculations(reader, identifiers)

        def checked_selected_sql(period, **kwargs):
            if (
                kwargs.get("voucher_ids") is not None
                and kwargs.get("authoritative_vouchers") is not None
            ):
                candidates = set(kwargs["voucher_ids"])
                assert set(kwargs["authoritative_vouchers"]) <= candidates
                selected_scopes.append((YearMonth(period).ordinal, candidates))
            return original_selected_sql(period, **kwargs)

        monkeypatch.setattr(V1Reads, "voucher_lines", counted_lines)
        monkeypatch.setattr(V1Reads, "calculations", counted_calculations)
        monkeypatch.setattr(report_projection_v1, "selected_voucher_sql", checked_selected_sql)
        original_decode = close_storage_v1.decode_close

        def counted_decode(*args, **kwargs):
            import sys

            decode_callers.append(sys._getframe(1).f_code.co_name)
            return original_decode(*args, **kwargs)

        monkeypatch.setattr(close_storage_v1, "decode_close", counted_decode)
        original_select = position_v1._V1SelectedJournal.select

        def checked_select(journal, *, accounts):
            selected.append(tuple(sorted(accounts)))
            return original_select(journal, accounts=accounts)

        monkeypatch.setattr(position_v1._V1SelectedJournal, "select", checked_select)
        with engine.store.connection(read_only=True) as connection:
            assert connection.execute("SELECT count(*) FROM voucher_version").fetchone()[0] > 0
            assert connection.execute("SELECT count(*) FROM period_close").fetchone()[0] == 3
            connection.execute("BEGIN")
            verify_integrity(engine, connection)
            content_v1.verify_v1_company(connection, engine.store.bundle)
        assert selected, "the v1 history journal path must be exercised"
        assert selected_scopes
        assert max(line_batches) > 1
        assert max(calculation_batches) > 1
        with engine.store.connection(read_only=True) as connection:
            for cutoff, candidates in selected_scopes:
                if not candidates:
                    continue
                latest = connection.execute(
                    "SELECT max(period) FROM voucher_version WHERE id IN "
                    "(SELECT value FROM json_each(?))",
                    (canonical(sorted(candidates)),),
                ).fetchone()[0]
                assert latest <= cutoff
        assert "__init__" not in decode_callers
        assert "select" not in decode_callers
        # A direct v1 owner-review check has no trusted prefix and retains its
        # original source-reading path and exact frozen output comparison.
        from dataclasses import replace

        from ai_accounting.kernel.close_review_integrity_v1 import verify_owner_review_integrity
        from ai_accounting.kernel.content_history_context import historical_content
        from ai_accounting.kernel.engine import Engine
        from ai_accounting.kernel.storage import Store

        historical_store = Store(
            engine.store.path,
            replace(engine.store.bundle, registry=content_v1.v1_registry()),
            engine.store.company_id,
            engine.store.database_id,
        )

        with engine.store.connection(read_only=True) as connection:
            row = connection.execute(
                "SELECT * FROM period_close ORDER BY period DESC LIMIT 1"
            ).fetchone()
            manifest = original_decode(connection, row)
            connection.execute("BEGIN")
            with historical_content(1):
                assert verify_owner_review_integrity(
                    connection, Engine(historical_store), manifest
                ) == {"status": "verified"}
        assert "__init__" in decode_callers
        assert "<genexpr>" in decode_callers
        created = create_portable(
            engine.store.path, tmp_path / "backups", _bundle=engine.store.bundle
        )
        verified = verify_portable(created["path"], _bundle=engine.store.bundle)
        assert verified["database_format"]["status"] == "released"
        assert verified["database_format"]["version"] == 1
        constructed = []
        original_prefix = position_v1._verified_close_prefix

        def counted_prefix(*args):
            constructed.append(True)
            return original_prefix(*args)

        monkeypatch.setattr(position_v1, "_verified_close_prefix", counted_prefix)
        damage(
            engine,
            "close_storage_block",
            "UPDATE close_storage_block SET content='{}' "
            "WHERE period=(SELECT max(period) FROM period_close) "
            "AND field='management_snapshot'",
        )
        with engine.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            with pytest.raises(KernelError):
                content_v1.verify_v1_company(connection, engine.store.bundle)
        assert not constructed, "damaged close bodies must fail before prefix reuse"
    finally:
        content_v1.v1_registry.cache_clear()


def test_readiness_section_is_bounded_and_full_logical_close_is_unchanged(book, monkeypatch):
    engine, period = _close_report_book(book)
    with engine.store.connection(read_only=True) as connection:
        row = connection.execute("SELECT * FROM period_close WHERE period=?", (period,)).fetchone()
        current = close_storage.decode_close(connection, row)
        historical = close_storage_v1.decode_close(connection, row)
        assert current == historical
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


def test_full_close_decode_reads_each_authenticated_family_and_adoption_once(book, monkeypatch):
    engine, period = _close_report_book(book)
    with engine.store.connection(read_only=True) as connection:
        row = connection.execute("SELECT * FROM period_close WHERE period=?", (period,)).fetchone()
        results = []
        for reader in (close_storage, close_storage_v1):
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
