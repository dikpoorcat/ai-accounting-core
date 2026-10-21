"""Finite directory fields preserve complete page meanings without off-page body reads."""

from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError, validate_contract
from ai_accounting_modular.ledger.public import AdoptedOriginProjection
from ai_accounting_modular.reading import (
    _brief_open_displays,
    _brief_open_members,
    _brief_open_rows,
    _brief_open_sources,
    _brief_vouchers,
    _brief_vouchers_display,
)
from ai_accounting_modular.reading._brief_activity_sources import Contents
from tests.modular.application._working_case import case as case
from tests.modular.domains.operations.test_tax_lifecycle import policy, sale

from .test_brief_contents import scope
from .test_brief_open import apply, members, page, publish


def simple_businesses(case):
    expense = case.seed()
    ids = {"source": case.source_id, "party": case.party}
    rule = policy(ids)
    sold = sale(ids, rule, "2026-01")
    facts = [
        {
            "business_id": str(uuid4()),
            "kind": kind,
            "period": "2026-01",
            "evidence_ids": [case.source_id],
            "data": data,
        }
        for kind, data in (
            (
                "operations.sale_return",
                {
                    "sale_id": sold["business_id"],
                    "customer_id": case.party,
                    "gross_fen": "5050",
                    "credit_note_vat_fen": "50",
                },
            ),
            (
                "operations.expense_recovery",
                {
                    "expense_id": expense["business_id"],
                    "counterparty_id": case.party,
                    "amount_fen": "20000",
                },
            ),
            (
                "operations.pass_through",
                {"payer_id": case.party, "beneficiary_id": case.party, "amount_fen": "300"},
            ),
        )
    ]
    apply(case, rule, sold, *facts)
    return [expense, sold, *facts]


def test_all_five_origins_prepare_without_bodies_and_match_full_origin_displays(case, monkeypatch):
    facts = simple_businesses(case)
    reader, ledger = case.kernel.reading, case.kernel.ledger
    with case.kernel.storage.read_unit(case.company) as read:
        captured = _brief_open_sources.capture(reader, scope(case), read)
        assert {item.reference["business_id"] for item in captured.contents.values()} == {
            fact["business_id"] for fact in facts
        }
        assert all(isinstance(item, AdoptedOriginProjection) for item in captured.contents.values())
        refs = [item.reference for item in captured.contents.values()]
        full = ledger.get_adopted_contents(refs, read)
        expected = _brief_vouchers.descriptors_for_references(
            reader, refs, read, known_contents=full
        )
        actual = _brief_open_displays.capture(
            reader, refs, read, known_origins=captured.contents.values()
        )
        assert [item["display"] for item in actual] == [item["display"] for item in expected]
        assert [item["token"] for item in actual] == [
            item["template"]["voucher_version_id"] for item in expected
        ]
    with monkeypatch.context() as patch:
        patch.setattr(
            ledger,
            "get_adopted_contents",
            lambda *_args: pytest.fail("simple cold preparation must not hydrate any full body"),
        )
        snapshot = publish(case)
    seen = set()
    for group in page(case, snapshot)["collections"]["open_items"]["items"]:
        result = members(case, snapshot, group["group_key"])["collections"]
        seen.update(item["subject_id"] for item in result["members"]["items"])
        assert result["vouchers"]["items"]
    assert seen == {fact["business_id"] for fact in facts}


