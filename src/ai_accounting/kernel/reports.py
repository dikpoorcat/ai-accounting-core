"""Quarterly statements from immutable journal versions, without ORM persistence."""

from __future__ import annotations

import calendar
import hashlib
import json
import os
import tempfile
import uuid
from collections import defaultdict
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ai_accounting.financial_statement_template import (
    ACCOUNTING_RULE_EFFECTIVE_FROM,
    ACCOUNTING_RULE_SOURCE_URL,
    ACCOUNTING_RULE_VERSION,
    BALANCE_NAMES,
    CASH_FLOW_NAMES,
    PROFIT_NAMES,
    TEMPLATE_FILE_NAME,
    TEMPLATE_MAX_FEN,
    TEMPLATE_PROFILE,
    TEMPLATE_SHA256,
    render_quarterly_template,
)

from .account_definitions import (
    CASH_ACCOUNTS,
    KNOWN_POSITION_ACCOUNTS,
    PROFIT_ACCOUNTS,
    RECLASS,
)
from .backup import _worker_lock
from .contracts import Fact, KernelError, Read
from .dashboard_reads import posted_account_totals
from .query_semantics import (
    classify_financial_position,
    report_party_splits,
)
from .report_semantics import (
    expand_line_fields,
    immutable_line_fields,
    read_report_semantics,
    report_source_fact,
)
from .types import Fen, NonNegativeFen, PositiveFen, YearMonth, canonical, digest, sum_fen

_POSITION_ACCOUNTS = KNOWN_POSITION_ACCOUNTS


class ReportProfile(Fact):
    kind: ClassVar[str] = "report_profile"
    company_name: str = Field(min_length=1, max_length=200)
    accounting_standard: Literal["small_enterprise"]
    bookkeeping_start: YearMonth
    newly_established_zero_opening_confirmed: Literal[True]

    @model_validator(mode="after")
    def valid_start(self):
        if self.bookkeeping_start > self.period:
            raise ValueError("bookkeeping start cannot follow the profile effective period")
        return self


class ContinuationReportProfile(Fact):
    kind: ClassVar[str] = "continuation_report_profile"
    company_name: str = Field(min_length=1, max_length=200)
    accounting_standard: Literal["small_enterprise"]
    bookkeeping_start: YearMonth
    opening_package_id: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def valid_start(self):
        if self.bookkeeping_start > self.period:
            raise ValueError("bookkeeping start cannot follow profile effective period")
        return self


class PriorBalanceRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    line: int
    beginning_fen: Fen


class PriorFlowRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    line: int
    quarter_to_date_fen: Fen
    year_to_date_fen: Fen


class ReportCarryForward(Fact):
    """Documented earlier statements, never a source of journal entries."""

    kind: ClassVar[str] = "report_carry_forward"
    immutable: ClassVar[bool] = True
    lane: ClassVar[Literal["management"]] = "management"
    opening_package_id: str = Field(min_length=1, max_length=200)
    year_beginning_balance: tuple[PriorBalanceRow, ...]
    prior_profit: tuple[PriorFlowRow, ...]
    prior_cash: tuple[PriorFlowRow, ...]

    @model_validator(mode="after")
    def complete_prior_statements(self):
        for rows, names in (
            (self.year_beginning_balance, BALANCE_NAMES),
            (self.prior_profit, PROFIT_NAMES),
            (self.prior_cash, CASH_FLOW_NAMES),
        ):
            if len(rows) != len(names) or {row.line for row in rows} != set(names):
                raise ValueError(
                    "prior statements must explicitly contain every fixed template row"
                )
        return self


DetailCode = Literal[
    "management_startup",
    "management_entertainment",
    "management_research",
    "management_other",
    "sales_merchandise_repair",
    "sales_advertising_promotion",
    "sales_other",
    "finance_interest",
    "finance_other",
    "tax_urban",
    "tax_education",
    "tax_local_education",
]


class ProfitDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    line_no: int = Field(ge=1)
    detail_code: DetailCode
    amount_fen: PositiveFen


class CounterpartyDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    line_no: int = Field(ge=1)
    counterparty_id: str = Field(min_length=1, max_length=150)


class CashDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    line_no: int = Field(ge=1)
    category: Literal[1, 2, 3, 4, 5, 6, 8, 9, 10, 11, 12, 14, 15, 16, 17, 18]
    amount_fen: PositiveFen


class ReportClassification(Fact):
    """Classify existing immutable lines; no accounts or journal amounts are accepted."""

    kind: ClassVar[str] = "report_classification"
    voucher_version_id: str = Field(min_length=1)
    profit_details: tuple[ProfitDetail, ...] = ()
    counterparties: tuple[CounterpartyDetail, ...] = ()
    cash_details: tuple[CashDetail, ...] = ()

    @model_validator(mode="after")
    def unique_lines(self):
        if not (self.profit_details or self.counterparties or self.cash_details):
            raise ValueError("classification requires a bounded report detail")
        for values in (self.counterparties,):
            if len({x.line_no for x in values}) != len(values):
                raise ValueError("duplicate classification line")
        return self


class ReportIncomeTaxConfirmation(Fact):
    kind: ClassVar[str] = "report_income_tax_confirmation"
    treatment: Literal["not_applicable", "zero", "assessed"]
    cumulative_assessed_fen: NonNegativeFen
    calculation_id: str | None = None
    explanation: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def quarter_and_amount(self):
        if not self.explanation.strip():
            raise ValueError("income tax basis explanation cannot be whitespace")
        if int(self.period[5:]) not in (3, 6, 9, 12):
            raise ValueError("income tax report confirmation belongs to a quarter end")
        if self.treatment == "assessed":
            if not self.calculation_id:
                raise ValueError("assessed treatment requires an immutable calculation reference")
        elif self.cumulative_assessed_fen or self.calculation_id is not None:
            raise ValueError("zero/not-applicable confirmation cannot hide an assessed amount")
        return self


PROFILE_KINDS = (ReportProfile.kind, ContinuationReportProfile.kind)
REPORT_KINDS = (
    *PROFILE_KINDS,
    ReportClassification.kind,
    ReportIncomeTaxConfirmation.kind,
    ReportCarryForward.kind,
)


def required_reads(period):
    following = YearMonth.from_ordinal(period.ordinal + 1) if period.ordinal < 119987 else None
    return (
        *(Read("fact", kind, "*", following) for kind in PROFILE_KINDS),
        Read("fact", ReportCarryForward.kind, "*", following),
        Read("fact", ReportClassification.kind, str(period)),
        Read("fact", ReportIncomeTaxConfirmation.kind, str(period)),
    )


def freeze_report_references(period, context):
    # Freeze presence and absence; filing after close is not a circular close gate.
    for read in required_reads(period):
        context.select(read)
    return []


def register(registry):
    for model in (
        ReportProfile,
        ContinuationReportProfile,
        ReportCarryForward,
        ReportClassification,
        ReportIncomeTaxConfirmation,
    ):
        registry.register(model)
    registry.register_readiness("financial_reports", required_reads, freeze_report_references)
    registry.register_snapshot_readiness("financial_reports", check_report_readiness)


def check_report_readiness(store, connection, period, *, reads=None):
    """Query adapter uses the caller's one read-only snapshot; calculators stay pure."""
    if reads is not None and (reads.store is not store or reads.connection is not connection):
        raise ValueError("report readiness reads belong to another snapshot")
    try:
        profiles = tuple(
            row
            for read in required_reads(period)
            if read.kind in PROFILE_KINDS
            for row in (store.select(connection, read) if reads is None else reads.select(read))
        )
    except ValidationError as exc:
        raise KernelError("content_integrity_failed", "报表档案事实正文损坏") from exc
    if not profiles:
        # Close still adopts the other required report facts when no profile has
        # been filed. Their typed bodies are consumed by the frozen reference
        # selection, so the early readiness return must not skip source proof.
        try:
            selected_ids = {
                row.id
                for read in required_reads(period)
                if read.kind not in PROFILE_KINDS
                for row in (store.select(connection, read) if reads is None else reads.select(read))
            }
        except ValidationError as exc:
            raise KernelError("content_integrity_failed", "报表依据事实正文损坏") from exc
        if selected_ids:
            if reads is None:
                from .integrity import verify_sources

                verify_sources(SimpleNamespace(store=store), connection, fact_ids=selected_ids)
            else:
                _verify_report_fact_sources(connection, reads, selected_ids)
        return []
    profile_ids = {row.id for row in profiles}
    if reads is None:
        from .integrity import verify_sources

        verify_sources(SimpleNamespace(store=store), connection, fact_ids=profile_ids)
    else:
        _verify_report_fact_sources(connection, reads, profile_ids)
    adapter = Reports(SimpleNamespace(store=store))
    result = adapter._report(
        int(period[:4]),
        (int(period[5:]) - 1) // 3 + 1,
        source="open",
        connection=connection,
        through_period=period,
        reads=reads,
        _issues_only=True,
    )
    problems = result["fact_issues"]
    if any(item["field"] == "report_carry_forward" for item in problems):
        # Earlier report comparatives can be supplied later as an exact immutable
        # supplementary reference. They do not alter the period's accounting.
        problems = [
            item
            for item in problems
            if item["field"]
            not in {"report_carry_forward", "cross_checks", "cumulative_assessed_fen"}
        ]
    return problems


# Exact codes emitted by typed business modules are shared with dashboard queries.
DETAIL_LINES = {
    "management_startup": 15,
    "management_entertainment": 16,
    "management_research": 17,
    "sales_merchandise_repair": 12,
    "sales_advertising_promotion": 13,
    "finance_interest": 19,
}
CASH_CATEGORIES = {
    "managed_reserve_outflow": 6,
    "customer_receipts": 1,
    "other_operating_receipts": 2,
    "pass_through_receipts": 2,
    "tax_refunds": 2,
    "pass_through_payments": 6,
    "pass_through_refund": 6,
    "asset_acquisition": 12,
    "asset_disposal": 10,
    # SAS appendix: short-term investment sale proceeds include its sale gain;
    # row 9 is separately distributed dividends/profits/interest, not that gain.
    "investment_recovery": 8,
    "investment_acquisition": 11,
    "loan_receipts": 14,
    "loan_repayment": 16,
    "financing_repayment": 16,
    "interest_payments": 17,
    "tax_payments": 5,
    "labor": 3,
}
INFLOW_ROWS = {1, 2, 8, 9, 10, 14, 15}


def issue(field, message, **data):
    return {"field": field, "message": message, "semantics": "accounting", **data}


def _periods(year, quarter):
    if (
        type(year) is not int
        or not 1 <= year <= 9999
        or type(quarter) is not int
        or quarter not in range(1, 5)
    ):
        raise ValueError("invalid report year or quarter")
    start = YearMonth(f"{year:04d}-{(quarter - 1) * 3 + 1:02d}")
    end = YearMonth(f"{year:04d}-{quarter * 3:02d}")
    return start, end, YearMonth(f"{year:04d}-01")


def _closed_report_fact_sources(connection, end, reads=None, *, closes=None, retain_ids=None):
    """Discover frozen report IDs in authenticated close readiness sections.

    The reverse close_reference directory is repairable; it is not the source
    of the report's complete historical fact-ID set. Consumers still validate
    the content and digest of the particular facts they use.
    """

    from .content_history_context import close_reader

    selected = (
        closes
        if closes is not None
        else connection.execute(
            "SELECT * FROM period_close WHERE period<=? ORDER BY period", (end.ordinal,)
        ).fetchall()
    )
    sources = []
    for close in selected:
        if reads is None:
            reader = close_reader()
            header = reader.verified_header(connection, close, require_marker=True)
            financial = reader.read_readiness_check(connection, header, "financial_reports")
        else:
            financial = reads.close_readiness_check(close, "financial_reports")
        fact_ids = financial.get("facts", []) if isinstance(financial, dict) else None
        if not isinstance(fact_ids, list) or any(
            type(ident) is not str or not ident for ident in fact_ids
        ):
            raise KernelError("content_integrity_failed", "冻结报表准备来源格式不一致")
        sources.extend(
            {"close_period": close["period"], "reference_id": ident}
            for ident in fact_ids
            if retain_ids is None or ident in retain_ids
        )
    return sources


