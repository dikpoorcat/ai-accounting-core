"""Independent saved-ledger checks, including corruption normal writers cannot produce."""

import json
import sqlite3
from contextlib import closing
from copy import deepcopy
from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest
from _financial_snapshot_review_support import BANK as APP_BANK
from _financial_snapshot_review_support import PARTY_A as APP_PARTY
from _financial_snapshot_review_support import assemble, fact, loan_contract
from _financial_snapshot_review_support import expense as app_expense
from _financial_snapshot_review_support import opening as app_opening
from _financial_snapshot_review_support import payment as app_payment
from test_adoption import BUSINESS, COMPANY, SOURCE, _payment_at, adopt_declared, expense, refund
from test_adoption import store as store
from test_aggregations import journal
from test_closing import close
from test_financial_core import ORIGIN, funds, origin, save

from ai_accounting_modular.contracts.public import ContractError, canonical_json, content_digest
from ai_accounting_modular.ledger import _closing, _financial
from ai_accounting_modular.ledger._output_digest import RESOURCE_TABLES, output_digests
from ai_accounting_modular.ledger.public import (
    AdoptedBusinessVersion,
    LedgerSnapshot,
    create_ledger,
)
from ai_accounting_modular.storage.public import module_schema


def _snapshot(storage, ledger):
    with storage.read_unit(COMPANY) as read:
        return ledger.verify_snapshot(read)


def _tamper(storage, statement, parameters=()):
    """Restore exact installed triggers after modelling an externally damaged file."""
    with storage.read_unit(COMPANY) as read:
        path = storage.company_metadata(read).database_path
    triggers = [item for item in module_schema("ledger").objects if item.type == "trigger"]
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("PRAGMA ignore_check_constraints=ON")
        for trigger in triggers:
            connection.execute(f'DROP TRIGGER "{trigger.name}"')
        if callable(statement):
            statement(connection)
        else:
            connection.execute(statement, parameters)
        for trigger in triggers:
            connection.execute(trigger.sql)
        connection.commit()


def test_empty_snapshot_and_immutable_summary(store):
    ledger = create_ledger()
    summary = _snapshot(store, ledger)
    assert summary == LedgerSnapshot(0, 0, (), (), None)
    with store.read_unit(COMPANY) as read:
        assert not ledger.has_formal_results(read)
    with pytest.raises(FrozenInstanceError):
        summary.result_count = 3


def test_valid_open_ledger_sources_are_the_complete_deduplicated_basis_locations(store):
    ledger = create_ledger()
    change = expense()
    change["plans"][0]["basis"]["source_locations"] = [
        {"source_id": SOURCE, "location": "原件第一行"},
        {"source_id": SOURCE, "location": "原件第二行"},
    ]
    save(store, ledger, [change])
    with store.read_unit(COMPANY) as read:
        assert ledger.has_formal_results(read)
    assert _snapshot(store, ledger) == LedgerSnapshot(
        1, 1, (SOURCE,), (AdoptedBusinessVersion(BUSINESS, 1, 1, ("main",)),), "2026-01"
    )


def test_real_close_and_payment_correction_select_only_one_historical_movement(store, monkeypatch):
    ledger = create_ledger()
    save(store, ledger, [origin(), expense(), _payment_at()])
    close(store, ledger, "2026-01")
    save(store, ledger, [_payment_at("80000", seq=1)], correction="2026-02")

    def no_policy_recalculation(*args, **kwargs):
        pytest.fail("saved-ledger verification must not recalculate financial snapshots")

    monkeypatch.setattr(_financial, "snapshot", no_policy_recalculation)
    summary = _snapshot(store, ledger)
    assert summary.result_count == 4 and summary.voucher_count == 4


