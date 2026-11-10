"""Purpose-sized activities from exact typed business inputs and results."""

from collections import defaultdict
from dataclasses import dataclass

from ai_accounting_modular.contracts.public import (
    ContractError,
    content_digest,
    format_fen,
    parse_fen,
)
from ai_accounting_modular.money.public import counterparty_meaning

from . import _payroll_journals
from ._brief_activity_amounts import amount, recognition, signed
from ._business_labels import (
    BusinessLabel,
    ObligationFields,
    business_label,
    business_matter,
    origin_fields,
    remuneration_identity,
    settlement_label,
    settlement_title,
)
from ._counterparty_display import label as counterparty_label
from ._investment_settlement_display import authorized_sources
from ._opening_investment_meaning import label as opening_investment_label


@dataclass(frozen=True)
class Part:
    label: BusinessLabel
    amount_fen: str | None
    amount_label: str
    parties: tuple[str, ...]
    phase: str
    batch: bool
    scope: dict


@dataclass(frozen=True)
class ActivityEvent:
    reference: dict
    role: str
    voucher_number: int
    voucher_token: str
    journal_key: str = "main"


_COMPONENTS = {
    "employee_social": ("employee_social", "个人承担社保费"),
    "employer_social": ("employer_social", "单位承担社保费"),
    "employee_housing": ("employee_housing", "个人承担住房公积金"),
    "employer_housing": ("employer_housing", "单位承担住房公积金"),
    "tax": ("withholding", "代扣个人所得税"),
}


def _individual_party(content, cache):
    """Only an individual settlement proves that all allocations share one actual party."""
    target, basis = origin_fields(content)
    data = basis.get("input", {})
    if (
        target["kind"] != "money.movement"
        or data.get("type") != "settlement"
        or data.get("payment_method") != "individual"
    ):
        return None
    authorized = authorized_sources(content, cache, data.get("counterparty_id"))
    known = {
        item["party_id"]
        for item in basis["adopted_quantities"]
        if item["type"] == "obligation"
        and item.get("party_id") is not None
        and (item["source"]["business_id"], item["source"]["slot"]) not in authorized
    }
    if len(known) > 1:
        raise ContractError("storage_corrupt", "普通收付的正式来源存在不同实际交易方。")
    inherited = next(iter(known), None)
    party = data.get("counterparty_id", inherited)
    if inherited is not None and party != inherited:
        raise ContractError("storage_corrupt", "普通收付交易方与正式采用义务不一致。")
    return party


def business_parties(content, cache):
    """Actual typed participants in this business, without assuming one party per result."""
    target, basis = origin_fields(content)
    data, kind = basis.get("input", {}), target["kind"]
    match kind:
        case "people.remuneration":
            return (data["person_id"],)
        case "people.remuneration_tax":
            return (target["values"]["person_id"],)
        case "operations.service_sale" | "operations.sale_return":
            return (data["customer_id"],)
        case "operations.expense":
            return (data["creditor_id"],)
        case "operations.expense_recovery" | "operations.advance":
            return (data["counterparty_id"],)
        case "operations.deposit":
            return tuple(
                dict.fromkeys(
                    (data["counterparty_id"], data["employee_id"])
                    if data["type"] == "reimbursed"
                    else (data["counterparty_id"],)
                )
            )
        case "operations.other_income":
            return (data["counterparty_id"],) if "counterparty_id" in data else ()
        case "operations.overpayment":
            if "counterparty_id" in data:
                return (data["counterparty_id"],)
            parties = {
                item["party_id"]
                for item in content.basis["adopted_quantities"]
                if item["type"] == "obligation"
                and item["source"] == data["source"]
                and "party_id" in item
            }
            return tuple(sorted(parties))
        case "operations.reimbursement":
            return (data["payer_id"],)
        case "operations.pass_through":
            return tuple(dict.fromkeys((data["payer_id"], data["beneficiary_id"])))
        case "operations.pass_through_return":
            return business_parties(cache.source(content, data["pass_through_id"]), cache)
        case "operations.tax_release":
            return business_parties(
                cache.source(content, content.reference["business_id"], result_slot="main"), cache
            )
        case "assets.acquisition":
            if data["type"] == "direct":
                return (data["supplier_id"],)
            return tuple(item["person_id"] for item in content.target["values"]["creditors"])
        case "assets.project_cost":
            return (data["supplier_id"],)
        case "assets.disposal":
            return (data["buyer_id"],) if data["type"] == "sale" else ()
        case "financing.owner_funding":
            return (data["owner_id"],)
        case (
            "investment.money_fund_purchase"
            | "investment.money_fund_redemption"
            | "investment.acquire"
            | "investment.income"
            | "investment.dispose"
            | "investment.capital_return"
            | "investment.reinvest"
            | "investment.loss"
            | "investment.debt_interest"
        ):
            return (data["counterparty_id"],) if "counterparty_id" in data else ()
        case "money.movement":
            if data.get("payment_method") == "individual" and data["type"] == "settlement":
                party = _individual_party(content, cache)
                return (party,) if party is not None else ()
            if data.get("payment_method") == "bank_batch" or data["type"] == "payroll_reserve":
                return tuple(
                    dict.fromkeys(
                        item["party_id"]
                        for item in basis["adopted_quantities"]
                        if item["type"] == "obligation" and item.get("party_id") is not None
                    )
                )
            return (data["counterparty_id"],) if "counterparty_id" in data else ()
        case "investment.cooperation_collection" | "investment.cooperation_payment":
            return tuple(
                dict.fromkeys(content.target["values"][key] for key in ("party_id", "operator_id"))
            )
        case "investment.cooperation_contract":
            return (data["operator_id"],)
        case "investment.cooperation_position":
            return business_parties(
                cache.source(content, data["contract_id"], result_slot="main"), cache
            )
        case (
            "investment.cooperation_funding"
            | "investment.cooperation_operator_transfer"
            | "investment.cooperation_distribution"
            | "investment.cooperation_exit"
        ):
            return business_parties(cache.source(content, data["contract_id"]), cache)
        case "operations.advance_refund" | "operations.advance_fulfillment":
            return business_parties(cache.source(content, data["advance_id"]), cache)
        case "financing.loan_drawdown":
            return business_parties(cache.source(content, data["contract_id"]), cache)
        case "financing.interest_period":
            return business_parties(
                cache.source(content, data["principal_source"]["business_id"]), cache
            )
        case "financing.loan_contract":
            return (data["lender_id"],)
        case _:
            return ()


