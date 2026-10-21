"""Month pages share complete components and one read snapshot for their collections."""

from dataclasses import dataclass

from ai_accounting_modular.contracts.public import (
    ContractError,
    ReadInputs,
    canonical_json,
    content_digest,
    loads_json,
    validate_contract,
)

from . import _dependencies, _financial_projection
from ._collections import collection_items as _collection_items
from ._collections import collection_page
from ._context import last_period, period
from ._types import ModelDelivery, MonthScope

FORMAT_VERSION = 7
FINANCIAL = "financial"
FUNDS = "funds"
ASSETS = "assets"
EMPLOYEES = "employees"
BRIEF_ACTIVITY = "brief_activity"
BRIEF_OPEN_ITEMS = "brief_open_items"
BRIEF_CONTEXT = "brief_context"
COMPONENTS = (FINANCIAL, FUNDS, ASSETS, EMPLOYEES, BRIEF_ACTIVITY, BRIEF_OPEN_ITEMS, BRIEF_CONTEXT)


def version(component):
    return 8 if component == FINANCIAL else FORMAT_VERSION


def normalize(scope):
    validate_contract("core.schema.json", "LedgerScope", scope)
    month = scope["period_to"]
    if (
        scope["view"] != "as_posted"
        or scope["period_from"] != month
        or scope.get("as_of", month) != month
        or "object_ids" in scope
    ):
        raise ContractError("invalid_input", "月页须指定整家公司、一个月份及该月正式采用口径。")
    return {name: value for name, value in scope.items() if name != "expected_head"} | {
        "as_of": month
    }


def key(scope):
    return canonical_json({"page": "monthly", "scope": scope})


@dataclass(frozen=True)
class MonthSnapshot:
    scope_key: str
    scope: dict
    version: str
    components: dict
    selected_period: dict
    company_id: str
    database_id: str


class _CorruptComponent(Exception):
    def __init__(self, component):
        self.component = component


def collection(snapshot, component, name, query, page, read):
    if component not in snapshot.components:
        raise ContractError("invalid_input", "页面没有选择这个读取组件。")
    try:
        return collection_page(snapshot.scope_key, component, name, query, page, read)
    except ContractError as error:
        if error.code == "storage_corrupt":
            raise _CorruptComponent(component) from error
        raise


def collection_items(snapshot, component, name, item_ids, read):
    if component not in snapshot.components:
        raise ContractError("invalid_input", "页面没有选择这个读取组件。")
    try:
        return _collection_items(snapshot.scope_key, component, name, item_ids, read)
    except ContractError as error:
        if error.code == "storage_corrupt":
            raise _CorruptComponent(component) from error
        raise


def _load(repository, scope_key, component, state):
    if state["format_version"] != version(component):
        raise _CorruptComponent(component)
    payload = repository.payload(scope_key, component)
    try:
        if (
            payload is None
            or payload["head"] != state["ready_head"]
            or state["ready_head"] != state["wanted_head"]
        ):
            raise ValueError
        value = loads_json(payload["payload_json"])
        if not isinstance(value, dict) or content_digest(value) != state["content_digest"]:
            raise ValueError
        _dependencies.inputs(value)
        if component == BRIEF_OPEN_ITEMS:
            from . import _brief_open_index

            _brief_open_index.validate(value)
        return {key: item for key, item in value.items() if key not in {"_inputs", "_item_epoch"}}
    except (ContractError, KeyError, TypeError, ValueError) as error:
        raise _CorruptComponent(component) from error


def _ready_snapshot(reader, normalized, states, read):
    if any(state is None or state["state"] != "ready" for state in states.values()):
        return None
    scope_key = key(normalized)
    repository = read.repository("reading")
    values = {name: _load(repository, scope_key, name, state) for name, state in states.items()}
    selected = period(normalized["period_to"], reader.ledger.closed_through(read))
    version = content_digest(
        {
            "company_id": read.company_id,
            "database_id": read.database_id,
            "scope": normalized,
            "period_status": selected["status"],
            "components": {
                name: values[name]["_semantic_digest"]
                if name == BRIEF_OPEN_ITEMS
                else state["content_digest"]
                for name, state in sorted(states.items())
            },
        }
    )
    return MonthSnapshot(
        scope_key, normalized, version, values, selected, read.company_id, read.database_id
    )


def ready_snapshot(reader, normalized, components, read):
    """Load only already-ready components, without registration, repair or another read."""
    repository, scope_key = read.repository("reading"), key(normalized)
    states = {name: repository.state(scope_key, name) for name in components}
    return _ready_snapshot(reader, normalized, states, read)


