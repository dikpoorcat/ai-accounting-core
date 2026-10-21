"""One full asset projection plus bounded convenience queries over that projection."""

from copy import deepcopy
from dataclasses import asdict

from ai_accounting_modular.contracts.public import ContractError, validate_contract

from ._common import total
from ._read_cards import cards, flow_state
from ._read_snapshot import AssetSnapshot
from ._read_support import identity, page_values
from ._read_types import (
    AssetActivity,
    AssetCardQuery,
    AssetLedgerAmounts,
    AssetProjectCost,
    AssetTypeSummary,
    ProjectCostPage,
    ProjectCostQuery,
    ProjectCostTotals,
)

_LEDGER_FIELDS = (
    "fixed_cost",
    "fixed_accumulated",
    "fixed_net",
    "intangible_cost",
    "intangible_accumulated",
    "intangible_net",
    "long_term_cost",
    "long_term_accumulated",
    "long_term_net",
)


def _ledger_amounts(financial):
    cells = {key: deepcopy(financial.values[key]) for key in _LEDGER_FIELDS}
    referenced = dict.fromkeys(
        index
        for cell in cells.values()
        if cell["state"] == "unestablished"
        for index in cell["issue_indexes"]
    )
    remapped = {old: new for new, old in enumerate(referenced)}
    issues = tuple(deepcopy(financial.issues[index]) for index in referenced)
    for cell in cells.values():
        if cell["state"] == "unestablished":
            cell["issue_indexes"] = [remapped[index] for index in cell["issue_indexes"]]
    return AssetLedgerAmounts(**cells, issues=issues)


def _sum_optional(values):
    values = tuple(values)
    return None if any(item is None for item in values) else total(values)


def _summary(kind, all_cards, flow):
    selected = [card for card in all_cards if card.asset_type == kind]
    active = [card for card in selected if card.state == "active"]
    pending = [card for card in selected if card.state == "pending"]
    selected_ids = {
        asset_id for asset_id, asset_type in flow["types"].items() if asset_type == kind
    }
    charge = (
        None
        if any(card.month_charge_fen is None for card in selected if card.state != "unestablished")
        else total(
            amount for (_, asset_type), amount in flow["charges"].items() if asset_type == kind
        )
    )
    return AssetTypeSummary(
        kind,
        len(selected),
        sum(card.state == "unestablished" for card in selected),
        len(active),
        total(card.cost_fen for card in active),
        _sum_optional(card.accumulated_fen for card in active),
        _sum_optional(card.net_fen for card in active),
        len(pending),
        total(card.cost_fen for card in pending),
        sum(card.state == "exited" for card in selected),
        len(flow["acquired"] & selected_ids),
        total(
            amount
            for (_, asset_type), amount in flow["acquired_amounts"].items()
            if asset_type == kind
        ),
        total(
            amount for (_, asset_type), amount in flow["adjustments"].items() if asset_type == kind
        ),
        len(flow["activated"] & selected_ids),
        len(flow["exited"] & selected_ids),
        charge,
    )


def project_totals(items):
    return ProjectCostTotals(
        len(items),
        total(item.cost_fen for item in items),
        total(item.consumed_fen for item in items),
        total(item.remaining_fen for item in items),
    )


def _projects(snapshot):
    projects = []
    for position in snapshot.project_costs:
        definition = position.definition
        content = snapshot.mains.get(definition.reference["business_id"])
        if content is None or content.source != definition.source:
            raise ContractError("storage_corrupt", "项目成本缺少相同正式采用的来源结果。")
        data, kind = content.basis["input"], content.target["kind"]
        if kind not in {"assets.project_cost", "people.remuneration"}:
            raise ContractError("storage_corrupt", "项目成本不是已实现的资本化来源。")
        party = data.get("supplier_id", data.get("person_id"))
        projects.append(
            AssetProjectCost(
                definition.reference,
                definition.source,
                snapshot.require_object(definition.value["object_id"], "project"),
                snapshot.require_object(party) if party is not None else None,
                definition.value["business_period"],
                kind,
                data["type"] if kind == "people.remuneration" else "project_cost",
                data.get("project_nature", data.get("destination", {}).get("project_nature")),
                position.amounts.amount_fen,
                position.amounts.consumed_fen,
                position.amounts.remaining_fen,
                snapshot.payment((definition.reference["business_id"],), "individual"),
            )
        )
    return tuple(
        sorted(
            projects,
            key=lambda item: (
                snapshot.project_order[item.project["object_id"]],
                item.period,
                item.reference["business_id"],
                item.reference["slot"],
            ),
        )
    )


def asset_activity(owner, scope, read):
    if owner._ledger is None or owner._identity is None:
        raise ContractError("unsupported_business", "资产读取需要接入正式账务及对象模块。")
    snapshot = AssetSnapshot(owner, scope, read)
    flow = flow_state(snapshot)
    selected_cards, projects = cards(snapshot, flow), _projects(snapshot)
    fixed, intangible = (_summary(kind, selected_cards, flow) for kind in ("fixed", "intangible"))
    ledger = _ledger_amounts(owner._ledger.financial_balances(snapshot.scope, _LEDGER_FIELDS, read))
    payload = {
        "scope": snapshot.scope,
        "cards": selected_cards,
        "project_costs": projects,
        "fixed": fixed,
        "intangible": intangible,
        "project_totals": project_totals(projects),
        "ledger": ledger,
        "card_issues": tuple(issue for card in selected_cards for issue in card.issues),
    }
    version = identity(
        read,
        "asset_activity",
        payload,
    )
    if scope.get("expected_head", version) != version:
        raise ContractError("snapshot_changed", "资产范围已变化，请重新读取。")
    return AssetActivity(
        version=version,
        **payload,
        inputs=snapshot.read_inputs().merge(owner._ledger.financial_summary_inputs(_LEDGER_FIELDS)),
    )


def query_cards(owner, scope, query, page, read):
    if (
        not isinstance(query, AssetCardQuery)
        or not isinstance(query.filter, str)
        or query.filter
        not in {
            "all",
            "active",
            "fixed",
            "intangible",
            "pending",
            "exited",
        }
    ):
        raise ContractError("invalid_input", "资产卡片须使用有效 AssetCardQuery。")
    if query.asset_id is not None:
        validate_contract("core.schema.json", "Id", query.asset_id)
    activity = asset_activity(owner, scope, read)
    filtered = tuple(
        card
        for card in activity.cards
        if (
            (query.asset_id is None or card.asset_id == query.asset_id)
            and (
                query.filter == "all"
                or card.asset_type == query.filter
                or card.state == query.filter
            )
        )
    )
    version = identity(read, "asset_cards", {"activity": activity.version, "query": asdict(query)})
    return page_values(version, activity.cards, filtered, page)


def query_project_costs(owner, scope, query, page, read):
    if not isinstance(query, ProjectCostQuery) or type(query.include_consumed) is not bool:
        raise ContractError("invalid_input", "项目成本须使用有效 ProjectCostQuery。")
    if query.project_id is not None:
        validate_contract("core.schema.json", "Id", query.project_id)
    activity = asset_activity(owner, scope, read)
    filtered = tuple(
        item
        for item in activity.project_costs
        if (
            (query.project_id is None or item.project["object_id"] == query.project_id)
            and (query.include_consumed or item.remaining_fen != 0)
        )
    )
    version = identity(
        read, "asset_projects", {"activity": activity.version, "query": asdict(query)}
    )
    return ProjectCostPage(
        page_values(version, activity.project_costs, filtered, page),
        activity.project_totals,
        project_totals(filtered),
    )
