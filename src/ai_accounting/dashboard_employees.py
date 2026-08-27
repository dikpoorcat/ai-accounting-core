from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy import Engine, and_, func, or_, select
from sqlalchemy.orm import Session, aliased

from .dashboard_common import (
    dashboard_session,
    list_dashboard_periods,
    period_view,
    resolve_dashboard_organization,
    resolve_dashboard_period,
)
from .models import (
    Account,
    AccountingPeriod,
    BusinessEvent,
    Employee,
    EmployeePayrollProfileVersion,
    LaborRemunerationBatch,
    LaborRemunerationEventLink,
    LaborRemunerationLine,
    Organization,
    PayrollBatch,
    PayrollLine,
    PayrollSalaryActualDeductionAllocation,
    UnifiedPayoutRun,
    UnifiedPayoutRunItem,
    Voucher,
    VoucherLine,
)

FINAL_VOUCHER_STATUSES = ("posted", "reversed")
PAYROLL_EXPENSE_ROLES = {
    "payroll_management_expense",
    "payroll_sales_expense",
    "payroll_service_cost",
}
EMPLOYEE_EXPENSE_AREAS = {
    "payroll_management_expense": "管理费用",
    "payroll_sales_expense": "销售费用",
    "payroll_service_cost": "主营业务成本",
}
LABOR_EXPENSE_ROLES = {
    "labor_management_expense",
    "labor_sales_expense",
    "labor_service_cost",
}


