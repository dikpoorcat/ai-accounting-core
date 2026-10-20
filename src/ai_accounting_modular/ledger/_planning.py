"""Temporary result identities for the caller's single company transaction."""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import uuid4

from ai_accounting_modular.contracts.public import content_digest, validate_contract

from ._formal_output import same_formal_output, target_lines
from ._values import fail


@dataclass(frozen=True)
class PreparedResult:
    """A candidate identity; binding a complete no-impact plan reuses its adopted head."""

    business_id: str
    result_slot: str
    result_id: str
    generation: int
    baseline_result_id: str | None
    correction_period: str | None


@dataclass(frozen=True)
class BoundPlan:
    identity: PreparedResult
    source_ref: dict
    unchanged: bool
    plan_digest: str


@dataclass(frozen=True)
class PlannedAdoption:
    plan: dict
    fact_seq: int
    result_id: str
    generation: int
    posting_period: str
    baseline_result_id: str | None
    bound: BoundPlan


def prepare_result(business_id, result_slot, uow, correction_period):
    validate_contract("core.schema.json", "Id", business_id)
    validate_contract("core.schema.json", "Slot", result_slot)
    if correction_period is not None:
        validate_contract("core.schema.json", "Month", correction_period)
    repo = uow.repository("ledger")
    old = repo.current_result(business_id, result_slot)
    cached = repo.prepared(business_id, result_slot)
    if old and old["state"] == "open":
        identity = old["result_id"], old["generation"] + 1, old["baseline_result_id"]
    else:
        identity = (
            cached.result_id if cached else str(uuid4()),
            1,
            old["result_id"] if old else None,
        )
    if cached is not None:
        if identity != (cached.result_id, cached.generation, cached.baseline_result_id):
            fail("conflict", "已准备的正式结果身份与本事务当前采用不一致。")
        if (
            cached.correction_period is not None
            and correction_period is not None
            and cached.correction_period != correction_period
        ):
            fail("conflict", "同一准备结果不能指定两个不同发现期。")
        if correction_period is None or cached.correction_period == correction_period:
            return cached
    prepared = PreparedResult(business_id, result_slot, *identity, correction_period)
    repo.remember_prepared(prepared)
    return prepared


def source_digest(calculation_key, calculation_revision, basis_digest, target_digest):
    return content_digest(
        {
            "calculation_key": calculation_key,
            "calculation_revision": calculation_revision,
            "basis_digest": basis_digest,
            "target_digest": target_digest,
        }
    )


def source_reference(
    prepared, *, calculation_key, calculation_revision, basis_digest, target_digest
):
    value = {
        "business_id": prepared.business_id,
        "result_slot": prepared.result_slot,
        "result_id": prepared.result_id,
        "result_generation": prepared.generation,
        "content_digest": source_digest(
            calculation_key, calculation_revision, basis_digest, target_digest
        ),
    }
    validate_contract("business-facts.schema.json", "ResultSourceRef", value)
    return value


def stored_source_reference(row):
    return source_reference(
        PreparedResult(
            row["business_id"],
            row["result_slot"],
            row["result_id"],
            row["generation"],
            row["baseline_result_id"],
            None,
        ),
        calculation_key=row["calculation_key"],
        calculation_revision=row["calculation_revision"],
        basis_digest=row["basis_digest"],
        target_digest=row["target_digest"],
    )


def bind_plan(plan, uow, correction_period=None):
    """Choose the actual formal identity once the complete output is known."""
    candidate = prepare_result(plan["business_id"], plan["result_slot"], uow, correction_period)
    repo = uow.repository("ledger")
    plan_digest = content_digest(plan)
    existing = repo.bound_plan(candidate.business_id, candidate.result_slot)
    if existing is not None:
        if existing.plan_digest != plan_digest:
            fail("conflict", "已提供给同单消费者的完整来源计划不能静默改变。")
        return existing
    old = repo.current_result(candidate.business_id, candidate.result_slot)
    unchanged = old is not None and same_formal_output(repo, old, plan)
    if unchanged and old["state"] == "open":
        unchanged = (
            old["basis_digest"] == content_digest(plan["basis"])
            and old["calculation_key"] == plan["calculation_key"]
            and old["calculation_revision"] == plan["calculation_revision"]
            and target_lines(repo, old) == plan["lines"]
        )
    if unchanged:
        identity = PreparedResult(
            old["business_id"],
            old["result_slot"],
            old["result_id"],
            old["generation"],
            old["baseline_result_id"],
            None,
        )
        source = stored_source_reference(old)
    else:
        identity = candidate
        source = source_reference(
            identity,
            calculation_key=plan["calculation_key"],
            calculation_revision=plan["calculation_revision"],
            basis_digest=content_digest(plan["basis"]),
            target_digest=content_digest(plan["target"]),
        )
    bound = BoundPlan(identity, source, unchanged, plan_digest)
    repo.remember_bound_plan(bound)
    return bound


def check_source_references(proposed, repo, retired=()):
    """A fresh consumer uses staged producers before looking at persisted heads.

    A locator already in the consumer's immutable baseline may describe a used
    quantity whose open producer has since changed. That baseline is authority
    for the old locator; retaining the producer's replaced body is unnecessary.
    """
    staged = {}
    exiting = {(row["business_id"], row["result_slot"]) for row in retired}
    for item in proposed:
        plan = item.plan
        if plan["target"]["kind"] == "system.withdrawal":
            exiting.add((plan["business_id"], plan["result_slot"]))
        staged[plan["business_id"], plan["result_slot"]] = item.bound.source_ref
    saved = {}
    for item in proposed:
        if item.bound.unchanged:
            continue
        plan, baseline_id = item.plan, item.baseline_result_id
        inherited = []
        if baseline_id is not None:
            baseline = repo.result(baseline_id)
            if baseline["state"] == "frozen":
                inherited = json.loads(baseline["basis_json"]).get("source_results", [])
        for ref in plan["basis"].get("source_results", []):
            key = ref["business_id"], ref["result_slot"]
            pending = staged.get(key)
            if ref in inherited:
                continue
            if key in exiting:
                fail("dependency_conflict", "本单退出的结果不能再作为新采用依据。")
            if pending == ref:
                continue
            if pending is not None:
                fail("dependency_conflict", "本单来源已重算，消费者须采用同一新结果头及内容。")
            if key not in saved:
                saved[key] = repo.current_result(*key, metadata_only=True)
            row = saved[key]
            if row is None or row["target_state"] == "withdrawn":
                fail("dependency_conflict", "采用依据引用的正式来源不存在。")
            expected = stored_source_reference(row)
            if ref != expected:
                fail("dependency_conflict", "采用依据中的正式来源代次或内容已变化。")
        if plan["basis"]["kind"] == "system.withdrawal":
            if baseline_id is None:
                fail("dependency_conflict", "冲正撤去目标须有被冻结的真实基准。")
            row = repo.result(baseline_id)
            expected = stored_source_reference(row)
            if plan["basis"]["baseline_ref"] != expected:
                fail("dependency_conflict", "撤去依据必须准确引用本业务被冻结的基准。")
