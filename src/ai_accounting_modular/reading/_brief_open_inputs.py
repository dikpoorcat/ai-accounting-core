"""Exact dependency membership and current work are stored in BriefOpen's private index."""

from collections import Counter
from dataclasses import replace

from ai_accounting_modular.contracts.public import ReadInputs

from . import _brief_open_index as index
from ._collections import _decode

_IDENTITIES = ("business_ids", "current_business_ids", "current_fact_ids", "object_ids")
ORDINARY = ReadInputs(result_kinds=("opening.package",), all_obligations=True)


def business_ids(selected):
    return (
        set(selected.business_ids)
        | set(selected.current_business_ids)
        | set(selected.current_fact_ids)
    )


def static(inputs: ReadInputs):
    return replace(inputs, **{name: () for name in _IDENTITIES})


def records(captured, members, bindings):
    result = {}

    def record(business):
        return result.setdefault(
            business,
            {
                "business": True,
                "current": True,
                "fact": False,
                "members": [],
                "objects": set(),
                "vouchers": Counter(),
            },
        )

    displays = {}
    for body in captured.contents.values():
        record(body.reference["business_id"])
    for value in (*captured.selected, *captured.current.values()):
        party = value.party_id
        if party:
            record(value.reference["business_id"])["objects"].add(party)
    for member, binding in zip(members, bindings, strict=True):
        value = record(member["subject_id"])
        value["members"].append(member["id"])
        if member.get("_person_id"):
            value["objects"].add(member["_person_id"])
        position = member.get("_detail_scope", {}).get("position", {})
        value["objects"].update(
            position[key] for key in ("operator_id", "project_id") if position.get(key)
        )
        if binding is not None:
            display = binding["display"]
            ident = index.content_digest([binding["token"], index.content_digest(display)])
            if ident in displays and displays[ident] != display:
                index.corrupt("同一有效凭证的显示身份相互冲突。")
            displays[ident] = display
            value["vouchers"][ident] += 1
            for kind in ("parties", "assets"):
                value["objects"].update(display[kind])
    for value in result.values():
        value["objects"] = sorted(value["objects"])
        value["members"].sort()
        value["vouchers"] = dict(value["vouchers"])
    return result


def with_provider(inputs, records):
    records = {key: dict(value) for key, value in records.items()}
    for name, flag in (
        ("business_ids", "business"),
        ("current_business_ids", "current"),
        ("current_fact_ids", "fact"),
    ):
        for identity in getattr(inputs, name):
            value = records.setdefault(
                identity,
                {
                    "business": False,
                    "current": False,
                    "fact": False,
                    "members": [],
                    "objects": [],
                    "vouchers": {},
                },
            )
            value[flag] = True
    fixed = sorted(set(inputs.object_ids))
    if fixed:
        records["@fixed"] = {
            "business": False,
            "current": False,
            "fact": False,
            "members": [],
            "objects": fixed,
            "vouchers": {},
        }
    return records


def object_counts(records):
    return Counter(identity for value in records.values() for identity in value["objects"])


def voucher_counts(records):
    result = Counter()
    for value in records.values():
        result.update(value["vouchers"])
    return result


def affected(directory, month, impact):
    businesses = {
        identity
        for change in impact.formal_changes
        for identity in (change.business_id, *change.resource_business_ids)
    }
    businesses.update(identity for identity, _, _ in impact.facts)
    known = directory.get(index.BUSINESSES, businesses)
    if any(known.get(identity, {}).get("fact") for identity, _, _ in impact.facts):
        return True
    objects = {identity for identity, _ in impact.objects}
    if directory.get(index.OBJECTS, objects):
        return True
    for change in impact.formal_changes:
        identities = {change.business_id, *change.resource_business_ids}
        if any(known.get(identity, {}).get("current") for identity in identities):
            return True
        if any(period <= month for period in change.posted_periods) and any(
            known.get(identity, {}).get("business") for identity in identities
        ):
            return True
    return False


def mark(repository, scope_key, impact, head, selected, cooperation, directory, *, full=False):
    objects = (
        directory.get(index.OBJECTS, {identity for identity, _ in impact.objects})
        if directory
        else {}
    )
    facts = (
        directory.get(index.BUSINESSES, {identity for identity, _, _ in impact.facts})
        if directory
        else {}
    )
    full = full or bool(
        objects
        or any(kind in selected.object_kinds for _, kind in impact.objects)
        or any(
            (facts.get(identity, {}).get("fact") or kind in selected.fact_kinds)
            and not (identity in cooperation.current_fact_ids or kind in cooperation.fact_kinds)
            for identity, kind, _ in impact.facts
        )
        or impact.closure_ranges
        or any(
            kind == "opening.package"
            for change in impact.formal_changes
            for kind in change.result_kinds
        )
    )
    identities = {
        identity
        for change in impact.formal_changes
        for identity in (change.business_id, *change.resource_business_ids)
    }
    identities.update(identity for identity, _, _ in impact.facts)
    identities.update(value[9:] for value in impact.note_scopes if value in selected.note_scopes)
    repository.open_mark(scope_key, head, identities, full=full)


def pending(repository, scope_key):
    rows = repository.open_pending_rows(scope_key)
    values = [_decode(row) for row in rows]
    return any(value.get("full") for value in values), {
        value["business_id"] for value in values if "business_id" in value
    }
