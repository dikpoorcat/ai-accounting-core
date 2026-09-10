"""Derive bank payment exports from posted, company-scoped kernel facts."""

from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from .business_metadata import metadata_projection
from .models import (
    AccountingPeriod,
    BusinessEvent,
    BusinessEventComponent,
    Employee,
    OpenItem,
    Organization,
    PayrollBatch,
    PayrollLine,
)
from .mybank_export import (
    canonical_hash,
    publish_export,
    read_recipients,
    read_workbook_file,
    render_import,
    validate_template,
)
from .mybank_profiles import company_export_profile
from .mybank_schemas import GenerateMybankExportRequest, PreviewMybankExportRequest

REIMBURSEMENT_FIELDS = {
    "回扣报销1": "rebate_1_fen",
    "回扣报销2": "rebate_2_fen",
    "发票报销": "invoice_fen",
    "内核合并报销": "combined_reimbursement_fen",
}


class MybankExportService:
    def __init__(self, session: Session):
        self.session = session

    def _prepare(self, request: PreviewMybankExportRequest) -> tuple[dict, bytes, dict]:
        org_id = request.org_id
        organization = self.session.get(Organization, org_id)
        if organization is None:
            raise ValueError("ORGANIZATION_NOT_FOUND")
        profile = company_export_profile(organization)
        template = read_workbook_file(request.template_path)
        validate_template(template)
        recipients = read_recipients(read_workbook_file(request.recipients_path))
        employees = self.session.scalars(select(Employee).where(Employee.org_id == org_id)).all()
        by_id = {e.id: e for e in employees}
        by_name: dict[str, list[Employee]] = {}
        by_party = {e.counterparty_id: e for e in employees}
        for employee in employees:
            by_name.setdefault(employee.name, []).append(employee)
        selected = set(request.employee_ids) if request.employee_ids else set(by_id)
        issues: list[dict] = []
        material_snapshot_hash = None
        material_satisfied = False
        material_expected_sources = set()
        material_labor_sources = set()
        material_salary_people = set()
        from .material_service import MaterialService

        material_service = MaterialService(self.session)
        period = self.session.scalar(
            select(AccountingPeriod).where(
                AccountingPeriod.org_id == org_id,
                AccountingPeriod.calendar_year == int(request.payroll_period[:4]),
                AccountingPeriod.calendar_month == int(request.payroll_period[5:7]),
            )
        )
        if period and request.scope == "complete":
            completeness = material_service.check(org_id, period.id)
            material_snapshot_hash = completeness["snapshot_hash"]
            material_satisfied = completeness["satisfied"]
            from .material_schemas import MaterialComponentLink

            for material in completeness["items"]:
                resolution = material["resolution"]
                category = resolution.get("export_category")
                if category not in {"reimbursement", "salary", "labor"}:
                    continue
                for raw in resolution.get("links", []):
                    link = MaterialComponentLink.model_validate(raw)
                    component, error = material_service._link(org_id, link)
                    if error:
                        continue
                    query = select(OpenItem).where(
                        OpenItem.org_id == org_id,
                        OpenItem.source_component_id == component.id,
                        OpenItem.item_type == "payable",
                        OpenItem.status.in_(("open", "partial")),
                        OpenItem.original_amount_fen > OpenItem.settled_amount_fen,
                    )
                    if link.open_item_key:
                        query = query.where(OpenItem.component_key == link.open_item_key)
                    for item in self.session.scalars(query):
                        if category == "reimbursement":
                            material_expected_sources.add(item.id)
                        elif category == "labor":
                            material_labor_sources.add(item.id)
                        elif item.payable_category == "salary" and item.counterparty_id in by_party:
                            material_salary_people.add(by_party[item.counterparty_id].id)
            if request.include_salary and material_salary_people - selected:
                issues.append(
                    {
                        "code": "MYBANK_MATERIAL_SALARY_EMPLOYEES_MISSING",
                        "message": "完整代发漏掉清单已确认且尚未结清工资的人员。",
                        "employee_ids": sorted(map(str, material_salary_people - selected)),
                    }
                )
            if not completeness["satisfied"]:
                issues.append(
                    {
                        "code": "MYBANK_MATERIAL_INCOMPLETE",
                        "message": "月度资料尚有漏项或未完成核对，不能生成完整代发文件。",
                        "material_issues": completeness["issues"],
                    }
                )
            expected_people = {
                item["resolution"]["employee_id"]
                for item in completeness["items"]
                if item["resolution"].get("export_category") == "reimbursement"
                and item["resolution"].get("employee_id")
            }
            if expected_people:
                requested_people = {str(e) for e in request.reimbursement_employee_ids or []}
                if requested_people and not expected_people <= requested_people:
                    issues.append(
                        {
                            "code": "MYBANK_MATERIAL_EMPLOYEES_MISSING",
                            "message": "代发范围漏掉资料清单中已知的报销人员。",
                            "employee_ids": sorted(expected_people - requested_people),
                        }
                    )
        if request.scope == "complete":
            if period is None:
                issues.append(
                    {"code": "MYBANK_PERIOD_REQUIRED", "message": "内核没有本月会计期间。"}
                )
            if (
                not request.include_salary
                or request.employee_ids
                or request.reimbursement_open_item_ids
            ):
                issues.append(
                    {
                        "code": "MYBANK_COMPLETE_SCOPE_REQUIRED",
                        "message": "完整代发只选择公司和月份，不接受人工筛选员工或应付款。",
                    }
                )
        from .mybank_sources import MybankPaymentSourceService

        source_service = MybankPaymentSourceService(self.session)
        tax_version = source_service.latest(org_id, request.payroll_period, "actual_tax")
        register_version = source_service.latest(org_id, request.payroll_period, "payment_register")
        actual_tax = (
            {row["name"]: row for row in tax_version.content["rows"]} if tax_version else {}
        )
        register = (
            {row["name"]: row for row in register_version.content["rows"]}
            if register_version
            else {}
        )
        uses_actual_tax = profile["salary"]["source"] == (
            "reported_salary_minus_actual_tax_and_employee_contributions"
        )
        if request.scope == "complete" and register_version is None and not material_satisfied:
            issues.append(
                {
                    "code": "MYBANK_PAYMENT_REGISTER_MISSING",
                    "message": "本月清单尚未核对通过，也没有代发原表可辅助核对人员金额。",
                }
            )
        skipped: list[dict] = []
        payments: dict[uuid.UUID, dict] = {}
        sources: list[dict] = [
            source_service.describe(version)
            for version in (tax_version, register_version)
            if version
        ]

        def issue(code: str, message: str, **context) -> None:
            issues.append({"code": code, "message": message, **context})

        def payment(employee: Employee) -> dict:
            return payments.setdefault(
                employee.id,
                {
                    "employee_id": str(employee.id),
                    "name": employee.name,
                    "salary_fen": 0,
                    "labor_fen": 0,
                    "rebate_1_fen": 0,
                    "rebate_2_fen": 0,
                    "invoice_fen": 0,
                    "combined_reimbursement_fen": 0,
                },
            )

        for employee_id in sorted(selected - set(by_id), key=str):
            issue("EMPLOYEE_NOT_IN_COMPANY", "所选员工不属于当前公司", employee_id=str(employee_id))
        seen: set[uuid.UUID] = set()
        if request.include_salary:
            rows = self.session.execute(
                select(PayrollBatch, PayrollLine, Employee, BusinessEvent)
                .join(
                    PayrollLine,
                    (PayrollLine.payroll_batch_id == PayrollBatch.id)
                    & (PayrollLine.org_id == org_id),
                )
                .join(
                    Employee, (Employee.id == PayrollLine.employee_id) & (Employee.org_id == org_id)
                )
                .join(
                    BusinessEvent,
                    (BusinessEvent.id == PayrollBatch.business_event_id)
                    & (BusinessEvent.org_id == org_id),
                )
                .where(
                    PayrollBatch.org_id == org_id,
                    PayrollBatch.payroll_period == request.payroll_period,
                    PayrollBatch.batch_kind == "regular",
                    PayrollBatch.status == "posted",
                    PayrollBatch.reversal_of_batch_id.is_(None),
                    BusinessEvent.status == "posted",
                    BusinessEvent.reversed_by_event_id.is_(None),
                    Employee.id.in_(selected),
                )
                .order_by(Employee.employee_code, PayrollLine.id)
            ).all()
            seen: set[uuid.UUID] = set()
            no_wages_confirmed = (
                request.scope == "complete"
                and material_satisfied
                and not any(row["tax_reported_salary_fen"] for row in register.values())
            )
            if not rows and not no_wages_confirmed:
                issue("POSTED_PAYROLL_NOT_FOUND", "所选范围没有本月已过账的有效工资批次")
            for batch, line, employee, event in rows:
                if employee.id in seen:
                    issue(
                        "DUPLICATE_PAYROLL",
                        "同一员工本月存在多条有效工资，请先核对内核批次",
                        employee_id=str(employee.id),
                        name=employee.name,
                    )
                    continue
                seen.add(employee.id)
                source = {
                    "kind": "salary",
                    "batch_id": str(batch.id),
                    "line_id": str(line.id),
                    "event_id": str(event.id),
                    "employee_id": str(employee.id),
                    "calculation_hash": batch.calculation_hash,
                    "net_salary_fen": line.net_salary_fen,
                }
                sources.append(source)
                if line.wage_tax_scope == "contributions_only":
                    skipped.append({"name": employee.name, "reason": "仅缴社保，无工资"})
                    continue
                salary_fen = line.net_salary_fen
                if uses_actual_tax:
                    tax_row = actual_tax.get(employee.name)
                    if tax_row is None:
                        issue(
                            "ACTUAL_INCOME_TAX_MISSING",
                            "缺少该员工本月税务局实际个税，不能用计算税额或零替代",
                            name=employee.name,
                        )
                        continue
                    if tax_row["tax_reported_salary_fen"] != line.tax_reported_salary_fen:
                        issue(
                            "ACTUAL_TAX_INCOME_CONFLICT",
                            "税局本期收入与内核报税工资不一致",
                            name=employee.name,
                        )
                        continue
                    salary_fen = (
                        line.tax_reported_salary_fen
                        - tax_row["actual_individual_income_tax_fen"]
                        - line.employee_social_insurance_fen
                        - line.employee_housing_fund_fen
                    )
                    source.update(
                        {
                            "tax_reported_salary_fen": line.tax_reported_salary_fen,
                            "actual_individual_income_tax_fen": tax_row[
                                "actual_individual_income_tax_fen"
                            ],
                            "calculated_individual_income_tax_fen": line.individual_income_tax_fen,
                            "employee_social_insurance_fen": line.employee_social_insurance_fen,
                            "employee_housing_fund_fen": line.employee_housing_fund_fen,
                            "salary_fen": salary_fen,
                        }
                    )
                if salary_fen == 0:
                    skipped.append({"name": employee.name, "reason": "实发工资为零"})
                    continue
                if salary_fen < 0:
                    issue("NEGATIVE_NET_SALARY", "内核实发工资为负数，无法代发", name=employee.name)
                    continue
                items = self.session.scalars(
                    select(OpenItem).where(
                        OpenItem.org_id == org_id,
                        OpenItem.source_event_id == event.id,
                        OpenItem.component_key == f"salary:{line.id}",
                        OpenItem.payable_category == "salary",
                        OpenItem.item_type == "payable",
                    )
                ).all()
                if len(items) != 1:
                    issue(
                        "SALARY_PAYABLE_NOT_UNIQUE",
                        "工资应付款来源不唯一或缺失",
                        name=employee.name,
                    )
                    continue
                item = items[0]
                source.update(self._balance_source(item))
                if item.status == "settled" and item.settled_amount_fen == item.original_amount_fen:
                    skipped.append(
                        {"name": employee.name, "reason": "工资已结清", "source": source}
                    )
                    continue
                if item.status != "open" or item.settled_amount_fen != 0:
                    issue(
                        "PARTIALLY_SETTLED_SALARY",
                        "工资已有核销或状态变更，须核对剩余实发金额",
                        name=employee.name,
                        open_item_id=str(item.id),
                    )
                    continue
                if item.original_amount_fen != line.gross_salary_fen:
                    issue(
                        "SALARY_BALANCE_CONFLICT",
                        "工资应付款与工资批次应发金额不一致",
                        name=employee.name,
                    )
                    continue
                payment(employee)["salary_fen"] = salary_fen
            if request.employee_ids:
                for employee_id in sorted(selected - seen - (selected - set(by_id)), key=str):
                    issue(
                        "EMPLOYEE_PAYROLL_NOT_FOUND",
                        "所选员工没有本月有效工资",
                        name=by_id[employee_id].name,
                    )

        counts = dict.fromkeys(REIMBURSEMENT_FIELDS, 0)
        reimbursement_ids = set(request.reimbursement_open_item_ids)
        labor_items = {}
        if request.scope == "complete":
            reimbursement_ids = set(material_expected_sources)
            for item, component in self.session.execute(
                select(OpenItem, BusinessEventComponent)
                .join(
                    BusinessEventComponent,
                    (OpenItem.source_component_id == BusinessEventComponent.id)
                    & (BusinessEventComponent.org_id == org_id),
                )
                .where(OpenItem.org_id == org_id, OpenItem.item_type == "payable")
            ):
                metadata = metadata_projection(
                    self.session, org_id, component.event_id, component.key
                )["metadata"]
                payment_period = metadata.get(
                    "payment_period"
                ) or material_service._component_period(component)
                if (
                    payment_period != request.payroll_period
                    and item.id not in material_labor_sources
                ):
                    continue
                if metadata.get("payment_category") == "labor" or item.id in material_labor_sources:
                    labor_items[item.id] = (item, component, metadata)
                elif metadata.get("purpose") in REIMBURSEMENT_FIELDS or (
                    component.kind == "expense"
                    and component.facts.get("payment_basis") == "person_advance"
                ):
                    reimbursement_ids.add(item.id)
        if reimbursement_ids & labor_items.keys():
            issue(
                "MYBANK_SOURCE_CATEGORY_CONFLICT",
                "同一应付款同时被归为劳务和报销，请核对具体来源及拆分金额。",
                open_item_ids=sorted(map(str, reimbursement_ids & labor_items.keys())),
            )
        for item_id in sorted(reimbursement_ids, key=str):
            item = self.session.scalar(
                select(OpenItem).where(OpenItem.org_id == org_id, OpenItem.id == item_id)
            )
            if item is None:
                issue(
                    "REIMBURSEMENT_SOURCE_NOT_FOUND",
                    "所选报销应付款不在当前公司",
                    open_item_id=str(item_id),
                )
                continue
            event = self.session.scalar(
                select(BusinessEvent).where(
                    BusinessEvent.org_id == org_id, BusinessEvent.id == item.source_event_id
                )
            )
            component = self.session.scalar(
                select(BusinessEventComponent).where(
                    BusinessEventComponent.org_id == org_id,
                    BusinessEventComponent.id == item.source_component_id,
                    BusinessEventComponent.event_id == item.source_event_id,
                )
            )
            if (
                event is None
                or component is None
                or event.status != "posted"
                or event.reversed_by_event_id is not None
                or item.status == "reversed"
                or item.item_type != "payable"
            ):
                issue(
                    "INACTIVE_REIMBURSEMENT_SOURCE",
                    "报销来源不是有效的已过账应付款",
                    open_item_id=str(item_id),
                )
                continue
            metadata = metadata_projection(self.session, org_id, event.id, component.key)
            purpose = metadata["metadata"].get("purpose")
            if purpose not in REIMBURSEMENT_FIELDS:
                purpose = "内核合并报销"
            configured_categories = set(profile["reimbursement"]["categories"])
            if (purpose != "内核合并报销" and purpose not in configured_categories) or (
                purpose == "内核合并报销"
                and configured_categories != {"回扣报销1", "回扣报销2", "发票报销"}
            ):
                issue(
                    "REIMBURSEMENT_OUTSIDE_PROFILE",
                    "报销来源不符合公司默认类别或缺少合并拆分",
                    open_item_id=str(item_id),
                )
                continue
            valid_rebate = (
                component.kind == "pass_through" and item.payable_category == "pass_through"
            )
            valid_invoice = (
                component.kind == "expense"
                and component.facts.get("payment_basis") == "person_advance"
            )
            if not (valid_rebate or valid_invoice):
                issue(
                    "REIMBURSEMENT_CLASSIFICATION_MISSING",
                    "所选来源须为内核代收代付或个人垫付费用应付款",
                    open_item_id=str(item_id),
                )
                continue
            source = {
                "kind": purpose,
                "event_id": str(event.id),
                "component_id": str(component.id),
                "component_key": component.key,
                "posting_date": str(event.posting_date),
                "metadata_version": metadata["version"],
                "metadata": metadata["metadata"],
                **self._balance_source(item),
            }
            party_id_for_name = (
                item.pass_through_beneficiary_id if valid_rebate else item.counterparty_id
            )
            reference_for_name = metadata["metadata"].get("beneficiary") or {}
            known_employee = by_party.get(party_id_for_name)
            if known_employee is None:
                known_matches = by_name.get(reference_for_name.get("name"), [])
                known_employee = known_matches[0] if len(known_matches) == 1 else None
            if known_employee:
                source["employee_id"] = str(known_employee.id)
            sources.append(source)
            balance = item.original_amount_fen - item.settled_amount_fen
            if balance == 0 and item.status == "settled":
                skipped.append({"reason": f"{purpose}已结清", "source": source})
                continue
            if balance <= 0 or item.status not in {"open", "partial"}:
                issue(
                    "REIMBURSEMENT_BALANCE_CONFLICT",
                    "报销应付款状态与余额冲突",
                    open_item_id=str(item_id),
                )
                continue
            party_id = item.pass_through_beneficiary_id if valid_rebate else item.counterparty_id
            employee = by_party.get(party_id)
            reference = metadata["metadata"].get("beneficiary") or {}
            if employee is None and valid_rebate:
                if reference.get("id"):
                    employee = by_party.get(uuid.UUID(reference["id"]))
                elif reference.get("kind") == "employee":
                    matches = by_name.get(reference.get("name"), [])
                    employee = matches[0] if len(matches) == 1 else None
            if employee is None or employee.id not in selected:
                issue(
                    "REIMBURSEMENT_BENEFICIARY_MISSING",
                    "内核报销收款员工不明确、重名或不在所选范围",
                    open_item_id=str(item_id),
                )
                continue
            if party_id and reference.get("id") and str(party_id) != str(reference["id"]):
                issue(
                    "BENEFICIARY_CONFLICT",
                    "核算受益人与管理资料受益人冲突",
                    open_item_id=str(item_id),
                )
                continue
            if party_id and reference.get("name") and employee.name != reference["name"]:
                issue(
                    "BENEFICIARY_CONFLICT",
                    "核算受益人与管理资料姓名冲突",
                    open_item_id=str(item_id),
                )
                continue
            source["employee_id"] = str(employee.id)
            payment(employee)[REIMBURSEMENT_FIELDS[purpose]] += balance
            counts[purpose] += 1
        if request.scope == "complete":
            from .models import Counterparty

            for name, raw in register.items():
                matches = by_name.get(name, [])
                if raw["tax_reported_salary_fen"] > 0 and (
                    len(matches) != 1 or matches[0].id not in seen
                ):
                    issue(
                        "REGISTER_SALARY_SOURCE_MISSING", "原表报税工资缺少内核工资来源", name=name
                    )
            labor_by_name = {}
            for item, component, metadata in labor_items.values():
                party_id = item.pass_through_beneficiary_id or item.counterparty_id
                party = self.session.get(Counterparty, party_id) if party_id else None
                reference = metadata.get("beneficiary") or metadata.get("counterparty") or {}
                beneficiary = (
                    party.name if party and party.org_id == org_id else reference.get("name")
                )
                if not beneficiary:
                    issue(
                        "LABOR_BENEFICIARY_MISSING",
                        "已知劳务应付款尚未核定收款人",
                        open_item_id=str(item.id),
                    )
                    continue
                labor_by_name.setdefault(beneficiary, []).append((item, component))
            names = set(labor_by_name) | {
                name for name, raw in register.items() if raw["untaxed_labor_fen"] > 0
            }
            for name in sorted(names):
                candidates = labor_by_name.get(name, [])
                original = sum(item.original_amount_fen for item, _ in candidates)
                expected = register[name]["untaxed_labor_fen"] if name in register else original
                row = {
                    "employee_id": None,
                    "name": name,
                    "salary_fen": 0,
                    "labor_fen": expected,
                    **{field: 0 for field in REIMBURSEMENT_FIELDS.values()},
                }
                payments[f"labor:{name}"] = row
                if not candidates:
                    issue(
                        "UNTAXED_LABOR_PAYABLE_MISSING",
                        "未报税劳务原表已有金额，但缺少本月内核应付款",
                        name=name,
                        amount_fen=expected,
                    )
                    continue
                if original != expected:
                    issue(
                        "UNTAXED_LABOR_AMOUNT_CONFLICT",
                        "未报税劳务原始金额与内核应付款不一致",
                        name=name,
                    )
                balance = 0
                for item, component in candidates:
                    event = self.session.get(BusinessEvent, item.source_event_id)
                    if (
                        event is None
                        or event.status != "posted"
                        or event.reversed_by_event_id
                        or item.status not in {"open", "partial", "settled"}
                    ):
                        issue("UNTAXED_LABOR_SOURCE_INACTIVE", "劳务来源已失效", name=name)
                        continue
                    balance += item.original_amount_fen - item.settled_amount_fen
                    sources.append(
                        {
                            "kind": "labor",
                            "name": name,
                            **self._balance_source(item),
                            "component_id": str(component.id),
                            "facts": component.facts,
                        }
                    )
                row["labor_fen"] = balance
        ordered = sorted(
            (
                row
                for row in payments.values()
                if row["salary_fen"]
                + row["labor_fen"]
                + sum(row[f] for f in REIMBURSEMENT_FIELDS.values())
                > 0
            ),
            key=lambda row: (row["name"], row["employee_id"] or ""),
        )
        for row in ordered:
            row["reimbursement_fen"] = sum(row[field] for field in REIMBURSEMENT_FIELDS.values())
            row["total_fen"] = row["salary_fen"] + row["labor_fen"] + row["reimbursement_fen"]
            if row["employee_id"] is not None and len(by_name[row["name"]]) != 1:
                issue(
                    "DUPLICATE_EMPLOYEE_NAME",
                    "内核员工重名，无法按姓名安全匹配账号",
                    name=row["name"],
                )
            row["has_recipient_account"] = row["name"] in recipients
            if not row["has_recipient_account"]:
                issue(
                    "RECIPIENT_ACCOUNT_MISSING",
                    "缺少收款账号",
                    name=row["name"],
                    employee_id=row["employee_id"],
                )
        if request.reimbursement_employee_ids is not None:
            expected = set(map(str, request.reimbursement_employee_ids))
            actual = {row["employee_id"] for row in ordered if row["reimbursement_fen"] > 0}
            for employee_id in sorted(expected - actual):
                employee = by_id.get(uuid.UUID(employee_id))
                issue(
                    "EXPECTED_REIMBURSEMENT_SOURCE_MISSING",
                    "已确认报销人员缺少本次未结清内核来源，不能按零金额漏人导出",
                    employee_id=employee_id,
                    name=employee.name if employee else None,
                )
            for employee_id in sorted(actual - expected):
                issue(
                    "UNEXPECTED_REIMBURSEMENT_EMPLOYEE",
                    "所选内核来源含本次报销范围之外的人员",
                    employee_id=employee_id,
                )
        row_count = sum(
            row[field] > 0
            for row in ordered
            for field in ("salary_fen", "labor_fen", "reimbursement_fen")
        )
        if row_count > 2000:
            issue("BANK_ROW_LIMIT", "合并付款记录超过模板单文件 2000 条上限")
        if register and request.scope == "complete":
            for name, raw in register.items():
                expected_reimbursement = sum(
                    raw[field] for field in ("rebate_1_fen", "rebate_2_fen", "invoice_fen")
                )
                # Compare original obligations, including settled sources.
                actual_original = sum(
                    source.get("original_amount_fen", 0)
                    for source in sources
                    if source.get("kind") in REIMBURSEMENT_FIELDS
                    and source.get("employee_id") in {str(e.id) for e in by_name.get(name, [])}
                )
                if expected_reimbursement != actual_original:
                    issue(
                        "REGISTER_REIMBURSEMENT_SOURCE_CONFLICT",
                        "原表三项报销合计与已归集内核来源不一致",
                        name=name,
                        expected_fen=expected_reimbursement,
                        kernel_fen=actual_original,
                    )
        if not ordered and not issues:
            issue("NO_OUTSTANDING_PAYMENTS", "所选内核来源没有尚需支付的金额")
        snapshot = {
            "scope": request.scope,
            "material_snapshot_hash": material_snapshot_hash,
            "org_id": str(org_id),
            "company_export_profile": profile,
            "company_name": organization.name,
            "payroll_period": request.payroll_period,
            "include_salary": request.include_salary,
            "reimbursement_employee_ids": sorted(map(str, request.reimbursement_employee_ids))
            if request.reimbursement_employee_ids is not None
            else None,
            "employee_ids": sorted(map(str, selected)),
            "requested_reimbursement_open_item_ids": sorted(
                map(str, request.reimbursement_open_item_ids)
            ),
            "payments": ordered,
            "sources": sources,
            "skipped": skipped,
            "template_sha256": hashlib.sha256(template).hexdigest(),
            "recipients_sha256": canonical_hash(
                {row["name"]: recipients.get(row["name"]) for row in ordered}
            ),
        }
        summary = {
            **snapshot,
            "status": "needs_information" if issues else "ready",
            "source_hash": canonical_hash(snapshot),
            "missing_information": issues,
            "totals": {
                field: sum(row[field] for row in ordered)
                for field in (
                    "salary_fen",
                    "labor_fen",
                    "rebate_1_fen",
                    "rebate_2_fen",
                    "invoice_fen",
                    "combined_reimbursement_fen",
                    "reimbursement_fen",
                    "total_fen",
                )
            },
            "reimbursement_source_counts": counts,
            "unused_recipient_names": sorted(set(recipients) - {row["name"] for row in ordered}),
            "scope_note": (
                "完整代发按公司和月份自动归集，并核对原表及资料完整性；"
                "selected 仅为明确选取的部分来源。"
            ),
            "payment_note": (
                "导出不会登记付款；导出后发生更正或付款须重新生成，银行回单入账后才更新核销状态。"
            ),
        }
        return summary, template, recipients

    @staticmethod
    def _balance_source(item: OpenItem) -> dict:
        return {
            "open_item_id": str(item.id),
            "original_amount_fen": item.original_amount_fen,
            "settled_amount_fen": item.settled_amount_fen,
            "open_item_status": item.status,
        }

    def preview(self, request: PreviewMybankExportRequest) -> dict:
        return self._prepare(request)[0]

    def generate(self, request: GenerateMybankExportRequest) -> dict:
        summary, template, recipients = self._prepare(request)
        if summary["status"] != "ready":
            return summary
        if summary["source_hash"] != request.expected_source_hash:
            return {
                **summary,
                "status": "needs_information",
                "missing_information": [
                    {
                        "code": "SOURCE_CHANGED",
                        "message": "内核、人员范围、模板或账号已变更，请重新预览",
                    }
                ],
            }
        profile = summary["company_export_profile"]
        rows = [
            {
                "name": row["name"],
                "account": recipients[row["name"]],
                "amount_fen": row[field],
                "category": label,
            }
            for row in summary["payments"]
            for field, label in (
                ("salary_fen", profile["salary"]["label"]),
                ("labor_fen", profile["labor"]["label"]),
                ("reimbursement_fen", profile["reimbursement"]["label"]),
            )
            if row[field] > 0
        ]
        files = {
            f"网商银行_{request.payroll_period}_代发.xlsx": render_import(
                template, rows, request.payroll_period
            )
        }
        # Re-read after rendering to detect concurrent corrections/settlements.
        self.session.expire_all()
        current = self._prepare(request)[0]
        if current["source_hash"] != summary["source_hash"] or current["status"] != "ready":
            return {
                **current,
                "status": "needs_information",
                "missing_information": [
                    *current["missing_information"],
                    {"code": "SOURCE_CHANGED", "message": "生成期间来源变更，请重新预览"},
                ],
            }
        return publish_export(request.output_dir, files, summary)