def allocation_source(content, allocation, cache, *, payment_party=None):
    source = allocation["source"]
    producer = cache.source(content, source["business_id"])
    kind, slot = producer.target["kind"], source["slot"]
    label = business_label(producer)
    adopted = [
        item
        for item in content.basis["adopted_quantities"]
        if item["type"] == "obligation" and item["source"] == source
    ]
    if len(adopted) != 1:
        raise ContractError("storage_corrupt", "实际分配缺少唯一的正式采用义务。")
    if kind.startswith("investment.") and slot in {"declared_income", "income"}:
        label = business_label(
            producer,
            ObligationFields(
                source,
                adopted[0]["direction"],
                "investment",
                producer.basis["period"],
                adopted[0].get("party_id"),
            ),
        )
    person, recipient, payroll, component = None, adopted[0].get("party_id"), None, None
    data = content.basis.get("input", {})
    if (
        content.target["kind"] == "money.movement"
        and data["type"] == "settlement"
        and data["payment_method"] == "individual"
    ):
        recipient = payment_party
    if allocation.get("recipient_id", recipient) != recipient:
        raise ContractError("storage_corrupt", "实际分配对象与正式收付对象不一致。")
    payroll_identity = remuneration_identity(producer)
    if payroll_identity is not None:
        person, payroll, _ = payroll_identity
        component = slot
        if slot in _COMPONENTS:
            label = BusinessLabel(*_COMPONENTS[slot])
    elif kind == "opening.package":
        item = cache.opening(producer).get((source["business_id"], source["slot"]))
        if item is not None:
            person, payroll, component = (
                item.person_id,
                item.payroll_period,
                item.component,
            )
            label = BusinessLabel(*_COMPONENTS.get(component, ("wage", "期初工资")))
        else:
            reference = cache.opening_obligations(producer).get(
                (source["business_id"], source["slot"])
            )
            label = opening_investment_label(reference) or BusinessLabel(
                "opening_obligation", "期初往来款"
            )
    elif kind == "operations.pass_through":
        label = (
            BusinessLabel("pass_through_collection", "代收款")
            if slot == "collection"
            else BusinessLabel("pass_through_remittance", "代付款转付")
        )
    elif kind in {"financing.owner_funding", "financing.loan_drawdown"} and slot == "principal":
        label = BusinessLabel("loan_principal", "借款本金")
    elif kind in {"taxes.assessment", "taxes.actual_declaration"} and slot == "surtax":
        label = BusinessLabel("surtax", "增值税附加税费")
    return label, {
        "source": source,
        "producer": producer.source,
        "amount_fen": allocation["amount_fen"],
        "person_id": person,
        "recipient_id": recipient,
        "payroll_period": payroll,
        "component": component,
        "matter": business_matter(producer),
    }