def _report_references(connection, end, source, reads=None, *, closed_rows=None, open_kinds=None):
    selected = (
        closed_rows
        if closed_rows is not None
        else _closed_report_fact_sources(connection, end, reads)
    )
    references = {row["reference_id"] for row in selected}
    if source == "open":
        from .query_reads import OPEN_FACT_PERIODS_CTE

        kinds = set(REPORT_KINDS if open_kinds is None else open_kinds)
        # Classification heads grow with each business and month. Locate their
        # exact open periods first; rare profile/tax kinds keep their kind seek.
        if "report_classification" in kinds:
            references.update(
                row[0]
                for row in connection.execute(
                    OPEN_FACT_PERIODS_CTE
                    + "SELECT f.id FROM open_periods p "
                    "CROSS JOIN fact_revision f INDEXED BY fact_period ON f.period=p.period "
                    "CROSS JOIN fact_current a ON a.fact_id=f.id "
                    "CROSS JOIN subject s ON s.id=f.subject_id "
                    "WHERE s.kind='report_classification'",
                    (end.ordinal, end.ordinal),
                )
            )
            kinds.remove("report_classification")
        references.update(
            row[0]
            for row in connection.execute(
                "SELECT f.id FROM fact_current a JOIN fact_revision f ON f.id=a.fact_id "
                "JOIN subject s ON s.id=f.subject_id "
                "WHERE s.kind IN (SELECT value FROM json_each(?)) AND f.period<=? "
                "AND NOT EXISTS(SELECT 1 FROM period_close p WHERE p.period=f.period)",
                (
                    canonical(sorted(kinds)),
                    end.ordinal,
                ),
            )
        )
    return references


def _report_tax_and_carry_refs(connection, references, year_start, end, book_start, *, omit_carry):
    """Select rare authoritative metadata, then require exact adopted membership."""
    return {
        row["id"]
        for row in connection.execute(
            "SELECT f.id,s.kind,f.period FROM subject s CROSS JOIN fact_revision f "
            "ON f.subject_id=s.id "
            "WHERE s.kind='report_income_tax_confirmation' "
            "AND f.period>=? AND f.period<=? UNION ALL "
            "SELECT f.id,s.kind,f.period FROM subject s CROSS JOIN fact_revision f "
            "ON f.subject_id=s.id "
            "WHERE s.kind='report_carry_forward' AND f.period=?",
            (year_start, end, book_start),
        )
        if row["id"] in references and (not omit_carry or row["kind"] != ReportCarryForward.kind)
    }


def _applicable_profile(facts, end):
    profiles = [f for f in facts if f.fact.kind in PROFILE_KINDS and f.fact.period <= end]
    if not profiles:
        return None
    latest = max(f.fact.period for f in profiles)
    applicable = {f.id: f for f in profiles if f.fact.period == latest}
    return next(iter(applicable.values())) if len(applicable) == 1 else None


def _closed_period_issues(closes, book_start, year_start, end):
    problems = []
    if end.ordinal not in closes:
        problems.append(issue("period", "季度末尚未关账"))
    problems.extend(
        issue(
            "closed_periods",
            "年初或建账月起须逐月关账",
            period=str(YearMonth.from_ordinal(ordinal)),
        )
        for ordinal in range(max(book_start.ordinal, year_start.ordinal), end.ordinal + 1)
        if ordinal not in closes
    )
    return problems


def _report_vouchers(cutoff, source, **scope):
    from .query_reads import selected_voucher_sql

    sql, parameters = selected_voucher_sql(YearMonth.from_ordinal(cutoff), **scope)
    if source == "closed":
        sql = "SELECT * FROM (" + sql + ") WHERE selection_source='close_manifest'"
    return sql, parameters


def _unfrozen_voucher_ids(connection, year_start, end, frozen_flows, *, historical_accounts=None):
    """Seek actual unfrozen months; preserve the separate pre-year account lane."""
    months = [month for month in range(year_start, end + 1) if month not in frozen_flows]
    sql = (
        "SELECT v.id FROM json_each(?) months "
        "CROSS JOIN voucher_version v INDEXED BY voucher_period "
        "WHERE v.period=months.value"
    )
    parameters = [canonical(months)]
    if historical_accounts is not None:
        sql += (
            " UNION SELECT l.version_id FROM voucher_line l "
            "INDEXED BY voucher_line_account "
            "JOIN voucher_version v ON v.id=l.version_id "
            "WHERE l.account IN (SELECT value FROM json_each(?)) AND v.period<?"
        )
        parameters.extend((canonical(sorted(historical_accounts)), year_start))
    return {row[0] for row in connection.execute(sql, parameters)}


def _has_open_publication(connection, cutoff):
    # Seek from one actual posting month to the next. A later close does not
    # stand in for an exact close here; retain the original per-month test.
    return bool(connection.execute(
        "WITH RECURSIVE months(period) AS ("
        "SELECT min(posting_period) FROM calculation_publication WHERE posting_period<=? "
        "UNION ALL SELECT (SELECT min(posting_period) FROM calculation_publication "
        "WHERE posting_period>m.period AND posting_period<=?) FROM months m "
        "WHERE m.period IS NOT NULL) SELECT 1 FROM months m WHERE m.period IS NOT NULL "
        "AND NOT EXISTS(SELECT 1 FROM period_close c WHERE c.period=m.period) LIMIT 1",
        (cutoff, cutoff),
    ).fetchone())


def _frozen_unused_classification_ids(connection, reads, pending, source_vouchers, end):
    """Locate exact old, unused facts already accepted by a frozen report flow."""
    if (
        not reads._snapshot_active
        or reads.connection is not connection
        or source_vouchers is None
    ):
        return set()
    cache = reads._report_snapshot_cache
    year_start = YearMonth(str(end)[:4] + "-01").ordinal
    candidates = {
        row["revision_id"]: row
        for voucher, row in pending.items()
        if voucher not in source_vouchers
        and row["fact_period"] is not None
        and row["fact_period"] < year_start
        and row["fact_digest"] is not None
    }
    if not candidates:
        return set()
    closed_sources = cache.get(("closed_report_fact_sources", end.ordinal))
    if closed_sources is None:
        closed_sources = _closed_report_fact_sources(connection, end, reads)
        cache[("closed_report_fact_sources", end.ordinal)] = closed_sources
    first_close = {}
    for item in closed_sources:
        ident = item["reference_id"]
        if ident in candidates:
            first_close[ident] = min(
                item["close_period"], first_close.get(ident, item["close_period"])
            )
    by_close = defaultdict(set)
    for ident, period in first_close.items():
        by_close[period].add(ident)
    from .report_flow import read_report_flow

    covered = set()
    for period, identifiers in sorted(by_close.items()):
        flow = read_report_flow(connection, period, reads=reads)
        if flow is None or flow["issues"]:
            continue
        refs = dict(flow["classification_refs"])
        for ident in identifiers:
            stored = refs.get(ident)
            if stored is None:
                continue
            if stored != candidates[ident]["fact_digest"].hex():
                raise KernelError(
                    "content_integrity_failed",
                    "冻结报表分类来源摘要不一致",
                    component="report_classification",
                    record_id=ident,
                    reason="fact_digest_mismatch",
                )
            covered.add(ident)
    return covered


def _validated_report_classification_headers(
    connection, reads, references, end, source, problems, *, source_vouchers=None
):
    """Validate selected headers and every normalized child against exact lines."""
    before = len(problems)
    headers, conflicts = {}, set()
    for row in connection.execute(
        "SELECT c.revision_id,c.period,c.voucher_version_id,v.period AS voucher_period,"
        "f.period AS fact_period,f.digest AS fact_digest "
        "FROM json_each(?) ids CROSS JOIN fact_report_classification c ON c.revision_id=ids.value "
        "LEFT JOIN voucher_version v ON v.id=c.voucher_version_id "
        "LEFT JOIN fact_revision f ON f.id=c.revision_id",
        (canonical(sorted(references)),),
    ):
        key = row["voucher_version_id"]
        if key in headers or key in conflicts:
            problems.append(
                issue(
                    "report_classification",
                    "同一凭证版本存在多个分类来源",
                    voucher_version_id=key,
                )
            )
            headers.pop(key, None)
            conflicts.add(key)
        else:
            headers[key] = row
    if not headers:
        return {}

    cache = (
        reads._report_snapshot_cache
        if reads._snapshot_active and reads.connection is connection
        else None
    )
    cache_key = ("report_classification_validated_references", end.ordinal, source)
    verified = cache.get(cache_key, frozenset()) if cache is not None else frozenset()
    # Always detect conflicts in the complete requested set above. Only the
    # independent line/source checks of an exact revision can be reused.
    pending = {
        key: row for key, row in headers.items() if row["revision_id"] not in verified
    }
    covered = _frozen_unused_classification_ids(
        connection, reads, pending, source_vouchers, end
    )
    pending = {
        key: row for key, row in pending.items() if row["revision_id"] not in covered
    }
    if not pending:
        return headers
    candidates = set(pending)
    candidates.update(
        row[0]
        for row in connection.execute(
            "SELECT v.id FROM json_each(?) ids CROSS JOIN voucher_version v "
            "INDEXED BY voucher_version_reverses ON v.reverses_id=ids.value",
            (canonical(sorted(pending)),),
        )
    )
    sql, parameters = _report_vouchers(end.ordinal, source, voucher_ids=candidates)
    selected_periods, selected_ids = defaultdict(set), set()
    for row in connection.execute("SELECT id,period,reverses_id FROM (" + sql + ")", parameters):
        selected_ids.add(row["id"])
        periods = selected_periods[row["reverses_id"] or row["id"]]
        if row["reverses_id"] is None:
            periods.add(row["period"])
    selected_references = connection.execute(
        "SELECT r.* FROM close_reference r WHERE r.reference_type='voucher' "
        "AND r.reference_id IN (SELECT value FROM json_each(?)) AND r.close_period<=?",
        (canonical(sorted(selected_ids)), end.ordinal),
    ).fetchall()
    reads.verify_close_references(selected_references)

    detail_ids = []
    for key, row in pending.items():
        if key not in selected_periods:
            if row["voucher_period"] != row["period"]:
                problems.append(
                    issue(
                        "report_classification.voucher_version_id",
                        "分类必须引用本月已经存在的凭证版本",
                        voucher_version_id=key,
                    )
                )
            continue
        if row["period"] not in selected_periods[key]:
            problems.append(
                issue(
                    "report_classification.period",
                    "分类月份必须等于原凭证记账月份",
                    voucher_version_id=key,
                )
            )
        detail_ids.append(row["revision_id"])

    # These are the same line/account checks for current and historical sources.
    # Stream the normalized child keys and exact line account; do not construct
    # historical typed classifications or load their calculation outcomes.
    for detail_kind, accounts in (
        ("profit_details", {"5403", "5601", "5602", "5603"}),
        ("counterparties", RECLASS),
        ("cash_details", CASH_ACCOUNTS),
    ):
        for row in connection.execute(
            "SELECT c.voucher_version_id,l.account FROM json_each(?) ids "
            "CROSS JOIN fact_report_classification c ON c.revision_id=ids.value "
            f"CROSS JOIN fact_report_classification_{detail_kind} d ON d.revision_id=c.revision_id "
            "LEFT JOIN voucher_line l ON l.version_id=c.voucher_version_id AND l.line_no=d.line_no",
            (canonical(detail_ids),),
        ):
            message = (
                "分类引用不存在的凭证行"
                if row["account"] is None
                else "该凭证行不接受此类报表分类"
                if row["account"] not in accounts
                else None
            )
            if message is not None:
                problems.append(
                    issue(
                        "report_classification.line_no",
                        message,
                        voucher_version_id=row["voucher_version_id"],
                    )
                )
    if cache is not None and len(problems) == before:
        cache[cache_key] = verified | {row["revision_id"] for row in pending.values()}
    return headers


