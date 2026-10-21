"""Cutoff candidates are formal stock differences, never a replay of removed history."""

import sqlite3
from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest
import test_adoption
from test_adoption import (
    BUSINESS,
    COMPANY,
    PAYMENT,
    RECOVERY,
    _payment_at,
    adopt_declared,
    expense,
    publication,
    seed_payment,
)
from test_aggregations import at_period, journal
from test_read_positions import corrected_paid

from ai_accounting_modular.contracts.public import ContractError, QuerySelection
from ai_accounting_modular.ledger import _read_obligation_directory
from ai_accounting_modular.ledger.public import ObligationCutoffCandidates, create_ledger
from ai_accounting_modular.storage.public import CompanySpec
from tests.modular.acceptance.performance.ledger_variant import MODES, repository_factory

SECOND = "50000000-0000-4000-8000-000000000002"
THIRD = "50000000-0000-4000-8000-000000000008"
TRANSFER = "50000000-0000-4000-8000-000000000009"


@pytest.fixture(params=MODES)
def implementation(request, monkeypatch):
    factory = repository_factory(request.param)
    monkeypatch.setattr(test_adoption, "repository_factory", lambda: factory)


@pytest.fixture
def store(implementation, tmp_path, request):
    return test_adoption.store.__wrapped__(tmp_path, request)


def reference_candidates(repository, previous, cutoff):
    """Independent full endpoint selection, with no optimized range/slot prefilter."""
    endpoints = []
    for month in (previous, cutoff):
        table, where, args = repository.read_result_scope(
            {"period_from": month, "period_to": month}, (), QuerySelection("as_posted", month)
        )
        endpoints.append(
            {
                row["result_id"]: row["business_id"]
                for row in repository._session.fetchall(
                    "SELECT r.result_id,r.business_id FROM " + table + " WHERE " + where, args
                )
            }
        )
    changed = endpoints[0].keys() ^ endpoints[1].keys()
    owners = endpoints[0] | endpoints[1]
    result = set()
    for row in repository.children("obligation", changed):
        result.add(owners[row["result_id"]])
    for row in repository.children("settlement", changed):
        result.add(row["source_business_id"])
    for row in repository.children("obligation_reclassification", changed):
        result.update((row["source_business_id"], row["target_business_id"]))
    return tuple(sorted(result))


def check(ledger, read, previous, cutoff, expected):
    actual = ledger.obligation_cutoff_candidates(previous, cutoff, read)
    assert type(actual) is ObligationCutoffCandidates
    assert actual.previous_cutoff_period == previous
    assert actual.cutoff_period == cutoff
    assert actual.business_ids == tuple(sorted(expected))
    assert actual.business_ids == reference_candidates(read.repository("ledger"), previous, cutoff)
    return actual


def withdraw(business_id, seq=1):
    return {
        "domain": "money" if business_id == PAYMENT else "operations",
        "publications": [],
        "businesses": [
            {
                "business_id": business_id,
                "expected_seq": seq,
                "op": "withdraw",
                "natural_key": None,
                "complete_result_slots": [],
            }
        ],
        "retained_results": [],
        "plans": [],
        "dependencies": [],
        "issues": [],
    }


def positions(ledger, read, cutoff, ids=(BUSINESS,)):
    return {
        item.definition.reference["business_id"]: item
        for item in ledger.get_obligation_positions(
            cutoff, read, business_ids=ids, selection=QuerySelection("as_posted", cutoff)
        )
    }


def test_empty_and_quiet_ranges_return_detached_immutable_candidates(store):
    ledger = create_ledger()
    with store.read_unit(COMPANY) as read:
        actual = check(ledger, read, "2026-01", "2026-02", ())
        assert type(actual.business_ids) is tuple and not hasattr(actual, "__dict__")
        with pytest.raises(FrozenInstanceError):
            actual.business_ids = (BUSINESS,)
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
    with store.read_unit(COMPANY) as read:
        check(ledger, read, "2026-01", "2026-03", ())
        assert positions(ledger, read, "2026-01")[BUSINESS].amounts.remaining_fen == 100000
        assert positions(ledger, read, "2026-03")[BUSINESS].amounts.remaining_fen == 100000


