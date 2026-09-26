import json
from pathlib import Path

import pytest
import xlrd
from payroll_plan_fixture import confirm_wage_inputs
from test_payroll import contribution_policy, income_tax_policy, opening, payroll, profile
from test_payroll_corrections import Company
from test_payroll_preparation import company

from ai_accounting.kernel import tax_import
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.domains.payroll import ContributionRuleFact, PayrollContributionPolicy
from ai_accounting.kernel.types import YearMonth


def complete_details(instance):
    calculation = instance.current("january")
    details = tax_import.TaxImportDetails(
        period="2026-01",
        employee_id="employee",
        payroll_result_digest=calculation.result_digest,
        cumulative_special_fen={key: 0 for key in tax_import.SPECIAL_COLUMNS},
        current_other_fen={key: 0 for key in tax_import.OTHER_COLUMNS},
        cumulative_personal_pension_fen=0,
        tax_relief_fen=0,
        treaty_relief_fen=0,
    )
    instance.save(details, "details")
    instance.save(
        tax_import.TaxImportIdentity(
            period="2026-01",
            employee_id="employee",
            employee_code="00007",
            name="测试员工",
            document_type="居民身份证",
            document_number="001234567890123456",
        ),
        "identity",
    )
    instance.save(
        tax_import.TaxImportMapping(
            period="2026-01",
            pension_code="pension",
            medical_code=None,
            unemployment_code=None,
        ),
        "mapping",
    )
    return tax_import.TaxImport(instance.engine)


def mapping_assessment(instance):
    with instance.engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        return tax_import.assess_tax_import_mapping(
            instance.engine.store, connection, YearMonth("2026-01")
        )


def replace_contribution_policy(instance, rules):
    source = contribution_policy().model_dump(mode="json")
    source.update(version="four-components", rules=rules)
    policy = PayrollContributionPolicy.model_validate(source)
    instance.save(policy, "four-contributions")
    instance.save(payroll(contribution_policy_id="four-contributions"), "january", revision=1)
    confirm_wage_inputs(
        instance.engine,
        "january",
        evidence=(instance.owner_confirmation,),
        request_id=instance.request(),
    )
    instance.publish("january")


def contribution_rule(code, *, employee_rate="0.01", employer_rate="0.01"):
    return ContributionRuleFact(
        code=code,
        base_kind="social_insurance",
        employee_rate=employee_rate,
        employer_rate=employer_rate,
        minimum_base_fen=0,
        maximum_base_fen=10_000_000,
        rounding="half_up",
        enabled=True,
    )


def test_mapping_assessment_distinguishes_missing_management_fact_from_ready(tmp_path):
    instance = company(tmp_path)
    missing = mapping_assessment(instance)
    assert missing["status"] == "needs_information"
    assert missing["blocking_scope"] == "tax_import_file"
    assert missing["mapping_fact_ids"] == []
    assert len(missing["calculation_ids"]) == 1
    assert missing["issues"] == [
        {
            "code": "tax_import_mapping_required",
            "category": "management_fact",
            "field": "tax_import_mapping",
            "message": "需要本月唯一的险种与个税列对应关系",
        }
    ]

    complete_details(instance)
    ready = mapping_assessment(instance)
    assert ready["status"] == "ready"
    assert len(ready["mapping_fact_ids"]) == 1
    assert ready["issues"] == []


def test_unpublished_wage_is_pending_publication_without_inventing_a_mapping_question(tmp_path):
    instance = Company(tmp_path / "pending.sqlite")
    for fact, subject in (
        (profile(), "profile"),
        (contribution_policy(), "contributions"),
        (income_tax_policy(), "income-tax"),
        (opening(), "opening"),
        (payroll(), "january"),
    ):
        instance.save(fact, subject)
    assessment = mapping_assessment(instance)
    assert assessment["status"] == "pending_publication"
    assert assessment["calculation_ids"] == []
    assert [issue["code"] for issue in assessment["issues"]] == ["tax_import_payroll_pending"]


