"""Give each formal obligation its own party, matter and historical/current amounts."""

from dataclasses import dataclass

from ai_accounting_modular.contracts.public import (
    ContractError,
    content_digest,
    format_fen,
    sum_fen,
)
from ai_accounting_modular.ledger.opening.public import (
    OpeningLoanReference,
    OpeningOrdinaryReference,
    OpeningPayrollReference,
    OpeningTaxReference,
    opening_obligation_references,
    opening_payroll_references,
)

from ._brief_activity_amounts import recognition
from ._brief_open_groups import group_key, status
from ._brief_open_sources import batches, ref_key
from ._business_labels import BusinessLabel, business_label, business_matter

_CONTRIBUTIONS = {
    "employee_social": "个人社保",
    "employer_social": "公司社保",
    "employee_housing": "个人公积金",
    "employer_housing": "公司公积金",
}
_OPENING = {
    "customer_receivable": ("opening_customer_receivable", "期初客户应收款"),
    "supplier_service_payable": ("service_expense", "服务业务成本"),
    "supplier_administration_payable": ("administration_expense", "管理费用"),
    "supplier_sales_payable": ("sales_expense", "销售费用"),
    "employee_reimbursement": ("opening_reimbursement", "期初报销款"),
    "owner_reimbursement": ("opening_owner_reimbursement", "期初负责人垫付款"),
    "deposit_receivable": ("deposit", "押金"),
    "deposit_payable": ("deposit_returnable", "待退押金"),
    "other_receivable": ("opening_other_receivable", "期初其他应收款"),
    "other_payable": ("opening_other_payable", "期初其他应付款"),
}
_TAXES = {
    "vat": "增值税",
    "surtax": "附加税费",
    "individual_income_tax": "代扣个人所得税",
    "enterprise_income_tax": "企业所得税",
}


@dataclass(frozen=True)
class Meaning:
    category: str
    label: BusinessLabel
    party_id: str | None
    description: str
    purpose: str | None = None
    person_id: str | None = None
    recipient_id: str | None = None
    component: str | None = None
    payroll_period: str | None = None
    opening_identity: tuple[str, ...] | None = None


def _category(content, definition, metadata):
    category, direction = (definition.value[key] for key in ("category", "direction"))
    other = "other_" + ("receivables" if direction == "receivable" else "payables")
    if isinstance(metadata, OpeningOrdinaryReference):
        return {
            "customer_receivable": "customer_receivables",
            "supplier_service_payable": "supplier_payables",
            "supplier_administration_payable": "supplier_payables",
            "supplier_sales_payable": "supplier_payables",
            "employee_reimbursement": "employee_payables",
            "deposit_receivable": "refundable_deposit_receivables",
        }.get(metadata.nature, other)
    kind, data = content.target["kind"], content.basis.get("input", {})
    if category == "trade":
        if direction == "receivable" and (
            kind == "operations.service_sale"
            or (kind == "operations.advance" and data["side"] == "customer")
        ):
            return "customer_receivables"
        if direction == "payable" and kind == "operations.expense":
            return {
                "supplier": "supplier_payables",
                "employee": "employee_payables",
                "individual": "other_payables",
            }[data["creditor_kind"]]
        if direction == "payable" and (
            kind == "assets.project_cost"
            or (kind == "assets.acquisition" and data["type"] == "direct")
            or (kind == "operations.advance" and data["side"] == "supplier")
        ):
            return "supplier_payables"
        return other
    if category == "advance" and direction == "receivable":
        return "supplier_advances"
    if category == "deposit" and direction == "receivable":
        return "refundable_deposit_receivables"
    if category == "reimbursement" and direction == "payable":
        return (
            "employee_payables"
            if data.get("payer_kind") == "employee"
            or (kind == "operations.deposit" and data["type"] == "reimbursed")
            else other
        )
    return other


