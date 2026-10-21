from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import get_type_hints

import pytest

from ai_accounting_modular.contracts.public import (
    MAX_FEN,
    MIN_FEN,
    ActualDate,
    ChangeDescription,
    ChangePlan,
    ContractError,
    PostingPlan,
    ReadContext,
    UnitOfWork,
    YearMonth,
    canonical_json,
    checked_fen,
    content_digest,
    format_fen,
    loads_json,
    parse_fen,
    parse_percent,
    parse_rate,
    schema_document,
    sum_fen,
    validate_contract,
)

ROOT = Path(__file__).resolve().parents[3]
EXAMPLES = ROOT / "docs/redesign/examples"


def example(name):
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("value", [MIN_FEN, -1, 0, 1, MAX_FEN])
def test_money_round_trip_is_exact(value):
    text = format_fen(value)
    assert parse_fen(text) == value
    assert validate_contract("core.schema.json", "Fen", text) == text


@pytest.mark.parametrize(
    "value",
    [True, False, 1.0, Decimal("1"), "1", None, MAX_FEN + 1, MIN_FEN - 1],
)
def test_internal_money_refuses_coercion_and_overflow(value):
    with pytest.raises(ValueError):
        checked_fen(value)
    with pytest.raises(ValueError):
        format_fen(value)


@pytest.mark.parametrize(
    "value",
    [0, True, 1.0, "01", "-0", "+1", "1.00", " 1", "1\n", "1e3", "１", str(MAX_FEN + 1)],
)
def test_wire_money_refuses_noncanonical_or_overflow_values(value):
    with pytest.raises(ValueError):
        parse_fen(value)
    with pytest.raises(ContractError, match="Fen"):
        validate_contract("core.schema.json", "Fen", value)


def test_positive_money_and_checked_totals():
    validate_contract("core.schema.json", "PositiveFen", "1")
    for definition, value in [("PositiveFen", "0"), ("NonNegativeFen", "-1")]:
        with pytest.raises(ContractError):
            validate_contract("core.schema.json", definition, value)
    assert sum_fen([MAX_FEN, -MAX_FEN, 17]) == 17
    with pytest.raises(ValueError):
        sum_fen([MAX_FEN, 1])
    with pytest.raises(ValueError):
        sum_fen([1, True])


@pytest.mark.parametrize("month", ["0001-01", "2026-10", "9999-12"])
def test_month_round_trip_without_inventing_days(month):
    value = YearMonth(month)
    assert YearMonth.from_ordinal(value.ordinal) == month
    validate_contract("core.schema.json", "Month", month)
    with pytest.raises(ValueError):
        ActualDate(month)


@pytest.mark.parametrize("month", ["0000-01", "2026-00", "2026-13", "2026-1", "2026-10\n", 202610])
def test_invalid_months_are_rejected_by_helpers_and_wire(month):
    with pytest.raises(ValueError):
        YearMonth(month)
    with pytest.raises(ContractError):
        validate_contract("core.schema.json", "Month", month)


@pytest.mark.parametrize(
    "value", ["2026-02-30", "1900-02-29", "2026-01", "2026-10-20\n", "0000-01-01"]
)
def test_invalid_actual_dates(value):
    with pytest.raises(ValueError):
        ActualDate(value)
    with pytest.raises(ContractError):
        validate_contract("core.schema.json", "Date", value)


def test_valid_leap_day_and_decimal_rates():
    assert ActualDate("2000-02-29").period == "2000-02"
    assert parse_rate("0.123456789012345678") == Decimal("0.123456789012345678")
    assert parse_percent("99.999999999999999999") == Decimal("99.999999999999999999")
    for value in ["0", "0.50", "1.000000000000000000"]:
        validate_contract("core.schema.json", "Rate", value)
    for value in ["NaN", "Infinity", "1e-2", "0.1\n", ".5", "1.1", "0.1234567890123456789", 0.5]:
        with pytest.raises(ValueError):
            parse_rate(value)
        with pytest.raises(ContractError):
            validate_contract("core.schema.json", "Rate", value)
    for value in ["100.1", "-1", "01", "50\n", 50]:
        with pytest.raises(ValueError):
            parse_percent(value)


