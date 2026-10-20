"""Frozen slot retirement is one ledger operation, independent of business kind."""

import json

from ai_accounting_modular.contracts.public import ContractError

from ._planning import stored_source_reference
from ._values import check_plan, fail


def retirement_plan(repo, old, explanation):
    """An open correction retires against its frozen baseline, never its open body."""
    if not isinstance(explanation, str) or not explanation.strip():
        raise ContractError(
            "needs_information",
            "撤去已冻结结果需要说明原业务为什么应退出。",
            fact_issues=[
                {
                    "code": "withdrawal_explanation_missing",
                    "business_id": old["business_id"],
                    "field": "/explanation",
                    "meaning": "撤去冻结业务的事实原因",
                    "message": "请提供简明的事实原因；开放月份直接撤去不需要此项。",
                    "allowed_precision": ["source"],
                    "reusable_sources": [],
                    "blocking_scope": "business",
                }
            ],
        )
    baseline = old if old["state"] == "frozen" else repo.result(old["baseline_result_id"])
    if (
        baseline is None
        or baseline["state"] != "frozen"
        or (baseline["business_id"], baseline["result_slot"])
        != (old["business_id"], old["result_slot"])
    ):
        fail("storage_corrupt", "冻结退出缺少可准确引用的被冻结基准。")
    target = json.loads(old["target_json"])
    plan = {
        "business_id": old["business_id"],
        "result_slot": old["result_slot"],
        "accounting_period": old["business_period"],
        "calculation_key": "system.withdrawal",
        "calculation_revision": 1,
        "rule_refs": [
            {
                "key": "system.withdrawal",
                "revision": 1,
                "policy_business_ids": [],
                "official_urls": [],
            }
        ],
        "target": {
            "kind": "system.withdrawal",
            "business_kind": target["kind"],
            "result_slot": old["result_slot"],
            "state": "withdrawn",
        },
        "basis": {
            "kind": "system.withdrawal",
            "result_slot": old["result_slot"],
            "period": old["business_period"],
            "baseline_ref": stored_source_reference(baseline),
            "explanation": explanation,
            "source_locations": [],
        },
        **{
            name: []
            for name in (
                "lines",
                "obligations",
                "settlements",
                "obligation_reclassifications",
                "cost_lots",
                "cost_consumptions",
                "fund_movements",
                "agency_proofs",
                "tax_sales",
                "report_contributions",
            )
        },
    }
    check_plan(plan)
    return plan
