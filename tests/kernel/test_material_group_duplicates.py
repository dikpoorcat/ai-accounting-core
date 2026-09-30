"""Duplicate originals select complete allocations, never arbitrary pool portions."""

import pytest
from entity_fixture import seed_fact_entities
from test_materials import Company, codes, csv_spec

from ai_accounting.kernel.domains.managed_reserve import ManagedReserveExpense


@pytest.fixture
def company(tmp_path):
    return Company(tmp_path)


def setup_pool(company, *, reserve=False):
    spec = csv_spec()
    spec["columns"][1]["funds_direction"] = "outflow"
    source, proof = company.source(
        b"name,amount,period\npool,61.00,2026-01\n", subject="original", spec=spec
    )
    links = [company.expense("first", 1100), company.expense("second", 5000)]
    if reserve:
        fact = ManagedReserveExpense(
            period="2026-01",
            actual_date="2026-01-10",
            amount_fen=5000,
            bank_account_id="bank",
        )
        seed_fact_entities(company.engine, fact)
        saved = company.engine.save_fact(
            fact.kind,
            "reserve",
            fact.model_dump(mode="json"),
            evidence=(company.proof,),
            expected_revision=0,
            request_id=company.request(),
        )
        preview = company.engine.preview(["reserve"])
        published = company.engine.confirm(
            ["reserve"],
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id=company.request(),
        )["results"][0]
        links[1] = links[1] | dict(
            subject_id="reserve",
            fact_kind=fact.kind,
            fact_id=saved["fact_id"],
            calculation_id=published["calculation_id"],
        )
    data = dict(
        period="2026-01",
        source_id="original",
        source_fact_id=source["fact_id"],
        members=[dict(location="CSV!B2", amount_fen=6100)],
        group_amount_fen=6100,
        links=links,
        joint_basis_confirmed=True,
        basis_evidence_digest=company.proof,
        basis_location="L1",
        reason="The complete original pool covers two confirmed costs.",
    )
    company.materials.resolve_group(
        "pool",
        data,
        evidence=(proof, company.proof),
        expected_revision=0,
        request_id=company.request(),
    )
    return data, links


def duplicate(company, links, *, amount="50.00", period="2026-01", direction=None, **changes):
    spec = csv_spec()
    if direction is not None:
        spec["columns"][1]["funds_direction"] = direction
    source, _ = company.source(
        f"name,amount,period\ncopy,{amount},{period}\n".encode(), subject="copy", spec=spec
    )
    company.resolve(
        source,
        "CSV!B2",
        links,
        subject="copy-resolution",
        treatment="duplicate",
        duplicate_source_id="original",
        duplicate_location="CSV!B2",
        reason="The original confirms the same complete allocation.",
        **changes,
    )
    return company.materials.check(period)


def test_complete_allocation_duplicate_does_not_consume_capacity_twice(company):
    _, links = setup_pool(company)
    result = duplicate(company, [links[1]])
    assert result["status"] == "complete", result["issues"]
    assert result["group_versions"]
    assert len(result["resolution_versions"]) == 1


def test_several_complete_allocations_can_cover_duplicate_original(company):
    _, links = setup_pool(company)
    result = duplicate(company, links, amount="61.00")
    assert result["status"] == "complete", result["issues"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("amount_fen", 4900),
        ("subject_id", "invented"),
        ("fact_id", "invented"),
        ("fact_kind", "payment"),
        ("calculation_id", "invented"),
        ("amount_field", "result.amount_fen"),
        ("recognition_period", "2026-02"),
    ],
)
def test_selector_must_match_all_seven_fields(company, field, value):
    _, links = setup_pool(company)
    result = duplicate(company, [links[1] | {field: value}])
    assert "material_duplicate_basis" in codes(result)


def test_repeated_selector_is_not_a_second_allocation(company):
    _, links = setup_pool(company)
    result = duplicate(company, [links[1], links[1]], amount="100.00")
    assert "material_duplicate_basis" in codes(result)


def test_sum_must_match_duplicate_original(company):
    _, links = setup_pool(company)
    assert "material_duplicate_amount" in codes(duplicate(company, [links[1]], amount="49.00"))


def test_duplicate_original_period_is_checked(company):
    _, links = setup_pool(company)
    assert "material_duplicate_period" in codes(duplicate(company, [links[1]], period="2026-02"))


def test_unselected_allocation_must_still_be_current(company):
    _, links = setup_pool(company)
    duplicate(company, [links[1]])
    company.expense("first", 1200, revision=1)
    assert "material_result_stale" in codes(company.materials.check("2026-01"))


def test_non_group_target_cannot_accept_selector(company):
    source, _ = company.source(subject="original")
    link = company.expense("second", 1000)
    company.resolve(source, "CSV!B2", [link])
    assert "material_duplicate_basis" in codes(duplicate(company, [link], amount="10.00"))


def test_empty_selector_keeps_whole_original_amount_check(company):
    setup_pool(company)
    assert "material_duplicate_amount" in codes(duplicate(company, []))


def test_empty_selector_can_still_duplicate_whole_group_row(company):
    setup_pool(company)
    result = duplicate(company, [], amount="61.00")
    assert result["status"] == "complete", result["issues"]


@pytest.mark.parametrize("direction", [None, "inflow", "outflow"])
def test_duplicate_original_direction_is_independently_checked(company, direction):
    _, links = setup_pool(company, reserve=True)
    result = duplicate(company, [links[1]], direction=direction)
    if direction == "outflow":
        assert result["status"] == "complete", result["issues"]
    else:
        assert "material_funds_direction_mismatch" in codes(result)


def test_empty_selector_duplicate_cycle_still_blocks(company):
    sources = {
        name: company.source(b"name,amount,period\ncopy,50.00,2026-01\n", subject=name)[0]
        for name in ("one", "two")
    }
    for name, target in (("one", "two"), ("two", "one")):
        company.resolve(
            sources[name],
            "CSV!B2",
            subject=name + "-resolution",
            treatment="duplicate",
            duplicate_source_id=target,
            duplicate_location="CSV!B2",
            reason="Duplicate copy",
        )
    assert "material_duplicate_cycle" in codes(company.materials.check("2026-01"))
