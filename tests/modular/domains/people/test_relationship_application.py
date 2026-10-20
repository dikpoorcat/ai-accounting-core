"""Real atomic fact/source/document management with payroll and closed amounts."""

from copy import deepcopy
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError, validate_contract
from ai_accounting_modular.domains.people.public import create_people
from ai_accounting_modular.records.public import open_standard_bundle
from tests.modular.application._working_case import case as case

from .test_application import assembled as assembled
from .test_application import get, replacement, request
from .test_people import PERSON, SOURCE, no_contributions
from .test_people import case as business_case
from .test_relationships import relationship


def _read(app, company, person_id=PERSON, period="2026-02"):
    with app.storage.read_unit(company) as read:
        return create_people().relationships([person_id], period, read)[0]


def _bundle(app, company):
    with app.storage.read_unit(company) as read:
        root = app.storage.company_metadata(read).company_root
    return open_standard_bundle(root / "standard/current.json")


def test_real_timeline_is_zero_result_sourced_findable_and_exported_in_its_archive_month(assembled):
    app, company = assembled
    fact = business_case("B46")
    submitted = request(company, fact)
    saved = app.accounting_apply(submitted)
    assert saved["completion"] == "complete", saved
    assert saved["changes"] == "committed" and saved["accounting"] == "unchanged"
    assert saved["businesses"][0]["results"] == []
    current = _read(app, company, fact["data"]["person_id"])
    assert current.employee_member is current.labor_member is True
    assert current.business_id == fact["business_id"]
    assert current.evidence_ids == tuple(fact["evidence_ids"])
    found = app.accounting_find(
        {
            "company_id": company,
            "family": "people",
            "period_from": "2026-05",
            "period_to": "2026-05",
            "object_id": fact["data"]["person_id"],
            "limit": 20,
        }
    )
    assert [item["business_id"] for item in found["items"]] == [fact["business_id"]]
    with app.storage.read_unit(company) as read:
        before = read.head
        assert app.ledger.get_results([fact["business_id"]], read) == ()
        assert read.repository("application").dependencies([fact["business_id"]]) == ()
    bundle = _bundle(app, company)
    assert bundle.facts == (fact,)
    assert {item["source_id"] for item in bundle.sources} == set(fact["evidence_ids"])
    repeated = app.accounting_apply({**submitted, "command_id": str(uuid4())})
    assert repeated["changes"] == "none" and repeated["accounting"] == "unchanged"
    assert _read(app, company, fact["data"]["person_id"]) == current
    with app.storage.read_unit(company) as read:
        assert read.head == before


def test_source_free_management_fact_is_allowed_and_does_not_establish_wages(assembled):
    app, company = assembled
    fact = relationship(("regular", "2026-02"))
    fact["evidence_ids"] = []
    fact["data"]["segments"][0]["relationship_start"] = True
    submitted = request(company, fact)
    submitted.pop("sources")
    saved = app.accounting_apply(submitted)
    assert saved["completion"] == "complete" and saved["accounting"] == "unchanged", saved
    current = _read(app, company)
    assert current.segments[0].relationship_start is True
    assert current.evidence_ids == ()
    bundle = _bundle(app, company)
    assert bundle.facts == (fact,) and bundle.sources == ()


def test_one_timeline_per_person_not_per_archive_month_and_atomic_failure(assembled):
    app, company = assembled
    first = relationship(("regular", "2026-01"), period="2026-01")
    duplicate = relationship(("labor", "2026-03"), period="2026-06")
    refused = app.accounting_apply(request(company, first, duplicate))
    assert refused["completion"] == "not_saved", refused
    assert get(app, company, first["business_id"], duplicate["business_id"])["businesses"] == []
    with app.storage.read_unit(company) as read:
        assert app.identity.get_batch([PERSON], read) == ()
        assert app.records.resolve_sources([SOURCE], read) == ()
    assert app.accounting_apply(request(company, first))["completion"] == "complete"
    refused = app.accounting_apply(request(company, duplicate))
    assert refused["completion"] == "not_saved"
    assert _read(app, company).business_id == first["business_id"]


