"""Unchanged formal results remain real resources, without another calculation."""

from copy import deepcopy

import pytest
from test_adoption import BUSINESS, COMPANY, RECOVERY, adopt_declared, expense, refund, result
from test_adoption import store as store

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.ledger.public import create_ledger


def keep(reference, *, op="recalculate", template=None):
    change = (
        deepcopy(template)
        if template is not None
        else expense(seq=1, business_id=reference["business_id"])
    )
    change["businesses"][0].update(op=op, expected_seq=1)
    change["plans"] = []
    change["retained_results"] = [reference]
    return change


def candidate(ledger, unit, changes):
    return ledger.begin_candidate(
        [row for change in changes for row in change["businesses"]],
        unit,
        publications=[row for change in changes for row in change["publications"]],
        resource_uses=[],
        retained_results=[row for change in changes for row in change["retained_results"]],
    )


def test_clean_result_is_not_hydrated_replaced_or_retired_while_another_result_changes(
    store, monkeypatch
):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        unit.repository("application").business(RECOVERY)
        adopt_declared(ledger, [expense(), expense("50000", business_id=RECOVERY)], unit)
    before, other = result(store), result(store, RECOVERY)
    preserved = keep(before["reference"])
    changed = expense("40000", seq=1, business_id=RECOVERY)
    with store.write_unit(COMPANY) as unit:
        repository = unit.repository("ledger")
        read_result = repository.result

        def narrow(result_id):
            assert result_id != before["reference"]["result_id"], "未使用的保留正文不应被展开。"
            return read_result(result_id)

        monkeypatch.setattr(repository, "result", narrow)
        view = candidate(ledger, unit, [preserved, changed])
        (definition,) = view.resource_definitions(
            [{"business_id": BUSINESS, "slot": "primary"}], "obligation"
        )
        assert definition.value["amount_fen"] == "100000"
        view.stage(changed["plans"][0])
        view.complete()
        adopted = adopt_declared(ledger, [preserved, changed], unit)
        assert before["reference"] in adopted.results
        assert not adopted.retired_slots
    assert result(store) == before
    after = result(store, RECOVERY)
    assert after["target"]["values"]["amount_fen"] == "40000"
    assert after["reference"]["generation"] == other["reference"]["generation"] + 1
    assert after["reference"]["voucher_number"] == other["reference"]["voucher_number"]


