"""Route changed public inputs to the projections which declared consuming them."""

from dataclasses import replace

from ai_accounting_modular.contracts.public import (
    ContractError,
    ReadInputs,
    loads_json,
)

from ._collections import prepare_model


def bind(model, inputs):
    if not isinstance(inputs, ReadInputs):
        raise TypeError("a projection must declare its public inputs")
    payload = loads_json(model.payload_json)
    payload["_inputs"] = inputs.wire()
    return replace(prepare_model(payload), rows=model.rows)


def inputs(payload):
    try:
        return ReadInputs.from_wire(payload["_inputs"])
    except (KeyError, TypeError, ValueError) as error:
        raise ContractError("storage_corrupt", "读取组件缺少完整的公开输入依赖。") from error


def affected(selected, month, impact):
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
