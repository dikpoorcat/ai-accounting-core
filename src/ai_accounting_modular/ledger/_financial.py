"""The fixed three-statement snapshot uses one company transaction and selection."""

from ai_accounting_modular.contracts.public import (
    ContractError,
    validate_contract,
)

from ._financial_calculation import MAPPING_V1
from ._values import fail

CURRENT_MAPPING_VERSION = MAPPING_V1


def normalize(scope, context):
    validate_contract("core.schema.json", "LedgerScope", scope)
    first, last = scope["period_from"], scope["period_to"]
    if scope["company_id"] != context.company_id:
        fail("invalid_input", "报表范围与同一公司读取上下文不一致。")
    if "object_ids" in scope or first[:4] != last[:4] or first > last:
        fail("invalid_input", "三表只支持整家公司、同年单月或完整自然季度。")
    quarter = int(first[5:]) in (1, 4, 7, 10) and int(last[5:]) == int(first[5:]) + 2
    if first != last and not quarter:
        fail("invalid_input", "报表范围必须是单月或完整自然季度，不能自动补月。")
    if int(first[:4]) < 2013:
        fail("unsupported_business", "当前报表映射适用2013年起的小企业会计准则期间。")
    result = {key: value for key, value in scope.items() if key != "expected_head"}
    if scope["view"] == "as_posted":
        if scope.get("as_of", last) != last:
            fail("invalid_input", "历史采用报表的截止月必须与报告末月一致。")
        result["as_of"] = last
    elif "as_of" in scope:
        fail("invalid_input", "当前完整目标口径不接受历史采用截止月。")
    if last < context.start_period:
        raise ContractError(
            "needs_information",
            "接账前的独立月份缺少逐月正式来源。",
            fact_issues=[
                {
                    "code": "missing_prior_month",
                    "field": "/period_from",
                    "meaning": "接账前独立月份",
                    "message": "接续累计额不能均摊或反推为此前单月。",
                    "allowed_precision": ["month", "source"],
                    "reusable_sources": [],
                    "blocking_scope": "none",
                }
            ],
        )
    return result


def snapshot(scope, context, *, mapping_version=None):
    version = mapping_version or CURRENT_MAPPING_VERSION
    if version != MAPPING_V1:
        fail("unsupported_policy", "该历史报告映射版本尚未装配，不能改用另一版本。")
    return _snapshot_v1(scope, context)


def _snapshot_v1(scope, context):
    from ._financial_projection import public_snapshot

    return public_snapshot(scope, context)
