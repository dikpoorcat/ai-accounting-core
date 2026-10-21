"""The ledger's only cross-module entry point.

Plans are trusted-program outputs, never a public AI free-journal API. All reads
and writes use the caller's company-scoped unit and the ledger-owned repository.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from ai_accounting_modular.contracts.public import (
    MAX_FEN,
    BusinessRef,
    ChangePlan,
    ContractError,
    LedgerScope,
    PageQuery,
    QuerySelection,
    ReadContext,
    ReadPage,
    ResultRef,
    ResultSourceRef,
    UnitOfWork,
    Voucher,
    query_selection,
    validate_contract,
)
from ai_accounting_modular.contracts.public import canonical_json as canonical
from ai_accounting_modular.contracts.public import content_digest as digest

from . import (
    _accounts,
    _aggregation,
    _financial,
    _financial_reads,
    _read_contents,
    _read_events,
    _read_positions,
)
from ._adoption import adopt as _adopt
from ._adoption_changes import FormalChange as FormalChange
from ._balances import balance_wire, obligation_balances
from ._candidate import CandidateLedger
from ._cashflow import contributions as _cashflow_contributions
from ._closing import (
    LedgerPeriodCheck,
    PeriodClosure,
    check_period,
    close_period,
    closed_report,
    period_closure,
)
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
from ._types import (
    AccountBalance as AccountBalance,
)
from ._types import (
    AccountBalanceKey as AccountBalanceKey,
)
from ._types import (
    AdoptedContent as AdoptedContent,
)
from ._types import (
    AdoptedDirectoryEntry as AdoptedDirectoryEntry,
)
from ._types import (
    AdoptedTarget as AdoptedTarget,
)
from ._types import (
    AdoptedValues as AdoptedValues,
)
from ._types import (
    CostPositionQuery as CostPositionQuery,
)
from ._types import (
    FinancialSummary as FinancialSummary,
)
from ._types import (
    ObligationPositionQuery as ObligationPositionQuery,
)
from ._types import (
    PositionAmounts as PositionAmounts,
)
from ._types import (
    PositionPage as PositionPage,
)
from ._types import (
    PositionSelection as PositionSelection,
)
from ._types import (
    PositionTotal as PositionTotal,
)
from ._types import (
    ResourcePosition as ResourcePosition,
)
from ._types import (
    SettlementEvent as SettlementEvent,
)
from ._types import (
    SettlementEventQuery as SettlementEventQuery,
)
from ._types import (
    VoucherDirectoryEntry as VoucherDirectoryEntry,
)
from ._types import (
    VoucherLocator as VoucherLocator,
)
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
    formal_changes: tuple[FormalChange, ...]


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
    def query_business_settlements(
        self,
        query: SettlementEventQuery,
        page: PageQuery,
        read_context: ReadContext,
        *,
        selection: QuerySelection | None = None,
    ) -> ReadPage[SettlementEvent]:
        """Count and version only settlements within the specified known identities."""
        return _read_events.query_settlement_events(
            query, page, read_context, selection, business_scope=True
        )

    def query_settlement_events(
        self,
        query: SettlementEventQuery,
        page: PageQuery,
        read_context: ReadContext,
        *,
        selection: QuerySelection | None = None,
    ) -> ReadPage[SettlementEvent]:
        return _read_events.query_settlement_events(query, page, read_context, selection)

    def query_obligation_positions(
        self,
        query: ObligationPositionQuery,
        page: PageQuery,
        read_context: ReadContext,
        *,
        selection: QuerySelection | None = None,
    ) -> PositionPage:
        return _read_positions.query_positions(
            query, page, read_context, selection, kind="obligation"
        )

    def query_cost_positions(
        self,
        query: CostPositionQuery,
        page: PageQuery,
        read_context: ReadContext,
        *,
        selection: QuerySelection | None = None,
    ) -> PositionPage:
        return _read_positions.query_positions(query, page, read_context, selection, kind="cost")

    def capture_obligation_positions(
        self,
        query: ObligationPositionQuery,
        read_context: ReadContext,
        *,
        selection: QuerySelection | None = None,
    ) -> PositionSelection:
        """Complete filtered obligations, retaining the wide query's full-scope totals."""
        return _read_positions.capture_positions(query, read_context, selection, kind="obligation")

    def capture_cost_positions(
        self,
        query: CostPositionQuery,
        read_context: ReadContext,
        *,
        selection: QuerySelection | None = None,
    ) -> PositionSelection:
        """Complete filtered costs, retaining the wide query's full-scope totals."""
        return _read_positions.capture_positions(query, read_context, selection, kind="cost")

    def get_obligation_positions(
        self,
        cutoff_period: str,
        read_context: ReadContext,
        *,
        business_ids: Sequence[str] = (),
        references: Sequence[BusinessRef] = (),
        selection: QuerySelection | None = None,
    ) -> tuple[ResourcePosition, ...]:
        """Complete positions of known businesses/keys; no whole-company statistics."""
        return _read_positions.get_positions(
            cutoff_period,
            read_context,
            selection,
            kind="obligation",
            business_ids=business_ids,
            references=references,
        )

    def get_cost_positions(
        self,
        cutoff_period: str,
        read_context: ReadContext,
        *,
        business_ids: Sequence[str] = (),
        references: Sequence[BusinessRef] = (),
        selection: QuerySelection | None = None,
    ) -> tuple[ResourcePosition, ...]:
        """Complete positions of known businesses/keys; absent resources stay absent."""
        return _read_positions.get_positions(
            cutoff_period,
            read_context,
            selection,
            kind="cost",
            business_ids=business_ids,
            references=references,
        )

    def account_names(self, codes: Sequence[str]) -> dict[str, str]:
        """Display labels for controlled subjects; this does not open a database."""
        return _accounts.names(codes)

    def financial_summary(self, scope: LedgerScope, read_context: ReadContext) -> FinancialSummary:
        return _financial_reads.financial_summary(scope, read_context)

    def financial_summary_inputs(self, names):
        """Public dependencies of the selected explicit balance fields."""
        return _financial_reads.summary_inputs(names)

    def account_balances(
        self,
        keys: Sequence[AccountBalanceKey],
        period: str,
        read_context: ReadContext,
        *,
        selection: QuerySelection | None = None,
    ) -> tuple[AccountBalance, ...]:
        return _financial_reads.account_balances(keys, period, read_context, selection)

    def query_adopted_directory(
        self,
        scope: LedgerScope,
        kinds: tuple[str, ...],
        page: PageQuery,
        read_context: ReadContext,
        *,
        business_ids=(),
        result_slots=(),
    ) -> ReadPage[AdoptedDirectoryEntry]:
        return _read_contents.query_adopted(
            scope, kinds, page, read_context, business_ids=business_ids, result_slots=result_slots
        )

    def query_adopted_targets(
        self,
        scope: LedgerScope,
        kinds: tuple[str, ...],
        page: PageQuery,
        read_context: ReadContext,
        *,
        business_ids=(),
        result_slots=(),
    ) -> ReadPage[AdoptedTarget]:
        """Selected complete targets, without calculation bases or earlier versions."""
        return _read_contents.query_adopted(
            scope,
            kinds,
            page,
            read_context,
            targets=True,
            business_ids=business_ids,
            result_slots=result_slots,
        )

    def query_adopted_values(
        self,
        scope: LedgerScope,
        kinds: tuple[str, ...],
        page: PageQuery,
        read_context: ReadContext,
    ) -> ReadPage[AdoptedValues]:
        return _read_contents.query_adopted(scope, kinds, page, read_context, flow=True)

    def get_adopted_contents(
        self,
        locators: Sequence[ResultRef],
        read_context: ReadContext,
    ) -> tuple[AdoptedContent, ...]:
        return _read_contents.get_adopted_contents(locators, read_context)

    def get_adopted_sources(
        self,
        sources: Sequence[ResultSourceRef],
        read_context: ReadContext,
    ) -> tuple[AdoptedDirectoryEntry, ...]:
        """Locate exact retained sources, including a consumer's earlier frozen basis."""
        return _read_contents.get_adopted_sources(sources, read_context)

    def query_voucher_directory(
        self,
        scope: LedgerScope,
        kinds: tuple[str, ...],
        page: PageQuery,
        read_context: ReadContext,
        *,
        business_ids=(),
        roles=(),
    ) -> ReadPage[VoucherDirectoryEntry]:
        return _read_contents.query_voucher_directory(
            scope, kinds, page, read_context, business_ids=business_ids, roles=roles
        )

    def get_effective_voucher_directory(
        self,
        locators: Sequence[ResultRef],
        read_context: ReadContext,
    ) -> tuple[VoucherDirectoryEntry | None, ...]:
        """Locate each target's real journal, following formal-only baseline inheritance."""
        return _read_contents.get_effective_voucher_directory(locators, read_context)

    def get_vouchers(
        self,
        locators: Sequence[VoucherLocator],
        read_context: ReadContext,
    ) -> tuple[Voucher, ...]:
        return _read_contents.get_vouchers(locators, read_context)

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

    def period_closure(self, period, read_context: ReadContext) -> PeriodClosure:
        return period_closure(period, read_context)

    def query_cashflow_contributions(
        self, query: LedgerQuery, read_context: ReadContext, *, selection=None
    ):
        """Formal adoption-month history, just like query_vouchers; never actual money count."""
        self._validate_query(query)
        if query.roles:
            fail("invalid_input", "现金流采用明细不接受凭证角色筛选。")
        repo = read_context.repository("ledger")
        selected = query_selection(read_context, selection)
        last = (
            query.period_to
            or selected.as_of
            or repo.latest_adoption_period()
            or read_context.start_period
        )
        if selected.view == "as_posted":
            last = min(last, selected.as_of)
        first = query.period_from or read_context.start_period
        selector = Selection(repo, selected.view, last, read_context.start_period, Issues())
        items = _cashflow_contributions(selector, first, last, query.business_ids)
        return self._page(items, query, read_context, "cashflow_contributions", selected)

    def latest_period(self, read_context: ReadContext):
        """Latest economic month already represented by formal results."""
        return read_context.repository("ledger").latest_period()

    def has_formal_results(self, read_context: ReadContext) -> bool:
        """Any adopted result counts, including zero, voucherless and retired results."""
        return read_context.repository("ledger").has_formal_results()

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

    def _current_aggregate_view(self, context, selection=None):
        selected = query_selection(context, selection)
        if selected.view != "current":
            fail(
                "unsupported_business", "本取数端口当前只支持完整当前目标，尚未交付as_posted口径。"
            )
        return self._resource_view(context, selected)

    def tax_sales(self, period_from, period_to, read_context: ReadContext, *, selection=None):
        return _aggregation.tax_sales(
            self._current_aggregate_view(read_context, selection), period_from, period_to
        )

    def pre_tax(self, period_from, period_to, read_context: ReadContext, *, selection=None):
        return _aggregation.pre_tax(
            self._current_aggregate_view(read_context, selection),
            {},
            period_from,
            period_to,
            read_context.start_period,
        )

    def resource_definitions(self, refs, kind, read_context: ReadContext, *, selection=None):
        validate_resource_refs(refs, kind)
        if not refs:
            return ()
        ids = tuple(dict.fromkeys(ref["business_id"] for ref in refs))
        repo = self._resource_view(read_context, selection)
        return definitions(repo, repo.current_heads(ids), refs, kind)

    def _resource_view(self, context, selection=None):
        selected = query_selection(context, selection)
        return context.repository("ledger").resource_view(as_of=selected.as_of)

    def fund_movements(
        self, account_id, period_from, period_to, read_context: ReadContext, *, selection=None
    ):
        return _fund_movements(
            self._resource_view(read_context, selection),
            account_id,
            period_from,
            period_to,
        )

    def prepare_result(
        self, business_id, result_slot, uow: UnitOfWork, *, correction_period=None
    ) -> PreparedResult:
        return _prepare_result(business_id, result_slot, uow, correction_period)

    def get_result_source(
        self, business_id, result_slot, context: ReadContext, *, plan=None, selection=None
    ):
        validate_contract("core.schema.json", "Slot", result_slot)
        if plan is not None:
            validate_contract("business-facts.schema.json", "PostingPlan", plan)
            if (plan["business_id"], plan["result_slot"]) != (business_id, result_slot):
                fail("invalid_input", "同单来源计划与所请求业务槽不一致。")
            return dict(bind_plan(plan, context).source_ref)
        _, heads = self._heads(LedgerQuery((business_id,)), context, selection)
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
        refs, retired, changed, formal_changes = _adopt(
            change_plans, uow, correction_period, explanation
        )
        return AdoptionResult(refs, retired, changed, formal_changes)

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

    def _heads(self, query, context, selection=None):
        self._validate_query(query)
        repo = context.repository("ledger")
        selected = query_selection(context, selection)
        if selected.view == "as_posted":
            heads = repo.heads_through(selected.as_of, query.business_ids)
        elif selected.view == "current":
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

    def get_results(
        self, business_ids, read_context: ReadContext, *, result_refs=(), selection=None
    ):
        if len(result_refs) > 500:
            fail("invalid_input", "一次最多读取500个精确正式结果。")
        if not business_ids and not result_refs:
            return ()
        repo = read_context.repository("ledger")
        heads = []
        if business_ids:
            _, heads = self._heads(LedgerQuery(tuple(business_ids)), read_context, selection)
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

    def get_result_refs(self, business_ids, read_context: ReadContext, *, selection=None):
        query = LedgerQuery(tuple(business_ids))
        self._validate_query(query)
        selected = query_selection(read_context, selection)
        if not business_ids:
            return ()
        rows = read_context.repository("ledger").result_references(
            business_ids, as_of=selected.as_of
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

    def _page(self, items, query, context, collection, selection=None):
        selected = query_selection(context, selection)
        identity = digest(
            {
                "company_id": context.company_id,
                "database_id": context.database_id,
                "head": context.head,
                "view": selected.view,
                "as_of": selected.as_of,
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

    def _query_scope(self, query, read_context, selection):
        self._validate_query(query)
        selected = query_selection(read_context, selection)
        repo = read_context.repository("ledger")
        last = (
            query.period_to
            or selected.as_of
            or max(
                repo.latest_period() or read_context.start_period,
                repo.latest_adoption_period() or read_context.start_period,
                query.period_from or read_context.start_period,
            )
        )
        first = query.period_from or min(read_context.start_period, last)
        scope = {
            "company_id": read_context.company_id,
            "period_from": first,
            "period_to": last,
            "view": selected.view,
        }
        if selected.as_of is not None:
            scope["as_of"] = selected.as_of
        return scope

    def query_results(self, query: LedgerQuery, read_context: ReadContext, *, selection=None):
        scope = self._query_scope(query, read_context, selection)
        page = _read_contents.query_adopted(
            scope,
            (),
            PageQuery(query.limit, query.cursor),
            read_context,
            business_ids=query.business_ids,
            economic_from=query.period_from,
        )
        repo = read_context.repository("ledger")
        heads = repo.results([item.reference["result_id"] for item in page.items])
        items = self._views(repo, heads)
        return LedgerPage(items, page.filtered_count, read_context.head, page.next_cursor)

    def query_vouchers(self, query: LedgerQuery, read_context: ReadContext, *, selection=None):
        scope = self._query_scope(query, read_context, selection)
        page = self.query_voucher_directory(
            scope,
            (),
            PageQuery(query.limit, query.cursor),
            read_context,
            business_ids=query.business_ids,
            roles=query.roles,
        )
        locators = [
            VoucherLocator(item.voucher_id, item.result["result_id"], item.result["generation"])
            for item in page.items
        ]
        return LedgerPage(
            self.get_vouchers(locators, read_context),
            page.filtered_count,
            read_context.head,
            page.next_cursor,
        )

    def query_obligations(self, query: LedgerQuery, read_context: ReadContext, *, selection=None):
        scope = self._query_scope(query, read_context, selection)
        positions = _read_positions.query_positions(
            ObligationPositionQuery(scope["period_to"], business_ids=query.business_ids),
            PageQuery(query.limit, query.cursor),
            read_context,
            selection,
            kind="obligation",
            economic_from=query.period_from,
        ).page
        repo = read_context.repository("ledger")
        heads = {
            row["result_id"]: row
            for row in repo.read_heads(
                {item.definition.source["result_id"] for item in positions.items}
            )
        }
        items = []
        for item in positions.items:
            definition, amounts = item.definition, item.amounts
            wire = definition.value
            row = {
                **wire,
                "public_slot": wire["slot"],
                "party_id": wire.get("party_id"),
                "amount_fen": amounts.amount_fen,
            }
            totals = {
                "money": amounts.money_settled_fen,
                "agency": amounts.agency_settled_fen,
                "offset": amounts.offset_settled_fen,
                "transfer": amounts.transferred_out_fen,
                "reclassify_excess": amounts.excess_reclassified_fen,
            }
            value = {
                "obligation": wire,
                "balance": balance_wire(
                    row,
                    totals,
                    amounts.remaining_fen,
                    _read_contents.reference(heads[definition.source["result_id"]]),
                ),
            }
            validate_contract("core.schema.json", "ObligationView", value)
            items.append(value)
        return LedgerPage(
            tuple(items), positions.filtered_count, read_context.head, positions.next_cursor
        )

    def _obligations(self, repo, heads, read_context, selection=None):
        selected = query_selection(read_context, selection)
        by_id = {h["result_id"]: h for h in heads}
        references = {key: _read_contents.reference(row) for key, row in by_id.items()}
        items = []
        for ob, totals, remaining in obligation_balances(
            repo,
            heads,
            through=selected.as_of,
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

    def resolve(
        self,
        refs,
        kind: Literal["result", "obligation", "cost", "funds"],
        read_context,
        *,
        selection=None,
    ):
        if not refs:
            return ()
        if len(refs) > 500:
            fail("invalid_input", "一次最多解析500个公开引用。")
        for ref in refs:
            validate_contract("core.schema.json", "BusinessRef", ref)
        ids = tuple(dict.fromkeys(r["business_id"] for r in refs))
        if kind == "result":
            wanted = {(ref["business_id"], ref["slot"]) for ref in refs}
            result_refs = [
                ref
                for ref in self.get_result_refs(ids, read_context, selection=selection)
                if (ref["business_id"], ref["result_slot"]) in wanted
            ]
            values = self.get_results([], read_context, result_refs=result_refs)
            mapped = {
                (x["reference"]["business_id"], x["reference"]["result_slot"]): x for x in values
            }
        elif kind == "obligation":
            repo = self._resource_view(read_context, selection)
            heads = repo.current_heads(ids)
            values = self._obligations(repo, heads, read_context, selection)
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
