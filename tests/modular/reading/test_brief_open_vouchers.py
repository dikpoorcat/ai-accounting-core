"""One formal capture supplies exact effective journals to both cold and hot displays."""

from collections import Counter
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError, loads_json
from ai_accounting_modular.reading import (
    _brief_open,
    _brief_open_displays,
    _brief_open_sources,
)
from tests.modular.application._working_case import case as case
from tests.modular.domains.people.test_application import replacement
from tests.modular.domains.people.test_people import case as business_case
from tests.modular.domains.people.test_people import declaration, no_contributions
from tests.modular.domains.people.test_read_remuneration import add
from tests.modular.reading._support import source

from .test_brief_contents import scope
from .test_brief_open import apply, publish
from .test_brief_open_compact import simple_businesses
from .test_brief_open_incremental import matches_cold
from .test_brief_open_settlements import cooperation_due


def external_model(case, selected, read, monkeypatch):
    capture = _brief_open_displays.capture

    def external(*args, **kwargs):
        kwargs.pop("known_vouchers", None)
        return capture(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(_brief_open_displays, "capture", external)
        return _brief_open.prepare(case.kernel.reading, selected, read)


def forbidden(*args, **kwargs):
    pytest.fail("selected formal heads must not be sent through public voucher lookup again")


def test_five_kinds_and_duplicate_resources_reuse_one_capture_without_changing_packet(
    case, monkeypatch
):
    simple_businesses(case)
    reader, ledger = case.kernel.reading, case.kernel.ledger
    with case.kernel.storage.read_unit(case.company) as read:
        selected = scope(case)
        expected = external_model(case, selected, read, monkeypatch)
        captured = _brief_open_sources.capture(reader, selected, read)
        assert len(captured.selected) > len(captured.selected_vouchers)
        assert set(captured.selected_vouchers) == {
            item.source["result_id"] for item in captured.selected
        }
        monkeypatch.setattr(ledger, "get_effective_voucher_directory", forbidden)
        monkeypatch.setattr(ledger, "get_adopted_contents", forbidden)
        actual = _brief_open.prepare(reader, selected, read)
    assert actual == expected  # Exact header, hashes, every public and private row.


def test_hot_change_uses_the_same_binder_and_stays_cold_equal(case, monkeypatch):
    first = case.seed()
    publish(case)
    changed = deepcopy(first)
    changed["data"]["amount_fen"] = "90000"
    apply(case, mutations=[{"op": "replace", "expected_seq": 1, "fact": changed}])
    monkeypatch.setattr(case.kernel.ledger, "get_effective_voucher_directory", forbidden)
    assert case.kernel.reading.run_pending(case.company)
    matches_cold(case)


def test_selected_frozen_journal_is_not_replaced_by_its_future_current_adoption(case, monkeypatch):
    first = case.seed()
    assert case.close(case.check())["state"] == "closed"
    apply(case, case.expense("1", "2026-02"))
    changed = deepcopy(first)
    changed["data"]["amount_fen"] = "80000"
    apply(
        case,
        mutations=[{"op": "replace", "expected_seq": 1, "fact": changed}],
        correction_period="2026-02",
    )
    with case.kernel.storage.read_unit(case.company) as read:
        selected = scope(case)
        captured = _brief_open_sources.capture(case.kernel.reading, selected, read)
        posted = captured.selected[0]
        current = captured.current[_brief_open_sources.ref_key(posted.reference)]
        assert posted.source["result_id"] != current.source["result_id"]
        assert set(captured.selected_vouchers) == {posted.source["result_id"]}
        assert (
            captured.selected_vouchers[posted.source["result_id"]].result["result_id"]
            == posted.source["result_id"]
        )
        expected = external_model(case, selected, read, monkeypatch)
        monkeypatch.setattr(case.kernel.ledger, "get_effective_voucher_directory", forbidden)
        actual = _brief_open.prepare(case.kernel.reading, selected, read)
        value = next(
            loads_json(row[-1])["value"] for row in actual.rows if row[0] == "open_members"
        )
        assert (value["outstanding_fen"], value["current_outstanding_fen"]) == ("100000", "80000")
    assert actual == expected


def test_no_journal_is_a_known_none_not_an_absent_lookup(case, monkeypatch):
    package = {
        "business_id": str(uuid4()),
        "kind": "opening.package",
        "period": "2026-01",
        "evidence_ids": [case.source_id],
        "data": {
            "basis": "existing_ledger",
            "members": [
                {
                    "member_id": str(uuid4()),
                    "evidence_ids": [case.source_id],
                    "data": {
                        "type": "obligation",
                        "counterparty_id": case.party,
                        "nature": nature,
                        "amount_fen": "100",
                        "original_business_reference": nature,
                    },
                }
                for nature in ("other_payable", "other_receivable")
            ],
        },
    }
    apply(
        case,
        package,
        objects=[
            {
                "object_id": case.party,
                "kind": "organization",
                "name": "合成期初对象",
                "expected_seq": 0,
                "data": {},
            }
        ],
        sources=[source("opening-journal-none") | {"source_id": case.source_id}],
    )
    with case.kernel.storage.read_unit(case.company) as read:
        selected = scope(case)
        captured = _brief_open_sources.capture(case.kernel.reading, selected, read)
        assert len(captured.selected) == 2
        assert list(captured.selected_vouchers.values()) == [None]
        expected = external_model(case, selected, read, monkeypatch)
        monkeypatch.setattr(case.kernel.ledger, "get_effective_voucher_directory", forbidden)
        assert _brief_open.prepare(case.kernel.reading, selected, read) == expected


def test_inherited_journal_still_loads_its_exact_old_owner_origin_once(case, monkeypatch):
    case.seed(expense=False)
    wage = business_case("B17Z")
    add(case, no_contributions(), wage)
    with case.kernel.storage.read_unit(case.company) as read:
        initial = _brief_open_sources.capture(case.kernel.reading, scope(case), read)
        old = next(iter(initial.contents.values())).reference
    assert case.close(case.check())["state"] == "closed"
    declared, reference = declaration(wage, "0")
    wage["data"]["tax_basis"]["pooled_source"] = reference
    wire = replacement(case.company, wage)
    wire["correction_period"] = "2026-02"
    wire["mutations"].append({"op": "create", "expected_seq": 0, "fact": declared})
    assert case.app.accounting_apply(wire)["completion"] == "complete"
    with case.kernel.storage.read_unit(case.company) as read:
        selected = scope(case, "2026-02")
        expected = external_model(case, selected, read, monkeypatch)
        captured = _brief_open_sources.capture(case.kernel.reading, selected, read)
        assert old["result_id"] not in captured.contents
        assert {entry.result["result_id"] for entry in captured.selected_vouchers.values()} == {
            old["result_id"]
        }
        get, calls = case.kernel.ledger.get_obligation_origins, Counter()

        def observed(references, actual_read):
            calls.update(ref["result_id"] for ref in references)
            return get(references, actual_read)

        monkeypatch.setattr(case.kernel.ledger, "get_obligation_origins", observed)
        monkeypatch.setattr(case.kernel.ledger, "get_effective_voucher_directory", forbidden)
        actual = _brief_open.prepare(case.kernel.reading, selected, read)
        assert calls == {old["result_id"]: 1}
    assert actual == expected


def test_cooperation_extra_position_falls_back_without_requerying_the_ordinary_sources(
    case, monkeypatch
):
    cooperation_due(case)
    with case.kernel.storage.read_unit(case.company) as read:
        selected = scope(case)
        expected = external_model(case, selected, read, monkeypatch)
        captured = _brief_open_sources.capture(case.kernel.reading, selected, read)
        known = set(captured.selected_vouchers)
        get, calls = case.kernel.ledger.get_effective_voucher_directory, []

        def extra_only(references, actual_read):
            assert not known.intersection(ref["result_id"] for ref in references)
            calls.extend(references)
            return get(references, actual_read)

        monkeypatch.setattr(case.kernel.ledger, "get_effective_voucher_directory", extra_only)
        actual = _brief_open.prepare(case.kernel.reading, selected, read)
        assert calls and len({ref["result_id"] for ref in calls}) == 1
    assert actual == expected


@pytest.mark.parametrize("damage", ["none", "missing", "extra"])
def test_capture_rejects_an_incomplete_selected_voucher_mapping(case, monkeypatch, damage):
    case.seed()
    capture = case.kernel.ledger.capture_obligation_directory

    def damaged(query, read):
        value = capture(query, read)
        mapping = (
            None
            if damage == "none"
            else {}
            if damage == "missing"
            else {
                **value.selected_vouchers,
                str(uuid4()): None,
            }
        )
        return replace(value, selected_vouchers=mapping)

    monkeypatch.setattr(case.kernel.ledger, "capture_obligation_directory", damaged)
    with case.kernel.storage.read_unit(case.company) as read, pytest.raises(ContractError) as error:
        _brief_open_sources.capture(case.kernel.reading, scope(case), read)
    assert error.value.code == "storage_corrupt"


@pytest.mark.parametrize("change", ["generation", "boolean_generation", "period", "source"])
def test_reused_entries_still_require_the_exact_requested_ref_and_source(case, change):
    case.seed()
    with case.kernel.storage.read_unit(case.company) as read:
        captured = _brief_open_sources.capture(case.kernel.reading, scope(case), read)
        origin = next(iter(captured.contents.values()))
        ref, origins = dict(origin.reference), (origin,)
        if change == "source":
            origins = (replace(origin, source=origin.source | {"result_generation": 2}),)
        else:
            ref.update(
                {
                    "generation": {"generation": 2, "boolean_generation": True}.get(
                        change, ref["generation"]
                    ),
                    "business_period": "2026-02" if change == "period" else ref["business_period"],
                }
            )
        with pytest.raises(ContractError) as error:
            _brief_open_displays.capture(
                case.kernel.reading,
                [ref],
                read,
                known_origins=origins,
                known_vouchers=captured.selected_vouchers,
            )
        assert error.value.code == "storage_corrupt"
