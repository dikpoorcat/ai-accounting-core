"""Deterministic small-enterprise quarterly statements and tax-template export."""

from __future__ import annotations

import hashlib
import io
import json
import re
import uuid
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from importlib import resources
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .financial_statement_schemas import (
    ConfirmEnterpriseIncomeTaxQuarterRequest,
    ConfirmFinancialStatementClassificationRequest,
    EnterpriseIncomeTaxTreatment,
    FinancialStatementDetailCode,
    FinancialStatementInformationRequirement,
    FinancialStatementResult,
    FinancialStatementResultStatus,
    GetFinancialStatementRequirementsRequest,
    PreviewQuarterlyFinancialStatementsRequest,
)
from .ledger import AccountingPeriodError, Entry, account_balance_fen, create_voucher
from .models import (
    Account,
    AccountingPeriod,
    AccountingPeriodClose,
    AuditLog,
    BusinessEvent,
    EnterpriseIncomeTaxQuarterConfirmation,
    Evidence,
    FinancialStatementClassification,
    OpenItem,
    Organization,
    Settlement,
    TaxPeriod,
    UnifiedPayoutRun,
    UnifiedPayoutRunItem,
    Voucher,
    VoucherLine,
)
from .organization_profiles import profile_as_of
from .service import FinanceService

ACCOUNTING_RULE_VERSION = "small-enterprise-statements-2013-v1"
ACCOUNTING_RULE_EFFECTIVE_FROM = "2013-01-01"
ACCOUNTING_RULE_SOURCE_URL = "https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852734144.pdf"
TEMPLATE_FILE_NAME = "财务报表报送与信息采集（小企业会计准则）月季报.xlsx"
TEMPLATE_SHA256 = "BD83011100C52143B3B9A5CF5E13BA4DBE1899191BB1395BD92A5CE0F0C6F7FD"
TEMPLATE_PROFILE = "small-enterprise-monthly-quarterly-user-2026-08-26"
TEMPLATE_MAX_FEN = 999_999_999_999_900

BALANCE_NAMES = {
    1: "货币资金",
    2: "短期投资",
    3: "应收票据",
    4: "应收账款",
    5: "预付账款",
    6: "应收股利",
    7: "应收利息",
    8: "其他应收款",
    9: "存货",
    10: "其中：原材料",
    11: "在产品",
    12: "库存商品",
    13: "周转材料",
    14: "其他流动资产",
    15: "流动资产合计",
    16: "长期债券投资",
    17: "长期股权投资",
    18: "固定资产原价",
    19: "减：累计折旧",
    20: "固定资产账面价值",
    21: "在建工程",
    22: "工程物资",
    23: "固定资产清理",
    24: "生产性生物资产",
    25: "无形资产",
    26: "开发支出",
    27: "长期待摊费用",
    28: "其他非流动资产",
    29: "非流动资产合计",
    30: "资产合计",
    31: "短期借款",
    32: "应付票据",
    33: "应付账款",
    34: "预收账款",
    35: "应付职工薪酬",
    36: "应交税费",
    37: "应付利息",
    38: "应付利润",
    39: "其他应付款",
    40: "其他流动负债",
    41: "流动负债合计",
    42: "长期借款",
    43: "长期应付款",
    44: "递延收益",
    45: "其他非流动负债",
    46: "非流动负债合计",
    47: "负债合计",
    48: "实收资本（或股本）",
    49: "资本公积",
    50: "盈余公积",
    51: "未分配利润",
    52: "所有者权益合计",
    53: "负债和所有者权益总计",
}

PROFIT_NAMES = {
    1: "营业收入",
    2: "营业成本",
    3: "税金及附加",
    4: "其中：消费税",
    5: "营业税",
    6: "城市维护建设税",
    7: "资源税",
    8: "土地增值税",
    9: "城镇土地使用税、房产税、车船税、印花税",
    10: "教育费附加、矿产资源补偿费、排污费",
    11: "销售费用",
    12: "其中：商品维修费",
    13: "广告费和业务宣传费",
    14: "管理费用",
    15: "其中：开办费",
    16: "业务招待费",
    17: "研究费用",
    18: "财务费用",
    19: "其中：利息费用（收入以负号填列）",
    20: "投资收益（损失以负号填列）",
    21: "营业利润",
    22: "营业外收入",
    23: "其中：政府补助",
    24: "营业外支出",
    25: "其中：坏账损失",
    26: "无法收回的长期债券投资损失",
    27: "无法收回的长期股权投资损失",
    28: "自然灾害等不可抗力因素造成的损失",
    29: "税收滞纳金",
    30: "利润总额",
    31: "所得税费用",
    32: "净利润",
}

CASH_FLOW_NAMES = {
    1: "销售产成品、商品、提供劳务收到的现金",
    2: "收到其他与经营活动有关的现金",
    3: "购买原材料、商品、接受劳务支付的现金",
    4: "支付的职工薪酬",
    5: "支付的税费",
    6: "支付其他与经营活动有关的现金",
    7: "经营活动产生的现金流量净额",
    8: "收回短期投资、长期债券投资和长期股权投资收到的现金",
    9: "取得投资收益收到的现金",
    10: "处置固定资产、无形资产和其他非流动资产收回的现金净额",
    11: "短期投资、长期债券投资和长期股权投资支付的现金",
    12: "购建固定资产、无形资产和其他非流动资产支付的现金",
    13: "投资活动产生的现金流量净额",
    14: "取得借款收到的现金",
    15: "吸收投资者投资收到的现金",
    16: "偿还借款本金支付的现金",
    17: "偿还借款利息支付的现金",
    18: "分配利润支付的现金",
    19: "筹资活动产生的现金流量净额",
    20: "现金净增加额",
    21: "期初现金余额",
    22: "期末现金余额",
}

_SERVICE_COST_ROLES = {
    "service_cost",
    "labor_service_cost",
    "payroll_service_cost",
    "service_cost_depreciation",
    "service_cost_amortization",
}
_SALES_EXPENSE_ROLES = {
    "sales_expense",
    "payroll_sales_expense",
    "labor_sales_expense",
    "sales_depreciation_expense",
    "sales_amortization_expense",
}
_MANAGEMENT_EXPENSE_ROLES = {
    "general_expense",
    "payroll_management_expense",
    "labor_management_expense",
    "management_depreciation_expense",
    "management_amortization_expense",
}
_NONOPERATING_INCOME_ROLES = {"tax_relief_income", "fixed_asset_disposal_gain"}
_NONOPERATING_EXPENSE_ROLES = {
    "fixed_asset_disposal_loss",
    "intangible_asset_retirement_loss",
    "social_insurance_late_fee_expense",
}
_TAX_PAYABLE_ROLES = {
    "vat_payable",
    "deferred_output_vat",
    "surtax_payable",
    "individual_income_tax_payable",
    "enterprise_income_tax_payable",
}
_EMPLOYEE_COMPENSATION_ROLES = {
    "employee_salary_payable",
    "employer_social_payable",
    "employer_housing_fund_payable",
}
_OTHER_PAYABLE_ROLES = {
    "employee_payable",
    "owner_payable",
    "withheld_employee_social_payable",
    "withheld_employee_housing_fund_payable",
    "labor_remuneration_payable",
}
_RECLASSIFICATION_ROLES = {
    "accounts_receivable",
    "accounts_payable",
    "contract_liability",
    "prepayments",
    "employee_receivable",
    "other_receivable",
} | _OTHER_PAYABLE_ROLES
_BALANCE_DEBIT_LINES = {
    "short_term_investment": 2,
    "notes_receivable": 3,
    "dividends_receivable": 6,
    "interest_receivable": 7,
    "inventory": 9,
    "raw_materials": 10,
    "work_in_progress": 11,
    "finished_goods": 12,
    "consumable_materials": 13,
    "other_current_asset": 14,
    "long_term_bond_investment": 16,
    "long_term_equity_investment": 17,
    "fixed_asset_pending": 21,
    "construction_materials": 22,
    "fixed_asset_clearance": 23,
    "productive_biological_asset": 24,
    "development_expenditure": 26,
    "long_term_prepaid_expense": 27,
    "other_noncurrent_asset": 28,
}
_BALANCE_CREDIT_LINES = {
    "short_term_borrowing": 31,
    "notes_payable": 32,
    "interest_payable": 37,
    "dividends_payable": 38,
    "other_current_liability": 40,
    "long_term_borrowing": 42,
    "long_term_payable": 43,
    "deferred_income": 44,
    "other_noncurrent_liability": 45,
    "paid_in_capital": 48,
    "capital_reserve": 49,
    "surplus_reserve": 50,
}
_BALANCE_SPECIAL_ROLES = (
    {
        "fixed_asset_cost",
        "accumulated_depreciation",
        "intangible_asset_cost",
        "accumulated_amortization",
        "retained_earnings",
        "profit_distribution",
    }
    | _EMPLOYEE_COMPENSATION_ROLES
    | _TAX_PAYABLE_ROLES
)
_P_AND_L_ROLES = (
    {
        "service_revenue",
        "taxes_and_surcharges",
        "finance_expense",
        "borrowing_interest_expense",
        "enterprise_income_tax_expense",
    }
    | _SERVICE_COST_ROLES
    | _SALES_EXPENSE_ROLES
    | _MANAGEMENT_EXPENSE_ROLES
    | _NONOPERATING_INCOME_ROLES
    | _NONOPERATING_EXPENSE_ROLES
)

