from __future__ import annotations

import base64
import hashlib
import os
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .models import Evidence, Organization
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
        source_path = request.file_path.resolve(strict=True)
        size = source_path.stat().st_size
        if size > settings.finance_max_evidence_bytes:
            raise ValueError("evidence exceeds configured maximum size")
        content = source_path.read_bytes()
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
        return existing

    root = settings.finance_evidence_dir.resolve()
    destination = root / digest[:2] / digest[2:4] / digest
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        temporary = destination.with_name(f".{digest}.{uuid.uuid4().hex}.tmp")
        temporary.write_bytes(content)
        os.replace(temporary, destination)

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
