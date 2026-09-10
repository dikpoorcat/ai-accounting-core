"""Small append-only monthly inventory and a single deterministic close gate."""

from __future__ import annotations

import copy
import uuid
from collections import defaultdict
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile

from sqlalchemy import or_, select, text

from .accounting_periods import canonical_sha256
from .company_notes import ensure_company_notes, read_company_notes
from .fact_requirements import AccountingFactIssue
from .material_reader import inspect_material, verify_material_bytes
from .material_schemas import MaterialComponentLink, MaterialResolution
from .models import (
    AccountingPeriod,
    BankTransaction,
    BankTransactionMatch,
    BusinessEvent,
    BusinessEventComponent,
    Employee,
    Evidence,
    LaborRemunerationBatch,
    OpenItem,
    Organization,
    PayrollBatch,
    PeriodMaterialInventory,
    Settlement,
    Voucher,
)


def lock_material_company(session, org_id):
    if session.get_bind().dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended('tax-period-org:' || :org,0))"),
            {"org": str(org_id)},
        )


class MaterialService:
    def __init__(self, session):
        self.session = session

    def period(self, org_id, period_id, *, writing=False):
        if writing:
            lock_material_company(self.session, org_id)
        period = self.session.get(AccountingPeriod, period_id, populate_existing=True)
        if period is None or period.org_id != org_id:
            raise ValueError("MATERIAL_PERIOD_NOT_FOUND")
        if writing and period.status != "open":
            raise ValueError("MATERIAL_PERIOD_NOT_OPEN")
        return period

    def latest(self, org_id, period_id):
        return self.session.scalar(
            select(PeriodMaterialInventory)
            .where(
                PeriodMaterialInventory.org_id == org_id,
                PeriodMaterialInventory.period_id == period_id,
            )
            .order_by(PeriodMaterialInventory.revision.desc(), PeriodMaterialInventory.period_id)
            .limit(1)
        )

    def _latest_all(self, org_id):
        rows = self.session.scalars(
            select(PeriodMaterialInventory)
            .where(PeriodMaterialInventory.org_id == org_id)
            .order_by(PeriodMaterialInventory.period_id, PeriodMaterialInventory.revision.desc())
        )
        result = {}
        for row in rows:
            result.setdefault(row.period_id, row)
        return result

    def _start(self, request):
        lock_material_company(self.session, request.org_id)
        self.period(request.org_id, request.period_id)
        digest = canonical_sha256(request.model_dump(mode="json"))
        replay = self.session.scalar(
            select(PeriodMaterialInventory).where(
                PeriodMaterialInventory.org_id == request.org_id,
                PeriodMaterialInventory.idempotency_key == request.idempotency_key,
            )
        )
        if replay:
            if replay.request_hash != digest:
                raise ValueError("MATERIAL_IDEMPOTENCY_CONFLICT")
            return replay, None, digest
        self.period(request.org_id, request.period_id, writing=True)
        latest = self.latest(request.org_id, request.period_id)
        revision = latest.revision if latest else 0
        if request.expected_revision != revision:
            raise ValueError("MATERIAL_INVENTORY_STALE")
        content = (
            copy.deepcopy(latest.content)
            if latest
            else {
                "sources": {},
                "items": {},
                "resolutions": {},
                "bank_resolutions": {},
                "reviewed_notes_hash": None,
            }
        )
        return None, content, digest

    def _save(self, request, content, digest):
        row = PeriodMaterialInventory(
            org_id=request.org_id,
            period_id=request.period_id,
            revision=request.expected_revision + 1,
            idempotency_key=request.idempotency_key,
            request_hash=digest,
            content=content,
        )
        self.session.add(row)
        self.session.flush()
        return {
            "status": "recorded",
            "inventory_id": str(row.id),
            "revision": row.revision,
            "completeness": self.check(request.org_id, request.period_id),
        }

    def register(self, request):
        replay, content, digest = self._start(request)
        if replay:
            return {"status": "recorded", "revision": replay.revision, "idempotent_replay": True}
        org = self.session.get(Organization, request.org_id)
        ensure_company_notes(org)
        all_latest = self._latest_all(request.org_id)
        elsewhere = {
            key
            for other in all_latest.values()
            if other.period_id != request.period_id
            for key in other.content["items"]
        }
        for spec in request.sources:
            evidence = self.session.get(Evidence, spec.evidence_id)
            if evidence is None or evidence.org_id != request.org_id:
                raise ValueError("MATERIAL_EVIDENCE_NOT_FOUND")
            # Source errors become durable blockers, not an omitted import row.
            try:
                source = inspect_material(evidence, spec)
            except (ValueError, OSError, BadZipFile, ParseError, KeyError) as exc:
                source = {
                    "evidence_id": str(evidence.id),
                    "sha256": evidence.sha256,
                    "name": evidence.original_name,
                    "spec": spec.model_dump(mode="json"),
                    "items": [],
                    "coverage": [],
                    "issues": [
                        {
                            "code": "MATERIAL_SOURCE_UNREADABLE",
                            "location": "全文",
                            "message": "资料尚不能完整读取，请核对原文件。",
                            "detail": type(exc).__name__,
                        }
                    ],
                }
            source["review_version"] = 1 + max(
                (
                    other.content["sources"].get(str(evidence.id), {}).get("review_version", 0)
                    for other in all_latest.values()
                ),
                default=0,
            )
            content["sources"][str(evidence.id)] = source
            for item in source["items"]:
                old = content["items"].get(item["key"])
                if old is None and item["key"] in elsewhere:
                    continue
                if (
                    old
                    and old.get("split_children")
                    and old.get("amount_fen") == item.get("amount_fen")
                ):
                    item = item | {
                        "split_children": old["split_children"],
                        "split_basis": old["split_basis"],
                    }
                if old != item:
                    content["resolutions"].pop(item["key"], None)
                content["items"][item["key"]] = item
            # Additive inventory: changing a mapping never deletes an earlier item.
        content["reviewed_notes_hash"] = None
        return self._save(request, content, digest)

    def update(self, request):
        replay, content, digest = self._start(request)
        if replay:
            return {"status": "recorded", "revision": replay.revision, "idempotent_replay": True}
        org = self.session.get(Organization, request.org_id)
        notes = read_company_notes(org)
        if notes["sha256"] != request.reviewed_notes_hash:
            raise ValueError("COMPANY_NOTES_CHANGED")
        if len({r.item_key for r in request.resolutions}) != len(request.resolutions):
            raise ValueError("MATERIAL_DUPLICATE_RESOLUTION")
        with self.session.begin_nested():
            for split in request.splits:
                item = content["items"].get(split.item_key)
                if not item or item.get("split_children") or not split.basis.strip():
                    raise ValueError("MATERIAL_SPLIT_SOURCE_INVALID")
                if sum(part.amount_fen for part in split.parts) != item.get("amount_fen"):
                    raise ValueError("MATERIAL_SPLIT_AMOUNT_CONFLICT")
                children = []
                for index, part in enumerate(split.parts, 1):
                    key = f"{split.item_key}/拆分{index}"
                    if key in content["items"]:
                        raise ValueError("MATERIAL_SPLIT_KEY_CONFLICT")
                    content["items"][key] = item | {
                        "key": key,
                        "amount_fen": part.amount_fen,
                        "excerpt": part.label or item["excerpt"],
                    }
                    children.append(key)
                item["split_children"] = children
                item["split_basis"] = split.basis
                content["resolutions"].pop(split.item_key, None)
            for resolution in request.resolutions:
                item = content["items"].get(resolution.item_key)
                if item is None:
                    raise ValueError("MATERIAL_ITEM_NOT_FOUND")
                if item.get("split_children"):
                    raise ValueError("MATERIAL_SPLIT_PARENT_NOT_RESOLVABLE")
                if resolution.treatment == "other_period":
                    self._transfer(request, content, item, resolution)
                content["resolutions"][resolution.item_key] = resolution.model_dump(mode="json")
            for key, links in request.subsequent_bank_resolutions.items():
                bank = self.session.get(BankTransaction, uuid.UUID(key))
                if bank is None or bank.org_id != request.org_id:
                    raise ValueError("MATERIAL_BANK_SOURCE_NOT_FOUND")
                content["bank_resolutions"][key] = [link.model_dump(mode="json") for link in links]
            content["reviewed_notes_hash"] = request.reviewed_notes_hash
            content["reviewed_notes_content"] = notes["content"]
            return self._save(request, content, digest)

    def _transfer(self, request, content, item, resolution):
        if resolution.target_period_id is None or resolution.target_period_id == request.period_id:
            raise ValueError("MATERIAL_TARGET_PERIOD_REQUIRED")
        target = self.period(request.org_id, resolution.target_period_id, writing=True)
        if (
            resolution.recognition_period != target.start_date.strftime("%Y-%m")
            or not resolution.basis.strip()
        ):
            raise ValueError("MATERIAL_TRANSFER_PERIOD_FACT_REQUIRED")
        latest = self.latest(request.org_id, target.id)
        target_content = (
            copy.deepcopy(latest.content)
            if latest
            else {
                "sources": {},
                "items": {},
                "resolutions": {},
                "bank_resolutions": {},
                "reviewed_notes_hash": None,
            }
        )
        if (
            target_content["items"].get(item["key"]) == item
            and target_content["resolutions"].get(item["key"], {}).get("recognition_period")
            == resolution.recognition_period
        ):
            return
        if item["source_id"] in content["sources"]:
            target_content["sources"][item["source_id"]] = content["sources"][item["source_id"]]
        target_content["items"][item["key"]] = item
        target_content["resolutions"][item["key"]] = MaterialResolution(
            item_key=item["key"],
            recognition_period=resolution.recognition_period,
            business_kind=resolution.business_kind,
            amount_fen=resolution.amount_fen,
            basis=resolution.basis,
        ).model_dump(mode="json")
        target_content["reviewed_notes_hash"] = None
        transfer_key = "transfer:" + canonical_sha256([request.idempotency_key, item["key"]])
        self.session.add(
            PeriodMaterialInventory(
                org_id=request.org_id,
                period_id=target.id,
                revision=latest.revision + 1 if latest else 1,
                idempotency_key=transfer_key,
                request_hash=canonical_sha256(resolution.model_dump(mode="json")),
                content=target_content,
            )
        )
        self.session.flush()

    def _component(self, org_id, reference):
        query = (
            select(BusinessEventComponent)
            .join(BusinessEvent, BusinessEvent.id == BusinessEventComponent.event_id)
            .where(BusinessEventComponent.org_id == org_id)
        )
        if reference.component_id:
            query = query.where(BusinessEventComponent.id == reference.component_id)
        else:
            query = query.where(
                BusinessEvent.idempotency_key == reference.event_key,
                BusinessEventComponent.key == reference.component_key,
            )
        return self.session.scalar(query)

    def _component_period(self, component):
        facts = component.facts
        nested = facts.get("facts", facts)
        for field in (
            "recognition_period",
            "payroll_period",
            "depreciation_period",
            "amortization_period",
            "contribution_period",
        ):
            if nested.get(field):
                return nested[field]
        if component.kind in {"payroll_accrual", "labor_remuneration_accrual"}:
            model = PayrollBatch if component.kind == "payroll_accrual" else LaborRemunerationBatch
            key = (
                facts.get("batch_id")
                or component.derived.get("payroll_batch_id")
                or component.derived.get("batch_id")
            )
            batch = self.session.get(model, uuid.UUID(str(key))) if key else None
            if batch:
                return str(
                    batch.payroll_period
                    if component.kind == "payroll_accrual"
                    else batch.remuneration_period
                )
        for field in ("fulfillment_date", "business_date", "period_end", "posting_date"):
            if nested.get(field):
                return str(nested[field])[:7]
        return None

    def _capacity(self, component, key):
        if key:
            item = self.session.scalar(
                select(OpenItem).where(
                    OpenItem.org_id == component.org_id,
                    OpenItem.source_component_id == component.id,
                    OpenItem.component_key == key,
                )
            )
            return item.original_amount_fen if item else None
        facts = component.facts.get("facts", component.facts)
        for field in ("amount_fen", "cost_fen", "gross_amount_fen"):
            if type(facts.get(field)) is int:
                return abs(facts[field])
        if type(component.derived.get("amount_fen")) is int:
            return abs(component.derived["amount_fen"])
        entries = component.derived.get("_posting_entries", [])
        return (
            max(
                sum(x.get("debit_fen", 0) for x in entries),
                sum(x.get("credit_fen", 0) for x in entries),
            )
            or None
        )

    def _link(self, org_id, link):
        component = self._component(org_id, link.source)
        if component is None:
            return None, "MATERIAL_COMPONENT_NOT_FOUND"
        event = self.session.get(BusinessEvent, component.event_id)
        if event.status != "posted" or event.reversed_by_event_id is not None:
            return component, "MATERIAL_COMPONENT_INACTIVE"
        if canonical_sha256(component.facts) != link.expected_facts_hash:
            return component, "MATERIAL_COMPONENT_CHANGED"
        voucher = self.session.scalar(
            select(Voucher).where(Voucher.event_id == event.id, Voucher.status == "posted")
        )
        if voucher is None:
            return component, "MATERIAL_VOUCHER_NOT_POSTED"
        return component, None

    def check(self, org_id, period_id):
        period = self.period(org_id, period_id)
        org = self.session.get(Organization, org_id)
        notes = read_company_notes(org)
        row = self.latest(org_id, period_id)
        closed = period.status == "closed"
        content = (
            row.content
            if row
            else {"sources": {}, "items": {}, "resolutions": {}, "bank_resolutions": {}}
        )
        issues = []

        def issue(code, message, item=None, **extra):
            issues.append(
                {
                    "code": code,
                    "message": message,
                    "item_key": (item or {}).get("key"),
                    "location": (item or {}).get("location"),
                    "excerpt": (item or {}).get("excerpt"),
                    "amount_fen": (item or {}).get("amount_fen"),
                    "source_name": content["sources"]
                    .get((item or {}).get("source_id"), {})
                    .get("name"),
                    "next_tool": "finance_update_period_material_inventory",
                    **extra,
                }
            )

        if row is None:
            issue("MATERIAL_INVENTORY_REQUIRED", "请先登记本期资料核对清单；空清单也需明确登记。")
        if not closed and content.get("reviewed_notes_hash") != notes["sha256"]:
            issue("MATERIAL_NOTES_REVIEW_REQUIRED", "公司业务说明尚未核对或已有变化，请重新查阅。")
        all_latest = self._latest_all(org_id)
        assigned = {key for other in all_latest.values() for key in other.content["sources"]}
        evidence = list(
            self.session.scalars(
                select(Evidence).where(Evidence.org_id == org_id).order_by(Evidence.id)
            )
        )
        # Enumerate evidence independently so an empty inventory cannot hide a source.
        unassigned = [
            {"id": str(e.id), "name": e.original_name, "sha256": e.sha256}
            for e in evidence
            if not closed and str(e.id) not in assigned
        ]
        for e in unassigned:
            issue("MATERIAL_EVIDENCE_UNREVIEWED", "已登记资料尚未纳入任何核对清单。", evidence=e)
        all_sources = {}
        for other in all_latest.values():
            if closed and other.period_id != period_id:
                continue
            for key, source in other.content["sources"].items():
                if key not in all_sources or source.get("review_version", 0) > all_sources[key].get(
                    "review_version", 0
                ):
                    all_sources[key] = source
        confirmed_amounts = {}
        for other in all_latest.values():
            if closed and other.period_id != period_id:
                continue
            for key, resolution in other.content["resolutions"].items():
                amount = resolution.get("amount_fen")
                if (
                    type(amount) is int
                    and resolution.get("basis", "").strip()
                    and resolution.get("treatment") != "other_period"
                ):
                    if key in confirmed_amounts and confirmed_amounts[key] != amount:
                        issue(
                            "MATERIAL_SOURCE_AMOUNT_CONFLICT",
                            "同一来源金额存在相互冲突的补充确认。",
                        )
                    confirmed_amounts[key] = amount
        source_totals = []
        for source in all_sources.values():
            recoverable = {
                "MATERIAL_AMOUNT_MISSING",
                "MATERIAL_AMOUNT_UNREADABLE",
                "MATERIAL_AMOUNT_PRECISION",
                "MATERIAL_FORMULA_RESULT_MISSING",
            }
            missing_keys = {item["key"] for item in source["items"] if item["amount_fen"] is None}
            issues.extend(
                error
                | {
                    "evidence_id": source["evidence_id"],
                    "source_name": source["name"],
                    "next_tool": "finance_register_period_materials",
                }
                for error in source["issues"]
                if error["code"] != "MATERIAL_TOTAL_MISMATCH"
                and not (
                    error["code"] in recoverable
                    and f"{source['evidence_id']}:{error['location']}" in missing_keys
                    and f"{source['evidence_id']}:{error['location']}" in confirmed_amounts
                )
            )
            for control in source.get("control_totals", []):
                group = control["location"].rstrip("0123456789")
                amounts = [
                    item["amount_fen"]
                    if item["amount_fen"] is not None
                    else confirmed_amounts.get(item["key"])
                    for item in source["items"]
                    if item["location"].rstrip("0123456789") == group
                ]
                actual = sum(amounts) if all(type(value) is int for value in amounts) else None
                source_totals.append(
                    {"evidence_id": source["evidence_id"], **control, "actual_fen": actual}
                )
                if actual != control["expected_fen"]:
                    issue(
                        "MATERIAL_TOTAL_MISMATCH",
                        "原资料合计与已核定明细不一致。",
                        location=control["location"],
                        source_name=source["name"],
                        expected_fen=control["expected_fen"],
                        actual_fen=actual,
                    )
            original = next((e for e in evidence if str(e.id) == source["evidence_id"]), None)
            if original is None or original.sha256 != source["sha256"]:
                issue("MATERIAL_SOURCE_CHANGED", "原资料已变化或不属于本公司。")
            else:
                try:
                    verify_material_bytes(original)
                except (ValueError, OSError):
                    issue(
                        "MATERIAL_SOURCE_CHANGED",
                        "原资料文件不可读取或哈希已变化。",
                        evidence_id=source["evidence_id"],
                    )
        allocations = defaultdict(int)
        capacities = {}
        facts_snapshot = {}
        results = []
        month = period.start_date.strftime("%Y-%m")
        related_scope = []
        for other in all_latest.values():
            if closed or other.period_id == period_id:
                continue
            other_period = self.period(org_id, other.period_id)
            for key, item in other.content["items"].items():
                resolution = other.content["resolutions"].get(key, {})
                related_scope.append(
                    {"item_key": key, "period_id": str(other.period_id), "resolution": resolution}
                )
                if (
                    key in content["items"]
                    or other_period.status == "closed"
                    or item.get("split_children")
                ):
                    continue
                treatment = resolution.get("treatment", "pending")
                attributed = resolution.get("recognition_period")
                basis = bool(resolution.get("basis", "").strip())
                if treatment == "supporting":
                    scoped = basis and item.get("amount_fen") in (None, 0)
                elif treatment == "no_accounting":
                    scoped = basis and bool(resolution.get("non_accounting_reason"))
                elif treatment == "duplicate":
                    target = other.content["resolutions"].get(resolution.get("duplicate_of"), {})
                    scoped = (
                        basis
                        and target.get("treatment") == "recognize"
                        and bool(target.get("recognition_period"))
                    )
                else:
                    scoped = bool(
                        attributed and attributed > month and (basis or treatment == "recognize")
                    )
                    # A future label cannot override an already known earlier component.
                    for raw in resolution.get("links", []):
                        component, error = self._link(
                            org_id, MaterialComponentLink.model_validate(raw)
                        )
                        component_month = self._component_period(component) if component else None
                        if error or component_month is None or component_month <= month:
                            scoped = False
                    if other_period.start_date < period.start_date and treatment == "recognize":
                        scoped = attributed == other_period.start_date.strftime("%Y-%m")
                if not scoped:
                    issue(
                        "MATERIAL_OTHER_INVENTORY_UNRESOLVED",
                        "其他清单中仍有未核定归属的已接收资料，不能据此排除本月漏项。",
                        item,
                        period_id=str(other.period_id),
                    )
        for key, item in content["items"].items():
            before = len(issues)
            if item.get("split_children"):
                children = [content["items"].get(k, {}) for k in item["split_children"]]
                if sum(c.get("amount_fen", 0) for c in children) != item["amount_fen"]:
                    issue("MATERIAL_SPLIT_AMOUNT_CONFLICT", "拆分金额与原资料不一致。", item)
                results.append(item | {"resolution": {}, "satisfied": len(issues) == before})
                continue
            resolution = MaterialResolution.model_validate(
                content["resolutions"].get(key, {"item_key": key})
            )
            data = resolution.model_dump(mode="json")
            if resolution.treatment == "pending":
                issue("MATERIAL_ITEM_PENDING", "该项业务尚未核对处理。", item)
            elif resolution.treatment == "other_period":
                target = all_latest.get(resolution.target_period_id)
                if target is None or key not in target.content["items"]:
                    issue("MATERIAL_TRANSFER_MISSING", "其他期间未保存该项业务。", item)
                elif not closed:
                    target_resolution = target.content["resolutions"].get(key, {})
                    if target_resolution.get("recognition_period") != resolution.recognition_period:
                        issue(
                            "MATERIAL_TRANSFER_SCOPE_CHANGED",
                            "转入清单的业务归属已有变化，请重新核对本月是否应确认。",
                            item,
                        )
                    for raw in target_resolution.get("links", []):
                        component, error = self._link(
                            org_id, MaterialComponentLink.model_validate(raw)
                        )
                        component_month = self._component_period(component) if component else None
                        if error or component_month != resolution.recognition_period:
                            issue(
                                "MATERIAL_TRANSFER_SCOPE_CHANGED",
                                "转入清单的正式确认与移出本月的依据不一致。",
                                item,
                            )
            elif resolution.treatment == "duplicate":
                target_key = resolution.duplicate_of
                target = content["resolutions"].get(target_key)
                if (
                    not target
                    or target_key == key
                    or target.get("treatment") != "recognize"
                    or not resolution.basis.strip()
                ):
                    issue(
                        "MATERIAL_DUPLICATE_SOURCE_REQUIRED",
                        "重复资料须关联具体的原业务并说明依据。",
                        item,
                    )
                elif item.get("amount_fen") is not None and content["items"][target_key].get(
                    "amount_fen"
                ) not in (None, item["amount_fen"]):
                    issue(
                        "MATERIAL_DUPLICATE_AMOUNT_CONFLICT", "重复资料的金额与原业务不一致。", item
                    )
            elif resolution.treatment == "supporting":
                if not resolution.basis.strip() or item.get("amount_fen") not in (None, 0):
                    issue(
                        "MATERIAL_NO_RECOGNITION_BASIS_REQUIRED",
                        "有金额的业务不能用背景资料说明跳过。",
                        item,
                    )
            elif resolution.treatment == "no_accounting":
                if not resolution.basis.strip() or not resolution.non_accounting_reason:
                    issue(
                        "MATERIAL_NO_RECOGNITION_BASIS_REQUIRED",
                        "不产生核算事项须确认具体性质及依据。",
                        item,
                    )
            else:
                expected = (
                    item.get("amount_fen")
                    if item.get("amount_fen") is not None
                    else resolution.amount_fen
                )
                if (
                    expected is None
                    or expected < 0
                    or not resolution.business_kind
                    or not resolution.recognition_period
                ):
                    issue(
                        "MATERIAL_ACCOUNTING_FACTS_REQUIRED",
                        "请核对该项业务性质、金额和核算所属期；不需要补造管理日期。",
                        item,
                        fields=(["business_kind"] if not resolution.business_kind else [])
                        + (["amount_fen"] if expected is None or expected < 0 else [])
                        + (["recognition_period"] if not resolution.recognition_period else []),
                        allowed_precision=["month"],
                    )
                if item.get("amount_fen") is not None and resolution.amount_fen not in (
                    None,
                    item["amount_fen"],
                ):
                    issue(
                        "MATERIAL_SOURCE_AMOUNT_CONFLICT",
                        "提交金额与原资料金额不同。",
                        item,
                        fields=["amount_fen"],
                        actual_values={"amount_fen": resolution.amount_fen},
                        expected={"amount_fen": item["amount_fen"]},
                    )
                if resolution.recognition_period != month:
                    issue(
                        "MATERIAL_RECOGNITION_PERIOD_CONFLICT",
                        "其他期间业务须转入对应清单。",
                        item,
                        fields=["recognition_period"],
                        actual_values={"recognition_period": resolution.recognition_period},
                        expected={"recognition_period": month},
                    )
                recognized = 0
                for link in resolution.links:
                    component, error = self._link(org_id, link)
                    if error:
                        issue(error, "关联的正式业务无效或事实已变化，请重新核对。", item)
                        continue
                    event = self.session.get(BusinessEvent, component.event_id)
                    facts_snapshot[str(component.id)] = {
                        "facts_hash": canonical_sha256(component.facts),
                        "posting_date": str(event.posting_date),
                        "status": event.status,
                    }
                    if component.kind != resolution.business_kind:
                        issue(
                            "MATERIAL_BUSINESS_KIND_CONFLICT",
                            "凭证组件的业务性质与资料不一致。",
                            item,
                            fields=["business_kind"],
                            actual_values={"business_kind": component.kind},
                            expected={"business_kind": resolution.business_kind},
                        )
                    if (
                        self._component_period(component) != month
                        or not period.start_date <= event.posting_date <= period.end_date
                    ):
                        issue(
                            "MATERIAL_POSTING_PERIOD_CONFLICT",
                            "本月业务尚未在本月正式确认。",
                            item,
                            fields=["recognition_period", "posting_date"],
                            actual_values={
                                "recognition_period": self._component_period(component),
                                "posting_date": str(event.posting_date),
                            },
                            expected={"recognition_period": month, "posting_month": month},
                        )
                    for field, expected_value in resolution.required_facts.items():
                        actual = component.facts
                        for part in field.split("."):
                            actual = actual.get(part) if isinstance(actual, dict) else None
                        if actual != expected_value:
                            issue(
                                "MATERIAL_COMPONENT_FACT_CONFLICT",
                                "关联组件的核算事实与已确认资料不一致。",
                                item,
                                field=field,
                                fields=[field],
                                actual_values={field: actual},
                                expected={field: expected_value},
                            )
                    if resolution.employee_id:
                        employee = self.session.get(Employee, resolution.employee_id)
                        if employee is None or employee.org_id != org_id:
                            issue("MATERIAL_EMPLOYEE_NOT_FOUND", "本公司人员来源不明确。", item)
                        elif component.kind == "expense" and component.facts.get("payer", {}).get(
                            "id"
                        ) != str(employee.counterparty_id):
                            issue(
                                "MATERIAL_EMPLOYEE_CONFLICT", "费用垫付人与清单人员不一致。", item
                            )
                    allocation_key = (str(component.id), link.open_item_key)
                    allocations[allocation_key] += link.amount_fen
                    capacities[allocation_key] = self._capacity(component, link.open_item_key)
                    recognized += link.amount_fen
                if expected is not None and recognized != expected:
                    issue(
                        "MATERIAL_RECOGNITION_AMOUNT_MISSING",
                        "原资料金额尚未足额对应正式确认。",
                        item,
                        expected_fen=expected,
                        recognized_fen=recognized,
                        difference_fen=expected - recognized,
                    )
            results.append(item | {"resolution": data, "satisfied": len(issues) == before})
        for key, allocated in allocations.items():
            if capacities[key] is None or allocated > capacities[key]:
                issue(
                    "MATERIAL_COMPONENT_OVERALLOCATED",
                    "同一正式确认金额被重复或超额关联。",
                    component_id=key[0],
                )
            if key[1] is not None and (key[0], None) in allocations:
                issue(
                    "MATERIAL_COMPONENT_OVERALLOCATED",
                    "同一组件不能同时按整项及子往来重复核对。",
                    component_id=key[0],
                )
        bank_snapshot = [] if closed else self._subsequent_bank(org_id, period, content, issues)
        from .accounting_period_service import AccountingPeriodService

        domain_service = AccountingPeriodService(self.session)
        domain_checks = domain_service._module_checks(org_id, period)
        for name, check in domain_checks.items():
            if check["blocking"]:
                issue(
                    check["code"],
                    "现有业务模块仍有本月应确认项目。",
                    module=name,
                    count=check["count"],
                    next_tool={
                        "fixed_assets": "finance_preview_fixed_asset_depreciation_batch",
                        "intangible_assets": "finance_preview_intangible_asset_amortization",
                        "borrowings": "finance_preview_borrowing_interest",
                        "payroll": "finance_preview_payroll",
                        "labor_remuneration": "finance_preview_labor_remuneration_batch",
                    }.get(name, "finance_get_owner_workflow"),
                )
        snapshot = {
            "version": "period_material_completeness_v1",
            "revision": row.revision if row else 0,
            "inventory": content,
            "source_coverage": all_sources,
            "source_totals": sorted(source_totals, key=lambda x: (x["evidence_id"], x["location"])),
            "component_facts": facts_snapshot,
            "unassigned_evidence": unassigned,
            "notes_hash": content.get("reviewed_notes_hash") if closed else notes["sha256"],
            "notes_content": content.get("reviewed_notes_content") if closed else notes["content"],
            "subsequent_bank": bank_snapshot,
            "related_scope": sorted(related_scope, key=lambda x: (x["period_id"], x["item_key"])),
            "domain_checks": domain_service._immutable_module_checks(domain_checks),
        }
        return {
            "satisfied": not issues,
            "closed": closed,
            "revision": row.revision if row else 0,
            "snapshot_hash": canonical_sha256(snapshot),
            "snapshot": snapshot,
            "issues": issues,
            "fact_issues": [
                AccountingFactIssue(
                    code=item["code"],
                    kind="missing_accounting_fact"
                    if item["code"] == "MATERIAL_ACCOUNTING_FACTS_REQUIRED"
                    else "conflicting_accounting_facts",
                    fields=[f"items.{item['item_key']}.{field}" for field in item["fields"]],
                    actual_values=item.get("actual_values", {}),
                    expected={
                        "recognition_period_precision": "month",
                        "amount_unit": "fen",
                        **item.get("expected", {}),
                    },
                    context={"item_key": item["item_key"], "location": item.get("location") or ""},
                    message=item["message"],
                ).model_dump(mode="json")
                for item in issues
                if item.get("fields")
            ],
            "items": results,
            "company_notes": {k: notes[k] for k in ("path", "sha256", "exists")},
            "unassigned_evidence": unassigned,
            "domain_checks": domain_checks,
            "next_tool": "finance_update_period_material_inventory",
        }

    def _subsequent_bank(self, org_id, period, content, issues):
        from .bank_statement_service import BankStatementService

        result = []
        bank_service = BankStatementService(self.session)
        explicit_allocations = defaultdict(int)
        transactions = self.session.scalars(
            select(BankTransaction)
            .where(BankTransaction.org_id == org_id, BankTransaction.booking_date > period.end_date)
            .order_by(BankTransaction.booking_date, BankTransaction.id)
        )
        for bank in transactions:
            matches = list(
                self.session.scalars(
                    select(BankTransactionMatch).where(
                        BankTransactionMatch.org_id == org_id,
                        BankTransactionMatch.bank_transaction_id == bank.id,
                        BankTransactionMatch.invalidated_by_event_id.is_(None),
                    )
                )
            )
            components = []
            valid_matches = []
            for match in matches:
                try:
                    if not bank_service._valid_current_match(bank, match):
                        continue
                except ValueError:
                    continue
                valid_matches.append(match)
                event = self.session.get(BusinessEvent, match.event_id)
                if event and event.status == "posted":
                    settled_component_ids = select(Settlement.payment_component_id).where(
                        Settlement.payment_event_id == event.id,
                        Settlement.reversed.is_(False),
                        Settlement.payment_component_id.is_not(None),
                    )
                    components.extend(
                        self.session.scalars(
                            select(BusinessEventComponent).where(
                                BusinessEventComponent.event_id == event.id,
                                BusinessEventComponent.kind.not_in(("funds", "settlement")),
                                or_(
                                    BusinessEventComponent.id.not_in(settled_component_ids),
                                    BusinessEventComponent.id.in_(
                                        select(OpenItem.source_component_id).where(
                                            OpenItem.org_id == org_id,
                                            OpenItem.source_event_id == event.id,
                                            OpenItem.source_component_id.is_not(None),
                                        )
                                    ),
                                ),
                            )
                        )
                    )
                    source_ids = self.session.scalars(
                        select(OpenItem.source_component_id)
                        .join(Settlement, Settlement.open_item_id == OpenItem.id)
                        .where(
                            Settlement.payment_event_id == event.id, Settlement.reversed.is_(False)
                        )
                    )
                    for component_id in source_ids:
                        source = self.session.get(BusinessEventComponent, component_id)
                        if source:
                            components.append(source)
            explicit = (
                [] if valid_matches else content.get("bank_resolutions", {}).get(str(bank.id), [])
            )
            explicit_total = 0
            explicit_valid = True
            for raw in explicit:
                link = MaterialComponentLink.model_validate(raw)
                component, error = self._link(org_id, link)
                if error is None:
                    expected_type = "receivable" if bank.amount_fen > 0 else "payable"
                    query = select(OpenItem).where(
                        OpenItem.source_component_id == component.id,
                        OpenItem.org_id == org_id,
                        OpenItem.item_type == expected_type,
                    )
                    if link.open_item_key:
                        query = query.where(OpenItem.component_key == link.open_item_key)
                    originals = list(self.session.scalars(query))
                    if len(originals) == 1:
                        explicit_allocations[originals[0].id] += link.amount_fen
                    if (
                        len(originals) == 1
                        and explicit_allocations[originals[0].id]
                        <= originals[0].original_amount_fen - originals[0].settled_amount_fen
                    ):
                        components.append(component)
                        explicit_total += link.amount_fen
                    else:
                        explicit_valid = False
                else:
                    explicit_valid = False
            known = bool(valid_matches and components) or (
                bool(explicit) and explicit_valid and explicit_total == abs(bank.amount_fen)
            )
            component_states = []
            for component in sorted(
                {c.id: c for c in components}.values(), key=lambda c: str(c.id)
            ):
                component_period = self._component_period(component)
                event = self.session.get(BusinessEvent, component.event_id)
                component_states.append(
                    {
                        "id": str(component.id),
                        "hash": canonical_sha256(component.facts),
                        "period": component_period,
                        "status": event.status,
                    }
                )
                if (
                    component_period is None
                    or event.status != "posted"
                    or event.reversed_by_event_id
                ):
                    known = False
                elif (
                    component_period <= period.start_date.strftime("%Y-%m")
                    and event.posting_date > period.end_date
                ):
                    issues.append(
                        {
                            "code": "MATERIAL_LATE_RECOGNITION",
                            "message": "后续收付款含本期或以前期间尚未正确入账的业务。",
                            "bank_transaction_id": str(bank.id),
                            "component_id": str(component.id),
                        }
                    )
            if not known:
                issues.append(
                    {
                        "code": "MATERIAL_SUBSEQUENT_BANK_UNREVIEWED",
                        "message": "后续月份收付款的业务归属尚未核对。",
                        "bank_transaction_id": str(bank.id),
                        "booking_date": str(bank.booking_date),
                        "amount_fen": bank.amount_fen,
                        "memo": bank.memo,
                    }
                )
            result.append(
                {
                    "id": str(bank.id),
                    "date": str(bank.booking_date),
                    "amount_fen": bank.amount_fen,
                    "components": component_states,
                    "reviewed": known,
                }
            )
        return result
