"""One backup meaning, independent of capture locations and SQLite maintenance bytes."""

from ai_accounting_modular.contracts.public import (
    BackupManifest,
    ContractError,
    content_digest,
    validate_contract,
)

DATABASE_PATH = "runtime/company.sqlite3"
CONTRACT_PATH = "contracts/company.json"
MANIFEST_PATH = "manifest.json"


def backup_scope_head(manifest: BackupManifest) -> str:
    validate_contract("core.schema.json", "BackupManifest", manifest)
    files = manifest["files"]
    for item in files:
        path = item["path"]
        if (
            path.startswith("/")
            or any(char in path for char in ("\\", ":", "\x00"))
            or any(part in {"", ".", ".."} for part in path.split("/"))
        ):
            raise ContractError("invalid_input", "备份清单位置必须是明确的包内相对路径。")
    names = [item["path"].casefold() for item in files]
    if len(names) != len(set(names)):
        raise ContractError("invalid_input", "备份清单不能含重复或大小写冲突的位置。")
    databases = [item for item in files if item["kind"] == "database"]
    if len(databases) != 1 or databases[0]["path"] != DATABASE_PATH:
        raise ContractError("invalid_input", "备份必须包含唯一固定位置的公司数据库。")
    return content_digest(
        {
            key: manifest[key]
            for key in (
                "format",
                "company_identity",
                "company_id",
                "database_id",
                "database_head",
                "structure_fingerprint",
                "document_set_id",
            )
        }
        | {
            "database": {"path": DATABASE_PATH, "kind": "database"},
            "files": sorted(
                (item for item in files if item["kind"] != "database"),
                key=lambda item: item["path"],
            ),
        }
    )
