"""Opening principal and interest retain the actual adopted loan contract identity."""

from copy import deepcopy
from uuid import uuid4

import pytest

from tests.modular.application._working_case import case as case
from tests.modular.domains.people.test_read_remuneration import bank_object
from tests.modular.ledger.opening._support import loan_opening
from tests.modular.reading._support import source

from .test_brief_open import apply, page, publish


@pytest.mark.parametrize("change", ("contract", "amount"))
def test_opening_loan_progress_distinguishes_another_contract_from_changed_amount(case, change):
    first, package, _ = loan_opening()
    bank = bank_object()
    for fact in (first, package):
        fact["period"] = "2026-01"
        fact["evidence_ids"] = [case.source_id]
    first["data"]["lender_id"] = case.party
    second = deepcopy(first)
    second["business_id"] = str(uuid4())
    for member in package["data"]["members"]:
        member["evidence_ids"] = [case.source_id]
        if member["data"]["type"] == "bank":
            member["data"].update(funds_account_id=bank["object_id"], balance_fen="10100")
        else:
            member["data"].update(
                lender_id=case.party,
                interest_start_on="2026-01-01",
                accrued_interest_fen="100",
            )
    apply(
        case,
        first,
        second,
        package,
        objects=[
            bank,
            {
                "object_id": case.party,
                "kind": "organization",
                "name": "合成贷款机构",
                "expected_seq": 0,
                "data": {},
            },
        ],
        sources=[source("opening-loan") | {"source_id": case.source_id}],
    )
    checked = case.check()
    assert checked["ready"], checked
    assert case.close(checked)["state"] == "closed"
    loan = package["data"]["members"][0]["data"]
    if change == "contract":
        loan["contract_id"] = second["business_id"]
    else:
        loan.update(principal_fen="8000", accrued_interest_fen="80")
        package["data"]["members"][1]["data"]["balance_fen"] = "8080"
    changed = case.app.accounting_apply(
        case.request(
            mutations=[{"op": "replace", "expected_seq": 1, "fact": package}],
            correction_period="2026-02",
        )
    )
    assert changed.get("completion") == "complete", changed
    groups = page(case, publish(case))["collections"]["open_items"]["items"]
    old = {row["description"]: row for row in groups}
    assert old["借款本金"]["outstanding_fen"] == "10000"
    assert old["借款利息"]["outstanding_fen"] == "100"
    for title, expected_amount in (("借款本金", "8000"), ("借款利息", "80")):
        expected = ("open", expected_amount) if change == "amount" else ("reversed", "0")
        assert (old[title]["current_status"], old[title]["current_outstanding_fen"]) == expected
