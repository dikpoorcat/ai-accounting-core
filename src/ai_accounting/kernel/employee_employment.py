"""One source-preserving workforce timeline for employees and labor.

Profile revisions are immutable event identities. Calendar bounds reason about
precision only; a month is never persisted as an invented actual day.
"""

from __future__ import annotations

import base64
import binascii
import calendar
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema, model_validator

from .content_history_context import source_canonical as canonical
from .content_history_context import source_digest as digest
from .contracts import KernelError, NeedsInformation
from .types import ActualDate, YearMonth

WorkforceState = Literal[
    "regular", "unpaid_leave", "departed", "labor", "labor_ended", "unknown"
]
WorkforceDate = Annotated[
    YearMonth | ActualDate,
    WithJsonSchema({
        "type": "string",
        "anyOf": [
            {"pattern": r"^[0-9]{4}-(0[1-9]|1[0-2])$"},
            {"pattern": r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"},
        ],
        "x-accounting-fact": {
            "role": "management", "meaning": "workforce_business_effective_date",
            "allowed_precision": ["month", "day"],
            "reusable_sources": ["owner_confirmation", "workforce_document"],
        },
    }),
]
EventReference = Annotated[str, Field(min_length=1, max_length=200)]


class WorkforceEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal[
        "hire", "start_labor", "unpaid_leave", "resume", "depart", "end_labor",
        "to_employee", "to_labor",
    ]
    effective_date: WorkforceDate
    initial_unpaid_leave: bool = False
    after_event_id: EventReference | None = None
    replaces_event_id: EventReference | None = None

    @model_validator(mode="after")
    def validate_initial_state(self):
        if self.initial_unpaid_leave and self.action not in {"hire", "to_employee"}:
            raise ValueError("initial_unpaid_leave requires hire or to_employee")
        return self


class WorkforceChange(WorkforceEvent):
    """A complete replacement fact when replaces_event_id is supplied."""


def issue(field, message, *references):
    selected = [reference for reference in references if isinstance(reference, str) and reference]
    return {
        "field": field, "message": message, "semantics": "management",
        "reusable_sources": selected or ["owner_confirmation", "workforce_document"],
        "allowed_precision": ["month", "day"] if field == "effective_date" else [],
    }


def require_occurred(value):
    from .business_queries import _today_china

    today = _today_china()
    if value[:7] > today[:7] or len(value) == 10 and value > today:
        raise NeedsInformation(
            [issue("effective_date", "仅登记已发生的用工事项，生效时间不能在未来")]
        )


def bounds(value):
    if len(value) == 10:
        return value, value
    year, month = map(int, value.split("-"))
    return value + "-01", value + "-" + str(calendar.monthrange(year, month)[1])


def records(connection, entity_id):
    from .entities import _profile_record

    return [
        _profile_record(row)
        for row in connection.execute(
            "SELECT * FROM entity_profile_revision WHERE entity_id=? ORDER BY revision",
            (entity_id,),
        )
    ]


def active_events(history):
    """Validate person-scoped corrections and preserve order-reference aliases."""
    active, aliases, events = {}, {}, {}
    for profile in history:
        raw = profile.get("workforce_event")
        if raw is None:
            continue
        try:
            event = WorkforceEvent.model_validate_json(canonical(raw)).model_dump(mode="json")
        except ValueError as exc:
            raise KernelError(
                "entity_profile_corrupt", "用工事件结构错误", profile_id=profile["id"]
            ) from exc
        ident = profile["id"]
        replaced, after = event["replaces_event_id"], event["after_event_id"]
        if replaced is not None:
            if replaced not in active:
                raise NeedsInformation(
                    [issue("replaces_event_id", "须引用本人员尚未被替代的用工事件", replaced)]
                )
            active.pop(replaced)
            aliases[replaced] = ident
        if after is not None and after not in events:
            raise NeedsInformation(
                [issue("after_event_id", "先后关联须引用本人员此前登记的用工事件", after)]
            )
        if replaced is not None and after == replaced:
            raise NeedsInformation(
                [issue("after_event_id", "替代事实不能同时发生在被替代事项之后", after)]
            )
        events[ident] = event
        active[ident] = event
    return active, aliases, events


def ordered_events(active, aliases):
    edges = {ident: set() for ident in active}
    ids = list(active)
    for pos, first in enumerate(ids):
        for second in ids[pos + 1 :]:
            low1, high1 = bounds(active[first]["effective_date"])
            low2, high2 = bounds(active[second]["effective_date"])
            if high1 < low2:
                edges[first].add(second)
            elif high2 < low1:
                edges[second].add(first)
    for ident, event in active.items():
        after = event["after_event_id"]
        while after in aliases:
            after = aliases[after]
        if after is not None:
            if after == ident or after not in active:
                raise NeedsInformation(
                    [issue("after_event_id", "用工先后关联形成自身引用或失效引用", ident, after)]
                )
            edges[after].add(ident)
    remaining, result = set(ids), []
    while remaining:
        ready = remaining - {target for origin in remaining for target in edges[origin]}
        if not ready:
            raise NeedsInformation(
                [issue("after_event_id", "业务日期与先后关联冲突", *sorted(remaining))]
            )
        if len(ready) > 1:
            raise NeedsInformation([issue(
                "after_event_id", "月份或同日精度无法确定事项先后，请补充有来源的先后关联",
                *sorted(ready),
            )])
        ident = ready.pop()
        result.append((ident, active[ident]))
        remaining.remove(ident)
    return result


def projection(history):
    active, aliases, events = active_events(history)
    state, segments = "unknown", []
    allowed = {
        # An explicit hire establishes employment and atomically ends earlier
        # labor: the confirmed relationship rule permits only one at a time.
        # This also allows an earlier labor fact to be backfilled before a hire.
        "hire": {"unknown", "departed", "labor_ended", "labor"},
        "start_labor": {"unknown", "departed", "labor_ended"},
        "unpaid_leave": {"regular"}, "resume": {"unpaid_leave"},
        "depart": {"regular", "unpaid_leave"}, "end_labor": {"labor"},
        "to_employee": {"labor"}, "to_labor": {"regular", "unpaid_leave"},
    }
    targets = {
        "hire": "regular", "start_labor": "labor", "unpaid_leave": "unpaid_leave",
        "resume": "regular", "depart": "departed", "end_labor": "labor_ended",
        "to_employee": "regular", "to_labor": "labor",
    }
    for ident, event in ordered_events(active, aliases):
        action, effective = event["action"], event["effective_date"]
        if state not in allowed[action]:
            raise NeedsInformation(
                [issue("action", "用工事项与生效时的关系不符，请补充或更正相关事实", ident)]
            )
        if segments:
            segments[-1]["end"] = effective
            segments[-1]["end_event_id"] = ident
        state = "unpaid_leave" if event["initial_unpaid_leave"] else targets[action]
        segments.append({
            "state": state, "start": effective, "end": None,
            "event_id": ident, "end_event_id": None,
        })
    retained = [profile for profile in history if profile["id"] in events]
    return {
        "employment_state": state, "segments": segments,
        "event_version": digest(
            [[item["id"], item.get("digest"), item["workforce_event"]] for item in retained]
        ).hex(),
        "event_references": retained,
    }


FORBIDDEN_PROFILE_FIELDS = {
    "employment_event", "employment_start", "employment_end", "employment_status",
    "employment_state", "employment_baseline", "employment_intervals",
}


def verify_history(history, *, retained_ids=frozenset()):
    """Every retained revision must contain metadata and at most one event."""
    from .entities import Entities, PersonProfile

    prefix = []
    derived = projection([])
    for profile in history:
        if profile["revision"] != len(prefix) + 1 or (
            prefix and profile["entity_id"] != prefix[0]["entity_id"]
        ):
            raise KernelError(
                "entity_profile_corrupt", "人员档案版本缺失或混入其他人员", profile_id=profile["id"]
            )
        prefix.append(profile)
        if profile["id"] in retained_ids:
            from .entity_profile_history import verify_retained_profile

            verify_retained_profile(profile)
            continue
        if FORBIDDEN_PROFILE_FIELDS.intersection(profile):
            raise KernelError(
                "entity_profile_corrupt", "人员档案含旧任职基线或派生状态，须使用新用工事件模型",
                profile_id=profile["id"],
            )
        try:
            PersonProfile.model_validate_json(canonical(Entities._content(profile)))
        except ValueError as exc:
            raise KernelError(
                "entity_profile_corrupt", "人员档案或用工事件结构错误", profile_id=profile["id"]
            ) from exc
        if profile.get("workforce_event") is None:
            continue
        if not isinstance(profile.get("source"), str) or not profile["source"].strip():
            raise KernelError(
                "entity_profile_corrupt", "用工事件缺少明确来源", profile_id=profile["id"]
            )
        try:
            derived = projection(prefix)
        except NeedsInformation as exc:
            raise KernelError(
                "entity_profile_corrupt", "已保存用工事件存在冲突", profile_id=profile["id"]
            ) from exc
    return derived


def recorded_event_times(connection, events):
    from .provenance import recorded_times

    references = [("display_profile", event["id"]) for event in events]
    times = recorded_times(connection, references)
    missing = next((ident for kind, ident in references if (kind, ident) not in times), None)
    if missing is not None:
        raise KernelError(
            "entity_profile_corrupt", "用工事件缺少唯一的登记审计时间", profile_id=missing
        )
    return times


def monthly_projection(timeline, period):
    """Confirmed occupancy and precision uncertainty for half-open segments."""
    period = str(YearMonth(period))
    if not timeline["segments"]:
        return {
            "period_states": ["unknown"], "possible_period_states": [],
            "period_end_state": "unknown", "employee_member": None, "labor_member": None,
            "fact_issues": [],
        }
    first, last = bounds(period)
    states, possible, issues = [], [], []
    end_state = "unknown"
    for segment in timeline["segments"]:
        low_start, high_start = bounds(segment["start"])
        if high_start <= last:
            end_state = segment["state"]
        if segment["end"] is None:
            low_end, high_end = "9999-12-31", "9999-12-31"
        else:
            low_end, high_end = bounds(segment["end"])
        # Exclusive end: a first-day transition gives the predecessor no
        # presence in that month.
        if low_start > last or high_end <= first or low_start >= high_end:
            continue
        if high_start < low_end and high_start <= last and low_end > first:
            if segment["state"] not in states:
                states.append(segment["state"])
        else:
            if segment["state"] not in possible:
                possible.append(segment["state"])
            issues.append(issue(
                "effective_date", "生效月份不足以确认该状态在所选月内是否实际持续",
                segment["event_id"], segment["end_event_id"],
            ))
    possible = [state for state in possible if state not in states]

    def membership(targets):
        if targets.intersection(states):
            return True
        return None if targets.intersection(possible) else False

    return {
        "period_states": states, "possible_period_states": possible,
        "period_end_state": end_state,
        "employee_member": membership({"regular", "unpaid_leave"}),
        "labor_member": membership({"labor"}), "fact_issues": issues,
    }


def labor_filter_matches(selected, in_period, period_states, end_state):
    """Apply the shared personnel controls to a month's labor membership."""
    if selected == "all":
        return True
    if selected in {"in_period", "employment_active"}:
        return in_period is True
    if selected in {"unknown", "employment_unknown"}:
        return in_period is None
    if selected == "ended":
        return in_period is False
    if selected == "employment_departed":
        return (
            in_period is False
            or "labor_ended" in period_states
            or in_period is True and end_state != "labor"
        )
    return False


def monthly_membership(timeline, period, relationship="employee"):
    if relationship not in {"employee", "labor"}:
        raise ValueError("relationship must be employee or labor")
    return monthly_projection(timeline, period)[relationship + "_member"]


def decode_cursor(cursor, scope):
    try:
        if not isinstance(cursor, str) or len(cursor) > 2048:
            raise ValueError("shape")
        value = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        if (
            set(value) != {"version", "scope", "offset"}
            or type(value["version"]) is not int or value["version"] != 1
        ):
            raise ValueError("shape")
        if value["scope"] != scope:
            raise KernelError(
                "workforce_history_cursor_stale", "人员或用工事件版本已变化，请重新读取历史首页"
            )
        if type(value["offset"]) is not int or value["offset"] < 0:
            raise ValueError("offset")
        return value["offset"]
    except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error) as exc:
        if isinstance(exc, KernelError):
            raise
        raise KernelError("workforce_history_cursor_invalid", "用工历史分页游标无效") from exc


def encode_cursor(scope, offset):
    return base64.urlsafe_b64encode(
        canonical({"version": 1, "scope": scope, "offset": offset}).encode()
    ).decode()
