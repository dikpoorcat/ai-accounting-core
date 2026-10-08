"""Malformed public references stop before writes; content checks retain their semantics."""

import copy
import json
import sys
from functools import partial
from types import SimpleNamespace

import pytest
from pydantic import TypeAdapter, ValidationError

from ai_accounting.kernel.asset_batches import AssetBatches
from ai_accounting.kernel.command_schema import validate_command
from ai_accounting.kernel.contracts import KernelError, NeedsInformation
from ai_accounting.kernel.diagnostics import error_response
from ai_accounting.kernel.discovery import Discovery
from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.identity_corrections import IdentityCorrections
from ai_accounting.kernel.materials import Materials
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.service import LocalService
from ai_accounting.kernel.types import EvidenceDigest, canonical

UNKNOWN = "a" * 64
MALFORMED = [
    "",
    "a",
    "a" * 63,
    "a" * 65,
    "g" * 64,
    "A" * 64,
    " " + UNKNOWN,
    UNKNOWN + " ",
    UNKNOWN + "\n",
    UNKNOWN[:32] + " " + UNKNOWN[32:],
    "\t" + UNKNOWN,
    "\u3000" + UNKNOWN,
    "ａ" * 64,
    True,
    1,
    1.5,
    [],
    {},
]
EXPENSE = dict(
    period="2026-01",
    amount_fen=100,
    counterparty_id="supplier",
    expense_class="administration",
    creditor_kind="supplier",
)
SOURCE = dict(
    period="2026-01",
    evidence_digest=UNKNOWN,
    category="transactions",
    purpose="business",
    specification={"format": "csv"},
)
LINK = dict(
    subject_id="expense",
    fact_kind="expense",
    fact_id="f",
    calculation_id="c",
    amount_field="fact.amount_fen",
    amount_fen=100,
    recognition_period="2026-01",
)
GROUP = dict(
    period="2026-01",
    source_id="source",
    source_fact_id="f",
    members=[{"location": "row:2", "amount_fen": 100}],
    group_amount_fen=100,
    links=[LINK],
    basis_evidence_digest=UNKNOWN,
    basis_location="confirmation",
    reason="synthetic",
)
RESOLUTION = dict(
    period="2026-01",
    source_id="source",
    source_fact_id="f",
    location="row:2",
    treatment="no_accounting",
    amount_fen=100,
    non_accounting_reason="forecast",
)


