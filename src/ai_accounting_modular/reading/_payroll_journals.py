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
    "wage": "员工工资",
    "bonus": "员工奖金",
    "labor": "劳务报酬",
}


def components(content, phase):
    """Read-only amounts under one business stage, never another accounting entry."""
    values = content.target["values"]
    if content.target["kind"] == "people.remuneration_tax":
        return ({"label": _TITLES["tax"], "amount_fen": values["additional_tax_fen"]},)
    result = []
    for component, field, stage, _ in _COMPONENTS:
        if stage != phase or not parse_fen(values[field]):
            continue
        label = (
            _REMUNERATION[values["remuneration_type"]]
            if component == "remuneration"
            else _TITLES[component]
        )
        result.append({"label": label, "amount_fen": values[field]})
    if not result and phase == "accrual":
        result.append({"label": _REMUNERATION[values["remuneration_type"]], "amount_fen": "0"})
    return tuple(result)


def phases(content):
    if content.target["kind"] == "people.remuneration_tax":
        return ("deductions",)
    return tuple(
        phase
        for phase in ("accrual", "deductions")
        if parse_fen(amount(content, journal_key=phase)[0])
    ) or ("accrual",)


def amount(content, *, journal_key=None, component=None):
    values = content.target["values"]
    if content.target["kind"] == "people.remuneration_tax":
        return values["additional_tax_fen"], "代扣金额"
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
    if component == "net_pay":
        return (
            "劳务净额" if content.target["values"]["remuneration_type"] == "labor" else "工资净额"
        )
    if component is not None:
        return _TITLES[component]
    return (
        "个人代扣"
        if journal_key == "deductions" or content.target["kind"] == "people.remuneration_tax"
        else "薪酬计提"
    )


def slots(*, journal_key="main", component=None):
    if component is not None:
        return (component,)
    return _DEDUCTIONS if journal_key == "deductions" else _SLOTS


def component_from_scope(scope):
    return scope.get("component")


def phase_from_scope(scope):
    """A business stage stays specific even when its frozen physical journal is main."""
    return scope.get("phase", scope.get("journal_key"))


def select_header(entries, *, component=None):
    """Select a declared phase, never the first journal returned for a result."""
    headers = {entry.journal_key: entry for entry in entries}
    if len(headers) != len(entries) or "main" in headers and len(headers) != 1:
        raise ContractError("storage_corrupt", "正式结果的有效凭证阶段不唯一。")
    if "main" in headers:
        return headers["main"]
    phase = "deductions" if component in _DEDUCTIONS else "accrual"
    return headers.get(phase)
