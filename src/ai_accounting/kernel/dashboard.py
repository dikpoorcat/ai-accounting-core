"""Read-only presentation of the local journal for the existing five-page dashboard.

Journal amounts are selected by posting period. Closed periods use sealed versions;
an open-period reversal reads the original calculation, never the replacement's values.
The browser receives summaries of the complete snapshot and bounded detail pages.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import re
from collections import defaultdict
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import cached_property
from typing import Literal, get_args

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
    payroll_list_head_metadata,
    scalar_facts,
    verified_adopted_head_identities,
    verified_payroll_heads,
    verified_scalar_facts,
)
from .domains.money import ACTUAL_PAYMENT_KINDS, FUNDS_ACCOUNT_TYPE_BY_BALANCE_CATEGORY
from .provenance import recorded_times
from .query_reads import QueryReads
from .query_semantics import classify_financial_position, report_party_splits
from .reports import Reports, _workbook_name
from .types import ActualDate, YearMonth, canonical, digest

EmployeeFilter = Literal[
    "all", "in_period", "unknown", "ended",
    "employment_active", "employment_unpaid_leave",
    "employment_departed", "employment_unknown",
]

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
    "managed_reserve_internal_movement": "备用金内部平台原行留证",
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
    "pass_through": "代收与代付",
    "correction": "更正与冲正",
    "other": "其他业务",
}
PAYROLL_KINDS = {"payroll", "payroll_bounded", "annual_bonus"}
LABOR_KINDS = {"labor", "labor_accrual"}
_PAYROLL_COMPONENT_NAMES = {
    "net": "实发工资",
    "tax": "代扣个人所得税",
    "withheld_tax": "代扣个人所得税",
    "employee_social": "个人社保",
    "employee_housing": "个人公积金",
    "employer_social": "单位社保",
    "employer_housing": "单位公积金",
}
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


def _group(kind, reversal=False, *, creditor_kind=None, settlement_sources=(), direction=None,
           nature=None, payer_kind=None):
    if reversal:
        return "correction"
    if kind in ACTUAL_PAYMENT_KINDS:
        groups = {source[0] for source in settlement_sources}
        return groups.pop() if len(groups) == 1 else "other"
    if kind in {"pass_through", "pass_through_return"}:
        return "pass_through"
    if kind == "opening_loan":
        return "financing_owner"
    if kind == "opening_obligation":
        meanings = {
            "customer_receivable": "income_customer",
            "supplier_service_payable": "expense_supplier",
            "supplier_administration_payable": "expense_supplier",
            "supplier_sales_payable": "expense_supplier",
            "employee_reimbursement": "employee_reimbursement",
            "owner_reimbursement": "financing_owner",
            "deposit_receivable": "fund_movement", "deposit_payable": "fund_movement",
            "other_receivable": "other", "other_payable": "other",
        }
        if not isinstance(nature, str) or nature not in meanings:
            raise KernelError("content_integrity_failed", "期初往来的采用业务性质不匹配")
        return meanings[nature]
    if kind in {"employee_advance", "reimbursement_acceptance"}:
        if not isinstance(payer_kind, str) or payer_kind not in {"employee", "owner"}:
            raise KernelError("content_integrity_failed", "垫付业务的采用付款方性质不匹配")
        return "employee_reimbursement" if payer_kind == "employee" else "financing_owner"
    if kind in PAYROLL_KINDS | {"opening_payroll_payable"} or kind.startswith("payroll_"):
        return "payroll"
    if kind in LABOR_KINDS or kind == "labor_project_cost":
        return "labor"
    if "asset" in kind:
        return "assets"
    if "tax" in kind:
        return "tax"
    if "funding" in kind or kind.startswith("loan_"):
        return "financing_owner"
    if kind in {"service_sale", "sale_return", "advance_fulfillment", "bank_income"}:
        return "income_customer"
    if kind == "expense":
        if not isinstance(creditor_kind, str) or creditor_kind not in {
            "employee", "supplier", "individual",
        }:
            raise KernelError("content_integrity_failed", "费用采用的债权方性质不匹配")
        return "employee_reimbursement" if creditor_kind == "employee" else "expense_supplier"
    if "expense" in kind or kind.startswith("project_"):
        return "expense_supplier"
    if "transfer" in kind or "deposit" in kind or kind == "overpayment":
        return "fund_movement"
    return "other"


def _settlement_group(kind, *, creditor_kind=None, obligation_name=None, nature=None,
                      payer_kind=None):
    # Acceptance records describe an asset/deposit; paying their personal
    # creditor is reimbursement. A deposit refund retains its deposit meaning.
    if kind in {"reimbursed_asset", "reimbursed_asset_batch"} or (
        kind == "reimbursed_deposit" and obligation_name == "reimbursement"
    ):
        return "employee_reimbursement"
    if kind == "pass_through" and (
        not isinstance(obligation_name, str) or obligation_name not in {"collection", "remittance"}
    ):
        raise KernelError("content_integrity_failed", "代收代付采用的精确义务不匹配")
    if kind == "reimbursed_deposit" and obligation_name != "refund":
        raise KernelError("content_integrity_failed", "保证金采用的精确义务不匹配")
    return _group(kind or "", creditor_kind=creditor_kind, nature=nature, payer_kind=payer_kind)


def _settlement_source(kind, *, creditor_kind=None, obligation_name=None, nature=None,
                       payer_kind=None):
    return (
        _settlement_group(kind, creditor_kind=creditor_kind, obligation_name=obligation_name,
                          nature=nature, payer_kind=payer_kind),
        kind,
        obligation_name,
    )


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
        try:
            self._initialize(engine, connection, period, reads)
        except BaseException:
            self.release()
            raise

    def _initialize(self, engine, connection, period, reads):
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
        self.month_account_gross = tuple(
            (row["account"], row["debit"], row["credit"])
            for row in connection.execute(
                "SELECT account,debit,credit FROM monthly_account WHERE period=?", (self.month,)
            )
        )
        self.month_accounts = defaultdict(
            int,
            {
                account: debit - credit
                for account, debit, credit in self.month_account_gross
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

    @cached_property
    def owner_month_state(self):
        """Classify the selected month without decoding its review directory."""
        if self.close is not None:
            return "closed"
        covering = self.connection.execute(
            "SELECT period,manifest,digest FROM period_close "
            "WHERE period>? ORDER BY period LIMIT 1", (self.month,),
        ).fetchone()
        if covering is not None:
            self.reads.close_header(covering)
            return "covered"
        return "open"

    @cached_property
    def accounts(self):
        """Load ledger totals only for projections that consume them.

        Funds use their account-balance sources, and employees use this month's
        payroll amounts. Neither needs a second whole-ledger balance read.
        """
        return defaultdict(int, account_totals(self))

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
        missing = {subject for subject in subjects if subject not in self.settlement_cache}
        if missing:
            result = self.settlement_summary(subject_ids=missing)
            current = self.settlement_summary(subject_ids=missing, current=True)
            for subject in missing:
                self.settlement_cache[subject] = result
                self.settlement_current_cache[subject] = current

    def settlement_summary(self, *, subject_ids=None, current=False, include_history_counts=True):
        key = (
            current, None if subject_ids is None else frozenset(subject_ids), include_history_counts
        )
        if key not in self.settlement_summaries:
            self.settlement_summaries[key] = self.queries.settlement_summary(
                self.connection, self.period, subject_ids=subject_ids, current=current,
                include_history_counts=include_history_counts,
            )
        return self.settlement_summaries[key]

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
            "employment_state",
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

    def party_details(self, ident, *, exact_identity=False):
        """Reuse frozen names and supplements for an already located object.

        Exact business references need no employee-roster membership lookup:
        the counterparty profile mapping includes the same person records.
        Other consumers retain their existing employee-first lookup.
        """
        if not ident:
            return {"name": "未提供", "source": None}
        for kind in (("counterparty",) if exact_identity else ("employee", "counterparty")):
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

    def party_code_details(self, ident, *, exact_identity=False):
        profile = self.profile("counterparty" if exact_identity else "employee", ident)
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
        missing = {proof for proof in proofs if proof not in self.evidence_cache}
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
        self, calc, sign=1, relations=None, *, include_sources=False, asset_references=(),
        include_parties=True,
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
            if include_parties and i and i != "payroll-group"
            and self.party_details(i).get("source")
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

    @cached_property
    def activity_components(self):
        from .dashboard_activity_parts import payment_components

        return payment_components(self)

    @cached_property
    def activity_classification(self):
        """Classify exact adopted parts; whole-voucher contexts keep one label."""
        from .dashboard_activity_parts import adopted_rows, classification_values

        rows = adopted_rows(self)
        semantic = {"expense", "opening_obligation", "employee_advance", "reimbursement_acceptance"}
        selected = {row["basis_calculation_id"] for row in rows if row["basis_kind"] in semantic}
        values = classification_values(self, selected)
        by_basis, counts = {}, defaultdict(int)
        for row in rows:
            reversal = row["reverses_id"] is not None
            parts = self.activity_components.get(row["id"])
            if parts is not None:
                categories = {part["group"] for part in parts}
                group = next(iter(categories)) if len(categories) == 1 else "other"
                for part in parts:
                    counts[part["group"], row["basis_kind"]] += 1
            else:
                data = values.get(row["basis_calculation_id"], {})
                group = _group(row["basis_kind"], reversal,
                               creditor_kind=data.get("creditor_kind"), nature=data.get("nature"),
                               payer_kind=data.get("payer_kind"))
                counts[group, row["basis_kind"]] += 1
            by_basis[row["basis_calculation_id"], reversal] = group
        return by_basis, counts

    def _voucher_asset_details(self, row):
        calc = row["basis"]
        kind, data = calc["kind"], calc["fact"]["data"]
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
        return asset_references, asset_reference, asset_members, asset_lines, member_evidence

    def owner_voucher(self, row):
        """Render the selected voucher's saved lines without diagnostic graphs."""
        calc = row["basis"]
        relations = self.voucher_relations(calc, row["sign"])
        references, asset, members, asset_lines, _ = self._voucher_asset_details(row)
        short, summary = self.business_summary(
            calc, row["sign"], relations, asset_references=references or members
        )
        amount, label = self.business_amount(calc)
        recognition = _recognition(calc["fact"]["data"], str(YearMonth.from_ordinal(row["period"])))

        def owner_asset(reference):
            return {key: value for key, value in reference.items() if key != "field_sources"}

        voucher = {
            "number": str(row["number"]),
            "voucher_version_id": row["id"],
            "subject_id": calc["subject_id"],
            "reverses_version_id": row["reverses_id"],
            "date": recognition["date"], "recognition": recognition,
            "type": _name(calc["kind"]), "kind": calc["kind"],
            "state": "冲正" if row["sign"] < 0 else "已入账",
            "group": (row["_owner_activity_group"] if "_owner_activity_group" in row
                      else self.activity_classification[0][calc["id"], row["sign"] < 0]),
            "summary": summary, "list_summary": short,
            "amount_fen": row["total"],
            "business_amount_fen": row["sign"] * amount if amount is not None else None,
            "business_amount_label": label,
            "asset": owner_asset(asset) if asset is not None else None,
            "asset_members": [owner_asset(member) for member in members],
            "lines": [
                {
                    "line_number": line["line_no"], "code": line["account"],
                    "account": ACCOUNT_NAMES.get(line["account"], "未配置名称的科目"),
                    "debit_fen": line["debit"], "credit_fen": line["credit"],
                    "party": self.line_party(line, relations),
                    "source_label": self.asset_reference_label(asset_lines[line["line_no"]])
                    if line["line_no"] in asset_lines else self.line_source(line, relations),
                    "parties": [
                        {"id": r["party_id"], "name": r["party"], "amount_fen": r["amount_fen"]}
                        for r in relations
                        if r.get("line_number") == line["line_no"] and r["party_id"]
                    ],
                    "party_state": self.line_party_state(line, relations),
                    **({"asset": owner_asset(asset_lines[line["line_no"]])}
                       if line["line_no"] in asset_lines else {}),
                }
                for line in row["lines"]
            ],
        }
        voucher["has_business_progress"] = _voucher_profile_progress(
            voucher, self.owner_progress_profiles[calc["subject_id"]]
        )
        return voucher

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
        asset_references, asset_reference, asset_members, asset_lines, member_evidence = (
            self._voucher_asset_details(row)
        )
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
            "group": self.activity_classification[0][calc["id"], row["sign"] < 0],
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
    def __init__(self, engine, *, company_name="", companies=None, owner_review_request=None):
        self.engine, self.store = engine, engine.store
        self.company_name, self.companies = company_name, companies
        self._owner_review_request = owner_review_request

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
            quarters = [
                {
                    "key": f"{year}-Q{quarter}",
                    "year": year,
                    "quarter": quarter,
                    "label": f"{year} 年第 {quarter} 季度",
                }
                for year, quarter in quarter_keys
            ]
            return {
                "schema_version": 3,
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
            read_context = self._read_context(snapshot.connection, snapshot.as_of)
        else:
            with self.store.connection(read_only=True) as connection:
                read_context = self._read_context(connection, _today_china())
        return {
            "schema_version": 8,
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
        self, period: str | None = None, *, limit: int = 20,
        expected_version: str | None = None, section: str | None = None,
        cursor: str | None = None, voucher_version_id: str | None = None,
        voucher_number: int | None = None,
        preparation: Literal["complete", "deferred"] = "deferred",
    ):
        # The owner brief is a business projection. Full preparation and its
        # technical graphs belong to the separate AI-accountant read contract.
        if preparation not in {"complete", "deferred"}:
            raise KernelError("invalid_command", "不支持的准备检查投影")
        validate_page("brief", section, cursor, limit)
        if voucher_version_id is not None and voucher_number is not None:
            raise KernelError("invalid_command", "精确业务定位须只提供一个身份")
        if voucher_number is not None and (type(voucher_number) is not int or voucher_number < 1):
            raise KernelError("invalid_command", "业务编号须为正整数")
        with self._snapshot(period) as snap:
            if snap is None:
                return {**self._response(None, None), "schema_version": 17}
            self._check_page_version(snap, cursor, expected_version)
            # Authenticate the complete month's money first. Later scalar and
            # page reads can reuse this successful proof in this snapshot.
            position = _brief_amounts(snap)
            from .dashboard_brief_groups import (
                activity_group_page,
                activity_groups,
                vouchers_for_rows,
            )

            voucher_sort = "voucher-number/1"
            after = decode_cursor(snap, "brief", section, cursor, {})
            grouped_activity = activity_group_page(
                snap, after=after if section == "activity" else None, limit=limit,
            ) if section in {None, "activity"} else None
            summaries, _, _ = activity_groups(snap)
            if section == "vouchers":
                rows, page = snap.month_journal.page(after or 0, limit, include_lines=True)
            else:
                rows, page = [], None
            focused_row = None
            if voucher_version_id is not None or voucher_number is not None:
                found, _ = snap.month_journal.page(
                    0, 1, voucher_number=voucher_number, voucher_version_id=voucher_version_id,
                    include_lines=False,
                )
                if not found:
                    raise KernelError("dashboard_voucher_not_found", "所选月份没有这项精确业务")
                focused_row = found[0]
            displayed_rows = [*rows, *([focused_row] if focused_row else [])]
            displayed_vouchers = vouchers_for_rows(snap, displayed_rows)
            focused_parts = (
                snap.activity_components.get(focused_row["id"], ()) if focused_row else ()
            )
            focused = (
                _brief_activity_row(snap, focused_row)
                if focused_row and len(focused_parts) <= 1 else None
            )
            focused_voucher = next(
                (item for item in displayed_vouchers
                 if item["voucher_version_id"] == focused_row["id"]), None,
            ) if focused_row else None
            vouchers = displayed_vouchers[:len(rows)]
            _, counts = snap.activity_classification
            groups = [
                {"key": key, "label": label,
                 "event_count": sum(count for (group, _), count in counts.items() if group == key),
                 "group_count": sum(item["group"] == key for item in summaries.values()),
                 "type_counts": [
                     {"label": _name(kind), "count": count}
                     for (group, kind), count in sorted(counts.items()) if group == key
                 ]}
                for key, label in GROUPS.items()
                if any(group == key for group, _ in counts)
            ]
            funds = _funds(snap, summary_only=True)
            totals = snap.month_journal.totals()
            valid = (
                totals["debit"] == totals["credit"]
                and position["complete"]
            )
            from .dashboard_open_groups import open_group_page

            open_items = open_group_page(
                snap, after=after if section == "open_items" else None, limit=limit,
                summary_only=section not in {None, "open_items"},
            )
            collections = {}
            if section in {None, "activity"}:
                collections["activity"] = grouped_activity
            if section == "vouchers":
                collections["vouchers"] = {
                    "items": vouchers, "page": page,
                }
            if section in {None, "open_items"}:
                collection = open_items.pop("collection")
                collections["open_items"] = collection
            open_items.pop("collection", None)
            for category in open_items["categories"]:
                category.pop("groups", None)
            risks = []
            mapping_issues = [
                item for item in position["issues"] if item.get("field") == "account_mapping"
            ]
            if any("amount_fen" in item for item in mapping_issues):
                risks.append({"key": "month_amounts", "title": "本月经营金额尚需核对",
                              "impact": "本月收入、费用仅包含已确认分类的金额",
                              "status": "ai_reviewing"})
            if any("amount_fen" not in item for item in mapping_issues):
                risks.append({"key": "ending_amounts", "title": "月末账面金额尚需核对",
                              "impact": "部分月末余额尚未确认分类，不能作为完整结论",
                              "status": "ai_reviewing"})
            if valid is not True and not mapping_issues:
                risks.append({"key": "amounts", "title": "经营金额尚需核对",
                              "impact": "部分金额暂不能作为完整结论", "status": "ai_reviewing"})
            if open_items["complete"] is False:
                risks.append({"key": "settlements", "title": "待收待付尚需核对",
                              "impact": "已知余额暂不能代表全部款项", "status": "ai_reviewing"})
            if funds["total_fen"] is None:
                risks.append({"key": "funds_amounts", "title": "资金金额尚需核对",
                              "impact": "月末账面资金暂不能作为完整结论",
                              "status": "ai_reviewing"})
            commentary = snap.commentary
            from .dashboard_owner import owner_tasks
            def note(item):
                if item is None:
                    return None
                return {"id": item["id"], "text": item["text"],
                        "status": item["content_validity"]["status"]}
            data = {
                "generated_at": datetime.now(UTC).isoformat(),
                "month_state": snap.owner_month_state,
                "owner_review_request": self._owner_review_request(snap)
                if self._owner_review_request is not None and snap.owner_month_state == "open"
                else None,
                "management_commentary_details": {
                    "status": commentary["status"], "current": note(commentary.get("current")),
                    "latest": note(commentary.get("latest")),
                    "supplements": [note(item) for item in commentary.get("supplements", ())],
                },
                "activity_count": sum(item["member_count"] for item in summaries.values()),
                "group_count": len(summaries), "focused_activity": focused,
                "focused_activity_group": summaries[focused["group_key"]] if focused else None,
                "voucher_count": len(snap.month_journal), "focused_voucher": focused_voucher,
                "activity_groups": groups,
                "position": {key: position[key] for key in (
                    "month_revenue_fen", "month_expense_fen", "month_result_fen", "complete"
                )},
                "funds_overview": {key: funds[key] for key in (
                    "total_fen", "bank_fen", "cash_fen", "payment_platform_fen",
                    "inflow_fen", "outflow_fen", "net_change_fen", "internal_transfer_fen"
                )},
                "open_items": {key: open_items[key] for key in (
                    "receivable_count", "receivable_fen", "payable_count", "payable_fen",
                    "total_count", "group_count", "complete", "categories", "cutoff_period",
                    "current_cutoff_period",
                )},
                "risks": risks, "owner_tasks": owner_tasks(snap),
                "collections": collections,
            }
            if section is None:
                financial_position = _position(snap)
                data["financial_position"] = {
                    key: value for key, value in financial_position.items()
                    if not key.startswith("month_") and key != "issues"
                } | {
                    "bank_calculation": funds["bank_calculation"],
                    "issues": [
                        {"message": item["message"]}
                        | ({"amount_fen": item["amount_fen"]} if "amount_fen" in item else {})
                        for item in financial_position["issues"]
                    ],
                }
                data["workforce_cost"] = _brief_workforce_cost(snap)
                data["long_term_assets"] = _long_term_assets(snap)
            return {**self._response(snap, seal_collections(
                snap, "brief", data, {}, sort_profiles={"vouchers": voucher_sort}
            )),
                    "schema_version": 17}

    def brief_group(
        self, period: str | None = None, *, section: Literal["activity", "open_items"],
        group_key: str, limit: int = 20, cursor: str | None = None,
        expected_version: str | None = None,
    ):
        """Read one selected group's member page without rebuilding the brief."""
        validate_page("brief-group", section, cursor, limit)
        if not isinstance(group_key, str) or not re.fullmatch(r"[0-9a-f]{64}", group_key):
            raise KernelError("invalid_command", "业务组身份不正确")
        with self._snapshot(period) as snap:
            if snap is None:
                raise KernelError("dashboard_group_not_found", "没有可读取的业务月份")
            self._check_page_version(snap, cursor, expected_version)
            filters = {"group_key": group_key}
            after = decode_cursor(snap, "brief-group", section, cursor, filters)
            from .dashboard_brief_groups import (
                activity_group_members,
                exact_open_voucher_rows,
                vouchers_for_rows,
            )

            if section == "activity":
                rows, page = activity_group_members(snap, group_key, after=after, limit=limit)
                vouchers = vouchers_for_rows(snap, rows)
                items = [_brief_activity_row(snap, row) for row in rows]
            else:
                from .dashboard_open_groups import open_group_members

                member_page = open_group_members(snap, group_key, after=after, limit=limit)
                items, page = member_page["items"], member_page["page"]
                rows = exact_open_voucher_rows(snap, items)
                vouchers = vouchers_for_rows(snap, rows)
            voucher_page = {
                "total_count": len(vouchers), "filtered_count": len(vouchers),
                "returned_count": len(vouchers), "has_more": False, "next_cursor": None,
            }
            data = {
                "section": section, "group_key": group_key,
                "collections": {
                    "members": {"items": items, "page": seal_page(
                        snap, "brief-group", section, page, filters,
                    )},
                    "vouchers": {"items": vouchers, "page": voucher_page},
                },
            }
            return {**self._response(snap, data), "schema_version": 2}

    def funds(
        self,
        period: str | None = None,
        *,
        movement_account_type: str | None = None,
        movement_account_id: str | None = None,
        statement_account_id: str | None = None,
        movement_account_selection: Literal["all", "first"] = "all",
        limit: int = 20,
        expected_version: str | None = None,
        section: str | None = None,
        cursor: str | None = None,
        preparation: Literal["complete", "deferred"] = "deferred",
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
        if movement_account_selection not in {"all", "first"} or (
            movement_account_selection == "first"
            and (movement_account_type is not None or section is not None or cursor is not None)
        ):
            raise KernelError(
                "invalid_command", "自动选择首个资金账户只适用于未指定账户的完整首屏请求"
            )
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
                return {**self._response(None, None), "schema_version": 9}
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
                select_first_account=movement_account_selection == "first",
            )
            return {**self._response(snap, seal_collections(snap, "funds", data, filters)),
                    "schema_version": 9}

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
        limit: int = 20,
        expected_version: str | None = None,
        employee_filter: EmployeeFilter = "employment_active",
        employee_id: str | None = None,
        preparation: Literal["complete", "deferred"] = "deferred",
    ):
        validate_page("employees", section, cursor, limit)
        if preparation not in {"complete", "deferred"}:
            raise KernelError("invalid_command", "不支持的准备检查投影")
        if employee_id == "":
            raise KernelError("invalid_command", "须指定有效 employee_id")
        if employee_filter not in get_args(EmployeeFilter):
            raise KernelError("invalid_command", "不支持的员工筛选")
        filters = {"employee_filter": employee_filter, "employee_id": employee_id}
        with self._snapshot(period) as snap:
            if snap is None:
                return {**self._response(None, None), "schema_version": 11}
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
            if section:
                data["collections"] = {section: data["collections"][section]}
            return {**self._response(snap, seal_collections(snap, "employees", data, filters)),
                    "schema_version": 11}

    def assets(
        self,
        period: str | None = None,
        *,
        section: str | None = None,
        cursor: str | None = None,
        limit: int = 20,
        expected_version: str | None = None,
        asset_filter: str = "all",
        asset_id: str | None = None,
        project_id: str | None = None,
        preparation: Literal["complete", "deferred"] = "deferred",
    ):
        validate_page("assets", section, cursor, limit)
        if preparation not in {"complete", "deferred"}:
            raise KernelError("invalid_command", "不支持的准备检查投影")
        if asset_id == "" or project_id == "":
            raise KernelError("invalid_command", "须指定有效 asset_id 或 project_id")
        if asset_filter not in {"all", "active", "fixed", "intangible", "pending", "exited"}:
            raise KernelError("invalid_command", "不支持的资产筛选")
        filters = {"asset_filter": asset_filter, "asset_id": asset_id, "project_id": project_id}
        with self._snapshot(period) as snap:
            if snap is None:
                return {**self._response(None, None), "schema_version": 10}
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
            if section:
                data["collections"] = {section: data["collections"][section]}
            return {**self._response(snap, seal_collections(snap, "assets", data, filters)),
                    "schema_version": 10}

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
        allowed_slots=None,
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
            allowed_slots=allowed_slots,
        )
        if "collection_version" in result:
            result["page"]["collection_version"] = result.pop("collection_version")
        if section == "settlement_events":
            from .dashboard_owner import settlement_event_party_identity, settlement_event_view

            identities = [settlement_event_party_identity(item) for item in result["items"]]
            party_ids = {ident for ident, _ in identities if ident is not None}
            snap.metadata.prime_profiles("employee", party_ids)
            snap.metadata.prime_profiles("counterparty", party_ids)
            unnamed = {
                ident for ident in party_ids
                if not any(profiles["counterparty"].get(ident, {}).get("display_name")
                           for profiles in (snap.profiles, snap.current_profiles))
            }
            snap.metadata.payee_records.prime(unnamed)
            snap.metadata.current_payees.prime(unnamed)
            snap.metadata.tax_candidates.prime({
                ident for ident in unnamed
                if ident not in snap.payees and ident not in snap.current_payees
            })
            names = {ident: snap.party_details(ident, exact_identity=True) for ident in party_ids}
            for item in result["items"]:
                if item["source_business"]["kind"] == "opening_payroll_payable":
                    source = snap.query_calculation(item["source_calculation_id"])
                    item["purpose_component"] = source["fact_data"]["component"]
            result["items"] = [
                settlement_event_view(
                    item, party=names[ident]["name"]
                    if ident is not None and names[ident].get("source")
                    else missing,
                )
                for item, (ident, missing) in zip(result["items"], identities, strict=True)
            ]
        return result

    def business_status(
        self,
        period: str,
        subject_id: str,
        *,
        section: str | None = None,
        cursor: str | None = None,
        limit: int = 20,
        expected_version: str | None = None,
        as_of: str | None = None,
        settlement_view: Literal["historical", "current"] = "current",
        voucher_version_id: str | None = None,
        detail_scope_category: str | None = None,
    ):
        validate_page("business-status", section, cursor, limit)
        if settlement_view not in {"historical", "current"}:
            raise KernelError("invalid_command", "清偿口径须为 historical 或 current")
        if (voucher_version_id is None) != (detail_scope_category is None):
            raise KernelError("invalid_command", "分类明细须同时提供精确凭证和业务类别")
        if voucher_version_id == "" or detail_scope_category == "":
            raise KernelError("invalid_command", "分类明细身份不能为空")
        with self._snapshot(period) as snap:
            self._check_page_version(snap, cursor, expected_version)
            component, scoped_row = None, None
            if voucher_version_id is not None:
                rows, _ = snap.month_journal.page(
                    0, 1, voucher_version_id=voucher_version_id, include_lines=False,
                )
                if not rows or rows[0]["basis"]["subject_id"] != subject_id:
                    raise KernelError("dashboard_voucher_not_found", "所选月份没有这项精确业务")
                scoped_row = rows[0]
                component = next((
                    item for item in snap.activity_components.get(voucher_version_id, ())
                    if item["source_category"] == detail_scope_category
                ), None)
                if component is None:
                    raise KernelError("invalid_command", "所选精确凭证没有该业务类别")
            data = snap.queries._business_status(
                snap.connection,
                subject_id,
                period,
                as_of=as_of,
                summary=True,
                owner_projection=True,
            )
            snap.as_of = data["as_of"]
            filters = {
                "subject_id": subject_id,
                "as_of": data["as_of"],
                "settlement_view": settlement_view,
            }
            if component is not None:
                filters.update(voucher_version_id=voucher_version_id,
                               detail_scope_category=detail_scope_category)
            from .dashboard_owner import business_profiles, business_view, scope_business_status
            from .obligation_classification import classify_obligations

            data["display_profiles"] = business_profiles(
                snap, data, fact=scoped_row["basis"]["fact"] if scoped_row is not None else None,
            )
            if component is not None:
                source_headers = snap.reads.metadata(
                    component["source_calculation_ids"], state=False,
                )
                source_subjects = {item["subject_id"] for item in source_headers.values()}
                # A payment owns no payable of its own. Progress belongs to the
                # exact adopted sources, then is restricted by their stable keys.
                data["settlements"] = dict(snap.settlement_summary(subject_ids=source_subjects))
                data["current_followups"]["settlements"] = dict(snap.settlement_summary(
                    subject_ids=source_subjects, current=True,
                ))
            for settlements in (
                data["settlements"], data["current_followups"]["settlements"],
            ):
                settlements["obligations"] = classify_obligations(
                    snap.connection, settlements["obligations"], reads=snap.reads,
                )
            if component is not None:
                scope_business_status(data, component, scoped_row)
            projected = business_view(data)
            projected["settlement_view"] = settlement_view
            sections = (section,) if section else ("settlement_events",)
            projected["collections"] = {
                key: self._business_collection(
                    snap,
                    "business-status",
                    subject_id,
                    key,
                    cursor if key == section else None,
                    filters,
                    limit,
                    settlement_view=settlement_view,
                    allowed_slots={(voucher_version_id, index) for index in component["slots"]}
                    if component is not None else None,
                )
                for key in sections
            }
            response = self._response(
                snap, seal_collections(snap, "business-status", projected, filters)
            )
            response["schema_version"] = 9
            return response

    def quarterly_report(
        self,
        year: int,
        quarter: int,
        *,
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
                _issues_only=True,
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
                )
            plan = closed if closed["status"] == "ready" else opened
            response = _quarterly_view(
                plan,
                closed,
            )
            response["read_context"] = self._read_context(connection, as_of)
            if preparation == "deferred":
                response.update(
                    projection="dashboard_quarterly_report_deferred",
                )
            return response


