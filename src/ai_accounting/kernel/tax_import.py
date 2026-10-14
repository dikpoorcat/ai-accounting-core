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
from typing import Any, ClassVar

import xlwt
from pydantic import Field, model_validator

from .backup import _worker_lock
from .contracts import Context, Fact, KernelError, NeedsInformation, Read
from .domains.payroll import (
    PAYROLL_KINDS,
    employee_month,
    payroll_contribution_reads,
    prepare_payroll_contributions,
)
from .export_directories import (
    ExportParentDirectory,
    publish_export_directory,
    resolve_export_parent,
    validate_export_parent,
)
from .payroll_confirmation import FactRevisionReference, revision_reference
from .reporting_fields import payroll_import_fields
from .types import NonNegativeFen, YearMonth, canonical, digest, sum_fen
from .workflow import _payroll_population_issues

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
    period: YearMonth = Field(
        description="负责人明确的工资薪金导入文件所属期；独立于所引用工资核算月，不从税额依据推定",
        json_schema_extra={
            "x-accounting-fact": {
                "role": "management",
                "meaning": "confirmed_tax_import_filing_period",
                "allowed_precision": ["month"],
                "reusable_sources": ["confirmed_tax_import_material"],
            }
        },
    )
    employee_id: str = Field(min_length=1)
    payroll_revision: FactRevisionReference
    include_in_filing: bool = Field(description="有来源确认本期是否纳入正常工资薪金导入文件")
    tax_reported_salary_fen: NonNegativeFen
    tax_exempt_income_fen: NonNegativeFen
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
        if self.tax_exempt_income_fen > self.tax_reported_salary_fen:
            raise ValueError("exempt income cannot exceed reported income")
        if (
            self.current_other_fen["current_personal_pension"]
            > self.cumulative_personal_pension_fen
        ):
            raise ValueError(
                "current personal pension cannot exceed its supplied cumulative amount"
            )
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
    from .payroll_history import HistoricalTaxImportDetails

    registry.register(TaxImportIdentity)
    registry.register(
        TaxImportDetails,
        table="fact_tax_import_details_v2_confirmed",
        historical=((HistoricalTaxImportDetails, "fact_tax_import_details_v2"),),
    )
    registry.register(TaxImportMapping)


def _content_error(message, **details):
    raise KernelError("content_integrity_failed", message, component="tax_import", **details)


