"""Pure compilation of preserved final facts into a fresh durable replay graph.

This module never opens a database or a source file. Source bytes must already
have been verified and preserved by the caller. Runtime identities and receipts
embedded in historical documents are deliberately outside its input contract.
"""

from __future__ import annotations

import copy
import hashlib
import uuid

from pydantic import ValidationError

from .contracts import KernelError
from .replay_contracts import REPLAY_COMMANDS, ReplayPrepare, ReplaySource
from .types import canonical, digest


class ReplayDocumentCompileError(ValueError):
    def __init__(self, issues):
        self.issues = list(issues)
        super().__init__(
            "final replay document cannot be compiled: "
            + ", ".join(item["code"] for item in self.issues)
        )


class ReplayDocumentBuilder:
    """Shared graph builder for typed business and material compilers.

    Logical step/reference names are hashes; business IDs are fresh UUIDs or
    actual response bindings. ``bind`` only names a real response field.
    Payload references are resolved once all subgraphs have been declared.
    """

    def __init__(self, document, *, company_id, database_id, owner_confirmation, request_id):
        self.document = document
        self.company_id, self.database_id = company_id, database_id
        self.owner_confirmation, self.request_id = owner_confirmation, request_id
        self.steps, self.issues, self.source_manifest = [], [], []
        self.preservation_requirements = []
        self.source_digests, self.source_steps = {}, {}
        self.logical_ids = {
            ("target_company", "company_id"): company_id,
            ("target_company", "database_id"): database_id,
        }
        self.bindings = {}
        self.save_steps, self.publication_steps, self.entity_steps = {}, {}, {}
        self._steps_by_key, self._keys_by_id, self._pending_dependencies = {}, {}, []
        self.fact_documents = {item["key"]: item for item in document.get("facts", [])}
        self.opening_publications = []

    @staticmethod
    def node_id(key):
        return "s_" + hashlib.sha256(key.encode("utf-8")).hexdigest()

    @staticmethod
    def reference_id(key):
        return "r_" + hashlib.sha256(key.encode("utf-8")).hexdigest()

    def issue(self, path, code, message, **details):
        issue = {"path": path, "code": code, "message": message, **details}
        if issue not in self.issues:
            self.issues.append(issue)

    def allocate_subject(self, key):
        pair = (key, "subject_id")
        if pair not in self.logical_ids:
            self.logical_ids[pair] = "subject_" + uuid.uuid4().hex
        return self.logical_ids[pair]

    def add_step(self, key, command, payload, *, depends_on=(), outputs=None):
        if key in self._steps_by_key:
            self.issue(key, "duplicate_step_key", "step logical key is repeated")
            return self.node_id(key)
        if command not in REPLAY_COMMANDS:
            self.issue(key, "unsupported_replay_command", command)
        step_id = self.node_id(key)
        step = {
            "step_id": step_id,
            "command": command,
            "payload": copy.deepcopy(payload),
            "depends_on": list(dict.fromkeys(depends_on)),
        }
        self.steps.append(step)
        self._steps_by_key[key] = step
        self._keys_by_id[step_id] = key
        for binding, path in (outputs or {}).items():
            self.bind(key, binding, key, path)
        return step_id

    def bind(self, key, binding, step_key, path):
        step = self._steps_by_key.get(step_key)
        if step is None:
            step = next((s for s in self.steps if s["step_id"] == step_key), None)
        if step is None:
            self.issue(key, "binding_producer_missing", step_key)
            return
        producer_key = self._keys_by_id[step["step_id"]]
        output_ref = self.reference_id(producer_key)
        step["output_ref"] = output_ref
        fields = step.setdefault("output_fields", [])
        if path not in fields:
            fields.append(path)
        pair = (key, binding)
        reference = {"$replay_ref": output_ref, "field": path}
        previous = self.bindings.get(pair)
        if previous is not None and previous != reference:
            self.issue(key, "duplicate_business_binding", binding)
        self.bindings[pair] = reference

    def bind_selected(
        self, key, binding, step_key, array_path, subject_id, field, *, match_field="subject_id"
    ):
        alias = (
            "b_"
            + hashlib.sha256(
                canonical([key, binding, array_path, field]).encode("utf-8")
            ).hexdigest()
        )
        self.bind(key, binding, step_key, alias)
        step = self._steps_by_key.get(step_key)
        if step is None:
            step = next(s for s in self.steps if s["step_id"] == step_key)
        selector = {
            "array_path": array_path,
            "match_field": match_field,
            "match_value": subject_id,
            "field": field,
        }
        step.setdefault("output_selectors", {})[alias] = selector
        return alias

    def resolve(self, value, path="payload"):
        if isinstance(value, str) and value.startswith("$ref:"):
            try:
                key, binding = value[5:].rsplit(":", 1)
            except ValueError:
                self.issue(path, "invalid_logical_reference", value)
                return value
            pair = key, binding
            if pair in self.logical_ids:
                return self.logical_ids[pair]
            if pair in self.bindings:
                return copy.deepcopy(self.bindings[pair])
            self.issue(path, "unbound_logical_reference", value)
            return value
        if isinstance(value, dict):
            return {key: self.resolve(child, path + "/" + key) for key, child in value.items()}
        if isinstance(value, (list, tuple)):
            return [
                self.resolve(child, path + "/" + str(index)) for index, child in enumerate(value)
            ]
        return value

    def dependency(self, key, published=False):
        if key in self._keys_by_id:
            return key
        if published and key in self.publication_steps:
            return self.publication_steps[key]
        if key in self._steps_by_key:
            return self.node_id(key)
        if key in self.entity_steps:
            return self.entity_steps[key]
        if key in self.save_steps:
            return self.save_steps[key]
        if key in self.source_digests:
            return self.source_steps.get(key)
        self.issue(key, "dependency_producer_missing", "logical dependency has no typed producer")
        return None

    def defer_dependencies(self, step_id, keys, *, published=False):
        self._pending_dependencies.append((step_id, list(keys), published))

    def evidence(self, keys, path):
        result = []
        for key in keys:
            if key not in self.source_digests:
                self.issue(path, "source_not_in_manifest", key)
            elif self.source_digests[key] not in result:
                result.append(self.source_digests[key])
        if not result:
            self.issue(path, "fact_evidence_missing", "typed facts need explicit preserved sources")
        return result

    def finish(self):
        for step_id, keys, published in self._pending_dependencies:
            step = next(item for item in self.steps if item["step_id"] == step_id)
            for key in keys:
                dep = self.dependency(key, published)
                if dep and dep != step_id and dep not in step["depends_on"]:
                    step["depends_on"].append(dep)
        for step in self.steps:
            step["payload"] = self.resolve(step["payload"], step["step_id"])
            if "output_selectors" in step:
                step["output_selectors"] = self.resolve(
                    step["output_selectors"], step["step_id"] + "/outputs"
                )
        if self.issues:
            raise ReplayDocumentCompileError(self.issues)
        manifest = [
            ReplaySource.model_validate_json(canonical(item)).model_dump(mode="json")
            for item in self.source_manifest
        ]
        request = {
            "company_id": self.company_id,
            "database_id": self.database_id,
            "first_period": self.document["scope"]["first_period"],
            "last_period": self.document["scope"]["last_period"],
            "owner_confirmation": self.owner_confirmation,
            "source_manifest_digest": digest(manifest).hex(),
            "source_manifest": manifest,
            "preserved_evidence": sorted(set(self.source_digests.values())),
            "steps": self.steps,
            "request_id": self.request_id,
        }
        from .replay_bank_dependencies import add_bank_reconciliation_dependencies

        bank_ordering = add_bank_reconciliation_dependencies(request)
        request = bank_ordering["request"]
        from .replay_registration_dependencies import add_replay_registration_preparations

        registration_preparations = add_replay_registration_preparations(request)
        request = registration_preparations["request"]
        try:
            validated = ReplayPrepare.model_validate_json(canonical(request))
            from .replay_runner import compile_replay_steps, validate_replay_sequence

            order, dependencies = compile_replay_steps(validated.steps)
            validate_replay_sequence(
                order, dependencies, validated.first_period, validated.last_period
            )
        except (ValidationError, ValueError, KernelError) as exc:
            raise ReplayDocumentCompileError(
                [
                    {
                        "path": "request",
                        "code": getattr(exc, "code", "compiled_plan_invalid"),
                        "message": str(exc),
                    }
                ]
            ) from exc
        for step in request["steps"]:
            step["depends_on"] = list(dependencies[step["step_id"]])
        mapping = {
            "identities": [
                {"key": key, "binding": binding, "value": value}
                for (key, binding), value in self.logical_ids.items()
            ],
            "bindings": [
                {"key": key, "binding": binding, **value}
                for (key, binding), value in self.bindings.items()
            ],
            "steps": {key: item["step_id"] for key, item in self._steps_by_key.items()},
        }
        return {
            "request": request,
            "mapping": mapping,
            "issues": [],
            "preservation_requirements": self.preservation_requirements,
            "bank_reconciliation_edges": bank_ordering["added_edges"],
            "registration_preparations": registration_preparations["added_preparations"],
        }