def load_employees_dashboard(
    engine: Engine,
    *,
    period_key: str | None = None,
    org_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Load the employee dashboard without changing accounting state."""

    with dashboard_session(engine) as session:
        organization = resolve_dashboard_organization(session, org_id)
        periods = list_dashboard_periods(session, org_id=organization.id)
        period = resolve_dashboard_period(periods, period_key)
        if period is None:
            return {"schema_version": 1, "selected_period": None, "data": None}
        return {
            "schema_version": 1,
            "selected_period": period_view(period),
            "data": build_employees_data(
                session, organization=organization, period=period
            ),
        }


def build_employees_data(
    session: Session,
    *,
    organization: Organization,
    period: AccountingPeriod,
) -> dict[str, Any]:
    """Build employee and workforce views for one accounting month."""

    employee_cost = _load_employee_compensation(
        session,
        org_id=organization.id,
        period=period,
    )
    personal_labor = _load_personal_labor_cost(
        session,
        org_id=organization.id,
        period=period,
    )
    workforce_cost = {
        "has_activity": employee_cost["has_activity"] or personal_labor["has_activity"],
        "total_fen": employee_cost["total_fen"] + personal_labor["total_fen"],
        "employee": employee_cost,
        "personal_labor": personal_labor,
    }
    return {
        "employees": _load_employees(
            session,
            org_id=organization.id,
            period=period,
            employee_cost=employee_cost,
        ),
        "workforce_cost": workforce_cost,
    }


def _load_employees(
    session: Session,
    *,
    org_id: uuid.UUID,
    period: AccountingPeriod,
    employee_cost: dict[str, Any],
) -> dict[str, Any]:
    """Build the owner-facing payroll roster without inferring labor relationships."""

    employee_records = list(
        session.scalars(
            select(Employee)
            .where(Employee.org_id == org_id)
            .order_by(Employee.employee_code, Employee.name)
        )
    )
    active_profiles = list(
        session.scalars(
            select(EmployeePayrollProfileVersion)
            .where(
                EmployeePayrollProfileVersion.org_id == org_id,
                EmployeePayrollProfileVersion.effective_from <= period.end_date,
                or_(
                    EmployeePayrollProfileVersion.effective_to.is_(None),
                    EmployeePayrollProfileVersion.effective_to >= period.end_date,
                ),
            )
            .order_by(
                EmployeePayrollProfileVersion.employee_id,
                EmployeePayrollProfileVersion.effective_from.desc(),
                EmployeePayrollProfileVersion.created_at.desc(),
            )
        )
    )
    profile_by_employee: dict[uuid.UUID, EmployeePayrollProfileVersion] = {}
    for profile in active_profiles:
        profile_by_employee.setdefault(profile.employee_id, profile)

    payroll_rows = session.execute(
        select(PayrollBatch, PayrollLine, EmployeePayrollProfileVersion)
        .join(
            PayrollLine,
            and_(
                PayrollLine.org_id == PayrollBatch.org_id,
                PayrollLine.payroll_batch_id == PayrollBatch.id,
            ),
        )
        .join(
            EmployeePayrollProfileVersion,
            and_(
                EmployeePayrollProfileVersion.org_id == PayrollLine.org_id,
                EmployeePayrollProfileVersion.employee_id == PayrollLine.employee_id,
                EmployeePayrollProfileVersion.id
                == PayrollLine.employee_payroll_profile_version_id,
            ),
        )
        .join(
            BusinessEvent,
            and_(
                BusinessEvent.org_id == PayrollBatch.org_id,
                BusinessEvent.id == PayrollBatch.business_event_id,
            ),
        )
        .where(
            PayrollBatch.org_id == org_id,
            PayrollBatch.status.in_(FINAL_VOUCHER_STATUSES),
            BusinessEvent.posting_date >= period.start_date,
            BusinessEvent.posting_date <= period.end_date,
            BusinessEvent.status.in_(FINAL_VOUCHER_STATUSES),
        )
        .order_by(PayrollBatch.posting_date, PayrollBatch.id, PayrollLine.id)
    ).all()

    amount_keys = (
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
    payroll_by_employee: dict[uuid.UUID, dict[str, Any]] = {}
    for batch, line, profile in payroll_rows:
        values = payroll_by_employee.setdefault(
            line.employee_id,
            _empty_employee_payroll(amount_keys),
        )
        sign = -1 if batch.reversal_of_batch_id is not None else 1
        for key in amount_keys:
            amount = getattr(line, key)
            if amount is not None:
                values[key] += sign * amount
        values["batch_ids"].add(batch.id)
        values["payroll_periods"].add(batch.payroll_period)
        values["batch_kinds"].add(batch.batch_kind)
        values["expense_roles"].add(profile.expense_role)
        values["declaration_states"].add(line.wage_tax_declaration_state)

    items: list[dict[str, Any]] = []
    for employee in employee_records:
        profile = profile_by_employee.get(employee.id)
        payroll = payroll_by_employee.get(employee.id) or _empty_employee_payroll(amount_keys)
        in_period = employee.employment_start_date <= period.end_date and (
            employee.employment_end_date is None
            or employee.employment_end_date >= period.start_date
        )
        if employee.employment_start_date > period.end_date:
            period_state = "not_started"
            period_state_label = "本月尚未开始工资核算"
        elif (
            employee.employment_end_date is not None
            and employee.employment_end_date < period.start_date
        ):
            period_state = "ended"
            period_state_label = "本月开始前已结束工资核算"
        else:
            period_state = "in_period"
            period_state_label = "本月在工资核算日期范围内"

        company_cost_fen = (
            payroll["gross_salary_fen"]
            + payroll["employer_social_insurance_fen"]
            + payroll["employer_housing_fund_fen"]
        )
        personal_deduction_fen = (
            payroll["employee_social_insurance_fen"]
            + payroll["employee_housing_fund_fen"]
            + payroll["individual_income_tax_fen"]
        )
        declaration_state, declaration_label = _declaration_view(
            payroll["declaration_states"]
        )
        expense_roles = payroll["expense_roles"]
        if not expense_roles and profile is not None:
            expense_roles = {profile.expense_role}
        expense_areas = sorted(
            {EMPLOYEE_EXPENSE_AREAS.get(role, "其他费用归属") for role in expense_roles}
        )
        items.append(
            {
                "code": employee.employee_code,
                "name": employee.name,
                "record_status": employee.status,
                "period_state": period_state,
                "period_state_label": period_state_label,
                "in_period": in_period,
                "employment_start_date": employee.employment_start_date.isoformat(),
                "employment_end_date": (
                    employee.employment_end_date.isoformat()
                    if employee.employment_end_date is not None
                    else None
                ),
                "tax_withholding_start_date": (
                    employee.tax_withholding_start_date.isoformat()
                    if employee.tax_withholding_start_date is not None
                    else None
                ),
                "profile_available": profile is not None,
                "expense_areas": expense_areas,
                "social_insurance_participating": (
                    profile.social_insurance_participating if profile is not None else None
                ),
                "housing_fund_participating": (
                    profile.housing_fund_participating if profile is not None else None
                ),
                "social_insurance_base_fen": (
                    profile.social_insurance_base_fen if profile is not None else None
                ),
                "housing_fund_base_fen": (
                    profile.housing_fund_base_fen if profile is not None else None
                ),
                "resident_employee": profile.resident_employee if profile is not None else None,
                "has_payroll_activity": bool(payroll["batch_ids"]),
                "batch_count": len(payroll["batch_ids"]),
                "payroll_periods": sorted(payroll["payroll_periods"]),
                "has_annual_bonus": "annual_bonus" in payroll["batch_kinds"],
                "gross_salary_fen": payroll["gross_salary_fen"],
                "annual_bonus_fen": payroll["annual_bonus_fen"],
                "employer_social_insurance_fen": payroll[
                    "employer_social_insurance_fen"
                ],
                "employer_housing_fund_fen": payroll["employer_housing_fund_fen"],
                "employee_social_insurance_fen": payroll[
                    "employee_social_insurance_fen"
                ],
                "employee_housing_fund_fen": payroll["employee_housing_fund_fen"],
                "individual_income_tax_fen": payroll["individual_income_tax_fen"],
                "personal_deduction_fen": personal_deduction_fen,
                "net_salary_fen": payroll["net_salary_fen"],
                "tax_reported_salary_fen": payroll["tax_reported_salary_fen"],
                "company_cost_fen": company_cost_fen,
                "declaration_state": declaration_state,
                "declaration_label": declaration_label,
            }
        )

    items.sort(key=lambda item: (not item["in_period"], item["code"], item["name"]))
    controlled_cost_fen = sum(item["company_cost_fen"] for item in items)
    settlement_adjustment_fen = employee_cost["settlement_adjustment_fen"]
    ledger_cost_fen = employee_cost["total_fen"]
    return {
        "registered_count": len(items),
        "in_period_count": sum(item["in_period"] for item in items),
        "payroll_count": sum(item["has_payroll_activity"] for item in items),
        "without_payroll_count": sum(
            item["in_period"] and not item["has_payroll_activity"] for item in items
        ),
        "profile_missing_count": sum(
            item["in_period"] and not item["profile_available"] for item in items
        ),
        "declaration_attention_count": sum(
            item["declaration_state"] == "not_declared" for item in items
        ),
        "gross_salary_fen": sum(item["gross_salary_fen"] for item in items),
        "annual_bonus_fen": sum(item["annual_bonus_fen"] for item in items),
        "employer_social_insurance_fen": sum(
            item["employer_social_insurance_fen"] for item in items
        ),
        "employer_housing_fund_fen": sum(
            item["employer_housing_fund_fen"] for item in items
        ),
        "personal_deduction_fen": sum(item["personal_deduction_fen"] for item in items),
        "individual_income_tax_fen": sum(
            item["individual_income_tax_fen"] for item in items
        ),
        "net_salary_fen": sum(item["net_salary_fen"] for item in items),
        "controlled_cost_fen": controlled_cost_fen,
        "settlement_adjustment_fen": settlement_adjustment_fen,
        "ledger_cost_fen": ledger_cost_fen,
        "detail_reconciled": controlled_cost_fen + settlement_adjustment_fen
        == ledger_cost_fen,
        "breakdown_available": employee_cost["breakdown_available"],
        "breakdown_reason": employee_cost["reason"],
        "items": items,
        "identity_note": "员工开始、结束日期仅用于工资核算身份，不判断或证明劳动关系。",
    }


def _empty_employee_payroll(amount_keys: tuple[str, ...]) -> dict[str, Any]:
    return {
        **{key: 0 for key in amount_keys},
        "batch_ids": set(),
        "payroll_periods": set(),
        "batch_kinds": set(),
        "expense_roles": set(),
        "declaration_states": set(),
    }


def _declaration_view(states: set[str]) -> tuple[str, str]:
    if not states:
        return "none", "本月无已过账工资申报状态"
    if "not_declared" in states:
        return "not_declared", "存在未申报工资个税的工资行"
    if states == {"not_applicable"}:
        return "not_applicable", "仅全年一次性奖金，不适用工资申报状态"
    if states == {"declared"}:
        return "declared", "工资个税申报状态已记录"
    return "mixed", "工资与奖金采用不同申报状态"


def _load_employee_compensation(
    session: Session,
    *,
    org_id: uuid.UUID,
    period: AccountingPeriod,
) -> dict[str, Any]:
    total_fen = _load_ledger_cost(
        session,
        org_id=org_id,
        period=period,
        expense_roles=PAYROLL_EXPENSE_ROLES,
    )
    rows = session.execute(
        select(PayrollBatch, PayrollLine)
        .join(
            PayrollLine,
            and_(
                PayrollLine.org_id == PayrollBatch.org_id,
                PayrollLine.payroll_batch_id == PayrollBatch.id,
            ),
        )
        .join(
            BusinessEvent,
            and_(
                BusinessEvent.org_id == PayrollBatch.org_id,
                BusinessEvent.id == PayrollBatch.business_event_id,
            ),
        )
        .where(
            PayrollBatch.org_id == org_id,
            PayrollBatch.status.in_(FINAL_VOUCHER_STATUSES),
            BusinessEvent.posting_date >= period.start_date,
            BusinessEvent.posting_date <= period.end_date,
            BusinessEvent.status.in_(FINAL_VOUCHER_STATUSES),
        )
        .order_by(PayrollBatch.posting_date, PayrollBatch.id, PayrollLine.id)
    ).all()
    totals = {
        "gross_salary_fen": 0,
        "employer_social_insurance_fen": 0,
        "employer_housing_fund_fen": 0,
        "employee_social_insurance_fen": 0,
        "employee_housing_fund_fen": 0,
    }
    by_payroll_period: dict[str, dict[str, Any]] = {}
    batch_ids: set[uuid.UUID] = set()
    for batch, line in rows:
        sign = -1 if batch.reversal_of_batch_id is not None else 1
        batch_ids.add(batch.id)
        period_totals = by_payroll_period.setdefault(
            batch.payroll_period,
            {
                "payroll_period": batch.payroll_period,
                **{key: 0 for key in totals},
                "total_fen": 0,
                "has_reversal": False,
            },
        )
        for key in totals:
            value = sign * getattr(line, key)
            totals[key] += value
            period_totals[key] += value
        period_totals["total_fen"] += (
            sign * line.gross_salary_fen
            + sign * line.employer_social_insurance_fen
            + sign * line.employer_housing_fund_fen
        )
        period_totals["has_reversal"] = (
            period_totals["has_reversal"] or batch.reversal_of_batch_id is not None
        )

    controlled_total_fen = (
        totals["gross_salary_fen"]
        + totals["employer_social_insurance_fen"]
        + totals["employer_housing_fund_fen"]
    )
    settlement_adjustments = _load_employee_settlement_adjustments(
        session,
        org_id=org_id,
        period=period,
    )
    settlement_adjustment_fen = settlement_adjustments["total_fen"]
    has_controlled_basis = bool(batch_ids)
    has_reconciliation_basis = has_controlled_basis or settlement_adjustments["has_basis"]
    breakdown_available = controlled_total_fen + settlement_adjustment_fen == total_fen and (
        has_reconciliation_basis or total_fen == 0
    )
    if breakdown_available:
        reason = None
    elif not has_controlled_basis:
        reason = "现有历史数据缺少受控工资批次关联，明细不可拆。"
    else:
        reason = "受控工资批次及工资结算调整与职工薪酬科目净额不一致，明细不可拆。"
    periods = sorted(by_payroll_period.values(), key=lambda item: item["payroll_period"])
    return {
        "has_activity": total_fen != 0 or has_reconciliation_basis,
        "breakdown_available": breakdown_available,
        "reason": reason,
        "total_fen": total_fen,
        "controlled_total_fen": controlled_total_fen,
        "settlement_adjustment_fen": settlement_adjustment_fen,
        "prior_period_settlement_adjustment_fen": settlement_adjustments[
            "prior_period_fen"
        ],
        "gross_salary_fen": totals["gross_salary_fen"] if breakdown_available else None,
        "employer_social_insurance_fen": (
            totals["employer_social_insurance_fen"] if breakdown_available else None
        ),
        "employer_housing_fund_fen": (
            totals["employer_housing_fund_fen"] if breakdown_available else None
        ),
        "employee_social_insurance_fen": (
            totals["employee_social_insurance_fen"] if breakdown_available else None
        ),
        "employee_housing_fund_fen": (
            totals["employee_housing_fund_fen"] if breakdown_available else None
        ),
        "personal_withholding_fen": (
            totals["employee_social_insurance_fen"]
            + totals["employee_housing_fund_fen"]
            if breakdown_available
            else None
        ),
        "batch_count": len(batch_ids),
        "periods": periods if breakdown_available else [],
    }


def _load_ledger_cost(
    session: Session,
    *,
    org_id: uuid.UUID,
    period: AccountingPeriod,
    expense_roles: set[str],
) -> int:
    debit = func.coalesce(func.sum(VoucherLine.debit_fen), 0)
    credit = func.coalesce(func.sum(VoucherLine.credit_fen), 0)
    value = session.execute(
        select((debit - credit).label("total_fen"))
        .select_from(Account)
        .join(VoucherLine, VoucherLine.account_id == Account.id)
        .join(Voucher, Voucher.id == VoucherLine.voucher_id)
        .where(
            Account.org_id == org_id,
            Account.system_role.in_(expense_roles),
            Voucher.org_id == org_id,
            Voucher.posting_date >= period.start_date,
            Voucher.posting_date <= period.end_date,
            Voucher.status.in_(FINAL_VOUCHER_STATUSES),
        )
    ).scalar_one()
    return int(value)


def _load_personal_labor_cost(
    session: Session,
    *,
    org_id: uuid.UUID,
    period: AccountingPeriod,
) -> dict[str, Any]:
    total_fen = _load_ledger_cost(
        session,
        org_id=org_id,
        period=period,
        expense_roles=LABOR_EXPENSE_ROLES,
    )
    rows = session.execute(
        select(
            LaborRemunerationEventLink,
            LaborRemunerationBatch,
            LaborRemunerationLine,
        )
        .join(
            BusinessEvent,
            and_(
                BusinessEvent.org_id == LaborRemunerationEventLink.org_id,
                BusinessEvent.id == LaborRemunerationEventLink.event_id,
            ),
        )
        .join(
            LaborRemunerationBatch,
            and_(
                LaborRemunerationBatch.org_id == LaborRemunerationEventLink.org_id,
                LaborRemunerationBatch.id == LaborRemunerationEventLink.batch_id,
            ),
        )
        .join(
            LaborRemunerationLine,
            and_(
                LaborRemunerationLine.org_id == LaborRemunerationBatch.org_id,
                LaborRemunerationLine.batch_id == LaborRemunerationBatch.id,
                or_(
                    LaborRemunerationEventLink.labor_line_id.is_(None),
                    LaborRemunerationEventLink.labor_line_id == LaborRemunerationLine.id,
                ),
            ),
        )
        .where(
            LaborRemunerationEventLink.org_id == org_id,
            LaborRemunerationEventLink.link_kind.in_(("accrual", "reversal")),
            LaborRemunerationBatch.status.in_(FINAL_VOUCHER_STATUSES),
            BusinessEvent.posting_date >= period.start_date,
            BusinessEvent.posting_date <= period.end_date,
            BusinessEvent.status.in_(FINAL_VOUCHER_STATUSES),
        )
        .order_by(
            BusinessEvent.posting_date,
            LaborRemunerationEventLink.id,
            LaborRemunerationLine.id,
        )
    ).all()
    gross_remuneration_fen = 0
    theoretical_withholding_tax_fen = 0
    by_remuneration_period: dict[str, dict[str, Any]] = {}
    batch_ids: set[uuid.UUID] = set()
    event_link_ids: set[uuid.UUID] = set()
    line_weights: dict[uuid.UUID, int] = defaultdict(int)
    for link, batch, line in rows:
        sign = -1 if link.link_kind == "reversal" else 1
        batch_ids.add(batch.id)
        event_link_ids.add(link.id)
        line_weights[line.id] += sign
        period_totals = by_remuneration_period.setdefault(
            batch.remuneration_period,
            {
                "remuneration_period": batch.remuneration_period,
                "gross_remuneration_fen": 0,
                "theoretical_withholding_tax_fen": 0,
                "total_fen": 0,
                "has_reversal": False,
            },
        )
        gross = sign * line.gross_remuneration_fen
        theoretical_withholding = sign * line.withholding_tax_fen
        gross_remuneration_fen += gross
        theoretical_withholding_tax_fen += theoretical_withholding
        period_totals["gross_remuneration_fen"] += gross
        period_totals["theoretical_withholding_tax_fen"] += theoretical_withholding
        period_totals["total_fen"] += gross
        period_totals["has_reversal"] = (
            period_totals["has_reversal"] or link.link_kind == "reversal"
        )

    has_controlled_basis = bool(event_link_ids)
    breakdown_available = gross_remuneration_fen == total_fen and (
        has_controlled_basis or total_fen == 0
    )
    if breakdown_available:
        reason = None
    elif not has_controlled_basis:
        reason = "现有历史数据缺少受控个人劳务批次关联，明细不可拆。"
    else:
        reason = "受控个人劳务批次与个人劳务费用科目净额不一致，明细不可拆。"
    periods = sorted(
        by_remuneration_period.values(), key=lambda item: item["remuneration_period"]
    )
    effective_line_weights = {
        line_id: weight for line_id, weight in line_weights.items() if weight != 0
    }
    payout_rows = (
        session.execute(
            select(UnifiedPayoutRunItem, UnifiedPayoutRun)
            .join(
                UnifiedPayoutRun,
                and_(
                    UnifiedPayoutRun.org_id == UnifiedPayoutRunItem.org_id,
                    UnifiedPayoutRun.id == UnifiedPayoutRunItem.payout_run_id,
                ),
            )
            .where(
                UnifiedPayoutRunItem.org_id == org_id,
                UnifiedPayoutRunItem.item_kind == "labor",
                UnifiedPayoutRunItem.labor_line_id.in_(tuple(effective_line_weights)),
                UnifiedPayoutRun.status == "posted",
            )
            .order_by(UnifiedPayoutRun.posting_date, UnifiedPayoutRunItem.id)
        ).all()
        if effective_line_weights
        else []
    )
    payouts_by_line: dict[uuid.UUID, list[UnifiedPayoutRunItem]] = defaultdict(list)
    for item, _run in payout_rows:
        if item.labor_line_id is not None:
            payouts_by_line[item.labor_line_id].append(item)

    actual_withholding_tax_fen = 0
    unwithheld_tax_fen = 0
    settled_gross_fen = 0
    settlement_modes: set[str] = set()
    for line_id, weight in effective_line_weights.items():
        for item in payouts_by_line.get(line_id, []):
            actual_withholding_tax_fen += weight * item.individual_income_tax_fen
            unwithheld_tax_fen += weight * item.unwithheld_individual_income_tax_fen
            settled_gross_fen += weight * item.gross_amount_fen
            settlement_modes.add(item.settlement_mode)
    unsettled_gross_fen = gross_remuneration_fen - settled_gross_fen
    pending_theoretical_tax_fen = (
        theoretical_withholding_tax_fen
        - actual_withholding_tax_fen
        - unwithheld_tax_fen
    )
    has_cost_correction = any(weight < 0 for weight in effective_line_weights.values())
    if has_cost_correction:
        withholding_status = "correction"
    elif theoretical_withholding_tax_fen == 0:
        withholding_status = "none"
    elif unsettled_gross_fen != 0 and settled_gross_fen != 0:
        withholding_status = "partially_settled"
    elif unsettled_gross_fen != 0:
        withholding_status = "pending_payment"
    elif actual_withholding_tax_fen == 0 and unwithheld_tax_fen != 0:
        withholding_status = "not_withheld"
    elif actual_withholding_tax_fen != 0 and unwithheld_tax_fen == 0:
        withholding_status = "withheld"
    else:
        withholding_status = "mixed"
    return {
        "has_activity": total_fen != 0 or has_controlled_basis,
        "breakdown_available": breakdown_available,
        "reason": reason,
        "total_fen": total_fen,
        "gross_remuneration_fen": gross_remuneration_fen if breakdown_available else None,
        "theoretical_withholding_tax_fen": (
            theoretical_withholding_tax_fen if breakdown_available else None
        ),
        "actual_withholding_tax_fen": (
            actual_withholding_tax_fen if breakdown_available else None
        ),
        "unwithheld_tax_fen": unwithheld_tax_fen if breakdown_available else None,
        "pending_theoretical_tax_fen": (
            pending_theoretical_tax_fen if breakdown_available else None
        ),
        "settled_gross_fen": settled_gross_fen if breakdown_available else None,
        "unsettled_gross_fen": unsettled_gross_fen if breakdown_available else None,
        "withholding_status": withholding_status if breakdown_available else "unavailable",
        "settlement_modes": sorted(settlement_modes) if breakdown_available else [],
        "batch_count": len(batch_ids),
        "periods": periods if breakdown_available else [],
    }


def _load_employee_settlement_adjustments(
    session: Session,
    *,
    org_id: uuid.UUID,
    period: AccountingPeriod,
) -> dict[str, int | bool]:
    payment_event = aliased(BusinessEvent, name="salary_adjustment_payment_event")
    reversal_event = aliased(BusinessEvent, name="salary_adjustment_reversal_event")
    rows = session.execute(
        select(
            PayrollSalaryActualDeductionAllocation.amount_fen,
            PayrollBatch.payroll_period,
            payment_event.posting_date.label("payment_posting_date"),
            payment_event.status.label("payment_status"),
            reversal_event.posting_date.label("reversal_posting_date"),
            reversal_event.status.label("reversal_status"),
        )
        .join(
            PayrollLine,
            and_(
                PayrollLine.org_id == PayrollSalaryActualDeductionAllocation.org_id,
                PayrollLine.id == PayrollSalaryActualDeductionAllocation.payroll_line_id,
            ),
        )
        .join(
            PayrollBatch,
            and_(
                PayrollBatch.org_id == PayrollLine.org_id,
                PayrollBatch.id == PayrollLine.payroll_batch_id,
            ),
        )
        .join(
            payment_event,
            and_(
                payment_event.org_id == PayrollSalaryActualDeductionAllocation.org_id,
                payment_event.id == PayrollSalaryActualDeductionAllocation.payment_event_id,
            ),
        )
        .outerjoin(
            reversal_event,
            and_(
                reversal_event.org_id == PayrollSalaryActualDeductionAllocation.org_id,
                reversal_event.id
                == PayrollSalaryActualDeductionAllocation.reversed_by_event_id,
            ),
        )
        .where(
            PayrollSalaryActualDeductionAllocation.org_id == org_id,
            PayrollSalaryActualDeductionAllocation.expense_role.in_(PAYROLL_EXPENSE_ROLES),
            or_(
                and_(
                    payment_event.posting_date >= period.start_date,
                    payment_event.posting_date <= period.end_date,
                ),
                and_(
                    reversal_event.posting_date >= period.start_date,
                    reversal_event.posting_date <= period.end_date,
                ),
            ),
        )
        .order_by(
            payment_event.posting_date,
            PayrollSalaryActualDeductionAllocation.id,
        )
    ).all()

    total_fen = 0
    prior_period_fen = 0
    effect_count = 0
    current_period_key = f"{period.calendar_year:04d}-{period.calendar_month:02d}"
    for row in rows:
        effects: list[int] = []
        if (
            period.start_date <= row.payment_posting_date <= period.end_date
            and row.payment_status in FINAL_VOUCHER_STATUSES
        ):
            effects.append(-row.amount_fen)
        if (
            row.reversal_posting_date is not None
            and period.start_date <= row.reversal_posting_date <= period.end_date
            and row.reversal_status in FINAL_VOUCHER_STATUSES
        ):
            effects.append(row.amount_fen)
        for effect in effects:
            total_fen += effect
            if row.payroll_period < current_period_key:
                prior_period_fen += effect
            effect_count += 1
    return {
        "total_fen": total_fen,
        "prior_period_fen": prior_period_fen,
        "has_basis": effect_count > 0,
    }
