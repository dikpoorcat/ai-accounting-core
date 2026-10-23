"""Cooperation positions share the ordinary atomic Open patch, not its full capture."""

from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import (
    ReadInputs,
    canonical_json,
    loads_json,
)
from ai_accounting_modular.reading import (
    _brief_open_index as index,
)
from ai_accounting_modular.reading import (
    _brief_open_sources,
    _brief_open_update,
    _monthly,
)
from ai_accounting_modular.reading._collections import CollectionFilter
from ai_accounting_modular.reading.public import MonthScope
from tests.modular.acceptance.performance.ledger_variant import MODES, install_ledger_implementation
from tests.modular.application._working_case import case as company_fixture
from tests.modular.domains.financing._cooperation_support import PROJECT, collection, policy, sale
from tests.modular.domains.financing._support import BANK, COUNTERPARTY, OWNER
from tests.modular.domains.people.test_read_remuneration import bank_object
from tests.modular.reading._support import source

from .test_brief_open import apply, payment, publish
from .test_brief_open_incremental import matches_cold, scope, snapshot
from .test_brief_open_settlements import cooperation_due
from .test_funds import apply as apply_financing
from .test_funds_integrity import original_row, pair


@pytest.fixture(params=MODES)
def case(request, tmp_path):
    with install_ledger_implementation(request.param):
        yield from company_fixture.__wrapped__(tmp_path)


def ordinary_claims(case, count=140):
    values = [case.expense("100") for _ in range(count)]
    for value in values:
        value["data"]["creditor_id"] = COUNTERPARTY
    apply(case, *values)
    return values


def bounded_patch(case, monkeypatch):
    original, seen = _brief_open_sources.capture, []

    def bounded(reader, selected, read, *, business_ids=None, **options):
        assert business_ids is not None, "a cooperation position cannot force all ordinary claims"
        seen.extend(business_ids)
        return original(reader, selected, read, business_ids=business_ids, **options)

    with monkeypatch.context() as patch:
        patch.setattr(_brief_open_sources, "capture", bounded)
        assert case.kernel.reading.run_pending(case.company)
    assert snapshot(case) is not None
    fresh = matches_cold(case)
    with case.kernel.storage.read_unit(case.company) as read:
        repository = read.repository("reading")
        directory = index.base(
            repository, repository.state(_monthly.key(scope(case)), index.COMPONENT)
        )
        actual = directory.item_page("open_members", CollectionFilter(), None, 500)
        directory.check_many(actual)
        expected = loads_json(fresh.payload_json)["_collections"]["open_members"]
        assert len(actual) == expected["count"]
        assert directory.payload["_collections"]["open_members"] == expected
    return set(seen)


def private(case, collection, ids):
    with case.kernel.storage.read_unit(case.company) as read:
        repository = read.repository("reading")
        key = _monthly.key(scope(case))
        payload = loads_json(repository.payload(key, index.COMPONENT)["payload_json"])
        return index.Directory(repository, key, payload).get(collection, ids)


def test_ordinary_change_with_real_cooperation_keeps_other_claims_unloaded(case, monkeypatch):
    cooperation_due(case)
    claims = ordinary_claims(case)
    publish(case)
    changed = deepcopy(claims[0])
    changed["data"]["amount_fen"] = "90"
    apply(case, mutations=[{"op": "replace", "expected_seq": 1, "fact": changed}])
    seen = bounded_patch(case, monkeypatch)
    assert claims[0]["business_id"] in seen
    assert not seen.intersection(value["business_id"] for value in claims[1:])


def test_cooperation_agency_change_is_local_and_matches_full_members_and_directory(
    case, monkeypatch
):
    agreement, _, agency, _ = cooperation_due(case)
    claims = ordinary_claims(case)
    publish(case)
    changed = deepcopy(agency)
    changed["evidence_ids"] = [case.source_id]
    changed["data"].update(project_amount_fen="200", company_amount_fen="100")
    apply(case, mutations=[{"op": "replace", "expected_seq": 1, "fact": changed}])
    seen = bounded_patch(case, monkeypatch)
    assert not seen.intersection(value["business_id"] for value in claims)
    assert not private(
        case, "open_members", ["cooperation-position:" + agreement["business_id"] + ":payable"]
    )
    assert not private(case, index.OBJECTS, [OWNER, PROJECT])
    assert not private(case, index.BUSINESSES, ["@fixed"])


