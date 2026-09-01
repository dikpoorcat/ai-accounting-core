from __future__ import annotations

import hashlib
import uuid
from decimal import Decimal
from io import BytesIO
from typing import Any

import xlwt
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .accounting_periods import canonical_sha256
from .config import Settings, get_settings
from .models import (
    Employee,
    Organization,
    PayrollBatch,
    PayrollLine,
    PayrollTaxImportExport,
)
from .path_security import (
    PathSecurityError,
    read_regular_file_in_root,
    write_new_regular_file_in_root,
)
from .schemas import (
    GeneratePayrollTaxImportRequest,
    PayrollTaxImportEmployeeItem,
    PayrollTaxImportResult,
    PayrollTaxImportResultStatus,
)

_TEMPLATE_SHEET_NAME = "正常工资薪金收入"
_INSTRUCTIONS_SHEET_NAME = "填表说明"
_MEDIA_TYPE = "application/vnd.ms-excel"
_MAX_EXPORT_BYTES = 20 * 1024 * 1024
_MAX_DATA_ROWS = 65_535
_HEADERS = (
    "工号",
    "*姓名",
    "*证件类型",
    "*证件号码",
    "本期收入",
    "本期免税收入",
    "基本养老保险费",
    "基本医疗保险费",
    "失业保险费",
    "住房公积金",
    "累计子女教育",
    "累计继续教育",
    "累计住房贷款利息",
    "累计住房租金",
    "累计赡养老人",
    "累计3岁以下婴幼儿照护",
    "累计个人养老金",
    "企业(职业)年金",
    "商业健康保险",
    "税延养老保险",
    "公务交通费用",
    "通讯费用",
    "律师办案费用",
    "住房公积金调整",
    "西藏附加减除费用",
    "其他",
    "准予扣除的捐赠额",
    "减免税额",
    "协定减免",
    "备注",
)
_COLUMN_WIDTHS = (
    8.44,
    11.75,
    24.38,
    19.69,
    12.13,
    18.44,
    15.63,
    14.13,
    11.13,
    11.13,
    17.44,
    16.69,
    17.75,
    13.25,
    13.25,
    16.75,
    16.75,
    17.25,
    15.19,
    14.94,
    14.94,
    14.94,
    14.94,
    14.94,
    14.94,
    8.94,
    17.25,
    8.94,
    8.94,
    13.44,
)
_DOCUMENT_TYPES = (
    "居民身份证",
    "港澳居民来往内地通行证",
    "港澳居民来往内地通行证（非中国籍）",
    "中华人民共和国港澳居民居住证",
    "台湾居民来往大陆通行证",
    "中华人民共和国台湾居民居住证",
    "中国护照",
    "外国护照",
    "外国人永久居留身份证（外国人永久居留证）",
    "中华人民共和国外国人工作许可证（A类）",
    "中华人民共和国外国人工作许可证（B类）",
    "中华人民共和国外国人工作许可证（C类）",
    "其他个人证件",
)


def payroll_tax_source_snapshot(
    rows: list[tuple[PayrollBatch, PayrollLine, Employee]],
    org_id: uuid.UUID,
    payroll_period: str,
) -> dict[str, Any]:
    """Return the shared source hash used by export persistence and workflow staleness."""

    projection = {
        "version": "payroll_tax_import_source_v1",
        "org_id": str(org_id),
        "payroll_period": payroll_period,
        "lines": [
            {
                "batch_id": str(batch.id),
                "batch_calculation_hash": batch.calculation_hash,
                "employee_id": str(line.employee_id),
                "profile_id": str(line.employee_payroll_profile_version_id),
                "tax_reported_salary_fen": line.tax_reported_salary_fen,
                "special_additional_deduction_fen": line.special_additional_deduction_fen,
                "other_legal_deduction_fen": line.other_legal_deduction_fen,
                "individual_income_tax_fen": line.individual_income_tax_fen,
            }
            for batch, line, _employee in sorted(
                rows,
                key=lambda item: (
                    str(item[0].id),
                    str(item[1].employee_id),
                    str(item[1].id),
                ),
            )
        ],
    }
    return {"hash": canonical_sha256(projection), "data": projection}


