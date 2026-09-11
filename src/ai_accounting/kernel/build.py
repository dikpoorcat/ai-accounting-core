"""Content identity of the controlled calculator build, captured at process start."""

import hashlib
from pathlib import Path

SHARED_SOURCE_FILES = (
    "borrowings.py",
    "fixed_assets.py",
    "intangible_assets.py",
    "fact_requirements.py",
    "mybank_export.py",
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
    files.extend((package / "kernel" / "migrations").glob("*.json"))
    files.extend(package / name for name in SHARED_SOURCE_FILES)
    hashed = hashlib.sha256()
    for path in sorted(files, key=lambda value: value.relative_to(package).as_posix()):
        hashed.update(path.relative_to(package).as_posix().encode())
        hashed.update(b"\0")
        hashed.update(path.read_bytes().replace(b"\r\n", b"\n"))
        hashed.update(b"\0")
    return "local-kernel-2:" + hashed.hexdigest()