_ALLOWED_DETAILS = {
    "general_expense": {
        FinancialStatementDetailCode.MANAGEMENT_STARTUP.value,
        FinancialStatementDetailCode.MANAGEMENT_ENTERTAINMENT.value,
        FinancialStatementDetailCode.MANAGEMENT_RESEARCH.value,
        FinancialStatementDetailCode.MANAGEMENT_OTHER.value,
    },
    "sales_expense": {
        FinancialStatementDetailCode.SALES_MERCHANDISE_REPAIR.value,
        FinancialStatementDetailCode.SALES_ADVERTISING_PROMOTION.value,
        FinancialStatementDetailCode.SALES_OTHER.value,
    },
    "finance_expense": {
        FinancialStatementDetailCode.FINANCE_INTEREST.value,
        FinancialStatementDetailCode.FINANCE_OTHER.value,
    },
}


@dataclass(frozen=True)
class _LedgerRow:
    line: VoucherLine
    voucher: Voucher
    event: BusinessEvent
    account: Account

    @property
    def role(self) -> str | None:
        return self.account.system_role

    @property
    def debit_signed(self) -> int:
        return self.line.debit_fen - self.line.credit_fen

    @property
    def credit_signed(self) -> int:
        return self.line.credit_fen - self.line.debit_fen


