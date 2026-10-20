"""Real host plans, file tasks and frozen dashboard export endpoints."""

import io
import sqlite3
import zipfile
from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from openpyxl import load_workbook

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.host.public import create_kernel
from ai_accounting_modular.identity.public import IdentityContext
from ai_accounting_modular.reading.public import PeriodRange, ReadingImpact, create_report_producer
from ai_accounting_modular.storage.public import CompanySpec, OwnerProfile, create_catalog


@dataclass
class ReportCase:
    kernel: object
    company: object
    expense: dict
    party: str

    def apply(self, *, amount=None, period=None):
        fact = deepcopy(self.expense)
        if amount is not None:
            fact["data"]["amount_fen"] = amount
            fact["data"]["report_details"][0]["amount_fen"] = amount
        request = {
            "contract": "ai-accounting-modular/1",
            "company_id": self.company.company_id,
            "command_id": str(uuid4()),
            "mutations": [{"op": "replace", "expected_seq": 1, "fact": fact}],
        }
        if period:
            request["correction_period"] = period
        result = self.kernel.application.accounting_apply(request)
        assert result["completion"] == "complete", result
        return result

    def preview(self):
        query = {"company_id": self.company.company_id, "year": "2026", "quarter": "1"}
        with self.kernel.dashboard.get("/api/dashboard/quarterly-report", query) as result:
            if result.body["state"] == "preparing":
                self.kernel.reading.run_pending(self.company.company_id)
        with self.kernel.dashboard.get("/api/dashboard/quarterly-report", query) as result:
            assert result.status == 200 and result.body["state"] == "ready", result.body
            return result.body["response"]

    def request(self, preview=None, request_id=None):
        preview = preview or self.preview()
        return {
            "company_id": self.company.company_id,
            "year": 2026,
            "quarter": 1,
            "preview_digest": preview["export"]["preview_digest"],
            "epochs": preview["export"]["epochs"],
            "request_id": request_id or str(uuid4()),
        }

    def post(self, body):
        return self.kernel.dashboard.post("/api/local/report-export", body)

    def status(self, job_id):
        with self.kernel.dashboard.get(
            f"/api/local/report-export/{job_id}/status", {"company_id": self.company.company_id}
        ) as response:
            return response


@pytest.fixture
def report_case(tmp_path):
    catalog = tmp_path / "catalog.sqlite3"
    create_catalog(catalog, OwnerProfile(str(uuid4()), "合成负责人", "synthetic-hash"))
    kernel = create_kernel(catalog)
    company = kernel.storage.create_company(
        CompanySpec(
            str(uuid4()),
            str(uuid4()),
            "913100000000000001",
            "真实导出合成公司",
            "2026-01",
            tmp_path / "company",
        )
    )
    party, source_id = str(uuid4()), str(uuid4())
    fact = {
        "business_id": str(uuid4()),
        "kind": "operations.expense",
        "period": "2026-01",
        "evidence_ids": [source_id],
        "data": {
            "creditor_id": party,
            "creditor_kind": "supplier",
            "amount_fen": "10000",
            "expense_class": "administration",
            "report_details": [{"code": "management_other", "amount_fen": "10000"}],
        },
    }
    text = "合成验收：新设期初零，行政维修费100元尚未支付。"
    result = kernel.application.accounting_apply(
        {
            "contract": "ai-accounting-modular/1",
            "company_id": company.company_id,
            "command_id": str(uuid4()),
            "objects": [
                {
                    "object_id": party,
                    "kind": "organization",
                    "name": "合成供应商",
                    "expected_seq": 0,
                    "data": {},
                }
            ],
            "sources": [
                {
                    "source_id": source_id,
                    "kind": "owner_statement",
                    "text": text,
                    "sha256": sha256(text.encode()).hexdigest(),
                }
            ],
            "mutations": [
                {"op": "create", "expected_seq": 0, "fact": item}
                for item in (
                    {
                        "business_id": str(uuid4()),
                        "kind": "opening.package",
                        "period": "2026-01",
                        "evidence_ids": [source_id],
                        "data": {"basis": "newly_established", "members": []},
                    },
                    fact,
                )
            ],
        }
    )
    assert result["completion"] == "complete", result
    try:
        yield ReportCase(kernel, company, fact, party)
    finally:
        kernel.close()


