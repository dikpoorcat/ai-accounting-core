"""Compose only the financial relationships explicitly selected for one review."""

from dataclasses import dataclass, field

from ai_accounting_modular.contracts.public import ContractError, canonical_json, content_digest
from ai_accounting_modular.ledger.public import (
    ReviewMovementQuery,
    ReviewObligationQuery,
    SettlementEventQuery,
)
from ai_accounting_modular.money.public import ReviewRowQuery

from ._review_wire import chunks


def ref_key(reference):
    return reference["business_id"], reference["slot"]


def normalize(value):
    if isinstance(value, dict):
        return {key: normalize(child) for key, child in sorted(value.items())}
    if isinstance(value, list):
        return sorted((normalize(child) for child in value), key=canonical_json)
    return value


def scope_of(request):
    keys = (
        ("action", "cutoff_period", "collection", "criteria")
        if request["action"] == "find"
        else ("action", "cutoff_period", "subject", "related_scope")
    )
    return normalize({key: request.get(key, {}) for key in keys})


def obligation_query(cutoff, criteria):
    return ReviewObligationQuery(
        cutoff,
        **{
            key: tuple(value) if isinstance(value, list) else value
            for key, value in criteria.items()
        },
    )


def check_periods(criteria, cutoff):
    first, last = criteria.get("period_from"), criteria.get("period_to")
    if (first and last and first > last) or (first and first > cutoff) or (last and last > cutoff):
        raise ContractError("invalid_input", "核查起月不能晚于止月，范围不能超过经济截止月。")


@dataclass
class ReviewScope:
    scope: dict
    selections: dict = field(default_factory=dict)
    subject_basis: list = field(default_factory=list)
    direct_obligations: set = field(default_factory=set)
    settlement_sources: set = field(default_factory=set)
    direct_movements: set = field(default_factory=set)
    direct_businesses: set = field(default_factory=set)

    def version(self, read):
        return content_digest(
            {
                "company_id": read.company_id,
                "database_id": read.database_id,
                "scope": self.scope,
                "subject": self.subject_basis,
                "selections": {key: value.version for key, value in self.selections.items()},
            }
        )

    def relationship(self, collection, value):
        if self.scope["action"] == "find":
            return "candidate"
        if collection in ("statement_rows", "links"):
            return "direct"
        if collection == "obligations":
            direct = ref_key(value.definition.reference) in self.direct_obligations
        elif collection == "movements":
            direct = value["movement"]["movement_id"] in self.direct_movements
        else:
            source = value.use.value["source"]
            direct = (
                ref_key(source) in self.settlement_sources
                or value.use.source["business_id"] in self.direct_businesses
                or (
                    value.movement is not None
                    and value.movement.value["movement_id"] in self.direct_movements
                )
            )
        return "direct" if direct else "candidate"


def capture_find(application, scope, read):
    captured = ReviewScope(scope)
    collection, criteria = scope["collection"], scope["criteria"]
    check_periods(criteria, scope["cutoff_period"])
    if collection == "obligations":
        selected = application.ledger.capture_review_obligations(
            obligation_query(scope["cutoff_period"], criteria), read
        )
    elif collection == "movements":
        selected = application.money.capture_review_movements(ReviewMovementQuery(**criteria), read)
    else:
        selected = application.money.capture_review_rows(ReviewRowQuery(**criteria), read)
    captured.selections[collection] = selected
    return captured


