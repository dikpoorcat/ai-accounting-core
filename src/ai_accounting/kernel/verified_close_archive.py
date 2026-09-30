"""Bounded, transaction-local reuse of fully verified close bodies.

The archive is made only after the integrity checker has authenticated every
logical close. Its compressed bytes are temporary process memory, not another
stored authority. Consumers open one month's body at a time; they must never
materialize the complete archive into a dict of decoded manifests.
"""

from __future__ import annotations

import json
import zlib
from collections.abc import Mapping

from .contracts import KernelError
from .types import canonical
from .verified_source_lease import current_verified_lease, require_verified_lease


def pack_verified_close(manifest):
    return zlib.compress(canonical(manifest).encode("utf-8"), level=1)


def unpack_verified_close(content):
    try:
        return json.loads(zlib.decompress(content))
    except (ValueError, zlib.error) as error:
        raise ValueError("verified close memory image is invalid") from error


class _CloseLookup(Mapping):
    def __init__(self, pairs, connection, lease):
        self._pairs = pairs
        self._connection = connection
        self._lease = lease

    def _require_scope(self):
        try:
            require_verified_lease(self._connection, self._lease)
        except ValueError as error:
            raise KernelError("content_integrity_failed", "已核验关账来源不属于当前事务") from error

    def __getitem__(self, period):
        self._require_scope()
        if isinstance(period, str):
            try:
                ordinal = int(period)
            except ValueError as error:
                raise KeyError(period) from error
            if str(ordinal) != period:
                raise KeyError(period)
            period = ordinal
        row, packed = self._pairs[period]
        return row, unpack_verified_close(packed)

    def __iter__(self):
        self._require_scope()
        return iter(self._pairs)

    def __len__(self):
        self._require_scope()
        return len(self._pairs)


class VerifiedCloseArchive:
    """Complete close proof tied to the original SQLite transaction."""

    def __init__(self, connection, pairs):
        self.connection = connection
        self._pairs = {row["period"]: (row, packed) for row, packed in pairs}
        if len(self._pairs) != len(pairs):
            raise ValueError("duplicate verified close period")
        self._lease = current_verified_lease(connection)

    def __len__(self):
        return len(self._pairs)

    def lookup(self, connection, rows):
        try:
            require_verified_lease(connection, self._lease)
        except ValueError as error:
            raise KernelError("content_integrity_failed", "已核验关账来源不属于当前事务") from error
        actual = {row["period"]: row for row in rows}
        if (
            len(actual) != len(rows)
            or actual.keys() != self._pairs.keys()
            or any(
                tuple(row.keys()) != tuple(self._pairs[period][0].keys())
                or tuple(row[key] for key in row.keys())
                != tuple(self._pairs[period][0][key] for key in row.keys())
                for period, row in actual.items()
            )
        ):
            raise KernelError("content_integrity_failed", "已核验关账集合与权威来源不一致")
        return _CloseLookup(self._pairs, connection, self._lease)
