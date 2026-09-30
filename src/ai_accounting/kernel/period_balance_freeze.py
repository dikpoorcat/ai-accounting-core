"""Bounded, close-rooted v1 period balance read projection.

The publication chain and stored calculation outcomes remain authoritative.
Rows here are repairable copies; a close's private derived root commits their
entire bucket directory and is never reconstructed from the rows being read.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass

from .contracts import KernelError
from .types import canonical, checked, digest

DERIVED_ROOT_NAME = "period_balance"
CONTRACT = "ai-accounting-kernel/2/period-balance-freeze/1"

BALANCE_FREEZE_DDL = """
CREATE TABLE period_balance_freeze_root(
  period INTEGER PRIMARY KEY REFERENCES period_close(period),
  close_digest BLOB NOT NULL CHECK(length(close_digest)=32),
  publication_highwater INTEGER NOT NULL,
  payload TEXT NOT NULL,
  digest BLOB NOT NULL CHECK(length(digest)=32)
) STRICT;
CREATE TABLE period_balance_freeze_bucket(
  period INTEGER NOT NULL REFERENCES period_balance_freeze_root(period),
  category TEXT NOT NULL,
  bucket INTEGER NOT NULL CHECK(bucket BETWEEN 0 AND 255),
  row_count INTEGER NOT NULL CHECK(row_count>0),
  digest BLOB NOT NULL CHECK(length(digest)=32),
  PRIMARY KEY(period,category,bucket)
) STRICT;
CREATE TABLE period_balance_freeze_row(
  period INTEGER NOT NULL REFERENCES period_balance_freeze_root(period),
  category TEXT NOT NULL,
  balance_key TEXT NOT NULL,
  bucket INTEGER NOT NULL CHECK(bucket BETWEEN 0 AND 255),
  ending INTEGER NOT NULL,
  activity INTEGER NOT NULL,
  ending_present INTEGER NOT NULL CHECK(ending_present IN (0,1)),
  activity_present INTEGER NOT NULL CHECK(activity_present IN (0,1)),
  PRIMARY KEY(period,category,balance_key)
) STRICT;
CREATE INDEX period_balance_freeze_row_bucket
  ON period_balance_freeze_row(period,category,bucket,balance_key);
