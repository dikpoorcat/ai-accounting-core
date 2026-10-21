"""Read adopted cooperation positions; these are not ordinary payable resources."""

from dataclasses import asdict

from ai_accounting_modular.contracts.public import (
    PageQuery,
    ReadInputs,
    content_digest,
    validate_contract,
)

from ._common import amount, fail
from ._cooperation_common import PREFIX
from ._read_types import CooperationPosition, CooperationPositions


def _batches(values):
    values = tuple(values)
    for start in range(0, len(values), 500):
        yield values[start : start + 500]


def _latest(ledger, scope, read, contract_ids):
    page, pending = PageQuery(500), {}
    while True:
        result = ledger.query_adopted_directory(
            scope, (PREFIX + "position",), page, read, business_ids=contract_ids
        )
        for item in result.items:
            pending.setdefault(item.reference["business_id"], []).append(item.reference)
        if result.next_cursor is None:
            break
        page = PageQuery(500, result.next_cursor, result.version)
    for refs in pending.values():
        refs.sort(key=lambda ref: (ref["business_period"], ref["result_slot"]))
    selected = {}
    while pending:
        candidates = [refs.pop() for refs in pending.values()]
        for batch in _batches(candidates):
            for body in ledger.get_adopted_contents(batch, read):
                business_id = body.reference["business_id"]
                if body.target["kind"] == PREFIX + "position":
                    selected[body.reference["result_id"]] = body
                    del pending[business_id]
                elif body.target["kind"] != "system.withdrawal":
                    fail("合作位置目录包含其他类型的正式结果。", "storage_corrupt")
                elif not pending[business_id]:
                    del pending[business_id]
    return selected


def positions(owner, scope, read, contract_ids):
    validate_contract("core.schema.json", "LedgerScope", scope)
    month = scope["period_to"]
    if (
        scope["company_id"] != read.company_id
        or scope["period_from"] != month
        or "object_ids" in scope
        or (scope["view"] == "as_posted" and scope.get("as_of", month) != month)
    ):
        fail("合作月末位置须指定同一公司的一个明确月份。", "invalid_input")
    if not isinstance(contract_ids, tuple) or len(contract_ids) > 500:
        fail("合作位置一次最多筛选500个明确合同身份。", "invalid_input")
    for identifier in contract_ids:
        validate_contract("core.schema.json", "Id", identifier)
    if len(set(contract_ids)) != len(contract_ids):
        fail("合作位置不能重复筛选同一合同。", "invalid_input")
    contract_ids = tuple(sorted(contract_ids))
    normalized = {key: value for key, value in scope.items() if key != "expected_head"}
    if scope["view"] == "as_posted":
        normalized["as_of"] = month
    query = normalized | {"period_from": read.start_period}
    bodies = _latest(owner._ledger, query, read, contract_ids)
    targets = tuple(bodies.values())
    contracts = {}
    for item in targets:
        sources = [
            source
            for source in bodies[item.reference["result_id"]].basis["source_results"]
            if source["business_id"] == item.reference["business_id"]
            and source["result_slot"] == "main"
        ]
        if len(sources) != 1:
            fail("合作月末位置缺少唯一的实际采用合同。", "storage_corrupt")
        previous = contracts.setdefault(sources[0]["result_id"], sources[0])
        if previous != sources[0]:
            fail("合作位置采用的同一合同来源不一致。", "storage_corrupt")
    contract_bodies = {}
    for batch in _batches(contracts.values()):
        headers = owner._ledger.get_adopted_sources(batch, read)
        contract_bodies.update(
            (body.reference["result_id"], body)
            for body in owner._ledger.get_adopted_contents(
                tuple(item.reference for item in headers), read
            )
        )
    values = []
    for item in targets:
        body = bodies[item.reference["result_id"]]
        contract = next(
            contract_bodies[source["result_id"]]
            for source in body.basis["source_results"]
            if source["business_id"] == item.reference["business_id"]
            and source["result_slot"] == "main"
        )
        data, terms = item.target["values"], contract.target["values"]
        if (
            contract.target["kind"] != PREFIX + "contract"
            or data["contract_id"] != item.reference["business_id"]
            or data["as_of_period"] > month
            or item.reference["business_period"] != data["as_of_period"]
            or item.reference["result_slot"] != "position:" + data["as_of_period"]
        ):
            fail("合作月末位置的合同或期间不一致。", "storage_corrupt")
        values.append(
            CooperationPosition(
                data["contract_id"],
                terms["operator_id"],
                terms["project_id"],
                data["as_of_period"],
                amount(data["held_fen"]),
                amount(data["operator_due_fen"]),
                amount(data["company_due_fen"]),
                data["status"],
                item.reference,
                item.source,
            )
        )
    values.sort(key=lambda item: item.contract_id)
    version = content_digest(
        {
            "company_id": read.company_id,
            "database_id": read.database_id,
            "scope": normalized,
            "contract_ids": list(contract_ids),
            "positions": [asdict(item) for item in values],
        }
    )
    if scope.get("expected_head", version) != version:
        fail("合作月末位置已经变化，请重新读取。", "snapshot_changed")
    return CooperationPositions(
        version,
        tuple(values),
        ReadInputs(
            result_kinds=(PREFIX + "position",),
            business_ids=tuple(item.contract_id for item in values),
        ),
    )