@pytest.mark.parametrize("invalid", ["wrong_role", "globally_reversed", "false_start"])
def test_invalid_person_or_time_rolls_back_sources_objects_and_other_valid_business(
    assembled, invalid
):
    app, company = assembled
    fact = relationship(
        ("regular", "2026-01-20"), ("unpaid_leave", "2026-01"), ("departed", "2026-01-25")
    )
    if invalid == "globally_reversed":
        fact["data"]["segments"][-1]["effective_from"] = "2026-01-10"
    elif invalid == "false_start":
        fact["data"]["segments"][1]["relationship_start"] = True
    submitted = request(company, no_contributions(), business_case("B17Z"), fact)
    if invalid == "wrong_role":
        submitted["objects"][0]["kind"] = "organization"
    refused = app.accounting_apply(submitted)
    assert refused["completion"] == "not_saved", refused
    assert refused["fact_issues"]
    assert any(item["business_id"] == fact["business_id"] for item in refused["fact_issues"])
    assert (
        get(app, company, fact["business_id"], business_case("B17Z")["business_id"])["businesses"]
        == []
    )
    with app.storage.read_unit(company) as read:
        assert app.identity.get_batch([PERSON], read) == ()
        assert app.records.resolve_sources([SOURCE], read) == ()


def test_replace_withdraw_restore_and_before_commit_failure_keep_one_final_timeline(
    assembled, monkeypatch
):
    app, company = assembled
    first = relationship(("labor", "2026-01-01"), ("regular", "2026-02-01"))
    assert app.accounting_apply(request(company, first))["completion"] == "complete"
    before = _read(app, company)
    final = deepcopy(first)
    final["data"]["segments"][-1]["effective_from"] = "2026-03"
    owner = app.registry.owner("people.relationship")
    original = owner.apply_current

    def fail_after_writes(plan, unit):
        original(plan, unit)

        def fail():
            raise ContractError("dependency_conflict", "Synthetic final atomicity check")

        unit.check_before_commit(fail)

    with monkeypatch.context() as patch:
        patch.setattr(owner, "apply_current", fail_after_writes)
        refused = app.accounting_apply(replacement(company, final))
    assert refused["completion"] == "not_saved" and refused["error"] == "dependency_conflict", (
        refused
    )
    assert _read(app, company) == before
    assert _bundle(app, company).facts == (first,)
    saved = app.accounting_apply(replacement(company, final))
    assert saved["completion"] == "complete", saved
    assert _read(app, company).labor_member is True
    assert _read(app, company).employee_member is False
    final_segments = _read(app, company).segments
    assert _bundle(app, company).facts == (final,)
    withdrawn = app.accounting_apply(
        {
            **replacement(company, final),
            "mutations": [
                {"op": "withdraw", "business_id": final["business_id"], "expected_seq": 2}
            ],
        }
    )
    assert withdrawn["completion"] == "complete", withdrawn
    absent = _read(app, company)
    assert absent.business_id is None and absent.segments == ()
    assert absent.employee_member is absent.labor_member is None
    restored = app.accounting_apply(
        {
            **replacement(company, final),
            "mutations": [{"op": "restore", "expected_seq": 3, "fact": final}],
        }
    )
    assert restored["completion"] == "complete", restored
    assert _read(app, company).segments == final_segments
    assert _bundle(app, company).facts == (final,)


def test_absence_and_unrelated_changes_have_scope_specific_versions_and_bounded_read(assembled):
    app, company = assembled
    absent = _read(app, company)
    validate_contract("core.schema.json", "Digest", absent.version)
    unrelated = relationship(("departed", "2026-01"), person=str(uuid4()))
    assert app.accounting_apply(request(company, unrelated))["completion"] == "complete"
    assert _read(app, company) == absent
    fact = relationship(("regular", "2026-02-15"))
    assert app.accounting_apply(request(company, fact))["completion"] == "complete"
    current = _read(app, company)
    assert current.version != absent.version
    rename = request(company)
    rename.update(
        objects=[
            {
                "object_id": PERSON,
                "kind": "person",
                "name": "正确新姓名",
                "expected_seq": 1,
                "data": {},
            }
        ],
        sources=[],
    )
    assert app.accounting_apply(rename)["completion"] == "complete"
    assert _read(app, company) == current
    with app.storage.read_unit(company) as read:
        owner = create_people()
        assert owner.relationships([], "2026-02", read) == ()
        assert owner.relationships([PERSON, PERSON], "2026-02", read) == (current,)
        many = [PERSON, *[str(uuid4()) for _ in range(499)]]
        rows = owner.relationships(many, "2026-02", read)
        assert len(rows) == 500 and [row.person_id for row in rows] == many
        assert all(row.business_id is None for row in rows[1:])
        for ids in (PERSON, [str(uuid4()) for _ in range(501)], [False]):
            with pytest.raises(ContractError):
                owner.relationships(ids, "2026-02", read)
        with pytest.raises(ContractError):
            owner.relationships([PERSON], "2026-13", read)