def _compile_sources(builder, document):
    for index, source in enumerate(document.get("sources", [])):
        key, sha = source.get("key"), source.get("sha256")
        if not isinstance(key, str) or not isinstance(sha, str) or len(sha) != 64:
            builder.issue(
                f"sources/{index}", "invalid_preserved_source", "source key/hash required"
            )
            continue
        if key in builder.source_digests:
            builder.issue(key, "duplicate_source_key", "source key repeated")
            continue
        builder.source_digests[key] = sha
        builder.logical_ids[key, "evidence_digest"] = sha
        builder.logical_ids[key, "digest"] = sha
        builder.source_manifest.append(
            {
                "source_id": builder.reference_id("source:" + key),
                "kind": "confirmation"
                if source.get("role") == "owner_confirmation"
                else "document",
                "evidence_digest": sha,
                "disposition": "adopt",
            }
        )
    # A fresh range confirmation is an input of this compilation, never a
    # historical confirmation selected from the document by filename.
    if not any(
        item["kind"] == "confirmation" and item["evidence_digest"] == builder.owner_confirmation
        for item in builder.source_manifest
    ):
        builder.source_manifest.append(
            {
                "source_id": builder.reference_id("owner-scope-confirmation"),
                "kind": "confirmation",
                "evidence_digest": builder.owner_confirmation,
                "disposition": "adopt",
            }
        )
        builder.source_digests["owner-scope-confirmation"] = builder.owner_confirmation


