"""Registration and nested diagnostics retain declared question metadata."""

from typing import Annotated, ClassVar, Literal

import pytest
from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from schema_fixture import test_bundle
from test_tax_import import company, complete_details

from ai_accounting.kernel import contracts, tax_import
from ai_accounting.kernel import payroll_preparation as preparation
from ai_accounting.kernel.command_schema import command_models, validate_command
from ai_accounting.kernel.contracts import Fact, NeedsInformation, Registry, fact_missing_issue
from ai_accounting.kernel.duplicates import Duplicates
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.response_contracts import ReadinessIssue, TaxImportMappingIssue
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.types import ActualDate, YearMonth


class QuestionRow(BaseModel):
    accepted_period: YearMonth = Field(json_schema_extra={"x-accounting-fact": {
        "role": "accounting", "allowed_precision": ["month", "day"],
        "reusable_sources": ["confirmed_schedule", "acceptance_document"],
        "constraint": "必须提供实际验收所属期",
    }})
    contact: str = Field(json_schema_extra={"x-accounting-fact": {
        "role": "management", "reusable_sources": [], "allowed_precision": [],
    }})


class OtherQuestionRow(BaseModel):
    other: str


class QuestionFact(Fact):
    kind: ClassVar[str] = "question_metadata_fact"
    rows: list[QuestionRow]
    actual_date: ActualDate


@pytest.fixture
def question_engine(tmp_path):
    registry = Registry()
    registry.register(QuestionFact)
    return Engine(Store.create(
        tmp_path / "questions.sqlite", test_bundle(registry),
        "questions", "91310000123456789A", "question-database",
    ))


def missing_response(call):
    with pytest.raises(NeedsInformation) as failure:
        call()
    return failure.value.response()


@pytest.fixture
def missing_schema_calls(monkeypatch):
    calls = []

    class CountingAdapter(TypeAdapter):
        def json_schema(self, *args, **kwargs):
            calls.append(1)
            return super().json_schema(*args, **kwargs)

    monkeypatch.setattr(contracts, "TypeAdapter", CountingAdapter)
    return calls


@pytest.mark.parametrize("entrypoint", ["prepare", "save", "command", "batch"])
def test_missing_metadata_work_depends_on_leaf_fields_and_is_request_local(
    question_engine, missing_schema_calls, entrypoint,
):
    engine = question_engine
    models = command_models(engine.store.registry)
    payload = {
        "kind": QuestionFact.kind, "subject_id": "question", "data": {"rows": [{}] * 100},
        "evidence": ["synthetic-evidence"], "expected_revision": 0,
    }

    def call():
        if entrypoint == "prepare":
            return Duplicates(engine).prepare_fact_registration(**payload)
        if entrypoint == "save":
            return engine.save_fact(**payload, request_id="missing-work")
        command = "save_facts" if entrypoint == "batch" else "save_fact"
        arguments = (
            {"facts": [payload, {**payload, "subject_id": "other"}]}
            if entrypoint == "batch" else payload
        )
        return validate_command(
            models, command,
            {**arguments, "company_id": "questions", "request_id": "missing-work"},
            registry=engine.store.registry,
        )

    for attempt in range(2):
        response = missing_response(call)
        assert len(missing_schema_calls) == 4 * (attempt + 1)
        issues = {item["field"]: item for item in response["fact_issues"]}
        prefixes = [("facts.0.", "question"), ("facts.1.", "other")] if entrypoint == "batch" else [
            ("", "question"),
        ]
        assert len(issues) == 202 * len(prefixes)
        for prefix, subject in prefixes:
            assert issues[prefix + "actual_date"]["reusable_sources"] == [subject]
            for row in range(100):
                accepted = issues[f"{prefix}rows.{row}.accepted_period"]
                assert accepted["constraint"] == "必须提供实际验收所属期"
                assert accepted["reusable_sources"] == ["confirmed_schedule", "acceptance_document"]
                assert issues[f"{prefix}rows.{row}.contact"]["semantics"] == "management"


def test_missing_metadata_cache_keeps_union_branches_and_new_groups_distinct(missing_schema_calls):
    class AcceptedRow(BaseModel):
        tag: Literal["accepted"]
        date: ActualDate = Field(json_schema_extra={"x-accounting-fact": {
            "role": "accounting", "constraint": "实际验收日期",
        }})

    class ContactRow(BaseModel):
        tag: Literal["contact"]
        date: ActualDate = Field(json_schema_extra={"x-accounting-fact": {
            "role": "management", "constraint": "联系日期",
        }})

    class BranchFact(Fact):
        kind: ClassVar[str] = "branch_question"
        rows: list[Annotated[AcceptedRow | ContactRow, Field(discriminator="tag")]]

    cache = {}
    accepted = fact_missing_issue(
        BranchFact, ("rows", 0, "accepted", "date"), _metadata_cache=cache,
    )
    contact = fact_missing_issue(
        BranchFact, ("rows", 1, "contact", "date"), _metadata_cache=cache,
    )
    assert len(missing_schema_calls) == 2
    assert accepted["constraint"] == "实际验收日期"
    assert accepted["semantics"] == "accounting"
    assert contact["constraint"] == "联系日期"
    assert contact["semantics"] == "management"
    assert accepted["allowed_precision"] == contact["allowed_precision"] == ["day"]
    accepted_metadata = AcceptedRow.model_fields["date"].json_schema_extra["x-accounting-fact"]
    accepted_metadata["constraint"] = "新验收依据"
    current = fact_missing_issue(
        BranchFact, ("rows", 0, "accepted", "date"), _metadata_cache={},
    )
    assert len(missing_schema_calls) == 3
    assert current["constraint"] == "新验收依据"
    assert accepted["constraint"] == "实际验收日期"


