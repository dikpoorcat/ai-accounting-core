"""Own publication anchors prove selected source bytes, never adoption or ancestors."""

import hashlib
import json

import pytest
from stage9_metrics import measure_work
from test_integrity_content import damage
from test_reports import book as book  # noqa: F401
from test_reports import scenario

from ai_accounting.kernel import integrity, stored_json
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.report_open_contribution import verify_published_source_bindings
from ai_accounting.kernel.types import canonical, digest


@pytest.fixture
def sources(book):
    engine = book[0]
    scenario(book, classification=False, tax=False)
    with engine.store.connection(read_only=True) as connection:
        rows = {
            row["subject_id"]: dict(row)
            for row in connection.execute(
                "SELECT c.*,p.id publication_id,d.content FROM calculation c "
                "JOIN calculation_publication p ON p.calculation_id=c.id "
                "JOIN report_open_contribution d ON d.publication_id=p.id"
            )
        }
    return engine, rows


def _reject(engine, identifiers):
    with QueryReads.snapshot(engine) as reads:
        with pytest.raises(KernelError) as failure:
            verify_published_source_bindings(reads, identifiers)
        assert failure.value.code == "content_integrity_failed"
        assert not reads._anchored_source_bytes
        assert not reads._verified_sql_outcomes
        assert not reads._verified_saved_input_identities


def _replace_bound_content(engine, row, value):
    # Forge both saved digests to exercise identity checks independently of
    # checksum rejection. Normal writes cannot modify the immutable anchor.
    body = canonical(value)
    checksum = hashlib.sha256(body.encode()).digest()
    damage(engine, "report_open_contribution",
           "UPDATE report_open_contribution SET content=?,content_digest=? "
           "WHERE publication_id=?", (body, checksum, row["publication_id"]))
    damage(engine, "report_open_contribution_anchor",
           "UPDATE report_open_contribution_anchor SET content_digest=? WHERE publication_id=?",
           (checksum, row["publication_id"]))


def test_exact_own_source_proof_does_not_decode_dependencies_or_repeat_sql_decoder(
    sources, monkeypatch, record_property
):
    engine, rows = sources
    payment = rows["payment"]["id"]
    bound = json.loads(rows["payment"]["content"])["source_bindings"]
    assert rows["cost"]["id"] in {item[0] for item in bound}

    def unnecessary(*_args, **_kwargs):
        raise AssertionError("canonical own proof must not decode result or dependency inputs")

    monkeypatch.setattr(integrity, "_object", unnecessary)
    monkeypatch.setattr(QueryReads, "verify_saved_input_identity", unnecessary)
    monkeypatch.setattr(stored_json, "verify_outcome_bytes", unnecessary)

    def read():
        with QueryReads.snapshot(engine) as reads:
            assert verify_published_source_bindings(reads, {payment}) == frozenset()
            assert set(reads._anchored_source_bytes) == {payment}
            assert reads._verified_source_contents == {}
            reads.verify_sql_outcomes({payment})
            reads.verify_sql_outcomes({payment})
            assert reads._anchored_source_bytes.keys() == {payment}
            assert reads._verified_saved_input_identities == set()
            return reads

    work, reads = measure_work(engine, read)
    assert reads._anchored_source_bytes == {}
    assert not any("dependency_" in item["statement"] for item in work["sql"])
    record_property("own_proof_returned_rows", work["counters"]["returned_rows"])
    record_property("own_proof_returned_bytes", work["counters"]["returned_value_bytes"])


@pytest.mark.parametrize("case", [
    "publication", "anchor", "both", "anchor_owner", "publication_subject", "publication_digest",
    "anchor_digest", "body_digest", "body_damage",
])
def test_missing_or_wrong_publication_authority_never_falls_back(sources, case):
    engine, rows = sources
    row = rows["cost"]
    publication = row["publication_id"]
    if case in {"anchor", "both"}:
        damage(engine, "report_open_contribution_anchor",
               "DELETE FROM report_open_contribution_anchor WHERE publication_id=?",
               (publication,), foreign_keys=False)
    if case in {"publication", "both"}:
        damage(engine, "calculation_publication", "DELETE FROM calculation_publication WHERE id=?",
               (publication,), foreign_keys=False)
    elif case == "anchor_owner":
        # Remove the other anchor to permit a wrong but existing owner ID.
        damage(engine, "report_open_contribution_anchor",
               "DELETE FROM report_open_contribution_anchor WHERE calculation_id=?",
               (rows["capital"]["id"],), foreign_keys=False)
        damage(engine, "report_open_contribution_anchor",
               "UPDATE report_open_contribution_anchor SET calculation_id=? WHERE publication_id=?",
               (rows["capital"]["id"], publication))
    elif case == "publication_subject":
        damage(engine, "calculation_publication",
               "UPDATE calculation_publication SET subject_id='profile' WHERE id=?", (publication,))
    elif case == "publication_digest":
        damage(engine, "calculation_publication",
               "UPDATE calculation_publication SET sequence=sequence+100 WHERE id=?",
               (publication,))
    elif case == "anchor_digest":
        damage(engine, "report_open_contribution_anchor",
               "UPDATE report_open_contribution_anchor SET content_digest=zeroblob(32) "
               "WHERE publication_id=?", (publication,))
    elif case == "body_digest":
        damage(engine, "report_open_contribution",
               "UPDATE report_open_contribution SET content_digest=zeroblob(32) "
               "WHERE publication_id=?",
               (publication,))
    elif case == "body_damage":
        damage(engine, "report_open_contribution",
               "UPDATE report_open_contribution SET content='{}' WHERE publication_id=?",
               (publication,))
    _reject(engine, {rows["payment"]["id"], row["id"]})


