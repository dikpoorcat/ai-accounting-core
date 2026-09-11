"""Declared nonnegative income/outgoing columns preserve original monetary values."""

import copy

import pytest
from test_materials import Company, codes
from test_platform_material_dimensions import group_data, setup_transfer

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.materials import Column, Specification, inspect_bytes
from ai_accounting.kernel.types import canonical, digest


def specification(direction):
    return Specification.model_validate_json(
        canonical(
            {
                "format": "csv",
                "columns": [
                    {"column": "A", "role": "context"},
                    {"column": "B", "role": "amount", "funds_direction": direction},
                    {"column": "C", "role": "recognition_period"},
                ],
            }
        )
    )


@pytest.mark.parametrize("direction", ["inflow", "outflow"])
def test_inspect_retains_raw_positive_and_exposes_declared_column_direction(direction):
    result = inspect_bytes(b"name,amount,period\nentry,10,2026-01\n", specification(direction))
    assert result["issues"] == []
    assert result["items"][0]["amount_fen"] == 1000
    assert result["items"][0]["funds_direction"] == direction


@pytest.mark.parametrize("direction", ["inflow", "outflow"])
def test_unsigned_column_negative_original_is_not_absorbed_as_positive(direction):
    result = inspect_bytes(b"name,amount,period\nentry,-10,2026-01\n", specification(direction))
    assert result["items"][0]["amount_fen"] == -1000
    assert "material_unsigned_amount_negative" in codes(result)


@pytest.mark.parametrize("role", ["context", "recognition_period"])
def test_nonmonetary_column_cannot_declare_funds_direction(role):
    with pytest.raises(ValueError, match="original amount column"):
        Column(column="A", role=role, funds_direction="outflow")


def test_undeclared_column_and_nested_specification_canonical_are_unchanged():
    column = {"column": "B", "role": "amount", "label": "original heading"}
    assert canonical(Column.model_validate(column).model_dump(mode="json")) == canonical(column)
    old = {
        "format": "csv",
        "columns": [column],
        "sheet_columns": {},
        "header_rows": {},
        "total_rows": {},
        "passages": [],
        "all_pages_reviewed": None,
    }
    parsed = Specification.model_validate_json(canonical(old))
    assert canonical(parsed.model_dump(mode="json")) == canonical(old)
    explicit_null = copy.deepcopy(old)
    explicit_null["columns"][0]["funds_direction"] = None
    assert (
        Specification.model_validate_json(canonical(explicit_null)).model_dump(mode="json") == old
    )
    data = inspect_bytes(b"context,amount\na,-10\n", parsed)
    assert data["items"][0]["amount_fen"] == -1000
    assert "funds_direction" not in data["items"][0]
    unchanged = Specification(
        format="csv",
        columns=(
            Column(column="A", role="context"),
            Column(column="B", role="amount"),
            Column(column="C", role="recognition_period"),
        ),
    )
    # Recorded from the previously verified 10fefb runtime, before this field
    # existed. This locks the complete old inspection shape, not just the amount.
    assert digest(inspect_bytes(b"context,amount,period\nbank,-10,2026-01\n", unchanged)).hex() == (
        "234303c4682aa916ad3668be7163b9b90d3e21627897627f9558dc0ad13b0f1b"
    )


@pytest.mark.parametrize("direction", ["bank_to_platform", "platform_to_bank"])
def test_separate_positive_bank_columns_cover_both_real_money_sides(tmp_path, direction):
    company = Company(tmp_path)
    bank, platform, link, sign = setup_transfer(company, direction, separate_bank_columns=True)
    used, unused = ("B", "C") if sign > 0 else ("C", "B")
    company.resolve(
        bank,
        f"CSV!{used}2",
        [{**link, "amount_fen": 1000, "amount_field": "result.bank_amount_fen"}],
        subject="bank-link",
    )
    company.resolve(
        bank,
        f"CSV!{unused}2",
        [],
        subject="zero-side",
        treatment="no_accounting",
        non_accounting_reason="zero_amount",
        reason="Original unused column is zero",
    )
    company.resolve(
        platform,
        "CSV!B2",
        [{**link, "amount_fen": -sign * 1000, "amount_field": "result.platform_amount_fen"}],
        subject="platform-link",
    )
    result = company.materials.check("2026-01")
    assert result["status"] == "complete", result["issues"]
    # Rebuild against the same frozen original with the field switched to the
    # opposite account side: same positive raw value cannot grant a second side.
    company.resolve(
        bank,
        f"CSV!{used}2",
        [{**link, "amount_fen": 1000, "amount_field": "result.platform_amount_fen"}],
        subject="bank-link",
        revision=1,
    )
    assert "material_funds_direction_mismatch" in codes(company.materials.check("2026-01"))


def test_unsigned_group_direction_is_reconstructed_from_current_source_version(tmp_path):
    company = Company(tmp_path)
    _, _, link, _ = setup_transfer(company)
    spec = specification("outflow").model_dump(mode="json")
    source, proof = company.source(
        b"name,amount,period\na,4,2026-01\nb,6,2026-01\n", subject="group-original", spec=spec
    )
    data = group_data(company, source, link, [400, 600], "result.bank_amount_fen")
    company.materials.resolve_group(
        "group",
        data,
        evidence=(proof, company.proof),
        expected_revision=0,
        request_id=company.request(),
    )
    assert "material_funds_direction_mismatch" not in codes(company.materials.check("2026-01"))
    with company.engine.store.connection(read_only=True) as connection:
        original = company.engine.store.current_fact(connection, "group-original")
    changed = original.fact.model_dump(mode="json")
    changed["specification"]["columns"][1]["funds_direction"] = "inflow"
    replacement = company.materials.receive(
        "group-original",
        changed,
        evidence=(proof, company.proof),
        expected_revision=1,
        request_id=company.request(),
    )
    assert any(
        item.get("source_id") == "group-original" and item["code"] == "material_item_unresolved"
        for item in company.materials.check("2026-01")["issues"]
    )
    data["source_fact_id"] = replacement["fact_id"]
    with pytest.raises(KernelError) as error:
        company.materials.resolve_group(
            "group",
            data,
            evidence=(proof, company.proof),
            expected_revision=1,
            request_id=company.request(),
        )
    assert error.value.code == "material_funds_direction_mismatch"


def test_group_positive_income_and_positive_outgoing_cannot_mix_into_one_side(tmp_path):
    company = Company(tmp_path)
    _, _, link, _ = setup_transfer(company)
    source, proof = company.source(
        b"outgoing,income,period\n4,6,2026-01\n",
        subject="mixed",
        spec={
            "format": "csv",
            "columns": [
                {"column": "A", "role": "amount", "funds_direction": "outflow"},
                {"column": "B", "role": "amount", "funds_direction": "inflow"},
                {"column": "C", "role": "recognition_period"},
            ],
        },
    )
    data = group_data(company, source, link, [400, 600], "result.bank_amount_fen")
    data["members"] = [
        {"location": "CSV!A2", "amount_fen": 400},
        {"location": "CSV!B2", "amount_fen": 600},
    ]
    with pytest.raises(KernelError) as error:
        company.materials.resolve_group(
            "group",
            data,
            evidence=(proof, company.proof),
            expected_revision=0,
            request_id=company.request(),
        )
    assert error.value.code == "material_funds_direction_mismatch"