def _voucher_profile_progress(voucher, context):
    """Match the progress panel's exact displayed-text and identity exclusions."""
    if context["deleted"]:
        return True
    profiles = context["profiles"]
    assets = [*([voucher["asset"]] if voucher["asset"] else []),
              *voucher["asset_members"],
              *(line["asset"] for line in voucher["lines"] if line.get("asset"))]
    visible = {voucher[field] for field in (
        "summary", "list_summary", "type", "business_amount_label"
    )}
    visible.update(asset.get("name") or asset.get("code") or "资产卡片" for asset in assets)
    for line in voucher["lines"]:
        visible.update((line["party"], line["source_label"]))
        visible.update(party["name"] for party in line["parties"])
    business = profiles["business"]["values"]
    if any(business.get(field) and business[field] not in visible for field in ("purpose", "note")):
        return True
    visible_parties = {
        party["id"] for line in voucher["lines"] for party in line["parties"]
        if len(line["parties"]) > 1 or line["party"] == party["name"]
    }
    visible_assets = {asset["asset_id"] for asset in assets}
    groups = ("employees", "counterparties", "assets", "fund_accounts")
    roles = defaultdict(set)
    for group in groups:
        for item in profiles.get(group, ()):
            if item.get("entity_id"):
                roles[item["entity_id"]].add(group)
    for group in groups:
        seen = set()
        for item in profiles.get(group, ()):
            ident = item.get("entity_id")
            if ident and ident in seen:
                continue
            seen.add(ident)
            if not item["values"].get("display_name"):
                continue
            if (not ident or len(roles[ident]) > 1 or group == "fund_accounts"
                    or group == "assets" and ident not in visible_assets
                    or group in {"employees", "counterparties"} and ident not in visible_parties):
                return True
    return False