"""


@dataclass(frozen=True)
class PreparedBalanceFreeze:
    period: int
    close_digest: bytes
    publication_highwater: int
    payload: str
    root_digest: bytes
    buckets: tuple[tuple, ...]
    rows: tuple[tuple, ...]


def _error(reason: str, period: int) -> None:
    raise KernelError(
        "content_integrity_failed",
        "关账期间余额冻结汇总与来源不一致",
        component="period_balance_freeze",
        record_id=str(period),
        reason=reason,
    )


def _bucket(key: str) -> int:
    return hashlib.sha256(key.encode("utf-8")).digest()[0]


def _wire(row: tuple) -> list:
    return [row[1], row[2], row[4], row[5], row[6], row[7]]


def _asof_segments(connection, highwater: int):
    """Replay every state transition through the exact close sequence."""
    from .publication import verify_record

    active = {}
    for row in connection.execute(
        "SELECT * FROM calculation_publication WHERE sequence<=? ORDER BY sequence",
        (highwater,),
    ):
        verify_record(row)
        _apply_publication(active, row)
    return [row for segments in active.values() for row in segments]


def _apply_publication(active, row):
    segments = active.setdefault(row["subject_id"], [])
    mode = row["mode"]
    if mode in ("initial", "closed_correction"):
        segments.append(row)
    elif mode in ("open_replace", "review_no_impact"):
        if not segments:
            _error("missing_prior_segment", row["posting_period"])
        segments[-1] = row
    elif mode == "withdrawn":
        if not segments:
            _error("missing_withdrawn_segment", row["posting_period"])
        segments.pop()
    else:
        _error("unknown_publication_mode", row["posting_period"])


def _source_rows_from_segments(connection, segments, period: int, *, outcomes=None) -> list[tuple]:
    segments = [
        row for row in segments if row["posting_period"] <= period
    ]
    identifiers = {
        row[field]
        for row in segments
        for field in ("calculation_id", "baseline_calculation_id")
        if row[field]
    }
    outcomes = {} if outcomes is None else outcomes
    missing = identifiers - outcomes.keys()
    if missing:
        verified = {}
        for row in connection.execute(
            "SELECT id,outcome,digest FROM calculation "
            "WHERE id IN (SELECT value FROM json_each(?))",
            (canonical(sorted(missing)),),
        ):
            value = json.loads(row["outcome"])
            if digest(value) != row["digest"]:
                _error("source_digest", period)
            verified[row["id"]] = (
                "opening" if value.get("opening") else "activity",
                tuple(
                    (effect["category"], effect["key"], effect["amount"])
                    for effect in value["balances"]
                ),
                row["digest"],
            )
        if verified.keys() != missing:
            _error("missing_calculation", period)
        outcomes.update(verified)
    if not identifiers <= outcomes.keys():
        _error("missing_calculation", period)
    result = []
    for segment in segments:
        calculation_id = segment["calculation_id"]
        baseline_id = segment["baseline_calculation_id"]
        totals = defaultdict(int)
        for ident, sign in ((calculation_id, 1), (baseline_id, -1)):
            if ident is None:
                continue
            component, effects, _ = outcomes[ident]
            for category, key, amount in effects:
                key = category, key, component
                totals[key] = checked(totals[key] + sign * amount)
        for (category, key, component), amount in sorted(totals.items()):
            if amount:
                result.append(
                    (
                        segment["id"],
                        segment["posting_period"],
                        category,
                        key,
                        component,
                        amount,
                        calculation_id,
                        outcomes[calculation_id][2],
                        baseline_id,
                        outcomes[baseline_id][2] if baseline_id else None,
                    )
                )
    return sorted(result)


def _source_rows(connection, period: int, highwater: int) -> list[tuple]:
    return _source_rows_from_segments(connection, _asof_segments(connection, highwater), period)


def _projection_rows(connection, period: int) -> list[tuple]:
    from .period_balances import COLUMNS

    return [
        tuple(row)
        for row in connection.execute(
            "SELECT " + ",".join(COLUMNS) + " FROM period_balance "
            "WHERE posting_period<=? ORDER BY publication_id,category,balance_key,component",
            (period,),
        )
    ]


def prepare_balance_freeze(
    connection,
    period: int,
    logical_close_digest: bytes,
    publication_highwater: int,
    *,
    check_projection: bool = True,
) -> PreparedBalanceFreeze:
    """Rebuild one close's cumulative and current activity from as-of authority."""
    if len(logical_close_digest) != 32 or publication_highwater < 0:
        raise ValueError("invalid close identity for balance freeze")
    if (
        check_projection
        and connection.execute(
            "SELECT coalesce(max(sequence),0) FROM calculation_publication"
        ).fetchone()[0]
        != publication_highwater
    ):
        _error("publication_highwater_changed", period)
    source = _source_rows(connection, period, publication_highwater)
    if check_projection and _projection_rows(connection, period) != source:
        _error("period_balance_projection_mismatch", period)
    return _prepare_from_source_rows(period, logical_close_digest, publication_highwater, source)