def test_future_definition_and_payment_are_discovered_without_naming_the_payment_owner(store):
    ledger = create_ledger()
    seed_payment(store)
    with store.write_unit(COMPANY) as unit:
        for business in (SECOND, THIRD):
            unit.repository("application").business(business)
        adopt_declared(
            ledger,
            [
                expense(),
                at_period(expense("700", business_id=SECOND), "2026-02"),
                at_period(expense("600", business_id=THIRD), "2026-03"),
                _payment_at(period="2026-02"),
            ],
            unit,
        )
    with store.read_unit(COMPANY) as read:
        check(ledger, read, "2026-01", "2026-02", (BUSINESS, SECOND))
        assert positions(ledger, read, "2026-01")[BUSINESS].amounts.remaining_fen == 100000
        february = positions(ledger, read, "2026-02", (BUSINESS, SECOND))
        assert february[BUSINESS].amounts.remaining_fen == 0
        assert february[SECOND].amounts.remaining_fen == 700
        check(ledger, read, "2026-02", "2026-03", (THIRD,))


def test_candidates_only_use_the_callers_company_read_unit(store, tmp_path):
    ledger, other = create_ledger(), str(uuid4())
    store.create_company(
        CompanySpec(
            company_id=other,
            database_id=str(uuid4()),
            credit_code="913100000000000002",
            legal_name="另一家合成公司",
            start_period="2026-01",
            company_root=tmp_path / "other",
        )
    )
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [at_period(expense(), "2026-02")], unit)
    with store.write_unit(other) as unit:
        unit.repository("application").business(SECOND)
        unit.repository("identity").supplier()
        unit.repository("records").source()
        adopt_declared(ledger, [at_period(expense(business_id=SECOND), "2026-02")], unit)
    with store.read_unit(COMPANY) as read:
        check(ledger, read, "2026-01", "2026-02", (BUSINESS,))
    with store.read_unit(other) as read:
        check(ledger, read, "2026-01", "2026-02", (SECOND,))


def test_business_month_boundary_is_independent_of_the_adoption_month(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
        unit.repository("ledger").freeze_heads("2026-01")
    with store.write_unit(COMPANY) as unit:
        adopt_declared(
            ledger,
            [at_period(expense("90000", seq=1), "2026-03")],
            unit,
            correction_period="2026-02",
        )
    with store.read_unit(COMPANY) as read:
        check(ledger, read, "2026-01", "2026-02", (BUSINESS,))
        check(ledger, read, "2026-02", "2026-03", (BUSINESS,))
        assert positions(ledger, read, "2026-02") == {}
        march = positions(ledger, read, "2026-03")[BUSINESS]
        assert march.amounts.amount_fen == 90000


@pytest.mark.parametrize(
    "original,revised,expected",
    [
        ("2026-01", "2026-02", (BUSINESS,)),
        ("2026-02", "2026-01", ()),
    ],
)
def test_open_month_moves_compare_the_saved_endpoints_not_the_overwritten_generation(
    store, original, revised, expected
):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [at_period(expense(), original)], unit)
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [at_period(expense("80000", seq=1), revised)], unit)
    with store.read_unit(COMPANY) as read:
        check(ledger, read, "2026-01", "2026-02", expected)
        january = positions(ledger, read, "2026-01")
        assert (BUSINESS in january) == (revised == "2026-01")
        assert positions(ledger, read, "2026-02")[BUSINESS].amounts.amount_fen == 80000


def test_open_retirement_is_not_invented_as_a_retained_change_log(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [withdraw(BUSINESS)], unit)
    with store.read_unit(COMPANY) as read:
        check(ledger, read, "2026-01", "2026-02", ())
        assert positions(ledger, read, "2026-01") == positions(ledger, read, "2026-02") == {}


def test_frozen_payment_retirement_discovers_the_old_claim_which_reopens(store):
    ledger = create_ledger()
    seed_payment(store)
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense(), _payment_at()], unit)
        unit.repository("ledger").freeze_heads("2026-01")
    with store.write_unit(COMPANY) as unit:
        adopt_declared(
            ledger,
            [withdraw(PAYMENT)],
            unit,
            correction_period="2026-02",
            explanation="银行确认该笔付款没有实际发生",
        )
    with store.read_unit(COMPANY) as read:
        check(ledger, read, "2026-01", "2026-02", (BUSINESS,))
        assert positions(ledger, read, "2026-01")[BUSINESS].amounts.remaining_fen == 0
        assert positions(ledger, read, "2026-02")[BUSINESS].amounts.remaining_fen == 100000


