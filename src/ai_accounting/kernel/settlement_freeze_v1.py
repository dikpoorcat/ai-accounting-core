"""Closed-period settlement state, committed by the private close storage root.

This is a rebuildable projection.  The publication chain remains authoritative.
The ordered directories prove which content-addressed leaf covers a key or page
range; a read verifies every directory reference and hydrates only selected
leaves and state revisions.  Complete integrity checks independently rebuild
the projection from publications.
"""

from __future__ import annotations

import bisect
import copy
import hashlib
import json
from dataclasses import dataclass

from .contracts import KernelError

_LEAF_SIZE = 32
_COUNTS = (
    "obligation_count",
    "unknown_count",
    "open_count",
    "open_unknown_count",
    "movement_count",
    "unresolved_movement_count",
    "source_event_count",
    "bad_source_count",
    "bad_paid_count",
    "bad_other_count",
)
_SUMS = ("remaining_sum", "open_sum", "source_amount_sum", "paid_sum", "other_sum")


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def checked(value):
    if type(value) is not int or not -(2**63) <= value <= 2**63 - 1:
        raise OverflowError("v1 amount exceeds signed 64-bit cents")
    return value


def _sha(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def _fail(reason: str, period: int) -> None:
    raise KernelError(
        "content_integrity_failed",
        "冻结清偿依据不匹配",
        component="settlement_freeze",
        record_id=str(period),
        reason=reason,
    )


def _empty_state(key: str) -> dict:
    return {
        "obligation_key": key,
        "first_source_period": None,
        "last_change_period": None,
        "source_event_count": 0,
        "source_amount": 0,
        "paid": 0,
        "other_settled": 0,
        "period_paid": 0,
        "period_other": 0,
        "bad_source": False,
        "bad_paid": False,
        "bad_other": False,
        "movement_count": 0,
        "unresolved_movement_count": 0,
        "latest_order": [-1, -1],
        "source_subject_id": None,
        "category": None,
        "account": None,
        "counterparty_id": None,
        "component": None,
        "source_calculation_id": None,
        "source_kind": None,
        "source_fact_id": None,
        "source_digest": None,
        "previous_revision": None,
        "events": [],
    }


def _remaining(state: dict) -> int | None:
    if state["bad_source"] or state["bad_paid"] or state["bad_other"]:
        return None
    return checked(state["source_amount"] - state["paid"] - state["other_settled"])


def _group_key(state: dict) -> tuple:
    return (
        state["first_source_period"],
        state["category"],
        state["account"],
        state["counterparty_id"],
        state["source_kind"],
    )


def _group_delta(state: dict, direction: int) -> dict:
    if not state["source_event_count"]:
        return {}
    remaining = _remaining(state)
    opened = remaining is None or remaining != 0
    return {
        "obligation_count": direction,
        "unknown_count": direction * (remaining is None),
        "open_count": direction * opened,
        "open_unknown_count": direction * (opened and remaining is None),
        "movement_count": direction * state["movement_count"],
        "unresolved_movement_count": direction * state["unresolved_movement_count"],
        "source_event_count": direction * state["source_event_count"],
        "bad_source_count": direction * bool(state["bad_source"]),
        "bad_paid_count": direction * bool(state["bad_paid"]),
        "bad_other_count": direction * bool(state["bad_other"]),
        "remaining_sum": direction * (remaining or 0),
        "open_sum": direction * (remaining or 0) if opened else 0,
        "source_amount_sum": direction * state["source_amount"],
        "paid_sum": direction * state["paid"],
        "other_sum": direction * state["other_settled"],
    }


def _add_group(groups: dict, state: dict, direction: int) -> None:
    delta = _group_delta(state, direction)
    if not delta:
        return
    key = _group_key(state)
    group = groups.setdefault(key, {field: 0 for field in (*_COUNTS, *_SUMS)})
    for field, value in delta.items():
        group[field] = checked(group[field] + value)
    if group["obligation_count"] == 0:
        if any(group.values()):
            raise ValueError("settlement group was not fully reversed")
        del groups[key]


def _groups_from_root(root: dict) -> dict:
    return {
        tuple(row[:5]): dict(zip((*_COUNTS, *_SUMS), row[5:], strict=True))
        for row in root["groups"]
    }


def _groups_for_root(groups: dict) -> list:
    return [
        [*key, *(values[field] for field in (*_COUNTS, *_SUMS))]
        for key, values in sorted(groups.items(), key=lambda item: canonical(item[0]))
    ]


def _change_period_counts(root: dict | None) -> dict[tuple[int, int], int]:
    if root is None:
        return {}
    return {(first, last): count for first, last, count in root["change_period_counts"]}


def _update_change_period_counts(counts: dict, state: dict, direction: int) -> None:
    if not state["source_event_count"]:
        return
    key = (state["first_source_period"], state["last_change_period"])
    count = counts.get(key, 0) + direction
    if count < 0:
        raise ValueError("settlement change-period count underflow")
    if count:
        counts[key] = count
    else:
        counts.pop(key, None)


def _apply(state: dict, row: dict, period: int) -> None:
    state["last_change_period"] = max(period, state["last_change_period"] or period)
    state["events"].append(
        [
            row["publication_id"],
            row["item_no"],
            row["source_calculation_id"],
            row["source_digest"].hex() if row["source_digest"] is not None else None,
        ]
    )
    if row["change_kind"] == "source":
        state["source_event_count"] += 1
        state["first_source_period"] = (
            min(period, state["first_source_period"])
            if state["first_source_period"] is not None
            else period
        )
        if row["amount"] is None:
            state["bad_source"] = True
        else:
            state["source_amount"] = checked(state["source_amount"] + row["amount"])
        if row["source_calculation_id"] == row["publication_calculation_id"]:
            order = [row["sequence"], row["item_no"]]
            if order > state["latest_order"]:
                state["latest_order"] = order
                for field in (
                    "source_subject_id",
                    "category",
                    "account",
                    "counterparty_id",
                    "component",
                    "source_calculation_id",
                    "source_kind",
                    "source_fact_id",
                ):
                    state[field] = row[field]
                state["source_digest"] = (
                    row["source_digest"].hex() if row["source_digest"] is not None else None
                )
        return
    state["movement_count"] += 1
    if row["state"] == "unresolved":
        state["unresolved_movement_count"] += 1
        state["bad_paid" if row["change_kind"] == "payment" else "bad_other"] = True
        return
    field = "paid" if row["change_kind"] == "payment" else "other_settled"
    state[field] = checked(state[field] + row["amount"])
    if row["change_kind"] == "payment":
        state["period_paid"] = checked(state["period_paid"] + row["amount"])
    else:
        state["period_other"] = checked(state["period_other"] + row["amount"])


def _header(items: list[list], ordinal: int) -> list:
    encoded = canonical(items)
    return [ordinal, items[0][0], items[-1][0], len(items), _sha(encoded).hex()]


def _pack(entries: list[list]) -> tuple[list[list], dict[bytes, str]]:
    headers = []
    payloads = {}
    for ordinal, begin in enumerate(range(0, len(entries), _LEAF_SIZE)):
        items = entries[begin : begin + _LEAF_SIZE]
        encoded = canonical(items)
        block_hash = _sha(encoded)
        headers.append(_header(items, ordinal))
        payloads[block_hash] = encoded
    return headers, payloads


def _updated_directory(
    connection,
    previous_period: int | None,
    previous: list[list],
    changes: dict[str, list | None],
    *,
    load_block=None,
) -> tuple[list[list], dict[bytes, str]]:
    """Copy only leaves whose key references changed; retain other block bytes."""
    if not previous:
        return _pack(sorted([key, *value] for key, value in changes.items() if value is not None))
    last_keys = [header[2] for header in previous]
    buckets = {}
    for key, value in changes.items():
        index = min(bisect.bisect_left(last_keys, key), len(previous) - 1)
        buckets.setdefault(index, {})[key] = value
    headers = []
    payloads = {}
    for index, header in enumerate(previous):
        updates = buckets.get(index)
        if not updates:
            headers.append(header[1:])
            continue
        items = (
            load_block(header)
            if load_block is not None
            else _read_block(connection, previous_period, header)
        )
        values = {item[0]: item[1:] for item in items}
        for key, value in updates.items():
            if value is None:
                values.pop(key, None)
            else:
                values[key] = value
        ordered = sorted([key, *value] for key, value in values.items())
        for begin in range(0, len(ordered), _LEAF_SIZE):
            items = ordered[begin : begin + _LEAF_SIZE]
            encoded = canonical(items)
            block_hash = _sha(encoded)
            headers.append([items[0][0], items[-1][0], len(items), block_hash.hex()])
            payloads[block_hash] = encoded
    return [[ordinal, *entry] for ordinal, entry in enumerate(headers)], payloads


@dataclass
class PreparedFreeze:
    period: int
    close_digest: bytes
    root_json: str
    root_digest: bytes
    blocks: dict[bytes, str]
    revisions: dict[bytes, tuple[str, str]]


@dataclass
class FrozenScope:
    cutoff: int
    through: int
    base_period: int
    base_root: dict
    groups: dict
    period_amounts: dict[str, tuple[int, int]]
    overrides: dict[str, dict]
    current: bool


def _read_root(connection, period: int) -> dict:
    from .content_history_context import close_reader

    reader = close_reader()
    close = connection.execute(
        "SELECT period,manifest,digest FROM period_close WHERE period=?", (period,)
    ).fetchone()
    if close is None:
        _fail("close_missing", period)
    header = reader.verified_header(connection, close)
    expected = reader.derived_root(header, "settlement")
    row = connection.execute(
        "SELECT close_digest,root_json,root_digest FROM settlement_freeze_root WHERE period=?",
        (period,),
    ).fetchone()
    if expected is None or row is None:
        _fail("freeze_root_missing", period)
    if (
        bytes(row["close_digest"]) != bytes(close["digest"])
        or bytes(row["root_digest"]) != expected
        or _sha(row["root_json"]) != expected
    ):
        _fail("freeze_root_digest_mismatch", period)
    try:
        root = json.loads(row["root_json"])
        if (
            root["format"] != "settlement-freeze/2"
            or not isinstance(root["subject_directory"], list)
            or root["period"] != period
            or root["close_digest"] != bytes(close["digest"]).hex()
            or set(root["directories"]) != {"all", "open"}
        ):
            _fail("freeze_root_identity_mismatch", period)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        _fail("freeze_root_contract_invalid", period)
    root["root_digest"] = expected.hex()
    _validate_directory(root["subject_directory"], period)
    for kind in ("all", "open"):
        entries = root["directories"][kind]
        if (
            not isinstance(entries, list)
            or [item[0] for item in entries] != list(range(len(entries)))
            or any(item[1] > item[2] or item[3] <= 0 for item in entries)
            or any(left[2] >= right[1] for left, right in zip(entries, entries[1:], strict=False))
        ):
            _fail("freeze_directory_invalid", period)
        actual = [
            [row[0], row[1], row[2], row[3], bytes(row[4]).hex(), row[5]]
            for row in connection.execute(
                "SELECT r.ordinal,r.first_key,r.last_key,r.row_count,r.block_digest,"
                "b.digest FROM settlement_freeze_ref r "
                "LEFT JOIN settlement_freeze_block b ON b.digest=r.block_digest "
                "WHERE r.period=? AND r.kind=? ORDER BY r.ordinal",
                (period, kind),
            )
        ]
        if len(actual) != len(entries) or any(
            a[:5] != e or a[5] is None for a, e in zip(actual, entries, strict=True)
        ):
            _fail("freeze_directory_incomplete", period)
    return root


def _validate_directory(entries: list, period: int) -> None:
    if (
        not isinstance(entries, list)
        or any(
            not isinstance(item, list)
            or len(item) != 5
            or item[0] != index
            or not isinstance(item[1], str)
            or not isinstance(item[2], str)
            or item[1] > item[2]
            or type(item[3]) is not int
            or item[3] <= 0
            or not isinstance(item[4], str)
            or len(item[4]) != 64
            for index, item in enumerate(entries)
        )
        or any(left[2] >= right[1] for left, right in zip(entries, entries[1:], strict=False))
    ):
        _fail("freeze_subject_directory_invalid", period)


def _read_subject_leaf(connection, period: int, header: list) -> list[list]:
    digest = bytes.fromhex(header[4])
    row = connection.execute(
        "SELECT payload FROM settlement_freeze_block WHERE digest=?", (digest,)
    ).fetchone()
    if row is None or _sha(row[0]) != digest:
        _fail("freeze_subject_block_digest_mismatch", period)
    try:
        items = json.loads(row[0])
        ids = [item[0] for item in items]
        if (
            len(items) != header[3]
            or not ids
            or ids != sorted(set(ids))
            or ids[0] != header[1]
            or ids[-1] != header[2]
            or any(
                len(item) != 3
                or not isinstance(item[0], str)
                or not isinstance(item[1], list)
                or not isinstance(item[2], list)
                or any(
                    not isinstance(pointer, list)
                    or len(pointer) != 3
                    or type(pointer[0]) is not int
                    or pointer[0] > period
                    or not isinstance(pointer[1], str)
                    or len(pointer[1]) != 64
                    or type(pointer[2]) is not int
                    or pointer[2] <= 0
                    for pointer in (*item[1], *item[2])
                )
                or any(
                    left[0] >= right[0]
                    for chunks in item[1:]
                    for left, right in zip(chunks, chunks[1:], strict=False)
                )
                for item in items
            )
        ):
            _fail("freeze_subject_block_invalid", period)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        _fail("freeze_subject_block_invalid", period)
    return items


def _subject_item(
    connection, period: int, directory: list, subject: str, *, load_block=None
) -> list | None:
    index = bisect.bisect_left(directory, subject, key=lambda header: header[2])
    if index >= len(directory) or directory[index][1] > subject:
        return None
    header = directory[index]
    items = (
        load_block(header)
        if load_block is not None
        else _read_subject_leaf(connection, period, header)
    )
    offset = bisect.bisect_left(items, subject, key=lambda item: item[0])
    return items[offset] if offset < len(items) and items[offset][0] == subject else None


def _read_block(connection, period: int, header: list) -> list[list]:
    expected = bytes.fromhex(header[4])
    row = connection.execute(
        "SELECT payload FROM settlement_freeze_block WHERE digest=?", (expected,)
    ).fetchone()
    if row is None or _sha(row[0]) != expected:
        _fail("freeze_block_digest_mismatch", period)
    try:
        items = json.loads(row[0])
        keys = [item[0] for item in items]
        if (
            len(items) != header[3]
            or len(keys) != len(set(keys))
            or keys != sorted(keys)
            or keys[0] != header[1]
            or keys[-1] != header[2]
            or any(
                len(item) != 3 or (item[2] is not None and type(item[2]) is not int)
                for item in items
            )
        ):
            _fail("freeze_block_range_invalid", period)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        _fail("freeze_block_contract_invalid", period)
    return items


def _read_state(connection, period: int, key: str, digest_hex: str) -> dict:
    expected = bytes.fromhex(digest_hex)
    row = connection.execute(
        "SELECT obligation_key,payload FROM settlement_state_revision WHERE digest=?",
        (expected,),
    ).fetchone()
    if row is None or row["obligation_key"] != key or _sha(row["payload"]) != expected:
        _fail("freeze_state_digest_mismatch", period)
    try:
        state = json.loads(row["payload"])
        if state["obligation_key"] != key:
            _fail("freeze_state_key_mismatch", period)
        return state
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        _fail("freeze_state_contract_invalid", period)


def _lookup(
    connection, period: int, directory: list, key: str, *, load_block=None
) -> tuple[str | None, list | None]:
    index = bisect.bisect_left([header[2] for header in directory], key)
    if index >= len(directory) or directory[index][1] > key:
        return None, None
    header = directory[index]
    items = (
        load_block(header) if load_block is not None else _read_block(connection, period, header)
    )
    found = bisect.bisect_left([item[0] for item in items], key)
    if found < len(items) and items[found][0] == key:
        return items[found][1], header
    return None, header


def _entries(connection, period: int, directory: list) -> list[list]:
    return [item for header in directory for item in _read_block(connection, period, header)]


def _close_before(connection, period: int) -> int | None:
    row = connection.execute(
        "SELECT max(period) FROM period_close WHERE period<?", (period,)
    ).fetchone()
    return row[0]


def _build_prepared(
    connection,
    period: int,
    logical_close_digest: bytes,
    publication_highwater: int,
    previous: dict | None,
    by_key: dict[str, list[dict]],
    period_rows: list[dict],
    *,
    load_block=None,
    load_state=None,
    load_subject=None,
) -> PreparedFreeze:
    previous_period = previous["period"] if previous else None
    old_directory = previous["directories"]["all"] if previous else []
    old_open = previous["directories"]["open"] if previous else []
    groups = _groups_from_root(previous) if previous else {}
    change_period_counts = _change_period_counts(previous)
    revisions = {}
    replacement = {}
    open_replacement = {}
    period_amounts = []
    for key, rows in by_key.items():
        old_digest, _ = (
            _lookup(connection, previous_period, old_directory, key, load_block=load_block)
            if previous
            else (None, None)
        )
        old_state = (
            (
                load_state(key, old_digest)
                if load_state is not None
                else _read_state(connection, previous_period, key, old_digest)
            )
            if old_digest
            else None
        )
        if old_state is not None:
            _add_group(groups, old_state, -1)
            _update_change_period_counts(change_period_counts, old_state, -1)
        state = copy.deepcopy(old_state) if old_state else _empty_state(key)
        state["period_paid"] = state["period_other"] = 0
        state["previous_revision"] = old_digest
        state["events"] = []
        for row in rows:
            _apply(state, row, period)
        if state["period_paid"] or state["period_other"]:
            period_amounts.append([key, state["period_paid"], state["period_other"]])
        _add_group(groups, state, 1)
        _update_change_period_counts(change_period_counts, state, 1)
        payload = canonical(state)
        revision_digest = _sha(payload)
        revisions[revision_digest] = (key, payload)
        replacement[key] = [revision_digest.hex(), state["first_source_period"]]
        if state["source_event_count"] and _remaining(state) != 0:
            open_replacement[key] = [revision_digest.hex(), state["first_source_period"]]
        else:
            open_replacement[key] = None
    all_headers, blocks = _updated_directory(
        connection, previous_period, old_directory, replacement, load_block=load_block
    )
    open_headers, open_blocks = _updated_directory(
        connection, previous_period, old_open, open_replacement, load_block=load_block
    )
    blocks.update(open_blocks)
    old_subjects = previous["subject_directory"] if previous else []
    source_by_subject: dict[str, set[str]] = {}
    events_by_subject: dict[str, list[list]] = {}
    for row in period_rows:
        subject = row["source_subject_id"]
        if subject is None:
            continue
        if row["change_kind"] == "source" and row["obligation_key"] is not None:
            source_by_subject.setdefault(subject, set()).add(row["obligation_key"])
        events_by_subject.setdefault(subject, []).append(
            [
                row["publication_id"],
                row["item_no"],
                row["obligation_key"],
                row["change_kind"],
                row["state"],
            ]
        )
    subject_replacement = {}
    for subject, events in events_by_subject.items():
        previous_item = (
            _subject_item(
                connection,
                previous_period,
                old_subjects,
                subject,
                load_block=load_subject,
            )
            if previous
            else None
        )
        sources = list(previous_item[1]) if previous_item else []
        direct = list(previous_item[2]) if previous_item else []
        keys = sorted(source_by_subject.get(subject, ()))
        if keys:
            payload = canonical(keys)
            digest = _sha(payload)
            blocks[digest] = payload
            sources.append([period, digest.hex(), len(keys)])
        payload = canonical(events)
        digest = _sha(payload)
        blocks[digest] = payload
        direct.append([period, digest.hex(), len(events)])
        subject_replacement[subject] = [sources, direct]
    subject_headers, subject_blocks = _updated_directory(
        connection,
        previous_period,
        old_subjects,
        subject_replacement,
        load_block=load_subject
        or (lambda header: _read_subject_leaf(connection, previous_period, header)),
    )
    blocks.update(subject_blocks)
    root = {
        "format": "settlement-freeze/2",
        "period": period,
        "close_digest": bytes(logical_close_digest).hex(),
        "publication_highwater": publication_highwater,
        "previous_root": previous["root_digest"] if previous else None,
        "directories": {"all": all_headers, "open": open_headers},
        "subject_directory": subject_headers,
        "period_amounts": sorted(period_amounts),
        "groups": _groups_for_root(groups),
        "change_period_counts": [
            [*key, count] for key, count in sorted(change_period_counts.items())
        ],
    }
    root_json = canonical(root)
    return PreparedFreeze(
        period, bytes(logical_close_digest), root_json, _sha(root_json), blocks, revisions
    )


def _authoritative_freezes(
    engine, connection, *, verified_calculations=None, _verified_projection=None
):
    """Build expected immutable roots from outcomes and the publication chain.

    This path never reads stored frozen state, blocks, or directories. It is
    deliberately reserved for complete integrity checks and explicit repair.
    """
    from .settlement_projection_v1 import (
        _COLUMNS,
        _VerifiedProjection,
        compare_settlement_projection,
    )

    if _verified_projection is None:
        compared = compare_settlement_projection(
            engine, connection, verified_calculations=verified_calculations
        )
        if compared["changed"]:
            _fail("authoritative_projection_mismatch", -1)
        expected = compared["expected"]
    else:
        from .verified_source_lease import require_verified_lease

        if (
            not isinstance(_verified_projection, _VerifiedProjection)
            or _verified_projection.connection is not connection
            or not connection.in_transaction
        ):
            raise ValueError("v1 settlement source verification belongs to another snapshot")
        require_verified_lease(connection, _verified_projection.lease)
        expected = _verified_projection.expected
    publications = {
        row["id"]: row
        for row in connection.execute(
            "SELECT id,sequence,calculation_id,posting_period FROM calculation_publication"
        )
    }
    calculations = {
        row["id"]: row for row in connection.execute("SELECT id,kind,fact_id FROM calculation")
    }
    by_period = {}
    for values in expected:
        row = dict(zip(_COLUMNS, values, strict=True))
        publication = publications[row["publication_id"]]
        calculation = calculations.get(row["source_calculation_id"])
        row.update(
            {
                "sequence": publication["sequence"],
                "publication_calculation_id": publication["calculation_id"],
                "source_kind": calculation["kind"] if calculation else None,
                "source_fact_id": calculation["fact_id"] if calculation else None,
            }
        )
        by_period.setdefault(row["posting_period"], []).append(row)
    blocks = {}
    revisions = {}
    prepared = []
    previous = None

    def load_block(header):
        return json.loads(blocks[bytes.fromhex(header[4])])

    def load_state(key, digest_hex):
        actual_key, payload = revisions[bytes.fromhex(digest_hex)]
        if actual_key != key:
            raise ValueError("recomputed settlement state key differs")
        return json.loads(payload)

    closes = connection.execute(
        "SELECT period,manifest,digest FROM period_close ORDER BY period"
    ).fetchall()
    for close in closes:
        period = close["period"]
        from .content_history_context import close_reader

        reader = close_reader()
        header = reader.verified_header(connection, close)
        highwater = header.root["small"]["publication_sequence"]
        period_rows = by_period.pop(period, [])
        period_rows.sort(key=lambda row: (row["sequence"], row["item_no"]))
        rows = {}
        for item in period_rows:
            if item["obligation_key"] is not None:
                rows.setdefault(item["obligation_key"], []).append(item)
        new = _build_prepared(
            connection,
            period,
            bytes(close["digest"]),
            highwater,
            previous,
            rows,
            period_rows,
            load_block=load_block,
            load_state=load_state,
            load_subject=load_block,
        )
        if reader.derived_root(header, "settlement") != new.root_digest:
            _fail("authoritative_root_mismatch", period)
        prepared.append(new)
        blocks.update(new.blocks)
        revisions.update(new.revisions)
        previous = json.loads(new.root_json)
        previous["root_digest"] = new.root_digest.hex()
    if closes and any(period <= closes[-1]["period"] for period in by_period):
        _fail("unfrozen_posting_period", closes[-1]["period"])
    return prepared, blocks, revisions


def _frozen_difference(connection, prepared, blocks, revisions) -> bool:
    expected_roots = {
        item.period: (item.close_digest, item.root_json, item.root_digest) for item in prepared
    }
    actual_roots = {
        row[0]: (bytes(row[1]), row[2], bytes(row[3]))
        for row in connection.execute(
            "SELECT period,close_digest,root_json,root_digest FROM settlement_freeze_root"
        )
    }
    if actual_roots != expected_roots:
        return True
    expected_refs = {
        (item.period, kind, ordinal): (first, last, count, bytes.fromhex(digest_hex))
        for item in prepared
        for kind, directory in json.loads(item.root_json)["directories"].items()
        for ordinal, first, last, count, digest_hex in directory
    }
    actual_refs = {
        (row[0], row[1], row[2]): (row[3], row[4], row[5], bytes(row[6]))
        for row in connection.execute(
            "SELECT period,kind,ordinal,first_key,last_key,row_count,block_digest "
            "FROM settlement_freeze_ref"
        )
    }
    if actual_refs != expected_refs:
        return True
    actual_blocks = {
        bytes(row[0]): row[1]
        for row in connection.execute("SELECT digest,payload FROM settlement_freeze_block")
    }
    if actual_blocks != blocks:
        return True
    actual_revisions = {
        bytes(row[0]): (row[1], row[2])
        for row in connection.execute(
            "SELECT digest,obligation_key,payload FROM settlement_state_revision"
        )
    }
    return actual_revisions != revisions


def require_frozen_settlement_projection(
    engine, connection, *, verified_calculations=None, _verified_projection=None
) -> dict:
    """Independently compare all frozen roots and bytes to authoritative outcomes."""
    prepared, blocks, revisions = _authoritative_freezes(
        engine,
        connection,
        verified_calculations=verified_calculations,
        _verified_projection=_verified_projection,
    )
    if _frozen_difference(connection, prepared, blocks, revisions):
        _fail("frozen_projection_mismatch", -1)
    return {"periods": len(prepared), "blocks": len(blocks), "revisions": len(revisions)}


def frozen_position_rows(connection, period: str, accounts: set[str]) -> list[dict] | None:
    """Classify a sealed v1 month from its authenticated frozen group root."""
    from .content_v1 import _V1YearMonth

    cutoff = _V1YearMonth(period).ordinal
    if (
        connection.execute("SELECT 1 FROM period_close WHERE period=?", (cutoff,)).fetchone()
        is None
    ):
        return None
    groups = _groups_from_root(_read_root(connection, cutoff))
    positions = {}
    for key, group in groups.items():
        effective_period, category, account, counterparty_id, _ = key
        if effective_period is None or effective_period > cutoff or account not in accounts:
            continue
        item = positions.setdefault(
            (account, category, counterparty_id),
            {"remaining": 0, "unknown": False, "known_count": 0},
        )
        item["remaining"] = checked(item["remaining"] + group["remaining_sum"])
        item["unknown"] = item["unknown"] or bool(group["unknown_count"])
        item["known_count"] += group["obligation_count"] - group["unknown_count"]
    return [
        {
            "account": key[0],
            "category": key[1],
            "counterparty_id": key[2],
            "remaining": value["remaining"] if value["known_count"] else None,
            "unknown": value["unknown"],
        }
        for key, value in positions.items()
    ]
