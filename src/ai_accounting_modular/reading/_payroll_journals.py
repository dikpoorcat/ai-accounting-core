"""One payroll meaning for journal phases, business components and progress scopes."""

from ai_accounting_modular.contracts.public import ContractError, format_fen, parse_fen

_DEDUCTIONS = ("employee_social", "employee_housing", "tax")
_SLOTS = (
    "net_pay",
    "employee_social",
    "employee_housing",
    "employer_social",
    "employer_housing",
    "tax",
)
_COMPONENTS = (
    ("remuneration", "gross_fen", "accrual", "应发金额"),
    ("employer_social", "employer_social_fen", "accrual", "单位承担金额"),
    ("employer_housing", "employer_housing_fen", "accrual", "单位承担金额"),
    ("employee_social", "employee_social_fen", "deductions", "代扣金额"),
    ("employee_housing", "employee_housing_fen", "deductions", "代扣金额"),
    ("tax", "tax_fen", "deductions", "代扣金额"),
)
_TITLES = {
    "employer_social": "单位社保",
    "employer_housing": "单位公积金",
    "employee_social": "代扣个人社保",
    "employee_housing": "代扣个人公积金",
    "tax": "代扣个人所得税",
}
_REMUNERATION = {
    "wage": ("wage", "员工工资"),
    "bonus": ("bonus", "员工奖金"),
    "labor": ("labor", "劳务报酬"),
}


def components(content):
    """Use adopted final quantities, including frozen one-journal remuneration."""
    values = content.target["values"]
    result = []
    for component, field, phase, label in _COMPONENTS:
        if not parse_fen(values[field]):
            continue
        code, title = (
            _REMUNERATION[values["remuneration_type"]]
            if component == "remuneration"
            else (component, _TITLES[component])
        )
        result.append((component, phase, code, title, values[field], label))
    if not result:
        code, title = _REMUNERATION[values["remuneration_type"]]
        result.append(("remuneration", "accrual", code, title, values["gross_fen"], "应发金额"))
    return result


def amount(content, *, journal_key=None, component=None):
    values = content.target["values"]
    if component == "net_pay":
        return values["net_fen"], (
            "劳务净额" if values["remuneration_type"] == "labor" else "工资净额"
        )
    if component is not None:
        for key, field, _, label in _COMPONENTS:
            if key == component:
                return values[field], label
        raise ContractError("storage_corrupt", "薪酬展示组成没有明确金额含义。")
    if journal_key == "deductions":
        return format_fen(
            sum(parse_fen(values[field + "_fen"]) for field in _DEDUCTIONS)
        ), "代扣金额"
    if journal_key in {"main", "accrual"}:
        return values["cost_fen"], "计提金额"
    return values["gross_fen"], "应发金额"


def title(content, *, journal_key=None, component=None):
    if component == "remuneration":
        return _REMUNERATION[content.target["values"]["remuneration_type"]][1]
    if component == "net_pay":
        return (
            "劳务净额" if content.target["values"]["remuneration_type"] == "labor" else "工资净额"
        )
    if component is not None:
        return _TITLES[component]
    return "个人扣款" if journal_key == "deductions" else "薪酬计提"


def slots(*, journal_key="main", component=None):
    if component == "remuneration":
        return ("net_pay", *_DEDUCTIONS)
    if component is not None:
        return (component,)
    return _DEDUCTIONS if journal_key == "deductions" else _SLOTS


def component_from_scope(scope):
    if "component" in scope:
        return scope["component"]
    entries = scope.get("obligations", ())
    return entries[0].get("component") if len(entries) == 1 else None


def select_header(entries, *, component=None):
    """Select a declared phase, never the first journal returned for a result."""
    headers = {entry.journal_key: entry for entry in entries}
    if len(headers) != len(entries) or "main" in headers and len(headers) != 1:
        raise ContractError("storage_corrupt", "正式结果的有效凭证阶段不唯一。")
    if "main" in headers:
        return headers["main"]
    phase = "deductions" if component in _DEDUCTIONS else "accrual"
    return headers.get(phase)
