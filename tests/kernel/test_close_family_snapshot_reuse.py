"""Shared accounting subroots never replace independent complete frozen fields."""

import pytest
from test_engine import close, publish, save
from test_engine import engine as _engine
from test_integrity_content import damage

from ai_accounting.kernel import close_storage
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.types import YearMonth

engine = _engine


def _closed(engine):
    save(engine)
    save(engine, subject="other", amount=200, request="other")
    publish(engine, subjects=["charge", "other"])
    close(engine)


def _row(reads):
    return reads.authoritative_close_rows(periods=(YearMonth("2026-01").ordinal,))[0]


def test_close_section_rejects_caller_cache_foreign_connection_and_expired_snapshot(engine):
    _closed(engine)
    with engine.store.connection(read_only=True) as other:
        with QueryReads.snapshot(engine) as reads:
            row = _row(reads)
            header = reads.close_header(row)
            parts = reads._verified_close_storage_parts
            with pytest.raises(KernelError) as failure:
                close_storage.read_section(reads.connection, header, "vouchers", _verified_parts={})
            assert failure.value.details["reason"] == "storage_section_snapshot_invalid"
            with pytest.raises(KernelError):
                close_storage.read_section(other, header, "vouchers", _verified_parts=parts)
            assert not parts
            vouchers = reads.close_section(row, "vouchers")
            assert len(vouchers) == 2
            assert {item["total"] for item in vouchers} == {100, 200}
        with pytest.raises(KernelError):
            close_storage.read_section(other, header, "vouchers", _verified_parts=parts)


@pytest.mark.parametrize("prime_adopted", [False, True])
def test_shared_accounting_family_does_not_hide_damaged_complete_voucher_leaf(
    engine, prime_adopted,
):
    _closed(engine)
    damage(engine, "close_storage_block",
           "UPDATE close_storage_block SET content='[]' WHERE field='vouchers'")
    with QueryReads.snapshot(engine) as reads:
        row = _row(reads)
        parts = reads._verified_close_storage_parts
        if prime_adopted:
            adopted = reads.close_section(row, "adopted_results")
            assert len(adopted) == 2
            assert parts
        prior = dict(parts)
        with pytest.raises(KernelError) as failure:
            reads.close_section(row, "vouchers")
        assert failure.value.details["reason"] == "storage_block_digest_mismatch"
        assert parts == prior  # Failed complete reads cannot publish new family success.
        assert (row["period"], "vouchers") not in reads._close_sections


def test_cached_section_cannot_hide_damage_after_an_attached_transaction_writes(engine):
    _closed(engine)
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        with QueryReads.attached_snapshot(engine, connection) as reads:
            connection.execute("PRAGMA query_only=ON")
            row = _row(reads)
            original = reads.close_section(row, "vouchers")
            assert len(original) == 2
            assert (row["period"], "vouchers") in reads._close_sections
            connection.execute("PRAGMA query_only=OFF")
            triggers = list(connection.execute(
                "SELECT name,sql FROM sqlite_schema WHERE type='trigger' "
                "AND tbl_name='close_storage_block'"
            ))
            for name, _ in triggers:
                connection.execute(f'DROP TRIGGER "{name}"')
            connection.execute("UPDATE close_storage_block SET content='[]' WHERE field='vouchers'")
            for _, sql in triggers:
                connection.execute(sql)
            connection.execute("PRAGMA query_only=ON")
            with pytest.raises(KernelError) as failure:
                reads.close_section(row, "vouchers")
            assert failure.value.details["reason"] == "storage_block_digest_mismatch"
            assert reads._close_sections[row["period"], "vouchers"] == original
