"""Replay inventory through public controls and re-read the original sources."""

from __future__ import annotations

import copy
import uuid
from hashlib import sha256

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from .accounting_periods import canonical_sha256
from .company_notes import read_company_notes, read_company_notes_bytes
from .material_schemas import MaterialComponentLink
from .material_service import MaterialService
from .models import (
    AccountingPeriod,
    MybankPaymentSourceVersion,
    Organization,
    PeriodMaterialInventory,
)


def export_material_operations(session, org_id, maps, company_dir):
    from . import replay_cli as replay

    org = session.get(Organization, org_id)
    notes = read_company_notes(org)
    operations = []
    if notes["exists"]:
        path = company_dir / "业务说明.md"
        raw = read_company_notes_bytes(org)
        if raw is None or sha256(raw).hexdigest() != notes["sha256"]:
            raise replay.ReplayError("REPLAY_COMPANY_NOTES_CHANGED")
        path.write_bytes(raw)
        operations.append(
            {"key": "company-notes", "kind": "company_notes", "relative_path": path.name}
        )
    if not inspect(session.connection()).has_table("period_material_inventories"):
        return operations
    if inspect(session.connection()).has_table("mybank_payment_source_versions"):
        seen = set()
        for source in session.scalars(
            select(MybankPaymentSourceVersion)
            .where(MybankPaymentSourceVersion.org_id == org_id)
            .order_by(
                MybankPaymentSourceVersion.payroll_period,
                MybankPaymentSourceVersion.source_kind,
                MybankPaymentSourceVersion.revision.desc(),
            )
        ):
            identity = (source.payroll_period, source.source_kind)
            if identity in seen:
                continue
            seen.add(identity)
            key = f"mybank-source:{source.payroll_period}:{source.source_kind}"
            operations.append(
                {
                    "key": key,
                    "kind": "tool",
                    "tool": "finance_import_mybank_payment_source",
                    "allowed_statuses": ["recorded"],
                    "request": replay._replace_stable_references(
                        {
                            "org_id": str(org_id),
                            "payroll_period": source.payroll_period,
                            "source_kind": source.source_kind,
                            "evidence_id": str(source.evidence_id),
                            "expected_revision": 0,
                            "idempotency_key": replay._replay_idempotency(key),
                        },
                        org_id=org_id,
                        maps=maps,
                    ),
                }
            )
    service = MaterialService(session)
    for period_id, row in sorted(
        service._latest_all(org_id).items(),
        key=lambda item: session.get(AccountingPeriod, item[0]).start_date,
    ):
        period = session.get(AccountingPeriod, period_id)
        content = copy.deepcopy(row.content)
        sources = []

        def stable_item_key(key):
            original_id, suffix = key.split(":", 1)
            return f"{maps['evidence'][original_id]['sha256']}:{suffix}"

        for source_id, source in content["sources"].items():
            spec = source["spec"] | {"evidence_id": maps["evidence"][source_id]}
            sources.append({"original_id": maps["evidence"][source_id]["sha256"], "spec": spec})
        resolutions = []

        def portable_links(links):
            for raw in links:
                component, error = service._link(org_id, MaterialComponentLink.model_validate(raw))
                if error:
                    return []
                raw["source"] = {"component_id": str(component.id)}
            return links

        for resolution in content["resolutions"].values():
            resolution["item_key"] = stable_item_key(resolution["item_key"])
            if resolution.get("duplicate_of"):
                resolution["duplicate_of"] = stable_item_key(resolution["duplicate_of"])
            if any(
                service._link(org_id, MaterialComponentLink.model_validate(raw))[1]
                for raw in resolution.get("links", [])
            ):
                resolution["treatment"] = "pending"
                resolution["links"] = []
            resolution["links"] = portable_links(resolution.get("links", []))
            target = resolution.get("target_period_id")
            if target:
                target_period = session.get(AccountingPeriod, uuid.UUID(target))
                resolution["target_period_id"] = {
                    "$ref": "period",
                    "period_month": target_period.start_date.strftime("%Y-%m"),
                }
            resolutions.append(
                replay._replace_stable_references(resolution, org_id=org_id, maps=maps)
            )
        bank_reviews = [
            {
                "bank": maps["bank"][key],
                "links": replay._replace_stable_references(
                    portable_links(links), org_id=org_id, maps=maps
                ),
            }
            for key, links in content.get("bank_resolutions", {}).items()
        ]
        operations.append(
            {
                "key": f"materials:{period.start_date:%Y-%m}",
                "kind": "material_inventory",
                "period_month": period.start_date.strftime("%Y-%m"),
                "sources": sources,
                "resolutions": resolutions,
                "requires_notes_review": content.get("reviewed_notes_hash") != notes["sha256"],
                "expected_complete": service.check(org_id, period_id)["satisfied"],
                "splits": [
                    {
                        "item_key": stable_item_key(item["key"]),
                        "basis": item["split_basis"],
                        "parts": [
                            {
                                "amount_fen": content["items"][child]["amount_fen"],
                                "label": content["items"][child]["excerpt"],
                            }
                            for child in item["split_children"]
                        ],
                    }
                    for item in content["items"].values()
                    if item.get("split_children")
                ],
                "bank_reviews": bank_reviews,
            }
        )
    return operations


