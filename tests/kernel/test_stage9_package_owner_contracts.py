"""Run packaged owner-consumer checks against current native and serialized reads."""

import copy

import pytest
from jsonschema import ValidationError as WireValidationError
from material_fixture import supporting_text
from response_samples import http_samples, native_samples
from stage9_package_agent_harness import assert_owner_brief_result
from test_close_review_transport import prepared_company
from test_payroll import payroll
from test_payroll_corrections import company as payroll_company
from test_resident_service import PASSWORD
from test_resident_service import resident as resident_fixture

from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.response_contracts import http_response
from scripts.verify_local_package import (
    assert_owner_close_review,
    assert_owner_read,
    assert_payroll_confirmation_history,
)

resident = resident_fixture


@pytest.fixture(scope="module")
def current_samples(tmp_path_factory):
    return native_samples(tmp_path_factory.mktemp("package-owner-contracts"))


def test_all_current_native_and_http_owner_consumers(current_samples):
    commands = set()
    for wire, samples in ((False, current_samples), (True, http_samples(current_samples))):
        for sample in samples.values():
            command, value = sample["command"], sample["response"]
            if not command.startswith("dashboard_"):
                continue
            context = value.get("read_context", {})
            company_id = context.get("company_id")
            period = (value.get("selected_period") or {}).get("key")
            if command == "dashboard_period_preparation":
                period = value["period"]
            if command == "dashboard_context":
                company_id = (value["current_company"] or {}).get("company_id")
            subject_id = (
                value["data"]["identity"]["subject_id"]
                if command == "dashboard_business_status"
                else None
            )
            assert_owner_read(
                command,
                value,
                company_id=company_id,
                period=period,
                subject_id=subject_id,
                wire=wire,
            )
            commands.add(command)
    assert commands == {
        "dashboard_context",
        "dashboard_brief",
        "dashboard_funds",
        "dashboard_employees",
        "dashboard_assets",
        "dashboard_business_status",
        "dashboard_quarterly_report",
        "dashboard_period_preparation",
    }


def test_all_owner_consumers_reject_obsolete_envelopes(current_samples):
    commands = set()
    for sample in current_samples.values():
        command = sample["command"]
        if not command.startswith("dashboard_") or command in commands:
            continue
        value = copy.deepcopy(sample["response"])
        value["schema_version"] -= 1
        with pytest.raises(KernelError) as rejected:
            assert_owner_read(command, value)
        assert rejected.value.code == "response_contract_mismatch"
        commands.add(command)
    assert len(commands) == 8


def test_owner_consumer_checks_pagination_scope_and_transport_money(current_samples):
    source = current_samples["brief"]["response"]
    value = copy.deepcopy(source)
    value["data"]["collections"]["activity"]["page"]["returned_count"] += 1
    with pytest.raises(AssertionError):
        assert_owner_read("dashboard_brief", value)
    with pytest.raises(AssertionError):
        assert_owner_read("dashboard_brief", source, company_id="another-company")
    with pytest.raises(AssertionError):
        assert_owner_read("dashboard_brief", source, period="2026-12")
    with pytest.raises(WireValidationError):
        assert_owner_read("dashboard_brief", source, wire=True)
    with pytest.raises(KernelError):
        assert_owner_read("dashboard_brief", http_response("dashboard_brief", source))
    outside_int64 = http_response("dashboard_brief", source)
    outside_int64["data"]["position"]["month_expense_fen"] = str(2**63)
    with pytest.raises(WireValidationError):
        assert_owner_read("dashboard_brief", outside_int64, wire=True)


def test_package_agent_harness_checks_current_scope_pages_and_all_fund_amounts(current_samples):
    brief = current_samples["brief"]["response"]
    funds = current_samples["cash_funds"]["response"]
    company_id = brief["read_context"]["company_id"]
    period = brief["selected_period"]["key"]
    assert_owner_brief_result(brief, funds, company_id=company_id, period=period)
    wrong_funds = copy.deepcopy(funds)
    wrong_funds["data"]["cash_fen"] += 1
    with pytest.raises(AssertionError):
        assert_owner_brief_result(brief, wrong_funds, company_id=company_id, period=period)
    wrong_funds = copy.deepcopy(funds)
    wrong_funds["read_context"]["company_id"] = "another-company"
    with pytest.raises(AssertionError):
        assert_owner_brief_result(brief, wrong_funds, company_id=company_id, period=period)


@pytest.mark.parametrize("amount", [True, 1.0, "01", "-0", str(2**63), str(-(2**63) - 1)])
def test_wire_owner_amounts_reject_noncanonical_and_outside_int64(current_samples, amount):
    value = http_response("dashboard_brief", current_samples["brief"]["response"])
    value["data"]["position"]["month_expense_fen"] = amount
    with pytest.raises(WireValidationError):
        assert_owner_read("dashboard_brief", value, wire=True)