def _prepared_wages(store, connection, period, *, reads=None):
    """Prepare current, owner-confirmed inputs without requiring a wage tax basis."""
    selections = [
        Read("fact", "payroll_profile", "*"),
        Read("fact", TaxImportMapping.kind, str(period)),
        Read("fact", TaxImportDetails.kind, str(period)),
    ]
    if reads is not None:
        if (
            reads.connection is not connection
            or reads.store is not store
            or not reads._snapshot_active
        ):
            raise ValueError("tax import reads belong to another active snapshot")
        reads.prime_select(selections)
        selected = {read: reads.select(read) for read in selections}
    else:
        selected = store.select_many(connection, selections)
    details = selected[Read("fact", TaxImportDetails.kind, str(period))]
    referenced_subjects = {
        item.fact.payroll_revision.subject_id
        for item in details
        if isinstance(item.fact, TaxImportDetails)
    }
    # Header periods only bound the batched read. The typed versions below still
    # prove the exact current employee/revision before any row can enter a file.
    source_periods = {
        YearMonth.from_ordinal(row[0])
        for row in connection.execute(
            "SELECT f.period FROM json_each(?) ids "
            "JOIN fact_current c ON c.subject_id=ids.value "
            "JOIN subject s ON s.id=c.subject_id "
            "JOIN fact_revision f ON f.id=c.fact_id "
            "WHERE s.kind IN (SELECT value FROM json_each(?))",
            (json.dumps(sorted(referenced_subjects)), json.dumps(PAYROLL_KINDS)),
        )
    } or {period}
    wage_reads = tuple(
        Read("fact", kind, str(source_period))
        for source_period in sorted(source_periods)
        for kind in PAYROLL_KINDS
    )
    if reads is not None:
        reads.prime_select(wage_reads)
        selected.update({read: reads.select(read) for read in wage_reads})
    else:
        selected.update(store.select_many(connection, wage_reads))
    facts = [item for read in wage_reads for item in selected[read]]
    profiles = selected[Read("fact", "payroll_profile", "*")]
    issues = [
        _assessment_issue(
            "tax_import_payroll_pending",
            "publication",
            item["field"],
            item["message"],
            employee_id=item["employee_id"],
            blocking_scope="tax_import_file",
            cause=dict(item),
        )
        for source_period in sorted(source_periods)
        for item in _payroll_population_issues(profiles, facts, source_period, source_period)
    ]
    mappings = selected[Read("fact", TaxImportMapping.kind, str(period))]
    available_subjects = {item.subject_id for item in facts}
    issues.extend(
        _assessment_issue(
            "tax_import_details_expired",
            "management_fact",
            "payroll_revision",
            "导入范围引用的当前工资来源不存在或不是工资事实",
            employee_id=item.fact.employee_id,
        )
        for item in details
        if isinstance(item.fact, TaxImportDetails)
        and item.fact.payroll_revision.subject_id not in available_subjects
    )
    details_by_employee = {}
    for detail in details:
        details_by_employee.setdefault(detail.fact.employee_id, []).append(detail)
    eligible = []
    for version in facts:
        matching = details_by_employee.get(version.fact.employee_id, ())
        if (
            len(matching) == 1
            and isinstance(matching[0].fact, TaxImportDetails)
            and (
                matching[0].fact.payroll_revision == revision_reference(version)
                and matching[0].evidence
            )
        ):
            eligible.append(version)
    dependency_reads = tuple(
        dict.fromkeys(
            read
            for version in eligible
            for read in payroll_contribution_reads(version.fact, version.subject_id)
        )
    )
    if reads is not None:
        reads.prime_select(dependency_reads)
        dependencies = {read: reads.select(read) for read in dependency_reads}
    else:
        dependencies = store.select_many(connection, dependency_reads)
    prepared, excluded, sources = [], [], [item.id for item in mappings]
    for version in sorted(facts, key=lambda item: (item.fact.employee_id, item.subject_id)):
        employee = version.fact.employee_id
        matching = details_by_employee.get(employee, ())
        if len(matching) != 1 or not isinstance(matching[0].fact, TaxImportDetails):
            issues.append(
                _assessment_issue(
                    "tax_import_details_required",
                    "management_fact",
                    "tax_import_details",
                    "需要文件所属期明确的工资税导入范围和原始资料明细",
                    employee_id=employee,
                )
            )
            continue
        detail = matching[0]
        if detail.fact.payroll_revision != revision_reference(version) or not detail.evidence:
            issues.append(
                _assessment_issue(
                    "tax_import_details_expired",
                    "management_fact",
                    "tax_import_details",
                    "导入资料需要依据并精确引用当前工资版本",
                    employee_id=employee,
                )
            )
            continue
        try:
            basis = prepare_payroll_contributions(version, Context(dependencies))
        except NeedsInformation as exc:
            issues.extend(
                _assessment_issue(
                    "tax_import_payroll_pending",
                    "publication",
                    issue["field"],
                    issue["message"],
                    employee_id=employee,
                    blocking_scope="tax_import_file",
                    cause=issue,
                )
                for issue in exc.issues_with_context()
            )
            continue
        except KernelError as exc:
            if exc.code not in {"ambiguous_source", "payroll_profile_mismatch"}:
                raise
            issues.append(
                _assessment_issue(
                    "tax_import_payroll_pending",
                    "publication",
                    "payroll_confirmation",
                    str(exc),
                    employee_id=employee,
                )
            )
            continue
        exact_sources = [
            version.id,
            detail.id,
            basis["profile_version"].id,
            basis["contribution_policy_version"].id,
            basis["confirmation"]["confirmation_fact_id"],
            *basis["confirmation"].get("notice_fact_ids", ()),
        ]
        if basis["actual"] is not None:
            exact_sources.append(basis["actual"].id)
        if "prior_payroll_fact_id" in basis["confirmation"]:
            prior_fact_id = basis["confirmation"]["prior_payroll_fact_id"]
            exact_sources.append(prior_fact_id)
            exact_sources.extend(
                item.id
                for read, items in dependencies.items()
                if read.source == "calculation"
                for item in items
                if item.fact_id == prior_fact_id
            )
        sources.extend(exact_sources)
        if not detail.fact.include_in_filing:
            excluded.append(
                {
                    "employee_id": employee,
                    "fact_id": version.id,
                    "details_fact_id": detail.id,
                    "reason": "explicitly_excluded_from_filing",
                }
            )
            continue
        prepared.append((version, detail, basis))
    return facts, prepared, excluded, mappings, issues, sources


