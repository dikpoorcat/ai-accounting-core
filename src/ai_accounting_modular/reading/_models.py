"""Capture in a read snapshot, then publish only the still-wanted component."""

from copy import deepcopy

from ai_accounting_modular.contracts.public import (
    ContractError,
    ReadInputs,
    canonical_json,
    content_digest,
    loads_json,
    validate_contract,
)

from . import _dependencies, _monthly
from ._collections import prepare_model
from ._types import ModelDelivery, ReadingImpact

FORMAT_VERSION = 2
FINANCIAL = "financial"


def normalize(scope):
    validate_contract("core.schema.json", "LedgerScope", scope)
    first, last = scope["period_from"], scope["period_to"]
    if (
        scope["view"] != "as_posted"
        or "object_ids" in scope
        or scope.get("as_of", last) != last
        or first[:4] != last[:4]
        or int(first[5:]) not in (1, 4, 7, 10)
        or int(last[5:]) != int(first[5:]) + 2
    ):
        raise ContractError("invalid_input", "季度读取须指定整家公司、自然季度及对应采用截止月。")
    return {key: value for key, value in scope.items() if key != "expected_head"} | {"as_of": last}


def key(scope):
    return canonical_json({"page": "quarterly_report", "scope": scope})


def semantic(snapshot):
    # The company commit counter is not this report's content or version.
    value = deepcopy(snapshot)
    value.pop("head")
    return value


def _delivery(scope, state, row, payload, metadata):
    from ._report_plans import preview

    if state["format_version"] != FORMAT_VERSION:
        raise ContractError("storage_corrupt", "读取组件格式尚未装配。")
    if state["state"] == "preparing":
        return ModelDelivery("preparing", scope)
    if state["state"] == "failed":
        return ModelDelivery(
            "failed",
            scope,
            reason=("corrupt" if state["error_code"] == "storage_corrupt" else "production_failed"),
        )
    if (
        payload is None
        or payload["head"] != state["ready_head"]
        or state["ready_head"] != state["wanted_head"]
    ):
        raise ContractError("storage_corrupt", "读取组件与已就绪目标不一致。")
    value = loads_json(payload["payload_json"])
    if content_digest(value) != state["content_digest"]:
        raise ContractError("storage_corrupt", "读取组件内容摘要不符。")
    snapshot = value["snapshot"] | {"head": row.head}
    validate_contract("reporting.schema.json", "FinancialSnapshot", snapshot)
    if snapshot["scope"] != scope or (snapshot["company_id"], snapshot["database_id"]) != (
        row.company_id,
        row.database_id,
    ):
        raise ContractError("storage_corrupt", "读取组件保存了其他范围的报表。")
    organization = {"name": metadata.legal_name, "taxpayer_id": metadata.credit_code}
    version = content_digest({"component": state["content_digest"], "organization": organization})
    return ModelDelivery(
        "ready",
        scope,
        version,
        {
            "snapshot": snapshot,
            "organization": organization,
            "export": preview(snapshot, state, metadata),
        },
    )


def page(reader, scope):
    normalized = normalize(scope)
    scope_key, corrupt = key(normalized), False
    with reader.storage.read_unit(scope["company_id"]) as read:
        if normalized["period_to"] < read.start_period:
            raise ContractError("invalid_input", "不能查看建账前缺少逐月正式资料的独立季度。")
        metadata = reader.storage.company_metadata(read)
        repository = read.repository("reading")
        state = repository.state(scope_key, FINANCIAL)
        if state is not None:
            try:
                result = _delivery(
                    normalized, state, read, repository.payload(scope_key, FINANCIAL), metadata
                )
            except (ContractError, KeyError, TypeError):
                corrupt = True
    if state is None:
        with reader.storage.maintenance_unit(scope["company_id"]) as unit:
            unit.repository("reading").register(scope_key, FINANCIAL, unit.head, FORMAT_VERSION)
        return ModelDelivery("preparing", normalized)
    if corrupt:
        with reader.storage.maintenance_unit(scope["company_id"]) as unit:
            unit.repository("reading").fail(
                scope_key,
                FINANCIAL,
                state["wanted_head"],
                "storage_corrupt",
                expected_state=state["state"],
            )
        return ModelDelivery("failed", normalized, reason="corrupt")
    expected = scope.get("expected_head")
    if expected is not None and result.version != expected:
        raise ContractError("snapshot_changed", "所选报表范围已变化，请重新读取。")
    if result.state == "ready" and reader.files is None:
        result.payload["export"].update(available=False, preview_digest=None, epochs=None)
    return result


