"""Company-sized internal execution must not relax the public AI request budget."""

import pytest

from ai_accounting_modular.contracts.public import canonical_json

from .conftest import initial_request, source


def test_eight_mib_wire_budget_is_checked_before_source_io_or_any_write(assembled, monkeypatch):
    app, company = assembled
    request = initial_request(company)
    request["sources"] = [source(f"资料{index}" + "资料" * 3000) for index in range(480)]
    request["mutations"][0]["fact"]["evidence_ids"] = [request["sources"][0]["source_id"]]
    assert len(canonical_json(request).encode()) > 8 * 1024 * 1024
    monkeypatch.setattr(
        app.records, "prepare_sources", lambda *a, **kw: pytest.fail("预算超限不能先核读全部原件")
    )
    outcome = app.accounting_apply(request)
    assert outcome["error"] == "batch_too_large"
    assert outcome["completion"] == "not_saved"
    with app.storage.read_unit(company) as read:
        assert read.head == 0
