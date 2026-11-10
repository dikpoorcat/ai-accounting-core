"""Verified payroll archives retain both journals without duplicating cost or obligations."""

from pathlib import Path

from ai_accounting_modular.contracts.public import PageQuery
from ai_accounting_modular.host.public import create_offline_application
from ai_accounting_modular.ledger.public import ObligationPositionQuery
from ai_accounting_modular.records.public import open_standard_bundle
from tests.modular.domains.people.test_read_remuneration import add, wage_with_actuals

from ._working_case import case as case
from .test_isolated_targets import read_target
from .test_isolated_targets import request as isolated_request


def seed_payroll(case):
    case.seed(expense=False)
    wage, employment, actual = wage_with_actuals()
    add(case, wage, employment, actual)
    return wage


def checked_payroll(app, read, wage_id):
    (result,) = app.ledger.get_results([wage_id], read)
    journals = {journal["journal_key"]: journal for journal in result["vouchers"]}
    assert len(result["vouchers"]) == 2
    assert set(journals) == {"accrual", "deductions"}
    assert journals["accrual"]["summary"] == "工资、单位社保、单位公积金计提"
    assert journals["deductions"]["summary"] == "代扣个人社保、个人公积金、个人所得税"
    assert len({journal["voucher_id"] for journal in journals.values()}) == 2
    assert len({journal["number"] for journal in journals.values()}) == 2
    for journal in journals.values():
        assert [line["line_no"] for line in journal["lines"]] == list(
            range(1, len(journal["lines"]) + 1)
        )
        assert sum(int(line["debit_fen"]) for line in journal["lines"]) == sum(
            int(line["credit_fen"]) for line in journal["lines"]
        )
    assert result["target"]["values"]["cost_fen"] == "690000"
    assert app.ledger.pre_tax("2026-01", "2026-01", read).fields["profit_before_tax"] == {
        "state": "known",
        "amount_fen": "-690000",
    }
    positions = app.ledger.query_obligation_positions(
        ObligationPositionQuery("2026-01", business_ids=(wage_id,)), PageQuery(), read
    ).page
    assert positions.filtered_count == len(positions.items) == 6
    assert {
        position.definition.reference["slot"]: position.amounts.remaining_fen
        for position in positions.items
    } == {
        "net_pay": 500000,
        "tax": 30000,
        "employee_social": 50000,
        "employee_housing": 20000,
        "employer_social": 60000,
        "employer_housing": 30000,
    }
    assert read.repository("ledger").current_result(wage_id, "main")["calculation_revision"] == 3
    return result


def test_verified_payroll_zip_restore_preserves_both_frozen_journals_exactly(case, tmp_path):
    wage = seed_payroll(case)
    checked = case.check()
    assert checked["ready"], checked
    assert case.close(checked)["state"] == "closed"
    with case.kernel.storage.read_unit(case.company) as read:
        expected = checked_payroll(case.app, read, wage["business_id"])
        assert expected["reference"]["state"] == "frozen"
        metadata = case.kernel.storage.company_metadata(read)
        expected_head = read.head
    pending = case.app.accounting_backup(
        {"company_id": case.company, "expected_database_id": metadata.database_id}
    )
    done = case.kernel.files.run(case.company, pending["task_id"])
    assert done["state"] == "succeeded", done
    package = Path(done["path"])
    assert package.suffix == ".zip"
    offline, target = create_offline_application(), tmp_path / "restored-payroll"
    with package.open("rb") as stream:
        manifest = offline.backup_verifier(stream)
    assert manifest["database_head"] == expected_head
    checked = offline.accounting_isolated(isolated_request("restore", "check", package, target))
    assert checked["state"] == "checked", checked
    assert not target.exists()
    created = offline.accounting_isolated(isolated_request("restore", "run", package, target))
    assert created["state"] == "created", created
    assert created["company_id"] == case.company
    assert created["database_id"] == metadata.database_id
    with read_target(offline, target) as read:
        offline.backup_verifier.verify_snapshot(read)
        assert checked_payroll(offline, read, wage["business_id"]) == expected
        assert offline.ledger.closed_through(read) == "2026-01"


def test_standard_payroll_rebuild_uses_two_current_journals_and_one_cost_and_obligation_set(
    case, tmp_path
):
    wage = seed_payroll(case)
    with case.kernel.storage.read_unit(case.company) as read:
        original = checked_payroll(case.app, read, wage["business_id"])
        metadata = case.kernel.storage.company_metadata(read)
    bundle = open_standard_bundle(metadata.company_root / "standard/current.json")
    assert sum(fact["business_id"] == wage["business_id"] for fact in bundle.facts) == 1
    offline, target = create_offline_application(), tmp_path / "rebuilt-payroll"
    created = offline.accounting_isolated(
        isolated_request("rebuild", "run", bundle.manifest_path, target)
    )
    assert created["state"] == "created", created
    assert created["company_id"] != case.company
    assert created["database_id"] != metadata.database_id
    with read_target(offline, target) as read:
        offline.backup_verifier.verify_snapshot(read)
        rebuilt = checked_payroll(offline, read, wage["business_id"])
        assert rebuilt["target"] == original["target"]
        assert {
            journal["journal_key"]: (journal["summary"], journal["lines"])
            for journal in rebuilt["vouchers"]
        } == {
            journal["journal_key"]: (journal["summary"], journal["lines"])
            for journal in original["vouchers"]
        }
        assert offline.records.document_status(read)["state"] == "ready"