def test_issue_projection_keeps_all_fields_and_does_not_modify_original_diagnostics():
    original = [
        {"field": "confirmation", "message": "需要确认", "constraint": "引用原件",
         "allowed_precision": ["month"], "reusable_sources": ["confirmed_schedule"]},
        {"field": "source", "message": "需要来源", "category": "business_fact",
         "resolution": {"command": "find_facts"}, "blocking_scope": "specific_source"},
    ]
    failure = NeedsInformation(
        original, resolution={"command": "save_fact"}, blocking_scope="payroll_confirmation",
    )
    projected = failure.issues_with_context()
    assert projected == [
        {"resolution": {"command": "save_fact"}, "blocking_scope": "payroll_confirmation",
         **original[0]},
        original[1],
    ]
    assert failure.issues == original
    assert all(result is not issue for result, issue in zip(projected, failure.issues, strict=True))
    projected[0]["blocking_scope"] = "outer_scope"
    assert "blocking_scope" not in failure.issues[0]
    assert failure.details["blocking_scope"] == "payroll_confirmation"


def test_all_registration_paths_keep_all_leaf_metadata_and_are_atomic(question_engine):
    engine = question_engine
    payload = {
        "kind": QuestionFact.kind, "subject_id": "question", "data": {"rows": [{}]},
        "evidence": ["synthetic-evidence"], "expected_revision": 0,
    }
    responses = [
        missing_response(lambda: Duplicates(engine).prepare_fact_registration(**payload)),
        missing_response(lambda: engine.save_fact(**payload, request_id="missing-question")),
        missing_response(lambda: validate_command(
            command_models(engine.store.registry), "save_fact",
            {**payload, "company_id": "questions", "request_id": "missing-question"},
            registry=engine.store.registry,
        )),
    ]
    assert responses[0] == responses[1] == responses[2]
    response = responses[0]
    assert response["resolution"] == {"fact_kind": QuestionFact.kind}
    issues = {item["field"]: item for item in response["fact_issues"]}
    assert set(issues) == {"period", "actual_date", "rows.0.accepted_period", "rows.0.contact"}
    assert issues["period"]["allowed_precision"] == ["month"]
    assert issues["actual_date"]["allowed_precision"] == ["day"]
    assert issues["rows.0.accepted_period"]["reusable_sources"] == [
        "confirmed_schedule", "acceptance_document",
    ]
    assert issues["rows.0.accepted_period"]["allowed_precision"] == ["month", "day"]
    assert issues["rows.0.accepted_period"]["constraint"] == "必须提供实际验收所属期"
    assert issues["rows.0.contact"]["semantics"] == "management"
    assert issues["rows.0.contact"]["reusable_sources"] == []
    assert engine.request_result("missing-question")["status"] == "unknown"
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM fact_revision").fetchone()[0] == 0


def test_batch_registration_keeps_leaf_metadata_and_index_prefix(question_engine):
    engine = question_engine
    response = missing_response(lambda: validate_command(
        command_models(engine.store.registry), "save_facts",
        {"company_id": "questions", "request_id": "missing-batch", "facts": [{
            "kind": QuestionFact.kind, "subject_id": "question", "data": {"rows": [{}]},
            "evidence": ["synthetic-evidence"], "expected_revision": 0,
        }]}, registry=engine.store.registry,
    ))
    accepted = next(item for item in response["fact_issues"]
                    if item["field"] == "facts.0.rows.0.accepted_period")
    assert accepted["constraint"] == "必须提供实际验收所属期"
    assert accepted["allowed_precision"] == ["month", "day"]


def test_nested_union_and_management_date_keep_the_declared_semantics():
    class UnionFact(Fact):
        kind: ClassVar[str] = "union_question"
        row: QuestionRow | OtherQuestionRow

    issue = fact_missing_issue(UnionFact, ("row", "QuestionRow", "accepted_period"))
    assert issue["constraint"] == "必须提供实际验收所属期"
    assert issue["allowed_precision"] == ["month", "day"]

    class ManagementFact(Fact):
        kind: ClassVar[str] = "management_question"
        lane: ClassVar[Literal["management"]] = "management"
        actual_date: ActualDate

    issue = fact_missing_issue(ManagementFact, ("actual_date",))
    assert issue["semantics"] == "management"
    assert issue["allowed_precision"] == ["day"]


