from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .models import Evidence, Organization
from .path_security import (
    PathSecurityError,
    ensure_directory_in_root,
    read_regular_file_in_root,
    write_new_regular_file_in_root,
)
from .schemas import RegisterEvidenceRequest


def register_evidence(
    session: Session,
    request: RegisterEvidenceRequest,
    settings: Settings | None = None,
) -> Evidence:
    settings = settings or get_settings()
    if session.get(Organization, request.org_id) is None:
        raise ValueError("ORGANIZATION_NOT_FOUND")

    if request.file_path is not None:
        import_root = settings.finance_evidence_import_dir or request.file_path.parent
        source_path, content = read_regular_file_in_root(
            request.file_path,
            import_root,
            max_bytes=settings.finance_max_evidence_bytes,
        )
        original_name = request.original_name or source_path.name
    else:
        try:
            content = base64.b64decode(request.content_base64 or "", validate=True)
        except ValueError as exc:
            raise ValueError("content_base64 is not valid base64") from exc
        if len(content) > settings.finance_max_evidence_bytes:
            raise ValueError("evidence exceeds configured maximum size")
        original_name = request.original_name or "attachment.bin"

    digest = hashlib.sha256(content).hexdigest()
    existing = session.scalar(
        select(Evidence).where(Evidence.org_id == request.org_id, Evidence.sha256 == digest)
    )
    if existing:
        _, stored_content = read_regular_file_in_root(
            Path(existing.storage_path),
            settings.finance_evidence_dir,
            max_bytes=settings.finance_max_evidence_bytes,
        )
        if hashlib.sha256(stored_content).hexdigest() != existing.sha256:
            raise PathSecurityError("EVIDENCE_CONTENT_ADDRESS_MISMATCH")
        return existing

    root = ensure_directory_in_root(settings.finance_evidence_dir, settings.finance_evidence_dir)
    destination = root / digest[:2] / digest[2:4] / digest
    destination_parent = ensure_directory_in_root(destination.parent, root)
    destination = destination_parent / digest
    destination = write_new_regular_file_in_root(
        destination,
        root,
        content,
        max_bytes=settings.finance_max_evidence_bytes,
    )
    destination, stored_content = read_regular_file_in_root(
        destination,
        root,
        max_bytes=settings.finance_max_evidence_bytes,
    )
    if hashlib.sha256(stored_content).hexdigest() != digest:
        raise PathSecurityError("EVIDENCE_CONTENT_ADDRESS_MISMATCH")

    evidence = Evidence(
        org_id=request.org_id,
        sha256=digest,
        original_name=original_name,
        media_type=request.media_type,
        source=request.source,
        size_bytes=len(content),
        storage_path=str(destination),
        metadata_json=request.metadata,
    )
    session.add(evidence)
    session.flush()
    return evidence
