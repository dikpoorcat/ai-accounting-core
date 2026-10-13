"""Synthetic documentary DAG checks; no real company or source bytes are read."""

from copy import deepcopy
from hashlib import sha256

import pytest

from ai_accounting.kernel.periods import MATERIAL_CATEGORIES
from ai_accounting.kernel.replay_document_materials import compile_materials_and_closes

PROOF = "a" * 64
ORIGINAL = "b" * 64


class Builder:
    def __init__(self):
        self.steps = []
        self.issues = []
        self.logical_ids = {}
        self.bindings = {}
        self.source_digests = {"proof": PROOF, "original": ORIGINAL}
        self.source_steps = {}
        self.save_steps = {"later": "later-save"}
        self.publication_steps = {"later": "later-publish"}
        self.owner_confirmation = PROOF

    def node_id(self, key):
        return "s_" + sha256(key.encode()).hexdigest()

    def issue(self, path, code, message, **details):
        self.issues.append((path, code, message))

    def dependency(self, key, published=False):
        result = (self.publication_steps if published else self.save_steps).get(key)
        if result is None:
            self.issue(key, "dependency_missing", "missing")
        return result

    def add_step(self, key, command, payload, depends_on=(), outputs=None):
        step_id = self.node_id(key)
        self.steps.append(
            {
                "step_id": step_id,
                "command": command,
                "payload": payload,
                "depends_on": tuple(depends_on),
            }
        )
        for binding, path in (outputs or {}).items():
            self.bind(key, binding, key, path)
        return step_id

    def bind(self, key, binding, step_key, path):
        self.bindings[key, binding] = {"$replay_ref": self.node_id(step_key), "field": path}


def material(key, kind, data, depends_on=()):
    return {
        "key": key,
        "kind": kind,
        "data": data,
        "source_keys": ["proof", "original"],
        "depends_on": list(depends_on),
    }


def source(key="source"):
    return material(
        key,
        "material_source_v2",
        {
            "period": "2026-01",
            "evidence_digest": ORIGINAL,
            "category": "bank",
            "purpose": "business",
            "specification": {"format": "text", "passages": [], "all_pages_reviewed": True},
        },
    )


def document(materials=None):
    rows = []
    for period in ("2026-01", "2026-02"):
        for category in MATERIAL_CATEGORIES:
            business = category == "bank"
            rows.append(
                {
                    "key": f"inventory:{period}:{category}",
                    "period": period,
                    "category": category,
                    "no_business": not business,
                    "basis": {
                        "confirmation_source_key": "proof",
                        "confirmation_sha256": PROOF,
                        "explicit_no_business_passage": "Explicit zero activity"
                        if not business
                        else None,
                    },
                    "source_coverage": [{"source_key": "original", "sha256": ORIGINAL}]
                    if business
                    else [],
                }
            )
    return {
        "scope": {"first_period": "2026-01", "last_period": "2026-02"},
        "facts": [{"key": "later", "kind": "expense"}],
        "materials": materials if materials is not None else [source()],
        "inventory_replay_plan": {"rows": rows},
    }


def test_material_receive_binds_both_actual_source_and_automatic_allocation():
    builder = Builder()
    compile_materials_and_closes(builder, document())
    assert not builder.issues
    assert builder.bindings["source", "fact_id"]["field"] == "fact_id"
    assert builder.bindings["source", "allocation_fact_id"]["field"] == "allocation_fact_id"
    assert builder.bindings["source", "allocation_revision"]["field"] == "allocation_revision"
    other = Builder()
    compile_materials_and_closes(other, document())
    assert other.logical_ids["source", "subject_id"] != builder.logical_ids["source", "subject_id"]


def allocation_steps():
    assignment = {
        "location": "L1",
        "recognition_period": "2026-02",
        "basis": "confirmed_period",
        "basis_evidence_digest": PROOF,
        "basis_location": "L1",
        "basis_excerpt": "February",
    }
    preview = {
        "key": "declared-preview",
        "command": "preview_material_allocation",
        "data": {"source_id": "$ref:source:subject_id", "assignments": [assignment]},
        "depends_on": ["source"],
        "outputs": [],
    }
    confirmation = {
        "key": "declared-confirm",
        "command": "confirm_material_allocation",
        "data": {
            "source_id": "$ref:source:subject_id",
            "expected_revision": 0,
            "preview_digest": "$ref:declared-preview:preview_digest",
            "epochs": "$ref:declared-preview:epochs",
        },
        "depends_on": ["source", "declared-preview"],
        "outputs": [{"key": "declared-allocation", "kind": "material_period_allocation"}],
    }
    return preview, confirmation


