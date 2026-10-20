"""Saved output seals prove omissions without guessing a kind's accounting rules."""

from copy import deepcopy

import pytest
from test_adoption import BUSINESS, COMPANY, SOURCE, adopt_declared, expense
from test_adoption import store as store
from test_closing import close
from test_financial_core import origin, save
from test_report_details import change as detailed_expense
from test_report_details import detail
from test_snapshot import _snapshot, _tamper

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.ledger._output_digest import EMPTY_OUTPUT_DIGEST
from ai_accounting_modular.ledger._repository import LedgerRepository
from ai_accounting_modular.ledger.public import create_ledger


@pytest.mark.parametrize("damage", ["journal", "obligation", "report_detail", "balanced_accounts"])
def test_individually_legal_remaining_rows_cannot_hide_missing_or_changed_outputs(store, damage):
    ledger = create_ledger()
    save(store, ledger, [detailed_expense(detail(amount="1000"))])
    assert _snapshot(store, ledger).result_count == 1

    def mutate(connection):
        if damage == "journal":
            connection.execute("DELETE FROM voucher_line")
            connection.execute("DELETE FROM voucher")
        elif damage == "obligation":
            connection.execute("DELETE FROM obligation")
        elif damage == "report_detail":
            connection.execute("DELETE FROM result_report_contribution")
        else:
            connection.execute("UPDATE voucher_line SET account_code='540101' WHERE line_no=1")

    _tamper(store, mutate)
    with pytest.raises(ContractError, match="正式输出不完整") as error:
        _snapshot(store, ledger)
    assert error.value.code == "storage_corrupt"


def test_no_output_is_explicitly_sealed_and_fact_version_or_freeze_does_not_reseal_it(store):
    ledger = create_ledger()
    save(store, ledger, [origin("0")])
    with store.read_unit(COMPANY) as read:
        saved = read.repository("ledger").current_heads()[0]
        assert saved["formal_output_digest"] == EMPTY_OUTPUT_DIGEST
    close(store, ledger, "2026-01")
    assert _snapshot(store, ledger).voucher_count == 0
    with store.read_unit(COMPANY) as read:
        frozen = read.repository("ledger").current_heads()[0]
        assert frozen["formal_output_digest"] == saved["formal_output_digest"]


def test_open_replacement_keeps_number_and_same_output_keeps_its_seal(store):
    ledger = create_ledger()
    save(store, ledger, [expense()])
    with store.read_unit(COMPANY) as read:
        repo = read.repository("ledger")
        original = repo.current_result(BUSINESS, "main")
        number = repo.vouchers([original["result_id"]])[0]["number"]
    save(store, ledger, [expense("90000", seq=1)])
    with store.read_unit(COMPANY) as read:
        repo = read.repository("ledger")
        changed = repo.current_result(BUSINESS, "main")
        assert repo.vouchers([changed["result_id"]])[0]["number"] == number
        assert changed["formal_output_digest"] != original["formal_output_digest"]
    save(store, ledger, [expense("90000", seq=2)])
    with store.read_unit(COMPANY) as read:
        repeated = read.repository("ledger").current_result(BUSINESS, "main")
        assert repeated["adopted_fact_seq"] == 3
        assert repeated["formal_output_digest"] == changed["formal_output_digest"]
    assert _snapshot(store, ledger).voucher_count == 1


def test_metadata_only_frozen_revalidation_keeps_original_seal_and_basis(store):
    ledger = create_ledger()
    save(store, ledger, [origin(), expense()])
    close(store, ledger, "2026-01")
    with store.read_unit(COMPANY) as read:
        before = read.repository("ledger").current_result(BUSINESS, "main")
    location_only = expense(seq=1)
    location_only["plans"][0]["basis"]["source_locations"] = [
        {"source_id": SOURCE, "location": "重新定位后的正确原件页"}
    ]
    save(store, ledger, [location_only], correction="2026-02")
    with store.read_unit(COMPANY) as read:
        after = read.repository("ledger").current_result(BUSINESS, "main")
    assert after == before
    assert _snapshot(store, ledger).result_count == 2


