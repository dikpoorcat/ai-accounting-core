"""Capture in a read snapshot, then publish only the still-wanted component."""

from copy import deepcopy

from ai_accounting_modular.contracts.public import (
    ContractError,
    canonical_json,
    content_digest,
    loads_json,
    validate_contract,
)

from . import _dependencies, _financial_projection, _monthly
from ._financial_projection import PreparedFinancial
from ._financial_store import COMPONENT, FINANCIAL_PROJECTION_FORMAT, cache_corruption, corrupt
from ._types import ModelDelivery, ReadingImpact

FORMAT_VERSION = 3
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
                if result.state == "preparing":
                    reason = _financial_projection.waiting(reader, normalized, "snapshot", read)
                    if reason:
                        result = ModelDelivery("failed", normalized, reason=reason)
            except (ContractError, KeyError, TypeError):
                corrupt = True
    if state is None:
        with reader.storage.maintenance_unit(scope["company_id"]) as unit:
            _financial_projection.register(
                reader, normalized, "snapshot", scope_key, FORMAT_VERSION, unit
            )
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


def _selection(state, read):
    """Validate only Reading's own saved target, never accounting contents."""
    value = loads_json(state["scope_key"])
    if not isinstance(value, dict):
        corrupt()
    if "model" in value:
        if (
            state["scope_key"] != _financial_projection.key(read)
            or state["component_key"] != COMPONENT
        ):
            corrupt()
        return None, FINANCIAL_PROJECTION_FORMAT
    if set(value) != {"page", "scope"}:
        corrupt()
    if value["page"] == "monthly" and state["component_key"] in _monthly.COMPONENTS:
        normalized = _monthly.normalize(value["scope"])
        expected_key, version = _monthly.key(normalized), _monthly.version(state["component_key"])
    elif value["page"] == "quarterly_report" and state["component_key"] == FINANCIAL:
        normalized = normalize(value["scope"])
        expected_key, version = key(normalized), FORMAT_VERSION
    else:
        corrupt()
    if state["scope_key"] != expected_key or normalized["company_id"] != read.company_id:
        corrupt()
    return value, version


def _fail_corrupt(repository, state):
    repository.fail(
        state["scope_key"],
        state["component_key"],
        state["wanted_head"],
        "storage_corrupt",
        expected_state=state["state"],
        expected_format=state["format_version"],
    )


def _mark_target(reader, repository, state, selection, impact, unit):
    scope, component = selection["scope"], state["component_key"]
    if component == FINANCIAL:
        selected = _financial_projection.mark(
            reader,
            scope,
            "summary_values" if selection["page"] == "monthly" else "snapshot",
            state["scope_key"],
            impact,
            unit,
        )
        return selected is not None, selected
    if component == _monthly.BRIEF_OPEN_ITEMS:
        from . import _brief_open_update

        return _brief_open_update.mark(repository, state, scope, impact, unit.next_head), None
    if state["state"] != "ready":
        # First captures have no sparse inputs yet; keep their wanted point current.
        if state["state"] == "preparing":
            repository.mark(state["scope_key"], component, unit.next_head)
        return False, None
    raw = repository.input_dependencies(state["scope_key"], component)
    selected = _dependencies.inputs({"_inputs": loads_json(raw)})
    affected = _monthly.affected(scope, component, impact, selected)
    if affected:
        repository.mark(state["scope_key"], component, unit.next_head)
    return affected, None


def mark_targets(reader, impact, unit):
    if not isinstance(impact, ReadingImpact):
        raise ContractError("invalid_input", "读取通知须提供明确的影响范围。")
    repository = unit.repository("reading")
    touched, financial = [], []
    for state in repository.scopes():
        try:
            selection, version = _selection(state, unit)
            if state["format_version"] != version:
                corrupt()
            if selection is None:
                continue
            affected, selected = _mark_target(reader, repository, state, selection, impact, unit)
            if affected:
                touched.append(state["scope_key"])
            if selected is not None:
                financial.append(selected)
        except (ContractError, KeyError, TypeError, ValueError) as error:
            if not cache_corruption(error):
                raise
            _fail_corrupt(repository, state)
    if financial:
        _financial_projection.queue_requests(
            reader,
            financial,
            unit,
            changes=impact.formal_changes,
            closures=tuple((value.period_from, value.period_to) for value in impact.closure_ranges),
            repair=True,
        )
    return tuple(touched)