def public_cases():
    """Use complete wire models so only the selected evidence field is invalid."""
    revision = dict(expected_revision=0, request_id="rejected-reference")
    confirmation = dict(
        preview_digest=UNKNOWN,
        epochs={"accounting": 0, "material": 0, "management": 0},
        request_id="rejected-reference",
    )
    fact = dict(kind="expense", subject_id="expense", data=EXPENSE, evidence=[UNKNOWN], **revision)
    cases = [
        (
            "register_entity",
            "evidence_digest",
            dict(
                kind="person",
                data={},
                source="synthetic",
                request_id="rejected-reference",
                evidence_digest=UNKNOWN,
            ),
        ),
        (
            "update_entity_profile",
            "evidence_digest",
            dict(
                entity_id="entity", data={}, source="synthetic", evidence_digest=UNKNOWN, **revision
            ),
        ),
        ("save_fact", "evidence", fact),
        ("amend_fact", "evidence", dict(fact, recording_error_confirmed=True)),
        (
            "save_facts",
            "facts",
            dict(
                facts=[{k: v for k, v in fact.items() if k != "request_id"}],
                request_id="rejected-reference",
            ),
        ),
        (
            "prepare_fact_registration",
            "evidence",
            {k: v for k, v in fact.items() if k != "request_id"},
        ),
        (
            "preview_identity_correction",
            "evidence",
            dict(changes=[], evidence=[UNKNOWN], reason="synthetic"),
        ),
        (
            "confirm_identity_correction",
            "evidence",
            dict(changes=[], evidence=[UNKNOWN], reason="synthetic", **confirmation),
        ),
        (
            "update_company_note",
            "evidence_digest",
            dict(text="synthetic", evidence_digest=UNKNOWN, **revision),
        ),
        (
            "preview_delete",
            "recording_error_evidence",
            dict(subject_id="expense", recording_error_evidence=UNKNOWN),
        ),
        (
            "delete",
            "recording_error_evidence",
            dict(subject_id="expense", recording_error_evidence=UNKNOWN, **confirmation),
        ),
        (
            "inventory",
            "evidence",
            dict(
                period="2026-01",
                category="transactions",
                evidence=[UNKNOWN],
                expected=1,
                no_business=False,
                confirmation_evidence=UNKNOWN,
                request_id="rejected-reference",
            ),
        ),
        (
            "inventory",
            "confirmation_evidence",
            dict(
                period="2026-01",
                category="transactions",
                evidence=[UNKNOWN],
                expected=1,
                no_business=False,
                confirmation_evidence=UNKNOWN,
                request_id="rejected-reference",
            ),
        ),
        ("preview_close", "owner_confirmation", dict(period="2026-01", owner_confirmation=UNKNOWN)),
        (
            "close",
            "owner_confirmation",
            dict(
                period="2026-01",
                owner_confirmation=UNKNOWN,
                approval_id="synthetic",
                **confirmation,
            ),
        ),
        (
            "save_payee",
            "evidence_digest",
            dict(
                party_id="supplier",
                name="synthetic",
                account="00123",
                evidence_digest=UNKNOWN,
                **revision,
            ),
        ),
        (
            "preview_export",
            "template_evidence_digest",
            dict(period="2026-01", template_evidence_digest=UNKNOWN),
        ),
        (
            "confirm_export",
            "template_evidence_digest",
            dict(
                period="2026-01",
                template_evidence_digest=UNKNOWN,
                output_directory="synthetic",
                **confirmation,
            ),
        ),
        (
            "inspect_material",
            "evidence_digest",
            dict(evidence_digest=UNKNOWN, specification={"format": "csv"}),
        ),
        (
            "receive_material",
            "evidence",
            dict(subject_id="source", data=SOURCE, evidence=[UNKNOWN], **revision),
        ),
        (
            "resolve_material",
            "evidence",
            dict(subject_id="resolution", data=RESOLUTION, evidence=[UNKNOWN], **revision),
        ),
        (
            "resolve_material_group",
            "evidence",
            dict(subject_id="group", data=GROUP, evidence=[UNKNOWN], **revision),
        ),
        (
            "prepare_asset_activation_batch",
            "evidence",
            dict(
                subject_id="batch",
                period="2026-01",
                members=[],
                evidence=[UNKNOWN],
                expected_revision=0,
            ),
        ),
        (
            "confirm_asset_activation_batch",
            "evidence",
            dict(
                subject_id="batch",
                period="2026-01",
                members=[],
                evidence=[UNKNOWN],
                expected_revision=0,
                **confirmation,
            ),
        ),
        (
            "prepare_asset_consumption_month",
            "evidence",
            dict(period="2026-01", evidence=[UNKNOWN], expected_revision=0),
        ),
        (
            "confirm_asset_consumption_month",
            "evidence",
            dict(period="2026-01", evidence=[UNKNOWN], expected_revision=0, **confirmation),
        ),
        (
            "update_period_commentary",
            "evidence_digest",
            dict(
                period="2026-01",
                text="synthetic",
                context_digest=UNKNOWN,
                source="synthetic",
                evidence_digest=UNKNOWN,
                **revision,
            ),
        ),
        (
            "preview_replay_close_range",
            "owner_confirmation",
            dict(first_period="2026-01", last_period="2026-01", owner_confirmation=UNKNOWN),
        ),
        (
            "confirm_replay_close_range",
            "owner_confirmation",
            dict(
                first_period="2026-01",
                last_period="2026-01",
                owner_confirmation=UNKNOWN,
                **confirmation,
            ),
        ),
    ]
    return cases


@pytest.fixture(scope="module")
def service(tmp_path_factory):
    app = LocalService(tmp_path_factory.mktemp("evidence-reference-boundary"))
    app.security.provision("owner", "test-password-123")
    token = app.security.login("owner", "test-password-123").session_token
    app.dispatch = partial(app.dispatch, session_token=token)
    company = app.dispatch(
        "create_company", {"taxpayer_id": "911100000000000001", "name": "Synthetic"}
    )["id"]
    return app, company


def database_snapshot(engine):
    with engine.store.connection(read_only=True) as connection:
        return tuple(connection.iterdump())