def test_declared_step_pair_compiles_fresh_preparation_and_real_revision_bindings():
    doc = document()
    doc["steps"] = list(allocation_steps())
    original = deepcopy(doc)
    builder = Builder()
    compile_materials_and_closes(builder, doc)
    assert doc == original and not builder.issues
    previews = [row for row in builder.steps if row["command"] == "preview_material_allocation"]
    confirms = [row for row in builder.steps if row["command"] == "confirm_material_allocation"]
    assert len(previews) == len(confirms) == 1
    assert previews[0]["payload"]["assignments"] == doc["steps"][0]["data"]["assignments"]
    assert builder.node_id("source") in previews[0]["depends_on"]
    assert confirms[0]["payload"]["expected_revision"] == (
        "$ref:declared-allocation:preview:expected_revision"
    )
    assert confirms[0]["payload"]["preview_digest"] == "$ref:declared-allocation:preview:digest"
    assert builder.bindings["declared-preview", "preview_digest"]["field"] == "digest"
    assert builder.bindings["declared-confirm", "fact_id"]["field"] == "fact_id"
    assert (
        builder.node_id("declared-allocation")
        in next(
            row
            for row in builder.steps
            if row["step_id"] == builder.node_id("close-preview:2026-01")
        )["depends_on"]
    )


def test_identical_material_and_step_allocations_share_one_prepared_confirmation():
    preview, confirmation = allocation_steps()
    allocation = material(
        "existing-allocation",
        "material_period_allocation",
        {
            "period": "2026-01",
            "source_id": "$ref:source:subject_id",
            "source_fact_id": "$ref:source:fact_id",
            "entries": preview["data"]["assignments"],
        },
        ["source"],
    )
    doc = document([source(), allocation])
    doc["steps"] = [preview, confirmation]
    builder = Builder()
    compile_materials_and_closes(builder, doc)
    assert not builder.issues
    assert sum(row["command"] == "confirm_material_allocation" for row in builder.steps) == 1
    assert builder.save_steps["declared-allocation"] == builder.node_id("existing-allocation")
    assert (
        builder.bindings["declared-allocation", "fact_id"]
        == (builder.bindings["existing-allocation", "fact_id"])
    )


@pytest.mark.parametrize(
    "change,code",
    [
        ("orphan", "material_allocation_confirmation_missing"),
        ("mismatch", "material_allocation_pair_mismatch"),
        ("unknown_source", "material_allocation_source_missing"),
        ("conflict", "material_allocation_source_conflict"),
        ("missing_evidence", "material_evidence_missing"),
        ("old_runtime", "material_allocation_runtime_input_forbidden"),
    ],
)
def test_declared_allocations_fail_explicitly_without_complete_consistent_sources(change, code):
    preview, confirmation = allocation_steps()
    doc = document()
    doc["steps"] = [preview, confirmation]
    if change == "orphan":
        doc["steps"].pop()
    elif change == "mismatch":
        confirmation["data"]["assignments"] = []
    elif change == "unknown_source":
        preview["data"]["source_id"] = confirmation["data"]["source_id"] = "$ref:unknown:subject_id"
    elif change == "missing_evidence":
        preview["data"]["assignments"][0]["basis_evidence_digest"] = "c" * 64
    elif change == "old_runtime":
        confirmation["data"]["epochs"] = {"accounting": 999, "material": 999, "management": 999}
    else:
        entries = deepcopy(preview["data"]["assignments"])
        entries[0]["recognition_period"] = "2026-01"
        doc["materials"].append(
            material(
                "conflicting",
                "material_period_allocation",
                {
                    "period": "2026-01",
                    "source_id": "$ref:source:subject_id",
                    "source_fact_id": "$ref:source:fact_id",
                    "entries": entries,
                },
                ["source"],
            )
        )
    builder = Builder()
    compile_materials_and_closes(builder, doc)
    assert code in {issue[1] for issue in builder.issues}


