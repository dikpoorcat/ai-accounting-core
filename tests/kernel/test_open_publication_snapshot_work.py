"""Shared owner read proofs reduce real work without enlarging their meaning."""

import pytest
from stage9_metrics import measure_work
from test_engine import close, publish, save
from test_engine import engine as engine  # noqa: F401
from test_integrity_content import damage

from ai_accounting.kernel import query_reads
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.query_reads import QueryReads, verify_current_voucher_publications
from ai_accounting.kernel.types import YearMonth

JAN = YearMonth("2026-01").ordinal
FEB = JAN + 1
MAR = JAN + 2


def vouchers(connection):
    return {row[0] for row in connection.execute("SELECT version_id FROM voucher_current")}


def plain(mapping):
    return {key: dict(value) for key, value in mapping.items()}


def two_open_months(engine):
    save(engine, subject="jan", amount=100, request="jan")
    save(engine, subject="feb", amount=200, period="2026-02", request="feb")
    publish(engine, ["jan", "feb"])


def test_public_and_owned_calls_share_successful_whole_prefix(engine):
    two_open_months(engine)

    def read(repeat=False):
        with QueryReads.snapshot(engine) as reads:
            reads.verify_open_voucher_scope(MAR)
            if repeat:
                # Report callers lacking a local reads argument must share the
                # same owned transaction, including a smaller exact month.
                query_reads.verify_open_voucher_scope(reads.connection, FEB)
                query_reads.verify_open_voucher_scope(
                    reads.connection, JAN, posting_period=JAN,
                )
                reads.verify_open_voucher_scope(FEB, posting_period=FEB)
            return plain(verify_current_voucher_publications(
                reads.connection, vouchers(reads.connection),
            ))

    once_work, once = measure_work(engine, read)
    repeat_work, repeat = measure_work(engine, lambda: read(True))
    assert repeat == once and len(once) == 2
    for counter in ("sql_calls", "returned_rows", "returned_value_bytes"):
        assert repeat_work["counters"][counter] == once_work["counters"][counter]
    assert repeat_work["counters"]["calculation_result_rows_loaded"] == 0


def test_exact_month_cannot_hide_a_damaged_other_open_month(engine):
    two_open_months(engine)
    damage(engine, "voucher_current", "DELETE FROM voucher_current WHERE voucher_id="
           "(SELECT voucher_id FROM calculation_publication WHERE subject_id='jan')")
    with QueryReads.snapshot(engine) as reads:
        reads.verify_open_voucher_scope(FEB, posting_period=FEB)
        assert None not in reads._verified_open_voucher_scopes
        with pytest.raises(KernelError) as failure:
            query_reads.verify_open_voucher_scope(reads.connection, FEB)
        assert failure.value.code == "content_integrity_failed"
        assert None not in reads._verified_open_voucher_scopes
        # A failed broader request does not silently become a successful prefix.
        with pytest.raises(KernelError):
            reads.verify_open_voucher_scope(FEB)


def test_actual_closed_boundary_proves_exact_month_equals_open_prefix(engine):
    save(engine)
    publish(engine)
    close(engine)
    save(engine, subject="feb", amount=250, period="2026-02", request="feb")
    publish(engine, ["feb"], request="feb-publish")

    def read(repeat=False):
        with QueryReads.snapshot(engine) as reads:
            reads.verify_open_voucher_scope(FEB, posting_period=FEB)
            if repeat:
                query_reads.verify_open_voucher_scope(reads.connection, FEB)
                reads.verify_open_voucher_scope(JAN)
            return plain(verify_current_voucher_publications(
                reads.connection, vouchers(reads.connection),
            ))

    once_work, once = measure_work(engine, read)
    repeat_work, repeat = measure_work(engine, lambda: read(True))
    assert repeat == once and len(once) == 1
    for counter in ("sql_calls", "returned_rows", "returned_value_bytes"):
        assert repeat_work["counters"][counter] == once_work["counters"][counter]


def test_subject_check_cannot_promote_to_full_scope(engine):
    two_open_months(engine)
    damage(engine, "voucher_current", "DELETE FROM voucher_current WHERE voucher_id="
           "(SELECT voucher_id FROM calculation_publication WHERE subject_id='jan')")
    with QueryReads.snapshot(engine) as reads:
        query_reads.verify_open_voucher_scope(reads.connection, FEB, subject_ids={"feb"})
        assert reads._verified_open_voucher_scopes == {}
        with pytest.raises(KernelError):
            reads.verify_open_voucher_scope(FEB)


