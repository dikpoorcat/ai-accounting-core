"""Physical voucher rows may be reused without replacing exact fund/source proofs."""

import pytest
from test_banking import book as _bank_book
from test_dashboard_funds_alignment import _publish_filter_funding
from test_dashboard_model_transactions import comparable, raw_read
from test_integrity_content import damage

from ai_accounting.kernel import fund_line_details as details
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.runtime import _PrivateConnection

bank_book = _bank_book


def _versions(connection):
    return [row[0] for row in connection.execute(
        "SELECT version_id FROM voucher_current ORDER BY version_id"
    )]


def test_raw_line_reuse_keeps_original_rows_apart_from_display_and_source_proof(
    bank_book, monkeypatch,
):
    engine = bank_book[0]
    _publish_filter_funding(engine, ["bank-a"] * 2)
    batches = []
    execute = _PrivateConnection.execute

    def observe(connection, sql, *args, **kwargs):
        if "JOIN voucher_line l ON l.version_id=ids.value" in sql:
            batches.append(args[0][0])
        return execute(connection, sql, *args, **kwargs)

    monkeypatch.setattr(_PrivateConnection, "execute", observe)
    with QueryReads.snapshot(engine) as reads:
        versions = _versions(reads.connection)
        visible = reads.voucher_lines(versions)
        assert len(batches) == 1
        original = reads.raw_voucher_lines(versions)
        # Display callers receive mutable dictionaries, not proof-bearing rows.
        visible[versions[0]][0]["debit"] = 999
        visible[versions[0]].append(dict(visible[versions[0]][0]))
        selected = details.verify_fund_line_details(
            reads.connection, version_ids=versions, calculation_ids=[], _voucher_reads=reads,
        )
        assert len(selected["lines"]) == 2
        assert {row["entity_id"] for row in selected["lines"]} == {"bank-a"}
        assert all(row["source_fact_id"] and row["basis_calculation_id"]
                   for row in selected["lines"])
        assert len(batches) == 1
        assert reads.raw_voucher_lines(versions) == original
        original[versions[0]][0]["credit"] = 888
        assert reads.raw_voucher_lines(versions) != original
        # A raw row reader is deliberately not a VerifiedSourceReads lease.
        with pytest.raises(KernelError) as failure:
            details.verify_fund_line_details(
                reads.connection, version_ids=versions, calculation_ids=[], _source_reads=reads,
            )
        assert failure.value.details["reason"] == "verified_source_reader_identity"
    with pytest.raises(KernelError, match="快照已失效"):
        reads.raw_voucher_lines(versions)


def test_raw_line_reuse_rejects_foreign_unowned_and_changed_transactions(bank_book):
    engine = bank_book[0]
    _publish_filter_funding(engine, ["bank-a"])
    with engine.store.connection(read_only=True) as other:
        with QueryReads.snapshot(engine) as reads:
            with pytest.raises(KernelError) as failure:
                details.verify_fund_line_details(
                    other, version_ids=[], calculation_ids=[], _voucher_reads=reads,
                )
            assert failure.value.details["reason"] == "voucher_reader_identity"
        other.execute("BEGIN")
        unowned = QueryReads(engine, other)
        with pytest.raises(KernelError, match="快照已失效"):
            unowned.raw_voucher_lines(())
    # Attached snapshots used during writing cannot lend stale physical rows.
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        with QueryReads.attached_snapshot(engine, connection) as reads:
            with pytest.raises(KernelError, match="快照已失效"):
                reads.raw_voucher_lines(())
            connection.execute("PRAGMA query_only=ON")
            versions = _versions(connection)
            assert reads.raw_voucher_lines(versions)
            connection.execute("PRAGMA query_only=OFF")
            connection.execute("CREATE TEMP TABLE line_reuse_guard(value INTEGER)")
            connection.execute("INSERT INTO line_reuse_guard VALUES(1)")
            connection.execute("PRAGMA query_only=ON")
            with pytest.raises(KernelError, match="快照已失效"):
                details.verify_fund_line_details(
                    connection, version_ids=versions, calculation_ids=[], _voucher_reads=reads,
                )


