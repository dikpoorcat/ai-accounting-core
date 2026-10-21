"""Real commit/document/model endpoints; fixture setup and checks stay untimed."""

from contextlib import contextmanager
from time import perf_counter_ns
from unittest.mock import patch

from ai_accounting_modular.contracts.public import validate_contract
from ai_accounting_modular.records.public import open_standard_bundle

from .._support import COMPANY, defer_documents, ready
from .dataset import count_businesses
from .projections import PAGES, parameters
from .workload_reads import reconciliation_request


def submission(kernel, request, period, *, observation=None):
    """Timestamp after the actual successful write context, including all real finishing."""
    commits = []
    original = kernel.storage.write_unit

    @contextmanager
    def observed(company):
        with original(company) as unit:
            yield unit
        commits.append(perf_counter_ns())

    with patch.object(kernel.storage, "write_unit", observed):
        saved = kernel.application.accounting_apply(request)
    assert saved["completion"] == "complete", saved
    assert len(commits) == 1, "A sample must observe exactly one real formal commit"
    pages = {}
    for page in PAGES:
        pages[page] = ready(kernel, page, parameters(COMPANY, period, page))
    elapsed = (perf_counter_ns() - commits[0]) / 1_000_000
    if observation is not None:
        observation["elapsed_ms"] = elapsed
    assert saved["documents"] == "ready", saved
    assert kernel.reading.run_pending(COMPANY) == ()
    for page, body in pages.items():
        validate_contract("dashboard-response-v2.json", "dashboard_" + page.replace("-", "_"), body)
    return {"elapsed_ms": elapsed, "saved": saved, "pages": pages}


def whole_month(kernel, planned, *, observation=None):
    """Defer publication for preceding real batches, then finish the entire current month.

    This explicitly measures public resumption after the final formal commit. The
    test pause is before Records.prepare_documents, so preceding batches do not
    precompute/archive their shards. No formal write or owner calculation is mocked.
    """
    with defer_documents(kernel):
        for request in planned.requests:
            saved = kernel.application.accounting_apply(request)
            assert (
                saved["completion"] == "saved_documents_pending" and saved["documents"] == "pending"
            ), {
                key: saved.get(key)
                for key in ("completion", "accounting", "documents", "error", "message")
            }
    final = reconciliation_request(kernel, planned)
    result = submission(kernel, final, planned.period, observation=observation)
    # These full public checks are intentionally after the endpoint timestamp.
    expected = {
        mutation["fact"]["business_id"]: mutation["fact"]
        for request in (*planned.requests, final)
        for mutation in request["mutations"]
    }
    assert count_businesses(kernel, planned.period) == len(expected)
    with kernel.storage.read_unit(COMPANY) as read:
        metadata = kernel.storage.company_metadata(read)
        status = kernel.records.document_status(read)
    bundle = open_standard_bundle(metadata.company_root / "standard/current.json")
    current_facts = [fact for fact in bundle.facts if fact["period"] == planned.period]
    actual = {fact["business_id"]: fact for fact in current_facts}
    assert len(current_facts) == len(actual) and actual == expected
    assert status["state"] == "ready"
    result["evidence"] = {
        "document_set_id": bundle.set_id,
        "period": planned.period,
        "archived_business_count": len(actual),
        "expected_business_count": len(expected),
        "all_current_month_facts_verified": True,
        "model_pages": list(result["pages"]),
    }
    return result
