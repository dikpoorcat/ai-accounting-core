"""The public export use case rejects invented inputs and preserves exact tasks."""

from uuid import uuid4

import pytest

from ai_accounting_modular.agent.public import create_agent
from ai_accounting_modular.contracts.public import ContractError, validate_contract

from ._working_case import case as case


def generate(case, **fields):
    agent = create_agent(case.app)
    payload = {"action": "generate", "company_id": case.company, "year": 2026, "quarter": 1}
    return agent.dispatch("accounting_report_export", payload | fields)


def prepared(case):
    result = generate(case)
    for _ in range(5):
        if result["state"] != "preparing":
            return result
        case.kernel.reading.run_pending(case.company)
        result = generate(case)
    pytest.fail("合成季度读取未就绪")


def test_unknown_report_is_partial_and_does_not_create_a_file_task_or_advance_books(case):
    assert generate(case)["state"] == "preparing"
    result = prepared(case)
    validate_contract("core.schema.json", "ReportExportResult", result)
    assert result["state"] == "partial" and result["issues"]
    assert "task" not in result
    with case.kernel.storage.read_unit(case.company) as read:
        assert read.head == 0
        assert read.repository("files").current() == ()


@pytest.mark.parametrize(
    "fields",
    [
        {"action": "inspect"},
        {"action": "status"},
        {"request_id": str(uuid4())},
        {"preview_digest": "0" * 64},
        {"epochs": {"accounting": 0, "material": 0, "management": 1}},
        {"output_directory": "C:/arbitrary"},
        {"amount_fen": "10000"},
        {"quarter": True},
        {"year": 0},
    ],
)
def test_export_rejects_undeclared_actions_versions_paths_and_amounts(case, fields):
    with pytest.raises(ContractError) as failed:
        generate(case, **fields)
    assert failed.value.code == "invalid_input"


def test_saving_an_unknown_task_never_invokes_the_desktop_writer(case):
    def unexpected_write(_):
        pytest.fail("未知报表任务不能调用桌面写入")

    case.app.save_report = unexpected_write
    with pytest.raises(ContractError) as failed:
        case.app.accounting_report_export(
            {
                "action": "save",
                "company_id": case.company,
                "task_id": str(uuid4()),
                "destination": "desktop",
            }
        )
    assert failed.value.code == "unknown_report_job"


def test_failed_desktop_write_preserves_the_successful_source_task(case):
    case.seed(expense=False)
    queued = prepared(case)
    task_id = queued["task"]["task_id"]
    case.kernel.files.run_pending(case.company)
    before = case.app.accounting_jobs({"company_id": case.company, "task_id": task_id})
    assert before["state"] == "succeeded"

    def full_desktop(_):
        raise ContractError("desktop_write_failed", "桌面空间不足。")

    case.app.save_report = full_desktop
    with pytest.raises(ContractError) as failed:
        case.app.accounting_report_export(
            {
                "action": "save",
                "company_id": case.company,
                "task_id": task_id,
                "destination": "desktop",
            }
        )
    assert failed.value.code == "desktop_write_failed"
    assert case.app.accounting_jobs({"company_id": case.company, "task_id": task_id}) == before
