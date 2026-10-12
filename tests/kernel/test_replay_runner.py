import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.replay_contracts import REPLAY_COMMANDS, ReplayStep
from ai_accounting.kernel.replay_runner import (
    _check_payroll_publication_preparation,
    _field,
    _output_field,
    _output_spec,
    after_replay_write,
    before_replay_write,
    compile_replay_steps,
    replay_write_scope,
    validate_replay_sequence,
)
from ai_accounting.kernel.report_classification_prepare import (
    prepare_typed_report_classifications,
    resolve_unique_voucher_line,
)
from ai_accounting.kernel.types import YearMonth, canonical


def test_dependency_compiler_orders_dynamic_output_references_and_rejects_cycles():
    member = ReplayStep(
        step_id="member",
        command="save_facts",
        payload={"source": {"$replay_ref": "source", "field": "fact_id"}},
    )
    source = ReplayStep(
        step_id="source",
        command="receive_material",
        payload={},
        output_ref="source",
        output_fields=("fact_id",),
    )
    ordered, edges = compile_replay_steps((member, source))
    assert [item.step_id for item in ordered] == ["source", "member"]
    assert edges["member"] == ("source",)

    cycle = (
        ReplayStep(step_id="a", command="save_facts", payload={}, depends_on=("b",)),
        ReplayStep(step_id="b", command="save_facts", payload={}, depends_on=("a",)),
    )
    with pytest.raises(KernelError, match="循环"):
        compile_replay_steps(cycle)


def test_replay_sequence_rejects_material_before_receipt_and_missing_month_review():
    receive = ReplayStep(
        step_id="receive",
        command="receive_material",
        payload={},
        output_ref="received",
        output_fields=("source_fact_id",),
    )
    resolve = ReplayStep(
        step_id="resolve",
        command="resolve_material",
        payload={
            "data": {"source_fact_id": {"$replay_ref": "received", "field": "source_fact_id"}}
        },
    )
    preview = ReplayStep(step_id="review", command="preview_close", payload={"period": "2025-01"})
    close = ReplayStep(
        step_id="close", command="close", payload={"period": "2025-01"}, depends_on=("review",)
    )
    ordered, deps = compile_replay_steps((resolve, receive, preview, close))
    validate_replay_sequence(ordered, deps, YearMonth("2025-01"), YearMonth("2025-01"))

    broken = (
        ReplayStep(
            step_id="resolve",
            command="resolve_material",
            payload={"data": {"treatment": "recognize"}},
        ),
        preview,
        close,
    )
    broken_order, broken_deps = compile_replay_steps(broken)
    with pytest.raises(KernelError, match="接收"):
        validate_replay_sequence(
            broken_order, broken_deps, YearMonth("2025-01"), YearMonth("2025-01")
        )


def test_continuous_month_plan_requires_prior_close_before_next_preview():
    steps = (
        ReplayStep(step_id="p1", command="preview_close", payload={"period": "2025-01"}),
        ReplayStep(
            step_id="c1", command="close", payload={"period": "2025-01"}, depends_on=("p1",)
        ),
        ReplayStep(step_id="p2", command="preview_close", payload={"period": "2025-02"}),
        ReplayStep(
            step_id="c2", command="close", payload={"period": "2025-02"}, depends_on=("p2",)
        ),
    )
    ordered, dependencies = compile_replay_steps(steps)
    with pytest.raises(KernelError, match="前月关账"):
        validate_replay_sequence(ordered, dependencies, YearMonth("2025-01"), YearMonth("2025-02"))