@pytest.mark.parametrize("case", [
    "missing_own", "duplicate_own", "other_owner", "publication", "calculation", "header",
    "binding_fact", "format", "digest_shape", "binding_shape",
])
def test_own_binding_identity_remains_strict_when_saved_checksums_match(sources, case):
    engine, rows = sources
    row = rows["payment"]
    value = json.loads(row["content"])
    own = next(item for item in value["source_bindings"] if item[0] == row["id"])
    if case == "missing_own":
        value["source_bindings"].remove(own)
    elif case == "duplicate_own":
        value["source_bindings"].append(own)
    elif case == "other_owner":
        # A binding from this payment cannot become another source's own anchor.
        value["calculation_id"] = rows["cost"]["id"]
    elif case == "publication":
        value["publication_id"] = rows["cost"]["publication_id"]
    elif case == "calculation":
        value["calculation_id"] = "c_missing"
    elif case == "header":
        value["result_digest"] = "0" * 64
    elif case == "binding_fact":
        own[2] = rows["cost"]["fact_id"]
        value["fact_id"] = own[2]
    elif case == "format":
        value["format"] = "ai-accounting-kernel/2/report-open-contribution/999"
    elif case == "digest_shape":
        own[3] = "z" * 64
    else:
        own.append("extra")
    _replace_bound_content(engine, row, value)
    _reject(engine, {row["id"]})


@pytest.mark.parametrize("case", [
    "result", "result_and_digest", "fact_body", "fact_body_and_digest", "fact_digest",
    "fact_identity", "kind",
    "calculation_seal", "fact_seal", "physical_period", "duplicate_result",
])
def test_exact_result_fact_identity_and_seals_remain_authenticated(sources, case):
    engine, rows = sources
    row = rows["cost"]
    if case in {"result", "result_and_digest", "duplicate_result"}:
        value = json.loads(row["outcome"])
        value["values"]["creditor_kind"] = "other"
        raw = canonical(value)
        if case == "duplicate_result":
            raw = '{"values":{},' + row["outcome"][1:]
        if case == "result_and_digest":
            damage(engine, "calculation", "UPDATE calculation SET outcome=?,digest=? WHERE id=?",
                   (raw, digest(value), row["id"]))
        else:
            damage(engine, "calculation", "UPDATE calculation SET outcome=? WHERE id=?",
                   (raw, row["id"]))
    elif case in {"fact_body", "fact_body_and_digest"}:
        with engine.store.connection(read_only=True) as connection:
            original = engine.store.fact_data_many(connection, {row["fact_id"]})[row["fact_id"]]
        damage(engine, "fact_expense",
               "UPDATE fact_expense SET creditor_kind='employee' WHERE revision_id=?",
               (row["fact_id"],))
        if case == "fact_body_and_digest":
            original["creditor_kind"] = "employee"
            damage(engine, "fact_revision", "UPDATE fact_revision SET digest=? WHERE id=?",
                   (digest(original), row["fact_id"]))
    elif case == "fact_digest":
        damage(engine, "fact_revision", "UPDATE fact_revision SET digest=zeroblob(32) WHERE id=?",
               (row["fact_id"],))
    elif case == "fact_identity":
        damage(engine, "calculation", "UPDATE calculation SET fact_id=? WHERE id=?",
               (rows["capital"]["fact_id"], row["id"]))
    elif case == "kind":
        damage(engine, "calculation", "UPDATE calculation SET kind='cash_funding' WHERE id=?",
               (row["id"],))
    elif case == "physical_period":
        damage(engine, "fact_expense",
               "UPDATE fact_expense SET period=period+1 WHERE revision_id=?",
               (row["fact_id"],))
    else:
        field = "calculation_id" if case == "calculation_seal" else "fact_id"
        ident = row["id"] if case == "calculation_seal" else row["fact_id"]
        damage(engine, case, f"DELETE FROM {case} WHERE {field}=?", (ident,), foreign_keys=False)
    _reject(engine, {rows["payment"]["id"], row["id"]})


