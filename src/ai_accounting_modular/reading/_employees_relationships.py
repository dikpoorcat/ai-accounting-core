"""Project public relationship facts for display, without inferring employment."""

EMPLOYEE_STATES = {"regular", "unpaid_leave"}


def people(reader, scope, read):
    objects, cursor = [], None
    while True:
        query = {"action": "find", "company_id": read.company_id, "kind": "person", "limit": 500}
        if cursor is not None:
            query["cursor"] = cursor
        result = reader.identity.find(query, read)
        objects.extend(result["objects"])
        cursor = result["page"]["next_cursor"]
        if cursor is None:
            break
    ids = [obj["object_id"] for obj in objects]
    relationships = {}
    for offset in range(0, len(ids), 500):
        relationships.update(
            (item.person_id, item)
            for item in reader.people.relationships(
                ids[offset : offset + 500], scope["period_to"], read
            )
        )
    return objects, relationships


def known_role(relationship, month, *, labor=False):
    states = {"labor", "labor_ended"} if labor else {*EMPLOYEE_STATES, "departed"}
    return any(
        segment.state in states and segment.start[:7] <= month for segment in relationship.segments
    )


def fields(relationship, month, *, labor=False):
    return {
        "employment_state": relationship.period_end_state,
        "period_states": list(relationship.period_states),
        "possible_period_states": list(relationship.possible_period_states),
        "relationship_segments": [
            {"state": segment.state, "start": segment.start, "end": segment.end}
            for segment in relationship.segments
            if segment.start[:7] <= month and (segment.end is None or segment.end[:7] >= month)
        ],
        "in_period": relationship.labor_member if labor else relationship.employee_member,
    }


def employment_dates(relationship, month):
    prior = [segment for segment in relationship.segments if segment.start[:7] <= month]
    indexes = [index for index, segment in enumerate(prior) if segment.state in EMPLOYEE_STATES]
    if not indexes:
        departed = next(
            (segment.start for segment in reversed(prior) if segment.state == "departed"), None
        )
        return None, departed
    last = first = indexes[-1]
    while first and prior[first - 1].state in EMPLOYEE_STATES:
        first -= 1
    start = next(
        (segment.start for segment in prior[first : last + 1] if segment.relationship_start), None
    )
    end = prior[last + 1].start if last + 1 < len(prior) else None
    return start, end


def category(relationship, *, labor=False):
    member = relationship.labor_member if labor else relationship.employee_member
    states, end = relationship.period_states, relationship.period_end_state
    selected = {"all", "in_period" if member is True else "ended" if member is False else "unknown"}
    if labor:
        if member is True:
            selected.add("employment_active")
        if member is None:
            selected.add("employment_unknown")
        if member is False or "labor_ended" in states or (member is True and end != "labor"):
            selected.add("employment_departed")
    else:
        for state in ("regular", "unpaid_leave", "departed", "unknown"):
            if (
                state in states
                or (state == "departed" and end == state)
                or (state == "unknown" and member is None)
            ):
                selected.add("employment_active" if state == "regular" else "employment_" + state)
    return "|".join(sorted(selected))


def employee_order(relationship, month, name_order):
    start, _ = employment_dates(relationship, month)
    return (
        start[:7] if start else "9999-99",
        start is None or len(start) == 7,
        start or "",
        name_order,
    )