def test_duplicate_material_group_requires_each_member_copy_before_final_group():
    receive = ReplayStep(step_id="receive", command="receive_material", payload={})
    master = ReplayStep(
        step_id="master",
        command="resolve_material_group",
        payload={
            "data": {
                "source_id": "primary",
                "members": [{"location": "original", "amount_fen": 100}],
                "group_amount_fen": 100,
                "links": [
                    {"calculation_id": "calc", "amount_field": "gross_fen", "amount_fen": 100}
                ],
            }
        },
        depends_on=("receive",),
    )
    first = ReplayStep(
        step_id="copy-a",
        command="resolve_material",
        payload={
            "data": {
                "source_id": "source",
                "location": "a",
                "treatment": "duplicate",
                "duplicate_source_id": "primary",
                "duplicate_location": "original",
            }
        },
        depends_on=("master", "receive"),
    )
    second = ReplayStep(
        step_id="copy-b",
        command="resolve_material",
        payload={
            "data": {
                "source_id": "source",
                "location": "b",
                "treatment": "duplicate",
                "duplicate_source_id": "primary",
                "duplicate_location": "original",
            }
        },
        depends_on=("master", "receive"),
    )
    final = ReplayStep(
        step_id="final",
        command="resolve_material_group",
        payload={
            "data": {
                "source_id": "source",
                "members": [
                    {"location": "a", "amount_fen": 40},
                    {"location": "b", "amount_fen": 60},
                ],
                "group_amount_fen": 100,
                "links": [
                    {"calculation_id": "calc", "amount_field": "gross_fen", "amount_fen": 100}
                ],
            }
        },
        depends_on=("copy-a", "copy-b", "receive"),
    )
    preview = ReplayStep(step_id="preview", command="preview_close", payload={"period": "2025-01"})
    close = ReplayStep(
        step_id="close", command="close", payload={"period": "2025-01"}, depends_on=("preview",)
    )
    steps = (receive, master, first, second, final, preview, close)
    ordered, dependencies = compile_replay_steps(steps)
    validate_replay_sequence(ordered, dependencies, YearMonth("2025-01"), YearMonth("2025-01"))

    incomplete = tuple(
        item.model_copy(update={"depends_on": ("copy-a", "receive")})
        if item.step_id == "final"
        else item
        for item in steps
        if item.step_id != "copy-b"
    )
    ordered, dependencies = compile_replay_steps(incomplete)
    with pytest.raises(KernelError, match="每个成员"):
        validate_replay_sequence(ordered, dependencies, YearMonth("2025-01"), YearMonth("2025-01"))

    unordered = tuple(
        item.model_copy(update={"depends_on": ("master", "receive")})
        if item.step_id == "final"
        else item
        for item in steps
    )
    ordered, dependencies = compile_replay_steps(unordered)
    with pytest.raises(KernelError, match="全部成员"):
        validate_replay_sequence(ordered, dependencies, YearMonth("2025-01"), YearMonth("2025-01"))

    missing_primary = tuple(
        item.model_copy(update={"depends_on": ("receive",)}) if item.step_id == "copy-a" else item
        for item in steps
    )
    ordered, dependencies = compile_replay_steps(missing_primary)
    with pytest.raises(KernelError, match="主组"):
        validate_replay_sequence(ordered, dependencies, YearMonth("2025-01"), YearMonth("2025-01"))


def _one_month_close_steps():
    return (
        ReplayStep(step_id="review", command="preview_close", payload={"period": "2025-01"}),
        ReplayStep(
            step_id="close", command="close", payload={"period": "2025-01"}, depends_on=("review",)
        ),
    )


def test_normal_material_group_and_single_row_duplicate_do_not_require_copy_group():
    steps = (
        ReplayStep(step_id="receive", command="receive_material", payload={}),
        ReplayStep(
            step_id="recognize",
            command="resolve_material_group",
            payload={
                "data": {
                    "source_id": "source",
                    "members": [{"location": "a"}, {"location": "b"}],
                    "group_amount_fen": 100,
                }
            },
            depends_on=("receive",),
        ),
        ReplayStep(
            step_id="single-copy",
            command="resolve_material",
            payload={
                "data": {
                    "source_id": "other",
                    "location": "copy",
                    "treatment": "duplicate",
                    "duplicate_source_id": "row-source",
                    "duplicate_location": "row",
                }
            },
            depends_on=("receive",),
        ),
        *_one_month_close_steps(),
    )
    ordered, dependencies = compile_replay_steps(steps)
    validate_replay_sequence(ordered, dependencies, YearMonth("2025-01"), YearMonth("2025-01"))