def test_mapping_assessment_reports_no_wage_as_pending_without_inventing_a_question(tmp_path):
    assessment = mapping_assessment(Company(tmp_path / "empty.sqlite"))
    assert assessment == {
        "status": "pending_publication",
        "blocking_scope": "tax_import_file",
        "mapping_fact_ids": [],
        "calculation_ids": [],
        "issues": [],
    }


def test_four_nonzero_personal_components_are_a_template_capability_error(tmp_path):
    instance = company(tmp_path)
    replace_contribution_policy(
        instance,
        tuple(contribution_rule(code) for code in ("pension", "medical", "unemployment", "injury")),
    )
    service = complete_details(instance)
    assessment = mapping_assessment(instance)
    assert assessment["status"] == "unsupported"
    assert assessment["issues"] == [
        {
            "code": "tax_import_format_unsupported",
            "category": "capability",
            "field": "tax_import_mapping",
            "message": "本期实际非零个人社保扣款超过个税模板的三个固定险种列",
            "component_codes": ["injury", "medical", "pension", "unemployment"],
            "amount_fen": 40_000,
        }
    ]
    preview = service.preview("2026-01")
    assert preview["status"] == "unsupported"
    assert all("semantics" not in issue for issue in preview["tax_import_mapping"]["issues"])
    with pytest.raises(KernelError) as error:
        service.confirm(
            "2026-01",
            preview_digest=preview["digest"],
            output_directory=str(tmp_path / "unsupported"),
            request_id=instance.request(),
        )
    assert error.value.code == "tax_import_format_unsupported"
    assert instance.current("january").values["employee_contributions_fen"] == 40_000


def test_zero_personal_component_does_not_consume_a_template_column(tmp_path):
    instance = company(tmp_path)
    replace_contribution_policy(
        instance,
        (
            contribution_rule("pension"),
            contribution_rule("employer-only", employee_rate="0", employer_rate="0.02"),
            contribution_rule("zero", employee_rate="0", employer_rate="0"),
            contribution_rule("another-zero", employee_rate="0", employer_rate="0"),
        ),
    )
    complete_details(instance)
    assessment = mapping_assessment(instance)
    assert assessment["status"] == "ready"
    assert assessment["issues"] == []


def test_broken_published_contribution_trace_remains_a_kernel_error(tmp_path):
    instance = company(tmp_path)
    complete_details(instance)
    calculation = instance.current("january")
    with instance.engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        triggers = list(
            connection.execute(
                "SELECT name,sql FROM sqlite_schema WHERE type='trigger' AND tbl_name='calculation'"
            )
        )
        for name, _sql in triggers:
            connection.execute(f'DROP TRIGGER "{name}"')
        row = connection.execute(
            "SELECT outcome FROM calculation WHERE id=?", (calculation.id,)
        ).fetchone()
        outcome = json.loads(row[0])
        item = next(
            item
            for item in outcome["explanation"]
            if item["step"] == "contribution_burden_allocation"
        )
        item["values"]["code"] = "corrupted-code"
        connection.execute(
            "UPDATE calculation SET outcome=? WHERE id=?",
            (json.dumps(outcome, ensure_ascii=False, separators=(",", ":")), calculation.id),
        )
        for _name, sql in triggers:
            connection.execute(sql)
        connection.commit()
    with pytest.raises(KernelError) as error:
        mapping_assessment(instance)
    assert error.value.code == "content_integrity_failed"
    assert error.value.details["component"] == "tax_import"


def test_missing_identity_only_blocks_export_and_biff8_preserves_every_column(tmp_path):
    instance = company(tmp_path)
    assert tax_import.TaxImport(instance.engine).preview("2026-01")["status"] == "needs_information"
    original = instance.current("january")
    service = complete_details(instance)
    plan = service.preview("2026-01")
    assert plan["status"] == "ready", plan["fact_issues"]
    assert plan["rows_fen"][0][6] == 80000
    assert len(plan["rows_fen"][0]) == 30
    assert instance.current("january") == original
    saved = service.confirm(
        "2026-01",
        preview_digest=plan["digest"],
        output_directory=str(tmp_path / "export"),
        request_id=instance.request(),
    )
    jobs = tax_import.run_tax_import_jobs(instance.engine)
    assert jobs[0]["job_id"] == saved["job_id"]
    assert jobs[0]["status"] == "succeeded", jobs
    path = Path(jobs[0]["result"]["path"])
    book = xlrd.open_workbook(path)
    assert book.sheet_names() == ["正常工资薪金收入", "填表说明"]
    sheet = book.sheet_by_index(0)
    assert sheet.row_values(0) == list(tax_import._HEADERS)
    assert sheet.cell_value(1, 0) == "00007"
    assert sheet.cell_value(1, 3) == "001234567890123456"
    assert sheet.cell_value(1, 6) == 800
    assert instance.count("voucher") == 1