def _rooted_classification_headers(connection, reads, source_vouchers, end, source, problems):
    """Use the frozen complete voucher-key set and inspect only live/unsafe rows."""
    from .query_reads import OPEN_FACT_PERIODS_CTE
    from .report_classification_directory import classification_directory_scope

    open_rows = ()
    if source == "open":
        open_rows = tuple(connection.execute(
            OPEN_FACT_PERIODS_CTE
            + "SELECT f.id,f.digest,c.voucher_version_id FROM open_periods p "
            "CROSS JOIN fact_revision f INDEXED BY fact_period ON f.period=p.period "
            "JOIN fact_current a ON a.fact_id=f.id "
            "JOIN subject s ON s.id=f.subject_id AND s.kind='report_classification' "
            "JOIN fact_report_classification c ON c.revision_id=f.id",
            (end.ordinal, end.ordinal),
        ))
    keys = set(source_vouchers) | {row["voucher_version_id"] for row in open_rows}
    rooted = classification_directory_scope(connection, end.ordinal, keys, reads=reads)
    if rooted is None:
        return None
    expected_old = {}
    validation_ids = {row["id"] for row in open_rows}
    conflict_keys = set(rooted["conflicts"])
    open_by_key = defaultdict(list)
    for row in open_rows:
        open_by_key[row["voucher_version_id"]].append(row["id"])
    for key in keys:
        frozen = rooted["membership"].get(key, ())
        for ident, digest_hex, _first_close, _safe in frozen:
            if key in source_vouchers:
                expected_old[ident] = key, digest_hex
                validation_ids.add(ident)
        if len(frozen) + len(open_by_key[key]) > 1:
            conflict_keys.add(key)
    for key, ident, digest_hex, _first_close, _safe in rooted["unsafe"]:
        expected_old[ident] = key, digest_hex
        validation_ids.add(ident)
    local_problems = []
    headers = _validated_report_classification_headers(
        connection,
        reads,
        validation_ids,
        end,
        source,
        local_problems,
        source_vouchers=source_vouchers,
    )
    actual = {row["revision_id"]: row for row in headers.values()}
    for ident, (key, digest_hex) in expected_old.items():
        if key in conflict_keys:
            continue
        row = actual.get(ident)
        if row is None or row["voucher_version_id"] != key or row["fact_digest"] is None:
            raise KernelError("content_integrity_failed", "冻结报表分类身份不一致")
        if row["fact_digest"].hex() != digest_hex:
            raise KernelError("content_integrity_failed", "冻结报表分类来源摘要不一致")
    duplicate_events = []
    for key in conflict_keys:
        frozen = rooted["membership"].get(key)
        if frozen is None:
            frozen = rooted["conflict_membership"].get(key, ())
        identifiers = sorted({entry[0] for entry in frozen} | set(open_by_key[key]))
        if len(identifiers) < 2:
            raise KernelError("content_integrity_failed", "冻结报表分类冲突目录不一致")
        duplicate_events.extend((ident, key) for ident in identifiers[1:])
    for _ident, key in sorted(duplicate_events):
        problems.append(
            issue("report_classification", "同一凭证版本存在多个分类来源", voucher_version_id=key)
        )
    problems.extend(
        item
        for item in local_problems
        if item["field"] != "report_classification"
        or item.get("voucher_version_id") not in conflict_keys
    )
    for key in conflict_keys:
        headers.pop(key, None)
    return headers


def _verify_report_fact_sources(connection, reads, identifiers):
    """Verify only report facts actually read, reusing successful snapshot proofs."""

    identifiers = set(identifiers)
    if not identifiers:
        return
    cache = (
        reads._report_snapshot_cache
        if reads._snapshot_active and reads.connection is connection and connection.in_transaction
        else None
    )
    verified = (
        cache.setdefault("verified_report_fact_sources", set())
        if cache is not None
        else set()
    )
    missing = identifiers - verified
    if missing:
        from .integrity import verify_sources

        verify_sources(reads.engine, connection, fact_ids=missing)
        if cache is not None:
            verified.update(missing)


def _report_classifications(
    connection,
    reads,
    references,
    source_vouchers,
    end,
    source,
    problems,
    *,
    rooted=False,
    fallback_references=None,
):
    """Validate all applicable references; decode only classifications used by rows."""

    cache = (
        reads._report_snapshot_cache
        if reads._snapshot_active and reads.connection is connection
        else None
    )
    key = (
        "report_classification_headers",
        end.ordinal,
        source,
        "rooted" if rooted else frozenset(references),
        frozenset(source_vouchers),
    )
    if cache is not None and key in cache:
        headers = cache[key]
    else:
        before = len(problems)
        headers = (
            _rooted_classification_headers(
                connection, reads, source_vouchers, end, source, problems
            )
            if rooted
            else None
        )
        if headers is None:
            if fallback_references is not None:
                references = fallback_references()
            headers = _validated_report_classification_headers(
                connection,
                reads,
                references,
                end,
                source,
                problems,
                source_vouchers=source_vouchers,
            )
        if cache is not None and len(problems) == before:
            cache[key] = headers
    identifiers = {row["revision_id"] for key, row in headers.items() if key in source_vouchers}
    _verify_report_fact_sources(connection, reads, identifiers)
    return {
        version.fact.voucher_version_id: version.fact
        for version in reads.fact_versions(identifiers).values()
    }


def _account_totals(
    connection, cutoff, source, opening_rows, unknown_opening_period=None, *, reads=None
):
    """Use the shared posting-period account selector for every consumer."""
    boundary = connection.execute(
        "SELECT period FROM period_close WHERE period<=? ORDER BY period DESC LIMIT 1",
        (cutoff,),
    ).fetchone()
    if boundary is None and unknown_opening_period is not None and unknown_opening_period <= cutoff:
        return None
    if boundary is None:
        values = defaultdict(int)
        if source == "open":
            for row in connection.execute(
                "SELECT account,sum(debit-credit) amount FROM monthly_account "
                "WHERE period<=? GROUP BY account",
                (cutoff,),
            ):
                values[row["account"]] += row["amount"]
        else:
            sql, parameters = _report_vouchers(cutoff, source)
            for row in connection.execute(
                "WITH selected AS (" + sql + ") "
                "SELECT l.account,sum(l.debit-l.credit) amount FROM selected v "
                "JOIN voucher_line l ON l.version_id=v.id GROUP BY l.account",
                parameters,
            ):
                values[row["account"]] += row["amount"]
        for row in opening_rows:
            if row["period"] <= cutoff:
                values[row["account"]] += row["amount"]
        return dict(values)
    return posted_account_totals(connection, cutoff, source=source, reads=reads)


