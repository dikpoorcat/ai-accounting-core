"""Authenticated, service-local intent for an exact active close preview.

This retains the owner conclusion, not the full manifest or its directories.
It is not a frozen storage contract or authority to close: approval and close
still reconstruct the complete manifest inside the company write transaction.
"""

import hmac
import json
from dataclasses import dataclass
from hashlib import sha256

from pydantic import TypeAdapter, ValidationError

from .close_review import PublicOwnerReview
from .contracts import KernelError
from .types import canonical

_PUBLIC_OWNER_REVIEW_ADAPTER = TypeAdapter(PublicOwnerReview)
_LANES = ("accounting", "material", "management")
_READ_VERSION = (*_LANES, "read_repair_revision")


def _invalid():
    raise KernelError(
        "content_integrity_failed",
        "活动关账预览内容或绑定不完整",
        component="close_preview",
        record_id="*",
        reason="invalid_active_close_intent",
    )


def require_public_owner_summary(value):
    """Validate the explicit public summary, never infer a full-review shape."""
    try:
        return _PUBLIC_OWNER_REVIEW_ADAPTER.validate_python(value)
    except ValidationError:
        _invalid()


def _object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate summary field")
        result[name] = value
    return result


@dataclass(frozen=True, slots=True)
class ClosePreviewIntent:
    company_id: str
    database_id: str
    period: str
    preview_digest: str
    epochs: tuple[int, int, int]
    read_version: tuple[int, int, int, int]
    owner_confirmation: str
    summary_bytes: bytes
    summary_sha256: bytes
    signature: bytes

    def epoch_values(self):
        return dict(zip(_LANES, self.epochs, strict=True))

    def read_version_values(self):
        return dict(zip(_READ_VERSION, self.read_version, strict=True))

    def _signed_bytes(self):
        # The final field binds the exact immutable summary bytes, not an
        # ordinary digest that an internal damaged entry could replace too.
        header = canonical(
            [
                self.company_id,
                self.database_id,
                self.period,
                self.preview_digest,
                self.epochs,
                self.read_version,
                self.owner_confirmation,
                self.summary_sha256.hex(),
            ]
        ).encode("utf-8")
        return header + b"\0" + self.summary_bytes

    def require(self, key, *, company_id, database_id, period, preview_digest):
        """Return a newly decoded summary only after exact binding authentication."""
        try:
            valid = (
                type(self.summary_bytes) is bytes
                and type(self.summary_sha256) is bytes
                and type(self.signature) is bytes
                and (self.company_id, self.database_id, self.period, self.preview_digest)
                == (company_id, database_id, period, preview_digest)
                and hmac.compare_digest(sha256(self.summary_bytes).digest(), self.summary_sha256)
                and hmac.compare_digest(
                    hmac.digest(key, self._signed_bytes(), "sha256"), self.signature
                )
            )
            if not valid:
                _invalid()
            value = json.loads(self.summary_bytes, object_pairs_hook=_object)
            review = require_public_owner_summary(value)
            if (
                review["period"] != period
                or canonical(review).encode("utf-8") != self.summary_bytes
            ):
                _invalid()
            return review
        except (TypeError, ValueError, AttributeError, ValidationError):
            _invalid()


def make_close_preview_intent(key, *, manifest, preview_digest, epochs, owner_confirmation):
    """Called only with the complete locally constructed preview, before disposal."""
    from dataclasses import replace

    from .close_contract import require_close_contract
    from .close_review import _public_owner_review
    from .types import digest

    require_close_contract(manifest)
    if (
        set(epochs) != set(_LANES)
        or any(type(value) is not int or value < 0 for value in epochs.values())
        or manifest["owner_confirmation"] != owner_confirmation
        or {name: manifest["read_version"][name] for name in _LANES} != epochs
        or digest([manifest, *(epochs[name] for name in _LANES)]).hex() != preview_digest
    ):
        _invalid()
    review = require_public_owner_summary(_public_owner_review(manifest["owner_review"]))
    if review["period"] != manifest["period"]:
        _invalid()
    summary_bytes = canonical(review).encode("utf-8")
    intent = ClosePreviewIntent(
        company_id=manifest["company_id"],
        database_id=manifest["database_id"],
        period=manifest["period"],
        preview_digest=preview_digest,
        epochs=tuple(epochs[name] for name in _LANES),
        read_version=tuple(manifest["read_version"][name] for name in _READ_VERSION),
        owner_confirmation=owner_confirmation,
        summary_bytes=summary_bytes,
        summary_sha256=sha256(summary_bytes).digest(),
        signature=b"",
    )
    return replace(intent, signature=hmac.digest(key, intent._signed_bytes(), "sha256"))
