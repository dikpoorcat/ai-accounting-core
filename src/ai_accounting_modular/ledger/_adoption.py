"""Complete result replacement and frozen-baseline correction."""

from __future__ import annotations

from dataclasses import replace

from ai_accounting_modular.contracts.public import ContractError, validate_contract

from ._balances import unique_movements, validate_capacities
from ._formal_output import same_journal, target_voucher
from ._planning import PlannedAdoption, bind_plan, check_source_references
from ._values import check_plan, fail


def adopt(change_plans, uow, correction_period, reference):
    completed = [False]

    def require_complete():
        if not completed[0]:
            fail("invalid_operation", "本单采用未完整成功，不能提交部分结果。")

    uow.check_before_commit(require_complete)
    changes = list(change_plans)
    plans = {}
    responsibilities = {}
    for change in changes:
        validate_contract("business-facts.schema.json", "ChangePlan", change)
        if change["issues"]:
            raise ContractError(
                "needs_information",
                "存在尚未解决的核算问题，不能采用不完整结果。",
                fact_issues=change["issues"],
            )
        for responsibility in change["businesses"]:
            if responsibility["op"] == "handoff_out":
                continue
            business_id = responsibility["business_id"]
            if business_id in responsibilities:
                fail("invalid_input", "一项业务只能有一个完整采用责任。")
            responsibilities[business_id] = responsibility
        for plan in change["plans"]:
            check_plan(plan)
            key = plan["business_id"], plan["result_slot"]
            if key in plans:
                fail("invalid_input", "同一结果槽不能产生两份采用计划。")
            plans[key] = plan
    for business_id, responsibility in responsibilities.items():
        actual = {slot for biz, slot in plans if biz == business_id}
        if actual != set(responsibility["complete_result_slots"]):
            fail("invalid_input", "采用计划与本业务声明的完整结果槽不一致。")
    if any(biz not in responsibilities for biz, _ in plans):
        fail("invalid_input", "采用计划缺少对应业务责任。")
    if not responsibilities:
        completed[0] = True
        return (), (), False
    if correction_period is not None:
        validate_contract("core.schema.json", "Month", correction_period)
    repo = uow.repository("ledger")
    touched = repo.linked_sources(list(responsibilities))
    previous = repo.current_heads(list(responsibilities))
    movement_ids = {
        row["movement_id"]
        for row in repo.children("result_fund_movement", [head["result_id"] for head in previous])
    }
    movement_ids.update(
        move["movement_id"] for plan in plans.values() for move in plan["fund_movements"]
    )
    old_by_key = {(r["business_id"], r["result_slot"]): r for r in previous}
    retired = []
    proposed = []
    for key, old in old_by_key.items():
        if key not in plans:
            if old["state"] == "frozen" or old["baseline_result_id"]:
                fail("dependency_conflict", "冻结结果退出必须有明确的发现期撤去目标。")
            retired.append(old)
    for key, plan in plans.items():
        old = old_by_key.get(key)
        bound = bind_plan(plan, uow, correction_period)
        prepared = bound.identity
        result_id, generation = prepared.result_id, prepared.generation
        effective_correction_period = correction_period or prepared.correction_period
        if bound.unchanged:
            baseline_id, posting_period = old["baseline_result_id"], old["posting_period"]
        elif old and old["state"] == "open":
            baseline_id = old["baseline_result_id"]
            posting_period = old["posting_period"] if baseline_id else plan["accounting_period"]
            if (
                baseline_id
                and effective_correction_period is not None
                and effective_correction_period != posting_period
            ):
                fail(
                    "dependency_conflict", "开放更正头须在原发现期完整替换；不能留下两份开放更正。"
                )
        else:
            baseline_id = old["result_id"] if old else None
            posting_period = (
                effective_correction_period if baseline_id else plan["accounting_period"]
            )
            if posting_period is None:
                fail("closed_period_required", "冻结业务更正须明确一个开放发现期。")
            if not baseline_id and repo.is_closed(posting_period):
                posting_period = effective_correction_period
                if posting_period is None:
                    fail("closed_period_required", "新增闭期事实须明确一个开放发现期。")
        if not bound.unchanged and repo.is_closed(posting_period):
            fail("closed_period_required", "不能向已关账月份写入正式结果。")
        if (
            not bound.unchanged
            and baseline_id
            and posting_period <= repo.result(baseline_id)["posting_period"]
        ):
            fail("closed_period_required", "发现期须晚于被冻结的采用期。")
        responsibility = responsibilities[key[0]]
        fact_seq = responsibility["expected_seq"] + (responsibility["op"] != "recalculate")
        if fact_seq < 1:
            fail("invalid_input", "采用事实序号无效。")
        proposed.append(
            PlannedAdoption(
                plan, fact_seq, result_id, generation, posting_period, baseline_id, bound
            )
        )
    check_source_references(proposed, repo, retired)
    # A caught failure must not allow the caller to commit half of an adoption.
    result_ids = [p.result_id for p in proposed]

    def before_commit():
        repo.check_sealed(result_ids)
        validate_capacities(repo, touched)
        unique_movements(repo, movement_ids)
        live = []
        for item in proposed:
            row = repo.result(item.result_id)
            if row is None or row["generation"] != item.generation:
                continue
            if row["state"] == "frozen" and item.plan["target"]["kind"] != "system.withdrawal":
                item = replace(item, baseline_result_id=row["result_id"])
            live.append(item)
        check_source_references(live, repo)

    uow.check_before_commit(before_commit)
    retired_keys = []
    for old in retired:
        for table in ("obligation", "cost_lot"):
            retired_keys.extend(
                (old["business_id"], x["public_slot"])
                for x in repo.children(table, [old["result_id"]])
            )
        repo.remove_open(old)
    for item in proposed:
        if item.bound.unchanged:
            repo.sync_open_fact_seq(item.result_id, item.fact_seq)
            continue
        plan, result_id = item.plan, item.result_id
        baseline_id, posting_period = item.baseline_result_id, item.posting_period
        specs = []
        if baseline_id:
            baseline = repo.result(baseline_id)
            original = target_voucher(repo, baseline)
            baseline_lines = original["lines"] if original else []
            if baseline["business_period"] != plan["accounting_period"] or not same_journal(
                baseline_lines, plan["lines"]
            ):
                if original:
                    reversal = [
                        dict(line, debit_fen=line["credit_fen"], credit_fen=line["debit_fen"])
                        for line in original["lines"]
                    ]
                    specs.append(("reversal", reversal, original["voucher_id"]))
                if plan["lines"]:
                    specs.append(("replacement", plan["lines"], None))
        elif plan["lines"]:
            specs.append(("original", plan["lines"], None))
        repo.clear_children(result_id)
        repo.save_head(
            plan,
            item.fact_seq,
            result_id=result_id,
            generation=item.generation,
            posting_period=posting_period,
            baseline_id=baseline_id,
        )
        repo.write_vouchers(result_id, posting_period, specs)
        repo.write_resources(result_id, plan)
    touched.update(repo.linked_sources(list(responsibilities)))
    validate_capacities(repo, touched, retired_keys)
    unique_movements(repo, movement_ids)
    repo.check_sealed(result_ids)
    for business_id, slot in plans:
        repo.consume_prepared(business_id, slot)
    completed[0] = True
    return (
        tuple(reference(repo.result(result_id), repo) for result_id in result_ids),
        tuple({"business_id": r["business_id"], "slot": r["result_slot"]} for r in retired),
        bool(retired) or any(not item.bound.unchanged for item in proposed),
    )
