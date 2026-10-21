"""Read adopted card quantities and their precise closing consumption result."""

from collections import defaultdict

from ai_accounting_modular.contracts.public import ContractError, parse_fen

from ._common import total
from ._read_types import AssetCard, AssetExit


def target_assets(target):
    if target is not None and target["kind"] in {"assets.acquisition", "assets.activation"}:
        return {item["asset_id"]: item for item in target["values"]["assets"]}
    return {}


def flow_state(snapshot):
    acquired, activated, exited = set(), set(), set()
    acquired_amounts, adjustments, charges = defaultdict(list), defaultdict(list), defaultdict(list)
    types = {asset_id: card["asset_type"] for asset_id, (_, card) in snapshot.origins.items()}
    for obj in snapshot.asset_objects:
        if obj["data"].get("asset_type") is not None:
            types.setdefault(obj["object_id"], obj["data"]["asset_type"])
    snapshot.load_refs(
        reference
        for item in snapshot.flows
        if item.business_kind == "assets.consumption"
        for reference in (item.reference, item.baseline_reference)
        if reference is not None
    )
    for item in snapshot.flows:
        current, baseline = target_assets(item.target), target_assets(item.baseline_target)
        types.update((asset_id, card["asset_type"]) for asset_id, card in baseline.items())
        types.update((asset_id, card["asset_type"]) for asset_id, card in current.items())
        if item.business_kind == "assets.acquisition":
            for asset_id in current.keys() | baseline.keys():
                now = parse_fen(current[asset_id]["cost_fen"]) if asset_id in current else 0
                old = parse_fen(baseline[asset_id]["cost_fen"]) if asset_id in baseline else 0
                if (
                    asset_id in current
                    and asset_id not in baseline
                    and (
                        item.baseline_target is None
                        or item.baseline_target["kind"] != "system.withdrawal"
                    )
                ):
                    acquired.add(asset_id)
                    acquired_amounts[asset_id, current[asset_id]["asset_type"]].append(now)
                else:
                    if asset_id in current:
                        adjustments[asset_id, current[asset_id]["asset_type"]].append(now)
                    if asset_id in baseline:
                        adjustments[asset_id, baseline[asset_id]["asset_type"]].append(-old)
        elif item.business_kind == "assets.activation":
            activated.update(current.keys() - baseline.keys())
        elif item.business_kind == "assets.disposal":
            if item.target["kind"] == "assets.disposal" and (
                item.baseline_target is None or item.baseline_target["kind"] != "assets.disposal"
            ):
                exited.add(item.target["values"]["asset_id"])
        elif item.business_kind == "assets.consumption":
            for sign, target, reference in (
                (1, item.target, item.reference),
                (-1, item.baseline_target, item.baseline_reference),
            ):
                if target is not None and target["kind"] == "assets.consumption":
                    adopted = {
                        card["asset_id"]: card["asset_type"]
                        for card in snapshot.contents[reference["result_id"]].basis["adopted_cards"]
                    }
                    for card in target["values"]["items"]:
                        asset_id = card["asset_id"]
                        if asset_id not in adopted:
                            raise ContractError(
                                "storage_corrupt", "正式摊折发生额缺少所采用的卡片类别。"
                            )
                        charges[asset_id, adopted[asset_id]].append(
                            sign * parse_fen(card["amount_fen"])
                        )
                        types.setdefault(asset_id, adopted[asset_id])
    typed_charges = {key: total(parts) for key, parts in charges.items()}
    card_charges = defaultdict(list)
    for (asset_id, _), amount in typed_charges.items():
        card_charges[asset_id].append(amount)
    return {
        "acquired": acquired,
        "activated": activated,
        "exited": exited,
        "types": types,
        "acquired_amounts": {key: total(parts) for key, parts in acquired_amounts.items()},
        "adjustments": {key: total(parts) for key, parts in adjustments.items()},
        "charges": typed_charges,
        "card_charges": {key: total(parts) for key, parts in card_charges.items()},
    }


def _exit(snapshot, asset_id):
    content = snapshot.exits.get(asset_id)
    if content is None:
        return None
    data, amounts = content.basis["input"], content.target["values"]
    return AssetExit(
        content.reference,
        content.reference["business_period"],
        content.basis["effective_on"],
        data["type"],
        *(
            parse_fen(amounts[key])
            for key in (
                "cost_fen",
                "accumulated_fen",
                "carrying_fen",
                "gross_proceeds_fen",
                "net_proceeds_fen",
                "vat_fen",
                "gain_loss_fen",
            )
        ),
        snapshot.require_object(data["buyer_id"]) if "buyer_id" in data else None,
    )