def test_formal_only_frozen_baseline_inherits_original_journal_before_later_reversal(store):
    ledger = create_ledger()
    save(store, ledger, [origin(), expense()])
    close(store, ledger, "2026-01")
    formal = expense(seq=1)
    formal["plans"][0]["obligations"][0]["allowed_settlements"] = ["money"]
    save(store, ledger, [formal], correction="2026-02")
    close(store, ledger, "2026-02")
    changed = expense("90000", seq=2)
    changed["plans"][0]["obligations"][0]["allowed_settlements"] = ["money"]
    save(store, ledger, [changed], correction="2026-03")
    assert _snapshot(store, ledger) == LedgerSnapshot(
        4,
        3,
        (),
        tuple(
            sorted(
                (
                    AdoptedBusinessVersion(BUSINESS, 3, 3, ("main",)),
                    AdoptedBusinessVersion(ORIGIN, 1, None, ("main",)),
                ),
                key=lambda version: version.business_id,
            )
        ),
        "2026-03",
    )


def test_future_adopted_version_is_exposed_for_application_owner_comparison(store):
    ledger = create_ledger()
    save(store, ledger, [expense()])
    _tamper(store, "UPDATE adopted_result SET adopted_fact_seq=2")
    summary = _snapshot(store, ledger)
    assert summary.business_versions == (AdoptedBusinessVersion(BUSINESS, 2, 2, ("main",)),)
    with pytest.raises(FrozenInstanceError):
        summary.business_versions[0].max_fact_seq = 3


def test_frozen_old_version_is_preserved_beside_current_open_version(store):
    ledger = create_ledger()
    save(store, ledger, [origin(), expense()])
    close(store, ledger, "2026-01")
    assert _snapshot(store, ledger).business_versions == tuple(
        sorted(
            (
                AdoptedBusinessVersion(BUSINESS, 1, None, ("main",)),
                AdoptedBusinessVersion(ORIGIN, 1, None, ("main",)),
            ),
            key=lambda version: version.business_id,
        )
    )
    save(store, ledger, [expense("90000", seq=1)], correction="2026-02")
    assert _snapshot(store, ledger).business_versions == tuple(
        sorted(
            (
                AdoptedBusinessVersion(BUSINESS, 2, 2, ("main",)),
                AdoptedBusinessVersion(ORIGIN, 1, None, ("main",)),
            ),
            key=lambda version: version.business_id,
        )
    )


def _two_slot_expense():
    change = expense()
    extra = deepcopy(change["plans"][0])
    extra["result_slot"] = extra["basis"]["result_slot"] = extra["target"]["result_slot"] = "aux"
    extra["obligations"][0]["slot"] = "second"
    change["plans"].append(extra)
    change["businesses"][0]["complete_result_slots"].append("aux")
    change["publications"].append({**change["publications"][0], "result_slot": "aux"})
    return change


def test_two_open_slots_cannot_claim_different_versions_of_one_business(store):
    ledger = create_ledger()
    save(store, ledger, [_two_slot_expense()])
    assert _snapshot(store, ledger).business_versions == (
        AdoptedBusinessVersion(BUSINESS, 1, 1, ("aux", "main")),
    )
    _tamper(store, "UPDATE adopted_result SET adopted_fact_seq=2 WHERE result_slot='aux'")
    with pytest.raises(ContractError) as error:
        _snapshot(store, ledger)
    assert error.value.code == "storage_corrupt"


