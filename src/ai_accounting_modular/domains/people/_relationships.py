"""Current relationship facts and month projections, independent of payroll."""

from calendar import monthrange
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal

from ai_accounting_modular.contracts.public import (
    ContractError,
    Fact,
    FactIssue,
    PeopleRelationshipMoment,
    PeopleRelationshipState,
    content_digest,
    validate_contract,
)

type RelationshipState = PeopleRelationshipState | Literal["unknown"]

_STATES = ("regular", "unpaid_leave", "departed", "labor", "labor_ended", "unknown")
_AFTER_LAST_DAY = date.max.toordinal() + 1


@dataclass(frozen=True)
class RelationshipSegment:
    """A half-open interval; month precision remains a month, never a guessed day."""

    state: PeopleRelationshipState
    start: PeopleRelationshipMoment
    end: PeopleRelationshipMoment | None
    relationship_start: Literal[True] | None = None


@dataclass(frozen=True)
class PersonRelationship:
    person_id: str
    business_id: str | None
    period: str
    version: str
    segments: tuple[RelationshipSegment, ...]
    period_end_state: RelationshipState
    period_states: tuple[RelationshipState, ...]
    possible_period_states: tuple[RelationshipState, ...]
    employee_member: bool | None
    labor_member: bool | None
    evidence_ids: tuple[str, ...]
    issues: tuple[FactIssue, ...]


def checked_person_ids(ids: Sequence[str]) -> tuple[str, ...]:
    if isinstance(ids, str | bytes) or not isinstance(ids, Sequence) or len(ids) > 500:
        raise ContractError("invalid_input", "人员关系须明确指定最多500个人员身份。")
    for person_id in ids:
        validate_contract("core.schema.json", "Id", person_id)
    return tuple(dict.fromkeys(ids))


def _bounds(moment):
    if len(moment) == 10:
        ordinal = date.fromisoformat(moment).toordinal()
        return ordinal, ordinal
    year, month = map(int, moment.split("-"))
    return date(year, month, 1).toordinal(), date(
        year, month, monthrange(year, month)[1]
    ).toordinal()


def _issue(fact, field, code, message, *, blocking="none") -> FactIssue:
    return {
        **({"business_id": fact["business_id"]} if fact is not None else {}),
        "code": code,
        "field": field,
        "meaning": "人员关系管理时间；不作为工资、缴费或扣税的核算依据",
        "message": message,
        "allowed_precision": ["month", "day"],
        "reusable_sources": [],
        "blocking_scope": blocking,
    }


def ordered_bounds(fact):
    """Constrain all confirmed points together; equal-day transitions are valid."""
    segments = fact["data"]["segments"]
    points = [_bounds(segment["effective_from"]) for segment in segments]
    lower = 0
    earliest = []
    for index, (lo, hi) in enumerate(points):
        lower = max(lower, lo)
        if lower > hi:
            issue = _issue(
                fact,
                f"/data/segments/{index}/effective_from",
                "relationship_order_conflict",
                "该生效时间与已确认的前后顺序矛盾；月份精度不补造实际日。",
                blocking="business",
            )
            raise ContractError("invalid_input", issue["message"], fact_issues=[issue])
        earliest.append(lower)
        if index and segments[index].get("relationship_start"):
            active = (
                {"regular", "unpaid_leave"} if segments[index]["state"] != "labor" else {"labor"}
            )
            if segments[index - 1]["state"] in active:
                issue = _issue(
                    fact,
                    f"/data/segments/{index}/relationship_start",
                    "relationship_start_conflict",
                    "前一段已属于同一持续雇佣或劳务关系；复工或状态变动不能再次标作关系开始。",
                    blocking="business",
                )
                raise ContractError("invalid_input", issue["message"], fact_issues=[issue])
    upper = _AFTER_LAST_DAY
    latest = []
    for _, hi in reversed(points):
        upper = min(upper, hi)
        latest.append(upper)
    return tuple(zip(earliest, reversed(latest), strict=True))


def _presence(states, bounds, month_start, month_end, *, members=None):
    # Merge adjacent equal labels only for reasoning. In particular, contiguous
    # regular/leave intervals can guarantee employment even if neither guarantees
    # an individually fixed interval. The original fact and precision stay intact.
    labels = ["unknown", *states]
    if members is not None:
        labels = [
            "unknown" if state == "unknown" else "member" if state in members else "other"
            for state in labels
        ]
    edges = [(0, 0), *bounds, (_AFTER_LAST_DAY, _AFTER_LAST_DAY)]
    certain, possible = set(), set()
    index = 0
    while index < len(labels):
        stop = index + 1
        while stop < len(labels) and labels[stop] == labels[index]:
            stop += 1
        start_lo, start_hi = edges[index]
        end_lo, end_hi = edges[stop]
        if max(start_lo, month_start) < min(end_hi, month_end):
            possible.add(labels[index])
        if max(start_hi, month_start) < min(end_lo, month_end):
            certain.add(labels[index])
        index = stop
    return certain, possible - certain


def _member(states, bounds, lo, hi, members):
    certain, possible = _presence(states, bounds, lo, hi, members=members)
    if "member" in certain:
        return True
    if "member" in possible or "unknown" in certain | possible:
        return None
    return False


def project_relationship(person_id, period, fact: Fact | None, read_context) -> PersonRelationship:
    lo, last_day = _bounds(period)
    hi = last_day + 1
    items = fact["data"]["segments"] if fact is not None else []
    bounds = ordered_bounds(fact) if fact is not None else ()
    states = [item["state"] for item in items]
    certain, possible = _presence(states, bounds, lo, hi)
    issues = []
    if "unknown" in certain | possible:
        issues.append(
            _issue(
                fact,
                "/data/segments/0/effective_from" if items else "/data/segments",
                "relationship_history_unknown",
                "本月部分或全部人员关系尚不明确；不能由工资或缴费资料反推。",
            )
        )
    for index, (item, (earliest, latest)) in enumerate(zip(items, bounds, strict=True)):
        if item["effective_from"] == period and earliest != latest:
            issues.append(
                _issue(
                    fact,
                    f"/data/segments/{index}/effective_from",
                    "relationship_month_precision",
                    "该关系变动只知月份；保留本月内可能的状态，不补造实际日。",
                )
            )
    return PersonRelationship(
        person_id=person_id,
        business_id=fact["business_id"] if fact is not None else None,
        period=period,
        version=content_digest(
            {
                "company_id": read_context.company_id,
                "database_id": read_context.database_id,
                "person_id": person_id,
                "period": period,
                "fact": fact,
            }
        ),
        segments=tuple(
            RelationshipSegment(
                state=item["state"],
                start=item["effective_from"],
                end=items[index + 1]["effective_from"] if index + 1 < len(items) else None,
                relationship_start=item.get("relationship_start"),
            )
            for index, item in enumerate(items)
        ),
        period_end_state=next(
            (item["state"] for item in reversed(items) if item["effective_from"][:7] <= period),
            "unknown",
        ),
        period_states=tuple(state for state in _STATES if state in certain),
        possible_period_states=tuple(state for state in _STATES if state in possible),
        employee_member=_member(states, bounds, lo, hi, {"regular", "unpaid_leave"}),
        labor_member=_member(states, bounds, lo, hi, {"labor"}),
        evidence_ids=tuple(fact["evidence_ids"]) if fact is not None else (),
        issues=tuple(issues),
    )