def test_several_frozen_corrections_use_the_actual_two_endpoints(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
        unit.repository("ledger").freeze_heads("2026-01")
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense("90000", seq=1)], unit, correction_period="2026-02")
        unit.repository("ledger").freeze_heads("2026-02")
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense("80000", seq=2)], unit, correction_period="2026-03")
        unit.repository("ledger").freeze_heads("2026-03")
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense("70000", seq=3)], unit, correction_period="2026-04")
    with store.read_unit(COMPANY) as read:
        check(ledger, read, "2026-01", "2026-03", (BUSINESS,))
        check(ledger, read, "2026-02", "2026-03", (BUSINESS,))
        assert positions(ledger, read, "2026-01")[BUSINESS].amounts.amount_fen == 100000
        assert positions(ledger, read, "2026-03")[BUSINESS].amounts.amount_fen == 80000


def test_equal_amount_with_a_new_formal_source_is_still_a_candidate(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
        unit.repository("ledger").freeze_heads("2026-01")
    revised = expense(seq=1)
    revised["plans"][0]["obligations"][0]["allowed_settlements"] = ["money"]
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [revised], unit, correction_period="2026-02")
    with store.read_unit(COMPANY) as read:
        check(ledger, read, "2026-01", "2026-02", (BUSINESS,))
        january = positions(ledger, read, "2026-01")[BUSINESS]
        february = positions(ledger, read, "2026-02")[BUSINESS]
        assert january.amounts == february.amounts
        assert january.definition.source != february.definition.source


def test_only_the_changed_result_slot_contributes_and_unrelated_journals_do_not(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
    revised = expense(seq=1)
    auxiliary = at_period(expense("500"), "2026-02")["plans"][0]
    auxiliary["result_slot"] = auxiliary["target"]["result_slot"] = "auxiliary"
    auxiliary["basis"]["result_slot"] = "auxiliary"
    auxiliary["obligations"][0]["slot"] = "auxiliary"
    revised["plans"].append(auxiliary)
    revised["publications"].append(publication(BUSINESS, "2026-02", slot="auxiliary"))
    revised["businesses"][0]["complete_result_slots"].append("auxiliary")
    with store.write_unit(COMPANY) as unit:
        unit.repository("application").business(SECOND)
        adopt_declared(
            ledger,
            [revised, journal(SECOND, [("5602", 1), ("2202", -1)], period="2026-02")],
            unit,
        )
    with store.read_unit(COMPANY) as read:
        check(ledger, read, "2026-01", "2026-02", (BUSINESS,))
        values = ledger.get_obligation_positions(
            "2026-02",
            read,
            business_ids=(BUSINESS,),
            selection=QuerySelection("as_posted", "2026-02"),
        )
        assert {item.definition.reference["slot"]: item.amounts.amount_fen for item in values} == {
            "primary": 100000,
            "auxiliary": 500,
        }


def test_reclassified_excess_includes_the_real_payable_and_receivable_without_netting(store):
    ledger = corrected_paid(store)
    with store.read_unit(COMPANY) as read:
        check(ledger, read, "2026-01", "2026-02", (BUSINESS, RECOVERY))
        actual = positions(ledger, read, "2026-02", (BUSINESS, RECOVERY))
        assert actual[BUSINESS].amounts.remaining_fen == 0
        assert actual[BUSINESS].amounts.money_settled_fen == 100000
        assert actual[BUSINESS].amounts.excess_reclassified_fen == 20000
        assert actual[RECOVERY].amounts.remaining_fen == 20000


def test_frozen_transfer_replacement_keeps_both_old_and_new_target_businesses(store):
    ledger = create_ledger()

    def transfer(target, *, seq=0):
        change = journal(TRANSFER, [], period="2026-01", publishes=False)
        change["businesses"][0].update(expected_seq=seq, op="replace" if seq else "create")
        change["plans"][0]["obligation_reclassifications"] = [
            {
                "relation_id": str(uuid4()),
                "owner_business_id": TRANSFER,
                "source": {"business_id": BUSINESS, "slot": "primary"},
                "target": {"business_id": target, "slot": "primary"},
                "amount_fen": "30000",
                "mode": "transfer",
            }
        ]
        return change

    with store.write_unit(COMPANY) as unit:
        for business in (SECOND, THIRD, TRANSFER):
            unit.repository("application").business(business)
        adopt_declared(
            ledger,
            [
                expense(),
                expense("30000", business_id=SECOND),
                expense("30000", business_id=THIRD),
                transfer(SECOND),
            ],
            unit,
        )
        unit.repository("ledger").freeze_heads("2026-01")
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [transfer(THIRD, seq=1)], unit, correction_period="2026-02")
    with store.read_unit(COMPANY) as read:
        check(ledger, read, "2026-01", "2026-02", (BUSINESS, SECOND, THIRD))
        actual = positions(ledger, read, "2026-02")[BUSINESS]
        assert actual.amounts.transferred_out_fen == 30000
        assert actual.amounts.remaining_fen == 70000


