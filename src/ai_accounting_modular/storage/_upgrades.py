"""Explicit forward company transitions preserve every retained source row."""

import sqlite3
from importlib.resources import files
from pathlib import Path

from ai_accounting_modular.contracts.public import ContractError

from ._database import _connect, _database_error, _stored_company
from ._schema import company_schema, module_schema, packaged_schema, verify_structure
from ._types import DatabaseSnapshot
from ._units import _active_writes

TRANSITIONS = {1: "company-v1-to-v2.sql", 2: "company-v2-to-v3.sql"}


def _transition(connection, version):
    statement = ""
    for line in (
        files(__package__)
        .joinpath("schema", TRANSITIONS[version])
        .read_text("utf-8")
        .splitlines(True)
    ):
        statement += line
        if sqlite3.complete_statement(statement):
            connection.execute(statement)
            statement = ""
    if statement.strip():
        raise RuntimeError("Packaged structure transition is incomplete")


def _integrity(connection):
    if (
        connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
        or connection.execute("PRAGMA foreign_key_check").fetchone() is not None
    ):
        raise ContractError("storage_corrupt", "数据库完整性检查失败，不能升级")


def upgrade_company_database(
    path: Path | str,
    *,
    expected_structure_fingerprint: str,
    expected_snapshot: DatabaseSnapshot | None = None,
) -> DatabaseSnapshot:
    """Upgrade an exact offline, backed-up known structure; never replay business.

    The caller stops its service and verifies its backup first. The original exact
    contract is checked again under the write lock. A failed transition rolls back
    the DDL, module records and data together. A completed retry changes nothing.
    """
    current = packaged_schema("company")
    contracts = {
        company_schema(version)["fingerprint"]: company_schema(version)
        for version in (*TRANSITIONS, current["version"])
    }
    if (
        type(expected_structure_fingerprint) is not str
        or expected_structure_fingerprint not in contracts
    ):
        raise ContractError("unsupported_structure", "升级仅接受明确的本体系起始结构合同")
    path = Path(path).resolve()
    if path in _active_writes.get():
        raise ContractError("invalid_operation", "不能在公司写事务中升级结构")
    connection = _connect(path, writable=True)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM system_meta WHERE singleton=1").fetchone()
        if row is None or row["structure_fingerprint"] not in contracts:
            raise ContractError("storage_corrupt", "数据库没有已知的起始结构")
        actual = contracts[row["structure_fingerprint"]]
        verify_structure(connection, "company", expected=actual)
        system, company = _stored_company(connection)
        before = (company["company_id"], system["database_id"], system["global_seq"])
        if expected_snapshot is not None and before != (
            expected_snapshot.company_id,
            expected_snapshot.database_id,
            expected_snapshot.head,
        ):
            raise ContractError("conflict", "公司身份或账务版本已变化，请重新核验备份")
        _integrity(connection)
        if actual is not current:
            if expected_structure_fingerprint != actual["fingerprint"]:
                raise ContractError("conflict", "数据库结构与本次明确的起始结构不一致")
            while actual["version"] < current["version"]:
                version = actual["version"]
                following = company_schema(version + 1)
                _transition(connection, version)
                connection.execute(
                    "UPDATE system_meta SET structure_fingerprint=? WHERE singleton=1",
                    (following["fingerprint"],),
                )
                for owner in sorted({item["owner"] for item in following["objects"]}):
                    module = module_schema(owner, structure_version=following["version"])
                    if module == module_schema(owner, structure_version=version):
                        continue
                    connection.execute(
                        "UPDATE module_schema SET version=?,contract_digest=?,parent_digest=?,"
                        "sql_fingerprint=? WHERE module_key=?",
                        (
                            module.version,
                            module.contract_digest,
                            module.parent_digest,
                            module.sql_fingerprint,
                            owner,
                        ),
                    )
                verify_structure(connection, "company", expected=following)
                actual = following
            verify_structure(connection, "company")
            _integrity(connection)
            system, company = _stored_company(connection)
            if before != (company["company_id"], system["database_id"], system["global_seq"]):
                raise ContractError("storage_corrupt", "结构升级不得改变公司身份或账务版本")
        connection.commit()
        return DatabaseSnapshot(path, *before, current["fingerprint"])
    except sqlite3.DatabaseError as error:
        connection.rollback()
        raise _database_error(error) from error
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