def base_scope(content, *, direction=None, allocations=()):
    return {
        "reference": content.reference,
        "source": content.source,
        "business_period": content.reference["business_period"],
        "posting_period": content.reference["posting_period"],
        "kind": content.target["kind"],
        "direction": direction,
        "allocations": list(allocations),
        "matter": business_matter(content),
    }


def parts(content, cache):
    kind, data = content.target["kind"], content.basis.get("input", {})
    if kind == "people.remuneration":
        return [
            Part(
                BusinessLabel(code, title),
                value,
                label,
                (data["person_id"],),
                phase,
                False,
                base_scope(content) | {"component": component},
            )
            for component, phase, code, title, value, label in _payroll_journals.components(content)
        ]
    if kind == "money.movement" and data["type"] in {"settlement", "payroll_reserve"}:
        direction = data.get("direction", "outflow")
        batch = data.get("payment_method") == "bank_batch" or data["type"] == "payroll_reserve"
        grouped = defaultdict(list)
        labels = {}
        payment_party = _individual_party(content, cache)
        for allocation in data["allocations"]:
            label, actual = allocation_source(
                content, allocation, cache, payment_party=payment_party
            )
            parties = () if batch else tuple(x for x in (actual["recipient_id"],) if x)
            group = label.code, parties, tuple(sorted(actual["matter"].items()))
            grouped[group].append(actual)
            labels[group] = label
        result = []
        for group, actual in grouped.items():
            label, parties = labels[group], group[1]
            title = settlement_title(label.title, label.code, direction)
            result.append(
                Part(
                    BusinessLabel(label.code, title),
                    format_fen(sum(parse_fen(item["amount_fen"]) for item in actual)),
                    "实际付款" if direction == "outflow" else "实际收款",
                    parties,
                    direction,
                    batch,
                    base_scope(content, direction=direction, allocations=actual)
                    | {"matter": actual[0]["matter"]},
                )
            )
        if data["type"] == "payroll_reserve":
            result.append(
                Part(
                    BusinessLabel("managed_reserve", "支付备用金费用"),
                    data["reserve_expense_fen"],
                    "实际付款",
                    (),
                    "outflow",
                    True,
                    base_scope(content, direction="outflow"),
                )
            )
        return result
    if kind == "money.transfer" and data["type"] == "offset":
        # The two obligations describe one offset, rather than two new cash movements.
        allocations = [
            allocation_source(content, data[key], cache)[1] for key in ("first", "second")
        ]
        value = amount(content)
        return [
            Part(
                business_label(content),
                value.amount_fen,
                "抵销金额",
                tuple(
                    dict.fromkeys(
                        item["recipient_id"]
                        for item in allocations
                        if item["recipient_id"] is not None
                    )
                ),
                "offset",
                False,
                base_scope(content, allocations=allocations),
            )
        ]
    value, parties, label = (
        amount(content),
        business_parties(content, cache),
        business_label(content),
    )
    phase, amount_label = "recognition", value.label
    if kind == "money.movement":
        phase = data["direction"]
        amount_label = "实际付款" if phase == "outflow" else "实际收款"
        label = settlement_label(content, (), phase)
    elif kind == "money.transfer":
        phase = "internal_transfer"
    return [
        Part(
            label,
            value.amount_fen,
            amount_label,
            parties,
            phase,
            False,
            base_scope(content, direction=phase if phase in {"inflow", "outflow"} else None),
        )
    ]


def for_journal(prepared, journal_key):
    return [part for part in prepared if journal_key == "main" or part.phase == journal_key]


def category(kind, phase, role):
    if role in {"reversal", "replacement", "reclassification"}:
        return "correction"
    if phase in {"inflow", "outflow", "offset", "internal_transfer"}:
        return "cash"
    if kind.startswith("people."):
        return "people"
    if kind.startswith("assets."):
        return "assets"
    if kind.startswith("taxes."):
        return "tax"
    if kind.startswith(("investment.", "financing.")):
        return "investment"
    return "business"