def _brief_prime_voucher_profiles(snap, rows):
    """Prime the bounded page's adopted business objects in the same snapshot."""
    from .dashboard_owner import business_profiles
    from .entity_references import references_from_data

    subjects = {snap.calculation(row["basis_calculation_id"])["subject_id"] for row in rows}
    snap.owner_progress_profiles = {}
    if not subjects:
        return
    facts = {
        row["subject_id"]: row["fact_id"] for row in snap.connection.execute(
            "SELECT subject_id,fact_id FROM fact_current WHERE subject_id IN "
            "(SELECT value FROM json_each(?))", (canonical(sorted(subjects)),)
        )
    }
    selected = {
        row["subject_id"]: row["id"]
        for row in snap.queries._current_accounting_heads(snap.connection, subjects)
    }
    closes = snap.reads.authoritative_close_rows(periods=[snap.month])
    for part in snap.reads.close_adopted_results_many(closes, subjects=subjects):
        selected.update({
            item["subject_id"]: item["calculation_id"] for item in part.adopted_results
        })
    snap.reads.prime_calculations(set(selected.values()))
    unselected_facts = {fact for subject, fact in facts.items() if subject not in selected}
    if unselected_facts:
        from .integrity import verify_sources

        verify_sources(snap.engine, snap.connection, fact_ids=unselected_facts)
    selected_facts = {
        subject: snap.calculation(selected[subject])["fact"] if subject in selected
        else snap.fact(facts[subject])
        for subject in subjects if subject in facts
    }
    references = [
        item for fact in selected_facts.values()
        for item in references_from_data(fact["kind"], fact["data"], registry=snap.store.registry)
        if item["reference_type"] == "entity"
    ]
    ids = {item["entity_id"] for item in references}
    kinds = {row["id"]: row["kind"] for row in snap.connection.execute(
        "SELECT id,kind FROM entity WHERE id IN(SELECT value FROM json_each(?))",
        (canonical(sorted(ids)),),
    )}
    for profile_kind, entity_kinds in (
        ("counterparty", {"person", "organization"}),
        ("asset", {"asset", "project", "fund_product"}),
        ("fund_account", {"fund_account"}),
    ):
        snap.metadata.prime_profiles(
            profile_kind, {ident for ident, kind in kinds.items() if kind in entity_kinds}
        )
    for subject in subjects:
        snap.owner_progress_profiles[subject] = {
            "deleted": subject not in facts,
            "profiles": business_profiles(
                snap, {"identity": {"subject_id": subject}, "frozen_adoption": None,
                       "current_business_result": None},
                fact=selected_facts[subject], entity_kinds=kinds,
            ) if subject in selected_facts else {},
        }