def test_tagged_union_and_current_required_nullable_field_keep_leaf_metadata():
    class AcceptedRow(QuestionRow):
        tag: Literal["accepted"]

    class AlternateRow(OtherQuestionRow):
        tag: Literal["alternate"]

    class TaggedFact(Fact):
        kind: ClassVar[str] = "tagged_question"
        row: AcceptedRow | AlternateRow = Field(discriminator="tag")
        confirmed_date: ActualDate | None = Field(
            default=None, json_schema_extra={"x-accounting-fact": {
                "role": "accounting", "required_for_registration": True,
                "constraint": "需要确认真实日期", "reusable_sources": ["original_receipt"],
            }},
        )

    issue = fact_missing_issue(TaggedFact, ("row", "accepted", "accepted_period"))
    assert issue["constraint"] == "必须提供实际验收所属期"
    assert issue["allowed_precision"] == ["month", "day"]
    assert fact_missing_issue(TaggedFact, ("confirmed_date",))["allowed_precision"] == ["day"]
    fact = TaggedFact(
        period="2026-01", row=AlternateRow(tag="alternate", other="有来源"),
    )
    response = missing_response(fact.validate_registration)
    assert response["fact_issues"][0] == {
        "field": "confirmed_date", "message": "需要确认真实日期", "semantics": "accounting",
        "allowed_precision": ["day"], "reusable_sources": ["original_receipt"],
        "constraint": "需要确认真实日期",
    }


def test_tax_import_preserves_original_issue_target_scope_and_classification(tmp_path, monkeypatch):
    instance = company(tmp_path, publish=False)
    complete_details(instance)
    original = {
        "field": "acceptance_period", "message": "需要验收月份",
        "semantics": "accounting", "category": "business_fact",
        "allowed_precision": ["month"], "reusable_sources": ["confirmed_schedule"],
        "constraint": "所属期必须与验收资料一致", "location": "工资资料第2行",
    }

    def pending(*args, **kwargs):
        raise NeedsInformation(
            [original], resolution={"command": "find_facts"}, blocking_scope="labor_source",
        )

    monkeypatch.setattr(tax_import, "prepare_payroll_contributions", pending)
    with instance.engine.store.connection(read_only=True) as connection:
        assessment = tax_import.assess_tax_import_mapping(
            instance.engine.store, connection, YearMonth("2026-01"),
        )
    issue = assessment["issues"][0]
    assert assessment["status"] == "pending_confirmation"
    assert issue["category"] == "publication"
    assert issue["blocking_scope"] == "tax_import_file"
    assert issue["cause"] == {
        **original, "resolution": {"command": "find_facts"}, "blocking_scope": "labor_source",
    }
    assert TypeAdapter(TaxImportMappingIssue).validate_python(issue) == issue


def test_payroll_preparation_preserves_shared_and_issue_specific_targets(tmp_path, monkeypatch):
    from test_payroll_preparation import company as payroll_company
    from test_payroll_preparation import confirm_no_change

    instance = payroll_company(tmp_path)
    service = confirm_no_change(instance)
    original = [
        {
            "field": "confirmation", "message": "需要负责人确认", "semantics": "accounting",
            "allowed_precision": ["month"], "reusable_sources": ["confirmed_payroll_plan"],
        },
        {
            "field": "source", "message": "需要精确来源", "category": "business_fact",
            "resolution": {"command": "find_facts"}, "blocking_scope": "specific_source",
        },
    ]

    def pending(*args, **kwargs):
        raise NeedsInformation(
            original, resolution={"command": "save_fact"}, blocking_scope="payroll_confirmation",
        )

    monkeypatch.setattr(preparation, "resolve_payroll_confirmation", pending)
    response = service.prepare("2026-02")
    assert response["status"] == "needs_information"
    assert response["fact_issues"] == [
        {"resolution": {"command": "save_fact"}, "blocking_scope": "payroll_confirmation",
         **original[0], "employee_id": "employee"},
        {**original[1], "employee_id": "employee"},
    ]
    for issue in response["fact_issues"]:
        assert TypeAdapter(ReadinessIssue).validate_python(issue) == issue


@pytest.mark.parametrize("target", [
    {"command": "find_facts", "fact_kind": "payroll"},
    {"command": "unknown"}, {"candidates": []}, {"candidates": ["same", "same"]},
])
def test_issue_resolution_keeps_the_single_typed_target_contract(target):
    issue = {"field": "source", "message": "需要来源", "resolution": target}
    with pytest.raises(ValidationError):
        TypeAdapter(ReadinessIssue).validate_python(issue)
    with pytest.raises(ValueError):
        NeedsInformation([issue])


def test_needs_information_keeps_explicit_scope_without_changing_target():
    response = NeedsInformation(
        "source", resolution={"command": "find_facts"}, blocking_scope="tax_import_file",
    ).response()
    assert response["blocking_scope"] == "tax_import_file"
    assert response["resolution"] == {"command": "find_facts"}