def semantic(content, prepared):
    """Ignore proof versions, retaining actual business meanings and allocated identities."""
    kind = content.target["kind"]
    values = dict(content.target.get("values", {}))
    if kind == "taxes.assessment":
        for field in ("filing_fields", "filing_state", "filing_issues"):
            values.pop(field, None)
    elif kind == "operations.sale_return":
        for field in ("original_tax_period", "tax_returns"):
            values.pop(field, None)
    return {
        "kind": kind,
        "values": values,
        "matter": business_matter(content),
        "details": _event_details(content),
        "recognition": recognition(content),
        "parts": [
            {
                "purpose": part.label.code,
                "amount_fen": part.amount_fen,
                "parties": list(part.parties),
                "phase": part.phase,
                "batch": part.batch,
                "allocations": [
                    {key: value for key, value in allocation.items() if key != "producer"}
                    for allocation in part.scope["allocations"]
                ],
            }
            for part in prepared
        ],
    }


def _event_details(content):
    """A few actual facts not represented in their public Target values."""
    kind, data = content.target["kind"], content.basis.get("input", {})
    details = {}
    if (
        kind
        in {
            "operations.service_sale",
            "operations.expense",
            "operations.sale_return",
            "operations.expense_recovery",
            "operations.overpayment",
        }
        and "project_total_fen" in data
    ):
        details["project_total_fen"] = data["project_total_fen"]
    if kind == "assets.disposal":
        details["type"] = data["type"]
    if kind == "operations.expense":
        details["report_details"] = data.get("report_details")
    if kind == "operations.reimbursement":
        field = "allocations" if data["type"] == "personal_payment" else "sources"
        details[field] = data[field]
    if kind == "money.transfer" and data["type"] == "physical":
        details.update((field, data[field]) for field in ("from_account_id", "to_account_id"))
    return details


def rows(event, content, objects, *, prepared, obligations=(), own_funds_channel=None):
    result, correction = [], event.role in {"reversal", "replacement", "reclassification"}
    sign, token = (-1 if event.role == "reversal" else 1), event.voucher_token
    state = {"reversal": "更正前", "replacement": "更正后"}.get(
        event.role, "更正原业务" if correction else "已入账"
    )
    for index, part in enumerate(prepared):
        ident = content_digest(
            {"voucher": token, "source": content.source, "role": event.role, "part": index}
        )
        group = category(content.target["kind"], part.phase, event.role)
        group_key = content_digest(
            {
                "purpose": part.label.code,
                "matter": part.scope["matter"],
                "parties": list(part.parties),
                "phase": part.phase,
                "role": event.role if correction else "original",
                "isolated": ident
                if part.batch or not part.parties or part.label.code == "unknown"
                else None,
            }
        )
        title = (
            "冲正原业务 · " if event.role == "reversal" else "更正原业务 · " if correction else ""
        ) + part.label.title
        rec = recognition(content)
        selected_obligations = obligations
        if content.target["kind"] == "people.remuneration":
            allowed = _payroll_journals.slots(
                journal_key=event.journal_key, component=part.scope.get("component")
            )
            selected_obligations = [
                item for item in obligations if item["source"]["slot"] in allowed
            ]
        actual_scope = dict(part.scope) | {
            "voucher": token,
            "voucher_role": event.role,
            "sign": sign,
            "part": index,
            "purpose": part.label.code,
            "journal_key": event.journal_key,
            "obligations": list(selected_obligations),
        }
        progress = bool(part.scope["allocations"] or selected_obligations) and not content.target[
            "kind"
        ].startswith(("investment.cooperation_", "opening.", "reports."))
        if part.batch:
            party = "银行批量付款"
        elif part.parties or part.phase in {"outflow", "inflow", "internal_transfer"}:
            meaning = counterparty_meaning(
                party_ids=part.parties,
                internal_transfer=part.phase == "internal_transfer",
                own_funds_channel=own_funds_channel if event.role != "reversal" else None,
            )
            party = counterparty_label(meaning, objects, direction=part.phase)
        else:
            party = "—"
        result.append(
            {
                "row": {
                    "key": ident,
                    "group_key": group_key,
                    "subject_id": event.reference["business_id"],
                    "voucher_version_id": token,
                    "detail_scope_key": ident if progress else None,
                    "voucher_number": event.voucher_number,
                    "date": rec["date"],
                    "recognition": rec,
                    "title": title,
                    "description": title,
                    "amount_fen": signed(part.amount_fen, sign),
                    "amount_label": part.amount_label,
                    "state": state,
                    "party": party,
                    "group": group,
                },
                "scope": actual_scope,
                "batch": part.batch,
            }
        )
    if not result:
        raise ContractError("storage_corrupt", "正式凭证缺少可以定位的业务活动。")
    return result