def test_retained_publisher_is_ready_before_any_new_plan_is_staged(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
    before = result(store)
    with store.write_unit(COMPANY) as unit:
        view = candidate(ledger, unit, [keep(before["reference"])])
        selected = view.pre_tax("2026-01", "2026-01")
        assert selected.fields["profit_before_tax"]["amount_fen"] == "-100000"
        view.complete()
        adopted = adopt_declared(ledger, [keep(before["reference"])], unit)
        assert adopted.changed is False and adopted.results == (before["reference"],)


@pytest.mark.parametrize(
    "patch",
    [
        {"result_id": "10000000-0000-4000-8000-000000000099"},
        {"generation": 999},
        {"business_period": "2026-02"},
        {"posting_period": "2026-02"},
        {"state": "frozen"},
        {"voucher_number": 999},
    ],
)
def test_forged_or_old_reference_cannot_be_retained_by_candidate_or_adoption(store, patch):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
    before = result(store)
    change = keep({**before["reference"], **patch})
    with store.write_unit(COMPANY) as unit:
        with pytest.raises(ContractError) as error:
            candidate(ledger, unit, [change])
        assert error.value.code == "conflict"
    with pytest.raises(ContractError) as error:
        with store.write_unit(COMPANY) as unit:
            adopt_declared(ledger, [change], unit)
    assert error.value.code == "conflict"
    assert result(store) == before


@pytest.mark.parametrize("fault", ["duplicate", "changed_fact", "overlap", "missing"])
def test_complete_responsibility_cannot_hide_duplicate_or_missing_work(store, fault):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
    before = result(store)
    change = keep(before["reference"])
    if fault == "duplicate":
        change["retained_results"].append(deepcopy(before["reference"]))
    elif fault == "changed_fact":
        change["businesses"][0]["op"] = "replace"
    elif fault == "overlap":
        change["plans"] = expense(seq=1)["plans"]
    else:
        change["retained_results"] = []
    with pytest.raises(ContractError) as error:
        with store.write_unit(COMPANY) as unit:
            adopt_declared(ledger, [change], unit)
    assert error.value.code == "invalid_input"
    assert result(store) == before


def test_later_change_to_a_retained_head_rolls_back_the_whole_unit(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
    before = result(store)
    with pytest.raises(ContractError) as error:
        with store.write_unit(COMPANY) as unit:
            adopt_declared(ledger, [keep(before["reference"])], unit)
            adopt_declared(ledger, [expense("80000", seq=1)], unit)
    assert error.value.code == "conflict"
    assert result(store) == before


def test_same_unit_freeze_of_an_unchanged_retained_head_is_valid(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
    before = result(store)
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [keep(before["reference"])], unit)
        unit.repository("ledger").freeze_heads("2026-01")
    after = result(store)
    expected = deepcopy(before)
    expected["reference"]["state"] = "frozen"
    for voucher in expected["vouchers"]:
        voucher["result"]["state"] = "frozen"
    assert after == expected


@pytest.mark.parametrize("include_consumer", [False, True])
def test_changed_source_cannot_leave_an_open_consumer_using_stale_basis(store, include_consumer):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
        original = ledger.get_result_source(BUSINESS, "main", unit)
        unit.repository("application").business(RECOVERY, kind="operations.expense_recovery")
        adopt_declared(ledger, [refund(original)], unit)
    before, consumer = result(store), result(store, RECOVERY)
    changes = [expense("120000", seq=1)]
    if include_consumer:
        changes.append(keep(consumer["reference"], template=refund(original)))
    with pytest.raises(ContractError) as error:
        with store.write_unit(COMPANY) as unit:
            adopt_declared(ledger, changes, unit)
    assert error.value.code == "dependency_conflict"
    assert result(store) == before and result(store, RECOVERY) == consumer


def test_retained_current_reference_cannot_name_a_frozen_baseline_instead(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
        unit.repository("ledger").freeze_heads("2026-01")
    frozen = result(store)["reference"]
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense("80000", seq=1)], unit, correction_period="2026-02")
    current = result(store)
    with pytest.raises(ContractError) as error:
        with store.write_unit(COMPANY) as unit:
            candidate(ledger, unit, [keep(frozen)])
    assert error.value.code == "conflict"
    assert result(store) == current


def test_new_consumer_cannot_adopt_a_superseded_frozen_source(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
        old_source = ledger.get_result_source(BUSINESS, "main", unit)
        unit.repository("ledger").freeze_heads("2026-01")
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense("80000", seq=1)], unit, correction_period="2026-02")
    before = result(store)
    with pytest.raises(ContractError) as error:
        with store.write_unit(COMPANY) as unit:
            unit.repository("application").business(RECOVERY, kind="operations.expense_recovery")
            adopt_declared(ledger, [refund(old_source)], unit, correction_period="2026-02")
    assert error.value.code == "dependency_conflict"
    assert result(store) == before
    with store.read_unit(COMPANY) as context:
        assert ledger.get_results([RECOVERY], context) == ()


def test_unchanged_frozen_consumer_keeps_necessary_old_source_without_old_open_body(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
        original = ledger.get_result_source(BUSINESS, "main", unit)
        unit.repository("application").business(RECOVERY, kind="operations.expense_recovery")
        adopt_declared(ledger, [refund(original)], unit)
        unit.repository("ledger").freeze_heads("2026-01")
    consumer = result(store, RECOVERY)
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense("80000", seq=1)], unit, correction_period="2026-02")
        adopt_declared(
            ledger,
            [keep(consumer["reference"], template=refund(original))],
            unit,
        )
    assert result(store, RECOVERY) == consumer