def _brief_prime_activity(snap, rows):
    """Batch the business names and exact relations used by the bounded page."""
    identifiers = {row["basis_calculation_id"] for row in rows}
    resolutions = snap.reads.relations_many(identifiers)
    subjects, parties, asset_ids = set(), set(), set()
    for ident in identifiers:
        calc = snap.calculation(ident)
        subjects.add(calc["subject_id"])
        data = calc["fact"]["data"]
        parties.update(
            data[field] for field in (
                "employee_id", "person_id", "counterparty_id", "customer_id", "supplier_id",
                "owner_id", "buyer_id", "lender_id", "payer_id", "recipient_id"
            ) if data.get(field)
        )
        for field in ("allocations", "sources"):
            parties.update(
                item["recipient_id"] for item in data.get(field, ()) if item.get("recipient_id")
            )
        asset_ids.update(ident for ident, _ in snap.asset_identities(calc))
    for resolution in resolutions.values():
        parties.update(
            item["creditor_id"] for item in resolution["obligations"] if item.get("creditor_id")
        )
        parties.update(
            item["recipient_id"] for item in resolution["line_relations"]
            if item.get("recipient_id")
        )
    parties.discard("payroll-group")
    snap.metadata.prime_profiles("business", subjects)
    snap.management.prime(subjects)
    snap.metadata.prime_profiles("employee", parties)
    snap.metadata.prime_profiles("counterparty", parties)
    if asset_ids:
        snap.metadata.prime_profiles("asset", asset_ids)
    return resolutions


def _brief_activity_row(snap, row):
    """Keep exact business facts and amounts without materialising voucher cards."""
    from .dashboard_brief_groups import activity_groups

    parts = snap.activity_components.get(row["id"], ())
    component = row.get("activity_component")
    if component is None and len(parts) == 1:
        component = parts[0]
    if component is not None:
        data = row["basis"]["fact"]["data"]
        recognition = _recognition(data, str(YearMonth.from_ordinal(row["period"])))
        description = component["description"]
        if len(parts) == 1:
            profile = snap.profile("business", row["basis"]["subject_id"])
            supplied = list(dict.fromkeys(
                value.strip() for value in (profile.get("purpose"), profile.get("note"))
                if value and value.strip()
            ))
            if supplied:
                description += "；" + "；".join(supplied)
        return {
            "key": component["key"],
            "group_key": activity_groups(snap)[2][row["id"]][component["key"]],
            "voucher_number": row["number"], "subject_id": row["basis"]["subject_id"],
            "voucher_version_id": row["id"], "date": recognition["date"],
            "recognition": recognition, "title": component["title"],
            "description": description, "amount_fen": component["amount_fen"],
            "amount_label": component["amount_label"],
            "state": "更正原业务" if row["sign"] < 0 else "已入账",
            "party": component["party"], "group": component["group"],
            "detail_scope_category": component["source_category"],
        }
    calc = row["basis"]
    relations = snap.voucher_relations(calc, row["sign"])
    assets = [
        {**item, "name": item["name"] or (None if item["code"] else "资产名称未提供")}
        for item in snap.asset_references(calc)
    ]
    title, description = snap.business_summary(
        calc, row["sign"], relations, asset_references=assets, include_parties=False
    )
    data = calc["fact"]["data"]
    parties = {
        data[field] for field in (
            "employee_id", "person_id", "counterparty_id", "customer_id", "supplier_id",
            "owner_id", "buyer_id", "lender_id", "payer_id", "recipient_id"
        ) if data.get(field)
    }
    if data.get("payment_method") == "bank_batch":
        parties.discard(data.get("counterparty_id"))
    parties.update(item["party_id"] for item in relations if item.get("party_id"))
    for field in ("allocations", "sources"):
        parties.update(
            item["recipient_id"] for item in data.get(field, ()) if item.get("recipient_id")
        )
    parties.discard("payroll-group")
    amount, label = snap.business_amount(calc)
    recognition = _recognition(data, str(YearMonth.from_ordinal(row["period"])))
    from .dashboard_brief_groups import activity_groups

    group_key = activity_groups(snap)[2][row["id"]][row["id"]]
    return {
        "key": row["id"], "group_key": group_key, "detail_scope_category": None,
        "voucher_number": row["number"], "subject_id": calc["subject_id"],
        "voucher_version_id": row["id"],
        "date": recognition["date"], "recognition": recognition,
        "title": title, "description": description,
        "amount_fen": row["sign"] * amount if amount is not None else None,
        "amount_label": label, "state": "更正原业务" if row["sign"] < 0 else "已入账",
        "party": "、".join(snap.party(ident) for ident in sorted(parties)),
        "group": snap.activity_classification[0][calc["id"], row["sign"] < 0],
    }


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


def _profit_amounts(values):
    """Signed posted operating amounts and unmapped month amounts."""
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
    issues = [
        {
            "field": "account_mapping",
            "message": "存在未映射的非零账户余额，本月收入、费用不含该金额",
            "semantics": "accounting",
            "account": account,
            "amount_fen": value,
        }
        for account, value in values.items()
        if value and account not in KNOWN_POSITION_ACCOUNTS
    ]
    return revenue, expense, revenue - expense, issues


def _brief_amounts(snap):
    """Read posted operating money, without constructing a balance sheet.

    The journal selector retains actual posting periods and exact frozen
    adoption. Check its month amounts against the derived month projection;
    an inconsistent projection is a content error, not an owner question.
    """
    amounts = snap.month_journal.account_amounts()
    projected = {account: [debit, credit]
                 for account, debit, credit in snap.month_account_gross if debit or credit}
    if amounts != projected:
        raise KernelError(
            "content_integrity_failed", "本月金额与正式入账不一致", component="monthly_account"
        )
    revenue, expense, result, issues = _profit_amounts(
        {account: debit - credit for account, (debit, credit) in amounts.items()}
    )
    issues.extend(
        {
            "field": "account_mapping",
            "message": "存在未映射的非零账户余额",
            "account": account,
        }
        for account, amount in snap.accounts.items()
        if amount and account not in KNOWN_POSITION_ACCOUNTS
    )
    if snap.opening_selection["unestablished_state_selections"]:
        issues.append({"field": "opening.selection", "message": "期初采用依据尚未建立"})
    return {
        "month_revenue_fen": revenue,
        "month_expense_fen": expense,
        "month_result_fen": result,
        "complete": not issues,
        "issues": issues,
    }


