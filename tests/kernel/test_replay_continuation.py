"""Continuation is built exclusively from current-run immutable receipts."""

from copy import deepcopy

import pytest
from test_replay_document import compile_document, document

from ai_accounting.kernel.replay_continuation import prepare_replay_continuation
from ai_accounting.kernel.replay_document import ReplayDocumentCompileError
from ai_accounting.kernel.replay_runner import _request_key
from ai_accounting.kernel.types import canonical, digest


def original_and_receipts():
    request = compile_document(document())["request"]
    save, preview, confirm = request["steps"][:3]
    subject = save["payload"]["facts"][0]["subject_id"]
    responses = [
        {
            "status": "confirmed",
            "results": [
                {
                    "status": "confirmed",
                    "subject_id": subject,
                    "fact_id": "actual-fact",
                    "revision": 1,
                }
            ],
        },
        {
            "status": "preview",
            "digest": "c" * 64,
            "epochs": {"accounting": 1, "material": 0, "management": 0},
        },
        {
            "status": "published",
            "results": [{"subject_id": subject, "calculation_id": "actual-calculation"}],
        },
    ]
    receipts = [
        {
            "step_id": step["step_id"],
            "command": step["command"],
            "status": "completed",
            "request_id": _request_key(request["request_id"], step["step_id"]),
            "response": response,
        }
        for step, response in zip((save, preview, confirm), responses, strict=True)
    ]
    return request, receipts


def explicit_child():
    original, receipts = original_and_receipts()
    child = prepare_replay_continuation(
        original, completed_receipts=receipts[:1], request_id="child", parent_run_id="parent"
    )["request"]
    child_receipts = []
    for step, receipt in zip(child["steps"][:2], receipts[1:], strict=True):
        row = deepcopy(receipt)
        row["step_id"] = step["step_id"]
        row["request_id"] = _request_key("child", step["step_id"])
        child_receipts.append(row)
    return original, receipts, child, child_receipts


def test_explicit_ancestor_chain_uses_actual_original_keys_without_business_recompile():
    original, receipts, child, child_receipts = explicit_child()
    frozen = deepcopy(child)
    result = prepare_replay_continuation(
        child, completed_receipts=child_receipts, request_id="grandchild",
        parent_run_id="child-run",
        ancestor_checkpoints=[{
            "run_id": "parent", "request": original, "completed_receipts": receipts[:1],
        }],
    )
    assert child == frozen
    assert result["ancestor_run_ids"] == ["parent"]
    assert result["ancestor_binding_proof"][0]["original_request_id"] == receipts[0]["request_id"]
    assert result["request"]["continuation_from"] == "child-run"
    assert [row["step_id"] for row in result["request"]["steps"]] == [
        row["step_id"] for row in child["steps"][2:]
    ]
    assert child_receipts[1]["step_id"] in result["request"]["steps"][0]["depends_on"]


@pytest.mark.parametrize("case,code", [
    ("missing", "continuation_ancestral_context_required"),
    ("unlinked", "continuation_ancestor_not_linked"),
    ("scope", "continuation_ancestor_scope_mismatch"),
    ("wrong_key", "continuation_request_mismatch"),
    ("cycle", "continuation_ancestor_cycle"),
])
def test_ancestor_chain_never_accepts_unlinked_or_unverified_history(case, code):
    original, receipts, child, child_receipts = explicit_child()
    checkpoints = [{"run_id": "parent", "request": original, "completed_receipts": receipts[:1]}]
    if case == "missing":
        checkpoints = []
    elif case == "unlinked":
        checkpoints.append(dict(checkpoints[0], run_id="unlinked-old-root"))
    elif case == "scope":
        original["company_id"] = "other-company"
    elif case == "wrong_key":
        receipts[0]["request_id"] = "wrong-seed"
    elif case == "cycle":
        original["continuation_from"] = "parent"
    with pytest.raises(ReplayDocumentCompileError) as exc:
        prepare_replay_continuation(
            child, completed_receipts=child_receipts, request_id="grandchild",
            parent_run_id="child-run", ancestor_checkpoints=checkpoints,
        )
    assert exc.value.issues[0]["code"] == code


