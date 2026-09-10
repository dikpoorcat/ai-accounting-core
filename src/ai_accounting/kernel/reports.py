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
from .schema import table_name
from .types import NonNegativeFen, PositiveFen, YearMonth, canonical, digest, sum_fen


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


REPORT_KINDS = (ReportProfile.kind, ReportClassification.kind, ReportIncomeTaxConfirmation.kind)


def required_reads(period):
    following = YearMonth.from_ordinal(period.ordinal + 1) if period.ordinal < 119987 else None
    return (
        Read("fact", ReportProfile.kind, "*", following),
        Read("fact", ReportClassification.kind, str(period)),
        Read("fact", ReportIncomeTaxConfirmation.kind, str(period)),
    )


def freeze_report_references(period, context):
    # Freeze presence and absence; filing after close is not a circular close gate.
    for read in required_reads(period):
        context.select(read)
    return []


def register(registry):
    for model in (ReportProfile, ReportClassification, ReportIncomeTaxConfirmation):
        registry.register(model)
    registry.register_readiness("financial_reports", required_reads, freeze_report_references)
    registry.register_snapshot_readiness("financial_reports", check_report_readiness)


def check_report_readiness(store, connection, period):
    """Query adapter uses the caller's one read-only snapshot; calculators stay pure."""
    profiles = store.select(connection, required_reads(period)[0])
    if not profiles:
        return []
    adapter = Reports(SimpleNamespace(store=store))
    return adapter._report(
        int(period[:4]),
        (int(period[5:]) - 1) // 3 + 1,
        source="open",
        connection=connection,
        through_period=period,
    )["fact_issues"]


