"""Month membership and payable amounts come only from their public business owners."""

from collections import defaultdict

from ai_accounting_modular.contracts.public import (
    ContractError,
    content_digest,
    format_fen,
    parse_fen,
    sum_fen,
)

from ._sources import batches, ordinary_sources

KINDS = (
    "people.remuneration",
    "operations.expense",
    "operations.reimbursement",
    "operations.pass_through",
    "operations.pass_through_return",
    "opening.package",
)
PAYROLL_PURPOSES = {"wage": "工资", "bonus": "奖金", "labor": "劳务报酬"}


def issue(code, field, message, **identity):
    return {"code": code, "field": field, "message": message, **identity}


def members(owner, scope, read):
    if scope["selection"] == "selected":
        return list(scope["business_ids"])
    identities = []
    for kind in KINDS:
        query = {
            "company_id": read.company_id,
            "family": kind.split(".")[0],
            "kind": kind,
            "period_from": scope["period"],
            "period_to": scope["period"],
            "status": "active",
            "limit": 500,
        }
        while True:
            result = owner.businesses.find_businesses(query, read)
            identities.extend(item["business_id"] for item in result["items"])
            if not result["page"]["has_more"]:
                break
            query["cursor"] = result["page"]["next_cursor"]
    return sorted(set(identities))


def _facts(owner, identities, scope, read):
    result = []
    for batch in batches(identities):
        current = owner.businesses.get_businesses(
            {"company_id": read.company_id, "business_ids": batch, "include": ["facts"]}, read
        )
        if current["missing_business_ids"]:
            raise ContractError("not_found", "指定的代发业务在当前公司不存在。")
        for item in current["businesses"]:
            fact = item.get("fact")
            if fact is None or item["state"]["state"] != "active":
                if scope["selection"] == "selected":
                    raise ContractError("invalid_input", "选定代发范围包含已经撤去的业务。")
                continue
            if fact["kind"] not in KINDS:
                raise ContractError("invalid_input", "选定代发业务须属于已支持的正式付款类型。")
            if fact["kind"] == "operations.expense" and fact["data"]["creditor_kind"] == "supplier":
                if scope["selection"] == "selected":
                    raise ContractError(
                        "unsupported_business", "供应商采购付款不属于当前代发表范围。"
                    )
                continue
            result.append(fact)
    return sorted(result, key=lambda item: item["business_id"])