def test_saved_wage_and_policy_use_direct_contract_and_prepare_before_publication():
    steps = (
        ReplayStep(
            step_id="policy",
            command="save_facts",
            payload={
                "facts": [
                    {
                        "kind": "payroll_contribution_policy",
                        "subject_id": "policy",
                        "data": {"period": "2025-01"},
                    },
                    {
                        "kind": "payroll_plan_v2",
                        "subject_id": "plan",
                        "data": {"period": "2025-01"},
                    },
                ]
            },
        ),
        ReplayStep(
            step_id="wage",
            command="save_facts",
            payload={
                "facts": [
                    {
                        "kind": "payroll_bounded",
                        "subject_id": "wage",
                        "data": {"period": "2025-01"},
                    },
                ]
            },
        ),
        ReplayStep(
            step_id="prepare",
            command="prepare_payroll",
            payload={"period": "2025-01"},
            depends_on=("wage", "policy"),
        ),
        ReplayStep(
            step_id="publish-preview",
            command="preview",
            payload={"subjects": ["wage"]},
            depends_on=("prepare",),
        ),
        ReplayStep(
            step_id="publish",
            command="confirm",
            payload={"subjects": ["wage"]},
            depends_on=("publish-preview",),
        ),
        *_one_month_close_steps(),
    )
    ordered, dependencies = compile_replay_steps(steps)
    validate_replay_sequence(ordered, dependencies, YearMonth("2025-01"), YearMonth("2025-01"))
    for changed in (
        {"step_id": "prepare", "payload": {"period": "2025-02"}},
        {"step_id": "prepare", "depends_on": ("policy",)},
        {"step_id": "publish-preview", "depends_on": ("wage",)},
    ):
        wrong = tuple(
            item.model_copy(
                update={key: value for key, value in changed.items() if key != "step_id"}
            )
            if item.step_id == changed["step_id"]
            else item
            for item in steps
        )
        ordered, dependencies = compile_replay_steps(wrong)
        with pytest.raises(KernelError, match="工资准备核验"):
            validate_replay_sequence(
                ordered, dependencies, YearMonth("2025-01"), YearMonth("2025-01")
            )


@pytest.mark.parametrize(
    "result,path,expected",
    [
        (
            {"results": [{"fact_id": "actual-fact-a"}, {"fact_id": "actual-fact-b"}]},
            "results.1.fact_id",
            "actual-fact-b",
        ),
        (
            {"calculations": ({"id": "actual-calculation"},)},
            "calculations.0.id",
            "actual-calculation",
        ),
        ({"results": [[{"id": "actual-nested"}]]}, "results.0.0.id", "actual-nested"),
        ({"results": {"0": {"id": "actual-dictionary"}}}, "results.0.id", "actual-dictionary"),
    ],
)
def test_output_field_reads_exact_nested_receipts(result, path, expected):
    assert _field(result, path) == expected


@pytest.mark.parametrize(
    "path",
    [
        "results.01.fact_id",
        "results.-1.fact_id",
        "results.1.fact_id",
        "results.x.fact_id",
        "results..fact_id",
        "results.0.missing",
        "results.0.fact_id.0",
        "results.0.",
    ],
)
def test_output_field_rejects_invalid_noncanonical_missing_or_out_of_bounds_paths(path):
    with pytest.raises(KernelError) as error:
        _field({"results": [{"fact_id": "actual-fact"}]}, path)
    assert error.value.code == "replay_output_missing"


def test_subject_selector_uses_exact_receipt_independent_of_array_order_and_storage_encoding():
    selectors = {
        "selected": {
            "array_path": "results",
            "match_field": "subject_id",
            "match_value": "target",
            "field": "calculation_id",
        }
    }
    fields, decoded = _output_spec(
        {"output_fields": canonical({"fields": ["selected"], "selectors": selectors})}
    )
    assert fields == ("selected",)
    response = {
        "results": [
            {"subject_id": "other", "calculation_id": "other-calc"},
            {"subject_id": "target", "calculation_id": "actual-target-calc"},
        ]
    }
    assert _output_field(response, "selected", decoded) == "actual-target-calc"
    response["results"].reverse()
    assert _output_field(response, "selected", decoded) == "actual-target-calc"
    assert _output_spec({"output_fields": '["results.0.subject_id"]'}) == (
        ("results.0.subject_id",),
        {},
    )


@pytest.mark.parametrize(
    "results",
    [
        [],
        [{"subject_id": "other", "calculation_id": "calc"}],
        [
            {"subject_id": "target", "calculation_id": "one"},
            {"subject_id": "target", "calculation_id": "two"},
        ],
        [{"subject_id": "target"}],
        {"subject_id": "target", "calculation_id": "calc"},
    ],
)
def test_subject_selector_rejects_absent_ambiguous_nonarray_or_missing_real_fields(results):
    with pytest.raises(KernelError) as error:
        _output_field(
            {"results": results},
            "selected",
            {
                "selected": {
                    "array_path": "results",
                    "match_field": "subject_id",
                    "match_value": "target",
                    "field": "calculation_id",
                },
            },
        )
    assert error.value.code == "replay_output_missing"


