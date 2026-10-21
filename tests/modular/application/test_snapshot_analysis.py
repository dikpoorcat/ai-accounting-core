"""One real verification shares only the current fact proof, never frozen accounting."""

from copy import deepcopy

from ai_accounting_modular.application import _snapshot
from ai_accounting_modular.records import _documents, _standard
from ai_accounting_modular.records import _snapshot as records_snapshot

from .conftest import initial_request


def test_current_fact_analysis_is_shared_once_through_the_full_saved_snapshot(
    assembled, monkeypatch
):
    app, company = assembled
    request = initial_request(company)
    assert app.accounting_apply(request)["completion"] == "complete"
    analyses = []
    analyze = _snapshot.analyze_fact

    def observed(fact):
        analyses.append(deepcopy(fact))
        return analyze(fact)

    def redundant(*args, **kwargs):
        raise AssertionError("The current strict fact proof was already built by its coordinator")

    monkeypatch.setattr(_snapshot, "analyze_fact", observed)
    monkeypatch.setattr(records_snapshot, "analyze_fact", redundant)
    monkeypatch.setattr(_standard, "fact_source_ids", redundant)
    monkeypatch.setattr(_documents, "capture_documents", redundant)
    verifier = _snapshot.SnapshotVerifier(app.identity, app.ledger, app.records, app.registry)
    with app.storage.read_unit(company) as read:
        before = app.records.document_status(read)["set_id"]
        verified = verifier.verify(read)
        assert verified.document_set_id == before
    assert analyses == [request["mutations"][0]["fact"]]
