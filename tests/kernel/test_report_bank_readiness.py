"""The report purpose retains actual bank and internal-transfer source checks."""

import pytest
from material_fixture import supporting_text
from test_materials import Company
from test_platform_material_dimensions import resolve_sides, setup_transfer
from test_report_material_readiness import checks

from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods
from ai_accounting.kernel.types import canonical


def non_tax_inventories(company, bank_sources):
    with company.engine.store.connection(read_only=True) as connection:
        originals = [company.engine.store.current_fact(connection, item["subject_id"])
                     .fact.evidence_digest for item in bank_sources]
    periods = Periods(company.engine)
    for category in MATERIAL_CATEGORIES:
        if category == "tax":
            continue
        evidence = originals if category == "bank" else (
            [company.proof] if category == "transactions" else []
        )
        periods.inventory(
            "2026-01", category, evidence=evidence, expected=len(evidence),
            no_business=category not in {"bank", "transactions"},
            confirmation_evidence=company.proof, request_id=company.request(),
        )


def same_bank_checks(company, *, summary):
    report, close = checks(company, summary=summary)
    assert sorted(map(canonical, report["issues"])) == sorted(
        canonical(item) for item in close["issues"] if item["field"] != "materials.tax"
    )
    assert any(item["field"] == "materials.tax" for item in close["issues"])
    assert any(item["field"] == "bank_reconciliation" and item["bank_account_id"] == "bank"
               for item in report["issues"])
    return report


@pytest.mark.parametrize("summary", [False, True])
def test_report_keeps_unreconciled_bank_and_both_internal_transfer_source_capacities(
    tmp_path, summary,
):
    company = Company(tmp_path)
    supporting_text(company.engine, company.proof)
    bank, platform, link, sign = setup_transfer(company)
    non_tax_inventories(company, (bank, platform))
    initial = same_bank_checks(company, summary=summary)
    assert {
        item["source_id"] for item in initial["materials"]["issues"]
        if item.get("code") == "material_item_unresolved"
    } == {bank["subject_id"], platform["subject_id"]}

    resolve_sides(company, bank, platform, link, sign)
    linked = same_bank_checks(company, summary=summary)
    assert not any(item.get("code") == "material_item_unresolved"
                   for item in linked["materials"]["issues"])

    duplicate, _ = company.source(
        b"name,amount,period\nrepeated-bank-outflow,-10,2026-01\n",
        subject="duplicate-bank-original", category="bank",
    )
    company.resolve(duplicate, "CSV!B2", [{
        **link, "amount_fen": -1000, "amount_field": "result.bank_amount_fen",
    }], subject="duplicate-bank-link")
    non_tax_inventories(company, (bank, platform, duplicate))
    overallocated = same_bank_checks(company, summary=summary)
    assert any(item.get("code") == "material_business_overallocated"
               and item["subject_id"] == "transfer"
               for item in overallocated["materials"]["issues"])
