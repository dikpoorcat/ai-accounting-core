"""Exact dependency membership and current work are stored in BriefOpen's private index."""

from collections import Counter
from dataclasses import replace

from ai_accounting_modular.contracts.public import ReadInputs, content_digest

from . import _brief_open_index as index
from . import _brief_open_tree as blocks
from ._collections import _decode

_IDENTITIES = (
    "business_ids",
    "current_business_ids",
    "current_fact_ids",
    "object_ids",
    "note_scopes",
)
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
                "notes": set(),
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
        if member.get("_note_scope"):
            value["notes"].add(member["_note_scope"])
        if member.get("_person_id"):
            value["objects"].add(member["_person_id"])
        position = member.get("_detail_scope", {}).get("position", {})
        value["objects"].update(
            position[key] for key in ("operator_id", "project_id") if position.get(key)
        )
        if binding is not None:
            display = binding["display"]
            ident = content_digest([binding["token"], content_digest(display)])
            if ident in displays and displays[ident] != display:
                index.corrupt("同一有效凭证的显示身份相互冲突。")
            displays[ident] = display
            value["vouchers"][ident] += 1
            for kind in ("parties", "assets"):
                value["objects"].update(display[kind])
    for value in result.values():
        value["objects"] = sorted(value["objects"])
        value["notes"] = sorted(value["notes"])
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
                    "notes": [],
                    "vouchers": {},
                },
            )
            value[flag] = True
    fixed = sorted(set(inputs.object_ids))
    if fixed or inputs.note_scopes:
        records["@fixed"] = {
            "business": False,
            "current": False,
            "fact": False,
            "members": [],
            "objects": fixed,
            "notes": list(inputs.note_scopes),
            "vouchers": {},
        }
    return records


def selected(records):
    return ReadInputs(
        business_ids=tuple(sorted(key for key, value in records.items() if value["business"])),
        current_business_ids=tuple(
            sorted(key for key, value in records.items() if value["current"])
        ),
        current_fact_ids=tuple(sorted(key for key, value in records.items() if value["fact"])),
        object_ids=tuple(sorted(object_counts(records))),
        note_scopes=tuple(sorted(note_counts(records))),
    )


def business_changes(before, after):
    added, removed = {}, {}
    for field, flag in (
        ("business_ids", "business"),
        ("current_business_ids", "current"),
        ("current_fact_ids", "fact"),
    ):
        old = {key for key, value in before.items() if value[flag]}
        new = {key for key, value in after.items() if value[flag]}
        added[field], removed[field] = tuple(sorted(new - old)), tuple(sorted(old - new))
    return ReadInputs(**added), ReadInputs(**removed)


def provider_items(inputs):
    for field, values in inputs.wire().items():
        if type(values) is bool:
            values = (True,) if values else ()
        for value in values:
            identity = blocks.key(field, "1" if value is True else value)
            yield index.item(index.PROVIDER, identity, {"field": field, "value": value})


def provider_inputs(entries):
    values = ReadInputs().wire()
    for identity, value in entries:
        if type(value) is not dict or set(value) != {"field", "value"}:
            index.corrupt()
        field, actual = value["field"], value["value"]
        if (
            type(field) is not str
            or field not in values
            or (type(actual) is not str and actual is not True)
            or identity != blocks.key(field, "1" if actual is True else actual)
        ):
            index.corrupt()
        if type(values[field]) is bool:
            if actual is not True or values[field]:
                index.corrupt()
            values[field] = True
        else:
            if type(actual) is not str or not actual or actual in values[field]:
                index.corrupt()
            values[field].append(actual)
    try:
        return ReadInputs.from_wire(values)
    except (TypeError, ValueError):
        index.corrupt()


def matching_provider(directory, month, impact):
    """Probe only provider subscriptions named by this notification."""
    from . import _subscription_keys

    selected = ReadInputs().wire()
    for field, identity, first, last, component in _subscription_keys.queries(impact):
        if field not in selected or month < first or (last is not None and month >= last):
            continue
        if component is not None and component != index.COMPONENT:
            continue
        if type(selected[field]) is bool:
            selected[field] = True
        else:
            selected[field].append(identity)
    return directory.provider_inputs(ReadInputs.from_wire(selected))


def object_counts(records):
    return Counter(identity for value in records.values() for identity in value["objects"])


def note_counts(records):
    return Counter(identity for value in records.values() for identity in value["notes"])


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
    if directory.get(index.NOTES, impact.note_scopes):
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
    notes = directory.get(index.NOTES, impact.note_scopes) if directory else {}
    identities.update(value[9:] for value in notes if value.startswith("business:"))
    repository.open_mark(scope_key, head, identities, full=full)


def pending(repository, scope_key):
    rows = repository.open_pending_rows(scope_key)
    values = [_decode(row) for row in rows]
    return any(value.get("full") for value in values), {
        value["business_id"] for value in values if "business_id" in value
    }
