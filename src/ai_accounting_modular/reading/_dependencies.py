"""Route changed public inputs to the projections which declared consuming them."""

from dataclasses import replace

from ai_accounting_modular.contracts.public import (
    ReadInputs,
)


def bind(model, inputs):
    if not isinstance(inputs, ReadInputs):
        raise TypeError("a projection must declare its public inputs")
    return replace(model, inputs=inputs)


def difference(before, after):
    """Return only membership transitions, without carrying unchanged identities."""
    old, new = before.wire(), after.wire()
    added, removed = {}, {}
    for name, value in new.items():
        if type(value) is bool:
            added[name], removed[name] = value and not old[name], old[name] and not value
        else:
            added[name] = sorted(set(value) - set(old[name]))
            removed[name] = sorted(set(old[name]) - set(value))
    return ReadInputs.from_wire(added), ReadInputs.from_wire(removed)


def affected(selected, month, impact):
    if set(selected.note_scopes) & set(impact.note_scopes):
        return True
    objects = set(selected.object_ids)
    if any(
        object_id in objects or kind in selected.object_kinds for object_id, kind in impact.objects
    ):
        return True
    if any(
        business_id in selected.current_fact_ids or kind in selected.fact_kinds
        for business_id, kind, _ in impact.facts
    ):
        return True
    businesses = set(selected.business_ids)
    current = set(selected.current_business_ids)
    for change in impact.formal_changes:
        identities = {change.business_id, *change.resource_business_ids}
        if current & identities:
            return True
        if not any(period <= month for period in change.posted_periods):
            continue
        if (
            selected.all_results
            or (selected.posted_results and month in change.posted_periods)
            or (selected.all_obligations and change.obligation_categories)
            or businesses & identities
            or set(selected.result_kinds) & set(change.result_kinds)
            or (
                month in change.business_periods
                and set(selected.business_result_kinds) & set(change.result_kinds)
            )
            or (
                month in change.posted_periods
                and set(selected.posted_result_kinds) & set(change.result_kinds)
            )
            or set(selected.account_codes) & set(change.account_codes)
            or set(selected.obligation_categories) & set(change.obligation_categories)
            or set(selected.cost_categories) & set(change.cost_categories)
            or (selected.movements and month in change.movement_periods)
        ):
            return True
    return False