@pytest.mark.parametrize("frozen", [False, True])
def test_selected_fund_slots_cannot_overlap_across_different_adoption_months(store, frozen):
    ledger = create_ledger()
    change = funds(journal(BUSINESS, [("1002", 100), ("3001", -100)]), 100, "capital_receipts")
    extra = deepcopy(change["plans"][0])
    extra["result_slot"] = extra["basis"]["result_slot"] = extra["target"]["result_slot"] = "aux"
    extra["accounting_period"] = extra["basis"]["period"] = "2026-02"
    extra["fund_movements"][0].update(
        movement_id=str(uuid4()), public_slot="second", actual_on="2026-02-15"
    )
    change["plans"].append(extra)
    change["businesses"][0]["complete_result_slots"].append("aux")
    change["publications"].append({**change["publications"][0], "result_slot": "aux"})
    save(store, ledger, [origin("0"), change])
    if frozen:
        close(store, ledger, "2026-02")
        changed = deepcopy(change)
        changed["businesses"][0].update(op="replace", expected_seq=1)
        changed["plans"][0]["fund_movements"][0].update(
            movement_id=str(uuid4()), public_slot="third"
        )
        save(store, ledger, [changed], correction="2026-03")
    assert _snapshot(store, ledger).result_count == (4 if frozen else 3)

    def duplicate(connection):
        connection.execute(
            "UPDATE result_fund_movement SET public_slot='funds' WHERE result_id IN "
            "(SELECT result_id FROM adopted_result WHERE result_slot='aux')"
        )
        # Preserve the saved-output seal so only selected public identity is damaged.
        connection.row_factory = sqlite3.Row
        tables = ("voucher", "voucher_line", *RESOURCE_TABLES)
        records = {
            table: [dict(row) for row in connection.execute("SELECT * FROM " + table)]
            for table in tables
        }
        result_ids = [row[0] for row in connection.execute("SELECT result_id FROM adopted_result")]
        connection.executemany(
            "UPDATE adopted_result SET formal_output_digest=? WHERE result_id=?",
            [
                (digest, result_id)
                for result_id, digest in output_digests(records, result_ids).items()
            ],
        )

    _tamper(store, duplicate)
    with pytest.raises(ContractError, match="公开资源槽不能重复") as error:
        _snapshot(store, ledger)
    assert error.value.code == "storage_corrupt"


def test_retired_slot_keeps_old_adoption_version_after_same_month_fact_changes(store):
    ledger = create_ledger()
    save(store, ledger, [origin(), _two_slot_expense()])
    close(store, ledger, "2026-01")
    with store.write_unit(COMPANY) as unit:
        adopt_declared(
            ledger,
            [expense("90000", seq=1)],
            unit,
            correction_period="2026-02",
            explanation="辅助结果不再属于最终正确事实。",
        )
    save(store, ledger, [expense("80000", seq=2)], correction="2026-02")
    summary = _snapshot(store, ledger)
    assert next(v for v in summary.business_versions if v.business_id == BUSINESS) == (
        AdoptedBusinessVersion(BUSINESS, 3, 3, ("main",))
    )
    with store.read_unit(COMPANY) as read:
        retired = read.repository("ledger").current_result(BUSINESS, "aux")
        assert retired["target_state"] == "withdrawn" and retired["adopted_fact_seq"] == 2
    close(store, ledger, "2026-02")
    assert next(
        v for v in _snapshot(store, ledger).business_versions if v.business_id == BUSINESS
    ) == (AdoptedBusinessVersion(BUSINESS, 3, None, ("main",)))


@pytest.mark.parametrize("damage", ["stale_source", "ordered_index"])
def test_open_consumers_require_current_source_and_exact_ordered_basis_index(store, damage):
    ledger = create_ledger()
    save(store, ledger, [expense()])
    with store.read_unit(COMPANY) as read:
        source = ledger.get_result_source(BUSINESS, "main", read)
    save(store, ledger, [refund(source)])
    assert _snapshot(store, ledger).result_count == 2

    def mutate(connection):
        if damage == "stale_source":
            connection.execute(
                "UPDATE adopted_result SET generation=generation+1 WHERE business_id=?", (BUSINESS,)
            )
            connection.execute(
                "UPDATE result_slot_current SET generation=generation+1 WHERE business_id=?",
                (BUSINESS,),
            )
        else:
            connection.execute("UPDATE result_basis_ref SET item_no=2")

    _tamper(store, mutate)
    with pytest.raises(ContractError) as error:
        _snapshot(store, ledger)
    assert error.value.code == "storage_corrupt"


