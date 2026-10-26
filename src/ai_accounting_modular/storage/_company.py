"""Company creation and metadata need no catalog or owner login."""

from pathlib import Path

from ._database import _candidate, _company_identity, _identifier, _initialize, _stored_company
from ._schema import COMPANY_FORMAT, module_schema, packaged_schema
from ._session import StorageContext
from ._types import CompanyMetadata, CompanySpec


def spec_metadata(spec: CompanySpec) -> CompanyMetadata:
    _identifier(spec.company_id, "company_id")
    _identifier(spec.database_id, "database_id")
    _company_identity(spec.credit_code, spec.legal_name, spec.start_period)
    root = Path(spec.company_root).resolve()
    backup = (
        Path(spec.backup_directory).resolve()
        if spec.backup_directory is not None
        else root / "backups"
    )
    return CompanyMetadata(
        spec.company_id,
        spec.database_id,
        spec.credit_code,
        spec.legal_name,
        str(spec.start_period),
        root,
        root / "runtime/company.sqlite3",
        backup,
        "small_enterprise",
        "CNY",
    )


def create_database(metadata: CompanyMetadata):
    """Return the private publication receipt for catalog failure compensation."""

    def seed(connection):
        connection.execute(
            "INSERT INTO system_meta VALUES(1,?,?,?,?,0)",
            (
                COMPANY_FORMAT,
                metadata.database_id,
                packaged_schema("company")["version"],
                packaged_schema("company")["fingerprint"],
            ),
        )
        connection.execute(
            "INSERT INTO company_profile VALUES(1,?,?,?,?,?,?,1)",
            (
                metadata.company_id,
                metadata.credit_code,
                metadata.legal_name,
                metadata.accounting_standard,
                metadata.currency,
                metadata.start_period,
            ),
        )
        for owner in sorted({item["owner"] for item in packaged_schema("company")["objects"]}):
            contract = module_schema(owner)
            connection.execute(
                "INSERT INTO module_schema VALUES(?,?,?,?)",
                (
                    owner,
                    contract.version,
                    contract.contract_digest,
                    contract.sql_fingerprint,
                ),
            )

    with _candidate(metadata.database_path) as candidate:
        _initialize(candidate.path, "company", seed)
    return candidate.publication


def create_company_database(spec: CompanySpec) -> CompanyMetadata:
    """Create only the new company database; registration is a separate action."""
    metadata = spec_metadata(spec)
    create_database(metadata)
    return metadata


def company_metadata(context: StorageContext) -> CompanyMetadata:
    context._require_active()
    system, row = _stored_company(context._connection)
    location = context._location
    return CompanyMetadata(
        row["company_id"],
        system["database_id"],
        row["credit_code"],
        row["legal_name"],
        row["start_period"],
        Path(location["company_root"]),
        Path(location["database_path"]),
        Path(location["backup_directory"]),
        row["accounting_standard"],
        row["currency"],
        row["profile_seq"],
    )
