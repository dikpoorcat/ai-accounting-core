"""One transaction's complete resource replacement, without a second ledger."""

from __future__ import annotations

from calendar import monthrange
from collections import defaultdict
from copy import deepcopy

from ai_accounting_modular.contracts.public import (
    ContractError,
    ResourceCutoff,
    ResourceState,
    ResourceUse,
    ResourceWindow,
    content_digest,
    format_fen,
    validate_contract,
)

from . import _aggregation
from ._balances import obligation_remaining, sum_amounts, unique_movements
from ._planning import bind_plan
from ._publications import Publications
from ._resource_promises import ResourceUses, exclusions
from ._resource_view import (
    ResourceRepository,
    definitions,
    fund_movements,
    validate_resource_refs,
)
from ._timing import ResultTimes
from ._values import check_plan, fail, unique


class CandidateLedger:
    """Definitions and promised local windows; full state after every slot arrives."""

    def __init__(
        self, responsibilities, unit, correction_period=None, *, publications=(), resource_uses=None
    ):
        if unit.view != "current":
            fail("invalid_input", "候选资源只能建立在当前公司工作单元。")
        supplied = [deepcopy(item) for item in responsibilities]
        for item in supplied:
            validate_contract("business-facts.schema.json", "BusinessChangeResponsibility", item)
        items = [item for item in supplied if item["op"] != "handoff_out"]
        unique((item["business_id"] for item in items), "候选业务责任不能重复。")
        self._unit, self._correction_period = unit, correction_period
        self._repo = ResourceRepository(unit.repository("ledger"), items)
        self._times = ResultTimes(self._repo)
        self._time_version = self._repo.version
        self._expected = {
            (item["business_id"], slot) for item in items for slot in item["complete_result_slots"]
        }
        self._plans = {}
        self._publications = Publications(items, publications)
        self._resource_uses = ResourceUses(items, resource_uses)
        self._complete = False

    def stage(self, plan):
        self._unit.repository("ledger")
        if self._complete:
            fail("invalid_operation", "完整候选集合不能继续添加或修改计划。")
        check_plan(plan)
        self._publications.check_plan(plan)
        self._resource_uses.check_plan(plan)
        key = plan["business_id"], plan["result_slot"]
        if key not in self._expected:
            fail("invalid_input", "候选计划不在已声明的完整责任槽内。")
        if key in self._plans:
            if content_digest(self._plans[key]) != content_digest(plan):
                fail("conflict", "已暂存的完整候选计划不能静默改变。")
            return
        copied = deepcopy(plan)
        bound = bind_plan(copied, self._unit, self._correction_period)
        self._plans[key] = copied
        self._repo.add(copied, bound)

    def complete(self):
        self._unit.repository("ledger")
        if set(self._plans) != self._expected:
            fail("dependency_conflict", "候选集合尚未收到全部已声明结果槽。")
        ids = tuple(self._repo.responsibilities)
        heads = self._repo.current_heads(ids) if ids else ()
        for kind in ("obligation", "cost", "funds"):
            definitions(self._repo, heads, (), kind)
        movement_ids = {
            row["movement_id"]
            for records in self._repo.records.values()
            for row in records["result_fund_movement"]
        }
        if movement_ids:
            unique_movements(self._repo, movement_ids)
        self._complete = True
        return self

    def resource_definitions(self, refs, kind):
        self._unit.repository("ledger")
        validate_resource_refs(refs, kind)
        ids = tuple(dict.fromkeys(ref["business_id"] for ref in refs))
        return definitions(self._repo, self._repo.current_heads(ids) if ids else (), refs, kind)

    def tax_sales(self, period_from, period_to):
        self._unit.repository("ledger")
        self._publications.require_ready("tax_sales", period_from, period_to, self._plans)
        return _aggregation.tax_sales(self._repo, period_from, period_to)

    def pre_tax(self, period_from, period_to):
        self._unit.repository("ledger")
        self._publications.require_ready("pre_tax", period_from, period_to, self._plans)
        return _aggregation.pre_tax(
            self._repo, self._plans, period_from, period_to, self._unit.start_period
        )

    def resource_states(
        self, refs, kind, cutoff, *, excluding_consumer=None, excluding_consumers=()
    ):
        windows = self.resource_windows(
            refs,
            kind,
            cutoff,
            excluding_consumer=excluding_consumer,
            excluding_consumers=excluding_consumers,
        )
        for window in windows:
            if window.uncertain_uses:
                raise ContractError(
                    "needs_information",
                    "该月的非资金变动只有月份，无法确定是否已在所查询实际日前发生。",
                    fact_issues=[
                        {
                            "code": "resource_day_required",
                            "business_id": window.uncertain_uses[0].source["business_id"],
                            "field": "/effective_on",
                            "meaning": "影响本次日截面的非资金变动实际日",
                            "message": (
                                "按月核验无需补日；本次核验月中实际日，需要有来源的先后依据。"
                            ),
                            "allowed_precision": ["day"],
                            "reusable_sources": [],
                            "blocking_scope": "business",
                        }
                    ],
                )
        return tuple(window.certain for window in windows)

    def resource_windows(
        self, refs, kind, cutoff, *, excluding_consumer=None, excluding_consumers=()
    ):
        self._unit.repository("ledger")
        if not isinstance(cutoff, ResourceCutoff):
            fail("invalid_input", "候选资源必须明确经济截止月及可选实际日。")
        validate_resource_refs(refs, kind)
        excluded = exclusions(excluding_consumer, excluding_consumers)
        if not self._complete:
            self._resource_uses.require_ready(refs, kind, cutoff, excluded, self._plans)
        if self._time_version != self._repo.version:
            self._times = ResultTimes(self._repo)
            self._time_version = self._repo.version
        resources = self.resource_definitions(refs, kind)
        self._repo.prepare_states(resources)
        return tuple(self._window(resource, cutoff, excluded) for resource in resources)

    def fund_movements(self, account_id, period_from, period_to):
        self._unit.repository("ledger")
        if not self._complete:
            fail("invalid_operation", "候选结果尚未齐备，不能读取不完整资金范围。")
        return fund_movements(self._repo, account_id, period_from, period_to)

    def _visibility(self, row, table, cutoff, excluded):
        """True is certain, False is outside, None is a month-only boundary use."""
        head = self._repo.result(row["result_id"])
        if (head["business_id"], head["result_slot"]) in excluded:
            return False
        period = head["business_period"]
        if period > cutoff.period:
            return False
        if table == "settlement" and row["method"] == "money":
            movements = self._repo.movements([row["movement_id"]])
            if len(movements) != 1:
                fail("dependency_conflict", "实际资金清偿必须对应唯一有效资金发生。")
            actual_on = movements[0]["actual_on"]
            return actual_on[:7] <= cutoff.period and (cutoff.on is None or actual_on <= cutoff.on)
        if cutoff.on is not None:
            actual_on = self._times.usage_day(row)
            if actual_on is not None:
                return actual_on <= cutoff.on
            if period == cutoff.period:
                last_day = monthrange(int(period[:4]), int(period[5:]))[1]
                if int(cutoff.on[-2:]) < last_day:
                    return None
        return True

    def _window(self, definition, cutoff, excluded):
        value = definition.value
        head = self._times.head(definition.source["result_id"])
        period = head["business_period"]
        actual_on = (
            value["actual_on"] if definition.kind == "funds" else self._times.day(head["result_id"])
        )
        if period > cutoff.period or (
            actual_on is not None and cutoff.on is not None and actual_on > cutoff.on
        ):
            fail("dependency_conflict", "公开资源在所需经济时点尚未成立。")
        totals, uses, uncertain = defaultdict(int), [], []
        for table, rows in self._repo.uses(definition):
            for row in rows:
                visibility = self._visibility(row, table, cutoff, excluded)
                if visibility is False:
                    continue
                if visibility is None:
                    uncertain.append(self._use(row, table))
                    continue
                key = (
                    row["method"]
                    if table == "settlement"
                    else row["mode"]
                    if table == "obligation_reclassification"
                    else "cost"
                )
                totals[key] = sum_amounts((totals[key], row["amount_fen"]))
                uses.append(self._use(row, table))
        amount = int(value["amount_fen"])
        remaining = (
            obligation_remaining(amount, totals)
            if definition.kind == "obligation"
            else sum_amounts((amount, -sum_amounts(totals.values())))
        )
        for group in (uses, uncertain):
            group.sort(
                key=lambda use: (
                    use.source["business_id"],
                    use.source["result_slot"],
                    use.kind,
                    content_digest(use.value),
                )
            )
        return ResourceWindow(
            ResourceState(
                definition,
                totals["money"],
                totals["offset"],
                totals["agency"],
                totals["transfer"],
                totals["reclassify_excess"],
                totals["cost"],
                remaining,
                tuple(uses),
            ),
            tuple(uncertain),
        )

    def _use(self, row, table):
        head = self._repo.result(row["result_id"])
        source = self._repo.source(row["result_id"])
        ref = {"business_id": row["source_business_id"], "slot": row["source_public_slot"]}
        if table == "settlement":
            value = {
                "settlement_id": row["settlement_id"],
                "source": ref,
                "consumer": {
                    "business_id": head["business_id"],
                    "slot": row["consumer_public_slot"],
                },
                "period": row["period"],
                "amount_fen": format_fen(row["amount_fen"]),
                "method": row["method"],
            }
            for key in ("movement_id", "agency_business_id", "agency_public_slot", "actual_on"):
                if row[key] is not None:
                    value[key] = row[key]
            kind = "settlement"
        elif table == "obligation_reclassification":
            value = {
                "relation_id": row["relation_id"],
                "owner_business_id": head["business_id"],
                "source": ref,
                "target": {
                    "business_id": row["target_business_id"],
                    "slot": row["target_public_slot"],
                },
                "amount_fen": format_fen(row["amount_fen"]),
                "mode": row["mode"],
            }
            kind = "reclassification"
        else:
            value = {
                "source": ref,
                "consumer": {
                    "business_id": head["business_id"],
                    "slot": row["consumer_public_slot"],
                },
                "period": row["period"],
                "amount_fen": format_fen(row["amount_fen"]),
            }
            kind = "cost"
        return ResourceUse(kind, source, value)