class PayrollTaxImportService:
    """Generate a tax-client import workbook from immutable posted payroll facts."""

    def __init__(self, session: Session, *, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()

    def generate(self, request: GeneratePayrollTaxImportRequest) -> PayrollTaxImportResult:
        if self.session.get(Organization, request.org_id) is None:
            return PayrollTaxImportResult(
                status=PayrollTaxImportResultStatus.REJECTED,
                errors=["ORGANIZATION_NOT_FOUND"],
            )
        request_payload_hash = canonical_sha256(
            {
                "command": "finance_generate_payroll_tax_import",
                "request": request.model_dump(mode="json"),
            }
        )
        existing = self.session.scalar(
            select(PayrollTaxImportExport).where(
                PayrollTaxImportExport.org_id == request.org_id,
                PayrollTaxImportExport.idempotency_key == request.idempotency_key,
            )
        )
        if existing is not None and existing.request_payload_hash != request_payload_hash:
            return PayrollTaxImportResult(
                status=PayrollTaxImportResultStatus.REJECTED,
                errors=["PAYROLL_TAX_IMPORT_IDEMPOTENCY_PAYLOAD_MISMATCH"],
            )
        rows = self.session.execute(
            select(PayrollBatch, PayrollLine, Employee)
            .join(
                PayrollLine,
                (PayrollLine.org_id == PayrollBatch.org_id)
                & (PayrollLine.payroll_batch_id == PayrollBatch.id),
            )
            .join(
                Employee,
                (Employee.org_id == PayrollLine.org_id)
                & (Employee.id == PayrollLine.employee_id),
            )
            .where(
                PayrollBatch.org_id == request.org_id,
                PayrollBatch.batch_kind == "regular",
                PayrollBatch.payroll_period == request.payroll_period,
                PayrollBatch.status == "posted",
                PayrollBatch.reversal_of_batch_id.is_(None),
                PayrollLine.wage_tax_declaration_state == "declared",
            )
            .order_by(Employee.employee_code, PayrollLine.id)
        ).all()
        if not rows:
            return PayrollTaxImportResult(
                status=PayrollTaxImportResultStatus.NEEDS_INFORMATION,
                missing_information=[
                    {
                        "code": "posted_regular_payroll",
                        "message": (
                            f"{request.payroll_period} has no posted regular payroll lines "
                            "included in wage-tax declaration"
                        ),
                        "fields": ["payroll_period"],
                    }
                ],
            )
        if len(rows) > _MAX_DATA_ROWS:
            return PayrollTaxImportResult(
                status=PayrollTaxImportResultStatus.REJECTED,
                errors=["PAYROLL_TAX_IMPORT_XLS_ROW_LIMIT_EXCEEDED"],
            )

        payroll_source = payroll_tax_source_snapshot(rows, request.org_id, request.payroll_period)
        source_snapshot_data = {
            "version": "payroll_tax_import_export_source_v1",
            "payroll_source": payroll_source["data"],
            "employee_items": [
                item.model_dump(mode="json")
                for item in sorted(request.employee_items, key=lambda item: str(item.employee_id))
            ],
            "insurance_code_mapping": {
                "pension": request.pension_insurance_code,
                "medical": request.medical_insurance_code,
                "unemployment": request.unemployment_insurance_code,
            },
        }
        source_snapshot = {
            "data": source_snapshot_data,
            "hash": canonical_sha256(source_snapshot_data),
        }
        if existing is not None and existing.source_snapshot_hash != source_snapshot["hash"]:
            return PayrollTaxImportResult(
                status=PayrollTaxImportResultStatus.REJECTED,
                export_id=existing.id,
                errors=["PAYROLL_TAX_IMPORT_IDEMPOTENCY_SOURCE_STALE"],
            )

        supplied = {item.employee_id: item for item in request.employee_items}
        payroll_employee_ids = {line.employee_id for _batch, line, _employee in rows}
        missing_employee_ids = sorted(payroll_employee_ids - supplied.keys(), key=str)
        unexpected_employee_ids = sorted(supplied.keys() - payroll_employee_ids, key=str)
        if missing_employee_ids or unexpected_employee_ids:
            return PayrollTaxImportResult(
                status=PayrollTaxImportResultStatus.NEEDS_INFORMATION,
                missing_information=[
                    {
                        "code": "payroll_tax_import_employee_set",
                        "message": (
                            "tax import facts must contain exactly every declared payroll employee"
                        ),
                        "fields": ["employee_items"],
                        "missing_employee_ids": [str(value) for value in missing_employee_ids],
                        "unexpected_employee_ids": [
                            str(value) for value in unexpected_employee_ids
                        ],
                    }
                ],
            )

        workbook_rows: list[list[Any]] = []
        missing_information: list[dict[str, Any]] = []
        employee_sources: list[dict[str, str]] = []
        source_batch_ids: set[uuid.UUID] = set()
        for batch, line, employee in rows:
            item = supplied[line.employee_id]
            state = _tax_state_after(line)
            if (
                state is None
                or state.get("through_period") != request.payroll_period
                or _nonnegative_int(state.get("cumulative_special_additional_deduction_fen"))
                is None
                or _nonnegative_int(state.get("cumulative_other_legal_deduction_fen")) is None
            ):
                return PayrollTaxImportResult(
                    status=PayrollTaxImportResultStatus.REJECTED,
                    errors=["PAYROLL_TAX_IMPORT_CUMULATIVE_STATE_INVALID"],
                )
            employee_missing = self._validate_employee_facts(
                request=request,
                batch=batch,
                line=line,
                employee=employee,
                item=item,
                state=state,
            )
            if employee_missing:
                missing_information.extend(employee_missing)
                continue
            workbook_rows.append(
                self._workbook_row(
                    request=request,
                    line=line,
                    employee=employee,
                    item=item,
                )
            )
            source_batch_ids.add(batch.id)
            employee_sources.append(
                {
                    "employee_id": str(employee.id),
                    "employee_code": employee.employee_code,
                    "payroll_batch_id": str(batch.id),
                    "calculation_hash": batch.calculation_hash,
                }
            )
        if missing_information:
            return PayrollTaxImportResult(
                status=PayrollTaxImportResultStatus.NEEDS_INFORMATION,
                source_batch_ids=sorted(source_batch_ids, key=str),
                missing_information=missing_information,
            )

        content = build_payroll_tax_import_xls(workbook_rows)
        digest = hashlib.sha256(content).hexdigest()
        file_name = f"正常工资薪金所得_{request.payroll_period}_{digest}.xls"
        storage_root = self.settings.finance_storage_dir
        output_path = (
            storage_root
            / "exports"
            / "individual-income-tax"
            / str(request.org_id)
            / request.payroll_period
            / file_name
        )
        try:
            written_path = write_new_regular_file_in_root(
                output_path,
                storage_root,
                content,
                max_bytes=_MAX_EXPORT_BYTES,
            )
            _, stored_content = read_regular_file_in_root(
                written_path,
                storage_root,
                max_bytes=_MAX_EXPORT_BYTES,
            )
        except PathSecurityError as exc:
            return PayrollTaxImportResult(
                status=PayrollTaxImportResultStatus.REJECTED,
                errors=[str(exc)],
            )
        if hashlib.sha256(stored_content).hexdigest() != digest:
            return PayrollTaxImportResult(
                status=PayrollTaxImportResultStatus.REJECTED,
                errors=["PAYROLL_TAX_IMPORT_FILE_COLLISION"],
            )
        relative_path = written_path.relative_to(storage_root.resolve(strict=True)).as_posix()
        source_batches = sorted(
            (
                {
                    "batch_id": str(batch_id),
                    "calculation_hash": next(
                        item["calculation_hash"]
                        for item in employee_sources
                        if item["payroll_batch_id"] == str(batch_id)
                    ),
                }
                for batch_id in source_batch_ids
            ),
            key=lambda item: item["batch_id"],
        )
        if existing is not None:
            if (
                existing.file_sha256 != digest
                or existing.file_name != file_name
                or existing.relative_storage_path != relative_path
                or existing.row_count != len(workbook_rows)
            ):
                return PayrollTaxImportResult(
                    status=PayrollTaxImportResultStatus.REJECTED,
                    export_id=existing.id,
                    errors=["PAYROLL_TAX_IMPORT_EXPORT_RECORD_MISMATCH"],
                )
            return PayrollTaxImportResult(
                status=PayrollTaxImportResultStatus.GENERATED,
                export_id=existing.id,
                file_name=file_name,
                file_path=written_path,
                media_type=_MEDIA_TYPE,
                sha256=digest,
                row_count=len(workbook_rows),
                source_batch_ids=sorted(source_batch_ids, key=str),
                data={
                    "template_sheet": _TEMPLATE_SHEET_NAME,
                    "template_headers": list(_HEADERS),
                    "employee_sources": employee_sources,
                    "payroll_source_hash": payroll_source["hash"],
                    "source_snapshot_hash": source_snapshot["hash"],
                    "relative_storage_path": relative_path,
                },
                idempotent_replay=True,
            )

        predecessor = self.session.scalar(
            select(PayrollTaxImportExport)
            .where(
                PayrollTaxImportExport.org_id == request.org_id,
                PayrollTaxImportExport.payroll_period == request.payroll_period,
            )
            .order_by(PayrollTaxImportExport.created_at.desc(), PayrollTaxImportExport.id.desc())
            .limit(1)
        )
        export = PayrollTaxImportExport(
            org_id=request.org_id,
            payroll_period=request.payroll_period,
            payroll_source_hash=payroll_source["hash"],
            source_snapshot_hash=source_snapshot["hash"],
            source_batches=source_batches,
            idempotency_key=request.idempotency_key,
            request_payload_hash=request_payload_hash,
            relative_storage_path=relative_path,
            file_name=file_name,
            file_sha256=digest,
            row_count=len(workbook_rows),
            supersedes_id=predecessor.id if predecessor is not None else None,
        )
        try:
            with self.session.begin_nested():
                self.session.add(export)
                self.session.flush()
        except IntegrityError:
            concurrent = self.session.scalar(
                select(PayrollTaxImportExport).where(
                    PayrollTaxImportExport.org_id == request.org_id,
                    PayrollTaxImportExport.idempotency_key == request.idempotency_key,
                )
            )
            if concurrent is None or concurrent.request_payload_hash != request_payload_hash:
                return PayrollTaxImportResult(
                    status=PayrollTaxImportResultStatus.REJECTED,
                    errors=["PAYROLL_TAX_IMPORT_CONCURRENT_WRITE_CONFLICT"],
                )
            export = concurrent
        return PayrollTaxImportResult(
            status=PayrollTaxImportResultStatus.GENERATED,
            export_id=export.id,
            file_name=file_name,
            file_path=written_path,
            media_type=_MEDIA_TYPE,
            sha256=digest,
            row_count=len(workbook_rows),
            source_batch_ids=sorted(source_batch_ids, key=str),
            data={
                "template_sheet": _TEMPLATE_SHEET_NAME,
                "template_headers": list(_HEADERS),
                "employee_sources": employee_sources,
                "payroll_source_hash": payroll_source["hash"],
                "source_snapshot_hash": source_snapshot["hash"],
                "relative_storage_path": relative_path,
            },
        )

    def _validate_employee_facts(
        self,
        *,
        request: GeneratePayrollTaxImportRequest,
        batch: PayrollBatch,
        line: PayrollLine,
        employee: Employee,
        item: PayrollTaxImportEmployeeItem,
        state: dict[str, Any],
    ) -> list[dict[str, Any]]:
        missing: list[dict[str, Any]] = []
        special_total = sum(
            (
                item.cumulative_child_education_fen,
                item.cumulative_continuing_education_fen,
                item.cumulative_housing_loan_interest_fen,
                item.cumulative_housing_rent_fen,
                item.cumulative_elderly_support_fen,
                item.cumulative_infant_care_fen,
            )
        )
        expected_special = int(state["cumulative_special_additional_deduction_fen"])
        if special_total != expected_special:
            missing.append(
                _employee_requirement(
                    employee,
                    "cumulative_special_additional_deduction_breakdown",
                    "cumulative special-additional deduction columns must reconcile to payroll",
                    [
                        "cumulative_child_education_fen",
                        "cumulative_continuing_education_fen",
                        "cumulative_housing_loan_interest_fen",
                        "cumulative_housing_rent_fen",
                        "cumulative_elderly_support_fen",
                        "cumulative_infant_care_fen",
                    ],
                    expected_fen=expected_special,
                    supplied_fen=special_total,
                )
            )

        current_other_total = sum(
            (
                item.current_personal_pension_fen,
                item.enterprise_occupational_annuity_fen,
                item.commercial_health_insurance_fen,
                item.tax_deferred_pension_insurance_fen,
                item.official_transportation_fen,
                item.communication_fen,
                item.lawyer_case_expense_fen,
                item.housing_fund_adjustment_fen,
                item.tibet_additional_deduction_fen,
                item.other_deduction_fen,
                item.deductible_donation_fen,
            )
        )
        if current_other_total != line.other_legal_deduction_fen:
            missing.append(
                _employee_requirement(
                    employee,
                    "current_other_legal_deduction_breakdown",
                    "current other legal-deduction columns must reconcile to payroll",
                    [
                        "current_personal_pension_fen",
                        "enterprise_occupational_annuity_fen",
                        "commercial_health_insurance_fen",
                        "tax_deferred_pension_insurance_fen",
                        "official_transportation_fen",
                        "communication_fen",
                        "lawyer_case_expense_fen",
                        "housing_fund_adjustment_fen",
                        "tibet_additional_deduction_fen",
                        "other_deduction_fen",
                        "deductible_donation_fen",
                    ],
                    expected_fen=line.other_legal_deduction_fen,
                    supplied_fen=current_other_total,
                )
            )
        cumulative_other = int(state["cumulative_other_legal_deduction_fen"])
        if (
            item.current_personal_pension_fen > item.cumulative_personal_pension_fen
            or item.cumulative_personal_pension_fen > cumulative_other
        ):
            missing.append(
                _employee_requirement(
                    employee,
                    "cumulative_personal_pension",
                    "personal-pension current and cumulative amounts conflict with payroll state",
                    ["current_personal_pension_fen", "cumulative_personal_pension_fen"],
                    cumulative_other_legal_deduction_fen=cumulative_other,
                )
            )

        tax_relief = _current_tax_relief_fen(batch, employee.id)
        if tax_relief is None:
            return [
                _employee_requirement(
                    employee,
                    "payroll_tax_relief_source",
                    "posted payroll lacks its immutable current tax-relief input",
                    ["payroll_batch"],
                )
            ]
        supplied_tax_relief = item.tax_relief_fen + item.treaty_relief_fen
        if supplied_tax_relief != tax_relief:
            missing.append(
                _employee_requirement(
                    employee,
                    "current_tax_relief_breakdown",
                    "tax-relief and treaty-relief columns must reconcile to payroll",
                    ["tax_relief_fen", "treaty_relief_fen"],
                    expected_fen=tax_relief,
                    supplied_fen=supplied_tax_relief,
                )
            )

        allowed_social_codes = {
            request.pension_insurance_code,
            request.medical_insurance_code,
            request.unemployment_insurance_code,
        }
        unsupported_social = {
            code: int(amount)
            for code, amount in line.employee_social_insurance_items.items()
            if code not in allowed_social_codes and int(amount) != 0
        }
        if unsupported_social:
            missing.append(
                _employee_requirement(
                    employee,
                    "social_insurance_import_mapping",
                    "non-zero employee social-insurance codes are not mapped to tax columns",
                    [
                        "pension_insurance_code",
                        "medical_insurance_code",
                        "unemployment_insurance_code",
                    ],
                    unsupported_components_fen=unsupported_social,
                )
            )
        return missing

    @staticmethod
    def _workbook_row(
        *,
        request: GeneratePayrollTaxImportRequest,
        line: PayrollLine,
        employee: Employee,
        item: PayrollTaxImportEmployeeItem,
    ) -> list[Any]:
        social = line.employee_social_insurance_items
        return [
            employee.employee_code,
            employee.name,
            item.document_type.value,
            item.document_number,
            _yuan(line.tax_reported_salary_fen or 0),
            _yuan(0),
            _yuan(int(social.get(request.pension_insurance_code, 0))),
            _yuan(int(social.get(request.medical_insurance_code, 0))),
            _yuan(int(social.get(request.unemployment_insurance_code, 0))),
            _yuan(line.employee_housing_fund_fen),
            _yuan(item.cumulative_child_education_fen),
            _yuan(item.cumulative_continuing_education_fen),
            _yuan(item.cumulative_housing_loan_interest_fen),
            _yuan(item.cumulative_housing_rent_fen),
            _yuan(item.cumulative_elderly_support_fen),
            _yuan(item.cumulative_infant_care_fen),
            _yuan(item.cumulative_personal_pension_fen),
            _yuan(item.enterprise_occupational_annuity_fen),
            _yuan(item.commercial_health_insurance_fen),
            _yuan(item.tax_deferred_pension_insurance_fen),
            _yuan(item.official_transportation_fen),
            _yuan(item.communication_fen),
            _yuan(item.lawyer_case_expense_fen),
            _yuan(item.housing_fund_adjustment_fen),
            _yuan(item.tibet_additional_deduction_fen),
            _yuan(item.other_deduction_fen),
            _yuan(item.deductible_donation_fen),
            _yuan(item.tax_relief_fen),
            _yuan(item.treaty_relief_fen),
            item.remark,
        ]


def build_payroll_tax_import_xls(rows: list[list[Any]]) -> bytes:
    """Build the exact two-sheet BIFF8 shape of the tax-authority template."""

    workbook = xlwt.Workbook(encoding="utf-8")
    workbook.set_colour_RGB(xlwt.Style.colour_map["ice_blue"], 221, 235, 247)
    data_sheet = workbook.add_sheet(_TEMPLATE_SHEET_NAME, cell_overwrite_ok=False)
    instructions_sheet = workbook.add_sheet(_INSTRUCTIONS_SHEET_NAME, cell_overwrite_ok=False)
    header_style = _style(
        bold=True,
        horizontal="center",
        vertical="center",
        wrap=True,
        background="ice_blue",
    )
    required_header_style = _style(
        bold=True,
        font_color="red",
        horizontal="center",
        vertical="center",
        wrap=True,
        background="ice_blue",
    )
    text_style = _style(number_format="@")
    money_style = _style(number_format="0.00_);(0.00)")
    for column, (header, width) in enumerate(zip(_HEADERS, _COLUMN_WIDTHS, strict=True)):
        data_sheet.col(column).width = min(65_535, round(width * 256))
        data_sheet.write(
            0,
            column,
            header,
            required_header_style if column in {1, 2, 3} else header_style,
        )
    data_sheet.row(0).height_mismatch = True
    data_sheet.row(0).height = 900
    data_sheet.panes_frozen = True
    data_sheet.horz_split_pos = 1
    for row_index, values in enumerate(rows, start=1):
        if len(values) != len(_HEADERS):
            raise ValueError("PAYROLL_TAX_IMPORT_ROW_WIDTH_INVALID")
        for column, value in enumerate(values):
            data_sheet.write(
                row_index,
                column,
                value,
                text_style if column in {0, 1, 2, 3, 29} else money_style,
            )

    note_style = _style(font_size=12, wrap=True)
    red_heading_style = _style(bold=True, font_color="red", font_size=12)
    instructions_sheet.col(0).width = round(57.5 * 256)
    instructions_sheet.write(
        0,
        0,
        "注意事项：\n"
        "1、模板中标识为红色带*号的栏目为必填项，导入时不能为空！\n"
        "2、部分栏目内容需从如下表格中选择，否则系统禁止导入！",
        note_style,
    )
    instructions_sheet.row(0).height_mismatch = True
    instructions_sheet.row(0).height = 1_350
    instructions_sheet.write(4, 0, "证照类型填写范围", red_heading_style)
    bordered_style = _style(border=True, font_size=12)
    for row_index, document_type in enumerate(_DOCUMENT_TYPES, start=5):
        instructions_sheet.write(row_index, 0, document_type, bordered_style)
    instructions_sheet.write(19, 0, "金额栏数据格式填写说明", red_heading_style)
    instructions_sheet.write(20, 0, "小数点后保留两位，多于两位的数据自动\n四舍五入", note_style)

    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def _style(
    *,
    bold: bool = False,
    font_color: str | None = None,
    font_size: int = 10,
    horizontal: str | None = None,
    vertical: str | None = None,
    wrap: bool = False,
    background: str | None = None,
    border: bool = False,
    number_format: str | None = None,
) -> xlwt.XFStyle:
    style = xlwt.XFStyle()
    font = xlwt.Font()
    font.name = "宋体"
    font.height = font_size * 20
    font.bold = bold
    if font_color is not None:
        font.colour_index = xlwt.Style.colour_map[font_color]
    style.font = font
    alignment = xlwt.Alignment()
    if horizontal is not None:
        alignment.horz = {
            "left": xlwt.Alignment.HORZ_LEFT,
            "center": xlwt.Alignment.HORZ_CENTER,
        }[horizontal]
    if vertical is not None:
        alignment.vert = {
            "center": xlwt.Alignment.VERT_CENTER,
        }[vertical]
    alignment.wrap = int(wrap)
    style.alignment = alignment
    if background is not None:
        pattern = xlwt.Pattern()
        pattern.pattern = xlwt.Pattern.SOLID_PATTERN
        pattern.pattern_fore_colour = xlwt.Style.colour_map[background]
        style.pattern = pattern
    if border:
        borders = xlwt.Borders()
        borders.left = xlwt.Borders.THIN
        borders.right = xlwt.Borders.THIN
        borders.top = xlwt.Borders.THIN
        borders.bottom = xlwt.Borders.THIN
        style.borders = borders
    if number_format is not None:
        style.num_format_str = number_format
    return style


def _tax_state_after(line: PayrollLine) -> dict[str, Any] | None:
    entry = next(
        (
            value
            for value in reversed(line.calculation_trace)
            if value.get("step") == "tax_state_after"
        ),
        None,
    )
    values = entry.get("values") if entry else None
    return values if isinstance(values, dict) else None


def _current_tax_relief_fen(batch: PayrollBatch, employee_id: uuid.UUID) -> int | None:
    request = batch.calculation_input.get("request")
    if not isinstance(request, dict):
        return None
    employee_items = request.get("employee_items")
    if not isinstance(employee_items, list):
        return None
    matches = [
        item
        for item in employee_items
        if isinstance(item, dict) and item.get("employee_id") == str(employee_id)
    ]
    if len(matches) != 1:
        return None
    value = matches[0].get("tax_relief_fen")
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _nonnegative_int(value: Any) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    return value


def _employee_requirement(
    employee: Employee,
    code: str,
    message: str,
    fields: list[str],
    **details: Any,
) -> dict[str, Any]:
    return {
        "code": code,
        "message": f"employee {employee.employee_code}: {message}",
        "fields": fields,
        "employee_id": str(employee.id),
        "employee_code": employee.employee_code,
        **details,
    }


def _yuan(value_fen: int) -> Decimal:
    return (Decimal(value_fen) / Decimal(100)).quantize(Decimal("0.00"))