@pytest.mark.parametrize("value", MALFORMED)
def test_shared_type_rejects_exact_format_and_type(value):
    with pytest.raises(ValidationError):
        TypeAdapter(EvidenceDigest).validate_python(value)
    with pytest.raises(ValidationError):
        TypeAdapter(EvidenceDigest).validate_json(canonical(value))
    assert TypeAdapter(EvidenceDigest).validate_python(UNKNOWN) == UNKNOWN


@pytest.mark.parametrize("command,field,payload", public_cases())
def test_all_public_references_reject_before_dispatch_and_company_writes(
    service, command, field, payload
):
    app, company = service
    payload = copy.deepcopy(payload) | {"company_id": company}
    # The baseline is syntactically valid, without claiming content is registered.
    validate_command(app.command_models, command, payload, registry=app.registry)
    engine = app.engine(company)
    before = database_snapshot(engine)
    for value in ("a", "A" * 64, UNKNOWN + "\n", True, 1.5):
        bad = copy.deepcopy(payload)
        if field == "facts":
            bad[field][0]["evidence"] = [UNKNOWN, value]
        else:
            bad[field] = [UNKNOWN, value] if field == "evidence" else value
        with pytest.raises(KernelError) as caught:
            app.dispatch(command, bad)
        response = caught.value.response()
        assert response["status"] == "rejected"
        assert response["code"] == "invalid_command"
        assert any(field in item["loc"] for item in response["issues"])
        assert all("input" not in item and "ctx" not in item for item in response["issues"])
        assert database_snapshot(engine) == before


def test_missing_container_and_later_batch_item_are_atomic(service):
    app, company = service
    before = database_snapshot(app.engine(company))
    base = dict(
        company_id=company,
        kind="expense",
        subject_id="new",
        data=EXPENSE,
        expected_revision=0,
        request_id="batch-invalid",
    )
    for evidence in (None, True, 1.5, UNKNOWN, {}):
        payload = base | {"evidence": evidence}
        with pytest.raises(KernelError) as caught:
            app.dispatch("save_fact", payload)
        assert caught.value.code == "invalid_command"
    with pytest.raises(KernelError) as caught:
        app.dispatch("save_fact", base)
    assert caught.value.code == "invalid_command"
    good = {k: v for k, v in base.items() if k not in {"company_id", "request_id"}} | {
        "evidence": [UNKNOWN]
    }
    with pytest.raises(KernelError) as caught:
        app.dispatch(
            "save_facts",
            dict(
                company_id=company,
                request_id="batch-invalid",
                facts=[good, good | {"subject_id": "later", "evidence": ["bad"]}],
            ),
        )
    assert caught.value.code == "invalid_command"
    assert any(item["loc"][:2] == ("facts", 1) for item in caught.value.details["issues"])
    assert database_snapshot(app.engine(company)) == before


def test_fact_missing_accounting_information_keeps_original_boundary(service):
    app, company = service
    data = {k: v for k, v in EXPENSE.items() if k != "period"}
    with pytest.raises(NeedsInformation) as caught:
        app.dispatch(
            "save_fact",
            dict(
                company_id=company,
                kind="expense",
                subject_id="missing-period",
                data=data,
                evidence=[UNKNOWN],
                expected_revision=0,
                request_id="missing-period",
            ),
        )
    assert caught.value.response()["status"] == "needs_information"
    assert caught.value.details["fact_issues"][0]["field"] == "period"


def test_valid_unknown_reference_keeps_content_checks_and_no_writes(service):
    app, company = service
    engine = app.engine(company)
    before = database_snapshot(engine)
    selected = {
        "register_entity",
        "update_company_note",
        "prepare_fact_registration",
        "save_fact",
        "inspect_material",
        "preview_close",
        "save_payee",
        "preview_export",
        "prepare_asset_activation_batch",
        "prepare_asset_consumption_month",
    }
    for command, _, payload in public_cases():
        if command not in selected:
            continue
        with pytest.raises(NeedsInformation):
            app.dispatch(command, copy.deepcopy(payload) | {"company_id": company})
        assert database_snapshot(engine) == before
    # Inventory already uses the database foreign-key constraint for unknown references.
    inventory = next(payload for command, _, payload in public_cases() if command == "inventory")
    try:
        app.dispatch("inventory", inventory | {"company_id": company})
    except Exception as exc:
        assert error_response(exc)["code"] == "accounting_constraint"
    else:
        pytest.fail("unknown inventory evidence was accepted")
    assert database_snapshot(engine) == before