@pytest.mark.parametrize(
    "raw",
    [
        '{"a":1,"a":2}',
        '{"a":{"x":1,"x":2}}',
        '{"a":NaN}',
        '{"a":Infinity}',
        '{"a":1e9999}',
        '"\\ud800"',
        b'"\xff"',
    ],
)
def test_json_rejects_duplicate_keys_nonfinite_numbers_and_invalid_unicode(raw):
    with pytest.raises(ContractError) as failure:
        loads_json(raw)
    assert failure.value.code == "invalid_input"


def test_canonical_json_retains_business_array_order_and_string_money():
    left = {"金额": "100", "a": ["2", "1"]}
    assert canonical_json(left) == '{"a":["2","1"],"金额":"100"}'
    assert content_digest(left) == hashlib.sha256(canonical_json(left).encode("utf-8")).hexdigest()
    assert content_digest(left) == content_digest({"a": ["2", "1"], "金额": "100"})
    assert content_digest(left) != content_digest({"a": ["1", "2"], "金额": "100"})
    for invalid in [{"amount": 1.0}, {1: "one"}, {"a": (1, 2)}]:
        with pytest.raises(ValueError):
            canonical_json(invalid)


def test_integer_fields_and_numeric_constants_do_not_accept_float_or_bool():
    request = example("core-acceptance.json")["requests"]
    original = next(value for value in request.values() if value["mutations"])
    for wrong in [False, 0.0]:
        value = deepcopy(original)
        value["mutations"][0]["expected_seq"] = wrong
        with pytest.raises(ContractError):
            validate_contract("core.schema.json", "ApplyRequest", value)
    source_query = example("review-materials-cases.json")["examples"]["response"]
    for wrong in [True, 1.0]:
        value = deepcopy(source_query)
        value["schema_version"] = wrong
        with pytest.raises(ContractError):
            validate_contract("review-materials.schema.json", "ReviewMaterialsResponse", value)


def test_exact_identifiers_and_digests_and_no_unknown_fields():
    for definition, value in [
        ("Id", "00000000-0000-4000-8000-000000000001\n"),
        ("Digest", "a" * 64 + "\n"),
    ]:
        with pytest.raises(ContractError):
            validate_contract("core.schema.json", definition, value)
    query = {"company_id": "00000000-0000-4000-8000-000000000001", "hidden": True}
    with pytest.raises(ContractError):
        validate_contract("core.schema.json", "ContextRequest", query)


def test_installed_frozen_dashboard_money_keyword_is_enforced():
    # The existing dashboard uses a distinct x-fen-int64 keyword. Select a real
    # scalar definition from its installed contract, including nested variants.
    document = schema_document("dashboard-response-v2.json", "dashboard_brief")
    node = next(
        child
        for child in walk(document)
        if isinstance(child, dict) and child.get("x-fen-int64") is True
    )
    assert node["type"] == "string"
    # A complete published dashboard fixture is owned by reading. Here a local
    # registry exercises this exact frozen scalar without manufacturing a page.
    from ai_accounting_modular.contracts._validation import _Validator

    checker = _Validator(node)
    checker.validate(str(MAX_FEN))
    for value in [str(MAX_FEN + 1), str(MIN_FEN - 1), "1\n", "-0", 1, True]:
        assert not checker.is_valid(value), value


def walk(value):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def test_existing_core_and_business_examples_validate_under_runtime_contracts():
    core = example("core-acceptance.json")
    for value in core["objects"]:
        validate_contract("core.schema.json", "Object", value)
    for value in core["sources"]:
        validate_contract("core.schema.json", "Source", value)
    for value in core["requests"].values():
        validate_contract("core.schema.json", "ApplyRequest", value)
    business = example("business-facts-cases.json")
    for case in business["valid_cases"]:
        validate_contract("business-facts.schema.json", "Fact", case["when"])
    for case in business["negative_cases"]:
        if case["then"]["schema"] == "invalid":
            with pytest.raises(ContractError):
                validate_contract("business-facts.schema.json", "Fact", case["when"])
        else:
            validate_contract("business-facts.schema.json", "Fact", case["when"])
    lifecycle = business["investment_lifecycle"]
    for value in lifecycle["bootstrap_facts"]:
        validate_contract("business-facts.schema.json", "Fact", value)
    for case in lifecycle["cases"]:
        for step in case.get("steps", []):
            if "fact" in step:
                validate_contract("business-facts.schema.json", "Fact", step["fact"])
    for key in ["company_funding", "agent_collection", "february_position_before_operator_topup"]:
        validate_contract(
            "business-facts.schema.json", "PostingPlan", lifecycle["plan_examples"][key]
        )


