"""Frozen tax-client import files; producing a file is never a tax submission.

The BIFF8 renderer below preserves the former validated two-sheet format. It has
no ORM, legacy service, or business database dependency.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Annotated, Any, ClassVar

import xlwt
from pydantic import Field, model_validator

from .backup import _worker_lock
from .contracts import Context, Fact, KernelError, Read
from .domains.payroll import PAYROLL_KINDS, employee_month
from .types import NonNegativeFen, YearMonth, canonical, digest, sum_fen
from .workflow import payroll_required_reads, payroll_required_work

SPECIAL_COLUMNS = (
    "child_education",
    "continuing_education",
    "housing_loan_interest",
    "housing_rent",
    "elderly_support",
    "infant_care",
)
OTHER_COLUMNS = (
    "current_personal_pension",
    "enterprise_occupational_annuity",
    "commercial_health_insurance",
    "tax_deferred_pension_insurance",
    "official_transportation",
    "communication",
    "lawyer_case_expense",
    "housing_fund_adjustment",
    "tibet_additional_deduction",
    "other_deduction",
    "deductible_donation",
)


class TaxImportIdentity(Fact):
    kind: ClassVar[str] = "tax_import_identity_v2"
    lane: ClassVar[str] = "management"
    identity_fields: ClassVar[tuple[str, ...]] = ("employee_id",)
    employee_id: str = Field(min_length=1)
    employee_code: str = Field(min_length=1)
    name: str = Field(min_length=1)
    document_type: str = Field(min_length=1)
    document_number: str = Field(min_length=1)

    def scopes(self):
        return ("tax-identity:" + self.employee_id,)

    @model_validator(mode="after")
    def known_document(self):
        if self.document_type not in _DOCUMENT_TYPES:
            raise ValueError("document type must match the tax client template")
        return self


class TaxImportDetails(Fact):
    kind: ClassVar[str] = "tax_import_details_v2"
    lane: ClassVar[str] = "management"
    identity_fields: ClassVar[tuple[str, ...]] = ("employee_id", "period")
    employee_id: str = Field(min_length=1)
    payroll_result_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    cumulative_special_fen: dict[str, NonNegativeFen]
    current_other_fen: dict[str, NonNegativeFen]
    cumulative_personal_pension_fen: NonNegativeFen
    tax_relief_fen: NonNegativeFen
    treaty_relief_fen: NonNegativeFen
    remark: str = ""

    def scopes(self):
        return (str(self.period), employee_month(self.employee_id, self.period))

    @model_validator(mode="after")
    def exact_columns(self):
        if set(self.cumulative_special_fen) != set(SPECIAL_COLUMNS) or set(
            self.current_other_fen
        ) != set(OTHER_COLUMNS):
            raise ValueError("every deduction column must be explicitly supplied, including zero")
        return self


class TaxImportMapping(Fact):
    kind: ClassVar[str] = "tax_import_mapping_v2"
    lane: ClassVar[str] = "management"
    identity_fields: ClassVar[tuple[str, ...]] = ("period",)
    pension_code: str | None
    medical_code: str | None
    unemployment_code: str | None

    @model_validator(mode="after")
    def separate_codes(self):
        codes = [
            item
            for item in (self.pension_code, self.medical_code, self.unemployment_code)
            if item is not None
        ]
        if any(not item for item in codes) or len(codes) != len(set(codes)):
            raise ValueError("tax social columns need distinct explicit codes or explicit absence")
        return self


def register(registry):
    for model in (TaxImportIdentity, TaxImportDetails, TaxImportMapping):
        registry.register(model)


def _contributions(connection, store, calculation, mapping):
    policy = store.select(
        connection,
        Read("fact", "payroll_contribution_policy", "#" + calculation.values["rule_versions"][0]),
    )
    raw = connection.execute(
        "SELECT outcome FROM calculation WHERE id=?", (calculation.id,)
    ).fetchone()
    trace = [
        item["values"]
        for item in json.loads(raw[0])["explanation"]
        if item["step"] == "contribution_burden_allocation"
    ]
    if len(policy) != 1 or len(trace) != len(policy[0].fact.rules):
        raise ValueError("published contribution breakdown is incomplete")
    social, housing = {}, []
    for rule, item in zip(policy[0].fact.rules, trace, strict=True):
        if item["code"] != rule.code:
            raise ValueError("contribution component provenance differs")
        amount = item["employee_deduction_fen"]
        if rule.base_kind == "social_insurance":
            if rule.code in social:
                raise ValueError("ambiguous social component")
            social[rule.code] = amount
        else:
            housing.append(amount)
    selected = (mapping.pension_code, mapping.medical_code, mapping.unemployment_code)
    if any(amount and code not in selected for code, amount in social.items()):
        raise ValueError("a nonzero employee insurance component has no tax import column")
    if any(code is not None and code not in social for code in selected):
        raise ValueError("a declared tax insurance code is not in the published policy")
    amounts = [social.get(code, 0) for code in selected] + [sum_fen(housing)]
    if sum_fen(amounts) != calculation.values["employee_contributions_fen"]:
        raise ValueError("tax insurance columns differ from published employee deductions")
    return amounts


class TaxImport:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    def preview(self, period: str):
        month = YearMonth(period)
        with self.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            facts = [
                item
                for kind in PAYROLL_KINDS
                for item in self.store.select(connection, Read("fact", kind, period))
            ]
            calculations = {
                item.subject_id: item
                for kind in PAYROLL_KINDS
                for item in self.store.select(connection, Read("calculation", kind, period))
            }
            pending = {
                row[0] for row in connection.execute("SELECT DISTINCT subject_id FROM pending")
            }
            mappings = self.store.select(connection, Read("fact", TaxImportMapping.kind, period))
            issues = payroll_required_work(
                month,
                Context(
                    {
                        read: self.store.select(connection, read)
                        for read in payroll_required_reads(month)
                    }
                ),
            )
            rows, sources, excluded = [], [], []
            if not facts:
                issues.append(
                    {"field": "payroll", "message": "没有已明确的本月工资，不推定空申报文件"}
                )
            if len(mappings) != 1:
                issues.append(
                    {"field": "tax_import_mapping", "message": "需要唯一的险种与个税列对应关系"}
                )
            else:
                sources.append(mappings[0].id)
            for version in sorted(facts, key=lambda item: item.fact.employee_id):
                fact, calculation = version.fact, calculations.get(version.subject_id)
                employee = fact.employee_id
                identity = self.store.select(
                    connection, Read("fact", TaxImportIdentity.kind, "tax-identity:" + employee)
                )
                details = self.store.select(
                    connection, Read("fact", TaxImportDetails.kind, employee_month(employee, month))
                )
                if (
                    calculation is None
                    or calculation.fact_id != version.id
                    or version.subject_id in pending
                ):
                    issues.append(
                        {
                            "field": "payroll",
                            "employee_id": employee,
                            "message": "工资尚未正式发布或待更正",
                        }
                    )
                    continue
                if calculation.values.get("tax_status") == "not_started":
                    excluded.append(
                        {
                            "employee_id": employee,
                            "fact_id": version.id,
                            "calculation_id": calculation.id,
                            "reason": "withholding_not_started",
                            "withholding_start": calculation.values["withholding_start"],
                        }
                    )
                    sources.extend((version.id, calculation.id))
                    continue
                if calculation.values.get("tax_state_bounds"):
                    issues.extend(
                        {
                            **item,
                            "employee_id": employee,
                            "message": (
                                "个税文件需要完整已确认的本期和累计扣除，不能导出零税证明下界"
                            ),
                            "reusable_sources": [item["fact_id"], "confirmed_payroll_deductions"],
                        }
                        for item in calculation.values["tax_state_bounds"]["unknown_fields"]
                    )
                    continue
                if len(identity) != 1 or len(details) != 1 or len(mappings) != 1:
                    issues.append(
                        {
                            "field": "tax_import_details",
                            "employee_id": employee,
                            "message": "导出需要身份、扣除明细和险种映射；这些资料不阻断工资入账",
                        }
                    )
                    continue
                detail, person = details[0].fact, identity[0].fact
                errors = []
                state = calculation.values["tax_state"]
                if detail.payroll_result_digest != calculation.result_digest:
                    errors.append("扣除明细依据的正式工资结果已变化")
                if (
                    sum_fen(detail.cumulative_special_fen.values())
                    != state["cumulative_special_additional_deduction_fen"]
                ):
                    errors.append("累计专项附加扣除明细与正式累计税额依据不符")
                if sum_fen(detail.current_other_fen.values()) != fact.other_legal_deduction_fen:
                    errors.append("本期其他扣除明细与正式工资不符")
                if (
                    not detail.current_other_fen["current_personal_pension"]
                    <= detail.cumulative_personal_pension_fen
                    <= state["cumulative_other_legal_deduction_fen"]
                ):
                    errors.append("个人养老金的本期与累计数不符")
                if (
                    sum_fen((detail.tax_relief_fen, detail.treaty_relief_fen))
                    != fact.tax_relief_fen
                ):
                    errors.append("减免税拆分与正式工资不符")
                try:
                    contributions = _contributions(
                        connection, self.store, calculation, mappings[0].fact
                    )
                except (ValueError, KeyError) as exc:
                    errors.append(str(exc))
                    contributions = []
                if errors:
                    issues.extend(
                        {"field": "tax_import_details", "employee_id": employee, "message": error}
                        for error in errors
                    )
                    continue
                rows.append(
                    [
                        person.employee_code,
                        person.name,
                        person.document_type,
                        person.document_number,
                        fact.tax_reported_salary_fen,
                        fact.tax_exempt_income_fen,
                        *contributions,
                        *(detail.cumulative_special_fen[key] for key in SPECIAL_COLUMNS),
                        detail.cumulative_personal_pension_fen,
                        *(detail.current_other_fen[key] for key in OTHER_COLUMNS[1:]),
                        detail.tax_relief_fen,
                        detail.treaty_relief_fen,
                        detail.remark,
                    ]
                )
                sources.extend((version.id, calculation.id, identity[0].id, details[0].id))
            if facts and len(excluded) == len(facts):
                issues = [issue for issue in issues if issue["field"] != "tax_import_mapping"]
                issues.append(
                    {
                        "field": "tax_income_period",
                        "message": (
                            "本期仅有扣缴起点前的社保成本，无本类工资税导出行，不生成空申报文件"
                        ),
                    }
                )
            for issue in issues:
                issue.setdefault("semantics", "export_only")
            if len(rows) > 65_535:
                issues.append({"field": "rows", "message": "BIFF8模板最多65535条工资记录"})
            plan = {
                "company_id": self.store.company_id,
                "database_id": self.store.database_id,
                "period": month,
                "rows_fen": rows,
                "row_count": len(rows),
                "source_versions": sorted(sources),
                "excluded_sources": excluded,
                "fact_issues": issues,
                "epochs": {
                    key: value
                    for key, value in self.store.epochs(connection).items()
                    if key in {"accounting", "management"}
                },
            }
            return {
                **plan,
                "status": "needs_information" if issues else "ready",
                "digest": digest(plan).hex(),
            }

    def confirm(self, period: str, *, preview_digest: str, output_directory: str, request_id: str):
        directory = str(Path(output_directory).resolve())
        request_hash = digest(["tax_import", period, preview_digest, directory])
        cached = self.engine._cached(request_id, request_hash)
        if cached is not None:
            return cached
        plan = self.preview(period)
        if plan["digest"] != preview_digest:
            raise KernelError("preview_expired", "工资或导入资料已有变化，请重新预览")
        if plan["fact_issues"]:
            raise KernelError(
                "needs_information", "个税导入文件尚缺明确资料", fact_issues=plan["fact_issues"]
            )

        def operation(connection):
            job = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO jobs(id,kind,payload,status) VALUES(?,'tax_import',?,'pending')",
                (job, canonical({"plan": plan, "output_directory": directory})),
            )
            return {
                "status": "queued",
                "job_id": job,
                "row_count": plan["row_count"],
                "preview_digest": preview_digest,
            }

        return self.engine._write(
            request_id,
            request_hash,
            plan["epochs"],
            (),
            "tax_import",
            operation,
            checked_lanes=("accounting", "management"),
        )


def _verify_tax_plan(raw, plan):
    # Verify the exact two-sheet shape and every frozen original amount/text.
    import xlrd

    book = xlrd.open_workbook(file_contents=raw)
    sheet = book.sheet_by_index(0)
    if (
        book.sheet_names() != [_TEMPLATE_SHEET_NAME, _INSTRUCTIONS_SHEET_NAME]
        or sheet.row_values(0) != list(_HEADERS)
        or sheet.nrows != len(plan["rows_fen"]) + 1
    ):
        raise KernelError("export_verification_failed", "BIFF8模板结构与冻结计划不符")
    for number, row in enumerate(plan["rows_fen"], 1):
        for column, value in enumerate(row):
            actual = sheet.cell_value(number, column)
            if (
                actual != value
                if column in {0, 1, 2, 3, 29}
                else Decimal(str(actual)) * 100 != value
            ):
                raise KernelError("export_verification_failed", "BIFF8内容与冻结金额或身份不符")
    book.release_resources()


def _render_tax_plan(target, job, plan):
    def existing():
        try:
            manifest = json.loads((target / "个税导入核对.json").read_text(encoding="utf-8"))
            raw = (target / "正常工资薪金收入.xls").read_bytes()
        except (OSError, ValueError) as exc:
            raise KernelError("export_target_conflict", "导入目标目录已有其他内容") from exc
        checksum = hashlib.sha256(raw).hexdigest()
        if manifest != {"job_id": job, "plan": plan, "sha256": checksum}:
            raise KernelError("export_target_conflict", "已存在文件与冻结工资导入计划不符")
        _verify_tax_plan(raw, plan)
        return {
            "path": str(target / "正常工资薪金收入.xls"),
            "sha256": checksum,
            "row_count": plan["row_count"],
            "notice": "文件用于个税客户端导入；生成文件不代表实际申报或税款支付。",
        }

    if target.exists():
        return existing()
    rows = [
        [
            value if index in {0, 1, 2, 3, 29} else Decimal(value) / 100
            for index, value in enumerate(row)
        ]
        for row in plan["rows_fen"]
    ]
    raw = build_payroll_tax_import_xls(rows)
    _verify_tax_plan(raw, plan)
    target.parent.mkdir(parents=True, exist_ok=True)
    manifest = {"job_id": job, "plan": plan, "sha256": hashlib.sha256(raw).hexdigest()}
    with tempfile.TemporaryDirectory(prefix=".tax-import-", dir=target.parent) as temporary:
        staging = Path(temporary) / "batch"
        staging.mkdir()
        for name, content in (
            ("正常工资薪金收入.xls", raw),
            ("个税导入核对.json", canonical(manifest).encode()),
        ):
            with (staging / name).open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        if target.exists():
            return existing()
        staging.rename(target)
    return existing()


def run_tax_import_jobs(engine, *, limit: int = 10, fault=None):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("job limit must be 1..100")
    outcomes, attempted = [], []
    fault = fault or (lambda stage, job: None)
    with _worker_lock(engine.store.path) as acquired:
        if not acquired:
            return outcomes
        for _ in range(limit):
            with engine.store.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                exclude = f"AND id NOT IN ({','.join('?' for _ in attempted)})" if attempted else ""
                row = connection.execute(
                    "SELECT id,payload FROM jobs WHERE kind='tax_import' "
                    "AND status IN ('pending','running','failed') "
                    f"AND attempts<3 {exclude} ORDER BY attempts,id LIMIT 1",
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
                expected = digest(
                    {key: value for key, value in plan.items() if key not in {"digest", "status"}}
                ).hex()
                if (
                    plan["digest"] != expected
                    or plan["company_id"] != engine.store.company_id
                    or plan["database_id"] != engine.store.database_id
                ):
                    raise KernelError(
                        "invalid_export_plan", "冻结个税导入计划不属于当前公司或摘要不符"
                    )
                fault("before_files", row["id"])
                result = _render_tax_plan(Path(payload["output_directory"]), row["id"], plan)
                fault("files_published", row["id"])
                status, error = "succeeded", None
            except Exception as exc:
                result, status, error = None, "failed", f"{type(exc).__name__}: {exc}"[:500]
            with engine.store.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE jobs SET status=?,result=?,last_error=? WHERE id=?",
                    (status, canonical(result) if result else None, error, row["id"]),
                )
                connection.commit()
            outcomes.append(
                {"job_id": row["id"], "status": status, "result": result, "error": error}
            )
    return outcomes


_TEMPLATE_SHEET_NAME = "正常工资薪金收入"


_INSTRUCTIONS_SHEET_NAME = "填表说明"


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
