"""The ledger's only cross-module entry point.

Plans are trusted-program outputs, never a public AI free-journal API. All reads
and writes use the caller's company-scoped unit and the ledger-owned repository.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Literal

from ai_accounting_modular.contracts.public import (
    MAX_FEN,
    ChangePlan,
    ContractError,
    ReadContext,
    UnitOfWork,
    validate_contract,
)
from ai_accounting_modular.contracts.public import canonical_json as canonical
from ai_accounting_modular.contracts.public import content_digest as digest

from . import _aggregation, _financial
from ._adoption import adopt as _adopt
from ._balances import balance_wire, obligation_balances
from ._candidate import CandidateLedger
from ._cashflow import contributions as _cashflow_contributions
from ._closing import LedgerPeriodCheck, check_period, close_period, closed_report
from ._financial_cells import Issues
from ._financial_selection import Selection
from ._planning import PreparedResult, bind_plan, stored_source_reference
from ._planning import prepare_result as _prepare_result
from ._repository import LedgerRepository
from ._resource_view import (
    definitions,
    resource_definition,
    validate_resource_refs,
)
from ._resource_view import fund_movements as _fund_movements
from ._snapshot import AdoptedBusinessVersion as AdoptedBusinessVersion
from ._snapshot import LedgerSnapshot
from ._snapshot import verify_snapshot as _verify_snapshot
from ._values import fail, unique


@dataclass(frozen=True)
class LedgerQuery:
    """Result/resource months are business months; voucher months are posting months."""

    business_ids: tuple[str, ...] = ()
    period_from: str | None = None
    period_to: str | None = None
    limit: int = 500
    cursor: str | None = None
    roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class LedgerPage:
    items: tuple[dict, ...]
    total: int
    head: int
    next_cursor: str | None


@dataclass(frozen=True)
class AdoptionResult:
    """Whether formal adoption changed, excluding reference metadata synchronization."""

    results: tuple[dict, ...]
    retired_slots: tuple[dict, ...]
    changed: bool


def repository_factory():
    from ai_accounting_modular.storage.public import RepositoryFactory, module_schema

    return RepositoryFactory(module_schema("ledger"), LedgerRepository)


def _reference(row, repo, vouchers=None):
    value = {
        key: row[key]
        for key in (
            "business_id",
            "result_slot",
            "result_id",
            "generation",
            "business_period",
            "posting_period",
            "state",
        )
    }
    if row["baseline_result_id"] is not None:
        value["baseline_result_id"] = row["baseline_result_id"]
    original = next(
        (
            v
            for v in (repo.vouchers([row["result_id"]]) if vouchers is None else vouchers)
            if v["role"] in ("original", "replacement")
        ),
        None,
    )
    if original:
        value.update(voucher_id=original["voucher_id"], voucher_number=original["number"])
    return value


def _exact_row(repo, result_id, generation, *, mismatch_code="snapshot_changed"):
    row = repo.result(result_id)
    if row is None:
        fail("not_found", "指定正式结果不存在。")
    if row["generation"] != generation:
        fail(mismatch_code, "指定的开放结果已被替换，请重新读取。")
    return row


def _voucher_wire(voucher, row, repo, reference=None):
    value = {
        key: voucher[key]
        for key in (
            "voucher_id",
            "period",
            "number",
            "role",
            "summary",
            "lines",
        )
    }
    value["result"] = reference if reference is not None else _reference(row, repo)
    if voucher["reverses_voucher_id"] is not None:
        value["reverses_voucher_id"] = voucher["reverses_voucher_id"]
    return value


class Ledger:
    def verify_snapshot(self, read_context: ReadContext) -> LedgerSnapshot:
        return _verify_snapshot(read_context)

    def financial_snapshot(self, scope, read_context: ReadContext):
        normalized = _financial.normalize(scope, read_context)
        if normalized["view"] == "as_posted" and read_context.repository("ledger").is_closed(
            normalized["period_to"], start_period=read_context.start_period
        ):
            return closed_report(scope, read_context)
        return _financial.snapshot(scope, read_context)

    def check_period(self, period, read_context: ReadContext) -> LedgerPeriodCheck:
        return check_period(period, read_context)

    def close_period(self, period, expected_head, uow: UnitOfWork) -> LedgerPeriodCheck:
        return close_period(period, expected_head, uow)

    def get_closed_report(self, scope, read_context: ReadContext):
        return closed_report(scope, read_context)

    def closed_through(self, read_context: ReadContext):
        return read_context.repository("ledger").closed_through()

    def query_cashflow_contributions(self, query: LedgerQuery, read_context: ReadContext):
        """Formal adoption-month history, just like query_vouchers; never actual money count."""
        self._validate_query(query)
        if query.roles:
            fail("invalid_input", "现金流采用明细不接受凭证角色筛选。")
        repo = read_context.repository("ledger")
        if read_context.view not in ("current", "as_posted") or (
            read_context.view == "as_posted" and read_context.as_of is None
        ):
            fail("invalid_input", "正式现金流读取口径与截止月不一致。")
        last = (
            query.period_to
            or read_context.as_of
            or repo.latest_adoption_period()
            or read_context.start_period
        )
        if read_context.view == "as_posted":
            last = min(last, read_context.as_of)
        first = query.period_from or read_context.start_period
        selection = Selection(repo, read_context.view, last, read_context.start_period, Issues())
        items = _cashflow_contributions(selection, first, last, query.business_ids)
        return self._page(items, query, read_context, "cashflow_contributions")

    def latest_period(self, read_context: ReadContext):
        """Latest economic month already represented by formal results."""
        return read_context.repository("ledger").latest_period()

    def begin_candidate(
        self,
        responsibilities,
        uow: UnitOfWork,
        *,
        correction_period=None,
        publications=(),
        resource_uses=None,
        retained_results=(),
    ):
        return CandidateLedger(
            responsibilities,
            uow,
            correction_period,
            publications=publications,
            resource_uses=resource_uses,
            retained_results=retained_results,
        )

    def _current_aggregate_view(self, context):
        if context.view != "current" or context.as_of is not None:
            fail(
                "unsupported_business", "本取数端口当前只支持完整当前目标，尚未交付as_posted口径。"
            )
        return self._resource_view(context)

    def tax_sales(self, period_from, period_to, read_context: ReadContext):
        return _aggregation.tax_sales(
            self._current_aggregate_view(read_context), period_from, period_to
        )

    def pre_tax(self, period_from, period_to, read_context: ReadContext):
        return _aggregation.pre_tax(
            self._current_aggregate_view(read_context),
            {},
            period_from,
            period_to,
            read_context.start_period,
        )

    def resource_definitions(self, refs, kind, read_context: ReadContext):
        validate_resource_refs(refs, kind)
        if not refs:
            return ()
        ids = tuple(dict.fromkeys(ref["business_id"] for ref in refs))
        repo = self._resource_view(read_context)
        return definitions(repo, repo.current_heads(ids), refs, kind)

    def _resource_view(self, context):
        if context.view not in ("current", "as_posted") or (
            context.view == "as_posted" and context.as_of is None
        ):
            fail("invalid_input", "资源读取口径与采用截止月不一致。")
        return context.repository("ledger").resource_view(
            as_of=context.as_of if context.view == "as_posted" else None
        )

    def fund_movements(self, account_id, period_from, period_to, read_context: ReadContext):
        return _fund_movements(
            self._resource_view(read_context),
            account_id,
            period_from,
            period_to,
        )

    def prepare_result(
        self, business_id, result_slot, uow: UnitOfWork, *, correction_period=None
    ) -> PreparedResult:
        return _prepare_result(business_id, result_slot, uow, correction_period)

    def get_result_source(self, business_id, result_slot, context: ReadContext, *, plan=None):
        validate_contract("core.schema.json", "Slot", result_slot)
        if plan is not None:
            validate_contract("business-facts.schema.json", "PostingPlan", plan)
            if (plan["business_id"], plan["result_slot"]) != (business_id, result_slot):
                fail("invalid_input", "同单来源计划与所请求业务槽不一致。")
            return dict(bind_plan(plan, context).source_ref)
        _, heads = self._heads(LedgerQuery((business_id,)), context)
        row = next((h for h in heads if h["result_slot"] == result_slot), None)
        if row is None:
            fail("dependency_conflict", "所需正式来源尚未采用。")
        return stored_source_reference(row)

    def adopt(
        self,
        change_plans: list[ChangePlan],
        uow: UnitOfWork,
        *,
        correction_period=None,
        explanation=None,
    ) -> AdoptionResult:
        refs, retired, changed = _adopt(change_plans, uow, correction_period, explanation)
        return AdoptionResult(refs, retired, changed)

    def _validate_query(self, query):
        if not isinstance(query, LedgerQuery):
            raise TypeError("query must be LedgerQuery")
        if type(query.limit) is not int or not 1 <= query.limit <= 500:
            fail("invalid_input", "每页数量须为1至500。")
        if len(query.business_ids) > 500:
            fail("invalid_input", "一次最多查询500个业务身份。")
        unique(query.business_ids, "查询业务身份不能重复。")
        unique(query.roles, "凭证角色筛选不能重复。")
        if any(
            role not in {"original", "reversal", "replacement", "reclassification", "closing"}
            for role in query.roles
        ):
            fail("invalid_input", "未知的正式凭证角色。")
        for item in query.business_ids:
            validate_contract("core.schema.json", "Id", item)
        for month in (query.period_from, query.period_to):
            if month is not None:
                validate_contract("core.schema.json", "Month", month)
        if query.period_from and query.period_to and query.period_from > query.period_to:
            fail("invalid_input", "查询起月不能晚于末月。")

    def _heads(self, query, context):
        self._validate_query(query)
        repo = context.repository("ledger")
        if context.view == "as_posted":
            if context.as_of is None:
                fail("invalid_input", "历史月末读取须明确截止月。")
            heads = repo.heads_through(context.as_of, query.business_ids)
        elif context.view == "current":
            heads = repo.current_heads(query.business_ids)
        else:
            fail("invalid_input", "未知的正式读取口径。")
        return repo, [
            h
            for h in heads
            if (
                (not query.business_ids or h["business_id"] in query.business_ids)
                and (query.period_from is None or h["business_period"] >= query.period_from)
                and (query.period_to is None or h["business_period"] <= query.period_to)
            )
        ]

    def get_results(self, business_ids, read_context: ReadContext, *, result_refs=()):
        if len(result_refs) > 500:
            fail("invalid_input", "一次最多读取500个精确正式结果。")
        if not business_ids and not result_refs:
            return ()
        repo = read_context.repository("ledger")
        heads = []
        if business_ids:
            _, heads = self._heads(LedgerQuery(tuple(business_ids)), read_context)
        selected = {row["result_id"]: row for row in heads}
        for ref in result_refs:
            validate_contract("core.schema.json", "ResultRef", ref)
            row = _exact_row(repo, ref["result_id"], ref["generation"])
            if any(
                row[key] != ref[key]
                for key in (
                    "business_id",
                    "result_slot",
                    "generation",
                    "business_period",
                    "posting_period",
                )
            ):
                fail("snapshot_changed", "指定的开放结果已被替换，请重新读取。")
            selected[row["result_id"]] = row
        return self._views(repo, selected.values())

    def get_result_refs(self, business_ids, read_context: ReadContext):
        query = LedgerQuery(tuple(business_ids))
        self._validate_query(query)
        if read_context.view not in ("current", "as_posted") or (
            read_context.view == "as_posted" and read_context.as_of is None
        ):
            fail("invalid_input", "正式结果引用的读取口径与截止月不一致。")
        if not business_ids:
            return ()
        rows = read_context.repository("ledger").result_references(
            business_ids, as_of=read_context.as_of if read_context.view == "as_posted" else None
        )
        return tuple(validate_contract("core.schema.json", "ResultRef", row) for row in rows)

    def get_result(self, result_id, generation, read_context: ReadContext):
        validate_contract("core.schema.json", "Id", result_id)
        if type(generation) is not int or not 1 <= generation <= MAX_FEN:
            fail("invalid_input", "正式结果代次须为有效的正整数。")
        repo = read_context.repository("ledger")
        row = _exact_row(repo, result_id, generation, mismatch_code="conflict")
        return self._views(repo, [row])[0]

    def _views(self, repo, heads):
        heads = sorted(
            heads, key=lambda r: (r["business_id"], r["result_slot"], r["posting_period"])
        )
        vouchers = repo.vouchers([row["result_id"] for row in heads])
        grouped = {row["result_id"]: [] for row in heads}
        for voucher in vouchers:
            grouped[voucher["result_id"]].append(voucher)
        views = []
        for row in heads:
            owned_vouchers = grouped[row["result_id"]]
            ref = _reference(row, repo, owned_vouchers)
            view = {
                "reference": ref,
                "target": json.loads(row["target_json"]),
                "basis": json.loads(row["basis_json"]),
                "vouchers": [_voucher_wire(v, row, repo, ref) for v in owned_vouchers],
            }
            validate_contract("core.schema.json", "ResultView", view)
            views.append(view)
        return tuple(views)

    def _page(self, items, query, context, collection):
        identity = digest(
            {
                "company_id": context.company_id,
                "database_id": context.database_id,
                "head": context.head,
                "view": context.view,
                "as_of": context.as_of,
                "business_ids": list(query.business_ids),
                "from": query.period_from,
                "to": query.period_to,
                "roles": list(query.roles),
                "collection": collection,
            }
        )
        offset = 0
        if query.cursor:
            try:
                cursor = json.loads(base64.urlsafe_b64decode(query.cursor.encode("ascii")))
                offset = cursor["offset"]
                if (
                    type(cursor) is not dict
                    or set(cursor) != {"scope", "offset"}
                    or type(offset) is not int
                    or offset < 0
                ):
                    raise ValueError
            except (ValueError, KeyError, TypeError, UnicodeError) as exc:
                raise ContractError("invalid_input", "分页标识无效。") from exc
            if cursor["scope"] != identity:
                fail("snapshot_changed", "分页期间或正式结果已变化，请从第一页读取。")
        page = items[offset : offset + query.limit]
        next_cursor = None
        if offset + len(page) < len(items):
            next_cursor = base64.urlsafe_b64encode(
                canonical(
                    {
                        "scope": identity,
                        "offset": offset + len(page),
                    }
                ).encode("utf-8")
            ).decode("ascii")
        return LedgerPage(tuple(page), len(items), context.head, next_cursor)

    def query_results(self, query: LedgerQuery, read_context: ReadContext):
        repo, heads = self._heads(query, read_context)
        items = list(self._views(repo, heads))
        return self._page(items, query, read_context, "results")

    def query_vouchers(self, query: LedgerQuery, read_context: ReadContext):
        self._validate_query(query)
        if read_context.view not in ("current", "as_posted") or (
            read_context.view == "as_posted" and read_context.as_of is None
        ):
            fail("invalid_input", "凭证读取口径与截止月不一致。")
        repo = read_context.repository("ledger")
        # Voucher history is formal posting history; current-head resource
        # selection cannot erase already frozen original/correction vouchers.
        heads = repo.formal_heads_through(
            read_context.as_of if read_context.view == "as_posted" else None, query.business_ids
        )
        by_id = {h["result_id"]: h for h in heads}
        vouchers = repo.vouchers(list(by_id))
        grouped = {key: [] for key in by_id}
        for voucher in vouchers:
            grouped[voucher["result_id"]].append(voucher)
        references = {key: _reference(row, repo, grouped[key]) for key, row in by_id.items()}
        items = [
            _voucher_wire(v, by_id[v["result_id"]], repo, references[v["result_id"]])
            for v in vouchers
            if (not query.roles or v["role"] in query.roles)
            and (query.period_from is None or v["period"] >= query.period_from)
            and (query.period_to is None or v["period"] <= query.period_to)
        ]
        return self._page(items, query, read_context, "vouchers")

    def query_obligations(self, query: LedgerQuery, read_context: ReadContext):
        repo, heads = self._heads(query, read_context)
        items = self._obligations(repo, heads, read_context)
        return self._page(items, query, read_context, "obligations")

    def _obligations(self, repo, heads, read_context):
        by_id = {h["result_id"]: h for h in heads}
        vouchers = repo.vouchers(list(by_id))
        grouped = {key: [] for key in by_id}
        for voucher in vouchers:
            grouped[voucher["result_id"]].append(voucher)
        references = {key: _reference(row, repo, grouped[key]) for key, row in by_id.items()}
        items = []
        for ob, totals, remaining in obligation_balances(
            repo,
            heads,
            through=read_context.as_of if read_context.view == "as_posted" else None,
        ):
            row = by_id[ob["result_id"]]
            wire = resource_definition("obligation", row, ob, stored_source_reference(row)).value
            item = {
                "obligation": wire,
                "balance": balance_wire(ob, totals, remaining, references[row["result_id"]]),
            }
            validate_contract("core.schema.json", "ObligationView", item)
            items.append(item)
        items.sort(key=lambda x: (x["obligation"]["business_id"], x["obligation"]["slot"]))
        return items

    def resolve(self, refs, kind: Literal["result", "obligation", "cost", "funds"], read_context):
        if not refs:
            return ()
        if len(refs) > 500:
            fail("invalid_input", "一次最多解析500个公开引用。")
        for ref in refs:
            validate_contract("core.schema.json", "BusinessRef", ref)
        ids = tuple(dict.fromkeys(r["business_id"] for r in refs))
        if kind == "result":
            values = self.get_results(ids, read_context)
            mapped = {
                (x["reference"]["business_id"], x["reference"]["result_slot"]): x for x in values
            }
        elif kind == "obligation":
            repo, heads = self._heads(LedgerQuery(ids), read_context)
            values = self._obligations(repo, heads, read_context)
            mapped = {(x["obligation"]["business_id"], x["obligation"]["slot"]): x for x in values}
        else:
            fail("unsupported_business", "该资源解析能力尚未交付。")
        result = []
        for ref in refs:
            value = mapped.get((ref["business_id"], ref["slot"]))
            if value is None:
                fail("dependency_conflict", "引用的当前公开资源不存在。")
            result.append(value)
        return tuple(result)


def create_ledger() -> Ledger:
    return Ledger()
