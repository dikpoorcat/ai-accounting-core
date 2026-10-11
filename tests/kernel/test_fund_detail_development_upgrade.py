"""Explicit fund identity inputs, atomic detail activation and source preservation."""

import hashlib
from contextlib import closing

import pytest
from test_development_upgrade import create_old_company, old_business

from ai_accounting.kernel import offline_development_upgrade as development
from ai_accounting.kernel.backup import verify_file
from ai_accounting.kernel.contracts import NeedsInformation
from ai_accounting.kernel.runtime import connect
from ai_accounting.kernel.types import canonical, digest
from ai_accounting.kernel.versions import verify_schema


def account_inputs(connection):
    """Use the exact synthetic account identities declared by entity_fixture."""
    result = []
    for row in connection.execute("SELECT id,account_type FROM entity WHERE kind='fund_account'"):
        number = str(int.from_bytes(digest(row["id"])[:8], "big"))
        identity = (
            {
                "bank_name": "合成银行",
                "bank_account_number": number,
                "account_holder_name": "合成公司",
            }
            if row["account_type"] == "bank"
            else {
                "provider_name": "合成平台",
                "account_identifier": number,
                "account_holder_name": "合成公司",
            }
            if row["account_type"] == "platform"
            else {"display_name": "合成现金账户 " + row["id"]}
        )
        proof = hashlib.sha256(
            canonical(["synthetic fund identity", row["id"], row["account_type"]]).encode()
        ).hexdigest()
        assert connection.execute(
            "SELECT 1 FROM evidence WHERE digest=?", (bytes.fromhex(proof),)
        ).fetchone()
        result.append(
            {
                "entity_id": row["id"],
                "data": identity,
                "source": "synthetic test fixture",
                "evidence_digest": proof,
            }
        )
    return result


def source_company(tmp_path):
    path, source, target = create_old_company(
        tmp_path / "company.sqlite",
        source_fingerprint=development.FUND_DETAIL_SOURCE_FINGERPRINT,
    )
    old_business(path, source)
    return path, source, target


def test_missing_identity_input_rolls_back_all_added_structure(tmp_path):
    path, source, target = source_company(tmp_path)
    with closing(connect(path)) as connection:
        before = development._all_existing_rows(connection, source.current())
        with pytest.raises(NeedsInformation):
            development.upgrade_company(connection, target)
        assert development._all_existing_rows(connection, source.current()) == before
        assert verify_schema(connection, bundle=source) == 0
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE name='fund_account_definition'"
            ).fetchone()
            is None
        )


def test_migration_does_not_infer_missing_identity_source(tmp_path):
    path, source, target = source_company(tmp_path)
    with closing(connect(path)) as connection:
        inputs = account_inputs(connection)
        inputs[0].pop("source")
        before = development._all_existing_rows(connection, source.current())
        with pytest.raises(NeedsInformation) as failure:
            development.upgrade_company(connection, target, fund_accounts=inputs)
        assert [issue["field"] for issue in failure.value.issues] == ["source"]
        assert verify_schema(connection, bundle=source) == 0
        assert development._all_existing_rows(connection, source.current()) == before


@pytest.mark.parametrize("point", ["after_read_model_ddl", "before_commit"])
def test_identity_and_line_initialization_are_atomic_and_retryable(tmp_path, point):
    path, source, target = source_company(tmp_path)
    with closing(connect(path)) as connection:
        inputs = account_inputs(connection)
        before = development._all_existing_rows(connection, source.current())

        def fail(boundary):
            if boundary == point:
                raise RuntimeError("synthetic funds upgrade failure")

        with pytest.raises(RuntimeError, match="synthetic funds upgrade failure"):
            development.upgrade_company(connection, target, fund_accounts=inputs, fault=fail)
        assert verify_schema(connection, bundle=source) == 0
        assert development._all_existing_rows(connection, source.current()) == before
        result = development.upgrade_company(connection, target, fund_accounts=inputs)
        assert result["status"] == "upgraded"
        assert result["original_rows_digest"] == before[0]
        assert connection.execute("SELECT count(*) FROM schema_draft_history").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM fund_line_detail").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM fund_account_definition").fetchone()[0] == 1
        assert development.upgrade_company(connection, target) == {"status": "verified_skip"}
    assert verify_file(path, _bundle=target)["verification"]["status"] == "verified"
