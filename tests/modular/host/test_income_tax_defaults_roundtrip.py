"""Profit defaults survive real standard-document rebuild and verified ZIP restore."""

import pytest

from ai_accounting_modular.host.public import create_offline_application
from ai_accounting_modular.records.public import open_standard_bundle
from tests.modular.application._working_case import case as case
from tests.modular.application.test_isolated_targets import read_target, source_bundle
from tests.modular.application.test_isolated_targets import request as isolated_request
from tests.modular.application.test_restore_proof import package
from tests.modular.domains.taxes._fixtures import income_inputs, policy, sale
from tests.modular.domains.taxes.test_application import request as tax_request


def tax_businesses(app, read, *facts):
    result = app.get_businesses(
        {
            "company_id": read.company_id,
            "business_ids": [fact["business_id"] for fact in facts],
            "include": ["facts", "results", "sources"],
        },
        read,
    )
    assert result["missing_business_ids"] == []
    return {row["fact"]["business_id"]: row for row in result["businesses"]}


def assert_adoption(businesses, assessed, basis, *, defaults):
    for fact in (assessed, basis):
        assert businesses[fact["business_id"]]["fact"] == fact
    if defaults:
        assert "method" not in businesses[assessed["business_id"]]["fact"]["data"]
        basis_fact = businesses[basis["business_id"]]["fact"]["data"]
        assert "collection_method" not in basis_fact
        assert "basis_source" not in basis_fact
    results = {
        fact["business_id"]: next(
            row
            for row in businesses[fact["business_id"]]["results"]
            if row["reference"]["result_slot"] == "main"
        )
        for fact in (assessed, basis)
    }
    tax = results[assessed["business_id"]]
    assert tax["target"]["values"]["profit_before_tax_fen"] == "80000"
    assert tax["target"]["values"]["cumulative_tax_fen"] == "4000"
    assert tax["basis"]["input"]["method"] == "computed_provision"
    assert tax["vouchers"][0]["lines"][0]["debit_fen"] == "4000"
    adopted = results[basis["business_id"]]["basis"]["input"]
    assert adopted["collection_method"] == "actual_profit"
    if defaults:
        assert adopted["collection_method_basis"] == "project_default"
    else:
        assert "collection_method_basis" not in adopted
    return results


@pytest.mark.parametrize("defaults", [True, False], ids=["project-defaults", "explicit-sourced"])
def test_income_tax_defaults_keep_original_facts_and_adoption_across_rebuild_and_restore(
    case, tmp_path, defaults
):
    case.seed(expense=False)
    assessed, basis, params = income_inputs()
    if defaults:
        assessed["data"].pop("method")
        basis["data"].pop("collection_method")
        basis["data"].pop("basis_source")
    # Real ledger revenue of 1,000 yuan less a 200 yuan expense produces 800 yuan profit.
    saved = case.app.accounting_apply(
        tax_request(
            case.company, sale(), policy(), case.expense("20000"), assessed, basis, params
        )
    )
    assert saved["completion"] == "complete", saved
    with case.kernel.storage.read_unit(case.company) as read:
        original = tax_businesses(case.app, read, assessed, basis)
    expected = assert_adoption(original, assessed, basis, defaults=defaults)
    metadata, bundle = source_bundle(case)
    exported = {row["business_id"]: row for row in bundle.facts}
    for fact in (assessed, basis):
        assert exported[fact["business_id"]] == fact

    archive = package(case, seed=False)
    offline = create_offline_application()
    with archive.open("rb") as stream, offline.backup_verifier.open(stream) as verified:
        assert verified.manifest["company_id"] == case.company
        assert verified.manifest["database_id"] == metadata.database_id

    for action, source in (("rebuild", bundle.manifest_path), ("restore", archive)):
        target = tmp_path / action
        delivered = offline.accounting_isolated(isolated_request(action, "run", source, target))
        assert delivered["state"] == "created", delivered
        with read_target(offline, target) as read:
            actual = assert_adoption(
                tax_businesses(offline, read, assessed, basis), assessed, basis, defaults=defaults
            )
            for identity in expected:
                assert actual[identity]["target"] == expected[identity]["target"]
                assert actual[identity]["basis"]["input"] == expected[identity]["basis"]["input"]
            if action == "restore":
                assert actual == expected
                assert (read.company_id, read.database_id) == (case.company, metadata.database_id)
        rebuilt = open_standard_bundle(target / "standard/current.json")
        delivered_facts = {row["business_id"]: row for row in rebuilt.facts}
        for fact in (assessed, basis):
            assert delivered_facts[fact["business_id"]] == fact