def test_query_reads_only_selected_metadata_and_flat_resource_references(store, monkeypatch):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [at_period(expense(), "2026-02")], unit)
    with store.read_unit(COMPANY) as read:
        repository = read.repository("ledger")
        expected = reference_candidates(repository, "2026-01", "2026-02")
        original, statements = repository._session.fetchall, []
        connection, columns = read._connection, set()

        class ObservedConnection:
            def __getattr__(self, name):
                return getattr(connection, name)

            def set_authorizer(self, callback):
                def authorize(action, table, column, database, trigger):
                    if action == sqlite3.SQLITE_READ:
                        columns.add((table, column))
                        if column.endswith("_json"):
                            return sqlite3.SQLITE_DENY
                    return callback(action, table, column, database, trigger)

                connection.set_authorizer(authorize if callback is not None else None)

        def inspect(sql, args=()):
            statements.append(sql)
            return original(sql, args)

        monkeypatch.setattr(read, "_connection", ObservedConnection())
        monkeypatch.setattr(repository._session, "fetchall", inspect)
        monkeypatch.setattr(
            repository,
            "read_obligation_directory_rows",
            lambda *a, **k: pytest.fail("候选读取不应重算全量位置"),
        )
        monkeypatch.setattr(
            _read_obligation_directory, "_amounts", lambda *a: pytest.fail("候选口不算金额")
        )
        head = read.head
        actual = ledger.obligation_cutoff_candidates("2026-01", "2026-02", read)
        assert actual.business_ids == expected == (BUSINESS,)
        assert len(statements) == 1 and read.head == head
        assert {"result_id", "posting_period", "business_period"} <= {
            column for table, column in columns if table == "adopted_result"
        }
        assert all(not column.endswith("_json") for _, column in columns)
    with pytest.raises(ContractError, match="工作单元已结束"):
        ledger.obligation_cutoff_candidates("2026-01", "2026-02", read)


def test_invalid_saved_candidate_identity_is_not_silently_returned_as_an_empty_set(
    store, monkeypatch
):
    with store.read_unit(COMPANY) as read:
        monkeypatch.setattr(
            read.repository("ledger"),
            "read_obligation_cutoff_candidates",
            lambda *a: ("not-a-business-id",),
        )
        with pytest.raises(ContractError) as error:
            create_ledger().obligation_cutoff_candidates("2026-01", "2026-02", read)
    assert error.value.code == "storage_corrupt"


@pytest.mark.parametrize(
    "previous,cutoff",
    [
        ("2025-12", "2026-02"),
        ("2026-01", "2026-01"),
        ("2026-02", "2026-01"),
        ("2026-13", "2027-01"),
        (True, "2026-02"),
    ],
)
def test_invalid_bounds_are_rejected_before_querying_saved_results(
    store, monkeypatch, previous, cutoff
):
    with store.read_unit(COMPANY) as read:
        monkeypatch.setattr(
            read.repository("ledger")._session, "fetchall", lambda *a: pytest.fail("坏条件进入SQL")
        )
        with pytest.raises(ContractError) as error:
            create_ledger().obligation_cutoff_candidates(previous, cutoff, read)
        assert error.value.code == "invalid_input"
