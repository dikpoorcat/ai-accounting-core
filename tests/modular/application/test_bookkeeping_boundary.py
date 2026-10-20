"""An earlier economic month cannot bypass the single ledger boundary."""

from copy import deepcopy
from uuid import uuid4

from .conftest import get, initial_request


def test_move_to_a_pre_book_month_rolls_back_fact_ledger_and_document_set(assembled):
    app, company = assembled
    original = initial_request(company)
    saved = app.accounting_apply(original)
    assert saved["completion"] == "complete", saved
    business = original["mutations"][0]["fact"]["business_id"]
    before = get(app, company, business)
    documents = app.accounting_documents({"company_id": company, "action": "export"})
    changed = deepcopy(original)
    changed["command_id"] = str(uuid4())
    changed["mutations"][0].update(op="replace", expected_seq=1)
    changed["mutations"][0]["fact"]["period"] = "2025-12"
    refused = app.accounting_apply(changed)
    assert refused["completion"] == "not_saved", refused
    assert refused["error"] == "invalid_input"
    assert get(app, company, business) == before
    assert app.accounting_documents({"company_id": company, "action": "export"}) == documents