def _compile_entities(builder, document):
    for entity in document.get("entities", []):
        key = entity["key"]
        evidence = builder.evidence(entity.get("sources", []), key)
        payload = {
            "kind": entity["kind"],
            "data": entity["data"],
            "source": canonical(entity.get("source_locations", []))
            if entity.get("source_locations")
            else key,
            "evidence_digest": evidence[0] if evidence else None,
        }
        if entity.get("account_type") is not None:
            payload["account_type"] = entity["account_type"]
        builder.entity_steps[key] = builder.add_step("entity:" + key, "register_entity", payload)
        for field in ("entity_id", "profile_id", "revision"):
            builder.bind(key, field, "entity:" + key, field)


def _compile_workforce(builder, document):
    for relation in document.get("workforce_relationships", []):
        entity_key, previous_key = relation["entity_key"], relation["entity_key"]
        evidence = builder.evidence(relation.get("sources", []), entity_key)
        confirmation = relation.get("owner_confirmation_in_document")
        confirmation_sha = None
        if confirmation:
            content = canonical(
                {"entity_key": entity_key, "owner_confirmation_in_document": confirmation}
            )
            sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
            confirmation_key = "workforce-confirmation:" + entity_key
            builder.source_digests[confirmation_key] = sha
            builder.source_manifest.append(
                {
                    "source_id": builder.reference_id(confirmation_key),
                    "kind": "confirmation",
                    "evidence_digest": sha,
                    "disposition": "adopt",
                }
            )
            builder.preservation_requirements.append(
                {
                    "key": confirmation_key,
                    "sha256": sha,
                    "content": content,
                    "media_type": "application/json",
                }
            )
            confirmation_sha = sha
        additional_sha = relation.get("additional_preserved_source_sha256")
        if additional_sha and additional_sha not in builder.source_digests.values():
            additional_key = "workforce-additional:" + additional_sha
            builder.source_digests[additional_key] = additional_sha
            builder.source_manifest.append(
                {
                    "source_id": builder.reference_id(additional_key),
                    "kind": "confirmation",
                    "evidence_digest": additional_sha,
                    "disposition": "adopt",
                }
            )
            builder.preservation_requirements.append(
                {
                    "key": additional_key,
                    "sha256": additional_sha,
                    "archive_locator": relation.get("additional_source_location"),
                    "entity_key": entity_key,
                }
            )
        for index, event in enumerate(relation.get("events", [])):
            key = event["key"]
            action = event["data"]["action"]
            chosen_evidence = evidence[0] if evidence else None
            locations = relation.get("source_locations", [])
            # An adopted labour management start is explicitly scoped to that
            # action/date. It must not become the evidence for a later hire.
            confirms_labour_start = bool(
                confirmation
                and action == "start_labor"
                and confirmation.get("adopted_management_start_date")
                == event["data"]["effective_date"]
            )
            confirms_hire = bool(
                confirmation
                and action == "hire"
                and not confirmation.get("adopted_management_start_date")
            )
            if confirms_labour_start or confirms_hire:
                chosen_evidence = confirmation_sha
                locations = ["owner_confirmation_in_document"]
            elif additional_sha and action == "hire":
                chosen_evidence = additional_sha
                locations = [relation.get("additional_source_location")]
            payload = {
                "entity_id": f"$ref:{entity_key}:entity_id",
                "data": event["data"],
                "source": canonical(
                    {
                        "action": action,
                        "effective_date": event["data"]["effective_date"],
                        "source_locations": locations,
                    }
                ),
                "expected_revision": f"$ref:{previous_key}:revision",
                "evidence_digest": chosen_evidence,
            }
            command = "register_worker" if index == 0 else "change_workforce_relationship"
            if index == 0:
                entity = next(
                    (item for item in document["entities"] if item["key"] == entity_key), {}
                )
                payload["data"] = dict(entity.get("data", {}), **event["data"])
            step_id = builder.add_step("workforce:" + key, command, payload)
            builder.defer_dependencies(step_id, [previous_key])
            builder.save_steps[key] = step_id
            for field in ("entity_id", "profile_id", "revision"):
                builder.bind(key, field, "workforce:" + key, field)
            previous_key = key