def _assessment_issue(code, category, field, message, **details):
    return {
        "code": code,
        "category": category,
        "field": field,
        "message": message,
        **details,
    }


def assess_tax_import_mapping(store, connection, period: YearMonth, *, reads=None, _prepared=None):
    """Assess fixed template columns from confirmed inputs in the active snapshot."""
    month = YearMonth(str(period))
    facts, prepared, excluded, mappings, issues, _sources = (
        _prepared
        if _prepared is not None
        else _prepared_wages(store, connection, month, reads=reads)
    )
    issues = [dict(item) for item in issues]
    result = {
        "status": "ready",
        "blocking_scope": "tax_import_file",
        "mapping_fact_ids": sorted(item.id for item in mappings),
        "calculation_ids": [],
        "issues": issues,
    }
    if not facts:
        return {**result, "status": "pending_confirmation"}
    if len(excluded) == len(facts) and not issues:
        return {**result, "status": "not_applicable"}
    nonzero_by_code = {}
    for _version, _detail, basis in prepared:
        for code, amount in basis["burden"].employee_social_insurance_items.items():
            if amount:
                nonzero_by_code[code] = sum_fen((nonzero_by_code.get(code, 0), amount))
    if len(nonzero_by_code) > 3:
        issues.append(
            _assessment_issue(
                "tax_import_format_unsupported",
                "capability",
                "tax_import_mapping",
                "本期实际非零个人社保扣款超过个税模板的三个固定险种列",
                component_codes=sorted(nonzero_by_code),
                amount_fen=sum_fen(nonzero_by_code.values()),
            )
        )
        return {**result, "status": "unsupported"}
    if issues:
        status = (
            "pending_confirmation"
            if any(item["category"] == "publication" for item in issues)
            else "needs_information"
        )
        return {**result, "status": status}
    if len(mappings) != 1:
        issues.append(
            _assessment_issue(
                "tax_import_mapping_required" if not mappings else "tax_import_mapping_ambiguous",
                "management_fact",
                "tax_import_mapping",
                "需要本月唯一的险种与个税列对应关系",
            )
        )
        return {**result, "status": "needs_information"}
    mapping = mappings[0].fact
    codes = (mapping.pension_code, mapping.medical_code, mapping.unemployment_code)
    for version, _detail, basis in prepared:
        social = basis["burden"].employee_social_insurance_items
        missing = sorted(code for code, amount in social.items() if amount and code not in codes)
        absent = sorted(code for code in codes if code is not None and code not in social)
        if missing:
            issues.append(
                _assessment_issue(
                    "tax_import_mapping_incomplete",
                    "management_fact",
                    "tax_import_mapping",
                    "本月险种映射未覆盖实际非零个人社保扣款",
                    employee_id=version.fact.employee_id,
                    component_codes=missing,
                    amount_fen=sum_fen(social[code] for code in missing),
                )
            )
        if absent:
            issues.append(
                _assessment_issue(
                    "tax_import_mapping_policy_mismatch",
                    "management_fact",
                    "tax_import_mapping",
                    "本月险种映射声明的代码不属于该工资采用的社保政策",
                    employee_id=version.fact.employee_id,
                    component_codes=absent,
                )
            )
    return {**result, "status": "needs_information" if issues else "ready"}


def _contributions(basis, mapping):
    burden = basis["burden"]
    social = burden.employee_social_insurance_items
    codes = (mapping.pension_code, mapping.medical_code, mapping.unemployment_code)
    amounts = [social.get(code, 0) for code in codes] + [burden.employee_housing_fund_fen]
    if sum_fen(amounts) != burden.employee_total_fen:
        _content_error("个税模板险种列与已确认工资个人扣款不一致")
    return amounts


