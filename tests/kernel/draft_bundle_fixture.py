"""Explicit synthetic draft contracts, independent of the installed release."""

from schema_fixture import full_contract, write_contract

from ai_accounting.kernel import schema_bundle
from ai_accounting.kernel.catalog import catalog_sql
from ai_accounting.kernel.schema import schema_sql
from ai_accounting.kernel.service import default_registry


def synthetic_draft_bundle(directory):
    """Retain real typed rules and exact contracts without changing production."""
    registry = default_registry()
    company = full_contract(schema_sql(registry), kind="company", family=schema_bundle.FAMILY)
    catalog = full_contract(catalog_sql(), kind="catalog", family=schema_bundle.FAMILY)
    write_contract(directory, company)
    write_contract(directory, catalog)
    return schema_bundle.load_bundle(
        registry,
        directory,
        family=schema_bundle.FAMILY,
        application_id=schema_bundle.APPLICATION_ID,
        status="draft",
        current_versions={"company": 0, "catalog": 0},
        company_verifiers={0: schema_bundle.verify_current_company},
    )
