"""Explicit synthetic account manifests for existing draft-upgrade regression tests."""

from contextlib import closing

from ai_accounting.kernel import offline_development_upgrade as development
from ai_accounting.kernel.runtime import connect
from ai_accounting.kernel.types import digest


def synthetic_fund_manifest(connection):
    """Use the exact declared identity/proof convention of entity_fixture only."""
    result = []
    for entity in connection.execute(
        "SELECT id,account_type FROM entity WHERE kind='fund_account' ORDER BY id"
    ):
        entity_id, account_type = entity["id"], entity["account_type"]
        proof = digest(["synthetic fund identity", entity_id, account_type]).hex()
        assert connection.execute(
            "SELECT 1 FROM evidence WHERE digest=?", (bytes.fromhex(proof),)
        ).fetchone(), "synthetic fund identity requires its original test proof"
        number = str(int.from_bytes(digest(entity_id)[:8], "big"))
        if account_type == "bank":
            identity = {
                "bank_name": "合成银行",
                "bank_account_number": number,
                "account_holder_name": "合成公司",
            }
        elif account_type == "platform":
            identity = {
                "provider_name": "合成平台",
                "account_identifier": number,
                "account_holder_name": "合成公司",
            }
        else:
            identity = {"display_name": "合成现金账户 " + entity_id}
        result.append(
            {
                "entity_id": entity_id,
                "data": identity,
                "source": "synthetic test fixture",
                "evidence_digest": proof,
            }
        )
    return result


def upgrade_company(connection, bundle, **kwargs):
    return development.upgrade_company(
        connection, bundle, fund_accounts=synthetic_fund_manifest(connection), **kwargs
    )


def upgrade_root(root, *, bundle=None, **kwargs):
    inputs = {}
    with closing(connect(root / "catalog.sqlite", read_only=True)) as catalog:
        companies = list(catalog.execute("SELECT id,path FROM company ORDER BY id"))
    for company in companies:
        with closing(connect(company["path"], read_only=True)) as connection:
            inputs[company["id"]] = {"accounts": synthetic_fund_manifest(connection)}
    return development.upgrade_root(root, bundle=bundle, fund_account_inputs=inputs, **kwargs)