def _compile_company_note(builder, document):
    note = document.get("company_note")
    if not note:
        return
    builder.evidence(note.get("sources", []), "company_note")
    selected_source = note.get("execution_requirements_source")
    if selected_source is None:
        candidates = [
            item["source_key"]
            for item in note.get("source_locations", [])
            if isinstance(item, dict) and "owner_acceptance.verbatim" in item.get("location", "")
        ]
        if len(set(candidates)) == 1:
            selected_source = candidates[0]
        elif len(note.get("sources", [])) == 1:
            selected_source = note["sources"][0]
    if selected_source not in note.get("sources", []):
        builder.issue(
            "company_note",
            "company_note_acceptance_source_missing",
            "company note needs one explicit current owner acceptance source",
        )
    selected_digest = builder.source_digests.get(selected_source)
    builder.add_step(
        "company_note:" + note["key"],
        "update_company_note",
        {"text": note["text"], "expected_revision": 0, "evidence_digest": selected_digest},
    )


def _logical_references(value):
    if isinstance(value, str) and value.startswith("$ref:"):
        key, _, binding = value[5:].rpartition(":")
        yield key, binding
    elif isinstance(value, dict):
        for child in value.values():
            yield from _logical_references(child)
    elif isinstance(value, list):
        for child in value:
            yield from _logical_references(child)


