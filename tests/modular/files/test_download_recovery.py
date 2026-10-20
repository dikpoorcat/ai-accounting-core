"""Definite damage ends only the delivery; temporary reads preserve a valid task."""

from pathlib import Path

import pytest
from openpyxl import load_workbook

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.files import _paths

from ._support import report_plan


@pytest.mark.parametrize("stage", ["read", "verify"])
def test_transient_access_failure_keeps_same_verified_delivery(file_case, monkeypatch, stage):
    _, (company, _), producer, files = file_case
    plan = report_plan(company)
    task = files.ensure_current(plan, plan["scope_head"], "creator")
    done = files.run(company.company_id, task["task_id"])
    original = Path(done["path"]).read_bytes()

    def temporarily_locked(*args):
        raise PermissionError("synthetic temporary file access")

    with monkeypatch.context() as patch:
        if stage == "read":
            patch.setattr(_paths, "snapshot_file", temporarily_locked)
        else:
            patch.setattr(producer, "verify_hook", temporarily_locked)
        with pytest.raises(ContractError) as error:
            with files.verified_download(company.company_id, task["task_id"]):
                pytest.fail("locked file delivered")
        assert error.value.code == "temporary_failure"
    assert files.get(company.company_id, task["task_id"]) == done
    assert files.ensure_current(plan, plan["scope_head"], "another client") == done
    with files.verified_download(company.company_id, task["task_id"]) as downloaded:
        assert downloaded.stream.read() == original


def test_valid_workbook_with_same_semantic_header_but_wrong_saved_bytes_is_terminal(file_case):
    _, (company, _), _, files = file_case
    plan = report_plan(company)
    task = files.ensure_current(plan, plan["scope_head"], "creator")
    done = files.run(company.company_id, task["task_id"])
    book = load_workbook(done["path"])
    book.active["C1"] = "different content, still the same synthetic semantic identity"
    book.save(done["path"])
    book.close()
    with pytest.raises(ContractError) as error:
        with files.verified_download(company.company_id, task["task_id"]):
            pytest.fail("changed bytes delivered")
    assert error.value.code == "file_invalid"
    failed = files.get(company.company_id, task["task_id"])
    assert failed["state"] == "failed" and failed["attempts"] == done["attempts"]
    assert files.ensure_current(plan, plan["scope_head"], "creator") == failed