def test_partial_publication_outputs_substitute_literals_without_new_business_ids():
    original, receipts = original_and_receipts()
    frozen = deepcopy(original)
    result = prepare_replay_continuation(
        original,
        completed_receipts=receipts,
        request_id="continuation",
        source_manifest_extra=[
            {
                "source_id": "this-run-receipts",
                "kind": "source_record",
                "evidence_digest": "d" * 64,
                "disposition": "reference",
            }
        ],
    )
    assert original == frozen
    assert len(result["request"]["steps"]) == 3
    assert result["completed_step_ids"] == [row["step_id"] for row in receipts]
    assert {
        row["value"] for row in result["binding_proof"] if row["field"] == "results.0.fact_id"
    } == {"actual-fact"}
    assert any(row["value"] == "actual-calculation" for row in result["binding_proof"])
    assert "d" * 64 in result["request"]["preserved_evidence"]
    assert (
        result["request"]["source_manifest_digest"]
        == digest(result["request"]["source_manifest"]).hex()
    )
    assert all(
        not set(step["depends_on"]).intersection(result["completed_step_ids"])
        for step in result["request"]["steps"]
    )


def test_only_completed_reference_values_change_and_uncommitted_refs_remain():
    original, receipts = original_and_receipts()
    result = prepare_replay_continuation(
        original, completed_receipts=receipts[:1], request_id="after-save"
    )
    remaining = result["request"]["steps"]
    assert remaining[0]["payload"] == original["steps"][1]["payload"]
    assert (
        remaining[1]["payload"]["preview_digest"]
        == original["steps"][2]["payload"]["preview_digest"]
    )
    assert remaining[1]["output_selectors"] == original["steps"][2]["output_selectors"]


@pytest.mark.parametrize(
    "change,code",
    [
        (lambda rows: rows[0].update(status="pending"), "continuation_receipt_not_completed"),
        (lambda rows: rows[0].update(command="register_entity"), "continuation_command_mismatch"),
        (lambda rows: rows[0].update(request_id="other-request"), "continuation_request_mismatch"),
        (
            lambda rows: rows[0]["response"].update(company_id="other-company"),
            "continuation_company_mismatch",
        ),
        (
            lambda rows: rows[0]["response"]["results"][0].update(subject_id="other-subject"),
            "continuation_subject_mismatch",
        ),
        (
            lambda rows: rows[0]["response"]["results"][0].pop("revision"),
            "continuation_output_missing",
        ),
        (
            lambda rows: rows[2]["response"]["results"].append(
                deepcopy(rows[2]["response"]["results"][0])
            ),
            "continuation_output_missing",
        ),
    ],
)
def test_unreliable_receipts_do_not_establish_continuation(change, code):
    original, receipts = original_and_receipts()
    change(receipts)
    with pytest.raises(ReplayDocumentCompileError) as exc:
        prepare_replay_continuation(original, completed_receipts=receipts, request_id="new-prepare")
    assert exc.value.issues[0]["code"] == code


def test_receipt_dependency_closure_duplicates_and_reused_prepare_id_are_rejected():
    original, receipts = original_and_receipts()
    for rows, request_id, code in [
        (receipts[1:], "new", "continuation_receipt_dependency_missing"),
        ([*receipts, receipts[0]], "new", "continuation_duplicate_receipt"),
        (receipts, original["request_id"], "continuation_request_id_reused"),
    ]:
        with pytest.raises(ReplayDocumentCompileError) as exc:
            prepare_replay_continuation(original, completed_receipts=rows, request_id=request_id)
        assert exc.value.issues[0]["code"] == code