def test_native_owner_amounts_keep_integer_only(current_samples):
    value = copy.deepcopy(current_samples["brief"]["response"])
    value["data"]["position"]["month_expense_fen"] = "1000"
    with pytest.raises(KernelError) as rejected:
        assert_owner_read("dashboard_brief", value)
    assert rejected.value.code == "response_contract_mismatch"


def test_package_close_review_checks_exact_preview_and_frozen_public_amount(resident, tmp_path):
    service, engine, company, proof, _, execute, read, _ = prepared_company(resident, prepare=False)
    supplier = Entities(engine).register_entity(
        "organization", {}, source="synthetic supplier", request_id="supplier"
    )["entity_id"]
    engine.save_fact(
        "expense",
        "expense",
        {
            "period": "2026-01",
            "counterparty_id": supplier,
            "amount_fen": 1000,
            "expense_class": "administration",
            "creditor_kind": "supplier",
        },
        evidence=(proof,),
        expected_revision=0,
        request_id="expense",
    )
    publication = engine.preview(["expense"])
    engine.confirm(
        ["expense"],
        preview_digest=publication["digest"],
        epochs=publication["epochs"],
        request_id="publish-expense",
    )
    supporting_text(engine, proof)
    Periods(engine).inventory(
        "2026-01",
        "transactions",
        evidence=[proof],
        expected=1,
        no_business=False,
        confirmation_evidence=proof,
        request_id="expense-materials",
    )
    preview = execute("preview_close", period="2026-01", owner_confirmation=proof)
    options = {
        "company_id": company["id"],
        "database_id": company["database_id"],
        "period": "2026-01",
        "preview": preview,
        "expected_expense_fen": 1000,
    }
    native = execute("dashboard_close_review", period="2026-01", preview_digest=preview["digest"])
    assert_owner_close_review(native, state="prepared", **options)
    shown = read(preview_digest=preview["digest"])[3]
    assert_owner_close_review(shown, state="prepared", wire=True, **options)
    assert "accounting_summary" in preview["manifest"]["owner_review"]
    assert "accounting_summary" not in native["owner_review"]
    for field in ("preview_digest", "database_id"):
        altered = copy.deepcopy(native)
        altered[field] = "0" * 64
        with pytest.raises(AssertionError):
            assert_owner_close_review(altered, state="prepared", **options)
    legacy = copy.deepcopy(shown)
    legacy["schema_version"] = 1
    with pytest.raises(WireValidationError):
        assert_owner_close_review(legacy, state="prepared", wire=True, **options)
    request = service.security_controller.request(
        kind="approve_period_close",
        company_id=company["id"],
        database_id=company["database_id"],
        period="2026-01",
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
    )
    status, _, _, approved = resident[3].native(
        "native_execute",
        request_id=request["request_id"],
        password=PASSWORD.get_secret_value(),
    )
    assert status == 200
    closed = execute(
        "close",
        period="2026-01",
        owner_confirmation=proof,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        approval_id=approved["approval_id"],
        request_id="close",
        backup_directory=str(tmp_path / "backups"),
    )
    frozen = read()[3]
    assert_owner_close_review(frozen, state="closed", wire=True, **options)
    assert frozen["close_digest"] == closed["digest"]
    assert frozen["owner_review"] == shown["owner_review"]


def test_confirmation_history_stays_in_complete_business_read(tmp_path):
    book = payroll_company.__wrapped__(tmp_path)
    book.publish("january", "february")
    book.close("2026-01")
    queries = BusinessQueries(book.engine)
    before = queries.business_status("january", "2026-01", as_of="2026-02-28")
    frozen_evidence = before["frozen_adoption"]["payroll_confirmation"]["evidence"][0]
    book.save(payroll(expense_class="sales"), "january", revision=1)
    book.confirm_payroll("january")
    book.publish("january", posting_period="2026-02")
    current = queries.business_status("january", "2026-01", as_of="2026-02-28")
    current_evidence = current["current_business_result"]["payroll_confirmation"]["evidence"][0]
    assert_payroll_confirmation_history(
        current, current_evidence=current_evidence, frozen_evidence=frozen_evidence
    )
    owner = Dashboard(book.engine).business_status("2026-01", "january", as_of="2026-02-28")
    assert_owner_read(
        "dashboard_business_status",
        owner,
        company_id="payroll-company",
        period="2026-01",
        subject_id="january",
    )
    assert "payroll_confirmation" not in owner["data"]["current_business_result"]
    assert "payroll_confirmation" not in owner["data"]["frozen_adoption"]