def _canonical(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(payload: Any) -> tuple[str, str]:
    serialized = _canonical(payload)
    return serialized, hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _quarter_dates(year: int, quarter: int) -> tuple[date, date, date]:
    month = (quarter - 1) * 3 + 1
    start = date(year, month, 1)
    end_month = month + 2
    next_month = date(year + (end_month == 12), 1 if end_month == 12 else end_month + 1, 1)
    end = date.fromordinal(next_month.toordinal() - 1)
    return start, end, date(year, 1, 1)


def _requirement(
    code: str,
    message: str,
    *,
    fields: list[str] | None = None,
    data: dict[str, Any] | None = None,
) -> FinancialStatementInformationRequirement:
    return FinancialStatementInformationRequirement(
        code=code, message=message, fields=fields or [], data=data or {}
    )


class FinancialStatementService(FinanceService):
    """Calculate reports from immutable ledger facts and confirm bounded supporting facts."""

    def preview_quarterly(
        self, request: PreviewQuarterlyFinancialStatementsRequest
    ) -> FinancialStatementResult:
        organization = self.session.get(Organization, request.org_id)
        if organization is None:
            return self._statement_result(
                FinancialStatementResultStatus.REJECTED,
                errors=["ORGANIZATION_NOT_FOUND"],
            )
        quarter_start, quarter_end, year_start = _quarter_dates(request.year, request.quarter)
        profile = profile_as_of(
            self.session,
            org_id=request.org_id,
            as_of=quarter_end,
        )
        if profile.accounting_standard != "small_enterprise":
            return self._statement_result(
                FinancialStatementResultStatus.REJECTED,
                errors=["FINANCIAL_STATEMENT_ACCOUNTING_STANDARD_UNSUPPORTED"],
            )
        if profile.filing_cycle != "quarterly":
            return self._statement_result(
                FinancialStatementResultStatus.REJECTED,
                errors=["FINANCIAL_STATEMENT_FILING_CYCLE_UNSUPPORTED"],
            )
        missing: list[FinancialStatementInformationRequirement] = []
        control_start = organization.accounting_period_control_start_date
        if control_start is None or control_start > year_start:
            missing.append(
                _requirement(
                    "FINANCIAL_STATEMENT_OPENING_BALANCE_UNAVAILABLE",
                    "缺少完整年初余额依据，不能推断期初数。",
                    fields=["organization.accounting_period_control_start_date"],
                )
            )
        periods = list(
            self.session.scalars(
                select(AccountingPeriod)
                .where(
                    AccountingPeriod.org_id == request.org_id,
                    AccountingPeriod.start_date >= year_start,
                    AccountingPeriod.end_date <= quarter_end,
                )
                .order_by(AccountingPeriod.start_date)
            )
        )
        expected_months = request.quarter * 3
        if len(periods) != expected_months or any(item.status != "closed" for item in periods):
            missing.append(
                _requirement(
                    "FINANCIAL_STATEMENT_PERIOD_NOT_CLOSED",
                    "当年年初至季度末的全部自然月必须存在且已结账。",
                    data={
                        "expected_months": expected_months,
                        "found_months": len(periods),
                        "open_period_ids": [
                            str(item.id) for item in periods if item.status != "closed"
                        ],
                    },
                )
            )
        close_hashes = [
            item.calculation_hash
            for item in self.session.scalars(
                select(AccountingPeriodClose)
                .join(
                    AccountingPeriod,
                    AccountingPeriod.close_id == AccountingPeriodClose.id,
                )
                .where(
                    AccountingPeriod.org_id == request.org_id,
                    AccountingPeriod.start_date >= year_start,
                    AccountingPeriod.end_date <= quarter_end,
                )
                .order_by(AccountingPeriod.start_date)
            )
        ]
        if len(close_hashes) != expected_months:
            missing.append(
                _requirement(
                    "FINANCIAL_STATEMENT_CLOSE_SNAPSHOT_MISSING",
                    "存在缺少不可变结账快照的月份。",
                )
            )

        rows = self._ledger_rows(request.org_id, quarter_end)
        classifications, classification_missing = self._classification_state(
            request.org_id, rows, year_start, quarter_end
        )
        missing.extend(classification_missing)
        tax_confirmations, tax_missing = self._income_tax_state(
            request.org_id, request.year, request.quarter
        )
        missing.extend(tax_missing)

        balance = self._balance_sheet(rows, year_start, quarter_end, missing)
        profit = self._profit_statement(
            rows,
            classifications,
            quarter_start,
            year_start,
            quarter_end,
            request.org_id,
            missing,
        )
        cash_flow = self._cash_flow_statement(
            rows, request.org_id, quarter_start, year_start, quarter_end, missing
        )
        checks = self._checks(balance, profit, cash_flow)
        for check in checks:
            if not check["passed"]:
                missing.append(
                    _requirement(
                        check["code"],
                        "财务报表勾稽关系未通过，禁止生成税局导入文件。",
                        data=check,
                    )
                )
        for statement in (balance, profit, cash_flow):
            for values in statement.values():
                for key, value in values.items():
                    if key.endswith("_fen") and abs(value) > TEMPLATE_MAX_FEN:
                        missing.append(
                            _requirement(
                                "FINANCIAL_STATEMENT_TEMPLATE_AMOUNT_OUT_OF_RANGE",
                                "金额超过税局模板允许范围。",
                                data={"amount_fen": value},
                            )
                        )
        missing = self._deduplicate_requirements(missing)
        payload = {
            "organization": {
                "org_id": str(organization.id),
                "name": profile.name,
                "taxpayer_identification_number": profile.taxpayer_identification_number,
                "accounting_standard": profile.accounting_standard,
                "filing_cycle": profile.filing_cycle,
            },
            "period": {
                "year": request.year,
                "quarter": request.quarter,
                "quarter_start": quarter_start.isoformat(),
                "quarter_end": quarter_end.isoformat(),
                "year_start": year_start.isoformat(),
            },
            "template": {
                "profile": TEMPLATE_PROFILE,
                "sha256": TEMPLATE_SHA256,
                "file_name": TEMPLATE_FILE_NAME,
            },
            "rule": {
                "version": ACCOUNTING_RULE_VERSION,
                "effective_from": ACCOUNTING_RULE_EFFECTIVE_FROM,
                "source_url": ACCOUNTING_RULE_SOURCE_URL,
            },
            "source_close_hashes": close_hashes,
            "classification_ids": sorted(str(item.id) for item in classifications.values()),
            "enterprise_income_tax_confirmation_ids": [str(item.id) for item in tax_confirmations],
            "statements": {
                "balance_sheet": {str(k): v for k, v in balance.items()},
                "profit_statement": {str(k): v for k, v in profit.items()},
                "cash_flow_statement": {str(k): v for k, v in cash_flow.items()},
            },
            "checks": checks,
        }
        _serialized, calculation_hash = _hash(payload)
        data = {**payload, "calculation_hash": calculation_hash}
        status = (
            FinancialStatementResultStatus.NEEDS_INFORMATION
            if missing
            else FinancialStatementResultStatus.CALCULATED
        )
        return self._statement_result(
            status,
            calculation_hash=calculation_hash,
            missing=missing,
            trace=[
                {"stage": "period_selected", "quarter_end": quarter_end.isoformat()},
                {"stage": "ledger_projected", "voucher_line_count": len(rows)},
                {"stage": "statement_checks", "checks": checks},
                {"stage": "calculation_hashed", "calculation_hash": calculation_hash},
            ],
            data=data,
        )

    def get_requirements(
        self, request: GetFinancialStatementRequirementsRequest
    ) -> FinancialStatementResult:
        return self.preview_quarterly(
            PreviewQuarterlyFinancialStatementsRequest.model_validate(request.model_dump())
        )

    def confirm_classification(
        self, request: ConfirmFinancialStatementClassificationRequest
    ) -> FinancialStatementResult:
        request_hash = self._request_payload_hash(request)
        existing = self.session.scalar(
            select(FinancialStatementClassification).where(
                FinancialStatementClassification.org_id == request.org_id,
                FinancialStatementClassification.idempotency_key == request.idempotency_key,
            )
        )
        if existing is not None:
            if existing.request_payload_hash != request_hash:
                return self._statement_result(
                    FinancialStatementResultStatus.REJECTED,
                    errors=["FINANCIAL_STATEMENT_CLASSIFICATION_IDEMPOTENCY_MISMATCH"],
                )
            return self._statement_result(
                FinancialStatementResultStatus.POSTED,
                calculation_hash=existing.allocation_hash,
                classification_id=existing.id,
                data={"idempotent_replay": True},
            )
        target = self.session.execute(
            select(VoucherLine, Account, Voucher, BusinessEvent)
            .join(Account, Account.id == VoucherLine.account_id)
            .join(Voucher, Voucher.id == VoucherLine.voucher_id)
            .join(BusinessEvent, BusinessEvent.id == Voucher.event_id)
            .where(
                VoucherLine.org_id == request.org_id,
                VoucherLine.id == request.voucher_line_id,
            )
        ).one_or_none()
        if target is None:
            return self._statement_result(
                FinancialStatementResultStatus.REJECTED,
                errors=["FINANCIAL_STATEMENT_VOUCHER_LINE_NOT_FOUND"],
            )
        line, account, voucher, source_event = target
        role = account.system_role
        if role not in _ALLOWED_DETAILS or voucher.reversal_of_voucher_id is not None:
            return self._statement_result(
                FinancialStatementResultStatus.REJECTED,
                errors=["FINANCIAL_STATEMENT_CLASSIFICATION_TARGET_UNSUPPORTED"],
            )
        source_amount = line.debit_fen - line.credit_fen
        if source_amount <= 0:
            return self._statement_result(
                FinancialStatementResultStatus.REJECTED,
                errors=["FINANCIAL_STATEMENT_CLASSIFICATION_TARGET_NOT_EXPENSE"],
            )
        allocations = [item.model_dump(mode="json") for item in request.allocations]
        if sum(item["amount_fen"] for item in allocations) != source_amount:
            return self._statement_result(
                FinancialStatementResultStatus.REJECTED,
                errors=["FINANCIAL_STATEMENT_CLASSIFICATION_AMOUNT_MISMATCH"],
            )
        if any(item["detail_code"] not in _ALLOWED_DETAILS[role] for item in allocations):
            return self._statement_result(
                FinancialStatementResultStatus.REJECTED,
                errors=["FINANCIAL_STATEMENT_CLASSIFICATION_DETAIL_UNSUPPORTED"],
            )
        if len({item["detail_code"] for item in allocations}) != len(allocations):
            return self._statement_result(
                FinancialStatementResultStatus.REJECTED,
                errors=["FINANCIAL_STATEMENT_CLASSIFICATION_DETAIL_DUPLICATE"],
            )
        evidence_error = self._validate_evidence(request.org_id, request.evidence_references)
        if evidence_error:
            return self._statement_result(
                FinancialStatementResultStatus.REJECTED, errors=[evidence_error]
            )
        current = self._active_classification(request.org_id, request.voucher_line_id)
        if current is None and request.supersedes_classification_id is not None:
            return self._statement_result(
                FinancialStatementResultStatus.REJECTED,
                errors=["FINANCIAL_STATEMENT_CLASSIFICATION_SUPERSESSION_MISMATCH"],
            )
        if current is not None and request.supersedes_classification_id != current.id:
            return self._statement_result(
                FinancialStatementResultStatus.REJECTED,
                errors=["FINANCIAL_STATEMENT_CLASSIFICATION_SUPERSESSION_REQUIRED"],
            )
        allocation_payload, allocation_hash = _hash(
            {
                "voucher_line_id": str(line.id),
                "parent_role": role,
                "allocations": allocations,
                "supersedes_id": str(current.id) if current else None,
                "rule_version": ACCOUNTING_RULE_VERSION,
            }
        )
        record = FinancialStatementClassification(
            org_id=request.org_id,
            voucher_line_id=line.id,
            parent_role=role,
            allocations=allocations,
            allocation_payload=allocation_payload,
            allocation_hash=allocation_hash,
            supersedes_id=current.id if current else None,
            idempotency_key=request.idempotency_key,
            request_payload_hash=request_hash,
            confirmation_note=request.confirmation_note,
            evidence_references=[str(item) for item in request.evidence_references],
        )
        try:
            with self.session.begin_nested():
                self.session.add(record)
                self.session.flush()
        except IntegrityError:
            return self._statement_result(
                FinancialStatementResultStatus.REJECTED,
                errors=["FINANCIAL_STATEMENT_CLASSIFICATION_CONCURRENT_CONFLICT"],
            )
        self.session.add(
            AuditLog(
                org_id=request.org_id,
                event_id=source_event.id,
                action="financial_statement_classification_confirmed",
                details={
                    "classification_id": str(record.id),
                    "voucher_line_id": str(line.id),
                    "allocation_hash": allocation_hash,
                },
            )
        )
        return self._statement_result(
            FinancialStatementResultStatus.POSTED,
            calculation_hash=allocation_hash,
            classification_id=record.id,
            data={"idempotent_replay": False},
        )

    def confirm_enterprise_income_tax(
        self, request: ConfirmEnterpriseIncomeTaxQuarterRequest
    ) -> FinancialStatementResult:
        if self.session.get(Organization, request.org_id) is None:
            return self._statement_result(
                FinancialStatementResultStatus.REJECTED,
                errors=["ORGANIZATION_NOT_FOUND"],
            )
        request_hash = self._request_payload_hash(request)
        existing = self.session.scalar(
            select(EnterpriseIncomeTaxQuarterConfirmation).where(
                EnterpriseIncomeTaxQuarterConfirmation.org_id == request.org_id,
                EnterpriseIncomeTaxQuarterConfirmation.idempotency_key == request.idempotency_key,
            )
        )
        if existing is not None:
            if existing.request_payload_hash != request_hash:
                return self._statement_result(
                    FinancialStatementResultStatus.REJECTED,
                    errors=["ENTERPRISE_INCOME_TAX_IDEMPOTENCY_MISMATCH"],
                )
            voucher = (
                self.session.scalar(
                    select(Voucher).where(Voucher.event_id == existing.business_event_id)
                )
                if existing.business_event_id
                else None
            )
            return self._statement_result(
                FinancialStatementResultStatus.POSTED,
                calculation_hash=existing.calculation_hash,
                enterprise_income_tax_confirmation_id=existing.id,
                event_id=existing.business_event_id,
                voucher_id=voucher.id if voucher else None,
                voucher_number=voucher.voucher_number if voucher else None,
                data={"idempotent_replay": True},
            )
        duplicate = self.session.scalar(
            select(EnterpriseIncomeTaxQuarterConfirmation.id).where(
                EnterpriseIncomeTaxQuarterConfirmation.org_id == request.org_id,
                EnterpriseIncomeTaxQuarterConfirmation.calendar_year == request.year,
                EnterpriseIncomeTaxQuarterConfirmation.calendar_quarter == request.quarter,
            )
        )
        if duplicate is not None:
            return self._statement_result(
                FinancialStatementResultStatus.REJECTED,
                errors=["ENTERPRISE_INCOME_TAX_QUARTER_ALREADY_CONFIRMED"],
            )
        evidence_error = self._validate_evidence(request.org_id, request.evidence_references)
        if evidence_error:
            return self._statement_result(
                FinancialStatementResultStatus.REJECTED, errors=[evidence_error]
            )
        quarter_start, quarter_end, _year_start = _quarter_dates(request.year, request.quarter)
        event: BusinessEvent | None = None
        voucher: Voucher | None = None
        if request.treatment in {
            EnterpriseIncomeTaxTreatment.ACCRUE,
            EnterpriseIncomeTaxTreatment.REDUCE,
        }:
            assert request.posting_date is not None
            if not quarter_start <= request.posting_date <= quarter_end:
                return self._statement_result(
                    FinancialStatementResultStatus.REJECTED,
                    errors=["ENTERPRISE_INCOME_TAX_POSTING_DATE_OUTSIDE_QUARTER"],
                )
            if request.treatment is EnterpriseIncomeTaxTreatment.REDUCE:
                expense_balance = max(
                    0,
                    account_balance_fen(
                        self.session, request.org_id, "enterprise_income_tax_expense"
                    ),
                )
                payable_balance = max(
                    0,
                    -account_balance_fen(
                        self.session, request.org_id, "enterprise_income_tax_payable"
                    ),
                )
                if request.amount_fen > min(expense_balance, payable_balance):
                    return self._statement_result(
                        FinancialStatementResultStatus.REJECTED,
                        errors=["ENTERPRISE_INCOME_TAX_REDUCTION_EXCEEDS_BALANCE"],
                    )

        try:
            with self.session.begin_nested():
                if request.treatment in {
                    EnterpriseIncomeTaxTreatment.ACCRUE,
                    EnterpriseIncomeTaxTreatment.REDUCE,
                }:
                    assert request.posting_date is not None
                    event = BusinessEvent(
                        org_id=request.org_id,
                        idempotency_key=request.idempotency_key,
                        request_payload_hash=request_hash,
                        event_type="enterprise_income_tax_assessment",
                        status="draft",
                        description=f"{request.year}年第{request.quarter}季度企业所得税确认",
                        facts=request.model_dump(mode="json"),
                        business_date=quarter_end,
                        tax_obligation_date=quarter_end,
                        posting_date=request.posting_date,
                        rule_trace=[
                            {
                                "stage": "rule_selected",
                                "rule": ACCOUNTING_RULE_VERSION,
                                "source_url": ACCOUNTING_RULE_SOURCE_URL,
                            }
                        ],
                        rule_version=ACCOUNTING_RULE_VERSION,
                    )
                    self.session.add(event)
                    self.session.flush()
                    self._attach_evidence(event, request.evidence_references)
                    entries = (
                        [
                            Entry(
                                account_role="enterprise_income_tax_expense",
                                debit_fen=request.amount_fen,
                            ),
                            Entry(
                                account_role="enterprise_income_tax_payable",
                                credit_fen=request.amount_fen,
                            ),
                        ]
                        if request.treatment is EnterpriseIncomeTaxTreatment.ACCRUE
                        else [
                            Entry(
                                account_role="enterprise_income_tax_payable",
                                debit_fen=request.amount_fen,
                            ),
                            Entry(
                                account_role="enterprise_income_tax_expense",
                                credit_fen=request.amount_fen,
                            ),
                        ]
                    )
                    voucher = create_voucher(
                        self.session,
                        event=event,
                        posting_date=request.posting_date,
                        description=event.description,
                        entries=entries,
                    )
                    event.status = "posted"
                    self.session.flush()
                calculation = {
                    "org_id": str(request.org_id),
                    "year": request.year,
                    "quarter": request.quarter,
                    "treatment": request.treatment.value,
                    "amount_fen": request.amount_fen,
                    "posting_date": (
                        request.posting_date.isoformat() if request.posting_date else None
                    ),
                    "event_id": str(event.id) if event else None,
                    "voucher_id": str(voucher.id) if voucher else None,
                    "rule_version": ACCOUNTING_RULE_VERSION,
                    "source_url": ACCOUNTING_RULE_SOURCE_URL,
                }
                calculation_payload, calculation_hash = _hash(calculation)
                confirmation = EnterpriseIncomeTaxQuarterConfirmation(
                    org_id=request.org_id,
                    calendar_year=request.year,
                    calendar_quarter=request.quarter,
                    treatment=request.treatment.value,
                    amount_fen=request.amount_fen,
                    posting_date=request.posting_date,
                    business_event_id=event.id if event else None,
                    idempotency_key=request.idempotency_key,
                    request_payload_hash=request_hash,
                    calculation_payload=calculation_payload,
                    calculation_hash=calculation_hash,
                    confirmation_note=request.confirmation_note,
                    evidence_references=[str(item) for item in request.evidence_references],
                )
                self.session.add(confirmation)
                self.session.flush()
        except AccountingPeriodError as exc:
            return self._statement_result(
                FinancialStatementResultStatus.REJECTED, errors=[exc.code]
            )
        except IntegrityError:
            return self._statement_result(
                FinancialStatementResultStatus.REJECTED,
                errors=["ENTERPRISE_INCOME_TAX_CONCURRENT_CONFLICT"],
            )
        self.session.add(
            AuditLog(
                org_id=request.org_id,
                event_id=event.id if event else None,
                action="enterprise_income_tax_quarter_confirmed",
                details={
                    "confirmation_id": str(confirmation.id),
                    "year": request.year,
                    "quarter": request.quarter,
                    "treatment": request.treatment.value,
                    "calculation_hash": calculation_hash,
                },
            )
        )
        return self._statement_result(
            FinancialStatementResultStatus.POSTED,
            calculation_hash=calculation_hash,
            enterprise_income_tax_confirmation_id=confirmation.id,
            event_id=event.id if event else None,
            voucher_id=voucher.id if voucher else None,
            voucher_number=voucher.voucher_number if voucher else None,
            data={"idempotent_replay": False},
        )

    def export_quarterly_xlsx(
        self, request: PreviewQuarterlyFinancialStatementsRequest
    ) -> tuple[FinancialStatementResult, bytes | None]:
        result = self.preview_quarterly(request)
        if result.status is not FinancialStatementResultStatus.CALCULATED:
            return result, None
        return result, render_quarterly_template(result.data)

    def _ledger_rows(self, org_id: uuid.UUID, through_date: date) -> list[_LedgerRow]:
        rows = self.session.execute(
            select(VoucherLine, Voucher, BusinessEvent, Account)
            .join(Voucher, Voucher.id == VoucherLine.voucher_id)
            .join(BusinessEvent, BusinessEvent.id == Voucher.event_id)
            .join(Account, Account.id == VoucherLine.account_id)
            .where(
                VoucherLine.org_id == org_id,
                Voucher.posting_date <= through_date,
                Voucher.status.in_(("posted", "reversed")),
                BusinessEvent.status.in_(("posted", "reversed")),
            )
            .order_by(Voucher.posting_date, Voucher.id, VoucherLine.line_number)
        ).all()
        return [_LedgerRow(*row) for row in rows]

    def _active_classification(
        self, org_id: uuid.UUID, voucher_line_id: uuid.UUID
    ) -> FinancialStatementClassification | None:
        rows = list(
            self.session.scalars(
                select(FinancialStatementClassification).where(
                    FinancialStatementClassification.org_id == org_id,
                    FinancialStatementClassification.voucher_line_id == voucher_line_id,
                )
            )
        )
        superseded = {item.supersedes_id for item in rows if item.supersedes_id is not None}
        active = [item for item in rows if item.id not in superseded]
        return active[0] if len(active) == 1 else None

    def _classification_state(
        self,
        org_id: uuid.UUID,
        rows: list[_LedgerRow],
        start: date,
        end: date,
    ) -> tuple[
        dict[uuid.UUID, FinancialStatementClassification],
        list[FinancialStatementInformationRequirement],
    ]:
        candidates = [
            row
            for row in rows
            if start <= row.voucher.posting_date <= end
            and row.role in _ALLOWED_DETAILS
            and row.event.event_type not in {"bank_fee", "bank_interest_received", "reversal"}
            and row.voucher.reversal_of_voucher_id is None
            and row.debit_signed > 0
        ]
        classifications: dict[uuid.UUID, FinancialStatementClassification] = {}
        missing: list[FinancialStatementInformationRequirement] = []
        for row in candidates:
            current = self._active_classification(org_id, row.line.id)
            if current is None:
                missing.append(
                    _requirement(
                        "FINANCIAL_STATEMENT_CLASSIFICATION_REQUIRED",
                        "该费用凭证行需要明确报表明细分类。",
                        fields=["allocations"],
                        data={
                            "voucher_line_id": str(row.line.id),
                            "voucher_id": str(row.voucher.id),
                            "voucher_number": row.voucher.voucher_number,
                            "posting_date": row.voucher.posting_date.isoformat(),
                            "parent_role": row.role,
                            "amount_fen": row.debit_signed,
                            "allowed_detail_codes": sorted(_ALLOWED_DETAILS[row.role]),
                        },
                    )
                )
                continue
            if (
                current.parent_role != row.role
                or sum(int(item["amount_fen"]) for item in current.allocations) != row.debit_signed
            ):
                missing.append(
                    _requirement(
                        "FINANCIAL_STATEMENT_CLASSIFICATION_STALE",
                        "现有分类与凭证行金额或父级科目不一致。",
                        data={
                            "classification_id": str(current.id),
                            "voucher_line_id": str(row.line.id),
                        },
                    )
                )
                continue
            classifications[row.line.id] = current
        return classifications, missing

    def _income_tax_state(
        self, org_id: uuid.UUID, year: int, through_quarter: int
    ) -> tuple[
        list[EnterpriseIncomeTaxQuarterConfirmation],
        list[FinancialStatementInformationRequirement],
    ]:
        rows = list(
            self.session.scalars(
                select(EnterpriseIncomeTaxQuarterConfirmation)
                .where(
                    EnterpriseIncomeTaxQuarterConfirmation.org_id == org_id,
                    EnterpriseIncomeTaxQuarterConfirmation.calendar_year == year,
                    EnterpriseIncomeTaxQuarterConfirmation.calendar_quarter <= through_quarter,
                )
                .order_by(EnterpriseIncomeTaxQuarterConfirmation.calendar_quarter)
            )
        )
        by_quarter = {item.calendar_quarter: item for item in rows}
        missing: list[FinancialStatementInformationRequirement] = []
        active: list[EnterpriseIncomeTaxQuarterConfirmation] = []
        for quarter in range(1, through_quarter + 1):
            item = by_quarter.get(quarter)
            if item is None:
                missing.append(
                    _requirement(
                        "ENTERPRISE_INCOME_TAX_QUARTER_CONFIRMATION_REQUIRED",
                        "必须明确确认该季度企业所得税处理，零元也需确认。",
                        fields=["treatment", "amount_fen", "evidence_references"],
                        data={"year": year, "quarter": quarter},
                    )
                )
                continue
            if item.business_event_id is not None:
                event = self.session.get(BusinessEvent, item.business_event_id)
                if event is None or event.status != "posted":
                    missing.append(
                        _requirement(
                            "ENTERPRISE_INCOME_TAX_CONFIRMATION_EVENT_INACTIVE",
                            "企业所得税确认分录已冲正或不可用，需要重新确认。",
                            data={"confirmation_id": str(item.id)},
                        )
                    )
                    continue
            active.append(item)
        return active, missing

    def _balance_sheet(
        self,
        rows: list[_LedgerRow],
        year_start: date,
        end: date,
        missing: list[FinancialStatementInformationRequirement],
    ) -> dict[int, dict[str, Any]]:
        def values(as_of: date) -> dict[int, int]:
            result = {line: 0 for line in BALANCE_NAMES}
            role_debit: defaultdict[str, int] = defaultdict(int)
            role_credit: defaultdict[str, int] = defaultdict(int)
            counterparty: defaultdict[tuple[uuid.UUID, str, uuid.UUID | None], int] = defaultdict(
                int
            )
            account_balances: defaultdict[tuple[uuid.UUID, str, str | None], int] = defaultdict(int)
            cumulative_profit = 0
            for row in rows:
                if row.voucher.posting_date > as_of:
                    continue
                role = row.role
                if row.account.requires_bank_reconciliation or role in {"cash", "bank"}:
                    result[1] += row.debit_signed
                    continue
                if role in _P_AND_L_ROLES:
                    if row.account.category == "revenue":
                        cumulative_profit += row.credit_signed
                    else:
                        cumulative_profit -= row.debit_signed
                    continue
                if role in _RECLASSIFICATION_ROLES:
                    counterparty[(row.account.id, role, row.line.counterparty_id)] += (
                        row.debit_signed
                    )
                    continue
                if role:
                    role_debit[role] += row.debit_signed
                    role_credit[role] += row.credit_signed
                account_balances[(row.account.id, row.account.code, role)] += row.debit_signed
            for (
                _account_id,
                role,
                _counterparty_id,
            ), debit_balance in counterparty.items():
                if role == "accounts_receivable":
                    result[4 if debit_balance >= 0 else 34] += abs(debit_balance)
                elif role == "accounts_payable":
                    result[33 if debit_balance <= 0 else 5] += abs(debit_balance)
                elif role == "contract_liability":
                    result[34 if debit_balance <= 0 else 4] += abs(debit_balance)
                elif role == "prepayments":
                    result[5 if debit_balance >= 0 else 33] += abs(debit_balance)
                elif role in {"employee_receivable", "other_receivable"}:
                    result[8 if debit_balance >= 0 else 39] += abs(debit_balance)
                else:
                    result[39 if debit_balance <= 0 else 8] += abs(debit_balance)
            for role, line in _BALANCE_DEBIT_LINES.items():
                result[line] += role_debit[role]
            for role, line in _BALANCE_CREDIT_LINES.items():
                result[line] += role_credit[role]
            recognized_roles = (
                set(_BALANCE_DEBIT_LINES) | set(_BALANCE_CREDIT_LINES) | _BALANCE_SPECIAL_ROLES
            )
            for (account_id, account_code, role), balance in account_balances.items():
                if balance and role not in recognized_roles:
                    missing.append(
                        _requirement(
                            "FINANCIAL_STATEMENT_UNMAPPED_BALANCE_ACCOUNT",
                            "存在未映射且有余额的账户。",
                            data={
                                "account_id": str(account_id),
                                "account_code": account_code,
                                "account_role": role,
                            },
                        )
                    )
            result[18] = role_debit["fixed_asset_cost"]
            result[19] = role_credit["accumulated_depreciation"]
            result[20] = result[18] - result[19]
            result[25] = (
                role_debit["intangible_asset_cost"] - role_credit["accumulated_amortization"]
            )
            result[35] = sum(role_credit[role] for role in _EMPLOYEE_COMPENSATION_ROLES)
            for role in _TAX_PAYABLE_ROLES:
                balance = role_credit[role]
                if balance >= 0:
                    result[36] += balance
                else:
                    result[14] += -balance
            result[51] = (
                cumulative_profit
                + role_credit["retained_earnings"]
                + role_credit["profit_distribution"]
            )
            # The tax template exposes inventory and fixed-asset detail rows, but its
            # subtotals include only the corresponding parent/net rows.  Mirroring
            # those formulas avoids double-counting the disclosed detail amounts.
            result[15] = sum(result[line] for line in range(1, 10)) + result[14]
            result[29] = sum(result[line] for line in (16, 17, 20, 21, 22, 23, 24, 25, 26, 27, 28))
            result[30] = result[15] + result[29]
            result[41] = sum(result[line] for line in range(31, 41))
            result[46] = sum(result[line] for line in range(42, 46))
            result[47] = result[41] + result[46]
            result[52] = sum(result[line] for line in range(48, 52))
            result[53] = result[47] + result[52]
            return result

        ending = values(end)
        beginning = values(date.fromordinal(year_start.toordinal() - 1))
        return {
            line: {
                "name": BALANCE_NAMES[line],
                "ending_fen": ending[line],
                "beginning_fen": beginning[line],
            }
            for line in BALANCE_NAMES
        }

    def _profit_statement(
        self,
        rows: list[_LedgerRow],
        classifications: dict[uuid.UUID, FinancialStatementClassification],
        quarter_start: date,
        year_start: date,
        end: date,
        org_id: uuid.UUID,
        missing: list[FinancialStatementInformationRequirement],
    ) -> dict[int, dict[str, Any]]:
        original_lines: dict[tuple[uuid.UUID, int], uuid.UUID] = {
            (row.voucher.id, row.line.line_number): row.line.id
            for row in rows
            if row.voucher.reversal_of_voucher_id is None
        }

        def classification_for(
            row: _LedgerRow,
        ) -> FinancialStatementClassification | None:
            if row.voucher.reversal_of_voucher_id is None:
                return classifications.get(row.line.id)
            original_line_id = original_lines.get(
                (row.voucher.reversal_of_voucher_id, row.line.line_number)
            )
            return classifications.get(original_line_id) if original_line_id else None

        def values(start: date) -> dict[int, int]:
            result = {line: 0 for line in PROFIT_NAMES}
            for row in rows:
                if not start <= row.voucher.posting_date <= end:
                    continue
                role = row.role
                amount = (
                    row.credit_signed if row.account.category == "revenue" else row.debit_signed
                )
                if role == "service_revenue":
                    result[1] += amount
                elif role in _SERVICE_COST_ROLES:
                    result[2] += amount
                elif role == "taxes_and_surcharges":
                    result[3] += amount
                elif role in _SALES_EXPENSE_ROLES:
                    result[11] += amount
                elif role in _MANAGEMENT_EXPENSE_ROLES:
                    result[14] += amount
                elif role in {"finance_expense", "borrowing_interest_expense"}:
                    result[18] += amount
                    if (
                        role == "borrowing_interest_expense"
                        or row.event.event_type == "bank_interest_received"
                    ):
                        result[19] += amount
                elif role in _NONOPERATING_INCOME_ROLES:
                    result[22] += amount
                    if role == "tax_relief_income" and row.event.event_type == "tax_relief":
                        result[23] += amount
                elif role in _NONOPERATING_EXPENSE_ROLES:
                    result[24] += amount
                elif role == "enterprise_income_tax_expense":
                    result[31] += amount
                elif row.account.category in {"revenue", "expense"} and amount:
                    missing.append(
                        _requirement(
                            "FINANCIAL_STATEMENT_UNMAPPED_PROFIT_ACCOUNT",
                            "存在未映射的非零损益活动。",
                            data={
                                "account_id": str(row.account.id),
                                "account_code": row.account.code,
                            },
                        )
                    )
                classification = classification_for(row)
                if classification is not None:
                    direction = 1 if row.debit_signed >= 0 else -1
                    for item in classification.allocations:
                        detail_amount = int(item["amount_fen"]) * direction
                        code = item["detail_code"]
                        if code == FinancialStatementDetailCode.MANAGEMENT_STARTUP.value:
                            result[15] += detail_amount
                        elif code == FinancialStatementDetailCode.MANAGEMENT_ENTERTAINMENT.value:
                            result[16] += detail_amount
                        elif code == FinancialStatementDetailCode.MANAGEMENT_RESEARCH.value:
                            result[17] += detail_amount
                        elif code == FinancialStatementDetailCode.SALES_MERCHANDISE_REPAIR.value:
                            result[12] += detail_amount
                        elif code == FinancialStatementDetailCode.SALES_ADVERTISING_PROMOTION.value:
                            result[13] += detail_amount
                        elif code == FinancialStatementDetailCode.FINANCE_INTEREST.value:
                            result[19] += detail_amount
            tax_periods = self.session.scalars(
                select(TaxPeriod).where(
                    TaxPeriod.org_id == org_id,
                    TaxPeriod.start_date >= start,
                    TaxPeriod.end_date <= end,
                    TaxPeriod.status == "posted",
                )
            ).all()
            result[6] = sum(
                int(item.calculation.get("urban_maintenance_tax_fen", 0)) for item in tax_periods
            )
            result[10] = sum(
                int(item.calculation.get("education_surcharge_fen", 0))
                + int(item.calculation.get("local_education_surcharge_fen", 0))
                for item in tax_periods
            )
            result[21] = (
                result[1]
                - result[2]
                - result[3]
                - result[11]
                - result[14]
                - result[18]
                + result[20]
            )
            result[30] = result[21] + result[22] - result[24]
            result[32] = result[30] - result[31]
            return result

        current = values(quarter_start)
        ytd = values(year_start)
        return {
            line: {
                "name": PROFIT_NAMES[line],
                "current_fen": current[line],
                "year_to_date_fen": ytd[line],
            }
            for line in PROFIT_NAMES
        }

    def _cash_flow_statement(
        self,
        rows: list[_LedgerRow],
        org_id: uuid.UUID,
        quarter_start: date,
        year_start: date,
        end: date,
        missing: list[FinancialStatementInformationRequirement],
    ) -> dict[int, dict[str, Any]]:
        by_voucher: defaultdict[uuid.UUID, list[_LedgerRow]] = defaultdict(list)
        for row in rows:
            by_voucher[row.voucher.id].append(row)

        def cash_balance(as_of: date) -> int:
            return sum(
                row.debit_signed
                for row in rows
                if row.voucher.posting_date <= as_of
                and (row.account.requires_bank_reconciliation or row.role in {"cash", "bank"})
            )

        def values(start: date) -> dict[int, int]:
            result = {line: 0 for line in CASH_FLOW_NAMES}
            for voucher_rows in by_voucher.values():
                row0 = voucher_rows[0]
                if not start <= row0.voucher.posting_date <= end:
                    continue
                cash_delta = sum(
                    row.debit_signed
                    for row in voucher_rows
                    if row.account.requires_bank_reconciliation or row.role in {"cash", "bank"}
                )
                if cash_delta == 0:
                    continue
                event_type = row0.event.event_type
                source_event_id = row0.event.id
                source_event = row0.event
                if event_type == "reversal" and row0.voucher.reversal_of_voucher_id is not None:
                    original = by_voucher.get(row0.voucher.reversal_of_voucher_id)
                    if original:
                        event_type = original[0].event.event_type
                        source_event_id = original[0].event.id
                        source_event = original[0].event
                if event_type in {"internal_transfer", "cash_bank_transfer"}:
                    continue
                if event_type in {
                    "service_cash_sale",
                    "customer_receipt",
                    "customer_advance",
                    "customer_refund",
                }:
                    result[1] += cash_delta
                elif event_type in {
                    "other_income_received",
                    "bank_interest_received",
                    "refundable_deposit_return_received",
                }:
                    result[2] += cash_delta
                elif event_type == "expense_cash":
                    expense_roles = {
                        row.role for row in voucher_rows if row.account.category == "expense"
                    }
                    result[3 if expense_roles & _SERVICE_COST_ROLES else 6] += -cash_delta
                elif event_type == "supplier_payment":
                    self._allocate_settlement_cash(result, source_event_id, -cash_delta, missing)
                elif event_type == "employee_reimbursement":
                    expense_roles = {
                        row.role for row in voucher_rows if row.account.category == "expense"
                    }
                    result[3 if expense_roles & _SERVICE_COST_ROLES else 6] += -cash_delta
                elif event_type == "employee_reimbursement_payment":
                    self._allocate_settlement_cash(result, source_event_id, -cash_delta, missing)
                elif event_type == "unified_payout_run":
                    self._allocate_unified_payout_cash(
                        result, source_event_id, -cash_delta, missing
                    )
                elif event_type == "social_insurance_payment":
                    late_fee_fen = int(
                        source_event.facts.get("derived", {}).get(
                            "social_insurance_late_fee_fen", 0
                        )
                    )
                    signed_late_fee_fen = late_fee_fen if cash_delta < 0 else -late_fee_fen
                    result[4] += -cash_delta - signed_late_fee_fen
                    result[6] += signed_late_fee_fen
                elif event_type in {"salary_payment", "housing_fund_payment"}:
                    result[4] += -cash_delta
                elif event_type in {
                    "tax_payment",
                    "individual_income_tax_payment",
                    "labor_withholding_tax_payment",
                }:
                    result[5] += -cash_delta
                elif event_type in {"refundable_deposit_paid", "bank_fee"}:
                    result[6] += -cash_delta
                elif event_type == "fixed_asset_disposal":
                    result[10] += cash_delta
                elif event_type in {
                    "fixed_asset_acquisition",
                    "intangible_asset_acquisition",
                }:
                    result[12] += -cash_delta
                elif event_type in {"borrowing_drawdown", "owner_loan_received"}:
                    result[14] += cash_delta
                elif event_type == "owner_contribution_received":
                    result[15] += cash_delta
                elif event_type in {"borrowing_principal_repayment", "owner_repayment"}:
                    result[16] += -cash_delta
                elif event_type == "borrowing_interest_payment":
                    result[17] += -cash_delta
                else:
                    missing.append(
                        _requirement(
                            "FINANCIAL_STATEMENT_UNMAPPED_CASH_EVENT",
                            "存在未映射的现金收支事件。",
                            data={
                                "event_id": str(row0.event.id),
                                "event_type": event_type,
                                "cash_delta_fen": cash_delta,
                            },
                        )
                    )
            result[7] = result[1] + result[2] - result[3] - result[4] - result[5] - result[6]
            result[13] = result[8] + result[9] + result[10] - result[11] - result[12]
            result[19] = result[14] + result[15] - result[16] - result[17] - result[18]
            result[20] = result[7] + result[13] + result[19]
            result[21] = cash_balance(date.fromordinal(start.toordinal() - 1))
            result[22] = result[20] + result[21]
            return result

        current = values(quarter_start)
        ytd = values(year_start)
        return {
            line: {
                "name": CASH_FLOW_NAMES[line],
                "current_fen": current[line],
                "year_to_date_fen": ytd[line],
            }
            for line in CASH_FLOW_NAMES
        }

    def _allocate_settlement_cash(
        self,
        result: dict[int, int],
        payment_event_id: uuid.UUID,
        cash_outflow: int,
        missing: list[FinancialStatementInformationRequirement],
    ) -> None:
        settlements = list(
            self.session.scalars(
                select(Settlement).where(Settlement.payment_event_id == payment_event_id)
            )
        )
        allocation_sign = 1 if cash_outflow >= 0 else -1
        if sum(item.amount_fen for item in settlements) != abs(cash_outflow):
            missing.append(
                _requirement(
                    "FINANCIAL_STATEMENT_CASH_SETTLEMENT_MISMATCH",
                    "付款金额与原始事项分配不一致。",
                    data={"payment_event_id": str(payment_event_id)},
                )
            )
            return
        for settlement in settlements:
            source = self.session.scalar(
                select(BusinessEvent)
                .join(OpenItem, OpenItem.source_event_id == BusinessEvent.id)
                .where(OpenItem.id == settlement.open_item_id)
            )
            if source is None:
                missing.append(
                    _requirement(
                        "FINANCIAL_STATEMENT_CASH_SOURCE_MISSING",
                        "找不到付款对应的原始事项。",
                    )
                )
                continue
            roles = set(
                self.session.scalars(
                    select(Account.system_role)
                    .join(VoucherLine, VoucherLine.account_id == Account.id)
                    .join(Voucher, Voucher.id == VoucherLine.voucher_id)
                    .where(Voucher.event_id == source.id, Account.category == "expense")
                )
            )
            if roles & _SERVICE_COST_ROLES or source.event_type == "labor_remuneration_accrual":
                result[3] += settlement.amount_fen * allocation_sign
            elif source.event_type in {
                "fixed_asset_acquisition",
                "intangible_asset_acquisition",
            }:
                result[12] += settlement.amount_fen * allocation_sign
            else:
                result[6] += settlement.amount_fen * allocation_sign

    def _allocate_unified_payout_cash(
        self,
        result: dict[int, int],
        payment_event_id: uuid.UUID,
        cash_outflow: int,
        missing: list[FinancialStatementInformationRequirement],
    ) -> None:
        run = self.session.scalar(
            select(UnifiedPayoutRun).where(UnifiedPayoutRun.business_event_id == payment_event_id)
        )
        if run is None:
            missing.append(
                _requirement("FINANCIAL_STATEMENT_PAYOUT_SOURCE_MISSING", "找不到统一付款明细。")
            )
            return
        items = list(
            self.session.scalars(
                select(UnifiedPayoutRunItem).where(UnifiedPayoutRunItem.payout_run_id == run.id)
            )
        )
        allocation_sign = 1 if cash_outflow >= 0 else -1
        if sum(item.net_amount_fen for item in items) != abs(cash_outflow):
            missing.append(
                _requirement(
                    "FINANCIAL_STATEMENT_PAYOUT_AMOUNT_MISMATCH",
                    "统一付款明细与现金流出不一致。",
                    data={"payout_run_id": str(run.id)},
                )
            )
            return
        for item in items:
            result[4 if item.item_kind == "salary" else 3] += item.net_amount_fen * allocation_sign

    @staticmethod
    def _checks(
        balance: dict[int, dict[str, Any]],
        profit: dict[int, dict[str, Any]],
        cash: dict[int, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []
        for column in ("ending_fen", "beginning_fen"):
            checks.append(
                {
                    "code": f"FINANCIAL_STATEMENT_BALANCE_SHEET_{column.upper()}",
                    "passed": balance[30][column] == balance[53][column],
                    "left_fen": balance[30][column],
                    "right_fen": balance[53][column],
                }
            )
        for column in ("current_fen", "year_to_date_fen"):
            checks.extend(
                [
                    {
                        "code": f"FINANCIAL_STATEMENT_PROFIT_FORMULA_{column.upper()}",
                        "passed": profit[32][column] == profit[30][column] - profit[31][column],
                    },
                    {
                        "code": f"FINANCIAL_STATEMENT_CASH_FORMULA_{column.upper()}",
                        "passed": cash[20][column]
                        == cash[7][column] + cash[13][column] + cash[19][column],
                    },
                    {
                        "code": f"FINANCIAL_STATEMENT_CASH_ENDING_{column.upper()}",
                        "passed": cash[22][column] == balance[1]["ending_fen"],
                        "cash_ending_fen": cash[22][column],
                        "balance_sheet_cash_fen": balance[1]["ending_fen"],
                    },
                ]
            )
        return checks

    def _validate_evidence(self, org_id: uuid.UUID, evidence_ids: list[uuid.UUID]) -> str | None:
        if not evidence_ids:
            return "EVIDENCE_REQUIRED"
        found = set(
            self.session.scalars(
                select(Evidence.id).where(Evidence.org_id == org_id, Evidence.id.in_(evidence_ids))
            )
        )
        return None if found == set(evidence_ids) else "EVIDENCE_NOT_FOUND_OR_ORGANIZATION_MISMATCH"

    @staticmethod
    def _deduplicate_requirements(
        items: list[FinancialStatementInformationRequirement],
    ) -> list[FinancialStatementInformationRequirement]:
        result: list[FinancialStatementInformationRequirement] = []
        seen: set[str] = set()
        for item in items:
            key = _canonical(item.model_dump(mode="json"))
            if key not in seen:
                seen.add(key)
                result.append(item)
        return result

    @staticmethod
    def _statement_result(
        status: FinancialStatementResultStatus,
        *,
        calculation_hash: str | None = None,
        classification_id: uuid.UUID | None = None,
        enterprise_income_tax_confirmation_id: uuid.UUID | None = None,
        event_id: uuid.UUID | None = None,
        voucher_id: uuid.UUID | None = None,
        voucher_number: str | None = None,
        errors: list[str] | None = None,
        missing: list[FinancialStatementInformationRequirement] | None = None,
        trace: list[dict[str, Any]] | None = None,
        data: dict[str, Any] | None = None,
    ) -> FinancialStatementResult:
        return FinancialStatementResult(
            status=status,
            calculation_hash=calculation_hash,
            classification_id=classification_id,
            enterprise_income_tax_confirmation_id=enterprise_income_tax_confirmation_id,
            event_id=event_id,
            voucher_id=voucher_id,
            voucher_number=voucher_number,
            errors=errors or [],
            missing_information=missing or [],
            trace=trace or [],
            data=data or {},
        )


def _template_bytes() -> bytes:
    data = (
        resources.files("ai_accounting")
        .joinpath("templates/financial_reports")
        .joinpath(TEMPLATE_FILE_NAME)
        .read_bytes()
    )
    if hashlib.sha256(data).hexdigest().upper() != TEMPLATE_SHA256:
        raise ValueError("FINANCIAL_STATEMENT_TEMPLATE_VERSION_MISMATCH")
    return data


def _excel_serial(value: date) -> int:
    return value.toordinal() - date(1899, 12, 30).toordinal()


def _yuan_text(fen: int) -> str:
    return format((Decimal(fen) / Decimal(100)).quantize(Decimal("0.00")), "f")


def _cell_span(xml: str, reference: str) -> tuple[int, int, str, str]:
    start_match = re.search(
        rf'<c\b(?=[^>]*\br={quoteattr(reference)}(?:\s|/?>))[^>]*>',
        xml,
    )
    if start_match is None:
        raise ValueError(f"FINANCIAL_STATEMENT_TEMPLATE_CELL_MISSING:{reference}")
    start_tag = start_match.group(0)
    if start_tag.endswith("/>"):
        return start_match.start(), start_match.end(), start_tag, ""
    closing = xml.find("</c>", start_match.end())
    if closing < 0:
        raise ValueError(f"FINANCIAL_STATEMENT_TEMPLATE_CELL_INVALID:{reference}")
    return start_match.start(), closing + len("</c>"), start_tag, xml[start_match.end() : closing]


def _cell_start_tag(start_tag: str, *, cell_type: str | None) -> str:
    tag = re.sub(r"\s+t=(?:\"[^\"]*\"|'[^']*')", "", start_tag)
    tag = tag[:-2] if tag.endswith("/>") else tag[:-1]
    if cell_type is not None:
        tag += f" t={quoteattr(cell_type)}"
    return tag + ">"


def _formula_xml(body: str) -> str:
    match = re.search(r"<f\b[^>]*(?:/>|>.*?</f>)", body, flags=re.DOTALL)
    return match.group(0) if match is not None else ""


def _replace_cell(
    xml: str,
    reference: str,
    *,
    value_xml: str,
    cell_type: str | None,
) -> str:
    start, end, start_tag, body = _cell_span(xml, reference)
    replacement = (
        _cell_start_tag(start_tag, cell_type=cell_type)
        + _formula_xml(body)
        + value_xml
        + "</c>"
    )
    return xml[:start] + replacement + xml[end:]


def _set_numeric(xml: str, reference: str, value: str) -> str:
    return _replace_cell(
        xml,
        reference,
        value_xml=f"<v>{escape(value)}</v>",
        cell_type=None,
    )


def _set_text(xml: str, reference: str, value: str, *, formula_cache: bool = False) -> str:
    escaped = escape(value)
    if formula_cache:
        return _replace_cell(
            xml,
            reference,
            value_xml=f"<v>{escaped}</v>",
            cell_type="str",
        )
    return _replace_cell(
        xml,
        reference,
        value_xml=f"<is><t>{escaped}</t></is>",
        cell_type="inlineStr",
    )


def _enable_workbook_recalculation(xml: str) -> str:
    match = re.search(r"<calcPr\b[^>]*>", xml)
    if match is None:
        closing = xml.rfind("</workbook>")
        if closing < 0:
            raise ValueError("FINANCIAL_STATEMENT_TEMPLATE_WORKBOOK_INVALID")
        calc = '<calcPr calcMode="auto" fullCalcOnLoad="1" forceFullCalc="1"/>'
        return xml[:closing] + calc + xml[closing:]
    tag = match.group(0)
    self_closing = tag.endswith("/>")
    base = tag[:-2] if self_closing else tag[:-1]
    for name in ("calcMode", "fullCalcOnLoad", "forceFullCalc"):
        base = re.sub(rf"\s+{name}=(?:\"[^\"]*\"|'[^']*')", "", base)
    suffix = "/>" if self_closing else ">"
    updated = base + ' calcMode="auto" fullCalcOnLoad="1" forceFullCalc="1"' + suffix
    return xml[: match.start()] + updated + xml[match.end() :]


def render_quarterly_template(data: dict[str, Any]) -> bytes:
    template = _template_bytes()
    organization = data["organization"]
    period = data["period"]
    balance = data["statements"]["balance_sheet"]
    profit = data["statements"]["profit_statement"]
    cash = data["statements"]["cash_flow_statement"]
    start = date.fromisoformat(period["quarter_start"])
    end = date.fromisoformat(period["quarter_end"])
    replacements: dict[str, bytes] = {}

    with zipfile.ZipFile(io.BytesIO(template), "r") as source:
        sheets = [
            source.read(f"xl/worksheets/sheet{index}.xml").decode("utf-8")
            for index in (1, 2, 3)
        ]
        sheets[0] = _set_text(
            sheets[0], "D3", organization["taxpayer_identification_number"]
        )
        sheets[0] = _set_text(sheets[0], "H3", organization["name"])
        sheets[0] = _set_numeric(sheets[0], "D4", str(_excel_serial(start)))
        sheets[0] = _set_numeric(sheets[0], "H4", str(_excel_serial(end)))
        for index in (1, 2):
            sheets[index] = _set_text(
                sheets[index],
                "D3",
                organization["taxpayer_identification_number"],
                formula_cache=True,
            )
            sheets[index] = _set_text(
                sheets[index], "F3", organization["name"], formula_cache=True
            )
            sheets[index] = _set_numeric(
                sheets[index], "D4", str(_excel_serial(start))
            )
            sheets[index] = _set_numeric(
                sheets[index], "F4", str(_excel_serial(end))
            )

        balance_cells: dict[int, tuple[str, str]] = {}
        for line in range(1, 16):
            balance_cells[line] = (f"D{line + 6}", f"E{line + 6}")
        for line in range(16, 31):
            balance_cells[line] = (f"D{line + 7}", f"E{line + 7}")
        for line in range(31, 42):
            balance_cells[line] = (f"H{line - 24}", f"I{line - 24}")
        for line in range(42, 48):
            balance_cells[line] = (f"H{line - 23}", f"I{line - 23}")
        for line in range(48, 54):
            balance_cells[line] = (f"H{line - 16}", f"I{line - 16}")
        for line, (ending_cell, beginning_cell) in balance_cells.items():
            row = balance[str(line)]
            sheets[0] = _set_numeric(
                sheets[0], ending_cell, _yuan_text(int(row["ending_fen"]))
            )
            sheets[0] = _set_numeric(
                sheets[0], beginning_cell, _yuan_text(int(row["beginning_fen"]))
            )

        for line in range(1, 33):
            row = profit[str(line)]
            sheets[1] = _set_numeric(
                sheets[1], f"D{line + 5}", _yuan_text(int(row["current_fen"]))
            )
            sheets[1] = _set_numeric(
                sheets[1], f"E{line + 5}", _yuan_text(int(row["year_to_date_fen"]))
            )

        cash_rows = {
            **{line: line + 6 for line in range(1, 8)},
            **{line: line + 7 for line in range(8, 14)},
            **{line: line + 8 for line in range(14, 23)},
        }
        for line, sheet_row in cash_rows.items():
            row = cash[str(line)]
            sheets[2] = _set_numeric(
                sheets[2], f"D{sheet_row}", _yuan_text(int(row["current_fen"]))
            )
            sheets[2] = _set_numeric(
                sheets[2], f"E{sheet_row}", _yuan_text(int(row["year_to_date_fen"]))
            )
        for index, sheet in enumerate(sheets, start=1):
            replacements[f"xl/worksheets/sheet{index}.xml"] = sheet.encode("utf-8")
        replacements["xl/workbook.xml"] = _enable_workbook_recalculation(
            source.read("xl/workbook.xml").decode("utf-8")
        ).encode("utf-8")
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as target:
            for info in source.infolist():
                target.writestr(info, replacements.get(info.filename, source.read(info.filename)))
    return output.getvalue()
