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

from .account_definitions import (
    ACCOUNT_NAMES,
    KNOWN_POSITION_ACCOUNTS,
    PROFIT_ACCOUNTS,
    RECLASS,
)
from .business_queries import BusinessQueries, business_display_amount
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
    adopted_head_metadata,
    metric_rows,
    page_keys,
    payroll_head_identities,
    payroll_head_metadata,
    scalar_facts,
    verified_scalar_facts,
)
from .domains.money import FUNDS_ACCOUNT_TYPE_BY_BALANCE_CATEGORY
from .provenance import recorded_times
from .query_reads import QueryReads
from .query_semantics import classify_financial_position, report_party_splits
from .reports import Reports, _workbook_name
from .types import ActualDate, YearMonth, canonical, digest

KIND_NAMES = {
    "external_completion": "外部办理完成依据",
    "external_basis_review": "外部办理与账务核对",
    "payroll_disbursement_basis": "工资代发金额依据",
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
    "managed_reserve_expense": "备用金支出",
    "managed_reserve_refund": "备用金退款",
    "payroll_reserve_payment": "净薪及备用金支出付款",
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
    "asset_activation_batch": "资产批次启用",
    "asset_consumption_month": "月度折旧摊销",
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
PAYROLL_KINDS = {"payroll", "payroll_bounded", "annual_bonus"}
LABOR_KINDS = {"labor", "labor_accrual"}
ASSET_KINDS = {"asset", "reimbursed_asset", "opening_asset"}
ASSET_LIFECYCLE_KINDS = {
    *ASSET_KINDS,
    "asset_activation",
    "asset_consumption",
    "asset_disposal",
}
ASSET_BATCH_OWNER_KINDS = {"asset_activation_batch", "asset_consumption_month"}
FUND_TYPES = FUNDS_ACCOUNT_TYPE_BY_BALANCE_CATEGORY


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
    def __init__(self, engine, connection, period, reads=None):
        from .business_queries import _today_china

        self.engine = engine
        self.store, self.connection, self.period = engine.store, connection, period
        self.month = YearMonth(period).ordinal
        self.as_of = _today_china()
        self.epochs = self.store.epochs(connection)
        from .read_state import repair_revision

        self.read_repair_revision = repair_revision(connection)
        self.reads = reads or QueryReads(engine, connection)
        self.queries = BusinessQueries(engine, reads=self.reads)
        self.closes = ClosedPeriods(self)
        self.close = self.closes.get(self.month)
        self.fact_cache, self.calculation_cache = {}, {}
        self.relation_cache, self.evidence_cache = {}, {}
        self.semantic_cache = {}
        self.settlement_summaries = {}
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
                    "read_repair_revision": self.read_repair_revision,
                    "closes": sorted(self.closes),
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()

    def release(self):
        """Drop helper views and their back-references at the read boundary."""
        self.__dict__.clear()

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
            kinds={kind for kind in self.store.registry.models if kind.startswith("opening_")},
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

    def calculations_of_kind(self, *kinds, posting_period=None):
        """Select by kind; posting_period narrows to one publication month.

        Callers that filter on ``posting_period`` afterwards must pass it here:
        the unrestricted selection materialises every historical calculation of
        these kinds and its whole close-adoption graph before the filter runs.
        """
        return self.calculations.selected(kinds=set(kinds), posting_period=posting_period).values()

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

    def asset_member_events(self, *, kinds=None, subjects=None, asset_ids=None):
        """Reuse adoption evidence already read within this snapshot."""
        cache = getattr(self, "_asset_member_events", None)
        if cache is None:
            cache = self._asset_member_events = {}
        key = (
            None if kinds is None else frozenset(kinds),
            None if subjects is None else frozenset(subjects),
            None if asset_ids is None else frozenset(asset_ids),
        )
        if key not in cache:
            if (None, None, None) in cache:
                cache[key] = [
                    event
                    for event in cache[None, None, None]
                    if (kinds is None or event["kind"] in kinds)
                    and (subjects is None or event["subject_id"] in subjects)
                    and (asset_ids is None or event["asset_id"] in asset_ids)
                ]
            else:
                cache[key] = self.queries._selected_asset_members(
                    self.connection,
                    self.period,
                    kinds=kinds,
                    subjects=subjects,
                    asset_ids=asset_ids,
                )
        return cache[key]

    def prepare_settlements(self, subjects):
        if not hasattr(self, "settlement_cache"):
            self.settlement_cache = {}
            self.settlement_current_cache = {}
        missing = set(subjects) - self.settlement_cache.keys()
        if missing:
            result = self.settlement_summary(subject_ids=missing)
            current = self.settlement_summary(subject_ids=missing, current=True)
            for subject in missing:
                self.settlement_cache[subject] = result
                self.settlement_current_cache[subject] = current

    def settlement_summary(self, *, subject_ids=None, current=False):
        key = (current, None if subject_ids is None else frozenset(subject_ids))
        if key not in self.settlement_summaries:
            self.settlement_summaries[key] = self.queries.settlement_summary(
                self.connection, self.period, subject_ids=subject_ids, current=current
            )
        return self.settlement_summaries[key]

    @cached_property
    def preparation(self):
        parallel_checks = getattr(self, "_brief_parallel_checks", None)
        return self.queries._period_readiness(
            self.connection, self.period,
            as_of=self.as_of if parallel_checks is not None else None,
            summary=True,
            _allow_frozen_materials=True,
            _parallel_checks=parallel_checks,
        )

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
        parameters = [json.dumps(kinds)]
        period_clause = " AND f.period=?" if period is not None else ""
        if period is not None:
            parameters.append(YearMonth(period).ordinal)
        parameters.extend([self.month] * 4)
        rows = self.connection.execute(
            "WITH scoped AS MATERIALIZED (SELECT f.id,f.subject_id,f.revision,f.period "
            "FROM subject s CROSS JOIN fact_revision f ON f.subject_id=s.id "
            "WHERE s.kind IN (SELECT value FROM json_each(?))"
            + period_clause
            + "), candidates AS (SELECT f.id,f.subject_id,f.revision FROM scoped f "
            "WHERE ((f.period<=? AND EXISTS(SELECT 1 FROM fact_current a WHERE a.fact_id=f.id) "
            "AND NOT EXISTS(SELECT 1 FROM period_close p WHERE p.period=f.period)) OR EXISTS("
            "SELECT 1 FROM close_reference r WHERE r.reference_type='fact' "
            "AND r.reference_id=f.id AND r.close_period<=?) OR EXISTS("
            "SELECT 1 FROM calculation c INDEXED BY calculation_subject "
            "CROSS JOIN close_reference r ON r.reference_id=c.id "
            "AND r.reference_type='calculation' WHERE c.subject_id=f.subject_id "
            "AND c.fact_id=f.id AND r.close_period<=?) "
            "OR EXISTS(SELECT 1 FROM dependency_fact d JOIN close_reference r "
            "ON r.reference_id=d.calculation_id AND r.reference_type='calculation' "
            "WHERE d.fact_id=f.id AND r.close_period<=?))) "
            "SELECT id FROM (SELECT *,row_number() OVER(PARTITION BY subject_id "
            "ORDER BY revision DESC) AS n FROM candidates) WHERE n=1",
            parameters,
        )
        return [row[0] for row in rows]

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
                and item["role"] not in {"funds", "tax_transfer", "reserve_expense"}
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

    @staticmethod
    def asset_identities(calc):
        """Return only card identities explicitly carried by this typed calculation."""
        kind, fact = calc["kind"], calc["fact"]
        data = fact["data"]
        identities = []
        if kind in ASSET_KINDS:
            identities.append((data["asset_id"], data.get("asset_type")))
        elif kind in ASSET_LIFECYCLE_KINDS:
            identities.append(
                (
                    data.get("asset_id") or calc["outcome"].get("values", {}).get("asset_id"),
                    data.get("asset_type") or calc["outcome"].get("values", {}).get("asset_type"),
                )
            )
        elif kind == "reimbursed_asset_batch":
            identities.extend(
                (item.get("asset_id"), item.get("asset_type")) for item in data.get("assets", ())
            )
        if not identities:
            return ()

        accounts = {line.get("account") for line in calc["outcome"].get("lines", ())}
        account_type = (
            "intangible"
            if accounts & {"1701", "1702", "189901"}
            else "fixed"
            if accounts & {"1601", "1602", "1604"}
            else None
        )
        result = []
        for asset_id, asset_type in identities:
            if not isinstance(asset_id, str) or not asset_id:
                continue
            resolved_type = asset_type if asset_type in {"fixed", "intangible"} else account_type
            if resolved_type not in {"fixed", "intangible"}:
                # Every supported fact has a typed source. Keep the response contract
                # stable if an older zero-line calculation omitted the derived type.
                resolved_type = "fixed"
            item = (asset_id, resolved_type)
            if item not in result:
                result.append(item)
        return tuple(result)

    def asset_references(self, calc):
        references = []
        for asset_id, asset_type in self.asset_identities(calc):
            profile = self.profile("asset", asset_id)
            references.append(
                {
                    "asset_id": asset_id,
                    "asset_type": asset_type,
                    "name": profile.get("display_name"),
                    "code": profile.get("display_number"),
                    "field_sources": _display_sources(
                        profile, name="display_name", code="display_number"
                    ),
                }
            )
        return references

    def asset_reference(self, calc):
        references = self.asset_references(calc)
        return references[0] if len(references) == 1 else None

    def asset_member_contributions(self, row):
        """Resolve an owner voucher's frozen members once for display and profile priming."""
        if row["basis"]["kind"] not in ASSET_BATCH_OWNER_KINDS:
            return None, ()
        cached = row.get("_dashboard_asset_members")
        if cached is not None:
            return cached
        frozen_owner = row["voucher_calculation_id"]
        if row["reverses_id"]:
            frozen_owner = self.connection.execute(
                "SELECT calculation_id FROM voucher_version WHERE id=?",
                (row["reverses_id"],),
            ).fetchone()[0]
        members = tuple(
            (member, self.calculation(member["member_calculation_id"]))
            for member in self.reads.asset_members(frozen_owner)
        )
        cached = (frozen_owner, members)
        row["_dashboard_asset_members"] = cached
        return cached

    @staticmethod
    def asset_reference_label(asset):
        name, code = asset.get("name"), asset.get("code")
        if name and code:
            return f"{name}（{code}）"
        if name:
            return name
        if code:
            return f"资产卡片 {code}"
        return f"资产卡片 {asset['asset_id']}"

    def asset_references_label(self, assets):
        unique = list({item["asset_id"]: item for item in assets}.values())
        if not unique:
            return ""
        if len(unique) == 1:
            return self.asset_reference_label(unique[0])
        labels = "、".join(self.asset_reference_label(item) for item in unique[:2])
        suffix = "等" if len(unique) > 2 else ""
        return f"{len(unique)} 张资产卡片：{labels}{suffix}"

    def business_summary(
        self, calc, sign=1, relations=None, *, include_sources=False, asset_references=()
    ):
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
            "asset": "购置资产",
            "asset_activation": "启用资产",
            "asset_consumption": "折旧摊销",
            "asset_disposal": "处置资产",
            "asset_activation_batch": "启用整批资产",
            "asset_consumption_month": "计提月度折旧摊销",
            "reimbursed_asset_batch": "验收整批资产",
            "reimbursed_asset": "确认报销资产",
            "opening_asset": "接续资产卡片",
            "labor": "确认劳务报酬",
            "labor_accrual": "确认劳务报酬",
            "labor_project_cost": "确认项目劳务",
            "project_cost": "确认项目投入",
            "managed_reserve_expense": "支出备用金",
            "managed_reserve_refund": "收到备用金退款",
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
        if asset_references and kind == "asset_consumption":
            short = "计提摊销" if asset_references[0]["asset_type"] == "intangible" else "计提折旧"
        elif kind == "asset_disposal":
            short = "出售资产" if data.get("disposal_kind") == "sale" else "报废资产"
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
        asset_label = self.asset_references_label(asset_references)
        detail = (
            f"{short}（{when}）"
            + (f" · {asset_label}" if asset_label else "")
            + (f" · {who}" if who else "")
        )
        if kind in ASSET_BATCH_OWNER_KINDS and not asset_references:
            count = calc["outcome"].get("values", {}).get("member_count")
            if type(count) is int:
                detail += f" · {count} 张资产卡片"
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
            for asset_reference in asset_references:
                for field in ("name", "code"):
                    source = asset_reference["field_sources"].get(field)
                    sources.extend(
                        source if isinstance(source, list) else [source] if source else []
                    )
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
        return business_display_amount(calc)

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
        asset_references = self.asset_references(calc)
        asset_reference = asset_references[0] if len(asset_references) == 1 else None
        asset_members = []
        asset_lines = {}
        member_evidence = set()
        if kind in ASSET_BATCH_OWNER_KINDS:
            # A reviewed owner may reuse a voucher. Its displayed line sources
            # remain those of the actual voucher, including a reversal's source.
            frozen_owner, contributions = self.asset_member_contributions(row)
            for member, contribution in contributions:
                reference = self.asset_reference(contribution)
                if reference is None:
                    raise KernelError(
                        "dashboard_asset_reference_missing",
                        "资产批次成员缺少精确资产卡片身份",
                    )
                member_amount, member_label = self.business_amount(contribution)
                detail = {
                    **reference,
                    "calculation_id": contribution["id"],
                    "owner_calculation_id": frozen_owner,
                    "amount_fen": (
                        row["sign"] * member_amount if member_amount is not None else None
                    ),
                    "amount_label": member_label,
                    "line_start": member["line_start"],
                    "line_count": member["line_count"],
                }
                asset_members.append(detail)
                member_evidence.update(contribution["fact"]["evidence"])
                if member["line_start"] is not None:
                    for number in range(
                        member["line_start"], member["line_start"] + member["line_count"]
                    ):
                        asset_lines[number] = detail
        elif kind == "reimbursed_asset_batch" and len(asset_references) > 1:
            costs = {
                item["asset_id"]: item["cost_fen"]
                for item in data.get("assets", ())
                if isinstance(item.get("asset_id"), str) and type(item.get("cost_fen")) is int
            }
            asset_members = [
                {
                    **reference,
                    "calculation_id": calc["id"],
                    "owner_calculation_id": calc["id"],
                    "amount_fen": row["sign"] * costs[reference["asset_id"]],
                    "amount_label": "已确认资产成本",
                    "line_start": None,
                    "line_count": 0,
                }
                for reference in asset_references
                if reference["asset_id"] in costs
            ]
        summary_asset_references = asset_references or asset_members
        short_summary, summary, summary_sources = self.business_summary(
            calc,
            row["sign"],
            relations,
            include_sources=True,
            asset_references=summary_asset_references,
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
        evidence = set(fact["evidence"]) | member_evidence
        dependency_fact_ids = row.get("_voucher_dependency_facts")
        if dependency_fact_ids is None:
            dependency_fact_ids = (
                ref[0]
                for ref in self.connection.execute(
                    "SELECT fact_id FROM dependency_fact WHERE calculation_id=?", (calc["id"],)
                )
            )
        for fact_id in dependency_fact_ids:
            evidence.update(self.fact(fact_id)["evidence"])
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
            },
            "recognition": recognition,
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
            "asset": asset_reference,
            **({"asset_members": asset_members} if asset_members else {}),
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
                    "source_label": (
                        self.asset_reference_label(asset_lines[line["line_no"]])
                        if line["line_no"] in asset_lines
                        else self.line_source(line, relations)
                    ),
                    **(
                        {"asset": asset_lines[line["line_no"]]}
                        if line["line_no"] in asset_lines
                        else {}
                    ),
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
        # Step between actual indexed periods before checking their current
        # heads. Listing twelve months must not materialize every fact and
        # voucher identity accumulated during those months.
        values = {
            row[0]
            for row in connection.execute(
                "WITH RECURSIVE fact_periods(period) AS ("
                "SELECT min(period) FROM fact_revision WHERE period>0 UNION ALL "
                "SELECT (SELECT min(period) FROM fact_revision WHERE period>p.period) "
                "FROM fact_periods p WHERE p.period IS NOT NULL),"
                "voucher_periods(period) AS ("
                "SELECT min(period) FROM voucher_version WHERE period>0 UNION ALL "
                "SELECT (SELECT min(period) FROM voucher_version WHERE period>p.period) "
                "FROM voucher_periods p WHERE p.period IS NOT NULL) "
                "SELECT p.period FROM voucher_periods p WHERE EXISTS("
                "SELECT 1 FROM voucher_version v INDEXED BY voucher_period "
                "JOIN voucher_current c ON c.version_id=v.id WHERE v.period=p.period) "
                "UNION SELECT period FROM period_close UNION "
                "SELECT p.period FROM fact_periods p WHERE EXISTS("
                "SELECT 1 FROM fact_revision f INDEXED BY fact_period "
                "JOIN fact_current c ON c.fact_id=f.id WHERE f.period=p.period) "
                "UNION SELECT period FROM material_revision"
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
        with QueryReads.snapshot(self.engine) as reads:
            connection = reads.connection
            if period is None:
                periods = self._periods(connection)
                period = periods[0]["key"] if periods else None
            if period is not None:
                try:
                    month = YearMonth(period).ordinal
                except ValueError as exc:
                    raise KernelError("invalid_command", "会计月份格式不正确") from exc
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
            snapshot = _Snapshot(self.engine, connection, period, reads) if period else None
            try:
                yield snapshot
            finally:
                # Page projections are fully materialized before this scope
                # exits. Their helper views point back to the snapshot, so
                # release its ownership now instead of retaining entire read
                # graphs until a later cyclic-GC collection.
                if snapshot is not None:
                    snapshot.release()

    def context(self):
        with QueryReads.snapshot(self.engine) as reads:
            connection = reads.connection
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
            coverage = reports.closed_period_coverages(
                quarter_keys, connection=connection, reads=reads
            )
            quarters = [
                {
                    "key": f"{year}-Q{quarter}",
                    "year": year,
                    "quarter": quarter,
                    "label": f"{year} 年第 {quarter} 季度",
                    "complete": coverage[year, quarter]["complete"],
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

    def _response(self, snapshot, data):
        from .business_queries import _today_china

        if snapshot:
            snapshot.attach_recorded_times()
            read_context = self._read_context(snapshot.connection, snapshot.as_of)
        else:
            with self.store.connection(read_only=True) as connection:
                read_context = self._read_context(connection, _today_china())
        return {
            "schema_version": 7,
            "snapshot_version": snapshot.snapshot_version if snapshot else None,
            "selected_period": _period_view(snapshot.period, bool(snapshot.close))
            if snapshot
            else None,
            "read_semantics": {
                "knowledge": "current_knowledge",
                "accounting": "as_posted",
                "business_basis": (
                    "frozen_adoption" if snapshot and snapshot.close else "current_known"
                ),
                "display": "frozen_with_current_supplements"
                if snapshot and snapshot.close
                else "current",
                "system_time_replay": False,
                "recorded_at": "system_recording_time",
                "recording_period": "business_recording_period",
                "recorded_later": "business_recording_period_after_selected_period",
            },
            "read_context": read_context,
            "data": data,
        }

    def _read_context(self, connection, as_of):
        from .engine import PROGRAM_VERSION
        from .read_state import repair_revision

        identity = dict(
            connection.execute("SELECT company_id,database_id FROM identity WHERE id=1").fetchone()
        )
        epochs = self.store.epochs(connection)
        context = {**identity, "as_of": str(ActualDate(as_of))}
        return {
            **context,
            "read_version": digest(
                {
                    "protocol": "dashboard-deferred-read-v1",
                    "build": PROGRAM_VERSION,
                    **context,
                    "epochs": epochs,
                    "read_repair_revision": repair_revision(connection),
                }
            ).hex(),
        }

    def period_preparation(
        self, period: YearMonth, *, expected_read_version: str, as_of: ActualDate
    ):
        from .business_queries import _today_china

        period, as_of = str(YearMonth(period)), str(ActualDate(as_of))
        with QueryReads.snapshot(self.engine) as reads:
            current_as_of = _today_china()
            context = self._read_context(reads.connection, current_as_of)
            if as_of != current_as_of or expected_read_version != context["read_version"]:
                raise KernelError(
                    "dashboard_snapshot_changed", "资料或核对日期已变化，请刷新主数据后重新核对。"
                )
            prepared = BusinessQueries(self.engine, reads=reads)._period_readiness(
                reads.connection, period, as_of=as_of, summary=True, _allow_frozen_materials=True
            )
            return {
                "schema_version": 4,
                "projection": "dashboard_period_preparation_result",
                "read_context": context,
                "period": period,
                "data": {
                    "period_preparation": preparation_view(prepared),
                    "brief_checks": _brief_checks(prepared),
                },
            }

    def brief(
        self,
        period: str | None = None,
        *,
        limit: int = 100,
        expected_version: str | None = None,
        section: str | None = None,
        cursor: str | None = None,
        voucher_version_id: str | None = None,
        voucher_number: int | None = None,
        preparation: Literal["complete", "deferred"] = "complete",
        _parallel_attempt=None,
    ):
        if preparation not in {"complete", "deferred"}:
            raise KernelError("invalid_command", "不支持的准备检查投影")
        validate_page("brief", section, cursor, limit)
        if voucher_version_id is not None and voucher_number is not None:
            raise KernelError("invalid_command", "精确凭证定位须只提供一个身份")
        if voucher_number is not None and (type(voucher_number) is not int or voucher_number < 1):
            raise KernelError("invalid_command", "凭证编号须为正整数")
        with self._snapshot(period) as snap:
            if snap is None:
                response = self._response(None, None)
                if preparation == "deferred":
                    response.update(projection="dashboard_brief_deferred")
                return response
            self._check_page_version(snap, cursor, expected_version)
            parallel_started = (
                _parallel_attempt is not None and _parallel_attempt.start(snap)
            )
            if parallel_started:
                snap._brief_parallel_checks = _parallel_attempt
            # Brief consumes the page count, kind groups and line totals in the
            # same snapshot; select their narrow shared aggregate once.
            snap.month_journal.prime_summary()
            after = (
                decode_cursor(snap, "brief", section, cursor, {})
                if section != "file_jobs"
                else None
            )
            if section in {None, "vouchers"}:
                rows, page = snap.month_journal.page(after or 0, limit)
            else:
                rows, page = [], page_keys([], limit=limit, total_count=len(snap.month_journal))[1]
            focused_row = None
            if voucher_version_id is not None or voucher_number is not None:
                found, _ = snap.month_journal.page(
                    0, 1, voucher_number=voucher_number, voucher_version_id=voucher_version_id
                )
                if not found:
                    raise KernelError("dashboard_voucher_not_found", "所选月份没有这张精确凭证")
                focused_row = found[0]
            profile_rows = [*rows, *([focused_row] if focused_row else [])]
            calculation_ids = {row["basis_calculation_id"] for row in profile_rows}
            snap.reads.prime_calculations(calculation_ids, ancestors=True)
            snap.reads.voucher_lines(row["id"] for row in profile_rows)
            asset_ids = set()
            member_ids = set()
            for row in profile_rows:
                asset_ids.update(asset_id for asset_id, _ in snap.asset_identities(row["basis"]))
                _, contributions = snap.asset_member_contributions(row)
                for _, contribution in contributions:
                    member_ids.add(contribution["id"])
                    asset_ids.update(
                        asset_id for asset_id, _ in snap.asset_identities(contribution)
                    )
            snap.reads.prime_calculations(member_ids, ancestors=True)
            resolutions = snap.reads.relations_many(calculation_ids)
            subjects = {snap.calculation(ident)["subject_id"] for ident in calculation_ids}
            snap.metadata.prime_profiles("business", subjects)
            snap.management.prime(subjects)
            party_ids, fund_account_ids = set(), set()
            for ident in calculation_ids:
                calc = snap.calculation(ident)
                data = calc["fact"]["data"]
                calc_parties = {
                    data[field]
                    for field in (
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
                    if data.get(field)
                }
                if data.get("payment_method") == "bank_batch":
                    calc_parties.discard(data.get("counterparty_id"))
                for field in ("allocations", "sources"):
                    calc_parties.update(
                        item["recipient_id"]
                        for item in data.get(field, ())
                        if item.get("recipient_id")
                    )
                party_ids.update(calc_parties)
                fund_account_ids.update(
                    effect["key"]
                    for effect in calc["outcome"].get("balances", ())
                    if effect["category"] in FUND_TYPES
                )
            for resolution in resolutions.values():
                party_ids.update(
                    item["creditor_id"]
                    for item in resolution["obligations"]
                    if item.get("creditor_id")
                )
                party_ids.update(
                    item["recipient_id"]
                    for item in resolution["line_relations"]
                    if item.get("recipient_id")
                )
                for item in resolution["obligations"]:
                    source_id = item.get("source_calculation_id")
                    if source_id:
                        source_data = snap.calculation(source_id)["fact"]["data"]
                        party_ids.update(
                            source_data[field]
                            for field in ("employee_id", "person_id")
                            if source_data.get(field)
                        )
            party_ids.discard("payroll-group")
            snap.metadata.prime_profiles("employee", party_ids)
            snap.metadata.prime_profiles("counterparty", party_ids)
            snap.metadata.prime_profiles("fund_account", fund_account_ids)
            if asset_ids:
                snap.metadata.prime_profiles("asset", asset_ids)
            dependencies = defaultdict(list)
            for row in snap.connection.execute(
                "SELECT d.calculation_id,d.fact_id FROM json_each(?) ids "
                "JOIN dependency_fact d ON d.calculation_id=ids.value "
                "ORDER BY d.calculation_id,d.fact_id",
                (canonical(sorted(calculation_ids)),),
            ):
                dependencies[row["calculation_id"]].append(row["fact_id"])
            snap.reads.facts(fact_id for fact_ids in dependencies.values() for fact_id in fact_ids)
            for row in profile_rows:
                row["_voucher_dependency_facts"] = tuple(dependencies[row["basis_calculation_id"]])
            vouchers = [snap.voucher(row) for row in rows]
            focused = snap.voucher(focused_row) if focused_row else None
            from .close_review import business_adopted_basis

            page_calculation_ids = [voucher["calculation_id"] for voucher in vouchers]
            adopted_basis = {
                "scope": "current_voucher_page",
                "calculation_ids": page_calculation_ids,
                **business_adopted_basis(
                    snap.connection,
                    self.engine,
                    page_calculation_ids,
                    reads=snap.reads,
                ),
            }
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
                                "asset": v["asset"],
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
            funds, workforce_cost, assets = (
                _funds(snap, summary_only=True),
                _brief_workforce_cost(snap),
                _long_term_assets(snap),
            )
            bank = funds["bank_statement"]
            unmatched = bank["unmatched_totals"]
            # Let the workers run while this snapshot completes independent
            # commentary and open-item reads. The ordinary path keeps its
            # existing order and one-transaction proof reuse.
            early_open_items = (
                _open_items(
                    snap,
                    after=after if section == "open_items" else None,
                    limit=limit,
                    summary_only=section not in {None, "open_items"},
                )
                if parallel_started else None
            )
            if parallel_started:
                _parallel_attempt.supply_position_obligations(snap)
                _ = snap.commentary
            prepared = snap.preparation if preparation == "complete" else None
            checks = _brief_checks(prepared)
            journal_totals = snap.month_journal.totals()
            debit, credit = journal_totals["debit"], journal_totals["credit"]
            position = _parallel_attempt.position() if parallel_started else _position(snap)
            position["bank_calculation"] = {
                "opening_fen": funds["bank_opening_fen"],
                "inflow_fen": funds["bank_inflow_fen"],
                "outflow_fen": funds["bank_outflow_fen"],
            }
            valid = debit == credit and position["equation_valid"]
            attention = (
                checks["attention_count"]
                + int(valid is None)
                + unmatched["count"]
                + int(bank["coverage_state"] in {"missing", "partial"})
            )
            commentary = snap.commentary.get("current")

            def commentary_item(item):
                if item is None:
                    return None
                return {
                    key: item[key]
                    for key in (
                        "id",
                        "period",
                        "revision",
                        "text",
                        "context_digest",
                        "close_digest",
                        "source",
                        "evidence_digest",
                        "digest",
                        "supplementary",
                        "content_validity",
                    )
                    if key in item
                }

            commentary_details = {
                "status": snap.commentary["status"],
                "current": commentary_item(snap.commentary.get("current")),
                "frozen": commentary_item(snap.commentary.get("frozen")),
                "latest": commentary_item(snap.commentary.get("latest")),
                "supplements": [
                    commentary_item(item) for item in snap.commentary.get("supplements", ())
                ],
            }
            data = {
                "generated_at": datetime.now(UTC).isoformat(),
                "management_commentary": (commentary or {}).get("text", ""),
                "management_commentary_details": commentary_details,
                "material_completeness": checks["material_completeness"],
                "period_preparation": preparation_view(prepared) if prepared is not None else None,
                "voucher_count": len(snap.month_journal),
                "line_count": journal_totals["line_count"],
                "total_debit_fen": debit,
                "total_credit_fen": credit,
                "focused_voucher": focused,
                "adopted_basis": adopted_basis,
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
                        "missing_account_count",
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
                "open_items": early_open_items if parallel_started else _open_items(
                    snap,
                    after=after if section == "open_items" else None,
                    limit=limit,
                    summary_only=section not in {None, "open_items"},
                ),
                "workforce_cost": workforce_cost,
                "long_term_assets": assets,
                "validation": {
                    "state": "error"
                    if valid is False
                    else "attention"
                    if attention
                    else "pending"
                    if preparation == "deferred"
                    else "complete",
                    "title": "账务核对",
                    "summary": "账务汇总平衡"
                    if valid is True
                    else "财务位置依据不完整"
                    if valid is None
                    else "账务汇总需核对",
                    "integrity_valid": valid,
                    "voucher_balanced": debit == credit,
                    "issues": checks["issues"],
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
                        *checks["items"],
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
            response = self._response(snap, seal_collections(snap, "brief", data, {}))
            if preparation == "deferred":
                response.update(
                    projection="dashboard_brief_deferred",
                    read_context=self._read_context(snap.connection, snap.as_of),
                )
            return response

    def funds(
        self,
        period: str | None = None,
        *,
        movement_account_type: str | None = None,
        movement_account_id: str | None = None,
        statement_account_id: str | None = None,
        limit: int = 100,
        expected_version: str | None = None,
        section: str | None = None,
        cursor: str | None = None,
        preparation: Literal["complete", "deferred"] = "complete",
    ):
        validate_page("funds", section, cursor, limit)
        if preparation not in {"complete", "deferred"}:
            raise KernelError("invalid_command", "不支持的准备检查投影")
        if (movement_account_type is None) != (movement_account_id is None):
            raise KernelError("invalid_command", "资金账户筛选须同时提供账户类别与账户标识")
        if movement_account_type is not None and movement_account_type not in FUND_TYPES.values():
            raise KernelError("invalid_command", "不支持的资金账户类别")
        if movement_account_id == "" or statement_account_id == "":
            raise KernelError("invalid_command", "资金账户标识不能为空")
        filters = {
            "movement_account_type": movement_account_type,
            "movement_account_id": movement_account_id,
            "statement_account_id": statement_account_id,
        }
        with self._snapshot(period) as snap:
            if snap is None:
                if cursor:
                    raise KernelError(
                        "dashboard_snapshot_changed", "所选公司或期间已变化，请重新加载明细。"
                    )
                return self._response(None, None)
            self._check_page_version(snap, cursor, expected_version)
            cursors = {section: cursor} if section and cursor is not None else {}
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
            data["period_preparation"] = (
                preparation_view(snap.preparation) if preparation == "complete" else None
            )
            return self._response(snap, seal_collections(snap, "funds", data, filters))

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
        preparation: Literal["complete", "deferred"] = "complete",
    ):
        validate_page("employees", section, cursor, limit)
        if preparation not in {"complete", "deferred"}:
            raise KernelError("invalid_command", "不支持的准备检查投影")
        if employee_id == "" or section == "settlement_events" and employee_id is None:
            raise KernelError("invalid_command", "员工清偿明细须指定有效 employee_id")
        if employee_filter not in {"all", "in_period", "payroll", "no_payroll", "unknown", "ended"}:
            raise KernelError("invalid_command", "不支持的员工筛选")
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
                    raise KernelError("invalid_command", "员工清偿明细须指定 employee_id")
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
            data["period_preparation"] = (
                preparation_view(snap.preparation) if preparation == "complete" else None
            )
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
        preparation: Literal["complete", "deferred"] = "complete",
    ):
        validate_page("assets", section, cursor, limit)
        if preparation not in {"complete", "deferred"}:
            raise KernelError("invalid_command", "不支持的准备检查投影")
        if (
            asset_id == ""
            or project_id == ""
            or (
                section in {"source_history", "settlement_events"}
                and asset_id is None
                and project_id is None
            )
        ):
            raise KernelError(
                "invalid_command", "资产来源或清偿明细须指定有效 asset_id 或 project_id"
            )
        if asset_filter not in {"all", "active", "fixed", "intangible", "pending", "exited"}:
            raise KernelError("invalid_command", "不支持的资产筛选")
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
                    raise KernelError(
                        "invalid_command", "资产来源或清偿明细须指定 asset_id 或 project_id"
                    )
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
            data["period_preparation"] = (
                preparation_view(snap.preparation) if preparation == "complete" else None
            )
            return self._response(snap, seal_collections(snap, "assets", data, filters))

    def _external_collection(self, snap, after, limit):
        from .workflow import LABELS, Workflow

        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("每页数量必须为 1 至 500")
        scope = (
            "FROM subject s JOIN fact_current f ON f.subject_id=s.id "
            "WHERE s.kind='external_obligation'"
        )
        total = snap.connection.execute("SELECT count(*) " + scope).fetchone()[0]
        if after is not None and snap.connection.execute(
            "SELECT 1 " + scope + " AND s.id=?", (after,)
        ).fetchone() is None:
            raise KernelError("dashboard_snapshot_changed", "分页位置已变化，请重新加载明细。")
        rows = list(
            snap.connection.execute(
                "SELECT s.id "
                + scope
                + (" AND s.id>?" if after is not None else "")
                + " ORDER BY s.id LIMIT ?",
                (*((after,) if after is not None else ()), limit + 1),
            )
        )
        more = len(rows) > limit
        keys = [row[0] for row in rows[:limit]]
        page = {
            "total_count": total,
            "filtered_count": total,
            "returned_count": len(keys),
            "has_more": more,
            "next_cursor": keys[-1] if more else None,
        }
        items = Workflow(self.engine)._external_obligations(
            snap.connection,
            snap.period,
            snap.as_of,
            reads=snap.reads,
            obligation_ids=set(keys),
        )
        return {
            "items": [
                {
                    "obligation_id": item["id"],
                    "kind": item["kind"],
                    "label": LABELS[item["kind"]],
                    "start_period": item["start_period"],
                    "end_period": item["end_period"],
                    "actual_completion_status": item["actual_completion_status"],
                    "basis_review_status": item["basis_review_status"],
                    "basis_review_calculation_id": item["basis_review_calculation_id"],
                    "due_date": item["due_date"],
                    "issue_count": len(item["basis_issues"]),
                    "fact_issues": item["basis_issues"],
                }
                for item in items
            ],
            "page": page,
        }

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
            raise KernelError("invalid_command", "清偿口径须为 historical 或 current")
        with self._snapshot(period) as snap:
            self._check_page_version(snap, cursor, expected_version)
            data = snap.queries._business_status(
                snap.connection,
                subject_id,
                period,
                as_of=as_of,
                summary=True,
                include_payroll_confirmation=True,
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
            response["schema_version"] = 5
            return response

    def quarterly_report(
        self,
        year: int,
        quarter: int,
        *,
        carry_forward_fact_id: str | None = None,
        preparation: Literal["complete", "deferred"] = "complete",
    ):
        from .business_queries import _today_china

        if (
            type(year) is not int
            or not 1 <= year <= 9999
            or type(quarter) is not int
            or quarter not in range(1, 5)
        ):
            raise KernelError("invalid_command", "报表年份或季度不正确")
        if preparation not in {"complete", "deferred"}:
            raise KernelError("invalid_command", "不支持的准备检查投影")

        reports = Reports(self.engine)
        with QueryReads.snapshot(self.engine) as reads:
            connection = reads.connection
            as_of = _today_china()
            opened = reports._report(
                year,
                quarter,
                source="open",
                connection=connection,
                reads=reads,
                carry_forward_fact_id=carry_forward_fact_id,
            )
            quarter_end = YearMonth(f"{year:04d}-{quarter * 3:02d}").ordinal
            if (
                connection.execute(
                    "SELECT 1 FROM period_close WHERE period=?", (quarter_end,)
                ).fetchone()
                is None
            ):
                # The open plan has already checked the visible closed sources.
                # An absent quarter-end close cannot produce an exportable plan.
                closed = {
                    "status": "needs_information",
                    "fact_issues": [{"field": "period"}],
                }
            else:
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
                reports.browser_report_details(plan, closed, connection=connection, reads=reads),
                carry_forward_fact_id,
            )
            response["read_context"] = self._read_context(connection, as_of)
            if preparation == "deferred":
                response.update(
                    projection="dashboard_quarterly_report_deferred",
                    period_preparations=None,
                )
            else:
                queries = BusinessQueries(self.engine, reads=reads)
                response["period_preparations"] = [
                    preparation_view(
                        queries._period_readiness(
                            connection,
                            f"{year}-{month:02}",
                            summary=True,
                            _allow_frozen_materials=True,
                        )
                    )
                    for month in range(quarter * 3 - 2, quarter * 3 + 1)
                ]
            return response


def _brief_checks(prepared):
    """The same checking projection for complete and deferred brief responses."""
    material, issues = None, []
    followups = prepared["current_followups"] if prepared is not None else None
    if followups is not None:
        materials = followups["materials"]
        material = {
            "closed": prepared["closure"]["state"] != "open",
            "satisfied": materials["status"] == "ready",
            "issues": materials["issues"],
            "coverage_digest": materials["coverage"]["coverage_digest"],
        }
        seen = set()
        for group in ("accounting", "close_requirements"):
            for issue in followups[group]["issues"]:
                key = json.dumps(issue, sort_keys=True, ensure_ascii=False)
                if key not in seen:
                    issues.append(issue)
                    seen.add(key)
        order_failure = (prepared["readiness"] or {}).get("order_failure")
        if order_failure:
            issues.insert(0, {
                "field": "close_order",
                "code": order_failure["code"],
                "message": order_failure["message"],
                **({"period": order_failure["details"]["period"]}
                   if "period" in order_failure["details"] else {}),
            })
    return {
        "material_completeness": material,
        "issues": issues,
        "attention_count": len(material["issues"]) + len(issues) if material else 0,
        "items": [
            {
                "key": "materials",
                "label": "资料完整性",
                "state": "pass" if material and material["satisfied"] else "pending",
                "text": "按逐项资料检查器核对",
            },
            {
                "key": "accounting",
                "label": "核算准备",
                "state": "pass"
                if followups and followups["accounting"]["status"] == "ready"
                else "pending",
                "text": "包括应建业务、正式发布及待复核状态",
            },
            {
                "key": "close_requirements",
                "label": "期间准备",
                "state": "pass" if prepared is not None and not issues else "pending",
                "text": "已关闭期间的当前跟进不改变原冻结结论"
                if material and material["closed"]
                else "按关账业务检查核对",
            },
        ],
    }


def _missing_adopted_report_classification_ids(connection, month):
    """Find frozen classification heads whose typed body is absent."""

    # Materialize only the missing typed heads before checking adoption. An
    # old replaced or withdrawn head still matters if a close adopted it.
    return {
        row[0]
        for row in connection.execute(
            "WITH missing AS MATERIALIZED ("
            "SELECT f.id FROM subject s INDEXED BY subject_kind "
            "CROSS JOIN fact_revision f ON f.subject_id=s.id "
            "LEFT JOIN fact_report_classification c ON c.revision_id=f.id "
            "WHERE s.kind='report_classification' AND c.revision_id IS NULL) "
            "SELECT m.id FROM missing m WHERE EXISTS("
            "SELECT 1 FROM close_reference r INDEXED BY close_reference_lookup "
            "WHERE r.reference_type='fact' AND r.reference_id=m.id "
            "AND r.path='readiness.financial_reports.facts[*]' AND r.close_period<=?)",
            (month,),
        )
    }


def _position(snap, *, position_obligations=None):
    balances = snap.accounts
    source_issues = []
    known = KNOWN_POSITION_ACCOUNTS
    detailed_accounts = set(RECLASS) | (set(balances) - known)
    rows = [
        {"account": account, "amount": value}
        for account, value in balances.items()
        if account not in detailed_accounts
    ]
    projected = defaultdict(int)
    from .settlement_projection import settlement_position_rows

    if position_obligations is None:
        position_obligations = settlement_position_rows(
            snap.connection, snap.period, RECLASS, reads=snap.reads
        )
    from .report_classification_directory import classification_directory_scope

    has_classification_type = snap.connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='fact_report_classification'"
    ).fetchone() is not None
    party_keys = (
        {
            row[0]
            for row in snap.connection.execute(
                "SELECT DISTINCT c.voucher_version_id "
                "FROM fact_report_classification_counterparties child "
                "CROSS JOIN fact_report_classification c ON c.revision_id=child.revision_id"
            )
        }
        if has_classification_type
        else set()
    )
    # A new profit-only classification can conflict with an old profit-only
    # one. Seek every exact open fact period before asking the authenticated
    # frozen tree; a later close never stands in for an earlier missing close.
    typed_join = (
        "LEFT JOIN fact_report_classification c ON c.revision_id=f.id "
        if has_classification_type
        else "CROSS JOIN (SELECT NULL AS voucher_version_id) c "
    )
    from .query_reads import OPEN_FACT_PERIODS_CTE

    open_rows = tuple(snap.connection.execute(
        OPEN_FACT_PERIODS_CTE
        + "SELECT f.id,c.voucher_version_id FROM open_periods p "
        "CROSS JOIN fact_revision f INDEXED BY fact_period ON f.period=p.period "
        "JOIN fact_current h ON h.fact_id=f.id "
        "JOIN subject s ON s.id=f.subject_id AND s.kind='report_classification' "
        + typed_join,
        (snap.month, snap.month),
    ))
    open_keys = {row[1] for row in open_rows if row[1] is not None}
    rooted = classification_directory_scope(
        snap.connection, snap.month, party_keys | open_keys, reads=snap.reads
    )
    classification_ids = {row[0] for row in open_rows}
    if rooted is not None:
        for entries in rooted["membership"].values():
            classification_ids.update(entry[0] for entry in entries)
        for entries in rooted["conflict_membership"].values():
            classification_ids.update(entry[0] for entry in entries)
        classification_ids.update(entry[1] for entry in rooted["unsafe"])
        if not has_classification_type and rooted["root"]["keys"]:
            raise KernelError("content_integrity_failed", "冻结报表分类缺少类型化来源")
    if not has_classification_type and classification_ids:
        raise KernelError("content_integrity_failed", "报表分类缺少类型化来源")
    if has_classification_type:
        # The frozen key tree cannot reverse-map a deleted typed row to its
        # voucher key. Retain this global authority check.
        classification_ids.update(
            _missing_adopted_report_classification_ids(snap.connection, snap.month)
        )
    fallback_accounts = {
        obligation["account"]
        for obligation in position_obligations
        if obligation["account"] in RECLASS
        and (obligation["counterparty_id"] is None or obligation["unknown"])
    }
    # Report classifications may contain only profit or cash details. Those
    # facts do not affect a financial-position party split. Keep duplicate
    # voucher classifications and missing typed rows in the checked set, but
    # load party facts only when they actually contain party detail.
    position_classification_ids = set()
    if classification_ids:
        position_classification_ids = {
            row[0]
            for row in snap.connection.execute(
                "WITH selected AS (SELECT value id FROM json_each(?)), "
                "duplicate_vouchers AS (SELECT c.voucher_version_id "
                "FROM selected ids JOIN fact_report_classification c ON c.revision_id=ids.id "
                "GROUP BY c.voucher_version_id HAVING count(*)>1) "
                "SELECT ids.id FROM selected ids LEFT JOIN fact_report_classification c "
                "ON c.revision_id=ids.id WHERE c.revision_id IS NULL "
                "OR c.voucher_version_id IN (SELECT voucher_version_id FROM duplicate_vouchers) "
                "OR EXISTS(SELECT 1 FROM fact_report_classification_counterparties child "
                "WHERE child.revision_id=ids.id)",
                (json.dumps(sorted(classification_ids)),),
            )
        }
    classifications, ambiguous = {}, set()
    has_explicit_party = False
    if position_classification_ids:
        from .reports import _verify_report_fact_sources

        _verify_report_fact_sources(
            snap.connection, snap.reads, position_classification_ids
        )
    for ident in sorted(position_classification_ids):
        data = snap.fact(ident)["data"]
        key = data["voucher_version_id"]
        has_explicit_party = has_explicit_party or bool(data["counterparties"])
        if key in classifications:
            ambiguous.add(key)
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
    for key in ambiguous:
        classifications[key] = {}
    if classifications:
        classified_accounts = {
            row[0]
            for row in snap.connection.execute(
                "SELECT DISTINCT l.account FROM json_each(?) ids "
                "JOIN voucher_line l ON l.version_id=ids.value "
                "WHERE l.account IN (SELECT value FROM json_each(?))",
                (json.dumps(sorted(classifications)), json.dumps(sorted(RECLASS))),
            )
        }
        # A stale party classification still takes the exact line path. A
        # profit-only classification cannot force all reclassified accounts
        # through historical journal hydration.
        fallback_accounts.update(
            classified_accounts or (RECLASS if has_explicit_party or ambiguous else ())
        )
    projected_fallback = set()
    if fallback_accounts and not classifications:
        from .report_projection import party_balance_rows

        party_rows = party_balance_rows(snap.engine, snap.connection, snap.month, reads=snap.reads)
        if party_rows is not None:
            rows.extend(row for row in party_rows if row["account"] in fallback_accounts)
            projected_fallback = set(fallback_accounts)
    journal_accounts = (
        (fallback_accounts - projected_fallback) | (set(balances) - known)
        if fallback_accounts
        else set()
    )
    if journal_accounts:
        events = list(snap.journal.select(accounts=journal_accounts))
        resolutions = snap.reads.relations_many(event["basis_calculation_id"] for event in events)
        for event in events:
            resolution = resolutions[event["basis_calculation_id"]]
            for line in event["lines"]:
                if line["account"] not in journal_accounts:
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
                    if line["account"] not in journal_accounts:
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
    for obligation in position_obligations:
        account = obligation.get("account")
        if account not in RECLASS or account in fallback_accounts:
            continue
        remaining = None if obligation["unknown"] else obligation["remaining"]
        category = obligation.get("category")
        amount = (
            remaining
            if category == "receivable" and type(remaining) is int
            else -remaining
            if category == "payable" and type(remaining) is int
            else None
        )
        if type(amount) is int:
            projected[account] += amount
        rows.append(
            {
                "account": account,
                "amount": amount,
                "party_key": (
                    ("party", obligation["counterparty_id"])
                    if obligation.get("counterparty_id")
                    else None
                ),
            }
        )
    for account in detailed_accounts - journal_accounts - projected_fallback:
        residual = balances[account] - projected[account]
        if residual:
            rows.append({"account": account, "amount": residual})
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
        # An account outside every mapping table reaches neither sum, so its amount would
        # silently understate revenue or expense; collect it instead of dropping it.
        for account, value in values.items():
            if value and account not in known:
                source_issues.append(
                    {
                        "field": "account_mapping",
                        "message": "存在未映射的非零账户余额，本月收入、费用不含该金额",
                        "semantics": "accounting",
                        "account": account,
                        "amount_fen": value,
                    }
                )
        return revenue, expense, revenue - expense

    revenue, expense, monthly = result(snap.month_accounts)
    fixed, intangible = balances["1601"] + balances["1602"], balances["1701"] + balances["1702"]
    return {
        "assets_fen": assets,
        "liabilities_fen": liabilities,
        "capital_fen": position["capital_fen"],
        "equity_fen": position["equity_fen"],
        "bank_fen": balances["1002"],
        "liability_calculation": {
            "current_fen": position["lines"][41],
            "non_current_fen": position["lines"][46],
        },
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
        "cumulative_result_fen": position["cumulative_result_fen"],
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


def _open_items(snap, *, after=None, limit=100, summary_only=False):
    from .settlement_projection import settlement_dashboard_open

    historical = settlement_dashboard_open(
        snap.connection,
        snap.period,
        after=after,
        limit=limit,
        summary_only=summary_only,
        reads=snap.reads,
    )
    selected = {row["key"] for row in historical["obligations"]}
    current = settlement_dashboard_open(
        snap.connection,
        snap.period,
        current=True,
        page_keys=selected,
        limit=limit,
        summary_only=summary_only,
        include_settled_page=True,
        reads=snap.reads,
    )
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

    sources = historical["obligations"]
    current_by_key = {row["key"]: row for row in current["obligations"]}
    page = historical["page"]
    payroll_components = {
        "net": "实发工资",
        "tax": "代扣个人所得税",
        "withheld_tax": "代扣个人所得税",
        "employee_social": "个人社保",
        "employee_housing": "个人公积金",
        "employer_social": "单位社保",
        "employer_housing": "单位公积金",
    }
    payroll_fact_ids = {
        source["source_fact_id"]
        for source in sources
        if source["key"] in selected
        and (source.get("source_business") or {}).get("kind")
        in PAYROLL_KINDS | {"opening_payroll_payable"}
        and source.get("source_fact_id")
    }
    payroll_facts = snap.reads.facts(payroll_fact_ids) if payroll_fact_ids else {}
    buckets, categories, items = defaultdict(list), [], []
    for source in sources:
        buckets[category(source)].append(source)
    for key, (label, direction) in configurations.items():
        category_total = historical["categories"].get(key)
        if category_total is None:
            continue
        sources_in_category = buckets[key]
        rows = []
        for source in sources_in_category:
            if source["key"] not in selected:
                continue
            current_source = current_by_key.get(source["key"])
            business = source.get("source_business") or {}
            settlement_party_id = (
                source.get("creditor_id")
                or source.get("counterparty_id")
                or source.get("recipient_id")
            )
            source_fact = payroll_facts.get(source.get("source_fact_id"), {})
            fact_data = source_fact.get("data", {})
            component = (
                fact_data.get("component")
                if business.get("kind") == "opening_payroll_payable"
                else source.get("name")
            )
            employee_id = fact_data.get("employee_id")
            party_id = (
                employee_id
                if component in payroll_components and isinstance(employee_id, str)
                else settlement_party_id
            )
            description = payroll_components.get(component, _name(business.get("kind", "")))
            if business.get("kind") == "annual_bonus" and component in {"net", "tax"}:
                description = "实发奖金" if component == "net" else "奖金代扣个税"
            row = {
                **source,
                "id": source["key"],
                "category_key": key,
                "voucher": "查看精确来源",
                "party_key": party_id or source["key"],
                **snap.party_field(party_id),
                "description": description,
                "status": source["settlement_status"],
                "outstanding_fen": source["remaining_fen"],
                "current_status": (
                    current_source.get("settlement_status") if current_source is not None else None
                ),
                "current_outstanding_fen": (
                    current_source.get("remaining_fen") if current_source is not None else None
                ),
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
                "count": category_total["count"],
                "loaded_count": len(rows),
                "outstanding_fen": category_total["amount"],
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
        result = {}
        for direction in ("receivable", "payable"):
            rows = [
                value
                for key, value in shared["categories"].items()
                if configurations[key][1] == direction
            ]
            result[direction + "_count"] = sum(row["count"] for row in rows)
            result[direction + "_fen"] = _nullable_sum(row["amount"] for row in rows)
        return result | {
            "total_count": shared["page"]["total_count"],
            "unestablished_count": 0,
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


def _source_settlement(snap, calc, *, limit=100, include_movements=True):
    """Project the shared obligation relation; no page-specific settlement arithmetic.

    ``include_movements=False`` keeps the obligation summary and drops the itemised
    settlement page.  Building that page costs a full separate selection per source,
    so callers whose view does not display it must opt out.
    """
    subject = calc["subject_id"]
    snap.prepare_settlements({subject})
    shared = snap.settlement_cache[subject]
    source_obligations = [
        item
        for item in shared["obligations"]
        if (item.get("source_business") or {}).get("subject_id") == subject
    ]
    obligations = [{**item, "amount_fen": item["source_amount_fen"]} for item in source_obligations]
    current = snap.settlement_current_cache[subject]
    result = {
        "subject_id": subject,
        "settlement_view": "historical",
        "movements_scope": "business_related_settlement_events",
        "status": shared["status"],
        "obligations": obligations,
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
    if not include_movements:
        return result
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
    result["movements"] = movements
    result["movements_page"] = seal_page(
        snap,
        "business-status",
        "settlement_events",
        collection["page"],
        {"subject_id": subject, "as_of": snap.as_of, "settlement_view": "historical"},
    )
    return result


_WORKFORCE_MONEY_KEYS = (
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


def _workforce_payroll_rows(snap, *, wage_heads=None):
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
    current_heads = (
        adopted_head_metadata(snap, PAYROLL_KINDS, posting_period=snap.period)
        if wage_heads is None
        else wage_heads
    )
    state_subjects = {
        head["subject_id"]
        for head in current_heads
        if head["posting_period"] == snap.month
        and not head["line_count"]
        and head["kind"] in PAYROLL_KINDS
    }
    state_calculations = (
        snap.calculations.selected(
            kinds=PAYROLL_KINDS, subjects=state_subjects, posting_period=snap.period
        ).values()
        if state_subjects
        else ()
    )
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
        for calc in state_calculations
        if calc["kind"] in PAYROLL_KINDS
        and calc["posting_period"] == snap.month
        and calc["subject_id"] not in posted
    )
    return rows


def _workforce_payroll_aggregates(rows, identity):
    aggregates = {}
    period_rows = defaultdict(list)
    for row in rows:
        calc, sign = row["basis"], row["sign"]
        data, values = calc["fact"]["data"], calc["outcome"]["values"]
        item = aggregates.setdefault(
            identity(calc),
            {
                **dict.fromkeys(_WORKFORCE_MONEY_KEYS, 0),
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
    return aggregates, period_rows


def _workforce_cost_from_rows(
    snap, rows, aggregates, period_rows, unestablished_employees, *, verify_sources=False
):
    sums = {key: sum(item[key] for item in aggregates.values()) for key in _WORKFORCE_MONEY_KEYS}
    sums["personal_deduction_fen"] = (
        sums["employee_social_insurance_fen"]
        + sums["employee_housing_fund_fen"]
        + sums["individual_income_tax_fen"]
    )
    sums["company_cost_fen"] = (
        sums["gross_salary_fen"]
        + sums["annual_bonus_fen"]
        + sums["employer_social_insurance_fen"]
        + sums["employer_housing_fund_fen"]
    )
    controlled = sums["company_cost_fen"]
    ledger = sum(
        value
        for account, value in snap.month_accounts.items()
        if account in {"560201", "560101", "540101"}
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
    capital_rows = metric_rows(
        snap.month_journal.select(kinds={"labor_project_cost"}), ("capitalized_fen",)
    )
    capitalized_labor = sum(
        row["sign"] * row["basis"]["outcome"]["values"]["capitalized_fen"]
        for row in capital_rows
        if row["basis"]["kind"] == "labor_project_cost"
    )
    amount_rows = (*rows, *labor_rows, *capital_rows)
    calculation_ids = {row["calculation_id"] for row in amount_rows}
    if calculation_ids:
        snap.reads.verify_selected_content(calculation_ids)
    if verify_sources:
        from .entity_references import verify_hits

        fact_ids = {row["basis"]["fact_id"] for row in amount_rows}
        if fact_ids:
            verify_hits(
                snap.connection,
                [{"fact_id": ident} for ident in fact_ids],
                identity_match="recorded" if snap.close else "current",
                registry=snap.store.registry,
            )
    return {
        "workforce_cost": {
            "has_activity": employee_cost["has_activity"]
            or labor["has_activity"]
            or capitalized_labor != 0,
            "total_fen": ledger + gross,
            "capitalized_labor_fen": capitalized_labor,
            "employee": employee_cost,
            "personal_labor": labor,
        },
        "sums": sums,
        "controlled": controlled,
        "ledger": ledger,
        "adjustment": adjustment,
    }


def _brief_workforce_cost(snap):
    rows = _workforce_payroll_rows(snap)
    aggregates, periods = _workforce_payroll_aggregates(rows, lambda _calc: None)
    return _workforce_cost_from_rows(snap, rows, aggregates, periods, {}, verify_sources=True)[
        "workforce_cost"
    ]


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
    money_keys = _WORKFORCE_MONEY_KEYS
    wage_heads = payroll_head_metadata(
        snap, PAYROLL_KINDS | {"opening_payroll_payable"}, line_count_period=snap.period
    )
    rows = _workforce_payroll_rows(snap, wage_heads=wage_heads)
    profile_facts = list(snap.by_kind("payroll_profile"))
    # The verified employee role locates the list. Load scalar wage fields only
    # for a selected source or a registry without that declared role.
    wage_scalars = {}
    confirmed_employees = payroll_head_identities(snap, wage_heads) if snap.close else {}
    checked_wage_facts = {head["fact_id"] for head in wage_heads} if snap.close else set()

    def verify_wage_facts(fact_ids):
        if not snap.close:
            return
        missing = set(fact_ids) - checked_wage_facts
        if missing:
            from .entity_references import verify_hits

            verify_hits(
                snap.connection,
                [{"fact_id": ident} for ident in missing],
                identity_match="recorded",
                registry=snap.store.registry,
            )
            checked_wage_facts.update(missing)

    heads_by_fact = {head["fact_id"]: head for head in wage_heads}

    def wage_identity(fact_id):
        if fact_id in confirmed_employees:
            return confirmed_employees[fact_id]
        load_wage_scalars((fact_id,))
        return wage_scalars[fact_id]["employee_id"]

    def load_wage_scalars(fact_ids):
        missing = set(fact_ids) - wage_scalars.keys()
        if missing:
            verify_wage_facts(missing)
            values = scalar_facts(snap, [heads_by_fact[ident] for ident in missing])
            wage_scalars.update(
                (
                    ident,
                    {**data, "employee_id": confirmed_employees.get(ident, data["employee_id"])},
                )
                for ident, data in values.items()
            )

    if not snap.close:
        from .entity_references import current_role_matches

        fact_ids = {row["basis"]["fact_id"] for row in rows}
        fact_ids.update(head["fact_id"] for head in wage_heads)
        fact_ids.update(fact["id"] for fact in profile_facts)
        confirmed_employees = current_role_matches(
            snap.connection, fact_ids, "employee", registry=snap.store.registry
        )
        missing_role_facts = {head["fact_id"] for head in wage_heads} - confirmed_employees.keys()
        if missing_role_facts:
            load_wage_scalars(missing_role_facts)
    aggregates, period_rows = _workforce_payroll_aggregates(
        rows,
        lambda calc: confirmed_employees.get(calc["fact_id"], calc["fact"]["data"]["employee_id"]),
    )
    profiles = defaultdict(list)
    for fact in profile_facts:
        data = fact["data"]
        if data["effective_from"] <= snap.period and (
            data["effective_to"] is None or data["effective_to"] >= snap.period
        ):
            profiles[confirmed_employees.get(fact["id"], data["employee_id"])].append(fact)
    known = set(aggregates) | set(snap.profiles.get("employee", {}))
    # The current accounting selector has no unresolved state candidate lane;
    # its unestablished_state_selections is empty. Do not scan every old wage
    # adoption a second time to project that empty collection.
    unestablished_employees = {}
    representative_heads = {}
    for head in wage_heads:
        ident = wage_identity(head["fact_id"])
        old = representative_heads.get(ident)
        if old is None or (head["posting_period"], head["id"]) > (old["posting_period"], old["id"]):
            representative_heads[ident] = head
    if representative_heads:
        proven = snap.calculations.selected(
            kinds=PAYROLL_KINDS | {"opening_payroll_payable"},
            subjects={head["subject_id"] for head in representative_heads.values()},
        )
        if any(
            (selected := proven.get(head["subject_id"])) is None
            or selected["id"] != head["id"]
            or selected["fact_id"] != head["fact_id"]
            for head in representative_heads.values()
        ):
            raise KernelError(
                "content_integrity_failed", "工资人员名单的采用身份不匹配", component="payroll"
            )
        verify_wage_facts(
            {row["basis"]["fact_id"] for row in rows}
            | {head["fact_id"] for head in representative_heads.values()}
        )
    known.update(wage_identity(head["fact_id"]) for head in wage_heads)
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
    detail_subjects = {
        head["subject_id"]
        for head in wage_heads
        if employee_id is not None and wage_identity(head["fact_id"]) == employee_id
    }
    if employee_id in selected_ids:
        load_wage_scalars(
            head["fact_id"] for head in wage_heads if head["subject_id"] in detail_subjects
        )
    wage_calculations = (
        list(
            snap.calculations.selected(
                kinds=PAYROLL_KINDS | {"opening_payroll_payable"},
                subjects=detail_subjects,
            ).values()
        )
        if detail_subjects and employee_id in selected_ids
        else []
    )
    head_by_subject = {head["subject_id"]: head["id"] for head in wage_heads}
    if any(head_by_subject.get(calc["subject_id"]) != calc["id"] for calc in wage_calculations):
        raise KernelError("content_integrity_failed", "工资来源采用身份不匹配", component="payroll")
    source_pages, picked_sources = {}, set()
    # Payroll history is returned only for the explicitly opened employee.
    # The default list still computes its complete amounts and payment summary,
    # but must not build discarded history cards for every listed employee.
    history_ids = selected_ids & {employee_id} if employee_id is not None else set()

    def wage_source_order(calc):
        data = wage_scalars[calc["fact_id"]]
        return (data.get("payroll_period") or data["period"], calc["subject_id"])

    for ident in history_ids:
        candidates = sorted(
            (calc for calc in wage_calculations if wage_identity(calc["fact_id"]) == ident),
            key=wage_source_order,
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
    displayed_wages = sorted(
        (calc for calc in wage_calculations if calc["id"] in picked_sources),
        key=wage_source_order,
    )
    snap.prepare_settlements({calc["subject_id"] for calc in displayed_wages})
    wage_sources = defaultdict(list)
    net_payments, direct_payments = defaultdict(int), defaultdict(int)
    summarized_wages = {
        head["subject_id"]: head
        for head in wage_heads
        if wage_identity(head["fact_id"]) in selected_ids
    }
    if summarized_wages:
        from .settlement_freeze import frozen_payroll_period_payments

        payment_rows = frozen_payroll_period_payments(
            snap.connection, snap.period, reads=snap.reads
        )
        if payment_rows is None:
            payment_rows = snap.settlement_summary(subject_ids=set(summarized_wages))["obligations"]
        paid_subjects = {
            subject
            for obligation in payment_rows
            if (
                subject := obligation.get("subject_id")
                or (obligation.get("source_business") or {}).get("subject_id")
            )
            in summarized_wages
        }
        if paid_subjects:
            proven_payments = snap.calculations.selected(
                kinds=PAYROLL_KINDS | {"opening_payroll_payable"},
                subjects=paid_subjects,
            )
            if any(
                (selected := proven_payments.get(subject)) is None
                or selected["id"] != summarized_wages[subject]["id"]
                for subject in paid_subjects
            ):
                raise KernelError(
                    "content_integrity_failed", "工资付款来源采用身份不匹配", component="payroll"
                )
            verify_wage_facts(summarized_wages[subject]["fact_id"] for subject in paid_subjects)
            load_wage_scalars(
                summarized_wages[subject]["fact_id"]
                for subject in paid_subjects
                if summarized_wages[subject]["kind"] == "opening_payroll_payable"
            )
        net_parts, paid_parts = defaultdict(list), defaultdict(list)
        for obligation in payment_rows:
            source = summarized_wages.get(
                obligation.get("subject_id")
                or (obligation.get("source_business") or {}).get("subject_id")
            )
            if source is None:
                continue
            net_name = (
                "primary"
                if source["kind"] == "opening_payroll_payable"
                and wage_scalars[source["fact_id"]]["component"] == "net"
                else "net"
            )
            if obligation.get("name") == net_name:
                ident = wage_identity(source["fact_id"])
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
        ident = confirmed_employees.get(calc["fact_id"], data["employee_id"])
        known.add(ident)
        # The card shows the obligation summary; the itemised settlement page is
        # fetched per employee on demand, so it is not built for every source here.
        settlement = _source_settlement(snap, calc, limit=limit, include_movements=False)
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
                "selection_status": "established",
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
            }
        )
    items.sort(key=lambda item: item["employee_id"])
    unknown = sum(item["in_period"] is None for item in all_items)
    cost = _workforce_cost_from_rows(snap, rows, aggregates, period_rows, unestablished_employees)
    workforce_cost = cost["workforce_cost"]
    sums = cost["sums"]
    controlled = cost["controlled"]
    ledger = cost["ledger"]
    adjustment = cost["adjustment"]
    employee_cost = workforce_cost["employee"]
    labor_items = []
    labor_heads = adopted_head_metadata(
        snap, LABOR_KINDS | {"labor_project_cost"}, posting_period=snap.period
    )
    entity_sources = {
        head["subject_id"]
        for head in wage_heads
        if employee_id is not None and wage_identity(head["fact_id"]) == employee_id
    }
    if employee_id in unestablished_employees:
        entity_sources.update(
            item["subject_id"]
            for item in unestablished_employees[employee_id]["candidate_selections"]
        )
    if employee_id is not None:
        from .schema import table_name

        labor_subjects = set()
        for kind in LABOR_KINDS | {"labor_project_cost"}:
            labor_subjects.update(
                row[0]
                for row in snap.connection.execute(
                    f"SELECT DISTINCT f.subject_id FROM {table_name(kind)} d "
                    "JOIN fact_revision f ON f.id=d.revision_id WHERE d.person_id=?",
                    (employee_id,),
                )
            )
        all_labor_calculations = (
            list(
                snap.calculations.selected(
                    kinds=LABOR_KINDS | {"labor_project_cost"}, subjects=labor_subjects
                ).values()
            )
            if labor_subjects
            else []
        )
        labor_scalar = scalar_facts(snap, all_labor_calculations)
        entity_sources.update(
            calc["subject_id"]
            for calc in all_labor_calculations
            if labor_scalar[calc["fact_id"]]["person_id"] == employee_id
        )
    labor_keys, labor_page = page_keys(
        [
            head["id"]
            for head in sorted(labor_heads, key=lambda row: (row["period"], row["subject_id"]))
        ],
        cursors.get("labor_sources"),
        limit,
    )
    displayed_labor = []
    if "labor_sources" in requested and not summary_only and labor_keys:
        labor_selected = snap.calculations.selected(
            kinds=LABOR_KINDS | {"labor_project_cost"},
            subjects={head["subject_id"] for head in labor_heads if head["id"] in labor_keys},
            posting_period=snap.period,
        )
        displayed_labor = [calc for calc in labor_selected.values() if calc["id"] in labor_keys]
        if {calc["id"] for calc in displayed_labor} != set(labor_keys):
            raise KernelError(
                "content_integrity_failed", "劳务来源采用身份不匹配", component="labor"
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
                **_source_settlement(snap, calc, limit=limit, include_movements=False),
            }
        )
    labor_items = sorted(labor_items, key=lambda item: (item["period"], item["source_id"]))
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
            "identity_note": (
                "在册状态和入离职日期仅展示已确认的管理资料，不决定工资核算资格或个税起点。"
            ),
        },
        "_entity_sources": entity_sources,
        "collections": {
            "employees": {"items": items, "page": employee_page},
            "labor_sources": {"items": labor_items, "page": labor_page},
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
        "workforce_cost": workforce_cost,
    }


def _asset_card_sources(snap):
    """Select and check card identities without loading consumption history."""
    acquisition_rows = list(snap.calculations_of_kind(*ASSET_KINDS))
    batches = list(snap.calculations_of_kind("reimbursed_asset_batch"))
    lifecycle = list(snap.calculations_of_kind("asset_activation", "asset_disposal"))
    projects = list(snap.calculations_of_kind("project_cost", "labor_project_cost"))
    scalar = verified_scalar_facts(snap, [*acquisition_rows, *batches, *lifecycle, *projects])
    acquisitions = {scalar[calc["fact_id"]]["asset_id"]: calc for calc in acquisition_rows}
    batch_by_fact = {calc["fact_id"]: calc for calc in batches}
    details = {}
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
            details[detail["asset_id"]] = detail
    activations, disposals = {}, {}
    for calc in lifecycle:
        target = activations if calc["kind"] == "asset_activation" else disposals
        target[scalar[calc["fact_id"]]["asset_id"]] = calc
    return {
        "acquisitions": acquisitions,
        "details": details,
        "activations": activations,
        "disposals": disposals,
        "lifecycle": lifecycle,
        "projects": projects,
        "scalar": scalar,
    }


def _asset_status(kind, asset_type, activated, disposed):
    if disposed:
        return "disposed" if asset_type == "fixed" else "retired"
    return "active" if kind == "opening_asset" or activated else "pending_activation"


def _project_cost_balances(snap):
    balances = defaultdict(int)
    # Select exact posted cost sources and reversals before reading their effects.
    query, parameters = snap.journal.select(accounts={"189901", "4301"}).sql()
    rows = snap.connection.execute(
        "SELECT j.id,j.close_period,j.basis_calculation_id,j.reverses_id FROM (" + query + ") j",
        parameters,
    ).fetchall()
    snap.reads.verify_selected_voucher_adoptions(rows, through_period=snap.month)
    outcomes = snap.reads.verify_selected_content({row["basis_calculation_id"] for row in rows})
    for row in rows:
        sign = -1 if row["reverses_id"] is not None else 1
        for effect in outcomes[row["basis_calculation_id"]]["balances"]:
            if effect["key"].startswith("project-cost:"):
                balances[effect["key"]] += sign * effect["amount"]
    return balances


def _long_term_assets(snap):
    """Brief consumes ledger totals and card counts, not card charge histories."""
    sources = _asset_card_sources(snap)
    counts = defaultdict(int)
    for ident, calc in sources["acquisitions"].items():
        data = sources["scalar"][calc["fact_id"]] | sources["details"].get(ident, {})
        status = _asset_status(
            calc["kind"],
            data["asset_type"],
            ident in sources["activations"],
            ident in sources["disposals"],
        )
        counts[data["asset_type"], status] += 1
    projects = _project_cost_balances(snap)
    accounts = snap.accounts
    return {
        "net_fen": sum(
            accounts[account]
            for account in ("1601", "1701", "1604", "189901", "4301", "1602", "1702")
        ),
        "fixed_net_fen": accounts["1601"] + accounts["1602"],
        "intangible_net_fen": accounts["1701"] + accounts["1702"],
        "fixed_active_count": counts["fixed", "active"],
        "intangible_active_count": counts["intangible", "active"],
        "pending_count": (
            counts["fixed", "pending_activation"] + counts["intangible", "pending_activation"]
        ),
        "project_cost_fen": sum(
            projects[f"project-cost:{calc['subject_id']}"] for calc in sources["projects"]
        ),
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
    sources = _asset_card_sources(snap)
    acquisitions, asset_details = sources["acquisitions"], sources["details"]
    activations, disposals = sources["activations"], sources["disposals"]
    lifecycle, project_calculations = sources["lifecycle"], sources["projects"]
    scalar = lifecycle_scalar = sources["scalar"]
    # Closed carrying balances are rooted by asset key; the open tail is
    # checked against its publication and category seals in the same snapshot.
    # A current, undisposed card can recover cumulative charge from cost less
    # carrying without decoding every older member outcome. Other cards keep
    # their exact historical member walk below.
    from .period_balances import balance_totals, verify_selected_balances

    candidates = {
        ident
        for ident, calc in acquisitions.items()
        if ident not in disposals and calc["kind"] != "opening_asset"
    }
    asset_keys = {ident: f"asset:{ident}:carrying" for ident in candidates}
    carrying = (
        {
            row["key"]: row["amount"]
            for row in balance_totals(
                snap.connection, snap.month, "asset", set(asset_keys.values()), reads=snap.reads
            )
        }
        if asset_keys
        else {}
    )
    # The old period_balance rows are not individually checked by
    # balance_totals: its closed part is certified by the frozen balance root.
    # Do not let those old rows decide whether cost minus carrying is charge.
    # Derive the non-charge part from the selected, immutable acquisition
    # publications and their checked result bodies instead.
    noncharge = defaultdict(int)
    if asset_keys:
        acquisition_journal = snap.journal.select(
            kinds={"asset", "reimbursed_asset", "reimbursed_asset_batch"}
        )
        acquisition_sql, acquisition_parameters = acquisition_journal.sql()
        acquisition_publications = snap.connection.execute(
            f"SELECT j.id,j.close_period,j.basis_calculation_id,j.reverses_id "
            f"FROM ({acquisition_sql}) j",
            acquisition_parameters,
        ).fetchall()
        snap.reads.verify_selected_voucher_adoptions(
            acquisition_publications, through_period=snap.month
        )
        acquisition_ids = {row["basis_calculation_id"] for row in acquisition_publications}
        if acquisition_ids:
            outcomes = snap.reads.verify_selected_content(acquisition_ids)
            published = {
                row[0]
                for row in snap.connection.execute(
                    "SELECT c.calculation_id FROM json_each(?) ids "
                    "JOIN calculation_publication c ON c.calculation_id=ids.value",
                    (canonical(sorted(acquisition_ids)),),
                )
            }
            if published != acquisition_ids:
                raise KernelError("content_integrity_failed", "资产取得缺少正式发布")
            for publication in acquisition_publications:
                sign = -1 if publication["reverses_id"] else 1
                for effect in outcomes[publication["basis_calculation_id"]]["balances"]:
                    if effect["category"] == "asset" and effect["key"] in asset_keys.values():
                        noncharge[effect["key"]] += sign * effect["amount"]
    asset_costs = {
        ident: (scalar[calc["fact_id"]] | asset_details.get(ident, {}))["cost_fen"]
        for ident, calc in acquisitions.items()
    }
    fast_assets = {
        ident
        for ident in candidates
        if (asset_keys[ident] in carrying or asset_costs[ident] == 0)
        and noncharge.get(asset_keys[ident], 0) == asset_costs[ident]
    }
    if fast_assets:
        verify_selected_balances(
            snap.connection, snap.month, "asset", periods={snap.month}, reads=snap.reads
        )
    monthly_asset_charge = (
        {
            row["balance_key"]: -row["amount"]
            for row in snap.connection.execute(
                "SELECT b.balance_key,sum(b.amount) amount FROM period_balance b "
                "JOIN calculation c ON c.id=b.calculation_id "
                "WHERE b.posting_period=? AND b.category='asset' "
                "AND b.balance_key IN (SELECT value FROM json_each(?)) "
                "AND c.kind IN ('asset_consumption','asset_consumption_month') "
                "GROUP BY b.balance_key",
                (snap.month, json.dumps(sorted(asset_keys[ident] for ident in fast_assets))),
            )
        }
        if fast_assets
        else {}
    )
    fallback_assets = set(acquisitions) - fast_assets
    full_batch_events = (
        snap.asset_member_events(asset_ids=fallback_assets) if fallback_assets else []
    )
    head_batch_events = (
        snap.queries._selected_asset_member_heads(
            snap.connection, snap.period, asset_ids=fast_assets
        )
        if fast_assets
        else []
    )
    batch_events = sorted(
        (*full_batch_events, *head_batch_events),
        key=lambda item: (
            item["adoption_period"],
            item["voucher_number"] or 0,
            item["owner_calculation_id"],
            item["asset_id"],
        ),
    )
    charges, monthly_charges, latest_charge = defaultdict(int), defaultdict(int), {}
    if "asset_consumption" in snap.store.registry.models:
        query, parameters = snap.journal.select(kinds={"asset_consumption"}).sql()
        snap.reads.verify_sql_outcomes(
            row[0]
            for row in snap.connection.execute(
                "SELECT DISTINCT basis_calculation_id FROM (" + query + ")", parameters
            )
        )
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
    for ident in fast_assets:
        charges[ident] = asset_costs[ident] - carrying.get(asset_keys[ident], 0)
        monthly_charges[ident] = monthly_asset_charge.get(asset_keys[ident], 0)
    consumption_ids = {
        event["calculation_id"]
        for event in full_batch_events
        if event["kind"] == "asset_consumption"
    }
    snap.reads.verify_sql_outcomes(consumption_ids)
    consumption_values = (
        {
            row["id"]: dict(row)
            for row in snap.connection.execute(
                "SELECT c.id,json_extract(c.outcome,'$.values.consumption_fen') amount,"
                "json_extract(c.outcome,'$.values.zero_reason') zero_reason "
                "FROM json_each(?) ids JOIN calculation c ON c.id=ids.value",
                (json.dumps(sorted(consumption_ids)),),
            )
        }
        if consumption_ids
        else {}
    )
    batch_references, latest_states, latest_positive, latest_period_event = {}, {}, {}, {}
    for event in batch_events:
        ident = event["asset_id"]
        if event["kind"] == "asset_consumption":
            if ident not in fast_assets:
                values = consumption_values[event["calculation_id"]]
                amount = values["amount"] * event["direction"]
                charges[ident] += amount
                if event["adoption_period"] == snap.period:
                    monthly_charges[ident] += amount
            latest_charge[ident] = max(
                latest_charge.get(ident, event["calculation_period"]), event["calculation_period"]
            )
            if ident in fast_assets and latest_charge[ident] == event["calculation_period"]:
                latest_period_event[ident] = event
            if event["direction"] > 0:
                if ident in fast_assets:
                    latest_positive[ident] = event
                else:
                    latest_states[ident] = values["zero_reason"]
        if event["direction"] > 0:
            # Keep activation and the latest monthly adoption on the card; the
            # complete history remains available through its source history.
            batch_references[ident, event["kind"]] = {
                "calculation_id": event["calculation_id"],
                "owner_calculation_id": event["owner_calculation_id"],
                "voucher_version_id": event["voucher_version_id"],
                "voucher_number": event["voucher_number"],
                "period": event["adoption_period"],
                "label": _name(event["kind"]),
            }
        elif (
            batch_references.get((ident, event["kind"]), {}).get("calculation_id")
            == event["calculation_id"]
        ):
            batch_references.pop((ident, event["kind"]), None)
    selected_heads = {
        (event["owner_calculation_id"], event["calculation_id"]): event
        for event in (
            *latest_positive.values(),
            *latest_period_event.values(),
            *(
                ref | {"asset_id": ident}
                for (ident, _), ref in batch_references.items()
                if ident in fast_assets
            ),
        )
    }
    if selected_heads:
        verified_heads = snap.reads.asset_members_many(owner_id for owner_id, _ in selected_heads)
        for (owner_id, member_id), head in selected_heads.items():
            if not any(
                member["member_calculation_id"] == member_id
                and member["asset_id"] == head["asset_id"]
                for member in verified_heads[owner_id]
            ):
                raise KernelError("asset_batch_identity", "资产汇总成员身份不匹配")
        for ident, event in latest_positive.items():
            member = next(
                member
                for member in verified_heads[event["owner_calculation_id"]]
                if member["member_calculation_id"] == event["calculation_id"]
            )
            latest_states[ident] = member["summary"].get("zero_reason")
    all_items = []
    for ident, calc in sorted(acquisitions.items()):
        data = scalar[calc["fact_id"]] | asset_details.get(ident, {})
        activation, disposal = activations.get(ident), disposals.get(ident)
        opening = calc["kind"] == "opening_asset"
        status = _asset_status(
            calc["kind"], data["asset_type"], activation is not None, disposal is not None
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
    page_calculations = [calc for ident, calc in acquisitions.items() if ident in selected]
    page_calculations.extend(
        calc
        for lookup in (activations, disposals)
        for ident, calc in lookup.items()
        if ident in selected
    )
    page_facts = snap.reads.facts({calc["fact_id"] for calc in page_calculations})
    source_subjects = set()
    for calc in page_calculations:
        if calc["kind"] not in ASSET_KINDS | {"reimbursed_asset_batch"}:
            continue
        data = page_facts[calc["fact_id"]]["data"]
        if data.get("acceptance_id"):
            source_subjects.add(data["acceptance_id"])
        source_subjects.update(source["source_id"] for source in data.get("project_sources", ()))
    source_calculations_by_subject = (
        snap.calculations.selected(subjects=source_subjects) if source_subjects else {}
    )
    entity_sources = set()
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
        entity_sources.update(
            snap.calculation(event["owner_calculation_id"])["subject_id"]
            for event in batch_events
            if event["asset_id"] == asset_id
        )
    settlement_subjects = {
        calc["subject_id"] for ident, calc in acquisitions.items() if ident in selected
    }
    snap.prepare_settlements(settlement_subjects)
    source_subject_by_asset = {}
    for ident in selected:
        calc = acquisitions[ident]
        data = calc["fact"]["data"] | asset_details.get(ident, {})
        acceptance = source_calculations_by_subject.get(data.get("acceptance_id"))
        source_subject_by_asset[ident] = (
            acceptance["subject_id"] if acceptance else calc["subject_id"]
        )
    source_rows_by_subject = defaultdict(list)
    if source_subject_by_asset:
        journal_subjects = set(source_subject_by_asset.values())
        for row in snap.journal.select(subjects=journal_subjects):
            subject_id = row["basis"]["subject_id"]
            if row["sign"] > 0 and subject_id in journal_subjects:
                source_rows_by_subject[subject_id].append(row)
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
        status = _asset_status(
            calc["kind"], data["asset_type"], activation is not None, disposal is not None
        )
        acquisition_day = data.get("acquisition_date")
        acceptance = source_calculations_by_subject.get(data.get("acceptance_id"))
        if acquisition_day is None and acceptance:
            acquisition_day = acceptance["fact"]["data"].get("acquisition_date")
        creditors = data.get("creditors") or (
            acceptance["fact"]["data"].get("creditors", ()) if acceptance else ()
        )
        source_rows = source_rows_by_subject[source_subject_by_asset[ident]]
        batch_source = acceptance or (calc if calc["kind"] == "reimbursed_asset_batch" else None)
        source_calculations = (
            [
                source_calculations_by_subject[source["source_id"]]
                for source in data.get("project_sources", ())
            ]
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
                    **_source_settlement(snap, source, limit=limit, include_movements=False),
                }
                for source in source_calculations
            ],
            "cost_fen": cost,
            "accumulated_charge_fen": accumulated,
            "month_charge_fen": monthly_charges[ident],
            "book_value_fen": 0 if disposal else cost - accumulated,
            "latest_charge_period": latest_charge.get(ident),
            "charge_state_label": {
                "before_consumption_start": "尚未到开始计提月份",
                "fully_consumed": "已完成折旧摊销",
            }.get(latest_states.get(ident)),
            "batch_references": [
                ref for (asset, _), ref in batch_references.items() if asset == ident
            ],
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
                "settlement": _source_settlement(
                    snap, disposal, limit=limit, include_movements=False
                ),
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
    items.sort(key=lambda item: item["asset_id"])
    fixed_items = [item for item in all_items if item["asset_type"] == "fixed"]
    intangible_items = [item for item in all_items if item["asset_type"] == "intangible"]

    def summary(rows, fixed):
        active = [row for row in rows if row["status"] == "active"]
        result = {
            "registered_count": len(rows),
            "unestablished_count": 0,
            "active_count": len(active),
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
    project_balances = _project_cost_balances(snap)
    project_scalar = scalar
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
    if picked_projects:
        snap.reads.calculations(calc["id"] for calc in candidates if calc["id"] in picked_projects)
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
            "settlement": _source_settlement(snap, calc, limit=limit, include_movements=False),
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
    return {
        "fixed_asset_cost_fen": balances["1601"],
        "accumulated_depreciation_fen": -balances["1602"],
        "fixed_asset_net_fen": balances["1601"] + balances["1602"],
        "intangible_asset_cost_fen": balances["1701"],
        "accumulated_amortization_fen": -balances["1702"],
        "intangible_asset_net_fen": balances["1701"] + balances["1702"],
        "active_count": fixed["active_count"] + intangible["active_count"],
        "registered_count": len(all_items),
        "unestablished_count": 0,
        "ledger_cost_fen": ledger_cost,
        "ledger_accumulated_fen": ledger_accumulated,
        "ledger_net_fen": ledger_cost - ledger_accumulated,
        "active_ledger_net_fen": balances["1601"] + balances["1701"] - ledger_accumulated,
        "pending_intangible_count": intangible["pending_count"],
        "pending_intangible_cost_fen": intangible["pending_cost_fen"],
        "project_cost_fen": project_cost,
        "reconciliation_scope": "在用及待启用资产、尚未转出项目成本",
        "card_cost_fen": card_cost,
        "card_accumulated_fen": card_accumulated,
        "card_net_fen": card_cost - card_accumulated,
        "established_card_totals": None,
        "pending_fixed_count": fixed["pending_count"],
        "pending_fixed_cost_fen": fixed["pending_cost_fen"],
        "month_charge_fen": _nullable_sum(row["month_charge_fen"] for row in all_items),
        "month_acquired_count": sum(row["month_acquired"] for row in all_items),
        "month_acquired_fen": sum(monthly_acquired.values()),
        "month_cost_adjustment_fen": sum(monthly_adjusted.values()),
        "month_activated_count": sum(row["month_activated"] for row in all_items),
        "month_exited_count": sum(row["month_exited"] for row in all_items),
        "reconciled": reconciled,
        "reconciliation_label": "资产卡片与账面一致"
        if reconciled is True
        else "资产卡片与账面差异需核对"
        if reconciled is False
        else "资产卡片与账面暂无法完整核对",
        "differences": differences,
        "fixed": fixed,
        "intangible": intangible,
        "_entity_sources": entity_sources,
        "collections": {
            "assets": {"items": items, "page": asset_page},
            "projects": {
                "items": sorted(projects, key=lambda item: (item["project_id"], item["source_id"])),
                "page": project_page,
            },
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
        "schema_version": 4,
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
            "liabilities_total_fen": statements["balance_sheet"]["47"]["ending_fen"],
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
