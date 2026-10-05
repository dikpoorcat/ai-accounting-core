"""Selected live heads remain bound to independent frozen voucher contents."""

import pytest
from test_asset_batch_reads import prepare_batch_assets
from test_asset_owner_frozen_scope import amend_activation
from test_asset_owner_frozen_scope import owner_book as _owner_book
from test_integrity_content import damage, verify
from test_payroll_corrections import Company

from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.read_indexes import selected_voucher_references
from ai_accounting.kernel.types import YearMonth

owner_book = _owner_book
MARCH = YearMonth("2026-03").ordinal


def consumption_voucher(engine):
    with engine.store.connection(read_only=True) as connection:
        return dict(connection.execute(
            "SELECT v.*,n.number,c.subject_id FROM voucher_version v "
            "JOIN voucher n ON n.id=v.voucher_id JOIN calculation c ON c.id=v.calculation_id "
            "WHERE c.kind='asset_consumption_month'",
        ).fetchone())


def damage_header(engine, target, field):
    if field == "reverses_id":
        with engine.store.connection(read_only=True) as connection:
            changed = connection.execute(
                "SELECT v.id FROM voucher_version v JOIN calculation c ON c.id=v.calculation_id "
                "WHERE c.kind='asset_activation_batch'",
            ).fetchone()[0]
    else:
        changed = target[field] + 1000
    table = "voucher" if field == "number" else "voucher_version"
    ident = target["voucher_id"] if field == "number" else target["id"]
    damage(engine, table, f"UPDATE {table} SET {field}=? WHERE id=?", (changed, ident))


def assert_consumers_reject(engine, subject):
    with Dashboard(engine)._snapshot("2026-03") as snap:
        for consume in (
            lambda: snap.queries._asset_owner_metadata_selection(snap.connection, snap.period),
            lambda: snap.queries._selected_accounting(snap.connection, subject, snap.period),
        ):
            with pytest.raises(KernelError) as failure:
                consume()
            assert failure.value.code == "content_integrity_failed"
            assert ("asset_owner_identity_selection", MARCH) not in (
                snap.reads._report_snapshot_cache
            )
    for consume in (
        lambda: Dashboard(engine).assets("2026-03", preparation="deferred"),
        lambda: Dashboard(engine).assets("2026-03", asset_id="computer", preparation="deferred"),
        lambda: BusinessQueries(engine).business_status(subject, "2026-03"),
        lambda: verify(engine),
    ):
        with pytest.raises(KernelError):
            consume()


@pytest.mark.parametrize("field", ["reverses_id", "total", "number"])
@pytest.mark.parametrize("warm", [False, True])
def test_changed_frozen_header_rejects_narrow_rows_and_all_asset_consumers(owner_book, field, warm):
    company, _ = owner_book
    engine = company.engine
    target = consumption_voucher(engine)
    selected = [{"id": target["id"], "close_period": MARCH}]
    with QueryReads.snapshot(engine) as reads:
        reads.verify_selected_voucher_adoptions(selected, through_period=MARCH)
        before = reads._verified_frozen_voucher_headers.copy()
        reads.verify_selected_voucher_adoptions(selected, through_period=MARCH)
        assert reads._verified_frozen_voucher_headers == before
    damage_header(engine, target, field)
    for _ in range(2):
        with QueryReads.snapshot(engine) as reads:
            if warm:
                refs = selected_voucher_references(
                    reads.connection, [target["id"]], through_period=MARCH,
                )
                reads.verify_close_references(refs)
            before = reads._verified_close_references.copy()
            with pytest.raises(KernelError) as failure:
                reads.verify_selected_voucher_adoptions(selected, through_period=MARCH)
            assert failure.value.code == "content_integrity_failed"
            assert failure.value.details["reason"] == "frozen_voucher_header_mismatch"
            assert not reads._verified_frozen_voucher_headers
            assert reads._verified_close_references == before
    assert_consumers_reject(engine, target["subject_id"])


@pytest.mark.parametrize("missing", ["moved_period", "voucher", "calculation", "number"])
def test_frozen_head_cannot_disappear_from_asset_selection(owner_book, missing):
    company, _ = owner_book
    engine = company.engine
    target = consumption_voucher(engine)
    if missing == "moved_period":
        damage(engine, "voucher_version", "UPDATE voucher_version SET period=? WHERE id=?",
               (YearMonth("2026-04").ordinal, target["id"]))
    else:
        table, ident = {
            "voucher": ("voucher_version", target["id"]),
            "calculation": ("calculation", target["calculation_id"]),
            "number": ("voucher", target["voucher_id"]),
        }[missing]
        damage(engine, table, f"DELETE FROM {table} WHERE id=?", (ident,), foreign_keys=False)
    assert_consumers_reject(engine, target["subject_id"])


