"""Portable job completion proof is separate from today's file availability."""

import json
from copy import deepcopy

import pytest
from monthly_close_fixture import ready
from test_workflow import setup_company
from test_worklist import view

from ai_accounting.kernel.backup import portable_result_verified, run_backup_jobs
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.worklist import Worklist


@pytest.fixture(scope="module")
def completed_backup(tmp_path_factory):
    directory = tmp_path_factory.mktemp("portable-job-display")
    company = setup_company(directory)
    ready(company.engine, company.owner_confirmation, first="2026-01", last="2026-01")
    periods = Periods(company.engine)
    preview = periods.preview_close("2026-01", owner_confirmation=company.owner_confirmation)
    periods.close(
        "2026-01",
        owner_confirmation=company.owner_confirmation,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        backup_directory=str(directory / "backup"),
        request_id=company.request(),
    )
    worker = run_backup_jobs(company.engine.store.path)
    assert len(worker) == 1 and worker[0]["status"] == "succeeded"
    with company.engine.store.connection(read_only=True) as connection:
        row = dict(connection.execute("SELECT * FROM jobs WHERE kind='portable_backup'").fetchone())
    return company, row, json.loads(row["result"])


def test_actual_portable_worker_result_is_verified_without_export_sha(
    completed_backup, monkeypatch
):
    company, _, result = completed_backup
    assert "sha256" not in result
    assert result["verification"]["status"] == "verified"

    def no_current_file_check(*args, **kwargs):
        raise AssertionError("Worklist must only inspect the saved completion proof")

    monkeypatch.setattr("ai_accounting.kernel.backup.verify_portable", no_current_file_check)
    jobs = view(company)["sections"]["files"]["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["verified_when_succeeded"] is True
    assert jobs[0]["result_issue"] is None
    assert jobs[0]["current_file_availability"] == "not_checked"


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("path",), ""),
        (("manifest",), None),
        (("manifest", "database_sha256"), "invalid"),
        (("manifest", "database_bytes"), 0),
        (("manifest", "extra"), "unsupported"),
        (("identity", "database_id"), "other-database"),
        (("database_format", "fingerprint"), "0" * 64),
        (("database_format", "version"), False),
        (("latest_closed_period",), "2026-02"),
        (("evidence_count",), True),
        (("verification", "status"), "unverified"),
        (("verification", "extra"), "unsupported"),
        (("verification", "limitations"), ["missing source"]),
        (("verification", "coverage", "read_indexes"), "not_checked"),
        (("verification", "coverage", "extra"), "verified"),
        (("verification", "coverage", "dashboard_read_models"), None),
        (("verification", "coverage", "dashboard_read_models"), {}),
        (("verification", "coverage", "dashboard_read_models"), {"status": "not_applicable"}),
        (("verification", "coverage", "dashboard_read_models", "status"), "unverified"),
        (("verification", "coverage", "dashboard_read_models", "derived_content"), "not_checked"),
        (("verification", "coverage", "dashboard_read_models", "extra"), "unsupported"),
        (("verification", "coverage", "dashboard_read_models", "receipts"), True),
        (("verification", "coverage", "dashboard_read_models", "receipts"), -1),
        (("verification", "coverage", "dashboard_read_models", "receipts"), "7"),
        (("verification", "coverage", "dashboard_read_models", "receipts"), 7.0),
        (("verification", "coverage", "dashboard_read_models", "current_compared"), True),
        (("verification", "coverage", "dashboard_read_models", "current_compared"), -1),
        (("verification", "coverage", "dashboard_read_models", "current_compared"), "1"),
        (("verification", "coverage", "dashboard_read_models", "current_compared"), 1.0),
        (("verification", "coverage", "dashboard_read_models", "current_compared"), 1000000),
        (("verification", "counts", "evidence"), -1),
        (("verification", "counts", "evidence"), 1000000),
        (("verification", "counts", "facts"), True),
        (("verification", "counts"), {}),
    ],
)
def test_damaged_portable_completion_proof_is_not_verified(completed_backup, path, replacement):
    _, original_row, original_result = completed_backup
    result = deepcopy(original_result)
    target = result
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = replacement
    row = dict(original_row, result=json.dumps(result))

    class SavedJobConnection:
        def execute(self, statement, parameters=()):
            assert statement.startswith("SELECT * FROM jobs")
            return [row]

    item = Worklist._company_jobs(SavedJobConnection())[0]
    assert item["verified_when_succeeded"] is False
    assert item["result_issue"]["field"] == "job.result"
    assert item["current_file_availability"] == "not_checked"


@pytest.mark.parametrize(
    "field", [None, "status", "receipts", "current_compared", "derived_content"]
)
def test_current_portable_proof_requires_complete_dashboard_model_verification(
    completed_backup, field
):
    _, _, original_result = completed_backup
    result = deepcopy(original_result)
    coverage = result["verification"]["coverage"]
    if field is None:
        del coverage["dashboard_read_models"]
    else:
        del coverage["dashboard_read_models"][field]
    assert portable_result_verified(result) is False