def test_empty_subject_work_does_not_grow_with_unrelated_open_vouchers(engine, record_property):
    observed = []
    for count in (8, 32):
        subjects = [f"unrelated-{index}" for index in range(count)]
        for index in range(count // 4 if count == 32 else 0, count):
            save(engine, subject=subjects[index], amount=index + 1, request=subjects[index])
        publish(engine, subjects, request=f"publish-{count}")

        def read():
            with QueryReads.snapshot(engine) as reads:
                query_reads.verify_open_voucher_scope(reads.connection, JAN, subject_ids=iter(()))
                assert not reads._verified_open_voucher_scopes
                assert not reads._verified_publication_ids
                return "empty"

        work, actual = measure_work(engine, read)
        assert actual == "empty"
        observed.append(work["counters"])
        # Only transaction/identity bookkeeping remains, no open-tail selector.
        assert work["counters"]["sql_calls"] <= 3
        assert work["counters"].get("returned_rows", 0) <= 1
        assert work["counters"].get("sqlite_vm_steps", 0) == 0
        record_property(f"empty_scope_{count}_work", work["counters"])
    assert observed[0] == observed[1]


def test_empty_subject_success_cannot_hide_missing_publication_for_generator_or_none(engine):
    two_open_months(engine)
    damage(engine, "calculation_publication",
           "DELETE FROM calculation_publication WHERE subject_id='feb'", foreign_keys=False)
    with QueryReads.snapshot(engine) as reads:
        query_reads.verify_open_voucher_scope(reads.connection, FEB, subject_ids=())
        assert not reads._verified_open_voucher_scopes
        for subjects in (iter(("feb",)), {"feb"}, None):
            with pytest.raises(KernelError) as failure:
                query_reads.verify_open_voucher_scope(reads.connection, FEB, subject_ids=subjects)
            assert failure.value.code == "content_integrity_failed"
            assert not reads._verified_open_voucher_scopes


@pytest.mark.parametrize("corruption", ["fact_body", "fact_seal", "evidence"])
def test_fact_only_verification_keeps_body_seal_and_evidence_checks(engine, corruption):
    from ai_accounting.kernel.integrity import verify_sources

    save(engine)
    publish(engine)
    with engine.store.connection(read_only=True) as connection:
        fact_id = connection.execute(
            "SELECT fact_id FROM fact_current WHERE subject_id='charge'"
        ).fetchone()[0]
        proof = connection.execute("SELECT digest FROM evidence").fetchone()[0]
        assert verify_sources(engine, connection, fact_ids={fact_id})["facts"] == 1
    if corruption == "fact_body":
        damage(engine, "fact_test_charge",
               "UPDATE fact_test_charge SET amount=amount+1 WHERE revision_id=?", (fact_id,))
    elif corruption == "fact_seal":
        damage(engine, "fact_seal", "DELETE FROM fact_seal WHERE fact_id=?", (fact_id,),
               foreign_keys=False)
    else:
        damage(engine, "evidence", "UPDATE evidence SET content=? WHERE digest=?",
               (b"changed evidence", proof))
    with QueryReads.snapshot(engine) as reads:
        for _ in range(2):
            with pytest.raises(KernelError) as failure:
                reads.verify_fact_versions({fact_id})
            assert failure.value.code == "content_integrity_failed"
            assert not reads._fact_versions and not reads._verified_open_voucher_scopes


def test_closed_and_fixed_history_empty_scope_do_not_create_current_proofs(engine):
    save(engine)
    publish(engine)
    close(engine)
    with QueryReads.snapshot(engine) as reads:
        query_reads.verify_open_voucher_scope(reads.connection, JAN, subject_ids=set())
        assert not reads._verified_open_voucher_scopes
        reads.verify_open_voucher_scope(JAN)
        assert reads._verified_open_voucher_scopes == {None: JAN}

    class Unconsumed:
        def __iter__(self):
            raise AssertionError("current selector consumed a fixed-reader argument")

    with QueryReads.snapshot(engine) as reads, historical_content(1):
        query_reads.verify_open_voucher_scope(reads.connection, JAN, subject_ids=Unconsumed())
        assert not reads._verified_open_voucher_scopes


def test_larger_future_prefix_is_checked_and_does_not_reuse_smaller_success(engine):
    two_open_months(engine)
    save(engine, subject="march", period="2026-03", amount=300, request="march")
    publish(engine, ["march"], request="march-publish")
    damage(engine, "voucher_current", "DELETE FROM voucher_current WHERE voucher_id="
           "(SELECT voucher_id FROM calculation_publication WHERE subject_id='march')")
    with QueryReads.snapshot(engine) as reads:
        reads.verify_open_voucher_scope(FEB)
        with pytest.raises(KernelError):
            reads.verify_open_voucher_scope(MAR)
        assert reads._verified_open_voucher_scopes[None] == FEB


@pytest.mark.parametrize("mode", ["initial", "review", "replace"])
def test_reused_relationships_keep_exact_original_and_current_receipts(engine, mode):
    save(engine)
    _, initial = publish(engine)
    if mode != "initial":
        save(engine, amount=100 if mode == "review" else 150, revision=1, request="second")
        publish(engine, request="second-publish")
    with engine.store.connection(read_only=True) as connection:
        all_ids = {row[0] for row in connection.execute("SELECT id FROM voucher_version")}
        expected = plain(verify_current_voucher_publications(connection, all_ids))

    def read(repeat=False):
        with QueryReads.snapshot(engine) as reads:
            actual = plain(verify_current_voucher_publications(reads.connection, all_ids))
            if repeat:
                assert plain(verify_current_voucher_publications(
                    reads.connection, all_ids,
                )) == actual
            assert not reads._verified_source_contents
            return actual

    once_work, once = measure_work(engine, read)
    repeat_work, repeat = measure_work(engine, lambda: read(True))
    assert once == repeat == expected
    assert all("has_successor" in row for row in once.values())
    for counter in ("sql_calls", "returned_rows", "returned_value_bytes"):
        assert repeat_work["counters"][counter] == once_work["counters"][counter]
    if mode == "review":
        assert initial["results"][0]["calculation_id"] in once
        assert len(once) == 2


def test_mixed_hit_miss_returns_only_requested_versions_and_checks_unknown_id(engine):
    two_open_months(engine)
    with QueryReads.snapshot(engine) as reads:
        ids = sorted(vouchers(reads.connection))
        first = verify_current_voucher_publications(reads.connection, {ids[0]})
        combined = verify_current_voucher_publications(reads.connection, ids)
        second = verify_current_voucher_publications(reads.connection, {ids[1]})
        assert combined.keys() == first.keys() | second.keys()
        assert first.keys().isdisjoint(second)
        with pytest.raises(KernelError):
            verify_current_voucher_publications(reads.connection, {ids[0], "missing-version"})
        assert plain(verify_current_voucher_publications(
            reads.connection, {ids[1]},
        )) == plain(second)


def test_failed_relationship_batch_does_not_publish_partial_proof(engine):
    two_open_months(engine)
    damage(engine, "calculation_publication", "UPDATE calculation_publication "
           "SET voucher_id=(SELECT voucher_id FROM calculation_publication "
           "WHERE subject_id='jan') WHERE subject_id='feb'")
    with QueryReads.snapshot(engine) as reads:
        ids = vouchers(reads.connection)
        for _ in range(2):
            with pytest.raises(KernelError):
                verify_current_voucher_publications(reads.connection, ids)
            assert reads._verified_current_voucher_publications == {}


@pytest.mark.parametrize("damage_kind", ["original_missing", "original_voucher"])
def test_failed_head_batch_keeps_only_earlier_independent_success(engine, damage_kind):
    two_open_months(engine)
    save(engine, subject="feb", amount=200, period="2026-02", revision=1, request="review")
    publish(engine, ["feb"], request="review-publish")
    with engine.store.connection(read_only=True) as connection:
        original = connection.execute(
            "SELECT v.calculation_id FROM voucher_current h "
            "JOIN voucher_version v ON v.id=h.version_id "
            "JOIN calculation c ON c.id=v.calculation_id WHERE c.subject_id='feb'"
        ).fetchone()[0]
    if damage_kind == "original_missing":
        damage(engine, "calculation_publication",
               "DELETE FROM calculation_publication WHERE calculation_id=?",
               (original,), foreign_keys=False)
    else:
        damage(engine, "calculation_publication",
               "UPDATE calculation_publication SET voucher_id=NULL WHERE calculation_id=?",
               (original,))
    with QueryReads.snapshot(engine) as reads:
        reads.verify_open_voucher_scope(JAN)
        prior_records = dict(reads._verified_publication_ids)
        prior_relations = dict(reads._verified_current_voucher_publications)
        assert len(prior_records) == 1
        for _ in range(2):
            with pytest.raises(KernelError) as failure:
                reads.verify_open_voucher_scope(FEB)
            assert failure.value.code == "content_integrity_failed"
            assert reads._verified_publication_ids == prior_records
            assert reads._verified_current_voucher_publications == prior_relations
            assert reads._verified_open_voucher_scopes == {None: JAN}


@pytest.mark.parametrize("boundary", ["unowned", "historical", "registry_v1"])
def test_other_boundaries_keep_independent_relation_reads(engine, monkeypatch, boundary):
    save(engine)
    publish(engine)
    with QueryReads.snapshot(engine) as reads:
        if boundary == "unowned":
            # Reset the active owner without changing the transaction or IDs.
            token = query_reads._active_fact_reads.set(QueryReads(engine, reads.connection))
        else:
            token = None
        if boundary == "registry_v1":
            monkeypatch.setattr(engine.store.registry, "content_version", 1, raising=False)
        try:
            if boundary == "historical":
                with historical_content(1):
                    returned = verify_current_voucher_publications(
                        reads.connection, vouchers(reads.connection),
                    )
            else:
                returned = verify_current_voucher_publications(
                    reads.connection, vouchers(reads.connection),
                )
            assert returned
            assert reads._verified_current_voucher_publications == {}
        finally:
            if token is not None:
                query_reads._active_fact_reads.reset(token)


def test_exit_clears_proofs_and_next_request_detects_removed_source(engine):
    save(engine)
    publish(engine)
    with QueryReads.snapshot(engine) as reads:
        reads.verify_open_voucher_scope(JAN)
        assert reads._verified_current_voucher_publications
    assert reads._verified_open_voucher_scopes == {}
    assert reads._verified_current_voucher_publications == {}
    damage(engine, "voucher_current", "DELETE FROM voucher_current")
    with QueryReads.snapshot(engine) as later:
        with pytest.raises(KernelError):
            later.verify_open_voucher_scope(JAN)
        assert later._verified_open_voucher_scopes == {}


def test_relationship_signatures_are_created_once_per_input_row(engine, monkeypatch):
    two_open_months(engine)
    original = query_reads._current_voucher_signature
    calls = []

    def signature(row):
        result = original(row)
        calls.append(result)
        return result

    monkeypatch.setattr(query_reads, "_current_voucher_signature", signature)
    with QueryReads.snapshot(engine) as reads:
        rows = query_reads._current_voucher_publication_headers(
            reads.connection, vouchers(reads.connection),
        )
        expected = [original(row) for row in rows]
        first = query_reads._verify_current_voucher_publication_rows(reads.connection, rows)
        assert calls == expected
        assert [reads._verified_current_voucher_publications[row["id"]][0]
                for row in rows] == expected
        repeated = query_reads._verify_current_voucher_publication_rows(reads.connection, rows)
        assert plain(repeated) == plain(first)
        assert calls == expected + expected


@pytest.mark.parametrize("damage_kind", ["missing_field", "duplicate_identity"])
def test_relationship_input_failure_keeps_batch_proofs_empty(engine, damage_kind):
    save(engine)
    publish(engine)
    with QueryReads.snapshot(engine) as reads:
        row = dict(query_reads._current_voucher_publication_headers(
            reads.connection, vouchers(reads.connection),
        )[0])
        damaged = dict(row)
        if damage_kind == "missing_field":
            del damaged["subject_id"]
            expected_error = KeyError
        else:
            damaged["voucher_id"] = "different-voucher"
            expected_error = KernelError
        for _ in range(2):
            with pytest.raises(expected_error) as failure:
                query_reads._verify_current_voucher_publication_rows(
                    reads.connection, [row, damaged],
                )
            if damage_kind == "missing_field":
                assert failure.value.args == ("subject_id",)
            else:
                assert failure.value.code == "content_integrity_failed"
                assert "当前凭证缺少正式发布采用" in str(failure.value)
            assert reads._verified_current_voucher_publications == {}
            assert reads._verified_publication_ids == {}
