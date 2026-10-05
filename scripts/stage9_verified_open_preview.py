"""Read-only Stage 9 fixture verification and current open-month preview proof."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path


def preview_checkpoint(
    engine,
    *,
    checkpoint_path: Path,
    company: dict,
    snapshots: dict,
    period: str,
    source: Path,
    dimensions: dict | None = None,
):
    """Retain only exact construction metadata, never its saved result payloads."""
    raw = checkpoint_path.read_bytes()
    checkpoint = json.loads(raw.decode("utf-8"))
    expected_state = checkpoint.get("epochs")
    if (
        checkpoint.get("company") != company
        or checkpoint.get("snapshots") != snapshots
        or not isinstance(expected_state, list)
        or len(expected_state) != 6
        or any(type(value) is not int for value in expected_state)
        or period not in snapshots
        or snapshots[period].get("closed") is not False
        or not isinstance(snapshots[period].get("preview_digest"), str)
        or not snapshots[period]["preview_digest"]
        or not isinstance(snapshots[period].get("owner_confirmation"), str)
        or not snapshots[period]["owner_confirmation"]
        or engine.store.path.resolve(strict=True) != Path(company["path"]).resolve(strict=True)
    ):
        raise ValueError("Synthetic checkpoint identity or open snapshot changed")
    if dimensions is not None and any(
        checkpoint.get(field) != expected for field, expected in dimensions.items()
    ):
        raise ValueError("Synthetic checkpoint sample dimensions changed")
    expected_identity = {
        "company_id": company["id"],
        "taxpayer_id": company["taxpayer_id"],
        "database_id": company["database_id"],
    }
    # The fixture also saves every historical input/result for construction.
    # None is an input to production verification; retain only the checked
    # state and identity while the full database verifier builds its own proof.
    return {
        "state": expected_state,
        "identity": expected_identity,
        "checkpoint_path": checkpoint_path,
        "checkpoint_sha256": hashlib.sha256(raw).hexdigest(),
    }


def require_preview_state(connection, checkpoint):
    state = connection.execute("SELECT * FROM state WHERE id=1").fetchone()
    identity = connection.execute(
        "SELECT company_id,taxpayer_id,database_id FROM identity WHERE id=1"
    ).fetchone()
    if (
        state is None or list(state) != checkpoint["state"]
        or identity is None or dict(identity) != checkpoint["identity"]
    ):
        raise ValueError("Synthetic company state, repair revision or identity changed")
    with checkpoint["checkpoint_path"].open("rb") as handle:
        checkpoint_sha256 = hashlib.file_digest(handle, "sha256").hexdigest()
    if checkpoint_sha256 != checkpoint["checkpoint_sha256"]:
        raise ValueError("Synthetic checkpoint bytes changed during qualification")


def verify_registered_company(engine, checkpoint):
    """Build an independent proof with the installed registered company verifier."""
    from ai_accounting.kernel.versions import database_format

    started = time.perf_counter()
    bundle = engine.store.bundle
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        require_preview_state(connection, checkpoint)
        installed = database_format(connection, bundle=bundle)
        integrity = bundle.company_verifiers[installed["version"]](connection, bundle)
        require_preview_state(connection, checkpoint)
    if (
        integrity.get("status") != "verified" or integrity.get("limitations") != []
        or any(integrity.get("coverage", {}).get(section) != "verified" for section in (
            "sources", "historical_adoption", "projections", "read_indexes"
        ))
    ):
        raise ValueError("Synthetic company failed its registered content verifier")
    return {
        "integrity_contract": installed,
        "integrity": integrity,
        "integrity_ms": (time.perf_counter() - started) * 1000,
    }


def verify_book_open_preview(
    engine, *, checkpoint_path: Path, company: dict, snapshots: dict,
    period: str, source: Path, dimensions: dict | None = None, preview_close=None,
):
    """Keep construction digests intact; optionally dispatch in the resident app."""
    from ai_accounting.kernel.periods import Periods

    checkpoint = preview_checkpoint(
        engine, checkpoint_path=checkpoint_path, company=company, snapshots=snapshots,
        period=period, source=source, dimensions=dimensions,
    )
    verified = verify_registered_company(engine, checkpoint)
    expected_state = checkpoint["state"]

    # Periods.preview_close owns a second real read transaction. Exact state
    # and identity checks around it exclude an intervening business/repair write.
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        require_preview_state(connection, checkpoint)
    started = time.perf_counter()
    dispatch = preview_close or Periods(engine).preview_close
    preview = dispatch(period, owner_confirmation=snapshots[period]["owner_confirmation"])
    preview_ms = (time.perf_counter() - started) * 1000
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        require_preview_state(connection, checkpoint)
    if (
        preview.get("status") != "preview"
        or preview.get("manifest", {}).get("period") != period
        or preview.get("epochs")
        != dict(zip(("accounting", "material", "management"), expected_state[1:4], strict=True))
        or not isinstance(preview.get("digest"), str)
        or not preview["digest"]
    ):
        raise ValueError("Current synthetic preview differs from its checkpoint state")
    return {
        **verified,
        "preview_ms": preview_ms,
        "checkpoint_sha256": checkpoint["checkpoint_sha256"],
        "verified_open_preview": {
            "source": str(source.resolve(strict=True)),
            "company_id": company["id"],
            "database_id": company["database_id"],
            "period": period,
            "construction_preview_digest": snapshots[period]["preview_digest"],
            "digest": preview["digest"],
            "epochs": preview["epochs"],
            "state": expected_state,
        },
    }