def test_public_application_opening_and_frozen_reference_to_replaced_prebook_body(tmp_path):
    book = assemble(tmp_path)
    contract = loan_contract("prebook-snapshot-contract", "2028-01-01", month="2025-01")
    draw = fact(
        "snapshot-draw",
        "financing.loan_drawdown",
        {
            "contract_id": contract["business_id"],
            "principal_fen": "100",
            "funds_account_id": APP_BANK,
        },
        on="2026-01-15",
    )
    book.submit(app_opening(), contract, draw)
    with book.app.storage.write_unit(book.company) as unit:
        checked = book.app.ledger.check_period("2026-01", unit)
        assert checked.ready, checked
        book.app.ledger.close_period("2026-01", checked.head, unit)
    contract["data"]["matures_on"] = "2029-01-01"
    book.replace(contract, correction_period="2026-02")
    with book.app.storage.read_unit(book.company) as read:
        summary = book.app.ledger.verify_snapshot(read)
        current = read.repository("ledger").current_result(contract["business_id"], "main")
        assert current["state"] == "open" and current["posting_period"] == "2025-01"
    assert summary.result_count == 4 and summary.voucher_count == 1 and summary.source_ids


def test_public_application_frozen_expense_and_actual_payment_correction(tmp_path):
    book = assemble(tmp_path)
    charged = app_expense("snapshot-actual-cost", "30")
    paid = app_payment("snapshot-actual-paid", charged, "20")
    book.submit(app_opening(cash="100"), charged, paid)
    with book.app.storage.write_unit(book.company) as unit:
        checked = book.app.ledger.check_period("2026-01", unit)
        assert checked.ready
        book.app.ledger.close_period("2026-01", checked.head, unit)
    paid["data"]["amount_fen"] = "10"
    paid["data"]["allocations"][0]["amount_fen"] = "10"
    book.replace(paid, correction_period="2026-02")
    with book.app.storage.read_unit(book.company) as read:
        summary = book.app.ledger.verify_snapshot(read)
    assert summary.result_count == 4 and summary.voucher_count == 4


def test_unclosed_months_need_no_independent_capacity_before_later_real_recovery(tmp_path):
    book = assemble(tmp_path)
    charged = app_expense("final-cross-month-cost", "80")
    paid = app_payment("final-cross-month-paid", charged, "100")
    claim = fact(
        "final-cross-month-claim",
        "operations.overpayment",
        {
            "source": {"business_id": charged["business_id"], "slot": "primary"},
            "counterparty_id": APP_PARTY,
            "amount_fen": "20",
        },
        "2026-02",
    )
    received = app_payment(
        "final-cross-month-refund", claim, "20", on="2026-02-20", slot="recovery", incoming=True
    )
    book.submit(app_opening(cash="200"), received, claim, paid, charged)
    with book.app.storage.read_unit(book.company) as read:
        summary = book.app.ledger.verify_snapshot(read)
        assert summary.result_count == 5
        movements = book.app.ledger.fund_movements(APP_BANK, "2026-01", "2026-02", read)
    assert sorted(
        (movement.value["actual_on"], movement.value["amount_fen"]) for movement in movements
    ) == [("2026-01-21", "100"), ("2026-02-20", "20")]
    with book.app.storage.write_unit(book.company) as unit:
        checked = book.app.ledger.check_period("2026-02", unit)
        assert checked.ready
        book.app.ledger.close_period("2026-02", checked.head, unit)
    with book.app.storage.read_unit(book.company) as read:
        assert book.app.ledger.verify_snapshot(read).result_count == 5


