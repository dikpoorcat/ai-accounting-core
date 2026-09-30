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
from .stored_json import DuplicateStoredKey, loads_unique
from .types import YearMonth, canonical, digest

DUPLICATE_CONTRACT = "ai-accounting-kernel/2/business-duplicate"
DUPLICATE_CONTRACT_VERSION = 2

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
        "platform_payment",
        "funding",
        "cash_funding",
        "platform_funding",
        "funds_transfer",
        "cash_bank_transfer",
        "bank_platform_transfer",
        "bank_income",
        "loan_drawdown",
        "managed_reserve_expense",
        "managed_reserve_refund",
        "payroll_reserve_payment",
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
    "platform_payment": (
        "actual_date",
        "direction",
        "platform_account_id",
        "counterparty_id",
        "payment_method",
        "amount_fen",
        "movement_ids",
        "allocations",
    ),
    "funding": ("actual_date", "bank_account_id", "owner_id", "amount_fen", "funding_kind"),
    "cash_funding": ("actual_date", "cash_account_id", "owner_id", "amount_fen", "funding_kind"),
    "platform_funding": (
        "actual_date",
        "platform_account_id",
        "owner_id",
        "amount_fen",
        "funding_kind",
        "movement_ids",
    ),
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
    "bank_platform_transfer": (
        "actual_date",
        "direction",
        "bank_account_id",
        "platform_account_id",
        "amount_fen",
        "movement_ids",
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
    "managed_reserve_expense": (
        "actual_date",
        "bank_account_id",
        "cash_account_id",
        "platform_account_id",
        "movement_ids",
        "counterparty_id",
        "amount_fen",
    ),
    "managed_reserve_refund": (
        "actual_date",
        "bank_account_id",
        "cash_account_id",
        "platform_account_id",
        "movement_ids",
        "counterparty_id",
        "amount_fen",
    ),
    "payroll_reserve_payment": (
        "actual_date",
        "bank_account_id",
        "amount_fen",
        "allocations",
        "reserve_expense_fen",
        "return_period",
        "actual_return_date",
        "return_confirmed",
        "complete_group_confirmed",
    ),
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
    "platform_payment": (
        ("direction", "platform_account_id", "counterparty_id"),
        ("amount_fen",),
    ),
    "funding": (("bank_account_id", "owner_id", "funding_kind"), ("amount_fen",)),
    "cash_funding": (("cash_account_id", "owner_id", "funding_kind"), ("amount_fen",)),
    "platform_funding": (
        ("platform_account_id", "owner_id", "funding_kind"),
        ("amount_fen",),
    ),
    "funds_transfer": (("source_bank_account_id", "destination_bank_account_id"), ("amount_fen",)),
    "cash_bank_transfer": (("direction", "bank_account_id", "cash_account_id"), ("amount_fen",)),
    "bank_platform_transfer": (
        ("direction", "bank_account_id", "platform_account_id"),
        ("amount_fen",),
    ),
    "bank_income": (("bank_account_id", "counterparty_id", "income_kind"), ("amount_fen",)),
    "loan_drawdown": (("agreement_id", "bank_account_id"), ("principal_fen",)),
    "managed_reserve_expense": (
        (
            "bank_account_id",
            "cash_account_id",
            "platform_account_id",
            "counterparty_id",
        ),
        ("amount_fen",),
    ),
    "managed_reserve_refund": (
        (
            "bank_account_id",
            "cash_account_id",
            "platform_account_id",
            "counterparty_id",
        ),
        ("amount_fen",),
    ),
    "payroll_reserve_payment": (("bank_account_id",), ("amount_fen",)),
}

DUPLICATE_ROLES = {kind: kind for kind in ELIGIBLE_KINDS}

if set(SIGNATURE_FIELDS) != ELIGIBLE_KINDS or not set(CORE_FIELDS) <= ELIGIBLE_KINDS:
    raise RuntimeError("duplicate comparison fields must cover the declared allowlist")


def _money_leg(
    category: str,
    account_id: str,
    actual_date: str,
    direction: str,
    amount_fen: int,
    object_id: str | None,
    movement_ids: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "category": category,
        "account_id": account_id,
        "actual_date": actual_date,
        "direction": direction,
        "amount_fen": amount_fen,
        "object_id": object_id,
        "movement_ids": sorted(set(movement_ids)),
    }


def _actual_money_legs(fact: Fact) -> tuple[dict[str, Any], ...]:
    """Return each real company-funds side without its accounting treatment."""

    if fact.kind not in ACTUAL_MONEY_KINDS:
        return ()
    data = _fact_data(fact)
    actual_date = str(data["actual_date"])
    amount_fen = data.get("amount_fen", data.get("principal_fen"))
    movements = data.get("movement_ids", ())
    if fact.kind in {"payment", "cash_payment", "platform_payment"}:
        category = {
            "payment": "bank",
            "cash_payment": "cash",
            "platform_payment": "platform",
        }[fact.kind]
        return (
            _money_leg(
                category,
                data[f"{category}_account_id"],
                actual_date,
                data["direction"],
                amount_fen,
                data["counterparty_id"],
                movements,
            ),
        )
    if fact.kind in {"funding", "cash_funding", "platform_funding"}:
        category = {
            "funding": "bank",
            "cash_funding": "cash",
            "platform_funding": "platform",
        }[fact.kind]
        return (
            _money_leg(
                category,
                data[f"{category}_account_id"],
                actual_date,
                "inflow",
                amount_fen,
                data["owner_id"],
                movements,
            ),
        )
    if fact.kind in {"managed_reserve_expense", "managed_reserve_refund"}:
        category = next(
            category
            for category in ("bank", "cash", "platform")
            if data.get(f"{category}_account_id") is not None
        )
        return (
            _money_leg(
                category,
                data[f"{category}_account_id"],
                actual_date,
                "outflow" if fact.kind == "managed_reserve_expense" else "inflow",
                amount_fen,
                data.get("counterparty_id"),
                movements,
            ),
        )
    if fact.kind == "payroll_reserve_payment":
        return (
            _money_leg(
                "bank",
                data["bank_account_id"],
                actual_date,
                "outflow",
                amount_fen,
                data.get("counterparty_id", "payroll-group"),
            ),
        )
    if fact.kind == "bank_income":
        return (
            _money_leg(
                "bank",
                data["bank_account_id"],
                actual_date,
                "inflow",
                amount_fen,
                data["counterparty_id"],
            ),
        )
    if fact.kind == "loan_drawdown":
        return (
            _money_leg(
                "bank",
                data["bank_account_id"],
                actual_date,
                "inflow",
                amount_fen,
                "loan-agreement:" + data["agreement_id"],
            ),
        )
    if fact.kind == "funds_transfer":
        source, destination = data["source_bank_account_id"], data["destination_bank_account_id"]
        return (
            _money_leg(
                "bank",
                source,
                actual_date,
                "outflow",
                amount_fen,
                "funds-account:bank:" + destination,
            ),
            _money_leg(
                "bank",
                destination,
                actual_date,
                "inflow",
                amount_fen,
                "funds-account:bank:" + source,
            ),
        )
    if fact.kind == "cash_bank_transfer":
        bank, cash = data["bank_account_id"], data["cash_account_id"]
        withdrawal = data["direction"] == "withdrawal"
        return (
            _money_leg(
                "bank",
                bank,
                actual_date,
                "outflow" if withdrawal else "inflow",
                amount_fen,
                "funds-account:cash:" + cash,
            ),
            _money_leg(
                "cash",
                cash,
                actual_date,
                "inflow" if withdrawal else "outflow",
                amount_fen,
                "funds-account:bank:" + bank,
            ),
        )
    if fact.kind == "bank_platform_transfer":
        bank, platform = data["bank_account_id"], data["platform_account_id"]
        to_platform = data["direction"] == "bank_to_platform"
        return (
            _money_leg(
                "bank",
                bank,
                actual_date,
                "outflow" if to_platform else "inflow",
                amount_fen,
                "funds-account:platform:" + platform,
            ),
            _money_leg(
                "platform",
                platform,
                actual_date,
                "inflow" if to_platform else "outflow",
                amount_fen,
                "funds-account:bank:" + bank,
                movements,
            ),
        )
    raise RuntimeError(f"missing actual-money duplicate normalization for {fact.kind}")