def _prepare_from_source_rows(
    period: int,
    logical_close_digest: bytes,
    publication_highwater: int,
    source: list[tuple],
) -> PreparedBalanceFreeze:
    """Build one close independently from verified immutable source rows."""
    summaries = {}
    for row in source:
        _, posting_period, category, key, component, amount, *_ = row
        summary = summaries.setdefault((category, key), [0, 0, False, False])
        summary[0] = checked(summary[0] + amount)
        summary[2] = True
        if posting_period == period and component == "activity":
            summary[1] = checked(summary[1] + amount)
            summary[3] = True
    rows = tuple(
        (
            period,
            category,
            key,
            _bucket(key),
            ending,
            activity,
            int(ending_present),
            int(activity_present),
        )
        for (category, key), (ending, activity, ending_present, activity_present) in sorted(
            summaries.items()
        )
    )
    grouped = defaultdict(list)
    for row in rows:
        grouped[(row[1], row[3])].append(row)
    buckets = tuple(
        (period, category, bucket, len(values), digest([_wire(row) for row in values]))
        for (category, bucket), values in sorted(grouped.items())
    )
    root = {
        "contract": CONTRACT,
        "period": period,
        "close_digest": logical_close_digest.hex(),
        "publication_highwater": publication_highwater,
        "buckets": [
            [category, bucket, count, checksum.hex()]
            for _, category, bucket, count, checksum in buckets
        ],
    }
    payload = canonical(root)
    return PreparedBalanceFreeze(
        period,
        logical_close_digest,
        publication_highwater,
        payload,
        hashlib.sha256(payload.encode("utf-8")).digest(),
        buckets,
        rows,
    )


def _require_prepared_consistency(prepared: PreparedBalanceFreeze) -> None:
    """Reject an internally inconsistent candidate before any derived write."""
    period = prepared.period
    if hashlib.sha256(prepared.payload.encode("utf-8")).digest() != prepared.root_digest:
        _error("prepared_root_digest", period)
    try:
        root = json.loads(prepared.payload)
    except (TypeError, ValueError):
        _error("prepared_root_json", period)
    if (
        not isinstance(root, dict)
        or set(root) != {
            "contract", "period", "close_digest", "publication_highwater", "buckets"
        }
        or canonical(root) != prepared.payload
        or root["contract"] != CONTRACT
        or root["period"] != period
        or root["close_digest"] != prepared.close_digest.hex()
        or root["publication_highwater"] != prepared.publication_highwater
    ):
        _error("prepared_root_identity", period)
    rows = prepared.rows
    if rows != tuple(sorted(rows, key=lambda row: (row[1], row[2]))):
        _error("prepared_row_order", period)
    grouped = defaultdict(list)
    seen = set()
    for row in rows:
        if (
            len(row) != 8 or row[0] != period or type(row[1]) is not str
            or type(row[2]) is not str or row[3] != _bucket(row[2])
            or type(row[4]) is not int or type(row[5]) is not int
            or row[6] not in (0, 1) or row[7] not in (0, 1)
            or (row[1], row[2]) in seen
        ):
            _error("prepared_row_identity", period)
        seen.add((row[1], row[2]))
        grouped[(row[1], row[3])].append(row)
    expected = tuple(
        (period, category, bucket, len(values), digest([_wire(row) for row in values]))
        for (category, bucket), values in sorted(grouped.items())
    )
    if prepared.buckets != expected or root["buckets"] != [
        [category, bucket, count, checksum.hex()]
        for _, category, bucket, count, checksum in expected
    ]:
        _error("prepared_bucket_mismatch", period)


def persist_balance_freeze(connection, prepared: PreparedBalanceFreeze) -> None:
    """Persist repairable rows after the close stores their committed root."""
    from .close_storage import derived_root, verified_header

    _require_prepared_consistency(prepared)
    close = connection.execute(
        "SELECT * FROM period_close WHERE period=?", (prepared.period,)
    ).fetchone()
    if close is None:
        _error("missing_close", prepared.period)
    header = verified_header(connection, close)
    if (
        header.logical_digest != prepared.close_digest
        or header.root["small"]["publication_sequence"] != prepared.publication_highwater
        or derived_root(header, DERIVED_ROOT_NAME) != prepared.root_digest
    ):
        _error("close_root_mismatch", prepared.period)
    connection.execute(
        "INSERT INTO period_balance_freeze_root VALUES(?,?,?,?,?)",
        (
            prepared.period,
            prepared.close_digest,
            prepared.publication_highwater,
            prepared.payload,
            prepared.root_digest,
        ),
    )
    connection.executemany(
        "INSERT INTO period_balance_freeze_bucket VALUES(?,?,?,?,?)", prepared.buckets
    )
    connection.executemany(
        "INSERT INTO period_balance_freeze_row VALUES(?,?,?,?,?,?,?,?)", prepared.rows
    )


