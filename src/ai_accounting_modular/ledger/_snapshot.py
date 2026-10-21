"""Audit the saved ledger, including its original frozen adoption authority."""

from collections import defaultdict
from dataclasses import dataclass
from itertools import chain

from ai_accounting_modular.contracts.public import (
    MAX_FEN,
    ContractError,
    content_digest,
    loads_json,
    validate_contract,
)

from ._balances import unique_movements, validate_capacities
from ._closing import _basis, _record
from ._output_digest import verify_output_digests
from ._planning import stored_source_reference
from ._snapshot_resources import verify_resources
from ._snapshot_vouchers import verify_vouchers
from ._values import fail


@dataclass(frozen=True)
class AdoptedBusinessVersion:
    business_id: str
    max_fact_seq: int
    open_fact_seq: int | None
    active_result_slots: tuple[str, ...]


@dataclass(frozen=True)
class LedgerSnapshot:
    """Local proof summary; period_to covers economic, adoption and closed months."""

    result_count: int
    voucher_count: int
    source_ids: tuple[str, ...]
    business_versions: tuple[AdoptedBusinessVersion, ...]
    period_to: str | None


def _key(row):
    return row["business_id"], row["result_slot"]


def _business_versions(heads, slots):
    versions, active = defaultdict(list), defaultdict(list)
    for row in heads.values():
        versions[row["business_id"]].append(row)
    for row in slots.values():
        if row["target_state"] == "active":
            active[row["business_id"]].append(row)
    summaries = []
    for business_id, rows in sorted(versions.items()):
        current = active[business_id]
        open_versions = {row["adopted_fact_seq"] for row in current if row["state"] == "open"}
        if len(open_versions) > 1:
            fail("storage_corrupt", "同一业务的开放结果采用了不同事实版本。")
        summaries.append(
            AdoptedBusinessVersion(
                business_id,
                max(row["adopted_fact_seq"] for row in rows),
                next(iter(open_versions), None),
                tuple(sorted(row["result_slot"] for row in current)),
            )
        )
    return tuple(summaries)


def _results(records, context):
    heads, bases, groups = {}, {}, defaultdict(list)
    for row in records["adopted_result"]:
        ref = {
            key: row[key]
            for key in (
                "business_id",
                "result_slot",
                "result_id",
                "generation",
                "business_period",
                "posting_period",
                "state",
                "baseline_result_id",
            )
            if row[key] is not None
        }
        validate_contract("core.schema.json", "ResultRef", ref)
        if (
            row["result_id"] in heads
            or row["basis_schema"] != "business-facts.schema.json#/$defs/AdoptedBasis"
            or row["target_schema"] != "business-facts.schema.json#/$defs/ResultTarget"
            or not isinstance(row["calculation_key"], str)
            or not 1 <= len(row["calculation_key"]) <= 150
            or type(row["calculation_revision"]) is not int
            or not 1 <= row["calculation_revision"] <= MAX_FEN
            or type(row["adopted_fact_seq"]) is not int
            or not 1 <= row["adopted_fact_seq"] <= MAX_FEN
        ):
            fail("storage_corrupt", "正式结果身份、合同或核算版本不合法。")
        basis, target = loads_json(row["basis_json"]), loads_json(row["target_json"])
        validate_contract("business-facts.schema.json", "AdoptedBasis", basis)
        validate_contract("business-facts.schema.json", "ResultTarget", target)
        if (
            content_digest(basis) != row["basis_digest"]
            or content_digest(target) != row["target_digest"]
            or basis["kind"] != target["kind"]
            or basis["result_slot"] != row["result_slot"]
            or target["result_slot"] != row["result_slot"]
            or basis["period"] != row["business_period"]
            or row["target_state"]
            != ("withdrawn" if target["kind"] == "system.withdrawal" else "active")
        ):
            fail("storage_corrupt", "正式结果正文、摘要、类型或期间不一致。")
        heads[row["result_id"]] = row
        bases[row["result_id"]] = basis
        groups[_key(row)].append(row)
    slots = {}
    for slot in records["result_slot_current"]:
        row = heads.get(slot["head_result_id"])
        if (
            row is None
            or _key(slot) != _key(row)
            or slot["generation"] != row["generation"]
            or _key(slot) in slots
        ):
            fail("storage_corrupt", "当前结果目录的业务、槽或代次与正式结果不一致。")
        slots[_key(slot)] = row
    if slots.keys() != groups.keys():
        fail("storage_corrupt", "正式结果缺少完整当前目录。")
    frontier = max((row["period"] for row in records["period_close"]), default=None)
    for key, rows in groups.items():
        previous = None
        for row in sorted(rows, key=lambda item: item["posting_period"]):
            if previous is None:
                if row["baseline_result_id"] is not None:
                    fail("storage_corrupt", "首份正式结果不能有不存在的冻结基线。")
            elif (
                row["baseline_result_id"] != previous["result_id"]
                or previous["state"] != "frozen"
                or previous["posting_period"] >= row["posting_period"]
            ):
                fail("storage_corrupt", "正式更正链必须准确承接同业务同槽、更早采用期的冻结基线。")
            if row["state"] == "frozen" and (
                frontier is None or not context.start_period <= row["posting_period"] <= frontier
            ):
                fail("storage_corrupt", "冻结正式结果缺少关账范围覆盖。")
            if (
                row["state"] == "open"
                and frontier is not None
                and context.start_period <= row["posting_period"] <= frontier
            ):
                fail("storage_corrupt", "已关账范围仍有开放正式结果。")
            previous = row
        if slots[key]["result_id"] != previous["result_id"]:
            fail("storage_corrupt", "当前目录没有指向最新正式采用。")
    return heads, bases, slots