def test_a_cut_after_preview_refreshes_read_only_preparation_and_preserves_receipts():
    original, receipts = original_and_receipts()
    result = prepare_replay_continuation(
        original, completed_receipts=receipts[:2], request_id="new"
    )
    repeated = result["reprepared_steps"]
    assert len(repeated) == 1 and repeated[0]["command"] == "preview"
    assert repeated[0]["original_step_id"] == original["steps"][1]["step_id"]
    fresh_preview, pending_confirm = result["request"]["steps"][:2]
    assert fresh_preview["step_id"] == repeated[0]["new_step_id"]
    assert fresh_preview["step_id"] != original["steps"][1]["step_id"]
    assert fresh_preview["output_ref"] == original["steps"][1]["output_ref"]
    assert (
        pending_confirm["payload"]["preview_digest"]
        == original["steps"][2]["payload"]["preview_digest"]
    )
    assert fresh_preview["step_id"] in pending_confirm["depends_on"]
    assert len(result["completed_step_ids"]) == 2


def test_closed_prefix_is_not_silently_removed_or_skipped():
    original, _ = original_and_receipts()
    close = original["steps"][-1]
    receipt = {
        "step_id": close["step_id"],
        "command": "close",
        "status": "completed",
        "request_id": _request_key(original["request_id"], close["step_id"]),
        "response": {"status": "closed", "period": "2026-01"},
    }
    with pytest.raises(ReplayDocumentCompileError) as exc:
        prepare_replay_continuation(original, completed_receipts=[receipt], request_id="new")
    assert exc.value.issues[0]["code"] == "continuation_closed_prefix_requires_review"


def material_parent_request_and_receipts():
    original, receipts = original_and_receipts()
    proof = original["source_manifest"][0]["evidence_digest"]
    publication = original["steps"][2]
    calculation_ref = {
        "$replay_ref": publication["output_ref"],
        "field": publication["output_fields"][0],
    }
    rows = []

    def append(step_id, command, payload, dependencies, outputs, response):
        step = {
            "step_id": step_id,
            "command": command,
            "payload": payload,
            "depends_on": dependencies,
        }
        if outputs:
            step.update(output_ref="output-" + step_id, output_fields=outputs)
        rows.append(step)
        receipts.append(
            {
                "step_id": step_id,
                "command": command,
                "status": "completed",
                "request_id": _request_key(original["request_id"], step_id),
                "response": response,
            }
        )

    for name in ("main", "copy"):
        append(
            "receive-" + name,
            "receive_material",
            {
                "subject_id": "this-run-material-" + name,
                "data": {
                    "period": "2026-01",
                    "evidence_digest": proof,
                    "category": "bank",
                    "purpose": "business",
                    "specification": {"format": "text", "passages": [], "all_pages_reviewed": True},
                },
                "evidence": [proof],
                "expected_revision": 0,
            },
            [],
            ["fact_id", "revision", "allocation_fact_id", "allocation_revision"],
            {
                "status": "confirmed",
                "subject_id": "this-run-material-" + name,
                "fact_id": "actual-material-" + name,
                "revision": 1,
                "allocation_fact_id": "actual-allocation-" + name,
                "allocation_revision": 1,
            },
        )
    assignments = [
        {
            "location": "L1",
            "recognition_period": "2026-01",
            "basis": "confirmed_period",
            "basis_evidence_digest": proof,
            "basis_location": "L1",
            "basis_excerpt": "January",
        }
    ]
    append(
        "allocation-preview",
        "preview_material_allocation",
        {
            "source_id": "this-run-material-main",
            "assignments": assignments,
        },
        ["receive-main"],
        ["digest", "epochs", "expected_revision"],
        {
            "status": "preview",
            "digest": "d" * 64,
            "epochs": {"accounting": 1, "material": 1, "management": 0},
            "expected_revision": 1,
        },
    )
    append(
        "allocation-confirm",
        "confirm_material_allocation",
        {
            "source_id": "this-run-material-main",
            "assignments": assignments,
            "preview_digest": {"$replay_ref": "output-allocation-preview", "field": "digest"},
            "epochs": {"$replay_ref": "output-allocation-preview", "field": "epochs"},
            "expected_revision": {
                "$replay_ref": "output-allocation-preview",
                "field": "expected_revision",
            },
        },
        ["allocation-preview"],
        ["fact_id", "revision"],
        {
            "status": "confirmed",
            "fact_id": "actual-explicit-allocation",
            "revision": 2,
        },
    )
    append(
        "primary-group",
        "resolve_material_group",
        {
            "subject_id": "this-run-primary-group",
            "data": {
                "period": "2026-01",
                "source_id": "this-run-material-main",
                "source_fact_id": {"$replay_ref": "output-receive-main", "field": "fact_id"},
                "members": [{"location": "L1", "amount_fen": 100}],
                "group_amount_fen": 100,
                "links": [
                    {
                        "calculation_id": calculation_ref,
                        "amount_field": "fact.amount_fen",
                        "amount_fen": 100,
                    }
                ],
                "basis_evidence_digest": proof,
                "basis_location": "L1",
                "reason": "Explicit synthetic pool",
            },
            "evidence": [proof],
            "expected_revision": 0,
        },
        ["receive-main", "allocation-confirm", publication["step_id"]],
        ["fact_id", "revision"],
        {
            "status": "confirmed",
            "subject_id": "this-run-primary-group",
            "fact_id": "actual-primary-group",
            "revision": 1,
        },
    )
    pending = {
        "step_id": "copy-disposition",
        "command": "resolve_material",
        "payload": {
            "subject_id": "this-run-copy-disposition",
            "data": {
                "period": "2026-01",
                "source_id": "this-run-material-copy",
                "source_fact_id": {"$replay_ref": "output-receive-copy", "field": "fact_id"},
                "location": "L1",
                "treatment": "duplicate",
                "links": [],
                "amount_fen": 100,
                "duplicate_source_id": "this-run-material-main",
                "duplicate_location": "L1",
            },
            "evidence": [proof],
            "expected_revision": 0,
        },
        "depends_on": ["receive-copy", "receive-main", "primary-group"],
    }
    original["steps"] = [*original["steps"][:3], *rows, pending, *original["steps"][3:]]
    original["steps"][-2]["depends_on"].append("copy-disposition")
    return original, receipts