@pytest.mark.parametrize("bad_anchor", [False, True])
def test_missing_repairable_body_checks_complete_anchor_before_explicit_fallback(
    sources, bad_anchor
):
    engine, rows = sources
    row = rows["cost"]
    damage(engine, "report_open_contribution",
           "DELETE FROM report_open_contribution WHERE publication_id=?", (row["publication_id"],))
    if bad_anchor:
        damage(engine, "report_open_contribution_anchor",
               "UPDATE report_open_contribution_anchor SET content_digest=zeroblob(32) "
               "WHERE publication_id=?", (row["publication_id"],))
        _reject(engine, {row["id"]})
        return
    with QueryReads.snapshot(engine) as reads:
        fallback = verify_published_source_bindings(reads, {row["id"]})
        assert fallback == frozenset({row["id"]})
        assert reads._anchored_source_bytes == {}
        reads.verify_saved_input_identity(fallback)
        reads.verify_sql_outcomes(fallback)
        assert reads._verified_source_contents[row["id"]]["values"]["creditor_kind"] == "supplier"
        assert reads.connection.execute(
            "SELECT count(*) FROM report_open_contribution WHERE publication_id=?",
            (row["publication_id"],),
        ).fetchone()[0] == 0


def test_success_does_not_cross_snapshots_or_hide_a_failed_batch(sources):
    engine, rows = sources
    row = rows["cost"]
    with QueryReads.snapshot(engine) as reads:
        assert not verify_published_source_bindings(reads, {row["id"]})
        reads.verify_sql_outcomes({row["id"]})
    assert reads._anchored_source_bytes == {}
    damage(engine, "calculation", "UPDATE calculation SET outcome='{}' WHERE id=?", (row["id"],))
    _reject(engine, {row["id"], rows["payment"]["id"]})
    damage(engine, "calculation", "UPDATE calculation SET outcome=? WHERE id=?",
           (row["outcome"], row["id"]))
    with QueryReads.snapshot(engine) as reads:
        assert not verify_published_source_bindings(reads, {row["id"]})


def test_own_content_proof_does_not_expand_an_unconsumed_payment_parent(sources):
    engine, rows = sources
    damage(engine, "calculation", "UPDATE calculation SET outcome='{}' WHERE id=?",
           (rows["cost"]["id"],))
    with QueryReads.snapshot(engine) as reads:
        assert not verify_published_source_bindings(reads, {rows["payment"]["id"]})
        assert set(reads._anchored_source_bytes) == {rows["payment"]["id"]}
    _reject(engine, {rows["cost"]["id"]})


def test_equivalent_noncanonical_original_result_uses_strict_saved_decoder(sources, monkeypatch):
    engine, rows = sources
    row = rows["cost"]
    damage(engine, "calculation", "UPDATE calculation SET outcome=' '||outcome WHERE id=?",
           (row["id"],))
    decoded = []
    original = integrity._object

    def counted(raw, component, ident):
        decoded.append((component, ident))
        return original(raw, component, ident)

    monkeypatch.setattr(integrity, "_object", counted)
    with QueryReads.snapshot(engine) as reads:
        assert not verify_published_source_bindings(reads, {row["id"]})
    assert decoded == [("calculation", row["id"])]


def test_unowned_reader_cannot_mint_publication_proof(sources):
    engine, rows = sources
    with engine.store.connection(read_only=True) as connection:
        reads = QueryReads(engine, connection)
        with pytest.raises(ValueError, match="owned"):
            verify_published_source_bindings(reads, {rows["cost"]["id"]})


def test_existing_exact_publication_proof_skips_only_duplicate_record_hash(sources, monkeypatch):
    from ai_accounting.kernel import publication

    engine, rows = sources
    calls = []
    original = publication.verify_record

    def counted(row):
        calls.append(row["id"])
        return original(row)

    monkeypatch.setattr(publication, "verify_record", counted)
    with QueryReads.snapshot(engine) as reads:
        records = [dict(row) for row in reads.connection.execute(
            "SELECT * FROM calculation_publication WHERE calculation_id=?", (rows["cost"]["id"],)
        )]
        reads.verify_publication_records(records)
        assert calls == [rows["cost"]["publication_id"]]
        assert verify_published_source_bindings(reads, {rows["cost"]["id"]}) == frozenset()
        assert calls == [rows["cost"]["publication_id"]]
        assert set(reads._anchored_source_bytes) == {rows["cost"]["id"]}
