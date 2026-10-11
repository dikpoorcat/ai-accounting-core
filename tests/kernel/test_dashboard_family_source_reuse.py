"""Necessary frozen families and exact contribution proofs retain their scope."""

import hashlib
import json
from unittest.mock import patch

import pytest
from stage9_metrics import measure_work
from test_asset_batches import month
from test_integrity_content import damage
from test_owner_asset_activation_identity import closed_cards
from test_published_source_bindings import sources as sources  # noqa: F401
from test_reports import book as book  # noqa: F401

from ai_accounting.kernel import close_storage, publication, report_open_contribution
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard, _long_term_assets
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.types import YearMonth, canonical


def _assets(engine, period):
    with Dashboard(engine)._snapshot(period) as snap:
        return _long_term_assets(snap)


def test_owned_family_parser_preserves_all_frozen_work_and_history(tmp_path, record_property):
    company, identities = closed_cards(tmp_path, 3)
    engine = company.engine
    original = close_storage._decode_family
    parsed = []

    def control(header, family, row, **_options):
        return original(header, family, row)

    def counted(header, family, row, **options):
        value = original(header, family, row, **options)
        if family == "accounting":
            parsed.append((header.period, options.get("_native"), value))
        return value

    with patch.object(close_storage, "_decode_family", control):
        before, expected = measure_work(engine, lambda: _assets(engine, "2026-04"))
    with patch.object(close_storage, "_decode_family", counted):
        after, actual = measure_work(engine, lambda: _assets(engine, "2026-04"))
    assert actual == expected
    assert actual["fixed_active_count"] == len(identities)
    assert parsed and all(native for _, native, _ in parsed)
    assert all(set(value) == {"directories", "subject_filter"} for _, _, value in parsed)
    assert all(
        set(value["directories"]) == {"adopted_results", "vouchers", "fund_details"}
        for _, _, value in parsed
    )
    for key in ("sql_calls", "returned_rows", "returned_value_bytes", "sqlite_vm_steps",
                "calculation_result_rows_loaded", "typed_fact_json_decodes",
                "adoption_accounting_slice_reads"):
        assert after["counters"].get(key, 0) == before["counters"].get(key, 0)
    assert after["counters"]["stdlib_json_loads"] < before["counters"]["stdlib_json_loads"]
    company.close("2026-04")
    month(engine, company.owner_confirmation, "2026-05", "family-may")
    company.close("2026-05")
    month(engine, company.owner_confirmation, "2026-06", "family-june")
    parsed.clear()
    with patch.object(close_storage, "_decode_family", counted):
        grown = _assets(engine, "2026-06")
    assert grown["fixed_active_count"] == len(identities)
    assert grown["net_fen"] == sum(80000 + index * 8 for index in range(3))
    # Counts consume only the exact activation accounting leaf. Complete
    # amounts and every consumption owner's source proof remain independent.
    activation_month = YearMonth("2026-02").ordinal
    assert {period for period, _, _ in parsed} == {activation_month}
    with engine.store.connection(read_only=True) as connection:
        raw = connection.execute(
            "SELECT content,digest FROM close_storage_subroot "
            "WHERE period=? AND family='accounting'",
            (activation_month,),
        ).fetchone()
        assert hashlib.sha256(raw["content"].encode()).digest() == bytes(raw["digest"])
        assert all(value == json.loads(raw["content"]) for _, _, value in parsed)
    parsed.clear()
    with patch.object(close_storage, "_decode_family", counted):
        with Dashboard(engine)._snapshot("2026-06") as snap:
            assert snap.queries._selected_asset_activation_identities(snap.connection, snap.period)
            required = {row[0] for row in snap.connection.execute(
                "SELECT fact_id FROM calculation WHERE kind='asset_consumption_month'",
            )}
            assert len(required) == 4
            assert required <= snap.reads._fact_versions.keys()
            assert _long_term_assets(snap) == grown
    assert len({period for period, _, _ in parsed}) >= 4
    record_property("before_actual_work", before["counters"])
    record_property("after_actual_work", after["counters"])


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("broken", ["bytes", "root", "syntax"])
def test_family_native_parser_retains_raw_and_root_failures(native, broken):
    content = canonical({"directories": {"adopted_results": [], "vouchers": []}})
    expected = hashlib.sha256(content.encode()).digest()
    raw_digest = expected
    if broken == "bytes":
        content += " "
    elif broken == "root":
        expected = bytes(32)
    else:
        content = "{"
        raw_digest = expected = hashlib.sha256(content.encode()).digest()
    header = close_storage.CloseHeader(10, bytes(32), bytes(32), {
        "subroots": {"accounting": expected.hex()},
    })
    with pytest.raises(KernelError) as failure:
        close_storage._decode_family(header, "accounting", {
            "content": content, "digest": raw_digest,
        }, _native=native)
    assert failure.value.code == "content_integrity_failed"
    assert failure.value.details["reason"] == (
        "storage_subroot_invalid" if broken == "syntax" else "storage_subroot_digest_mismatch"
    )


def _full(reads, identifiers):
    return report_open_contribution.read_open_contributions(
        reads.engine, reads.connection, identifiers, reads=reads,
    )


