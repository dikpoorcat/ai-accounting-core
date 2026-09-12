"""Read-only presentation of the local journal for the existing five-page dashboard.

Journal amounts are selected by posting period. Closed periods use sealed versions;
an open-period reversal reads the original calculation, never the replacement's values.
The browser receives summaries of the complete snapshot and bounded detail pages.
"""

from __future__ import annotations

import calendar
import hashlib
import json
from collections import defaultdict
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import cached_property
from typing import Literal

from .business_queries import BusinessQueries
from .contracts import KernelError
from .dashboard_pages import (
    decode_cursor,
    preparation_view,
    seal_collections,
    seal_page,
    validate_page,
)
from .dashboard_reads import (
    Calculations,
    CalculationView,
    ClosedPeriods,
    FrozenFacts,
    Journal,
    account_totals,
    metric_rows,
    page_keys,
    scalar_facts,
)
from .provenance import recorded_times
from .query_reads import QueryReads
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
from .reports import Reports, _workbook_name
from .types import YearMonth, digest

KIND_NAMES = {
    "external_completion": "外部办理完成依据",
    "managed_reserve_scope": "备用金核算范围确认",
    "payroll_disbursement_basis": "工资代发金额依据",
    "platform_boundary_disposition": "平台原交易范围处置",
    "service_sale": "服务收入",
    "expense": "费用",
    "expense_recovery": "费用退回确认",
    "reimbursement_acceptance": "已付负债报销承接",
    "project_cost": "项目阶段成本",
    "project_release": "项目成本转费用",
    "pass_through": "代收代付",
    "advance": "预收预付款",
    "asset_advance": "资产预付款",
    "funding": "股东投入或借款",
    "payment": "实际收付款",
    "cash_payment": "现金收付款",
    "cash_funding": "现金投入或借款",
    "cash_bank_transfer": "现金存取",
    "platform_payment": "支付平台收付款",
    "platform_funding": "支付平台投入或借款",
    "bank_platform_transfer": "银行与支付平台转款",
    "platform_movement": "支付平台原始资金记录",
    "platform_expense_confirmation": "平台管理资金费用确认",
    "managed_reserve_bank_expense": "银行转入备用金费用确认",
    "managed_reserve_obligation_settlement": "备用金结清原应付款",
    "payroll_reserve_payment": "毛额工资支付及返池费用确认",
    "settlement": "非现金核销",
    "sale_return": "销售退回",
    "funds_transfer": "资金调拨",
    "bank_income": "其他收入",
    "refundable_deposit": "可退保证金",
    "reimbursed_deposit": "垫付押金确认",
    "overpayment": "超付追收",
    "employee_advance": "个人垫付及债务转移",
    "pass_through_return": "代收款退回",
    "asset": "资产购置",
    "reimbursed_asset": "报销形成的资产",
    "reimbursed_asset_batch": "整批资产验收",
    "asset_activation": "资产启用",
    "asset_consumption": "折旧与摊销",
    "asset_disposal": "资产处置",
    "loan_drawdown": "借款到账",
    "loan_interest": "借款利息计提",
    "tax_assessment": "增值税及附加税费确认",
    "income_tax_assessment": "企业所得税确认",
    "tax_credit_confirmation": "税额抵减与退税确认",
    "payroll": "工资计提",
    "payroll_bounded": "工资计提",
    "annual_bonus": "全年一次性奖金",
    "labor": "个人劳务计提",
    "labor_accrual": "未支付个人劳务计提",
    "labor_project_cost": "资产项目劳务成本",
    "bank_statement": "银行流水",
    "bank_opening": "银行账面起点",
    "bank_reconciliation": "银行对账",
    "service_tax_point": "服务收入增值税确认",
    "advance_fulfillment": "预收款履约确认",
    "advance_refund": "预收预付款退回",
    "money_fund_subscription": "货币基金申购确认",
    "money_fund_redemption": "货币基金赎回确认",
    "opening_asset": "期初资产卡片",
    "opening_package": "期初接续总清单",
    "opening_bank": "银行存款期初",
    "opening_cash": "库存现金期初",
    "opening_platform": "支付平台期初",
    "opening_obligation": "往来明细期初",
    "opening_money_fund": "期初货币基金成本",
    "opening_loan": "借款本金及利息期初",
    "opening_tax": "税费明细期初",
    "opening_payroll_payable": "薪酬未付明细期初",
    "opening_payroll_state": "人员薪酬累计接续",
    "opening_equity": "权益明细期初",
}
GROUPS = {
    "income_customer": "收入与客户",
    "expense_supplier": "费用与供应商",
    "employee_reimbursement": "员工报销",
    "payroll": "工资与社保",
    "labor": "个人劳务",
    "tax": "税费事项",
    "assets": "长期资产",
    "financing_owner": "融资与股东",
    "fund_movement": "资金调拨与保证金",
    "correction": "更正与冲正",
    "other": "其他业务",
}
ACCOUNT_NAMES = {
    "1001": "库存现金",
    "1002": "银行存款",
    "1012": "其他货币资金",
    "1101": "短期投资",
    "1122": "应收账款",
    "1123": "预付账款",
    "1221": "其他应收款",
    "1601": "固定资产",
    "1602": "累计折旧",
    "1604": "在建工程",
    "1701": "无形资产",
    "1702": "累计摊销",
    "189901": "待启用无形资产",
    "2001": "短期借款",
    "2202": "应付账款",
    "2203": "预收账款",
    "221101": "应付工资",
    "221102": "应付单位社保",
    "221103": "应付单位公积金",
    "2241": "其他应付款",
    "224101": "应付报销款",
    "224102": "代扣个人社保",
    "224103": "代扣个人公积金",
    "224104": "应付劳务报酬",
    "222103": "应付个人所得税",
    "3001": "实收资本",
    "4301": "项目成本",
    "5001": "主营业务收入",
    "5401": "主营业务成本",
    "5601": "销售费用",
    "5602": "管理费用",
    "5603": "财务费用",
    "5801": "所得税费用",
    "122101": "其他应收款明细",
    "122105": "应收代收款",
    "224105": "应付代付款",
    "222101": "应交增值税",
    "222102": "应交附加税费",
    "222104": "待转销项税额",
    "222105": "应交税费明细",
    "222106": "应交企业所得税",
    "5403": "税金及附加",
    "5111": "投资收益",
    "6301": "营业外收入",
    "630101": "资产处置收益",
    "571101": "固定资产处置损失",
    "571102": "无形资产处置损失",
    "571103": "税收滞纳金",
    "571104": "社保缴费滞纳金",
    "560301": "利息费用",
}
for _prefix, _label in (("5401", "主营业务成本"), ("5601", "销售费用"), ("5602", "管理费用")):
    for _suffix, _detail in (("01", "职工薪酬"), ("02", "折旧"), ("03", "摊销"), ("04", "劳务")):
        ACCOUNT_NAMES[_prefix + _suffix] = _label + "—" + _detail
PAYROLL_KINDS = {"payroll", "payroll_bounded", "annual_bonus"}
LABOR_KINDS = {"labor", "labor_accrual"}
ASSET_KINDS = {"asset", "reimbursed_asset", "opening_asset"}
FUND_TYPES = {"bank": "bank", "cash": "cash", "platform": "payment_platform"}


def _name(kind):
    return KIND_NAMES.get(kind, "其他业务")


def _group(kind, reversal=False):
    if reversal:
        return "correction"
    if kind in PAYROLL_KINDS or kind.startswith("payroll_"):
        return "payroll"
    if kind in LABOR_KINDS or kind == "labor_project_cost":
        return "labor"
    if "asset" in kind:
        return "assets"
    if "tax" in kind:
        return "tax"
    if "funding" in kind or kind.startswith("loan_"):
        return "financing_owner"
    if kind in {"service_sale", "sale_return", "advance_fulfillment"}:
        return "income_customer"
    if kind in {"employee_advance", "reimbursement_acceptance", "reimbursed_deposit"}:
        return "employee_reimbursement"
    if "expense" in kind or kind.startswith("project_"):
        return "expense_supplier"
    if "payment" in kind or "transfer" in kind or "deposit" in kind or kind == "bank_income":
        return "fund_movement"
    return "other"


def _page(rows, after, limit, *, key="id"):
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("明细页大小必须为 1 至 500")
    selected = [row for row in rows if after is None or row[key] > after]
    items = selected[:limit]
    return items, {
        "has_more": len(selected) > limit,
        "next_cursor": items[-1][key] if len(selected) > limit else None,
        "total_count": len(rows),
    }


def _period_view(period, closed=False, closed_at=None):
    year, month = int(period[:4]), int(period[5:])
    return {
        "key": period,
        "year": year,
        "month": month,
        "label": f"{year} 年 {month} 月",
        "short_label": f"{month} 月",
        "status": "closed" if closed else "open",
        "start_date": f"{period}-01",
        "end_date": f"{period}-{calendar.monthrange(year, month)[1]:02}",
        "closed_at": closed_at,
    }


def _recognition(data, period):
    # Posting has month precision. Only a source's actual day may be displayed as a day.
    for field in (
        "actual_date",
        "acquisition_date",
        "in_use_date",
        "disposal_date",
        "income_date",
        "fulfillment_date",
        "recognition_date",
    ):
        value = data.get(field)
        if isinstance(value, str) and len(value) == 10:
            return {"precision": "day", "period": period, "date": value, "label": value}
    return {"precision": "month", "period": period, "date": None, "label": period}


def _display_sources(profile, **fields):
    return {
        output: profile["field_sources"][field]
        for output, field in fields.items()
        if field in profile["field_sources"]
    }


def _source_list(source):
    return source if isinstance(source, list) else [source] if source else []