def _missing_consumption(content, period):
    return {
        "code": "formal_asset_consumption_missing",
        "business_id": content.reference["business_id"],
        "field": "/results/depreciation:" + period,
        "meaning": "正式资产摊折结果",
        "message": "尚无该资产截至所选月份应有的正式摊折结果。",
        "allowed_precision": ["month"],
        "reusable_sources": [],
        "blocking_scope": "none",
    }


def cards(snapshot, flow):
    result = []
    for obj in snapshot.asset_objects:
        asset_id = obj["object_id"]
        origin = snapshot.origins.get(asset_id)
        if origin is None:
            result.append(
                AssetCard(
                    asset_id=asset_id,
                    object=obj,
                    asset_type=obj["data"].get("asset_type"),
                    state="unestablished",
                    origin=None,
                    activation=None,
                    consumption=None,
                    acquired_period=None,
                    acquired_on=None,
                    in_use_period=None,
                    in_use_on=None,
                    card=None,
                    cost_fen=None,
                    accumulated_fen=None,
                    net_fen=None,
                    month_charge_fen=None,
                    completed_months=None,
                    month_acquired=False,
                    month_activated=False,
                    month_exited=False,
                    payment=snapshot.payment((), "unestablished"),
                    exit=None,
                )
            )
            continue
        content, original = origin
        opening = content.target["kind"] == "opening.package"
        acquired_period = None if opening else content.reference["business_period"]
        acquired_on = None if opening else content.basis.get("effective_on")
        activation = snapshot.activations.get(asset_id)
        active_content, card = activation if activation is not None else (None, None)
        in_use_period, in_use_on = None, None
        if opening:
            member = snapshot.input_cards(content)[asset_id]
            in_use_period, in_use_on = member["in_use_period"], member.get("in_use_on")
        elif activation is not None:
            item = snapshot.input_cards(active_content)[asset_id]
            in_use_period, in_use_on = (
                active_content.reference["business_period"],
                item.get("in_use_on"),
            )
        cost = parse_fen(original["cost_fen"])
        accumulated = parse_fen(card["accumulated_fen"]) if card is not None else 0
        completed = card["completed_months"] if card is not None else None
        consumption, issues = snapshot.consumptions.get(asset_id), []
        if asset_id in snapshot.consumptions:
            if consumption is None or consumption.target["kind"] != "assets.consumption":
                issues.append(
                    _missing_consumption(active_content, snapshot.consumption_periods[asset_id])
                )
                accumulated, completed = None, None
            else:
                actual = snapshot.consumption_items(consumption).get(asset_id)
                if actual is None:
                    raise ContractError("storage_corrupt", "正式摊折终点缺少所声明的资产卡片。")
                accumulated, completed = (
                    parse_fen(actual["closing_accumulated_fen"]),
                    actual["completed_months"],
                )
        exit = _exit(snapshot, asset_id)
        state = "exited" if exit is not None else "active" if card is not None else "pending"
        net = 0 if exit is not None else None if accumulated is None else cost - accumulated
        data = content.basis["input"]
        payment_scope = (
            "opening"
            if opening
            else "batch"
            if data["type"] == "reimbursed_batch"
            else "project_sources"
            if data["type"] == "project_completion"
            else "individual"
        )
        result.append(
            AssetCard(
                asset_id=asset_id,
                object=obj,
                asset_type=original["asset_type"],
                state=state,
                origin=content.reference,
                activation=active_content.reference if active_content else None,
                consumption=consumption.reference if consumption else None,
                acquired_period=acquired_period,
                acquired_on=acquired_on,
                in_use_period=in_use_period,
                in_use_on=in_use_on,
                card=card,
                cost_fen=cost,
                accumulated_fen=accumulated,
                net_fen=net,
                month_charge_fen=flow["card_charges"].get(asset_id, 0) if not issues else None,
                completed_months=completed,
                month_acquired=asset_id in flow["acquired"],
                month_activated=asset_id in flow["activated"],
                month_exited=asset_id in flow["exited"],
                payment=snapshot.payment(snapshot.payment_sources[asset_id], payment_scope),
                exit=exit,
                issues=tuple(issues),
            )
        )
    return tuple(result)