def test_selector_contract_is_strict_and_alias_must_be_declared():
    from pydantic import ValidationError

    step = ReplayStep(
        step_id="publication",
        command="confirm",
        payload={},
        output_ref="published",
        output_fields=("selected",),
        output_selectors={
            "selected": {
                "array_path": "results",
                "match_field": "subject_id",
                "match_value": "target",
                "field": "calculation_id",
            }
        },
    )
    assert step.model_dump(mode="json")["output_selectors"]["selected"]["match_value"] == "target"
    for selectors in (
        {
            "undeclared": {
                "array_path": "results",
                "match_field": "subject_id",
                "match_value": "target",
                "field": "calculation_id",
            }
        },
        {
            "selected": {
                "array_path": "results..",
                "match_field": "subject_id",
                "match_value": "target",
                "field": "calculation_id",
            }
        },
        {
            "selected": {
                "array_path": "results",
                "match_field": "subject_id",
                "match_value": "target",
                "field": "calculation_id",
                "fallback": "guess",
            }
        },
    ):
        with pytest.raises(ValidationError):
            ReplayStep(
                step_id="publication",
                command="confirm",
                payload={},
                output_ref="published",
                output_fields=("selected",),
                output_selectors=selectors,
            )
    assert REPLAY_COMMANDS == set(ReplayStep.model_fields["command"].annotation.__args__)
    assert "obligation_basis" in REPLAY_COMMANDS


def test_selector_reference_adds_dependency_and_preserves_wire_shape():
    selector = {
        "array_path": "results",
        "match_field": "values.asset_id",
        "match_value": {"$replay_ref": "asset", "field": "entity_id"},
        "field": "subject_id",
    }
    selected = ReplayStep(
        step_id="prepare",
        command="prepare_asset_consumption_month",
        payload={},
        output_ref="prepared",
        output_fields=("selected",),
        output_selectors={"selected": selector},
    )
    entity = ReplayStep(
        step_id="asset",
        command="register_entity",
        payload={},
        output_ref="asset",
        output_fields=("entity_id",),
    )
    ordered, dependencies = compile_replay_steps((selected, entity))
    assert [item.step_id for item in ordered] == ["asset", "prepare"]
    assert dependencies["prepare"] == ("asset",)
    assert selected.model_dump(mode="json")["output_selectors"]["selected"] == selector
    assert (
        _output_field(
            {
                "results": [
                    {"subject_id": "actual-inner-subject", "values": {"asset_id": "fresh-asset"}}
                ]
            },
            "selected",
            {"selected": {**selector, "match_value": "fresh-asset"}},
        )
        == "actual-inner-subject"
    )
    with pytest.raises(KernelError, match="未声明"):
        compile_replay_steps((selected,))


def test_safe_plan_preserves_whole_report_and_nested_exact_revision_references():
    from test_payroll import payroll

    from ai_accounting.kernel.command_schema import command_models
    from ai_accounting.kernel.replay_runner import _safe_step_payload
    from ai_accounting.kernel.service import default_registry

    registry = default_registry()
    service = SimpleNamespace(registry=registry, command_models=command_models(registry))
    ref = {"$replay_ref": "prepared", "field": "results.0.revision"}
    wage = payroll().model_dump(mode="json")
    for field in ("profile_revision", "contribution_policy_revision", "income_tax_policy_revision"):
        wage[field]["revision"] = ref
    facts = [
        {
            "kind": "payroll",
            "subject_id": "wage",
            "data": wage,
            "evidence": ["a" * 64],
            "expected_revision": 0,
        },
        {
            "kind": "report_classification",
            "subject_id": "classification",
            "data": {"$replay_ref": "report", "field": "items.0.data"},
            "evidence": ["a" * 64],
            "expected_revision": 0,
        },
    ]
    step = ReplayStep(step_id="facts", command="save_facts", payload={"facts": facts})
    safe = _safe_step_payload(service, "company", "plan", step)
    assert safe["facts"] == facts
    assert safe["facts"][0]["data"]["profile_revision"]["revision"] == ref
    invalid = step.model_copy(
        update={"payload": {"facts": [{**facts[1], "guessed_account": "9999"}]}}
    )
    with pytest.raises(KernelError):
        _safe_step_payload(service, "company", "plan", invalid)


def test_selector_matching_is_strict_for_integer_and_boolean_values():
    selector = {
        "selected": {"array_path": "results", "match_field": "row", "match_value": 1, "field": "id"}
    }
    assert (
        _output_field(
            {"results": [{"row": True, "id": "bool"}, {"row": 1, "id": "actual"}]},
            "selected",
            selector,
        )
        == "actual"
    )