def test_direct_python_public_conversion_guard_and_program_bugs(service):
    app, company = service
    engine = app.engine(company)
    before = database_snapshot(engine)
    calls = [
        lambda value: Entities(engine).register_entity(
            "person", {}, source="synthetic", evidence_digest=value, request_id="direct"
        ),
        lambda value: Discovery(engine).update_company_note(
            "synthetic", expected_revision=0, request_id="direct", evidence_digest=value
        ),
        lambda value: engine.save_fact(
            "expense",
            "direct",
            EXPENSE,
            evidence=(value,),
            expected_revision=0,
            request_id="direct",
        ),
        lambda value: IdentityCorrections(engine).preview_identity_correction(
            changes=[{"subject_id": "expense", "expected_revision": 1, "action": "supersede"}],
            evidence=[value],
            reason="synthetic",
        ),
        lambda value: Periods(engine).inventory(
            "2026-01",
            "transactions",
            evidence=[value],
            expected=1,
            no_business=False,
            confirmation_evidence=UNKNOWN,
            request_id="direct",
        ),
        lambda value: Materials(engine).inspect(value, {"format": "csv"}),
        lambda value: AssetBatches(engine).prepare_activation_batch(
            "direct", "2026-01", [], evidence=(value,), expected_revision=0
        ),
    ]
    for call in calls:
        for value in ("a", UNKNOWN + "\n", True, 1.5):
            with pytest.raises(KernelError) as caught:
                call(value)
            assert caught.value.code == "invalid_command"
            assert all("input" not in item for item in caught.value.details["issues"])
            assert database_snapshot(engine) == before
    assert error_response(ValueError("programming defect"))["code"] == "internal_error"


def test_registered_references_keep_management_versions_idempotency_and_publication(service):
    app, company = service
    engine = app.engine(company)
    proof = engine.register_evidence(
        b"source,amount\nsynthetic,1.00\n",
        "text/csv",
        "synthetic.csv",
        request_id="registered-reference",
    )["digest"]
    initial = database_snapshot(engine)
    payload = dict(
        company_id=company,
        kind="organization",
        data={},
        source="synthetic",
        evidence_digest=proof,
        request_id="registered-entity",
    )
    entity = app.dispatch("register_entity", payload)
    after = database_snapshot(engine)
    assert initial != after
    assert app.dispatch("register_entity", payload) == entity
    assert database_snapshot(engine) == after
    note = dict(
        company_id=company,
        text="synthetic note",
        evidence_digest=proof,
        expected_revision=0,
        request_id="registered-note",
    )
    assert app.dispatch("update_company_note", note)["revision"] == 1
    after_note = database_snapshot(engine)
    app.dispatch("update_company_note", note)
    assert database_snapshot(engine) == after_note
    data = EXPENSE | {"counterparty_id": entity["entity_id"]}
    saved = app.dispatch(
        "save_fact",
        dict(
            company_id=company,
            kind="expense",
            subject_id="registered-expense",
            data=data,
            evidence=[proof],
            expected_revision=0,
            request_id="registered-fact",
        ),
    )
    assert saved["revision"] == 1
    preview = app.dispatch("preview", dict(company_id=company, subjects=["registered-expense"]))
    result = app.dispatch(
        "confirm",
        dict(
            company_id=company,
            subjects=["registered-expense"],
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id="registered-publication",
        ),
    )
    assert result["status"] == "published"
    with engine.store.connection(read_only=True) as connection:
        assert engine.store.current_fact(connection, "registered-expense").evidence == (proof,)
    inventory = dict(
        company_id=company,
        period="2026-01",
        category="transactions",
        evidence=[proof],
        expected=1,
        no_business=False,
        confirmation_evidence=proof,
        request_id="registered-inventory",
    )
    assert app.dispatch("inventory", inventory)["status"] == "saved"
    after_inventory = database_snapshot(engine)
    app.dispatch("inventory", inventory)
    assert database_snapshot(engine) == after_inventory