def test_real_preview_task_download_uses_exact_seen_formal_amounts(report_case):
    case = report_case
    preview = case.preview()
    assert preview["export"]["available"] and preview["export"]["epochs"]["management"] == 1
    assert preview["export"]["epochs"]["material"] == 0
    with case.kernel.storage.read_unit(case.company.company_id) as read:
        before = read.head
    request = case.request(preview)
    receipt = case.post(request)
    assert receipt.status == 200 and receipt.body["status"] == "queued", receipt.body
    job = receipt.body["job_id"]
    assert case.post(request).body == receipt.body
    assert case.post(case.request(preview)).body == receipt.body
    assert case.status(job).body["status"] == "pending"
    path = f"/api/local/report-export/{job}/download"
    with case.kernel.dashboard.get(path, {"company_id": case.company.company_id}) as response:
        assert response.status == 409 and response.body["code"] == "report_job_not_ready"
    done = case.kernel.files.run(case.company.company_id, job)
    assert done["state"] == "succeeded", done
    assert case.status(job).body["status"] == "succeeded"
    with case.kernel.dashboard.get(path, {"company_id": case.company.company_id}) as response:
        assert response.status == 200
        assert response.headers["Content-Type"].endswith("spreadsheetml.sheet")
        assert "filename*=UTF-8''" in response.headers["Content-Disposition"]
        raw = response.body.read()
        assert sha256(raw).hexdigest() == done["sha256"] == response.headers["X-Content-SHA256"]
    book = load_workbook(io.BytesIO(raw), data_only=True)
    try:
        assert book.worksheets[0]["H3"].value == "真实导出合成公司"
        assert book.worksheets[0]["D37"].value == 0
        assert book.worksheets[1]["D19"].value == 100
        assert book.worksheets[1]["D37"].value == -100
        assert book.worksheets[2]["D30"].value == 0
    finally:
        book.close()
    with case.kernel.storage.read_unit(case.company.company_id) as read:
        assert read.head == before


def test_related_change_expires_status_and_download_then_replaces_only_current_task(report_case):
    case = report_case
    old = case.preview()
    request = case.request(old)
    receipt = case.post(request)
    job = receipt.body["job_id"]
    done = case.kernel.files.run(case.company.company_id, job)
    assert done["state"] == "succeeded", done
    case.apply(amount="12000")
    assert case.status(job).status == 409 and case.status(job).body["code"] == "preview_expired"
    assert case.post(request).body["code"] == "preview_expired"
    with case.kernel.dashboard.get(
        f"/api/local/report-export/{job}/download", {"company_id": case.company.company_id}
    ) as response:
        assert response.status == 409 and response.body["code"] == "preview_expired"
    fresh = case.post(case.request())
    assert fresh.status == 200 and fresh.body["job_id"] != job, fresh.body
    assert case.status(job).status == 404
    assert case.status(fresh.body["job_id"]).body["status"] == "pending"


def test_same_content_new_ready_head_keeps_accepted_file_but_old_preview_epochs_refresh(
    report_case,
):
    case = report_case
    before = case.preview()
    old_request = case.request(before)
    job = case.post(old_request).body["job_id"]
    assert case.kernel.files.run(case.company.company_id, job)["state"] == "succeeded"
    with case.kernel.storage.write_unit(case.company.company_id) as unit:
        current = case.kernel.identity.get_batch([case.party], unit)[0]
        updated = {key: current[key] for key in ("object_id", "kind", "data")}
        updated.update(name="供应商新显示名称", expected_seq=current["seq"])
        case.kernel.identity.apply_current(
            case.kernel.identity.plan_change([updated], IdentityContext(unit, str(uuid4()))), unit
        )
        case.kernel.reading.mark_targets(
            ReadingImpact(
                {
                    "families": [],
                    "business_ids": [],
                    "object_ids": [case.party],
                    "periods": [],
                    "slots": [],
                },
                posted_ranges=(PeriodRange("2026-01", "2026-01"),),
            ),
            unit,
        )
    refreshed = case.preview()
    assert refreshed["export"]["epochs"]["accounting"] > before["export"]["epochs"]["accounting"]
    assert refreshed["export"]["preview_digest"] == before["export"]["preview_digest"]
    assert case.status(job).body["status"] == "succeeded"
    assert case.post(old_request).body["code"] == "preview_expired"
    assert case.post(case.request(refreshed)).body["job_id"] == job


