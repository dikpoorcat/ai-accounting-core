"""Exact archived SQL for rejection tests; no current decoder or migration bridge."""

from dataclasses import replace
from pathlib import Path

from ai_accounting.kernel.contracts import Registry
from ai_accounting.kernel.development_contracts import load_development_contracts
from ai_accounting.kernel.runtime import initialize_file
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.versions import install_metadata, verify_schema

FINGERPRINTS = (
    "923584f720781cb28a369dd5b269537f64a4551034d306e9447bc2935d263861",
    "c9f9f7051bca67f1241ee5c89676fb9476bc819dc8f92c1a0c0bd1e940f459cb",
    "52556e8ec6cbbb81ab9ad9a69bb87897401471dba9c693e7b7ebddd2a5b08bf4",
    "0c8abb83f06fa66266ba5802f5d70cabed3b861015a977757e0538f14a8ee2e7",
)


def archived_company(path, fingerprint=FINGERPRINTS[0]):
    """Install declared archived objects without asserting historical content support."""
    from ai_accounting.kernel import schema_bundle

    active = production_bundle()
    contracts = load_development_contracts(
        Path(schema_bundle.__file__).with_name("schema_contracts") / "development/company",
        (fingerprint,),
        family=active.family,
        application_id=active.application_id,
    )
    contract = contracts["company"][fingerprint]
    archived = replace(
        active,
        registry=Registry(),
        contracts={"company": {0: contract}},
        # This exact metadata declaration permits checking the untouched oldest
        # shape, which predates schema_draft_history. It installs no decoder.
        company_verifiers={},
        development_contracts={"company": {fingerprint: contract}},
        draft_transitions={},
        steps=(),
    )

    def initialize(connection):
        connection.execute("BEGIN IMMEDIATE")
        for kind in ("table", "index", "trigger"):
            for item in contract["objects"]:
                if item["type"] == kind:
                    connection.execute(item["sql"])
        connection.execute("INSERT INTO state VALUES(1,0,0,0,1,0)")
        connection.execute("INSERT INTO source_change_head VALUES(1,0)")
        connection.execute(
            "INSERT INTO identity VALUES(1,?,?,?)",
            ("archived-synthetic-company", "913100000000000001", "archived-synthetic-database"),
        )
        install_metadata(connection, archived, "company")
        connection.commit()

    initialize_file(path, initialize, lambda connection: verify_schema(connection, bundle=archived))
    return path, archived