def test_compact_locator_is_exact_and_hydration_never_requeries_or_recalculates_positions(
    case, monkeypatch
):
    fact = case.seed()
    reader, ledger = case.kernel.reading, case.kernel.ledger
    with case.kernel.storage.read_unit(case.company) as read:
        captured = _brief_open_sources.capture(reader, scope(case), read)
        compact = _brief_open_rows.project(captured, case.company, reader.identity, read)
        member = compact[0]
        assert "_detail_scope" not in member and "recognition" not in member
        assert member["subject_id"] == fact["business_id"]
        assert set(member["_locator"]) == {
            "reference",
            "content_digest",
            "slot",
            "direction",
            "category",
            "business_period",
            "party_id",
        }
        body = next(iter(captured.contents.values()))
        assert _brief_open_rows.source(member["_locator"]) == body.source
        for name in (
            "capture_obligation_directory",
            "get_obligation_positions",
            "resource_definitions",
        ):
            monkeypatch.setattr(
                ledger, name, lambda *_args, **_kw: pytest.fail("page reads exact bodies only")
            )
        fixed = deepcopy(member)
        fixed.update(
            paid_fen="3",
            other_settled_fen="-1",
            outstanding_fen="99998",
            current_status="withdrawn",
            current_outstanding_fen="0",
        )
        hydrated, contents = _brief_open_members.hydrate(reader, [fixed], case.company, read)
        assert len(contents) == 1
        for key in (
            "source_amount_fen",
            "paid_fen",
            "other_settled_fen",
            "outstanding_fen",
            "current_status",
            "current_outstanding_fen",
        ):
            assert hydrated[0][key] == fixed[key]
        assert hydrated[0]["_detail_scope"]["source"] == body.source
        broken = deepcopy(member)
        broken["_locator"]["content_digest"] = "0" * 64
        with pytest.raises(ContractError, match="正式来源") as failure:
            _brief_open_members.hydrate(reader, [broken], case.company, read)
        assert failure.value.code == "storage_corrupt"


def test_display_projection_retains_actual_adopted_obligation_and_opening_parties(case):
    case.seed()
    reader = case.kernel.reading
    with case.kernel.storage.read_unit(case.company) as read:
        captured = _brief_open_sources.capture(reader, scope(case), read)
        projected = next(iter(captured.contents.values()))
        body = reader.ledger.get_adopted_contents([projected.reference], read)[0]
        first, second = str(uuid4()), str(uuid4())
        quantities = [
            {
                "type": "obligation",
                "source": {"business_id": str(uuid4()), "slot": "primary"},
                "account_code": "2202",
                "direction": "payable",
                "amount_fen": "1",
                "party_id": first,
            },
            {
                "type": "opening_balance",
                "member_id": str(uuid4()),
                "account_code": "2202",
                "debit_fen": "0",
                "credit_fen": "2",
                "party_id": second,
            },
            {
                "type": "balance",
                "funds_account_id": str(uuid4()),
                "balance_fen": "3",
                "as_of_period": "2026-01",
            },
        ]
        full = replace(body, basis=body.basis | {"adopted_quantities": quantities})
        light = replace(
            projected, basis_fields=projected.basis_fields | {"adopted_quantities": quantities}
        )
        validate_contract("business-facts.schema.json", "AdoptedBasis", full.basis)
        expected = tuple(sorted((case.party, first, second)))
        assert (
            _brief_vouchers_display.party_ids(full, (), Contents(reader.ledger, read)) == expected
        )
        assert (
            _brief_vouchers_display.party_ids(light, (), Contents(reader.ledger, read)) == expected
        )


@pytest.mark.parametrize("change", ["period", "source", "generation", "fields"])
def test_display_reuse_rejects_conflicting_exact_origin_identities(case, change):
    case.seed()
    reader = case.kernel.reading
    with case.kernel.storage.read_unit(case.company) as read:
        captured = _brief_open_sources.capture(reader, scope(case), read)
        origin = next(iter(captured.contents.values()))
        conflict = {
            "period": replace(origin, reference=origin.reference | {"business_period": "2026-02"}),
            "source": replace(origin, source=origin.source | {"content_digest": "0" * 64}),
            "generation": replace(origin, source=origin.source | {"result_generation": True}),
            "fields": replace(origin, basis_fields=origin.basis_fields | {"input": {}}),
        }[change]
        with pytest.raises(ContractError) as failure:
            _brief_open_displays.capture(
                reader, [origin.reference], read, known_origins=(origin, conflict)
            )
        assert failure.value.code == "storage_corrupt"
