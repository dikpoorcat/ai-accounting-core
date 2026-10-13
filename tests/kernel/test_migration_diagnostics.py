"""Public errors describe the actual migration result and hide SQLite text."""

import sqlite3

import pytest

from ai_accounting.kernel.diagnostics import error_response


@pytest.mark.parametrize(
    "outcome,committed,expected",
    [("committed", True, "已提交"), ("unknown", False, "尚未确认"),
     ("not_started", False, "尚未开始"), ("rolled_back", False, "已回滚")],
)
def test_sqlite_migration_error_reports_commit_state_without_exposing_input(
    outcome, committed, expected,
):
    error = sqlite3.OperationalError("PRIVATE SQL AND SECRET INPUT")
    error.migration_state = {"outcome": outcome, "committed": committed,
                             "connection_discarded": True}
    response = error_response(error)
    assert response["code"] == "database_error"
    assert response["migration_state"] == error.migration_state
    assert expected in response["message"]
    assert "PRIVATE" not in str(response) and "SECRET" not in str(response)
    if outcome != "rolled_back":
        assert "回滚" not in response["message"]


def test_ordinary_sqlite_error_keeps_its_existing_contract():
    assert error_response(sqlite3.OperationalError("PRIVATE INPUT")) == {
        "status": "rejected", "code": "database_error",
        "message": "数据库操作未完成，账务事务已回滚",
    }
