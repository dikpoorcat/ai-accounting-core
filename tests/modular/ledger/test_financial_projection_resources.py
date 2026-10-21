"""Controlled uses belong to the definition each exact payment Source adopted."""

from copy import deepcopy
from types import SimpleNamespace

import _financial_snapshot_review_support as support
import pytest

from ai_accounting_modular.ledger._financial_cells import Issues
from ai_accounting_modular.ledger._financial_projection_bodies import capture_bodies
from ai_accounting_modular.ledger._financial_projection_model import Model
from ai_accounting_modular.ledger._financial_projection_requests import request
from ai_accounting_modular.ledger._financial_selection import Selection


class Blocks:
    header_json = None

    def __init__(self):
        self.values = {}

    def get_blocks(self, keys):
        return tuple(self.values.get(key) for key in keys)

    def publish(self, plan):
        for key in plan.deletes:
            del self.values[key]
        for collection, key, raw in plan.writes:
            self.values[collection, key] = raw
        self.header_json = plan.header_json


def body(read, head):
    selection = Selection(
        read.repository("ledger"), "current", "2026-02", read.start_period, Issues()
    )
    return next(iter(capture_bodies(selection, (head,)).values()))


@pytest.mark.parametrize("opening", [False, True], ids=["drawdown", "opening-loan"])
@pytest.mark.parametrize("frozen", [False, True], ids=["open", "closed"])
def test_reclassification_uses_exact_adopted_definition_and_has_one_cold_hot_root(
    tmp_path, opening, frozen
):
    book, blocks = support.assemble(tmp_path), Blocks()
    contract = support.loan_contract("controlled-contract", "2026-12-31")
    contract["data"]["term"] = "short_term"
    if opening:
        member = support.member(
            "controlled-opening-loan",
            {
                "type": "loan",
                "contract_id": contract["business_id"],
                "lender_id": support.PARTY_B,
                "principal_fen": "1000",
                "accrued_interest_fen": "0",
                "interest_start_on": "2026-01-01",
            },
        )
        producer = support.opening(cash="1000", equity="0", members=[member])
        slot = "member:" + member["member_id"] + ":principal"
        book.submit(contract, producer)
    else:
        producer = support.drawdown("controlled-drawdown", contract, "1000")
        slot = "principal"
        book.submit(support.opening(), contract, producer)
    paid = support.payment("controlled-payment", producer, "100", slot=slot)
    paid["data"]["counterparty_id"] = support.PARTY_B
    book.submit(paid)
    if frozen:
        with book.app.storage.write_unit(book.company) as unit:
            checked = book.app.ledger.check_period("2026-01", unit)
            assert checked.ready
            book.app.ledger.close_period("2026-01", checked.head, unit)
    scope = {
        "company_id": book.company,
        "period_from": "2026-01",
        "period_to": "2026-01",
        "view": "as_posted",
    }
    with book.app.storage.read_unit(book.company) as read:
        requested = request("summary_values", scope, read)
        initial = Model(read, blocks)
        previous_roots = initial.advance((requested,), ())
        initial_descriptor = initial.descriptor(requested)
        blocks.publish(initial.plan(previous_roots))
        old_head = next(
            head
            for head in read.repository("ledger").financial_projection_chains()
            if head["business_id"] == paid["business_id"]
        )
        old_body = body(read, old_head)
        assert old_body["resources"] == {
            "obligation": [],
            "settlement": [],
            "obligation_reclassification": [],
        }

    amended = deepcopy(contract)
    amended["data"].update(term="long_term", matures_on="2028-12-31")
    book.replace(amended, correction_period="2026-02" if frozen else None)

    with book.app.storage.read_unit(book.company) as read:
        repo = read.repository("ledger")
        new_head = next(
            head
            for head in repo.financial_projection_chains()
            if head["business_id"] == paid["business_id"] and head["is_current"]
        )
        new_body = body(read, new_head)
        assert new_body["source"] != old_body["source"]
        assert new_body["resources"]["obligation"] == []
        assert new_body["resources"]["obligation_reclassification"] == []
        uses = new_body["resources"]["settlement"]
        assert len(uses) == 1
        assert uses[0]["source_business_id"] == producer["business_id"]
        assert uses[0]["source_public_slot"] == slot
        assert uses[0]["amount_fen"] == 100
        if frozen:
            # A future definition cannot change this retained Source's body.
            assert body(read, old_head) == old_body

        changed = tuple(
            SimpleNamespace(business_id=fact["business_id"], result_slot="main")
            for fact in (contract, producer, paid)
        )
        requested = request("summary_values", scope, read)
        hot = Model(read, blocks)
        hot.advance((requested,), changed)
        cold = Model(read)
        cold.advance((requested,), ())
        assert hot.descriptor(requested) == cold.descriptor(requested)
        if frozen:
            assert hot.descriptor(requested) == initial_descriptor
        else:
            assert hot.descriptor(requested) != initial_descriptor