def _position_party_rows(snap):
    """Keep exact party ownership separate from scalar ledger classification."""
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

    position_obligations = settlement_position_rows(
        snap.connection, snap.period, RECLASS, reads=snap.reads
    )
    from .report_classification_directory import classification_directory_scope

    has_classification_type = snap.connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='fact_report_classification'"
    ).fetchone() is not None
    # Keep the sparse party-child scan, but do not request directory membership
    # for future sources. Frozen adoption still matters for replaced revisions
    # and damaged headers; neither these candidates nor current status prove it.
    party_keys = (
        {
            row[0]
            for row in snap.connection.execute(
                "SELECT DISTINCT c.voucher_version_id "
                "FROM fact_report_classification_counterparties child "
                "CROSS JOIN fact_report_classification c ON c.revision_id=child.revision_id "
                "LEFT JOIN fact_revision f ON f.id=c.revision_id "
                "WHERE f.id IS NULL OR f.period<=? OR EXISTS("
                "SELECT 1 FROM close_reference r INDEXED BY close_reference_lookup "
                "WHERE r.reference_type='fact' AND r.reference_id=c.revision_id "
                "AND r.path='readiness.financial_reports.facts[*]' AND r.close_period<=?)",
                (snap.month, snap.month),
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
    obligation_totals = defaultdict(int)
    for obligation in position_obligations:
        if not obligation["unknown"]:
            obligation_totals[obligation["account"]] += (
                obligation["remaining"] if obligation["category"] == "receivable"
                else -obligation["remaining"]
            )
    # A complete obligation aggregate can be used directly only if it covers
    # the ledger account. A zero net account may still contain opposite party
    # balances, so absence of obligations also requires exact party ownership.
    fallback_accounts.update(
        account for account in set(balances) & set(RECLASS)
        if account not in obligation_totals or balances[account] != obligation_totals[account]
    )
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
    net_party_lines = None
    if fallback_accounts and not classifications and has_classification_type:
        from .report_projection import (
            _PARTY_POSITION_SUMMARY_UNSAFE,
            _party_balance_position_lines,
            party_balance_rows,
        )

        summarized = _party_balance_position_lines(
            snap.engine, snap.connection, snap.month, reads=snap.reads,
            _accounts=fallback_accounts, _validate_party_keys=True,
        )
        if summarized is _PARTY_POSITION_SUMMARY_UNSAFE:
            # Historical/external snapshots and uncertain decoded party shapes
            # retain their complete rows and original unknown/error semantics.
            party_rows = party_balance_rows(
                snap.engine, snap.connection, snap.month, reads=snap.reads
            )
            if party_rows is not None:
                rows.extend(row for row in party_rows if row["account"] in fallback_accounts)
                projected_fallback = set(fallback_accounts)
        elif summarized is not None:
            net_party_lines = summarized
            projected_fallback = set(fallback_accounts)
    journal_accounts = (
        (fallback_accounts - projected_fallback) | (set(balances) - known)
        if fallback_accounts
        else set()
    )
    if journal_accounts:
        # Only affected accounts need saved lines and their party relation.
        # Do not hydrate all historical JournalRow/calculation display records.
        journal = snap.journal.select(accounts=journal_accounts)
        query, parameters = journal.sql()
        events = list(snap.connection.execute(query, parameters))
        lines = journal.authenticated_selected_lines(events)
        lines_by_source = defaultdict(list)
        for event in events:
            lines_by_source[event["basis_calculation_id"]].extend(
                line for line in lines[event["id"]] if line["account"] in journal_accounts
            )
        resolutions = snap.reads.report_line_relations_many(lines_by_source)
        for event in events:
            resolution = resolutions[event["basis_calculation_id"]]
            for line in lines[event["id"]]:
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
        opening_ids = {
            item["calculation_id"] for item in snap.opening_selection["state_results"]
        }
        opening_outcomes = snap.reads.verify_selected_content(opening_ids)
        for outcome in opening_outcomes.values():
            members = outcome["values"].get("members")
            if members is None:
                members = [outcome]
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
    return rows, net_party_lines, source_issues


def _position(snap):
    balances = snap.accounts
    rows, net_party_lines, source_issues = _position_party_rows(snap)
    position = classify_financial_position(rows, _net_party_lines=net_party_lines)
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

    revenue, expense, monthly, month_issues = _profit_amounts(snap.month_accounts)
    source_issues.extend(month_issues)
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


def _contribution_identity(company_id, kind, fact_data, component):
    """Group presentation by formal employee/month facts, never by display names."""
    empty = {
        "contribution_group_key": None,
        "contribution_component": None,
        "payroll_period": None,
    }
    if kind not in {"payroll", "payroll_bounded", "opening_payroll_payable"} or component not in {
        "employee_social", "employer_social", "employee_housing", "employer_housing"
    }:
        return empty
    period = fact_data.get(
        "payroll_period" if kind == "opening_payroll_payable" else "period"
    )
    period = period if isinstance(period, str) and re.fullmatch(
        r"[0-9]{4}-(0[1-9]|1[0-2])", period
    ) else None
    employee_id = fact_data.get("employee_id")
    key = None
    if period is not None and isinstance(employee_id, str) and employee_id and company_id:
        key = hashlib.sha256(
            json.dumps([company_id, employee_id, period], ensure_ascii=False, separators=(",", ":"))
            .encode("utf-8")
        ).hexdigest()
    return {
        "contribution_group_key": key,
        "contribution_component": component,
        "payroll_period": period,
    }


def _open_item_display(kind, component, fact_data, settlement_party_id):
    """Describe an adopted obligation without inventing a creditor identity."""
    party_id, missing_party, description = settlement_party_id, "未提供", _name(kind)
    if kind in PAYROLL_KINDS | {"opening_payroll_payable"}:
        if component in _PAYROLL_COMPONENT_NAMES:
            employee_id = fact_data.get("employee_id")
            if isinstance(employee_id, str) and employee_id:
                party_id = employee_id
            description = _PAYROLL_COMPONENT_NAMES[component]
        if kind == "annual_bonus" and component in {"net", "tax"}:
            description = "实发奖金" if component == "net" else "奖金代扣个税"
    elif kind in LABOR_KINDS | {"labor_project_cost"}:
        if component in {"net", "tax", "withheld_tax"}:
            person_id = fact_data.get("person_id")
            if isinstance(person_id, str) and person_id:
                party_id = person_id
            description = "劳务报酬" if component == "net" else "代扣个人所得税"
    elif kind == "income_tax_assessment":
        missing_party = "税务机关"
        if component == "tax":
            description = "企业所得税"
    elif kind == "tax_assessment":
        missing_party = "税务机关"
        if component == "vat":
            description = "增值税"
        elif component == "surtax":
            description = "附加税"
        elif isinstance(component, str) and component.startswith("vat_credit_"):
            description = "增值税抵减与退税"
        elif isinstance(component, str) and component.startswith("surtax_credit_"):
            description = "附加税抵减与退税"
    elif kind == "pass_through" and component == "remittance":
        missing_party = "最终收款人未具名"
    return party_id, missing_party, description


def _open_items(snap, *, after=None, limit=100, summary_only=False):
    from .dashboard_party_supplements import (
        pass_through_party_supplements,
        supplemented_party_field,
    )
    from .dashboard_sort import open_item_order
    from .settlement_projection import settlement_dashboard_open

    historical = settlement_dashboard_open(
        snap.connection,
        snap.period,
        after=after,
        limit=limit,
        summary_only=summary_only,
        reads=snap.reads,
        order_rows=None if summary_only else lambda rows: open_item_order(snap, rows),
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

    sources = historical["obligations"]
    supplements = pass_through_party_supplements(snap, sources)
    current_by_key = {row["key"]: row for row in current["obligations"]}
    page = historical["page"]
    display_fact_ids = {
        source["source_fact_id"]
        for source in sources
        if source["key"] in selected
        and (source.get("source_business") or {}).get("kind")
        in PAYROLL_KINDS | LABOR_KINDS | {"opening_payroll_payable", "labor_project_cost"}
        and source.get("source_fact_id")
    }
    display_facts = snap.reads.facts(display_fact_ids) if display_fact_ids else {}
    buckets, categories, items = defaultdict(list), [], []
    for source in sources:
        buckets[source["category_key"]].append(source)
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
            source_fact = display_facts.get(source.get("source_fact_id"), {})
            fact_data = source_fact.get("data", {})
            component = (
                fact_data.get("component")
                if business.get("kind") == "opening_payroll_payable"
                else source.get("name")
            )
            party_id, missing_party, description = _open_item_display(
                business.get("kind", ""), component, fact_data, settlement_party_id,
            )
            supplement = supplements.get(source["key"])
            if party_id is None and supplement is not None:
                party_id = supplement["party_id"]
            row = {
                **source,
                "id": source["key"],
                "category_key": key,
                "voucher": "查看精确来源",
                "party_key": party_id or source["key"],
                **supplemented_party_field(
                    snap, party_id, missing=missing_party, supplement=supplement,
                ),
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
                **_contribution_identity(
                    snap.store.company_id, business.get("kind"), fact_data, component,
                ),
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

    item_order = {source["key"]: index for index, source in enumerate(historical["obligations"])}
    items.sort(key=lambda item: item_order[item["id"]])
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
        ),
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
        }
        for calc in state_calculations
        if calc["kind"] in PAYROLL_KINDS
        and calc["posting_period"] == snap.month
        and calc["subject_id"] not in posted
    )
    return rows


def _workforce_payroll_aggregates(rows, identity, *, include_details=True):
    aggregates = {}
    for row in rows:
        calc, sign = row["basis"], row["sign"]
        data, values = calc["fact"]["data"], calc["outcome"]["values"]
        item = aggregates.setdefault(
            identity(calc),
            {
                **dict.fromkeys(_WORKFORCE_MONEY_KEYS, 0),
                **({"batches": set(), "periods": set(), "areas": set(), "scopes": set()}
                   if include_details else {}),
            },
        )
        if include_details:
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
    return aggregates


def _workforce_employee_amounts(snap, aggregates):
    """Shared posted payroll amounts, separate from roster/detail presentation."""
    sums = {key: sum(item[key] for item in aggregates.values()) for key in _WORKFORCE_MONEY_KEYS}
    sums["personal_deduction_fen"] = (
        sums["employee_social_insurance_fen"] + sums["employee_housing_fund_fen"]
        + sums["individual_income_tax_fen"]
    )
    controlled = (
        sums["gross_salary_fen"] + sums["annual_bonus_fen"]
        + sums["employer_social_insurance_fen"] + sums["employer_housing_fund_fen"]
    )
    ledger = sum(
        value for account, value in snap.month_accounts.items()
        if account in {"560201", "560101", "540101"}
    )
    return sums, controlled, ledger, ledger - controlled


def _workforce_labor_amounts(snap, payroll_rows, *, with_withholding=False, verify_sources=False):
    fields = ("gross_fen", "withholding_method") if with_withholding else ("gross_fen",)
    labor_rows = metric_rows(snap.month_journal.select(kinds=LABOR_KINDS), fields)
    capital_rows = metric_rows(
        snap.month_journal.select(kinds={"labor_project_cost"}), ("capitalized_fen",)
    )
    amount_rows = (*payroll_rows, *labor_rows, *capital_rows)
    calculation_ids = {row["calculation_id"] for row in amount_rows}
    if calculation_ids:
        snap.reads.verify_selected_content(calculation_ids)
    if verify_sources:
        from .entity_references import verify_hits

        fact_ids = {row["basis"]["fact_id"] for row in amount_rows}
        if fact_ids:
            verify_hits(
                snap.connection, [{"fact_id": ident} for ident in fact_ids],
                identity_match="recorded" if snap.close else "current",
                registry=snap.store.registry,
            )
    labor = sum(
        row["sign"] * row["basis"]["outcome"]["values"]["gross_fen"] for row in labor_rows
    )
    capitalized = sum(
        row["sign"] * row["basis"]["outcome"]["values"]["capitalized_fen"] for row in capital_rows
    )
    return labor_rows, capital_rows, labor, capitalized


def _brief_workforce_cost(snap):
    rows = _workforce_payroll_rows(snap)
    aggregates = _workforce_payroll_aggregates(rows, lambda _calc: None, include_details=False)
    sums, controlled, ledger, adjustment = _workforce_employee_amounts(snap, aggregates)
    labor_rows, capital_rows, labor, capitalized = _workforce_labor_amounts(
        snap, rows, with_withholding=True, verify_sources=True
    )
    modes = {row["basis"]["outcome"]["values"]["withholding_method"] for row in labor_rows}
    employee = {
        "has_activity": bool(rows) or ledger != 0,
        "breakdown_available": adjustment == 0,
        "reason": None if adjustment == 0 else "账面人工成本包含尚需核对的调整。",
        "total_fen": ledger, "controlled_total_fen": controlled,
        "settlement_adjustment_fen": adjustment,
        **{key: sums[key] for key in (
            "gross_salary_fen", "annual_bonus_fen", "employer_social_insurance_fen",
            "employer_housing_fund_fen", "employee_social_insurance_fen",
            "employee_housing_fund_fen",
        )},
    }
    return {
        "has_activity": employee["has_activity"] or bool(labor_rows) or bool(capital_rows),
        "total_fen": ledger + labor,
        "capitalized_labor_fen": capitalized,
        "employee": employee,
        "personal_labor": {
            "has_activity": bool(labor_rows), "breakdown_available": True, "reason": None,
            "total_fen": labor, "gross_remuneration_fen": labor,
            "withholding_note": "本月无个人劳务费用。" if not labor_rows
            else "尚未记录扣缴及申报。" if "not_withheld_not_filed" in modes
            else "已确认按毛额支付、未扣税。" if "gross_paid_without_withholding" in modes
            else "已按核算事实确认扣税义务，付款及申报状态分别核对。",
        },
    }


def _prepare_people_asset_settlements(snap, subjects):
    """Keep the historical business amounts without reading current followups."""
    if not hasattr(snap, "people_asset_settlements"):
        snap.people_asset_settlements = {}
    missing = {subject for subject in subjects if subject not in snap.people_asset_settlements}
    if missing:
        summary = snap.settlement_summary(subject_ids=missing)
        for subject in missing:
            snap.people_asset_settlements[subject] = summary


def _people_asset_settlement(snap, calc):
    subject = calc["subject_id"]
    _prepare_people_asset_settlements(snap, {subject})
    summary = snap.people_asset_settlements[subject]
    return {
        "subject_id": subject,
        "cutoff_period": summary["cutoff_period"],
        "checking": bool(summary["issues"]),
        "obligations": [
            {"key": item["key"], "name": item["name"],
             "amount_fen": item["source_amount_fen"],
             **{field: item[field] for field in (
                 "paid_fen", "other_settled_fen", "remaining_fen",
                 "period_paid_fen", "period_other_settled_fen",
             )}}
            for item in summary["obligations"]
            if (item.get("source_business") or {}).get("subject_id") == subject
        ],
    }


def _employees(
    snap,
    *,
    sections=None,
    cursors=None,
    limit=20,
    employee_filter="employment_active",
    employee_id=None,
    summary_only=False,
    _full_wage_scope=False,
):
    cursors = cursors or {}
    requested = (
        set(sections) if sections is not None else {"employees", "labor_sources"}
    )
    money_keys = tuple(key for key in _WORKFORCE_MONEY_KEYS if key != "tax_reported_salary_fen")
    wage_kinds = PAYROLL_KINDS | {"opening_payroll_payable"}
    wage_heads = (
        payroll_list_head_metadata(snap, wage_kinds, line_count_period=snap.period)
        if not _full_wage_scope else None
    )
    scoped_wage_heads = wage_heads is not None
    if wage_heads is None:
        wage_heads = payroll_head_metadata(snap, wage_kinds, line_count_period=snap.period)
    rows = _workforce_payroll_rows(snap, wage_heads=wage_heads)
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
        missing = {ident for ident in fact_ids if ident not in wage_scalars}
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
        confirmed_employees = current_role_matches(
            snap.connection, fact_ids, "employee", registry=snap.store.registry
        )
        missing_role_facts = {head["fact_id"] for head in wage_heads} - confirmed_employees.keys()
        if missing_role_facts:
            load_wage_scalars(missing_role_facts)
    if scoped_wage_heads and any(
        head.get("roster_employee_id") is not None
        and confirmed_employees.get(head["fact_id"]) != head["roster_employee_id"]
        for head in wage_heads
    ):
        raise KernelError("entity_reference_corrupt", "工资人员名单见证与精确来源归属不匹配")
    aggregates = _workforce_payroll_aggregates(
        rows,
        lambda calc: confirmed_employees.get(calc["fact_id"], calc["fact"]["data"]["employee_id"]),
    )
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
    proven = verified_payroll_heads(snap, wage_heads)
    if representative_heads:
        if any(
            (selected := proven.get(head["subject_id"])) is None
            or selected["id"] != head["id"]
            or selected["fact_id"] != head["fact_id"]
            for head in wage_heads
        ):
            raise KernelError(
                "content_integrity_failed", "工资人员名单的采用身份不匹配", component="payroll"
            )
        verify_wage_facts(
            {row["basis"]["fact_id"] for row in rows}
            | {head["fact_id"] for head in representative_heads.values()}
        )
    known.update(wage_identity(head["fact_id"]) for head in wage_heads)
    known.update(unestablished_employees)
    if hasattr(snap, "metadata"):
        snap.metadata.prime_profiles("employee", known)
    employee_states, current_employment_states = {}, {}
    for ident in sorted(known):
        # Current personnel status is independent of the selected month's frozen
        # profile and employment interval. The metadata uses this read transaction.
        current = snap.current_profiles.get("employee", {}).get(ident, {})
        employment_state = current.get("employment_state")
        if employment_state is None:
            # Preserve already registered employment without inventing a more
            # precise personnel fact. An explicit new unknown never falls back.
            employment_state = "unknown"
            start, end = current.get("employment_start"), current.get("employment_end")
            conflict = start and end and (
                start[:7] > end[:7] or len(start) == len(end) == 10 and start > end
            )
            ended = end and (end <= snap.as_of if len(end) == 10 else end < snap.as_of[:7])
            if not conflict:
                if ended:
                    if current.get("employment_status") != "active":
                        employment_state = "departed"
                elif current.get("employment_status") == "active":
                    employment_state = "regular"
        current_employment_states[ident] = employment_state
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
            or employee_filter == "unknown"
            and (employee_states[ident][0] == "unknown" or ident in unestablished_employees)
            or employee_filter == "ended"
            and employee_states[ident][0] == "ended"
            or employee_filter == "employment_active"
            and current_employment_states[ident] == "regular"
            or employee_filter.startswith("employment_")
            and employee_filter != "employment_active"
            and current_employment_states[ident] == employee_filter.removeprefix("employment_")
        )
        and (employee_id is None or ident == employee_id)
    ]
    selected_ids, employee_page = page_keys(
        filtered_ids, cursors.get("employees"), limit, total_count=len(known)
    )
    selected_ids = set(selected_ids) if not summary_only else set()
    if "employees" not in requested:
        selected_ids = set()
    net_payments = defaultdict(int)
    direct_payments, outstanding = defaultdict(int), defaultdict(int)
    settlement_checking = False
    summarized_wages = {
        head["subject_id"]: head
        for head in wage_heads
        if wage_identity(head["fact_id"]) in known
    }
    frozen_net = None
    if summarized_wages or scoped_wage_heads:
        from .settlement_freeze import frozen_employee_net_summary

        frozen_net = frozen_employee_net_summary(
            snap.connection, snap.period, employee_ids=known,
            wage_heads=None if scoped_wage_heads else wage_heads, reads=snap.reads
        )
        if frozen_net is not None and not snap.close:
            from .dashboard_reads import payroll_cohort_identities_match

            if not payroll_cohort_identities_match(snap, wage_heads, confirmed_employees):
                frozen_net = None
        if frozen_net is not None:
            settlement_checking = frozen_net["checking"]
            for ident, amounts in frozen_net["employees"].items():
                outstanding[ident] = amounts["remaining_fen"]
                direct_payments[ident] = amounts["period_paid_fen"]
                net_payments[ident] = _nullable_sum((
                    amounts["period_paid_fen"], amounts["period_other_settled_fen"]
                ))
    if scoped_wage_heads and frozen_net is None:
        # Attribution conflicts and unsupported frozen scopes retain the full
        # existing reducer. No partial roster can narrow month-end wage money.
        return _employees(
            snap, sections=sections, cursors=cursors, limit=limit,
            employee_filter=employee_filter, employee_id=employee_id,
            summary_only=summary_only, _full_wage_scope=True,
        )
    if summarized_wages and frozen_net is None:
        # The month-end unpaid amount includes unchanged prior-month sources.
        # A changed-keys-only payment projection cannot supply that amount.
        summary = snap.settlement_summary(
            subject_ids=set(summarized_wages), include_history_counts=False
        )
        settlement_checking = summary["complete"] is False or bool(summary["issues"])
        payment_rows = summary["obligations"]
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
            if any(
                (selected := proven.get(subject)) is None
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
        remaining_parts = defaultdict(list)
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
                remaining_parts[ident].append(obligation["remaining_fen"])
                net_parts[ident].extend(
                    (obligation["period_paid_fen"], obligation["period_other_settled_fen"])
                )
        for ident in known:
            outstanding[ident] = _nullable_sum(remaining_parts[ident])
            direct_payments[ident] = _nullable_sum(paid_parts[ident])
            net_payments[ident] = _nullable_sum(net_parts[ident])
    items, all_items = [], []
    for ident in sorted(known):
        info = snap.profile("employee", ident)
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
        items.append(
            {
                "employee_id": ident,
                "selection_status": "established",
                "name": person["name"],
                "period_state": state,
                "employment_state": current_employment_states[ident],
                "period_state_label": {
                    "unknown": "未确认人员在册状态",
                    "not_started": "本月早于已提供的开始日期",
                    "ended": "已确认结束或不在册",
                    "in_period": "已确认在册",
                }[state],
                "in_period": in_period,
                "employment_start_date": start,
                "employment_end_date": end,
                "has_payroll_activity": bool(amounts["batches"]),
                "batch_count": len(amounts["batches"]),
                "recorded_net_payments_fen": net_payments[ident],
                "direct_net_payments_fen": direct_payments[ident],
                "outstanding_net_fen": outstanding[ident],
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
        items.append(
            {
                "employee_id": ident,
                "name": snap.party(ident),
                "selection_status": "unestablished",
                "employment_state": current_employment_states[ident],
                **dict.fromkeys(
                    (
                        *money_keys,
                        "personal_deduction_fen",
                        "company_cost_fen",
                        "recorded_net_payments_fen",
                        "direct_net_payments_fen",
                        "other_net_settlements_fen",
                        "outstanding_net_fen",
                    ),
                    None,
                ),
            }
        )
    items.sort(key=lambda item: item["employee_id"])
    unknown = sum(item["in_period"] is None for item in all_items)
    sums, controlled, ledger, adjustment = _workforce_employee_amounts(snap, aggregates)
    labor_rows, capital_rows, labor_cost, capitalized_labor = _workforce_labor_amounts(snap, rows)
    labor_items = []
    labor_heads = adopted_head_metadata(
        snap, LABOR_KINDS | {"labor_project_cost"}, posting_period=snap.period
    )
    labor_identities = verified_scalar_facts(snap, labor_heads) if employee_id is not None else {}
    labor_keys, labor_page = page_keys(
        [
            head["id"]
            for head in sorted(labor_heads, key=lambda row: (row["period"], row["subject_id"]))
            if employee_id is None or labor_identities[head["fact_id"]]["person_id"] == employee_id
        ],
        cursors.get("labor_sources"),
        limit,
        total_count=len(labor_heads),
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
    _prepare_people_asset_settlements(snap, {calc["subject_id"] for calc in displayed_labor})
    for calc in displayed_labor:
        if calc["kind"] not in LABOR_KINDS | {"labor_project_cost"}:
            continue
        data, values = calc["fact"]["data"], calc["outcome"]["values"]
        labor_items.append(
            {
                "source_id": calc["subject_id"],
                "period": data["period"],
                "person_id": data["person_id"],
                "name": snap.party(data["person_id"]),
                "capitalized": calc["kind"] == "labor_project_cost",
                "project_id": data.get("project_id"),
                "gross_fen": values["gross_fen"],
                "net_fen": values["net_fen"],
                "booked_tax_fen": values["tax_fen"],
                "withholding_method": values["withholding_method"],
                "withholding_label": {
                    "not_withheld_not_filed": "尚未记录扣缴及申报",
                    "gross_paid_without_withholding": "已按毛额付款、未扣税",
                    "net_after_withholding": "已确认净额及扣税义务",
                }[values["withholding_method"]],
                **_people_asset_settlement(snap, calc),
            }
        )
    labor_items = sorted(labor_items, key=lambda item: (item["period"], item["source_id"]))
    from .settlement_projection import settlement_labor_outstanding_net

    outstanding_wages = None if unestablished_employees else _nullable_sum(
        outstanding[ident] for ident in known
    )
    outstanding_labor = settlement_labor_outstanding_net(
        snap.connection, snap.period, reads=snap.reads
    )
    return {
        "outstanding_remuneration_fen": _nullable_sum((outstanding_wages, outstanding_labor)),
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
            "contributions_only_count": sum(
                item["wage_tax_scope"] == "contributions_only" for item in all_items
            ),
            "ledger_cost_fen": ledger,
            "checking": bool(unestablished_employees) or adjustment != 0 or settlement_checking,
            "direct_net_payments_fen": None if unestablished_employees else _nullable_sum(
                direct_payments[ident] for ident in known
            ),
            "other_net_settlements_fen": None if unestablished_employees else _nullable_sum(
                net_payments[ident] - direct_payments[ident]
                if net_payments[ident] is not None and direct_payments[ident] is not None else None
                for ident in known
            ),
            "outstanding_net_fen": outstanding_wages,
        },
        "employee_id": employee_id,
        "employee_filter": employee_filter,
        "collections": {
            "employees": {"items": items, "page": employee_page},
            "labor_sources": {"items": labor_items, "page": labor_page},
        },
        "workforce_cost": {
            "total_fen": ledger + labor_cost,
            "capitalized_labor_fen": capitalized_labor,
            "personal_labor_fen": labor_cost,
        },
    }


def _asset_card_sources(snap, *, activation_identities=False):
    """Select and check card identities without loading consumption history."""
    # Prove the selected batch identities before the generic head guard runs.
    # The guard may reuse exact frozen member identities in this owned
    # snapshot; publication discovery and independent heads remain unchanged.
    activation_events = (
        snap.queries._selected_asset_activation_identities(snap.connection, snap.period)
        if activation_identities else None
    )
    kinds = ASSET_KINDS | {
        "reimbursed_asset_batch", "asset_activation", "asset_disposal",
        "project_cost", "labor_project_cost",
    }
    heads = adopted_head_metadata(snap, kinds, read_line_counts=False)
    metadata = verified_adopted_head_identities(snap, heads, kinds=kinds)
    selected = {
        subject: CalculationView(snap, record) for subject, record in metadata.items()
    }
    # Activation members have no independent publication. Keep their exact
    # batch adoption and reversal selection rather than treating them as heads.
    member_heads = {}
    if activation_events is None:
        activation_events = snap.asset_member_events(kinds={"asset_activation"})
    for event in activation_events:
        subject = event["subject_id"]
        if event["direction"] < 0:
            if member_heads.get(subject, {}).get("calculation_id") == event["calculation_id"]:
                member_heads.pop(subject, None)
        else:
            member_heads[subject] = event
    for subject, event in member_heads.items():
        old = selected.get(subject)
        if old is None or YearMonth(event["adoption_period"]).ordinal >= old["posting_period"]:
            selected[subject] = (
                CalculationView(snap, {
                    "id": event["calculation_id"], "subject_id": event["subject_id"],
                    "fact_id": event["fact_id"], "kind": event["kind"],
                    "period": event["calculation_period"], "posting_period": None,
                    "result_digest": event["result_digest"],
                })
                if event.get("frozen_identity") else snap.calculation(event["calculation_id"])
            )
    acquisition_rows = [calc for calc in selected.values() if calc["kind"] in ASSET_KINDS]
    batches = [calc for calc in selected.values() if calc["kind"] == "reimbursed_asset_batch"]
    lifecycle = [calc for calc in selected.values() if calc["kind"] in {
        "asset_activation", "asset_disposal",
    }]
    projects = [calc for calc in selected.values() if calc["kind"] in {
        "project_cost", "labor_project_cost",
    }]
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


def _active_asset_counts(snap):
    """Count adopted month-end identities without assembling asset cards."""
    from .integrity import verify_sources

    kinds = ASSET_KINDS | {"reimbursed_asset_batch", "asset_activation", "asset_disposal"}
    activation_events = snap.asset_member_events(kinds={"asset_activation"})
    heads = adopted_head_metadata(snap, kinds, read_line_counts=False)
    selected = verified_adopted_head_identities(snap, heads, kinds=kinds)
    # Batch members have no independent publication. Retain the same ordered
    # replacement/reversal selection as the asset page, before counting in SQL.
    member_heads = {}
    for event in activation_events:
        subject = event["subject_id"]
        if event["direction"] < 0:
            if member_heads.get(subject, {}).get("calculation_id") == event["calculation_id"]:
                member_heads.pop(subject, None)
        else:
            member_heads[subject] = event
    for subject, event in member_heads.items():
        old = selected.get(subject)
        if old is None or YearMonth(event["adoption_period"]) >= YearMonth(old["posting_period"]):
            selected[subject] = {"fact_id": event["fact_id"], "kind": event["kind"]}
    if not selected:
        return {}
    verify_sources(
        snap.engine, snap.connection, fact_ids={calc["fact_id"] for calc in selected.values()},
    )
    return dict(snap.connection.execute(
        "WITH selected AS ("
        "SELECT json_extract(value,'$[0]') fact_id,json_extract(value,'$[1]') kind "
        "FROM json_each(?)), acquisition_rows AS ("
        "SELECT f.asset_id,f.asset_type,0 opening FROM selected s "
        "CROSS JOIN fact_asset f ON f.revision_id=s.fact_id WHERE s.kind='asset' UNION ALL "
        "SELECT f.asset_id,f.asset_type,0 FROM selected s "
        "CROSS JOIN fact_reimbursed_asset f ON f.revision_id=s.fact_id "
        "WHERE s.kind='reimbursed_asset' UNION ALL "
        "SELECT f.asset_id,f.asset_type,1 FROM selected s "
        "CROSS JOIN fact_opening_asset f ON f.revision_id=s.fact_id "
        "WHERE s.kind='opening_asset' UNION ALL "
        "SELECT f.asset_id,f.asset_type,0 FROM selected s "
        "CROSS JOIN fact_reimbursed_asset_batch_assets f ON f.revision_id=s.fact_id "
        "WHERE s.kind='reimbursed_asset_batch'), acquisitions AS ("
        "SELECT asset_id,asset_type,max(opening) opening FROM acquisition_rows "
        "GROUP BY asset_id,asset_type), lifecycle_rows AS ("
        "SELECT f.asset_id,1 activated,0 disposed FROM selected s "
        "CROSS JOIN fact_asset_activation f ON f.revision_id=s.fact_id "
        "WHERE s.kind='asset_activation' UNION ALL "
        "SELECT f.asset_id,0,1 FROM selected s CROSS JOIN fact_asset_disposal f "
        "ON f.revision_id=s.fact_id WHERE s.kind='asset_disposal'), lifecycle AS ("
        "SELECT asset_id,max(activated) activated,max(disposed) disposed "
        "FROM lifecycle_rows GROUP BY asset_id) "
        "SELECT asset_type,count(*) FROM acquisitions a LEFT JOIN lifecycle l "
        "ON l.asset_id=a.asset_id WHERE (opening=1 OR l.activated=1) "
        "AND coalesce(l.disposed,0)=0 "
        "GROUP BY asset_type",
        (canonical(sorted((calc["fact_id"], calc["kind"]) for calc in selected.values())),),
    ))


def _long_term_assets(snap):
    """Reuse checked ledger totals, including pending assets and project costs."""
    counts = _active_asset_counts(snap)
    accounts = snap.accounts
    return {
        "net_fen": sum(
            accounts[account]
            for account in ("1601", "1701", "1604", "189901", "4301", "1602", "1702")
        ),
        "fixed_active_count": counts.get("fixed", 0),
        "intangible_active_count": counts.get("intangible", 0),
    }


def _asset_payment_summary(summary, source_calculations):
    """Summarize exact source obligations without allocating them to the card."""
    subjects = {source["subject_id"] for source in source_calculations}
    obligations = {
        item["key"]: item for item in summary["obligations"]
        if (item.get("source_business") or {}).get("subject_id") in subjects
    }
    fields = {"amount_fen": "source_amount_fen", "paid_fen": "paid_fen",
              "other_settled_fen": "other_settled_fen", "remaining_fen": "remaining_fen"}
    unknown = any(item[field] is None for item in obligations.values() for field in fields.values())
    return {
        "obligation_count": len(obligations),
        "checking": bool(subjects and summary["issues"]),
        **{name: None if unknown else sum(item[field] for item in obligations.values())
           for name, field in fields.items()},
    }


def _asset_open_batch_owners(connection, through: int, after: int | None) -> set[str]:
    """Locate all consumed batch owners in the publication tail, including baselines.

    These IDs are only candidates for asset_members_many's source/member proof.
    A known close gives a real lower range bound; a nullable OR would leave the
    baseline branch scanning every earlier publication despite that boundary.
    """
    scope = "p.posting_period<=?"
    parameters = [through]
    if after is not None:
        scope = "p.posting_period>? AND " + scope
        parameters.insert(0, after)
    query = " UNION ".join(
        "SELECT c.id FROM calculation_publication p JOIN calculation c "
        f"ON c.id=p.{column} WHERE {scope} AND c.kind='asset_consumption_month'"
        for column in ("calculation_id", "baseline_calculation_id")
    )
    return {row[0] for row in connection.execute(query, parameters * 2)}


def _assets(
    snap,
    *,
    sections=None,
    cursors=None,
    limit=20,
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
    sources = _asset_card_sources(
        snap, activation_identities=not summary_only and "assets" in requested,
    )
    acquisitions, asset_details = sources["acquisitions"], sources["details"]
    activations, disposals = sources["activations"], sources["disposals"]
    project_calculations = sources["projects"]
    scalar = lifecycle_scalar = sources["scalar"]
    # Closed carrying balances are rooted by asset key; the open tail is
    # checked against its publication and category seals in the same snapshot.
    # A current, undisposed card can recover cumulative charge from cost less
    # carrying without decoding every older member outcome. Other cards keep
    # their exact historical member walk below.
    from .period_balances import balance_totals

    candidates = {
        ident
        for ident, calc in acquisitions.items()
        if ident not in disposals and calc["kind"] != "opening_asset"
    }
    asset_keys = {ident: f"asset:{ident}:carrying" for ident in candidates}
    carrying_keys = set(asset_keys.values())
    carrying = (
        {
            row["key"]: row["amount"]
            for row in balance_totals(
                snap.connection, snap.month, "asset", carrying_keys, reads=snap.reads
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
                    if effect["category"] == "asset" and effect["key"] in carrying_keys:
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
        snap.reads.verify_balance_periods(snap.month, "asset", {snap.month})
        # Frozen carrying roots cover closed amounts. The open tail still
        # consumes complete batch publications, whose member attribution must
        # remain intact even when the owner page displays no timing history.
        closed = snap.connection.execute(
            "SELECT max(period) FROM period_close WHERE period<=?", (snap.month,),
        ).fetchone()[0]
        open_owners = _asset_open_batch_owners(snap.connection, snap.month, closed)
        retained = snap.reads._report_snapshot_cache.get(
            ("asset_owner_identity_selection", snap.month)
        )
        snap.reads.asset_members_many(
            open_owners, _decoded_owners=retained[3] if retained is not None else None,
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
    charges, monthly_charges = defaultdict(int), defaultdict(int)
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
            "json_extract(c.outcome,'$.values.consumption_fen') ELSE 0 END) AS monthly "
            f"FROM ({query}) j JOIN calculation c "
            "ON c.id=j.basis_calculation_id JOIN fact_asset_consumption f "
            "ON f.revision_id=c.fact_id GROUP BY f.asset_id",
            [snap.month, *parameters],
        ):
            charges[row["asset_id"]] = row["total"]
            monthly_charges[row["asset_id"]] = row["monthly"]
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
                "SELECT c.id,json_extract(c.outcome,'$.values.consumption_fen') amount "
                "FROM json_each(?) ids JOIN calculation c ON c.id=ids.value",
                (json.dumps(sorted(consumption_ids)),),
            )
        }
        if consumption_ids
        else {}
    )
    for event in full_batch_events:
        if event["kind"] == "asset_consumption":
            amount = consumption_values[event["calculation_id"]]["amount"] * event["direction"]
            charges[event["asset_id"]] += amount
            if event["adoption_period"] == snap.period:
                monthly_charges[event["asset_id"]] += amount
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
    settlement_subjects = {
        calc["subject_id"] for ident, calc in acquisitions.items()
        if ident in selected and calc["kind"] != "opening_asset"
        and not page_facts[calc["fact_id"]]["data"].get("acceptance_id")
        and not page_facts[calc["fact_id"]]["data"].get("project_sources")
    }
    payment_summary = (
        snap.settlement_summary(subject_ids=settlement_subjects | source_subjects,
                                include_history_counts=False)
        if settlement_subjects | source_subjects else {"obligations": [], "issues": []}
    )
    snap.metadata.prime_profiles("asset", selected)
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
        item = {
            "asset_id": ident,
            "asset_type": data["asset_type"],
            "code": profile.get("display_number") or f"资产 {index}",
            "name": profile.get("display_name") or "未提供资产名称",
            "category": data["asset_type"],
            "category_label": profile.get("category_label")
            or ("固定资产" if fixed else "无形资产"),
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
            "settlement_scope": source_scope,
            "payment_summary": _asset_payment_summary(payment_summary, source_calculations),
            "cost_fen": cost,
            "accumulated_charge_fen": accumulated,
            "month_charge_fen": monthly_charges[ident],
            "book_value_fen": 0 if disposal else cost - accumulated,
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
                    "disposal": None,
                }
            )
        else:
            item.update(
                {
                    "available_for_use_date": basis.get("in_use_date"),
                    "retirement": None,
                }
            )
        if disposal:
            disposed, values = disposal["fact"]["data"], disposal["outcome"]["values"]
            detail = {
                "date": disposed["disposal_date"],
                "book_value_fen": cost - accumulated,
            }
            if fixed:
                detail.update(
                    {
                        "kind": "sale" if disposed["disposal_kind"] == "sale" else "retirement",
                        "gross_proceeds_fen": disposed["gross_proceeds_fen"],
                        "gain_fen": max(values["gain_loss_fen"], 0),
                        "loss_fen": max(-values["gain_loss_fen"], 0),
                        "party": snap.party(disposed.get("buyer_id")),
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
    projects = [
        {
            "source_id": calc["subject_id"],
            "project_id": calc["outcome"]["values"]["project_id"],
            "period": calc["fact"]["data"]["period"],
            "kind": calc["kind"],
            "label": _name(calc["kind"]),
            "party": snap.party(
                calc["fact"]["data"].get("person_id") or calc["fact"]["data"].get("supplier_id")
            ),
            "cost_fen": calc["outcome"]["values"]["capitalized_fen"],
            "remaining_fen": project_balances[f"project-cost:{calc['subject_id']}"],
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
    checking = ledger_cost != card_cost or ledger_accumulated != card_accumulated
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
        "established_card_totals": None,
        "pending_fixed_count": fixed["pending_count"],
        "pending_fixed_cost_fen": fixed["pending_cost_fen"],
        "month_charge_fen": _nullable_sum(row["month_charge_fen"] for row in all_items),
        "month_acquired_count": sum(row["month_acquired"] for row in all_items),
        "month_acquired_fen": sum(monthly_acquired.values()),
        "month_cost_adjustment_fen": sum(monthly_adjusted.values()),
        "month_activated_count": sum(row["month_activated"] for row in all_items),
        "month_exited_count": sum(row["month_exited"] for row in all_items),
        "checking": checking,
        "fixed": fixed,
        "intangible": intangible,
        "asset_id": asset_id,
        "project_id": project_id,
        "asset_filter": asset_filter,
        "collections": {
            "assets": {"items": items, "page": asset_page},
            "projects": {
                "items": sorted(projects, key=lambda item: (item["project_id"], item["source_id"])),
                "page": project_page,
            },
        },
    }


def _quarterly_view(plan, closed):
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
    return {
        "schema_version": 5,
        "close_state": "open" if any(
            item["field"] in {"period", "closed_periods"} for item in closed["fact_issues"]
        ) else "closed",
        "readiness_state": "ready" if ready else "blocked",
        "status": "ready" if exportable else "in_progress" if ready else "blocked",
        "status_label": "可下载" if exportable else "结账后可下载" if ready else "AI 会计核对中",
        "headline": "季度财务报表",
        "message": "当前报表已具备下载条件。" if exportable else (
            "相关月份结账后可下载；当前金额为试算结果。" if ready
            else "AI 会计正在核对报表资料，如需您补充资料会另列待办。"
        ),
        "checked_at": datetime.now(UTC).isoformat(),
        "organization": plan["organization"],
        "period": plan["period"]
        | {"label": f"{plan['period']['year']} 年第 {plan['period']['quarter']} 季度"},
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
        "draft": not exportable,
        "export": {
            "available": exportable,
            "file_name": _workbook_name(plan),
            "preview_digest": closed["digest"] if exportable else None,
            "epochs": closed["epochs"] if exportable else None,
        },
    }
