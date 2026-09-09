"""Payroll failures must distinguish dependency, concurrency and technical faults."""

from types import SimpleNamespace

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError

from ai_accounting.agent_contract import CORRECTION_RUNTIME_INSTRUCTION
from ai_accounting.service import FinanceService


def _error(error_type, state, primary, constraint=None):
    original = Exception("private driver text")
    original.sqlstate = state
    original.diag = SimpleNamespace(
        message_primary=primary,
        constraint_name=constraint,
        context=(
            "SQL statement private SQL and values\n"
            "PL/pgSQL function finance_assert_profile_correction_dependencies(uuid,uuid) "
            "line 69 at RAISE"
        ),
    )
    return error_type("private SQL", {"secret": "private value"}, original)


@pytest.mark.parametrize(
    "guard",
    [
        "R6_FINAL_PAYROLL_PROFILE_CORRECTION_BLOCKED",
        "R6_FINAL_PAYROLL_POLICY_CORRECTION_BLOCKED",
        "R6_FINAL_PAYROLL_OPENING_CORRECTION_BLOCKED",
    ],
)
def test_final_source_dependency_is_not_reported_as_concurrency(guard, caplog):
    result = FinanceService._payroll_database_failure(_error(DBAPIError, "P0001", guard))
    assert result.errors == ["PAYROLL_SOURCE_DEPENDENCY_CONFLICT"]
    assert result.data["failure_kind"] == "business_dependency"
    assert result.data["diagnostic"]["guard"] == guard
    assert result.data["next_action"] == "finance_preview_correction"
    assert result.missing_information == []
    assert "private" not in str(result) + caplog.text


@pytest.mark.parametrize("state", ["40001", "40P01", "55P03"])
def test_actual_concurrency_can_be_retried(state):
    result = FinanceService._payroll_database_failure(_error(OperationalError, state, "private"))
    assert result.errors == ["PAYROLL_CONCURRENT_WRITE_CONFLICT"]
    assert result.data["next_action"] == "retry_after_reload"


@pytest.mark.parametrize(
    ("error_type", "state", "constraint"),
    [
        (OperationalError, "08006", None),
        (IntegrityError, "23503", "fk_payroll_batch_org_event"),
        (IntegrityError, "23514", "ck_payroll_batch_kind"),
        (IntegrityError, "23505", "uq_unexpected_internal_key"),
        (DBAPIError, "P0001", None),
    ],
)
def test_unknown_database_fault_is_not_a_retry_or_missing_fact(
    error_type, state, constraint, caplog
):
    result = FinanceService._payroll_database_failure(
        _error(error_type, state, "private internal values", constraint)
    )
    assert result.errors == ["PAYROLL_INTERNAL_DATABASE_ERROR"]
    assert result.data["failure_kind"] == "technical_failure"
    assert result.data["diagnostic_id"]
    assert result.data["next_action"] == "inspect_failure"
    assert result.missing_information == []
    assert "private" not in str(result) + caplog.text


def test_known_unique_race_is_retryable():
    result = FinanceService._payroll_database_failure(
        _error(IntegrityError, "23505", "private", "uq_payroll_batch_idempotency")
    )
    assert result.errors == ["PAYROLL_CONCURRENT_WRITE_CONFLICT"]


def test_dependency_code_in_sql_parameters_does_not_classify_error():
    exc = _error(DBAPIError, "P0001", "unexpected failure")
    exc.params = {"value": "R6_FINAL_PAYROLL_PROFILE_CORRECTION_BLOCKED"}
    assert not FinanceService._is_round6_final_dependency_error(exc)


def test_runtime_contract_does_not_treat_repeated_database_failures_as_facts():
    assert "PAYROLL_INTERNAL_DATABASE_ERROR不是工资事实缺失" in CORRECTION_RUNTIME_INSTRUCTION
    assert "同一约束重复失败不得继续盲目重试" in CORRECTION_RUNTIME_INSTRUCTION
