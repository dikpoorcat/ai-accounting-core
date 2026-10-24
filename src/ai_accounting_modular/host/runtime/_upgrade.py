"""Offline structural upgrade, after a verified matching complete backup."""

from pathlib import Path

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.host.public import create_offline_application
from ai_accounting_modular.storage.public import (
    company_structure_contract,
    open_database_unit,
    upgrade_company_database,
    verify_database,
)


def run(company_directory: Path, backup: Path, expected_fingerprint: str):
    if not company_directory.is_absolute() or not backup.is_absolute():
        raise ContractError("invalid_path", "公司目录和备份须明确为绝对路径")
    root = company_directory.resolve()
    database = root / "runtime/company.sqlite3"
    archived = (company_structure_contract(version=version) for version in (1, 2))
    previous = next(
        (contract for contract in archived if contract["fingerprint"] == expected_fingerprint),
        None,
    )
    if previous is None:
        raise ContractError("unsupported_structure", "此命令仅接续明确封存的同体系公司结构。")
    # A repeated command may resume after the structure committed but final
    # verification was interrupted. Formal identities and the head still match.
    try:
        source = verify_database(database)
    except ContractError as error:
        if error.code != "structure_upgrade_required":
            raise
        source = verify_database(database, structure_version=previous["version"])
    application = create_offline_application()
    with backup.open("rb") as stream, application.backup_verifier.open(stream) as verified:
        manifest = verified.manifest
        if (source.company_id, source.database_id, source.head) != (
            manifest["company_id"],
            manifest["database_id"],
            manifest["database_head"],
        ) or manifest["structure_fingerprint"] != expected_fingerprint:
            raise ContractError("conflict", "备份不是这家公司当前账务版本的明确起始结构备份")
        upgraded = upgrade_company_database(
            database,
            expected_structure_fingerprint=expected_fingerprint,
            expected_snapshot=source,
        )
    with open_database_unit(
        database, company_root=root, repositories=application.repositories
    ) as read:
        application.backup_verifier.verify_snapshot(read)
    return {
        "status": "succeeded",
        "company_id": upgraded.company_id,
        "database_id": upgraded.database_id,
        "database_head": upgraded.head,
        "structure_version": company_structure_contract()["version"],
        "structure_fingerprint": upgraded.structure_fingerprint,
        "message": "结构升级及完整资料核验通过，可以继续使用原公司。",
    }