def test_prefilled_unowned_reader_cannot_lend_old_lines_to_a_new_attached_snapshot(bank_book):
    engine = bank_book[0]
    _publish_filter_funding(engine, ["bank-a"])
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        reads = QueryReads(engine, connection)
        versions = _versions(connection)
        old_lines = reads.voucher_lines(versions)
        reads._report_snapshot_cache["old-proof"] = True
        connection.commit()
        # Same connection/IDs, but a later transaction must observe physical damage.
        damage(engine, "voucher_line", "UPDATE voucher_line SET debit=debit+1 "
               "WHERE version_id=? AND debit>0", (versions[0],))
        connection.execute("BEGIN")
        with QueryReads.attached_snapshot(engine, connection, reads=reads) as attached:
            assert attached is reads
            assert not attached._report_snapshot_cache
            actual = attached.raw_voucher_lines(versions)
            assert actual != old_lines
            with pytest.raises(KernelError) as failure:
                details.verify_fund_line_details(
                    connection, version_ids=versions, calculation_ids=[], _voucher_reads=attached,
                )
            assert failure.value.details["reason"] == "posted_lines_differ_from_source"


@pytest.mark.parametrize("target", ["amount", "missing_line", "extra_line", "assignment",
                                   "fact_seal", "calculation_body"])
def test_cached_physical_lines_still_require_complete_rows_and_exact_fund_source(bank_book, target):
    engine = bank_book[0]
    _publish_filter_funding(engine, ["bank-a"])
    with engine.store.connection(read_only=True) as connection:
        version = _versions(connection)[0]
        calculation, fact = connection.execute(
            "SELECT c.id,c.fact_id FROM voucher_version v "
            "JOIN calculation c ON c.id=v.calculation_id WHERE v.id=?", (version,),
        ).fetchone()
    mutations = {
        "amount": ("voucher_line", "UPDATE voucher_line SET debit=debit+1 "
                   "WHERE version_id=? AND debit>0", (version,)),
        "missing_line": ("voucher_line", "DELETE FROM voucher_line "
                         "WHERE version_id=? AND line_no=1", (version,)),
        "extra_line": ("voucher_line", "INSERT INTO voucher_line "
                       "SELECT version_id,99,account,debit,credit,cashflow FROM voucher_line "
                       "WHERE version_id=? AND line_no=1", (version,)),
        "assignment": ("fund_line_detail", "UPDATE fund_line_detail SET source_fact_id=? "
                       "WHERE version_id=?", ("missing-fact", version)),
        "fact_seal": ("fact_seal", "DELETE FROM fact_seal WHERE fact_id=?", (fact,)),
        "calculation_body": ("calculation", "UPDATE calculation SET outcome='{}' WHERE id=?",
                             (calculation,)),
    }
    damage(engine, *mutations[target], foreign_keys=False)
    with QueryReads.snapshot(engine) as reads:
        reads.voucher_lines([version])
        with pytest.raises(KernelError):
            details.verify_fund_line_details(
                reads.connection, version_ids=[version], calculation_ids=[], _voucher_reads=reads,
            )
    # Both actual consumers retain their own complete row and returned-source proof.
    with raw_read(), pytest.raises(KernelError):
        Dashboard(engine).brief("2026-09", section="vouchers", preparation="deferred")
    with pytest.raises(KernelError):
        Dashboard(engine).brief("2026-09", section="vouchers", preparation="deferred")


def test_batched_fund_vouchers_keep_native_and_model_money_and_identities(bank_book):
    engine = bank_book[0]
    _publish_filter_funding(engine, ["bank-a"] * 3)
    dashboard = Dashboard(engine)
    with raw_read():
        native = dashboard.brief("2026-09", section="vouchers", limit=2)
    model = dashboard.brief("2026-09", section="vouchers", limit=2)
    assert comparable(model) == comparable(native)
    items = native["data"]["collections"]["vouchers"]["items"]
    assert len(items) == 2
    assert all(item["amount_fen"] == item["business_amount_fen"] == 1 for item in items)
    assert all(any(line["code"] == "1002.0001" for line in item["lines"]) for item in items)
