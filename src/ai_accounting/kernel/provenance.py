"""Read exact source recording times from the finite, retained audit contracts.

These are system confirmation times, not business dates or the owner's earliest
knowledge. An unsupported or ambiguous historical association stays unknown.
"""

import json
from collections.abc import Iterable
from datetime import datetime
from sqlite3 import Connection

_ACTIONS = {
    "fact": ("confirm_fact", "confirm_facts", "recording_correction"),
    "display_profile": ("save_display_profile",),
    "payee": ("payee",),
    # This result only contains a revision number, shared by many subjects. It
    # cannot identify a management_revision row without inventing an association.
    "management": (),
}


def _positive_revision(value):
    return type(value) is int and value > 0


def _nonempty_string(value):
    return isinstance(value, str) and bool(value)


def _result(payload):
    if not isinstance(payload, dict):
        return None
    if set(payload) == {"result", "actor"}:
        return payload["result"] if isinstance(payload["actor"], dict) else None
    return payload


def _fact_reference(entry):
    if (
        isinstance(entry, dict)
        and set(entry) == {"status", "subject_id", "fact_id", "revision", "pending"}
        and entry["status"] == "confirmed"
        and _nonempty_string(entry["subject_id"])
        and _nonempty_string(entry["fact_id"])
        and _positive_revision(entry["revision"])
        and isinstance(entry["pending"], list)
        and all(_nonempty_string(item) for item in entry["pending"])
    ):
        return "fact", entry["fact_id"]
    return None


def _references(action, result):
    if not isinstance(result, dict):
        return ()
    if action in {"confirm_fact", "recording_correction"}:
        return (_fact_reference(result),)
    if action == "confirm_facts":
        if (
            set(result) == {"status", "results"}
            and result["status"] == "confirmed"
            and isinstance(result["results"], list)
        ):
            return tuple(_fact_reference(item) for item in result["results"])
        return ()
    if action == "save_display_profile":
        if (
            result.get("status") == "saved"
            and _nonempty_string(result.get("id"))
            and _nonempty_string(result.get("kind"))
            and result.get("kind")
            in {"employee", "counterparty", "fund_account", "asset", "business"}
            and _nonempty_string(result.get("entity_id"))
            and _positive_revision(result.get("revision"))
            and _nonempty_string(result.get("source"))
            and _nonempty_string(result.get("digest"))
        ):
            return (("display_profile", result["id"]),)
    if action == "payee":
        if (
            set(result) == {"status", "payee_revision_id", "party_id", "revision"}
            and result["status"] == "saved"
            and _nonempty_string(result["payee_revision_id"])
            and _nonempty_string(result["party_id"])
            and _positive_revision(result["revision"])
        ):
            return (("payee", result["payee_revision_id"]),)
    return ()


def _trusted_timestamp(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.tzinfo is not None and parsed.utcoffset() is not None
    except (ValueError, OverflowError):
        return False


def recorded_times(
    connection: Connection, references: Iterable[tuple[str, str]]
) -> dict[tuple[str, str], str]:
    """Resolve one batch of exact source IDs with at most one audit read.

    Missing keys mean unknown, including management records whose old audit result
    does not identify a source. Read all candidate confirmations: finding one in
    reverse order does not establish uniqueness, even if repeated timestamps agree.
    Callers should collect a request's sources and reuse this returned map.
    """
    requested = set(references)
    if any(kind not in _ACTIONS or not _nonempty_string(ident) for kind, ident in requested):
        raise ValueError("recorded times require supported source types and exact string IDs")
    actions = sorted({action for kind, _ in requested for action in _ACTIONS[kind]})
    if not actions:
        return {}
    candidates = {}
    from .read_indexes import audit_rows

    for row in audit_rows(connection, requested):
        try:
            result = _result(json.loads(row["payload"]))
        except (ValueError, TypeError):
            continue
        for reference in _references(row["action"], result):
            if reference in requested:
                # A second matching confirmation is ambiguous, including a
                # duplicate within one batch. Never pick its earliest/latest.
                if reference in candidates:
                    candidates[reference] = None
                else:
                    timestamp = row["created_at"]
                    candidates[reference] = timestamp if _trusted_timestamp(timestamp) else None
    return {reference: timestamp for reference, timestamp in candidates.items() if timestamp}
