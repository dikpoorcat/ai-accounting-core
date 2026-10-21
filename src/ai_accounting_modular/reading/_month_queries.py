"""Validate month selections before registering any derived work."""

from ai_accounting_modular.contracts.public import ContractError, PageQuery, validate_contract

from ._types import MonthScope

FUND_COLLECTIONS = (
    "accounts",
    "movements",
    "statements",
    "investment_products",
    "investment_events",
)


def _base(query, scope, extra, collections):
    if not isinstance(scope, (MonthScope, dict)):
        raise ContractError("invalid_input", "月页须提供明确公司和月份选择。")
    if not isinstance(query, dict) or set(query) - {"section", "limit", "cursor"} - set(extra):
        raise ContractError("invalid_input", "月页含未支持的筛选条件。")
    selected = dict(query)
    section = selected.get("section")
    if "section" in selected and (not isinstance(section, str) or section not in collections):
        raise ContractError("invalid_input", "月页集合无效。")
    PageQuery(selected.get("limit", 20), selected.get("cursor"))
    expected = scope.expected_head if isinstance(scope, MonthScope) else scope.get("expected_head")
    if selected.get("cursor") is not None and (section is None or expected is None):
        raise ContractError("invalid_input", "续页须同时提供集合、游标及原结果版本。")
    return selected


def funds(query, scope):
    selected = _base(
        query,
        scope,
        (
            "movement_account_type",
            "movement_account_id",
            "statement_account_id",
            "movement_account_selection",
        ),
        FUND_COLLECTIONS,
    )
    section = selected.get("section")
    account_type = selected.get("movement_account_type")
    account_id = selected.get("movement_account_id")
    if ("movement_account_type" in selected) != ("movement_account_id" in selected) or (
        "movement_account_type" in selected
        and (
            not isinstance(account_type, str)
            or account_type not in {"bank", "cash", "payment_platform"}
        )
    ):
        raise ContractError("invalid_input", "资金账户须同时提供类型和身份。")
    for name in ("movement_account_id", "statement_account_id"):
        if name in selected:
            validate_contract("core.schema.json", "Id", selected[name])
    choice = selected.get("movement_account_selection", "all")
    if (
        not isinstance(choice, str)
        or choice not in {"all", "first"}
        or (
            choice == "first"
            and (account_id is not None or section is not None or "cursor" in selected)
        )
    ):
        raise ContractError("invalid_input", "默认首账户只用于尚未指定账户的首页。")
    return selected


def assets(query, scope):
    selected = _base(
        query, scope, ("asset_filter", "asset_id", "project_id"), ("assets", "projects")
    )
    filter = selected.get("asset_filter", "all")
    if not isinstance(filter, str) or filter not in {
        "all",
        "active",
        "fixed",
        "intangible",
        "pending",
        "exited",
    }:
        raise ContractError("invalid_input", "资产筛选无效。")
    for name in ("asset_id", "project_id"):
        if name in selected:
            validate_contract("core.schema.json", "Id", selected[name])
    return selected
