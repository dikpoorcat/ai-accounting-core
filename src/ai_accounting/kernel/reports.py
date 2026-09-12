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

from pydantic import BaseModel, ConfigDict, Field, model_validator

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

from .backup import _worker_lock
from .contracts import Fact, KernelError, Read
from .query_semantics import (
    CASH_ACCOUNTS,
    CREDIT_BALANCE,
    DEBIT_BALANCE,
    PROFIT_ACCOUNTS,
    RECLASS,
    TAX_ACCOUNTS,
    classify_financial_position,
    report_party_splits,
)
from .types import Fen, NonNegativeFen, PositiveFen, YearMonth, canonical, digest, sum_fen

_POSITION_ACCOUNTS = (
    CASH_ACCOUNTS
    | set(PROFIT_ACCOUNTS)
    | set(DEBIT_BALANCE)
    | set(CREDIT_BALANCE)
    | TAX_ACCOUNTS
    | set(RECLASS)
)


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


def check_report_readiness(store, connection, period):
    """Query adapter uses the caller's one read-only snapshot; calculators stay pure."""
    profiles = tuple(
        row
        for read in required_reads(period)
        if read.kind in PROFILE_KINDS
        for row in store.select(connection, read)
    )
    if not profiles:
        return []
    adapter = Reports(SimpleNamespace(store=store))
    result = adapter._report(
        int(period[:4]),
        (int(period[5:]) - 1) // 3 + 1,
        source="open",
        connection=connection,
        through_period=period,
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


def _report_references(connection, end, source):
    from .read_indexes import CLOSE_REPORT_FACTS, verify_close_references

    selected = connection.execute(
        "SELECT * FROM close_reference WHERE reference_type='fact' AND path=? AND close_period<=?",
        (CLOSE_REPORT_FACTS, end.ordinal),
    ).fetchall()
    verify_close_references(connection, selected)
    references = {row["reference_id"] for row in selected}
    if source == "open":
        references.update(
            row[0]
            for row in connection.execute(
                "SELECT f.id FROM fact_current a JOIN fact_revision f ON f.id=a.fact_id "
                "JOIN subject s ON s.id=f.subject_id "
                "WHERE s.kind IN (SELECT value FROM json_each(?)) AND f.period<=? "
                "AND NOT EXISTS(SELECT 1 FROM period_close p WHERE p.period=f.period)",
                (canonical(sorted(REPORT_KINDS)), end.ordinal),
            )
        )
    return references


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


def _report_classifications(connection, reads, references, source_vouchers, end, source, problems):
    """Validate all applicable references; decode only classifications used by rows."""
    from .read_indexes import verify_close_references

    headers, conflicts = {}, set()
    for row in connection.execute(
        "SELECT c.revision_id,c.period,c.voucher_version_id,v.period AS voucher_period "
        "FROM json_each(?) ids CROSS JOIN fact_report_classification c ON c.revision_id=ids.value "
        "LEFT JOIN voucher_version v ON v.id=c.voucher_version_id",
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

    candidates = set(headers)
    candidates.update(
        row[0]
        for row in connection.execute(
            "SELECT v.id FROM json_each(?) ids CROSS JOIN voucher_version v "
            "INDEXED BY voucher_version_reverses ON v.reverses_id=ids.value",
            (canonical(sorted(headers)),),
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
    verify_close_references(connection, selected_references)

    detail_ids = []
    for key, row in headers.items():
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
    identifiers = {row["revision_id"] for key, row in headers.items() if key in source_vouchers}
    return {
        version.fact.voucher_version_id: version.fact
        for version in reads.fact_versions(identifiers).values()
    }


def _account_totals(connection, cutoff, source, opening_rows, unknown_opening_period=None):
    """Frozen cumulative accounts plus the explicitly open monthly interval."""
    boundary = connection.execute(
        "SELECT period,json_extract(manifest,'$.trial_balance') AS totals "
        "FROM period_close WHERE period<=? ORDER BY period DESC LIMIT 1",
        (cutoff,),
    ).fetchone()
    values = defaultdict(int)
    start = -1
    if boundary is not None and boundary["totals"] is not None:
        start = boundary["period"]
        for row in json.loads(boundary["totals"]):
            values[row["account"]] += row["debit"] - row["credit"]
    if start < 0 and unknown_opening_period is not None and unknown_opening_period <= cutoff:
        return None
    if source == "open":
        for row in connection.execute(
            "SELECT account,sum(debit-credit) amount FROM monthly_account "
            "WHERE period>? AND period<=? GROUP BY account",
            (start, cutoff),
        ):
            values[row["account"]] += row["amount"]
    elif start < 0:
        # Old incomplete manifests do not license current balances. The exact
        # selected voucher rows are still sufficient for an accounting sum.
        sql, parameters = _report_vouchers(cutoff, source)
        for row in connection.execute(
            "WITH selected AS (" + sql + ") SELECT l.account,sum(l.debit-l.credit) amount "
            "FROM selected v JOIN voucher_line l ON l.version_id=v.id GROUP BY l.account",
            parameters,
        ):
            values[row["account"]] += row["amount"]
    if start < 0:
        for row in opening_rows:
            if row["period"] <= cutoff:
                values[row["account"]] += row["amount"]
    return dict(values)


class Reports:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

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
        _, end, year_start = _periods(year, quarter)
        manager = (
            nullcontext(connection)
            if connection is not None
            else self.store.connection(read_only=True)
        )
        with manager as selected:
            if connection is None:
                selected.execute("BEGIN")
            from .query_reads import QueryReads

            reads = reads or QueryReads(self.engine, selected)
            references = _report_references(selected, end, "closed")
            profiles = self._report_profiles(selected, references, end, reads)
            profile = _applicable_profile(profiles, end)
            book_start = profile.fact.bookkeeping_start if profile else year_start
            periods = {
                row[0]
                for row in selected.execute(
                    "SELECT period FROM period_close WHERE period<=?", (end.ordinal,)
                )
            }
            problems = _closed_period_issues(periods, book_start, year_start, end)
            return {
                "complete": not problems,
                "fact_issues": problems,
                "bookkeeping_start": str(book_start),
            }

    def _report_profiles(self, connection, references, end, reads):
        identifiers = {
            row[0]
            for row in connection.execute(
                "WITH profiles AS (SELECT f.id,f.period FROM json_each(?) j "
                "JOIN fact_revision f ON f.id=j.value JOIN subject s ON s.id=f.subject_id "
                "WHERE s.kind IN (?,?) AND f.period<=?) "
                "SELECT id FROM profiles WHERE period=(SELECT max(period) FROM profiles)",
                (canonical(sorted(references)), *PROFILE_KINDS, end.ordinal),
            )
        }
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
            epochs = self.store.epochs(connection)
            identity = dict(connection.execute("SELECT * FROM identity WHERE id=1").fetchone())
            close_rows = connection.execute(
                "SELECT period,digest FROM period_close WHERE period<=? ORDER BY period",
                (end.ordinal,),
            ).fetchall()
            closes = {r["period"] for r in close_rows}
            problems = []
            references = _report_references(connection, end, source)
            facts = self._report_profiles(connection, references, end, reads)
            if carry_forward_fact_id is not None:
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
            needed_references = {
                row["id"]
                for row in connection.execute(
                    "SELECT f.id,s.kind,f.period FROM json_each(?) j "
                    "JOIN fact_revision f ON f.id=j.value JOIN subject s ON s.id=f.subject_id "
                    "WHERE (s.kind='report_income_tax_confirmation' AND f.period>=? "
                    "AND f.period<=?) OR (s.kind='report_carry_forward' AND f.period=?)",
                    (
                        canonical(sorted(references)),
                        year_start.ordinal,
                        end.ordinal,
                        book_start.ordinal,
                    ),
                )
                if carry_forward_fact_id is None or row["kind"] != ReportCarryForward.kind
            }
            facts.extend(reads.fact_versions(sorted(needed_references)).values())
            if book_start > end:
                problems.append(issue("bookkeeping_start", "报表期间早于明确建账月"))
            if source == "closed":
                problems.extend(_closed_period_issues(closes, book_start, year_start, end))
            unknown_accounts = set(_account_totals(connection, end.ordinal, source, ())) - (
                _POSITION_ACCOUNTS
            )
            historical_accounts = set(RECLASS) | unknown_accounts
            candidates = {
                row[0]
                for row in connection.execute(
                    "SELECT id FROM voucher_version WHERE period>=? AND period<=? "
                    "UNION SELECT l.version_id FROM voucher_line l INDEXED BY voucher_line_account "
                    "JOIN voucher_version v ON v.id=l.version_id "
                    "WHERE l.account IN (SELECT value FROM json_each(?)) AND v.period<?",
                    (
                        year_start.ordinal,
                        end.ordinal,
                        canonical(sorted(historical_accounts)),
                        year_start.ordinal,
                    ),
                )
            }
            selected_sql, selected_params = _report_vouchers(
                end.ordinal, source, voucher_ids=candidates
            )
            # Direct balances use synchronous totals. Exact parties and this
            # posting year's flows retain their immutable classification sources.
            sql = (
                "WITH selected AS ("
                + selected_sql
                + """
                ) SELECT v.id version_id,v.period,v.basis_calculation_id calculation_id,
                  v.reverses_id,v.close_period,
                  l.line_no,l.account,l.debit,l.credit,l.cashflow,c.kind,c.fact_id,
                  c.period calculation_period,c.subject_id calculation_subject_id
                FROM selected v JOIN voucher_line l ON l.version_id=v.id
                JOIN calculation c ON c.id=v.basis_calculation_id WHERE
                  (v.period>=? AND l.account IN (SELECT value FROM json_each(?))) OR
                  l.account IN (SELECT value FROM json_each(?)) OR
                  l.account NOT IN (SELECT value FROM json_each(?))"""
            )
            params = [
                *selected_params,
                year_start.ordinal,
                canonical(sorted(CASH_ACCOUNTS | set(PROFIT_ACCOUNTS))),
                canonical(sorted(RECLASS)),
                canonical(sorted(_POSITION_ACCOUNTS)),
            ]
            rows = [dict(r) for r in connection.execute(sql, params)]
            from .read_indexes import verify_close_references

            selected_references = connection.execute(
                "SELECT r.* FROM close_reference r WHERE r.reference_type='voucher' "
                "AND r.reference_id IN (SELECT value FROM json_each(?)) "
                "AND r.close_period<=?",
                (canonical(sorted({row["version_id"] for row in rows})), end.ordinal),
            ).fetchall()
            verify_close_references(connection, selected_references)
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
            classifications = _report_classifications(
                connection, reads, references, source_vouchers, end, source, problems
            )
            calculations, actual_facts, relation_records = {}, {}, {}
            originals = {
                ident: row["calculation_id"]
                for ident, row in reads.vouchers(
                    {row["reverses_id"] for row in rows if row["reverses_id"]}
                ).items()
            }
            reads.prime_calculations(
                {row["calculation_id"] for row in rows} | set(originals.values())
            )

            def calculation(ident):
                if ident not in calculations:
                    record = reads.calculation(ident)
                    calculations[ident] = record | {
                        "decoded": record["outcome"],
                        "period": YearMonth(record["period"]).ordinal,
                    }
                return calculations[ident]

            def source_fact(ident):
                if ident not in actual_facts:
                    record = calculation(ident)
                    values = dict(record["decoded"]["values"])
                    if record["kind"] in {"funding", "cash_funding", "platform_funding"}:
                        values["funding_kind"] = record["fact_data"]["funding_kind"]
                    elif record["kind"] == "income_tax_assessment":
                        values["tax_year"] = record["fact_data"]["year"]
                    actual_facts[ident] = SimpleNamespace(**values)
                return actual_facts[ident]

            def relations(ident):
                if ident not in relation_records:
                    relation_records[ident] = reads.relations(ident)
                    problems.extend(relation_records[ident]["issues"])
                return relation_records[ident]

            for row in rows:
                row["amount"] = row["debit"] - row["credit"]
                source_id = row["calculation_id"]
                if row["reverses_id"]:
                    if row["reverses_id"] not in originals:
                        originals[row["reverses_id"]] = connection.execute(
                            "SELECT calculation_id FROM voucher_version WHERE id=?",
                            (row["reverses_id"],),
                        ).fetchone()[0]
                    source_id = originals[row["reverses_id"]]
                row["values"] = calculation(source_id)["decoded"]["values"]
                row["kind"] = calculation(source_id)["kind"]
                row["fact"] = source_fact(source_id)
                key = row["reverses_id"] or row["version_id"]
                row["classification"] = classifications.get(key)
                row["cash_source"] = row["fact"]
                resolution = relations(source_id)
                related = [
                    relation
                    for relation in resolution["line_relations"]
                    if relation["line_no"] == row["line_no"] and relation["state"] == "resolved"
                ]
                funds = next(
                    (relation for relation in related if relation["role"] == "funds"), None
                )
                if funds is not None:
                    row["cash_source"] = source_fact(funds["source_calculation_id"])
                    row["cash_obligation"] = next(
                        item
                        for item in resolution["obligations"]
                        if item["source_calculation_id"] == funds["source_calculation_id"]
                        and item["key"] == funds["obligation_key"]
                    )
                if row["account"] in RECLASS:
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
                    connection, cutoff, source, opening_rows, unknown_opening_period
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
                    from .read_indexes import verify_close_references

                    frozen = connection.execute(
                        "SELECT * FROM close_reference WHERE reference_type='calculation' "
                        "AND reference_id=? AND close_period<=?",
                        (tax["id"], month.ordinal),
                    ).fetchall()
                    verify_close_references(connection, frozen)
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
                    "SELECT p.subject_id FROM pending p JOIN fact_current a ON "
                    "a.subject_id=p.subject_id JOIN fact_revision f ON f.id=a.fact_id "
                    "WHERE f.period<=? LIMIT 1",
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
        plan = self.report(
            year, quarter, source="closed", carry_forward_fact_id=carry_forward_fact_id
        )
        if plan["fact_issues"]:
            raise KernelError(
                "needs_information", "季度三表尚不具备导出条件", fact_issues=plan["fact_issues"]
            )
        return plan

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
        plan = self.preview_export(year, quarter, carry_forward_fact_id=carry_forward_fact_id)
        if plan["digest"] != preview_digest or any(
            plan["epochs"].get(x) != epochs.get(x) for x in ("accounting", "material", "management")
        ):
            raise KernelError("preview_expired", "报表导出预览已过期")

        def operation(connection):
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
            fact_counts = dict(
                connection.execute(
                    "SELECT s.kind,count(*) FROM json_each(?) ids "
                    "JOIN fact_revision f ON f.id=ids.value JOIN subject s ON s.id=f.subject_id "
                    "GROUP BY s.kind",
                    (canonical(plan["report_fact_ids"]),),
                )
            )
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
            "classification_count": fact_counts.get(ReportClassification.kind, 0),
            "income_tax_confirmation_count": fact_counts.get(ReportIncomeTaxConfirmation.kind, 0),
            "issues": issues,
            "close_state": "open"
            if any(item["field"] in {"period", "closed_periods"} for item in closed["fact_issues"])
            else "closed",
        }

    def browser_job_results(self, jobs):
        """Expose a download only after the same checks used for file delivery."""
        result = []
        for job in jobs:
            item = {
                **job,
                "download_available": False,
                "download_file_name": None,
                "delivery_status": "pending"
                if job["status"] in {"pending", "running"}
                else "unavailable",
                "delivery_message": None,
            }
            if job["kind"] == "report_export":
                try:
                    with self.store.connection(read_only=True) as connection:
                        row = connection.execute(
                            "SELECT payload FROM jobs WHERE id=?", (job["id"],)
                        ).fetchone()
                        payload = json.loads(row[0])
                        plan = payload["plan"]
                        carries = [
                            ident
                            for ident in plan["report_fact_ids"]
                            if self.store.fact(connection, ident).fact.kind
                            == ReportCarryForward.kind
                        ]
                    item["report_source"] = {
                        "year": plan["period"]["year"],
                        "quarter": plan["period"]["quarter"],
                        "carry_forward_fact_id": carries[0] if len(carries) == 1 else None,
                    }
                except (ValueError, TypeError, KeyError):
                    item.update(
                        delivery_status="invalid",
                        delivery_message="任务来源信息无法验证，请重新生成报表。",
                    )
                    result.append(item)
                    continue
            if job["kind"] == "report_export" and job["status"] == "succeeded":
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


def _statements(rows, start, year_start, end, problems, *, account_balances=None):
    def balance(as_of):
        if account_balances is not None:
            totals = account_balances[as_of]
            if totals is None:
                return {line: None for line in range(1, 54)}
            selected = [
                row
                for row in rows
                if row["period"] <= as_of
                and (row["account"] in RECLASS or row["account"] not in _POSITION_ACCOUNTS)
            ]
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

    def profit(begin):
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
                and (
                    kind == "loan_interest" or getattr(fact, "income_kind", None) == "bank_interest"
                )
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
                        row["values"]["education_tax_fen"]
                        + row["values"]["local_education_tax_fen"]
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

    def cash(begin):
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
                        (1 if row["amount"] > 0 else -1)
                        * (1 if item.category in INFLOW_ROWS else -1)
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
                None
                if totals is None
                else sum_fen(totals.get(account, 0) for account in CASH_ACCOUNTS)
            )
        result[22] = None if result[21] is None else result[21] + result[20]
        return result

    balance_begin, balance_end = balance(year_start - 1), balance(end)
    current_profit, ytd_profit = profit(start), profit(year_start)
    current_cash, ytd_cash = cash(start), cash(year_start)
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
                    "UPDATE jobs SET status='running',attempts=attempts+1,last_error=NULL "
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
                status, error = "succeeded", None
            except Exception as exc:
                result, status, error = None, "failed", f"{type(exc).__name__}: {exc}"[:500]
            with engine.store.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE jobs SET status=?,last_error=?,result=? WHERE id=?",
                    (status, error, canonical(result) if result else None, row["id"]),
                )
                connection.commit()
            results.append(
                {"job_id": row["id"], "status": status, "result": result, "error": error}
            )
    return results