def _verified_root(connection, header):
    from .close_storage import derived_root

    expected = derived_root(header, DERIVED_ROOT_NAME)
    row = connection.execute(
        "SELECT close_digest,publication_highwater,payload,digest "
        "FROM period_balance_freeze_root WHERE period=?",
        (header.period,),
    ).fetchone()
    if row is None or expected is None:
        _error("missing_root", header.period)
    if (
        bytes(row["close_digest"]) != header.logical_digest
        or row["publication_highwater"] != header.root["small"]["publication_sequence"]
        or bytes(row["digest"]) != expected
        or hashlib.sha256(row["payload"].encode("utf-8")).digest() != expected
    ):
        _error("root_digest_mismatch", header.period)
    try:
        root = json.loads(row["payload"])
    except (TypeError, ValueError) as exc:
        raise KernelError(
            "content_integrity_failed",
            "期间余额冻结根格式无效",
            component="period_balance_freeze",
            record_id=str(header.period),
            reason="invalid_root_json",
        ) from exc
    if (
        not isinstance(root, dict)
        or set(root) != {
            "contract", "period", "close_digest", "publication_highwater", "buckets"
        }
        or canonical(root) != row["payload"]
        or root.get("contract") != CONTRACT
        or root.get("period") != header.period
        or root.get("close_digest") != header.logical_digest.hex()
        or root.get("publication_highwater") != row["publication_highwater"]
        or not isinstance(root.get("buckets"), list)
    ):
        _error("root_identity_mismatch", header.period)
    return root


def read_frozen_balances(connection, header, category=None, keys=None):
    """Verify the committed directory and only the requested complete buckets."""
    root = _verified_root(connection, header)
    requested = (
        None if category is None else set(category) if isinstance(category, tuple) else {category}
    )
    selected = None if keys is None else set(keys)
    directory = {}
    for entry in root["buckets"]:
        if (
            not isinstance(entry, list)
            or len(entry) != 4
            or type(entry[0]) is not str
            or type(entry[1]) is not int
            or type(entry[2]) is not int
            or type(entry[3]) is not str
            or not 0 <= entry[1] <= 255
            or entry[2] <= 0
            or len(entry[3]) != 64
            or any(char not in "0123456789abcdef" for char in entry[3])
        ):
            _error("invalid_bucket_directory", header.period)
        key = (entry[0], entry[1])
        if key in directory:
            _error("duplicate_bucket_directory", header.period)
        directory[key] = (entry[2], entry[3])
    if root["buckets"] != [
        [cat, bucket, count, checksum]
        for (cat, bucket), (count, checksum) in sorted(directory.items())
    ]:
        _error("unordered_bucket_directory", header.period)
    target_buckets = (
        {
            (cat, _bucket(key))
            for cat, _ in directory
            for key in selected
            if requested is None or cat in requested
        }
        if selected is not None
        else {key for key in directory if requested is None or key[0] in requested}
    )
    if selected is not None and requested is not None:
        target_buckets.update((cat, _bucket(key)) for cat in requested for key in selected)
    output = []
    for cat, bucket in sorted(target_buckets):
        rows = [
            tuple(row)
            for row in connection.execute(
                "SELECT period,category,balance_key,bucket,ending,activity,"
                "ending_present,activity_present FROM period_balance_freeze_row "
                "WHERE period=? AND category=? AND bucket=? ORDER BY balance_key",
                (header.period, cat, bucket),
            )
        ]
        expected = directory.get((cat, bucket))
        if expected is None:
            if rows:
                _error("unexpected_bucket_rows", header.period)
            continue
        if (
            len(rows) != expected[0]
            or any(
                row[0] != header.period or row[1] != cat or row[3] != _bucket(row[2])
                for row in rows
            )
            or digest([_wire(row) for row in rows]).hex() != expected[1]
        ):
            _error("bucket_content_mismatch", header.period)
        index = connection.execute(
            "SELECT row_count,digest FROM period_balance_freeze_bucket "
            "WHERE period=? AND category=? AND bucket=?",
            (header.period, cat, bucket),
        ).fetchone()
        if (
            index is None
            or index["row_count"] != expected[0]
            or index["digest"].hex() != expected[1]
        ):
            _error("bucket_index_mismatch", header.period)
        output.extend(
            {
                "category": row[1],
                "key": row[2],
                "ending": row[4],
                "activity": row[5],
                "ending_present": bool(row[6]),
                "activity_present": bool(row[7]),
            }
            for row in rows
            if selected is None or row[2] in selected
        )
    return sorted(output, key=lambda row: (row["category"], row["key"]))