def test_identity_unknown_and_registered_references_preserve_source_history(service):
    app, company = service
    engine = app.engine(company)
    proof = engine.register_evidence(
        b"synthetic identity", "text/plain", "identity", request_id="identity-proof"
    )["digest"]
    entities = Entities(engine)
    first = entities.register_entity(
        "organization", {}, source="synthetic", request_id="identity-first"
    )["entity_id"]
    second = entities.register_entity(
        "organization", {}, source="synthetic", request_id="identity-second"
    )["entity_id"]
    data = EXPENSE | {"counterparty_id": first}
    engine.save_fact(
        "expense",
        "identity-expense",
        data,
        evidence=(proof,),
        expected_revision=0,
        request_id="identity-fact",
    )
    preview = engine.preview(["identity-expense"])
    engine.confirm(
        ["identity-expense"],
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="identity-publication",
    )
    kwargs = dict(
        changes=[
            dict(
                subject_id="identity-expense",
                expected_revision=1,
                action="reassign",
                data=data | {"counterparty_id": second},
            )
        ],
        evidence=[UNKNOWN],
        reason="synthetic identity correction",
    )
    before = database_snapshot(engine)
    with pytest.raises(NeedsInformation):
        app.dispatch("preview_identity_correction", kwargs | {"company_id": company})
    assert database_snapshot(engine) == before
    kwargs["evidence"] = [proof]
    preview = app.dispatch("preview_identity_correction", kwargs | {"company_id": company})
    command = kwargs | dict(
        company_id=company,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="identity-correction",
    )
    result = app.dispatch("confirm_identity_correction", command)
    assert result["status"] == "corrected"
    after = database_snapshot(engine)
    assert app.dispatch("confirm_identity_correction", command) == result
    assert database_snapshot(engine) == after
    with engine.store.connection(read_only=True) as connection:
        assert engine.store.current_fact(connection, "identity-expense").revision == 2
        assert (
            connection.execute(
                "SELECT count(*) FROM fact_revision WHERE subject_id='identity-expense'"
            ).fetchone()[0]
            == 2
        )


def test_asset_references_use_public_batch_validation_and_registered_adoption(tmp_path):
    from test_asset_batches import activation_members, asset_engine

    engine, proof = asset_engine.__wrapped__(tmp_path)
    batches = AssetBatches(engine)
    kwargs = dict(
        subject_id="activation-batch",
        period="2026-01",
        members=activation_members(),
        evidence=(UNKNOWN,),
        expected_revision=0,
    )
    before = database_snapshot(engine)
    with pytest.raises(NeedsInformation):
        batches.prepare_activation_batch(**kwargs)
    assert database_snapshot(engine) == before
    kwargs["evidence"] = (proof,)
    preview = batches.prepare_activation_batch(**kwargs)
    confirm = dict(
        kwargs,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="registered-asset-batch",
    )
    result = batches.confirm_activation_batch(**confirm)
    assert result["status"] == "published"
    after = database_snapshot(engine)
    assert batches.confirm_activation_batch(**confirm) == result
    assert database_snapshot(engine) == after


def test_cli_and_mcp_use_shared_safe_dispatch_errors(service, monkeypatch, capsys, tmp_path):
    app, company = service
    payload = dict(
        company_id=company,
        kind="person",
        data={},
        source="synthetic",
        evidence_digest="sensitive-malformed-reference",
        request_id="transport-rejected",
    )
    before = database_snapshot(app.engine(company))
    from ai_accounting.kernel import cli, mcp

    monkeypatch.setattr(cli, "ServiceClient", lambda root: SimpleNamespace(dispatch=app.dispatch))
    request = tmp_path / "request.json"
    request.write_text(canonical(payload), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "finance-local",
            "--root",
            str(tmp_path),
            "call",
            "register_entity",
            "--input",
            str(request),
        ],
    )
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 1
    cli_response = json.loads(capsys.readouterr().out)
    assert cli_response["code"] == "invalid_command"
    tools = {}

    class FakeMCP:
        def __init__(self, *args, **kwargs):
            pass

        def tool(self):
            def record(fn):
                tools[fn.__name__] = fn
                return fn

            return record

        def run(self, **kwargs):
            assert kwargs == {"transport": "stdio"}

    monkeypatch.setattr(mcp, "FastMCP", FakeMCP)
    monkeypatch.setattr(mcp, "ServiceClient", lambda root: SimpleNamespace(dispatch=app.dispatch))
    mcp.serve(tmp_path)
    response = tools["finance_local_command"]("register_entity", payload)
    assert json.loads(canonical(response)) == cli_response
    assert payload["evidence_digest"] not in canonical(response)
    assert database_snapshot(app.engine(company)) == before