def deliver(reader, scope, components, build):
    """Build a page only after all its own components are ready in the same read."""
    if isinstance(scope, MonthScope):
        company_id, expected = scope.company_id, scope.expected_head
        normalized = (
            normalize(
                {
                    "company_id": company_id,
                    "period_from": scope.period,
                    "period_to": scope.period,
                    "view": "as_posted",
                }
            )
            if scope.period is not None
            else None
        )
    else:
        normalized = normalize(scope)
        company_id, expected = scope["company_id"], scope.get("expected_head")
    components = tuple(components)
    if (
        not components
        or len(set(components)) != len(components)
        or any(name not in COMPONENTS for name in components)
    ):
        raise ContractError("invalid_input", "月页须选择已装配且不重复的组件。")
    missing, corrupt = (), None
    with reader.storage.read_unit(company_id) as read:
        if normalized is None:
            month = last_period(reader, read)
            if month is None:
                if expected is not None:
                    raise ContractError(
                        "snapshot_changed", "当前没有同版的可查看月份，请重新读取。"
                    )
                return ModelDelivery(
                    "ready",
                    {"company_id": company_id},
                    payload={
                        "selected_period": None,
                        "snapshot_version": None,
                        "data": None,
                        "company_id": read.company_id,
                        "database_id": read.database_id,
                    },
                )
            normalized = normalize(
                {
                    "company_id": read.company_id,
                    "period_from": month,
                    "period_to": month,
                    "view": "as_posted",
                }
            )
        scope_key = key(normalized)
        if normalized["period_to"] < read.start_period:
            raise ContractError("invalid_input", "不能查看建账前缺少逐月正式资料的月份。")
        repository = read.repository("reading")
        states = {name: repository.state(scope_key, name) for name in components}
        missing = tuple(name for name, state in states.items() if state is None)
        failed = next(
            (
                state
                for state in states.values()
                if state is not None and state["state"] == "failed"
            ),
            None,
        )
        if failed is not None:
            result = ModelDelivery(
                "failed",
                normalized,
                reason="corrupt"
                if failed["error_code"] == "storage_corrupt"
                else "production_failed",
            )
        elif missing or any(state["state"] != "ready" for state in states.values()):
            reason = (
                _financial_projection.waiting(reader, normalized, "summary_values", read)
                if FINANCIAL in states
                and states[FINANCIAL] is not None
                and states[FINANCIAL]["state"] == "preparing"
                else None
            )
            result = ModelDelivery("failed" if reason else "preparing", normalized, reason=reason)
        else:
            try:
                snapshot = _ready_snapshot(reader, normalized, states, read)
                if expected is not None and expected != snapshot.version:
                    raise ContractError("snapshot_changed", "所选页面已变化，请重新读取首页。")
                payload = build(snapshot, read) | {
                    "company_id": read.company_id,
                    "database_id": read.database_id,
                }
                result = ModelDelivery("ready", normalized, snapshot.version, payload)
            except _CorruptComponent as error:
                corrupt = error.component
                result = ModelDelivery("failed", normalized, reason="corrupt")
    if missing or corrupt is not None:
        with reader.storage.maintenance_unit(company_id) as unit:
            repository = unit.repository("reading")
            for component in missing:
                if component == FINANCIAL:
                    _financial_projection.register(
                        reader, normalized, "summary_values", scope_key, version(component), unit
                    )
                else:
                    repository.register(scope_key, component, unit.head, version(component))
            if corrupt is not None:
                state = states[corrupt]
                repository.fail(
                    scope_key,
                    corrupt,
                    state["wanted_head"],
                    "storage_corrupt",
                    expected_state=state["state"],
                )
    if expected is not None and result.state != "ready":
        raise ContractError("snapshot_changed", "所选页面尚无同版就绪内容，请重新读取首页。")
    return result


def capture(reader, component, scope, read):
    normalized = normalize(scope)
    if component == FINANCIAL:
        return _financial_projection.capture(reader, normalized, "summary_values", read)
    if component == FUNDS:
        from ._funds import prepare

        return prepare(reader, normalized, read)
    if component == ASSETS:
        from ._assets import prepare

        return prepare(reader, normalized, read)
    if component == EMPLOYEES:
        from ._employees import prepare

        return prepare(reader, normalized, read)
    if component == BRIEF_ACTIVITY:
        from ._brief_activity import prepare

        return prepare(reader, normalized, read)
    if component == BRIEF_OPEN_ITEMS:
        from ._brief_open import prepare

        return prepare(reader, normalized, read)
    if component == BRIEF_CONTEXT:
        from ._brief_context import prepare

        return _dependencies.bind(prepare(reader, normalized, read), ReadInputs())
    raise ContractError("storage_corrupt", "月度读取组件尚未装配。")


def affected(scope, component, impact, selected):
    if component not in COMPONENTS:
        raise ContractError("storage_corrupt", "月度读取目标包含未装配组件。")
    if component == BRIEF_CONTEXT:
        return bool("period:" + scope["period_to"] in impact.note_scopes or impact.issue_scopes)
    if component == BRIEF_OPEN_ITEMS and (impact.working_period_changed or impact.closure_ranges):
        return True
    if component == FINANCIAL and any(
        value.period_from <= scope["period_to"] for value in impact.closure_ranges
    ):
        return True
    return _dependencies.affected(selected, scope["period_to"], impact)


def retry(reader, scope, components):
    normalized = normalize(scope)
    if not components or any(component not in COMPONENTS for component in components):
        raise ContractError("invalid_input", "月页重试须选择已装配的组件。")
    with reader.storage.maintenance_unit(scope["company_id"]) as unit:
        repository = unit.repository("reading")
        for component in components:
            if component == FINANCIAL:
                _financial_projection.retry(
                    reader, normalized, "summary_values", key(normalized), version(component), unit
                )
                continue
            state = repository.state(key(normalized), component)
            full = (
                component == BRIEF_OPEN_ITEMS
                and state is not None
                and (state["state"] == "failed" or state["format_version"] != version(component))
            )
            if full:
                repository.open_mark(key(normalized), unit.head, full=True)
            repository.retry(key(normalized), component, format_version=version(component))
            if full:
                repository.mark(key(normalized), component, unit.head)
