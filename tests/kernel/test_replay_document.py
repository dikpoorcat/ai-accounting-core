"""Pure fresh-document compiler tests; no company database or real files."""

from hashlib import sha256

import pytest

from ai_accounting.kernel.periods import MATERIAL_CATEGORIES
from ai_accounting.kernel.replay_document import (
    ReplayDocumentBuilder,
    ReplayDocumentCompileError,
    compile_replay_document,
)
from ai_accounting.kernel.types import canonical, digest

PROOF = "a" * 64
SCOPE = "b" * 64


def document():
    return {
        "format": "ai-accounting/final-replay-facts/1",
        "scope": {"first_period": "2026-01", "last_period": "2026-01"},
        "sources": [{"key": "owner-original", "sha256": PROOF, "role": "owner_confirmation"}],
        "entities": [],
        "facts": [
            {
                "key": "opening:zero",
                "kind": "opening_package",
                "data": {
                    "period": "2026-01",
                    "package_id": "$ref:opening:zero:subject_id",
                    "counts": {
                        "bank": 0,
                        "cash": 0,
                        "counterparties": 0,
                        "assets": 0,
                        "loans": 0,
                        "taxes": 0,
                        "payroll_payables": 0,
                        "payroll_cumulative": 0,
                        "equity": 0,
                    },
                    "members": [],
                    "completeness_confirmed": True,
                },
                "source_keys": ["owner-original"],
                "depends_on": [],
            }
        ],
        "materials": [],
        "steps": [],
        "workforce_relationships": [],
        "inventory_replay_plan": {
            "rows": [
                {
                    "key": "inventory:" + category,
                    "period": "2026-01",
                    "category": category,
                    "no_business": True,
                    "basis": {
                        "confirmation_source_key": "owner-original",
                        "confirmation_sha256": PROOF,
                        "explicit_no_business_passage": "Owner explicitly confirms no business",
                    },
                    "source_coverage": [],
                }
                for category in MATERIAL_CATEGORIES
            ]
        },
    }


def compile_document(value):
    return compile_replay_document(
        value,
        company_id="fresh-company",
        database_id="fresh-database",
        owner_confirmation=SCOPE,
        request_id="fresh-compile",
    )


def test_zero_opening_publishes_and_closes_with_fresh_response_bindings():
    result = compile_document(document())
    commands = [item["command"] for item in result["request"]["steps"]]
    assert commands == [
        "save_facts",
        "preview",
        "confirm",
        "inventory_batch",
        "preview_close",
        "close",
    ]
    assert any(
        item["kind"] == "confirmation" and item["evidence_digest"] == SCOPE
        for item in result["request"]["source_manifest"]
    )
    bindings = {(item["key"], item["binding"]): item for item in result["mapping"]["bindings"]}
    assert bindings["opening:zero", "fact_id"]["field"] == "results.0.fact_id"
    assert bindings["opening:zero", "calculation_id"]["field"].startswith("b_")
    confirmation = next(item for item in result["request"]["steps"] if item["command"] == "confirm")
    selector = next(iter(confirmation["output_selectors"].values()))
    assert selector["array_path"] == "results"
    assert selector["match_field"] == "subject_id"
    assert selector["field"] == "calculation_id"
    assert "$ref:" not in canonical(result["request"])
    assert (
        result["request"]["source_manifest_digest"]
        == digest(result["request"]["source_manifest"]).hex()
    )
    assert all(
        "depends_on" in item and "period" in item for item in result["request"]["source_manifest"]
    )


def test_separate_compilations_never_reuse_business_uuid_or_historical_bindings():
    value = document()
    value["runtime_bindings"] = {
        "opening:zero": {"subject_id": "old-subject", "calculation_id": "old-calc"}
    }
    value["runtime_execution"] = {"completed": True, "old-database": "old-company"}
    first, second = compile_document(value), compile_document(value)
    first_identity = next(
        item["value"] for item in first["mapping"]["identities"] if item["key"] == "opening:zero"
    )
    second_identity = next(
        item["value"] for item in second["mapping"]["identities"] if item["key"] == "opening:zero"
    )
    assert first_identity != second_identity
    assert first["mapping"]["steps"] == second["mapping"]["steps"]
    assert "old-calc" not in canonical(first)
    assert "old-subject" not in canonical(first)


def test_unknown_explicit_step_and_invalid_fact_produce_structured_issues():
    value = document()
    value["facts"][0]["data"]["inferred_amount_fen"] = 0
    value["steps"].append({"key": "unrecognized", "command": "arbitrary_journal", "data": {}})
    with pytest.raises(ReplayDocumentCompileError) as exc:
        compile_document(value)
    codes = {item["code"] for item in exc.value.issues}
    assert {"typed_fact_template_invalid", "unsupported_document_step"} <= codes


