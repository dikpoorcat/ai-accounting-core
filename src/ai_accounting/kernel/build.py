"""Content identity of the controlled calculator build, captured at process start."""

import hashlib
from pathlib import Path

SHARED_SOURCE_FILES = (
    "borrowings.py",
    "fixed_assets.py",
    "intangible_assets.py",
    "fact_requirements.py",
    "mybank_export.py",
    "policy_sources.py",
    "financial_statement_template.py",
    "bank_statements.py",
    "bank_statement_schemas.py",
)


def calculator_build_id():
    package = Path(__file__).resolve().parents[1]
    roots = (package / "kernel", package / "payroll")
    files = [path for root in roots for path in root.rglob("*.py")]
    files = [
        path
        for path in files
        if path.relative_to(package).as_posix() != "kernel/security/legacy.py"
    ]
    from .contract_files import production_contract_file_names

    contract_paths = {
        package / "kernel" / "schema_contracts" / name
        for name in production_contract_file_names()
    }
    files.extend(contract_paths)
    files.extend(package / name for name in SHARED_SOURCE_FILES)
    hashed = hashlib.sha256()
    for path in sorted(files, key=lambda value: value.relative_to(package).as_posix()):
        hashed.update(path.relative_to(package).as_posix().encode())
        hashed.update(b"\0")
        if path in contract_paths and not path.is_file():
            # Identity is not an installation trust gate. Exclusive release
            # export imports the engine before its declared contracts exist.
            # Normal bundle loading and packaging still reject missing files.
            hashed.update(b"missing-declared-contract\0")
            continue
        hashed.update(path.read_bytes().replace(b"\r\n", b"\n"))
        hashed.update(b"\0")
    return "local-kernel-2:" + hashed.hexdigest()