@pytest.mark.parametrize("kind", ["reimbursed_asset_batch", "payroll_reserve_payment"])
def test_safe_plan_keeps_distinct_typed_identity_refs_distinct_and_same_ref_identical(kind):
    from test_reimbursement_assets import accepted_batch

    from ai_accounting.kernel.command_schema import command_models
    from ai_accounting.kernel.replay_runner import _safe_step_payload
    from ai_accounting.kernel.service import default_registry

    registry = default_registry()
    service = SimpleNamespace(registry=registry, command_models=command_models(registry))
    first = {"$replay_ref": "first", "field": "entity_id"}
    second = {"$replay_ref": "second", "field": "entity_id"}
    if kind == "reimbursed_asset_batch":
        data = accepted_batch()
        rows, field = data["assets"], "asset_id"
    else:
        data = {
            "period": "2026-02",
            "actual_date": "2026-02-10",
            "bank_account_id": "bank",
            "amount_fen": 1000,
            "reserve_expense_fen": 100,
            "return_period": "2026-02",
            "return_confirmed": True,
            "complete_group_confirmed": True,
            "allocations": [
                {
                    "source_kind": "payroll",
                    "source_id": "one",
                    "recipient_id": "one",
                    "amount_fen": 500,
                },
                {
                    "source_kind": "payroll",
                    "source_id": "two",
                    "recipient_id": "two",
                    "amount_fen": 400,
                },
            ],
        }
        rows, field = data["allocations"], "source_id"
    rows[0][field], rows[1][field] = first, second
    step = ReplayStep(
        step_id="batch",
        command="save_facts",
        payload={
            "facts": [
                {
                    "kind": kind,
                    "subject_id": "batch",
                    "data": data,
                    "evidence": ["a" * 64],
                    "expected_revision": 0,
                },
            ]
        },
    )
    assert _safe_step_payload(service, "company", "plan", step)["facts"][0]["data"] == data
    rows[1][field] = first
    with pytest.raises(KernelError):
        _safe_step_payload(service, "company", "plan", step)


@pytest.mark.parametrize(
    "result",
    [
        {
            "period": "2025-01",
            "status": "ready",
            "fact_issues": [],
            "existing": [{"subject_id": "wage", "fact_id": "actual-fact"}],
            "candidates": [],
        },
        {
            "period": "2025-01",
            "status": "needs_information",
            "fact_issues": [{"field": "payroll_profile"}],
            "existing": [{"subject_id": "wage", "fact_id": "actual-fact"}],
            "candidates": [],
        },
        {
            "period": "2025-01",
            "status": "ready",
            "fact_issues": [],
            "existing": [{"subject_id": "wage", "fact_id": "old-fact"}],
            "candidates": [],
        },
        {
            "period": "2025-01",
            "status": "ready",
            "fact_issues": [],
            "existing": [{"subject_id": "wage", "fact_id": "actual-fact"}],
            "candidates": [{"subject_id": "other-wage"}],
        },
    ],
)
def test_direct_wage_publication_checks_durable_ready_exact_preparation_receipt(tmp_path, result):
    connection = _connection(tmp_path / "prepared.sqlite")
    connection.execute("ALTER TABLE replay_step ADD COLUMN dependencies TEXT")
    connection.execute("ALTER TABLE replay_step ADD COLUMN payload TEXT")
    for ident, command, payload, parents, response in (
        (
            "saved",
            "save_facts",
            {"facts": [{"kind": "payroll_bounded", "subject_id": "wage"}]},
            [],
            {},
        ),
        ("prepared", "prepare_payroll", {"period": "2025-01"}, ["saved"], result),
        ("preview", "preview", {"subjects": ["wage"]}, ["prepared"], {}),
    ):
        connection.execute(
            "INSERT INTO replay_step(run_id,step_id,command,request_id,status,"
            "dependencies,payload,response) VALUES(?,?,?,?,?,?,?,?)",
            (
                "run-1",
                ident,
                command,
                ident,
                "completed",
                canonical(parents),
                canonical(payload),
                canonical(response),
            ),
        )
    connection.commit()

    @contextmanager
    def read_connection(**kwargs):
        yield connection

    store = SimpleNamespace(
        connection=read_connection,
        current_fact=lambda conn, subject: SimpleNamespace(
            id="actual-fact", fact=SimpleNamespace(period=YearMonth("2025-01"))
        ),
    )
    engine = SimpleNamespace(store=store)
    if (
        result["status"] == "ready"
        and result["existing"][0]["fact_id"] == "actual-fact"
        and not result["candidates"]
    ):
        _check_payroll_publication_preparation(engine, "run-1", "preview", ["wage"])
    else:
        with pytest.raises(KernelError) as error:
            _check_payroll_publication_preparation(engine, "run-1", "preview", ["wage"])
        assert error.value.code == "replay_payroll_preparation_required"
    connection.rollback()
    connection.close()


