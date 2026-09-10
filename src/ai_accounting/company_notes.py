"""One editable Markdown file per company; no interpretation or journal writes."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from .config import get_settings
from .path_security import ensure_directory_in_root, read_regular_file_in_root
from .taxpayer_identity import normalize_taxpayer_identification_number

EMPTY_HASH = hashlib.sha256(b"").hexdigest()


def notes_path(organization) -> Path:
    code = normalize_taxpayer_identification_number(organization.taxpayer_identification_number)
    return get_settings().finance_storage_dir / code / "业务说明.md"


def read_company_notes_bytes(organization) -> bytes | None:
    path = notes_path(organization)
    if not path.exists():
        return None
    _, content = read_regular_file_in_root(
        path, get_settings().finance_storage_dir, max_bytes=4_000_000
    )
    return content


def read_company_notes(organization) -> dict:
    path = notes_path(organization)
    raw = read_company_notes_bytes(organization)
    content = raw if raw is not None else b""
    return {
        "path": str(path.absolute()),
        "exists": raw is not None,
        "sha256": hashlib.sha256(content).hexdigest(),
        "content": content.decode("utf-8-sig"),
    }


def update_company_notes(organization, expected_sha256: str, content: str) -> dict:
    # AI callers compare-and-swap; a human edit is never silently overwritten.
    current = read_company_notes(organization)
    if (
        current["exists"]
        and current["sha256"] == hashlib.sha256(content.encode("utf-8")).hexdigest()
    ):
        return {"status": "updated", "company_notes": current, "idempotent_replay": True}
    if current["sha256"] != expected_sha256:
        return {"status": "rejected", "errors": ["COMPANY_NOTES_CHANGED"], "company_notes": current}
    path = notes_path(organization)
    ensure_directory_in_root(path.parent, get_settings().finance_storage_dir)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temp = Path(handle.name)
        handle.write(content.encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
    try:
        current = read_company_notes(organization)
        if current["sha256"] != expected_sha256:
            return {
                "status": "rejected",
                "errors": ["COMPANY_NOTES_CHANGED"],
                "company_notes": current,
            }
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return {"status": "updated", "company_notes": read_company_notes(organization)}


def ensure_company_notes(organization) -> dict:
    current = read_company_notes(organization)
    if not current["exists"]:
        return update_company_notes(
            organization,
            EMPTY_HASH,
            f"# {organization.name}业务说明\n\n## 长期规则\n\n## 按月确认\n\n## 待澄清事项\n",
        )["company_notes"]
    return current