def test_explicit_allocation_binds_live_revision_and_precedes_resolution():
    allocation = material(
        "allocation",
        "material_period_allocation",
        {
            "period": "2026-01",
            "source_id": "$ref:source:subject_id",
            "source_fact_id": "$ref:source:fact_id",
            "entries": [
                {
                    "location": "L1",
                    "recognition_period": "2026-02",
                    "basis": "confirmed_period",
                    "basis_evidence_digest": PROOF,
                    "basis_location": "L1",
                    "basis_excerpt": "February",
                }
            ],
        },
        ("source",),
    )
    resolution = material(
        "resolution",
        "material_resolution_v2",
        {
            "period": "2026-01",
            "source_id": "$ref:source:subject_id",
            "source_fact_id": "$ref:source:fact_id",
            "location": "L1",
            "treatment": "recognize",
            "amount_fen": 100,
            "recognition_period": "2026-02",
            "links": [
                {
                    "calculation_id": "$ref:later:calculation_id",
                    "amount_field": "fact.amount_fen",
                    "amount_fen": 100,
                }
            ],
        },
        ("source", "later"),
    )
    builder = Builder()
    compile_materials_and_closes(builder, document([resolution, allocation, source()]))
    assert not builder.issues
    steps = {x["step_id"]: x for x in builder.steps}
    confirmation = steps[builder.node_id("allocation")]
    assert (
        confirmation["payload"]["expected_revision"] == "$ref:allocation:preview:expected_revision"
    )
    resolved = steps[builder.node_id("resolution")]
    assert builder.node_id("allocation") in resolved["depends_on"]
    assert "later-publish" in resolved["depends_on"]
    first_close_preview = steps[builder.node_id("close-preview:2026-01")]
    assert builder.node_id("resolution") in first_close_preview["depends_on"]
    second_preview = steps[builder.node_id("close-preview:2026-02")]
    assert builder.node_id("close:2026-01") in second_preview["depends_on"]


def test_primary_group_then_copy_members_then_complete_copy_group():
    links = [
        {
            "calculation_id": "$ref:later:calculation_id",
            "amount_field": "fact.amount_fen",
            "amount_fen": 100,
        }
    ]
    primary = material(
        "primary",
        "material_group_resolution",
        {
            "period": "2026-01",
            "source_id": "$ref:source:subject_id",
            "source_fact_id": "$ref:source:fact_id",
            "members": [{"location": "L1", "amount_fen": 100}],
            "group_amount_fen": 100,
            "links": links,
            "basis_evidence_digest": PROOF,
            "basis_location": "L1",
            "reason": "Explicit primary pool",
        },
        ("source", "later"),
    )
    copied = material(
        "copy-member",
        "material_resolution_v2",
        {
            "period": "2026-01",
            "source_id": "$ref:copy-source:subject_id",
            "source_fact_id": "$ref:copy-source:fact_id",
            "location": "L1",
            "treatment": "duplicate",
            "links": [],
            "duplicate_source_id": "$ref:source:subject_id",
            "duplicate_location": "L1",
        },
        ("copy-source", "source"),
    )
    group = deepcopy(primary)
    group["key"] = "copy-group"
    group["data"]["source_id"] = "$ref:copy-source:subject_id"
    group["data"]["source_fact_id"] = "$ref:copy-source:fact_id"
    builder = Builder()
    compile_materials_and_closes(
        builder, document([group, copied, primary, source(), source("copy-source")])
    )
    assert not builder.issues
    steps = {x["step_id"]: x for x in builder.steps}
    assert builder.node_id("primary") in steps[builder.node_id("copy-member")]["depends_on"]
    assert builder.node_id("copy-member") in steps[builder.node_id("copy-group")]["depends_on"]
    assert builder.node_id("copy-member") not in steps[builder.node_id("primary")]["depends_on"]


def test_inventory_full_six_categories_and_response_ids():
    builder = Builder()
    compile_materials_and_closes(builder, document())
    inventories = [x for x in builder.steps if x["command"] == "inventory_batch"]
    assert len(inventories) == 2
    assert all(len(x["payload"]["inventories"]) == 6 for x in inventories)
    bank = next(x for x in inventories[0]["payload"]["inventories"] if x["category"] == "bank")
    assert bank["evidence"] == [ORIGINAL]
    assert bank["expected"] == 1
    assert builder.bindings["inventory:2026-01:bank", "inventory_id"]["field"].endswith(
        ".inventory_id"
    )


