"""One reviewed, explicit company-v1 to company-v2 structural transition."""

import sqlite3
from importlib.resources import files
from pathlib import Path

from ai_accounting_modular.contracts.public import ContractError

from ._database import _connect, _database_error, _stored_company
from ._schema import module_schema, packaged_schema, previous_company_schema, verify_structure
from ._types import DatabaseSnapshot
from ._units import _active_writes


def _transition(connection):
    statement = ""
    for line in (
        files(__package__)
        .joinpath("schema", "company-v1-to-v2.sql")
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
    """Upgrade an offline, backed-up v1 database in place; never replay business.

    The caller stops its service and verifies its backup first. The original exact
    contract is checked again under the write lock. A failed transition rolls back
    the DDL, module records and data together. A completed retry changes nothing.
    """
    previous, current = previous_company_schema(), packaged_schema("company")
    if type(expected_structure_fingerprint) is not str or expected_structure_fingerprint not in {
        previous["fingerprint"],
        current["fingerprint"],
    }:
        raise ContractError("unsupported_structure", "升级仅接受明确的本体系起始结构合同")
    path = Path(path).resolve()
    if path in _active_writes.get():
        raise ContractError("invalid_operation", "不能在公司写事务中升级结构")
    connection = _connect(path, writable=True)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM system_meta WHERE singleton=1").fetchone()
        if row is None or row["structure_fingerprint"] not in {
            previous["fingerprint"],
            current["fingerprint"],
        }:
            raise ContractError("storage_corrupt", "数据库没有已知的起始结构")
        actual = current if row["structure_fingerprint"] == current["fingerprint"] else previous
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
        if actual is previous:
            if expected_structure_fingerprint != previous["fingerprint"]:
                raise ContractError("conflict", "数据库结构与本次明确的起始结构不一致")
            _transition(connection)
            connection.execute(
                "UPDATE system_meta SET structure_fingerprint=? WHERE singleton=1",
                (current["fingerprint"],),
            )
            for owner in ("records", "storage"):
                module = module_schema(owner)
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