def test_frozen_quarter_survives_later_correction_and_work_horizon(report_case):
    case = report_case
    request = {"company_id": case.company.company_id, "action": "check", "period": "2026-03"}
    checked = case.kernel.application.accounting_period(request)
    assert checked["ready"], checked
    closed = case.kernel.application.accounting_period(
        request | {"action": "close", "expected_head": checked["head"]}
    )
    assert closed["state"] == "closed", closed
    before = case.preview()
    job = case.post(case.request(before)).body["job_id"]
    assert case.kernel.files.run(case.company.company_id, job)["state"] == "succeeded"
    case.apply(amount="8000", period="2026-04")
    after = case.preview()
    assert after["export"] == before["export"]
    assert case.status(job).body["status"] == "succeeded"
    with case.kernel.reading.report_download(case.company.company_id, job) as download:
        assert download.stream.read(2) == b"PK"


def test_company_display_uses_actual_profile_version_and_expires_old_delivery(report_case):
    case = report_case
    before = case.preview()
    job = case.post(case.request(before)).body["job_id"]
    assert case.kernel.files.run(case.company.company_id, job)["state"] == "succeeded"
    # Model a storage-owned profile update without inventing a product editing API.
    with closing(sqlite3.connect(case.company.database_path)) as connection:
        connection.execute(
            "UPDATE company_profile SET legal_name=?,profile_seq=2", ("公司更正名称",)
        )
        connection.commit()
    after = case.preview()
    assert after["organization"]["name"] == "公司更正名称"
    assert after["export"]["epochs"]["management"] == 2
    assert after["export"]["preview_digest"] != before["export"]["preview_digest"]
    assert case.status(job).body["code"] == "preview_expired"


@pytest.mark.parametrize(
    "field,value",
    [
        ("year", True),
        ("year", 0),
        ("quarter", "1"),
        ("quarter", 5),
        ("preview_digest", "wrong"),
        ("epochs", {"accounting": 0}),
        ("request_id", ""),
        ("extra", 1),
    ],
)
def test_invalid_export_body_never_creates_a_task(report_case, field, value):
    case = report_case
    request = case.request()
    request[field] = value
    response = case.post(request)
    assert response.status == 400 and response.body["code"] == "invalid_command", response.body


def test_unknown_foreign_company_and_wrong_task_kind_do_not_download(report_case, tmp_path):
    case = report_case
    job = case.post(case.request()).body["job_id"]
    other = case.kernel.storage.create_company(
        CompanySpec(
            str(uuid4()),
            str(uuid4()),
            "913100000000000002",
            "另一合成公司",
            "2026-01",
            tmp_path / "other",
        )
    )
    with case.kernel.dashboard.get(
        f"/api/local/report-export/{job}/status", {"company_id": other.company_id}
    ) as result:
        assert result.status == 404 and result.body["code"] == "unknown_report_job"
    pending = case.kernel.application.accounting_backup(
        {"company_id": case.company.company_id, "expected_database_id": case.company.database_id}
    )
    assert case.status(pending["task_id"]).body["code"] == "unknown_report_job"
    with pytest.raises(ContractError) as error:
        with case.kernel.reading.report_download(case.company.company_id, pending["task_id"]):
            pytest.fail("backup delivered through report endpoint")
    assert error.value.code == "unknown_report_job"


def test_damaged_download_can_be_regenerated_by_a_new_explicit_request(report_case):
    case = report_case
    first = case.post(case.request()).body["job_id"]
    done = case.kernel.files.run(case.company.company_id, first)
    Path(done["path"]).write_bytes(b"damaged report, not a workbook")
    with case.kernel.dashboard.get(
        f"/api/local/report-export/{first}/download", {"company_id": case.company.company_id}
    ) as response:
        assert response.status == 409 and response.body["code"] == "report_download_invalid"
    second = case.post(case.request())
    assert second.status == 200 and second.body["job_id"] != first, second.body
    assert (
        case.kernel.files.run(case.company.company_id, second.body["job_id"])["state"]
        == "succeeded"
    )
    with case.kernel.reading.report_download(
        case.company.company_id, second.body["job_id"]
    ) as result:
        assert result.stream.read(2) == b"PK"