def test_nested_reference_models_and_published_schemas_keep_same_wire_shape(service):
    from ai_accounting.kernel.display import BusinessProfile
    from ai_accounting.kernel.domains.managed_reserve import ManagedReserveInternalMovement
    from ai_accounting.kernel.domains.platforms import PlatformMovement
    from ai_accounting.kernel.domains.taxes import TaxCreditConfirmation
    from ai_accounting.kernel.duplicates import ReviewBasis, SourceLocation
    from ai_accounting.kernel.materials import (
        MaterialGroupResolution,
        MaterialSource,
        PeriodAllocationEntry,
    )

    models = [
        (MaterialSource, "evidence_digest"),
        (MaterialGroupResolution, "basis_evidence_digest"),
        (PeriodAllocationEntry, "basis_evidence_digest"),
        (ReviewBasis, "evidence_digest"),
        (SourceLocation, "evidence_digest"),
        (BusinessProfile, "evidence_digest"),
        (PlatformMovement, "source_evidence_digest"),
        (ManagedReserveInternalMovement, "boundary_evidence_digest"),
        (TaxCreditConfirmation, "confirmation_evidence"),
    ]
    for model, field in models:
        adapter = TypeAdapter(model.model_fields[field].rebuild_annotation())
        for value in ("a", "A" * 64, UNKNOWN + "\n", True, 1.5):
            with pytest.raises(ValidationError):
                adapter.validate_json(canonical(value))
        assert adapter.validate_json(canonical(UNKNOWN)) == UNKNOWN
    app, _ = service
    schemas = app.dispatch("schema", {"view": "full"})["command_schemas"]
    for command, _field, _ in public_cases():
        encoded = canonical(schemas[command])
        assert '"pattern":"^[0-9a-f]{64}$"' in encoded


def test_generated_sql_matches_unchanged_draft_contracts():
    from ai_accounting.kernel.catalog import catalog_sql
    from ai_accounting.kernel.schema import schema_sql
    from ai_accounting.kernel.schema_bundle import production_bundle
    from ai_accounting.kernel.versions import contract_fingerprint

    bundle = production_bundle()
    assert bundle.status == "draft"
    assert dict(bundle.current_versions) == {"catalog": 0, "company": 0}
    for kind, sql in (("company", schema_sql(bundle.registry)), ("catalog", catalog_sql())):
        assert contract_fingerprint(sql).hex() == bundle.current(kind)["sha256"]


def test_nested_public_source_and_review_references_cannot_escape_into_plain_mapping(service):
    app, company = service
    cases = public_cases()
    fact = copy.deepcopy(next(p for cmd, _, p in cases if cmd == "save_fact"))
    source = dict(source_id="source", source_fact_id="f", evidence_digest="bad", location="row:2")
    review = dict(
        candidate_digest=UNKNOWN,
        action="create_separate",
        explanation="synthetic",
        review_basis=[dict(kind="owner_confirmation", evidence_digest="bad")],
    )
    receiving = copy.deepcopy(next(p for cmd, _, p in cases if cmd == "receive_material"))
    receiving["data"]["evidence_digest"] = "bad"
    commands = [
        (
            "save_fact",
            fact | {"source_locations": [source]},
            ("source_locations", 0, "evidence_digest"),
        ),
        (
            "prepare_fact_registration",
            {k: v for k, v in (fact | {"source_locations": [source]}).items() if k != "request_id"},
            ("source_locations", 0, "evidence_digest"),
        ),
        ("save_fact", fact | {"review": review}, ("review", "review_basis", 0, "evidence_digest")),
        ("receive_material", receiving, ("data", "evidence_digest")),
    ]
    before = database_snapshot(app.engine(company))
    for command, payload, field in commands:
        with pytest.raises(KernelError) as caught:
            app.dispatch(command, payload | {"company_id": company})
        assert caught.value.code == "invalid_command"
        assert any(item["loc"][-len(field) :] == field for item in caught.value.details["issues"])
    assert database_snapshot(app.engine(company)) == before