def capture_inspect(application, scope, read, *, resuming=False):
    captured = ReviewScope(scope)
    subject, cutoff = scope["subject"], scope["cutoff_period"]
    ledger, money = application.ledger, application.money
    first = read.start_period
    obligations, movements, settlements = [], [], []
    kind = subject["type"]

    def missing():
        raise ContractError(
            "snapshot_changed" if resuming else "invalid_input",
            "核查主体不在本公司的当前有效范围内，请重新确认其身份及截止月。",
        )

    if kind in ("business", "obligation", "statement_row"):
        business_id = subject.get("business_id", subject.get("statement_business_id"))
        head = read.repository("application").heads([business_id]).get(business_id)
        if head is None or head["state"] != "active":
            missing()
        if head["period"] is None:
            raise ContractError("dependency_conflict", "该业务尚未产生可核查的正式结果。")
        if head["period"] > cutoff:
            missing()
        captured.subject_basis = [
            ref
            for ref in ledger.get_result_refs([business_id], read)
            if ref["business_period"] <= cutoff
        ]
        if kind == "business":
            captured.direct_businesses.add(business_id)
            obligations.append(ReviewObligationQuery(cutoff, business_ids=(business_id,)))
            movements.append(ReviewMovementQuery(first, cutoff, business_ids=(business_id,)))
        elif kind == "obligation":
            obligations.append(
                ReviewObligationQuery(
                    cutoff, references=({"business_id": business_id, "slot": subject["slot"]},)
                )
            )
        if kind == "statement_row" or head["kind"] in (
            "money.bank_statement",
            "money.platform_activity",
        ):
            rows = money.capture_review_rows(
                ReviewRowQuery(
                    statement_ids=(business_id,),
                    row_ids=(subject["row_id"],) if kind == "statement_row" else (),
                ),
                read,
            )
            if kind == "statement_row" and not rows.total_count:
                missing()
            captured.selections["statement_rows"] = rows
            captured.direct_movements.update(
                key for link in rows.links for key in link.movement_ids
            )
            captured.direct_businesses.update(
                key for link in rows.links for key in link.linked_business_ids
            )
    else:
        captured.direct_movements.add(subject["movement_id"])

    # Discover direct members and candidates together, then follow only their
    # selected events. No independent second calculation of the same balance.
    related = scope["related_scope"]
    extra = related.get("obligations")
    if extra:
        if "criteria" in extra:
            check_periods(extra["criteria"], cutoff)
            obligations.append(obligation_query(cutoff, extra["criteria"]))
        else:
            obligations.append(ReviewObligationQuery(cutoff, references=tuple(extra["references"])))
    selected = ledger.capture_review_obligations(obligations, read) if obligations else None
    if selected and kind in ("business", "obligation"):
        captured.direct_obligations.update(
            ref_key(ref)
            for ref in selected.references
            if ref["business_id"] == subject["business_id"]
            and (kind == "business" or ref["slot"] == subject["slot"])
        )
    if kind == "obligation" and not captured.direct_obligations:
        missing()
    # Source obligations define which histories belong to the subject. A
    # co-paid obligation may be displayed directly without inheriting its history.
    captured.settlement_sources.update(captured.direct_obligations)
    if extra and "references" in extra:
        requested = {ref_key(ref) for ref in extra["references"]}
        if not requested <= {ref_key(ref) for ref in selected.references}:
            missing()
    if selected:
        captured.selections["obligations"] = selected
    for batch in chunks(sorted(captured.direct_businesses)):
        settlements.append(SettlementEventQuery(first, cutoff, consumer_business_ids=batch))
    for batch in chunks(sorted(captured.direct_movements)):
        movements.append(ReviewMovementQuery(first, cutoff, movement_ids=batch))
    if related.get("movements"):
        check_periods(related["movements"], cutoff)
        movements.append(ReviewMovementQuery(**related["movements"]))
    chosen_movements = money.capture_review_movements(movements, read) if movements else None
    if kind == "movement" and subject["movement_id"] not in chosen_movements.movement_ids:
        missing()
    if chosen_movements:
        captured.direct_movements.update(
            movement.movement_id
            for movement in chosen_movements.items
            if movement.reference["business_id"] in captured.direct_businesses
        )
        for batch in chunks(chosen_movements.movement_ids):
            settlements.append(SettlementEventQuery(first, cutoff, movement_ids=batch))
    # Obligation selection already captured the exact uses that establish its
    # balance. Reuse those identities when no other event range was requested.
    events = selected.settlements if selected else None
    if settlements:
        found = ledger.capture_review_settlements(settlements, read)
        events = ledger.merge_review_settlements((events, found), read) if events else found
    if events:
        captured.selections["settlements"] = events
        # Funds are included for selected obligations; their other allocation
        # histories are not traversed. Newly discovered obligations only expose balances.
        existing = set(chosen_movements.movement_ids) if chosen_movements else set()
        additional = set(events.movement_ids) - existing
        captured.direct_movements.update(
            event.movement_id
            for event in events.items
            if event.movement_id is not None
            and (
                ref_key(event.resource_reference) in captured.direct_obligations
                or event.consumer_reference["business_id"] in captured.direct_businesses
            )
        )
        captured.direct_obligations.update(
            ref_key(event.resource_reference)
            for event in events.items
            if event.consumer_reference["business_id"] in captured.direct_businesses
            or event.movement_id in captured.direct_movements
        )
        known_refs = {ref_key(ref) for ref in selected.references} if selected else set()
        additional_refs = [ref for ref in events.source_refs if ref_key(ref) not in known_refs]
        if additional_refs:
            found = ledger.capture_review_obligations(
                [
                    ReviewObligationQuery(cutoff, references=batch)
                    for batch in chunks(additional_refs)
                ],
                read,
            )
            captured.selections["obligations"] = (
                ledger.merge_review_obligations((selected, found), read) if selected else found
            )
        if additional:
            found = money.capture_review_movements(
                [
                    ReviewMovementQuery(first, cutoff, movement_ids=batch)
                    for batch in chunks(sorted(additional))
                ],
                read,
            )
            chosen_movements = (
                money.merge_review_movements((chosen_movements, found), read)
                if chosen_movements
                else found
            )
    if chosen_movements:
        captured.selections["movements"] = chosen_movements
    return captured