# Exact codes emitted by typed business modules. Unknown balances are not guessed.
CASH_ACCOUNTS = {"1001", "1002", "1012"}
PROFIT_ACCOUNTS = {
    "5001": (1, -1),
    "5401": (2, 1),
    "540101": (2, 1),
    "540104": (2, 1),
    "540102": (2, 1),
    "540103": (2, 1),
    "5403": (3, 1),
    "5601": (11, 1),
    "560101": (11, 1),
    "560102": (11, 1),
    "560103": (11, 1),
    "560104": (11, 1),
    "5602": (14, 1),
    "560201": (14, 1),
    "560202": (14, 1),
    "560203": (14, 1),
    "560204": (14, 1),
    "5603": (18, 1),
    "560301": (18, 1),
    "6301": (22, -1),
    "630101": (22, -1),
    "571101": (24, 1),
    "571102": (24, 1),
    "5801": (31, 1),
}
DEBIT_BALANCE = {
    "1101": 2,
    "1121": 3,
    "1131": 6,
    "1132": 7,
    "1403": 10,
    "1405": 12,
    "1411": 13,
    "4301": 9,
    "1501": 16,
    "1511": 17,
    "1601": 18,
    "1604": 21,
    "1605": 22,
    "1606": 23,
    "1621": 24,
    "1701": 25,
    "1801": 27,
    "189901": 28,
}
CREDIT_BALANCE = {
    "1602": 19,
    "1702": 25,
    "2001": 31,
    "2201": 32,
    "221101": 35,
    "221102": 35,
    "221103": 35,
    "2231": 37,
    "2232": 38,
    "2501": 42,
    "2701": 43,
    "2401": 44,
    "3001": 48,
    "3002": 49,
    "3101": 50,
    "3103": 51,
    "3104": 51,
}
TAX_ACCOUNTS = {"222101", "222102", "222103", "222104", "222105", "222106"}
RECLASS = {
    "1122": (4, 34),
    "1123": (5, 33),
    "1221": (8, 39),
    "122101": (8, 39),
    "122105": (8, 39),
    "2202": (5, 33),
    "2203": (4, 34),
    "2241": (8, 39),
    "224101": (8, 39),
    "224102": (8, 39),
    "224103": (8, 39),
    "224104": (8, 39),
    "224105": (8, 39),
}
DETAIL_LINES = {
    "management_startup": 15,
    "management_entertainment": 16,
    "management_research": 17,
    "sales_merchandise_repair": 12,
    "sales_advertising_promotion": 13,
    "finance_interest": 19,
}
CASH_CATEGORIES = {
    "customer_receipts": 1,
    "other_operating_receipts": 2,
    "pass_through_receipts": 2,
    "tax_refunds": 2,
    "pass_through_payments": 6,
    "pass_through_refund": 6,
    "asset_acquisition": 12,
    "asset_disposal": 10,
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


class Reports:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    def report(self, year: int, quarter: int, *, source: Literal["open", "closed"] = "open"):
        return self._report(year, quarter, source=source)

    def _report(self, year, quarter, *, source, connection=None, through_period=None):
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
            epochs = self.store.epochs(connection)
            identity = dict(connection.execute("SELECT * FROM identity WHERE id=1").fetchone())
            close_rows = connection.execute(
                "SELECT * FROM period_close WHERE period<=? ORDER BY period", (end.ordinal,)
            ).fetchall()
            closes = {r["period"]: json.loads(r["manifest"]) for r in close_rows}
            problems = []
            if source == "closed" and end.ordinal not in closes:
                problems.append(issue("period", "季度末尚未关账"))
            references = set()
            for manifest in closes.values():
                references.update(
                    manifest.get("readiness", {}).get("financial_reports", {}).get("facts", ())
                )
            if source == "open":
                for kind in REPORT_KINDS:
                    references.update(
                        r[0]
                        for r in connection.execute(
                            "SELECT c.fact_id FROM fact_current c JOIN fact_revision f ON "
                            "f.id=c.fact_id "
                            "JOIN subject s ON s.id=f.subject_id WHERE s.kind=? AND f.period<=? "
                            "AND NOT EXISTS(SELECT 1 FROM period_close p WHERE p.period=f.period)",
                            (kind, end.ordinal),
                        )
                    )
            facts = [self.store.fact(connection, ident) for ident in sorted(references)]
            profiles = [
                f for f in facts if f.fact.kind == ReportProfile.kind and f.fact.period <= end
            ]
            profile = None
            if profiles:
                latest = max(f.fact.period for f in profiles)
                applicable = {f.id: f for f in profiles if f.fact.period == latest}
                if len(applicable) == 1:
                    profile = next(iter(applicable.values()))
            if profile is None:
                problems.append(issue("report_profile", "需要唯一的报表口径及新设企业零期初确认"))
            book_start = profile.fact.bookkeeping_start if profile else year_start
            if book_start > end:
                problems.append(issue("bookkeeping_start", "报表期间早于明确建账月"))
            for ordinal in range(max(book_start.ordinal, year_start.ordinal), end.ordinal + 1):
                if source == "closed" and ordinal not in closes:
                    problems.append(
                        issue(
                            "closed_periods",
                            "年初或建账月起须逐月关账",
                            period=str(YearMonth.from_ordinal(ordinal)),
                        )
                    )
            # JSON table values select frozen IDs without a million-parameter IN list.
            sql = """SELECT v.id version_id,v.period,v.calculation_id,v.reverses_id,
                  l.line_no,l.account,l.debit,l.credit,l.cashflow,c.kind,c.fact_id,c.outcome,
                  c.period calculation_period
                FROM period_close p,json_each(p.manifest,'$.vouchers') j
                JOIN voucher_version v ON v.id=json_extract(j.value,'$.id')
                JOIN voucher_line l ON l.version_id=v.id JOIN calculation c ON c.id=v.calculation_id
                WHERE p.period<=?"""
            params = [end.ordinal]
            if source == "open":
                sql += """ UNION ALL SELECT v.id,v.period,v.calculation_id,v.reverses_id,
                  l.line_no,l.account,l.debit,l.credit,l.cashflow,c.kind,c.fact_id,c.outcome,c.period
                  FROM voucher_current a JOIN voucher_version v ON v.id=a.version_id
                  JOIN voucher_line l ON l.version_id=v.id
                  JOIN calculation c ON c.id=v.calculation_id
                  WHERE v.period<=? AND NOT EXISTS(
                    SELECT 1 FROM period_close p WHERE p.period=v.period)"""
                params.append(end.ordinal)
            rows = [dict(r) for r in connection.execute(sql, params)]
            if rows and min(r["period"] for r in rows) < book_start.ordinal:
                problems.append(
                    issue("bookkeeping_start", "已存在建账月之前的账；不能声明该月为新设企业零期初")
                )
            classifications = {}
            for version in facts:
                fact = version.fact
                if fact.kind != ReportClassification.kind:
                    continue
                key = fact.voucher_version_id
                if key in classifications:
                    problems.append(
                        issue(
                            "report_classification",
                            "同一凭证版本存在多个分类来源",
                            voucher_version_id=key,
                        )
                    )
                classifications[key] = fact
            calculations, actual_facts, parent_ids = {}, {}, {}
            for row in rows:
                if row["calculation_id"] not in calculations:
                    calculations[row["calculation_id"]] = {
                        "id": row["calculation_id"],
                        "kind": row["kind"],
                        "period": row["calculation_period"],
                        "fact_id": row["fact_id"],
                        "decoded": json.loads(row["outcome"]),
                    }
                del row["outcome"]

            def calculation(ident):
                if ident not in calculations:
                    record = connection.execute(
                        "SELECT * FROM calculation WHERE id=?", (ident,)
                    ).fetchone()
                    if not record:
                        raise KernelError("missing_report_source", "报表引用的不可变核算不存在")
                    calculations[ident] = dict(record) | {"decoded": json.loads(record["outcome"])}
                return calculations[ident]

            def source_fact(ident):
                if ident not in actual_facts:
                    record = calculation(ident)
                    values = dict(record["decoded"]["values"])
                    if record["kind"] in {"funding", "cash_funding"}:
                        values["funding_kind"] = connection.execute(
                            f"SELECT funding_kind FROM {table_name(record['kind'])} "
                            "WHERE revision_id=?",
                            (record["fact_id"],),
                        ).fetchone()[0]
                    elif record["kind"] == "income_tax_assessment":
                        values["tax_year"] = connection.execute(
                            "SELECT year FROM fact_income_tax_assessment WHERE revision_id=?",
                            (record["fact_id"],),
                        ).fetchone()[0]
                    actual_facts[ident] = SimpleNamespace(**values)
                return actual_facts[ident]

            def parents(ident):
                if ident not in parent_ids:
                    parent_ids[ident] = [
                        r[0]
                        for r in connection.execute(
                            "SELECT upstream_id FROM dependency_calculation WHERE calculation_id=?",
                            (ident,),
                        )
                    ]
                return parent_ids[ident]

            def party_candidates(ident, account, seen=None):
                seen = set() if seen is None else seen
                if ident in seen:
                    return set()
                seen.add(ident)
                own = {
                    x.get("counterparty_id")
                    for x in calculation(ident)["decoded"]["values"].get("obligations", ())
                    if x["account"] == account
                }
                if own:
                    return own
                result = set()
                for parent in parents(ident):
                    result.update(party_candidates(parent, account, seen))
                return result

            originals = {}
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
                if row["account"] in RECLASS:
                    choices = party_candidates(source_id, row["account"])
                    explicit = (
                        [
                            x.counterparty_id
                            for x in row["classification"].counterparties
                            if x.line_no == row["line_no"]
                        ]
                        if row["classification"]
                        else []
                    )
                    if explicit:
                        row["party"] = explicit[0]
                        if len(choices) == 1 and row["party"] not in choices:
                            problems.append(
                                issue(
                                    "counterparty_id",
                                    "分类与已确认交易方不一致",
                                    voucher_version_id=key,
                                    line_no=row["line_no"],
                                )
                            )
                    elif len(choices) == 1:
                        row["party"] = next(iter(choices))
                    else:
                        row["party"] = ("unresolved", row["version_id"], row["line_no"])
                        problems.append(
                            issue(
                                "report_classification.counterparty_id",
                                "往来明细无法唯一归属交易方",
                                voucher_version_id=key,
                                line_no=row["line_no"],
                            )
                        )
                row["cash_source"] = row["fact"]
                if (
                    row["values"].get("settlements")
                    and row["line_no"] <= len(row["values"]["settlements"]) * 2
                ):
                    settlement = row["values"]["settlements"][(row["line_no"] - 1) // 2]
                    parent = calculation(settlement["source_calculation"])
                    row["cash_source"] = source_fact(parent["id"])
                    row["cash_obligation"] = next(
                        (
                            o
                            for o in parent["decoded"]["values"].get("obligations", ())
                            if o["key"] == settlement["obligation"]
                        ),
                        {},
                    )
            classified_rows = defaultdict(list)
            known_accounts = (
                CASH_ACCOUNTS
                | set(PROFIT_ACCOUNTS)
                | set(DEBIT_BALANCE)
                | set(CREDIT_BALANCE)
                | TAX_ACCOUNTS
                | set(RECLASS)
            )
            for row in rows:
                classified_rows[row["reverses_id"] or row["version_id"]].append(row)
                if row["account"] not in known_accounts:
                    problems.append(
                        issue("account_mapping", "非零凭证行尚未映射到三表", account=row["account"])
                    )
            for key, fact in classifications.items():
                if key not in classified_rows:
                    header = connection.execute(
                        "SELECT period FROM voucher_version WHERE id=?", (key,)
                    ).fetchone()
                    if header is None or header[0] != fact.period.ordinal:
                        problems.append(
                            issue(
                                "report_classification.voucher_version_id",
                                "分类必须引用本月已经存在的凭证版本",
                                voucher_version_id=key,
                            )
                        )
                    continue
                related = classified_rows[key]
                line_map = {r["line_no"]: r for r in related}
                if fact.period.ordinal not in {
                    r["period"] for r in related if r["reverses_id"] is None
                }:
                    problems.append(
                        issue(
                            "report_classification.period",
                            "分类月份必须等于原凭证记账月份",
                            voucher_version_id=key,
                        )
                    )
                for detail in (*fact.profit_details, *fact.counterparties, *fact.cash_details):
                    line = line_map.get(detail.line_no)
                    if line is None:
                        problems.append(
                            issue(
                                "report_classification.line_no",
                                "分类引用不存在的凭证行",
                                voucher_version_id=key,
                            )
                        )
                    elif (
                        (
                            isinstance(detail, ProfitDetail)
                            and line["account"] not in {"5403", "5601", "5602", "5603"}
                        )
                        or (
                            isinstance(detail, CounterpartyDetail)
                            and line["account"] not in RECLASS
                        )
                        or (isinstance(detail, CashDetail) and line["account"] not in CASH_ACCOUNTS)
                    ):
                        problems.append(
                            issue(
                                "report_classification.line_no",
                                "该凭证行不接受此类报表分类",
                                voucher_version_id=key,
                            )
                        )
            statements = _statements(rows, start.ordinal, year_start.ordinal, end.ordinal, problems)
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
                    ids = {
                        c
                        for m, manifest in closes.items()
                        if m <= month.ordinal
                        for c in manifest.get("calculations", ())
                    }
                    active = connection.execute(
                        "SELECT 1 FROM calculation_current WHERE calculation_id=?", (tax["id"],)
                    ).fetchone()
                    if (
                        tax["kind"] != "income_tax_assessment"
                        or tax["period"] != month.ordinal
                        or tax["decoded"]["values"]["cumulative_assessed_fen"]
                        != confirmed.cumulative_assessed_fen
                        or (source == "closed" and tax["id"] not in ids)
                        or (source == "open" and tax["id"] not in ids and not active)
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
                abs(value) > TEMPLATE_MAX_FEN
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

    def preview_export(self, year: int, quarter: int):
        plan = self.report(year, quarter, source="closed")
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
    ):
        directory = str(Path(output_directory).resolve())
        hashed = digest(["report_export", year, quarter, preview_digest, epochs, directory])
        cached = self.engine._cached(request_id, hashed)
        if cached is not None:
            return cached
        plan = self.preview_export(year, quarter)
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


def _statements(rows, start, year_start, end, problems):
    def balance(as_of):
        result = {line: 0 for line in BALANCE_NAMES}
        accounts, parties = defaultdict(int), defaultdict(int)
        for row in rows:
            if row["period"] > as_of:
                continue
            account, amount = row["account"], row["amount"]
            if account in RECLASS:
                parties[(account, row["party"])] += amount
            else:
                accounts[account] += amount
        for (account, _party), amount in parties.items():
            result[RECLASS[account][0 if amount >= 0 else 1]] += abs(amount)
        for account, amount in accounts.items():
            if account in CASH_ACCOUNTS:
                result[1] += amount
            elif account in PROFIT_ACCOUNTS:
                result[51] -= amount
            elif account in DEBIT_BALANCE:
                result[DEBIT_BALANCE[account]] += amount
            elif account in CREDIT_BALANCE:
                result[CREDIT_BALANCE[account]] += amount if account == "1702" else -amount
            elif account in TAX_ACCOUNTS:
                result[14 if amount > 0 else 36] += abs(amount)
            elif amount:
                problems.append(
                    issue("account_mapping", "存在未映射的非零账户余额", account=account)
                )
        result[9] += sum(result[x] for x in (10, 11, 12, 13))
        result[20] = result[18] - result[19]
        result[15] = sum(result[x] for x in range(1, 10)) + result[14]
        result[29] = sum(result[x] for x in (16, 17, 20, 21, 22, 23, 24, 25, 26, 27, 28))
        result[30] = result[15] + result[29]
        result[41] = sum(result[x] for x in range(31, 41))
        result[46] = sum(result[x] for x in range(42, 46))
        result[47] = result[41] + result[46]
        result[52] = sum(result[x] for x in range(48, 52))
        result[53] = result[47] + result[52]
        return result

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
            if not begin <= row["period"] <= end or row["account"] not in CASH_ACCOUNTS:
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
            if row["kind"] in {"funds_transfer", "cash_bank_transfer"}:
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
        result[21] = sum_fen(
            r["amount"] for r in rows if r["account"] in CASH_ACCOUNTS and r["period"] < begin
        )
        result[22] = result[21] + result[20]
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
    result = [
        {"code": f"balance_{column}", "passed": balance["30"][column] == balance["53"][column]}
        for column in ("ending_fen", "beginning_fen")
    ]
    for column in ("current_fen", "year_to_date_fen"):
        result.extend(
            [
                {
                    "code": f"profit_{column}",
                    "passed": profit["32"][column] == profit["30"][column] - profit["31"][column],
                },
                {
                    "code": f"cash_{column}",
                    "passed": cash["20"][column]
                    == cash["7"][column] + cash["13"][column] + cash["19"][column],
                },
                {
                    "code": f"cash_ending_{column}",
                    "passed": cash["22"][column] == balance["1"]["ending_fen"],
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
                    "('pending','running','failed') " + exclude + " ORDER BY attempts,id LIMIT 1",
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