def run_pending(reader, company_id, limit):
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ContractError("invalid_input", "读取准备数量须在1至500之间。")
    completed = []
    with reader.storage.read_unit(company_id) as read:
        producer_key = _financial_projection.key(read)
        producer = _financial_projection.store(read).state()
    if producer is not None and producer["state"] == "preparing":
        if _financial_projection.produce(reader, company_id):
            completed.append(producer_key)
        limit -= 1
    if limit == 0:
        return tuple(completed)
    with reader.storage.read_unit(company_id) as read:
        pending, damaged = [], []
        for target in sorted(
            read.repository("reading").scopes(),
            key=lambda value: (value["scope_key"], value["component_key"]),
        ):
            if target["state"] != "preparing" or target["scope_key"] == producer_key:
                continue
            try:
                selection, _ = _selection(target, read)
                if selection is None:
                    corrupt()
                if target["component_key"] == FINANCIAL and _financial_projection.blocked(
                    reader,
                    selection["scope"],
                    "summary_values" if selection["page"] == "monthly" else "snapshot",
                    read,
                ):
                    continue
            except (ContractError, KeyError, TypeError, ValueError) as error:
                if not cache_corruption(error):
                    raise
                damaged.append(target)
                continue
            pending.append(target)
            if len(pending) == limit:
                break
    if damaged:
        with reader.storage.maintenance_unit(company_id) as unit:
            for state in damaged:
                _fail_corrupt(unit.repository("reading"), state)
    for target in pending:
        scope_key, component, wanted = (
            target["scope_key"],
            target["component_key"],
            target["wanted_head"],
        )
        model, failure_guard, state = None, None, target
        try:
            selection = loads_json(scope_key)
            scope = selection["scope"]
            format_version = (
                _monthly.version(component) if selection["page"] == "monthly" else FORMAT_VERSION
            )
            with reader.storage.read_unit(company_id) as read:
                state = read.repository("reading").state(scope_key, component)
                if state is None or state["wanted_head"] != wanted or state["state"] != "preparing":
                    continue
                if state["format_version"] != format_version:
                    raise ContractError("storage_corrupt", "读取目标的内容格式尚未装配。")
                if component == FINANCIAL:
                    selected = _financial_projection.request(
                        reader,
                        scope,
                        "summary_values" if selection["page"] == "monthly" else "snapshot",
                        read,
                    )
                    failure_guard = _financial_projection.store(read).consumer_token(
                        selected.descriptor_key
                    )
                if selection["page"] == "monthly":
                    if component == _monthly.BRIEF_OPEN_ITEMS:
                        from . import _brief_open_update

                        model = _brief_open_update.prepare(reader, scope, read, state)
                    else:
                        model = _monthly.capture(reader, component, scope, read)
                elif selection["page"] == "quarterly_report" and component == FINANCIAL:
                    model = _financial_projection.capture(reader, scope, "snapshot", read)
                else:
                    raise ContractError("storage_corrupt", "读取目标的范围或组件尚未装配。")
            if model is None:
                with reader.storage.maintenance_unit(company_id) as unit:
                    selected = _financial_projection.request(
                        reader,
                        scope,
                        "summary_values" if selection["page"] == "monthly" else "snapshot",
                        unit,
                    )
                    target_store = _financial_projection.store(unit)
                    try:
                        already_pending = selected.key in target_store.work()["pending"]
                    except (ContractError, KeyError, TypeError, ValueError) as error:
                        if not cache_corruption(error):
                            raise
                        already_pending = False
                    if not already_pending:
                        _financial_projection.queue(
                            reader, selected, unit, rebuild=True, repair=True
                        )
                continue
            with reader.storage.maintenance_unit(company_id) as unit:
                published = unit.repository("reading").publish(
                    scope_key, component, wanted, format_version, model
                )
            if published:
                completed.append(scope_key)
        except Exception as error:
            with reader.storage.maintenance_unit(company_id) as unit:
                if component == FINANCIAL:
                    target_store = _financial_projection.store(unit)
                    if isinstance(model, PreparedFinancial):
                        failure_guard = model.guard
                    if failure_guard is None:
                        if state is None or state["format_version"] == format_version:
                            continue
                    elif not target_store.matches_consumer(failure_guard, validate=False):
                        continue
                unit.repository("reading").fail(
                    scope_key,
                    component,
                    wanted,
                    error.code if isinstance(error, ContractError) else "production_failed",
                    expected_format=state["format_version"],
                )
    return tuple(completed)