def test_report_classification_requires_one_line_from_all_three_typed_relations():
    line = {"version_id": "voucher-v1", "line_no": 3}
    assert resolve_unique_voucher_line(
        source=[line], obligation=[line], payment_allocation=[line]
    ) == {"status": "prepared", "line": line, "issues": []}
    conflict = resolve_unique_voucher_line(
        source=[line],
        obligation=[line],
        payment_allocation=[{"version_id": "voucher-v1", "line_no": 4}],
    )
    assert conflict["status"] == "needs_information"
    assert conflict["line"] is None


def test_replay_classification_command_uses_typed_union_and_blocks_unresolved_data(monkeypatch):
    from ai_accounting.kernel import replay_report_classification, report_classification_prepare
    from ai_accounting.kernel.command_schema import command_models, validate_command
    from ai_accounting.kernel.replay_runner import ReplayCommands
    from ai_accounting.kernel.service import default_registry

    registry = default_registry()
    models = command_models(registry)
    old = {
        "source_id": "old",
        "source_subject_id": "source",
        "source_calculation_id": "calc",
        "obligation_key": "obligation",
        "payment_revision_id": "payment",
        "payment_allocation_scope": "allocation",
    }
    new = {
        "source_id": "new",
        "source_subject_id": "source",
        "source_calculation_id": "calc",
        "period": "2025-01",
        "profit_details": [{"detail_code": "management_other", "amount_fen": 100}],
    }
    body = validate_command(
        models,
        "replay_classification_prepare",
        {"company_id": "company", "items": [old, new]},
        registry=registry,
    )
    monkeypatch.setattr(
        report_classification_prepare,
        "prepare_typed_report_classifications",
        lambda engine, items: [
            {
                "source_id": "old",
                "status": "prepared",
                "line": {"version_id": "formal", "line_no": 2},
                "issues": [],
            }
        ],
    )
    result = {
        "source_id": "new",
        "status": "ready",
        "data": {"voucher_version_id": "actual"},
        "fact_issues": [],
    }
    monkeypatch.setattr(
        replay_report_classification,
        "prepare_source_report_classifications",
        lambda engine, items: [result],
    )
    commands = ReplayCommands(
        None, SimpleNamespace(store=SimpleNamespace(company_id="company")), None
    )
    response = commands.replay_classification_prepare(**body)
    assert response["items"][0]["line"] == {"version_id": "formal", "line_no": 2}
    assert response["items"][1]["data"] == {"voucher_version_id": "actual"}
    from ai_accounting.kernel.response_contracts import validate_response

    result["data"] = {
        "period": "2025-01",
        "voucher_version_id": "actual",
        "profit_details": [{"line_no": 2, "detail_code": "management_other", "amount_fen": 100}],
        "counterparties": [],
        "cash_details": [],
    }
    response = commands.replay_classification_prepare(**body)
    assert validate_response("replay_classification_prepare", response) == response
    assert "data" not in response["items"][0] and "fact_issues" not in response["items"][0]
    with pytest.raises(KernelError):
        validate_response(
            "replay_classification_prepare",
            {"status": "prepared", "items": [{**response["items"][1], "data": None}]},
        )
    with pytest.raises(KernelError) as invalid_response:
        validate_response(
            "replay_classification_prepare",
            {
                "status": "prepared",
                "items": [
                    {**response["items"][1], "data": {**result["data"], "account": "guessed"}}
                ],
            },
        )
    assert invalid_response.value.code == "response_contract_mismatch"
    result.update(
        status="needs_information", data=None, fact_issues=[{"field": "source_calculation_id"}]
    )
    with pytest.raises(KernelError) as unresolved:
        commands.replay_classification_prepare(**body)
    assert unresolved.value.code == "needs_information"
    for forbidden in ("voucher_version_id", "line_no", "account", "direction"):
        with pytest.raises(KernelError):
            validate_command(
                models,
                "replay_classification_prepare",
                {"company_id": "company", "items": [{**new, forbidden: "guessed"}]},
                registry=registry,
            )