def _validation_placeholders(value):
    """Validate typed shapes without pretending a source identity was committed."""
    if isinstance(value, str) and value.startswith("$ref:"):
        binding = value.rsplit(":", 1)[-1]
        if binding in {"revision", "expected_revision", "allocation_revision"}:
            return 1
        if binding.endswith("digest"):
            return "0" * 64
        return "uncommitted_" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
    if isinstance(value, dict):
        return {key: _validation_placeholders(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_validation_placeholders(child) for child in value]
    return value


def _validate_document_fact(builder, fact, model):
    try:
        model.model_validate_json(
            canonical(_validation_placeholders(fact["data"]))
        ).validate_registration()
    except Exception as exc:
        builder.issue(fact["key"], "typed_fact_template_invalid", str(exc))
    declared_command = fact.get("registration_command")
    if declared_command not in {None, "save_fact", "save_facts", model.registration_command}:
        builder.issue(fact["key"], "unsupported_fact_registration_path", declared_command)
    from .duplicates import SourceLocation

    for index, location in enumerate(fact.get("typed_source_locations") or []):
        try:
            SourceLocation.model_validate_json(canonical(_validation_placeholders(location)))
        except (ValidationError, ValueError) as exc:
            builder.issue(
                fact["key"] + f"/typed_source_locations/{index}",
                "typed_source_location_invalid",
                str(exc),
            )

    def check(value, path):
        if isinstance(value, dict):
            for field, child in value.items():
                if (
                    field.endswith("_id")
                    and isinstance(child, str)
                    and not child.startswith("$ref:")
                ):
                    # This report header denotes the aggregate workforce, not an
                    # entity from a prior database. All other IDs require refs.
                    if (field, child) != ("counterparty_id", "payroll-group"):
                        builder.issue(
                            path + "/" + field,
                            "runtime_identity_forbidden",
                            "business identities must bind to this run",
                        )
                if (
                    field.endswith("evidence_digest")
                    and child is not None
                    and child not in builder.source_digests.values()
                ):
                    builder.issue(
                        path + "/" + field,
                        "source_digest_not_in_manifest",
                        "explicit evidence must belong to this source manifest",
                    )
                check(child, path + "/" + field)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                check(child, path + "/" + str(index))

    check(fact["data"], fact["key"])
    check(fact.get("typed_source_locations") or [], fact["key"] + "/typed_source_locations")


def _compile_publication(builder, key, *, prerequisites=()):
    preview_key, confirm_key = "publication-preview:" + key, "publication-confirm:" + key
    payload = {"subjects": [f"$ref:{key}:subject_id"]}
    preview = builder.add_step(
        preview_key,
        "preview",
        payload,
        depends_on=tuple(dict.fromkeys(prerequisites)),
        outputs={"preview_digest": "digest", "epochs": "epochs"},
    )
    builder.defer_dependencies(preview, [key])
    confirm = builder.add_step(
        confirm_key,
        "confirm",
        {
            **payload,
            "preview_digest": f"$ref:{preview_key}:preview_digest",
            "epochs": f"$ref:{preview_key}:epochs",
        },
        depends_on=(preview,),
    )
    builder.bind_selected(
        key, "calculation_id", confirm_key, "results", f"$ref:{key}:subject_id", "calculation_id"
    )
    builder.publication_steps[key] = confirm
    return confirm


def _compile_remuneration_tax_dependencies(builder, facts):
    """Tax confirmation consumes a saved revision before remuneration adoption."""
    from .remuneration_tax_basis import REMUNERATION_KINDS, REMUNERATION_TAX_REQUIRED_KINDS

    def target(value, binding):
        if isinstance(value, str) and value.startswith("$ref:"):
            parts = value[5:].rsplit(":", 1)
            if len(parts) == 2 and parts[1] == binding:
                return parts[0]
        return None

    bases = {}
    for fact in facts:
        if fact["kind"] != "remuneration_tax_basis":
            continue
        key, data = fact["key"], fact["data"]
        source_key = target(data.get("source_id"), "subject_id")
        source = builder.fact_documents.get(source_key, {})
        if (
            not source
            or source.get("kind") not in REMUNERATION_KINDS
            or source.get("kind") != data.get("source_kind")
            or source.get("data", {}).get("period") != data.get("period")
            or target(data.get("source_fact_id"), "fact_id") != source_key
        ):
            builder.issue(
                key,
                "remuneration_tax_source_conflict",
                "tax basis must bind one remuneration subject, saved fact and accounting period",
            )
            continue
        bases.setdefault(source_key, []).append(key)
        save = builder.save_steps.get(key)
        if save:
            builder.defer_dependencies(save, [source_key])
            pool = data.get("pooled_tax_proof") or {}
            for allocation in pool.get("allocations", []):
                peer_key = target(allocation.get("source_id"), "subject_id")
                peer = builder.fact_documents.get(peer_key, {})
                if (
                    not peer
                    or peer.get("kind") not in REMUNERATION_KINDS
                    or peer.get("kind") != allocation.get("source_kind")
                    or peer.get("data", {}).get("period") != allocation.get("period")
                ):
                    builder.issue(
                        key,
                        "remuneration_tax_pool_source_conflict",
                        "pooled allocation must identify remuneration of the same kind and period",
                    )
                else:
                    builder.defer_dependencies(save, [peer_key])

    for fact in facts:
        if fact["kind"] not in REMUNERATION_KINDS:
            continue
        key = fact["key"]
        sources = bases.get(key, [])
        if len(sources) > 1 or (not sources and fact["kind"] in REMUNERATION_TAX_REQUIRED_KINDS):
            builder.issue(
                key,
                "remuneration_tax_basis_missing"
                if not sources
                else "ambiguous_remuneration_tax_basis",
                "each remuneration requires exactly one declared source-confirmed tax basis",
            )
        preview = builder._steps_by_key.get("publication-preview:" + key)
        if preview:
            builder.defer_dependencies(preview["step_id"], sources)
    return bases


def _compile_business(builder, document):
    from .remuneration_tax_basis import REMUNERATION_KINDS
    from .service import default_registry

    registry = default_registry()
    input_models = registry.input_models
    facts = document.get("facts", [])
    if len(builder.fact_documents) != len(facts):
        builder.issue("facts", "duplicate_fact_key", "business keys must be unique")
    # Allocate direct inputs only. Domain-produced identities come from their
    # dedicated preparation/confirmation responses, including asset members.
    for fact in facts:
        model = input_models.get(fact["kind"])
        if model is None:
            builder.issue(
                fact["key"],
                "fact_kind_not_registerable"
                if fact["kind"] in registry.historical_only
                else "unsupported_fact_kind",
                fact["kind"],
            )
        elif not model.registration_command:
            builder.allocate_subject(fact["key"])
        if model is not None:
            _validate_document_fact(builder, fact, model)
    for step in document.get("steps", []):
        for key in step.get("fresh_subject_key_allocations", []):
            builder.allocate_subject(key)
    payroll_periods = {}
    opening_keys = [fact["key"] for fact in facts if fact["kind"] == "opening_package"]
    if not opening_keys:
        builder.issue(
            "facts", "opening_package_missing", "reconstruction needs an explicit opening package"
        )
    for fact in facts:
        key, kind = fact["key"], fact["kind"]
        model = input_models.get(kind)
        if model is None or model.registration_command:
            continue
        if kind == "report_classification":
            # A root-owned typed classification subcompiler supplies new lineage.
            continue
        record = {
            "kind": kind,
            "subject_id": f"$ref:{key}:subject_id",
            "data": fact["data"],
            "evidence": builder.evidence(fact.get("source_keys", []), key),
            "expected_revision": 0,
            "source_locations": fact.get("typed_source_locations") or [],
        }
        if fact.get("review") is not None:
            record["review"] = fact["review"]
        save_key = "fact-save:" + key
        save = builder.add_step(save_key, "save_facts", {"facts": [record]})
        builder.save_steps[key] = save
        for field in ("fact_id", "revision"):
            builder.bind(key, field, save_key, "results.0." + field)
        dependencies = fact.get("depends_on", [])
        # Source revision references need saved inputs. Consumption of an
        # evaluable business source waits for formal adoption, not registration.
        for dependency in dependencies:
            dep_fact = builder.fact_documents.get(dependency, {})
            dep_kind = dep_fact.get("kind")
            published = dep_kind in registry.evaluators and not (
                (kind == "payroll_plan_v2" and dep_kind in {"payroll_bounded", "payroll"})
                or (kind == "remuneration_tax_basis" and dep_kind in REMUNERATION_KINDS)
            )
            builder.defer_dependencies(save, [dependency], published=published)
        if kind not in registry.evaluators:
            continue
        if kind in {"payroll_bounded", "payroll"}:
            payroll_periods.setdefault(fact["data"]["period"], []).append(key)
        publication = _compile_publication(builder, key)
        if kind == "opening_package":
            builder.opening_publications.append(publication)
        else:
            preview = builder._steps_by_key["publication-preview:" + key]
            builder.defer_dependencies(preview["step_id"], opening_keys, published=True)
    tax_basis_by_source = _compile_remuneration_tax_dependencies(builder, facts)
    # All wages, tax bases and exact monthly plans precede read-only preparation.
    # The runner validates ready/no-issues/no-candidates before allowing adoption.
    for period, keys in sorted(payroll_periods.items()):
        prepare_key = "payroll-prepare:" + period
        prepare = builder.add_step(prepare_key, "prepare_payroll", {"period": period})
        prepared_inputs = [
            fact["key"]
            for fact in facts
            if fact["kind"].startswith("payroll_")
            and (
                fact["data"].get("period") == period
                or fact["kind"]
                in {"payroll_profile", "payroll_income_tax_policy", "payroll_contribution_policy"}
            )
        ]
        builder.defer_dependencies(prepare, prepared_inputs)
        builder.defer_dependencies(
            prepare, [basis for key in keys for basis in tax_basis_by_source.get(key, [])]
        )
        for key in keys:
            builder._steps_by_key["publication-preview:" + key]["depends_on"].append(prepare)
    _compile_asset_business(builder, document, registry)
    _compile_external_basis(builder, document)
    compile_report_classifications(builder, document)
    # Explicit material allocation pairs are consumed by the material compiler,
    # including receipt bindings and dependencies before inventory and close.
    supported_steps = {
        "prepare_payroll",
        "confirm_payroll_preparation",
        "preview",
        "confirm",
        "prepare_asset_activation_batch",
        "confirm_asset_activation_batch",
        "prepare_asset_consumption_month",
        "confirm_asset_consumption_month",
        "preview_material_allocation",
        "confirm_material_allocation",
        "obligation_basis",
    }
    for step in document.get("steps", []):
        if step.get("command") and step["command"] not in supported_steps:
            builder.issue(step["key"], "unsupported_document_step", step["command"])
    for fact in facts:
        if fact["key"] not in builder.save_steps:
            builder.issue(
                fact["key"],
                "fact_producer_missing",
                "declared fact has no supported typed producer",
            )


def compile_report_classifications(builder, document):
    if not any(fact["kind"] == "report_classification" for fact in document.get("facts", [])):
        return
    from .replay_report_classification import compile_report_classifications as compile_reports

    compile_reports(builder, document)


def _compile_external_basis(builder, document):
    for fact in document.get("facts", []):
        if fact["kind"] != "external_basis_review":
            continue
        key = fact["key"]
        basis_key = "obligation-basis:" + key
        step = builder.add_step(
            basis_key,
            "obligation_basis",
            {
                "obligation_id": fact["data"]["obligation_id"],
            },
        )
        builder.defer_dependencies(step, fact.get("depends_on", []), published=True)
        builder._steps_by_key["fact-save:" + key]["depends_on"].append(step)


def _compile_asset_business(builder, document, registry):
    """Rebuild domain-owned card facts through current dedicated APIs."""
    facts = document.get("facts", [])
    activations = {fact["key"]: fact for fact in facts if fact["kind"] == "asset_activation"}
    batches = [fact for fact in facts if fact["kind"] == "asset_activation_batch"]
    monthly = {
        fact["data"]["period"]: fact for fact in facts if fact["kind"] == "asset_consumption_month"
    }
    explicit = [step for step in document.get("steps", []) if step.get("command")]
    for step in explicit:
        command = step["command"]
        for sha in step.get("data", {}).get("evidence", []):
            if sha not in builder.source_digests.values():
                builder.issue(
                    step["key"],
                    "source_digest_not_in_manifest",
                    "dedicated step evidence has no preserved source",
                )
        if command == "prepare_asset_activation_batch":
            data = step["data"]
            batch_key = next(
                key
                for key, binding in _logical_references(data["subject_id"])
                if binding == "subject_id"
            )
            batches.append(
                {
                    "key": batch_key,
                    "data": data,
                    "source_keys": [],
                    "depends_on": step.get("depends_on", []),
                    "_explicit": True,
                }
            )
        elif command == "prepare_asset_consumption_month":
            period = step["data"]["period"]
            monthly[period] = {
                "key": "asset-consumption-month:" + period,
                "data": step["data"],
                "source_keys": [],
                "depends_on": step.get("depends_on", []),
                "_explicit": True,
            }
    for batch in batches:
        key = batch["key"]
        builder.allocate_subject(key)
        members, dependencies = [], []
        if batch.get("_explicit"):
            for member in batch["data"]["members"]:
                member_key = next(
                    k for k, b in _logical_references(member["subject_id"]) if b == "subject_id"
                )
                builder.allocate_subject(member_key)
                members.append(copy.deepcopy(member))
            evidence = batch["data"]["evidence"]
            dependencies = batch["depends_on"]
        else:
            for member in batch["data"]["members"]:
                member_key = next(
                    k for k, b in _logical_references(member["subject_id"]) if b == "subject_id"
                )
                activation = activations.get(member_key)
                if not activation:
                    builder.issue(key, "activation_member_missing", member_key)
                    continue
                builder.allocate_subject(member_key)
                members.append(
                    {
                        "subject_id": f"$ref:{member_key}:subject_id",
                        "data": activation["data"],
                        "expected_revision": 0,
                    }
                )
                dependencies.extend(activation.get("depends_on", []))
            evidence = builder.evidence(batch.get("source_keys", []), key)
        prepare_key, confirm_key = (
            "asset-activation-prepare:" + key,
            "asset-activation-confirm:" + key,
        )
        payload = {
            "subject_id": f"$ref:{key}:subject_id",
            "period": batch["data"]["period"],
            "members": members,
            "evidence": evidence,
            "expected_revision": 0,
        }
        prepare = builder.add_step(
            prepare_key,
            "prepare_asset_activation_batch",
            payload,
            outputs={"preview_digest": "digest", "epochs": "epochs"},
        )
        builder.defer_dependencies(prepare, dependencies, published=True)
        builder.defer_dependencies(
            prepare, [f["key"] for f in facts if f["kind"] == "opening_package"], published=True
        )
        confirm = builder.add_step(
            confirm_key,
            "confirm_asset_activation_batch",
            {
                **payload,
                "preview_digest": f"$ref:{prepare_key}:preview_digest",
                "epochs": f"$ref:{prepare_key}:epochs",
            },
            depends_on=(prepare,),
        )
        for member_key in [
            key,
            *(
                next(k for k, b in _logical_references(m["subject_id"]) if b == "subject_id")
                for m in members
            ),
        ]:
            builder.save_steps[member_key] = confirm
            builder.publication_steps[member_key] = confirm
            for field in ("fact_id", "revision"):
                builder.bind_selected(
                    member_key,
                    field,
                    prepare_key,
                    "fact_changes",
                    f"$ref:{member_key}:subject_id",
                    field,
                )
            builder.bind_selected(
                member_key,
                "calculation_id",
                confirm_key,
                "results",
                f"$ref:{member_key}:subject_id",
                "calculation_id",
            )
    previous_month = None
    for period, month in sorted(monthly.items()):
        key = month["key"]
        prepare_key, confirm_key = (
            "asset-consumption-prepare:" + period,
            "asset-consumption-confirm:" + period,
        )
        evidence = (
            month["data"].get("evidence")
            if month.get("_explicit")
            else builder.evidence(month.get("source_keys", []), key)
        )
        payload = {"period": period, "evidence": evidence, "expected_revision": 0}
        prepare = builder.add_step(
            prepare_key,
            "prepare_asset_consumption_month",
            payload,
            depends_on=(previous_month,) if previous_month else (),
            outputs={"preview_digest": "digest", "epochs": "epochs"},
        )
        deps = [
            f["key"]
            for f in facts
            if f["kind"]
            in {
                "asset",
                "reimbursed_asset",
                "opening_asset",
                "asset_disposal",
                "asset_activation_batch",
            }
            and f["data"]["period"] <= period
        ]
        deps.extend(b["key"] for b in batches if b["data"]["period"] <= period)
        builder.defer_dependencies(prepare, deps, published=True)
        confirm = builder.add_step(
            confirm_key,
            "confirm_asset_consumption_month",
            {
                **payload,
                "preview_digest": f"$ref:{prepare_key}:preview_digest",
                "epochs": f"$ref:{prepare_key}:epochs",
            },
            depends_on=(prepare,),
        )
        previous_month = confirm
        builder.save_steps[key] = confirm
        builder.publication_steps[key] = confirm
        actual_owner = "asset-consumption-month:" + period
        for field in ("subject_id", "fact_id", "revision"):
            builder.bind_selected(key, field, prepare_key, "fact_changes", actual_owner, field)
        builder.bind_selected(
            key, "calculation_id", confirm_key, "results", actual_owner, "calculation_id"
        )
        outputs = [
            f for f in facts if f["kind"] == "asset_consumption" and f["data"]["period"] == period
        ]
        for step in explicit:
            if (
                step["command"] == "confirm_asset_consumption_month"
                and step["data"]["period"] == period
            ):
                outputs.extend(
                    item
                    for item in step.get("outputs", [])
                    if item.get("kind") == "asset_consumption"
                )
        for member in outputs:
            member_key = member["key"]
            entity_ref = member["data"]["asset_id"]
            builder.save_steps[member_key] = confirm
            builder.publication_steps[member_key] = confirm
            for field in ("subject_id", "fact_id", "revision"):
                builder.bind_selected(
                    member_key,
                    field,
                    prepare_key,
                    "fact_changes",
                    entity_ref,
                    field,
                    match_field="data.asset_id",
                )
            builder.bind_selected(
                member_key,
                "calculation_id",
                confirm_key,
                "results",
                f"$ref:{member_key}:subject_id",
                "calculation_id",
            )


def compile_replay_document(document, *, company_id, database_id, owner_confirmation, request_id):
    """Compile only declared facts and sources; return a request and fresh mapping."""
    if (
        not isinstance(document, dict)
        or document.get("format") != "ai-accounting/final-replay-facts/1"
    ):
        raise ReplayDocumentCompileError(
            [
                {
                    "path": "format",
                    "code": "unsupported_document_format",
                    "message": "expected final-replay-facts/1",
                }
            ]
        )
    builder = ReplayDocumentBuilder(
        document,
        company_id=company_id,
        database_id=database_id,
        owner_confirmation=owner_confirmation,
        request_id=request_id,
    )
    _compile_sources(builder, document)
    _compile_entities(builder, document)
    _compile_workforce(builder, document)
    _compile_company_note(builder, document)
    _compile_business(builder, document)
    from .replay_document_materials import compile_materials_and_closes

    compile_materials_and_closes(builder, document)
    return builder.finish()