def _money_key(leg: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(
        leg[key] for key in ("category", "account_id", "actual_date", "direction", "amount_fen")
    )


def _money_matches(proposed: Fact, candidate: Fact) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    candidate_by_key: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for leg in _actual_money_legs(candidate):
        candidate_by_key.setdefault(_money_key(leg), []).append(leg)
    return [
        (left, right)
        for left in _actual_money_legs(proposed)
        for right in candidate_by_key.get(_money_key(left), ())
    ]


DUPLICATE_DDL = """
CREATE TABLE business_duplicate_check(
 id TEXT PRIMARY KEY,
 contract TEXT NOT NULL CHECK(contract='ai-accounting-kernel/2/business-duplicate'),
 contract_version INTEGER NOT NULL CHECK(contract_version=2),
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
    manifest: dict,
    review_basis: list,
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
            "manifest": manifest,
            "review_basis": review_basis,
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
        raw_bases = loads_unique(row["review_basis"])
        bases = [ReviewBasis.model_validate(item) for item in raw_bases]
        expected = _check_record_digest(
            check_id=row["id"],
            proposed_subject_id=row["proposed_subject_id"],
            proposed_revision=row["proposed_revision"],
            proposed_digest=row["proposed_digest"],
            candidate_digest=row["candidate_digest"],
            action=row["action"],
            result_fact_id=row["result_fact_id"],
            selected_fact_id=row["selected_fact_id"],
            manifest=manifest,
            review_basis=raw_bases,
            explanation=row["explanation"],
        )
        if expected != row["record_digest"]:
            raise ValueError("duplicate check digest")
        return manifest, bases
    except DuplicateStoredKey as exc:
        raise KernelError(
            "content_integrity_failed",
            "疑似重复核对原文有重复字段",
            check_id=row["id"],
        ) from exc
    except (IndexError, KeyError, TypeError, ValueError, ValidationError) as exc:
        raise KernelError(
            "duplicate_review_corrupt",
            "疑似重复核对记录内容校验失败",
            check_id=row["id"],
        ) from exc


def _require_sql_manifest_scope(connection, *, predicate="1", parameters=()) -> None:
    """Reject ambiguous manifests before JSON1 can exclude a review candidate.

    SQLite JSON1 decodes escaped member names in ``json_tree.key``. Grouping by
    object parent therefore catches both literal and escape-equivalent duplicate
    keys without transferring every historical manifest into Python.
    """
    damaged = connection.execute(
        "SELECT d.id FROM business_duplicate_check d WHERE " + predicate + " AND ("
        "json_valid(d.manifest)<>1 OR json_type(d.manifest)<>'object' OR EXISTS("
        "SELECT 1 FROM json_tree(CASE WHEN json_valid(d.manifest) "
        "THEN d.manifest ELSE '{}' END) member "
        "WHERE member.key IS NOT NULL GROUP BY member.parent,member.key "
        "HAVING COUNT(*)>1)) LIMIT 1",
        parameters,
    ).fetchone()
    if damaged is not None:
        raise KernelError(
            "content_integrity_failed",
            "已保存的疑似重复核对原文有重复字段或格式错误",
            check_id=damaged["id"],
        )


def _source_locations_from_checks(
    connection, fact_ids: Iterable[str], *, current_sources: bool = False
) -> dict[str, list[dict]]:
    wanted = set(fact_ids)
    result = {fact_id: [] for fact_id in wanted}
    if not wanted:
        return result
    records = [
        (row["result_fact_id"], row)
        for row in connection.execute(
            "SELECT d.* FROM json_each(?) ids "
            "JOIN business_duplicate_check d ON d.result_fact_id=ids.value",
            (canonical(sorted(wanted)),),
        )
    ]
    missing = sorted(wanted - {fact_id for fact_id, _ in records})
    if missing:
        # Identity reassignment writes a new fact revision without re-registering
        # the business. Its original check remains the position authority.
        records.extend(
            (row["requested_id"], row)
            for row in connection.execute(
                "WITH RECURSIVE ancestry(requested_id,ancestor_id) AS ("
                "SELECT ids.value,i.before_fact_id FROM json_each(?) ids "
                "JOIN fact_revision f ON f.id=ids.value "
                "JOIN identity_correction_item i ON i.subject_id=f.subject_id "
                "AND i.after_fact_id=f.id AND i.action='reassign' UNION ALL "
                "SELECT a.requested_id,i.before_fact_id FROM ancestry a "
                "JOIN fact_revision f ON f.id=a.ancestor_id "
                "JOIN identity_correction_item i ON i.subject_id=f.subject_id "
                "AND i.after_fact_id=f.id AND i.action='reassign' "
                "WHERE NOT EXISTS(SELECT 1 FROM business_duplicate_check d "
                "WHERE d.result_fact_id=a.ancestor_id)) "
                "SELECT a.requested_id,d.* FROM ancestry a "
                "JOIN business_duplicate_check d ON d.result_fact_id=a.ancestor_id",
                (canonical(missing),),
            )
        )
    decoded = []
    for fact_id, row in records:
        try:
            manifest, _ = _require_check_record(row)
            if not isinstance(manifest, dict) or not isinstance(
                manifest.get("source_locations"), list
            ):
                raise ValueError("source locations")
            locations = [
                SourceLocation.model_validate(item) for item in manifest["source_locations"]
            ]
        except (TypeError, ValueError, ValidationError) as exc:
            raise KernelError(
                "duplicate_review_corrupt",
                "疑似重复核对记录内容校验失败",
                check_id=row["id"],
            ) from exc
        decoded.append((fact_id, locations))
    current_by_source = {}
    if current_sources and decoded:
        sources = sorted({location.source_id for _, items in decoded for location in items})
        if sources:
            current_by_source = {
                row["subject_id"]: row
                for row in connection.execute(
                    "SELECT c.subject_id,c.fact_id,s.evidence_digest FROM json_each(?) ids "
                    "JOIN fact_current c ON c.subject_id=ids.value "
                    "JOIN fact_material_source_v2 s ON s.revision_id=c.fact_id",
                    (canonical(sources),),
                )
            }
    for fact_id, locations in decoded:
        for item in locations:
            location = item.model_dump(mode="json")
            current = current_by_source.get(item.source_id)
            if current is not None:
                location["source_fact_id"] = current["fact_id"]
                location["evidence_digest"] = current["evidence_digest"]
            result[fact_id].append(location)
    return result


def _validate_source_locations(
    connection,
    source_locations: Sequence[SourceLocation],
    *,
    current: bool = True,
    _inspection_cache=None,
) -> None:
    from .materials import Specification, inspect_bytes

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
        "FROM fact_material_resolution_v2_links l "
        "JOIN fact_current c ON c.fact_id=l.revision_id "
        "JOIN fact_material_resolution_v2 r ON r.revision_id=c.fact_id "
        "JOIN fact_material_source_v2 s ON s.revision_id=r.source_fact_id "
        "WHERE l.fact_id IN (SELECT value FROM json_each(?)) "
        "AND r.treatment='recognize' ORDER BY l.fact_id,r.source_id,r.location",
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

    def exact_location_key(item):
        # Both inputs have strict string fields. Keep resolution identity in the
        # comparison: a current link and an older registered location at the
        # same file position are distinct candidate evidence.
        return (
            item.get("resolution_fact_id"),
            item["source_id"],
            item["source_fact_id"],
            item["evidence_digest"],
            item["location"],
        )

    for fact_id, locations in stored.items():
        known = {exact_location_key(item) for item in result[fact_id]}
        result[fact_id].extend(item for item in locations if exact_location_key(item) not in known)
    return result


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


def _strong_location_pair_fact_ids(connection, rows):
    """Find exact position pairs from registered locations and current material links.

    An empty check on a reassigned successor replaces its ancestor's location,
    so lineage propagation stops at every successor with its own check.
    """
    if len(rows) < 2:
        return set()
    scope_ids = canonical([row["id"] for row in rows])
    # Walk only the reassign ancestry of the actual candidate facts. A check
    # on a successor replaces its ancestor's recorded locations, so no older
    # check can affect that branch after the first checked ancestor.
    relevant_checks = [
        row
        for row in connection.execute(
            "WITH RECURSIVE ancestry(fact_id) AS ("
            "SELECT value FROM json_each(?) UNION "
            "SELECT i.before_fact_id FROM ancestry a "
            "JOIN fact_revision f ON f.id=a.fact_id "
            "JOIN identity_correction_item i ON i.subject_id=f.subject_id "
            "AND i.after_fact_id=f.id AND i.action='reassign' "
            "WHERE NOT EXISTS(SELECT 1 FROM business_duplicate_check d "
            "WHERE d.result_fact_id=a.fact_id)) "
            "SELECT DISTINCT d.* FROM ancestry a JOIN business_duplicate_check d "
            "ON d.result_fact_id=a.fact_id",
            (scope_ids,),
        )
    ]
    for check in relevant_checks:
        _require_check_record(check)
    check_ids = canonical([check["id"] for check in relevant_checks])
    return {
        fact_id
        for (group,) in connection.execute(
            "WITH RECURSIVE scoped AS MATERIALIZED ("
            "SELECT ids.value fact_id,CASE WHEN s.kind IN "
            "(SELECT value FROM json_each(?)) THEN 'actual_money' ELSE s.kind END role "
            "FROM json_each(?) ids JOIN fact_revision f ON f.id=ids.value "
            "JOIN subject s ON s.id=f.subject_id),"
            "located_checks AS MATERIALIZED ("
            "SELECT d.result_fact_id,location.value "
            "FROM json_each(?) check_ids JOIN business_duplicate_check d "
            "ON d.id=check_ids.value "
            "CROSS JOIN json_each(CASE WHEN json_valid(d.manifest) "
            "THEN d.manifest ELSE '{}' END,'$.source_locations') location "
            "WHERE d.result_fact_id IS NOT NULL),"
            "check_lineage(fact_id,location) AS ("
            "SELECT result_fact_id,value FROM located_checks UNION ALL "
            "SELECT i.after_fact_id,l.location FROM check_lineage l "
            "JOIN fact_revision f ON f.id=l.fact_id "
            "JOIN identity_correction_item i ON i.subject_id=f.subject_id "
            "AND i.before_fact_id=f.id AND i.action='reassign' "
            "WHERE NOT EXISTS(SELECT 1 FROM business_duplicate_check d "
            "WHERE d.result_fact_id=i.after_fact_id)),"
            "locations AS ("
            "SELECT scope.fact_id,scope.role,src.evidence_digest evidence_digest,r.location "
            "FROM fact_material_resolution_v2_links l "
            "CROSS JOIN fact_current c ON c.fact_id=l.revision_id "
            "CROSS JOIN fact_material_resolution_v2 r ON r.revision_id=c.fact_id "
            "CROSS JOIN fact_material_source_v2 src ON src.revision_id=r.source_fact_id "
            "CROSS JOIN scoped scope ON scope.fact_id=l.fact_id "
            "WHERE r.treatment='recognize' UNION ALL "
            "SELECT scope.fact_id,scope.role,"
            "coalesce(src.evidence_digest,CASE WHEN json_valid(l.location) "
            "THEN json_extract(l.location,'$.evidence_digest') END), "
            "CASE WHEN json_valid(l.location) "
            "THEN json_extract(l.location,'$.location') END "
            "FROM check_lineage l CROSS JOIN scoped scope ON scope.fact_id=l.fact_id "
            "LEFT JOIN fact_current current_source "
            "ON current_source.subject_id=CASE WHEN json_valid(l.location) "
            "THEN json_extract(l.location,'$.source_id') END "
            "LEFT JOIN fact_material_source_v2 src "
            "ON src.revision_id=current_source.fact_id) "
            "SELECT json_group_array(DISTINCT fact_id) FROM locations "
            "WHERE evidence_digest IS NOT NULL AND location IS NOT NULL "
            "GROUP BY role,evidence_digest,location HAVING count(DISTINCT fact_id)>1",
            (canonical(sorted(ACTUAL_MONEY_KINDS)), scope_ids, check_ids),
        )
        for fact_id in json.loads(group)
    }


def _strong_pair_fact_ids(connection, rows, location_paired, registry, through_period):
    """Find necessary strong-signal pairs before decoding any business facts.

    The exact signal reducer remains authoritative.  These three routes are
    deliberately supersets of its strong rules: shared exact location, same
    origin signature with shared evidence, or matching real-money coordinates
    with the same object or movement.
    """

    from .schema import table_name

    paired = set(location_paired)

    kinds = registry.models.keys()
    period_clause = " AND f.period<=?" if through_period is not None else ""
    origin_parts, parameters = [], []
    for kind in sorted(ORIGIN_KINDS & kinds):
        table = table_name(kind)
        columns = {row["name"] for row in connection.execute(f'PRAGMA table_info("{table}")')}
        # Structured arrays are normalized by the model before _signature;
        # their stored JSON text is not an admissible necessary equality key.
        scalar = [
            field
            for field in SIGNATURE_FIELDS[kind]
            if field in columns
            and field not in {"project_sources", "creditors", "assets", "annual_rate_percent"}
        ]
        signature = "json_array(" + ",".join(f'd."{field}"' for field in scalar) + ")"
        origin_parts.append(
            f"SELECT f.id,f.period,'{kind}' kind,{signature} signature "
            "FROM subject s CROSS JOIN fact_current c ON c.subject_id=s.id "
            f"CROSS JOIN {table} d ON d.revision_id=c.fact_id "
            f"CROSS JOIN fact_revision f ON f.id=c.fact_id WHERE s.kind='{kind}'" + period_clause
        )
        if through_period is not None:
            parameters.append(through_period)
    if origin_parts:
        origin = " UNION ALL ".join(origin_parts)
        # Each current fact occurs once in origin and fact_evidence's composite
        # primary key contributes each (fact, evidence) pair once.
        for (group,) in connection.execute(
            "WITH origin AS MATERIALIZED (" + origin + ") "
            "SELECT json_group_array(o.id) FROM origin o "
            "JOIN fact_evidence e ON e.fact_id=o.id "
            "GROUP BY o.kind,o.period,o.signature,e.evidence_digest "
            "HAVING count(*)>1",
            parameters,
        ):
            paired.update(json.loads(group))

    money_parts, parameters = [], []

    def money_leg(kind, category, account, direction, amount, object_id, *, movements="'[]'"):
        if kind not in kinds:
            return
        table = table_name(kind)
        money_parts.append(
            f"SELECT d.revision_id fact_id,'{kind}' kind,{category} category,"
            f"{account} account_id,d.actual_date actual_date,{direction} direction,"
            f"{amount} amount_fen,{object_id} object_id,{movements} movements "
            "FROM subject s CROSS JOIN fact_current c ON c.subject_id=s.id "
            f"CROSS JOIN {table} d ON d.revision_id=c.fact_id "
            f"CROSS JOIN fact_revision f ON f.id=c.fact_id WHERE s.kind='{kind}'" + period_clause
        )
        if through_period is not None:
            parameters.append(through_period)

    for kind, category in (
        ("payment", "bank"),
        ("cash_payment", "cash"),
        ("platform_payment", "platform"),
    ):
        money_leg(
            kind,
            f"'{category}'",
            f"d.{category}_account_id",
            "d.direction",
            "d.amount_fen",
            "d.counterparty_id",
            movements="d.movement_ids" if category == "platform" else "'[]'",
        )
    for kind, category in (
        ("funding", "bank"),
        ("cash_funding", "cash"),
        ("platform_funding", "platform"),
    ):
        money_leg(
            kind,
            f"'{category}'",
            f"d.{category}_account_id",
            "'inflow'",
            "d.amount_fen",
            "d.owner_id",
            movements="d.movement_ids" if category == "platform" else "'[]'",
        )
    for kind, direction in (
        ("managed_reserve_expense", "outflow"),
        ("managed_reserve_refund", "inflow"),
    ):
        money_leg(
            kind,
            "CASE WHEN d.bank_account_id IS NOT NULL THEN 'bank' "
            "WHEN d.cash_account_id IS NOT NULL THEN 'cash' ELSE 'platform' END",
            "coalesce(d.bank_account_id,d.cash_account_id,d.platform_account_id)",
            f"'{direction}'",
            "d.amount_fen",
            "d.counterparty_id",
            movements="d.movement_ids",
        )
    money_leg(
        "payroll_reserve_payment",
        "'bank'",
        "d.bank_account_id",
        "'outflow'",
        "d.amount_fen",
        "'payroll-group'",
    )
    money_leg(
        "bank_income",
        "'bank'",
        "d.bank_account_id",
        "'inflow'",
        "d.amount_fen",
        "d.counterparty_id",
    )
    money_leg(
        "loan_drawdown",
        "'bank'",
        "d.bank_account_id",
        "'inflow'",
        "d.principal_fen",
        "'loan-agreement:'||d.agreement_id",
    )
    money_leg(
        "funds_transfer",
        "'bank'",
        "d.source_bank_account_id",
        "'outflow'",
        "d.amount_fen",
        "'funds-account:bank:'||d.destination_bank_account_id",
    )
    money_leg(
        "funds_transfer",
        "'bank'",
        "d.destination_bank_account_id",
        "'inflow'",
        "d.amount_fen",
        "'funds-account:bank:'||d.source_bank_account_id",
    )
    money_leg(
        "cash_bank_transfer",
        "'bank'",
        "d.bank_account_id",
        "CASE WHEN d.direction='withdrawal' THEN 'outflow' ELSE 'inflow' END",
        "d.amount_fen",
        "'funds-account:cash:'||d.cash_account_id",
    )
    money_leg(
        "cash_bank_transfer",
        "'cash'",
        "d.cash_account_id",
        "CASE WHEN d.direction='withdrawal' THEN 'inflow' ELSE 'outflow' END",
        "d.amount_fen",
        "'funds-account:bank:'||d.bank_account_id",
    )
    money_leg(
        "bank_platform_transfer",
        "'bank'",
        "d.bank_account_id",
        "CASE WHEN d.direction='bank_to_platform' THEN 'outflow' ELSE 'inflow' END",
        "d.amount_fen",
        "'funds-account:platform:'||d.platform_account_id",
    )
    money_leg(
        "bank_platform_transfer",
        "'platform'",
        "d.platform_account_id",
        "CASE WHEN d.direction='bank_to_platform' THEN 'inflow' ELSE 'outflow' END",
        "d.amount_fen",
        "'funds-account:bank:'||d.bank_account_id",
        movements="d.movement_ids",
    )
    if money_parts:
        # Discovery only needs the identities that participate in a strong
        # signal. Enumerating every pair first creates quadratic intermediate
        # rows when many entries share a coordinate. Exact candidate reduction
        # below still checks each resulting business against the domain rules.
        # A fact contributes at most one leg to a given object coordinate;
        # transfer legs differ in category or direction. Movement lists may
        # repeat a value, so only their aggregation retains DISTINCT.
        coordinates = "category,account_id,actual_date,direction,amount_fen"
        for (group,) in connection.execute(
            "WITH legs AS MATERIALIZED (" + " UNION ALL ".join(money_parts) + ") "
            "SELECT json_group_array(fact_id) FROM legs "
            "WHERE object_id IS NOT NULL AND account_id IS NOT NULL "
            "AND actual_date IS NOT NULL GROUP BY " + coordinates + ",kind,object_id "
            "HAVING count(*)>1 UNION ALL "
            "SELECT json_group_array(DISTINCT fact_id) FROM legs "
            "JOIN json_each(legs.movements) m "
            "WHERE account_id IS NOT NULL AND actual_date IS NOT NULL "
            "AND m.value IS NOT NULL GROUP BY " + coordinates + ",m.value "
            "HAVING count(DISTINCT fact_id)>1",
            parameters,
        ):
            paired.update(json.loads(group))
    return paired


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
    same_kind = proposed.kind == candidate.kind
    actual_money_pair = proposed.kind in ACTUAL_MONEY_KINDS and candidate.kind in ACTUAL_MONEY_KINDS
    if not same_kind and not actual_money_pair:
        return None, []
    exact = same_kind and _signature(proposed) == _signature(candidate)
    proposed_core = _core_signature(proposed) if same_kind else None
    same_core = proposed_core is not None and proposed_core == _core_signature(candidate)
    money_matches = _money_matches(proposed, candidate) if actual_money_pair else []
    shared_evidence = sorted(set(proposed_evidence) & set(candidate_evidence))
    shared_business_evidence = [item for item in shared_evidence if item not in supporting]
    proposed_by_evidence = _locations_by_evidence(proposed_locations)
    candidate_by_evidence = _locations_by_evidence(candidate_locations)
    common_locations = sorted(
        item
        for item in _location_keys(proposed_locations) & _location_keys(candidate_locations)
        if item[0] not in supporting
    )
    distinct_material_proof = any(
        proposed_by_evidence.get(item)
        and candidate_by_evidence.get(item)
        and proposed_by_evidence[item].isdisjoint(candidate_by_evidence[item])
        for item in shared_evidence
    )
    common_movements = sorted(
        {
            movement
            for left, right in money_matches
            for movement in set(left["movement_ids"]) & set(right["movement_ids"])
        }
    )
    distinct_platform_proof = any(
        left["category"] == "platform"
        and left["movement_ids"]
        and right["movement_ids"]
        and set(left["movement_ids"]).isdisjoint(right["movement_ids"])
        for left, right in money_matches
    )
    distinct_proof = distinct_material_proof or distinct_platform_proof
    same_money_object = any(
        left["object_id"] is not None and left["object_id"] == right["object_id"]
        for left, right in money_matches
    )
    exact_money_source = bool(money_matches) and bool(
        common_movements or (common_locations and not distinct_platform_proof)
    )
    signals = []
    if exact_money_source:
        signal = {
            "code": (
                "same_exact_material_location" if common_locations else "same_complete_actual_money"
            ),
            "matched_fields": [
                "funds_category",
                "funds_account_id",
                "actual_date",
                "direction",
                "amount_fen",
                *(("movement_ids",) if common_movements else ()),
            ],
        }
        if common_locations:
            signal["source_locations"] = [
                {"evidence_digest": evidence, "location": location}
                for evidence, location in common_locations
            ]
        signals.append(signal)
    elif common_locations and same_core:
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
    if proposed.kind in ORIGIN_KINDS and exact and shared_business_evidence and not distinct_proof:
        signals.append(
            {
                "code": "same_complete_signature_and_evidence",
                "matched_fields": ["duplicate_role", "complete_business_signature"],
                "evidence": shared_business_evidence,
            }
        )
    if (
        same_kind
        and exact
        and money_matches
        and same_money_object
        and not distinct_proof
        and not exact_money_source
    ):
        signals.append(
            {
                "code": "same_complete_actual_money",
                "matched_fields": [
                    "funds_category",
                    "funds_account_id",
                    "actual_date",
                    "direction",
                    "amount_fen",
                    "business_object",
                ],
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
    if money_matches:
        weak.append(
            {
                "code": "same_actual_money_coordinates",
                "matched_fields": [
                    "funds_category",
                    "funds_account_id",
                    "actual_date",
                    "direction",
                    "amount_fen",
                ],
                "distinct_locations_proven": distinct_proof,
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


def _review_pair_identity(connection, proposed, candidate):
    """Current adoption identity; never changes the immutable v2 record digest.

    Reader/disposition revisions remain verified audit references. A new ID alone
    does not change an original location or its approved allocation semantics.
    """
    sides, resolutions = [], {}
    for side in (proposed, candidate):
        locations = {}
        for item in side.get("material_sources", ()):
            source = connection.execute(
                "SELECT period,category,purpose,supporting_purpose,specification "
                "FROM fact_material_source_v2 WHERE revision_id=?",
                (item["source_fact_id"],),
            ).fetchone()
            if source is None:
                raise KernelError("duplicate_review_corrupt", "疑似重复复核的原件版本不存在")
            original = {
                "source_id": item["source_id"],
                "evidence_digest": item["evidence_digest"],
                "location": item["location"],
                "source_semantics": [*source[:4], loads_unique(source["specification"])],
            }
            key = canonical(original)
            locations[key] = original
            resolution_id = item.get("resolution_fact_id")
            if resolution_id is not None:
                row = connection.execute(
                    "SELECT r.period,r.treatment,r.amount_fen,r.recognition_period "
                    "FROM fact_material_resolution_v2 r WHERE r.revision_id=?",
                    (resolution_id,),
                ).fetchone()
                if row is None:
                    raise KernelError(
                        "duplicate_review_corrupt", "疑似重复复核的材料处置版本不存在"
                    )
                links = [
                    {**dict(link), "fact_digest": link["fact_digest"].hex()}
                    for link in connection.execute(
                        "SELECT l.subject_id,l.fact_kind,l.amount_field,l.amount_fen,"
                        "l.recognition_period,f.digest AS fact_digest "
                        "FROM fact_material_resolution_v2_links l "
                        "JOIN fact_revision f ON f.id=l.fact_id WHERE l.revision_id=? "
                        "ORDER BY l.subject_id,l.fact_kind,l.amount_field,l.amount_fen,"
                        "l.recognition_period",
                        (resolution_id,),
                    )
                ]
                semantics = canonical([list(row), links])
                resolutions.setdefault(key, set()).add(semantics)
        sides.append(
            {
                "subject_id": side["subject_id"],
                "revision": side["revision"],
                "content_digest": side["content_digest"],
                "material_sources": [locations[key] for key in sorted(locations)],
            }
        )
    return canonical(sorted(sides, key=canonical)), resolutions


def _review_pair_compatible(connection, saved, current):
    if canonical(saved[1]["signals"]) != canonical(current[1]["signals"]):
        return False
    old_sides, old_resolutions = _review_pair_identity(connection, *saved)
    new_sides, new_resolutions = _review_pair_identity(connection, *current)
    if old_sides != new_sides:
        return False
    # Registration may precede the first material disposition. Once a reviewed
    # disposition exists, its complete allocation is binding, not only the
    # amount allocated to either duplicate candidate.
    return all(new_resolutions.get(key) == value for key, value in old_resolutions.items())


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
        batch_cache: dict | None = None,
    ):
        # Exact fact rows cover complete same-kind signatures. Actual-money
        # rows use their indexed real date across all supported money kinds.
        # Shared evidence remains cross-period so a copied-wrong period is seen.
        source_evidence = sorted({item.evidence_digest for item in source_locations})
        fact_digest = digest(fact.model_dump(mode="json"))
        scopes = ["SELECT id FROM fact_revision WHERE digest=?"]
        parameters: list[Any] = [fact_digest]
        check_cte = ""
        check_parameters: list[Any] = []
        candidate_kinds = ACTUAL_MONEY_KINDS if fact.kind in ACTUAL_MONEY_KINDS else {fact.kind}
        candidate_kinds = candidate_kinds & self.store.registry.models.keys()
        if fact.kind in ACTUAL_MONEY_KINDS:
            for kind in sorted(candidate_kinds):
                scopes.append(f"SELECT revision_id FROM fact_{kind} WHERE actual_date=?")
                parameters.append(str(fact.actual_date))
        evidence_scope = (
            "SELECT e.fact_id FROM fact_evidence e "
            "JOIN fact_revision f ON f.id=e.fact_id "
            "WHERE e.evidence_digest IN (SELECT unhex(value) FROM json_each(?))"
        )
        evidence_values: tuple[Any, ...] = (canonical(sorted(set(evidence))),)
        if strong_only:
            evidence_scope += " AND f.period<>?"
            evidence_values += (fact.period.ordinal,)
        if evidence:
            scopes.append(evidence_scope)
            parameters.extend(evidence_values)
        if (
            source_evidence
            and {
                "material_source_v2",
                "material_resolution_v2",
            }
            <= self.store.registry.models.keys()
        ):
            scopes.append(
                "SELECT l.fact_id FROM fact_material_resolution_v2_links l "
                "JOIN fact_current mc ON mc.fact_id=l.revision_id "
                "JOIN fact_material_resolution_v2 r ON r.revision_id=mc.fact_id "
                "JOIN fact_material_source_v2 ms ON ms.revision_id=r.source_fact_id "
                "WHERE ms.evidence_digest IN(SELECT value FROM json_each(?))"
            )
            parameters.append(canonical(source_evidence))
        if source_evidence:
            if batch_cache is None or not batch_cache.get("manifest_scope_verified"):
                _require_sql_manifest_scope(connection, predicate="d.result_fact_id IS NOT NULL")
                if batch_cache is not None:
                    batch_cache["manifest_scope_verified"] = True
            check_cte = (
                "matching_checks(id) AS (SELECT DISTINCT d.result_fact_id "
                "FROM business_duplicate_check d,"
                "json_each(CASE WHEN json_valid(d.manifest) THEN d.manifest "
                "ELSE '{}' END,'$.source_locations') location "
                "WHERE d.result_fact_id IS NOT NULL AND "
                "CASE WHEN json_valid(location.value) "
                "THEN json_extract(location.value,'$.evidence_digest') END "
                "IN(SELECT value FROM json_each(?))),"
                "check_descendants(id) AS (SELECT id FROM matching_checks UNION ALL "
                "SELECT i.after_fact_id FROM check_descendants d "
                "JOIN identity_correction_item i ON i.before_fact_id=d.id "
                "AND i.action='reassign'),"
            )
            check_parameters.append(canonical(source_evidence))
            scopes.append("SELECT id FROM check_descendants")
        kind_values = canonical(sorted(candidate_kinds))

        def select_rows(
            selected_scopes: Sequence[str],
            scope_parameters: Sequence[Any],
            *,
            location_cte: str = "",
            location_parameters: Sequence[Any] = (),
            exclude_subject: bool,
        ):
            if not selected_scopes:
                return []
            # Drive the final lookup from the indexed candidate scopes. SQLite
            # may otherwise scan every historical subject before testing the
            # candidate set. DISTINCT preserves the former UNION semantics.
            sql = (
                "WITH RECURSIVE "
                + location_cte
                + "raw_candidates(id) AS ("
                + " UNION ALL ".join(selected_scopes)
                + "), candidates(id) AS (SELECT DISTINCT id FROM raw_candidates) "
                "SELECT f.id,f.subject_id,f.revision,f.digest,f.period,"
                "EXISTS(SELECT 1 FROM calculation c JOIN calculation_current cc "
                "ON cc.calculation_id=c.id WHERE cc.subject_id=f.subject_id "
                "AND c.fact_id=f.id) published_current "
                "FROM candidates wanted CROSS JOIN fact_revision f "
                "CROSS JOIN fact_current fc CROSS JOIN subject s WHERE "
                "f.id=wanted.id AND fc.fact_id=f.id AND s.id=f.subject_id "
                "AND s.kind IN (SELECT value FROM json_each(?))"
            )
            values = [*location_parameters, *scope_parameters, kind_values]
            if exclude_subject:
                sql += " AND f.subject_id<>?"
                values.append(subject_id)
            if through_period is not None:
                sql += " AND f.period<=?"
                values.append(through_period)
            sql += " ORDER BY f.period DESC,f.id DESC"
            return list(connection.execute(sql, values))

        if batch_cache is None:
            return select_rows(
                scopes,
                parameters,
                location_cte=check_cte,
                location_parameters=check_parameters,
                exclude_subject=True,
            )

        # A batch reads every candidate before writing its first proposal.
        # Evidence/date/material scopes are often identical across hundreds of
        # rows, but each fact digest and own-subject exclusion remain distinct.
        # Cache only that common authoritative SQL result within this batch.
        common_scopes = scopes[1:]
        common_parameters = parameters[1:]
        common_key = (
            check_cte,
            tuple(check_parameters),
            tuple(common_scopes),
            tuple(common_parameters),
            kind_values,
            through_period,
        )
        common_cache = batch_cache["candidate_rows"]
        if common_key in common_cache:
            # Dict insertion order gives a tiny batch-local LRU without a
            # second cache layer or retained state after prepare_batch.
            common_rows = common_cache.pop(common_key)
        else:
            common_rows = select_rows(
                common_scopes,
                common_parameters,
                location_cte=check_cte,
                location_parameters=check_parameters,
                exclude_subject=False,
            )
        common_cache[common_key] = common_rows
        if len(common_cache) > 8:
            del common_cache[next(iter(common_cache))]
        exact_rows = select_rows(scopes[:1], parameters[:1], exclude_subject=True)
        rows = {row["id"]: row for row in common_rows if row["subject_id"] != subject_id}
        rows.update((row["id"], row) for row in exact_rows)
        return sorted(rows.values(), key=lambda row: (row["period"], row["id"]), reverse=True)

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
        _batch_cache=None,
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
            batch_cache=_batch_cache,
        )
        identifiers = {row["id"] for row in rows}
        if _batch_cache is None:
            versions = self.store.facts(connection, identifiers)
        else:
            known = _batch_cache["facts"]
            missing = identifiers - known.keys()
            if missing:
                known.update(self.store.facts(connection, missing))
            versions = {ident: known[ident] for ident in identifiers}
        materials_enabled = {
            "material_source_v2",
            "material_resolution_v2",
        } <= self.store.registry.models.keys()
        if materials_enabled:
            if _batch_cache is not None:
                _material_cache = _batch_cache["materials"]
            material = {
                fact_id: [dict(item) for item in _material_cache[fact_id]]
                for fact_id in versions
                if _material_cache is not None and fact_id in _material_cache
            }
            missing_material = set(versions) - set(material)
            if missing_material:
                loaded = _material_locations(connection, missing_material)
                material.update(loaded)
                if _batch_cache is not None:
                    _batch_cache["materials"].update(loaded)
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
        # All candidates are checked before the batch writes its first fact.
        # Share exact immutable candidates only during this read phase; every
        # proposal still selects its own authoritative scope and applies rules.
        cache = {
            "facts": {},
            "materials": {},
            "candidate_rows": {},
            "manifest_scope_verified": False,
        }
        prepared = [
            self.prepare(connection, **proposal, _batch_cache=cache) for proposal in proposals
        ]
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
        by_money: dict[tuple[Any, ...], list[int]] = {}
        by_location: dict[tuple[str, str, str], list[int]] = {}
        # Compare the proposed rows with each other.  No row is written until
        # the caller has reviewed every result, so a failure remains atomic.
        for index, left in enumerate(proposals):
            left_fact = left["fact"]
            if not self.eligible(left_fact.kind):
                continue
            signature_key = (left_fact.kind, signatures[index])
            candidate_indexes = set(by_signature.get(signature_key, ()))
            money_keys = {_money_key(leg) for leg in _actual_money_legs(left_fact)}
            for key in money_keys:
                candidate_indexes.update(by_money.get(key, ()))
            location_role = (
                "actual_money" if left_fact.kind in ACTUAL_MONEY_KINDS else left_fact.kind
            )
            for item in locations[index]:
                candidate_indexes.update(
                    by_location.get((location_role, item["evidence_digest"], item["location"]), ())
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
            for key in money_keys:
                by_money.setdefault(key, []).append(index)
            for item in locations[index]:
                by_location.setdefault(
                    (location_role, item["evidence_digest"], item["location"]), []
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
            if not _reuse_compatible(prepared["proposed"], selected_candidate):
                raise KernelError(
                    "duplicate_candidate_invalid",
                    "跨类型复用只适用于同一实际资金的强候选",
                )
            selected = selected_candidate["fact_id"]
            if selected is None and fact_ids_by_subject is not None:
                selected = fact_ids_by_subject.get(selected_candidate["subject_id"])
            if selected is None:
                raise KernelError("duplicate_candidate_invalid", "批内拟登记业务不能作为复用目标")
            current = connection.execute(
                "SELECT 1 FROM fact_current WHERE subject_id=? AND fact_id=?",
                (selected_candidate["subject_id"], selected),
            ).fetchone()
            if current is None:
                raise KernelError(
                    "duplicate_candidate_expired",
                    "复用对象的当前事实版本已变化，请重新核对",
                )
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
        review_basis = (
            [item.model_dump(mode="json") for item in value.review_basis] if value else []
        )
        review_json = canonical(review_basis)
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
            manifest=manifest,
            review_basis=review_basis,
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
        batch_cache: dict | None = None,
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
            _batch_cache=batch_cache,
        )

    def _valid_pair_reviews(
        self,
        connection,
        current_pairs: Mapping[str, tuple[dict, dict]],
    ) -> set[str]:
        wanted = set(current_pairs)
        if not wanted:
            return set()
        _require_sql_manifest_scope(connection, predicate="d.action='create_separate'")
        result = set()
        subjects = {side["subject_id"] for pair in current_pairs.values() for side in pair}
        by_subjects = {}
        for pair_digest, pair in current_pairs.items():
            by_subjects.setdefault(frozenset(side["subject_id"] for side in pair), []).append(
                (pair_digest, pair)
            )
        for row in connection.execute(
            "SELECT d.* FROM business_duplicate_check d "
            "WHERE d.action='create_separate' AND d.proposed_subject_id "
            "IN(SELECT value FROM json_each(?))",
            (canonical(sorted(subjects)),),
        ):
            manifest, bases = _require_check_record(row)
            review = DuplicateReview(
                candidate_digest=manifest["candidate_digest"],
                action="create_separate",
                explanation=row["explanation"],
                review_basis=tuple(bases),
            )
            self._verify_review_evidence(connection, review, manifest, current=False)
            for candidate in manifest["strong_candidates"]:
                _verify_candidate_materials(connection, candidate)
                key = frozenset((manifest["proposed"]["subject_id"], candidate["subject_id"]))
                for pair_digest, pair in by_subjects.get(key, ()):
                    if _review_pair_compatible(connection, (manifest["proposed"], candidate), pair):
                        result.add(pair_digest)
        return result

    def unresolved(
        self,
        connection,
        *,
        subject_ids: Iterable[str] | None = None,
        through_period: str | YearMonth | None = None,
        _inspection_cache=None,
        _query_reads=None,
    ) -> list[dict]:
        targets = None if subject_ids is None else set(subject_ids)
        # A publication batch should inspect the current authority once, then
        # decode only target facts that could form a strong pair.  A one-off
        # subject lookup keeps its narrower indexed path.
        preselect = targets is None or len(targets) > 8
        parameters: list[Any] = [canonical(sorted(ELIGIBLE_KINDS))]
        where = ["s.kind IN (SELECT value FROM json_each(?))"]
        if targets is not None and not preselect:
            where.append("f.subject_id IN (SELECT value FROM json_each(?))")
            parameters.append(canonical(sorted(targets)))
        limit = YearMonth(through_period).ordinal if through_period is not None else None
        if limit is not None:
            where.append("f.period<=?")
            parameters.append(limit)
        have_materials = {
            "material_source_v2",
            "material_resolution_v2",
        } <= self.store.registry.models.keys()
        narrowed = None
        if targets is None and limit is not None and have_materials:
            from .duplicate_freeze import narrowed_duplicate_candidates
            from .engine import Engine

            verification_engine = Engine(self.store)
            narrowed = narrowed_duplicate_candidates(
                verification_engine,
                connection,
                limit,
                inspection_cache=_inspection_cache,
                query_reads=_query_reads,
            )
        if narrowed is not None:
            from .integrity import verify_sources

            paired, changed_ids, changed_locations = narrowed
            _require_current_check_coverage(connection, changed_ids)
            if paired:
                if paired - changed_ids:
                    verify_sources(verification_engine, connection, fact_ids=paired - changed_ids)
                where.append("f.id IN (SELECT value FROM json_each(?))")
                parameters.append(canonical(sorted(paired)))
                rows = list(
                    connection.execute(
                        "SELECT f.id,f.subject_id,f.period,s.kind FROM fact_current c "
                        "JOIN fact_revision f ON f.id=c.fact_id "
                        "JOIN subject s ON s.id=f.subject_id "
                        "WHERE " + " AND ".join(where) + " ORDER BY f.period,f.subject_id",
                        parameters,
                    )
                )
            else:
                rows = []
            fact_ids = [row["id"] for row in rows]
        else:
            rows = list(
                connection.execute(
                    "SELECT f.id,f.subject_id,f.period,s.kind FROM fact_current c "
                    "JOIN fact_revision f ON f.id=c.fact_id JOIN subject s ON s.id=f.subject_id "
                    "WHERE " + " AND ".join(where) + " ORDER BY f.period,f.subject_id",
                    parameters,
                )
            )
            fact_ids = [row["id"] for row in rows]
            _require_current_check_coverage(connection, fact_ids)
        if preselect:
            if narrowed is None:
                location_paired = (
                    _strong_location_pair_fact_ids(connection, rows) if have_materials else set()
                )
                paired = _strong_pair_fact_ids(
                    connection, rows, location_paired, self.store.registry, limit
                )
            if narrowed is not None:
                old_locations = _material_locations(connection, paired - changed_ids)
                locations_by_fact = {
                    fact_id: changed_locations[fact_id] for fact_id in paired & changed_ids
                }
                locations_by_fact.update(old_locations)
            else:
                old_locations = None
                locations_by_fact = (
                    _material_locations(connection, paired)
                    if have_materials
                    else _source_locations_from_checks(connection, paired, current_sources=True)
                )
            checked_locations = (
                old_locations.values() if old_locations is not None else locations_by_fact.values()
            )
            locations = {
                (
                    item["source_id"],
                    item["source_fact_id"],
                    item["evidence_digest"],
                    item["location"],
                ): SourceLocation.model_validate(
                    {key: item[key] for key in SourceLocation.model_fields}
                )
                for facts in checked_locations
                for item in facts
            }
            if locations:
                _validate_source_locations(
                    connection, list(locations.values()), _inspection_cache=_inspection_cache
                )
            rows = [
                row
                for row in rows
                if row["id"] in paired and (targets is None or row["subject_id"] in targets)
            ]
        else:
            locations_by_fact = (
                _material_locations(connection, fact_ids)
                if have_materials
                else _source_locations_from_checks(connection, fact_ids, current_sources=True)
            )
        candidates, seen, current_pairs = [], set(), {}
        # This method only reads authority. Share successful manifest syntax
        # proof across its candidate preparations, never across write phases.
        candidate_cache = {
            "facts": {},
            "materials": dict(locations_by_fact),
            "candidate_rows": {},
            "manifest_scope_verified": False,
        }
        for row in rows:
            prepared = self._current_prepared(
                connection,
                row["id"],
                through_period=limit,
                source_locations=locations_by_fact[row["id"]],
                strong_only=True,
                material_cache=locations_by_fact,
                batch_cache=candidate_cache,
            )
            if prepared is None:
                continue
            for candidate in prepared["strong_candidates"]:
                pair = candidate["pair_digest"]
                if pair in seen:
                    continue
                seen.add(pair)
                current_pairs[pair] = (prepared["proposed"], candidate)
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
            current_pairs,
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

    def close_readiness(
        self, connection, period: str | YearMonth, *, _inspection_cache=None, _query_reads=None
    ) -> list[dict]:
        return self.unresolved(
            connection,
            through_period=period,
            _inspection_cache=_inspection_cache,
            _query_reads=_query_reads,
        )

    def business_detail(self, connection, subject_id: str, *, summary: bool = False) -> dict:
        _require_sql_manifest_scope(connection)
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