def test_changed_adoption_cannot_reinterpret_a_damaged_inherited_baseline_as_no_journal(store):
    ledger = create_ledger()
    save(store, ledger, [origin(), expense()])
    close(store, ledger, "2026-01")
    formal_only = expense(seq=1)
    formal_only["plans"][0]["obligations"][0]["allowed_settlements"] = ["money"]
    save(store, ledger, [formal_only], correction="2026-02")
    close(store, ledger, "2026-02")

    def delete_original(connection):
        connection.execute("DELETE FROM voucher_line")
        connection.execute("DELETE FROM voucher")

    _tamper(store, delete_original)
    changed = expense("90000", seq=2)
    changed["plans"][0]["obligations"][0]["allowed_settlements"] = ["money"]
    with pytest.raises(ContractError, match="正式输出不完整") as error:
        save(store, ledger, [changed], correction="2026-03")
    assert error.value.code == "storage_corrupt"
    with store.read_unit(COMPANY) as read:
        assert read.repository("ledger").current_result(BUSINESS, "main")["posting_period"] == (
            "2026-02"
        )


def test_commit_guard_cannot_silently_commit_later_partial_output_changes(store):
    ledger = create_ledger()
    with pytest.raises(ContractError, match="正式输出不完整"):
        with store.write_unit(COMPANY) as unit:
            adopt_declared(ledger, [expense()], unit)
            unit.repository("ledger")._session.execute("DELETE FROM obligation")
    assert _snapshot(store, ledger).result_count == 0


@pytest.mark.parametrize("action", ["check", "close"])
def test_damaged_saved_journal_cannot_be_frozen_as_an_empty_valid_report(store, action):
    ledger = create_ledger()
    save(store, ledger, [origin("0"), expense()])
    with store.read_unit(COMPANY) as read:
        checked = ledger.check_period("2026-01", read)
        assert checked.ready
        before_head = read.head
        before_refs = ledger.get_result_refs([BUSINESS], read)

    def delete_journal(connection):
        connection.execute("DELETE FROM voucher_line")
        connection.execute("DELETE FROM voucher")

    _tamper(store, delete_journal)
    with pytest.raises(ContractError, match="正式输出不完整") as error:
        if action == "check":
            with store.read_unit(COMPANY) as read:
                ledger.check_period("2026-01", read)
        else:
            with store.write_unit(COMPANY) as unit:
                ledger.close_period("2026-01", checked.head, unit)
    assert error.value.code == "storage_corrupt"
    with store.read_unit(COMPANY) as read:
        assert read.head == before_head and ledger.closed_through(read) is None
        assert ledger.get_result_refs([BUSINESS], read) == tuple(
            {
                key: value
                for key, value in ref.items()
                if key not in {"voucher_id", "voucher_number"}
            }
            for ref in before_refs
        )
        assert all(row["state"] == "open" for row in read.repository("ledger").current_heads())


def test_retained_and_unrelated_outputs_are_not_reread_by_a_changed_slot(store, monkeypatch):
    ledger = create_ledger()
    other_id = "50000000-0000-4000-8000-000000000099"
    save(store, ledger, [expense(), expense(business_id=other_id)])
    with store.read_unit(COMPANY) as read:
        retained_ref = ledger.get_result_refs([BUSINESS], read)[0]
        changed_ref = ledger.get_result_refs([other_id], read)[0]
    retained = deepcopy(expense(seq=1))
    retained["businesses"][0]["op"] = "recalculate"
    retained.update(plans=[], retained_results=[retained_ref])
    captured = []
    original = LedgerRepository.output_records

    def narrowed(repo, result_ids):
        captured.extend(result_ids)
        return original(repo, result_ids)

    monkeypatch.setattr(LedgerRepository, "output_records", narrowed)
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [retained, expense("90000", seq=1, business_id=other_id)], unit)
    assert captured and set(captured) == {changed_ref["result_id"]}
    assert _snapshot(store, ledger).result_count == 2