class Reports:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    def _cache(self, reads, connection):
        if reads._snapshot_active and reads.connection is connection:
            return reads._report_snapshot_cache
        return None

    def report(
        self,
        year: int,
        quarter: int,
        *,
        source: Literal["open", "closed"] = "open",
        carry_forward_fact_id: str | None = None,
    ):
        return self._report(
            year, quarter, source=source, carry_forward_fact_id=carry_forward_fact_id
        )

    def closed_period_coverage(self, year, quarter, *, connection=None, reads=None):
        """The context selector's existing close test, without building statements."""
        return self.closed_period_coverages([(year, quarter)], connection=connection, reads=reads)[
            year, quarter
        ]

    def closed_period_coverages(self, quarters, *, connection=None, reads=None):
        """Batch close coverage while retaining each quarter's exact source cutoff."""
        from .query_reads import QueryReads

        requested = {(year, quarter): _periods(year, quarter) for year, quarter in quarters}
        if not requested:
            return {}
        latest_end = max(end for _, end, _ in requested.values())
        manager = (
            nullcontext(reads or QueryReads(self.engine, connection))
            if connection is not None
            else QueryReads.snapshot(self.engine)
        )
        with manager as reads:
            selected = connection if connection is not None else reads.connection
            # Locate the rare profile revisions before scanning authenticated
            # readiness lists.  Every close section is still read and checked;
            # only its profile IDs need a first-adoption date for this view.
            profile_rows = list(selected.execute(
                "SELECT f.id,f.period FROM subject s CROSS JOIN fact_revision f "
                "ON f.subject_id=s.id WHERE s.kind IN (?,?) AND f.period<=?",
                (*PROFILE_KINDS, latest_end.ordinal),
            ))
            references = _closed_report_fact_sources(
                selected, latest_end, reads,
                retain_ids={row["id"] for row in profile_rows},
            )
            first_close = {}
            for row in references:
                ident, month = row["reference_id"], row["close_period"]
                first_close[ident] = min(month, first_close.get(ident, month))
            candidates = sorted(
                (max(row["period"], first_close[row["id"]]), row["period"], row["id"])
                for row in profile_rows if row["id"] in first_close
            )
            # A later close can first introduce an older fact. Its fact period
            # alone must not make that source visible in an earlier quarter.
            selected_profiles, latest_period, active_ids, position = {}, None, set(), 0
            for key in sorted(requested, key=lambda key: requested[key][1].ordinal):
                end = requested[key][1]
                while position < len(candidates) and candidates[position][0] <= end.ordinal:
                    _, fact_period, ident = candidates[position]
                    if latest_period is None or fact_period > latest_period:
                        latest_period, active_ids = fact_period, {ident}
                    elif fact_period == latest_period:
                        active_ids.add(ident)
                    position += 1
                selected_profiles[key] = set(active_ids)
            profile_ids = {
                ident for identifiers in selected_profiles.values() for ident in identifiers
            }
            _verify_report_fact_sources(selected, reads, profile_ids)
            profiles = reads.fact_versions(profile_ids)
            periods = {
                row[0]
                for row in selected.execute(
                    "SELECT period FROM period_close WHERE period<=?", (latest_end.ordinal,)
                )
            }
            results = {}
            for key, (_, end, year_start) in requested.items():
                profile = _applicable_profile(
                    [profiles[ident] for ident in selected_profiles[key]], end
                )
                book_start = profile.fact.bookkeeping_start if profile else year_start
                problems = _closed_period_issues(periods, book_start, year_start, end)
                results[key] = {
                    "complete": not problems,
                    "fact_issues": problems,
                    "bookkeeping_start": str(book_start),
                }
            return results

    def _report_profiles(self, connection, references, end, reads):
        # The already authenticated references decide adoption.  Read the rare
        # profile metadata first, then intersect with that exact set, so a full
        # report does not feed every historical reference through SQLite JSON.
        candidates = [
            (row["id"], row["period"])
            for row in connection.execute(
                "SELECT f.id,f.period FROM subject s "
                "CROSS JOIN fact_revision f ON f.subject_id=s.id "
                "WHERE s.kind IN (?,?) AND f.period<=?",
                (*PROFILE_KINDS, end.ordinal),
            )
            if row["id"] in references
        ]
        latest = max((period for _, period in candidates), default=None)
        identifiers = {ident for ident, period in candidates if period == latest}
        _verify_report_fact_sources(connection, reads, identifiers)
        return list(reads.fact_versions(identifiers).values())

    def _report(
        self,
        year,
        quarter,
        *,
        source,
        connection=None,
        through_period=None,
        carry_forward_fact_id=None,
        reads=None,
        _issues_only=False,
    ):
        start, end, year_start = _periods(year, quarter)
        if through_period is not None:
            end = through_period
            quarter = int(end[5:]) // 3
        if source not in ("open", "closed"):
            raise ValueError("source must be open or closed")
        external_connection = connection is not None
        manager = (
            nullcontext(connection)
            if external_connection
            else self.store.connection(read_only=True)
        )
        with manager as connection:
            if not external_connection:
                connection.execute("BEGIN")
            from .query_reads import QueryReads

            reads = reads or QueryReads(self.engine, connection)
            cache = self._cache(reads, connection)
            epochs = self.store.epochs(connection)
            identity = dict(connection.execute("SELECT * FROM identity WHERE id=1").fetchone())
            close_key = ("closes", end.ordinal)
            if cache is not None and close_key in cache:
                close_rows = cache[close_key]
            else:
                close_rows = connection.execute(
                    "SELECT * FROM period_close WHERE period<=? ORDER BY period",
                    (end.ordinal,),
                ).fetchall()
                if cache is not None:
                    cache[close_key] = close_rows
            closes = {r["period"] for r in close_rows}
            problems = []
            reference_key = ("closed_report_fact_sources", end.ordinal)
            full_reference_rows = cache.get(reference_key) if cache is not None else None

            def complete_references():
                nonlocal full_reference_rows
                if full_reference_rows is None:
                    full_reference_rows = _closed_report_fact_sources(
                        connection, end, reads, closes=close_rows
                    )
                    if cache is not None:
                        cache[reference_key] = full_reference_rows
                return _report_references(
                    connection, end, source, reads, closed_rows=full_reference_rows
                )

            if _issues_only:
                narrow_kinds = (*PROFILE_KINDS, ReportCarryForward.kind,
                                ReportIncomeTaxConfirmation.kind)
                candidate_ids = {
                    row[0]
                    for row in connection.execute(
                        "SELECT f.id FROM subject s CROSS JOIN fact_revision f "
                        "ON f.subject_id=s.id WHERE s.kind IN (SELECT value FROM json_each(?)) "
                        "AND f.period<=? AND (s.kind<>? OR f.period>=?)",
                        (canonical(narrow_kinds), end.ordinal,
                         ReportIncomeTaxConfirmation.kind, year_start.ordinal),
                    )
                }
                closed_reference_rows = (
                    [row for row in full_reference_rows
                     if row["reference_id"] in candidate_ids]
                    if full_reference_rows is not None
                    else _closed_report_fact_sources(
                        connection, end, reads, closes=close_rows,
                        retain_ids=candidate_ids,
                    )
                )
            else:
                closed_reference_rows = (
                    full_reference_rows
                    if full_reference_rows is not None
                    else _closed_report_fact_sources(connection, end, reads, closes=close_rows)
                )
                if cache is not None:
                    cache[reference_key] = closed_reference_rows
            references = _report_references(
                connection, end, source, reads, closed_rows=closed_reference_rows,
                open_kinds=narrow_kinds if _issues_only else None,
            )
            facts = self._report_profiles(connection, references, end, reads)
            if carry_forward_fact_id is not None:
                present = connection.execute(
                    "SELECT 1 FROM fact_revision f JOIN subject s ON s.id=f.subject_id "
                    "WHERE f.id=? AND s.kind=?",
                    (carry_forward_fact_id, ReportCarryForward.kind),
                ).fetchone()
                if present is None:
                    raise KernelError(
                        "invalid_report_reference", "需要本公司不可变的接续报表依据版本"
                    )
                _verify_report_fact_sources(connection, reads, {carry_forward_fact_id})
                explicit = self.store.select(
                    connection, Read("fact", ReportCarryForward.kind, "#" + carry_forward_fact_id)
                )
                if len(explicit) != 1:
                    raise KernelError(
                        "invalid_report_reference", "需要本公司不可变的接续报表依据版本"
                    )
                # An explicit replacement is chosen by the caller; never silently
                # select the latest supplementary statement after a period closed.
                facts = [fact for fact in facts if fact.fact.kind != ReportCarryForward.kind]
                facts.extend(explicit)
                references.add(carry_forward_fact_id)
            profile = _applicable_profile(facts, end)
            if profile is None:
                problems.append(
                    issue("report_profile", "需要唯一的报表口径及明确的新设或接续建账依据")
                )
            book_start = profile.fact.bookkeeping_start if profile else year_start
            needed_references = _report_tax_and_carry_refs(
                connection,
                references,
                year_start.ordinal,
                end.ordinal,
                book_start.ordinal,
                omit_carry=carry_forward_fact_id is not None,
            )
            _verify_report_fact_sources(connection, reads, needed_references)
            facts.extend(reads.fact_versions(sorted(needed_references)).values())
            if book_start > end:
                problems.append(issue("bookkeeping_start", "报表期间早于明确建账月"))
            if source == "closed":
                problems.extend(_closed_period_issues(closes, book_start, year_start, end))
            unknown_accounts = set(
                _account_totals(connection, end.ordinal, source, (), reads=reads)
            ) - (_POSITION_ACCOUNTS)
            from .report_projection import party_balance_rows

            party_balances = None
            if not unknown_accounts:

                def cached_party_balance(cutoff):
                    effective = source
                    if cache is not None and source == "open":
                        open_key = ("has_open_publication", cutoff)
                        if open_key not in cache:
                            cache[open_key] = _has_open_publication(connection, cutoff)
                        if not cache[open_key]:
                            effective = "closed"
                    key = ("party_balance", cutoff, effective)
                    if cache is not None and key in cache:
                        return cache[key]
                    result = party_balance_rows(
                        self.engine, connection, cutoff, source=effective, reads=reads
                    )
                    if cache is not None and result is not None:
                        cache[key] = result
                    return result

                start_parties = cached_party_balance(year_start.ordinal - 1)
                end_parties = cached_party_balance(end.ordinal)
                if start_parties is not None and end_parties is not None:
                    party_balances = {
                        year_start.ordinal - 1: start_parties,
                        end.ordinal: end_parties,
                    }
            from .report_flow import read_report_flow

            frozen_flows = {}
            if party_balances is not None and not unknown_accounts:
                for month in sorted(closes):
                    if year_start.ordinal <= month <= end.ordinal:
                        flow = read_report_flow(connection, month, reads=reads)
                        # A changed classification selection, or an issue-bearing
                        # month, retains the exact historical row/issue path.
                        if flow is not None and not flow["issues"]:
                            frozen_flows[month] = flow
            historical_accounts = set(RECLASS) | unknown_accounts
            candidates = _unfrozen_voucher_ids(
                connection,
                year_start.ordinal,
                end.ordinal,
                frozen_flows,
                historical_accounts=historical_accounts if party_balances is None else None,
            )
            # The open party balance has already selected every authoritative
            # line of this exact month in this snapshot. Its cache entry is
            # written only after proving that no voucher in the month has a
            # close reference through this same cutoff. Keep all other months
            # on the normal selector, including any frozen or mixed scope.
            reused_open_rows = (
                cache.get(("report_open_source_rows", end.ordinal))
                if cache is not None
                and source == "open"
                and party_balances is not None
                and end.ordinal not in closes
                and end.ordinal not in frozen_flows
                else None
            )
            if reused_open_rows is not None:
                selected_open_ids = {row[2] for row in reused_open_rows}
                if any(row[0] != "open" or row[1] != end.ordinal for row in reused_open_rows) or (
                    not selected_open_ids.issubset(candidates)
                ):
                    reused_open_rows = None
                else:
                    candidates.difference_update(
                        row[0]
                        for row in connection.execute(
                            "SELECT id FROM voucher_version WHERE period=?", (end.ordinal,)
                        )
                    )
            selected_sql, selected_params = _report_vouchers(
                end.ordinal, source, voucher_ids=candidates
            )
            # Direct balances use synchronous totals. Exact parties and this
            # posting year's flows retain their immutable classification sources.
            row_filter = (
                "v.period>=? AND l.account IN (SELECT value FROM json_each(?))"
                if party_balances is not None
                else "(v.period>=? AND l.account IN (SELECT value FROM json_each(?))) OR "
                "l.account IN (SELECT value FROM json_each(?)) OR "
                "l.account NOT IN (SELECT value FROM json_each(?))"
            )
            sql = (
                "WITH selected AS ("
                + selected_sql
                + """
                ) SELECT v.id version_id,v.period,v.basis_calculation_id calculation_id,
                  v.reverses_id,v.close_period,
                  l.line_no,l.account,l.debit,l.credit,l.cashflow,c.kind,c.fact_id,
                  c.period calculation_period,c.subject_id calculation_subject_id
                FROM selected v JOIN voucher_line l ON l.version_id=v.id
                JOIN calculation c ON c.id=v.basis_calculation_id WHERE """
                + row_filter
            )
            params = [
                *selected_params,
                year_start.ordinal,
                canonical(sorted(CASH_ACCOUNTS | set(PROFIT_ACCOUNTS))),
            ]
            if party_balances is None:
                params.extend((canonical(sorted(RECLASS)), canonical(sorted(_POSITION_ACCOUNTS))))
            rows = [dict(r) for r in connection.execute(sql, params)]
            if reused_open_rows is not None:
                for item in reused_open_rows:
                    if item[8] not in CASH_ACCOUNTS | set(PROFIT_ACCOUNTS):
                        continue
                    rows.append(
                        {
                            "version_id": item[2],
                            "period": item[1],
                            "calculation_id": item[5],
                            "reverses_id": item[7],
                            "close_period": None,
                            "line_no": item[3],
                            "account": item[8],
                            "debit": item[9],
                            "credit": item[10],
                            "cashflow": item[11],
                            "kind": item[12],
                            "fact_id": item[13],
                            "calculation_period": item[14],
                            "calculation_subject_id": item[15],
                        }
                    )
            selected_references = connection.execute(
                "SELECT r.* FROM close_reference r WHERE r.reference_type='voucher' "
                "AND r.reference_id IN (SELECT value FROM json_each(?)) "
                "AND r.close_period<=?",
                (canonical(sorted({row["version_id"] for row in rows})), end.ordinal),
            ).fetchall()
            reads.verify_close_references(selected_references)
            earliest = connection.execute(
                "SELECT min(period) FROM monthly_account WHERE period<=? AND "
                "(? OR EXISTS(SELECT 1 FROM period_close p WHERE p.period=monthly_account.period))",
                (end.ordinal, source == "open"),
            ).fetchone()[0]
            if earliest is not None and earliest < book_start.ordinal:
                problems.append(
                    issue("bookkeeping_start", "已存在建账月之前的账；不能声明该月为新设企业零期初")
                )
            source_vouchers = {row["reverses_id"] or row["version_id"] for row in rows}
            classification_refs = references
            def fallback_classification_references():
                all_references = complete_references() if _issues_only else references
                if not frozen_flows:
                    return all_references
                return {
                    row[0]
                    for row in connection.execute(
                        "SELECT f.id FROM json_each(?) ids "
                        "CROSS JOIN fact_revision f ON f.id=ids.value "
                        "CROSS JOIN subject s ON s.id=f.subject_id "
                        "CROSS JOIN fact_report_classification c ON c.revision_id=f.id "
                        "WHERE s.kind='report_classification' "
                        "AND (f.period NOT IN (SELECT value FROM json_each(?)) "
                        "OR c.voucher_version_id IN (SELECT value FROM json_each(?)))",
                        (
                            canonical(sorted(all_references)),
                            canonical(sorted(frozen_flows)),
                            canonical(sorted(source_vouchers)),
                        ),
                    )
                }
            classifications = _report_classifications(
                connection,
                reads,
                classification_refs,
                source_vouchers,
                end,
                source,
                problems,
                rooted=True,
                fallback_references=fallback_classification_references,
            )
            semantic_months = {
                month
                for month in closes
                if year_start.ordinal <= month <= end.ordinal and month not in frozen_flows
            }
            frozen_semantics = {}
            for month in sorted(semantic_months):
                key = ("report_semantics", month)
                if cache is not None and key in cache:
                    frozen_semantics[month] = cache[key]
                else:
                    semantic, sources = read_report_semantics(
                        connection, month, include_sources=True, reads=reads
                    )
                    frozen_semantics[month] = (
                        semantic,
                        {(item[2], item[3]): item for item in sources},
                    )
                    if cache is not None:
                        cache[key] = frozen_semantics[month]
            visible_semantic_keys = {month: set() for month in semantic_months}
            for row in rows:
                if row["period"] in visible_semantic_keys and row["account"] in CASH_ACCOUNTS | set(
                    PROFIT_ACCOUNTS
                ):
                    visible_semantic_keys[row["period"]].add((row["version_id"], row["line_no"]))
            if any(
                visible_semantic_keys[month] != set(frozen_semantics[month][0])
                for month in semantic_months
            ):
                raise KernelError("content_integrity_failed", "报表语义缺少已冻结凭证行")
            full_rows = [
                row
                for row in rows
                if row["period"] not in frozen_semantics
                or row["account"] not in CASH_ACCOUNTS | set(PROFIT_ACCOUNTS)
            ]
            calculations, actual_facts = {}, {}
            originals = {
                ident: row["calculation_id"]
                for ident, row in reads.vouchers(
                    {row["reverses_id"] for row in full_rows if row["reverses_id"]}
                ).items()
            }
            bounded_relations = source == "open" and reads._snapshot_active
            source_ids = {
                originals.get(row["reverses_id"], row["calculation_id"]) for row in full_rows
            }
            projected_lines = {}
            projected_relations = {}
            if bounded_relations and source_ids:
                from .report_open_contribution import (
                    read_open_contributions,
                    selected_open_line,
                )

                contributions = read_open_contributions(
                    self.engine, connection, source_ids, reads=reads
                )
                grouped = defaultdict(list)
                for row in full_rows:
                    grouped[originals.get(row["reverses_id"], row["calculation_id"])].append(
                        row
                    )
                for source_id, source_rows in grouped.items():
                    content = contributions.get(source_id)
                    matched = (
                        [selected_open_line(content, row) for row in source_rows]
                        if content is not None
                        else []
                    )
                    if matched and all(item is not None for item in matched):
                        projected_relations[source_id] = matched[0][1]
                        projected_lines.update(
                            (
                                (row["version_id"], row["line_no"]),
                                item[0],
                            )
                            for row, item in zip(source_rows, matched, strict=True)
                        )
            fallback_rows = [
                row for row in full_rows
                if (row["version_id"], row["line_no"]) not in projected_lines
            ]
            calculation_ids = {row["calculation_id"] for row in fallback_rows} | {
                originals[row["reverses_id"]]
                for row in fallback_rows
                if row["reverses_id"]
            }
            primed_calculations = reads.prime_calculations(
                calculation_ids, ancestors=not bounded_relations
            )
            if bounded_relations:
                # Cash and profit lines use their own result and the explicitly
                # linked funds source. Only reclassified balances can need the
                # resolver's fallback ancestor party candidates.
                party_rows = {
                    ident: [] for ident in source_ids if ident not in projected_relations
                }
                for row in fallback_rows:
                    if row["account"] in RECLASS:
                        ident = originals.get(row["reverses_id"], row["calculation_id"])
                        party_rows[ident].append(row)
                relation_records = (
                    reads.report_line_relations_many(party_rows) if party_rows else {}
                )
                relation_records.update(projected_relations)
            else:
                relation_records = reads.relations_many(source_ids)
            reported_relations = set()

            def calculation(ident):
                if ident not in calculations:
                    record = primed_calculations.get(ident)
                    if record is None:
                        record = reads.calculation(ident)
                    calculations[ident] = record | {
                        "decoded": record["outcome"],
                        "period": YearMonth(record["period"]).ordinal,
                    }
                return calculations[ident]

            def source_fact(ident):
                if ident not in actual_facts:
                    actual_facts[ident] = report_source_fact(calculation(ident))
                return actual_facts[ident]

            def relations(ident):
                if ident not in relation_records:
                    relation_records[ident] = reads.relations(ident)
                if ident not in reported_relations:
                    problems.extend(relation_records[ident]["issues"])
                    reported_relations.add(ident)
                return relation_records[ident]

            for row in rows:
                row["amount"] = row["debit"] - row["credit"]
                frozen = frozen_semantics.get(row["period"])
                if frozen is not None and row["account"] in CASH_ACCOUNTS | set(PROFIT_ACCOUNTS):
                    selected_key = row["version_id"], row["line_no"]
                    compact = frozen[0].get(selected_key)
                    source_row = frozen[1].get(selected_key)
                    if (
                        compact is None
                        or source_row is None
                        or (
                            row["period"],
                            row["version_id"],
                            row["line_no"],
                            row["calculation_id"],
                            row["reverses_id"],
                            row["account"],
                            row["debit"],
                            row["credit"],
                            row["cashflow"],
                            row["kind"],
                            row["fact_id"],
                            row["calculation_period"],
                            row["calculation_subject_id"],
                            compact["source_result_digest"],
                        )
                        != (
                            source_row[1],
                            source_row[2],
                            source_row[3],
                            source_row[5],
                            source_row[7],
                            source_row[8],
                            source_row[9],
                            source_row[10],
                            source_row[11],
                            source_row[12],
                            source_row[13],
                            source_row[14],
                            source_row[15],
                            source_row[16].hex(),
                        )
                        or compact["source_calculation_id"] != row["calculation_id"]
                        or compact["source_fact_id"] != row["fact_id"]
                    ):
                        raise KernelError(
                            "content_integrity_failed", "报表语义与已选凭证来源不一致"
                        )
                    row.update(expand_line_fields(compact))
                    source_id = compact["source_calculation_id"]
                    if source_id not in reported_relations:
                        problems.extend(compact["relation_issues"])
                        reported_relations.add(source_id)
                    row["classification"] = classifications.get(
                        row["reverses_id"] or row["version_id"]
                    )
                    continue
                source_id = row["calculation_id"]
                if row["reverses_id"]:
                    if row["reverses_id"] not in originals:
                        originals[row["reverses_id"]] = connection.execute(
                            "SELECT calculation_id FROM voucher_version WHERE id=?",
                            (row["reverses_id"],),
                        ).fetchone()[0]
                    source_id = originals[row["reverses_id"]]
                projected = projected_lines.get((row["version_id"], row["line_no"]))
                if projected is not None:
                    row.update(expand_line_fields(projected))
                    if source_id not in reported_relations:
                        problems.extend(relation_records[source_id]["issues"])
                        reported_relations.add(source_id)
                else:
                    row.update(
                        immutable_line_fields(
                            row,
                            source_id,
                            calculation=calculation,
                            source_fact=source_fact,
                            relations=relations,
                        )
                    )
                key = row["reverses_id"] or row["version_id"]
                row["classification"] = classifications.get(key)
                if row["account"] in RECLASS:
                    resolution = relation_records[source_id]
                    explicit = (
                        [
                            x.counterparty_id
                            for x in row["classification"].counterparties
                            if x.line_no == row["line_no"]
                        ]
                        if row["classification"]
                        else []
                    )
                    party = report_party_splits(
                        row,
                        resolution,
                        explicit_party_id=explicit[0] if len(explicit) == 1 else None,
                    )
                    row["party_state"] = party["state"]
                    row["party_key"] = party["party_key"]
                    row["party"] = party["party_id"]
                    if party["splits"] is not None:
                        row["party_splits"] = party["splits"]
                    problems.extend(party["issues"])
            for row in rows:
                if row["account"] not in _POSITION_ACCOUNTS:
                    problems.append(
                        issue("account_mapping", "非零凭证行尚未映射到三表", account=row["account"])
                    )
            opening_rows, opening_calculation, unknown_opening_period = _opening_rows(
                connection, self.engine, source, end, reads, problems
            )
            opening_source, new_company_opening = None, False
            if opening_calculation is not None:
                opening_fact = reads.fact_version(opening_calculation["fact_id"])
                references.add(opening_fact.id)
                opening_source = {
                    "subject_id": opening_calculation["subject_id"],
                    "calculation_id": opening_calculation["id"],
                    "fact_id": opening_fact.id,
                    "period": str(YearMonth.from_ordinal(opening_calculation["period"])),
                    "digest": opening_calculation["digest"].hex(),
                }
                new_company_opening = _new_company_zero_opening(
                    profile.fact if profile else None, opening_fact.fact, opening_calculation
                )
                if not new_company_opening and (
                    profile is None
                    or profile.fact.kind != ContinuationReportProfile.kind
                    or profile.fact.opening_package_id != opening_calculation["subject_id"]
                    or profile.fact.bookkeeping_start.ordinal != opening_calculation["period"]
                ):
                    problems.append(
                        issue(
                            "continuation_report_profile",
                            "非零或已接续期初必须采用匹配总清单及建账月的报表口径",
                        )
                    )
            elif profile is not None and profile.fact.kind == ContinuationReportProfile.kind:
                problems.append(issue("opening_package", "接续报表所采用的期初总清单尚未正式发布"))
            rows.extend(opening_rows)
            totals = {
                cutoff: _account_totals(
                    connection,
                    cutoff,
                    source,
                    opening_rows,
                    unknown_opening_period,
                    reads=reads,
                )
                for cutoff in {year_start.ordinal - 1, start.ordinal - 1, end.ordinal}
            }
            statements = _statements(
                rows,
                start.ordinal,
                year_start.ordinal,
                end.ordinal,
                problems,
                account_balances=totals,
                party_balances=party_balances,
                frozen_flows=frozen_flows,
            )
            carry = None
            if (
                opening_calculation is not None
                and not new_company_opening
                and year_start.ordinal < book_start.ordinal <= end.ordinal
            ):
                carries = [
                    f
                    for f in facts
                    if f.fact.kind == ReportCarryForward.kind
                    and f.fact.period == book_start
                    and f.fact.opening_package_id == opening_calculation["subject_id"]
                ]
                if len(carries) != 1:
                    problems.append(
                        issue(
                            "report_carry_forward",
                            "年中接账需要年初资产负债表及建账前本年、本季利润和现金流；不得推定为零",
                        )
                    )
                else:
                    carry = carries[0].fact
                    _apply_carry_forward(
                        statements, carry, opening_rows, start, book_start, problems
                    )
            confirmations = [f for f in facts if f.fact.kind == ReportIncomeTaxConfirmation.kind]
            for q in range((int(max(book_start, year_start)[5:]) - 1) // 3 + 1, quarter + 1):
                month = YearMonth(f"{year:04d}-{q * 3:02d}")
                selected = [f for f in confirmations if f.fact.period == month]
                if len(selected) != 1:
                    problems.append(
                        issue(
                            "report_income_tax_confirmation",
                            "该季度需要唯一的所得税不适用、零或已核算确认",
                            period=str(month),
                        )
                    )
                    continue
                confirmed = selected[0].fact
                assessed = sum_fen(
                    r["amount"]
                    for r in rows
                    if r["account"] == "5801"
                    and year_start.ordinal <= r["period"] <= month.ordinal
                    and getattr(r["fact"], "tax_year", year) == year
                )
                assessed += sum(
                    flow["assessed"].get(str(year), 0)
                    for selected_month, flow in frozen_flows.items()
                    if selected_month <= month.ordinal
                )
                if carry is not None:
                    assessed = sum_fen(
                        (
                            assessed,
                            next(
                                row.year_to_date_fen for row in carry.prior_profit if row.line == 31
                            ),
                        )
                    )
                if assessed != confirmed.cumulative_assessed_fen:
                    problems.append(
                        issue(
                            "cumulative_assessed_fen",
                            "所得税确认与当年累计所得税费用不一致",
                            period=str(month),
                        )
                    )
                if confirmed.calculation_id:
                    tax = calculation(confirmed.calculation_id)
                    frozen = connection.execute(
                        "SELECT * FROM close_reference WHERE reference_type='calculation' "
                        "AND reference_id=? AND close_period<=?",
                        (tax["id"], month.ordinal),
                    ).fetchall()
                    reads.verify_close_references(frozen)
                    active = connection.execute(
                        "SELECT 1 FROM calculation_current WHERE calculation_id=?", (tax["id"],)
                    ).fetchone()
                    if (
                        tax["kind"] != "income_tax_assessment"
                        or tax["period"] != month.ordinal
                        or tax["decoded"]["values"]["cumulative_assessed_fen"]
                        != confirmed.cumulative_assessed_fen
                        or (source == "closed" and not frozen)
                        or (source == "open" and not frozen and not active)
                    ):
                        problems.append(
                            issue(
                                "calculation_id",
                                "所得税确认未引用该季度有效的核算版本",
                                period=str(month),
                            )
                        )
            if source == "open":
                pending = connection.execute(
                    "SELECT p.subject_id FROM pending p CROSS JOIN fact_current a "
                    "CROSS JOIN fact_revision f WHERE a.subject_id=p.subject_id "
                    "AND f.id=a.fact_id AND f.period<=? LIMIT 1",
                    (end.ordinal,),
                ).fetchone()
                if pending:
                    problems.append(
                        issue(
                            "pending", "报表范围内存在尚未处理的事实或更正", subject_id=pending[0]
                        )
                    )
            if not external_connection:
                connection.commit()
        checks = _checks(statements)
        if not all(c["passed"] for c in checks):
            problems.append(issue("cross_checks", "三表勾稽未通过"))
        for statement in statements.values():
            if any(
                value is not None and abs(value) > TEMPLATE_MAX_FEN
                for row in statement.values()
                for key, value in row.items()
                if key.endswith("_fen")
            ):
                problems.append(issue("template_amount", "金额超出固定模板可精确展示范围"))
        problems = list({canonical(item): item for item in problems}.values())
        plan = {
            "status": "needs_information" if problems else "ready",
            "source": source,
            "company_id": self.store.company_id,
            "database_id": self.store.database_id,
            "organization": {
                "name": profile.fact.company_name if profile else "",
                "taxpayer_identification_number": identity["taxpayer_id"],
            },
            "period": {
                "year": year,
                "quarter": quarter,
                "quarter_start": f"{start}-01",
                "quarter_end": f"{end}-{calendar.monthrange(year, int(end[5:]))[1]:02d}",
            },
            "statements": statements,
            "checks": checks,
            "fact_issues": problems,
            "source_closes": [
                {"period": str(YearMonth.from_ordinal(r["period"])), "digest": r["digest"].hex()}
                for r in close_rows
            ],
            "report_fact_ids": sorted(references),
            "opening_source": opening_source,
            "epochs": epochs,
            "template": {
                "name": TEMPLATE_FILE_NAME,
                "sha256": TEMPLATE_SHA256.lower(),
                "profile": TEMPLATE_PROFILE,
            },
            "rule": {
                "version": ACCOUNTING_RULE_VERSION,
                "adapter_version": "sqlite-quarterly-v1",
                "effective_from": ACCOUNTING_RULE_EFFECTIVE_FROM,
                "source_url": ACCOUNTING_RULE_SOURCE_URL,
            },
        }
        return plan | {"digest": digest({k: v for k, v in plan.items() if k != "epochs"}).hex()}

    def preview_export(self, year: int, quarter: int, *, carry_forward_fact_id: str | None = None):
        plan, _ = self._prepare_export(year, quarter, carry_forward_fact_id=carry_forward_fact_id)
        return plan

    def _prepare_export(self, year, quarter, *, carry_forward_fact_id=None):
        from .query_reads import QueryReads
        from .read_state import repair_revision

        with QueryReads.snapshot(self.engine) as reads:
            prepared_revision = repair_revision(reads.connection)
            plan = self._report(
                year,
                quarter,
                source="closed",
                carry_forward_fact_id=carry_forward_fact_id,
                connection=reads.connection,
                reads=reads,
            )
        if plan["fact_issues"]:
            raise KernelError(
                "needs_information", "季度三表尚不具备导出条件", fact_issues=plan["fact_issues"]
            )
        return plan, prepared_revision

    def confirm_export(
        self,
        year: int,
        quarter: int,
        *,
        preview_digest: str,
        epochs: dict,
        output_directory: str,
        request_id: str,
        carry_forward_fact_id: str | None = None,
    ):
        directory = str(Path(output_directory).resolve())
        hashed = digest(
            [
                "report_export",
                year,
                quarter,
                preview_digest,
                epochs,
                directory,
                carry_forward_fact_id,
            ]
        )
        cached = self.engine._cached(request_id, hashed)
        if cached is not None:
            return cached
        plan, prepared_revision = self._prepare_export(
            year, quarter, carry_forward_fact_id=carry_forward_fact_id
        )
        if plan["digest"] != preview_digest or any(
            plan["epochs"].get(x) != epochs.get(x) for x in ("accounting", "material", "management")
        ):
            raise KernelError("preview_expired", "报表导出预览已过期")

        def operation(connection):
            from .read_state import check_repair_revision

            check_repair_revision(connection, prepared_revision)
            ident = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO jobs(id,kind,payload,status) VALUES(?,'report_export',?,'pending')",
                (ident, canonical({"plan": plan, "output_directory": directory})),
            )
            from .read_indexes import sync_job

            sync_job(connection, ident)
            return {"status": "queued", "job_id": ident, "preview_digest": preview_digest}

        return self.engine._write(
            request_id,
            hashed,
            epochs,
            (),
            "report_export",
            operation,
            checked_lanes=("accounting", "material", "management"),
        )

    def confirm_browser_export(
        self,
        year: int,
        quarter: int,
        *,
        preview_digest: str,
        epochs: dict,
        request_id: str,
        carry_forward_fact_id: str | None = None,
    ):
        """Queue the same frozen report in a service-owned directory for browser delivery."""
        root = self.store.path.parent.resolve()
        directory = (
            root
            / "exports"
            / "browser-reports"
            / hashlib.sha256(request_id.encode("utf-8")).hexdigest()
        ).resolve()
        if not directory.is_relative_to(root):
            raise KernelError("report_target_invalid", "报表输出目录不在当前公司目录内")
        return self.confirm_export(
            year,
            quarter,
            preview_digest=preview_digest,
            epochs=epochs,
            output_directory=str(directory),
            request_id=request_id,
            carry_forward_fact_id=carry_forward_fact_id,
        )

    def browser_report_details(self, plan, closed, *, connection=None, reads=None):
        """Describe the sources actually used without changing the frozen report plan."""
        external_connection = connection is not None
        if reads is not None and (
            not external_connection
            or reads.store is not self.store
            or reads.connection is not connection
            or not reads._snapshot_active
            or not connection.in_transaction
        ):
            raise ValueError("browser report reads belong to another snapshot")
        manager = (
            nullcontext(connection)
            if external_connection
            else self.store.connection(read_only=True)
        )
        with manager as connection:
            if not external_connection:
                connection.execute("BEGIN")
            from .query_reads import QueryReads

            reads = reads or QueryReads(self.engine, connection)
            # These are the only two source counts used by this projection.
            # Keep authoritative subject kinds and exact plan membership, but
            # avoid grouping every historical source into unused categories.
            classification_count, tax_confirmation_count = connection.execute(
                "SELECT count(*) FILTER (WHERE s.kind=?),"
                "count(*) FILTER (WHERE s.kind=?) FROM json_each(?) ids "
                "JOIN fact_revision f INDEXED BY fact_id_subject_cover ON f.id=ids.value "
                "JOIN subject s INDEXED BY subject_id_kind_cover ON s.id=f.subject_id",
                (ReportClassification.kind, ReportIncomeTaxConfirmation.kind,
                 canonical(plan["report_fact_ids"])),
            ).fetchone()
            source = plan.get("opening_source")
            choices = []
            if source:
                rows = connection.execute(
                    "SELECT f.id FROM fact_revision f JOIN subject s ON s.id=f.subject_id "
                    "WHERE s.kind='report_carry_forward' AND f.period=? "
                    "ORDER BY f.subject_id,f.revision",
                    (YearMonth(source["period"]).ordinal,),
                )
                identifiers = [row[0] for row in rows]
                _verify_report_fact_sources(connection, reads, identifiers)
                versions = reads.fact_versions(identifiers)
                for ident in identifiers:
                    fact = versions[ident]
                    if fact.fact.opening_package_id != source["subject_id"]:
                        continue
                    choices.append(
                        {
                            "fact_id": fact.id,
                            "subject_id": fact.subject_id,
                            "revision": fact.revision,
                            "period": str(fact.fact.period),
                            "label": (
                                f"资料 {len(choices) + 1} · {fact.fact.period} "
                                f"· 第 {fact.revision} 版"
                            ),
                            "evidence_count": len(fact.evidence),
                            "used": fact.id in plan["report_fact_ids"],
                        }
                    )
            issue_vouchers = reads.vouchers(
                {
                    row[0]
                    for row in connection.execute(
                        "SELECT id FROM voucher_version WHERE id IN "
                        "(SELECT json_extract(value,'$.voucher_version_id') FROM json_each(?))",
                        (canonical(plan["fact_issues"]),),
                    )
                }
            )
            issues = []
            for issue in plan["fact_issues"]:
                item = dict(issue)
                if item.get("voucher_version_id"):
                    voucher = issue_vouchers.get(item["voucher_version_id"])
                    if voucher:
                        item["voucher_number"] = voucher["number"]
                        item["period"] = str(YearMonth.from_ordinal(voucher["period"]))
                issues.append(item)
        return {
            "carry_forward_options": choices,
            "classification_count": classification_count,
            "income_tax_confirmation_count": tax_confirmation_count,
            "issues": issues,
            "close_state": "open"
            if any(item["field"] in {"period", "closed_periods"} for item in closed["fact_issues"])
            else "closed",
        }

    def browser_job_results(self, jobs):
        """Expose a download only after the same checks used for file delivery."""
        jobs = list(jobs)
        report_jobs = {job["id"] for job in jobs if job["kind"] == "report_export"}
        plans, sources, invalid = {}, {}, set()
        if report_jobs:
            from .query_reads import QueryReads

            # One authenticated read view for every title in this response. File
            # delivery is checked separately against its own current artifact.
            with QueryReads.snapshot(self.engine) as reads:
                connection = reads.connection
                rows = {
                    row["id"]: row["payload"]
                    for row in connection.execute(
                        "SELECT id,payload FROM jobs WHERE id IN "
                        "(SELECT value FROM json_each(?))",
                        (canonical(sorted(report_jobs)),),
                    )
                }
                requested_ids = set()
                for job_id in report_jobs:
                    try:
                        payload = json.loads(rows[job_id])
                        plan = payload["plan"]
                        identifiers = plan["report_fact_ids"]
                        period = plan["period"]
                        if (
                            not isinstance(identifiers, list)
                            or any(type(ident) is not str or not ident for ident in identifiers)
                            or len(set(identifiers)) != len(identifiers)
                            or not isinstance(period, dict)
                            or type(period.get("year")) is not int
                            or not 1 <= period["year"] <= 9999
                            or type(period.get("quarter")) is not int
                            or period["quarter"] not in range(1, 5)
                            or plan.get("company_id") != self.store.company_id
                            or plan.get("database_id") != self.store.database_id
                            or plan.get("digest")
                            != digest(
                                {
                                    key: value
                                    for key, value in plan.items()
                                    if key not in {"digest", "epochs"}
                                }
                            ).hex()
                        ):
                            raise ValueError("report plan identity or source set differs")
                        planned = set(identifiers)
                        plans[job_id] = payload, plan, planned
                        requested_ids.update(planned)
                    except (ValueError, TypeError, KeyError):
                        invalid.add(job_id)
                # A job title needs exact plan membership, not every non-carry
                # typed body. Complete content verification checks those bodies.
                existing = (
                    {
                        row[0]
                        for row in connection.execute(
                            "SELECT f.id FROM json_each(?) ids "
                            "JOIN fact_revision f ON f.id=ids.value "
                            "JOIN subject s ON s.id=f.subject_id",
                            (canonical(sorted(requested_ids)),),
                        )
                    }
                    if requested_ids
                    else set()
                )
                carry_kinds = {
                    row[0]
                    for row in connection.execute(
                        "SELECT f.id FROM subject s CROSS JOIN fact_revision f "
                        "ON f.subject_id=s.id WHERE s.kind=?",
                        (ReportCarryForward.kind,),
                    )
                    if row[0] in requested_ids
                }
                carry_typed = {
                    row[0]
                    for row in connection.execute(
                        "SELECT revision_id FROM fact_report_carry_forward"
                    )
                    if row[0] in requested_ids
                }
                for job_id, (_, plan, planned) in plans.items():
                    if not planned <= existing:
                        invalid.add(job_id)
                        continue
                    # Either independent header can reveal a planned carry.
                    # A changed subject kind must not hide its existing typed
                    # body, and a forged kind must not invent one.
                    matched = sorted(planned & (carry_kinds | carry_typed))
                    try:
                        if any(
                            ident not in carry_kinds or ident not in carry_typed
                            for ident in matched
                        ):
                            raise ValueError("report carry kind and typed body differ")
                        _verify_report_fact_sources(connection, reads, matched)
                        if any(
                            version.fact.kind != ReportCarryForward.kind
                            for version in reads.fact_versions(matched).values()
                        ):
                            raise ValueError("report carry source kind differs")
                    except (KernelError, ValidationError, ValueError, TypeError, KeyError):
                        invalid.add(job_id)
                        continue
                    sources[job_id] = {
                        "year": plan["period"]["year"],
                        "quarter": plan["period"]["quarter"],
                        "carry_forward_fact_id": matched[0] if len(matched) == 1 else None,
                    }
        result = []
        for job in jobs:
            item = {
                **job,
                "download_available": False,
                "download_file_name": None,
                "delivery_status": "pending"
                if job["status"] in {"pending", "running"}
                else "unavailable",
                "delivery_message": job.get("error_message") if job["status"] == "failed" else None,
            }
            if job["kind"] == "report_export":
                if job["id"] in invalid:
                    item.update(
                        delivery_status="invalid",
                        delivery_message="任务来源信息无法验证，请重新生成报表。",
                    )
                    result.append(item)
                    continue
                item["report_source"] = sources[job["id"]]
            if job["kind"] == "report_export" and job["status"] == "succeeded":
                payload = plans[job["id"]][0]
                directory = payload.get("output_directory")
                browser_root = (self.store.path.parent / "exports" / "browser-reports").resolve()
                if isinstance(directory, str) and not Path(directory).resolve().is_relative_to(
                    browser_root
                ):
                    item.update(
                        delivery_status="external",
                        delivery_message="此报表通过会计任务交付，未提供浏览器下载。",
                    )
                    result.append(item)
                    continue
                try:
                    name, _ = self.download_browser_report(job["id"])
                except KernelError as exc:
                    item.update(delivery_status="invalid", delivery_message=str(exc))
                else:
                    item.update(
                        download_available=True, download_file_name=name, delivery_status="verified"
                    )
            result.append(item)
        return result

    def download_browser_report(self, job_id: str):
        """Read a verified successful task; never accept a caller-controlled file path."""
        with self.store.connection(read_only=True) as connection:
            row = connection.execute(
                "SELECT kind,status,payload,result FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
        if row is None or row["kind"] != "report_export":
            raise KernelError("unknown_report_job", "当前公司没有该报表任务")
        if row["status"] != "succeeded":
            raise KernelError("report_job_not_ready", "报表文件尚未生成成功")
        try:
            payload, result = json.loads(row["payload"]), json.loads(row["result"])
            plan = payload["plan"]
            company_root = self.store.path.parent.resolve()
            root = (company_root / "exports" / "browser-reports").resolve()
            target = Path(payload["output_directory"]).resolve()
            name = _workbook_name(plan)
            file = (target / name).resolve()
            if (
                not root.is_relative_to(company_root)
                or target == root
                or not target.is_relative_to(root)
                or not file.is_relative_to(target)
                or Path(result["directory"]).resolve() != target
                or Path(result["path"]).resolve() != file
                or plan["company_id"] != self.store.company_id
                or plan["database_id"] != self.store.database_id
                or result["report_digest"] != plan["digest"]
                or plan["digest"]
                != digest({k: v for k, v in plan.items() if k not in {"digest", "epochs"}}).hex()
            ):
                raise ValueError("report identity or boundary differs")
            manifest_path = (target / "manifest.json").resolve()
            if not manifest_path.is_relative_to(target):
                raise ValueError("manifest escapes task directory")
            content = file.read_bytes()
            checksum = hashlib.sha256(content).hexdigest()
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if result["sha256"] != checksum or manifest != {
                "job_id": job_id,
                "report_digest": plan["digest"],
                "sha256": checksum,
                "plan": plan,
            }:
                raise ValueError("report file differs from frozen task")
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise KernelError("report_download_invalid", "报表文件校验未通过，请重新生成") from exc
        return name, content


def _new_company_zero_opening(profile, fact, calculation):
    """An evidenced formation boundary does not imply nonexistent earlier activity."""
    from .domains.opening import OpeningPackage

    if (
        not isinstance(profile, ReportProfile)
        or profile.newly_established_zero_opening_confirmed is not True
        or not isinstance(fact, OpeningPackage)
        or profile.bookkeeping_start != fact.period
        or profile.bookkeeping_start.ordinal != calculation["period"]
        or fact.members
    ):
        return False
    counts = fact.counts.model_dump()
    outcome = calculation["outcome"]
    values = (json.loads(outcome) if isinstance(outcome, str) else outcome)["values"]
    return (
        fact.completeness_confirmed is True
        and all(count == 0 for count in counts.values())
        and values.get("counts") == counts
        and values.get("members") == []
        and values.get("debit_fen") == 0
        and values.get("credit_fen") == 0
        and values.get("bookkeeping_start") == str(fact.period)
    )


def _opening_rows(connection, engine, source, end, reads, problems):
    from .business_queries import BusinessQueries

    selected = BusinessQueries(engine, reads=reads)._selected_accounting(
        connection, None, str(end), kinds={"opening_package"}, include_vouchers=False
    )["through_period"]
    unresolved = selected["unestablished_state_selections"]
    unknown_period = None
    if unresolved:
        for selection in unresolved:
            candidates = selection["candidates"]
            metadata = reads.metadata(item["calculation_id"] for item in candidates)
            affected = min(YearMonth(item["period"]).ordinal - 1 for item in metadata.values())
            unknown_period = affected if unknown_period is None else min(unknown_period, affected)
            problems.append(
                issue(
                    "opening_package.selection",
                    "冻结资料不能证明期初接续结果已被采用",
                    reason=selection["reason"],
                    candidates=candidates,
                    trace_targets=[
                        {"calculation_id": item["calculation_id"]} for item in candidates
                    ],
                )
            )
    identifiers = {
        item["calculation_id"]
        for item in selected["state_results"]
        if source == "open" or item["selection_source"] == "close_manifest"
    }
    if not identifiers:
        return [], None, unknown_period
    if len(identifiers) != 1:
        raise KernelError("ambiguous_opening", "报表期间存在不唯一的期初接续版本")
    record = reads.calculation(next(iter(identifiers)))
    decoded = record["outcome"]
    record = record | {
        "period": YearMonth(record["period"]).ordinal,
        "digest": bytes.fromhex(record["result_digest"]),
    }
    result = []
    for member in decoded["values"]["members"]:
        values = member["values"]
        for number, line in enumerate(member["opening_lines"], 1):
            amount = line["debit"] - line["credit"]
            normal = "debit" if amount > 0 else "credit"
            obligations = [
                item
                for item in values.get("obligations", ())
                if item["account"] == line["account"] and item.get("normal") == normal
            ]
            valid_splits = (
                obligations
                and all(
                    isinstance(item.get("counterparty_id"), str)
                    and item["counterparty_id"]
                    and type(item.get("amount_fen")) is int
                    and item["amount_fen"] > 0
                    for item in obligations
                )
                and sum(item["amount_fen"] for item in obligations) == abs(amount)
            )
            splits = (
                tuple(
                    (
                        ("party", item["counterparty_id"]),
                        item["amount_fen"] * (1 if amount > 0 else -1),
                    )
                    for item in obligations
                )
                if valid_splits
                else None
            )
            party_key = splits[0][0] if splits is not None and len(splits) == 1 else None
            result.append(
                {
                    **line,
                    "amount": amount,
                    "period": record["period"] - 1,
                    "kind": member["kind"],
                    "version_id": "opening:" + record["id"] + ":" + member["subject_id"],
                    "line_no": number,
                    "reverses_id": None,
                    "fact": SimpleNamespace(**values),
                    "cash_source": SimpleNamespace(**values),
                    "classification": None,
                    "party_state": "resolved" if splits is not None else "unresolved",
                    "party_key": party_key,
                    "party_splits": splits,
                    "opening": True,
                }
            )
    return result, record, unknown_period


def _apply_carry_forward(statements, fact, opening_rows, quarter_start, book_start, problems):
    balance = {row.line: row.beginning_fen for row in fact.year_beginning_balance}
    equations = (
        balance[20] == balance[18] - balance[19],
        balance[15] == sum(balance[row] for row in range(1, 10)) + balance[14],
        balance[29] == sum(balance[row] for row in (16, 17, 20, 21, 22, 23, 24, 25, 26, 27, 28)),
        balance[30] == balance[15] + balance[29],
        balance[41] == sum(balance[row] for row in range(31, 41)),
        balance[46] == sum(balance[row] for row in range(42, 46)),
        balance[47] == balance[41] + balance[46],
        balance[52] == sum(balance[row] for row in range(48, 52)),
        balance[53] == balance[47] + balance[52],
        balance[30] == balance[53],
    )
    if not all(equations):
        problems.append(
            issue("report_carry_forward", "原报表年初资产负债表的明细、合计或平衡不一致")
        )
    for line, value in balance.items():
        statements["balance_sheet"][str(line)]["beginning_fen"] = value
    cutover_cash = sum_fen(row["amount"] for row in opening_rows if row["account"] in CASH_ACCOUNTS)
    same_quarter = (int(book_start[5:]) - 1) // 3 == (int(quarter_start[5:]) - 1) // 3
    for name, previous in (
        ("profit_statement", fact.prior_profit),
        ("cash_flow_statement", fact.prior_cash),
    ):
        prior = {row.line: row for row in previous}
        if name == "cash_flow_statement" and prior[21].year_to_date_fen != balance[1]:
            problems.append(issue("report_carry_forward", "原报表年初现金与年初资产负债表不一致"))
        for column, previous_column in (
            ("year_to_date_fen", "year_to_date_fen"),
            ("current_fen", "quarter_to_date_fen"),
        ):
            values = {line: getattr(row, previous_column) for line, row in prior.items()}
            if name == "profit_statement":
                checks = (
                    values[32] == values[30] - values[31],
                    values[30] == values[21] + values[22] - values[24],
                    values[21]
                    == values[1]
                    - values[2]
                    - values[3]
                    - values[11]
                    - values[14]
                    - values[18]
                    + values[20],
                )
            else:
                checks = (
                    values[7] == values[1] + values[2] - sum(values[x] for x in (3, 4, 5, 6)),
                    values[13] == sum(values[x] for x in (8, 9, 10)) - values[11] - values[12],
                    values[19] == values[14] + values[15] - values[16] - values[17] - values[18],
                    values[20] == values[7] + values[13] + values[19],
                    values[22] == values[21] + values[20] == cutover_cash,
                )
            if not all(checks):
                problems.append(
                    issue(
                        "report_carry_forward",
                        "原报表累计数与内部勾稽或期初现金不一致",
                        statement=name,
                        column=column,
                    )
                )
            if column == "current_fen" and not same_quarter:
                continue
            for line, value in values.items():
                if name == "cash_flow_statement" and line in (21, 22):
                    continue
                statements[name][str(line)][column] = sum_fen(
                    (statements[name][str(line)][column], value)
                )
            if name == "cash_flow_statement":
                statements[name]["21"][column] = values[21]
                statements[name]["22"][column] = sum_fen(
                    (values[21], statements[name]["20"][column])
                )


def _profit_rows(rows, begin, end, problems):
    result = {line: 0 for line in PROFIT_NAMES}
    for row in rows:
        if not begin <= row["period"] <= end or row["account"] not in PROFIT_ACCOUNTS:
            continue
        main, sign = PROFIT_ACCOUNTS[row["account"]]
        result[main] += row["amount"] * sign
        account, kind, fact = row["account"], row["kind"], row["fact"]
        automatic = account == "5603" and (
            kind in {"loan_interest", "bank_income"}
            or (kind == "expense" and getattr(fact, "expense_class", None) == "bank_fee")
        )
        if account == "560301" or (
            account == "5603"
            and (kind == "loan_interest" or getattr(fact, "income_kind", None) == "bank_interest")
        ):
            result[19] += row["amount"]
        if account == "6301" and (
            kind == "tax_assessment" or getattr(fact, "income_kind", None) == "government_grant"
        ):
            result[23] -= row["amount"]
        details = (
            [x for x in row["classification"].profit_details if x.line_no == row["line_no"]]
            if row["classification"]
            else []
        )
        if account == "5403":
            direction = -1 if row["reverses_id"] else 1
            automatic_tax = (
                kind == "tax_assessment"
                and row["amount"] * direction > 0
                and abs(row["amount"]) == row["values"].get("surtax_fen")
            )
            if automatic_tax:
                result[6] += direction * row["values"]["urban_tax_fen"]
                result[10] += direction * (
                    row["values"]["education_tax_fen"] + row["values"]["local_education_tax_fen"]
                )
                if details:
                    problems.append(
                        issue(
                            "report_classification.profit_details",
                            "已由计税依据确定附加税明细，不应重复分类",
                        )
                    )
            elif sum(x.amount_fen for x in details) == abs(row["amount"]) and all(
                x.detail_code.startswith("tax_") for x in details
            ):
                for item in details:
                    result[6 if item.detail_code == "tax_urban" else 10] += item.amount_fen * (
                        1 if row["amount"] > 0 else -1
                    )
            else:
                problems.append(
                    issue(
                        "report_classification.profit_details",
                        "附加税退抵差额需有依据的城市维护税及教育附加明细",
                        voucher_version_id=row["reverses_id"] or row["version_id"],
                        line_no=row["line_no"],
                    )
                )
            continue
        if account in {"5601", "5602", "5603"} and not automatic:
            family = {"5601": "sales_", "5602": "management_", "5603": "finance_"}[account]
            if sum(x.amount_fen for x in details) != abs(row["amount"]) or any(
                not x.detail_code.startswith(family) for x in details
            ):
                problems.append(
                    issue(
                        "report_classification.profit_details",
                        "费用明细分类需完整覆盖原凭证金额",
                        voucher_version_id=row["reverses_id"] or row["version_id"],
                        line_no=row["line_no"],
                    )
                )
            else:
                for item in details:
                    if item.detail_code in DETAIL_LINES:
                        result[DETAIL_LINES[item.detail_code]] += item.amount_fen * (
                            1 if row["amount"] > 0 else -1
                        )
        elif details:
            problems.append(
                issue(
                    "report_classification.profit_details",
                    "该行已有确定性分类或不属于可分类的费用",
                    voucher_version_id=row["version_id"],
                    line_no=row["line_no"],
                )
            )
    result[21] = (
        result[1] - result[2] - result[3] - result[11] - result[14] - result[18] + result[20]
    )
    result[30] = result[21] + result[22] - result[24]
    result[32] = result[30] - result[31]
    return result


def _cash_rows(rows, begin, end, problems, *, account_balances=None):
    result = {line: 0 for line in CASH_FLOW_NAMES}
    transfers = defaultdict(int)
    for row in rows:
        if (
            row.get("opening")
            or not begin <= row["period"] <= end
            or row["account"] not in CASH_ACCOUNTS
        ):
            continue
        fact, tag = row["cash_source"], row["cashflow"]
        detail = (
            [x for x in row["classification"].cash_details if x.line_no == row["line_no"]]
            if row["classification"]
            else []
        )
        category = CASH_CATEGORIES.get(tag)
        if tag == "financing_receipts":
            category = 15 if getattr(fact, "funding_kind", None) == "capital" else 14
        elif tag == "operating_payments":
            expense_class = getattr(fact, "expense_class", None)
            category = (
                3
                if expense_class == "service"
                else (6 if expense_class in {"administration", "sales", "bank_fee"} else None)
            )
        elif tag == "payroll":
            category = 5 if row.get("cash_obligation", {}).get("account") == "222103" else 4
        if (
            row["kind"] in {"funds_transfer", "cash_bank_transfer", "bank_platform_transfer"}
            and tag != "managed_reserve_outflow"
        ):
            transfers[row["version_id"]] += row["amount"]
            continue
        if detail:
            if sum(item.amount_fen for item in detail) != abs(row["amount"]) or (
                category is not None and any(item.category != category for item in detail)
            ):
                problems.append(
                    issue(
                        "report_classification.cash_details",
                        "现金分类须完整覆盖金额并与明确业务来源一致",
                        voucher_version_id=row["version_id"],
                        line_no=row["line_no"],
                    )
                )
                continue
            for item in detail:
                result[item.category] += item.amount_fen * (
                    (1 if row["amount"] > 0 else -1) * (1 if item.category in INFLOW_ROWS else -1)
                )
            continue
        if category is None:
            problems.append(
                issue(
                    "report_classification.cash_details",
                    "非零现金流缺少确定分类",
                    voucher_version_id=row["reverses_id"] or row["version_id"],
                    line_no=row["line_no"],
                )
            )
            continue
        result[category] += row["amount"] * (1 if category in INFLOW_ROWS else -1)
    if any(transfers.values()):
        problems.append(issue("cash_transfer", "内部资金划转未完整抵销"))
    result[7] = result[1] + result[2] - result[3] - result[4] - result[5] - result[6]
    result[13] = result[8] + result[9] + result[10] - result[11] - result[12]
    result[19] = result[14] + result[15] - result[16] - result[17] - result[18]
    result[20] = result[7] + result[13] + result[19]
    if account_balances is None:
        result[21] = sum_fen(
            r["amount"] for r in rows if r["account"] in CASH_ACCOUNTS and r["period"] < begin
        )
    else:
        totals = account_balances[begin - 1]
        result[21] = (
            None if totals is None else sum_fen(totals.get(account, 0) for account in CASH_ACCOUNTS)
        )
    result[22] = None if result[21] is None else result[21] + result[20]
    return result


def _statements(
    rows,
    start,
    year_start,
    end,
    problems,
    *,
    account_balances=None,
    party_balances=None,
    frozen_flows=None,
):
    def balance(as_of):
        if account_balances is not None:
            totals = account_balances[as_of]
            if totals is None:
                return {line: None for line in range(1, 54)}
            selected = (
                list(party_balances[as_of])
                if party_balances is not None
                else [
                    row
                    for row in rows
                    if row["period"] <= as_of
                    and (row["account"] in RECLASS or row["account"] not in _POSITION_ACCOUNTS)
                ]
            )
            selected.extend(
                {"account": account, "amount": amount}
                for account, amount in sorted(totals.items())
                if account not in RECLASS and account in _POSITION_ACCOUNTS
            )
        else:
            selected = (row for row in rows if row["period"] <= as_of)
        position = classify_financial_position(selected)
        problems.extend(position["issues"])
        return position["lines"]

    balance_begin, balance_end = balance(year_start - 1), balance(end)
    current_profit = _profit_rows(rows, start, end, problems)
    ytd_profit = _profit_rows(rows, year_start, end, problems)
    current_cash = _cash_rows(rows, start, end, problems, account_balances=account_balances)
    ytd_cash = _cash_rows(rows, year_start, end, problems, account_balances=account_balances)
    for month, flow in (frozen_flows or {}).items():
        if not year_start <= month <= end:
            continue
        for line, value in flow["profit"].items():
            ytd_profit[int(line)] += value
            if month >= start:
                current_profit[int(line)] += value
        for line, value in flow["cash"].items():
            ytd_cash[int(line)] += value
            if month >= start:
                current_cash[int(line)] += value
    for cash in (current_cash, ytd_cash):
        cash[22] = None if cash[21] is None else cash[21] + cash[20]
    return {
        "balance_sheet": {
            str(k): {"name": v, "ending_fen": balance_end[k], "beginning_fen": balance_begin[k]}
            for k, v in BALANCE_NAMES.items()
        },
        "profit_statement": {
            str(k): {"name": v, "current_fen": current_profit[k], "year_to_date_fen": ytd_profit[k]}
            for k, v in PROFIT_NAMES.items()
        },
        "cash_flow_statement": {
            str(k): {"name": v, "current_fen": current_cash[k], "year_to_date_fen": ytd_cash[k]}
            for k, v in CASH_FLOW_NAMES.items()
        },
    }


def _checks(statements):
    balance, profit, cash = (
        statements[x] for x in ("balance_sheet", "profit_statement", "cash_flow_statement")
    )

    def equal(*values):
        return None if any(value is None for value in values) else values[0] == values[1]

    def equation(left, *parts):
        return None if left is None or any(value is None for value in parts) else left == sum(parts)

    result = [
        {
            "code": f"balance_{column}",
            "passed": equal(balance["30"][column], balance["53"][column]),
        }
        for column in ("ending_fen", "beginning_fen")
    ]
    for column in ("current_fen", "year_to_date_fen"):
        result.extend(
            [
                {
                    "code": f"profit_{column}",
                    "passed": equation(
                        profit["32"][column],
                        profit["30"][column],
                        -profit["31"][column],
                    ),
                },
                {
                    "code": f"cash_{column}",
                    "passed": equation(
                        cash["20"][column],
                        cash["7"][column],
                        cash["13"][column],
                        cash["19"][column],
                    ),
                },
                {
                    "code": f"cash_ending_{column}",
                    "passed": equal(cash["22"][column], balance["1"]["ending_fen"]),
                },
            ]
        )
    return result


def _workbook_name(plan):
    return (
        f"{Path(TEMPLATE_FILE_NAME).stem}_{plan['period']['year']}Q{plan['period']['quarter']}.xlsx"
    )


def _publish_report(target, job_id, plan):
    name = _workbook_name(plan)
    expected = render_quarterly_template(plan)
    checksum = hashlib.sha256(expected).hexdigest()
    manifest = {"job_id": job_id, "report_digest": plan["digest"], "sha256": checksum, "plan": plan}
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".report-export-", dir=target.parent) as temporary:
            stage = Path(temporary) / "report"
            stage.mkdir()
            for filename, content in (
                (name, expected),
                ("manifest.json", canonical(manifest).encode()),
            ):
                with (stage / filename).open("xb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
            stage.rename(target)
    try:
        if (
            json.loads((target / "manifest.json").read_text(encoding="utf-8")) != manifest
            or (target / name).read_bytes() != expected
        ):
            raise ValueError("published content differs")
    except (OSError, ValueError) as exc:
        raise KernelError("report_target_conflict", "目标目录已存在且不符合冻结报表任务") from exc
    return {
        "directory": str(target),
        "path": str(target / name),
        "sha256": checksum,
        "report_digest": plan["digest"],
    }


def run_report_jobs(engine, *, limit: int = 10, fault=None):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("job limit must be 1..100")
    fault = fault or (lambda stage, ident: None)
    results, attempted = [], []
    with _worker_lock(engine.store.path) as acquired:
        if not acquired:
            return results
        for _ in range(limit):
            with engine.store.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                exclude = f"AND id NOT IN ({','.join('?' for _ in attempted)})" if attempted else ""
                row = connection.execute(
                    "SELECT id,payload FROM jobs WHERE kind='report_export' AND status IN "
                    "('pending','running','failed') AND attempts<3 "
                    + exclude
                    + " ORDER BY attempts,id LIMIT 1",
                    attempted,
                ).fetchone()
                if row is None:
                    connection.rollback()
                    break
                connection.execute(
                    "UPDATE jobs SET status='running',attempts=attempts+1,"
                    "last_error=NULL,error_code=NULL "
                    "WHERE id=?",
                    (row["id"],),
                )
                connection.commit()
            attempted.append(row["id"])
            try:
                payload = json.loads(row["payload"])
                plan = payload["plan"]
                if (
                    digest({k: v for k, v in plan.items() if k not in {"digest", "epochs"}}).hex()
                    != plan["digest"]
                    or plan["company_id"] != engine.store.company_id
                    or plan["database_id"] != engine.store.database_id
                    or plan["status"] != "ready"
                    or plan["source"] != "closed"
                    or plan["template"]["sha256"] != TEMPLATE_SHA256.lower()
                ):
                    raise KernelError("invalid_report_plan", "冻结报表计划身份、摘要或模板不一致")
                fault("before_files", row["id"])
                result = _publish_report(Path(payload["output_directory"]), row["id"], plan)
                fault("files_published", row["id"])
                status, error, error_code = "succeeded", None, None
            except Exception as exc:
                from .diagnostics import job_error_code

                result, status, error = None, "failed", f"{type(exc).__name__}: {exc}"[:500]
                error_code = job_error_code(exc)
            with engine.store.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE jobs SET status=?,last_error=?,error_code=?,result=? WHERE id=?",
                    (status, error, error_code, canonical(result) if result else None, row["id"]),
                )
                connection.commit()
            results.append(
                {"job_id": row["id"], "status": status, "result": result, "error_code": error_code}
            )
    return results
