"""Exact saved calculation identities from already checked outcome encodings."""

from __future__ import annotations

import hashlib


def calculation_identity_prefix(*, fact_id, outcome_encoding, reads, program, encode):
    """Encode only this source's invariant fields before its version candidates."""
    return "".join((
        '{"fact":', encode(fact_id),
        ',"outcome":', outcome_encoding,
        ',"program":', encode(program),
        ',"reads":', encode(reads),
        ',"versions":',
    ))


def calculation_identity_digest(
    *, fact_id, outcome_encoding, reads, program, versions, encode, prefix=None,
):
    """Keep the original five-field canonical bytes without encoding outcome twice.

    The caller supplies the encoding it just used to authenticate this outcome,
    and its original current or historical encoder for the remaining fields.
    This owns no cache, does not accept stored raw JSON, and checks no sources.
    """
    if prefix is None:
        prefix = calculation_identity_prefix(
            fact_id=fact_id, outcome_encoding=outcome_encoding,
            reads=reads, program=program, encode=encode,
        )
    raw = "".join((prefix, encode(versions), "}"))
    return hashlib.sha256(raw.encode("utf-8")).digest()