def mark_targets(impact, unit):
    if not isinstance(impact, ReadingImpact):
        raise ContractError("invalid_input", "读取通知须提供明确的影响范围。")
    repository = unit.repository("reading")
    touched = []
    for state in repository.scopes():
        selection = loads_json(state["scope_key"])
        if selection["page"] not in {"quarterly_report", "monthly"}:
            continue
        scope = selection["scope"]
        if scope["company_id"] != unit.company_id:
            raise ContractError("storage_corrupt", "读取目标包含其他公司范围。")
        version = _monthly.FORMAT_VERSION if selection["page"] == "monthly" else FORMAT_VERSION
        if state["format_version"] != version:
            repository.fail(
                state["scope_key"],
                state["component_key"],
                state["wanted_head"],
                "storage_corrupt",
                expected_state=state["state"],
            )
            continue
        if selection["page"] == "monthly" and state["component_key"] == _monthly.BRIEF_OPEN_ITEMS:
            from . import _brief_open_update

            if _brief_open_update.mark(repository, state, scope, impact, unit.next_head):
                touched.append(state["scope_key"])
            continue
        if state["state"] != "ready":
            # A first capture has no declared rows yet. Keep its wanted point
            # current so an older in-flight capture cannot publish stale inputs.
            if state["state"] == "preparing":
                repository.mark(state["scope_key"], state["component_key"], unit.next_head)
            continue
        if selection["page"] == "monthly":
            try:
                raw = repository.input_dependencies(state["scope_key"], state["component_key"])
                selected = _dependencies.inputs({"_inputs": loads_json(raw)})
            except (ContractError, TypeError, ValueError):
                repository.fail(
                    state["scope_key"],
                    state["component_key"],
                    state["wanted_head"],
                    "storage_corrupt",
                    expected_state=state["state"],
                )
                continue
            if _monthly.affected(scope, state["component_key"], impact, selected):
                repository.mark(state["scope_key"], state["component_key"], unit.next_head)
                touched.append(state["scope_key"])
            continue
        if state["component_key"] != FINANCIAL:
            raise ContractError("storage_corrupt", "季度读取目标包含未装配组件。")
        # Earlier postings can affect ending stock and accumulated profit. A
        # future posting cannot affect this as-posted report.
        if _dependencies.affected(ReadInputs(all_results=True), scope["period_to"], impact) or any(
            r.period_from <= scope["period_to"] for r in impact.closure_ranges
        ):
            repository.mark(state["scope_key"], FINANCIAL, unit.next_head)
            touched.append(state["scope_key"])
    return tuple(touched)


def run_pending(reader, company_id, limit):
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ContractError("invalid_input", "读取准备数量须在1至500之间。")
    with reader.storage.read_unit(company_id) as read:
        pending = read.repository("reading").pending(limit)
    completed = []
    for target in pending:
        scope_key, component, wanted = (
            target["scope_key"],
            target["component_key"],
            target["wanted_head"],
        )
        try:
            selection = loads_json(scope_key)
            scope = selection["scope"]
            format_version = (
                _monthly.FORMAT_VERSION if selection["page"] == "monthly" else FORMAT_VERSION
            )
            with reader.storage.read_unit(company_id) as read:
                state = read.repository("reading").state(scope_key, component)
                if state is None or state["wanted_head"] != wanted or state["state"] != "preparing":
                    continue
                if state["format_version"] != format_version:
                    raise ContractError("storage_corrupt", "读取目标的内容格式尚未装配。")
                if selection["page"] == "monthly":
                    if component == _monthly.BRIEF_OPEN_ITEMS:
                        from . import _brief_open_update

                        model = _brief_open_update.prepare(reader, scope, read, state)
                    else:
                        model = _monthly.capture(reader, component, scope, read)
                elif selection["page"] == "quarterly_report" and component == FINANCIAL:
                    snapshot = reader.ledger.financial_snapshot(scope, read)
                    validate_contract("reporting.schema.json", "FinancialSnapshot", snapshot)
                    model = prepare_model({"snapshot": semantic(snapshot)})
                else:
                    raise ContractError("storage_corrupt", "读取目标的范围或组件尚未装配。")
            with reader.storage.maintenance_unit(company_id) as unit:
                published = unit.repository("reading").publish(
                    scope_key, component, wanted, format_version, model
                )
            if published:
                completed.append(scope_key)
        except Exception as error:
            with reader.storage.maintenance_unit(company_id) as unit:
                unit.repository("reading").fail(
                    scope_key,
                    component,
                    wanted,
                    error.code if isinstance(error, ContractError) else "production_failed",
                )
    return tuple(completed)
