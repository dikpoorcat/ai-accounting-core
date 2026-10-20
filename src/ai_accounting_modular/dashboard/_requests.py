"""Strict URL inputs: duplicate, unknown and empty values are never defaults."""

import re

from ai_accounting_modular.contracts.public import ContractError, validate_contract


def parameters(values, allowed, required=()):
    pairs = list(values.items()) if isinstance(values, dict) else list(values)
    result = {}
    for name, value in pairs:
        if name not in allowed or name in result or not isinstance(value, str) or not value:
            raise ContractError("invalid_command", "请求含重复、空值或未支持的参数。")
        result[name] = value
    if any(name not in result for name in required):
        raise ContractError("invalid_command", "请求缺少必要的范围参数。")
    return result


def integer(value, minimum, maximum):
    normalized = value.lstrip("0") or "0"
    if (
        not re.fullmatch(r"[0-9]+", value)
        or len(normalized) > len(str(maximum))
        or not minimum <= int(normalized) <= maximum
    ):
        raise ContractError("invalid_command", "请求中的整数范围无效。")
    return int(normalized)


def company(value):
    try:
        return validate_contract("core.schema.json", "Id", value)
    except ContractError as error:
        raise ContractError("invalid_command", "公司身份格式无效。") from error


def quarterly(values):
    values = parameters(
        values, {"company_id", "year", "quarter", "preparation"}, ("company_id", "year", "quarter")
    )
    if values.get("preparation", "deferred") != "deferred":
        raise ContractError("invalid_command", "本入口只提供当前季度读取。")
    selected = company(values["company_id"])
    year, quarter = integer(values["year"], 1, 9999), integer(values["quarter"], 1, 4)
    return {
        "company_id": selected,
        "period_from": f"{year:04d}-{quarter * 3 - 2:02d}",
        "period_to": f"{year:04d}-{quarter * 3:02d}",
        "view": "as_posted",
    }


def materials(values, *, source_id=None):
    allowed = {"company_id", "business_id", "result_id", "generation", "expected_version"}
    if source_id is None:
        allowed.update(("limit", "cursor"))
    required = ["company_id", "business_id"] + (["expected_version"] if source_id else [])
    query = parameters(values, allowed, required)
    for field in ("generation", "limit"):
        if field in query:
            query[field] = integer(
                query[field], 1, 500 if field == "limit" else 9223372036854775807
            )
    if source_id is not None:
        query["source_id"] = source_id
    try:
        validate_contract(
            "review-materials.schema.json",
            "SourceContentQuery" if source_id else "ReviewMaterialsQuery",
            query,
        )
    except ContractError as error:
        raise ContractError(
            "invalid_command", "资料查阅请求的身份、结果定位或分页条件无效。"
        ) from error
    return query