def test_actual_company_funding_change_replaces_only_its_position_and_related_claims(
    case, monkeypatch
):
    agreement, _, _, funded = cooperation_due(case)
    claims = ordinary_claims(case, 3)
    publish(case)
    changed = deepcopy(funded)
    changed["evidence_ids"] = [case.source_id]
    changed["data"]["amount_fen"] = "110"
    result = case.app.accounting_apply(
        case.request(mutations=[{"op": "replace", "expected_seq": 1, "fact": changed}])
    )
    assert result["completion"] == "complete", canonical_json(result)
    seen = bounded_patch(case, monkeypatch)
    assert not seen.intersection(value["business_id"] for value in claims)
    member = private(
        case, "open_members", ["cooperation-position:" + agreement["business_id"] + ":payable"]
    )
    assert next(iter(member.values()))["outstanding_fen"] == "10"


def test_actual_cooperation_collection_settles_the_selected_sale_without_reloading_other_claims(
    case, monkeypatch
):
    agreement, _, _, _ = cooperation_due(case)
    claims = ordinary_claims(case, 3)
    rule = policy()
    sold = sale(agreement, rule)
    sold["data"]["tax_point"]["source"]["source_id"] = case.source_id
    apply_financing(case, rule, sold)
    publish(case)
    apply_financing(case, collection(agreement, sold))
    seen = bounded_patch(case, monkeypatch)
    assert not seen.intersection(value["business_id"] for value in claims)
    assert not private(case, "open_members", [sold["business_id"] + ":primary"])


def test_frozen_position_current_change_does_not_reload_unrelated_ordinary_sources(
    case, monkeypatch
):
    agreement, charged, agency, funded = cooperation_due(case)
    claims = ordinary_claims(case, 3)
    apply_financing(
        case,
        *pair(BANK, [original_row(case, str(uuid4()), "100", "2026-01-03", "outflow")], [funded]),
    )
    assert case.close(case.check())["state"] == "closed"
    publish(case)
    agreement["data"]["company_revenue_expense_share"] = "0.4"
    charged["data"]["amount_fen"] = "120"
    agency["data"]["company_amount_fen"] = "96"
    for value in (agreement, charged, agency):
        value["evidence_ids"] = [case.source_id]
    apply(
        case,
        mutations=[
            {"op": "replace", "expected_seq": 1, "fact": value}
            for value in (agreement, charged, agency)
        ],
        correction_period="2026-02",
    )
    seen = bounded_patch(case, monkeypatch)
    assert not seen.intersection(value["business_id"] for value in claims)
    member = private(
        case, "open_members", ["cooperation-position:" + agreement["business_id"] + ":payable"]
    )
    value = next(iter(member.values()))
    assert value["outstanding_fen"] == "20" and value["current_outstanding_fen"] == "0"
    assert value["paid_fen"] is value["other_settled_fen"] is None


def test_opening_claim_payment_is_partial_but_actual_opening_change_still_rebuilds(
    case, monkeypatch
):
    opening = {
        "business_id": str(uuid4()),
        "kind": "opening.package",
        "period": "2026-01",
        "evidence_ids": [case.source_id],
    }
    bank = bank_object()
    liability = str(uuid4())
    opening["data"] = {
        "basis": "existing_ledger",
        "members": [
            {
                "member_id": str(uuid4()),
                "evidence_ids": [case.source_id],
                "data": {
                    "type": "bank",
                    "funds_account_id": bank["object_id"],
                    "balance_fen": "100",
                },
            },
            {
                "member_id": liability,
                "evidence_ids": [case.source_id],
                "data": {
                    "type": "obligation",
                    "counterparty_id": case.party,
                    "nature": "supplier_administration_payable",
                    "amount_fen": "100",
                    "original_business_reference": "合成期初维修应付款",
                },
            },
        ],
    }
    apply(
        case,
        opening,
        sources=[source("opening-partial-payment") | {"source_id": case.source_id}],
        objects=[
            bank,
            {
                "object_id": case.party,
                "kind": "organization",
                "name": "合成期初供应商",
                "expected_seq": 0,
                "data": {},
            },
        ],
    )
    publish(case)
    paid = payment(case, opening["business_id"], bank["object_id"], "40")
    paid["data"]["allocations"][0]["source"]["slot"] = "member:" + liability
    apply(case, paid)
    bounded_patch(case, monkeypatch)
    assert snapshot(case).components[index.COMPONENT]["open_items"]["payable_fen"] == "60"
    opening["data"]["members"][0]["data"]["balance_fen"] = "120"
    opening["data"]["members"][1]["data"]["amount_fen"] = "120"
    apply(case, mutations=[{"op": "replace", "expected_seq": 1, "fact": opening}])
    original, seen = _brief_open_sources.capture, []

    def recorded(reader, selected, read, *, business_ids=None, **options):
        seen.append(business_ids)
        return original(reader, selected, read, business_ids=business_ids, **options)

    with monkeypatch.context() as patch:
        patch.setattr(_brief_open_sources, "capture", recorded)
        assert case.kernel.reading.run_pending(case.company)
    assert seen == [None]
    matches_cold(case)