def test_full_then_own_proof_reads_only_missing_publication_and_memoizes_success(
    sources, monkeypatch, record_property,
):
    engine, rows = sources
    identifiers = {rows["payment"]["id"], rows["cost"]["id"]}
    checksums = []
    publications = []
    original_checksum = report_open_contribution._checksum
    original_record = publication.verify_record

    def checksum(raw):
        checksums.append(len(raw))
        return original_checksum(raw)

    def record(row):
        publications.append(row["id"])
        return original_record(row)

    monkeypatch.setattr(report_open_contribution, "_checksum", checksum)
    monkeypatch.setattr(publication, "verify_record", record)
    def run(*, control=False, repeat=False):
        with QueryReads.snapshot(engine) as reads:
            assert _full(reads, identifiers).keys() == identifiers
            # Full contribution verification does not prove immutable
            # publication records. Require that exact missing proof now.
            reads._verified_publication_ids.clear()
            checksums.clear()
            publications.clear()
            result = (_control_own(reads, identifiers) if control else
                      report_open_contribution.verify_published_source_bindings(reads, identifiers))
            if not control:
                assert checksums == []
                assert set(publications) == {rows["payment"]["publication_id"],
                                             rows["cost"]["publication_id"]}
            if repeat:
                assert result == report_open_contribution.verify_published_source_bindings(
                    reads, identifiers,
                )
            return result

    before, expected = measure_work(engine, lambda: run(control=True))
    after, actual = measure_work(engine, run)
    assert not actual and actual == expected
    repeated, result = measure_work(engine, lambda: run(repeat=True))
    assert not result
    for key in ("sql_calls", "returned_rows", "returned_value_bytes", "sqlite_vm_steps"):
        assert repeated["counters"].get(key, 0) == after["counters"].get(key, 0)
    assert after["counters"]["returned_value_bytes"] < before["counters"]["returned_value_bytes"]
    record_property("before_actual_work", before["counters"])
    record_property("after_actual_work", after["counters"])


def _control_own(reads, identifiers):
    reads._report_snapshot_cache.pop("report_open_own_source_bytes", None)
    return report_open_contribution.verify_published_source_bindings(reads, identifiers)


@pytest.mark.parametrize("broken", ["publication", "fact", "outcome", "body"])
def test_full_own_reuse_does_not_cross_snapshot_or_hide_source_damage(sources, broken):
    engine, rows = sources
    row = rows["cost"]
    with QueryReads.snapshot(engine) as reads:
        assert row["id"] in _full(reads, {row["id"]})
        assert not report_open_contribution.verify_published_source_bindings(reads, {row["id"]})
    if broken == "publication":
        damage(engine, "calculation_publication", "UPDATE calculation_publication "
               "SET sequence=sequence+100 WHERE id=?", (row["publication_id"],))
    elif broken == "fact":
        damage(engine, "fact_revision", "UPDATE fact_revision SET digest=zeroblob(32) WHERE id=?",
               (row["fact_id"],))
    elif broken == "outcome":
        damage(engine, "calculation", "UPDATE calculation SET outcome='{}' WHERE id=?",
               (row["id"],))
    else:
        damage(engine, "report_open_contribution", "UPDATE report_open_contribution "
               "SET content='{}' WHERE publication_id=?", (row["publication_id"],))
    with QueryReads.snapshot(engine) as reads:
        with pytest.raises(KernelError):
            report_open_contribution.verify_published_source_bindings(reads, {row["id"]})
        assert not reads._report_snapshot_cache.get("report_published_own_sources")


def test_full_contribution_does_not_hide_publication_damage(sources):
    engine, rows = sources
    row = rows["cost"]
    damage(engine, "calculation_publication", "UPDATE calculation_publication "
           "SET sequence=sequence+100 WHERE id=?", (row["publication_id"],))
    with QueryReads.snapshot(engine) as reads:
        assert row["id"] in _full(reads, {row["id"]})
        with pytest.raises(KernelError):
            report_open_contribution.verify_published_source_bindings(reads, {row["id"]})
        assert not reads._report_snapshot_cache.get("report_published_own_sources")


def test_missing_body_and_failed_batch_publish_no_own_success(sources):
    engine, rows = sources
    selected, missing = rows["payment"], rows["cost"]
    damage(engine, "report_open_contribution", "DELETE FROM report_open_contribution "
           "WHERE publication_id=?", (missing["publication_id"],))
    with QueryReads.snapshot(engine) as reads:
        assert missing["id"] not in _full(reads, {missing["id"]})
        assert report_open_contribution.verify_published_source_bindings(
            reads, {missing["id"]},
        ) == {missing["id"]}
        assert missing["id"] not in reads._report_snapshot_cache.get(
            "report_published_own_sources", {},
        )
    damage(engine, "calculation_publication", "UPDATE calculation_publication "
           "SET sequence=sequence+100 WHERE id=?", (missing["publication_id"],))
    with QueryReads.snapshot(engine) as reads:
        assert selected["id"] in _full(reads, {selected["id"]})
        with pytest.raises(KernelError):
            report_open_contribution.verify_published_source_bindings(
                reads, {selected["id"], missing["id"]},
            )
        assert not reads._report_snapshot_cache.get("report_published_own_sources")
