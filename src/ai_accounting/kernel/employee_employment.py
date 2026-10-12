"""Typed personnel events and deterministic, source-preserving management state.

The immutable profile revision is also the event identity. Date bounds below are
used only to compare precision; no synthetic calendar day is stored as a fact.
"""

from __future__ import annotations

import base64
import binascii
import calendar
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema

from .content_history_context import source_canonical as canonical
from .contracts import KernelError, NeedsInformation
from .types import ActualDate, YearMonth

EmploymentDate = Annotated[
    YearMonth | ActualDate,
    WithJsonSchema(
        {
            "type": "string",
            "anyOf": [
                {"pattern": r"^[0-9]{4}-(0[1-9]|1[0-2])$"},
                {"pattern": r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"},
            ],
            "x-accounting-fact": {
                "role": "management",
                "meaning": "employment_business_effective_date",
                "allowed_precision": ["month", "day"],
                "reusable_sources": ["owner_confirmation", "employment_document"],
            },
        }
    ),
]
ProfileReference = Annotated[str, Field(min_length=1, max_length=200)]


class EmploymentEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["hire", "unpaid_leave", "resume", "depart"]
    effective_date: EmploymentDate
    after_profile_id: ProfileReference | None = None
    replaces_profile_id: ProfileReference | None = None


class EmploymentChange(EmploymentEvent):
    """A complete replacement fact when replaces_profile_id is supplied."""


class EmploymentInterval(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    start: EmploymentDate | None
    end: EmploymentDate | None


def issue(field, message, *references):
    selected = [reference for reference in references if isinstance(reference, str) and reference]
    return {
        "field": field,
        "message": message,
        "semantics": "management",
        "reusable_sources": selected or ["owner_confirmation", "employment_document"],
        "allowed_precision": ["month", "day"]
        if field in {"effective_date", "employment_start", "employment_end"}
        else [],
    }


def require_occurred(value):
    from .business_queries import _today_china

    today = _today_china()
    if value[:7] > today[:7] or len(value) == 10 and value > today:
        raise NeedsInformation(
            [issue("effective_date", "仅登记已发生的任职事项，生效时间不能在未来")]
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
    """Validate correction references and retain aliases for existing order edges."""
    active, aliases, events = {}, {}, {}
    baseline, baseline_seen = None, False
    for profile in history:
        if profile.get("employment_intervals") is not None:
            selected = profile.get("employment_baseline")
            if baseline_seen and selected != baseline:
                raise KernelError(
                    "entity_profile_corrupt", "任职管理基线被改写", profile_id=profile["id"]
                )
            baseline = selected
            baseline_seen = True
        raw = profile.get("employment_event")
        if raw is None:
            continue
        try:
            event = EmploymentEvent.model_validate_json(canonical(raw)).model_dump(mode="json")
        except ValueError as exc:
            raise KernelError(
                "entity_profile_corrupt", "任职事件结构错误", profile_id=profile["id"]
            ) from exc
        ident = profile["id"]
        replaced = event["replaces_profile_id"]
        if replaced is not None:
            if replaced not in active:
                raise NeedsInformation(
                    [
                        issue(
                            "replaces_profile_id",
                            "被更正记录须为本人员尚未被替代的任职事件",
                            replaced,
                        )
                    ]
                )
            active.pop(replaced)
            aliases[replaced] = ident
        after = event["after_profile_id"]
        if after is not None and after not in events and after != baseline:
            raise NeedsInformation(
                [issue("after_profile_id", "先后关联须引用本人员此前的任职事件或原管理基线", after)]
            )
        if replaced is not None and after == replaced:
            raise NeedsInformation(
                [issue("after_profile_id", "替代事实不能同时声明发生在被替代事项之后", after)]
            )
        events[ident] = event
        active[ident] = event
    return active, aliases, events, baseline


def ordered_events(active, aliases, baseline):
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
        after = event["after_profile_id"]
        while after in aliases:
            after = aliases[after]
        if after is not None and after != baseline:
            if after == ident or after not in active:
                raise NeedsInformation(
                    [issue("after_profile_id", "任职先后关联形成自身引用或失效引用", ident, after)]
                )
            edges[after].add(ident)
    remaining, result = set(ids), []
    while remaining:
        ready = remaining - {target for origin in remaining for target in edges[origin]}
        if not ready:
            raise NeedsInformation(
                [
                    issue(
                        "after_profile_id",
                        "业务日期与先后关联冲突，任职记录无法排序",
                        *sorted(remaining),
                    )
                ]
            )
        if len(ready) > 1:
            raise NeedsInformation(
                [
                    issue(
                        "after_profile_id",
                        "月份或同日精度无法确定事项先后，请补充有来源的先后关联",
                        *sorted(ready),
                    )
                ]
            )
        ident = ready.pop()
        result.append((ident, active[ident]))
        remaining.remove(ident)
    return result


def projection(history):
    active, aliases, events, baseline_id = active_events(history)
    if not events:
        return None
    by_id = {profile["id"]: profile for profile in history}
    first = next(profile for profile in history if profile.get("employment_event") is not None)
    baseline = by_id.get(baseline_id, {})
    if baseline_id is not None and (
        not baseline
        or baseline["revision"] >= first["revision"]
        or baseline.get("employment_intervals") is not None
    ):
        raise KernelError(
            "entity_profile_corrupt",
            "任职基线必须是首次任职事件之前的原档案",
            profile_id=baseline_id,
        )
    sequence = ordered_events(active, aliases, baseline_id)
    state = baseline.get("employment_state", "unknown")
    start, end = baseline.get("employment_start"), baseline.get("employment_end")
    if state == "unknown" and (baseline.get("employment_status") == "inactive" or end is not None):
        # An explicit old inactive interval remains a closed management basis;
        # this only constrains the transition, it does not rewrite old state.
        state = "departed"
    intervals = []
    if start is not None or end is not None or state in {"regular", "unpaid_leave"}:
        intervals.append({"start": start, "end": end})
    for position, (ident, event) in enumerate(sequence):
        action, effective = event["action"], event["effective_date"]
        if action == "hire":
            # An old explicit special status is an authenticated baseline, not
            # evidence that a backfilled hire has reversed that status.
            establish = position == 0 and state in {"unknown", "regular"}
            if establish and start is not None and effective != start:
                raise NeedsInformation(
                    [
                        issue(
                            "effective_date", "补录入职时间与原档案入职资料冲突", ident, baseline_id
                        )
                    ]
                )
            if not establish and state != "departed":
                raise NeedsInformation(
                    [issue("action", "该入职记录与已有在职或停薪状态冲突", ident, baseline_id)]
                )
            if state == "departed" and position == 0:
                if end is None:
                    raise NeedsInformation(
                        [
                            issue(
                                "employment_end",
                                "原离职基线缺少结束时间，请先用离职事项补录其日期或月份再登记再入职",
                                baseline_id,
                            )
                        ]
                    )
                if bounds(end)[1] >= bounds(effective)[0]:
                    if event["after_profile_id"] != baseline_id:
                        raise NeedsInformation(
                            [
                                issue(
                                    "after_profile_id",
                                    "再入职须明确发生在原离职基线之后",
                                    ident,
                                    baseline_id,
                                )
                            ]
                        )
                    if end is not None and bounds(effective)[1] < bounds(end)[0]:
                        raise NeedsInformation(
                            [issue("effective_date", "再入职早于原离职时间", ident, baseline_id)]
                        )
            if establish:
                intervals = []
            start, end, state = effective, None, "regular"
            intervals.append({"start": start, "end": None})
        else:
            allowed = {
                "unpaid_leave": {"regular"},
                "resume": {"unpaid_leave"},
                "depart": {"regular", "unpaid_leave"},
            }[action]
            complete_departure = (
                position == 0
                and baseline_id is not None
                and action == "depart"
                and state == "departed"
                and end is None
            )
            if state not in allowed and not complete_departure:
                raise NeedsInformation(
                    [
                        issue(
                            "action",
                            "任职事项与生效时的状态不符，须补充或更正相关任职事实",
                            ident,
                            baseline_id,
                        )
                    ]
                )
            if position == 0 and baseline_id is not None:
                anchor = end or start
                if anchor is not None and bounds(effective)[1] < bounds(anchor)[0]:
                    raise NeedsInformation(
                        [
                            issue(
                                "effective_date",
                                "事项生效时间早于原管理基线的明确任职日期",
                                ident,
                                baseline_id,
                            )
                        ]
                    )
                if (
                    anchor is not None
                    and bounds(effective)[0] <= bounds(anchor)[1]
                    and event["after_profile_id"] != baseline_id
                ):
                    raise NeedsInformation(
                        [
                            issue(
                                "after_profile_id",
                                "月份或同日精度无法确定事项与原任职基线的先后",
                                ident,
                                baseline_id,
                            )
                        ]
                    )
            state = {"unpaid_leave": "unpaid_leave", "resume": "regular", "depart": "departed"}[
                action
            ]
            if action == "depart":
                end = effective
                if intervals:
                    intervals[-1]["end"] = effective
                else:
                    intervals.append({"start": None, "end": effective})
    return {
        "employment_start": start,
        "employment_end": end,
        "employment_status": "inactive" if state == "departed" else "active",
        "employment_state": state,
        "employment_baseline": baseline_id,
        "employment_intervals": intervals,
    }


PROJECTION_FIELDS = (
    "employment_start",
    "employment_end",
    "employment_status",
    "employment_state",
    "employment_baseline",
    "employment_intervals",
)


def verify_history(history):
    """Check every retained management version, including superseded events."""
    prefix, derived = [], None
    for profile in history:
        prefix.append(profile)
        try:
            if profile.get("employment_event") is not None:
                if not isinstance(profile.get("source"), str) or not profile["source"].strip():
                    raise KernelError(
                        "entity_profile_corrupt", "任职事件缺少明确来源", profile_id=profile["id"]
                    )
                derived = projection(prefix)
            if derived is not None and any(
                profile.get(key) != derived[key] for key in PROJECTION_FIELDS
            ):
                raise KernelError(
                    "entity_profile_corrupt",
                    "任职读取模型与事件历史不一致",
                    profile_id=profile["id"],
                )
            if derived is None and profile.get("employment_intervals") is not None:
                raise KernelError(
                    "entity_profile_corrupt", "任职读取模型缺少事件依据", profile_id=profile["id"]
                )
        except NeedsInformation as exc:
            raise KernelError(
                "entity_profile_corrupt", "已保存任职事件存在冲突", profile_id=profile["id"]
            ) from exc
    return derived


def recorded_event_times(connection, events):
    from .provenance import recorded_times

    references = [("display_profile", event["id"]) for event in events]
    times = recorded_times(connection, references)
    missing = next((ident for kind, ident in references if (kind, ident) not in times), None)
    if missing is not None:
        raise KernelError(
            "entity_profile_corrupt", "任职事件缺少唯一的登记审计时间", profile_id=missing
        )
    return times


def period_membership(intervals, period):
    """Monthly roster includes any employment in the month, also unpaid leave."""
    if any(
        (item["start"] is None or item["start"][:7] <= period)
        and (item["end"] is None or item["end"][:7] >= period)
        for item in intervals
    ):
        # Unknown start is retained as unknown rather than inventing employment.
        if any(
            item["start"] is not None
            and item["start"][:7] <= period
            and (item["end"] is None or item["end"][:7] >= period)
            for item in intervals
        ):
            return "in_period", True
        return "unknown", None
    if intervals and all(
        item["start"] is not None and item["start"][:7] > period for item in intervals
    ):
        return "not_started", False
    return "ended", False


def decode_cursor(cursor, scope):
    try:
        if not isinstance(cursor, str) or len(cursor) > 2048:
            raise ValueError("shape")
        value = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        if (
            set(value) != {"version", "scope", "offset"}
            or type(value["version"]) is not int
            or value["version"] != 1
        ):
            raise ValueError("shape")
        if value["scope"] != scope:
            raise KernelError(
                "employment_history_cursor_stale", "人员或档案版本已变化，请重新读取任职历史首页"
            )
        if type(value["offset"]) is not int or value["offset"] < 0:
            raise ValueError("offset")
        return value["offset"]
    except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error) as exc:
        if isinstance(exc, KernelError):
            raise
        raise KernelError("employment_history_cursor_invalid", "任职历史分页游标无效") from exc


def encode_cursor(scope, offset):
    return base64.urlsafe_b64encode(
        canonical({"version": 1, "scope": scope, "offset": offset}).encode()
    ).decode()