def _meaning(content, definition, opening):
    label = business_label(content, definition)
    party = definition.value.get("party_id")
    kind = content.target["kind"]
    component, person, payroll_period, labor = None, None, None, False
    metadata = opening.get(content.reference["result_id"], {}).get(ref_key(definition.reference))
    category = _category(content, definition, metadata)
    if kind == "people.remuneration":
        values = content.target["values"]
        person, component, payroll_period = (
            values["person_id"],
            definition.reference["slot"],
            content.reference["business_period"],
        )
        labor = values["remuneration_type"] == "labor"
    elif isinstance(metadata, OpeningPayrollReference):
        person, component, payroll_period = (
            metadata.person_id,
            "net_pay" if metadata.component == "net" else metadata.component,
            metadata.payroll_period,
        )
        if component == "net_pay":
            label = BusinessLabel("opening_net_pay", "期初未付净薪")
    if person is not None:
        description = "应付工资净额"
        if kind == "people.remuneration":
            description = {"wage": "应付工资净额", "bonus": "应付奖金净额", "labor": "劳务报酬"}[
                content.target["values"]["remuneration_type"]
            ]
        description = {**_CONTRIBUTIONS, "tax": "代扣个人所得税"}.get(component, description)
        return Meaning(
            "labor_payables" if labor else "payroll_payables",
            label,
            person,
            description,
            person_id=person,
            recipient_id=party,
            component=component,
            payroll_period=payroll_period,
        )
    if isinstance(metadata, OpeningOrdinaryReference):
        label = BusinessLabel(*_OPENING[metadata.nature])
        return Meaning(
            category,
            label,
            party,
            "期初" + label.title if not label.title.startswith("期初") else label.title,
            metadata.original_business_reference,
            opening_identity=("ordinary", metadata.nature, metadata.original_business_reference),
        )
    if isinstance(metadata, OpeningTaxReference):
        title = _TAXES[metadata.tax_kind]
        return Meaning(
            category,
            BusinessLabel(metadata.tax_kind, title),
            party,
            "期初" + title,
            metadata.tax_period + " 税款",
            opening_identity=("tax", metadata.tax_kind, metadata.tax_period, metadata.balance_kind),
        )
    if isinstance(metadata, OpeningLoanReference):
        return Meaning(
            category,
            label,
            party,
            label.title,
            opening_identity=(
                "loan",
                metadata.contract_id,
                metadata.lender_id,
                metadata.component,
            ),
        )
    return Meaning(category, label, party, label.title)


def _other(values):
    return sum_fen(
        (
            values.agency_settled_fen,
            values.offset_settled_fen,
            values.transferred_out_fen,
            -values.excess_reclassified_fen,
        )
    )


def _matter(content, definition, meaning):
    identity = {} if meaning.person_id is not None else business_matter(content, definition)
    if meaning.label.code == "unknown":
        identity = {"source": definition.reference}
    return {"type": meaning.label.code, "identity": identity}


def _progress(snapshot, item, meaning, meanings):
    definition = item.definition
    content = snapshot.contents[definition.source["result_id"]]
    if content.reference["posting_period"] > snapshot.current_cutoff:
        return None, None
    current = snapshot.current.get(ref_key(definition.reference))
    if current is None:
        withdrawn = (
            definition.reference["business_id"],
            content.reference["result_slot"],
        ) in snapshot.withdrawn
        return ("withdrawn" if withdrawn else "reversed"), 0
    actual = meanings[current.definition.source["result_id"], ref_key(definition.reference)]
    current_content = snapshot.contents[current.definition.source["result_id"]]
    original_meaning = _economic_identity(content, definition, meaning)
    current_meaning = _economic_identity(current_content, current.definition, actual)
    if original_meaning != current_meaning:
        return "reversed", 0
    values = current.amounts
    return status(
        values.remaining_fen, values.money_settled_fen, _other(values)
    ), values.remaining_fen