def test_classification_resolver_derives_voucher_line_from_server_reads(monkeypatch):
    from ai_accounting.kernel import publication, query_reads

    item = {
        "source_id": "source-1",
        "source_subject_id": "source-subject",
        "source_calculation_id": "source-calc",
        "obligation_key": "obligation-1",
        "payment_revision_id": "payment-revision",
        "payment_allocation_scope": "payment:payment:payment-subject:obligation-1",
    }
    allocation = SimpleNamespace(
        source_id="source-subject",
        obligation="obligation-1",
        scope=item["payment_allocation_scope"],
    )

    class Result:
        def __init__(self, value):
            self.value = value

        def fetchone(self):
            return self.value

        def __iter__(self):
            return iter([self.value] if self.value is not None else [])

    class Connection:
        def execute(self, sql, params=()):
            if "SELECT c.id FROM calculation_current" in sql:
                return Result(("payment-calc",))
            if "FROM settlement_change" in sql:
                return Result((1,))
            if "SELECT h.version_id FROM json_each" in sql:
                return Result(("voucher-version",))
            raise AssertionError(sql)

    class Reads:
        connection = Connection()

        def raw_calculations(self, ids):
            if "source-calc" in ids:
                return {
                    "source-calc": {
                        "subject_id": "source-subject",
                        "fact_id": "source-fact",
                        "outcome": {
                            "values": {
                                "obligations": [
                                    {"key": "obligation-1", "account": "accounts_payable"}
                                ]
                            }
                        },
                    }
                }
            return {
                "payment-calc": {
                    "subject_id": "payment-subject",
                    "fact_id": "payment-revision",
                    "outcome": {
                        "values": {
                            "settlements": [
                                {"source_calculation": "source-calc", "obligation": "obligation-1"}
                            ]
                        }
                    },
                }
            }

        def fact_version(self, revision_id):
            assert revision_id == "payment-revision"
            return SimpleNamespace(
                subject_id="payment-subject",
                fact=SimpleNamespace(actual_payment=True, allocations=(allocation,)),
            )

        def verify_publication_records(self, rows):
            return None

        def vouchers(self, version_ids):
            return {"voucher-version": {"calculation_id": "source-calc"}}

        def voucher_lines(self, version_ids):
            return {"voucher-version": [{"line_no": 2, "account": "accounts_payable"}]}

    class Snapshot:
        def __enter__(self):
            return Reads()

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(query_reads.QueryReads, "snapshot", staticmethod(lambda engine: Snapshot()))
    monkeypatch.setattr(publication, "verify_publication_chain", lambda connection, subjects: None)
    monkeypatch.setattr(
        publication,
        "active_tranches",
        lambda connection, subject_ids: [
            {"calculation_id": "source-calc", "voucher_id": "voucher-1"}
        ],
    )
    monkeypatch.setattr(query_reads, "verify_current_publication_voucher_heads", lambda *args: None)

    assert prepare_typed_report_classifications(object(), [item]) == [
        {
            "source_id": "source-1",
            "status": "prepared",
            "line": {"version_id": "voucher-version", "line_no": 2},
            "issues": [],
        }
    ]
    with pytest.raises(ValueError, match="typed source"):
        prepare_typed_report_classifications(object(), [{**item, "source_lines": [("fake", 1)]}])


def _connection(path):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript("""
      CREATE TABLE state(id INTEGER PRIMARY KEY, accounting INTEGER, material INTEGER,
        management INTEGER, read_repair_revision INTEGER);
      INSERT INTO state VALUES(1,0,0,0,0);
      CREATE TABLE period_close(period INTEGER PRIMARY KEY, logical_digest BLOB);
      CREATE TABLE replay_run(id TEXT PRIMARY KEY,status TEXT,checkpoint TEXT,current_step TEXT);
      CREATE TABLE replay_step(run_id TEXT,step_id TEXT,command TEXT,request_id TEXT UNIQUE,
        request_digest BLOB,status TEXT,response TEXT,error_code TEXT,last_error TEXT,
        PRIMARY KEY(run_id,step_id));
      CREATE TABLE replay_binding(run_id TEXT,reference TEXT,field TEXT,value TEXT,source_step TEXT,
        PRIMARY KEY(run_id,reference,field));
      CREATE TABLE business_write(value TEXT);
    """)
    return connection


class _Store:
    company_id = "company-1"
    database_id = "database-1"

    def __init__(self, path):
        self.path = Path(path)

    @staticmethod
    def epochs(connection):
        row = connection.execute(
            "SELECT accounting,material,management FROM state WHERE id=1"
        ).fetchone()
        return dict(zip(("accounting", "material", "management"), row, strict=True))