def compare_balance_freeze(connection, *, repair=False):
    """Independently rebuild all closes and compare every mutable projection row."""
    from .close_storage import derived_root, verified_header
    from .publication import verify_record

    changed = False
    periods = [
        row[0] for row in connection.execute("SELECT period FROM period_close ORDER BY period")
    ]
    publications = iter(
        connection.execute("SELECT * FROM calculation_publication ORDER BY sequence")
    )
    pending = next(publications, None)
    active = {}
    outcomes = {}
    last_sequence = 0
    for period in periods:
        close = connection.execute(
            "SELECT * FROM period_close WHERE period=?", (period,)
        ).fetchone()
        header = verified_header(connection, close)
        highwater = header.root["small"]["publication_sequence"]
        if not isinstance(highwater, int) or highwater < last_sequence:
            _error("publication_highwater_order", period)
        while pending is not None and pending["sequence"] <= highwater:
            verify_record(pending)
            _apply_publication(active, pending)
            last_sequence = pending["sequence"]
            pending = next(publications, None)
        if last_sequence != highwater:
            _error("publication_highwater_missing", period)
        source = _source_rows_from_segments(
            connection,
            (row for segments in active.values() for row in segments),
            period,
            outcomes=outcomes,
        )
        prepared = _prepare_from_source_rows(
            period, header.logical_digest, highwater, source
        )
        if derived_root(header, DERIVED_ROOT_NAME) != prepared.root_digest:
            _error("authoritative_root_mismatch", period)
        stored = connection.execute(
            "SELECT close_digest,publication_highwater,payload,digest "
            "FROM period_balance_freeze_root WHERE period=?",
            (period,),
        ).fetchone()
        buckets = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM period_balance_freeze_bucket WHERE period=? "
                "ORDER BY category,bucket",
                (period,),
            )
        )
        rows = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM period_balance_freeze_row WHERE period=? "
                "ORDER BY category,balance_key",
                (period,),
            )
        )
        differs = (
            stored is None
            or tuple(stored)
            != (
                prepared.close_digest,
                prepared.publication_highwater,
                prepared.payload,
                prepared.root_digest,
            )
            or buckets != prepared.buckets
            or rows != prepared.rows
        )
        if differs:
            changed = True
            if not repair:
                _error("projection_mismatch", period)
            connection.execute("DELETE FROM period_balance_freeze_row WHERE period=?", (period,))
            connection.execute("DELETE FROM period_balance_freeze_bucket WHERE period=?", (period,))
            connection.execute("DELETE FROM period_balance_freeze_root WHERE period=?", (period,))
            persist_balance_freeze(connection, prepared)
    return {"periods": len(periods), "changed": changed}