def _economic_identity(content, definition, meaning):
    """A displayed group may combine claims that remain different economic obligations."""
    return (
        ref_key(definition.reference),
        content.target["kind"],
        definition.value["direction"],
        definition.value["category"],
        definition.value["business_period"],
        meaning.party_id,
        meaning.person_id,
        meaning.recipient_id,
        meaning.payroll_period,
        meaning.component,
        content.target.get("values", {}).get("remuneration_type"),
        meaning.opening_identity,
        business_matter(content, definition),
    )


def project(snapshot, company_id, identity, read):
    opening = {}
    for content in snapshot.contents.values():
        if content.target["kind"] == "opening.package":
            opening[content.reference["result_id"]] = {
                ref_key(item.source): item
                for item in (
                    *opening_obligation_references(content),
                    *opening_payroll_references(content),
                )
            }
    meanings = {
        (item.definition.source["result_id"], ref_key(item.definition.reference)): _meaning(
            snapshot.contents[item.definition.source["result_id"]], item.definition, opening
        )
        for item in (*snapshot.selected, *snapshot.current.values())
    }
    ids = sorted(
        {meaning.party_id for meaning in meanings.values() if meaning.party_id is not None}
    )
    objects = {
        obj["object_id"]: obj for batch in batches(ids) for obj in identity.get_batch(batch, read)
    }
    if set(ids) != set(objects):
        raise ContractError("storage_corrupt", "正式往来引用的对象身份缺失。")
    rows = []
    for item in snapshot.selected:
        definition, values = item.definition, item.amounts
        source_id, reference = definition.source["result_id"], definition.reference
        content = snapshot.contents[source_id]
        meaning = meanings[source_id, ref_key(reference)]
        current_status, current_remaining = _progress(snapshot, item, meaning, meanings)
        recognition_value = recognition(content)
        key = group_key(
            company_id,
            definition.value["direction"],
            meaning.category,
            _matter(content, definition, meaning),
            meaning.party_id,
            reference,
        )
        contribution = meaning.component if meaning.component in _CONTRIBUTIONS else None
        rows.append(
            {
                "id": reference["business_id"] + ":" + reference["slot"],
                "group_key": key,
                "date": recognition_value["date"],
                "recognition": recognition_value,
                "source_period": content.reference["business_period"],
                "voucher_version_id": None,
                "voucher_number": None,
                "category_key": meaning.category,
                "party": objects[meaning.party_id]["name"]
                if meaning.party_id is not None
                else "税务机关"
                if definition.value["category"] in {"tax", "withholding"}
                else "未提供对象",
                "description": meaning.description,
                "purpose": meaning.purpose,
                "status": status(values.remaining_fen, values.money_settled_fen, _other(values)),
                "source_amount_fen": format_fen(values.amount_fen),
                "paid_fen": format_fen(values.money_settled_fen),
                "other_settled_fen": format_fen(_other(values)),
                "outstanding_fen": format_fen(values.remaining_fen),
                "current_status": current_status,
                "current_outstanding_fen": None
                if current_remaining is None
                else format_fen(current_remaining),
                "subject_id": reference["business_id"],
                "contribution_group_key": content_digest(
                    [company_id, meaning.person_id, meaning.payroll_period]
                )
                if contribution
                else None,
                "contribution_component": contribution,
                "payroll_period": meaning.payroll_period,
                "_matter_title": meaning.label.title,
                "_detail_scope": {
                    "reference": content.reference,
                    "source": content.source,
                    "business_period": content.reference["business_period"],
                    "posting_period": content.reference["posting_period"],
                    "kind": content.target["kind"],
                    "direction": definition.value["direction"],
                    "obligations": [
                        {
                            "source": reference,
                            "producer": definition.source,
                            "person_id": meaning.person_id,
                            "recipient_id": meaning.recipient_id,
                            "payroll_period": meaning.payroll_period,
                            "component": meaning.component,
                        }
                    ],
                    "allocations": [],
                },
            }
        )
    rows.sort(
        key=lambda row: (
            row["payroll_period"] or row["source_period"],
            row["contribution_component"] or row["description"],
            row["id"],
        )
    )
    return rows