class TaxImport:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    def preview(self, period: str):
        month = YearMonth(period)
        with self.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            prepared = _prepared_wages(self.store, connection, month)
            facts, wages, excluded, mappings, _issues, sources = prepared
            mapping_assessment = assess_tax_import_mapping(
                self.store,
                connection,
                month,
                _prepared=prepared,
            )
            issues = [dict(item) for item in mapping_assessment["issues"]]
            rows = []
            if not facts:
                issues.append(
                    {"field": "payroll", "message": "没有已明确的本月工资，不推定空申报文件"}
                )
            for version, detail_version, basis in wages:
                employee = version.fact.employee_id
                identity = self.store.select(
                    connection,
                    Read("fact", TaxImportIdentity.kind, "tax-identity:" + employee),
                )
                if len(identity) != 1 or not identity[0].evidence:
                    issues.append(
                        {
                            "field": "tax_import_identity",
                            "employee_id": employee,
                            "message": "导出需要有依据的身份资料；这些资料不阻断工资入账",
                        }
                    )
                    continue
                if mapping_assessment["status"] != "ready":
                    continue
                detail, person = detail_version.fact, identity[0].fact
                contributions = _contributions(basis, mappings[0].fact)
                shared = payroll_import_fields(detail, contributions)
                rows.append(
                    [
                        person.employee_code,
                        person.name,
                        person.document_type,
                        person.document_number,
                        shared["tax_reported_salary_fen"],
                        shared["tax_exempt_income_fen"],
                        *shared["employee_contributions_fen"],
                        *(shared["cumulative_special_fen"][key] for key in SPECIAL_COLUMNS),
                        shared["cumulative_personal_pension_fen"],
                        *(shared["current_other_fen"][key] for key in OTHER_COLUMNS[1:]),
                        shared["tax_relief_fen"],
                        shared["treaty_relief_fen"],
                        detail.remark,
                    ]
                )
                sources.append(identity[0].id)
            if facts and len(excluded) == len(facts) and not issues:
                issues.append(
                    {
                        "field": "include_in_filing",
                        "message": "本期工资已明确不纳入此导入文件，不生成空申报文件",
                    }
                )
            for issue in issues:
                issue.setdefault("semantics", "export_only")
            if len(rows) > 65_535:
                issues.append(
                    {
                        "field": "rows",
                        "message": "BIFF8模板最多65535条工资记录",
                        "semantics": "export_only",
                    }
                )
            plan = {
                "company_id": self.store.company_id,
                "database_id": self.store.database_id,
                "period": month,
                "rows_fen": rows,
                "row_count": len(rows),
                "source_versions": sorted(set(sources)),
                "excluded_sources": excluded,
                "tax_import_mapping": mapping_assessment,
                "fact_issues": issues,
                "epochs": {
                    key: value
                    for key, value in self.store.epochs(connection).items()
                    if key in {"accounting", "management"}
                },
            }
            return {
                **plan,
                "status": (
                    "unsupported"
                    if mapping_assessment["status"] == "unsupported"
                    else "needs_information"
                    if issues
                    else "ready"
                ),
                "digest": digest(plan).hex(),
            }

    def confirm(
        self,
        period: str,
        *,
        preview_digest: str,
        output_directory: ExportParentDirectory | None = None,
        request_id: str,
    ):
        parent = resolve_export_parent(output_directory, self.store.path)
        directory = str(parent)
        request_hash = digest(["tax_import", period, preview_digest, directory])
        cached = self.engine._cached(request_id, request_hash)
        if cached is not None:
            return cached
        validate_export_parent(parent, output_directory)
        plan = self.preview(period)
        if plan["digest"] != preview_digest:
            raise KernelError("preview_expired", "工资或导入资料已有变化，请重新预览")
        if plan["status"] == "unsupported":
            raise KernelError(
                "tax_import_format_unsupported",
                "本期实际个人社保扣款无法装入个税文件固定列",
                fact_issues=plan["tax_import_mapping"]["issues"],
            )
        if plan["fact_issues"]:
            raise KernelError(
                "needs_information", "个税导入文件尚缺明确资料", fact_issues=plan["fact_issues"]
            )

        def operation(connection):
            job = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO jobs(id,kind,payload,status) VALUES(?,'tax_import',?,'pending')",
                (job, canonical({"plan": plan, "output_directory": str(parent / job)})),
            )
            from .read_indexes import sync_job

            sync_job(connection, job)
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

    if os.path.lexists(target):
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
        if os.path.lexists(target):
            return existing()
        try:
            publish_export_directory(staging, target)
        except OSError:
            if not os.path.lexists(target):
                raise
            return existing()
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
                    "UPDATE jobs SET status='running',attempts=attempts+1,"
                    "last_error=NULL,error_code=NULL "
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
                status, error, error_code = "succeeded", None, None
            except Exception as exc:
                from .diagnostics import job_error_code

                result, status, error = None, "failed", f"{type(exc).__name__}: {exc}"[:500]
                error_code = job_error_code(exc)
            with engine.store.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE jobs SET status=?,result=?,last_error=?,error_code=? WHERE id=?",
                    (status, canonical(result) if result else None, error, error_code, row["id"]),
                )
                connection.commit()
            outcomes.append(
                {"job_id": row["id"], "status": status, "result": result, "error_code": error_code}
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