def test_cooperation_patch_keeps_a_later_commit_pending(case):
    _, _, agency, _ = cooperation_due(case)
    claims = ordinary_claims(case)
    publish(case)
    changed = deepcopy(agency)
    changed["evidence_ids"] = [case.source_id]
    changed["data"].update(project_amount_fen="200", company_amount_fen="100")
    apply(case, mutations=[{"op": "replace", "expected_seq": 1, "fact": changed}])
    key = _monthly.key(scope(case))
    with case.kernel.storage.read_unit(case.company) as read:
        state = read.repository("reading").state(key, _monthly.BRIEF_OPEN_ITEMS)
        prepared = _brief_open_update.prepare(case.kernel.reading, scope(case), read, state)
    revised = deepcopy(claims[0])
    revised["data"]["amount_fen"] = "80"
    apply(case, mutations=[{"op": "replace", "expected_seq": 1, "fact": revised}])
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        assert not unit.repository("reading").publish(
            key,
            _monthly.BRIEF_OPEN_ITEMS,
            state["wanted_head"],
            _monthly.version(_monthly.BRIEF_OPEN_ITEMS),
            prepared,
        )
    assert case.kernel.reading.run_pending(case.company)
    matches_cold(case)


def test_provider_current_fact_and_fixed_object_inputs_are_replaced_without_full_capture(
    case, monkeypatch
):
    _, _, agency, funded = cooperation_due(case)
    statements = pair(
        BANK, [original_row(case, str(uuid4()), "100", "2026-01-03", "outflow")], [funded]
    )
    apply_financing(case, *statements)
    statement = statements[0]
    statement["evidence_ids"] = [case.source_id]
    reader = case.kernel.reading
    original = reader.investment.cooperation_positions

    def provider(selected, read, *, contract_ids=()):
        actual = original(selected, read, contract_ids=contract_ids)
        if not any(value.company_due_fen or value.operator_due_fen for value in actual.positions):
            return actual
        # Exercise the provider's input contract with real current management
        # and identity reads; every cooperation amount is still the real owner's.
        value = reader.businesses.get_businesses(
            {
                "company_id": case.company,
                "business_ids": [statement["business_id"]],
                "include": ["facts"],
            },
            read,
        )["businesses"][0]["fact"]
        bank_id = value["data"]["funds_account_id"]
        assert reader.identity.get_batch((bank_id,), read)
        return replace(
            actual,
            inputs=actual.inputs.merge(
                ReadInputs(current_fact_ids=(statement["business_id"],), object_ids=(bank_id,))
            ),
        )

    monkeypatch.setattr(reader.investment, "cooperation_positions", provider)
    publish(case)
    statement["data"]["rows"][0]["memo"] = "合成原行用途补充"
    apply(case, mutations=[{"op": "replace", "expected_seq": 1, "fact": statement}])
    bounded_patch(case, monkeypatch)
    assert private(case, index.BUSINESSES, [statement["business_id"]])[statement["business_id"]][
        "fact"
    ]
    assert BANK in private(case, index.BUSINESSES, ["@fixed"])["@fixed"]["objects"]
    changed = deepcopy(agency)
    changed["evidence_ids"] = [case.source_id]
    changed["data"].update(project_amount_fen="200", company_amount_fen="100")
    apply(case, mutations=[{"op": "replace", "expected_seq": 1, "fact": changed}])
    bounded_patch(case, monkeypatch)
    assert not private(case, index.BUSINESSES, [statement["business_id"], "@fixed"])
    assert not private(case, index.OBJECTS, [BANK])


def test_only_open_format_upgrades_on_read_without_changing_other_ready_models(case):
    case.seed()
    publish(case)
    reader = case.kernel.reading
    month = MonthScope(case.company, "2026-01")
    reader.page("funds", month, {})
    assert reader.run_pending(case.company)
    funds = reader.page("funds", month, {})
    key = _monthly.key(scope(case))
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        repository = unit.repository("reading")
        repository._session.execute(
            "UPDATE read_scope_head SET format_version=? WHERE scope_key=? AND component_key=?",
            (index.FORMAT_VERSION - 1, key, index.COMPONENT),
        )
        head = unit.head
    result = _monthly.deliver(
        reader,
        scope(case),
        (index.COMPONENT,),
        lambda value, read: value.components[index.COMPONENT],
    )
    assert (result.state, result.reason) == ("preparing", None)
    assert reader.page("funds", month, {}) == funds
    assert reader.run_pending(case.company)
    assert snapshot(case) is not None
    assert reader.page("funds", month, {}) == funds
    with case.kernel.storage.read_unit(case.company) as read:
        assert read.head == head
        assert (
            read.repository("reading").state(key, index.COMPONENT)["format_version"]
            == index.FORMAT_VERSION
        )