def _sources(records, heads, bases, slots):
    locations, references, source_ids = defaultdict(list), defaultdict(list), set()
    for row in records["result_source"]:
        if row["result_id"] not in heads:
            fail("storage_corrupt", "正式来源索引缺少所属结果。")
        locations[row["result_id"]].append((row["source_id"], row["location"]))
    for row in records["result_basis_ref"]:
        if row["result_id"] not in heads:
            fail("storage_corrupt", "正式采用索引缺少所属结果。")
        ref = {
            "business_id": row["producer_business_id"],
            "result_slot": row["producer_result_slot"],
            "result_id": row["producer_result_id"],
            "result_generation": row["producer_generation"],
            "content_digest": row["producer_content_digest"],
        }
        references[row["result_id"]].append((row["item_no"], ref))
    for result_id, basis in bases.items():
        expected_locations = sorted(
            {
                (source["source_id"], source.get("location", ""))
                for source in basis.get("source_locations", ())
            }
        )
        expected_refs = basis.get("source_results", [])
        if sorted(locations[result_id]) != expected_locations or sorted(
            references[result_id], key=lambda item: item[0]
        ) != list(enumerate(expected_refs, 1)):
            fail("storage_corrupt", "正式来源或有序采用索引与Basis正文不一致。")
        source_ids.update(source_id for source_id, _ in expected_locations)
        row = heads[result_id]
        baseline = heads.get(row["baseline_result_id"])
        if basis["kind"] == "system.withdrawal":
            if baseline is None or basis["baseline_ref"] != stored_source_reference(baseline):
                fail("storage_corrupt", "退出依据没有准确引用本业务冻结基线。")
        if row["state"] == "frozen":
            continue
        inherited = bases[baseline["result_id"]].get("source_results", []) if baseline else []
        for ref in expected_refs:
            if ref in inherited:
                continue
            current = slots.get(_key(ref))
            if (
                current is None
                or current["target_state"] == "withdrawn"
                or ref != stored_source_reference(current)
            ):
                fail("storage_corrupt", "开放采用依据没有引用当前正确来源或准确继承冻结权威。")
    return tuple(sorted(source_ids))


def _verify(context, repo):
    records = repo.snapshot_records()
    heads, bases, slots = _results(records, context)
    source_ids = _sources(records, heads, bases, slots)
    outputs = verify_resources(records, heads) | verify_vouchers(records, heads, repo)
    verify_output_digests(records, heads)
    for result_id, row in heads.items():
        if row["business_period"] < context.start_period and (
            result_id in outputs
            or any(
                item["type"] == "opening_balance" for item in bases[result_id]["adopted_quantities"]
            )
        ):
            fail("storage_corrupt", "建账前的资料结果不能包含正式凭证或资源输出。")
    businesses = {head["business_id"] for head in heads.values()}
    movements = {row["movement_id"] for row in records["result_fund_movement"]}
    # Only a saved close promises an independently valid historical snapshot.
    # An open month may be completed by a later, genuinely dated recovery.
    cutoffs = {closed["period"] for closed in records["period_close"]}
    for cutoff in (None, *sorted(cutoffs)):
        selected = repo.resource_view(as_of=cutoff)
        validate_capacities(selected, businesses)
        unique_movements(selected, movements)
        selected.definition_maps(selected.current_heads(businesses), "obligation")
        selected.definition_maps(selected.current_heads(businesses), "cost")
        selected.definition_maps(selected.current_heads(businesses), "funds")
    for closed in sorted(records["period_close"], key=lambda row: row["period"]):
        validate_contract("core.schema.json", "Month", closed["period"])
        first, report = _record(repo, closed, context)
        if closed["period"] < context.start_period or report["head"] > context.head:
            fail("storage_corrupt", "关账期间或保存版本超出当前公司账本。")
        basis, _ = _basis(repo, first, closed["period"], context)
        if loads_json(closed["basis_json"]) != basis:
            fail("storage_corrupt", "关账原始冻结采用摘要与保存的正式结果不一致。")
    return LedgerSnapshot(
        len(heads),
        len(records["voucher"]),
        source_ids,
        _business_versions(heads, slots),
        max(
            chain(
                (
                    row[key]
                    for row in heads.values()
                    for key in ("business_period", "posting_period")
                ),
                cutoffs,
            ),
            default=None,
        ),
    )


def verify_snapshot(context) -> LedgerSnapshot:
    """Verify saved accounting evidence; do not replay Facts or recalculate policy."""
    repo = context.repository("ledger")
    try:
        return _verify(context, repo)
    except ContractError as error:
        if error.code == "storage_corrupt":
            raise
        raise ContractError("storage_corrupt", "账本快照核验失败：" + error.message) from error
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise ContractError("storage_corrupt", "账本快照包含不完整或不合法的正式记录。") from error