def collect(owner, scope, read, balance_period):
    facts = _facts(owner, members(owner, scope, read), scope, read)
    payroll = [item["business_id"] for item in facts if item["kind"] == "people.remuneration"]
    wages = {}
    for batch in batches(payroll):
        wages.update(
            (item["business_id"], item)
            for item in owner.people.remuneration_positions(batch, balance_period, read)
        )
    sources = ordinary_sources(owner, facts, read, balance_period)
    positions = {}
    for batch in batches([item.reference for item in sources]):
        positions.update(
            ((item.definition.reference["business_id"], item.definition.reference["slot"]), item)
            for item in owner.ledger.get_obligation_positions(
                balance_period, read, references=batch
            )
        )
    result, issues = [], []
    for fact in (item for item in facts if item["kind"] == "people.remuneration"):
        identity = fact["business_id"]
        wage = wages[identity]
        if wage["state"] != "active":
            continue
        if parse_fen(wage["payable_fen"]) and wage["payment_composition_state"] == "unconfirmed":
            issues.append(
                issue(
                    "payment_payable_composition_unconfirmed",
                    "/candidates/payable_fen",
                    "这笔薪酬的本次付款组成或安排尚未明确。AI须先核查已有薪酬资料和付款约定，明确是否有额外暂扣；仍缺当前付款必需的事实才向老板提问。",
                    business_id=identity,
                )
            )
        result.append(
            {
                "business_id": identity,
                "kind": "people.remuneration",
                "party_id": wage["person_id"],
                "purpose": f"{fact['period']} {PAYROLL_PURPOSES[wage['remuneration_type']]}",
                "remaining_fen": wage["remaining_fen"],
                "payable_fen": wage["payable_fen"],
                "obligation_refs": [{"business_id": identity, "slot": "net_pay"}],
                "basis_digest": content_digest(wage),
            }
        )
    for source in sources:
        ref = source.reference
        identity = ref["business_id"]
        position = positions.get((identity, ref["slot"]))
        if position is None:
            issues.append(
                issue(
                    "payment_result_missing",
                    "/business_ids",
                    "业务尚无有效的正式应付结果。",
                    business_id=identity,
                )
            )
            continue
        definition = position.definition
        if (
            definition.value["direction"] != "payable"
            or "money" not in definition.value["allowed_settlements"]
            or (
                source.recipient_id is not None
                and source.recipient_id != definition.value.get("party_id")
            )
        ):
            raise ContractError("storage_corrupt", "代发来源与允许实际付款的正式应付不一致。")
        remaining = format_fen(max(position.amounts.remaining_fen, 0))
        candidate = {
            "business_id": identity,
            "kind": source.kind,
            "purpose": source.purpose,
            "remaining_fen": remaining,
            "payable_fen": remaining,
            "obligation_refs": [ref],
            "basis_digest": content_digest(
                {
                    "source": definition.source,
                    "obligation": definition.value,
                    "remaining_fen": remaining,
                }
            ),
        }
        if definition.value.get("party_id"):
            candidate["party_id"] = definition.value["party_id"]
        elif parse_fen(remaining):
            issues.append(
                issue(
                    "payment_recipient_missing",
                    "/candidates/party_id",
                    "这笔正式应付尚未明确实际收款对象。",
                    business_id=identity,
                )
            )
        result.append(candidate)
    return sorted(
        result, key=lambda item: (item["business_id"], item["obligation_refs"][0]["slot"])
    ), issues


def recipient_rows(owner, candidates, read):
    from ._workbook import text

    grouped = defaultdict(list)
    for candidate in candidates:
        if parse_fen(candidate["payable_fen"]) > 0 and "party_id" in candidate:
            grouped[(candidate["party_id"], candidate["purpose"])].append(candidate)
    objects = {}
    for batch in batches(sorted({identity for identity, _ in grouped})):
        objects.update((item["object_id"], item) for item in owner.identity.get_batch(batch, read))
    payees, issues = {}, []
    for identity in sorted({identity for identity, _ in grouped}):
        obj = objects.get(identity)
        if obj is None or obj["kind"] not in {"person", "organization"}:
            issues.append(
                issue(
                    "payment_recipient_missing",
                    "/objects",
                    "收款对象须有本公司的个人或机构档案。",
                    object_id=identity,
                )
            )
            continue
        data = obj["data"]
        name, account = data.get("payee_name") or obj["name"], data.get("payee_account")
        if not account:
            issues.append(
                issue(
                    "payment_account_missing",
                    "/objects/data/payee_account",
                    "当前收款对象缺少银行收款账号，请先复用已有资料核查。",
                    object_id=identity,
                )
            )
            continue
        try:
            text(name)
            text(account)
            if any(character.isspace() for character in account):
                raise ContractError("payment_recipient_invalid", "银行收款账号不能含空格。")
        except ContractError as error:
            issues.append(
                issue(error.code, "/objects/data/payee_account", error.message, object_id=identity)
            )
            continue
        payees[identity] = (name, account)
    rows = []
    for (identity, purpose), items in sorted(grouped.items()):
        if identity not in payees:
            continue
        name, account = payees[identity]
        rows.append(
            {
                "party_id": identity,
                "name": name,
                "account": account,
                "purpose": purpose,
                "amount_fen": format_fen(sum_fen(parse_fen(item["payable_fen"]) for item in items)),
                "obligation_refs": [ref for item in items for ref in item["obligation_refs"]],
            }
        )
    return rows, issues
