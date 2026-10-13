"""Exact pre-event metadata, bound to a verified source or migration boundary.

Old profiles remain immutable source documents. This module never manufactures
workforce events; an explicit typed operation supplies every new event.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts import KernelError
from .types import ActualDate, YearMonth, canonical


class PreEventProfile(BaseModel):
    """The original source's shared profile grammar, for retained reads only."""

    model_config = ConfigDict(extra="forbid", strict=True)
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    display_number: str | None = Field(default=None, min_length=1, max_length=200)
    external_identifiers: dict[str, str] = Field(default_factory=dict)
    active: bool = True
    purpose: str | None = Field(default=None, max_length=50000)
    note: str | None = Field(default=None, max_length=50000)
    employment_start: YearMonth | ActualDate | None = None
    employment_end: YearMonth | ActualDate | None = None
    employment_status: Literal["active", "inactive", "unknown"] = "unknown"
    employment_state: Literal["regular", "unpaid_leave", "departed", "unknown"] = "unknown"
    category_label: str | None = None
    rights_description: str | None = None
    useful_life_basis: str | None = None

    @model_validator(mode="after")
    def valid_original_details(self):
        if any(not key.strip() or not value.strip() or len(value) > 500
               for key, value in self.external_identifiers.items()):
            raise ValueError("external identifiers require a name and an explicit value")
        if self.employment_start and self.employment_end:
            start, end = self.employment_start, self.employment_end
            if start[:7] > end[:7] or len(start) == len(end) == 10 and start > end:
                raise ValueError("employment end precedes start")
        return self


def retained_profile_ids(connection, histories):
    """Authenticate the old-format membership once for this selected read scope."""
    from .change_journal import _retained_coverage
    from .development_contracts import SOURCE24_FINGERPRINT
    from .runtime import _OwnedReadConnection
    from .storage import _active_fact_reads
    from .versions import fingerprint, objects

    profiles = [profile for history in histories for profile in history]
    if not profiles:
        return frozenset()
    active = _active_fact_reads.get()
    cache = (active._report_snapshot_cache
             if active is not None and active._snapshot_active
             and active.connection is connection
             and type(connection) is _OwnedReadConnection
             and connection._owned_read_guard is not None and connection.in_transaction else None)
    key = ("retained_entity_profile_membership",)
    if cache is not None and key in cache:
        return cache[key]
    if connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' "
        "AND name='source_change_retained_boundary'",
    ).fetchone():
        _through, records = _retained_coverage(connection)
        result = frozenset(reference for source, _target, reference, _revision in records
                           if source == "entity_profile")
    else:
        # No loose shape or JSON marker can assert an old grammar. Source reads
        # compare the complete actual SQL with the fixed package source digest.
        result = (frozenset(row[0] for row in connection.execute(
                    "SELECT id FROM entity_profile_revision"))
                  if fingerprint(objects(connection)).hex() == SOURCE24_FINGERPRINT
                  else frozenset())
    if cache is not None:
        cache[key] = result
    return result


def verify_retained_profile(profile):
    from .entities import Entities

    try:
        return PreEventProfile.model_validate_json(canonical(Entities._content(profile)))
    except ValueError as exc:
        raise KernelError(
            "entity_profile_corrupt", "历史对象档案不符合原格式", profile_id=profile["id"],
        ) from exc


def current_person_metadata(connection, profile, *, history):
    """Carry supported metadata after proving this exact immutable original."""
    from .entities import Entities, PersonProfile

    content = Entities._content(profile)
    if profile["id"] not in retained_profile_ids(connection, [history]):
        return content
    original = verify_retained_profile(profile).model_dump(mode="json")
    # A new event is supplied by the typed caller; no old employment state is
    # smuggled into it, and no source field is deleted from the retained record.
    return {key: value for key, value in original.items() if key in PersonProfile.model_fields}