def test_raw_old_business_id_cannot_enter_new_source_fact():
    value = document()
    value["facts"][0]["data"]["package_id"] = "old-opening-package"
    with pytest.raises(ReplayDocumentCompileError) as exc:
        compile_document(value)
    assert any(item["code"] == "runtime_identity_forbidden" for item in exc.value.issues)


def test_embedded_owner_confirmation_is_preserved_verbatim_with_explicit_source():
    value = document()
    value["entities"] = [
        {
            "key": "worker:example",
            "kind": "person",
            "data": {"display_name": "Example worker"},
            "sources": ["owner-original"],
        }
    ]
    confirmation = {"question": "Does the employee join on January 1?", "answer": "Yes, January 1."}
    value["workforce_relationships"] = [
        {
            "entity_key": "worker:example",
            "sources": ["owner-original"],
            "owner_confirmation_in_document": confirmation,
            "events": [
                {"key": "join:example", "data": {"action": "hire", "effective_date": "2026-01-01"}}
            ],
        }
    ]
    result = compile_document(value)
    preserved = result["preservation_requirements"][0]
    assert preserved["sha256"] == sha256(preserved["content"].encode("utf-8")).hexdigest()
    assert confirmation["answer"] in preserved["content"]
    assert any(
        item["kind"] == "confirmation" and item["evidence_digest"] == preserved["sha256"]
        for item in result["request"]["source_manifest"]
    )
    worker_step = next(
        item for item in result["request"]["steps"] if item["command"] == "register_worker"
    )
    assert worker_step["payload"]["evidence_digest"] == preserved["sha256"]
    assert isinstance(worker_step["payload"]["expected_revision"], dict)


def test_chinese_and_colon_logical_keys_use_ascii_graph_references():
    builder = ReplayDocumentBuilder(
        {}, company_id="c", database_id="d", owner_confirmation=PROOF, request_id="r"
    )
    builder.allocate_subject("中文:business:2026-01")
    builder.add_step(
        "中文:source", "save_facts", {"facts": []}, outputs={"revision": "results.0.revision"}
    )
    assert builder.resolve("$ref:中文:business:2026-01:subject_id").startswith("subject_")
    assert builder.resolve("$ref:中文:source:revision")["$replay_ref"].isascii()
    assert builder.steps[0]["step_id"].isascii()


def test_missing_source_never_defaults_to_the_owner_scope_confirmation():
    value = document()
    value["facts"][0]["source_keys"] = []
    with pytest.raises(ReplayDocumentCompileError) as exc:
        compile_document(value)
    assert any(item["code"] == "fact_evidence_missing" for item in exc.value.issues)


def test_labor_start_confirmation_does_not_replace_later_hire_source():
    value = document()
    value["entities"] = [
        {
            "key": "worker",
            "kind": "person",
            "data": {"display_name": "Example"},
            "sources": ["owner-original"],
        }
    ]
    value["workforce_relationships"] = [
        {
            "entity_key": "worker",
            "sources": ["owner-original"],
            "source_locations": ["Original L2-L4: hire in January"],
            "owner_confirmation_in_document": {
                "statements": ["Labor management starts on the month's first day"],
                "adopted_management_start_date": "2025-12-01",
            },
            "events": [
                {"key": "labor", "data": {"action": "start_labor", "effective_date": "2025-12-01"}},
                {"key": "hire", "data": {"action": "hire", "effective_date": "2026-01-01"}},
            ],
        }
    ]
    result = compile_document(value)
    labor = next(
        item for item in result["request"]["steps"] if item["command"] == "register_worker"
    )
    hire = next(
        item
        for item in result["request"]["steps"]
        if item["command"] == "change_workforce_relationship"
    )
    assert labor["payload"]["evidence_digest"] == result["preservation_requirements"][0]["sha256"]
    assert hire["payload"]["evidence_digest"] == PROOF
    assert "Original L2-L4" in hire["payload"]["source"]


def test_additional_preserved_hire_original_is_explicit_in_manifest_and_requirement():
    value = document()
    value["entities"] = [
        {
            "key": "worker",
            "kind": "person",
            "data": {"display_name": "Example"},
            "sources": ["owner-original"],
        }
    ]
    value["workforce_relationships"] = [
        {
            "entity_key": "worker",
            "sources": ["owner-original"],
            "additional_preserved_source_sha256": "c" * 64,
            "additional_source_location": "archive original-confirmation.txt L1",
            "events": [{"key": "hire", "data": {"action": "hire", "effective_date": "2026-01-01"}}],
        }
    ]
    result = compile_document(value)
    worker = next(
        item for item in result["request"]["steps"] if item["command"] == "register_worker"
    )
    assert worker["payload"]["evidence_digest"] == "c" * 64
    assert (
        result["preservation_requirements"][0]["archive_locator"]
        == "archive original-confirmation.txt L1"
    )
    assert any(item["evidence_digest"] == "c" * 64 for item in result["request"]["source_manifest"])