@pytest.mark.parametrize(
    "result,field,expected",
    [
        ({"fact_id": "fact-1"}, "fact_id", '"fact-1"'),
        (
            {"results": [{"fact_id": "actual-fact"}, {"calculation_id": "actual-calc"}]},
            "results.1.calculation_id",
            '"actual-calc"',
        ),
    ],
)
def test_engine_write_hook_commits_business_result_and_binding_together(
    tmp_path, result, field, expected
):
    path = tmp_path / "company.sqlite"
    engine = SimpleNamespace(store=_Store(path))
    request_id = "stable-step-key"
    request_hash = b"r" * 32
    step = {
        "step_id": "register-source",
        "output_ref": "source",
        "output_fields": (field,),
    }
    connection = _connection(path)
    connection.execute(
        "INSERT INTO replay_run(id,status,checkpoint) VALUES(?,?,?)",
        (
            "run-1",
            "pending",
            canonical(
                {
                    "epochs": {"accounting": 0, "material": 0, "management": 0},
                    "read_repair_revision": 0,
                    "prefix": None,
                }
            ),
        ),
    )
    connection.execute(
        "INSERT INTO replay_step(run_id,step_id,command,request_id,status) VALUES(?,?,?,?,?)",
        ("run-1", step["step_id"], "evidence", request_id, "pending"),
    )
    connection.commit()

    with replay_write_scope(
        engine,
        run_id="run-1",
        step=step,
        request_id=request_id,
        request_digest=None,
    ):
        before_replay_write(
            engine, connection, request_id=request_id, action="evidence", request_hash=request_hash
        )
        connection.execute("INSERT INTO business_write VALUES('typed-domain-result')")
        after_replay_write(
            engine,
            connection,
            request_id=request_id,
            action="evidence",
            request_hash=request_hash,
            result=result,
        )
    connection.commit()

    receipt = connection.execute(
        "SELECT status,response FROM replay_step WHERE run_id='run-1' AND step_id='register-source'"
    ).fetchone()
    binding = connection.execute("SELECT value FROM replay_binding").fetchone()[0]
    assert receipt["status"] == "completed"
    assert receipt["response"] == canonical(result)
    assert binding == expected
    assert (
        connection.execute("SELECT value FROM business_write").fetchone()[0]
        == "typed-domain-result"
    )
    connection.close()


@pytest.mark.parametrize(
    "missing_field",
    [
        None,
        "results.1.fact_id",
        "results.01.fact_id",
        "results.0.missing",
        "selected-absent",
        "selected-ambiguous",
    ],
)
def test_engine_write_hook_rollback_discards_domain_and_replay_receipts(tmp_path, missing_field):
    path = tmp_path / "company.sqlite"
    engine = SimpleNamespace(store=_Store(path))
    connection = _connection(path)
    checkpoint = {
        "epochs": {"accounting": 0, "material": 0, "management": 0},
        "read_repair_revision": 0,
        "prefix": None,
    }
    connection.execute(
        "INSERT INTO replay_run VALUES('run-1','pending',?,NULL)", (canonical(checkpoint),)
    )
    connection.execute(
        "INSERT INTO replay_step(run_id,step_id,command,request_id,status) "
        "VALUES('run-1','step-1','evidence','key-1','pending')"
    )
    connection.commit()
    step = {"step_id": "step-1"}
    result = {"results": [{"subject_id": "target", "fact_id": "actual-fact"}]}
    if missing_field:
        step.update(output_ref="saved", output_fields=("results.0.fact_id", missing_field))
        if missing_field.startswith("selected-"):
            step["output_selectors"] = {
                missing_field: {
                    "array_path": "results",
                    "match_field": "subject_id",
                    "match_value": "absent" if missing_field == "selected-absent" else "target",
                    "field": "fact_id",
                }
            }
            if missing_field == "selected-ambiguous":
                result["results"].append({"subject_id": "target", "fact_id": "second-fact"})
    with pytest.raises((RuntimeError, KernelError)):
        try:
            with replay_write_scope(
                engine, run_id="run-1", step=step, request_id="key-1", request_digest=None
            ):
                connection.execute("BEGIN IMMEDIATE")
                before_replay_write(
                    engine,
                    connection,
                    request_id="key-1",
                    action="evidence",
                    request_hash=b"x" * 32,
                )
                connection.execute("INSERT INTO business_write VALUES('uncommitted')")
                after_replay_write(
                    engine,
                    connection,
                    request_id="key-1",
                    action="evidence",
                    request_hash=b"x" * 32,
                    result=result,
                )
                raise RuntimeError("commit fault")
        except (RuntimeError, KernelError):
            connection.rollback()
            raise
    assert connection.execute("SELECT count(*) FROM business_write").fetchone()[0] == 0
    assert connection.execute("SELECT status FROM replay_step").fetchone()[0] == "pending"
    assert connection.execute("SELECT count(*) FROM replay_binding").fetchone()[0] == 0
    connection.close()