def test_person_correction_moves_the_current_relation_in_the_same_unit(assembled):
    app, company = assembled
    fact = relationship(("regular", "2026-02"))
    assert app.accounting_apply(request(company, fact))["completion"] == "complete"
    final = deepcopy(fact)
    person = final["data"]["person_id"] = str(uuid4())
    changed = replacement(company, final)
    changed["objects"] = [
        {"object_id": person, "kind": "person", "name": "正确人员", "expected_seq": 0, "data": {}}
    ]
    assert app.accounting_apply(changed)["completion"] == "complete"
    assert _read(app, company).business_id is None
    assert _read(app, company, person).business_id == final["business_id"]
    with app.storage.read_unit(company) as read:
        relations = read.repository("application").relations([fact["business_id"]])
        assert [item["producer_id"] for item in relations] == [person]
        assert app.identity.get_batch([PERSON], read)
    bundle = _bundle(app, company)
    assert bundle.facts == (final,)


def test_closed_management_correction_keeps_wage_refs_and_frozen_amounts(case):
    book = case
    book.seed(expense=False)
    employment, wage = no_contributions(), business_case("B17Z")
    timeline = relationship(("departed", "2026-01"), period="2026-01")
    saved = book.app.accounting_apply(
        book.request(
            employment,
            wage,
            timeline,
            sources=request(book.company, wage)["sources"],
            objects=[
                {
                    "object_id": PERSON,
                    "kind": "person",
                    "name": "合成人员",
                    "expected_seq": 0,
                    "data": {},
                }
            ],
        )
    )
    assert saved["completion"] == "complete", saved
    original = book.get(wage["business_id"])["businesses"][0]["results"]
    assert original[0]["target"]["values"]["net_fen"] == "100000"
    checked = book.check()
    assert book.close(checked)["state"] == "closed"
    closed_check = book.check()
    open_result = original[0]
    original = book.get(wage["business_id"])["businesses"][0]["results"]
    assert original[0]["target"] == open_result["target"]
    for field in ("result_id", "generation", "voucher_id", "voucher_number"):
        assert original[0]["reference"][field] == open_result["reference"][field]
    with book.kernel.storage.read_unit(book.company, view="as_posted", as_of="2026-01") as read:
        frozen = book.app.ledger.get_results([wage["business_id"]], read)
    final = deepcopy(timeline)
    final["data"]["segments"] = [
        {"state": "regular", "effective_from": "2026-01", "relationship_start": True}
    ]
    changed = book.app.accounting_apply(replacement(book.company, final))
    assert changed["completion"] == "complete" and changed["accounting"] == "unchanged", changed
    assert book.get(wage["business_id"])["businesses"][0]["results"] == original
    with book.kernel.storage.read_unit(book.company, view="as_posted", as_of="2026-01") as read:
        assert book.app.ledger.get_results([wage["business_id"]], read) == frozen
        assert (
            create_people().relationships([PERSON], "2026-01", read)[0].period_end_state
            == "regular"
        )
        assert book.app.ledger.closed_through(read) == "2026-01"
        book.app.backup_verifier.verify_snapshot(read)
    assert book.check()["head"] == closed_check["head"]
    bundle = _bundle(book.app, book.company)
    assert (
        next(item for item in bundle.facts if item["business_id"] == timeline["business_id"])
        == final
    )