class _Snapshot:
    def __init__(self, engine, connection, period):
        from .business_queries import _today_china

        self.engine = engine
        self.store, self.connection, self.period = engine.store, connection, period
        self.month = YearMonth(period).ordinal
        self.as_of = _today_china()
        self.epochs = self.store.epochs(connection)
        self.reads = QueryReads(engine, connection)
        self.queries = BusinessQueries(engine, reads=self.reads)
        self.closes = ClosedPeriods(self)
        self.close = self.closes.get(self.month)
        self.fact_cache, self.calculation_cache = {}, {}
        self.relation_cache, self.evidence_cache = {}, {}
        self.semantic_cache = {}
        self.profile_cache, self.source_metadata = {}, {}
        self.recorded_records = []
        self.journal = Journal(self)
        self.month_journal = Journal(self, month=self.month)
        self.calculations = Calculations(self)
        self.frozen_fact_ids = FrozenFacts(self)
        self.accounts = defaultdict(int, account_totals(self))
        self.month_accounts = defaultdict(
            int,
            {
                row["account"]: row["debit"] - row["credit"]
                for row in connection.execute(
                    "SELECT account,debit,credit FROM monthly_account WHERE period=?", (self.month,)
                )
            },
        )
        from .dashboard_metadata import initialize_metadata

        self.metadata = initialize_metadata(self)
        self.snapshot_version = hashlib.sha256(
            json.dumps(
                {
                    "company": self.store.company_id,
                    "database": self.store.database_id,
                    "period": period,
                    "epochs": self.epochs,
                    "closes": sorted(self.closes),
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()

    @property
    def tax_identities(self):
        return self._tax_identity_records[0]

    @property
    def tax_identity_candidates(self):
        return self._tax_identity_records[1]

    @cached_property
    def commentary(self):
        from .display import Display

        return Display.commentary(self.connection, self.period, registry=self.store.registry)

    @cached_property
    def selected_states(self):
        return self.queries._selected_accounting(
            self.connection, None, self.period, include_vouchers=False
        )["through_period"]

    @cached_property
    def opening_selection(self):
        return self.queries._selected_accounting(
            self.connection,
            None,
            self.period,
            include_vouchers=False,
            kinds={
                kind
                for kind in self.store.registry.models
                if kind.startswith("opening_") or kind == "bank_opening"
            },
        )["through_period"]

    @cached_property
    def openings(self):
        ids = [item["calculation_id"] for item in self.opening_selection["state_results"]]
        metadata = self.reads.metadata(ids)
        return [self.calculation(ident) for ident, record in metadata.items() if record["opening"]]

    def fact(self, ident):
        return self.reads.fact(ident)

    def calculation(self, ident):
        if ident not in self.calculation_cache:
            self.calculation_cache[ident] = CalculationView(
                self, self.reads.metadata((ident,))[ident]
            )
        return self.calculation_cache[ident]

    def calculations_of_kind(self, *kinds):
        return self.calculations.selected(kinds=set(kinds)).values()

    def calculations_for_entity(self, kind, field, ident):
        from .schema import table_name

        if (
            kind not in self.store.registry.models
            or field not in self.store.registry.models[kind].model_fields
        ):
            return ()
        subjects = {
            row[0]
            for row in self.connection.execute(
                f"SELECT DISTINCT f.subject_id FROM {table_name(kind)} d JOIN fact_revision f "
                f"ON f.id=d.revision_id WHERE d.{field}=?",
                (ident,),
            )
        }
        return self.calculations.selected(kinds={kind}, subjects=subjects).values()

    def prepare_settlements(self, subjects):
        if not hasattr(self, "settlement_cache"):
            self.settlement_cache = {}
            self.settlement_current_cache = {}
        missing = set(subjects) - self.settlement_cache.keys()
        if missing:
            result = self.queries.settlement_summary(
                self.connection,
                self.period,
                subject_ids=missing,
            )
            current = self.queries.settlement_summary(
                self.connection,
                self.period,
                subject_ids=missing,
                current=True,
            )
            for subject in missing:
                self.settlement_cache[subject] = result
                self.settlement_current_cache[subject] = current

    @cached_property
    def preparation(self):
        return self.queries._period_readiness(self.connection, self.period, summary=True)

    def query_calculation(self, ident):
        return self.reads.calculation(ident)

    def query_parents(self, ident):
        return self.reads.parents(ident)

    def query_relations(self, calc):
        return self.reads.relations(calc["id"])

    def profile(self, kind, ident):
        key = (kind, ident)
        if key in self.profile_cache:
            return self.profile_cache[key]
        current = self.current_profiles.get(kind, {}).get(ident, {})
        frozen = self.profiles.get(kind, {}).get(ident, {})
        base = frozen or current
        fields = (
            "display_name",
            "display_number",
            "purpose",
            "note",
            "employment_start",
            "employment_end",
            "employment_status",
            "active",
            "category_label",
            "rights_description",
            "useful_life_basis",
            "counterparty_id",
            "beneficiary_id",
            "handler_id",
        )
        result = {key: base[key] for key in ("id", "revision") if key in base}
        result["field_sources"] = {}
        for field in fields:
            value = frozen.get(field)
            missing = value is None or (
                field not in {"employment_start", "employment_end"} and value == ""
            )
            selected = current if missing else frozen
            value = selected.get(field)
            result[field] = value
            if value is not None and value != "":
                basis = (
                    "frozen"
                    if self.close and selected is frozen
                    else "current_supplement"
                    if self.close
                    else "current"
                )
                result["field_sources"][field] = self.source(
                    "display_profile", selected, basis, field=field
                )
        start, end = result.get("employment_start"), result.get("employment_end")
        result["field_conflicts"] = []
        if (
            start
            and end
            and (start[:7] > end[:7] or (len(start) == len(end) == 10 and start > end))
        ):
            result["field_conflicts"].append(
                {
                    "code": "employment_interval_conflict",
                    "fields": ["employment_start", "employment_end"],
                }
            )
        self.profile_cache[key] = result
        return result

    def source(self, source_type, row, basis, *, field=None):
        """Keep the displayed value bound to its exact record, independently of time."""
        key = (source_type, str(row["id"]), basis, field)
        if key not in self.source_metadata:
            evidence = row.get("evidence_digest")
            if isinstance(evidence, bytes):
                evidence = evidence.hex()
            self.source_metadata[key] = {
                "source_type": source_type,
                "id": row["id"],
                "revision": row.get("revision"),
                "field": field,
                "source": row.get("source"),
                "evidence_digest": evidence,
                "evidence": list(row.get("evidence", ())) or ([evidence] if evidence else []),
                "basis": basis,
                "recorded_at": None,
            }
        return self.source_metadata[key]

    def fact_source(self, fact, *, field=None):
        basis = (
            "frozen"
            if self.close and fact["id"] in self.frozen_fact_ids
            else "current_supplement"
            if self.close
            else "current"
        )
        return self.source("fact", fact, basis, field=field)

    def attach_recorded_times(self):
        references = {(key[0], key[1]) for key in self.source_metadata}
        references.update(reference for _, reference in self.recorded_records)
        times = recorded_times(self.connection, references)
        for key, metadata in self.source_metadata.items():
            metadata["recorded_at"] = times.get((key[0], key[1]))
        for record, reference in self.recorded_records:
            record["recorded_at"] = times.get(reference)

    def management_source(self, record, field):
        if field in record.get("field_sources", {}):
            return record["field_sources"][field]
        basis = (
            "frozen"
            if self.close and record["id"] in self.frozen_management_ids
            else "current_supplement"
            if self.close
            else "current"
        )
        return self.source("management", record, basis, field=field)

    def party_details(self, ident):
        if not ident:
            return {"name": "未提供", "source": None}
        for kind in ("employee", "counterparty"):
            for profiles in (self.profiles, self.current_profiles):
                profile = profiles.get(kind, {}).get(ident, {})
                if profile.get("display_name"):
                    return {
                        "name": profile["display_name"],
                        "source": "display_profile",
                        "id": profile.get("id"),
                        "field_sources": {
                            "name": self.source(
                                "display_profile",
                                profile,
                                "frozen"
                                if self.close and profiles is self.profiles
                                else "current_supplement"
                                if self.close
                                else "current",
                                field="display_name",
                            )
                        },
                    }
        if ident in self.payees:
            payee = self.payee_records[ident]
            return {
                "name": payee["name"],
                "source": "payee",
                "id": payee["id"],
                "field_sources": {
                    "name": self.source(
                        "payee", payee, "frozen" if self.close else "current", field="name"
                    )
                },
            }
        if ident in self.current_payees:
            payee = self.current_payees[ident]
            return {
                "name": payee["name"],
                "source": "payee",
                "id": payee["id"],
                "field_sources": {
                    "name": self.source(
                        "payee",
                        payee,
                        "current_supplement" if self.close else "current",
                        field="name",
                    )
                },
            }
        if ident in self.tax_identities:
            candidates = self.tax_identity_candidates[ident]
            names = sorted({fact["data"]["name"] for fact in candidates})
            if len(names) > 1:
                return {
                    "name": "、".join(names) + "（姓名资料待核对）",
                    "source": "tax_import_identity_v2",
                    "conflicting_ids": [fact["id"] for fact in candidates],
                    "field_sources": {
                        "name": [self.fact_source(fact, field="name") for fact in candidates]
                    },
                }
            fact = self.tax_identities[ident]
            return {
                "name": fact["data"]["name"],
                "source": "tax_import_identity_v2",
                "id": fact["id"],
                "field_sources": {"name": self.fact_source(fact, field="name")},
            }
        return {"name": "未提供姓名或名称", "source": None}

    def party_code(self, ident):
        return self.party_code_details(ident)[0]

    def party_code_details(self, ident):
        profile = self.profile("employee", ident)
        if profile.get("display_number"):
            return profile["display_number"], profile["field_sources"]["display_number"]
        codes = {
            fact["data"]["employee_code"] for fact in self.tax_identity_candidates.get(ident, ())
        }
        if len(codes) == 1:
            return next(iter(codes)), [
                self.fact_source(fact, field="employee_code")
                for fact in self.tax_identity_candidates[ident]
            ]
        return None, None

    def party(self, ident):
        return self.party_details(ident)["name"]

    def party_field(self, ident, field="party", *, missing=None):
        details = self.party_details(ident)
        source = details.get("field_sources", {}).get("name")
        return {
            field: missing if not ident and missing is not None else details["name"],
            "field_sources": {field: source} if source else {},
        }

    def by_kind(self, *kinds):
        return list(self.reads.facts(self.fact_ids_of_kind(*kinds)).values())

    def fact_ids_of_kind(self, *kinds, period=None):
        parameters = [json.dumps(kinds), self.month, self.month, self.month, self.month]
        period_clause = " AND f.period=?" if period is not None else ""
        if period is not None:
            parameters.append(YearMonth(period).ordinal)
        rows = self.connection.execute(
            "WITH candidates AS (SELECT f.id,f.subject_id,f.revision FROM fact_revision f "
            "JOIN subject s ON s.id=f.subject_id WHERE s.kind IN (SELECT value FROM json_each(?)) "
            "AND ((f.period<=? AND EXISTS(SELECT 1 FROM fact_current a WHERE a.fact_id=f.id) "
            "AND NOT EXISTS(SELECT 1 FROM period_close p WHERE p.period=f.period)) OR EXISTS("
            "SELECT 1 FROM close_reference r WHERE r.reference_type='fact' "
            "AND r.reference_id=f.id AND r.close_period<=?) OR EXISTS("
            "SELECT 1 FROM calculation c JOIN close_reference r ON r.reference_id=c.id "
            "AND r.reference_type='calculation' WHERE c.fact_id=f.id AND r.close_period<=?) "
            "OR EXISTS(SELECT 1 FROM dependency_fact d JOIN close_reference r "
            "ON r.reference_id=d.calculation_id AND r.reference_type='calculation' "
            "WHERE d.fact_id=f.id AND r.close_period<=?)) "
            + period_clause
            + ") "
            + "SELECT id FROM (SELECT *,row_number() OVER(PARTITION BY subject_id "
            "ORDER BY revision DESC) AS n FROM candidates) WHERE n=1",
            parameters,
        )
        return [row[0] for row in rows]

    def effects(self):
        """Aggregate preceding effects in SQLite; only this month's events remain detailed."""
        source, parameters = self.journal.sql()
        states = [item["calculation_id"] for item in self.selected_states["state_results"]]
        sql = (
            "WITH events AS (SELECT j.period,j.id,j.number,j.basis_calculation_id calculation_id,"
            "j.reverses_id,CASE WHEN j.reverses_id IS NULL THEN 1 ELSE -1 END sign,0 opening "
            f"FROM ({source}) j UNION ALL SELECT p.posting_period,NULL,0,c.id,NULL,1,"
            "coalesce(json_extract(c.outcome,'$.opening'),0) FROM json_each(?) ids "
            "JOIN calculation c ON c.id=ids.value JOIN calculation_publication p "
            "ON p.calculation_id=c.id), effects AS (SELECT e.*,"
            "json_extract(b.value,'$.category') category,json_extract(b.value,'$.key') balance_key,"
            "json_extract(b.value,'$.amount') amount FROM events e "
            "JOIN calculation c ON c.id=e.calculation_id,json_each(c.outcome,'$.balances') b) "
        )
        parameters.append(json.dumps(states))
        preceding = self.connection.execute(
            sql + "SELECT category,balance_key,sum(sign*amount) amount FROM effects "
            "WHERE period<? OR opening GROUP BY category,balance_key",
            [*parameters, self.month],
        )
        for row in preceding:
            yield (
                {"category": row["category"], "key": row["balance_key"], "amount": row["amount"]},
                self.month - 1,
                None,
                1,
            )
        current = self.connection.execute(
            sql + "SELECT * FROM effects WHERE period=? AND NOT opening "
            "ORDER BY number,calculation_id",
            [*parameters, self.month],
        ).fetchall()
        self.reads.metadata({row["calculation_id"] for row in current})
        for item in current:
            row = {
                "id": item["id"],
                "number": item["number"],
                "calculation_id": item["calculation_id"],
                "period": item["period"],
                "reverses_id": item["reverses_id"],
                "sign": item["sign"],
                "basis": self.calculation(item["calculation_id"]),
            }
            yield (
                {
                    "category": item["category"],
                    "key": item["balance_key"],
                    "amount": item["amount"],
                },
                item["period"],
                row,
                item["sign"],
            )

    def voucher_relations(self, calc, sign):
        """Read exact obligation identities from the calculation's dependency graph."""
        cache_key = (calc["id"], sign)
        if cache_key in self.relation_cache:
            return self.relation_cache[cache_key]
        resolution = self.query_relations(calc)
        obligations = {item["key"]: item for item in resolution["obligations"]}
        data = calc["fact"]["data"]
        relations = []
        for index, effect in enumerate(calc["outcome"].get("balances", ())):
            obligation = obligations.get(effect["key"])
            if obligation is None:
                continue
            source = self.calculation(obligation["source_calculation_id"])
            source_data = source["fact"]["data"]
            source_period = (
                source_data["payroll_period"]
                if source["kind"] == "opening_payroll_payable"
                else str(YearMonth.from_ordinal(source["period"]))
            )
            employee = source_data.get("employee_id") or source_data.get("person_id")
            person = self.party_details(employee) if employee else {}
            bindings = [
                item
                for item in resolution["line_relations"]
                if item["obligation_key"] == effect["key"]
                and item["role"] not in {"funds", "tax_transfer", "reserve_return"}
            ]
            recipients = {item["recipient_id"] for item in bindings if item["recipient_id"]}
            line_numbers = {item["line_no"] for item in bindings if item["state"] == "resolved"}
            amount = sign * effect["amount"]
            direction = obligation["normal"]
            if amount < 0:
                direction = "credit" if direction == "debit" else "debit"
            recipient = next(iter(recipients)) if len(recipients) == 1 else None
            relations.append(
                {
                    "id": f"{calc['id']}:{index}",
                    "key": effect["key"],
                    "label": "确认往来" if effect["amount"] > 0 else "核销往来",
                    "account": obligation["account"],
                    "direction": direction,
                    "amount_fen": abs(amount),
                    "change_fen": amount,
                    "party_id": recipient or obligation.get("creditor_id"),
                    "creditor_id": obligation.get("creditor_id"),
                    "actual_recipient_id": recipient,
                    "party": "",
                    "line_number": next(iter(line_numbers)) if len(line_numbers) == 1 else None,
                    "source_label": " · ".join(
                        part
                        for part in (source_period, _name(source["kind"]), person.get("name", ""))
                        if part
                    ),
                    "field_sources": {
                        "source_label": _source_list(person.get("field_sources", {}).get("name"))
                    },
                    "source_calculation_id": source["id"],
                    "source_fact_id": source["fact_id"],
                    "source_period": source_period,
                    "source_kind": source["kind"],
                    "source_subject_id": source["subject_id"],
                    "obligation_name": obligation["name"],
                    "relation_state": "resolved"
                    if bindings and all(item["state"] == "resolved" for item in bindings)
                    else "unresolved",
                }
            )
        if (
            calc["kind"] in {"funding", "cash_funding", "platform_funding"}
            and data.get("funding_kind") == "capital"
        ):
            for index, line in enumerate(calc["outcome"]["lines"]):
                if line["account"] == "3001":
                    relations.append(
                        {
                            "id": f"{calc['id']}:capital:{index}",
                            "key": "capital",
                            "label": "确认投入",
                            "account": "3001",
                            "direction": "credit" if sign > 0 else "debit",
                            "amount_fen": line["credit"],
                            "change_fen": sign * line["credit"],
                            "party_id": data.get("owner_id"),
                            "party": "未提供",
                            "source_calculation_id": calc["id"],
                            "source_period": data["period"],
                            "line_number": index + 1,
                            "source_label": "",
                        }
                    )
        if calc["kind"] == "bank_income" and len(calc["outcome"].get("lines", ())) == 2:
            line = calc["outcome"]["lines"][1]
            if line["credit"] == data["amount_fen"]:
                relations.append(
                    {
                        "id": f"{calc['id']}:income",
                        "key": "income",
                        "label": "收入来源",
                        "account": line["account"],
                        "direction": "credit" if sign > 0 else "debit",
                        "amount_fen": data["amount_fen"],
                        "change_fen": sign * data["amount_fen"],
                        "party_id": data["counterparty_id"],
                        "party": "未提供",
                        "source_calculation_id": calc["id"],
                        "source_period": data["period"],
                        "source_label": "",
                        "line_number": 2,
                    }
                )
        self.bind_relation_lines(calc, sign, relations)
        for relation in relations:
            if relation.get("actual_recipient_id"):
                relation["party_id"] = relation["actual_recipient_id"]
            party = self.party_field(relation["party_id"], missing=relation["party"])
            relation["party"] = party["party"]
            relation["field_sources"] = {
                **relation.get("field_sources", {}),
                **party["field_sources"],
            }
        self.relation_cache[cache_key] = relations
        return relations

    @staticmethod
    def bind_relation_lines(calc, sign, relations):
        """Locate roles in sealed output; amounts validate a binding, never identify it."""
        lines = calc["outcome"].get("lines", ())

        def side(line):
            original = "debit" if line["debit"] else "credit"
            return original if sign > 0 else "credit" if original == "debit" else "debit"

        # A single aggregate line may represent several explicitly named obligations.
        # Multiple lines with the same account are deliberately not paired by equal amounts.
        groups = defaultdict(list)
        for relation in relations:
            groups[(relation["account"], relation["direction"])].append(relation)
        for (account, direction), members in groups.items():
            candidates = [
                i + 1
                for i, line in enumerate(lines)
                if line["account"] == account and side(line) == direction
            ]
            if len(candidates) == 1 and all(not r.get("line_number") for r in members):
                number = candidates[0]
                if sum(r["amount_fen"] for r in members) == sum(
                    lines[number - 1][s] for s in ("debit", "credit")
                ):
                    for relation in members:
                        relation["line_number"] = number
            for number in {r.get("line_number") for r in members} - {None}:
                selected = [r for r in members if r.get("line_number") == number]
                line = lines[number - 1] if 0 < number <= len(lines) else None
                valid = (
                    line is not None
                    and line["account"] == account
                    and side(line) == direction
                    and sum(r["amount_fen"] for r in selected) == line["debit"] + line["credit"]
                )
                if not valid:
                    for relation in selected:
                        relation["line_number"] = None

    def evidence_details(self, proofs):
        missing = set(proofs) - self.evidence_cache.keys()
        if missing:
            self.evidence_cache.update(
                (item["digest"], item)
                for item in self.store.evidence_metadata(self.connection, missing)
            )
        return sorted(
            (self.evidence_cache[p] for p in set(proofs) if p in self.evidence_cache),
            key=lambda item: (item["name"], item["digest"]),
        )

    def business_summary(self, calc, sign=1, relations=None, *, include_sources=False):
        """Owner-facing wording from exact selected facts, without inferred business causes."""
        data, kind = calc["fact"]["data"], calc["kind"]
        profile = self.profile("business", calc["fact"]["subject_id"])
        relations = self.voucher_relations(calc, sign) if relations is None else relations
        short = {
            "payroll": "计提工资",
            "payroll_bounded": "计提工资",
            "annual_bonus": "计提奖金",
            "expense": "确认费用",
            "service_sale": "确认收入",
            "pass_through": "代收代付",
            "employee_advance": "个人代付",
            "reimbursement_acceptance": "确认代付款",
            "settlement": "款项抵销",
            "asset_consumption": "折旧摊销",
            "reimbursed_asset_batch": "验收整批资产",
            "reimbursed_asset": "确认报销资产",
            "labor": "确认劳务报酬",
            "labor_accrual": "确认劳务报酬",
            "labor_project_cost": "确认项目劳务",
            "project_cost": "确认项目投入",
        }.get(kind, _name(kind))
        periods = {
            r["source_period"] for r in relations if r["source_calculation_id"] != calc["id"]
        }
        period = data.get("period", str(YearMonth.from_ordinal(calc["period"])))
        if kind in {"payment", "cash_payment", "platform_payment", "payroll_reserve_payment"}:
            names = {
                self.calculation(r["source_calculation_id"])["fact"]["data"]["component"]
                if r.get("source_kind") == "opening_payroll_payable"
                else r.get("obligation_name")
                for r in relations
            }
            if names and names <= {"employee_social", "employer_social"}:
                short = "支付社保"
            elif names and names <= {"employee_housing", "employer_housing"}:
                short = "支付公积金"
            elif names and names <= {"net", "net_salary", "salary", "bonus"}:
                short = "支付工资奖金"
            else:
                short = "收款" if data.get("direction") == "inflow" else "付款"
        elif kind in {"funding", "cash_funding", "platform_funding"}:
            short = "股东投入" if data.get("funding_kind") == "capital" else "借款到账"
        elif kind == "bank_income":
            short = {
                "bank_interest": "银行利息入账",
                "government_grant": "补助到账",
                "retained_verification_payment": "确认验证款收入",
                "bank_promotion_reward": "奖励到账",
            }.get(data.get("income_kind"), short)
        if profile.get("display_name"):
            short = profile["display_name"]
        ids = [
            data.get(key)
            for key in (
                "employee_id",
                "person_id",
                "counterparty_id",
                "customer_id",
                "supplier_id",
                "owner_id",
                "payer_id",
                "lender_id",
                "buyer_id",
                "recipient_id",
            )
        ]
        if data.get("payment_method") == "bank_batch":
            ids = [ident for ident in ids if ident != data.get("counterparty_id")]
        ids.extend(r["party_id"] for r in relations)
        named_parties = [
            self.party_details(i)
            for i in dict.fromkeys(ids)
            if i and i != "payroll-group" and self.party_details(i).get("source")
        ]
        names = [party["name"] for party in named_parties]
        # Preserve different identities even when their display names happen to be equal.
        who = "、".join(names[:3]) + (f"等{len(names)}个对象" if len(names) > 3 else "")
        when = "、".join(sorted(periods)) if periods else period
        detail = f"{short}（{when}）" + (f" · {who}" if who else "")
        purposes = [
            profile.get("purpose"),
            profile.get("note"),
            (self.management.get(calc["fact"]["subject_id"]) or {}).get("note"),
        ]
        supplied = list(dict.fromkeys(p.strip() for p in purposes if p and p.strip()))
        if supplied:
            detail += "；" + "；".join(supplied)
        if sign < 0:
            short, detail = "冲正·" + short, "冲销原业务：" + detail
        if include_sources:
            sources = [
                profile["field_sources"][field]
                for field in ("display_name", "purpose", "note")
                if profile.get(field)
            ]
            management = self.management.get(calc["fact"]["subject_id"])
            if management and management.get("note"):
                sources.append(self.management_source(management, "note"))
            for party in named_parties:
                source = party.get("field_sources", {}).get("name")
                sources.extend(source if isinstance(source, list) else [source] if source else [])
            return (
                short,
                detail,
                {
                    **_display_sources(profile, list_summary="display_name"),
                    "display_summary": sources,
                },
            )
        return short, detail

    @staticmethod
    def business_amount(calc):
        data, values = calc["fact"]["data"], calc["outcome"]["values"]
        # Each nontrivial amount names its business meaning; unrelated values cannot
        # become a headline simply because their field happens to exist first.
        spec = {
            "service_sale": ("gross_fen", "含税收入确认额"),
            "payroll": ("gross_fen", "税前工资"),
            "payroll_bounded": ("gross_fen", "税前工资"),
            "annual_bonus": ("gross_fen", "税前奖金"),
            "labor": ("gross_fen", "劳务确认毛额"),
            "labor_accrual": ("gross_fen", "劳务确认毛额"),
            "labor_project_cost": ("gross_fen", "资本化劳务确认毛额"),
            "asset": ("cost_fen", "已确认资产成本"),
            "reimbursed_asset": ("cost_fen", "已确认资产成本"),
            "reimbursed_asset_batch": ("cost_fen", "整批确认成本"),
            "asset_activation": ("cost_fen", "启用资产成本"),
            "asset_consumption": ("consumption_fen", "本期折旧摊销"),
            "asset_disposal": ("gross_proceeds_fen", "处置确认价款"),
            "loan_interest": ("interest_fen", "本期确认利息"),
            "loan_drawdown": ("principal_fen", "借款本金"),
            "project_release": ("released_fen", "转费用成本"),
            "money_fund_subscription": ("cost_fen", "申购确认成本"),
            "money_fund_redemption": ("net_proceeds_fen", "赎回结算额"),
            "income_tax_assessment": ("change_fen", "本期所得税确认额"),
            "platform_expense_confirmation": ("confirmed_amount_fen", "确认费用"),
        }
        field, label = spec.get(calc["kind"], ("amount_fen", "业务确认金额"))
        amount = values.get(field, data.get(field))
        if calc["kind"] == "employee_advance":
            own = values.get("obligations", ())
            amount = own[0]["amount_fen"] if len(own) == 1 else None
            label = "代付转债确认额"
        elif calc["kind"] in {
            "payment",
            "cash_payment",
            "platform_payment",
            "payroll_reserve_payment",
        }:
            label = "实际收付款"
        elif calc["kind"] in {"funding", "cash_funding", "platform_funding"}:
            label = "实际投入或借入金额"
        elif calc["kind"] == "managed_reserve_obligation_settlement":
            label = "备用金核销原应付款"
        elif calc["kind"] == "bank_platform_transfer":
            label = (
                "费用边界退出金额"
                if values.get("accounting_treatment") == "reserve_expense"
                else "内部划转金额"
            )
        return (amount if type(amount) is int else None), label

    def voucher(self, row):
        calc = row["basis"]
        fact = calc["fact"]
        kind, data = calc["kind"], fact["data"]
        period = str(YearMonth.from_ordinal(row["period"]))
        recognition = _recognition(data, period)
        profile = self.profile("business", fact["subject_id"])
        management = self.management.get(fact["subject_id"])
        note = profile.get("note") or (management["note"] if management else None) or ""
        relations = self.voucher_relations(calc, row["sign"])
        short_summary, summary, summary_sources = self.business_summary(
            calc, row["sign"], relations, include_sources=True
        )
        note_source = profile["field_sources"].get("note")
        if not note_source and management and management.get("note"):
            note_source = self.management_source(management, "note")
        party_fields = (
            "employee_id",
            "person_id",
            "counterparty_id",
            "customer_id",
            "supplier_id",
            "owner_id",
            "buyer_id",
            "lender_id",
            "payer_id",
            "recipient_id",
        )
        party_ids = {data[field] for field in party_fields if data.get(field)}
        if data.get("payment_method") == "bank_batch":
            party_ids.discard(data.get("counterparty_id"))
        party_ids.update(item["party_id"] for item in relations if item["party_id"])
        # Batch recipients are a business relationship; do not copy them to every line.
        for field in ("allocations", "sources"):
            party_ids.update(
                item["recipient_id"] for item in data.get(field, ()) if item.get("recipient_id")
            )
        party_ids.discard("payroll-group")
        parties = list(dict.fromkeys(self.party(ident) for ident in sorted(party_ids)))
        amount, amount_label = self.business_amount(calc)
        funds = []
        for index, effect in enumerate(calc["outcome"].get("balances", ())):
            if effect["category"] not in FUND_TYPES:
                continue
            change = row["sign"] * effect["amount"]
            account = self.profile("fund_account", effect["key"])
            funds.append(
                {
                    "id": f"{row['id']}:{index}",
                    "account_id": effect["key"],
                    "category": effect["category"],
                    "name": account.get("display_name") or "未提供账户名称",
                    "field_sources": _display_sources(account, name="display_name"),
                    "direction": "inflow" if change > 0 else "outflow",
                    "amount_fen": abs(change),
                }
            )
        evidence = set(fact["evidence"])
        for ref in self.connection.execute(
            "SELECT fact_id FROM dependency_fact WHERE calculation_id=?", (calc["id"],)
        ):
            evidence.update(self.fact(ref[0])["evidence"])
        component = {
            "id": fact["subject_id"],
            "key": fact["subject_id"],
            "kind": kind,
            "group": _group(kind),
            "label": _name(kind),
            "description": note,
            "amount_fen": row["sign"] * amount if amount is not None else None,
            "amount_label": amount_label,
            "parties": parties,
            "management": {
                "version": profile.get("revision", 0),
                "version_scope": "base_profile",
                "metadata": {"purpose": profile.get("purpose") or "", "description": note},
                "field_sources": {
                    **_display_sources(profile, purpose="purpose"),
                    **({"description": note_source} if note_source else {}),
                },
                "history": [],
            },
            "facts": data,
            "recognition": recognition,
            "derived": calc["outcome"]["values"],
            "party_sources": [
                {"party_id": ident, **self.party_details(ident)} for ident in sorted(party_ids)
            ],
            "source_references": [
                {"type": "evidence", "value": proof} for proof in fact["evidence"]
            ],
        }
        return {
            "number": str(row["number"]),
            "calculation_id": calc["id"],
            "voucher_version_id": row["id"],
            "reverses_version_id": row["reverses_id"],
            "date": recognition["date"],
            "recognition": recognition,
            "type": _name(kind),
            "kind": kind,
            "state": "冲正" if row["sign"] < 0 else "已入账",
            "summary": summary,
            "display_summary": summary,
            "list_summary": short_summary,
            "field_sources": summary_sources,
            "amount_fen": row["total"],
            "business_amount_fen": row["sign"] * amount if amount is not None else None,
            "business_amount_label": amount_label,
            "fund_inflow_fen": sum(
                item["amount_fen"] for item in funds if item["direction"] == "inflow"
            ),
            "fund_outflow_fen": sum(
                item["amount_fen"] for item in funds if item["direction"] == "outflow"
            ),
            "evidence": sorted(evidence),
            "evidence_details": self.evidence_details(evidence),
            "components": [component],
            "funds": funds,
            "settlements": relations,
            "lines": [
                {
                    "line_number": line["line_no"],
                    "code": line["account"],
                    "account": ACCOUNT_NAMES.get(line["account"], "未配置名称的科目"),
                    "debit_fen": line["debit"],
                    "credit_fen": line["credit"],
                    "party": self.line_party(line, relations),
                    "source_label": self.line_source(line, relations),
                    "field_sources": {
                        field: [
                            source
                            for relation in relations
                            if relation.get("line_number") == line["line_no"]
                            for source in _source_list(relation["field_sources"].get(field))
                        ]
                        for field in ("party", "source_label")
                    },
                    "parties": [
                        {"id": r["party_id"], "name": r["party"], "amount_fen": r["amount_fen"]}
                        for r in relations
                        if r.get("line_number") == line["line_no"] and r["party_id"]
                    ],
                    "party_state": self.line_party_state(line, relations),
                    "component_id": fact["subject_id"],
                }
                for line in row["lines"]
            ],
        }

    @staticmethod
    def line_party(line, relations):
        related = [r for r in relations if r.get("line_number") == line["line_no"]]
        matches = {r["party_id"]: r["party"] for r in related if r["party_id"]}
        return "、".join(matches.values()) if matches else ""

    @staticmethod
    def line_party_state(line, relations):
        related = [r for r in relations if r.get("line_number") == line["line_no"]]
        if not related:
            return (
                "unresolved"
                if any(
                    not r.get("line_number") and r["account"] == line["account"] for r in relations
                )
                else "not_applicable"
            )
        people = {r["party_id"] for r in related if r["party_id"]}
        if not people:
            return "not_applicable"
        if any(r["party"] == "未提供姓名或名称" for r in related if r["party_id"]):
            return "name_missing"
        return "multiple" if len(people) > 1 else "known"

    @staticmethod
    def line_source(line, relations):
        matches = dict.fromkeys(
            item["source_label"]
            for item in relations
            if item.get("source_label") and item.get("line_number") == line["line_no"]
        )
        return "；".join(matches)


class Dashboard:
    def __init__(self, engine, *, company_name="", companies=None):
        self.engine, self.store = engine, engine.store
        self.company_name, self.companies = company_name, companies

    def _periods(self, connection):
        values = {
            row[0]
            for row in connection.execute(
                "SELECT period FROM voucher_version v JOIN voucher_current c ON c.version_id=v.id "
                "UNION SELECT period FROM period_close UNION SELECT f.period FROM fact_revision f "
                "JOIN fact_current a ON a.fact_id=f.id UNION SELECT period FROM material_revision"
            )
            if row[0] > 0
        }
        closed = {row[0] for row in connection.execute("SELECT period FROM period_close")}
        return [
            _period_view(str(YearMonth.from_ordinal(value)), value in closed)
            for value in sorted(values, reverse=True)
        ]

    @contextmanager
    def _snapshot(self, period):
        with self.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            if period is None:
                periods = self._periods(connection)
                period = periods[0]["key"] if periods else None
            if period is not None:
                month = YearMonth(period).ordinal
                exists = connection.execute(
                    "SELECT 1 FROM ("
                    "SELECT v.period FROM voucher_version v JOIN voucher_current c "
                    "ON c.version_id=v.id WHERE v.period=? UNION ALL "
                    "SELECT period FROM period_close WHERE period=? UNION ALL "
                    "SELECT f.period FROM fact_revision f JOIN fact_current c ON c.fact_id=f.id "
                    "WHERE f.period=? UNION ALL "
                    "SELECT period FROM material_revision WHERE period=?) LIMIT 1",
                    (month, month, month, month),
                ).fetchone()
                if exists is None:
                    raise KernelError("dashboard_period_not_found", "没有找到所选会计月份")
            yield _Snapshot(self.engine, connection, period) if period else None

    def context(self):
        with self.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            periods = self._periods(connection)
            identity = dict(connection.execute("SELECT * FROM identity WHERE id=1").fetchone())
            companies = [
                {
                    "company_id": row.get("company_id", row.get("id")),
                    "name": row["name"],
                    "taxpayer_id": row.get("taxpayer_id"),
                    "status": "active",
                }
                for row in (
                    self.companies
                    or [
                        {
                            "id": self.store.company_id,
                            "name": self.company_name,
                            "taxpayer_id": identity["taxpayer_id"],
                        }
                    ]
                )
            ]
            current = next(row for row in companies if row["company_id"] == self.store.company_id)
            quarter_keys = sorted(
                {(p["year"], (p["month"] - 1) // 3 + 1) for p in periods}, reverse=True
            )
            reports = Reports(self.engine)
            report_periods_closed = {
                (year, quarter): reports.closed_period_coverage(
                    year, quarter, connection=connection
                )["complete"]
                for year, quarter in quarter_keys
            }
            quarters = [
                {
                    "key": f"{year}-Q{quarter}",
                    "year": year,
                    "quarter": quarter,
                    "label": f"{year} 年第 {quarter} 季度",
                    "complete": report_periods_closed[(year, quarter)],
                }
                for year, quarter in quarter_keys
            ]
            return {
                "schema_version": 2,
                "company": current["name"],
                "companies": companies,
                "current_company": current,
                "generated_at": datetime.now(UTC).isoformat(),
                "default_period": periods[0]["key"] if periods else None,
                "periods": periods,
                "default_quarter": quarters[0]["key"] if quarters else None,
                "quarters": quarters,
                "disclaimer": "用于负责人内部管理，不作为法定账簿、纳税申报或报税系统。",
            }

    @staticmethod
    def _response(snapshot, data):
        if snapshot:
            snapshot.attach_recorded_times()
        return {
            "schema_version": 2,
            "snapshot_version": snapshot.snapshot_version if snapshot else None,
            "selected_period": _period_view(snapshot.period, bool(snapshot.close))
            if snapshot
            else None,
            "read_semantics": {
                "knowledge": "current_knowledge",
                "accounting": "frozen_close"
                if snapshot and snapshot.close
                else "current_published",
                "display": "frozen_with_current_supplements"
                if snapshot and snapshot.close
                else "current",
                "system_time_replay": False,
                "recorded_at": "system_recording_time",
                "recording_period": "business_recording_period",
                "recorded_later": "business_recording_period_after_selected_period",
            },
            "data": data,
        }

    def brief(
        self,
        period: str | None = None,
        *,
        after_number: int = 0,
        limit: int = 100,
        expected_version: str | None = None,
        section: str | None = None,
        cursor: str | None = None,
        voucher_version_id: str | None = None,
        voucher_number: int | None = None,
    ):
        validate_page("brief", section, cursor, limit)
        if type(after_number) is not int or after_number < 0:
            raise ValueError("凭证游标必须为非负整数")
        if voucher_version_id is not None and voucher_number is not None:
            raise ValueError("精确凭证定位须只提供一个身份")
        if voucher_number is not None and (type(voucher_number) is not int or voucher_number < 1):
            raise ValueError("凭证编号须为正整数")
        with self._snapshot(period) as snap:
            if snap is None:
                return self._response(None, None)
            self._check_page_version(snap, cursor or after_number, expected_version)
            after = (
                decode_cursor(snap, "brief", section, cursor, {})
                if section != "file_jobs"
                else None
            )
            if section in {None, "vouchers"}:
                rows, page = snap.month_journal.page(
                    after if section == "vouchers" and after is not None else after_number, limit
                )
            else:
                rows, page = [], page_keys([], limit=limit, total_count=len(snap.month_journal))[1]
            vouchers = [snap.voucher(row) for row in rows]
            focused = None
            if voucher_version_id is not None or voucher_number is not None:
                found, _ = snap.month_journal.page(
                    0, 1, voucher_number=voucher_number, voucher_version_id=voucher_version_id
                )
                if not found:
                    raise KernelError("dashboard_voucher_not_found", "所选月份没有这张精确凭证")
                focused = snap.voucher(found[0])
            groups = []
            kind_counts = snap.month_journal.kind_counts()
            for key, label in GROUPS.items():
                all_rows = [
                    row for row in kind_counts if _group(row["kind"], row["reversal"]) == key
                ]
                if not all_rows:
                    continue
                selected = [
                    v
                    for row, v in zip(rows, vouchers, strict=True)
                    if _group(row["basis"]["kind"], row["sign"] < 0) == key
                ]
                groups.append(
                    {
                        "key": key,
                        "label": label,
                        "event_count": sum(row["count"] for row in all_rows),
                        "loaded_count": len(selected),
                        "type_counts": [
                            {"label": _name(row["kind"]), "count": row["count"]} for row in all_rows
                        ],
                        "rows": [
                            {
                                "date": v["date"],
                                "recognition": v["recognition"],
                                "reference": v["number"],
                                "calculation_id": v["calculation_id"],
                                "voucher_version_id": v["voucher_version_id"],
                                "title": v["list_summary"],
                                "subject": v["type"],
                                "description": v["display_summary"],
                                "display_description": v["display_summary"],
                                "field_sources": {
                                    "display_description": v["field_sources"]["display_summary"],
                                    **(
                                        {"title": v["field_sources"]["list_summary"]}
                                        if "list_summary" in v["field_sources"]
                                        else {}
                                    ),
                                },
                                "amount_fen": v["business_amount_fen"],
                                "amount_label": v["business_amount_label"],
                                "journal_total_fen": v["amount_fen"],
                                "state": v["state"],
                                "party": "、".join(v["components"][0]["parties"]),
                                "evidence": v["evidence"],
                                "evidence_details": v["evidence_details"],
                                "components": v["components"],
                                "funds": v["funds"],
                                "settlements": v["settlements"],
                            }
                            for v in selected
                        ],
                    }
                )
            funds, employees, assets = (
                _funds(snap, summary_only=True),
                _employees(snap, summary_only=True),
                _assets(snap, summary_only=True),
            )
            bank = funds["bank_statement"]
            unmatched = bank["unmatched_totals"]
            preparation = snap.preparation
            followups = preparation["current_followups"]
            materials = followups["materials"]
            material = {
                "closed": preparation["closure"]["state"] != "open",
                "satisfied": materials["status"] == "ready",
                "issues": materials["issues"],
                "coverage_digest": materials["coverage"]["coverage_digest"],
            }
            preparation_issues = []
            seen_issues = set()
            for group in ("accounting", "close_requirements"):
                for issue in followups[group]["issues"]:
                    key = json.dumps(issue, sort_keys=True, ensure_ascii=False)
                    if key not in seen_issues:
                        preparation_issues.append(issue)
                        seen_issues.add(key)
            order_failure = (preparation["readiness"] or {}).get("order_failure")
            if order_failure:
                preparation_issues.insert(
                    0,
                    {
                        "field": "close_order",
                        **order_failure,
                    },
                )
            journal_totals = snap.month_journal.totals()
            debit, credit = journal_totals["debit"], journal_totals["credit"]
            position = _position(snap)
            valid = debit == credit and position["equation_valid"]
            attention = (
                len(material["issues"])
                + len(preparation_issues)
                + int(valid is None)
                + unmatched["count"]
                + int(bank["coverage_state"] in {"missing", "partial"})
            )
            commentary = snap.commentary.get("current")
            data = {
                "generated_at": datetime.now(UTC).isoformat(),
                "management_commentary": (commentary or {}).get("text", ""),
                "management_commentary_details": snap.commentary,
                "material_completeness": material,
                "period_preparation": preparation_view(preparation),
                "voucher_count": len(snap.month_journal),
                "line_count": journal_totals["line_count"],
                "total_debit_fen": debit,
                "total_credit_fen": credit,
                "vouchers": vouchers,
                "focused_voucher": focused,
                "voucher_page": {
                    "has_more": page["has_more"],
                    "next_after_number": page["next_cursor"],
                    "total_count": page["total_count"],
                },
                "activity_groups": groups,
                "position": position,
                "funds_overview": {
                    key: funds[key]
                    for key in (
                        "total_fen",
                        "bank_fen",
                        "cash_fen",
                        "payment_platform_fen",
                        "inflow_fen",
                        "outflow_fen",
                        "net_change_fen",
                        "internal_transfer_fen",
                    )
                },
                "cash": {
                    k: bank[k]
                    for k in (
                        "transaction_count",
                        "matched_count",
                        "unmatched_count",
                        "needs_review_count",
                        "coverage_state",
                        "inflow_fen",
                        "outflow_fen",
                    )
                }
                | {
                    "net_fen": bank["inflow_fen"] - bank["outflow_fen"]
                    if bank["inflow_fen"] is not None
                    else None
                },
                "unmatched_bank_activity": {
                    **unmatched,
                    "rows": [],
                    "rows_truncated": unmatched["count"] > 0,
                },
                "open_items": _open_items(
                    snap,
                    current=followups["settlements"],
                    after=after if section == "open_items" else None,
                    limit=limit,
                    summary_only=section not in {None, "open_items"},
                ),
                "workforce_cost": employees["workforce_cost"],
                "long_term_assets": {
                    "net_fen": assets["ledger_net_fen"],
                    "fixed_net_fen": assets["fixed_asset_net_fen"],
                    "intangible_net_fen": assets["intangible_asset_net_fen"],
                    "fixed_active_count": assets["fixed"]["active_count"],
                    "intangible_active_count": assets["intangible"]["active_count"],
                    "pending_count": (
                        assets["fixed"]["pending_count"] + assets["intangible"]["pending_count"]
                    ),
                    "project_cost_fen": assets["project_cost_fen"],
                },
                "validation": {
                    "state": "error"
                    if valid is False
                    else "attention"
                    if attention
                    else "complete",
                    "title": "账务核对",
                    "summary": "账务汇总平衡"
                    if valid is True
                    else "财务位置依据不完整"
                    if valid is None
                    else "账务汇总需核对",
                    "integrity_valid": valid,
                    "voucher_balanced": debit == credit,
                    "issues": preparation_issues,
                    "attention_count": attention,
                    "items": [
                        {
                            "key": "balance",
                            "label": "凭证与余额",
                            "state": "pass"
                            if valid is True
                            else "pending"
                            if valid is None
                            else "error",
                            "text": "借贷及资产负债关系核对",
                        },
                        {
                            "key": "materials",
                            "label": "资料完整性",
                            "state": "pass" if material["satisfied"] else "pending",
                            "text": "按逐项资料检查器核对",
                        },
                        {
                            "key": "accounting",
                            "label": "核算准备",
                            "state": "pass"
                            if followups["accounting"]["status"] == "ready"
                            else "pending",
                            "text": "包括应建业务、正式发布及待复核状态",
                        },
                        {
                            "key": "close_requirements",
                            "label": "期间准备",
                            "state": "pending" if preparation_issues else "pass",
                            "text": "已关闭期间的当前跟进不改变原冻结结论"
                            if material["closed"]
                            else "按关账业务检查核对",
                        },
                    ],
                },
            }
            data["collections"] = {
                **(
                    {"vouchers": {"items": vouchers, "page": page}}
                    if section in {None, "vouchers"}
                    else {}
                ),
                **(
                    {"open_items": data["open_items"].pop("collection")}
                    if section in {None, "open_items"}
                    else {}
                ),
            }
            data["open_items"].pop("collection", None)
            if section in {"businesses", "settlement_events", "file_jobs"}:
                collection = self._business_collection(
                    snap,
                    "brief",
                    None,
                    section,
                    cursor,
                    {},
                    limit,
                    source_section="events" if section == "businesses" else section,
                )
                if section == "businesses":
                    metadata = snap.reads.metadata(
                        {
                            item["calculation_id"]
                            for item in collection["items"]
                            if item.get("calculation_id")
                        }
                    )
                    collection["items"] = [
                        {
                            **item,
                            "label": _name(item.get("kind", "")),
                            "subject_id": item.get("subject_id")
                            or metadata.get(item.get("calculation_id"), {}).get("subject_id"),
                        }
                        for item in collection["items"]
                    ]
                data["collections"][section] = collection
            elif section == "external_followups":
                data["collections"][section] = self._external_collection(snap, after, limit)
            return self._response(snap, seal_collections(snap, "brief", data, {}))

    def funds(
        self,
        period: str | None = None,
        *,
        after_movement: str | None = None,
        after_statement: str | None = None,
        after_investment: str | None = None,
        movement_account_type: str | None = None,
        movement_account_id: str | None = None,
        statement_account_id: str | None = None,
        limit: int = 100,
        expected_version: str | None = None,
        section: str | None = None,
        cursor: str | None = None,
    ):
        validate_page("funds", section, cursor, limit)
        if (movement_account_type is None) != (movement_account_id is None):
            raise ValueError("资金账户筛选须同时提供账户类别与账户标识")
        if movement_account_type is not None and movement_account_type not in FUND_TYPES.values():
            raise ValueError("不支持的资金账户类别")
        if movement_account_id == "" or statement_account_id == "":
            raise ValueError("资金账户标识不能为空")
        filters = {
            "movement_account_type": movement_account_type,
            "movement_account_id": movement_account_id,
            "statement_account_id": statement_account_id,
        }
        with self._snapshot(period) as snap:
            active_cursor = cursor or after_movement or after_statement or after_investment
            if snap is None:
                if active_cursor:
                    raise KernelError(
                        "dashboard_snapshot_changed", "所选公司或期间已变化，请重新加载明细。"
                    )
                return self._response(None, None)
            self._check_page_version(snap, active_cursor, expected_version)
            cursors = {
                "movements": after_movement,
                "statements": after_statement,
                "investment_events": after_investment,
            }
            if section:
                cursors[section] = cursor
            decoded = {
                key: decode_cursor(snap, "funds", key, value, filters)
                for key, value in cursors.items()
                if value is not None
            }
            data = _funds(
                snap,
                sections={section} if section else None,
                cursors=decoded,
                limit=limit,
                filters=filters,
            )
            data["period_preparation"] = preparation_view(snap.preparation)
            seal_collections(snap, "funds", data, filters)
            for name, target, field in (
                ("movements", data, "movement_page"),
                ("statements", data["bank_statement"], "page"),
                ("investment_events", data["investments"], "page"),
            ):
                if name in data["collections"]:
                    page = data["collections"][name]["page"]
                    target[field] = page | {"total_count": page["filtered_count"]}
            return self._response(snap, data)

    @staticmethod
    def _check_page_version(snapshot, cursor, expected_version):
        if (
            cursor or expected_version is not None
        ) and expected_version != snapshot.snapshot_version:
            raise KernelError(
                "dashboard_snapshot_changed", "资料已更新，已加载的明细需要整体刷新。"
            )

    def employees(
        self,
        period: str | None = None,
        *,
        section: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
        expected_version: str | None = None,
        employee_filter: str = "all",
        employee_id: str | None = None,
    ):
        validate_page("employees", section, cursor, limit)
        if employee_id == "" or section == "settlement_events" and employee_id is None:
            raise ValueError("员工清偿明细须指定有效 employee_id")
        if employee_filter not in {"all", "in_period", "payroll", "no_payroll", "unknown", "ended"}:
            raise ValueError("不支持的员工筛选")
        filters = {"employee_filter": employee_filter, "employee_id": employee_id}
        with self._snapshot(period) as snap:
            if snap is None:
                return self._response(None, None)
            self._check_page_version(snap, cursor, expected_version)
            after = decode_cursor(snap, "employees", section, cursor, filters)
            data = _employees(
                snap,
                sections={section} if section else None,
                cursors={section: after} if section else None,
                limit=limit,
                employee_filter=employee_filter,
                employee_id=employee_id,
            )
            subjects = data.pop("_entity_sources")
            if section == "settlement_events":
                if employee_id is None:
                    raise ValueError("员工清偿明细须指定 employee_id")
                data["collections"][section] = self._business_collection(
                    snap,
                    "employees",
                    subjects,
                    section,
                    cursor,
                    filters,
                    limit,
                )
            if section:
                data["collections"] = {section: data["collections"][section]}
            for item in data["employees"]["items"]:
                item["payroll_source_page"] = seal_page(
                    snap,
                    "employees",
                    "payroll_sources",
                    item["payroll_source_page"],
                    filters | {"employee_id": item["employee_id"]},
                )
            data["period_preparation"] = preparation_view(snap.preparation)
            return self._response(snap, seal_collections(snap, "employees", data, filters))

    def assets(
        self,
        period: str | None = None,
        *,
        section: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
        expected_version: str | None = None,
        asset_filter: str = "all",
        asset_id: str | None = None,
        project_id: str | None = None,
    ):
        validate_page("assets", section, cursor, limit)
        if (
            asset_id == ""
            or project_id == ""
            or (
                section in {"source_history", "settlement_events"}
                and asset_id is None
                and project_id is None
            )
        ):
            raise ValueError("资产来源或清偿明细须指定有效 asset_id 或 project_id")
        if asset_filter not in {"all", "active", "fixed", "intangible", "pending", "exited"}:
            raise ValueError("不支持的资产筛选")
        filters = {"asset_filter": asset_filter, "asset_id": asset_id, "project_id": project_id}
        with self._snapshot(period) as snap:
            if snap is None:
                return self._response(None, None)
            self._check_page_version(snap, cursor, expected_version)
            after = decode_cursor(snap, "assets", section, cursor, filters)
            data = _assets(
                snap,
                sections={section} if section else None,
                cursors={section: after} if section else None,
                limit=limit,
                asset_filter=asset_filter,
                asset_id=asset_id,
                project_id=project_id,
            )
            subjects = data.pop("_entity_sources")
            if section in {"source_history", "settlement_events"}:
                if asset_id is None and project_id is None:
                    raise ValueError("资产来源或清偿明细须指定 asset_id 或 project_id")
                data["collections"][section] = self._business_collection(
                    snap,
                    "assets",
                    subjects,
                    section,
                    cursor,
                    filters,
                    limit,
                )
            if section:
                data["collections"] = {section: data["collections"][section]}
            data["period_preparation"] = preparation_view(snap.preparation)
            return self._response(snap, seal_collections(snap, "assets", data, filters))

    def _external_collection(self, snap, after, limit):
        from .workflow import Workflow

        keys = [
            row[0]
            for row in snap.connection.execute(
                "SELECT s.id FROM subject s JOIN fact_current f ON f.subject_id=s.id "
                "WHERE s.kind='external_obligation' ORDER BY s.id"
            )
        ]
        keys, page = page_keys(keys, after, limit)
        items = Workflow(self.engine)._external_obligations(
            snap.connection,
            snap.period,
            snap.as_of,
            reads=snap.reads,
            obligation_ids=set(keys),
        )
        return {"items": items, "page": page}

    def _business_collection(
        self,
        snap,
        endpoint,
        subjects,
        section,
        cursor,
        filters,
        limit,
        *,
        source_section=None,
        settlement_view="current",
    ):
        version = None
        if section == "file_jobs":
            metadata = snap.queries._file_jobs(
                snap.connection, subjects, snap.period, metadata_only=True
            )
            version = digest(metadata).hex()
        after = decode_cursor(snap, endpoint, section, cursor, filters, collection_version=version)
        result = snap.queries.business_collection(
            snap.connection,
            subjects,
            snap.period,
            section=source_section or section,
            after=after,
            limit=limit,
            as_of=snap.as_of,
            current=section == "settlement_events" and settlement_view == "current",
        )
        if "collection_version" in result:
            result["page"]["collection_version"] = result.pop("collection_version")
        return result

    def business_status(
        self,
        period: str,
        subject_id: str,
        *,
        section: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
        expected_version: str | None = None,
        as_of: str | None = None,
        settlement_view: Literal["historical", "current"] = "current",
    ):
        validate_page("business-status", section, cursor, limit)
        if settlement_view not in {"historical", "current"}:
            raise ValueError("清偿口径须为 historical 或 current")
        with self._snapshot(period) as snap:
            self._check_page_version(snap, cursor, expected_version)
            data = snap.queries._business_status(
                snap.connection, subject_id, period, as_of=as_of, summary=True
            )
            snap.as_of = data["as_of"]
            filters = {
                "subject_id": subject_id,
                "as_of": data["as_of"],
                "settlement_view": settlement_view,
            }
            data["settlement_view"] = settlement_view
            sections = (
                (section,)
                if section
                else ("events", "settlement_events", "source_history", "file_jobs")
            )
            data["collections"] = {
                key: self._business_collection(
                    snap,
                    "business-status",
                    subject_id,
                    key,
                    cursor if key == section else None,
                    filters,
                    limit,
                    settlement_view=settlement_view,
                )
                for key in sections
            }
            response = self._response(
                snap, seal_collections(snap, "business-status", data, filters)
            )
            response["schema_version"] = 1
            return response

    def quarterly_report(
        self, year: int, quarter: int, *, carry_forward_fact_id: str | None = None
    ):
        from .business_queries import BusinessQueries

        reports = Reports(self.engine)
        with self.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            reads = QueryReads(self.engine, connection)
            opened = reports._report(
                year,
                quarter,
                source="open",
                connection=connection,
                reads=reads,
                carry_forward_fact_id=carry_forward_fact_id,
            )
            closed = reports._report(
                year,
                quarter,
                source="closed",
                connection=connection,
                reads=reads,
                carry_forward_fact_id=carry_forward_fact_id,
            )
            plan = closed if closed["status"] == "ready" else opened
            response = _quarterly_view(
                plan,
                closed,
                reports.browser_report_details(plan, closed, connection=connection),
                carry_forward_fact_id,
            )
            queries = BusinessQueries(self.engine, reads=reads)
            response["period_preparations"] = [
                preparation_view(
                    queries._period_readiness(connection, f"{year}-{month:02}", summary=True)
                )
                for month in range(quarter * 3 - 2, quarter * 3 + 1)
            ]
            return response


def _position(snap):
    balances = snap.accounts
    # Financial classifications are exact fact references, independent of a report profile.
    classification_ids = {
        row[0]
        for row in snap.connection.execute(
            "SELECT DISTINCT r.reference_id FROM close_reference r "
            "JOIN fact_revision f ON f.id=r.reference_id JOIN subject s ON s.id=f.subject_id "
            "WHERE r.reference_type='fact' AND r.path='readiness.financial_reports.facts[*]' "
            "AND s.kind='report_classification' AND r.close_period<=?",
            (snap.month,),
        )
    }
    classification_ids.update(
        row[0]
        for row in snap.connection.execute(
            "SELECT f.id FROM fact_current c JOIN fact_revision f ON f.id=c.fact_id "
            "JOIN subject s ON s.id=f.subject_id WHERE s.kind='report_classification' "
            "AND f.period<=? AND NOT EXISTS(SELECT 1 FROM period_close p WHERE p.period=f.period)",
            (snap.month,),
        )
    )
    classifications, ambiguous_classifications, source_issues = {}, set(), []
    for ident in sorted(classification_ids):
        fact = snap.fact(ident)
        if fact["kind"] == "report_classification":
            data = fact["data"]
            key = data["voucher_version_id"]
            if key in classifications:
                ambiguous_classifications.add(key)
                source_issues.append(
                    {
                        "field": "report_classification",
                        "message": "同一凭证版本存在多个分类来源",
                        "voucher_version_id": key,
                    }
                )
            classifications[key] = {
                item["line_no"]: item["counterparty_id"] for item in data["counterparties"]
            }
    for key in ambiguous_classifications:
        classifications[key] = {}
    known = (
        CASH_ACCOUNTS
        | set(PROFIT_ACCOUNTS)
        | set(DEBIT_BALANCE)
        | set(CREDIT_BALANCE)
        | TAX_ACCOUNTS
        | set(RECLASS)
    )
    detailed_accounts = set(RECLASS) | (set(balances) - known)
    rows = [
        {"account": account, "amount": value}
        for account, value in balances.items()
        if account not in detailed_accounts
    ]
    for event in snap.journal.select(accounts=detailed_accounts):
        resolution = snap.query_relations(event["basis"])
        for line in event["lines"]:
            if line["account"] not in detailed_accounts:
                continue
            row = {
                **line,
                "amount": line["debit"] - line["credit"],
                "version_id": event["id"],
                "reverses_id": event["reverses_id"],
            }
            if line["account"] in RECLASS:
                party = report_party_splits(
                    row,
                    resolution,
                    explicit_party_id=classifications.get(
                        event["reverses_id"] or event["id"], {}
                    ).get(line["line_no"]),
                )
                row["party_splits"] = party["splits"]
                source_issues.extend(party["issues"])
            rows.append(row)
    for calculation in snap.openings:
        members = calculation["outcome"]["values"].get("members")
        if members is None:
            members = [calculation["outcome"]]
        for member in members:
            for line in member.get("opening_lines", ()):
                if line["account"] not in detailed_accounts:
                    continue
                parties = {
                    item.get("counterparty_id")
                    for item in member.get("values", {}).get("obligations", ())
                    if item["account"] == line["account"]
                }
                party = next(iter(parties)) if len(parties) == 1 else None
                rows.append(
                    {
                        **line,
                        "amount": line["debit"] - line["credit"],
                        "party_key": ("party", party) if party else None,
                    }
                )
    position = classify_financial_position(rows)
    if snap.opening_selection["unestablished_state_selections"]:
        source_issues.append(
            {
                "field": "opening.selection",
                "message": "期初冻结采用依据未能建立，保留精确追溯。",
                "selections": snap.opening_selection["unestablished_state_selections"],
            }
        )
        position.update(assets_fen=None, liabilities_fen=None, equation_valid=None, complete=False)
    assets, liabilities = position["assets_fen"], position["liabilities_fen"]
    capital = -sum(value for account, value in balances.items() if account.startswith("3"))

    def result(values):
        revenue = -sum(
            value
            for account, value in values.items()
            if PROFIT_ACCOUNTS.get(account, (0, 0))[1] == -1
        )
        expense = sum(
            value
            for account, value in values.items()
            if PROFIT_ACCOUNTS.get(account, (0, 0))[1] == 1
        )
        return revenue, expense, revenue - expense

    revenue, expense, monthly = result(snap.month_accounts)
    cumulative = result(balances)[2]
    fixed, intangible = balances["1601"] + balances["1602"], balances["1701"] + balances["1702"]
    return {
        "assets_fen": assets,
        "liabilities_fen": liabilities,
        "capital_fen": capital,
        "bank_fen": balances["1002"],
        "fixed_asset_cost_fen": balances["1601"],
        "accumulated_depreciation_fen": -balances["1602"],
        "fixed_asset_net_fen": fixed,
        "intangible_asset_cost_fen": balances["1701"],
        "accumulated_amortization_fen": -balances["1702"],
        "intangible_asset_net_fen": intangible,
        "other_assets_fen": None
        if assets is None
        else assets - balances["1002"] - fixed - intangible,
        "month_revenue_fen": revenue,
        "month_expense_fen": expense,
        "month_result_fen": monthly,
        "cumulative_result_fen": cumulative,
        "equation_valid": position["equation_valid"],
        "complete": position["complete"] and not source_issues,
        "issues": [*source_issues, *position["issues"]],
    }


def _funds(snap, **options):
    from .dashboard_funds import funds

    return funds(snap, **options)


def _nullable_sum(values):
    numbers = list(values)
    return None if any(value is None for value in numbers) else sum(numbers)


def _open_items(snap, *, historical=None, current=None, after=None, limit=100, summary_only=False):
    historical = historical or snap.queries.settlement_summary(snap.connection, snap.period)
    current = current or snap.queries.settlement_summary(snap.connection, snap.period, current=True)
    configurations = {
        "customer_receivables": ("待收客户款", "receivable"),
        "supplier_advances": ("待冲抵供应商预付款", "receivable"),
        "refundable_deposit_receivables": ("待收回保证金", "receivable"),
        "other_receivables": ("其他应收事项", "receivable"),
        "supplier_payables": ("待付供应商款", "payable"),
        "employee_payables": ("待付员工报销款", "payable"),
        "payroll_payables": ("待付工资、社保与个税", "payable"),
        "labor_payables": ("待付个人劳务及个税", "payable"),
        "other_payables": ("其他应付事项", "payable"),
    }

    def outstanding(shared):
        return [
            row
            for row in shared["obligations"]
            if row["remaining_fen"] is None or row["remaining_fen"] != 0
        ]

    def category(source):
        business = source.get("source_business") or {}
        kind, account = business.get("kind", ""), source.get("account")
        if source.get("category") == "receivable":
            return (
                "customer_receivables"
                if account == "1122"
                else "supplier_advances"
                if account == "1123"
                else "refundable_deposit_receivables"
                if "deposit" in kind
                else "other_receivables"
            )
        return (
            "payroll_payables"
            if kind in PAYROLL_KINDS or kind == "opening_payroll_payable"
            else "labor_payables"
            if kind in LABOR_KINDS
            else "supplier_payables"
            if account == "2202"
            else "employee_payables"
            if kind
            in {
                "employee_advance",
                "reimbursement_acceptance",
                "reimbursed_asset",
                "reimbursed_asset_batch",
            }
            else "other_payables"
        )

    sources = outstanding(historical)
    selected, page = page_keys([row["key"] for row in sources], after, limit)
    selected = set(selected) if not summary_only else set()
    buckets, categories, items = defaultdict(list), [], []
    for source in sources:
        buckets[category(source)].append(source)
    for key, (label, direction) in configurations.items():
        sources_in_category = buckets[key]
        if not sources_in_category:
            continue
        rows = []
        for source in sources_in_category:
            if source["key"] not in selected:
                continue
            business = source.get("source_business") or {}
            party_id = (
                source.get("creditor_id")
                or source.get("counterparty_id")
                or source.get("recipient_id")
            )
            row = {
                **source,
                "id": source["key"],
                "category_key": key,
                "voucher": "查看精确来源",
                "party_key": party_id or source["key"],
                **snap.party_field(party_id),
                "description": _name(business.get("kind", "")),
                "status": source["settlement_status"],
                "outstanding_fen": source["remaining_fen"],
                "subject_id": business.get("subject_id"),
            }
            rows.append(row)
            items.append(row)
        grouped = defaultdict(list)
        for row in rows:
            grouped[row["party_key"]].append(row)
        categories.append(
            {
                "key": key,
                "label": label,
                "direction": direction,
                "unit": "笔",
                "count": len(sources_in_category),
                "loaded_count": len(rows),
                "outstanding_fen": _nullable_sum(
                    source["remaining_fen"] for source in sources_in_category
                ),
                "items": rows,
                "groups": [
                    {
                        "key": ident,
                        "party": group[0]["party"],
                        "field_sources": group[0]["field_sources"],
                        "count": len(group),
                        "outstanding_fen": _nullable_sum(row["outstanding_fen"] for row in group),
                        "open_count": sum(row["status"] == "open" for row in group),
                        "partial_count": sum(row["status"] == "partial" for row in group),
                    }
                    for ident, group in grouped.items()
                ],
            }
        )

    def totals(shared):
        active = outstanding(shared)
        unknown = shared.get("unestablished_state_selections", ())
        result = {}
        for direction in ("receivable", "payable"):
            rows = [row for row in active if row.get("category") == direction]
            result[direction + "_count"] = len(rows)
            result[direction + "_fen"] = (
                None if unknown else _nullable_sum(row["remaining_fen"] for row in rows)
            )
        return result | {
            "total_count": len(active),
            "unestablished_count": len(unknown),
            "complete": shared.get("complete", True),
            "status": shared["status"],
            "issues": shared["issues"],
        }

    return totals(historical) | {
        "categories": categories,
        "current_outstanding": totals(current),
        "collection": {"items": items, "page": page},
        "cutoff_period": historical["cutoff_period"],
        "current_cutoff_period": current.get("current_cutoff_period", current["cutoff_period"]),
    }


def _source_settlement(snap, calc, *, limit=100):
    """Project the shared obligation relation; no page-specific settlement arithmetic."""
    subject = calc["subject_id"]
    snap.prepare_settlements({subject})
    shared = snap.settlement_cache[subject]
    source_obligations = [
        item
        for item in shared["obligations"]
        if (item.get("source_business") or {}).get("subject_id") == subject
    ]
    obligations = [{**item, "amount_fen": item["source_amount_fen"]} for item in source_obligations]
    if not hasattr(snap, "settlement_page_cache"):
        snap.settlement_page_cache = {}
    if (subject, limit) not in snap.settlement_page_cache:
        snap.settlement_page_cache[subject, limit] = snap.queries.business_collection(
            snap.connection,
            subject,
            snap.period,
            section="settlement_events",
            limit=limit,
            as_of=snap.as_of,
            current=False,
        )
    collection = snap.settlement_page_cache[subject, limit]
    movements = []
    for item in collection["items"]:
        business = item.get("settlement_business") or {}
        calculation_id = item.get("settlement_calculation_id")
        source = snap.query_calculation(calculation_id) if calculation_id else None
        data = source["fact_data"] if source else {}
        mode = "advance" if item["mode"] == "accepted" else item["mode"]
        party = item.get("recipient_id") or item.get("creditor_id")
        movements.append(
            {
                **item,
                "id": item["id"],
                "calculation_id": calculation_id,
                "source_id": business.get("subject_id"),
                "kind": business.get("kind"),
                "mode": mode,
                "label": {
                    "payment": "实际收付款",
                    "advance": "代付或转债",
                    "offset": "非现金抵销",
                }.get(mode, "其他清偿"),
                "period": item["posting_period"],
                "date": data.get("actual_date") or data.get("actual_creditor_payment_date"),
                "amount_fen": item.get("signed_amount_fen"),
                "obligation": item.get("obligation_name"),
                "party_id": party,
                "payer_id": data.get("payer_id"),
                "relation_state": item["state"],
                "reversal": item["direction"] < 0,
                **snap.party_field(party),
            }
        )
    current = snap.settlement_current_cache[subject]
    return {
        "subject_id": subject,
        "settlement_view": "historical",
        "movements_scope": "business_related_settlement_events",
        "status": shared["status"],
        "obligations": obligations,
        "movements": movements,
        "movements_page": seal_page(
            snap,
            "business-status",
            "settlement_events",
            collection["page"],
            {"subject_id": subject, "as_of": snap.as_of, "settlement_view": "historical"},
        ),
        "issues": shared["issues"],
        "cutoff_period": shared["cutoff_period"],
        "current_followups": {
            key: current[key]
            for key in ("status", "issues", "current_cutoff_period", "cutoff_semantics")
            if key in current
        }
        | {
            "obligations": [
                item
                for item in current["obligations"]
                if (item.get("source_business") or {}).get("subject_id") == subject
            ]
        },
    }


def _employees(
    snap,
    *,
    sections=None,
    cursors=None,
    limit=100,
    employee_filter="all",
    employee_id=None,
    summary_only=False,
):
    cursors = cursors or {}
    requested = (
        set(sections) if sections is not None else {"employees", "payroll_sources", "labor_sources"}
    )
    money_keys = (
        "gross_salary_fen",
        "annual_bonus_fen",
        "employer_social_insurance_fen",
        "employer_housing_fund_fen",
        "employee_social_insurance_fen",
        "employee_housing_fund_fen",
        "individual_income_tax_fen",
        "net_salary_fen",
        "tax_reported_salary_fen",
    )
    aggregates = {}
    period_rows = defaultdict(list)
    rows = metric_rows(
        snap.month_journal.select(kinds=PAYROLL_KINDS),
        (
            "gross_fen",
            "tax_fen",
            "net_fen",
            "tax_status",
            "obligations",
            "actual_withholding",
            "calculated_tax_fen",
        ),
        cost_accounts={"560201", "560101", "540101"},
    )
    posted = {row["basis"]["subject_id"] for row in rows}
    rows.extend(
        {
            "basis": calc,
            "sign": 1,
            "reverses_id": None,
            "calculation_id": calc["id"],
            "lines": [],
            "period": snap.month,
            "metric_cost": 0,
        }
        for calc in snap.calculations_of_kind(*PAYROLL_KINDS)
        if calc["kind"] in PAYROLL_KINDS
        and calc["posting_period"] == snap.month
        and calc["subject_id"] not in posted
    )
    for row in rows:
        calc, sign = row["basis"], row["sign"]
        data, values = calc["fact"]["data"], calc["outcome"]["values"]
        ident = data["employee_id"]
        item = aggregates.setdefault(
            ident,
            {
                **dict.fromkeys(money_keys, 0),
                "batches": set(),
                "periods": set(),
                "areas": set(),
                "scopes": set(),
            },
        )
        item["batches"].add(calc["subject_id"])
        item["periods"].add(data["period"])
        item["areas"].add(data.get("expense_class", "management"))
        item["scopes"].add(
            "contributions_only" if values.get("tax_status") == "not_started" else "wage_income"
        )
        item["annual_bonus_fen" if calc["kind"] == "annual_bonus" else "gross_salary_fen"] += (
            sign * values.get("gross_fen", 0)
        )
        item["individual_income_tax_fen"] += sign * values.get("tax_fen", 0)
        item["net_salary_fen"] += sign * values.get("net_fen", 0)
        item["tax_reported_salary_fen"] += sign * data.get("tax_reported_salary_fen", 0)
        contributions = {ob["name"]: ob["amount_fen"] for ob in values.get("obligations", ())}
        for side in ("employee", "employer"):
            for kind, label in (("social", "social_insurance"), ("housing", "housing_fund")):
                item[f"{side}_{label}_fen"] += sign * contributions.get(f"{side}_{kind}", 0)
        period_rows[data["period"]].append(row)
    profiles = defaultdict(list)
    for fact in snap.by_kind("payroll_profile"):
        data = fact["data"]
        if data["effective_from"] <= snap.period and (
            data["effective_to"] is None or data["effective_to"] >= snap.period
        ):
            profiles[data["employee_id"]].append(fact)
    known = set(aggregates) | set(snap.profiles.get("employee", {}))
    wage_calculations = list(snap.calculations_of_kind(*PAYROLL_KINDS, "opening_payroll_payable"))
    unestablished_employees = snap.calculations.unestablished_entities(
        kinds={*PAYROLL_KINDS, "opening_payroll_payable"}, field="employee_id"
    )
    wage_scalars = scalar_facts(snap, wage_calculations)
    known.update(data["employee_id"] for data in wage_scalars.values())
    known.update(profiles)
    known.update(unestablished_employees)
    if hasattr(snap, "metadata"):
        snap.metadata.prime_profiles("employee", known)
    employee_states = {}
    for ident in sorted(known):
        info = snap.profile("employee", ident)
        start, end = info.get("employment_start"), info.get("employment_end")
        if info["field_conflicts"]:
            state, in_period = "unknown", None
        elif start and start[:7] > snap.period:
            state, in_period = "not_started", False
        elif end and end[:7] < snap.period:
            state, in_period = "ended", False
        elif start and end or info.get("employment_status") == "active":
            state, in_period = "in_period", True
        else:
            state, in_period = "unknown", None
        employee_states[ident] = (state, in_period)
    filtered_ids = [
        ident
        for ident in sorted(known)
        if (
            employee_filter == "all"
            or employee_filter == "in_period"
            and employee_states[ident][1] is True
            or employee_filter == "payroll"
            and bool(aggregates.get(ident, {}).get("batches"))
            or employee_filter == "no_payroll"
            and employee_states[ident][1] is True
            and not aggregates.get(ident, {}).get("batches")
            or employee_filter == "unknown"
            and (employee_states[ident][0] == "unknown" or ident in unestablished_employees)
            or employee_filter == "ended"
            and employee_states[ident][0] == "ended"
        )
        and (employee_id is None or ident == employee_id)
    ]
    selected_ids, employee_page = page_keys(
        filtered_ids, cursors.get("employees"), limit, total_count=len(known)
    )
    selected_ids = set(selected_ids) if not summary_only else set()
    if not requested.intersection({"employees", "payroll_sources"}):
        selected_ids = set()
    source_pages, picked_sources = {}, set()
    for ident in selected_ids:
        candidates = sorted(
            (
                calc
                for calc in wage_calculations
                if wage_scalars[calc["fact_id"]]["employee_id"] == ident
            ),
            key=lambda calc: (
                wage_scalars[calc["fact_id"]].get("payroll_period")
                or wage_scalars[calc["fact_id"]]["period"],
                calc["subject_id"],
            ),
        )
        keys, source_pages[ident] = page_keys(
            [calc["id"] for calc in candidates],
            cursors.get("payroll_sources") if employee_id == ident else None,
            limit,
        )
        picked_sources.update(keys)
    # Declarations are observed later than their tax month. Reuse their current
    # recorded facts, keeping the recording month visible instead of hiding them.
    declaration_ids = (
        [
            row[0]
            for row in snap.connection.execute(
                "SELECT f.id FROM fact_current c JOIN fact_revision f ON f.id=c.fact_id "
                "JOIN fact_payroll_tax_declaration_actual d ON d.revision_id=f.id "
                "WHERE d.employee_id IN (SELECT value FROM json_each(?)) AND d.tax_period IN "
                "(SELECT value FROM json_each(?))",
                (
                    json.dumps(sorted(selected_ids)),
                    json.dumps(
                        [
                            YearMonth(value).ordinal
                            for value in sorted(
                                {snap.period}
                                | {
                                    wage_scalars[calc["fact_id"]]["period"]
                                    for calc in wage_calculations
                                    if calc["id"] in picked_sources
                                }
                            )
                        ]
                    ),
                ),
            )
        ]
        if selected_ids and "payroll_tax_declaration_actual" in snap.store.registry.models
        else []
    )
    declaration_facts = list(snap.reads.facts(declaration_ids).values())
    disbursement_calculations = []
    for row in (
        snap.connection.execute(
            "SELECT c.id,c.fact_id=f.fact_id AS current_fact,"
            "EXISTS(SELECT 1 FROM pending p WHERE p.subject_id=c.subject_id) AS pending "
            "FROM calculation_current a JOIN calculation c ON c.id=a.calculation_id "
            "JOIN calculation_publication v ON v.calculation_id=c.id "
            "JOIN fact_current f ON f.subject_id=c.subject_id "
            "JOIN fact_payroll_disbursement_basis b ON b.revision_id=c.fact_id "
            "WHERE c.kind='payroll_disbursement_basis' "
            "AND b.payroll_id IN (SELECT value FROM json_each(?))",
            (
                json.dumps(
                    [
                        calc["subject_id"]
                        for calc in wage_calculations
                        if calc["id"] in picked_sources
                    ]
                ),
            ),
        )
        if picked_sources and "payroll_disbursement_basis" in snap.store.registry.models
        else ()
    ):
        disbursement_calculations.append(
            snap.calculation(row["id"])
            | {"needs_review": not row["current_fact"] or bool(row["pending"])}
        )
    displayed_wages = [calc for calc in wage_calculations if calc["id"] in picked_sources]
    snap.prepare_settlements({calc["subject_id"] for calc in displayed_wages})
    wage_sources = defaultdict(list)
    net_payments, direct_payments = defaultdict(int), defaultdict(int)
    summarized_wages = {
        calc["subject_id"]: calc
        for calc in wage_calculations
        if wage_scalars[calc["fact_id"]]["employee_id"] in selected_ids
    }
    if summarized_wages:
        payment_summary = snap.queries.settlement_summary(
            snap.connection, snap.period, subject_ids=set(summarized_wages)
        )
        net_parts, paid_parts = defaultdict(list), defaultdict(list)
        for obligation in payment_summary["obligations"]:
            source = summarized_wages.get(
                (obligation.get("source_business") or {}).get("subject_id")
            )
            if source is None:
                continue
            data = wage_scalars[source["fact_id"]]
            net_name = (
                "primary"
                if source["kind"] == "opening_payroll_payable" and data["component"] == "net"
                else "net"
            )
            if obligation.get("name") == net_name:
                ident = data["employee_id"]
                paid_parts[ident].append(obligation["period_paid_fen"])
                net_parts[ident].extend(
                    (obligation["period_paid_fen"], obligation["period_other_settled_fen"])
                )
        for ident in selected_ids:
            direct_payments[ident] = _nullable_sum(paid_parts[ident])
            net_payments[ident] = _nullable_sum(net_parts[ident])
    for calc in displayed_wages:
        if calc["kind"] not in PAYROLL_KINDS | {"opening_payroll_payable"}:
            continue
        data = calc["fact"]["data"]
        ident = data["employee_id"]
        known.add(ident)
        settlement = _source_settlement(snap, calc, limit=limit)
        declarations = [
            {
                "fact_id": fact["id"],
                "revision": fact["revision"],
                "source": "current_record",
                "tax_period": fact["data"]["tax_period"],
                "recording_period": fact["data"]["period"],
                "date": fact["data"].get("declaration_date"),
                "declared_tax_fen": fact["data"]["declared_tax_fen"],
                "recorded_later": fact["data"]["period"] > snap.period,
                "recorded_at": None,
                "source_metadata": snap.fact_source(fact),
            }
            for fact in declaration_facts
            if calc["kind"] in {"payroll", "payroll_bounded"}
            and fact["data"]["employee_id"] == ident
            and fact["data"]["tax_period"] == data["period"]
        ]
        snap.recorded_records.extend(
            (record, ("fact", str(record["fact_id"]))) for record in declarations
        )
        bases = [
            {
                "calculation_id": basis["id"],
                "recording_period": basis["fact"]["data"]["period"],
                "needs_review": basis["needs_review"],
                "matches_displayed_wage": basis["outcome"]["values"]["payroll_calculation_id"]
                == calc["id"],
                **basis["outcome"]["values"],
            }
            for basis in disbursement_calculations
            if basis["outcome"]["values"]["payroll_id"] == calc["subject_id"]
        ]
        wage_sources[ident].append(
            {
                "source_id": calc["subject_id"],
                "calculation_id": calc["id"],
                "kind": calc["kind"],
                "period": data["payroll_period"]
                if calc["kind"] == "opening_payroll_payable"
                else data["period"],
                "opening_period": data["period"]
                if calc["kind"] == "opening_payroll_payable"
                else None,
                "component": data["component"]
                if calc["kind"] == "opening_payroll_payable"
                else None,
                "label": _name(calc["kind"]),
                "declarations": declarations,
                "disbursements": bases,
                **settlement,
            }
        )
    items, all_items = [], []
    for index, ident in enumerate(sorted(known), 1):
        info = snap.profile("employee", ident)
        choices = profiles[ident]
        if choices:
            latest = max(fact["data"]["effective_from"] for fact in choices)
            choices = [fact for fact in choices if fact["data"]["effective_from"] == latest]
        profile = choices[0]["data"] if len(choices) == 1 else None
        amounts = aggregates.get(
            ident,
            {
                **dict.fromkeys(money_keys, 0),
                "batches": set(),
                "periods": set(),
                "areas": set(),
                "scopes": set(),
            },
        )
        start, end = info.get("employment_start"), info.get("employment_end")
        # Explicit employment bounds describe the selected month even after the
        # current personnel record was deactivated. A current inactive flag on
        # its own does not establish when an earlier employment period ended.
        if info["field_conflicts"]:
            state, in_period = "unknown", None
        elif start and start[:7] > snap.period:
            state, in_period = "not_started", False
        elif end and end[:7] < snap.period:
            state, in_period = "ended", False
        elif start and end:
            state, in_period = "in_period", True
        elif info.get("employment_status") == "active":
            state, in_period = "in_period", True
        else:
            state, in_period = "unknown", None
        scopes = amounts["scopes"]
        scope = next(iter(scopes)) if len(scopes) == 1 else "mixed" if scopes else "none"
        all_items.append(
            {
                "employee_id": ident,
                "selection_status": "unestablished"
                if ident in unestablished_employees
                else "established",
                "in_period": in_period,
                "has_payroll_activity": bool(amounts["batches"]),
                "profile_available": profile is not None,
                "wage_tax_scope": scope,
                **{key: amounts[key] for key in money_keys},
                "personal_deduction_fen": amounts["employee_social_insurance_fen"]
                + amounts["employee_housing_fund_fen"]
                + amounts["individual_income_tax_fen"],
                "company_cost_fen": amounts["gross_salary_fen"]
                + amounts["annual_bonus_fen"]
                + amounts["employer_social_insurance_fen"]
                + amounts["employer_housing_fund_fen"],
            }
        )
        if ident not in selected_ids:
            continue
        person = snap.party_details(ident)
        code, code_source = snap.party_code_details(ident)
        tax_details = []
        for row in rows:
            calc = row["basis"]
            if calc["fact"]["data"]["employee_id"] != ident:
                continue
            value = calc["outcome"]["values"]
            actual = value.get("actual_withholding")
            calculated = value.get("calculated_tax_fen") if actual else value.get("tax_fen")
            tax_details.append(
                {
                    "calculation_id": calc["id"],
                    "period": calc["fact"]["data"]["period"],
                    "kind": calc["kind"],
                    "reversal": row["sign"] < 0,
                    "booked_tax_fen": row["sign"] * value.get("tax_fen", 0),
                    "calculated_tax_fen": row["sign"] * calculated
                    if calculated is not None
                    else None,
                    "actual_withholding_tax_fen": row["sign"] * actual["withheld_tax_fen"]
                    if actual
                    else None,
                    "actual_withholding_fact_id": actual["fact_id"] if actual else None,
                }
            )
        declarations = [
            fact
            for fact in declaration_facts
            if fact["data"]["employee_id"] == ident and fact["data"]["tax_period"] == snap.period
        ]
        items.append(
            {
                "employee_id": ident,
                "code": code or f"人员 {index}",
                "name": person["name"],
                "field_sources": {
                    **_display_sources(
                        info,
                        record_status="employment_status",
                        employment_start_date="employment_start",
                        employment_end_date="employment_end",
                    ),
                    **person.get("field_sources", {}),
                    **({"code": code_source} if code_source else {}),
                    **(
                        {
                            "declared_tax_fen": snap.fact_source(
                                declarations[0], field="declared_tax_fen"
                            )
                        }
                        if len(declarations) == 1
                        else {}
                    ),
                    **(
                        {
                            output: snap.fact_source(choices[0], field=field)
                            for output, field in (
                                ("tax_withholding_start_date", "withholding_start_date"),
                                (
                                    "social_insurance_participating",
                                    "social_insurance_participating",
                                ),
                                ("housing_fund_participating", "housing_fund_participating"),
                                ("social_insurance_base_fen", "social_insurance_base_fen"),
                                ("housing_fund_base_fen", "housing_fund_base_fen"),
                            )
                            if profile.get(field) is not None
                        }
                        if profile
                        else {}
                    ),
                },
                "field_conflicts": info["field_conflicts"],
                "record_status": info.get("employment_status") or "unknown",
                "period_state": state,
                "period_state_label": {
                    "unknown": "未确认人员在册状态",
                    "not_started": "本月早于已提供的开始日期",
                    "ended": "已确认结束或不在册",
                    "in_period": "已确认在册",
                }[state],
                "in_period": in_period,
                "employment_start_date": start,
                "employment_end_date": end,
                "tax_withholding_start_date": profile["withholding_start_date"]
                if profile
                else None,
                "profile_available": profile is not None,
                "expense_areas": sorted(
                    {
                        {
                            "management": "管理",
                            "administration": "管理",
                            "sales": "销售",
                            "service": "服务",
                        }.get(area, "其他费用归属")
                        for area in amounts["areas"]
                    }
                ),
                **{
                    field: profile[field] if profile else None
                    for field in (
                        "social_insurance_participating",
                        "housing_fund_participating",
                        "social_insurance_base_fen",
                        "housing_fund_base_fen",
                    )
                },
                "has_payroll_activity": bool(amounts["batches"]),
                "batch_count": len(amounts["batches"]),
                "tax_details": tax_details,
                "declared_tax_fen": declarations[0]["data"]["declared_tax_fen"]
                if len(declarations) == 1
                else None,
                "recorded_net_payments_fen": net_payments[ident],
                "direct_net_payments_fen": direct_payments[ident],
                "other_net_settlements_fen": net_payments[ident] - direct_payments[ident]
                if net_payments[ident] is not None and direct_payments[ident] is not None
                else None,
                "payroll_sources": sorted(
                    wage_sources[ident], key=lambda item: (item["period"], item["source_id"])
                ),
                "payroll_source_page": source_pages[ident],
                "payroll_periods": sorted(amounts["periods"]),
                "has_annual_bonus": amounts["annual_bonus_fen"] != 0,
                **{key: amounts[key] for key in money_keys},
                "personal_deduction_fen": amounts["employee_social_insurance_fen"]
                + amounts["employee_housing_fund_fen"]
                + amounts["individual_income_tax_fen"],
                "company_cost_fen": amounts["gross_salary_fen"]
                + amounts["annual_bonus_fen"]
                + amounts["employer_social_insurance_fen"]
                + amounts["employer_housing_fund_fen"],
                "wage_tax_scope": scope,
                "wage_tax_scope_label": {
                    "none": "本月无工资核算",
                    "wage_income": "工资薪金",
                    "contributions_only": "仅确认社保公积金",
                    "mixed": "包含不同核算情形",
                }[scope],
            }
        )
    sums = {
        key: sum(item[key] for item in all_items)
        for key in (*money_keys, "personal_deduction_fen", "company_cost_fen")
    }
    # The ordinary projections above contain only established amounts. Candidate
    # identities remain in the same page, with their full proof kept separately.
    established_items = {item["employee_id"]: item for item in items}
    items = [item for item in items if item["employee_id"] not in unestablished_employees]
    for ident in sorted(selected_ids & unestablished_employees.keys()):
        proof = unestablished_employees[ident]
        items.append(
            {
                "employee_id": ident,
                "name": snap.party(ident),
                "selection_status": "unestablished",
                "candidate_selections": proof["candidate_selections"],
                "trace_targets": proof["trace_targets"],
                "payroll_source_page": source_pages[ident],
                **dict.fromkeys(
                    (
                        *money_keys,
                        "personal_deduction_fen",
                        "company_cost_fen",
                        "recorded_net_payments_fen",
                        "direct_net_payments_fen",
                        "other_net_settlements_fen",
                    ),
                    None,
                ),
                **(
                    {"established_card": established_items[ident]}
                    if ident in established_items and established_items[ident]["payroll_sources"]
                    else {}
                ),
            }
        )
    items.sort(key=lambda item: item["employee_id"])
    controlled = sums["company_cost_fen"]
    salary_accounts = {"560201", "560101", "540101"}
    ledger = sum(
        value for account, value in snap.month_accounts.items() if account in salary_accounts
    )
    adjustment = ledger - controlled
    periods = []
    for period, records in sorted(period_rows.items()):
        total = sum(row["metric_cost"] for row in records)
        correction_ids = [row["calculation_id"] for row in records if row["sign"] < 0]
        values = {
            "gross_salary_fen": 0,
            "employer_social_insurance_fen": 0,
            "employer_housing_fund_fen": 0,
            "employee_social_insurance_fen": 0,
            "employee_housing_fund_fen": 0,
        }
        for row in records:
            out = row["basis"]["outcome"]["values"]
            values["gross_salary_fen"] += row["sign"] * out.get("gross_fen", 0)
            obligations = {item["name"]: item["amount_fen"] for item in out.get("obligations", ())}
            for side in ("employer", "employee"):
                for field, suffix in (("social", "social_insurance"), ("housing", "housing_fund")):
                    values[f"{side}_{suffix}_fen"] += row["sign"] * obligations.get(
                        f"{side}_{field}", 0
                    )
        periods.append(
            {
                "payroll_period": period,
                "total_fen": total,
                "has_reversal": bool(correction_ids),
                "has_amendment": any(row["basis"]["fact"]["revision"] > 1 for row in records),
                "correction_ids": correction_ids,
                **values,
            }
        )
    unknown = sum(item["in_period"] is None for item in all_items)
    employee_cost = {
        "has_activity": bool(rows) or ledger != 0,
        "breakdown_available": adjustment == 0,
        "reason": None if adjustment == 0 else "账面人工成本包含未能按人员分配的调整，请查看凭证。",
        "total_fen": ledger,
        "controlled_total_fen": controlled,
        "settlement_adjustment_fen": adjustment,
        "prior_period_settlement_adjustment_fen": 0,
        "batch_count": len({row["basis"]["subject_id"] for row in rows}),
        "periods": periods,
        **{
            key: sums[key]
            for key in (
                "gross_salary_fen",
                "annual_bonus_fen",
                "employer_social_insurance_fen",
                "employer_housing_fund_fen",
                "employee_social_insurance_fen",
                "employee_housing_fund_fen",
            )
        },
        "personal_withholding_fen": sums["personal_deduction_fen"],
    }
    if unestablished_employees:
        employee_cost.update(
            breakdown_available=False,
            reason="部分员工来源的冻结采用未能证明，保留已证明月度账面金额与精确追溯。",
            controlled_total_fen=None,
            settlement_adjustment_fen=None,
        )
    labor_rows = metric_rows(
        snap.month_journal.select(kinds=LABOR_KINDS),
        ("gross_fen", "tax_fen", "theoretical_tax_fen", "unwithheld_tax_fen", "withholding_method"),
    )
    labor_periods = defaultdict(list)
    for row in labor_rows:
        labor_periods[row["basis"]["fact"]["data"]["period"]].append(row)

    def labor_sum(key):
        numbers = [row["basis"]["outcome"]["values"].get(key) for row in labor_rows]
        return (
            None
            if any(value is None for value in numbers)
            else sum(row["sign"] * value for row, value in zip(labor_rows, numbers, strict=True))
        )

    modes = sorted({row["basis"]["outcome"]["values"]["withholding_method"] for row in labor_rows})
    gross = labor_sum("gross_fen") or 0
    labor = {
        "has_activity": bool(labor_rows),
        "breakdown_available": True,
        "reason": None,
        "total_fen": gross,
        "gross_remuneration_fen": gross,
        "theoretical_withholding_tax_fen": labor_sum("theoretical_tax_fen"),
        "booked_withholding_tax_fen": labor_sum("tax_fen"),
        "unwithheld_tax_fen": labor_sum("unwithheld_tax_fen"),
        "withholding_status": "none"
        if not labor_rows
        else "not_withheld"
        if any(mode != "net_after_withholding" for mode in modes)
        else "booked",
        "withholding_note": "尚未记录扣缴及申报；未从理论税額推定实际完成。"
        if "not_withheld_not_filed" in modes
        else "已确认按毛额支付、未扣税。"
        if "gross_paid_without_withholding" in modes
        else "按核算事实确认扣税义务，实际付款及申报分别查看。",
        "settlement_modes": modes,
        "batch_count": len({row["basis"]["subject_id"] for row in labor_rows}),
        "periods": [
            {
                "remuneration_period": period,
                "total_fen": sum(
                    row["sign"] * row["basis"]["outcome"]["values"]["gross_fen"] for row in records
                ),
                "gross_remuneration_fen": sum(
                    row["sign"] * row["basis"]["outcome"]["values"]["gross_fen"] for row in records
                ),
                "theoretical_withholding_tax_fen": None
                if any(
                    row["basis"]["outcome"]["values"].get("theoretical_tax_fen") is None
                    for row in records
                )
                else sum(
                    row["sign"] * row["basis"]["outcome"]["values"]["theoretical_tax_fen"]
                    for row in records
                ),
                "has_reversal": any(row["sign"] < 0 for row in records),
                "has_amendment": any(row["basis"]["fact"]["revision"] > 1 for row in records),
                "correction_ids": [row["calculation_id"] for row in records if row["sign"] < 0],
            }
            for period, records in sorted(labor_periods.items())
        ],
    }
    labor_items = []
    labor_calculations = list(snap.calculations_of_kind(*LABOR_KINDS, "labor_project_cost"))
    entity_sources = {
        calc["subject_id"]
        for calc in wage_calculations
        if employee_id is not None and wage_scalars[calc["fact_id"]]["employee_id"] == employee_id
    }
    if employee_id in unestablished_employees:
        entity_sources.update(
            item["subject_id"]
            for item in unestablished_employees[employee_id]["candidate_selections"]
        )
    if employee_id is not None:
        labor_scalar = scalar_facts(snap, labor_calculations)
        entity_sources.update(
            calc["subject_id"]
            for calc in labor_calculations
            if labor_scalar[calc["fact_id"]]["person_id"] == employee_id
        )
    labor_keys, labor_page = page_keys(
        [
            calc["id"]
            for calc in sorted(
                labor_calculations, key=lambda calc: (calc["period"], calc["subject_id"])
            )
        ],
        cursors.get("labor_sources"),
        limit,
    )
    displayed_labor = (
        [calc for calc in labor_calculations if calc["id"] in set(labor_keys)]
        if "labor_sources" in requested and not summary_only
        else []
    )
    snap.prepare_settlements({calc["subject_id"] for calc in displayed_labor})
    for calc in displayed_labor:
        if calc["kind"] not in LABOR_KINDS | {"labor_project_cost"}:
            continue
        data, values = calc["fact"]["data"], calc["outcome"]["values"]
        labor_items.append(
            {
                "source_id": calc["subject_id"],
                "calculation_id": calc["id"],
                "period": data["period"],
                "person_id": data["person_id"],
                **snap.party_field(data["person_id"], "name"),
                "capitalized": calc["kind"] == "labor_project_cost",
                "project_id": data.get("project_id"),
                "gross_fen": values["gross_fen"],
                "net_fen": values["net_fen"],
                "booked_tax_fen": values["tax_fen"],
                "theoretical_tax_fen": values.get("theoretical_tax_fen"),
                "withholding_method": values["withholding_method"],
                "withholding_label": {
                    "not_withheld_not_filed": "尚未记录扣缴及申报",
                    "gross_paid_without_withholding": "已按毛额付款、未扣税",
                    "net_after_withholding": "已确认净额及扣税义务",
                }[values["withholding_method"]],
                **_source_settlement(snap, calc, limit=limit),
            }
        )
    capitalized_labor = sum(
        row["sign"] * row["basis"]["outcome"]["values"]["capitalized_fen"]
        for row in metric_rows(
            snap.month_journal.select(kinds={"labor_project_cost"}), ("capitalized_fen",)
        )
        if row["basis"]["kind"] == "labor_project_cost"
    )
    labor["items"] = sorted(labor_items, key=lambda item: (item["period"], item["source_id"]))
    return {
        "employees": {
            **{
                key: None if unestablished_employees else sums[key]
                for key in (*money_keys, "personal_deduction_fen")
            },
            "unestablished_count": len(unestablished_employees),
            "registered_count": len(all_items),
            "in_period_count": sum(item["in_period"] is True for item in all_items),
            "unknown_period_count": unknown,
            "payroll_count": sum(item["has_payroll_activity"] for item in all_items),
            "without_payroll_count": sum(
                item["in_period"] is True
                and not item["has_payroll_activity"]
                and item["selection_status"] != "unestablished"
                for item in all_items
            ),
            "profile_missing_count": sum(
                item["in_period"] is True and not item["profile_available"] for item in all_items
            ),
            "contributions_only_count": sum(
                item["wage_tax_scope"] == "contributions_only" for item in all_items
            ),
            "controlled_cost_fen": None if unestablished_employees else controlled,
            "settlement_adjustment_fen": None if unestablished_employees else adjustment,
            "ledger_cost_fen": ledger,
            "detail_reconciled": None if unestablished_employees else adjustment == 0,
            "breakdown_available": not unestablished_employees and adjustment == 0,
            "breakdown_reason": employee_cost["reason"],
            "items": items,
            "identity_note": (
                "在册状态和入离职日期仅展示已确认的管理资料，不决定工资核算资格或个税起点。"
            ),
        },
        "_entity_sources": entity_sources,
        "collections": {
            "employees": {"items": items, "page": employee_page},
            "labor_sources": {"items": labor["items"], "page": labor_page},
            **(
                {
                    "payroll_sources": {
                        "items": wage_sources[employee_id],
                        "page": source_pages.get(employee_id, page_keys([], limit=limit)[1]),
                    }
                }
                if employee_id
                else {}
            ),
        },
        "workforce_cost": {
            "has_activity": employee_cost["has_activity"]
            or labor["has_activity"]
            or capitalized_labor != 0,
            "total_fen": ledger + gross,
            "capitalized_labor_fen": capitalized_labor,
            "employee": employee_cost,
            "personal_labor": labor,
        },
    }


def _assets(
    snap,
    *,
    sections=None,
    cursors=None,
    limit=100,
    asset_filter="all",
    asset_id=None,
    project_id=None,
    summary_only=False,
):
    cursors = cursors or {}
    requested = set(sections) if sections is not None else {"assets", "projects"}
    monthly_acquired, monthly_adjusted = defaultdict(int), defaultdict(int)
    acquired_rows = metric_rows(
        snap.month_journal.select(kinds={"asset", "reimbursed_asset", "reimbursed_asset_batch"}), ()
    )
    batch_fact_ids = {
        row["basis"]["fact_id"]
        for row in acquired_rows
        if row["basis"]["kind"] == "reimbursed_asset_batch"
    }
    batch_amounts = defaultdict(list)
    for record in (
        snap.connection.execute(
            "SELECT a.revision_id,a.asset_type,sum(a.cost_fen) cost_fen "
            "FROM fact_reimbursed_asset_batch_assets a "
            "JOIN json_each(?) ids ON a.revision_id=ids.value GROUP BY a.revision_id,a.asset_type",
            (json.dumps(sorted(batch_fact_ids)),),
        )
        if batch_fact_ids
        else ()
    ):
        batch_amounts[record["revision_id"]].append(dict(record))
    for row in acquired_rows:
        calc = row["basis"]
        if calc["kind"] not in {"asset", "reimbursed_asset", "reimbursed_asset_batch"}:
            continue
        data = calc["fact"]["data"]
        amounts = monthly_acquired if data["period"] == snap.period else monthly_adjusted
        for detail in (
            batch_amounts[calc["fact_id"]] if calc["kind"] == "reimbursed_asset_batch" else (data,)
        ):
            amounts[detail["asset_type"]] += row["sign"] * detail["cost_fen"]
    acquisitions = {
        calc["subject_id"]: calc
        for calc in snap.calculations_of_kind(*ASSET_KINDS)
        if calc["kind"] in ASSET_KINDS
    }
    unestablished_assets = snap.calculations.unestablished_entities(
        kinds={
            *ASSET_KINDS,
            "reimbursed_asset_batch",
            "asset_activation",
            "asset_disposal",
            "asset_consumption",
        },
        field="asset_id",
    )
    # Scalar card facts are sufficient for domain totals; growing provenance is loaded below.
    batches = list(snap.calculations_of_kind("reimbursed_asset_batch"))
    scalar = scalar_facts(snap, [*acquisitions.values(), *batches])
    batch_by_fact = {calc["fact_id"]: calc for calc in batches}
    asset_details = {}
    for record in (
        snap.connection.execute(
            "SELECT a.* FROM fact_reimbursed_asset_batch_assets a JOIN json_each(?) ids "
            "ON a.revision_id=ids.value ORDER BY a.revision_id,a.item_no",
            (json.dumps(sorted(batch_by_fact)),),
        )
        if batch_by_fact
        else ()
    ):
        detail = dict(record)
        if detail["asset_id"] not in acquisitions:
            acquisitions[detail["asset_id"]] = batch_by_fact[detail["revision_id"]]
            asset_details[detail["asset_id"]] = detail
    activations, disposals = {}, {}
    lifecycle = list(snap.calculations_of_kind("asset_activation", "asset_disposal"))
    lifecycle_scalar = scalar_facts(snap, lifecycle)
    for calc in lifecycle:
        if calc["kind"] == "asset_activation":
            activations[lifecycle_scalar[calc["fact_id"]]["asset_id"]] = calc
        elif calc["kind"] == "asset_disposal":
            disposals[lifecycle_scalar[calc["fact_id"]]["asset_id"]] = calc
    charges, monthly_charges, latest_charge = defaultdict(int), defaultdict(int), {}
    if "asset_consumption" in snap.store.registry.models:
        query, parameters = snap.journal.select(kinds={"asset_consumption"}).sql()
        for row in snap.connection.execute(
            "SELECT f.asset_id,sum((CASE WHEN j.reverses_id IS NULL THEN 1 ELSE -1 END)*"
            "json_extract(c.outcome,'$.values.consumption_fen')) AS total,"
            "sum(CASE WHEN j.period=? THEN (CASE WHEN j.reverses_id IS NULL THEN 1 ELSE -1 END)*"
            "json_extract(c.outcome,'$.values.consumption_fen') ELSE 0 END) AS monthly,"
            f"max(j.period) latest FROM ({query}) j JOIN calculation c "
            "ON c.id=j.basis_calculation_id JOIN fact_asset_consumption f "
            "ON f.revision_id=c.fact_id GROUP BY f.asset_id",
            [snap.month, *parameters],
        ):
            charges[row["asset_id"]] = row["total"]
            monthly_charges[row["asset_id"]] = row["monthly"]
            latest_charge[row["asset_id"]] = str(YearMonth.from_ordinal(row["latest"]))
    all_items = []
    for ident, calc in sorted(acquisitions.items()):
        data = scalar[calc["fact_id"]] | asset_details.get(ident, {})
        activation, disposal = activations.get(ident), disposals.get(ident)
        opening = calc["kind"] == "opening_asset"
        active = (opening or activation is not None) and disposal is None
        status = (
            "active"
            if active
            else "disposed"
            if disposal and data["asset_type"] == "fixed"
            else "retired"
            if disposal
            else "pending_activation"
        )
        accumulated = data.get("accumulated_fen", 0) + charges[ident]
        all_items.append(
            {
                "asset_id": ident,
                "asset_type": data["asset_type"],
                "status": status,
                "cost_fen": data["cost_fen"],
                "accumulated_charge_fen": accumulated,
                "month_charge_fen": monthly_charges[ident],
                "book_value_fen": 0 if disposal else data["cost_fen"] - accumulated,
                "month_acquired": not opening and data["period"] == snap.period,
                "month_activated": bool(
                    activation and lifecycle_scalar[activation["fact_id"]]["period"] == snap.period
                ),
                "month_exited": bool(
                    disposal and lifecycle_scalar[disposal["fact_id"]]["period"] == snap.period
                ),
            }
        )
    established_totals = {item["asset_id"]: item for item in all_items}
    all_items = [item for item in all_items if item["asset_id"] not in unestablished_assets]
    all_items.extend(
        {
            "asset_id": ident,
            "asset_type": proof["asset_type"],
            "status": "unestablished",
            "cost_fen": None,
            "accumulated_charge_fen": None,
            "month_charge_fen": None,
            "book_value_fen": None,
            "month_acquired": False,
            "month_activated": False,
            "month_exited": False,
        }
        for ident, proof in unestablished_assets.items()
    )
    all_items.sort(key=lambda item: item["asset_id"])
    filtered = [
        row["asset_id"]
        for row in all_items
        if (
            asset_filter == "all"
            or asset_filter == "active"
            and row["status"] == "active"
            or asset_filter in {"fixed", "intangible"}
            and row["asset_type"] == asset_filter
            or asset_filter == "pending"
            and row["status"] == "pending_activation"
            or asset_filter == "exited"
            and row["status"] in {"disposed", "retired"}
        )
        and (asset_id is None or row["asset_id"] == asset_id)
    ]
    selected, asset_page = page_keys(
        filtered, cursors.get("assets"), limit, total_count=len(all_items)
    )
    selected = set(selected) if not summary_only and "assets" in requested else set()
    entity_sources = set()
    if asset_id in unestablished_assets:
        entity_sources.update(
            item["subject_id"] for item in unestablished_assets[asset_id]["candidate_selections"]
        )
    if asset_id in acquisitions:
        entity_sources.add(acquisitions[asset_id]["subject_id"])
        data = scalar[acquisitions[asset_id]["fact_id"]] | asset_details.get(asset_id, {})
        if data.get("acceptance_id"):
            entity_sources.add(data["acceptance_id"])
        acquisition = acquisitions[asset_id]
        if "project_sources" in snap.store.registry.models[acquisition["kind"]].model_fields:
            from .schema import table_name

            entity_sources.update(
                row[0]
                for row in snap.connection.execute(
                    f"SELECT source_id FROM {table_name(acquisition['kind'])}_project_sources "
                    "WHERE revision_id=?",
                    (acquisition["fact_id"],),
                )
            )
        entity_sources.update(
            calc["subject_id"]
            for calc in lifecycle
            if lifecycle_scalar[calc["fact_id"]]["asset_id"] == asset_id
        )
        consumptions = list(snap.calculations_for_entity("asset_consumption", "asset_id", asset_id))
        consumption_scalar = scalar_facts(snap, consumptions)
        entity_sources.update(
            calc["subject_id"]
            for calc in consumptions
            if consumption_scalar[calc["fact_id"]]["asset_id"] == asset_id
        )
    settlement_subjects = {
        calc["subject_id"] for ident, calc in acquisitions.items() if ident in selected
    }
    snap.prepare_settlements(settlement_subjects)
    items = []
    for index, (ident, calc) in enumerate(sorted(acquisitions.items()), 1):
        if ident not in selected:
            continue
        data = calc["fact"]["data"] | asset_details.get(ident, {})
        profile = snap.profile("asset", ident)
        fixed = data["asset_type"] == "fixed"
        opening = calc["kind"] == "opening_asset"
        activation = activations.get(ident)
        basis = data if opening else (activation or {}).get("fact", {}).get("data", {})
        disposal = disposals.get(ident)
        cost = data["cost_fen"]
        accumulated = data.get("accumulated_fen", 0) + charges[ident]
        active = (opening or activation is not None) and disposal is None
        status = (
            "active"
            if active
            else "disposed"
            if disposal and fixed
            else "retired"
            if disposal
            else "pending_activation"
        )
        acquisition_day = data.get("acquisition_date")
        acceptance = snap.calculations.get(data.get("acceptance_id"))
        if acquisition_day is None and acceptance:
            acquisition_day = acceptance["fact"]["data"].get("acquisition_date")
        creditors = data.get("creditors") or (
            acceptance["fact"]["data"].get("creditors", ()) if acceptance else ()
        )
        source_subject = acceptance["subject_id"] if acceptance else calc["subject_id"]
        source_rows = [
            row
            for row in snap.journal.select(subjects={source_subject})
            if row["basis"]["subject_id"] == source_subject and row["sign"] > 0
        ]
        batch_source = acceptance or (calc if calc["kind"] == "reimbursed_asset_batch" else None)
        source_calculations = (
            [snap.calculations[source["source_id"]] for source in data.get("project_sources", ())]
            if data.get("project_sources")
            else []
            if opening
            else [batch_source or calc]
        )
        source_scope = (
            "成本来源结算（不分摊为本资产付款）"
            if data.get("project_sources")
            else "本验收批次结算"
            if batch_source
            else "本资产结算"
        )
        source_parties = [
            snap.party_details(party_id)
            for party_id in (
                [creditor["employee_id"] for creditor in creditors]
                if creditors
                else [data["supplier_id"]]
                if data.get("supplier_id")
                else []
            )
        ]
        item = {
            "asset_id": ident,
            "asset_type": data["asset_type"],
            "code": profile.get("display_number") or f"资产 {index}",
            "name": profile.get("display_name") or "未提供资产名称",
            "category": data["asset_type"],
            "category_label": profile.get("category_label")
            or ("固定资产" if fixed else "无形资产"),
            "field_sources": {
                **_display_sources(
                    profile,
                    code="display_number",
                    name="display_name",
                    category_label="category_label",
                    **(
                        {}
                        if fixed
                        else {
                            "life_basis_label": "useful_life_basis",
                            "life_basis_explanation": "note",
                            "rights_description": "rights_description",
                        }
                    ),
                ),
                "source_parties": [
                    source
                    for party in source_parties
                    for source in _source_list(party.get("field_sources", {}).get("name"))
                ],
            },
            "status": status,
            "status_label": {
                "active": "使用中",
                "disposed": "已处置",
                "retired": "已终止",
                "pending_activation": "待启用",
            }[status],
            "acquisition_date": acquisition_day,
            "posting_period": str(YearMonth.from_ordinal(calc["posting_period"])),
            "recognition_label": "期初接续"
            if opening
            else acquisition_day or f"{data['period']}（按月确认）",
            "source_label": "期初接续"
            if opening
            else "项目形成"
            if data.get("project_sources")
            else "整批验收"
            if batch_source
            else "报销承接"
            if "reimbursed" in calc["kind"]
            else "直接购入",
            "source_party_label": "本验收批次债权人"
            if batch_source
            else "报销债权人"
            if creditors
            else "供应方",
            "source_parties": "、".join(party["name"] for party in source_parties)
            if source_parties
            else None,
            "settlement_scope": source_scope,
            "settlements": [
                {
                    "source_id": source["subject_id"],
                    "label": _name(source["kind"]),
                    **_source_settlement(snap, source, limit=limit),
                }
                for source in source_calculations
            ],
            "cost_fen": cost,
            "accumulated_charge_fen": accumulated,
            "month_charge_fen": monthly_charges[ident],
            "book_value_fen": 0 if disposal else cost - accumulated,
            "latest_charge_period": latest_charge.get(ident),
            "benefit_area_label": {
                "administration": "管理",
                "sales": "销售",
                "service": "服务",
            }.get(basis.get("benefit_area")),
            "useful_life_months": basis.get("useful_life_months"),
            "acquisition_reference": str(source_rows[-1]["number"])
            if source_rows
            else "期初"
            if opening
            else "暂无凭证",
            "month_acquired": not opening and data["period"] == snap.period,
            "month_activated": bool(
                activation and activation["fact"]["data"]["period"] == snap.period
            ),
            "month_exited": bool(disposal and disposal["fact"]["data"]["period"] == snap.period),
        }
        if fixed:
            item.update(
                {
                    "in_service_date": basis.get("in_use_date"),
                    "residual_value_fen": basis.get("residual_fen"),
                    "depreciation_method_label": "直线法" if basis else None,
                    "rounding_policy_label": {
                        "floor_final_remainder": "整分均摊、末期调整",
                        "round_half_up_card": "四舍五入、末期调整",
                    }.get(basis.get("rounding_policy")),
                    "disposal": None,
                }
            )
        else:
            item.update(
                {
                    "available_for_use_date": basis.get("in_use_date"),
                    "life_basis_label": profile.get("useful_life_basis") or "未提供",
                    "life_basis_explanation": profile.get("note") or "",
                    "rights_description": profile.get("rights_description") or "未提供",
                    "retirement": None,
                }
            )
        if disposal:
            disposed, values = disposal["fact"]["data"], disposal["outcome"]["values"]
            references = [
                row
                for row in snap.journal.select(subjects={disposal["subject_id"]})
                if row["basis"]["id"] == disposal["id"] and row["sign"] > 0
            ]
            detail = {
                "date": disposed["disposal_date"],
                "book_value_fen": cost - accumulated,
                "reference": str(references[-1]["number"]) if references else "暂无凭证",
                "settlement": _source_settlement(snap, disposal, limit=limit),
            }
            if fixed:
                detail.update(
                    {
                        "kind": "sale" if disposed["disposal_kind"] == "sale" else "retirement",
                        "gross_proceeds_fen": disposed["gross_proceeds_fen"],
                        "gain_fen": max(values["gain_loss_fen"], 0),
                        "loss_fen": max(-values["gain_loss_fen"], 0),
                        **snap.party_field(disposed.get("buyer_id")),
                    }
                )
            item["disposal" if fixed else "retirement"] = detail
        items.append(item)
    established_cards = {item["asset_id"]: item for item in items}
    items = [item for item in items if item["asset_id"] not in unestablished_assets]
    for ident in sorted(selected & unestablished_assets.keys()):
        proof = unestablished_assets[ident]
        items.append(
            {
                "asset_id": ident,
                "asset_type": proof["asset_type"],
                "name": snap.profile("asset", ident).get("display_name") or "资产来源采用待核对",
                "selection_status": "unestablished",
                "candidate_selections": proof["candidate_selections"],
                "trace_targets": proof["trace_targets"],
                "cost_fen": None,
                "accumulated_charge_fen": None,
                "month_charge_fen": None,
                "book_value_fen": None,
                **(
                    {"established_card": established_cards[ident]}
                    if ident in established_cards
                    else {}
                ),
            }
        )
    items.sort(key=lambda item: item["asset_id"])
    fixed_items = [item for item in all_items if item["asset_type"] == "fixed"]
    intangible_items = [item for item in all_items if item["asset_type"] == "intangible"]

    def summary(rows, fixed):
        active = [row for row in rows if row["status"] == "active"]
        result = {
            "registered_count": len(rows),
            "unestablished_count": sum(row["status"] == "unestablished" for row in rows),
            "active_count": len(active),
            "items": [
                item for item in items if item["asset_type"] == ("fixed" if fixed else "intangible")
            ],
            "active_cost_fen": sum(row["cost_fen"] for row in active),
            "active_accumulated_fen": sum(row["accumulated_charge_fen"] for row in active),
            "active_net_fen": sum(row["book_value_fen"] for row in active),
            "pending_count": sum(row["status"] == "pending_activation" for row in rows),
            "pending_cost_fen": sum(
                row["cost_fen"] for row in rows if row["status"] == "pending_activation"
            ),
            "month_acquired_count": sum(row["month_acquired"] for row in rows),
            "month_acquired_fen": monthly_acquired["fixed" if fixed else "intangible"],
            "month_cost_adjustment_fen": monthly_adjusted["fixed" if fixed else "intangible"],
            "month_depreciation_fen" if fixed else "month_amortization_fen": _nullable_sum(
                row["month_charge_fen"] for row in rows
            ),
        }
        if fixed:
            result.update(
                {
                    "pending_count": sum(row["status"] == "pending_activation" for row in rows),
                    "pending_cost_fen": sum(
                        row["cost_fen"] for row in rows if row["status"] == "pending_activation"
                    ),
                    "disposed_count": sum(row["status"] == "disposed" for row in rows),
                    "month_activated_count": sum(row["month_activated"] for row in rows),
                    "month_disposed_count": sum(row["month_exited"] for row in rows),
                }
            )
        else:
            result["retired_count"] = sum(row["status"] == "retired" for row in rows)
            result["month_retired_count"] = sum(row["month_exited"] for row in rows)
        return result

    fixed, intangible = summary(fixed_items, True), summary(intangible_items, False)
    balances = snap.accounts
    project_balances = defaultdict(int)
    # The same frozen project effects are reached through their posted cost
    # accounts, including exact reversals, before any outcome JSON is examined.
    query, parameters = snap.journal.select(accounts={"189901", "4301"}).sql()
    for row in snap.connection.execute(
        "SELECT json_extract(e.value,'$.key') AS key,"
        "sum((CASE WHEN j.reverses_id IS NULL THEN 1 ELSE -1 END)*"
        "json_extract(e.value,'$.amount')) AS amount FROM (" + query + ") j "
        "JOIN calculation c ON c.id=j.basis_calculation_id "
        "JOIN json_each(c.outcome,'$.balances') e "
        "WHERE json_extract(e.value,'$.key') LIKE 'project-cost:%' GROUP BY key",
        parameters,
    ):
        project_balances[row["key"]] += row["amount"]
    project_calculations = list(snap.calculations_of_kind("project_cost", "labor_project_cost"))
    project_scalar = scalar_facts(snap, project_calculations)
    if project_id is not None:
        entity_sources.update(
            calc["subject_id"]
            for calc in project_calculations
            if project_scalar[calc["fact_id"]]["project_id"] == project_id
        )
    candidates = sorted(
        (
            calc
            for calc in project_calculations
            if project_balances[f"project-cost:{calc['subject_id']}"] != 0
        ),
        key=lambda calc: (project_scalar[calc["fact_id"]]["project_id"], calc["subject_id"]),
    )
    project_cost = sum(
        project_balances[f"project-cost:{calc['subject_id']}"] for calc in candidates
    )
    project_keys, project_page = page_keys(
        [
            calc["id"]
            for calc in candidates
            if project_id is None or project_scalar[calc["fact_id"]]["project_id"] == project_id
        ],
        cursors.get("projects"),
        limit,
        total_count=len(candidates),
    )
    picked_projects = set(project_keys) if "projects" in requested and not summary_only else set()
    snap.prepare_settlements(
        {calc["subject_id"] for calc in candidates if calc["id"] in picked_projects}
    )
    projects = [
        {
            "source_id": calc["subject_id"],
            "project_id": calc["outcome"]["values"]["project_id"],
            "period": calc["fact"]["data"]["period"],
            "kind": calc["kind"],
            "label": _name(calc["kind"]),
            **snap.party_field(
                calc["fact"]["data"].get("person_id") or calc["fact"]["data"].get("supplier_id")
            ),
            "cost_fen": calc["outcome"]["values"]["capitalized_fen"],
            "remaining_fen": project_balances[f"project-cost:{calc['subject_id']}"],
            "settlement": _source_settlement(snap, calc, limit=limit),
        }
        for calc in candidates
        if calc["id"] in picked_projects
    ]
    ledger_cost = sum(balances[account] for account in ("1601", "1701", "1604", "189901", "4301"))
    ledger_accumulated = -balances["1602"] - balances["1702"]
    card_cost = (
        sum(
            item["cost_fen"]
            for item in all_items
            if item["status"] in {"active", "pending_activation"}
        )
        + project_cost
    )
    card_accumulated = fixed["active_accumulated_fen"] + intangible["active_accumulated_fen"]
    differences = {
        "cost_fen": ledger_cost - card_cost,
        "accumulated_fen": ledger_accumulated - card_accumulated,
        "net_fen": ledger_cost - ledger_accumulated - card_cost + card_accumulated,
    }
    reconciled = not any(differences.values())
    if unestablished_assets:
        for asset_type, result in (("fixed", fixed), ("intangible", intangible)):
            if any(
                proof["asset_type"] in (None, asset_type) for proof in unestablished_assets.values()
            ):
                for field in (
                    "active_cost_fen",
                    "active_accumulated_fen",
                    "active_net_fen",
                    "pending_cost_fen",
                ):
                    result[field] = None
        differences = dict.fromkeys(differences, None)
        reconciled = None
    return {
        "fixed_asset_cost_fen": balances["1601"],
        "accumulated_depreciation_fen": -balances["1602"],
        "fixed_asset_net_fen": balances["1601"] + balances["1602"],
        "intangible_asset_cost_fen": balances["1701"],
        "accumulated_amortization_fen": -balances["1702"],
        "intangible_asset_net_fen": balances["1701"] + balances["1702"],
        "active_count": fixed["active_count"] + intangible["active_count"],
        "registered_count": len(all_items),
        "unestablished_count": len(unestablished_assets),
        "ledger_cost_fen": ledger_cost,
        "ledger_accumulated_fen": ledger_accumulated,
        "ledger_net_fen": ledger_cost - ledger_accumulated,
        "active_ledger_net_fen": balances["1601"] + balances["1701"] - ledger_accumulated,
        "pending_intangible_count": intangible["pending_count"],
        "pending_intangible_cost_fen": intangible["pending_cost_fen"],
        "project_cost_fen": project_cost,
        "projects": sorted(projects, key=lambda item: (item["project_id"], item["source_id"])),
        "reconciliation_scope": "在用及待启用资产、尚未转出项目成本",
        "card_cost_fen": None if unestablished_assets else card_cost,
        "card_accumulated_fen": None if unestablished_assets else card_accumulated,
        "card_net_fen": None if unestablished_assets else card_cost - card_accumulated,
        "established_card_totals": {
            "registered_count": len(established_totals),
            "cost_fen": sum(item["cost_fen"] for item in established_totals.values()),
            "accumulated_charge_fen": sum(
                item["accumulated_charge_fen"] for item in established_totals.values()
            ),
            "book_value_fen": sum(item["book_value_fen"] for item in established_totals.values()),
        }
        if unestablished_assets
        else None,
        "pending_fixed_count": fixed["pending_count"],
        "pending_fixed_cost_fen": fixed["pending_cost_fen"],
        "month_charge_fen": _nullable_sum(row["month_charge_fen"] for row in all_items),
        "month_acquired_count": sum(row["month_acquired"] for row in all_items),
        "month_acquired_fen": sum(monthly_acquired.values()),
        "month_cost_adjustment_fen": sum(monthly_adjusted.values()),
        "month_activated_count": sum(row["month_activated"] for row in all_items),
        "month_exited_count": sum(row["month_exited"] for row in all_items),
        "reconciled": reconciled,
        "reconciliation_label": "资产卡片与账面一致" if reconciled else "资产卡片与账面差异需核对",
        "differences": differences,
        "fixed": fixed,
        "intangible": intangible,
        "_entity_sources": entity_sources,
        "collections": {
            "assets": {"items": items, "page": asset_page},
            "projects": {"items": projects, "page": project_page},
        },
    }


def _quarterly_view(plan, closed, details=None, carry_forward_fact_id=None):
    ready = plan["status"] == "ready"
    exportable = closed["status"] == "ready"
    statements = plan["statements"]
    specifications = (
        (
            "balance_sheet",
            "资产负债表",
            (("ending_fen", "期末余额"), ("beginning_fen", "年初余额")),
            {15, 29, 30, 41, 47, 52, 53},
        ),
        (
            "profit_statement",
            "利润表",
            (("current_fen", "本季金额"), ("year_to_date_fen", "本年累计")),
            {19, 21, 30, 32},
        ),
        (
            "cash_flow_statement",
            "现金流量表",
            (("current_fen", "本季金额"), ("year_to_date_fen", "本年累计")),
            {7, 13, 19, 20, 22},
        ),
    )
    views = [
        {
            "key": key,
            "label": label,
            "columns": [{"key": field, "label": title} for field, title in columns],
            "rows": [
                {
                    "line": int(line),
                    "name": row["name"],
                    "values": {field: row[field] for field, _ in columns},
                    "is_total": int(line) in totals,
                    "has_amount": any(row[field] for field, _ in columns),
                }
                for line, row in sorted(statements[key].items(), key=lambda item: int(item[0]))
            ],
        }
        for key, label, columns, totals in specifications
    ]
    details = details or {}
    issues = details.get("issues", plan["fact_issues"])

    def issue_context(issue):
        labels = [issue.get("period", "")]
        if issue.get("voucher_number") is not None:
            labels.append(f"凭证 {issue['voucher_number']}")
        if issue.get("line_no") is not None:
            labels.append(f"第 {issue['line_no']} 行")
        if issue.get("account"):
            labels.append(f"科目 {issue['account']}")
        return " · ".join(value for value in labels if value)

    readiness = [
        {
            "key": str(index),
            "label": "报表资料核对",
            "state": "pending",
            "summary": issue["message"],
            "details": [
                {
                    "primary": issue_context(issue) or "请核对相应业务资料",
                    "secondary": "",
                    "location": issue,
                }
            ],
        }
        for index, issue in enumerate(issues)
    ]
    if not readiness:
        readiness = [
            {
                "key": "ready",
                "label": "报表资料核对",
                "state": "pass",
                "summary": "现有来源具备报表展示条件",
                "details": [],
            }
        ]
    labels = {
        "balance_ending_fen": "期末资产负债平衡",
        "balance_beginning_fen": "年初资产负债平衡",
        "profit_current_fen": "本季利润勾稽",
        "profit_year_to_date_fen": "累计利润勾稽",
        "cash_current_fen": "本季现金流量勾稽",
        "cash_year_to_date_fen": "累计现金流量勾稽",
        "cash_ending_current_fen": "本季现金余额勾稽",
        "cash_ending_year_to_date_fen": "累计现金余额勾稽",
    }
    return {
        "schema_version": 1,
        "close_state": details.get("close_state", "closed" if exportable else "open"),
        "readiness_state": "ready" if ready else "blocked",
        "carry_forward": {
            "selected_fact_id": carry_forward_fact_id,
            "options": details.get("carry_forward_options", []),
        },
        "status": "ready" if exportable else "in_progress" if ready else "blocked",
        "status_label": "可导出" if exportable else "期间尚未关账" if ready else "资料待核对",
        "headline": "季度财务报表",
        "message": "报表来源已核对" if ready else "请根据核对事项检查对应业务和资料来源。",
        "checked_at": datetime.now(UTC).isoformat(),
        "organization": plan["organization"],
        "period": plan["period"]
        | {"label": f"{plan['period']['year']} 年第 {plan['period']['quarter']} 季度"},
        "readiness": readiness,
        "summary": {
            "assets_total_fen": statements["balance_sheet"]["30"]["ending_fen"],
            "liabilities_equity_total_fen": statements["balance_sheet"]["53"]["ending_fen"],
            "current_net_profit_fen": statements["profit_statement"]["32"]["current_fen"],
            "year_to_date_net_profit_fen": statements["profit_statement"]["32"]["year_to_date_fen"],
            "current_cash_change_fen": statements["cash_flow_statement"]["20"]["current_fen"],
            "ending_cash_fen": statements["cash_flow_statement"]["22"]["current_fen"],
        },
        "statements": views,
        "checks": {
            "passed": sum(check["passed"] is True for check in plan["checks"]),
            "total": len(plan["checks"]),
            "items": [
                check | {"label": labels.get(check["code"], "报表勾稽核对")}
                for check in plan["checks"]
            ],
        },
        "draft": not exportable,
        "export": {
            "available": exportable,
            "file_name": _workbook_name(plan),
            "calculation_hash": closed["digest"] if exportable else None,
            "preview_digest": closed["digest"] if exportable else None,
            "epochs": closed["epochs"] if exportable else None,
        },
        "technical": {
            "calculation_hash": plan["digest"],
            "template": plan["template"] | {"file_name": plan["template"]["name"]},
            "rule": plan["rule"],
            "source_close_hashes": [row["digest"] for row in plan["source_closes"]],
            "classification_count": details.get("classification_count", 0),
            "income_tax_confirmation_count": details.get("income_tax_confirmation_count", 0),
            "requirement_codes": list(dict.fromkeys(issue["field"] for issue in issues)),
            "errors": [],
        },
    }
