"""Business roles resolve identities without treating accounts as counterparties."""

from dataclasses import dataclass

from ai_accounting_modular.contracts.public import ContractError

from ._common import issue

_KINDS = {
    "party": frozenset({"person", "organization"}),
    "person": frozenset({"person"}),
    "funds": frozenset({"bank_account", "cash_account", "platform_account"}),
}
_LABELS = {"party": "人员或组织", "person": "人员", "funds": "公司实际资金账户"}
_PARTY_FIELDS = frozenset({"customer_id", "counterparty_id", "beneficiary_id"})
_ROLE_FIELDS = (
    "creditor_id",
    "customer_id",
    "counterparty_id",
    "payer_id",
    "beneficiary_id",
    "employee_id",
    "funds_account_id",
)


@dataclass(frozen=True)
class ObjectRole:
    field: str
    object_id: str
    role: str


def object_roles(fact):
    """Keep every object-bearing field's accounting meaning in one place."""
    data, kind = fact["data"], fact["kind"]
    roles = []
    for field in _ROLE_FIELDS:
        if field not in data:
            continue
        object_id = data[field]
        if field in _PARTY_FIELDS:
            role = "party"
        elif field == "creditor_id":
            role = "party" if data["creditor_kind"] == "supplier" else "person"
        elif field == "employee_id":
            role = "person"
        elif field == "payer_id":
            role = "person" if kind == "operations.reimbursement" else "party"
        elif field == "funds_account_id":
            role = "funds"
        else:
            continue
        roles.append(ObjectRole("/data/" + field, object_id, role))
    for field in ("allocations", "sources"):
        for index, allocation in enumerate(data.get(field, ())):
            if "recipient_id" in allocation:
                roles.append(
                    ObjectRole(
                        f"/data/{field}/{index}/recipient_id", allocation["recipient_id"], "party"
                    )
                )
    return roles


def check_roles(fact, context, roles):
    issues = []
    for ref in roles:
        obj = context.get_object(ref.object_id)
        if obj is None:
            problem = issue(fact, ref.field, "引用对象不存在于当前公司。")
            problem["code"] = "reference_not_found"
        elif obj["kind"] not in _KINDS[ref.role]:
            problem = issue(
                fact, ref.field, f"该业务角色必须引用{_LABELS[ref.role]}，不能使用账户或其他对象。"
            )
            problem["code"] = "incompatible_object_kind"
        else:
            continue
        issues.append(problem)
    if issues:
        raise ContractError(
            "invalid_input", "经营业务所填对象身份不符合其业务角色。", fact_issues=issues
        )
