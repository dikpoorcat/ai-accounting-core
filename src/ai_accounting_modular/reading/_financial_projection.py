"""The single static prerequisite of monthly and quarterly financial components."""

from dataclasses import asdict, dataclass, fields

from ai_accounting_modular.contracts.public import (
    ContractError,
    ReadInputs,
    canonical_json,
    validate_contract,
)
from ai_accounting_modular.ledger.public import (
    FINANCIAL_PROJECTION_FORMAT,
    FinancialProjectionArtifact,
    FinancialSummaryValues,
    FormalChange,
)

from . import _dependencies
from ._collections import PreparedModel, prepare_model
from ._financial_store import ConsumerToken, cache_corruption, corrupt, empty_work

INPUTS = ReadInputs(all_results=True)


@dataclass(frozen=True)
class PreparedFinancial:
    model: PreparedModel
    producer_scope: str
    consumer_scope: str
    guard: ConsumerToken


def key(read):
    return canonical_json(
        {
            "model": "ledger_financial_projection",
            "company_id": read.company_id,
            "database_id": read.database_id,
        }
    )


def store(read):
    return read.repository("reading").financial_store(key(read))


def request(reader, scope, kind, read):
    return reader.ledger.financial_projection_request(kind, scope, read)


def _requests(reader, work, read):
    requests = {}
    for identity, value in work["registered"].items():
        if not isinstance(value, dict) or set(value) != {"kind", "scope"}:
            corrupt()
        try:
            selected = request(reader, value["scope"], value["kind"], read)
        except ContractError as error:
            if not cache_corruption(error):
                raise
            raise ContractError("storage_corrupt", "财务准备模型保存了无效的请求范围。") from error
        if selected.key != identity:
            corrupt()
        requests[identity] = selected
    return requests


def _changes(values):
    names = {field.name for field in fields(FormalChange)}
    result = []
    for value in values:
        if not isinstance(value, dict) or set(value) != names:
            corrupt()
        if not all(
            isinstance(value[name], str) and value[name] for name in ("business_id", "result_slot")
        ):
            corrupt()
        validate_contract("core.schema.json", "Id", value["business_id"])
        for name in names - {"business_id", "result_slot"}:
            items = value[name]
            if not isinstance(items, list) or any(
                not isinstance(item, str) or not item for item in items
            ):
                corrupt()
            if items != sorted(set(items)):
                corrupt()
        result.append(
            FormalChange(
                **{
                    name: item if name in {"business_id", "result_slot"} else tuple(item)
                    for name, item in value.items()
                }
            )
        )
    return tuple(result)


def _closures(values):
    for value in values:
        if not isinstance(value, list) or len(value) != 2 or value[0] > value[1]:
            corrupt()
        for period in value:
            validate_contract("core.schema.json", "Month", period)
    return tuple(tuple(value) for value in values)


def queue_requests(
    reader, selected, read, *, changes=(), closures=(), retry=False, rebuild=False, repair=False
):
    selected = tuple(selected)
    target = store(read)
    damaged = False
    state = target.state()
    if state is not None and state["format_version"] != FINANCIAL_PROJECTION_FORMAT:
        rebuild, damaged = True, not retry
    try:
        work = target.work()
        if state is not None and state["content_digest"] is not None and not work["rebuild"]:
            _ = target.header_json
        _requests(reader, work, read)
        old_changes = _changes(work["changes"])
        old_closures = _closures(work["closures"])
    except (ContractError, KeyError, TypeError, ValueError) as error:
        if not cache_corruption(error):
            raise
        if not retry and not repair:
            raise
        work, old_changes, old_closures, rebuild = empty_work(), (), (), True
        damaged |= not retry
    fresh = False
    for value in selected:
        registered = {"kind": value.kind, "scope": value.scope}
        if value.key in work["registered"] and work["registered"][value.key] != registered:
            corrupt()
        fresh |= value.key not in work["registered"]
        work["registered"][value.key] = registered
    if not fresh and not changes and not closures and not retry and not rebuild:
        return
    work["pending"] = sorted({*work["pending"], *(value.key for value in selected)})
    merged = {}
    for value in (*old_changes, *changes):
        identity = value.business_id, value.result_slot
        saved = merged.setdefault(identity, asdict(value))
        for name in saved.keys() - {"business_id", "result_slot"}:
            saved[name] = sorted({*saved[name], *getattr(value, name)})
    work["changes"] = [merged[identity] for identity in sorted(merged)]
    work["closures"] = [list(value) for value in sorted({*old_closures, *closures})]
    work["rebuild"] = work["rebuild"] or rebuild
    target.save_work(
        work,
        getattr(read, "next_head", read.head) if changes or closures else read.head,
        retry=retry,
    )
    if damaged:
        state = target.state()
        target.repository.fail(
            target.scope_key,
            "projection",
            state["wanted_head"],
            "storage_corrupt",
            expected_state=state["state"],
            expected_format=state["format_version"],
        )


def queue(reader, selected, read, **options):
    return queue_requests(reader, (selected,), read, **options)


