"""Capture only actual public Money occurrence IDs for the final bank matches."""

from ai_accounting_modular.contracts.public import CONTRACT, PageQuery
from ai_accounting_modular.money.public import MovementQuery

from .._support import COMPANY
from .workload import identity


def reconciliation_request(kernel, planned):
    facts = []
    with kernel.storage.read_unit(COMPANY) as read:
        scope = {
            "company_id": COMPANY,
            "period_from": planned.period,
            "period_to": planned.period,
            "view": "as_posted",
            "as_of": planned.period,
        }
        for seed in planned.reconciliations:
            observed, cursor = {}, None
            while True:
                page = kernel.money.query_movements(
                    scope, MovementQuery(seed.account_id), PageQuery(500, cursor), read
                )
                for item in page.items:
                    key = item["source"]["business_id"]
                    assert key not in observed, (
                        "This fixture expects one actual bank occurrence per business/account"
                    )
                    observed[key] = item["movement"]["movement_id"]
                cursor = page.next_cursor
                if cursor is None:
                    break
            assert set(observed) == {business for _, business in seed.rows}
            facts.append(
                {
                    "business_id": seed.business_id,
                    "kind": "money.reconciliation",
                    "period": planned.period,
                    "evidence_ids": [planned.requests[0]["sources"][0]["source_id"]],
                    "data": {
                        "statement_id": seed.statement_id,
                        "matches": [
                            {"statement_row_ids": [row], "fund_movement_ids": [observed[business]]}
                            for row, business in seed.rows
                        ],
                    },
                }
            )
    return {
        "contract": CONTRACT,
        "company_id": COMPANY,
        "command_id": identity(planned.period + "/reconciliation-command"),
        "mutations": [{"op": "create", "expected_seq": 0, "fact": fact} for fact in facts],
    }