def execute_material_operation(operation, company_dir, resolver):
    from . import replay_cli as replay

    org_id = str(resolver.org_id)
    if operation["kind"] == "company_notes":
        path = replay._safe_package_file(company_dir, operation["relative_path"])
        current = replay._call_tool("finance_get_company_notes", {"org_id": org_id})
        content = path.read_bytes().decode("utf-8-sig")
        if current["company_notes"]["exists"] and current["company_notes"]["content"] != content:
            raise replay.ReplayError("REPLAY_COMPANY_NOTES_CONFLICT")
        result = replay._call_tool(
            "finance_update_company_notes",
            {
                "org_id": org_id,
                "expected_sha256": current["company_notes"]["sha256"],
                "content": content,
            },
        )
        replay._require_status(result, ["updated"], operation_key=operation["key"])
        return result
    period_id = resolver.materialize({"$ref": "period", "period_month": operation["period_month"]})
    common = {"org_id": org_id, "period_id": period_id}
    current = replay._call_tool("finance_get_period_material_completeness", common)
    with Session(resolver.engine) as session:
        saved = {
            row.idempotency_key: row.revision
            for row in session.scalars(
                select(PeriodMaterialInventory).where(
                    PeriodMaterialInventory.org_id == resolver.org_id,
                    PeriodMaterialInventory.idempotency_key.in_(
                        [
                            replay._replay_idempotency(operation["key"] + suffix)
                            for suffix in (":sources", ":review")
                        ]
                    ),
                )
            )
        }
    review_key = replay._replay_idempotency(operation["key"] + ":review")
    if review_key in saved:
        if operation.get("expected_complete") and not current["satisfied"]:
            raise replay.ReplayError("REPLAY_MATERIAL_INCOMPLETE")
        return {"status": "recorded", "revision": saved[review_key], "idempotent_replay": True}
    specs = [resolver.materialize(source["spec"]) for source in operation["sources"]]
    ids = {
        source["original_id"]: spec["evidence_id"]
        for source, spec in zip(operation["sources"], specs, strict=True)
    }

    def remap_key(key):
        old, suffix = key.split(":", 1)
        return f"{ids[old]}:{suffix}"

    source_key = replay._replay_idempotency(operation["key"] + ":sources")
    registered = (
        {"status": "recorded", "revision": current["revision"]}
        if source_key in saved
        else replay._call_tool(
            "finance_register_period_materials",
            common
            | {
                "sources": specs,
                "expected_revision": current["revision"],
                "idempotency_key": replay._replay_idempotency(operation["key"] + ":sources"),
            },
        )
    )
    replay._require_status(registered, ["recorded"], operation_key=operation["key"])
    if operation.get("requires_notes_review"):
        raise replay.ReplayError("REPLAY_MATERIAL_NOTES_REVIEW_REQUIRED")
    # IDs and source hashes legitimately change on replay; bind newly verified component facts.
    with Session(resolver.engine) as session:
        service = MaterialService(session)

        def refresh_links(links):
            for raw in links:
                link = MaterialComponentLink.model_validate(raw)
                component = service._component(resolver.org_id, link.source)
                if component is None:
                    raise replay.ReplayError("REPLAY_MATERIAL_COMPONENT_MISSING")
                raw["expected_facts_hash"] = canonical_sha256(component.facts)
            return links

        resolutions = resolver.materialize(operation["resolutions"])
        for resolution in resolutions:
            resolution["item_key"] = remap_key(resolution["item_key"])
            if resolution.get("duplicate_of"):
                resolution["duplicate_of"] = remap_key(resolution["duplicate_of"])
            refresh_links(resolution["links"])
        bank_resolutions = {
            resolver.materialize(item["bank"]): refresh_links(resolver.materialize(item["links"]))
            for item in operation.get("bank_reviews", [])
        }
    notes = replay._call_tool("finance_get_company_notes", {"org_id": org_id})["company_notes"]
    splits = copy.deepcopy(operation.get("splits", []))
    for split in splits:
        split["item_key"] = remap_key(split["item_key"])
    result = replay._call_tool(
        "finance_update_period_material_inventory",
        common
        | {
            "expected_revision": registered["revision"],
            "idempotency_key": replay._replay_idempotency(operation["key"] + ":review"),
            "reviewed_notes_hash": notes["sha256"],
            "resolutions": resolutions,
            "splits": splits,
            "subsequent_bank_resolutions": bank_resolutions,
        },
    )
    replay._require_status(result, ["recorded"], operation_key=operation["key"])
    if operation.get("expected_complete") and not result["completeness"]["satisfied"]:
        raise replay.ReplayError("REPLAY_MATERIAL_INCOMPLETE")
    return result