@pytest.mark.parametrize("fault", ["missing", "type", "path", "related"])
def test_header_proof_retains_every_matched_reference_check(owner_book, fault):
    company, _ = owner_book
    engine = company.engine
    target = consumption_voucher(engine)
    selected = [{"id": target["id"], "close_period": MARCH}]
    if fault == "missing":
        statement = "DELETE FROM close_reference WHERE reference_id=? AND reference_type='voucher'"
        parameters = (target["id"],)
    else:
        column, value = {
            "type": ("reference_type", "calculation"),
            "path": ("path", "adopted_results[*].calculation_id"),
            "related": ("related_id", "wrong-related-id"),
        }[fault]
        statement = (f"UPDATE close_reference SET {column}=? "
                     "WHERE reference_id=? AND reference_type='voucher'")
        parameters = value, target["id"]
    damage(engine, "close_reference", statement, parameters)
    with QueryReads.snapshot(engine) as reads:
        with pytest.raises(KernelError):
            reads.verify_selected_voucher_adoptions(selected, through_period=MARCH)
        assert not reads._verified_frozen_voucher_headers
        assert not reads._verified_close_references


def test_no_impact_review_binds_original_voucher_not_new_adopted_basis(tmp_path):
    company = Company(tmp_path / "review-frozen-header.sqlite")
    prepare_batch_assets(company)
    evidence = company.engine.register_evidence(
        b"Additional identical activation evidence", "text/plain", "review",
        request_id="review-proof",
    )["digest"]
    amended = amend_activation(company, evidence, revision=1, posting_period="2026-02",
                               benefit_area="administration")
    assert any(item["impact"] == "review_no_impact" for item in amended["results"])
    company.close("2026-02")
    with QueryReads.snapshot(company.engine) as reads:
        period = YearMonth("2026-02").ordinal
        rows = [dict(row) for row in reads.connection.execute(
            "SELECT v.*,n.number,? close_period,v.calculation_id voucher_calculation_id "
            "FROM voucher_version v JOIN voucher n ON n.id=v.voucher_id WHERE v.period=?",
            (period, period),
        )]
        reads.verify_selected_voucher_adoptions(rows, through_period=period)
        close = reads.authoritative_close_rows(periods=(period,))[0]
        vouchers = reads.close_section(close, "vouchers")
        assert any(item["calculation_id"] != item["adopted_calculation_id"] for item in vouchers)
    Dashboard(company.engine).assets("2026-02", preparation="deferred")
    assert verify(company.engine)["status"] == "verified"


def test_owned_reference_scope_reuse_keeps_fields_and_cutoff_exact(owner_book):
    company, _ = owner_book
    engine = company.engine
    target = consumption_voucher(engine)
    selected = [{"id": target["id"], "close_period": MARCH}]
    for _ in range(2):
        with QueryReads.snapshot(engine) as reads:
            statements = []
            reads.connection.set_trace_callback(statements.append)
            reads.verify_selected_voucher_adoptions(selected, through_period=MARCH)
            assert any("close_reference_lookup" in sql for sql in statements)
            statements.clear()
            reads.verify_selected_voucher_adoptions(selected, through_period=MARCH)
            assert not statements
            with pytest.raises(KernelError):
                reads.verify_selected_voucher_adoptions(
                    [selected[0] | {"number": target["number"] + 1}], through_period=MARCH,
                )
            assert not statements
            reads.verify_selected_voucher_adoptions(selected, through_period=MARCH + 1)
            assert any("close_reference_lookup" in sql for sql in statements)
            reads.connection.set_trace_callback(None)


class NoFullCacheWalk(dict):
    def __iter__(self):
        raise AssertionError("An exact voucher proof must not walk the full snapshot cache")

    def keys(self):
        raise AssertionError("An exact voucher proof must not walk the full snapshot cache")


def test_exact_header_group_stages_only_new_parts_and_retains_prior_proof_on_failure(owner_book):
    company, _ = owner_book
    engine = company.engine
    target = consumption_voucher(engine)
    damage_header(engine, target, "total")
    with QueryReads.snapshot(engine) as reads:
        # A realistic earlier close proof supplies both root and bucket maps.
        feb = YearMonth("2026-02").ordinal
        close = reads.authoritative_close_rows(periods=(feb,))[0]
        reads.close_section(close, "vouchers")
        old_headers = dict(reads._close_headers)
        old_parts = dict(reads._verified_close_storage_parts)
        reads._close_headers = NoFullCacheWalk(old_headers)
        reads._verified_close_storage_parts = NoFullCacheWalk(old_parts)
        with pytest.raises(KernelError) as failure:
            reads.verify_selected_voucher_adoptions(
                [{"id": target["id"], "close_period": MARCH}], through_period=MARCH,
            )
        assert failure.value.details["reason"] == "frozen_voucher_header_mismatch"
        assert reads._close_headers == old_headers
        assert reads._verified_close_storage_parts == old_parts
        assert not reads._verified_close_references
        assert not reads._verified_frozen_voucher_headers
        assert not reads._verified_selected_voucher_adoptions