def test_company_note_selects_declared_acceptance_instead_of_source_list_position():
    value = document()
    value["sources"].append({"key": "other", "sha256": "c" * 64})
    value["company_note"] = {
        "key": "note",
        "text": "Accepted operational note",
        "sources": ["owner-original", "other"],
        "execution_requirements_source": "owner-original",
    }
    result = compile_document(value)
    note = next(
        item for item in result["request"]["steps"] if item["command"] == "update_company_note"
    )
    assert note["payload"]["evidence_digest"] == PROOF
    del value["company_note"]["execution_requirements_source"]
    with pytest.raises(ReplayDocumentCompileError) as exc:
        compile_document(value)
    assert any(
        item["code"] == "company_note_acceptance_source_missing" for item in exc.value.issues
    )


def test_compiled_synthetic_document_executes_opening_inventory_and_close(tmp_path, monkeypatch):
    from draft_bundle_fixture import synthetic_draft_bundle
    from test_new_company_reports import profile, zero_tax

    from ai_accounting.kernel import schema_bundle
    from ai_accounting.kernel.replay_runner import run_ready
    from ai_accounting.kernel.service import LocalService

    bundle = synthetic_draft_bundle(tmp_path / "draft-bundle")
    monkeypatch.setattr(schema_bundle, "production_bundle", lambda: bundle)
    root = tmp_path / "synthetic-only"
    bootstrap = LocalService(root)
    password = "Synthetic-compiled-document-owner-123"
    bootstrap.security.provision("owner", password)
    token = bootstrap.security.login("owner", password).session_token
    company = bootstrap.dispatch(
        "create_company",
        {"taxpayer_id": "91310000123456789A", "name": "Synthetic compile company"},
        session_token=token,
    )["id"]
    engine = bootstrap.engine(company)
    proof = engine.register_evidence(
        b"Owner explicitly confirms empty formation and no January activity",
        "text/plain",
        "synthetic owner proof",
        request_id="synthetic-evidence",
    )["digest"]
    database = engine.store.database_id
    scope = {
        "format": "ai-accounting-kernel/2/replay-close-scope/1",
        "catalog_instance_id": bootstrap.security.catalog_instance_id,
        "targets": [
            {
                "company_id": company,
                "database_id": database,
                "first_period": "2026-01",
                "last_period": "2026-01",
                "owner_confirmation": proof,
            }
        ],
    }
    bootstrap.close()
    value = document()
    value["sources"][0]["sha256"] = proof
    for row in value["inventory_replay_plan"]["rows"]:
        row["basis"]["confirmation_sha256"] = proof

    def collect(kind, key, data):
        value["facts"].append(
            {
                "key": key,
                "kind": kind,
                "data": data,
                "source_keys": ["owner-original"],
                "depends_on": [],
            }
        )

    profile(collect, "2026-01")
    zero_tax(collect, 2026, 1)
    compiled = compile_replay_document(
        value,
        company_id=company,
        database_id=database,
        owner_confirmation=proof,
        request_id="synthetic-compile",
    )
    app = LocalService(root, replay_scope=scope)
    try:
        plan = app.dispatch(
            "prepare_replay",
            {"company_id": company, "request": compiled["request"]},
            session_token=token,
        )
        queued = app.dispatch(
            "confirm_replay",
            {
                "company_id": company,
                "request": {
                    "plan_id": plan["plan_id"],
                    "plan_digest": plan["plan_digest"],
                    "request_id": "synthetic-confirm",
                },
            },
            session_token=token,
        )
        for _ in range(len(compiled["request"]["steps"]) + 1):
            result = run_ready(app, limit=1, company_id=company)
            assert not result or result[0]["status"] == "completed", result
        status = app.dispatch(
            "replay_status",
            {
                "company_id": company,
                "request": {"run_id": queued["run_id"]},
            },
            session_token=token,
        )
        assert status["status"] == "completed", status
        assert status["progress"]["closed_through"] == "2026-01"
        with app.engine(company).store.connection(read_only=True) as connection:
            assert connection.execute("SELECT count(*) FROM period_close").fetchone()[0] == 1
            assert connection.execute("SELECT count(*) FROM replay_binding").fetchone()[0] > 0
    finally:
        app.close()