def test_existing_reporting_files_and_source_examples_validate():
    reports = example("reporting-cases.json")
    for case in reports["cases"]:
        if "expected_snapshot" in case:
            validate_contract(
                "reporting.schema.json", "FinancialSnapshot", case["expected_snapshot"]
            )
    for case in reports["contract_examples"]:
        definition = case["schema_ref"].split("/$defs/")[1]
        validate_contract("reporting.schema.json", definition, case["value"])
    file_data = example("file-cases.json")
    for key, definition in file_data["schema_map"].items():
        validate_contract("core.schema.json", definition, file_data["contract_examples"][key])
    sources = example("review-materials-cases.json")
    mapping = {
        "current_query": "ReviewMaterialsQuery",
        "frozen_query": "ReviewMaterialsQuery",
        "response": "ReviewMaterialsResponse",
        "content_query": "SourceContentQuery",
        "statement_content": "SourceTextContent",
        "file_receipt": "SourceFileReceipt",
    }
    for key, definition in mapping.items():
        validate_contract("review-materials.schema.json", definition, sources["examples"][key])
    for case in sources["negative_cases"]:
        with pytest.raises(ContractError):
            validate_contract("review-materials.schema.json", case["definition"], case["value"])


def test_change_description_and_plan_have_distinct_responsibilities():
    assert "plans" not in get_type_hints(ChangeDescription)
    assert "plans" in get_type_hints(ChangePlan)
    assert "complete_result_slots" not in get_type_hints(PostingPlan)
    assert "target" in get_type_hints(PostingPlan)
    description = {
        "domain": "operations",
        "businesses": [
            {
                "business_id": "00000000-0000-4000-8000-000000000001",
                "expected_seq": 1,
                "op": "handoff_out",
                "natural_key": None,
                "complete_result_slots": [],
            }
        ],
        "dependencies": [],
        "publications": [],
        "issues": [],
    }
    validate_contract("business-facts.schema.json", "ChangeDescription", description)
    description["businesses"][0]["complete_result_slots"] = ["main"]
    with pytest.raises(ContractError):
        validate_contract("business-facts.schema.json", "ChangeDescription", description)


def test_errors_are_machine_readable_without_turning_program_errors_into_missing_facts():
    error = ContractError("invalid_input", "请修正接口字段")
    response = error.to_response()
    assert "fact_issues" not in response
    validate_contract("core.schema.json", "ErrorResponse", response)
    with pytest.raises(ValueError, match="actual fact issues"):
        ContractError("needs_information", "缺少事实")


def test_contexts_have_no_public_sql_or_commit_entry():
    for protocol in [ReadContext, UnitOfWork]:
        assert not any(name in protocol.__dict__ for name in ["connection", "execute", "commit"])
    assert "repository" in ReadContext.__dict__
    assert "check_before_commit" in UnitOfWork.__dict__


def test_returned_schemas_are_independent_and_unknown_contracts_fail():
    schema = schema_document("core.schema.json", "Fen")
    schema["$defs"]["Fen"]["type"] = "integer"
    assert schema_document("core.schema.json", "Fen")["$defs"]["Fen"]["type"] == "string"
    for name, definition in [("missing.json", "Fen"), ("core.schema.json", "Missing")]:
        with pytest.raises(ContractError) as failure:
            validate_contract(name, definition, "1")
        assert failure.value.code == "invalid_contract"


def test_generated_types_and_resources_match_the_only_source():
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools/generate_modular_contracts.py"), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_packaged_contracts_work_without_the_design_directory(tmp_path):
    package = tmp_path / "ai_accounting_modular"
    package.mkdir()
    shutil.copyfile(ROOT / "src/ai_accounting_modular/__init__.py", package / "__init__.py")
    shutil.copytree(ROOT / "src/ai_accounting_modular/contracts", package / "contracts")
    environment = dict(os.environ, PYTHONPATH=str(tmp_path))
    script = (
        "from pathlib import Path; "
        "import ai_accounting_modular.contracts.public as c; "
        "assert Path(c.__file__).resolve().is_relative_to(Path.cwd()); "
        "assert c.validate_contract('core.schema.json','Fen','9223372036854775807'); "
        "assert c.schema_document('business-facts.schema.json','PostingPlan')['$defs']"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
