"""Explicit synthetic registries use real exact JSON contracts, never a verifier bypass."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from ai_accounting.kernel.catalog import catalog_sql
from ai_accounting.kernel.schema import schema_sql
from ai_accounting.kernel.schema_bundle import (
    APPLICATION_ID,
    load_bundle,
    verify_current_company,
)
from ai_accounting.kernel.versions import contract, fingerprint

TEST_FAMILY = "ai-accounting-kernel-test/2"


def full_contract(script, *, kind, version=0, status="draft", family=TEST_FAMILY):
    items = contract(script)
    return {
        "family": family,
        "kind": kind,
        "status": status,
        "version": version,
        "application_id": APPLICATION_ID,
        "objects": items,
        "sha256": fingerprint(items).hex(),
    }


def write_contract(directory, value):
    folder = Path(directory) / value["kind"]
    folder.mkdir(parents=True, exist_ok=True)
    name = "draft.json" if value["status"] == "draft" else f"v{value['version']}.json"
    (folder / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", "utf-8")


def test_bundle(registry):
    with TemporaryDirectory(prefix="kernel-test-contracts-") as directory:
        write_contract(directory, full_contract(schema_sql(registry), kind="company"))
        write_contract(directory, full_contract(catalog_sql(), kind="catalog"))
        return load_bundle(
            registry,
            directory,
            family=TEST_FAMILY,
            application_id=APPLICATION_ID,
            status="draft",
            current_versions={"company": 0, "catalog": 0},
            company_verifiers={0: verify_current_company},
        )


test_bundle.__test__ = False