@pytest.mark.parametrize("field", ["voucher_id", "calculation_id", "period", "id"])
def test_narrow_projection_binds_remaining_actual_header_identity_fields(owner_book, field):
    company, _ = owner_book
    engine = company.engine
    target = consumption_voucher(engine)
    if field == "period":
        changed = MARCH + 1
    elif field == "id":
        changed = "deliberately-renamed-frozen-voucher"
    else:
        with engine.store.connection(read_only=True) as connection:
            changed = connection.execute(
                f"SELECT {field} FROM voucher_version WHERE period=? ORDER BY id LIMIT 1",
                (YearMonth("2026-02").ordinal,),
            ).fetchone()[0]
    damage(engine, "voucher_version", f"UPDATE voucher_version SET {field}=? WHERE id=?",
           (changed, target["id"]), foreign_keys=field != "id")
    with QueryReads.snapshot(engine) as reads:
        with pytest.raises(KernelError):
            reads.verify_selected_voucher_adoptions(
                [{"id": target["id"], "close_period": MARCH}], through_period=MARCH,
            )
        assert not reads._verified_selected_voucher_adoptions
        assert not reads._verified_frozen_voucher_headers


def test_unowned_reader_rechecks_live_headers_even_with_stale_private_maps(owner_book):
    company, _ = owner_book
    engine = company.engine
    target = consumption_voucher(engine)
    selected = [{"id": target["id"], "close_period": MARCH}]
    with QueryReads.snapshot(engine) as reads:
        reads.verify_selected_voucher_adoptions(selected, through_period=MARCH)
        old_headers = reads._verified_frozen_voucher_headers.copy()
        old_scopes = reads._verified_selected_voucher_adoptions.copy()
    damage_header(engine, target, "total")
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        unowned = QueryReads(engine, connection)
        unowned._verified_frozen_voucher_headers.update(old_headers)
        unowned._verified_selected_voucher_adoptions.update(old_scopes)
        with pytest.raises(KernelError) as failure:
            unowned.verify_selected_voucher_adoptions(selected, through_period=MARCH)
        assert failure.value.details["reason"] == "frozen_voucher_header_mismatch"


def test_second_header_failure_does_not_publish_first_new_head_or_reference_parts(owner_book):
    company, _ = owner_book
    engine = company.engine
    target = consumption_voucher(engine)
    damage_header(engine, target, "total")
    with QueryReads.snapshot(engine) as reads:
        earlier = reads.connection.execute(
            "SELECT id,period FROM voucher_version WHERE period=? ORDER BY id LIMIT 1",
            (YearMonth("2026-02").ordinal,),
        ).fetchone()
        with pytest.raises(KernelError) as failure:
            reads.verify_selected_voucher_adoptions([
                {"id": earlier["id"], "close_period": earlier["period"]},
                {"id": target["id"], "close_period": MARCH},
            ], through_period=MARCH)
        assert failure.value.details["record_id"] == target["id"]
        assert not reads._verified_close_references
        assert not reads._verified_close_storage_parts
        assert not reads._close_headers
        assert not reads._verified_frozen_voucher_headers
        assert not reads._verified_selected_voucher_adoptions


def test_fixed_reader_does_not_borrow_native_header_or_reference_scope_proof(
    owner_book, monkeypatch,
):
    from ai_accounting.kernel import close_storage, close_storage_v1

    company, _ = owner_book
    engine = company.engine
    target = consumption_voucher(engine)
    selected = [{"id": target["id"], "close_period": MARCH}]
    with QueryReads.snapshot(engine) as reads:
        reads.verify_selected_voucher_adoptions(selected, through_period=MARCH)
        old_headers = reads._verified_frozen_voucher_headers.copy()
        old_scopes = reads._verified_selected_voucher_adoptions.copy()
    fixed_sections = []
    original = close_storage_v1.read_section

    def fixed_section(connection, header, name):
        fixed_sections.append(name)
        return original(connection, header, name)

    def native_proof(*_, **__):
        raise AssertionError("fixed-v1 consumed a current voucher bucket proof")

    monkeypatch.setattr(close_storage_v1, "read_section", fixed_section)
    monkeypatch.setattr(close_storage, "voucher_reference_headers", native_proof)
    with QueryReads.snapshot(engine) as reads, historical_content(1):
        reads._verified_frozen_voucher_headers.update(old_headers)
        reads._verified_selected_voucher_adoptions.update(old_scopes)
        reads.verify_selected_voucher_adoptions(selected, through_period=MARCH)
        assert "vouchers" in fixed_sections
        assert reads._verified_frozen_voucher_headers == old_headers
    damage_header(engine, target, "total")
    with QueryReads.snapshot(engine) as reads, historical_content(1):
        reads._verified_frozen_voucher_headers.update(old_headers)
        reads._verified_selected_voucher_adoptions.update(old_scopes)
        with pytest.raises(KernelError) as failure:
            reads.verify_selected_voucher_adoptions(selected, through_period=MARCH)
        assert failure.value.details["reason"] == "frozen_voucher_header_mismatch"
