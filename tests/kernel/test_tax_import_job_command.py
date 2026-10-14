"""The public job command delivers existing, frozen tax-import tasks."""

import hashlib
import json
from pathlib import Path

import pytest
import xlrd
from test_tax_import import company, complete_details

from ai_accounting.kernel.backup import create_portable
from ai_accounting.kernel.service import LocalService


@pytest.fixture
def command_company(tmp_path):
    source = company(tmp_path, publish=False)
    complete_details(source)
    archive = create_portable(source.engine.store.path, tmp_path / "synthetic-backup")
    app = LocalService(tmp_path / "isolated-service")
    try:
        app.security.provision("owner", "test-password-123")
        token = app.security.login("owner", "test-password-123").session_token
        restored = app.dispatch(
            "restore_company",
            {
                "archive": archive["path"],
                "taxpayer_id": "91310000123456789A",
                "name": "合成个税文件测试公司",
            },
            session_token=token,
        )
        yield app, restored["id"], token
    finally:
        app.close()


def assert_delivery(item, frozen_plan):
    assert item["status"] == "succeeded", item
    assert item["error_code"] is None
    result = item["result"]
    path = Path(result["path"])
    assert path.name == "正常工资薪金收入.xls"
    raw = path.read_bytes()
    checksum = hashlib.sha256(raw).hexdigest()
    assert result["sha256"] == checksum
    assert result["row_count"] == 1
    manifest = json.loads((path.parent / "个税导入核对.json").read_text(encoding="utf-8"))
    assert manifest == {
        "job_id": item["job_id"],
        "plan": frozen_plan,
        "sha256": checksum,
    }
    book = xlrd.open_workbook(file_contents=raw)
    try:
        assert book.sheet_names() == ["正常工资薪金收入", "填表说明"]
        sheet = book.sheet_by_index(0)
        assert sheet.nrows == 2 and sheet.ncols == 30
        assert sheet.cell_value(1, 0) == "00007"
        assert sheet.cell_value(1, 1) == "测试员工"
        assert sheet.cell_value(1, 3) == "001234567890123456"
        assert sheet.cell_value(1, 4) == 10_000
        assert sheet.cell_value(1, 5) == 0
        assert sheet.cell_value(1, 6) == 800
    finally:
        book.release_resources()
    return raw


@pytest.mark.parametrize("limit", [None, 1, 2], ids=["default", "one", "two"])
def test_run_jobs_delivers_tax_import_files_and_preserves_other_results(command_company, limit):
    app, company_id, token = command_company
    engine = app.engine(company_id)
    common = {"company_id": company_id}
    plan = app.dispatch(
        "preview_tax_import", common | {"period": "2026-01"}, session_token=token
    )
    assert plan["status"] == "ready", plan["fact_issues"]
    tasks = [
        app.dispatch(
            "confirm_tax_import",
            common | {
                "period": "2026-01",
                "preview_digest": plan["digest"],
                "request_id": f"public-tax-task-{number}",
            },
            session_token=token,
        )
        for number in range(2)
    ]
    task_ids = {task["job_id"] for task in tasks}
    assert len(task_ids) == 2
    with engine.store.connection(read_only=True) as connection:
        payloads = {
            row["id"]: json.loads(row["payload"])
            for row in connection.execute("SELECT id,payload FROM jobs WHERE kind='tax_import'")
        }
        epochs = engine.store.epochs(connection)
    assert set(payloads) == task_ids
    assert all(not Path(payload["output_directory"]).exists() for payload in payloads.values())

    result = app.dispatch(
        "run_jobs", common | ({} if limit is None else {"limit": limit}), session_token=token
    )
    assert set(result) == {"backups", "exports", "reports", "tax_imports", "dashboard", "replay"}
    assert result["backups"] == result["exports"] == result["reports"] == result["replay"] == []
    assert isinstance(result["dashboard"], list)

    expected_count = 1 if limit is None else limit
    assert len(result["tax_imports"]) == expected_count
    delivered = {
        item["job_id"]: assert_delivery(item, payloads[item["job_id"]]["plan"])
        for item in result["tax_imports"]
    }
    assert len(delivered) == expected_count
    pending = task_ids - delivered.keys()
    for job_id in task_ids:
        job = app.dispatch("jobs", common | {"job_id": job_id}, session_token=token)[0]
        assert job["status"] == ("pending" if job_id in pending else "succeeded")
        assert job["attempts"] == (0 if job_id in pending else 1)
        assert Path(payloads[job_id]["output_directory"]).exists() == (job_id not in pending)

    followup = app.dispatch("run_jobs", common | {"limit": 1}, session_token=token)
    assert len(followup["tax_imports"]) == len(pending)
    assert {item["job_id"] for item in followup["tax_imports"]} == pending
    assert followup["backups"] == []
    for item in followup["tax_imports"]:
        delivered[item["job_id"]] = assert_delivery(item, payloads[item["job_id"]]["plan"])
    assert set(delivered) == task_ids
    for job_id, raw in delivered.items():
        job = app.dispatch("jobs", common | {"job_id": job_id}, session_token=token)[0]
        assert job["status"] == "succeeded" and job["attempts"] == 1
        assert Path(job["result"]["path"]).read_bytes() == raw
    assert app.dispatch("run_jobs", common | {"limit": 1}, session_token=token)["tax_imports"] == []
    with engine.store.connection(read_only=True) as connection:
        assert engine.store.epochs(connection) == epochs
        assert {
            row["id"]: json.loads(row["payload"])
            for row in connection.execute("SELECT id,payload FROM jobs WHERE kind='tax_import'")
        } == payloads
