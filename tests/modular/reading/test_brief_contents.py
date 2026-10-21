"""A preparation passes exact verified bodies; effective journals keep their own owners."""

from collections import Counter
from copy import deepcopy
from dataclasses import replace

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.reading import (
    _brief_open,
    _brief_open_sources,
    _brief_vouchers,
    _monthly,
)
from ai_accounting_modular.reading._brief_activity_sources import Contents
from tests.modular.application._working_case import case as case
from tests.modular.domains.people.test_application import replacement
from tests.modular.domains.people.test_people import case as business_case
from tests.modular.domains.people.test_people import declaration, no_contributions
from tests.modular.domains.people.test_read_remuneration import add


def scope(case, month="2026-01"):
    return _monthly.normalize(
        {"company_id": case.company, "period_from": month, "period_to": month, "view": "as_posted"}
    )


def observe_contents(monkeypatch, ledger):
    get = ledger.get_adopted_contents
    calls = Counter()

    def observed(references, read):
        calls.update((ref["result_id"], ref["generation"]) for ref in references)
        return get(references, read)

    monkeypatch.setattr(ledger, "get_adopted_contents", observed)
    return calls


def test_simple_prepare_uses_only_origins_and_keeps_the_entire_full_origin_model_equal(
    case, monkeypatch
):
    expense = case.seed()
    ledger, reader = case.kernel.ledger, case.kernel.reading
    calls = observe_contents(monkeypatch, ledger)
    capture = ledger.capture_obligation_directory

    def complete_origins(query, read):
        selected = capture(query, read)
        return replace(
            selected,
            origins={
                body.reference["result_id"]: body
                for body in ledger.get_adopted_contents(
                    [source.reference for source in selected.sources], read
                )
            },
        )

    with case.kernel.storage.read_unit(case.company) as read:
        with monkeypatch.context() as patch:
            patch.setattr(ledger, "capture_obligation_directory", complete_origins)
            original = _brief_open.prepare(reader, scope(case), read)
        assert sorted(calls.values()) == [1]
        calls.clear()
        reused = _brief_open.prepare(reader, scope(case), read)
        assert reused == original  # Includes all rows, DTO fields, display head and content hashes.
        assert not calls

    changed = deepcopy(expense)
    changed["data"]["amount_fen"] = "90000"
    result = case.app.accounting_apply(
        case.request(mutations=[{"op": "replace", "expected_seq": 1, "fact": changed}])
    )
    assert result["completion"] == "complete", result
    calls.clear()
    with case.kernel.storage.read_unit(case.company) as read:
        newer = _brief_open.prepare(reader, scope(case), read)
    assert newer.content_digest != original.content_digest
    assert not calls


def test_inherited_voucher_loads_its_missing_frozen_body_and_never_uses_the_new_body(
    case, monkeypatch
):
    case.seed(expense=False)
    wage = business_case("B17Z")
    add(case, no_contributions(), wage)
    with case.kernel.storage.read_unit(case.company) as read:
        old = _brief_open_sources.capture(case.kernel.reading, scope(case), read)
    original_body = next(iter(old.contents.values()))
    assert case.close(case.check())["state"] == "closed"
    declared, reference = declaration(wage, "0")
    wage["data"]["tax_basis"]["pooled_source"] = reference
    wire = replacement(case.company, wage)
    wire["correction_period"] = "2026-02"
    wire["mutations"].append({"op": "create", "expected_seq": 0, "fact": declared})
    result = case.app.accounting_apply(wire)
    assert result["completion"] == "complete", result
    calls = observe_contents(monkeypatch, case.kernel.ledger)
    with case.kernel.storage.read_unit(case.company) as read:
        current = _brief_open_sources.capture(case.kernel.reading, scope(case, "2026-02"), read)
        refs = [item.reference for item in current.contents.values()]
        assert original_body.reference["result_id"] not in current.contents
        plain = _brief_vouchers.descriptors_for_references(case.kernel.reading, refs, read)
        calls.clear()
        actual = _brief_vouchers.descriptors_for_references(
            case.kernel.reading, refs, read, known_contents=current.contents.values()
        )
        assert actual == plain
        assert actual[0]["reference"]["result_id"] == original_body.reference["result_id"]
        assert calls == {
            (original_body.reference["result_id"], original_body.reference["generation"]): 1
        }
        header = _brief_vouchers.effective_headers(case.kernel.reading, refs, read)[0]
        frozen_body = case.kernel.ledger.get_adopted_contents((header.result,), read)[0]
        calls.clear()
        complete = _brief_vouchers.descriptors_for_references(
            case.kernel.reading,
            refs,
            read,
            known_contents=(*current.contents.values(), frozen_body),
        )
        assert complete == plain and not calls


def test_reuse_rejects_conflicting_exact_references_sources_and_bodies(case, monkeypatch):
    case.seed()
    with case.kernel.storage.read_unit(case.company) as read:
        captured = _brief_open_sources.capture(case.kernel.reading, scope(case), read)
        body = case.kernel.ledger.get_adopted_contents(
            [next(iter(captured.contents.values())).reference], read
        )[0]
        bad_target = deepcopy(body.target)
        bad_target["kind"] = "system.withdrawal"
        conflicts = (
            replace(body, reference=body.reference | {"business_period": "2026-02"}),
            replace(body, source=body.source | {"content_digest": "0" * 64}),
            replace(body, target=bad_target),
            replace(body, basis=body.basis | {"period": "2026-02"}),
        )
        for conflict in conflicts:
            cache = Contents(case.kernel.ledger, read)
            with pytest.raises(ContractError) as failure:
                cache.reuse((body, conflict))
            assert failure.value.code == "storage_corrupt"
        for conflict in (
            replace(body, source=body.source | {"result_generation": 2}),
            replace(body, reference=body.reference | {"generation": True}),
            replace(body, source=body.source | {"result_slot": "another"}),
        ):
            with pytest.raises(ContractError) as failure:
                Contents(case.kernel.ledger, read).reuse((conflict,))
            assert failure.value.code == "storage_corrupt"

        cache = Contents(case.kernel.ledger, read)
        cache.reuse((body,))
        monkeypatch.setattr(
            case.kernel.ledger,
            "get_adopted_sources",
            lambda *_args: pytest.fail("A conflicting known source must fail before any query"),
        )
        with pytest.raises(ContractError) as failure:
            cache.load_sources((body.source | {"content_digest": "0" * 64},))
        assert failure.value.code == "storage_corrupt"