def test_parent_material_receipts_provide_validation_lineage_but_are_not_executed_again():
    original, receipts = material_parent_request_and_receipts()
    continued = prepare_replay_continuation(
        original,
        completed_receipts=receipts,
        request_id="with-parent",
        parent_run_id="actual-parent-run",
    )
    assert continued["request"]["continuation_from"] == "actual-parent-run"
    copy = next(
        step for step in continued["request"]["steps"] if step["step_id"] == "copy-disposition"
    )
    assert {"receive-copy", "receive-main", "primary-group"} <= set(copy["depends_on"])
    assert copy["payload"]["data"]["source_fact_id"] == "actual-material-copy"
    assert not any(
        step["command"]
        in {"receive_material", "resolve_material_group", "confirm_material_allocation"}
        for step in continued["request"]["steps"]
    )
    with pytest.raises(ReplayDocumentCompileError) as exc:
        prepare_replay_continuation(
            original, completed_receipts=receipts, request_id="without-parent"
        )
    assert exc.value.issues[0]["code"] == "continuation_requires_fresh_preparation"


def test_explicit_second_material_cut_keeps_verified_ancestor_anchors():
    original, receipts = material_parent_request_and_receipts()
    child = prepare_replay_continuation(
        original, completed_receipts=receipts[:4], request_id="material-child",
        parent_run_id="material-parent",
    )["request"]
    child_receipts = deepcopy(receipts[4:])
    for row in child_receipts:
        row["request_id"] = _request_key("material-child", row["step_id"])
    result = prepare_replay_continuation(
        child, completed_receipts=child_receipts, request_id="material-grandchild",
        parent_run_id="material-child-run",
        ancestor_checkpoints=[{
            "run_id": "material-parent", "request": original,
            "completed_receipts": receipts[:4],
        }],
    )
    copy = next(row for row in result["request"]["steps"] if row["step_id"] == "copy-disposition")
    assert {"receive-main", "receive-copy", "primary-group"} <= set(copy["depends_on"])
    assert copy["payload"]["data"]["source_fact_id"] == "actual-material-copy"
    assert not any(row["command"] == "receive_material" for row in result["request"]["steps"])


