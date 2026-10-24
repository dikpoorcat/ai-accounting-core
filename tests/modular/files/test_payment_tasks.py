"""Payment exports use the same durable task boundary without report or capture payloads."""

from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError

from ._support import payment_plan, service


def test_payment_task_survives_service_restart_and_delivers_checked_bytes(file_case):
    storage, (company, _), producer, files = file_case
    plan = payment_plan(company)
    task = files.ensure_current(plan, plan["scope_head"], "payment-request")
    assert task["kind"] == "payment_export" and task["state"] == "pending"
    assert files.ensure_current(plan, plan["scope_head"], "same-plan") == task
    with storage.read_unit(company.company_id) as read:
        row = read.repository("files").get(task["task_id"], plan=True)
        assert row["plan_schema"] == "PaymentExportPlan" and row["capture_id"] is None
    files.close()
    resumed = service(storage, producer)
    try:
        resumed.recover(company.company_id)
        done = resumed.run_pending(company.company_id)
        assert len(done) == 1 and done[0]["state"] == "succeeded"
        current = resumed.current_plan(
            company.company_id, "payment_export", producer.describe(plan).key
        )
        assert current.plan == plan and current.receipt == done[0]
        with resumed.verified_download(company.company_id, task["task_id"]) as artifact:
            assert artifact.file_name == "合成代发.xlsx"
            assert len(artifact.stream.read()) == artifact.byte_size > 0
    finally:
        resumed.close()


def test_payment_tasks_obey_current_head_fencing_and_company_identity(file_case):
    _, (company, other), producer, files = file_case
    plan = payment_plan(company)
    task = files.ensure_current(plan, plan["scope_head"], "payment-request")
    invalid = {**plan, "company_id": other.company_id}
    with pytest.raises(ContractError) as error:
        files.ensure_current(invalid, invalid["scope_head"], "foreign-payment")
    assert error.value.code == "conflict"
    producer.current_heads = set()
    current = files.current_plan(company.company_id, "payment_export", producer.describe(plan).key)
    assert current.plan is None and current.receipt["state"] == "expired"
    assert files.run(company.company_id, task["task_id"])["state"] == "expired"


def test_payment_plan_cannot_be_loaded_as_report_or_with_capture_inputs(file_case):
    storage, (company, _), producer, files = file_case
    plan = payment_plan(company)
    with pytest.raises(ContractError) as error:
        files.ensure_current(
            {**plan, "capture_id": str(uuid4())}, plan["scope_head"], "bad-capture"
        )
    assert error.value.code == "invalid_input"
    task = files.ensure_current(plan, plan["scope_head"], "payment-request")
    with storage.maintenance_unit(company.company_id) as unit:
        unit._connection.execute(
            "UPDATE file_task SET plan_schema='ReportExportPlan' WHERE task_id=?",
            (task["task_id"],),
        )
    with pytest.raises(ContractError) as error:
        files.current_plan(company.company_id, "payment_export", producer.describe(plan).key)
    assert error.value.code == "storage_corrupt"
