"""Private correction of one explicitly identified 50-person synthetic scenario.

This is not an accounting import or public batch API. The scenario defines
fictional employees; it does not claim a real owner supplied employment facts.
"""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.entities import Entities, _profile_record, require_entity
from ai_accounting.kernel.types import EvidenceDigest, canonical, digest

SCENARIO_SOURCE = (
    "Stage 9 synthetic workforce scenario v1: all 50 fictional employees are "
    "regularly employed from 2016-01-01; this is a test definition, not an owner confirmation."
)
EMPLOYMENT = {
    "active": True,
    "employment_status": "active",
    "employment_state": "regular",
    "employment_start": "2016-01-01",
}


class SyntheticIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    company_id: str = Field(min_length=1)
    taxpayer_id: str = Field(min_length=1)
    database_id: str = Field(min_length=1)


class EmployeeRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    entity_id: str = Field(min_length=1)
    expected_revision: int = Field(ge=1)
    profile_digest: EvidenceDigest


def current_employee_revisions(engine, employee_ids):
    """Bind the exact checked current profiles selected by the fixture checkpoint."""
    result = []
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        for entity_id in employee_ids:
            require_entity(connection, entity_id, kinds=("person",))
            row = connection.execute(
                "SELECT * FROM entity_profile_revision WHERE entity_id=? "
                "ORDER BY revision DESC LIMIT 1", (entity_id,),
            ).fetchone()
            profile = _profile_record(row)
            result.append({
                "entity_id": entity_id, "expected_revision": profile["revision"],
                "profile_digest": profile["digest"],
            })
    return result


def supplement_synthetic_employee_profiles(engine, members, *, identity, request_id):
    """Use one original kernel write, with normal model production at its end."""
    identity = SyntheticIdentity.model_validate(identity)
    selected = tuple(EmployeeRevision.model_validate(member) for member in members)
    if len(selected) != 50 or len({member.entity_id for member in selected}) != 50:
        raise ValueError("the synthetic workforce batch requires exactly 50 distinct employees")
    if (engine.store.company_id, engine.store.database_id) != (
        identity.company_id, identity.database_id,
    ):
        raise ValueError("synthetic workforce source identity differs from this engine")

    def operation(connection):
        stored = connection.execute(
            "SELECT company_id,taxpayer_id,database_id FROM identity WHERE id=1"
        ).fetchone()
        if dict(stored) != identity.model_dump():
            raise ValueError("synthetic workforce source identity differs from this database")
        results = []
        for index, member in enumerate(selected):
            require_entity(connection, member.entity_id, kinds=("person",))
            row = connection.execute(
                "SELECT * FROM entity_profile_revision WHERE entity_id=? "
                "ORDER BY revision DESC LIMIT 1", (member.entity_id,),
            ).fetchone()
            profile = _profile_record(row)
            if profile["revision"] != member.expected_revision:
                raise KernelError("entity_profile_version_conflict", "合成员工档案已变化")
            if profile["digest"] != member.profile_digest or profile["display_name"] != (
                f"合成员工{index + 1:03}"
            ):
                raise ValueError("synthetic employee source profile identity changed")
            data = json.loads(row["content"]) | EMPLOYMENT
            updated, evidence = Entities._profile(data, SCENARIO_SOURCE, None)
            results.append(Entities._insert_profile(
                connection, member.entity_id, member.expected_revision + 1,
                updated, SCENARIO_SOURCE, evidence,
            ))
        return {"status": "registered", "scenario_source": SCENARIO_SOURCE, "results": results}

    return engine._write(
        request_id,
        digest([
            "stage9_synthetic_employee_profiles", identity.model_dump(),
            [member.model_dump() for member in selected], EMPLOYMENT, SCENARIO_SOURCE,
        ]),
        None, ("management",), "stage9_synthetic_employee_profiles", operation,
    )


def accounting_source_fingerprint(connection):
    """Hash actual authoritative bytes, excluding the new management/audit rows."""
    tables = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name GLOB 'fact_*'"
        )
    } | {
        "identity", "calculation", "calculation_publication", "dependency_fact",
        "dependency_calculation", "dependency_scope", "voucher", "voucher_version",
        "voucher_line", "period_close", "asset_batch_member", "material_close_rule",
    }
    result = {}
    for table in sorted(tables):
        hashed, count = hashlib.sha256(), 0
        for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid'):
            encoded = canonical([
                ["blob", value.hex()] if isinstance(value, bytes) else ["value", value]
                for value in row
            ]).encode("utf-8")
            hashed.update(len(encoded).to_bytes(8, "big"))
            hashed.update(encoded)
            count += 1
        result[table] = {"rows": count, "sha256": hashed.hexdigest()}
    return {"tables": result, "sha256": digest(result).hex()}
