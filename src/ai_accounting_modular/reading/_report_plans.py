"""One report meaning for previews, saved plans and lightweight validity checks."""

import re
from pathlib import PureWindowsPath

from ai_accounting_modular.contracts.public import (
    ContractError,
    content_digest,
    parse_fen,
    validate_contract,
)
from ai_accounting_modular.files.public import ProductionError

from . import _models, _report_workbook

_TABLES = {
    "balance_sheet": (53, ("ending", "beginning")),
    "profit_statement": (32, ("current", "year_to_date")),
    "cash_flow_statement": (22, ("current", "year_to_date")),
}
_SAFE_INTEGER = 9007199254740991


def organization(metadata):
    return {
        "credit_code": metadata.credit_code,
        "legal_name": metadata.legal_name,
        "accounting_standard": metadata.accounting_standard,
        "currency": metadata.currency,
        "start_period": metadata.start_period,
    }


def _head(company_id, database_id, component, identity, variant, version):
    _report_workbook.check_text(identity)
    return content_digest(
        {
            "format": "ai-accounting-report-export/1",
            "company_id": company_id,
            "database_id": database_id,
            "component": component,
            "organization": {key: identity[key] for key in ("legal_name", "credit_code")},
            "variant": variant,
            "template_version": version,
            "template_sha256": _report_workbook.TEMPLATE_SHA256,
        }
    )


def scope_head(plan):
    _report_workbook.check_text(plan["organization"])
    return _head(
        plan["company_id"],
        plan["database_id"],
        content_digest({"snapshot": _models.semantic(plan["snapshot"])}),
        plan["organization"],
        plan["variant"],
        plan["template_version"],
    )


def validate(plan):
    validate_contract("core.schema.json", "ReportExportPlan", plan)
    _report_workbook.check_text(plan["organization"])
    if (
        plan["variant"] != "tax_bureau"
        or plan["template_version"] != _report_workbook.TEMPLATE_VERSION
    ):
        raise ProductionError(
            "report_template_unsupported", "当前只提供固定版本的税务局三表模板。", retryable=False
        )
    snapshot = plan["snapshot"]
    scope = _models.normalize(snapshot["scope"])
    if (
        scope != snapshot["scope"]
        or snapshot["company_id"] != plan["company_id"]
        or scope["company_id"] != plan["company_id"]
        or snapshot["database_id"] != plan["database_id"]
        or plan["organization"]["accounting_standard"] != snapshot["accounting_standard"]
        or plan["organization"]["currency"] != "CNY"
        or any(check["status"] == "fail" for check in snapshot["checks"])
    ):
        raise ProductionError(
            "report_invalid", "报表计划的公司、季度或正式口径不一致。", retryable=False
        )
    _check_cells(snapshot)
    if scope_head(plan) != plan["scope_head"]:
        raise ProductionError("report_invalid", "报表计划摘要与完整内容不一致。", retryable=False)
    return scope


def _check_cells(snapshot):
    for table, (count, columns) in _TABLES.items():
        if set(snapshot[table]) != {str(line) for line in range(1, count + 1)}:
            raise ProductionError("report_invalid", "报表计划缺少完整三表行。", retryable=False)
        for row in snapshot[table].values():
            for column in columns:
                cell = row[column]
                if cell["state"] != "known":
                    raise ProductionError(
                        "report_incomplete", "报表仍有未确定金额，不能补零导出。", retryable=False
                    )
                if abs(parse_fen(cell["amount_fen"])) > _report_workbook.TEMPLATE_MAX_FEN:
                    raise ProductionError(
                        "report_amount_out_of_range",
                        "报表金额超出此Excel模板的准确表示范围。",
                        retryable=False,
                    )


def epochs(state, metadata):
    value = {"accounting": state["ready_head"], "material": 0, "management": metadata.profile_seq}
    validate_epochs(value)
    return value


def validate_epochs(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"accounting", "material", "management"}
        or any(type(item) is not int or not 0 <= item <= _SAFE_INTEGER for item in value.values())
    ):
        raise ContractError("invalid_input", "报表版本须提供三个非负安全整数。")


def plan(snapshot, metadata):
    result = {
        "kind": "report_export",
        "company_id": snapshot["company_id"],
        "database_id": snapshot["database_id"],
        "snapshot": snapshot,
        "organization": organization(metadata),
        "variant": "tax_bureau",
        "template_version": _report_workbook.TEMPLATE_VERSION,
    }
    result["scope_head"] = scope_head(result)
    validate(result)
    return result


def file_name(scope, organization):
    first = scope["period_from"]
    period = f"{int(first[:4])}年第{(int(first[5:]) - 1) // 3 + 1}季度财务报表.xlsx"
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", organization["legal_name"])
    name = name.strip(" .")[:64].rstrip(" .") or "公司"
    value = f"{name}_{organization['credit_code']}_{period}"
    return "_" + value if PureWindowsPath(value).is_reserved() else value


def preview(snapshot, state, metadata):
    result = {
        "available": False,
        "file_name": file_name(snapshot["scope"], organization(metadata)),
        "preview_digest": None,
        "epochs": None,
    }
    if snapshot["status"] != "ready":
        return result
    try:
        _check_cells(snapshot)
        versions = epochs(state, metadata)
        head = _head(
            snapshot["company_id"],
            snapshot["database_id"],
            state["content_digest"],
            organization(metadata),
            "tax_bureau",
            _report_workbook.TEMPLATE_VERSION,
        )
    except ContractError as error:
        return result | {"reason": error.message}
    return result | {
        "available": True,
        "preview_digest": head,
        "epochs": versions,
    }


def is_current(plan, read, metadata):
    scope = _models.normalize(plan["snapshot"]["scope"])
    state = read.repository("reading").state(_models.key(scope), _models.FINANCIAL)
    if (
        (read.company_id, read.database_id) != (plan["company_id"], plan["database_id"])
        or state is None
        or state["state"] != "ready"
        or state["format_version"] != _models.FORMAT_VERSION
        or state["ready_head"] != state["wanted_head"]
    ):
        return False
    try:
        return (
            _head(
                read.company_id,
                read.database_id,
                state["content_digest"],
                organization(metadata),
                plan["variant"],
                plan["template_version"],
            )
            == plan["scope_head"]
        )
    except ProductionError:
        return False