def test_export_deduction_breakdown_must_match_current_official_wage_result(tmp_path):
    instance = company(tmp_path)
    service = complete_details(instance)
    instance.save(payroll(special_additional_deduction_fen=100000), "january", revision=1)
    instance.confirm_payroll("january")
    instance.publish("january")
    result = service.preview("2026-01")
    assert result["status"] == "needs_information"
    assert any("专项附加" in item["message"] for item in result["fact_issues"])


def test_frozen_tax_export_retries_after_file_publication_without_new_submission(tmp_path):
    instance = company(tmp_path)
    service = complete_details(instance)
    plan = service.preview("2026-01")
    key = instance.request()
    first = service.confirm(
        "2026-01",
        preview_digest=plan["digest"],
        output_directory=str(tmp_path / "export"),
        request_id=key,
    )

    def crash(stage, job):
        if stage == "files_published":
            raise RuntimeError("simulated crash after durable files")

    failed = tax_import.run_tax_import_jobs(instance.engine, fault=crash)[0]
    assert failed["status"] == "failed" and failed["error_code"] == "job_failed"
    assert instance.engine.jobs(job_id=failed["job_id"])[0]["error_code"] == "job_failed"
    retry = tax_import.run_tax_import_jobs(instance.engine)[0]
    assert retry["status"] == "succeeded", retry
    replay = service.confirm(
        "2026-01",
        preview_digest=plan["digest"],
        output_directory=str(tmp_path / "export"),
        request_id=key,
    )
    assert replay == first
    assert instance.count("voucher") == 1
    with instance.engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM subject WHERE kind='external_completion'"
            ).fetchone()[0]
            == 0
        )


def test_stale_tax_preview_cannot_freeze_old_identity(tmp_path):
    instance = company(tmp_path)
    service = complete_details(instance)
    plan = service.preview("2026-01")
    instance.save(
        tax_import.TaxImportIdentity(
            period="2026-01",
            employee_id="employee",
            employee_code="00007",
            name="更正姓名",
            document_type="居民身份证",
            document_number="001234567890123456",
        ),
        "identity",
        revision=1,
    )
    with pytest.raises(KernelError) as error:
        service.confirm(
            "2026-01",
            preview_digest=plan["digest"],
            output_directory=str(tmp_path / "export"),
            request_id=instance.request(),
        )
    assert error.value.code == "preview_expired"
    assert not (tmp_path / "export").exists()


def test_retry_verifies_contents_even_when_file_checksum_manifest_was_changed(tmp_path):
    import hashlib
    import json
    from decimal import Decimal

    instance = company(tmp_path)
    service = complete_details(instance)
    plan = service.preview("2026-01")
    target = tmp_path / "export"
    service.confirm(
        "2026-01",
        preview_digest=plan["digest"],
        output_directory=str(target),
        request_id=instance.request(),
    )

    def crash(stage, job):
        if stage == "files_published":
            raise RuntimeError("simulated crash")

    tax_import.run_tax_import_jobs(instance.engine, fault=crash)
    changed = [
        [
            value if column in {0, 1, 2, 3, 29} else Decimal(value) / 100
            for column, value in enumerate(row)
        ]
        for row in plan["rows_fen"]
    ]
    changed[0][4] += 1
    raw = tax_import.build_payroll_tax_import_xls(changed)
    (target / "正常工资薪金收入.xls").write_bytes(raw)
    manifest_path = target / "个税导入核对.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sha256"] = hashlib.sha256(raw).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = tax_import.run_tax_import_jobs(instance.engine)[0]
    assert result["status"] == "failed"
    assert result["error_code"] == "job_failed"
