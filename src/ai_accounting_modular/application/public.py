"""Single-call accounting coordination with explicit, currently delivered owners."""

from __future__ import annotations

from collections import defaultdict

from ai_accounting_modular.contracts.public import (
    CONTRACT,
    ApplyRequest,
    ApplyResult,
    BusinessGet,
    ContractError,
    canonical_json,
    fact_source_ids,
    fact_source_paths,
    schema_document,
    validate_contract,
)
from ai_accounting_modular.identity.public import IdentityContext
from ai_accounting_modular.records.public import RecordsChange, RecordsContext
from ai_accounting_modular.storage.public import RepositoryFactory, module_schema

from . import _documents, _handoff
from ._changes import classify
from ._context import DomainRegistry, SubmissionContext
from ._planning import collect_changes, evaluate
from ._repository import ApplicationRepository

__all__ = ["Application", "create_application", "repository_factory"]


def repository_factory():
    return RepositoryFactory(module_schema("application"), ApplicationRepository)


class Application:
    def __init__(self, storage, identity, ledger, records, domains, money=None):
        self.storage, self.identity, self.ledger, self.records = storage, identity, ledger, records
        self.registry = DomainRegistry(domains)
        self.money = money

    def accounting_apply(self, request: ApplyRequest) -> ApplyResult | dict:
        try:
            validate_contract("core.schema.json", "ApplyRequest", request)
        except ContractError as error:
            return error.to_response()
        try:
            outcome = self._apply(request)
        except ContractError as error:
            known = schema_document("core.schema.json")["$defs"]["ApplyResult"]["properties"][
                "error"
            ]["enum"]
            if error.code not in known:
                return error.to_response()
            result = self._base_response(request)
            result.update(
                error=error.code, message=error.message, fact_issues=list(error.fact_issues)
            )
            if error.diagnostic_id:
                result["diagnostic_id"] = error.diagnostic_id
            return validate_contract("core.schema.json", "ApplyResult", result)
        with self.storage.read_unit(request["company_id"]) as read:
            status = self.records.document_status(read)
        if status["state"] != "ready":
            status = _documents.finish(self, request["company_id"])
        outcome["documents"] = status["state"]
        outcome["completion"] = (
            "complete" if status["state"] == "ready" else "saved_documents_pending"
        )
        if status["set_id"]:
            outcome["document_set_id"] = status["set_id"]
        return validate_contract("core.schema.json", "ApplyResult", outcome)

    @staticmethod
    def _base_response(request):
        return {
            "contract": CONTRACT,
            "company_id": request["company_id"],
            "command_id": request["command_id"],
            "completion": "not_saved",
            "accounting": "unchanged",
            "documents": "unchanged",
            "reading": "unchanged",
            "businesses": [],
            "affected_periods": [],
            "fact_issues": [],
            "unresolved": [],
            "changes": "none",
            "participants": {"objects": [], "notes": [], "issue_scopes": []},
        }

    def _apply(self, request):
        if any(
            key in request
            for key in ("notes", "unresolved", "unresolved_scope", "expected_scope_head")
        ):
            raise ContractError("unsupported_business", "当前节点尚未接通说明及未解事项办理。")
        if len(canonical_json(request).encode("utf-8")) > 8 * 1024 * 1024:
            raise ContractError("batch_too_large", "本单规范内容超过8MiB，请按独立业务单元提交。")
        mutations, seen = request["mutations"], set()
        for mutation in mutations:
            business_id = mutation.get("business_id") or mutation["fact"]["business_id"]
            if business_id in seen:
                raise ContractError("invalid_input", "同单每个业务身份只能出现一次。")
            seen.add(business_id)
        if not (mutations or request.get("objects") or request.get("sources")):
            raise ContractError("invalid_input", "业务单不能没有任何内容。")
        company_id = request["company_id"]
        with self.storage.write_unit(company_id) as unit:
            repo = unit.repository("application")
            heads = repo.heads(seen)
            changes = [
                classify(
                    mutation,
                    heads.get(mutation.get("business_id") or mutation["fact"]["business_id"]),
                )
                for mutation in mutations
            ]
            previous_facts = {
                fact["business_id"]: fact
                for fact in self.registry.export_heads(list(heads.values()), unit)
            }
            handoffs = _handoff.prepare(changes, previous_facts, self.registry)
            for change in changes:
                if change.fact:
                    self.registry.owner(change.fact["kind"])
            identity_plan = self.identity.plan_change(
                request.get("objects", ()), IdentityContext(unit, request["command_id"])
            )
            records_plan = self.records.plan_change(
                RecordsChange(sources=tuple(request.get("sources", ()))),
                RecordsContext(unit, request["command_id"]),
            )
            self.records.apply_current(records_plan, unit)
            previous_period_to = max(
                self.storage.company_metadata(unit).start_period,
                repo.working_period_to() or "0001-01",
                repo.latest_period() or "0001-01",
                self.ledger.latest_period(unit) or "0001-01",
            )
            context = SubmissionContext(
                unit,
                self.registry,
                self.identity,
                self.ledger,
                self.records,
                objects=identity_plan.objects,
                facts={change.business_id: change.fact for change in changes},
                money=self.money,
                evaluation_period_to=max(
                    [
                        previous_period_to,
                        *(change.fact["period"] for change in changes if change.fact),
                    ]
                ),
                previous_period_to=previous_period_to,
            )
            for index, change in enumerate(changes):
                if change.fact:
                    paths = fact_source_paths(mutations[index]["fact"])
                    context.require_sources(
                        tuple(paths),
                        business_id=change.business_id,
                        field=f"/mutations/{index}/fact",
                        source_paths=paths,
                    )
            for index, obj in enumerate(request.get("objects", ())):
                context.require_sources(
                    obj.get("evidence_ids", ()), field=f"/objects/{index}/evidence_ids"
                )
            descriptions = collect_changes(
                changes, identity_plan.changed_ids, context, self.registry, handoffs
            )
            responsibilities = _handoff.active_responsibilities(descriptions, handoffs)
            plans = evaluate(descriptions, context, self.ledger, request.get("correction_period"))
            natural_keys = {
                business_id: item["natural_key"] for business_id, item in responsibilities.items()
            }
            prior_heads = repo.heads(responsibilities)
            proposed = [
                (
                    key,
                    item["fact"]["kind"] if "fact" in item else prior_heads[key]["kind"],
                    natural_keys[key] if "fact" in item else None,
                )
                for key, item in responsibilities.items()
            ]
            repo.validate_natural_keys(proposed)
            changed_keys = {
                key: value
                for key, _, value in proposed
                if key not in prior_heads or prior_heads[key]["natural_key"] != value
            }
            repo.clear_natural_keys(list(changed_keys))
            for change in changes:
                if change.changed:
                    repo.save(
                        change.head(
                            request["command_id"], natural_key=natural_keys[change.business_id]
                        )
                    )
            direct_changes = {change.business_id for change in changes if change.changed}
            for business_id, key in changed_keys.items():
                if business_id not in direct_changes:
                    repo.update_natural_key(business_id, key)
            repo.advance_working_period(
                context.evaluation_period_to, self.storage.company_metadata(unit).start_period
            )
            self.identity.apply_current(identity_plan, unit)
            for object_id, ids in identity_plan.source_uses.items():
                self.records.replace_source_uses("object", object_id, ids, unit)
            for owner, plan in plans:
                owner.apply_current(plan, unit)
            posting_plans = [posting for _, plan in plans for posting in plan["plans"]]
            if any(posting["fund_movements"] for posting in posting_plans):
                if self.money is None:
                    raise ContractError("invalid_operation", "实际资金身份模块尚未装配。")
                self.money.register_occurrences(posting_plans, unit)
            adoption = self.ledger.adopt(
                [plan for _, plan in plans],
                unit,
                correction_period=request.get("correction_period"),
                explanation=request.get("explanation"),
            )
            touched = sorted(responsibilities)
            dependencies = [edge for _, plan in plans for edge in plan["dependencies"]]
            projection_changed = False
            if touched:
                dependency_changed = repo.replace_dependencies(touched, dependencies)
                publication_changed = repo.replace_publications(
                    touched, [item for _, plan in plans for item in plan["publications"]]
                )
                projection_changed = dependency_changed or publication_changed
            for change in changes:
                if change.changed:
                    self.records.replace_source_uses(
                        "business",
                        change.business_id,
                        fact_source_ids(change.fact) if change.fact else (),
                        unit,
                    )
            keys = set()
            if context.evaluation_period_to > previous_period_to:
                keys.add("company")
            for change in changes:
                if change.changed:
                    for head in (
                        change.before,
                        change.head(
                            request["command_id"], natural_key=natural_keys[change.business_id]
                        ),
                    ):
                        if head and head["period"]:
                            keys.add(f"facts:{head['family']}:{head['period']}")
            if identity_plan.changed_ids:
                keys.add("objects")
            if records_plan.changed_source_ids:
                keys.add("sources")
            if keys:
                self.records.mark_document_targets(sorted(keys), unit)
            final_heads = repo.heads(seen | set(touched))

            def check_projection():
                self.registry.export_heads(list(final_heads.values()), unit)
                _handoff.verify_released(handoffs, unit)

            unit.check_before_commit(check_projection)
            result = self._base_response(request)
            result["businesses"] = self._states([final_heads[key] for key in sorted(seen)], unit)
            result["affected_periods"] = sorted(
                {
                    head["period"]
                    for change in changes
                    if change.changed
                    for head in (
                        change.before,
                        change.head(
                            request["command_id"], natural_key=natural_keys[change.business_id]
                        ),
                    )
                    if head and head["period"]
                }
                | {ref["posting_period"] for ref in adoption.results}
            )
            result["participants"]["objects"] = [
                {key: obj[key] for key in ("object_id", "seq", "content_digest")}
                for obj in identity_plan.objects
            ]
            changed = bool(keys or changed_keys or adoption.changed or projection_changed)
            result.update(
                changes="committed" if changed else "none",
                accounting="saved" if adoption.changed else "unchanged",
                reading="unchanged",
            )
        return result

    def _states(self, heads, read, *, views=None):
        if views is None:
            views = self.ledger.get_results([head["business_id"] for head in heads], read)
        grouped = defaultdict(list)
        for view in views:
            grouped[view["reference"]["business_id"]].append(view["reference"])
        return [
            {
                "business_id": head["business_id"],
                "seq": head["seq"],
                "state": head["state"],
                "fact_digest": head["current_digest"],
                "results": grouped[head["business_id"]],
            }
            for head in heads
        ]

    def accounting_get(self, query: BusinessGet):
        validate_contract("core.schema.json", "BusinessGet", query)
        if "result_id" in query:
            raise ContractError("unsupported_business", "精确历史结果读取尚未接入应用入口。")
        with self.storage.read_unit(query["company_id"]) as read:
            heads = read.repository("application").heads(query["business_ids"])
            views = self.ledger.get_results(list(heads), read)
            facts = {
                fact["business_id"]: fact
                for fact in self.registry.export_heads(list(heads.values()), read)
            }
            sources = {}
            if "sources" in query["include"]:
                source_ids = sorted(
                    {source_id for fact in facts.values() for source_id in fact_source_ids(fact)}
                )
                for offset in range(0, len(source_ids), 500):
                    sources.update(
                        {
                            source["source_id"]: source
                            for source in self.records.resolve_sources(
                                source_ids[offset : offset + 500], read
                            )
                        }
                    )
                if set(sources) != set(source_ids):
                    raise ContractError(
                        "storage_corrupt", "当前事实引用的来源缺失，不能返回不完整资料。"
                    )
            grouped = defaultdict(list)
            for view in views:
                grouped[view["reference"]["business_id"]].append(view)
            businesses = []
            for state in self._states(
                [heads[key] for key in query["business_ids"] if key in heads], read, views=views
            ):
                fact = facts.get(state["business_id"])
                item = {
                    "state": state,
                    "results": grouped[state["business_id"]]
                    if "results" in query["include"]
                    else [],
                }
                if fact and "facts" in query["include"]:
                    item["fact"] = fact
                if "sources" in query["include"]:
                    item["sources"] = (
                        [sources[source_id] for source_id in fact_source_ids(fact)] if fact else []
                    )
                businesses.append(item)
            return validate_contract(
                "core.schema.json",
                "BusinessGetResult",
                {
                    "company_id": read.company_id,
                    "database_id": read.database_id,
                    "head": read.head,
                    "businesses": businesses,
                    "missing_business_ids": [
                        key for key in query["business_ids"] if key not in heads
                    ],
                },
            )

    def accounting_documents(self, request):
        validate_contract("core.schema.json", "DocumentsRequest", request)
        if request["action"] == "finish":
            return _documents.finish(self, request["company_id"])
        with self.storage.read_unit(request["company_id"]) as read:
            return self.records.document_status(read)


def create_application(*, storage, identity, ledger, records, domains, money=None):
    return Application(storage, identity, ledger, records, domains, money)
