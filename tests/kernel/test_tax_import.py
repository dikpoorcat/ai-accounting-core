from pathlib import Path

import pytest
import xlrd
from test_payroll import payroll
from test_payroll_preparation import company

from ai_accounting.kernel import tax_import
from ai_accounting.kernel.contracts import KernelError


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

    assert tax_import.run_tax_import_jobs(instance.engine, fault=crash)[0]["status"] == "failed"
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
    assert "BIFF8内容" in result["error"]