def register(reader, scope, kind, scope_key, version, unit):
    selected = request(reader, scope, kind, unit)
    unit.repository("reading").register(scope_key, "financial", unit.head, version)
    queue(reader, selected, unit, repair=True)


def mark(reader, scope, kind, scope_key, impact, unit):
    if not (
        _dependencies.affected(INPUTS, scope["period_to"], impact)
        or any(value.period_from <= scope["period_to"] for value in impact.closure_ranges)
    ):
        return None
    selected = request(reader, scope, kind, unit)
    store(unit).mark_consumer(scope_key, unit.next_head)
    return selected


def waiting(reader, scope, kind, read):
    """Only this request's unmet dependency can fail a pending consumer."""
    selected, target = request(reader, scope, kind, read), store(read)
    try:
        work, state = target.work(), target.state()
        if selected.key not in work["pending"]:
            return None
        if state is not None and state["state"] == "failed":
            return "corrupt" if state["error_code"] == "storage_corrupt" else "production_failed"
    except (ContractError, KeyError, TypeError, ValueError) as error:
        if not cache_corruption(error):
            raise
        return "corrupt"
    return None


def blocked(reader, scope, kind, read):
    selected = request(reader, scope, kind, read)
    try:
        return selected.key in store(read).work()["pending"]
    except (ContractError, KeyError, TypeError, ValueError) as error:
        if not cache_corruption(error):
            raise
        return True


def retry(reader, scope, kind, scope_key, version, unit):
    repository, target = unit.repository("reading"), store(unit)
    state = repository.state(scope_key, "financial")
    if state is None or (state["state"] != "failed" and state["format_version"] == version):
        # A consumer waiting on a failed dependency is still technically preparing.
        reason = waiting(reader, scope, kind, unit) if state is not None else None
        if reason is None:
            return
    producer = target.state()
    full = producer is not None and (
        producer["format_version"] != FINANCIAL_PROJECTION_FORMAT
        or producer["error_code"] == "storage_corrupt"
    )
    full |= state is not None and state["error_code"] == "storage_corrupt"
    queue(reader, request(reader, scope, kind, unit), unit, retry=True, rebuild=full)
    repository.retry(scope_key, "financial", format_version=version)


def produce(reader, company_id):
    token = None
    try:
        with reader.storage.read_unit(company_id) as read:
            target = store(read)
            state = target.state()
            if state is None or state["state"] != "preparing":
                return False
            token = target.token()
            if state["format_version"] != FINANCIAL_PROJECTION_FORMAT:
                corrupt()
            work = target.work()
            registered = _requests(reader, work, read)
            rebuild = work["rebuild"] or target.header_json is None
            selected = (
                registered.values()
                if rebuild
                else (registered[identity] for identity in work["pending"])
            )
            plan = reader.ledger.prepare_financial_projection(
                tuple(selected),
                _changes(work["changes"]),
                _closures(work["closures"]),
                read,
                base=None if rebuild else target,
                rebuild=rebuild,
            )
        with reader.storage.maintenance_unit(company_id) as unit:
            return store(unit).publish(token, plan, rebuild=rebuild)
    except Exception as error:
        if token is not None:
            with reader.storage.maintenance_unit(company_id) as unit:
                store(unit).fail(
                    token, error.code if isinstance(error, ContractError) else "production_failed"
                )
        return False


def capture(reader, scope, kind, read):
    selected, target = request(reader, scope, kind, read), store(read)
    if blocked(reader, scope, kind, read):
        return None
    guard = target.consumer_token(selected.descriptor_key)
    artifact = reader.ledger.read_financial_projection(selected, read, target)
    if artifact is None:
        return None
    if (
        not isinstance(artifact, FinancialProjectionArtifact)
        or artifact.request_key != selected.key
    ):
        corrupt()
    if (
        artifact.descriptor_key != guard.descriptor_key
        or artifact.descriptor_digest != guard.descriptor_digest
        or not target.matches_consumer(guard)
    ):
        corrupt()
    value = artifact.value
    if kind == "summary_values":
        from ._monthly import key as consumer_key

        if (
            not isinstance(value, FinancialSummaryValues)
            or value.version != artifact.version
            or value.scope != selected.scope
            or {"version", "scope", "issues", "sources"} & value.values.keys()
        ):
            corrupt()
        summary = {
            "version": value.version,
            "scope": value.scope,
            **value.values,
            "issues": list(value.issues),
        }
        model = _dependencies.bind(prepare_model({"financial": summary}), INPUTS)
    else:
        from ._models import key as consumer_key
        from ._models import semantic

        validate_contract("reporting.schema.json", "FinancialSnapshot", value)
        if (
            value["scope"] != selected.scope
            or value["selection"]["selection_digest"] != artifact.version
            or (value["company_id"], value["database_id"]) != (read.company_id, read.database_id)
            or value["head"] != read.head
        ):
            corrupt()
        model = prepare_model({"snapshot": semantic(value)})
    return PreparedFinancial(model, key(read), consumer_key(selected.scope), guard)