@pytest.mark.parametrize("details", [False, True])
def test_actual_expense_keeps_known_and_unestablished_report_contributions(tmp_path, details):
    book = assemble(tmp_path)
    charged = app_expense("snapshot-report-details", "100000", details=details)
    if details:
        charged["data"]["report_details"] = [{"code": "management_startup", "amount_fen": "100000"}]
    book.submit(app_opening(), charged)
    with book.app.storage.read_unit(book.company) as read:
        summary = book.app.ledger.verify_snapshot(read)
        rows = read.repository("ledger").snapshot_records()["result_report_contribution"]
    assert summary.result_count == 2 and summary.voucher_count == 1
    assert rows
    if details:
        assert any(
            row["detail_code"] == "management_startup" and row["amount_fen"] == 100000
            for row in rows
        )
        assert all(row["issue_json"] is None for row in rows)
    else:
        assert all(row["amount_fen"] is None and row["issue_json"] is not None for row in rows)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE adopted_result SET basis_schema='other'",
        "UPDATE adopted_result SET basis_json='{}'",
        "UPDATE adopted_result SET target_digest='" + "0" * 64 + "'",
        "UPDATE adopted_result SET result_slot='wrong-slot'",
        "UPDATE result_slot_current SET generation=generation+1",
        "DELETE FROM result_slot_current",
        "UPDATE voucher SET sealed=0",
        "UPDATE voucher SET period='2026-02'",
        "UPDATE voucher SET number=0",
        "UPDATE voucher SET role='closing'",
        "UPDATE voucher_line SET line_no=9 WHERE line_no=2",
        "UPDATE voucher_line SET debit_fen=debit_fen+1 WHERE line_no=1",
        "UPDATE voucher_sequence SET next_number=1",
        "DELETE FROM voucher_sequence",
        "UPDATE obligation SET amount_fen=-1",
        "UPDATE obligation SET allowed_settlements_json='[\"invented\"]'",
        'UPDATE obligation SET allowed_settlements_json=\'["money","money"]\'',
        "UPDATE obligation SET cashflow_weights_json='["
        '{"category":"other_operating_payments","weight":"1"},'
        '{"category":"other_operating_payments","weight":"1"}]'
        "'",
        "INSERT INTO result_source SELECT result_id,'"
        + SOURCE
        + "','unrecorded source' FROM adopted_result",
        "INSERT INTO result_basis_ref SELECT result_id,1,business_id,result_slot,"
        "result_id,generation,basis_digest FROM adopted_result",
    ],
)
def test_corrupt_persisted_result_voucher_index_or_resource_is_rejected(store, statement):
    ledger = create_ledger()
    save(store, ledger, [expense()])
    _tamper(store, statement)
    with pytest.raises(ContractError) as error:
        _snapshot(store, ledger)
    assert error.value.code == "storage_corrupt"


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE result_fund_movement SET actual_on='2026-02-15'",
        "UPDATE result_fund_movement SET cashflow_parts_json='[]'",
        "UPDATE result_fund_movement SET cashflow_parts_json='["
        '{"category":"other_operating_payments","amount_fen":"1"}]'
        "'",
        "UPDATE settlement SET amount_fen=200000",
        "UPDATE settlement SET source_public_slot='missing'",
        "UPDATE settlement SET period='2026-02'",
        "UPDATE result_fund_movement SET direction='in'",
    ],
)
def test_corrupt_real_resource_schema_period_direction_or_capacity_is_rejected(store, statement):
    ledger = create_ledger()
    save(store, ledger, [origin(), expense(), _payment_at()])
    _tamper(store, statement)
    with pytest.raises(ContractError) as error:
        _snapshot(store, ledger)
    assert error.value.code == "storage_corrupt"


def test_duplicate_actual_movement_under_another_current_result_is_rejected(store):
    ledger = create_ledger()
    save(store, ledger, [origin(), expense(), _payment_at()])

    def duplicate(connection):
        values = list(connection.execute("SELECT * FROM result_fund_movement").fetchone())
        values[0] = connection.execute(
            "SELECT result_id FROM adopted_result WHERE business_id=?", (BUSINESS,)
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO result_fund_movement VALUES(" + ",".join("?" for _ in values) + ")", values
        )

    _tamper(store, duplicate)
    with pytest.raises(ContractError) as error:
        _snapshot(store, ledger)
    assert error.value.code == "storage_corrupt"


