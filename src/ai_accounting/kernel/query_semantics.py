"""Pure financial-position and exact settlement query semantics.

Callers select the immutable calculations and facts.  This module only relates
those frozen records; it never opens a database or substitutes a current source.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Hashable, Iterable, Mapping, Sequence
from typing import Any

CASH_ACCOUNTS = {"1001", "1002", "1012"}
PROFIT_ACCOUNTS = {
    "5001": (1, -1),
    "5111": (20, -1),
    "5401": (2, 1),
    "540101": (2, 1),
    "540104": (2, 1),
    "540102": (2, 1),
    "540103": (2, 1),
    "5403": (3, 1),
    "5601": (11, 1),
    "560101": (11, 1),
    "560102": (11, 1),
    "560103": (11, 1),
    "560104": (11, 1),
    "5602": (14, 1),
    "560201": (14, 1),
    "560202": (14, 1),
    "560203": (14, 1),
    "560204": (14, 1),
    "5603": (18, 1),
    "560301": (18, 1),
    "6301": (22, -1),
    "630101": (22, -1),
    "571101": (24, 1),
    "571102": (24, 1),
    "571103": (24, 1),
    "571104": (24, 1),
    "5801": (31, 1),
}
DEBIT_BALANCE = {
    "1101": 2,
    "1121": 3,
    "1131": 6,
    "1132": 7,
    "1403": 10,
    "1405": 12,
    "1411": 13,
    "4301": 9,
    "1501": 16,
    "1511": 17,
    "1601": 18,
    "1604": 21,
    "1605": 22,
    "1606": 23,
    "1621": 24,
    "1701": 25,
    "1801": 27,
    "189901": 28,
}
CREDIT_BALANCE = {
    "1602": 19,
    "1702": 25,
    "2001": 31,
    "2201": 32,
    "221101": 35,
    "221102": 35,
    "221103": 35,
    "2231": 37,
    "2232": 38,
    "2501": 42,
    "2701": 43,
    "2401": 44,
    "3001": 48,
    "3002": 49,
    "3101": 50,
    "3103": 51,
    "3104": 51,
}
TAX_ACCOUNTS = {"222101", "222102", "222103", "222104", "222105", "222106"}
RECLASS = {
    "1122": (4, 34),
    "1123": (5, 33),
    "1221": (8, 39),
    "122101": (8, 39),
    "122105": (8, 39),
    "2202": (5, 33),
    "2203": (4, 34),
    "2241": (8, 39),
    "224101": (8, 39),
    "224102": (8, 39),
    "224103": (8, 39),
    "224104": (8, 39),
    "224105": (8, 39),
}

PAYMENT_KINDS = {"payment", "cash_payment", "platform_payment", "payroll_reserve_payment"}
ACCEPTANCE_KINDS = {"reimbursement_acceptance", "managed_reserve_obligation_settlement"}
SETTLEMENT_SOURCE_SLOTS = {
    **{kind: ("allocations", "settlements", "source_calculation") for kind in PAYMENT_KINDS},
    **{
        kind: ("sources", "accepted_sources", "source_calculation_id")
        for kind in {"employee_advance", *ACCEPTANCE_KINDS}
    },
}


def _issue(field: str, message: str, **details) -> dict:
    return {"field": field, "message": message, "semantics": "accounting", **details}


def _outcome(calculation: Mapping[str, Any]) -> Mapping[str, Any]:
    return calculation.get("outcome") or calculation.get("decoded") or {}


def _values(calculation: Mapping[str, Any]) -> Mapping[str, Any]:
    return _outcome(calculation).get("values", {})


def _lines(calculation: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    return _outcome(calculation).get("lines", ())


def _fact(calculation: Mapping[str, Any]) -> Mapping[str, Any]:
    value = calculation.get("fact_data") or calculation.get("fact") or {}
    return value.get("data", value) if isinstance(value, Mapping) else {}


def _line_amount(line: Mapping[str, Any]) -> int:
    return line.get("debit", 0) - line.get("credit", 0)


def _business(calculation: Mapping[str, Any]) -> dict:
    return {"kind": calculation.get("kind"), "subject_id": calculation.get("subject_id")}


def _party(calculation: Mapping[str, Any], obligation: Mapping[str, Any]):
    party = obligation.get("counterparty_id")
    if isinstance(party, str) and party:
        return ("party", party), party, "creditor"
    if (
        calculation.get("kind") in {"payroll", "payroll_bounded"}
        and (obligation.get("name"), obligation.get("account"))
        in {("employee_social", "224102"), ("employee_housing", "224103")}
        and obligation.get("normal") == "credit"
    ):
        return ("statutory_payroll_obligation", obligation.get("key")), None, "statutory"
    return None, None, "unresolved"


def _obligation(calculation: Mapping[str, Any], item: Mapping[str, Any]) -> dict:
    party_key, creditor_id, party_role = _party(calculation, item)
    return {
        **dict(item),
        "source_business": _business(calculation),
        "source_calculation_id": calculation.get("id"),
        "source_fact_id": calculation.get("fact_id"),
        "source_period": calculation.get("period"),
        "source_result_digest": calculation.get("result_digest"),
        "party_key": party_key,
        "creditor_id": creditor_id,
        "party_role": party_role,
        "state": "resolved" if party_key is not None else "unresolved",
    }


def resolve_calculation_relations(
    calculation: Mapping[str, Any],
    *,
    load_calculation: Callable[[str], Mapping[str, Any]],
    load_parents: Callable[[str], Iterable[str]],
    source_cache: dict | None = None,
) -> dict:
    """Resolve supported frozen calculations to stable obligations and exact source lines."""

    issues: list[dict] = []
    calculations: dict[str, Mapping[str, Any]] = {}
    obligations: dict[str, dict] = {}
    parents: dict[str, tuple[str, ...]] = {}

    def loaded(ident: str) -> Mapping[str, Any] | None:
        if ident not in calculations:
            try:
                record = calculation if ident == calculation.get("id") else load_calculation(ident)
            except (KeyError, ValueError):
                record = None
            if record is None:
                issues.append(
                    _issue(
                        "query_source.calculation_id",
                        "冻结来源核算不存在或不可读取",
                        calculation_id=ident,
                    )
                )
                return None
            calculations[ident] = record
        return calculations[ident]

    def parent_ids(ident: str) -> tuple[str, ...]:
        if ident not in parents:
            parents[ident] = tuple(load_parents(ident))
        return parents[ident]

    sources = {} if source_cache is None else source_cache

    def source_records(record):
        """Memoize obligation-bearing ancestry without recursion or graph copies."""
        root_id = record.get("id")
        if not isinstance(root_id, str):
            return ()
        stack, visiting = [(record, False)], set()
        while stack:
            current, expanded = stack.pop()
            ident = current.get("id")
            if not isinstance(ident, str) or ident in sources:
                continue
            if not expanded:
                if ident in visiting:
                    continue
                visiting.add(ident)
                stack.append((current, True))
                for parent_id in reversed(parent_ids(ident)):
                    parent = loaded(parent_id)
                    if parent is not None and parent_id not in visiting:
                        stack.append((parent, False))
                continue
            branches = [sources.get(parent_id, ()) for parent_id in parent_ids(ident)]
            own = _values(current).get("obligations", ())
            if not own and len(branches) == 1:
                result = branches[0]
            else:
                combined = {}
                for branch in branches:
                    for source_id, items in branch:
                        combined.setdefault(source_id, items)
                if own:
                    combined[ident] = tuple(_obligation(current, item) for item in own)
                result = tuple(combined.items())
            sources[ident] = result
            visiting.discard(ident)
        return sources.get(root_id, ())

    def collect(record):
        for ident, items in source_records(record):
            for resolved in items:
                item = resolved
                key = item.get("key")
                if not isinstance(key, str) or not key:
                    issues.append(
                        _issue(
                            "report_source.obligations", "核算义务缺少稳定键", calculation_id=ident
                        )
                    )
                    continue
                previous = obligations.get(key)
                if previous is not None and previous != resolved:
                    issues.append(
                        _issue(
                            "report_source.obligations",
                            "稳定义务键指向不一致的冻结来源",
                            calculation_id=ident,
                            obligation_key=key,
                        )
                    )
                    continue
                obligations[key] = resolved

    loaded(str(calculation.get("id")))
    collect(calculation)
    line_relations: list[dict] = []
    settlements: list[dict] = []
    lines = _lines(calculation)
    fact = _fact(calculation)
    values = _values(calculation)
    kind = calculation.get("kind")

    def source_by_business(reference: Mapping[str, Any]):
        matches = []
        for parent_id in parent_ids(str(calculation.get("id"))):
            candidate = loaded(parent_id)
            if candidate is None:
                continue
            if candidate.get("kind") == reference.get("source_kind") and candidate.get(
                "subject_id"
            ) == reference.get("source_id"):
                matches.append(candidate)
        return matches[0] if len(matches) == 1 else None

    def source_obligation(source, *, key=None, name=None):
        if source is None:
            return None
        candidates = [
            item
            for item in _values(source).get("obligations", ())
            if (key is None or item.get("key") == key)
            and (name is None or item.get("name") == name)
        ]
        return _obligation(source, candidates[0]) if len(candidates) == 1 else None

    def add_relation(line_no: int, role: str, amount: int, source, item, **extra):
        line_relations.append(
            {
                "line_no": line_no,
                "role": role,
                "amount_fen": amount,
                "party_key": item.get("party_key") if item else None,
                "creditor_id": item.get("creditor_id") if item else None,
                "recipient_id": extra.pop("recipient_id", None),
                "obligation_key": item.get("key") if item else None,
                "obligation_name": item.get("name") if item else None,
                "source_business": (
                    item.get("source_business")
                    if item
                    else (_business(source) if source is not None else None)
                ),
                "source_calculation_id": (
                    item.get("source_calculation_id")
                    if item
                    else (source.get("id") if source is not None else None)
                ),
                "source_fact_id": (
                    item.get("source_fact_id")
                    if item
                    else (source.get("fact_id") if source is not None else None)
                ),
                "state": "resolved" if item is not None else "unresolved",
                **extra,
            }
        )

    def validate_line(line_no: int, *, account: str, amount: int, field: str) -> bool:
        valid = 0 < line_no <= len(lines)
        line = lines[line_no - 1] if valid else {}
        valid = valid and line.get("account") == account and _line_amount(line) == amount
        if not valid:
            issues.append(
                _issue(
                    field,
                    "冻结清偿来源与对应凭证行不一致",
                    calculation_id=calculation.get("id"),
                    line_no=line_no,
                )
            )
        return valid

    if kind in PAYMENT_KINDS:
        allocations = fact.get("allocations", ())
        frozen = values.get("settlements", ())
        if len(allocations) != len(frozen):
            issues.append(
                _issue(
                    "query_source.settlement",
                    "冻结付款的分配与清偿来源数量不一致",
                    calculation_id=calculation.get("id"),
                )
            )
        outgoing = values.get("direction") == "outflow"
        funds_account = {
            "payment": "1002",
            "cash_payment": "1001",
            "platform_payment": "1012",
            "payroll_reserve_payment": "1002",
        }[kind]
        if values.get("direction") not in {"inflow", "outflow"}:
            issues.append(
                _issue(
                    "query_source.settlement",
                    "冻结付款方向无效",
                    calculation_id=calculation.get("id"),
                )
            )
        for index, (allocation, frozen_item) in enumerate(zip(allocations, frozen, strict=False)):
            pointer = frozen_item.get("source_calculation")
            source = loaded(pointer) if isinstance(pointer, str) and pointer else None
            item = source_obligation(source, key=frozen_item.get("obligation"))
            amount = frozen_item.get("amount_fen")
            recipient = (
                allocation.get("recipient_id")
                if fact.get("payment_method", "individual") == "bank_batch"
                else fact.get("counterparty_id")
            )
            stable = (
                source is not None
                and item is not None
                and source.get("kind") == allocation.get("source_kind")
                and source.get("subject_id") == allocation.get("source_id")
                and item.get("name") == allocation.get("obligation")
                and item.get("normal") == ("credit" if outgoing else "debit")
                and type(amount) is int
                and amount > 0
                and amount == allocation.get("amount_fen")
            )
            obligation_line = index * 2 + (1 if outgoing else 2)
            funds_line = index * 2 + (2 if outgoing else 1)
            if stable:
                expected = amount if outgoing else -amount
                stable = validate_line(
                    obligation_line,
                    account=item["account"],
                    amount=expected,
                    field="query_source.settlement",
                ) and validate_line(
                    funds_line,
                    account=funds_account,
                    amount=-expected,
                    field="query_source.settlement",
                )
            if not stable:
                issues.append(
                    _issue(
                        "query_source.settlement",
                        "付款未能关联唯一的冻结义务、金额和行序",
                        calculation_id=calculation.get("id"),
                        allocation_index=index,
                    )
                )
                item = None
            add_relation(
                obligation_line,
                "settlement",
                (amount if outgoing else -amount) if type(amount) is int else 0,
                source,
                item,
                recipient_id=recipient,
            )
            add_relation(
                funds_line,
                "funds",
                (-amount if outgoing else amount) if type(amount) is int else 0,
                source,
                item,
                recipient_id=recipient,
            )
            settlements.append(
                {
                    "index": index,
                    "mode": "payment",
                    "settlement_business": _business(calculation),
                    "settlement_calculation_id": calculation.get("id"),
                    "settlement_fact_id": calculation.get("fact_id"),
                    "source_business": _business(source) if source else None,
                    "source_calculation_id": source.get("id") if source else pointer,
                    "source_fact_id": source.get("fact_id") if source else None,
                    "obligation_key": item.get("key") if item else frozen_item.get("obligation"),
                    "obligation_name": item.get("name") if item else allocation.get("obligation"),
                    "amount_fen": amount,
                    "party_key": item.get("party_key") if item else None,
                    "creditor_id": item.get("creditor_id") if item else None,
                    "recipient_id": recipient,
                    "line_numbers": [obligation_line, funds_line],
                    "state": "resolved" if stable else "unresolved",
                }
            )
        cursor = 2 * len(frozen)
        for transfer in values.get("tax_transfers", ()):
            vat = transfer.get("vat_fen")
            if type(vat) is not int or vat < 0:
                issues.append(
                    _issue(
                        "query_source.tax_transfer",
                        "冻结转税金额无效",
                        calculation_id=calculation.get("id"),
                    )
                )
                continue
            if not vat:
                continue
            pointer = transfer.get("source_calculation")
            source = loaded(pointer) if isinstance(pointer, str) and pointer else None
            for account, amount in (("222104", vat), ("222101", -vat)):
                cursor += 1
                valid = (
                    source is not None
                    and source.get("subject_id") == transfer.get("source_id")
                    and validate_line(
                        cursor,
                        account=account,
                        amount=amount,
                        field="query_source.tax_transfer",
                    )
                )
                add_relation(
                    cursor,
                    "tax_transfer",
                    amount,
                    source,
                    None,
                    state="resolved" if valid else "unresolved",
                    tax_source_calculation_id=pointer,
                )
        if kind == "payroll_reserve_payment":
            amount = values.get("reserve_return_fen")
            for account, signed in (
                ("5602", amount),
                ("1002", -amount if type(amount) is int else amount),
            ):
                cursor += 1
                valid = (
                    type(amount) is int
                    and amount > 0
                    and validate_line(
                        cursor, account=account, amount=signed, field="query_source.reserve_return"
                    )
                )
                add_relation(
                    cursor,
                    "reserve_return",
                    signed if type(signed) is int else 0,
                    None,
                    None,
                    state="resolved" if valid else "unresolved",
                )

    elif kind == "settlement":
        for reference in (fact.get("first"), fact.get("second")):
            reference = reference or {}
            source = source_by_business(reference)
            item = source_obligation(source, name=reference.get("obligation"))
            amount = reference.get("amount_fen")
            line_no = 1 if item and item.get("normal") == "credit" else 2
            expected = amount if line_no == 1 else -amount if type(amount) is int else amount
            valid = (
                item is not None
                and type(amount) is int
                and amount > 0
                and validate_line(
                    line_no,
                    account=item["account"],
                    amount=expected,
                    field="query_source.settlement",
                )
            )
            add_relation(
                line_no,
                "offset",
                expected if type(expected) is int else 0,
                source,
                item if valid else None,
            )
            settlements.append(
                {
                    "index": len(settlements),
                    "mode": "offset",
                    "settlement_business": _business(calculation),
                    "settlement_calculation_id": calculation.get("id"),
                    "settlement_fact_id": calculation.get("fact_id"),
                    "source_business": _business(source) if source else None,
                    "source_calculation_id": source.get("id") if source else None,
                    "source_fact_id": source.get("fact_id") if source else None,
                    "obligation_key": item.get("key") if item else None,
                    "obligation_name": reference.get("obligation"),
                    "amount_fen": amount,
                    "party_key": item.get("party_key") if valid else None,
                    "creditor_id": item.get("creditor_id") if valid else None,
                    "recipient_id": None,
                    "line_numbers": [line_no],
                    "state": "resolved" if valid else "unresolved",
                }
            )

    elif kind in {"employee_advance", *ACCEPTANCE_KINDS}:
        sources = fact.get("sources", ())
        accepted = values.get("accepted_sources", ())
        requires_frozen_acceptance = kind in ACCEPTANCE_KINDS
        if requires_frozen_acceptance and len(sources) != len(accepted):
            issues.append(
                _issue(
                    "query_source.accepted",
                    "冻结承接来源与业务来源数量不一致",
                    calculation_id=calculation.get("id"),
                )
            )
        for index, reference in enumerate(sources):
            frozen_item = accepted[index] if index < len(accepted) else {}
            pointer = frozen_item.get("source_calculation_id")
            source = (
                loaded(pointer)
                if pointer
                else (None if requires_frozen_acceptance else source_by_business(reference))
            )
            item = source_obligation(
                source,
                key=frozen_item.get("obligation") if frozen_item else None,
                name=None if frozen_item else reference.get("obligation"),
            )
            amount = frozen_item.get("amount_fen", reference.get("amount_fen"))
            recipient = frozen_item.get("recipient_id", reference.get("recipient_id"))
            if kind == "managed_reserve_obligation_settlement":
                recipient = fact.get("recipient_id")
            frozen_source_fact_id = frozen_item.get("source_fact_id")
            frozen_matches = not requires_frozen_acceptance or (
                frozen_item.get("amount_fen") == reference.get("amount_fen")
                and item is not None
                and frozen_item.get("obligation") == item.get("key")
                and reference.get("obligation") == item.get("name")
                and (
                    kind != "reimbursement_acceptance"
                    or frozen_source_fact_id == source.get("fact_id")
                )
                and (
                    frozen_source_fact_id is None or frozen_source_fact_id == source.get("fact_id")
                )
                and (
                    frozen_item.get("recipient_id") is None
                    or frozen_item.get("recipient_id") == reference.get("recipient_id")
                )
            )
            valid = (
                item is not None
                and source.get("kind") == reference.get("source_kind")
                and source.get("subject_id") == reference.get("source_id")
                and item.get("normal") == "credit"
                and frozen_matches
                and type(amount) is int
                and amount > 0
                and validate_line(
                    index + 1, account=item["account"], amount=amount, field="query_source.accepted"
                )
            )
            if not valid:
                issues.append(
                    _issue(
                        "query_source.accepted",
                        "承接来源事实、冻结引用、原义务与凭证行不一致",
                        calculation_id=calculation.get("id"),
                        source_index=index,
                    )
                )
            add_relation(
                index + 1,
                "advance" if kind == "employee_advance" else "accepted",
                amount if type(amount) is int else 0,
                source,
                item if valid else None,
                recipient_id=recipient,
            )
            settlements.append(
                {
                    "index": index,
                    "mode": "advance" if kind == "employee_advance" else "accepted",
                    "settlement_business": _business(calculation),
                    "settlement_calculation_id": calculation.get("id"),
                    "settlement_fact_id": calculation.get("fact_id"),
                    "source_business": _business(source) if source else None,
                    "source_calculation_id": source.get("id") if source else pointer,
                    "source_fact_id": (
                        frozen_item.get("source_fact_id")
                        if "source_fact_id" in frozen_item
                        else (source.get("fact_id") if source else None)
                    ),
                    "obligation_key": frozen_item.get("obligation")
                    if frozen_item
                    else (item.get("key") if item else None),
                    "obligation_name": reference.get("obligation"),
                    "amount_fen": amount,
                    "party_key": item.get("party_key") if valid else None,
                    "creditor_id": item.get("creditor_id") if valid else None,
                    "recipient_id": recipient,
                    "line_numbers": [index + 1],
                    "state": "resolved" if valid else "unresolved",
                }
            )

    elif kind == "overpayment":
        reference = {
            "source_kind": fact.get("source_kind"),
            "source_id": fact.get("source_id"),
            "obligation": fact.get("obligation_name"),
        }
        source = source_by_business(reference)
        item = source_obligation(source, name=reference["obligation"])
        amount = values.get("overpayment_fen")
        valid = (
            item is not None
            and type(amount) is int
            and amount > 0
            and validate_line(
                2, account=item["account"], amount=-amount, field="query_source.overpayment"
            )
        )
        add_relation(
            2,
            "overpayment_source",
            -amount if type(amount) is int else 0,
            source,
            item if valid else None,
        )

    # A calculation's own obligations can share one aggregate line.  Expand only
    # when their stable amounts exactly conserve that line.
    own = [_obligation(calculation, item) for item in values.get("obligations", ())]
    own_party_accounts = {item.get("account") for item in own if item.get("party_key") is not None}
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for item in own:
        if type(item.get("amount_fen")) is int and item["amount_fen"] > 0:
            grouped[(item.get("account"), item.get("normal"))].append(item)
    for (account, normal), items in grouped.items():
        expected = sum(item["amount_fen"] for item in items) * (1 if normal == "debit" else -1)
        candidates = [
            number
            for number, line in enumerate(lines, 1)
            if line.get("account") == account and _line_amount(line) == expected
        ]
        if len(candidates) != 1:
            continue
        line_no = candidates[0]
        existing = {
            relation.get("obligation_key")
            for relation in line_relations
            if relation["line_no"] == line_no
        }
        for item in items:
            if item["key"] not in existing:
                add_relation(
                    line_no,
                    "created_obligation",
                    item["amount_fen"] * (1 if normal == "debit" else -1),
                    calculation,
                    item,
                )

    candidates_by_account: dict[str, set[Hashable]] = defaultdict(set)
    for item in obligations.values():
        if item.get("party_key") is not None:
            candidates_by_account[item["account"]].add(item["party_key"])
    return {
        "business": _business(calculation),
        "calculation_id": calculation.get("id"),
        "fact_id": calculation.get("fact_id"),
        "obligations": list(obligations.values()),
        "settlements": settlements,
        "line_relations": line_relations,
        "party_candidates_by_account": dict(candidates_by_account),
        "own_party_accounts": own_party_accounts,
        "issues": issues,
    }


def report_party_splits(
    row: Mapping[str, Any], resolution: Mapping[str, Any], *, explicit_party_id: str | None = None
) -> dict:
    """Resolve one reclassifiable row, accepting an exact report classification."""

    amount = row.get("amount_fen", row.get("amount"))
    reverse = -1 if row.get("reverses_id") else 1
    original = amount * reverse if type(amount) is int else None
    relations = [
        relation
        for relation in resolution.get("line_relations", ())
        if relation.get("line_no") == row.get("line_no")
        and relation.get("role") not in {"funds", "tax_transfer", "reserve_return"}
    ]
    inferred = None
    if (
        original is not None
        and relations
        and all(
            relation.get("state") == "resolved" and relation.get("party_key") is not None
            for relation in relations
        )
        and sum(relation["amount_fen"] for relation in relations) == original
    ):
        parts: dict[Hashable, int] = defaultdict(int)
        for relation in relations:
            parts[relation["party_key"]] += relation["amount_fen"] * reverse
        inferred = tuple((party, part) for party, part in parts.items() if part)
    choices = {party for party, _ in inferred} if inferred is not None else set()
    if (
        inferred is None
        and not relations
        and row.get("account") not in resolution.get("own_party_accounts", ())
    ):
        choices = set(resolution.get("party_candidates_by_account", {}).get(row.get("account"), ()))
    explicit_key = ("party", explicit_party_id) if explicit_party_id else None
    issues = []
    if explicit_key is not None and choices and (len(choices) != 1 or explicit_key not in choices):
        issues.append(
            _issue(
                "counterparty_id",
                "分类与已确认交易方不一致",
                voucher_version_id=row.get("reverses_id") or row.get("version_id"),
                line_no=row.get("line_no"),
            )
        )
    if inferred is not None:
        party_key = inferred[0][0] if len(inferred) == 1 else None
        return {
            "state": "resolved",
            "party_key": explicit_key or party_key,
            "party_id": explicit_party_id
            or (party_key[1] if party_key and party_key[0] == "party" else None),
            "splits": inferred,
            "relations": relations,
            "issues": issues,
        }
    if explicit_key is not None:
        return {
            "state": "resolved",
            "party_key": explicit_key,
            "party_id": explicit_party_id,
            "splits": ((explicit_key, amount),),
            "relations": relations,
            "issues": issues,
        }
    if len(choices) == 1 and type(amount) is int:
        party_key = next(iter(choices))
        return {
            "state": "resolved",
            "party_key": party_key,
            "party_id": party_key[1] if party_key[0] == "party" else None,
            "splits": ((party_key, amount),),
            "relations": relations,
            "issues": issues,
        }
    issues.append(
        _issue(
            "report_classification.counterparty_id",
            "往来明细无法唯一归属交易方",
            voucher_version_id=row.get("reverses_id") or row.get("version_id"),
            line_no=row.get("line_no"),
        )
    )
    return {
        "state": "unresolved",
        "party_key": None,
        "party_id": None,
        "splits": None,
        "relations": relations,
        "issues": issues,
    }


def resolve_line_parties(
    calculation: Mapping[str, Any],
    *,
    load_calculation: Callable[[str], Mapping[str, Any]],
    load_parents: Callable[[str], Iterable[str]],
    explicit_classifications: Mapping[int, str] | None = None,
    sign: int = 1,
) -> dict:
    """Resolve every frozen voucher line, including exact explicit classifications."""

    resolution = resolve_calculation_relations(
        calculation, load_calculation=load_calculation, load_parents=load_parents
    )
    explicit_classifications = explicit_classifications or {}
    lines = {}
    for line_no, line in enumerate(_lines(calculation), 1):
        row = {
            "line_no": line_no,
            "account": line.get("account"),
            "amount": sign * _line_amount(line),
            "reverses_id": "reversal" if sign < 0 else None,
        }
        lines[line_no] = report_party_splits(
            row, resolution, explicit_party_id=explicit_classifications.get(line_no)
        )
    return {**resolution, "line_parties": lines}


def classify_financial_position(rows: Iterable[Mapping[str, Any]]) -> dict:
    """Classify signed account rows after netting each (account, stable party key)."""

    direct: dict[str, int] = defaultdict(int)
    parties: dict[tuple[str, Hashable], int] = defaultdict(int)
    unknown_lines: set[int] = set()
    unknown_account = False
    issues: list[dict] = []
    known = (
        CASH_ACCOUNTS
        | set(PROFIT_ACCOUNTS)
        | set(DEBIT_BALANCE)
        | set(CREDIT_BALANCE)
        | TAX_ACCOUNTS
        | set(RECLASS)
    )
    for index, row in enumerate(rows):
        account = row.get("account")
        amount = row.get("amount_fen", row.get("amount"))
        if not isinstance(account, str) or type(amount) is not int:
            issues.append(
                _issue("financial_position.row", "财务位置行缺少有效科目或整数分金额", row=index)
            )
            unknown_account = True
            continue
        if not amount:
            continue
        if account not in known:
            issues.append(_issue("account_mapping", "存在未映射的非零账户余额", account=account))
            unknown_account = True
            continue
        if account not in RECLASS:
            direct[account] += amount
            continue
        splits = row.get("party_splits")
        if (
            splits is None
            and row.get("party_key") is not None
            and row.get("party_state") != "unresolved"
        ):
            splits = ((row["party_key"], amount),)
        if splits is None and row.get("party") is not None:
            party = row["party"]
            party_key = party if isinstance(party, tuple) else ("party", party)
            splits = ((party_key, amount),)
        valid = splits is not None
        normalized = []
        if valid:
            for split in splits:
                if isinstance(split, Mapping):
                    party, part = split.get("party_key"), split.get("amount_fen")
                else:
                    try:
                        party, part = split
                    except (TypeError, ValueError):
                        valid = False
                        break
                if party is None or not isinstance(party, Hashable) or type(part) is not int:
                    valid = False
                    break
                normalized.append((party, part))
            valid = valid and sum(part for _, part in normalized) == amount
        if not valid:
            affected = RECLASS[account]
            unknown_lines.update(affected)
            issues.append(
                _issue(
                    "financial_position.party",
                    "往来余额缺少精确稳定归属，不能跨未知对象抵销",
                    account=account,
                    version_id=row.get("reverses_id") or row.get("version_id"),
                    line_no=row.get("line_no"),
                    affected_lines=list(affected),
                )
            )
            continue
        for party, part in normalized:
            parties[(account, party)] += part

    result: dict[int, int | None] = {line: 0 for line in range(1, 54)}
    for (account, _party_key), amount in parties.items():
        result[RECLASS[account][0 if amount >= 0 else 1]] += abs(amount)
    for account, amount in direct.items():
        if account in CASH_ACCOUNTS:
            result[1] += amount
        elif account in PROFIT_ACCOUNTS:
            result[51] -= amount
        elif account in DEBIT_BALANCE:
            result[DEBIT_BALANCE[account]] += amount
        elif account in CREDIT_BALANCE:
            result[CREDIT_BALANCE[account]] += amount if account == "1702" else -amount
        elif account in TAX_ACCOUNTS:
            result[14 if amount > 0 else 36] += abs(amount)
    for line in unknown_lines:
        result[line] = None

    def total(lines: Iterable[int], *, subtract=()) -> int | None:
        selected = tuple(lines)
        if any(result[line] is None for line in (*selected, *subtract)):
            return None
        return sum(result[line] for line in selected) - sum(result[line] for line in subtract)

    inventory_total = total((10, 11, 12, 13))
    result[9] = None if inventory_total is None else result[9] + inventory_total
    result[20] = total((18,), subtract=(19,))
    result[15] = total((*range(1, 10), 14))
    result[29] = total((16, 17, 20, 21, 22, 23, 24, 25, 26, 27, 28))
    result[30] = total((15, 29))
    result[41] = total(range(31, 41))
    result[46] = total(range(42, 46))
    result[47] = total((41, 46))
    result[52] = total(range(48, 52))
    result[53] = total((47, 52))
    if unknown_account:
        for line in (30, 47, 52, 53):
            result[line] = None
    equation_valid = None if result[30] is None or result[53] is None else result[30] == result[53]
    return {
        "lines": result,
        "assets_fen": result[30],
        "liabilities_fen": result[47],
        "capital_fen": total((48, 49, 50)),
        "cumulative_result_fen": result[51],
        "equity_fen": result[52],
        "equation_valid": equation_valid,
        "complete": not issues,
        "issues": issues,
    }
