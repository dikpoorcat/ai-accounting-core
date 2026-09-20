"""Evidence-bound suspected duplicate review for real business facts.

This module deliberately uses current authoritative fact revisions for every
decision.  A future lookup index may narrow the scan, but it must never be the
source of a clear result.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .contracts import Fact, KernelError, NeedsInformation
from .types import YearMonth, canonical, digest

DUPLICATE_CONTRACT = "ai-accounting-kernel/2/business-duplicate"
DUPLICATE_CONTRACT_VERSION = 1

# These are business-origin or actual-money facts without an existing hard
# real-world uniqueness key.  Source-consuming corrections and settlements
# retain their domain claim/capacity rules and are intentionally absent.
ORIGIN_KINDS = frozenset(
    {
        "expense",
        "service_sale",
        "project_cost",
        "pass_through",
        "advance",
        "asset_advance",
        "refundable_deposit",
        "reimbursed_deposit",
        "asset",
        "reimbursed_asset",
        "reimbursed_asset_batch",
        "labor_accrual",
        "labor",
        "labor_project_cost",
        "money_fund_subscription",
        "loan_agreement",
    }
)
ACTUAL_MONEY_KINDS = frozenset(
    {
        "payment",
        "cash_payment",
        "funding",
        "cash_funding",
        "funds_transfer",
        "cash_bank_transfer",
        "bank_income",
        "loan_drawdown",
    }
)
ELIGIBLE_KINDS = ORIGIN_KINDS | ACTUAL_MONEY_KINDS

# Duplicate comparison is deliberately declared per business type.  These are
# accounting fields, not display names or object aliases.  Period belongs to a
# complete origin signature; actual-money matching uses its real date instead.
SIGNATURE_FIELDS = {
    "expense": ("period", "counterparty_id", "amount_fen", "expense_class", "creditor_kind"),
    "service_sale": (
        "period",
        "customer_id",
        "gross_fen",
        "fulfillment_date",
        "vat_policy_id",
        "exemption_eligible",
        "tax_obligation_period",
        "tax_obligation_date",
    ),
    "project_cost": (
        "period",
        "project_id",
        "supplier_id",
        "amount_fen",
        "project_nature",
        "capitalization_conditions_confirmed",
    ),
    "pass_through": (
        "period",
        "payer_id",
        "beneficiary_id",
        "amount_fen",
        "rights_and_obligation_confirmed",
    ),
    "advance": (
        "period",
        "counterparty_id",
        "amount_fen",
        "side",
        "contractual_obligation_established",
        "vat_due_on_advance",
        "vat_policy_id",
        "exemption_eligible",
        "tax_obligation_period",
        "tax_obligation_date",
    ),
    "asset_advance": (
        "period",
        "counterparty_id",
        "asset_type",
        "amount_fen",
        "contractual_obligation_established",
    ),
    "refundable_deposit": ("period", "counterparty_id", "amount_fen", "refund_right_confirmed"),
    "reimbursed_deposit": (
        "period",
        "counterparty_id",
        "employee_id",
        "amount_fen",
        "company_acceptance_confirmed",
        "refund_right_confirmed",
    ),
    "asset": (
        "period",
        "asset_id",
        "asset_type",
        "supplier_id",
        "acquisition_date",
        "cost_fen",
        "acquisition_basis",
        "project_sources",
    ),
    "reimbursed_asset": (
        "period",
        "asset_id",
        "asset_type",
        "cost_fen",
        "company_acceptance_confirmed",
        "creditors",
        "acceptance_id",
        "acquisition_date",
    ),
    "reimbursed_asset_batch": (
        "period",
        "cost_fen",
        "company_acceptance_confirmed",
        "assets",
        "creditors",
        "acquisition_date",
    ),
    "labor_accrual": ("period", "person_id", "expense_class", "gross_fee_fen", "tax_treatment"),
    "labor": (
        "period",
        "person_id",
        "income_date",
        "policy_id",
        "expense_class",
        "recipient_tax_status",
        "remuneration_method",
        "withholding_method",
        "gross_payment_kind",
        "gross_payment_id",
        "fixed_fee_fen",
        "commission_base_fen",
        "commission_rate_ppm",
    ),
    "labor_project_cost": (
        "period",
        "person_id",
        "project_id",
        "gross_fee_fen",
        "tax_treatment",
        "capitalization_conditions_confirmed",
    ),
    "money_fund_subscription": (
        "period",
        "fund_id",
        "counterparty_id",
        "classification",
        "cost_basis",
        "purchase_price_fen",
        "acquisition_fees_fen",
        "excludes_declared_unpaid_distributions",
        "confirmation_date",
    ),
    "loan_agreement": (
        "period",
        "lender_id",
        "lender_is_licensed",
        "currency",
        "annual_rate_percent",
        "day_count_basis",
        "maturity_date",
        "loan_term",
    ),
    "payment": (
        "actual_date",
        "direction",
        "bank_account_id",
        "counterparty_id",
        "payment_method",
        "amount_fen",
        "allocations",
    ),
    "cash_payment": (
        "actual_date",
        "direction",
        "cash_account_id",
        "counterparty_id",
        "payment_method",
        "amount_fen",
        "allocations",
    ),
    "funding": ("actual_date", "bank_account_id", "owner_id", "amount_fen", "funding_kind"),
    "cash_funding": ("actual_date", "cash_account_id", "owner_id", "amount_fen", "funding_kind"),
    "funds_transfer": (
        "actual_date",
        "source_bank_account_id",
        "destination_bank_account_id",
        "amount_fen",
    ),
    "cash_bank_transfer": (
        "actual_date",
        "direction",
        "bank_account_id",
        "cash_account_id",
        "amount_fen",
    ),
    "bank_income": (
        "actual_date",
        "bank_account_id",
        "counterparty_id",
        "amount_fen",
        "income_kind",
        "entitlement_confirmed",
    ),
    "loan_drawdown": ("actual_date", "agreement_id", "bank_account_id", "principal_fen"),
}

# A verified original position is a strong signal only with the same business
# role, real counterparty/object and amount.  Own IDs allocated while registering
# an asset are intentionally absent so a second ID cannot hide a duplicate.
CORE_FIELDS = {
    "expense": (("counterparty_id",), ("amount_fen",)),
    "service_sale": (("customer_id",), ("gross_fen",)),
    "project_cost": (("project_id", "supplier_id"), ("amount_fen",)),
    "pass_through": (("payer_id", "beneficiary_id"), ("amount_fen",)),
    "advance": (("counterparty_id", "side"), ("amount_fen",)),
    "asset_advance": (("counterparty_id", "asset_type"), ("amount_fen",)),
    "refundable_deposit": (("counterparty_id",), ("amount_fen",)),
    "reimbursed_deposit": (("counterparty_id", "employee_id"), ("amount_fen",)),
    "asset": (("supplier_id", "asset_type"), ("cost_fen",)),
    "reimbursed_asset": (("creditors", "asset_type"), ("cost_fen",)),
    "reimbursed_asset_batch": (("creditors",), ("cost_fen",)),
    "labor_accrual": (("person_id",), ("gross_fee_fen",)),
    "labor": (("person_id",), ("fixed_fee_fen", "commission_base_fen", "commission_rate_ppm")),
    "labor_project_cost": (("person_id", "project_id"), ("gross_fee_fen",)),
    "money_fund_subscription": (
        ("fund_id", "counterparty_id"),
        ("purchase_price_fen", "acquisition_fees_fen"),
    ),
    "payment": (("direction", "bank_account_id", "counterparty_id"), ("amount_fen",)),
    "cash_payment": (("direction", "cash_account_id", "counterparty_id"), ("amount_fen",)),
    "funding": (("bank_account_id", "owner_id", "funding_kind"), ("amount_fen",)),
    "cash_funding": (("cash_account_id", "owner_id", "funding_kind"), ("amount_fen",)),
    "funds_transfer": (("source_bank_account_id", "destination_bank_account_id"), ("amount_fen",)),
    "cash_bank_transfer": (("direction", "bank_account_id", "cash_account_id"), ("amount_fen",)),
    "bank_income": (("bank_account_id", "counterparty_id", "income_kind"), ("amount_fen",)),
    "loan_drawdown": (("agreement_id", "bank_account_id"), ("principal_fen",)),
}

DUPLICATE_ROLES = {kind: kind for kind in ELIGIBLE_KINDS}

if set(SIGNATURE_FIELDS) != ELIGIBLE_KINDS or not set(CORE_FIELDS) <= ELIGIBLE_KINDS:
    raise RuntimeError("duplicate comparison fields must cover the declared allowlist")


DUPLICATE_DDL = """
CREATE TABLE business_duplicate_check(
 id TEXT PRIMARY KEY,
 contract TEXT NOT NULL CHECK(contract='ai-accounting-kernel/2/business-duplicate'),
 contract_version INTEGER NOT NULL CHECK(contract_version=1),
 proposed_subject_id TEXT NOT NULL,
 proposed_revision INTEGER NOT NULL CHECK(proposed_revision>0),
 proposed_digest BLOB NOT NULL CHECK(length(proposed_digest)=32),
 candidate_digest BLOB NOT NULL CHECK(length(candidate_digest)=32),
 action TEXT NOT NULL CHECK(action IN ('clear','reuse_existing','create_separate')),
 result_fact_id TEXT UNIQUE REFERENCES fact_revision(id) DEFERRABLE INITIALLY DEFERRED,
 selected_fact_id TEXT REFERENCES fact_revision(id),
 manifest TEXT NOT NULL CHECK(json_valid(manifest)),
 review_basis TEXT NOT NULL CHECK(json_valid(review_basis)),
 explanation TEXT NOT NULL,
 record_digest BLOB NOT NULL CHECK(length(record_digest)=32),
 created_at TEXT NOT NULL DEFAULT(strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 CHECK((action='clear' AND result_fact_id IS NOT NULL AND selected_fact_id IS NULL)
 OR (action='create_separate' AND result_fact_id IS NOT NULL AND selected_fact_id IS NULL)
 OR (action='reuse_existing' AND result_fact_id IS NULL AND selected_fact_id IS NOT NULL))
) STRICT;
CREATE INDEX business_duplicate_check_subject ON business_duplicate_check(
 proposed_subject_id,created_at,id);
CREATE INDEX business_duplicate_check_selected ON business_duplicate_check(
 selected_fact_id,created_at,id) WHERE selected_fact_id IS NOT NULL;
CREATE TRIGGER business_duplicate_check_result_owner BEFORE INSERT ON business_duplicate_check
 WHEN NEW.result_fact_id IS NOT NULL AND NOT EXISTS(
 SELECT 1 FROM fact_revision f WHERE f.id=NEW.result_fact_id
 AND f.subject_id=NEW.proposed_subject_id AND f.revision=NEW.proposed_revision)
 BEGIN SELECT RAISE(ABORT,'duplicate check result ownership mismatch'); END;
CREATE TRIGGER business_duplicate_check_no_update BEFORE UPDATE ON business_duplicate_check
 BEGIN SELECT RAISE(ABORT,'immutable duplicate check'); END;
CREATE TRIGGER business_duplicate_check_no_delete BEFORE DELETE ON business_duplicate_check
 BEGIN SELECT RAISE(ABORT,'immutable duplicate check'); END;
"""

DUPLICATE_ACTUAL_INDEX_DDL = {
    kind: f"CREATE INDEX duplicate_{kind}_actual ON fact_{kind}(actual_date,revision_id);"
    for kind in ACTUAL_MONEY_KINDS
}


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


def _parse_review(value: DuplicateReview | Mapping[str, Any]) -> DuplicateReview:
    return (
        value
        if isinstance(value, DuplicateReview)
        else DuplicateReview.model_validate_json(canonical(value))
    )


def _fact_data(fact: Fact) -> dict[str, Any]:
    return fact.model_dump(mode="json")


def _signature(fact: Fact) -> str:
    data = _fact_data(fact)
    return digest(
        [DUPLICATE_ROLES[fact.kind], {key: data[key] for key in SIGNATURE_FIELDS[fact.kind]}]
    ).hex()


def _core_signature(fact: Fact) -> str | None:
    fields = CORE_FIELDS.get(fact.kind)
    if fields is None:
        return None
    objects, amounts = fields
    data = _fact_data(fact)
    return digest(
        [
            DUPLICATE_ROLES[fact.kind],
            {key: data[key] for key in objects},
            {key: data[key] for key in amounts},
        ]
    ).hex()


def _proposal_digest(
    subject_id: str,
    revision: int,
    fact: Fact,
    evidence: Sequence[str],
    source_locations: Sequence[SourceLocation | Mapping[str, Any]],
) -> str:
    dumped_locations = [
        item.model_dump(mode="json") if isinstance(item, SourceLocation) else dict(item)
        for item in source_locations
    ]
    return digest(
        {
            "subject_id": subject_id,
            "revision": revision,
            "kind": fact.kind,
            "data": _fact_data(fact),
            "evidence": sorted(set(evidence)),
            "source_locations": sorted(dumped_locations, key=canonical),
        }
    ).hex()


def _content_digest(
    subject_id: str,
    revision: int,
    fact: Fact,
    evidence: Sequence[str],
) -> str:
    """Stable identity used on either side of an unordered candidate pair."""

    return digest(
        [
            subject_id,
            revision,
            digest(_fact_data(fact)).hex(),
            sorted(set(evidence)),
        ]
    ).hex()


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
        manifest = json.loads(row["manifest"])
        bases = [ReviewBasis.model_validate(item) for item in json.loads(row["review_basis"])]
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
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise KernelError(
            "duplicate_review_corrupt",
            "疑似重复核对记录内容校验失败",
            check_id=row["id"],
        ) from exc


def _source_locations_from_checks(
    connection, fact_ids: Iterable[str], *, current_sources: bool = False
) -> dict[str, list[dict]]:
    wanted = set(fact_ids)
    result = {fact_id: [] for fact_id in wanted}
    if not wanted:
        return result
    for row in connection.execute(
        "SELECT d.* FROM json_each(?) ids "
        "JOIN business_duplicate_check d ON d.result_fact_id=ids.value",
        (canonical(sorted(wanted)),),
    ):
        try:
            manifest, _ = _require_check_record(row)
            if not isinstance(manifest, dict) or not isinstance(
                manifest.get("source_locations"), list
            ):
                raise ValueError("source locations")
            locations = [
                SourceLocation.model_validate(item).model_dump(mode="json")
                for item in manifest["source_locations"]
            ]
            if current_sources:
                for location in locations:
                    current = connection.execute(
                        "SELECT c.fact_id,s.evidence_digest FROM fact_current c "
                        "JOIN fact_material_source_v2 s ON s.revision_id=c.fact_id "
                        "WHERE c.subject_id=?",
                        (location["source_id"],),
                    ).fetchone()
                    if current is not None:
                        location["source_fact_id"] = current["fact_id"]
                        location["evidence_digest"] = current["evidence_digest"]
        except (TypeError, ValueError, ValidationError) as exc:
            raise KernelError(
                "duplicate_review_corrupt",
                "疑似重复核对记录内容校验失败",
                check_id=row["id"],
            ) from exc
        result[row["result_fact_id"]].extend(locations)
    return result


def _validate_source_locations(
    connection, source_locations: Sequence[SourceLocation], *, current: bool = True
) -> None:
    from .materials import Specification, inspect_bytes

    for item in source_locations:
        current_join = "JOIN fact_current c ON c.fact_id=s.revision_id " if current else ""
        row = connection.execute(
            "SELECT s.evidence_digest,s.specification,e.content FROM fact_material_source_v2 s "
            + current_join
            + "JOIN fact_revision f ON f.id=s.revision_id "
            "JOIN evidence e ON e.digest=unhex(s.evidence_digest) "
            "WHERE f.subject_id=? AND f.id=?",
            (item.source_id, item.source_fact_id),
        ).fetchone()
        if row is None or row["evidence_digest"] != item.evidence_digest:
            raise KernelError(
                "duplicate_source_location_invalid",
                "疑似重复核对位置必须指向精确的已登记原件版本",
                source_id=item.source_id,
                source_fact_id=item.source_fact_id,
            )
        specification = Specification.model_validate_json(row["specification"])
        inspection = inspect_bytes(row["content"], specification)
        if item.location not in {entry["location"] for entry in inspection["items"]}:
            raise KernelError(
                "duplicate_source_location_invalid",
                "疑似重复核对位置不在已登记原件版本的验读明细中",
                source_id=item.source_id,
                source_fact_id=item.source_fact_id,
                location=item.location,
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


def _material_locations(connection, fact_ids: Iterable[str]) -> dict[str, list[dict]]:
    """Read exact current resolution links; group pools never invent row-to-result links."""

    identifiers = sorted(set(fact_ids))
    result = {fact_id: [] for fact_id in identifiers}
    if not identifiers:
        return result
    payload = canonical(identifiers)
    rows = connection.execute(
        "SELECT l.fact_id,r.revision_id resolution_fact_id,r.source_id,r.source_fact_id,"
        "r.location,s.evidence_digest "
        "FROM json_each(?) ids JOIN fact_material_resolution_v2_links l "
        "ON l.fact_id=ids.value JOIN fact_current c ON c.fact_id=l.revision_id "
        "JOIN fact_material_resolution_v2 r ON r.revision_id=c.fact_id "
        "JOIN fact_material_source_v2 s ON s.revision_id=r.source_fact_id "
        "WHERE r.treatment='recognize' ORDER BY l.fact_id,r.source_id,r.location",
        (payload,),
    )
    for row in rows:
        result[row["fact_id"]].append(
            {
                "resolution_fact_id": row["resolution_fact_id"],
                "source_id": row["source_id"],
                "source_fact_id": row["source_fact_id"],
                "evidence_digest": row["evidence_digest"],
                "location": row["location"],
            }
        )
    stored = _source_locations_from_checks(connection, identifiers, current_sources=True)
    for fact_id, locations in stored.items():
        known = {canonical(item) for item in result[fact_id]}
        result[fact_id].extend(item for item in locations if canonical(item) not in known)
    return result


def _supporting_evidence(connection, digests: Iterable[str]) -> set[str]:
    values = sorted(set(digests))
    if not values:
        return set()
    result = {}
    for row in connection.execute(
        "SELECT s.evidence_digest,s.purpose FROM json_each(?) ids "
        "JOIN fact_material_source_v2 s ON s.evidence_digest=ids.value "
        "JOIN fact_current c ON c.fact_id=s.revision_id",
        (canonical(values),),
    ):
        result.setdefault(row["evidence_digest"], set()).add(row["purpose"])
    return {evidence for evidence, purposes in result.items() if purposes == {"supporting"}}


def _location_keys(locations: Sequence[Mapping[str, Any]]) -> set[tuple[str, str]]:
    return {
        (str(item.get("evidence_digest", "")), str(item.get("location", "")))
        for item in locations
        if item.get("evidence_digest") and item.get("location")
    }


def _locations_by_evidence(
    locations: Sequence[Mapping[str, Any]],
) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for item in locations:
        evidence, location = item.get("evidence_digest"), item.get("location")
        if evidence and location:
            result.setdefault(str(evidence), set()).add(str(location))
    return result


def _candidate_signals(
    proposed: Fact,
    proposed_evidence: Sequence[str],
    proposed_locations: Sequence[Mapping[str, Any]],
    candidate: Fact,
    candidate_evidence: Sequence[str],
    candidate_locations: Sequence[Mapping[str, Any]],
    supporting: set[str],
) -> tuple[str | None, list[dict]]:
    if proposed.kind != candidate.kind:
        return None, []
    exact = _signature(proposed) == _signature(candidate)
    proposed_core = _core_signature(proposed)
    same_core = proposed_core is not None and proposed_core == _core_signature(candidate)
    shared_evidence = sorted(set(proposed_evidence) & set(candidate_evidence))
    shared_business_evidence = [item for item in shared_evidence if item not in supporting]
    proposed_by_evidence = _locations_by_evidence(proposed_locations)
    candidate_by_evidence = _locations_by_evidence(candidate_locations)
    common_locations = sorted(
        item
        for item in _location_keys(proposed_locations) & _location_keys(candidate_locations)
        if item[0] not in supporting
    )
    distinct_proof = any(
        proposed_by_evidence.get(item)
        and candidate_by_evidence.get(item)
        and proposed_by_evidence[item].isdisjoint(candidate_by_evidence[item])
        for item in shared_evidence
    )
    signals = []
    if common_locations and same_core:
        signals.append(
            {
                "code": "same_exact_material_location",
                "matched_fields": ["duplicate_role", "business_objects", "amounts"],
                "source_locations": [
                    {"evidence_digest": evidence, "location": location}
                    for evidence, location in common_locations
                ],
            }
        )
    if exact and shared_business_evidence and not distinct_proof:
        signals.append(
            {
                "code": "same_complete_signature_and_evidence",
                "matched_fields": ["duplicate_role", "complete_business_signature"],
                "evidence": shared_business_evidence,
            }
        )
    if (
        proposed.kind in ACTUAL_MONEY_KINDS
        and _signature(proposed) == _signature(candidate)
        and not distinct_proof
    ):
        signals.append(
            {
                "code": "same_complete_actual_money",
                "matched_fields": ["duplicate_role", "complete_actual_money_signature"],
            }
        )
    if signals:
        return "strong", signals
    weak = []
    if exact:
        weak.append(
            {
                "code": "same_complete_signature",
                "matched_fields": ["duplicate_role", "complete_business_signature"],
            }
        )
    if shared_evidence:
        weak.append(
            {
                "code": "shared_evidence",
                "matched_fields": ["evidence"],
                "evidence": shared_evidence,
                "distinct_locations_proven": distinct_proof,
            }
        )
    if common_locations:
        weak.append(
            {
                "code": "same_material_location_different_signature",
                "matched_fields": ["duplicate_role"],
                "source_locations": [
                    {"evidence_digest": evidence, "location": location}
                    for evidence, location in common_locations
                ],
            }
        )
    return ("weak", weak) if weak else (None, [])


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


class DuplicateCandidates:
    """Candidate preparation and immutable disposition hooks for Engine/Periods."""

    def __init__(self, store):
        self.store = store

    @staticmethod
    def eligible(kind: str) -> bool:
        return kind in ELIGIBLE_KINDS

    def _candidate_rows(
        self,
        connection,
        fact: Fact,
        subject_id: str,
        evidence: Sequence[str],
        source_locations: Sequence[SourceLocation],
        through_period: int | None = None,
        strong_only: bool = False,
    ):
        # Exact fact rows cover complete-signature/money matching.  Shared
        # evidence is queried across periods and catches a period copied wrongly.
        source_evidence = sorted({item.evidence_digest for item in source_locations})
        fact_digest = digest(fact.model_dump(mode="json"))
        # The full fact digest is exact within a business kind.  Actual-money
        # models require actual_date to belong to period, so their complete
        # comparison tuple cannot repeat across different periods either.
        scope_sql = "f.digest=?"
        scope_values: tuple[Any, ...] = (fact_digest,)
        scopes: list[str] = []
        parameters: list[Any] = [fact.kind, subject_id]

        def add_scope(sql, *values):
            if through_period is not None:
                sql = f"({sql}) AND f.period<=?"
                values = (*values, through_period)
            scopes.append(sql)
            parameters.extend(values)

        add_scope(scope_sql, *scope_values)
        evidence_scope = (
            "EXISTS(SELECT 1 FROM fact_evidence e WHERE e.fact_id=f.id "
            "AND hex(e.evidence_digest) IN (SELECT upper(value) FROM json_each(?)))"
        )
        evidence_values: tuple[Any, ...] = (canonical(sorted(set(evidence))),)
        if strong_only:
            evidence_scope += " AND f.period<>?"
            evidence_values += (fact.period.ordinal,)
        add_scope(evidence_scope, *evidence_values)
        if {
            "material_source_v2",
            "material_resolution_v2",
        } <= self.store.registry.models.keys():
            add_scope(
                "EXISTS(SELECT 1 FROM fact_material_resolution_v2_links l "
                "JOIN fact_current mc ON mc.fact_id=l.revision_id "
                "JOIN fact_material_resolution_v2 r ON r.revision_id=mc.fact_id "
                "JOIN fact_material_source_v2 ms ON ms.revision_id=r.source_fact_id "
                "WHERE l.fact_id=f.id AND ms.evidence_digest "
                "IN(SELECT value FROM json_each(?)))",
                canonical(source_evidence),
            )
        add_scope(
            "EXISTS(SELECT 1 FROM business_duplicate_check d,"
            "json_each(d.manifest,'$.source_locations') location "
            "WHERE d.result_fact_id=f.id AND "
            "json_extract(location.value,'$.evidence_digest') "
            "IN(SELECT value FROM json_each(?)))",
            canonical(source_evidence),
        )
        return list(
            connection.execute(
                "SELECT f.id,f.subject_id,f.revision,f.digest,f.period,"
                "EXISTS(SELECT 1 FROM calculation c JOIN calculation_current cc "
                "ON cc.calculation_id=c.id WHERE cc.subject_id=f.subject_id "
                "AND c.fact_id=f.id) published_current "
                "FROM fact_current fc JOIN fact_revision f ON f.id=fc.fact_id "
                "JOIN subject s ON s.id=f.subject_id WHERE s.kind=? AND f.subject_id<>? "
                "AND (" + " OR ".join(scopes) + ") ORDER BY f.period DESC,f.id DESC",
                parameters,
            )
        )

    def prepare(
        self,
        connection,
        *,
        subject_id: str,
        revision: int,
        fact: Fact,
        evidence: Sequence[str],
        source_locations: Sequence[SourceLocation | Mapping[str, Any]] = (),
        through_period: int | None = None,
        strong_only: bool = False,
        _resolved_locations: Sequence[Mapping[str, Any]] | None = None,
        _material_cache: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    ) -> dict:
        if type(revision) is not int or revision <= 0:
            raise ValueError("proposed fact revision must be positive")
        locations = tuple(
            item if isinstance(item, SourceLocation) else SourceLocation.model_validate(item)
            for item in source_locations
        )
        _validate_source_locations(connection, locations)
        effective_locations = (
            [dict(item) for item in _resolved_locations]
            if _resolved_locations is not None
            else [item.model_dump(mode="json") for item in locations]
        )
        proposed_digest = _proposal_digest(
            subject_id, revision, fact, evidence, effective_locations
        )
        content_digest = _content_digest(subject_id, revision, fact, evidence)
        if not self.eligible(fact.kind):
            return {
                "candidate_contract": DUPLICATE_CONTRACT,
                "candidate_version": DUPLICATE_CONTRACT_VERSION,
                "status": "not_applicable",
                "proposed": {
                    "subject_id": subject_id,
                    "revision": revision,
                    "kind": fact.kind,
                    "period": str(fact.period),
                    "content_digest": content_digest,
                    "proposal_digest": proposed_digest,
                },
                "source_locations": effective_locations,
                "strong_candidates": [],
                "weak_candidates": [],
                "candidate_digest": digest([DUPLICATE_CONTRACT, proposed_digest, []]).hex(),
            }
        rows = self._candidate_rows(
            connection,
            fact,
            subject_id,
            evidence,
            locations,
            through_period=through_period,
            strong_only=strong_only,
        )
        versions = self.store.facts(connection, [row["id"] for row in rows])
        materials_enabled = {
            "material_source_v2",
            "material_resolution_v2",
        } <= self.store.registry.models.keys()
        if materials_enabled:
            material = {
                fact_id: [dict(item) for item in _material_cache[fact_id]]
                for fact_id in versions
                if _material_cache is not None and fact_id in _material_cache
            }
            missing_material = set(versions) - set(material)
            if missing_material:
                material.update(_material_locations(connection, missing_material))
        else:
            material = _source_locations_from_checks(connection, versions)
        supporting = (
            _supporting_evidence(
                connection,
                {
                    *evidence,
                    *(item for version in versions.values() for item in version.evidence),
                },
            )
            if materials_enabled
            else set()
        )
        proposed = {
            "subject_id": subject_id,
            "revision": revision,
            "kind": fact.kind,
            "period": str(fact.period),
            "content_digest": content_digest,
            "proposal_digest": proposed_digest,
            "material_sources": effective_locations,
        }
        strong, weak = [], []
        for row in rows:
            version = versions[row["id"]]
            strength, signals = _candidate_signals(
                fact,
                evidence,
                effective_locations,
                version.fact,
                version.evidence,
                material[version.id],
                supporting,
            )
            if strength is None:
                continue
            if strong_only and strength != "strong":
                continue
            candidate = {
                "subject_id": version.subject_id,
                "fact_id": version.id,
                "revision": version.revision,
                "kind": version.fact.kind,
                "period": str(version.fact.period),
                "content_digest": _content_digest(
                    version.subject_id,
                    version.revision,
                    version.fact,
                    version.evidence,
                ),
                "material_sources": sorted(material[version.id], key=canonical),
                "published_current": bool(row["published_current"]),
                "signals": signals,
            }
            candidate["pair_digest"] = _pair_digest(proposed, candidate, signals)
            (strong if strength == "strong" else weak).append(candidate)
        strong.sort(key=lambda item: (item["period"], item["subject_id"], item["fact_id"]))
        weak.sort(key=lambda item: (item["period"], item["subject_id"], item["fact_id"]))
        dumped_locations = effective_locations
        candidate_digest = _candidate_digest(proposed, dumped_locations, strong)
        return {
            "candidate_contract": DUPLICATE_CONTRACT,
            "candidate_version": DUPLICATE_CONTRACT_VERSION,
            "status": "review_required" if strong else "clear",
            "proposed": proposed,
            "source_locations": dumped_locations,
            "strong_candidates": strong,
            "weak_candidates": weak,
            "candidate_digest": candidate_digest,
        }

    def prepare_batch(self, connection, proposals: Sequence[Mapping[str, Any]]) -> list[dict]:
        prepared = [self.prepare(connection, **proposal) for proposal in proposals]
        supporting = (
            _supporting_evidence(
                connection,
                {item for proposal in proposals for item in proposal["evidence"]},
            )
            if {
                "material_source_v2",
                "material_resolution_v2",
            }
            <= self.store.registry.models.keys()
            else set()
        )
        locations = [
            [
                (
                    item
                    if isinstance(item, SourceLocation)
                    else SourceLocation.model_validate(item)
                ).model_dump(mode="json")
                for item in proposal.get("source_locations", ())
            ]
            for proposal in proposals
        ]
        signatures = [
            canonical(_signature(proposal["fact"]))
            if self.eligible(proposal["fact"].kind)
            else None
            for proposal in proposals
        ]
        by_signature: dict[tuple[str, str], list[int]] = {}
        by_location: dict[tuple[str, str, str], list[int]] = {}
        # Compare the proposed rows with each other.  No row is written until
        # the caller has reviewed every result, so a failure remains atomic.
        for index, left in enumerate(proposals):
            left_fact = left["fact"]
            if not self.eligible(left_fact.kind):
                continue
            signature_key = (left_fact.kind, signatures[index])
            candidate_indexes = set(by_signature.get(signature_key, ()))
            for item in locations[index]:
                candidate_indexes.update(
                    by_location.get((left_fact.kind, item["evidence_digest"], item["location"]), ())
                )
            for right_index in sorted(candidate_indexes):
                right = proposals[right_index]
                right_fact = right["fact"]
                strength, signals = _candidate_signals(
                    left_fact,
                    left["evidence"],
                    locations[index],
                    right_fact,
                    right["evidence"],
                    locations[right_index],
                    supporting,
                )
                # Weak candidates are query/detail hints.  They neither block a
                # batch registration nor belong to its immutable receipt, so a
                # batch only materializes pairs that require a disposition.
                if strength != "strong":
                    continue
                candidate = {
                    **prepared[right_index]["proposed"],
                    "fact_id": None,
                    "published_current": False,
                    "signals": signals,
                }
                candidate["pair_digest"] = _pair_digest(
                    prepared[index]["proposed"], candidate, signals
                )
                prepared[index]["strong_candidates"].append(candidate)
            by_signature.setdefault(signature_key, []).append(index)
            for item in locations[index]:
                by_location.setdefault(
                    (left_fact.kind, item["evidence_digest"], item["location"]), []
                ).append(index)
        for item in prepared:
            item["strong_candidates"].sort(
                key=lambda candidate: (
                    candidate["period"],
                    candidate["subject_id"],
                    candidate.get("fact_id") or "",
                )
            )
            item["weak_candidates"].sort(
                key=lambda candidate: (
                    candidate["period"],
                    candidate["subject_id"],
                    candidate.get("fact_id") or "",
                )
            )
            item["candidate_digest"] = _candidate_digest(
                item["proposed"], item["source_locations"], item["strong_candidates"]
            )
            item["status"] = "review_required" if item["strong_candidates"] else item["status"]
        return prepared

    @staticmethod
    def require_review(prepared: Mapping[str, Any], review: DuplicateReview | Mapping | None):
        strong = prepared["strong_candidates"]
        if not strong:
            if review is not None:
                value = _parse_review(review)
                if value.candidate_digest != prepared["candidate_digest"]:
                    raise KernelError(
                        "duplicate_candidate_expired",
                        "疑似重复候选或依据已变化，请重新核对",
                    )
                raise KernelError(
                    "duplicate_review_unexpected", "当前登记内容没有需要处置的强重复候选"
                )
            return None
        if review is None:
            raise KernelError(
                "duplicate_review_required",
                "发现明显疑似重复业务，须先核对已有资料",
                candidate_preview=dict(prepared),
            )
        value = _parse_review(review)
        if value.candidate_digest != prepared["candidate_digest"]:
            raise KernelError("duplicate_candidate_expired", "疑似重复候选或依据已变化，请重新核对")
        candidates = {item["subject_id"]: item for item in strong}
        if value.action == "reuse_existing" and value.candidate_subject_id not in candidates:
            raise KernelError("duplicate_candidate_invalid", "复用对象不在当前强候选中")
        if value.action == "create_separate" and not any(
            item.kind
            in {
                "distinct_material_location",
                "external_reference",
                "split_basis",
                "owner_confirmation",
            }
            for item in value.review_basis
        ):
            raise KernelError(
                "duplicate_separation_basis_required",
                "另建业务须有不同位置、交易行、拆分或负责人确认依据",
            )
        return value

    @staticmethod
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

    def record_check(
        self,
        connection,
        *,
        prepared: Mapping[str, Any],
        result_fact_id: str | None,
        review: DuplicateReview | Mapping | None,
        fact_ids_by_subject: Mapping[str, str] | None = None,
    ) -> dict:
        value = self.require_review(prepared, review)
        if value is not None:
            self._verify_review_evidence(connection, value, prepared, current=True)
        action = value.action if value else "clear"
        selected = None
        if value and value.action == "reuse_existing":
            selected_candidate = next(
                item
                for item in prepared["strong_candidates"]
                if item["subject_id"] == value.candidate_subject_id
            )
            selected = selected_candidate["fact_id"]
            if selected is None and fact_ids_by_subject is not None:
                selected = fact_ids_by_subject.get(selected_candidate["subject_id"])
            if selected is None:
                raise KernelError("duplicate_candidate_invalid", "批内拟登记业务不能作为复用目标")
            result_fact_id = None
        if action != "reuse_existing" and result_fact_id is None:
            raise ValueError("a saved duplicate check requires its exact fact revision")
        manifest = {
            key: prepared[key]
            for key in (
                "candidate_contract",
                "candidate_version",
                "proposed",
                "source_locations",
                "strong_candidates",
                "candidate_digest",
            )
        }
        if fact_ids_by_subject:
            manifest["strong_candidates"] = [
                dict(
                    candidate,
                    fact_id=candidate.get("fact_id")
                    or fact_ids_by_subject.get(candidate["subject_id"]),
                )
                for candidate in manifest["strong_candidates"]
            ]
        check_id = uuid.uuid4().hex
        manifest_json = canonical(manifest)
        review_json = canonical(
            [item.model_dump(mode="json") for item in value.review_basis] if value else []
        )
        explanation = value.explanation if value else "未发现强重复候选"
        proposed_digest = bytes.fromhex(prepared["proposed"]["proposal_digest"])
        candidate_digest = bytes.fromhex(prepared["candidate_digest"])
        record_digest = _check_record_digest(
            check_id=check_id,
            proposed_subject_id=prepared["proposed"]["subject_id"],
            proposed_revision=prepared["proposed"]["revision"],
            proposed_digest=proposed_digest,
            candidate_digest=candidate_digest,
            action=action,
            result_fact_id=result_fact_id,
            selected_fact_id=selected,
            manifest=manifest_json,
            review_basis=review_json,
            explanation=explanation,
        )
        connection.execute(
            "INSERT INTO business_duplicate_check(id,contract,contract_version,"
            "proposed_subject_id,proposed_revision,proposed_digest,candidate_digest,action,"
            "result_fact_id,selected_fact_id,manifest,review_basis,explanation,record_digest) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                check_id,
                DUPLICATE_CONTRACT,
                DUPLICATE_CONTRACT_VERSION,
                prepared["proposed"]["subject_id"],
                prepared["proposed"]["revision"],
                proposed_digest,
                candidate_digest,
                action,
                result_fact_id,
                selected,
                manifest_json,
                review_json,
                explanation,
                record_digest,
            ),
        )
        return {
            "check_id": check_id,
            "action": action,
            "result_fact_id": result_fact_id,
            "selected_fact_id": selected,
            "candidate_digest": prepared["candidate_digest"],
        }

    def _current_prepared(
        self,
        connection,
        fact_id: str,
        *,
        through_period: int | None = None,
        source_locations: Sequence[SourceLocation | Mapping[str, Any]] | None = None,
        strong_only: bool = False,
        material_cache: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    ) -> dict | None:
        row = connection.execute(
            "SELECT f.subject_id,f.revision FROM fact_current c JOIN fact_revision f "
            "ON f.id=c.fact_id WHERE f.id=?",
            (fact_id,),
        ).fetchone()
        if row is None:
            return None
        version = self.store.fact(connection, fact_id)
        resolved_locations = None
        if source_locations is None:
            if {
                "material_source_v2",
                "material_resolution_v2",
            } <= self.store.registry.models.keys():
                resolved_locations = _material_locations(connection, (fact_id,))[fact_id]
                source_locations = [
                    {key: item[key] for key in SourceLocation.model_fields}
                    for item in resolved_locations
                ]
            else:
                source_locations = _source_locations_from_checks(
                    connection, (fact_id,), current_sources=True
                )[fact_id]
        elif any(
            isinstance(item, Mapping) and "resolution_fact_id" in item for item in source_locations
        ):
            resolved_locations = [dict(item) for item in source_locations]
            source_locations = [
                {key: item[key] for key in SourceLocation.model_fields}
                for item in resolved_locations
            ]
        return self.prepare(
            connection,
            subject_id=row["subject_id"],
            revision=row["revision"],
            fact=version.fact,
            evidence=version.evidence,
            source_locations=source_locations,
            through_period=through_period,
            strong_only=strong_only,
            _resolved_locations=resolved_locations,
            _material_cache=material_cache,
        )

    def _valid_pair_reviews(
        self, connection, pair_digests: Iterable[str], *, through_period: int | None = None
    ) -> set[str]:
        wanted = set(pair_digests)
        if not wanted:
            return set()
        result = set()
        for row in connection.execute(
            "SELECT DISTINCT d.* FROM business_duplicate_check d "
            "JOIN json_each(d.manifest,'$.strong_candidates') c "
            "WHERE d.action='create_separate' AND "
            "json_extract(c.value,'$.pair_digest') IN(SELECT value FROM json_each(?))",
            (canonical(sorted(wanted)),),
        ):
            manifest, bases = _require_check_record(row)
            prepared = self._current_prepared(
                connection,
                row["result_fact_id"],
                through_period=through_period,
                strong_only=True,
            )
            if prepared is None:
                continue
            review = DuplicateReview(
                candidate_digest=manifest["candidate_digest"],
                action="create_separate",
                explanation=row["explanation"],
                review_basis=tuple(bases),
            )
            self._verify_review_evidence(connection, review, manifest, current=False)
            current_pairs = {item["pair_digest"] for item in prepared["strong_candidates"]}
            result.update(
                item["pair_digest"]
                for item in manifest.get("strong_candidates", ())
                if isinstance(item, dict)
                and item.get("pair_digest") in current_pairs
                and item["pair_digest"] in wanted
            )
        return result

    def unresolved(
        self,
        connection,
        *,
        subject_ids: Iterable[str] | None = None,
        through_period: str | YearMonth | None = None,
    ) -> list[dict]:
        parameters: list[Any] = [canonical(sorted(ELIGIBLE_KINDS))]
        where = ["s.kind IN (SELECT value FROM json_each(?))"]
        if subject_ids is not None:
            where.append("f.subject_id IN (SELECT value FROM json_each(?))")
            parameters.append(canonical(sorted(set(subject_ids))))
        limit = YearMonth(through_period).ordinal if through_period is not None else None
        if limit is not None:
            where.append("f.period<=?")
            parameters.append(limit)
        rows = list(
            connection.execute(
                "SELECT f.id,f.subject_id,f.period FROM fact_current c "
                "JOIN fact_revision f ON f.id=c.fact_id JOIN subject s ON s.id=f.subject_id "
                "WHERE " + " AND ".join(where) + " ORDER BY f.period,f.subject_id",
                parameters,
            )
        )
        fact_ids = [row["id"] for row in rows]
        locations_by_fact = (
            _material_locations(connection, fact_ids)
            if {
                "material_source_v2",
                "material_resolution_v2",
            }
            <= self.store.registry.models.keys()
            else _source_locations_from_checks(connection, fact_ids, current_sources=True)
        )
        candidates, seen = [], set()
        for row in rows:
            prepared = self._current_prepared(
                connection,
                row["id"],
                through_period=limit,
                source_locations=locations_by_fact[row["id"]],
                strong_only=True,
                material_cache=locations_by_fact,
            )
            if prepared is None:
                continue
            for candidate in prepared["strong_candidates"]:
                pair = candidate["pair_digest"]
                if pair in seen:
                    continue
                seen.add(pair)
                review_period = max(
                    YearMonth(prepared["proposed"]["period"]), YearMonth(candidate["period"])
                )
                candidates.append(
                    (
                        {
                            "field": "business_duplicates",
                            "code": "duplicate_review_required",
                            "message": "明显疑似重复业务尚未核对",
                            "review_period": str(review_period),
                            "subject_id": prepared["proposed"]["subject_id"],
                            "candidate_subject_id": candidate["subject_id"],
                            "pair_digest": pair,
                            "signals": candidate["signals"],
                        },
                        review_period,
                    )
                )
        valid = self._valid_pair_reviews(
            connection,
            (item[0]["pair_digest"] for item in candidates),
            through_period=limit,
        )
        closed_through = (
            connection.execute("SELECT coalesce(max(period),-1) FROM period_close").fetchone()[0]
            if limit is not None
            else -1
        )
        problems = []
        for problem, review_period in candidates:
            if problem["pair_digest"] in valid:
                continue
            if limit is not None and review_period.ordinal <= closed_through < limit:
                problem["review_period"] = str(YearMonth.from_ordinal(limit))
            problems.append(problem)
        return problems

    def require_publishable(self, connection, subject_ids: Iterable[str]):
        problems = self.unresolved(connection, subject_ids=subject_ids)
        if problems:
            raise KernelError(
                "duplicate_review_required",
                "明显疑似重复业务尚未核对，不能正式发布",
                fact_issues=problems,
            )

    def close_readiness(self, connection, period: str | YearMonth) -> list[dict]:
        return self.unresolved(connection, through_period=period)

    def business_detail(self, connection, subject_id: str, *, summary: bool = False) -> dict:
        rows = list(
            connection.execute(
                "SELECT d.* FROM business_duplicate_check d "
                "LEFT JOIN fact_revision r ON r.id=d.result_fact_id "
                "LEFT JOIN fact_revision s ON s.id=d.selected_fact_id "
                "WHERE d.proposed_subject_id=? OR r.subject_id=? OR s.subject_id=? "
                "OR EXISTS(SELECT 1 FROM json_each(d.manifest,'$.strong_candidates') c "
                "WHERE json_extract(c.value,'$.subject_id')=?) "
                "ORDER BY d.created_at,d.id",
                (subject_id, subject_id, subject_id, subject_id),
            )
        )
        check_count = len(rows)
        if summary:
            rows = rows[-20:]
        checks = []
        for row in rows:
            manifest, bases = _require_check_record(row)
            checks.append(
                {
                    "check_id": row["id"],
                    "action": row["action"],
                    "proposed_subject_id": row["proposed_subject_id"],
                    "result_fact_id": row["result_fact_id"],
                    "selected_fact_id": row["selected_fact_id"],
                    "candidate_digest": row["candidate_digest"].hex(),
                    "manifest": manifest,
                    "review_basis": [item.model_dump(mode="json") for item in bases],
                    "explanation": row["explanation"],
                    "created_at": row["created_at"],
                }
            )
        current = connection.execute(
            "SELECT fact_id FROM fact_current WHERE subject_id=?", (subject_id,)
        ).fetchone()
        candidates = None
        if current is not None:
            candidates = self._current_prepared(connection, current["fact_id"])
        unresolved = [
            item
            for item in self.unresolved(connection, subject_ids=(subject_id,))
            if item["subject_id"] == subject_id or item["candidate_subject_id"] == subject_id
        ]
        return {
            "candidate_contract": DUPLICATE_CONTRACT,
            "candidate_version": DUPLICATE_CONTRACT_VERSION,
            "subject_id": subject_id,
            "status": "review_required" if unresolved else "clear",
            "strong_candidates": candidates["strong_candidates"] if candidates else [],
            "weak_candidates": candidates["weak_candidates"] if candidates else [],
            "unresolved": unresolved,
            "checks": checks,
            "check_count": check_count,
            "checks_truncated": check_count > len(checks),
        }


class Duplicates:
    """Public read-only facade; normal save paths repeat the same check in their write."""

    def __init__(self, engine):
        self.engine = engine
        self.store = engine.store

    def prepare_fact_registration(
        self,
        kind: str,
        subject_id: str,
        data: dict,
        *,
        evidence: Sequence[str],
        expected_revision: int,
        source_locations: Sequence[SourceLocation | Mapping[str, Any]] = (),
    ) -> dict:
        self.engine._require_direct_registration(kind)
        if not subject_id or len(subject_id) > 200:
            raise KernelError("invalid_subject", "invalid stable business identity")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision must be a nonnegative integer")
        from .engine import _reject_binary_numbers

        _reject_binary_numbers(data)
        try:
            fact = self.store.registry.models[kind].model_validate_json(canonical(data))
        except ValidationError as exc:
            missing = [item for item in exc.errors() if item["type"] == "missing"]
            if missing:
                issue = missing[0]
                raise NeedsInformation(
                    ".".join(map(str, issue["loc"])),
                    "缺少必需核算事实",
                    sources=(subject_id,),
                ) from exc
            raise KernelError(
                "invalid_fact",
                "业务事实校验失败",
                fact_issues=json.loads(exc.json(include_url=False)),
            ) from exc
        if not evidence:
            raise NeedsInformation("evidence", "已确认事实必须引用不可变依据")
        try:
            evidence_bytes = [bytes.fromhex(item) for item in evidence]
        except ValueError as exc:
            raise ValueError("evidence digest must be hexadecimal") from exc
        if any(len(item) != 32 for item in evidence_bytes):
            raise ValueError("evidence digest must be 32 bytes")
        with self.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            current = connection.execute(
                "SELECT s.kind,c.fact_id FROM subject s LEFT JOIN fact_current c "
                "ON c.subject_id=s.id WHERE s.id=?",
                (subject_id,),
            ).fetchone()
            if current is not None and current["fact_id"] is None:
                raise KernelError("withdrawn_subject", "已撤去身份保留审计；新业务须使用新身份")
            if current is not None and current["kind"] != kind:
                raise KernelError(
                    "identity_mismatch", "stable identity cannot change business type"
                )
            revision = 0
            if current is not None:
                revision = self.store.fact(connection, current["fact_id"]).revision
            if expected_revision != revision:
                raise KernelError("fact_version_conflict", "已确认事实版本发生变化")
            missing_evidence = connection.execute(
                "SELECT value FROM json_each(?) ids LEFT JOIN evidence e "
                "ON e.digest=unhex(ids.value) WHERE e.digest IS NULL LIMIT 1",
                (canonical(sorted(set(evidence))),),
            ).fetchone()
            if missing_evidence is not None:
                raise NeedsInformation(
                    "evidence", "已确认事实引用的依据尚未登记", sources=(missing_evidence[0],)
                )
            from .entity_references import validate_entity_references

            validate_entity_references(connection, fact, subject_id)
            prepared = DuplicateCandidates(self.store).prepare(
                connection,
                subject_id=subject_id,
                revision=revision + 1,
                fact=fact,
                evidence=evidence,
                source_locations=source_locations,
            )
            return {"schema_version": 1, **prepared}


def verify_duplicate_checks(connection) -> None:
    """Verify immutable review content without trusting its saved JSON summary."""

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
            DuplicateCandidates._verify_review_evidence(
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


__all__ = [
    "ACTUAL_MONEY_KINDS",
    "DUPLICATE_CONTRACT",
    "DUPLICATE_CONTRACT_VERSION",
    "DUPLICATE_DDL",
    "DUPLICATE_ACTUAL_INDEX_DDL",
    "DuplicateCandidates",
    "Duplicates",
    "DuplicateReview",
    "ELIGIBLE_KINDS",
    "ORIGIN_KINDS",
    "ReviewBasis",
    "SourceLocation",
    "verify_duplicate_checks",
]
