"""Closed-period settlement state, committed by the private close storage root.

This is a rebuildable projection.  The publication chain remains authoritative.
The authenticated root directories prove which content-addressed leaf covers a
key or page range; ordinary reads hydrate and verify only selected leaves and
state revisions. Complete integrity checks independently rebuild the projection
from publications and compare every directory mirror and saved byte.
"""

from __future__ import annotations

import bisect
import copy
import hashlib
import json
from dataclasses import dataclass
from dataclasses import field as dataclass_field

from .close_storage import derived_root, verified_header
from .contracts import KernelError
from .types import YearMonth, canonical, checked, is_sha256_hex

DDL = """
CREATE TABLE settlement_freeze_root(
 period INTEGER PRIMARY KEY REFERENCES period_close(period),
 close_digest BLOB NOT NULL CHECK(length(close_digest)=32),
 root_json TEXT NOT NULL CHECK(json_valid(root_json)),
 root_digest BLOB NOT NULL CHECK(length(root_digest)=32)) STRICT;
CREATE TABLE settlement_freeze_block(
 digest BLOB PRIMARY KEY CHECK(length(digest)=32),
 payload TEXT NOT NULL CHECK(json_valid(payload))) STRICT;
CREATE TABLE settlement_freeze_ref(
 period INTEGER NOT NULL REFERENCES settlement_freeze_root(period),
 kind TEXT NOT NULL CHECK(kind IN ('all','open')),
 ordinal INTEGER NOT NULL CHECK(ordinal>=0),
 first_key TEXT NOT NULL,
 last_key TEXT NOT NULL,
 row_count INTEGER NOT NULL CHECK(row_count>0),
 block_digest BLOB NOT NULL REFERENCES settlement_freeze_block(digest),
 PRIMARY KEY(period,kind,ordinal)) STRICT;
CREATE INDEX settlement_freeze_ref_range
 ON settlement_freeze_ref(period,kind,last_key,first_key);
CREATE TABLE settlement_state_revision(
 digest BLOB PRIMARY KEY CHECK(length(digest)=32),
 obligation_key TEXT NOT NULL,
 payload TEXT NOT NULL CHECK(json_valid(payload))) STRICT;
"""

_LEAF_SIZE = 32
_COUNTS = (
    "obligation_count", "unknown_count", "open_count", "open_unknown_count",
    "movement_count", "unresolved_movement_count", "source_event_count", "bad_source_count",
    "bad_paid_count", "bad_other_count",
)
_SUMS = ("remaining_sum", "open_sum", "source_amount_sum", "paid_sum", "other_sum")
_FIELDS = (*_COUNTS, *_SUMS)
(
    _G_OBLIGATION_COUNT, _G_UNKNOWN_COUNT, _G_OPEN_COUNT, _G_OPEN_UNKNOWN_COUNT,
    _G_MOVEMENT_COUNT, _G_UNRESOLVED_MOVEMENT_COUNT, _G_SOURCE_EVENT_COUNT,
    _G_BAD_SOURCE_COUNT, _G_BAD_PAID_COUNT, _G_BAD_OTHER_COUNT,
    _G_REMAINING_SUM, _G_OPEN_SUM, _G_SOURCE_AMOUNT_SUM, _G_PAID_SUM, _G_OTHER_SUM,
) = range(len(_FIELDS))


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
        state["first_source_period"], state["category"], state["account"],
        state["counterparty_id"], state["source_kind"],
    )


def _group_delta(state: dict, direction: int) -> tuple:
    if not state["source_event_count"]:
        return ()
    remaining = _remaining(state)
    opened = remaining is None or remaining != 0
    return (
        direction,
        direction * (remaining is None),
        direction * opened,
        direction * (opened and remaining is None),
        direction * state["movement_count"],
        direction * state["unresolved_movement_count"],
        direction * state["source_event_count"],
        direction * bool(state["bad_source"]),
        direction * bool(state["bad_paid"]),
        direction * bool(state["bad_other"]),
        direction * (remaining or 0),
        direction * (remaining or 0) if opened else 0,
        direction * state["source_amount"],
        direction * state["paid"],
        direction * state["other_settled"],
    )


def _add_group(groups: dict, state: dict, direction: int) -> None:
    delta = _group_delta(state, direction)
    if not delta:
        return
    key = _group_key(state)
    group = groups.get(key)
    if group is None:
        group = groups[key] = [0] * len(_FIELDS)
    for index, value in enumerate(delta):
        group[index] = checked(group[index] + value)
    if group[_G_OBLIGATION_COUNT] == 0:
        if any(group):
            raise ValueError("settlement group was not fully reversed")
        del groups[key]


def _groups_from_root(root: dict) -> dict:
    groups = {}
    for row in root["groups"]:
        if len(row) != 5 + len(_FIELDS):
            raise ValueError("settlement group has an invalid field count")
        groups[tuple(row[:5])] = row[5:]
    return groups


