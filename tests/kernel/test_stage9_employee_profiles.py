"""Synthetic sample completeness uses one ordinary kernel management write."""

import pytest
import stage9_book
from stage9_book import MixedBook
from stage9_employee_profiles import (
    accounting_source_fingerprint,
    current_employee_revisions,
    supplement_synthetic_employee_profiles,
)
from test_dashboard_model_integrity import database_state

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.maintenance import Maintenance


@pytest.fixture
def workforce_book(tmp_path, monkeypatch):
    # Reproduce the original synthetic definition before its employment fields
    # were supplied. This never mutates an existing real or large sample.
    monkeypatch.setattr(stage9_book, "synthetic_employee_profile", lambda name: {
        "display_name": name,
    })
    book = MixedBook(tmp_path / "stage9-small-workforce", employees=50, businesses=124)
    book.add_month(0, close=True)
    book.add_month(1, close=False)
    Maintenance(book.engine).rebuild_dashboard_read_model(
        "2016-02", request_id="bootstrap-workforce"
    )
    return book


def identity(book):
    return {
        "company_id": book.company["id"], "taxpayer_id": book.company["taxpayer_id"],
        "database_id": book.company["database_id"],
    }


def state_and_sources(engine):
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        return (
            tuple(connection.execute("SELECT * FROM state WHERE id=1").fetchone()),
            accounting_source_fingerprint(connection),
            tuple(tuple(row) for row in connection.execute(
                "SELECT * FROM entity_profile_revision ORDER BY rowid"
            )),
            tuple(tuple(row) for row in connection.execute(
                "SELECT period,request_id,audit_id FROM dashboard_read_model_commit ORDER BY rowid"
            )),
        )


def test_fifty_synthetic_profiles_produce_real_default_and_in_period_pages_once(workforce_book):
    book = workforce_book
    engine, dashboard = book.engine, Dashboard(book.engine)
    members = current_employee_revisions(engine, book.employees)
    assert dashboard.employees("2016-02")["data"]["collections"]["employees"]["page"][
        "filtered_count"
    ] == 0
    before_state, before_sources, before_profiles, before_commits = state_and_sources(engine)
    money_before = {
        period: dashboard.brief(period)["data"]["funds_overview"]
        for period in ("2016-01", "2016-02")
    }
    result = supplement_synthetic_employee_profiles(
        engine, members, identity=identity(book), request_id="synthetic-workforce-batch"
    )
    assert len(result["results"]) == 50
    after_state, after_sources, after_profiles, after_commits = state_and_sources(engine)
    assert after_state == (*before_state[:3], before_state[3] + 1, *before_state[4:])
    assert after_sources == before_sources
    assert after_profiles[:len(before_profiles)] == before_profiles
    assert len(after_profiles) == len(before_profiles) + 50
    assert after_commits[:len(before_commits)] == before_commits
    new_commits = after_commits[len(before_commits):]
    # One ordinary writer production refreshes each registered period, sharing
    # one real producer receipt rather than refreshing after each employee.
    assert sorted(row[0] for row in new_commits) == sorted({row[0] for row in before_commits})
    assert len({row[1] for row in new_commits}) == 1
    assert len({row[2] for row in new_commits}) == 1
    with engine.store.connection(read_only=True) as connection:
        audit = connection.execute(
            "SELECT request_id,action FROM audit WHERE id=?", (new_commits[0][2],),
        ).fetchone()
        assert tuple(audit) == (new_commits[0][1], "dashboard_read_model")
        assert connection.execute(
            "SELECT count(*) FROM request WHERE id=?", (new_commits[0][1],),
        ).fetchone()[0] == 1
    for employee_filter in ("employment_active", "all", "in_period"):
        response = dashboard.employees("2016-02", employee_filter=employee_filter)
        collection = response["data"]["collections"]["employees"]
        assert collection["page"]["total_count"] == 50
        assert collection["page"]["filtered_count"] == 50
        assert collection["page"]["returned_count"] == 20
        assert len(collection["items"]) == 20
        assert all(item["employment_state"] == "regular" for item in collection["items"])
        assert all(item["in_period"] is True for item in collection["items"])
    assert {
        period: dashboard.brief(period)["data"]["funds_overview"]
        for period in money_before
    } == money_before
    assert supplement_synthetic_employee_profiles(
        engine, members, identity=identity(book), request_id="synthetic-workforce-batch"
    ) == result
    assert state_and_sources(engine) == (
        after_state, after_sources, after_profiles, after_commits,
    )
    assert Maintenance(engine).verify_integrity()["status"] == "verified"


@pytest.mark.parametrize("mismatch", ("source_identity", "member_25_revision"))
def test_synthetic_workforce_identity_and_mid_batch_conflict_preserve_every_row(
    workforce_book, mismatch,
):
    book = workforce_book
    members = current_employee_revisions(book.engine, book.employees)
    expected_identity = identity(book)
    before = database_state(book.engine)
    if mismatch == "source_identity":
        expected_identity["taxpayer_id"] = "91310000123456789X"
        exception, message = ValueError, "source identity"
    else:
        members[24]["expected_revision"] += 1
        exception, message = KernelError, "合成员工档案已变化"
    with pytest.raises(exception, match=message):
        supplement_synthetic_employee_profiles(
            book.engine, members, identity=expected_identity, request_id="retry-workforce-batch"
        )
    assert database_state(book.engine) == before
    result = supplement_synthetic_employee_profiles(
        book.engine, current_employee_revisions(book.engine, book.employees),
        identity=identity(book), request_id="retry-workforce-batch",
    )
    assert len(result["results"]) == 50
    collection = Dashboard(book.engine).employees("2016-02")["data"]["collections"]["employees"]
    assert collection["page"]["filtered_count"] == 50
    assert len(collection["items"]) == 20
    assert Maintenance(book.engine).verify_integrity()["status"] == "verified"


def test_future_fixture_defines_employee_management_status_without_changing_owner(tmp_path):
    book = MixedBook(tmp_path / "stage9-new-workforce", employees=1, businesses=26)
    book.add_month(0, close=False)
    data = Dashboard(book.engine).employees("2016-01")["data"]["collections"]["employees"]
    assert data["page"]["filtered_count"] == 1
    assert data["items"][0]["employment_state"] == "regular"
    assert data["items"][0]["in_period"] is True
    with book.engine.store.connection(read_only=True) as connection:
        assert connection.execute(
            "SELECT json_extract(content,'$.workforce_event') FROM entity_profile_revision "
            "WHERE entity_id=? ORDER BY revision DESC LIMIT 1", (book.owner,),
        ).fetchone()[0] is None
    owner = next(
        item for item in book.entities.find_entities(kind="person")["items"]
        if item["entity_id"] == book.owner
    )
    assert owner["employment_state"] == "unknown"
