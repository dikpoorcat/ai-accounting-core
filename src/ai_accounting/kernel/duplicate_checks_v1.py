"""Frozen v1 immutable duplicate-check reader for historical verification.

This module contains only persisted check decoding and source references.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .contracts import KernelError
from .duplicate_rules_v1 import ACTUAL_MONEY_KINDS, ELIGIBLE_KINDS
from .history_encoding_v1 import canonical, digest
from .stored_json_v1 import loads_unique

DUPLICATE_CONTRACT = "ai-accounting-kernel/2/business-duplicate"


DUPLICATE_CONTRACT_VERSION = 2


class SourceLocation(BaseModel):
    """An exact location already verified by the material-source reader."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    source_id: str = Field(min_length=1)
    source_fact_id: str = Field(min_length=1)
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    location: str = Field(min_length=1, max_length=500)


class ReviewBasis(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    kind: Literal[
        "distinct_material_location",
        "external_reference",
        "split_basis",
        "owner_confirmation",
        "shared_source",
    ]
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_id: str | None = None
    source_fact_id: str | None = None
    location: str | None = Field(default=None, min_length=1, max_length=500)
    reference: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def exact_basis(self):
        if self.kind == "distinct_material_location" and not all(
            (self.source_id, self.source_fact_id, self.location)
        ):
            raise ValueError("a distinct material location needs its exact current source")
        if self.kind == "external_reference" and self.reference is None:
            raise ValueError("an external-reference basis needs the exact reference")
        return self


class DuplicateReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    candidate_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    action: Literal["reuse_existing", "create_separate"]
    candidate_subject_id: str | None = None
    explanation: str = Field(min_length=1, max_length=4000)
    review_basis: tuple[ReviewBasis, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def selected_candidate(self):
        if self.action == "reuse_existing" and not self.candidate_subject_id:
            raise ValueError("reuse requires one selected existing business")
        return self


def _saved_content_digest(connection, fact_id: str) -> str:
    row = connection.execute(
        "SELECT subject_id,revision,digest FROM fact_revision WHERE id=?", (fact_id,)
    ).fetchone()
    if row is None:
        raise ValueError("missing duplicate candidate fact")
    evidence = [
        item[0].hex()
        for item in connection.execute(
            "SELECT evidence_digest FROM fact_evidence WHERE fact_id=? ORDER BY evidence_digest",
            (fact_id,),
        )
    ]
    return digest([row["subject_id"], row["revision"], row["digest"].hex(), evidence]).hex()


def _check_record_digest(
    *,
    check_id: str,
    proposed_subject_id: str,
    proposed_revision: int,
    proposed_digest: bytes,
    candidate_digest: bytes,
    action: str,
    result_fact_id: str | None,
    selected_fact_id: str | None,
    manifest: str,
    review_basis: str,
    explanation: str,
) -> bytes:
    return digest(
        {
            "id": check_id,
            "contract": DUPLICATE_CONTRACT,
            "contract_version": DUPLICATE_CONTRACT_VERSION,
            "proposed_subject_id": proposed_subject_id,
            "proposed_revision": proposed_revision,
            "proposed_digest": proposed_digest.hex(),
            "candidate_digest": candidate_digest.hex(),
            "action": action,
            "result_fact_id": result_fact_id,
            "selected_fact_id": selected_fact_id,
            "manifest": json.loads(manifest),
            "review_basis": json.loads(review_basis),
            "explanation": explanation,
        }
    )


def _require_check_record(row) -> tuple[dict, list[ReviewBasis]]:
    try:
        if (
            row["contract"] != DUPLICATE_CONTRACT
            or type(row["contract_version"]) is not int
            or row["contract_version"] != DUPLICATE_CONTRACT_VERSION
        ):
            raise ValueError("duplicate check contract")
        manifest = loads_unique(row["manifest"])
        bases = [ReviewBasis.model_validate(item) for item in loads_unique(row["review_basis"])]
        expected = _check_record_digest(
            check_id=row["id"],
            proposed_subject_id=row["proposed_subject_id"],
            proposed_revision=row["proposed_revision"],
            proposed_digest=row["proposed_digest"],
            candidate_digest=row["candidate_digest"],
            action=row["action"],
            result_fact_id=row["result_fact_id"],
            selected_fact_id=row["selected_fact_id"],
            manifest=row["manifest"],
            review_basis=row["review_basis"],
            explanation=row["explanation"],
        )
        if expected != row["record_digest"]:
            raise ValueError("duplicate check digest")
        return manifest, bases
    except (IndexError, KeyError, TypeError, ValueError, ValidationError) as exc:
        raise KernelError(
            "duplicate_review_corrupt",
            "疑似重复核对记录内容校验失败",
            check_id=row["id"],
        ) from exc


def _validate_source_locations(
    connection,
    source_locations: Sequence[SourceLocation],
    *,
    current: bool = True,
    _inspection_cache=None,
) -> None:
    from .material_inspection_v1 import Specification, inspect_bytes

    grouped = {}
    for item in source_locations:
        grouped.setdefault((item.source_id, item.source_fact_id, item.evidence_digest), set()).add(
            item.location
        )
    for (source_id, source_fact_id, evidence_digest), requested in grouped.items():
        current_join = "JOIN fact_current c ON c.fact_id=s.revision_id " if current else ""
        row = connection.execute(
            "SELECT s.evidence_digest,s.specification,e.content FROM fact_material_source_v2 s "
            + current_join
            + "JOIN fact_revision f ON f.id=s.revision_id "
            "JOIN evidence e ON e.digest=unhex(s.evidence_digest) "
            "WHERE f.subject_id=? AND f.id=?",
            (source_id, source_fact_id),
        ).fetchone()
        if row is None or row["evidence_digest"] != evidence_digest:
            raise KernelError(
                "duplicate_source_location_invalid",
                "疑似重复核对位置必须指向精确的已登记原件版本",
                source_id=source_id,
                source_fact_id=source_fact_id,
            )
        specification = Specification.model_validate_json(row["specification"])
        inspection = (
            inspect_bytes(row["content"], specification)
            if _inspection_cache is None
            else _inspection_cache.inspect(
                connection, source_fact_id, evidence_digest, specification, row["content"]
            )
        )
        present = {entry["location"] for entry in inspection["items"]}
        if not requested <= present:
            raise KernelError(
                "duplicate_source_location_invalid",
                "疑似重复核对位置不在已登记原件版本的验读明细中",
                source_id=source_id,
                source_fact_id=source_fact_id,
                location=next(iter(sorted(requested - present))),
            )


def _verify_candidate_materials(connection, candidate: Mapping[str, Any]) -> None:
    materials = candidate.get("material_sources", ())
    if not isinstance(materials, list):
        raise ValueError("candidate material sources")
    locations = []
    for item in materials:
        if not isinstance(item, dict):
            raise ValueError("candidate material source")
        location = SourceLocation.model_validate(
            {key: item[key] for key in SourceLocation.model_fields}
        )
        locations.append(location)
        resolution = item.get("resolution_fact_id")
        if resolution is None:
            continue
        fact_id = candidate.get("fact_id")
        if (
            fact_id is None
            or connection.execute(
                "SELECT 1 FROM fact_material_resolution_v2 r "
                "JOIN fact_material_resolution_v2_links l ON l.revision_id=r.revision_id "
                "WHERE r.revision_id=? AND r.source_id=? AND r.source_fact_id=? "
                "AND r.location=? AND l.fact_id=?",
                (
                    resolution,
                    location.source_id,
                    location.source_fact_id,
                    location.location,
                    fact_id,
                ),
            ).fetchone()
            is None
        ):
            raise ValueError("candidate material resolution")
    _validate_source_locations(connection, locations, current=False)


def _require_current_check_coverage(connection, fact_ids):
    """Every registered current business has its immutable check or correction ancestor."""
    identifiers = sorted(set(fact_ids))
    if not identifiers:
        return
    missing = connection.execute(
        "SELECT ids.value fact_id FROM json_each(?) ids "
        "WHERE NOT EXISTS(SELECT 1 FROM business_duplicate_check d "
        "WHERE d.result_fact_id=ids.value) "
        "AND NOT EXISTS("
        "WITH RECURSIVE ancestry(fact_id) AS ("
        "SELECT i.before_fact_id FROM fact_revision f "
        "JOIN identity_correction_item i ON i.subject_id=f.subject_id "
        "AND i.after_fact_id=f.id AND i.action='reassign' "
        "WHERE f.id=ids.value UNION ALL "
        "SELECT i.before_fact_id FROM ancestry a "
        "JOIN fact_revision f ON f.id=a.fact_id "
        "JOIN identity_correction_item i ON i.subject_id=f.subject_id "
        "AND i.after_fact_id=f.id AND i.action='reassign' "
        "WHERE NOT EXISTS(SELECT 1 FROM business_duplicate_check d "
        "WHERE d.result_fact_id=a.fact_id)) "
        "SELECT 1 FROM ancestry a JOIN business_duplicate_check d "
        "ON d.result_fact_id=a.fact_id) LIMIT 1",
        (canonical(identifiers),),
    ).fetchone()
    if missing is not None:
        raise KernelError(
            "duplicate_review_corrupt",
            "已登记业务缺少不可变疑似重复核对记录",
            fact_id=missing["fact_id"],
        )


def _pair_digest(proposed: dict, candidate: dict, signals: Sequence[dict]) -> str:
    sides = sorted(
        (
            {
                "subject_id": proposed["subject_id"],
                "revision": proposed["revision"],
                "content_digest": proposed["content_digest"],
                "material_sources": proposed.get("material_sources", []),
            },
            {
                "subject_id": candidate["subject_id"],
                "revision": candidate["revision"],
                "content_digest": candidate["content_digest"],
                "material_sources": candidate.get("material_sources", []),
            },
        ),
        key=canonical,
    )
    return digest([DUPLICATE_CONTRACT, DUPLICATE_CONTRACT_VERSION, sides, list(signals)]).hex()


def _candidate_digest(
    proposed: Mapping[str, Any],
    source_locations: Sequence[Mapping[str, Any]],
    strong: Sequence[Mapping[str, Any]],
) -> str:
    """Bind review to facts and signals, excluding generated IDs and publication state."""

    bound = [
        {
            "subject_id": item["subject_id"],
            "revision": item["revision"],
            "kind": item["kind"],
            "period": item["period"],
            "content_digest": item["content_digest"],
            "material_sources": item.get("material_sources", []),
            "signals": item["signals"],
            "pair_digest": item["pair_digest"],
        }
        for item in strong
    ]
    return digest(
        {
            "contract": DUPLICATE_CONTRACT,
            "version": DUPLICATE_CONTRACT_VERSION,
            "proposed": dict(proposed),
            "source_locations": list(source_locations),
            "strong": bound,
        }
    ).hex()


def _reuse_compatible(proposed: Mapping[str, Any], candidate: Mapping[str, Any]) -> bool:
    if proposed["kind"] == candidate["kind"]:
        return True
    return (
        proposed["kind"] in ACTUAL_MONEY_KINDS
        and candidate["kind"] in ACTUAL_MONEY_KINDS
        and any(
            signal.get("code") in {"same_exact_material_location", "same_complete_actual_money"}
            for signal in candidate.get("signals", ())
        )
    )


def _verify_review_evidence(
    connection,
    review: DuplicateReview,
    prepared: Mapping[str, Any],
    *,
    current: bool,
):
    proposed_locations = {canonical(item) for item in prepared.get("source_locations", ())}
    candidate_locations = {
        (location.get("evidence_digest"), location.get("location"))
        for candidate in prepared.get("strong_candidates", ())
        for location in candidate.get("material_sources", ())
    }
    for basis in review.review_basis:
        if (
            connection.execute(
                "SELECT 1 FROM evidence WHERE digest=?", (bytes.fromhex(basis.evidence_digest),)
            ).fetchone()
            is None
        ):
            raise KernelError("duplicate_review_evidence_missing", "重复核对引用的依据尚未登记")
        if basis.kind != "distinct_material_location":
            continue
        location = SourceLocation(
            source_id=basis.source_id,
            source_fact_id=basis.source_fact_id,
            evidence_digest=basis.evidence_digest,
            location=basis.location,
        )
        _validate_source_locations(connection, (location,), current=current)
        if canonical(location.model_dump(mode="json")) not in proposed_locations:
            raise KernelError(
                "duplicate_separation_basis_invalid",
                "不同原件位置须属于本次拟登记业务的已核验来源",
            )
        if (
            not candidate_locations
            or (
                basis.evidence_digest,
                basis.location,
            )
            in candidate_locations
        ):
            raise KernelError(
                "duplicate_separation_basis_invalid",
                "不同原件位置须能证明与强候选的已核验位置不同",
            )


def verify_duplicate_checks(connection) -> None:
    """Verify immutable review content without trusting its saved JSON summary."""

    current = connection.execute(
        "SELECT f.id FROM fact_current c JOIN fact_revision f ON f.id=c.fact_id "
        "JOIN subject s ON s.id=f.subject_id "
        "WHERE s.kind IN(SELECT value FROM json_each(?))",
        (canonical(sorted(ELIGIBLE_KINDS)),),
    )
    _require_current_check_coverage(connection, (row["id"] for row in current))

    required_manifest = {
        "candidate_contract",
        "candidate_version",
        "proposed",
        "source_locations",
        "strong_candidates",
        "candidate_digest",
    }
    for row in connection.execute("SELECT * FROM business_duplicate_check ORDER BY id"):
        try:
            manifest, bases = _require_check_record(row)
            if not isinstance(manifest, dict) or set(manifest) != required_manifest:
                raise ValueError("manifest shape")
            proposed = manifest["proposed"]
            locations = [
                SourceLocation.model_validate(item) for item in manifest["source_locations"]
            ]
            strong = manifest["strong_candidates"]
            if (
                manifest["candidate_contract"] != DUPLICATE_CONTRACT
                or manifest["candidate_version"] != DUPLICATE_CONTRACT_VERSION
                or proposed["subject_id"] != row["proposed_subject_id"]
                or proposed["revision"] != row["proposed_revision"]
                or bytes.fromhex(proposed["proposal_digest"]) != row["proposed_digest"]
                or manifest["candidate_digest"] != row["candidate_digest"].hex()
                or _candidate_digest(
                    proposed,
                    [item.model_dump(mode="json") for item in locations],
                    strong,
                )
                != manifest["candidate_digest"]
            ):
                raise ValueError("saved digest")
            if proposed.get("material_sources") != manifest["source_locations"]:
                raise ValueError("proposed material sources")
            if row["result_fact_id"] is not None and (
                _saved_content_digest(connection, row["result_fact_id"])
                != proposed["content_digest"]
            ):
                raise ValueError("proposed fact content")
            for candidate in strong:
                if candidate["pair_digest"] != _pair_digest(
                    proposed, candidate, candidate["signals"]
                ):
                    raise ValueError("pair digest")
                fact_id = candidate.get("fact_id")
                if fact_id is not None:
                    fact = connection.execute(
                        "SELECT subject_id,revision FROM fact_revision WHERE id=?", (fact_id,)
                    ).fetchone()
                    if fact is None or (fact["subject_id"], fact["revision"]) != (
                        candidate["subject_id"],
                        candidate["revision"],
                    ):
                        raise ValueError("candidate fact")
                    if _saved_content_digest(connection, fact_id) != candidate["content_digest"]:
                        raise ValueError("candidate fact content")
                _verify_candidate_materials(connection, candidate)
            if row["action"] == "clear" and (strong or bases):
                raise ValueError("clear review")
            if row["action"] != "clear" and not bases:
                raise ValueError("missing review basis")
            if row["action"] == "reuse_existing":
                selected = connection.execute(
                    "SELECT subject_id FROM fact_revision WHERE id=?", (row["selected_fact_id"],)
                ).fetchone()
                if selected is None or selected["subject_id"] not in {
                    item["subject_id"] for item in strong
                }:
                    raise ValueError("selected candidate")
                selected_candidate = next(
                    item for item in strong if item.get("fact_id") == row["selected_fact_id"]
                )
                if not _reuse_compatible(proposed, selected_candidate):
                    raise ValueError("incompatible reused candidate")
            if row["action"] == "create_separate" and not any(
                item.kind
                in {
                    "distinct_material_location",
                    "external_reference",
                    "split_basis",
                    "owner_confirmation",
                }
                for item in bases
            ):
                raise ValueError("separation basis")
            _verify_review_evidence(
                connection,
                DuplicateReview(
                    candidate_digest=manifest["candidate_digest"],
                    action=(
                        "reuse_existing" if row["action"] == "reuse_existing" else "create_separate"
                    ),
                    candidate_subject_id=(
                        next(
                            item["subject_id"]
                            for item in strong
                            if item.get("fact_id") == row["selected_fact_id"]
                        )
                        if row["action"] == "reuse_existing"
                        else None
                    ),
                    explanation=row["explanation"],
                    review_basis=tuple(bases),
                ),
                manifest,
                current=False,
            ) if row["action"] != "clear" else None
        except (KeyError, StopIteration, TypeError, ValueError, ValidationError) as exc:
            raise KernelError(
                "duplicate_review_corrupt",
                "疑似重复核对记录内容校验失败",
                check_id=row["id"],
            ) from exc
