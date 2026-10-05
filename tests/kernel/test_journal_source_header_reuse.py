"""Journal reads only consumed frozen metadata and reuses exact published rows."""

import pytest
from stage9_metrics import measure_work
from test_engine import close, publish, save
from test_engine import engine as engine  # noqa: F401

from ai_accounting.kernel import query_reads
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.query_reads import QueryReads


@pytest.mark.parametrize("closed", [False, True])
def test_amount_proof_metadata_scope_is_exact_frozen_adoption(engine, monkeypatch, closed):
    save(engine, subject="first", amount=100, request="first")
    save(engine, subject="second", amount=200, request="second")
    publish(engine, ["first", "second"])
    save(engine, subject="second", amount=200, revision=1, request="no-impact")
    _, review = publish(engine, ["second"], request="publish-review")
    reviewed = review["results"][0]["calculation_id"]
    if closed:
        close(engine)
    with Dashboard(engine)._snapshot("2026-01") as snap:
        selected = list(snap.connection.execute(*snap.month_journal.sql()))
        expected = {row["basis_calculation_id"] for row in selected} if closed else set()
        if closed:
            # Frozen selection's old voucher owner differs from the adopted
            # no-impact review, whose metadata is the actual frozen comparison.
            expected.remove(next(row["voucher_calculation_id"] for row in selected
                                 if row["basis_subject_id"] == "second"))
            expected.add(reviewed)
        observed = []
        original = QueryReads.metadata

        def trace(self, identifiers, **options):
            identifiers = set(identifiers)
            observed.append(identifiers)
            return original(self, identifiers, **options)

        monkeypatch.setattr(QueryReads, "metadata", trace)
        assert snap.month_journal.account_amounts() == {"5602": [300, 0], "2202": [0, 300]}
        assert set().union(*observed) == expected


def test_actual_source_headers_remove_unused_metadata_and_repeat_publication_transfer(
    engine, monkeypatch, record_property
):
    subjects = [f"charge-{index}" for index in range(24)]
    for index, subject in enumerate(subjects):
        save(engine, subject=subject, amount=index + 1, request=subject)
    _, posted = publish(engine, subjects)
    identifiers = {row["calculation_id"] for row in posted["results"]}
    original = query_reads.verify_current_voucher_publications

    def read(*, old_transfer=False):
        with Dashboard(engine)._snapshot("2026-01") as snap:
            if old_transfer:
                snap.reads.metadata(identifiers, state=False)
            return snap.month_journal.account_amounts()

    current_work, current = measure_work(engine, read)
    with monkeypatch.context() as patch:
        # Reproduce the old consumer boundary: formal adoption is still fully
        # checked, but its successful rows were discarded and read again.
        def discarded(connection, identifiers):
            original(connection, identifiers)
            return {}

        patch.setattr(query_reads, "_selected_current_voucher_publications", lambda *a, **k: None)
        patch.setattr(query_reads, "verify_current_voucher_publications", discarded)
        old_work, old = measure_work(engine, lambda: read(old_transfer=True))
    assert current == old == {"5602": [300, 0], "2202": [0, 300]}
    for key in ("returned_rows", "returned_value_bytes", "sqlite_vm_steps"):
        assert current_work["counters"][key] < old_work["counters"][key]
        record_property(f"current_{key}", current_work["counters"][key])
        record_property(f"discarded_source_headers_{key}", old_work["counters"][key])
    assert current_work["counters"]["calculation_result_json_decodes"] == (
        old_work["counters"]["calculation_result_json_decodes"]
    )