@pytest.mark.parametrize("stage", ["produce", "download"])
def test_related_change_during_file_work_never_delivers_other_preview_numbers(
    report_case, monkeypatch, stage
):
    case = report_case
    job = case.post(case.request()).body["job_id"]
    producer_type = type(create_report_producer(None))
    if stage == "download":
        assert case.kernel.files.run(case.company.company_id, job)["state"] == "succeeded"
    name = "produce" if stage == "produce" else "verify"
    original = getattr(producer_type, name)
    changed = False

    def changing(producer, *args):
        nonlocal changed
        result = original(producer, *args)
        if not changed:
            changed = True
            case.apply(amount="12000")
        return result

    monkeypatch.setattr(producer_type, name, changing)
    if stage == "produce":
        result = case.kernel.files.run(case.company.company_id, job)
        assert result["state"] == "expired" and "path" not in result
    else:
        with case.kernel.dashboard.get(
            f"/api/local/report-export/{job}/download", {"company_id": case.company.company_id}
        ) as response:
            assert response.status == 409 and response.body["code"] == "preview_expired"
    assert changed


def test_xml_unrepresentable_profile_disables_export_without_failing_the_valid_financial_model(
    report_case,
):
    case = report_case
    case.preview()
    text = "公司\x01名称"
    with closing(sqlite3.connect(case.company.database_path)) as connection:
        connection.execute("UPDATE company_profile SET legal_name=?,profile_seq=2", (text,))
        connection.commit()
    current = case.preview()
    assert current["organization"]["name"] == text
    assert current["status"] == "ready"
    assert current["export"]["available"] is False
    assert "Excel不支持的字符" in current["message"]


def test_real_backup_download_repair_preserves_previous_and_original_request_failure(report_case):
    case = report_case
    service, company = case.kernel.files, case.company

    def backup(request_id):
        with case.kernel.application.capture_backup_snapshot(
            company.company_id, company.database_id
        ) as plan:
            saved = deepcopy(plan)
            task = service.ensure_current(plan, plan["scope_head"], request_id)
        done = service.run(company.company_id, task["task_id"])
        assert done["state"] == "succeeded", done
        return saved, done

    _, first = backup("backup-A")
    raw_a = Path(first["path"]).read_bytes()
    case.apply(amount="12000")
    plan_b, second = backup("backup-B")
    previous = company.backup_directory / (company.credit_code + ".previous.finance-company.zip")
    assert previous.read_bytes() == raw_a
    current = Path(second["path"])
    with zipfile.ZipFile(current) as package:
        entries = {item.filename: package.read(item.filename) for item in package.infolist()}
    entries["standard/README.md"] = b"wrong contents inside a clearly owned company package"
    damaged = io.BytesIO()
    with zipfile.ZipFile(damaged, "w") as package:
        for name, raw in entries.items():
            package.writestr(name, raw)
    current.write_bytes(damaged.getvalue())
    with pytest.raises(ContractError) as error:
        with service.verified_download(company.company_id, second["task_id"]):
            pytest.fail("damaged backup delivered")
    assert error.value.code == "file_invalid"
    failed = service.get(company.company_id, second["task_id"])
    assert failed["state"] == "failed" and failed["attempts"] == second["attempts"]
    assert service.ensure_current(plan_b, plan_b["scope_head"], "backup-B") == failed
    assert current.exists() and previous.read_bytes() == raw_a
    plan_c, repaired = backup("backup-C")
    assert plan_c["scope_head"] == plan_b["scope_head"]
    assert repaired["task_id"] != second["task_id"]
    assert previous.read_bytes() == raw_a
    with service.verified_download(company.company_id, repaired["task_id"]) as downloaded:
        assert downloaded.stream.read(2) == b"PK"