def test_ancestor_close_preview_is_replaced_by_current_fresh_preview():
    original, receipts = original_and_receipts()
    for step, response in zip(original["steps"][3:5], [
        {"results": [{"inventory_id": f"actual-inventory-{i}"} for i in range(6)]},
        {"digest": "d" * 64, "epochs": {"accounting": 1, "material": 1, "management": 0}},
    ], strict=True):
        receipts.append({"step_id": step["step_id"], "command": step["command"],
                         "status": "completed", "response": response,
                         "request_id": _request_key(original["request_id"], step["step_id"])})
    child = prepare_replay_continuation(
        original, completed_receipts=receipts, request_id="close-child",
        parent_run_id="close-parent",
    )["request"]
    result = prepare_replay_continuation(
        child, completed_receipts=[], request_id="close-grandchild",
        parent_run_id="close-child-run",
        ancestor_checkpoints=[{"run_id": "close-parent", "request": original,
                               "completed_receipts": receipts}],
    )
    assert [row["command"] for row in result["request"]["steps"]] == ["preview_close", "close"]
    assert result["request"]["steps"][1]["payload"]["preview_digest"] == (
        child["steps"][1]["payload"]["preview_digest"]
    )


@pytest.mark.parametrize("cut", (3, 4))
@pytest.mark.parametrize("use_parent", (False, True))
def test_synthetic_partial_run_continues_after_build_change_without_repeating_writes(
    tmp_path, monkeypatch, cut, use_parent
):
    from dataclasses import replace

    from draft_bundle_fixture import synthetic_draft_bundle
    from test_new_company_reports import profile, zero_tax

    from ai_accounting.kernel import runtime_snapshot, schema_bundle
    from ai_accounting.kernel.replay_document import compile_replay_document
    from ai_accounting.kernel.replay_runner import run_ready
    from ai_accounting.kernel.service import LocalService

    bundle = synthetic_draft_bundle(tmp_path / "draft-bundle")
    monkeypatch.setattr(schema_bundle, "production_bundle", lambda: bundle)
    root = tmp_path / "synthetic-continuation-only"
    bootstrap = LocalService(root)
    password = "Synthetic-continuation-owner-123"
    bootstrap.security.provision("owner", password)
    token = bootstrap.security.login("owner", password).session_token
    company = bootstrap.dispatch(
        "create_company",
        {
            "taxpayer_id": "91310000123456789A",
            "name": "Synthetic continuation company",
        },
        session_token=token,
    )["id"]
    engine = bootstrap.engine(company)
    proof = engine.register_evidence(
        b"Explicit synthetic empty formation and no January activity",
        "text/plain",
        "owner proof",
        request_id="proof",
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
    value["entities"] = [
        {
            "key": "person",
            "kind": "person",
            "data": {"display_name": "Synthetic person"},
            "sources": ["owner-original"],
        }
    ]

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
    original = compile_replay_document(
        value,
        company_id=company,
        database_id=database,
        owner_confirmation=proof,
        request_id="original-prepare",
    )["request"]

    def start(app, request, confirmation_key):
        prepared = app.dispatch(
            "prepare_replay", {"company_id": company, "request": request}, session_token=token
        )
        return app.dispatch(
            "confirm_replay",
            {
                "company_id": company,
                "request": {
                    "plan_id": prepared["plan_id"],
                    "plan_digest": prepared["plan_digest"],
                    "request_id": confirmation_key,
                },
            },
            session_token=token,
        )["run_id"]

    old_app = LocalService(root, replay_scope=scope)
    old_run = start(old_app, original, "original-confirm")
    for _ in range(cut):
        assert run_ready(old_app, company_id=company)[0]["status"] == "completed"
    receipts = [
        old_app.dispatch(
            "replay_status",
            {
                "company_id": company,
                "request": {"run_id": old_run, "step_id": step["step_id"]},
            },
            session_token=token,
        )["receipt"]
        for step in original["steps"][:cut]
    ]
    assert all(item["status"] == "completed" for item in receipts)
    assert receipts[0]["command"] == "register_entity"
    assert receipts[-1]["command"] == ("confirm" if cut == 4 else "preview")
    old_app.close()
    prior_snapshot = runtime_snapshot.capture_runtime_snapshot()
    changed_snapshot = replace(
        prior_snapshot,
        build_id=digest("synthetic new build").hex(),
        runtime_source_digest=digest("synthetic new runtime source").hex(),
    )
    monkeypatch.setattr(runtime_snapshot, "capture_runtime_snapshot", lambda **_: changed_snapshot)
    app = LocalService(root, replay_scope=scope)
    try:
        blocked = run_ready(app, company_id=company)
        assert blocked[0]["status"] == "blocked" and "build" in blocked[0]["issues"]
        sources = []
        if not use_parent:
            provenance = app.engine(company).register_evidence(
                canonical(receipts).encode("utf-8"),
                "application/json",
                "this run's exact receipts",
                request_id="receipt-proof",
            )["digest"]
            sources.append(
                {
                    "source_id": "parent-run-receipts",
                    "kind": "source_record",
                    "evidence_digest": provenance,
                    "disposition": "reference",
                }
            )
        continuation = prepare_replay_continuation(
            original,
            completed_receipts=receipts,
            request_id="continuation-prepare",
            source_manifest_extra=sources,
            parent_run_id=old_run if use_parent else None,
        )
        new_run = start(app, continuation["request"], "continuation-confirm")
        for _ in range(len(continuation["request"]["steps"]) + 1):
            progressed = run_ready(app, company_id=company)
            assert not progressed or progressed[0]["status"] == "completed", progressed
        status = app.dispatch(
            "replay_status",
            {"company_id": company, "request": {"run_id": new_run}},
            session_token=token,
        )
        assert status["status"] == "completed", status
        with app.engine(company).store.connection(read_only=True) as connection:
            assert connection.execute("SELECT count(*) FROM entity").fetchone()[0] == 1
            assert (
                connection.execute(
                    "SELECT count(*) FROM fact_revision f JOIN subject s ON s.id=f.subject_id "
                    "WHERE s.kind='opening_package'"
                ).fetchone()[0]
                == 1
            )
            assert connection.execute("SELECT count(*) FROM period_close").fetchone()[0] == 1
            for receipt in receipts:
                row = connection.execute(
                    "SELECT response,status FROM replay_step WHERE run_id=? AND step_id=?",
                    (old_run, receipt["step_id"]),
                ).fetchone()
                assert row["status"] == "completed"
                assert row["response"] == canonical(receipt["response"])
                if receipt["command"] != "preview":
                    assert (
                        connection.execute(
                            "SELECT count(*) FROM request WHERE id=?", (receipt["request_id"],)
                        ).fetchone()[0]
                        == 1
                    )
    finally:
        app.close()


def test_real_received_material_is_resolved_after_parent_continuation_without_second_receipt(
    tmp_path, monkeypatch
):
    from dataclasses import replace

    from draft_bundle_fixture import synthetic_draft_bundle
    from test_new_company_reports import profile, zero_tax

    from ai_accounting.kernel import runtime_snapshot, schema_bundle
    from ai_accounting.kernel.replay_document import compile_replay_document
    from ai_accounting.kernel.replay_runner import run_ready
    from ai_accounting.kernel.service import LocalService

    bundle = synthetic_draft_bundle(tmp_path / "draft-bundle")
    monkeypatch.setattr(schema_bundle, "production_bundle", lambda: bundle)
    root = tmp_path / "synthetic-material-continuation-only"
    bootstrap = LocalService(root)
    password = "Synthetic-material-continuation-123"
    bootstrap.security.provision("owner", password)
    token = bootstrap.security.login("owner", password).session_token
    company = bootstrap.dispatch(
        "create_company",
        {"taxpayer_id": "91310000123456789A", "name": "Synthetic material company"},
        session_token=token,
    )["id"]
    engine = bootstrap.engine(company)
    proof = engine.register_evidence(
        b"Explicit empty formation, no January transactions, policy is supporting only",
        "text/plain",
        "synthetic owner proof",
        request_id="owner-proof",
    )["digest"]
    policy = engine.register_evidence(
        b"Synthetic policy reference.",
        "text/plain",
        "supporting source",
        request_id="policy-original",
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
    value["sources"].append({"key": "policy-original", "sha256": policy})
    for row in value["inventory_replay_plan"]["rows"]:
        row["basis"]["confirmation_sha256"] = proof
        if row["category"] == "transactions":
            row["no_business"] = False
            row["source_coverage"] = [{"source_key": "policy-original", "sha256": policy}]
    value["materials"] = [
        {
            "key": "policy-material",
            "kind": "material_source_v2",
            "source_keys": ["policy-original", "owner-original"],
            "depends_on": [],
            "data": {
                "period": "2026-01",
                "evidence_digest": policy,
                "category": "transactions",
                "purpose": "supporting",
                "supporting_purpose": "Policy review only",
                "specification": {
                    "format": "text",
                    "all_pages_reviewed": True,
                    "passages": [
                        {"location": "L1", "page": 1, "excerpt": "Synthetic policy reference."}
                    ],
                },
            },
        },
        {
            "key": "policy-disposition",
            "kind": "material_resolution_v2",
            "source_keys": ["policy-original", "owner-original"],
            "depends_on": ["policy-material"],
            "data": {
                "period": "2026-01",
                "source_id": "$ref:policy-material:subject_id",
                "source_fact_id": "$ref:policy-material:fact_id",
                "location": "L1",
                "treatment": "supporting",
                "reason": "Policy reference only",
            },
        },
    ]

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
    original = compile_replay_document(
        value,
        company_id=company,
        database_id=database,
        owner_confirmation=proof,
        request_id="material-parent-prepare",
    )["request"]

    def start(app, request, confirmation):
        plan = app.dispatch(
            "prepare_replay", {"company_id": company, "request": request}, session_token=token
        )
        return app.dispatch(
            "confirm_replay",
            {
                "company_id": company,
                "request": {
                    "plan_id": plan["plan_id"],
                    "plan_digest": plan["plan_digest"],
                    "request_id": confirmation,
                },
            },
            session_token=token,
        )["run_id"]

    app = LocalService(root, replay_scope=scope)
    parent_run = start(app, original, "material-parent-confirm")
    completed = []
    for _ in range(len(original["steps"])):
        progressed = run_ready(app, company_id=company)[0]
        assert progressed["status"] == "completed", progressed
        receipt = app.dispatch(
            "replay_status",
            {
                "company_id": company,
                "request": {"run_id": parent_run, "step_id": progressed["step_id"]},
            },
            session_token=token,
        )["receipt"]
        completed.append(receipt)
        if receipt["command"] == "receive_material":
            break
    assert completed[-1]["command"] == "receive_material"
    app.close()
    before = runtime_snapshot.capture_runtime_snapshot()
    changed = replace(
        before,
        build_id=digest("synthetic material new build").hex(),
        runtime_source_digest=digest("synthetic material runtime").hex(),
    )
    monkeypatch.setattr(runtime_snapshot, "capture_runtime_snapshot", lambda **_: changed)
    app = LocalService(root, replay_scope=scope)
    try:
        assert run_ready(app, company_id=company)[0]["status"] == "blocked"
        continued = prepare_replay_continuation(
            original,
            completed_receipts=completed,
            request_id="material-continuation-prepare",
            parent_run_id=parent_run,
        )
        new_run = start(app, continued["request"], "material-continuation-confirm")
        for _ in range(len(continued["request"]["steps"]) + 1):
            progressed = run_ready(app, company_id=company)
            assert not progressed or progressed[0]["status"] == "completed", progressed
        status = app.dispatch(
            "replay_status",
            {"company_id": company, "request": {"run_id": new_run}},
            session_token=token,
        )
        assert status["status"] == "completed", status
        with app.engine(company).store.connection(read_only=True) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM subject WHERE kind='material_source_v2'"
                ).fetchone()[0]
                == 1
            )
            assert (
                connection.execute(
                    "SELECT count(*) FROM subject WHERE kind='material_resolution_v2'"
                ).fetchone()[0]
                == 1
            )
            assert connection.execute("SELECT count(*) FROM period_close").fetchone()[0] == 1
            assert (
                connection.execute(
                    "SELECT count(*) FROM request WHERE id=?", (completed[-1]["request_id"],)
                ).fetchone()[0]
                == 1
            )
    finally:
        app.close()