def test_missing_category_and_unreceived_original_are_explicit_compile_issues():
    value = document()
    value["inventory_replay_plan"]["rows"].pop()
    value["inventory_replay_plan"]["rows"][0]["source_coverage"] = [
        {"source_key": "proof", "sha256": PROOF}
    ]
    builder = Builder()
    compile_materials_and_closes(builder, value)
    codes = {x[1] for x in builder.issues}
    assert {"inventory_category_missing", "inventory_coverage_not_received"} <= codes


def test_old_material_calculation_id_must_not_be_accepted_as_input():
    value = document()
    value["materials"].append(
        material(
            "bad",
            "material_resolution_v2",
            {
                "period": "2026-01",
                "source_id": "$ref:source:subject_id",
                "source_fact_id": "old-fact-id",
                "location": "L1",
                "treatment": "recognize",
                "links": [
                    {
                        "calculation_id": "old-calculation-id",
                        "amount_field": "fact.amount_fen",
                        "amount_fen": 1,
                    }
                ],
            },
            ("source",),
        )
    )
    builder = Builder()
    compile_materials_and_closes(builder, value)
    assert "material_runtime_identity_forbidden" in {x[1] for x in builder.issues}


def test_duplicate_selects_whole_exact_allocation_within_larger_primary_pool():
    links = [
        {
            "calculation_id": "$ref:later:calculation_id",
            "amount_field": "fact.amount_fen",
            "amount_fen": 100,
        },
        {
            "calculation_id": "$ref:later:calculation_id",
            "amount_field": "result.other_fen",
            "amount_fen": 50,
        },
    ]
    primary = material(
        "primary",
        "material_group_resolution",
        {
            "period": "2026-01",
            "source_id": "$ref:source:subject_id",
            "source_fact_id": "$ref:source:fact_id",
            "members": [{"location": "L1", "amount_fen": 150}],
            "group_amount_fen": 150,
            "links": links,
            "basis_evidence_digest": PROOF,
            "basis_location": "L1",
            "reason": "Explicit pool",
        },
        ("source", "later"),
    )
    copy = material(
        "copy",
        "material_resolution_v2",
        {
            "period": "2026-01",
            "source_id": "$ref:copy-source:subject_id",
            "source_fact_id": "$ref:copy-source:fact_id",
            "location": "L1",
            "treatment": "duplicate",
            "links": links[:1],
            "amount_fen": 100,
            "duplicate_source_id": "$ref:source:subject_id",
            "duplicate_location": "L1",
        },
        ("copy-source", "source"),
    )
    builder = Builder()
    compile_materials_and_closes(
        builder, document([copy, primary, source(), source("copy-source")])
    )
    assert not builder.issues
    step = next(x for x in builder.steps if x["step_id"] == builder.node_id("copy"))
    assert builder.node_id("primary") in step["depends_on"]


def test_typed_material_validation_does_not_skip_unknown_accounting_fields():
    value = document()
    value["materials"][0]["data"]["inferred_period"] = "2026-02"
    builder = Builder()
    compile_materials_and_closes(builder, value)
    assert "material_template_invalid" in {x[1] for x in builder.issues}


def test_real_builder_emits_valid_replay_contract_and_sequential_closes():
    from ai_accounting.kernel.replay_document import ReplayDocumentBuilder, _compile_sources

    value = document()
    value["sources"] = [
        {"key": "proof", "sha256": PROOF, "role": "owner_confirmation"},
        {"key": "original", "sha256": ORIGINAL},
    ]
    builder = ReplayDocumentBuilder(
        value,
        company_id="fresh-company",
        database_id="fresh-database",
        owner_confirmation=PROOF,
        request_id="fresh-request",
    )
    _compile_sources(builder, value)
    compile_materials_and_closes(builder, value)
    result = builder.finish()
    steps = result["request"]["steps"]
    assert len(steps) == 7
    assert all(not str(x["payload"]).count("$ref:") for x in steps)
    assert (
        next(x for x in steps if x["command"] == "close")["payload"]["preview_digest"]["field"]
        == "digest"
    )