def test_historical_capacity_is_checked_even_when_the_current_correction_is_valid(store):
    ledger = create_ledger()
    save(store, ledger, [origin(), expense(), _payment_at()])
    close(store, ledger, "2026-01")
    save(store, ledger, [_payment_at("80000", seq=1)], correction="2026-02")
    assert _snapshot(store, ledger).result_count == 4
    _tamper(
        store,
        "UPDATE settlement SET amount_fen=200000 WHERE result_id IN "
        "(SELECT result_id FROM adopted_result WHERE state='frozen')",
    )
    with pytest.raises(ContractError) as error:
        _snapshot(store, ledger)
    assert error.value.code == "storage_corrupt"


def test_replacement_cannot_omit_its_frozen_original_reversal(store):
    ledger = create_ledger()
    save(store, ledger, [origin(), expense()])
    close(store, ledger, "2026-01")
    save(store, ledger, [expense("80000", seq=1)], correction="2026-02")

    def remove_reversal(connection):
        connection.execute(
            "DELETE FROM voucher_line WHERE voucher_id IN "
            "(SELECT voucher_id FROM voucher WHERE role='reversal')"
        )
        connection.execute("DELETE FROM voucher WHERE role='reversal'")

    _tamper(store, remove_reversal)
    with pytest.raises(ContractError, match="没有冲正") as error:
        _snapshot(store, ledger)
    assert error.value.code == "storage_corrupt"


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE adopted_result SET baseline_result_id=NULL WHERE posting_period='2026-02'",
        "UPDATE adopted_result SET business_id='"
        + str(uuid4())
        + "' WHERE posting_period='2026-02'",
        "UPDATE voucher SET reverses_voucher_id=NULL WHERE role='reversal'",
        "UPDATE voucher_line SET debit_fen=credit_fen,credit_fen=debit_fen "
        "WHERE voucher_id IN (SELECT voucher_id FROM voucher WHERE role='reversal')",
        "DELETE FROM period_close",
        "UPDATE adopted_result SET state='open' WHERE state='frozen'",
    ],
)
def test_corrupt_frozen_baseline_or_reversal_authority_is_rejected(store, statement):
    ledger = create_ledger()
    save(store, ledger, [origin(), expense()])
    close(store, ledger, "2026-01")
    save(store, ledger, [expense("80000", seq=1)], correction="2026-02")
    _tamper(store, statement)
    with pytest.raises(ContractError) as error:
        _snapshot(store, ledger)
    assert error.value.code == "storage_corrupt"


@pytest.mark.parametrize("change", ["report_digest", "mapper", "original_ref_digest"])
def test_saved_closing_hash_mapper_and_original_frozen_references_are_verified(store, change):
    ledger = create_ledger()
    save(store, ledger, [origin(), expense()])
    close(store, ledger, "2026-01")

    def damage(connection):
        row = connection.execute(
            "SELECT basis_json,report_json,result_head FROM period_close"
        ).fetchone()
        basis, report = json.loads(row[0]), json.loads(row[1])
        if change == "report_digest":
            connection.execute("UPDATE period_close SET report_digest=?", ("0" * 64,))
        elif change == "mapper":
            report["mapping_version"] = "uninstalled-version"
            connection.execute(
                "UPDATE period_close SET report_json=?,report_digest=?",
                (canonical_json(report), content_digest(report)),
            )
        else:
            basis["result_digest"] = "0" * 64
            connection.execute(
                "UPDATE period_close SET basis_json=?,result_head=?",
                (canonical_json(basis), _closing._head(basis, None, report)),
            )

    _tamper(store, damage)
    with pytest.raises(ContractError) as error:
        _snapshot(store, ledger)
    assert error.value.code == "storage_corrupt"