def _groups_for_root(groups: dict) -> list:
    return [
        [*key, *values]
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
    state["events"].append([
        row["publication_id"], row["item_no"], row["source_calculation_id"],
        row["source_digest"].hex() if row["source_digest"] is not None else None,
    ])
    if row["change_kind"] == "source":
        state["source_event_count"] += 1
        state["first_source_period"] = min(
            period, state["first_source_period"]
        ) if state["first_source_period"] is not None else period
        if row["amount"] is None:
            state["bad_source"] = True
        else:
            state["source_amount"] = checked(state["source_amount"] + row["amount"])
        if row["source_calculation_id"] == row["publication_calculation_id"]:
            order = [row["sequence"], row["item_no"]]
            if order > state["latest_order"]:
                state["latest_order"] = order
                for field in (
                    "source_subject_id", "category", "account", "counterparty_id",
                    "component", "source_calculation_id", "source_kind", "source_fact_id",
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
            load_block(header) if load_block is not None
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
    tail_rows: list[dict] = dataclass_field(default_factory=list)


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
            or not isinstance(root["directories"], dict)
            or set(root["directories"]) != {"all", "open"}
        ):
            _fail("freeze_root_identity_mismatch", period)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        _fail("freeze_root_contract_invalid", period)
    root["root_digest"] = expected.hex()
    _validate_directory(root["subject_directory"], period)
    for kind in ("all", "open"):
        _validate_directory(
            root["directories"][kind], period, reason="freeze_directory_invalid"
        )
    # Readers locate leaves by these committed headers, never by the mutable
    # settlement_freeze_ref mirror. Its completeness belongs to full integrity.
    return root


def _validate_directory(
    entries: list, period: int, *, reason: str = "freeze_subject_directory_invalid"
) -> None:
    if (
        not isinstance(entries, list)
        or any(
            not isinstance(item, list) or len(item) != 5
            or type(item[0]) is not int or item[0] != index or not isinstance(item[1], str)
            or not isinstance(item[2], str) or item[1] > item[2]
            or type(item[3]) is not int or item[3] <= 0
            or not is_sha256_hex(item[4])
            for index, item in enumerate(entries)
        )
        or any(left[2] >= right[1] for left, right in zip(entries, entries[1:], strict=False))
    ):
        _fail(reason, period)


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
            len(items) != header[3] or not ids or ids != sorted(set(ids))
            or ids[0] != header[1] or ids[-1] != header[2]
            or any(
                len(item) != 3 or not isinstance(item[0], str)
                or not isinstance(item[1], list) or not isinstance(item[2], list)
                or any(
                    not isinstance(pointer, list) or len(pointer) != 3
                    or type(pointer[0]) is not int or pointer[0] > period
                    or not isinstance(pointer[1], str) or len(pointer[1]) != 64
                    or type(pointer[2]) is not int or pointer[2] <= 0
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


def _read_subject_chunks(
    connection, period: int, pointers: set[tuple[str, tuple]], *, reads=None
) -> dict[tuple[str, tuple], list]:
    cache = (
        reads._frozen_settlement_blocks
        if reads is not None and getattr(reads, "_snapshot_active", False)
        else {}
    )
    missing = [
        (kind, pointer) for kind, pointer in pointers
        if ("subject_chunk", kind, pointer) not in cache
    ]
    if missing:
        payloads = {
            row["digest_hex"]: row["payload"]
            for row in connection.execute(
                "SELECT ids.value digest_hex,b.payload FROM json_each(?) ids "
                "LEFT JOIN settlement_freeze_block b ON b.digest=unhex(ids.value)",
                (canonical(sorted({pointer[1] for _, pointer in missing})),),
            )
        }
        for kind, pointer in missing:
            payload = payloads.get(pointer[1])
            if payload is None or _sha(payload).hex() != pointer[1]:
                _fail("freeze_subject_chunk_digest_mismatch", period)
            try:
                items = json.loads(payload)
                if kind == "source":
                    valid = (
                        isinstance(items, list) and len(items) == pointer[2]
                        and all(isinstance(key, str) for key in items)
                        and items == sorted(set(items))
                    )
                else:
                    valid = (
                        isinstance(items, list) and len(items) == pointer[2]
                        and all(
                            isinstance(item, list) and len(item) == 5
                            and isinstance(item[0], str) and type(item[1]) is int
                            and (item[2] is None or isinstance(item[2], str))
                            and item[3] in {"source", "payment", "other"}
                            and isinstance(item[4], str)
                            for item in items
                        )
                    )
                if not valid:
                    _fail("freeze_subject_chunk_invalid", period)
            except (TypeError, ValueError, json.JSONDecodeError):
                _fail("freeze_subject_chunk_invalid", period)
            cache["subject_chunk", kind, pointer] = items
    return {
        (kind, pointer): cache["subject_chunk", kind, pointer]
        for kind, pointer in pointers
    }


def _subject_item(connection, period: int, directory: list, subject: str,
                  *, load_block=None) -> list | None:
    index = bisect.bisect_left(directory, subject, key=lambda header: header[2])
    if index >= len(directory) or directory[index][1] > subject:
        return None
    header = directory[index]
    items = (load_block(header) if load_block is not None
             else _read_subject_leaf(connection, period, header))
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


def _decode_state(period: int, key: str, expected: bytes, row) -> dict:
    if row is None or row["obligation_key"] != key or _sha(row["payload"]) != expected:
        _fail("freeze_state_digest_mismatch", period)
    try:
        state = json.loads(row["payload"])
        if state["obligation_key"] != key:
            _fail("freeze_state_key_mismatch", period)
        return state
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        _fail("freeze_state_contract_invalid", period)


def _read_state(connection, period: int, key: str, digest_hex: str) -> dict:
    expected = bytes.fromhex(digest_hex)
    row = connection.execute(
        "SELECT obligation_key,payload FROM settlement_state_revision WHERE digest=?",
        (expected,),
    ).fetchone()
    return _decode_state(period, key, expected, row)


def _read_states(connection, period: int, digests: dict[str, str], *, reads=None) -> dict:
    cache = (
        reads._frozen_settlement_states
        if reads is not None and getattr(reads, "_snapshot_active", False)
        else {}
    )
    missing = [
        [key, digest_hex]
        for key, digest_hex in digests.items()
        if (period, key, digest_hex) not in cache
    ]
    if missing:
        for row in connection.execute(
            "SELECT json_extract(ids.value,'$[0]') requested_key,"
            "json_extract(ids.value,'$[1]') digest_hex,"
            "s.obligation_key,s.payload FROM json_each(?) ids "
            "LEFT JOIN settlement_state_revision s ON "
            "s.digest=unhex(json_extract(ids.value,'$[1]'))",
            (canonical(missing),),
        ):
            key, digest_hex = row["requested_key"], row["digest_hex"]
            cache[period, key, digest_hex] = _decode_state(
                period, key, bytes.fromhex(digest_hex), row
            )
    return {key: cache[period, key, digest_hex] for key, digest_hex in digests.items()}


def _lookup(
    connection, period: int, directory: list, key: str, *, load_block=None
) -> tuple[str | None, list | None]:
    index = bisect.bisect_left(directory, key, key=lambda header: header[2])
    if index >= len(directory) or directory[index][1] > key:
        return None, None
    header = directory[index]
    items = (
        load_block(header) if load_block is not None
        else _read_block(connection, period, header)
    )
    found = bisect.bisect_left(items, key, key=lambda item: item[0])
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
            _lookup(
                connection, previous_period, old_directory, key, load_block=load_block
            ) if previous else (None, None)
        )
        old_state = (
            load_state(key, old_digest) if load_state is not None
            else _read_state(connection, previous_period, key, old_digest)
        ) if old_digest else None
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
        events_by_subject.setdefault(subject, []).append([
            row["publication_id"], row["item_no"], row["obligation_key"],
            row["change_kind"], row["state"],
        ])
    subject_replacement = {}
    for subject, events in events_by_subject.items():
        previous_item = _subject_item(
            connection, previous_period, old_subjects, subject, load_block=load_subject,
        ) if previous else None
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
        connection, previous_period, old_subjects, subject_replacement,
        load_block=load_subject or (
            lambda header: _read_subject_leaf(connection, previous_period, header)
        ),
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


def prepare_freeze_projection(
    connection,
    period: int,
    logical_close_digest: bytes,
    publication_highwater: int,
) -> PreparedFreeze:
    """Prepare the closed state without writing, for one atomic close transaction."""
    from .settlement_projection import verify_settlement_periods

    # A close may have no publications at all in its posting month. There is
    # then no settlement seal for that month; passing the bare period would
    # incorrectly demand a synthetic empty-period seal.
    active_period = connection.execute(
        "SELECT 1 FROM calculation_publication WHERE posting_period=? "
        "UNION SELECT 1 FROM settlement_change WHERE posting_period=? "
        "UNION SELECT 1 FROM settlement_projection_seal WHERE posting_period=? LIMIT 1",
        (period, period, period),
    ).fetchone()
    if active_period is not None:
        verify_settlement_periods(connection, {period})
    previous_period = _close_before(connection, period)
    # Periods._manifest has independently verified every prior frozen root,
    # mirror, block and revision in this close transaction before preparation.
    # Reusing the committed headers here does not repeat that whole comparison.
    previous = _read_root(connection, previous_period) if previous_period is not None else None
    by_key = {}
    period_rows = []
    query = (
        "SELECT s.*,p.sequence,p.calculation_id publication_calculation_id,"
        "c.kind source_kind,c.fact_id source_fact_id FROM settlement_change s "
        "JOIN calculation_publication p ON p.id=s.publication_id "
        "LEFT JOIN calculation c ON c.id=s.source_calculation_id "
        "WHERE s.posting_period=? ORDER BY p.sequence,s.item_no"
    )
    for row in connection.execute(query, (period,)):
        period_rows.append(dict(row))
        if row["obligation_key"] is not None:
            by_key.setdefault(row["obligation_key"], []).append(dict(row))
    return _build_prepared(
        connection, period, logical_close_digest, publication_highwater, previous,
        by_key, period_rows,
    )


def persist_freeze_projection(connection, prepared: PreparedFreeze) -> None:
    """Persist the prepared proof after close_storage.write_close, in its transaction."""
    period = prepared.period
    row = connection.execute(
        "SELECT period,manifest,digest FROM period_close WHERE period=?", (period,)
    ).fetchone()
    if row is None or bytes(row["digest"]) != prepared.close_digest:
        _fail("close_digest_mismatch", period)
    header = verified_header(connection, row, require_marker=False)
    if derived_root(header, "settlement") != prepared.root_digest:
        _fail("close_derived_root_mismatch", period)
    connection.executemany(
        "INSERT OR IGNORE INTO settlement_freeze_block VALUES(?,?)",
        prepared.blocks.items(),
    )
    connection.executemany(
        "INSERT OR IGNORE INTO settlement_state_revision VALUES(?,?,?)",
        ((digest, key, payload) for digest, (key, payload) in prepared.revisions.items()),
    )
    connection.execute(
        "INSERT INTO settlement_freeze_root VALUES(?,?,?,?)",
        (period, prepared.close_digest, prepared.root_json, prepared.root_digest),
    )
    root = json.loads(prepared.root_json)
    connection.executemany(
        "INSERT INTO settlement_freeze_ref VALUES(?,?,?,?,?,?,?)",
        (
            (period, kind, ordinal, first, last, count, bytes.fromhex(digest_hex))
            for kind, directory in root["directories"].items()
            for ordinal, first, last, count, digest_hex in directory
        ),
    )


def frozen_root(connection, period: int) -> dict:
    """Return one fully anchored directory and its composable cohort summaries."""
    return _read_root(connection, period)


def frozen_page(
    connection,
    period: int,
    *,
    kind: str = "open",
    after: str | None = None,
    limit: int = 100,
) -> tuple[list[dict], dict]:
    """Hydrate a proven bounded range of frozen obligations."""
    if kind not in {"all", "open"} or limit < 0:
        raise ValueError("invalid frozen page request")
    root = _read_root(connection, period)
    amounts = {key: (paid, other) for key, paid, other in root["period_amounts"]}
    directory = root["directories"][kind]
    start = bisect.bisect_right([header[2] for header in directory], after) if after else 0
    result = []
    for header in directory[start:]:
        for key, digest_hex, _first_source in _read_block(connection, period, header):
            if after is None or key > after:
                state = _read_state(connection, period, key, digest_hex)
                state["period_paid"], state["period_other"] = amounts.get(key, (0, 0))
                result.append(state)
                if len(result) >= limit + 1:
                    return result, root
    return result, root


def frozen_key(connection, period: int, key: str) -> dict | None:
    """Get one key with a directory-backed presence or absence proof."""
    root = _read_root(connection, period)
    digest_hex, _ = _lookup(connection, period, root["directories"]["all"], key)
    if digest_hex is None:
        return None
    state = _read_state(connection, period, key, digest_hex)
    amounts = {item[0]: item[1:] for item in root["period_amounts"]}
    state["period_paid"], state["period_other"] = amounts.get(key, (0, 0))
    return state


def _verify_late_reviews(connection, period, reviews, *, reads=None, engine=None):
    """Prove later reviews preserve the entire frozen accounting meaning."""
    from .accounting import AccountingBook
    from .content_history_context import publication_reader
    from .integrity import verify_publication

    if engine is None and reads is not None:
        engine = reads.engine
    if engine is None:
        # Low-level SQL readers may lack a QueryReads context. Reuse this exact
        # connection and the installed bundle; constructing Store opens no DB.
        from .engine import Engine
        from .schema_bundle import production_bundle
        from .storage import Store

        identity = connection.execute("SELECT * FROM identity WHERE id=1").fetchone()
        path = next(
            row[2] for row in connection.execute("PRAGMA database_list") if row[1] == "main"
        )
        engine = Engine(Store(
            path, production_bundle(), identity["company_id"], identity["database_id"],
            taxpayer_id=identity["taxpayer_id"],
        ))
    publication_reader().verify_publication_chain(
        connection, subject_ids={row["subject_id"] for row in reviews}
    )
    pairs = []
    for row in reviews:
        prior = connection.execute(
            "SELECT calculation_id FROM calculation_publication WHERE id=?",
            (row["previous_publication_id"],),
        ).fetchone()
        if prior is None or prior[0] is None or row["calculation_id"] is None:
            _fail("late_review_predecessor_missing", period)
        pairs.append((prior[0], row["calculation_id"]))
    identifiers = {ident for pair in pairs for ident in pair}
    verify_publication(engine, connection, identifiers)
    book = AccountingBook(engine.store.registry)
    book.load(engine.store, connection, identifiers)
    for before, after in pairs:
        if book.signature(before) != book.signature(after):
            _fail("late_review_accounting_mismatch", period)


def _tail_periods(
    connection, base_root: dict, maximum: int | None, *, reads=None, minimum: int | None = None
) -> set[int]:
    """Verify every authoritative open posting period before reading its rows.

    Future postings published before the last close are included by posting
    period. Sequence is separately checked so a later financial publication
    cannot mutate a frozen posting period. No-impact reviews retain their
    original posting month but do not enter the frozen financial state.
    """
    from .settlement_projection import verify_settlement_periods

    last = base_root["period"]
    lower = max(last, minimum) if minimum is not None else last
    highwater = base_root["publication_highwater"]
    late = connection.execute(
        "SELECT * FROM calculation_publication "
        "WHERE sequence>? AND posting_period<=? ORDER BY sequence",
        (highwater, last),
    ).fetchall()
    if any(row["mode"] != "review_no_impact" for row in late):
        _fail("published_into_frozen_period", last)
    if late:
        _verify_late_reviews(connection, last, late, reads=reads)
        verify_settlement_periods(
            connection, {row["posting_period"] for row in late}, reads=reads
        )
    from .posting_period_reads import posting_periods

    periods = posting_periods(
        connection,
        ("calculation_publication", "settlement_change", "settlement_projection_seal"),
        after=lower, through=maximum,
    )
    if periods:
        verify_settlement_periods(connection, periods, reads=reads)
    return periods


def _read_tail_rows(connection, periods, *, subject_ids=None, obligation_keys=None):
    if not periods:
        return []
    query = (
        "SELECT s.*,p.sequence,p.calculation_id publication_calculation_id,"
        "c.kind source_kind,c.fact_id source_fact_id FROM settlement_change s "
        "JOIN calculation_publication p ON p.id=s.publication_id "
        "LEFT JOIN calculation c ON c.id=s.source_calculation_id "
        "WHERE s.posting_period IN (SELECT value FROM json_each(?)) "
    )
    parameters = [canonical(sorted(periods))]
    if subject_ids is not None:
        query = (
            query + "AND s.source_subject_id IN (SELECT value FROM json_each(?)) UNION "
            + query + "AND s.obligation_key IN (SELECT value FROM json_each(?)) "
        )
        parameters = [
            *parameters, canonical(sorted(subject_ids)),
            *parameters, canonical(sorted(obligation_keys)),
        ]
    return [
        dict(row)
        for row in connection.execute(
            query + "ORDER BY posting_period,sequence,item_no", parameters,
        )
    ]


def _tail_rows(
    connection, base_root: dict, maximum: int | None, *, reads=None, minimum: int | None = None
) -> list[dict]:
    periods = _tail_periods(connection, base_root, maximum, reads=reads, minimum=minimum)
    return _read_tail_rows(connection, periods)


def _scope(
    connection, period: str, *, current: bool, reads=None, subject_ids=None
) -> FrozenScope | None:
    if reads is not None and reads.connection is not connection:
        raise ValueError("frozen settlement reads belong to another snapshot")
    if subject_ids is not None:
        return _subject_scope(connection, period, subject_ids, current=current, reads=reads)
    cache = (
        reads._frozen_settlement_scopes
        if reads is not None and getattr(reads, "_snapshot_active", False)
        else None
    )
    cache_key = (period, current)
    if cache is not None and cache_key in cache:
        return cache[cache_key]
    cutoff = YearMonth(period).ordinal
    latest = connection.execute("SELECT max(period) FROM period_close").fetchone()[0]
    if latest is None:
        return None
    base_period = latest if current or cutoff > latest else cutoff
    if base_period != latest and connection.execute(
        "SELECT 1 FROM period_close WHERE period=?", (base_period,)
    ).fetchone() is None:
        return None
    counterpart = cache.get((period, not current)) if cache is not None else None
    if (
        not current and counterpart is not None
        and counterpart.base_period == base_period
        and all(row["posting_period"] <= cutoff for row in counterpart.tail_rows)
    ):
        result = FrozenScope(
            cutoff, cutoff, base_period, counterpart.base_root,
            counterpart.groups, counterpart.period_amounts, counterpart.overrides,
            False, counterpart.tail_rows,
        )
        cache[cache_key] = result
        return result
    root_cache = (
        reads._frozen_settlement_roots
        if reads is not None and getattr(reads, "_snapshot_active", False)
        and reads.connection is connection
        else {}
    )

    def read_root_once(value):
        if value not in root_cache:
            root_cache[value] = _read_root(connection, value)
        return root_cache[value]

    base_root = read_root_once(base_period)
    block_cache = (
        reads._frozen_settlement_blocks
        if reads is not None and getattr(reads, "_snapshot_active", False)
        else {}
    )
    state_cache = (
        reads._frozen_settlement_states
        if reads is not None and getattr(reads, "_snapshot_active", False)
        else {}
    )

    def load_block(header):
        key = (base_period, tuple(header))
        if key not in block_cache:
            block_cache[key] = _read_block(connection, base_period, header)
        return block_cache[key]

    tail = []
    if current or cutoff > base_period:
        tail_cache = (
            reads._frozen_settlement_tails
            if reads is not None and getattr(reads, "_snapshot_active", False)
            else {}
        )
        tail_key = (base_period, base_root["root_digest"])
        maximum = None if current else cutoff
        cached_tail = tail_cache.get(tail_key)
        if cached_tail is None:
            tail = _tail_rows(connection, base_root, maximum, reads=reads)
            tail_cache[tail_key] = (maximum, tail)
        else:
            covered, rows = cached_tail
            if covered is not None and (maximum is None or maximum > covered):
                rows = rows + _tail_rows(
                    connection, base_root, maximum, reads=reads, minimum=covered
                )
                tail_cache[tail_key] = (maximum, rows)
            tail = rows if maximum is None else [
                row for row in rows if row["posting_period"] <= maximum
            ]
        if (
            counterpart is not None
            and counterpart.base_period == base_period
            and not any(row["posting_period"] > cutoff for row in tail_cache[tail_key][1])
        ):
            result = FrozenScope(
                cutoff, cutoff, base_period, base_root,
                counterpart.groups, counterpart.period_amounts, counterpart.overrides,
                current, tail,
            )
            cache[cache_key] = result
            return result

    # An equal counterpart already contains these authenticated aggregates.
    # Construct them only after checking the complete open tail and deciding
    # that reuse is impossible. The tail checks above still run independently
    # when current scope may include a later posting period.
    groups = _groups_from_root(base_root)
    change_counts = _change_period_counts(base_root)
    overrides = {}
    amount_root = (
        read_root_once(cutoff) if current and cutoff < base_period
        and connection.execute("SELECT 1 FROM period_close WHERE period=?", (cutoff,)).fetchone()
        else base_root if cutoff == base_period else None
    )
    target_amounts = (
        {key: (paid, other) for key, paid, other in amount_root["period_amounts"]}
        if amount_root else {}
    )
    if current or cutoff > base_period:
        grouped = {}
        for row in tail:
            if row["obligation_key"] is not None:
                grouped.setdefault(row["obligation_key"], []).append(row)
                if row["posting_period"] == cutoff and row["state"] == "resolved":
                    paid, other = target_amounts.get(row["obligation_key"], (0, 0))
                    if row["change_kind"] == "payment":
                        paid = checked(paid + row["amount"])
                    elif row["change_kind"] == "other":
                        other = checked(other + row["amount"])
                    target_amounts[row["obligation_key"]] = paid, other
        base_digests = {}
        for key in grouped:
            digest_hex, _ = _lookup(
                connection, base_period, base_root["directories"]["all"], key,
                load_block=load_block,
            )
            if digest_hex:
                base_digests[key] = digest_hex
        missing_states = [
            [key, digest_hex]
            for key, digest_hex in base_digests.items()
            if (base_period, key, digest_hex) not in state_cache
        ]
        if missing_states:
            for row in connection.execute(
                "SELECT json_extract(ids.value,'$[0]') requested_key,"
                "json_extract(ids.value,'$[1]') digest_hex,"
                "s.obligation_key,s.payload FROM json_each(?) ids "
                "LEFT JOIN settlement_state_revision s ON "
                "s.digest=unhex(json_extract(ids.value,'$[1]'))",
                (canonical(missing_states),),
            ):
                key, digest_hex = row["requested_key"], row["digest_hex"]
                state_cache[base_period, key, digest_hex] = _decode_state(
                    base_period, key, bytes.fromhex(digest_hex), row
                )
        for key, rows in grouped.items():
            digest_hex = base_digests.get(key)
            old = state_cache[base_period, key, digest_hex] if digest_hex else None
            if old is not None:
                _add_group(groups, old, -1)
                _update_change_period_counts(change_counts, old, -1)
            state = copy.deepcopy(old) if old else _empty_state(key)
            state["period_paid"] = state["period_other"] = 0
            state["events"] = []
            for row in rows:
                _apply(state, row, row["posting_period"])
            _add_group(groups, state, 1)
            _update_change_period_counts(change_counts, state, 1)
            overrides[key] = state
    through = cutoff
    if current:
        through = max(
            [cutoff]
            + [last for (first, last), count in change_counts.items() if first <= cutoff and count]
        )
    result = FrozenScope(
        cutoff, through, base_period, base_root, groups, target_amounts, overrides,
        current, tail,
    )
    if cache is not None:
        cache[cache_key] = result
    return result


def _included_groups(scope: FrozenScope):
    return (
        (key, values) for key, values in scope.groups.items()
        if key[0] is not None and key[0] <= scope.cutoff
    )


def frozen_followup_summary(
    connection, period: str, *, current: bool = False, reads=None
) -> dict | None:
    """Compose a scoped KPI from authenticated cohort measures and open tail."""
    scope = _scope(connection, period, current=current, reads=reads)
    if scope is None:
        return None
    totals = {field: 0 for field in (*_COUNTS, *_SUMS)}
    for _, group in _included_groups(scope):
        for index, field in enumerate(_FIELDS):
            totals[field] = checked(totals[field] + group[index])
    unresolved = bool(totals["unknown_count"] or totals["unresolved_movement_count"])
    cutoff_label = str(YearMonth.from_ordinal(scope.through))
    return {
        "status": (
            "partially_established" if unresolved else "established"
            if totals["obligation_count"] else "not_established"
        ),
        "cutoff_period": cutoff_label,
        "current_cutoff_period": cutoff_label if current else None,
        "issues": (
            [{"field": "settlements", "message": "存在尚未确立的清偿关系"}]
            if unresolved else []
        ),
        "obligation_count": totals["obligation_count"],
        "followup_count": totals["open_count"],
        "complete": not unresolved,
        "unestablished_state_selection_count": 0,
        "movement_count": totals["movement_count"],
        "source_amount_fen": (
            None if totals["bad_source_count"] else totals["source_amount_sum"]
        ),
        "paid_fen": None if totals["bad_paid_count"] else totals["paid_sum"],
        "other_settled_fen": None if totals["bad_other_count"] else totals["other_sum"],
        "remaining_fen": None if totals["unknown_count"] else totals["remaining_sum"],
    }


def _subject_proof(connection, scope: FrozenScope, subject_ids: set[str], *, reads=None):
    """Hydrate only authenticated subject leaves and their period chunks."""
    source_keys: set[str] = set()
    direct_events: list[tuple[int, list]] = []
    source_pointers: list[tuple] = []
    direct_pointers: list[tuple] = []
    subject_parts = []
    directory = scope.base_root["subject_directory"]
    cache = (
        reads._frozen_settlement_blocks
        if reads is not None and getattr(reads, "_snapshot_active", False)
        else {}
    )

    def load_leaf(header):
        key = ("subject_leaf", scope.base_period, tuple(header))
        if key not in cache:
            cache[key] = _read_subject_leaf(connection, scope.base_period, header)
        return cache[key]

    for subject in subject_ids:
        item = _subject_item(
            connection, scope.base_period, directory, subject, load_block=load_leaf,
        )
        if item is None:
            continue
        subject_parts.append(item)
        for pointer in item[1]:
            if pointer[0] <= scope.cutoff:
                source_pointers.append(tuple(pointer))
        for pointer in item[2]:
            if scope.current or pointer[0] <= scope.cutoff:
                direct_pointers.append(tuple(pointer))
    chunks = _read_subject_chunks(
        connection, scope.base_period,
        {("source", pointer) for pointer in source_pointers}
        | {("direct", pointer) for pointer in direct_pointers},
        reads=reads,
    )
    for _subject, sources, events in subject_parts:
        source_by_period = {
            pointer[0]: set(chunks["source", tuple(pointer)])
            for pointer in sources if pointer[0] <= scope.cutoff
        }
        direct_by_period = {
            pointer[0]: chunks["direct", tuple(pointer)]
            for pointer in events if scope.current or pointer[0] <= scope.cutoff
        }
        for posting, source_keys_at_posting in source_by_period.items():
            if posting not in direct_by_period:
                _fail("freeze_subject_source_events_missing", scope.base_period)
            source_keys.update(source_keys_at_posting)
        for posting, items in direct_by_period.items():
            if posting <= scope.cutoff and {
                item[2] for item in items
                if item[3] == "source" and item[2] is not None
            } != source_by_period.get(posting, set()):
                _fail("freeze_subject_source_events_mismatch", scope.base_period)
            direct_events.extend((posting, event) for event in items)
    for row in scope.tail_rows:
        posting = row["posting_period"]
        if row["source_subject_id"] not in subject_ids:
            continue
        if row["change_kind"] == "source" and posting <= scope.cutoff:
            if row["obligation_key"] is not None:
                source_keys.add(row["obligation_key"])
        if scope.current or posting <= scope.cutoff:
            direct_events.append((posting, [
                row["publication_id"], row["item_no"], row["obligation_key"],
                row["change_kind"], row["state"],
            ]))
    return source_keys, direct_events


def _subject_scope(connection, period, subject_ids, *, current, reads=None):
    """Check the complete tail range, hydrating only exact subject/key contributions.

    This scope is never published in the global scope or tail maps. Their
    completeness and cohort measures describe all subjects in the snapshot.
    """
    if reads is not None and reads.connection is not connection:
        raise ValueError("frozen settlement reads belong to another snapshot")
    active = reads is not None and getattr(reads, "_snapshot_active", False)
    if active:
        full_scope = reads._frozen_settlement_scopes.get((period, current))
        if full_scope is not None:
            return full_scope
    root_cache = reads._frozen_settlement_roots if active else {}

    def read_root_once(value):
        if value not in root_cache:
            root_cache[value] = _read_root(connection, value)
        return root_cache[value]
    cutoff = YearMonth(period).ordinal
    latest = connection.execute("SELECT max(period) FROM period_close").fetchone()[0]
    if latest is None:
        return None
    base_period = latest if current or cutoff > latest else cutoff
    if base_period != latest and connection.execute(
        "SELECT 1 FROM period_close WHERE period=?", (base_period,)
    ).fetchone() is None:
        return None
    root = read_root_once(base_period)
    scope = FrozenScope(cutoff, cutoff, base_period, root, {}, {}, {}, current)
    source_keys, _ = _subject_proof(connection, scope, set(subject_ids), reads=reads)
    if current or cutoff > base_period:
        periods = _tail_periods(
            connection, root, None if current else cutoff, reads=reads,
        )
        source_keys.update(
            row[0] for row in connection.execute(
                "SELECT DISTINCT obligation_key FROM settlement_change "
                "WHERE source_subject_id IN (SELECT value FROM json_each(?)) "
                "AND posting_period IN (SELECT value FROM json_each(?)) "
                "AND posting_period<=? AND change_kind='source' "
                "AND obligation_key IS NOT NULL",
                (canonical(sorted(subject_ids)), canonical(sorted(periods)), cutoff),
            )
        )
        scope.tail_rows = _read_tail_rows(
            connection, periods, subject_ids=subject_ids, obligation_keys=source_keys,
        )
    amount_root = (
        read_root_once(cutoff) if current and cutoff < base_period
        and connection.execute("SELECT 1 FROM period_close WHERE period=?", (cutoff,)).fetchone()
        else root if cutoff == base_period else None
    )
    scope.period_amounts = {
        key: (paid, other) for key, paid, other in amount_root["period_amounts"]
        if key in source_keys
    } if amount_root else {}
    grouped = {}
    for row in scope.tail_rows:
        key = row["obligation_key"]
        if key in source_keys:
            grouped.setdefault(key, []).append(row)
            if row["posting_period"] == cutoff and row["state"] == "resolved":
                paid, other = scope.period_amounts.get(key, (0, 0))
                if row["change_kind"] == "payment":
                    paid = checked(paid + row["amount"])
                elif row["change_kind"] == "other":
                    other = checked(other + row["amount"])
                scope.period_amounts[key] = paid, other
    digests = {}
    block_cache = reads._frozen_settlement_blocks if active else {}

    def load_block(header):
        cache_key = (base_period, tuple(header))
        if cache_key not in block_cache:
            block_cache[cache_key] = _read_block(connection, base_period, header)
        return block_cache[cache_key]

    for key in grouped:
        digest_hex, _ = _lookup(
            connection, base_period, root["directories"]["all"], key, load_block=load_block,
        )
        if digest_hex is not None:
            digests[key] = digest_hex
    base_states = _read_states(connection, base_period, digests, reads=reads)
    for key, rows in grouped.items():
        state = copy.deepcopy(base_states[key]) if key in base_states else _empty_state(key)
        state["period_paid"] = state["period_other"] = 0
        state["events"] = []
        for row in rows:
            _apply(state, row, row["posting_period"])
        scope.overrides[key] = state
    return scope


def _key_publications(
    connection, scope: FrozenScope, base_digests: dict[str, str],
    base_states: dict[str, dict], *, reads=None,
) -> tuple[dict[str, int], list[dict], dict[str, str]]:
    publications = {}
    for row in scope.tail_rows:
        if row["obligation_key"] in base_digests:
            publications[row["publication_id"]] = row["posting_period"]
    seen: set[tuple[str, str]] = set()
    source_states = []
    event_sources = {}
    active = dict(base_digests)
    states = base_states
    while active:
        prior = {}
        for key, digest in active.items():
            if (key, digest) in seen:
                _fail("freeze_state_cycle", scope.base_period)
            seen.add((key, digest))
            state = states[key]
            if state["source_calculation_id"] is not None:
                source_states.append(state)
            for event in state["events"]:
                publication_id = event[0]
                posting = state["last_change_period"]
                if publication_id in publications and publications[publication_id] != posting:
                    _fail("freeze_publication_period_mismatch", scope.base_period)
                publications[publication_id] = posting
                calculation_id, source_digest = event[2], event[3]
                if calculation_id is None or source_digest is None:
                    _fail("freeze_event_source_missing", scope.base_period)
                if (
                    calculation_id in event_sources
                    and event_sources[calculation_id] != source_digest
                ):
                    _fail("freeze_event_source_conflict", scope.base_period)
                event_sources[calculation_id] = source_digest
            if state["previous_revision"] is not None:
                prior[key] = state["previous_revision"]
        active = prior
        if active:
            states = _read_states(connection, scope.base_period, active, reads=reads)
    return publications, source_states, event_sources


def frozen_subject_summary(
    connection, period: str, *, subject_ids: set[str], current: bool = False, reads=None,
    include_history_counts: bool = True,
) -> dict | None:
    """Use closed subject membership proof and checked open-tail contributions."""
    from .settlement_projection import _obligation_view

    scope = _scope(connection, period, current=current, reads=reads, subject_ids=subject_ids)
    if scope is None:
        return None
    source_keys, direct_events = _subject_proof(
        connection, scope, set(subject_ids), reads=reads,
    )
    block_cache = (
        reads._frozen_settlement_blocks
        if reads is not None and getattr(reads, "_snapshot_active", False)
        else {}
    )

    def load_block(header):
        cache_key = (scope.base_period, tuple(header))
        if cache_key not in block_cache:
            block_cache[cache_key] = _read_block(connection, scope.base_period, header)
        return block_cache[cache_key]

    base_digests = {}
    for key in source_keys:
        digest, _ = _lookup(
            connection, scope.base_period, scope.base_root["directories"]["all"], key,
            load_block=load_block,
        )
        if digest is not None:
            base_digests[key] = digest
    base_states = _read_states(connection, scope.base_period, base_digests, reads=reads)
    states = {}
    for key in source_keys:
        state = scope.overrides.get(key) or base_states.get(key)
        if state is None or not state["source_event_count"]:
            _fail("freeze_subject_source_missing", scope.base_period)
        if state["first_source_period"] > scope.cutoff:
            _fail("freeze_subject_source_period_mismatch", scope.base_period)
        states[key] = state
    through = scope.cutoff
    if current:
        through = max(
            [through]
            + [state["last_change_period"] for state in states.values()]
            + [posting for posting, _ in direct_events]
        )
    obligations = []
    for key in sorted(states):
        state = dict(states[key])
        state["period_paid"], state["period_other"] = scope.period_amounts.get(key, (0, 0))
        obligations.append(_obligation_view(state))
    unresolved = any(item["remaining_fen"] is None for item in obligations)
    # The authenticated current states already carry complete obligation amounts.
    # Only the full core summary needs the historical chain to count and expose
    # every contributing publication. Owner employee totals consume no counts.
    publications, historical_source_states, event_sources = (
        _key_publications(connection, scope, base_digests, base_states, reads=reads)
        if include_history_counts else ({}, [], {})
    )
    for row in scope.tail_rows:
        if row["obligation_key"] in source_keys:
            publication_id = row["publication_id"]
            posting = row["posting_period"]
            if publication_id in publications and publications[publication_id] != posting:
                _fail("freeze_publication_period_mismatch", scope.base_period)
            publications[publication_id] = posting
    movement_count = sum(state["movement_count"] for state in states.values())
    _verified_page_sources(
        connection, scope.base_period, [*states.values(), *historical_source_states]
    )
    _verified_event_sources(connection, scope.base_period, event_sources)
    for posting, event in direct_events:
        if posting > through:
            continue
        publication_id, _item_no, key, kind, state = event
        if publication_id in publications and publications[publication_id] != posting:
            _fail("freeze_publication_period_mismatch", scope.base_period)
        publications[publication_id] = posting
        if key not in source_keys and kind != "source":
            movement_count += 1
            unresolved = unresolved or state == "unresolved"
    _verified_publication_sources(
        connection, scope.base_period, publications, reads=reads
    )
    label = str(YearMonth.from_ordinal(through))
    result = {
        "cutoff_period": label,
        "status": "partially_established" if unresolved else "established" if obligations
        else "not_established",
        "business": [], "obligations": obligations, "movements": [],
        "line_relations": [],
        "issues": ([{"field": "settlements", "message": "存在尚未确立的清偿关系"}]
                   if unresolved else []),
        **({"business_count": len(publications), "movement_count": movement_count,
            "line_relation_count": 0} if include_history_counts else {}),
        "unestablished_state_selections": [],
        "complete": not unresolved,
    }
    if current:
        result.update({
            "scope_period": period,
            "current_cutoff_period": label,
            "cutoff_semantics": "current_published_relations_independent_of_as_of",
        })
    return result


def frozen_payroll_period_payments(connection, period: str, *, reads=None) -> list[dict] | None:
    """Read only this month's settled wage keys from an anchored freeze scope.

    A cumulative unresolved payment makes even a zero period amount unknown in
    the existing obligation view. Such a scope uses the ordinary checked
    reducer so no unknown is lost by selecting only changed keys here.
    """
    scope = _scope(connection, period, current=False, reads=reads)
    if scope is None:
        return None
    if any(
        group[_G_BAD_PAID_COUNT] or group[_G_BAD_OTHER_COUNT]
        for key, group in _included_groups(scope)
    ):
        return None
    return [
        {"subject_id": state["source_subject_id"], "kind": state["source_kind"],
         "name": state["component"], "period_paid_fen": paid,
         "period_other_settled_fen": other}
        for state, paid, other in _payroll_period_payment_states(connection, scope, reads=reads)
    ]


def _payroll_period_payment_states(connection, scope, *, reads=None, keys=None):
    payroll_kinds = {"payroll", "payroll_bounded", "annual_bonus", "opening_payroll_payable"}
    amounts = scope.period_amounts if keys is None else {
        key: scope.period_amounts[key] for key in keys if key in scope.period_amounts
    }
    result, states = [], []
    entries = _page_entries(
        connection, scope, include_settled=True, page_keys=set(amounts),
        reads=reads,
    )
    base_states = _read_states(
        connection, scope.base_period,
        {key: entries[key][0] for key in amounts
         if key in entries and key not in scope.overrides},
        reads=reads,
    )
    for key, (paid, other) in amounts.items():
        if key not in entries:
            digest_hex, header = _lookup(
                connection, scope.base_period, scope.base_root["directories"]["all"], key
            )
            first = None
            if digest_hex is not None:
                items = _read_block(connection, scope.base_period, header)
                index = bisect.bisect_left(items, key, key=lambda item: item[0])
                first = items[index][2]
            if first is None or first <= scope.cutoff:
                _fail("freeze_period_amount_missing_key", scope.base_period)
            continue
        state = scope.overrides.get(key)
        if state is None:
            state = base_states[key]
        if not state["source_event_count"] or state["source_kind"] not in payroll_kinds:
            continue
        states.append(state)
        result.append((state, paid, other))
    _verified_page_sources(connection, scope.base_period, states)
    return result


def _payroll_period_keys(scope):
    """Locate wage keys in the authenticated complete period-amount directory.

    The prefix is the shared obligation_key encoding, only a candidate filter.
    Each selected state's exact key, source kind, creditor and amount still
    come from its independently committed revision and source checks.
    """
    prefixes = ("payroll:", "payroll_bounded:", "annual_bonus:")
    return {key for key in scope.period_amounts
            if key.startswith(prefixes) and key.endswith(":net")
            or key.startswith("opening_payroll_payable:") and key.endswith(":primary")}


def frozen_employee_net_summary(
    connection, period: str, *, employee_ids, wage_heads=None, reads=None
):
    """Project net pay from authenticated wage cohorts, retaining all wage unknowns.

    Current payroll and opening net pay both use account 221101 and the employee
    as creditor. Other payroll components retain their aggregate completeness
    warning, without becoming net pay. This is not a full business summary.
    """
    from .content_history_context import settlement_reader
    from .domains.transactions import obligation_key

    if getattr(settlement_reader(), "frozen_subject_summary", None) is not frozen_subject_summary:
        return None
    scope = _scope(connection, period, current=False, reads=reads)
    if scope is None:
        return None
    kinds = {"payroll", "payroll_bounded", "annual_bonus", "opening_payroll_payable"}
    employees = set(employee_ids)
    included = list(_included_groups(scope))
    if any(group[_G_UNKNOWN_COUNT] or group[_G_UNRESOLVED_MOVEMENT_COUNT]
           for key, group in included if key[4] not in kinds):
        # A wage can clear another business's obligation. Without its exact
        # subject-event proof, an unresolved external movement is not attributable.
        return None
    groups = [(key, group) for key, group in included if key[4] in kinds]
    net = {employee: {field: 0 for field in (
        "remaining_fen", "period_paid_fen", "period_other_settled_fen"
    )} for employee in employees}
    unknown_paid, unknown_other = set(), set()
    for key, group in groups:
        if key[2] != "221101":
            continue
        employee = key[3]
        if employee not in employees:
            # A cohort must not guess the wage's owner from an unmatched creditor.
            return None
        item = net[employee]
        if group[_G_UNKNOWN_COUNT]:
            item["remaining_fen"] = None
        elif item["remaining_fen"] is not None:
            item["remaining_fen"] = checked(item["remaining_fen"] + group[_G_REMAINING_SUM])
        if group[_G_BAD_PAID_COUNT]:
            unknown_paid.add(employee)
        if group[_G_BAD_OTHER_COUNT]:
            unknown_other.add(employee)
    keys = _payroll_period_keys(scope) if wage_heads is None else {
        obligation_key(head["kind"], head["subject_id"],
                       "primary" if head["kind"] == "opening_payroll_payable" else "net")
        for head in wage_heads
    }
    for state, paid, other in _payroll_period_payment_states(
        connection, scope, reads=reads, keys=keys
    ):
        if state["account"] != "221101":
            continue
        employee = state["counterparty_id"]
        if employee not in employees:
            return None
        item = net[employee]
        item["period_paid_fen"] = checked(item["period_paid_fen"] + paid)
        item["period_other_settled_fen"] = checked(item["period_other_settled_fen"] + other)
    for employee in unknown_paid:
        net[employee]["period_paid_fen"] = None
    for employee in unknown_other:
        net[employee]["period_other_settled_fen"] = None
    return {
        "checking": any(group[_G_UNKNOWN_COUNT] for _, group in groups),
        "employees": net,
    }


def frozen_labor_outstanding_net(connection, period: str, *, reads=None) -> dict | None:
    """Read historical personal remuneration from authenticated account cohorts.

    None means no frozen scope applies; an applicable scope wraps the nullable
    amount so unknown remuneration is never mistaken for a fallback request.
    Account 224104 covers earned and capitalized labor, excluding withheld tax.
    """
    scope = _scope(connection, period, current=False, reads=reads)
    if scope is None:
        return None
    remaining, unknown = 0, False
    for key, group in _included_groups(scope):
        if key[1] != "payable" or key[2] != "224104":
            continue
        remaining = checked(remaining + group[_G_REMAINING_SUM])
        unknown = unknown or bool(group[_G_UNKNOWN_COUNT])
    return {"remaining_fen": None if unknown else remaining}


def obligation_category(category, account, source_kind) -> str:
    if category == "receivable":
        if account == "1122":
            return "customer_receivables"
        if account == "1123":
            return "supplier_advances"
        if "deposit" in (source_kind or ""):
            return "refundable_deposit_receivables"
        return "other_receivables"
    if source_kind in {"payroll", "payroll_bounded", "annual_bonus", "opening_payroll_payable"}:
        return "payroll_payables"
    if source_kind in {"labor", "labor_accrual"}:
        return "labor_payables"
    if account == "2202":
        return "supplier_payables"
    if source_kind in {
        "employee_advance", "reimbursement_acceptance", "reimbursed_asset",
        "reimbursed_asset_batch",
    }:
        return "employee_payables"
    return "other_payables"


def _verified_page_sources(connection, period: int, states: list[dict]) -> None:
    expected = [
        state for state in states if state["source_calculation_id"] is not None
    ]
    if not expected:
        return
    actual = {
        row["id"]: row
        for row in connection.execute(
            "SELECT id,digest,kind,fact_id FROM calculation "
            "WHERE id IN (SELECT value FROM json_each(?))",
            (canonical(sorted({state["source_calculation_id"] for state in expected})),),
        )
    }
    for state in expected:
        row = actual.get(state["source_calculation_id"])
        if (
            row is None
            or state["source_digest"] is None
            or bytes(row["digest"]).hex() != state["source_digest"]
            or row["kind"] != state["source_kind"]
            or row["fact_id"] != state["source_fact_id"]
        ):
            _fail("freeze_page_source_mismatch", period)


def _verified_publication_sources(
    connection, period: int, identifiers: dict[str, int], *, reads=None
) -> None:
    """Check the exact publication heads named by hydrated subject events."""
    if not identifiers:
        return
    from .publication import CONTENT_FIELDS, verify_record

    cache = (
        reads._verified_publication_ids
        if reads is not None and getattr(reads, "_snapshot_active", False)
        and reads.connection is connection
        else {}
    )
    for identifier, posting in identifiers.items():
        if identifier in cache and cache[identifier] != posting:
            _fail("freeze_page_publication_period_mismatch", period)
    pending = set(identifiers) - cache.keys()
    if not pending:
        return
    fields = ",".join(f"p.{field}" for field in CONTENT_FIELDS)
    found = set()
    for row in connection.execute(
        f"SELECT p.id,{fields} FROM calculation_publication p "
        "JOIN json_each(?) ids ON p.id=ids.value",
        (canonical(sorted(pending)),),
    ):
        verify_record(row)
        if row["posting_period"] != identifiers[row["id"]]:
            _fail("freeze_page_publication_period_mismatch", period)
        found.add(row["id"])
    if found != pending:
        _fail("freeze_page_publication_missing", period)
    cache.update((identifier, identifiers[identifier]) for identifier in pending)


def _verified_event_sources(connection, period: int, expected: dict[str, str]) -> None:
    if not expected:
        return
    actual = {
        row["id"]: bytes(row["digest"]).hex()
        for row in connection.execute(
            "SELECT c.id,c.digest FROM calculation c "
            "JOIN json_each(?) ids ON c.id=ids.value",
            (canonical(sorted(expected)),),
        )
    }
    if actual != expected:
        _fail("freeze_event_source_digest_mismatch", period)


def _page_entries(
    connection,
    scope: FrozenScope,
    *,
    include_settled: bool,
    page_keys: set[str] | None,
    after: str | None = None,
    limit: int | None = None,
    reads=None,
) -> dict:
    directory = scope.base_root["directories"]["all" if include_settled else "open"]
    block_cache = (
        reads._frozen_settlement_blocks
        if reads is not None and getattr(reads, "_snapshot_active", False)
        else {}
    )

    def load_block(header):
        cache_key = (scope.base_period, tuple(header))
        if cache_key not in block_cache:
            block_cache[cache_key] = _read_block(connection, scope.base_period, header)
        return block_cache[cache_key]

    def eligible(state):
        return (
            state["source_event_count"]
            and state["first_source_period"] <= scope.cutoff
            and (include_settled or _remaining(state) != 0)
        )

    if page_keys is not None:
        ordinals = {
            index
            for key in page_keys
            if (index := bisect.bisect_left(
                directory, key, key=lambda header: header[2]
            )) < len(directory)
            and directory[index][1] <= key
        }
        headers = [directory[index] for index in sorted(ordinals)]
        entries = {
            key: [digest_hex, first]
            for header in headers
            for key, digest_hex, first in load_block(header)
            if first is not None and first <= scope.cutoff and key in page_keys
        }
        for key, state in scope.overrides.items():
            if key in page_keys:
                if eligible(state):
                    entries[key] = [None, state["first_source_period"]]
                else:
                    entries.pop(key, None)
        return entries

    if limit is None:
        raise ValueError("bounded page limit is required")
    if after is not None:
        override = scope.overrides.get(after)
        if override is not None:
            cursor_valid = eligible(override)
        else:
            digest_hex, header = _lookup(
                connection, scope.base_period, directory, after, load_block=load_block
            )
            cursor_valid = False
            if digest_hex is not None:
                items = load_block(header)
                index = bisect.bisect_left(items, after, key=lambda item: item[0])
                first = items[index][2]
                cursor_valid = first is not None and first <= scope.cutoff
        if not cursor_valid:
            raise KernelError("dashboard_snapshot_changed", "分页位置已变化，请重新加载明细。")

    def base_entries():
        first_index = (
            bisect.bisect_right(directory, after, key=lambda header: header[2])
            if after is not None else 0
        )
        for index in range(first_index, len(directory)):
            header = directory[index]
            for key, digest_hex, first in load_block(header):
                if (
                    (after is None or key > after)
                    and first is not None
                    and first <= scope.cutoff
                    and key not in scope.overrides
                ):
                    yield key, [digest_hex, first]

    overrides = iter(sorted(
        (key, [None, state["first_source_period"]])
        for key, state in scope.overrides.items()
        if (after is None or key > after) and eligible(state)
    ))
    frozen = iter(base_entries())
    base_item, override_item = next(frozen, None), next(overrides, None)
    entries = {}
    while len(entries) <= limit and (base_item is not None or override_item is not None):
        if override_item is None or (
            base_item is not None and base_item[0] < override_item[0]
        ):
            key, value = base_item
            base_item = next(frozen, None)
        else:
            key, value = override_item
            override_item = next(overrides, None)
        entries[key] = value
    return entries


def frozen_dashboard_open(
    connection,
    period: str,
    *,
    current: bool = False,
    after: str | None = None,
    limit: int = 100,
    page_keys: set[str] | None = None,
    summary_only: bool = False,
    include_settled_page: bool = False,
    reads=None,
    order_rows=None,
) -> dict | None:
    """Build the dashboard from frozen cohort measures and a verified open tail."""
    from .settlement_projection import _obligation_view

    scope = _scope(connection, period, current=current, reads=reads)
    if scope is None:
        return None
    if page_keys is not None and not page_keys:
        through = str(YearMonth.from_ordinal(scope.through))
        return {
            "cutoff_period": through,
            "status": "not_established",
            "complete": True,
            "issues": [],
            "categories": {},
            "obligations": [],
            "page": {
                "total_count": 0,
                "filtered_count": 0,
                "returned_count": 0,
                "has_more": False,
                "next_cursor": None,
            },
            **({"current_cutoff_period": through} if current else {}),
        }
    if limit < 1:
        raise ValueError("page limit must be positive")
    categories = {}
    obligation_count = 0
    unknown = False
    for key, group in _included_groups(scope):
        obligation_count += group[_G_OBLIGATION_COUNT]
        unknown = unknown or bool(group[_G_UNKNOWN_COUNT])
        if not group[_G_OPEN_COUNT]:
            continue
        label = obligation_category(key[1], key[2], key[4])
        item = categories.setdefault(label, {"count": 0, "amount": 0, "unknown": False})
        item["count"] += group[_G_OPEN_COUNT]
        item["amount"] = checked(item["amount"] + group[_G_OPEN_SUM])
        item["unknown"] = item["unknown"] or bool(group[_G_OPEN_UNKNOWN_COUNT])
    category_rows = {
        key: {"count": value["count"], "amount": None if value["unknown"] else value["amount"]}
        for key, value in categories.items()
    }
    open_entries = None
    if page_keys is None or page_keys:
        open_entries = _page_entries(
            connection, scope, include_settled=include_settled_page,
            page_keys=page_keys,
            after=None if order_rows is not None and not summary_only else after,
            limit=(sum(row["count"] for row in category_rows.values()) + len(scope.overrides)
                   if order_rows is not None and not summary_only else limit), reads=reads,
        )
    if page_keys is None:
        selected = list(open_entries)
        if order_rows is not None and not summary_only:
            # The proven directory determines membership. Scalar state fields
            # locate display order only; selected full states retain their
            # independent digest and precise frozen-source verification below.
            fields = ("source_subject_id", "counterparty_id", "component",
                      "source_calculation_id", "source_kind", "source_fact_id")
            candidates = [scope.overrides[key] for key in selected if key in scope.overrides]
            base = [[key, open_entries[key][0]] for key in selected if key not in scope.overrides]
            if base:
                found = list(connection.execute(
                    "SELECT json_extract(ids.value,'$[0]') obligation_key," + ",".join(
                        f"json_extract(s.payload,'$.{field}') {field}" for field in fields
                    ) + " FROM json_each(?) ids LEFT JOIN settlement_state_revision s ON "
                    "s.digest=unhex(json_extract(ids.value,'$[1]'))",
                    (canonical(base),),
                ))
                if len(found) != len(base) or any(row["source_kind"] is None for row in found):
                    _fail("freeze_state_missing_sort_source", scope.base_period)
                candidates.extend(dict(row) for row in found)
            ordered = order_rows(candidates)
            if after is not None and after not in ordered:
                raise KernelError("dashboard_snapshot_changed", "分页位置已变化，请重新加载明细。")
            start = ordered.index(after) + 1 if after is not None else 0
            selected = ordered[start:start + limit + 1]
    else:
        selected = [] if not page_keys else sorted(page_keys & open_entries.keys())[: limit + 1]
    states = []
    if not summary_only:
        # The ordered directory's extra key proves continuation, not a returned
        # business item. Keep that lookahead scalar-only; authenticate complete
        # payloads and sources only for the rows this page actually displays.
        displayed = selected[:limit] if order_rows is not None else selected
        base_states = _read_states(
            connection, scope.base_period,
            {key: open_entries[key][0] for key in displayed
             if key not in scope.overrides},
            reads=reads,
        )
        for key in displayed:
            state = scope.overrides.get(key)
            if state is None:
                state = base_states[key]
            state = dict(state)
            state["period_paid"], state["period_other"] = scope.period_amounts.get(key, (0, 0))
            states.append(state)
    _verified_page_sources(connection, scope.base_period, states)
    obligations = [_obligation_view(state) for state in states[:limit]]
    more = len(selected) > limit
    through = str(YearMonth.from_ordinal(scope.through))
    total = sum(item["count"] for item in category_rows.values())
    return {
        "cutoff_period": through,
        "status": "partially_established" if unknown else "established"
        if obligation_count else "not_established",
        "complete": not unknown,
        "issues": ([{"field": "settlements", "message": "存在尚未确立的清偿关系"}]
                   if unknown else []),
        "categories": category_rows,
        "obligations": obligations,
        "page": {
            "total_count": total,
            "filtered_count": total,
            "returned_count": min(len(selected), limit),
            "has_more": more,
            "next_cursor": selected[limit - 1] if more else None,
        },
        **({"current_cutoff_period": through} if current else {}),
    }


def frozen_position_rows(
    connection, period: str, accounts: set[str], *, reads=None
) -> list[dict] | None:
    scope = _scope(connection, period, current=False, reads=reads)
    if scope is None:
        return None
    positions = {}
    for key, group in _included_groups(scope):
        _, category, account, counterparty_id, _ = key
        if account not in accounts:
            continue
        item = positions.setdefault(
            (account, category, counterparty_id),
            {"remaining": 0, "unknown": False, "known_count": 0},
        )
        item["remaining"] = checked(item["remaining"] + group[_G_REMAINING_SUM])
        item["unknown"] = item["unknown"] or bool(group[_G_UNKNOWN_COUNT])
        item["known_count"] += group[_G_OBLIGATION_COUNT] - group[_G_UNKNOWN_COUNT]
    return [
        {
            "account": key[0], "category": key[1], "counterparty_id": key[2],
            "remaining": value["remaining"] if value["known_count"] else None,
            "unknown": value["unknown"],
        }
        for key, value in positions.items()
    ]


def _authoritative_freezes(
    engine, connection, *, verified_calculations=None, _verified_projection=None
):
    """Build expected immutable roots from outcomes and the publication chain.

    This path never reads stored frozen state, blocks, or directories. It is
    deliberately reserved for complete integrity checks and explicit repair.
    """
    from .settlement_projection import (
        _COLUMNS,
        _VerifiedProjection,
        compare_settlement_projection,
        expected_settlement_projection,
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
            raise ValueError("settlement source verification belongs to another snapshot")
        require_verified_lease(connection, _verified_projection.lease)
        expected = _verified_projection.expected
    publications = {}
    publications_by_period = {}
    for publication in connection.execute("SELECT * FROM calculation_publication"):
        publications[publication["id"]] = publication
        publications_by_period.setdefault(publication["posting_period"], []).append(publication)
    calculations = {
        row["id"]: row
        for row in connection.execute("SELECT id,kind,fact_id FROM calculation")
    }
    by_period = {}
    for values in expected:
        row = dict(zip(_COLUMNS, values, strict=True))
        publication = publications[row["publication_id"]]
        calculation = calculations.get(row["source_calculation_id"])
        row.update({
            "sequence": publication["sequence"],
            "publication_calculation_id": publication["calculation_id"],
            "source_kind": calculation["kind"] if calculation else None,
            "source_fact_id": calculation["fact_id"] if calculation else None,
        })
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
        late = [
            publication for publication in publications_by_period.get(period, ())
            if publication["sequence"] > highwater
        ]
        if late:
            if any(publication["mode"] != "review_no_impact" for publication in late):
                _fail("published_into_frozen_period", period)
            _verify_late_reviews(connection, period, late, engine=engine)
            # A current review replaces its tranche, so filtering current rows
            # would also lose the original. Reconstruct at the close boundary.
            historical = expected_settlement_projection(
                engine, connection, periods={period},
                verified_calculations=verified_calculations,
                publication_highwater=highwater,
            )
            period_rows = []
            for values in historical:
                item = dict(zip(_COLUMNS, values, strict=True))
                publication = publications[item["publication_id"]]
                calculation = calculations.get(item["source_calculation_id"])
                item.update({
                    "sequence": publication["sequence"],
                    "publication_calculation_id": publication["calculation_id"],
                    "source_kind": calculation["kind"] if calculation else None,
                    "source_fact_id": calculation["fact_id"] if calculation else None,
                })
                period_rows.append(item)
        period_rows.sort(key=lambda row: (row["sequence"], row["item_no"]))
        rows = {}
        for item in period_rows:
            if item["obligation_key"] is not None:
                rows.setdefault(item["obligation_key"], []).append(item)
        new = _build_prepared(
            connection, period, bytes(close["digest"]), highwater, previous,
            rows, period_rows, load_block=load_block, load_state=load_state,
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
        item.period: (item.close_digest, item.root_json, item.root_digest)
        for item in prepared
    }
    actual_roots = {
        row[0]: (bytes(row[1]), row[2], bytes(row[3]))
        for row in connection.execute(
            "SELECT period,close_digest,root_json,root_digest FROM settlement_freeze_root"
        )
    }
    if actual_roots != expected_roots:
        return True
    if _directory_references_differ(connection, prepared):
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


def _directory_references_differ(connection, prepared: list[PreparedFreeze]) -> bool:
    """Compare the complete mirror to roots independently rebuilt from sources.

    Full verification and repair also compare every block and revision. Ordinary
    reads use the committed root headers directly and do not consume this mirror.
    """
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
    return actual_refs != expected_refs


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


def repair_frozen_settlement_projection(engine, connection) -> dict:
    """Repair derived bytes only when they reproduce the committed close roots."""
    if not connection.in_transaction:
        raise ValueError("frozen settlement repair requires a write transaction")
    prepared, blocks, revisions = _authoritative_freezes(engine, connection)
    if not _frozen_difference(connection, prepared, blocks, revisions):
        return {"changed": False, "periods": len(prepared)}
    expected_periods = [item.period for item in prepared]
    encoded_periods = canonical(expected_periods)
    connection.execute(
        "DELETE FROM settlement_freeze_ref WHERE period NOT IN "
        "(SELECT value FROM json_each(?))", (encoded_periods,),
    )
    connection.execute(
        "DELETE FROM settlement_freeze_root WHERE period NOT IN "
        "(SELECT value FROM json_each(?))", (encoded_periods,),
    )
    for digest_value, payload in blocks.items():
        connection.execute(
            "INSERT INTO settlement_freeze_block(digest,payload) VALUES(?,?) "
            "ON CONFLICT(digest) DO UPDATE SET payload=excluded.payload",
            (digest_value, payload),
        )
    for digest_value, (key, payload) in revisions.items():
        connection.execute(
            "INSERT INTO settlement_state_revision(digest,obligation_key,payload) "
            "VALUES(?,?,?) ON CONFLICT(digest) DO UPDATE SET "
            "obligation_key=excluded.obligation_key,payload=excluded.payload",
            (digest_value, key, payload),
        )
    for item in prepared:
        connection.execute(
            "INSERT INTO settlement_freeze_root VALUES(?,?,?,?) "
            "ON CONFLICT(period) DO UPDATE SET close_digest=excluded.close_digest,"
            "root_json=excluded.root_json,root_digest=excluded.root_digest",
            (item.period, item.close_digest, item.root_json, item.root_digest),
        )
        connection.execute("DELETE FROM settlement_freeze_ref WHERE period=?", (item.period,))
        for kind, directory in json.loads(item.root_json)["directories"].items():
            connection.executemany(
                "INSERT INTO settlement_freeze_ref VALUES(?,?,?,?,?,?,?)",
                (
                    (item.period, kind, ordinal, first, last, count, bytes.fromhex(digest_hex))
                    for ordinal, first, last, count, digest_hex in directory
                ),
            )
    encoded_blocks = canonical(sorted(value.hex() for value in blocks))
    encoded_revisions = canonical(sorted(value.hex() for value in revisions))
    connection.execute(
        "DELETE FROM settlement_freeze_block WHERE lower(hex(digest)) NOT IN "
        "(SELECT value FROM json_each(?))", (encoded_blocks,),
    )
    connection.execute(
        "DELETE FROM settlement_state_revision WHERE lower(hex(digest)) NOT IN "
        "(SELECT value FROM json_each(?))", (encoded_revisions,),
    )
    if _frozen_difference(connection, prepared, blocks, revisions):
        _fail("repair_did_not_rebuild_projection", -1)
    return {"changed": True, "periods": len(prepared)}
