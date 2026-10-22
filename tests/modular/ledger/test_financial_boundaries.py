"""Sparse formal boundaries preserve history without scanning ordinary results."""

import sqlite3
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict
from uuid import NAMESPACE_URL, uuid5

import pytest
import test_adoption as adoption_fixture
from test_adoption import COMPANY, adopt_declared, expense, publication, source_only_change
from test_adoption import store as store
from test_closing import close
from test_financial_core import FEE, ORIGIN, origin, save

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.ledger._repository import _FINANCIAL_KINDS, LedgerRepository
from ai_accounting_modular.ledger.public import create_ledger
from ai_accounting_modular.storage.public import verify_database
from tests.modular.acceptance.performance.ledger_variant import MODES, repository_factory


@pytest.fixture(autouse=True, params=MODES)
def ledger_mode(request, monkeypatch):
    factory = repository_factory(request.param)
    monkeypatch.setattr(adoption_fixture, "repository_factory", lambda: factory)
    return request.param


def legacy_boundaries(repo, *, as_of=None):
    """The original query is an independent selection oracle, not another calculator."""
    sql = "SELECT r.*" + _FINANCIAL_KINDS + " FROM adopted_result r "
    if as_of is None:
        sql += "JOIN result_slot_current c ON c.head_result_id=r.result_id "
    sql += (
        "WHERE r.result_slot='main' AND json_extract(r.target_json,'$.kind') "
        "IN ('opening.package','reports.continuation')"
    )
    args = ()
    if as_of is not None:
        sql += (
            " AND r.posting_period<=? AND NOT EXISTS(SELECT 1 FROM adopted_result newer "
            "WHERE newer.business_id=r.business_id AND newer.result_slot=r.result_slot "
            "AND newer.posting_period<=? AND newer.posting_period>r.posting_period)"
        )
        args = as_of, as_of
    return [dict(row) for row in repo._session.fetchall(sql, args)]


def assert_original_public_results(store, ledger, monkeypatch, period, view):
    scope = {
        "company_id": COMPANY,
        "period_from": period,
        "period_to": period,
        "view": view,
    }
    with store.read_unit(COMPANY, view=view, as_of=period if view == "as_posted" else None) as read:
        actual_summary = asdict(ledger.financial_summary(scope, read))
        actual_snapshot = ledger.financial_snapshot(scope, read)
        with monkeypatch.context() as patched:
            patched.setattr(LedgerRepository, "financial_boundaries", legacy_boundaries)
            assert asdict(ledger.financial_summary(scope, read)) == actual_summary
            assert ledger.financial_snapshot(scope, read) == actual_snapshot


def continuation():
    change = source_only_change("reports.continuation")
    change["businesses"][0]["fact"]["data"]["opening_package_id"] = ORIGIN
    change["plans"][0]["basis"]["input"]["opening_package_id"] = ORIGIN
    return change


def retire_origin(seq):
    change = origin("0")
    responsibility = change["businesses"][0]
    responsibility.pop("fact")
    responsibility.update(op="withdraw", expected_seq=seq, complete_result_slots=[])
    change.update(plans=[], publications=[])
    return change


def test_boundary_targets_keep_nonmain_and_calculation_identity_distinct(store, monkeypatch):
    ledger = create_ledger()
    initial = origin("0")
    plan = initial["plans"][0]
    plan["calculation_key"], plan["calculation_revision"] = "fixture.opening.rule", 7
    auxiliary = deepcopy(plan)
    auxiliary["result_slot"] = auxiliary["target"]["result_slot"] = auxiliary["basis"][
        "result_slot"
    ] = "aux"
    initial["plans"].append(auxiliary)
    initial["businesses"][0]["complete_result_slots"].append("aux")
    initial["publications"].append(publication(ORIGIN, slot="aux"))
    continued = continuation()
    save(store, ledger, [initial, continued, expense("100", business_id=FEE)])
    with store.read_unit(COMPANY) as read:
        repo = read.repository("ledger")
        expected = {(ORIGIN, "main"), (continued["businesses"][0]["business_id"], "main")}
        for cutoff in (None, "2026-01"):
            rows = repo.financial_boundaries(as_of=cutoff)
            assert {(row["business_id"], row["result_slot"]) for row in rows} == expected
            assert (
                next(row for row in rows if row["business_id"] == ORIGIN)["calculation_key"]
                == "fixture.opening.rule"
            )
        assert repo.financial_boundaries(as_of="2025-12") == []
    for view in ("current", "as_posted"):
        assert_original_public_results(store, ledger, monkeypatch, "2026-01", view)


def test_closed_correction_then_withdrawal_does_not_revive_old_boundary(store, monkeypatch):
    ledger = create_ledger()
    save(store, ledger, [origin("1000"), expense("100", business_id=FEE)])
    close(store, ledger, "2026-01")
    with store.read_unit(COMPANY) as read:
        original = read.repository("ledger").financial_boundaries(as_of="2026-01")[0]
        assert original["state"] == "frozen"
    changed = origin("2000")
    changed["businesses"][0].update(op="replace", expected_seq=1)
    save(store, ledger, [changed], correction="2026-02")
    close(store, ledger, "2026-02")
    with store.read_unit(COMPANY) as read:
        repo = read.repository("ledger")
        updated = repo.financial_boundaries(as_of="2026-02")[0]
        assert updated["result_id"] != original["result_id"]
        assert updated["baseline_result_id"] == original["result_id"]
        assert updated["business_period"] == "2026-01"
        assert updated["posting_period"] == "2026-02"
        assert repo.financial_boundaries() == [updated]
        assert repo.financial_boundaries(as_of="2026-01") == [original]
    with store.write_unit(COMPANY) as unit:
        adopt_declared(
            ledger,
            [retire_origin(2)],
            unit,
            correction_period="2026-03",
            explanation="实际资料确认这份期初应退出",
        )
    with store.read_unit(COMPANY) as read:
        repo = read.repository("ledger")
        withdrawn = repo.current_heads([ORIGIN])[0]
        assert withdrawn["target_state"] == "withdrawn"
        assert withdrawn["baseline_result_id"] == updated["result_id"]
        assert repo.financial_boundaries() == []
        assert repo.financial_boundaries(as_of="2026-03") == []
        assert repo.financial_boundaries(as_of="2026-02") == [updated]
        assert repo.financial_boundaries(as_of="2026-01") == [original]
    for period, view in (
        ("2026-01", "as_posted"),
        ("2026-03", "as_posted"),
        ("2026-03", "current"),
    ):
        assert_original_public_results(store, ledger, monkeypatch, period, view)


def test_open_retirement_removes_boundary_from_both_selections(store):
    ledger = create_ledger()
    save(store, ledger, [origin("0")])
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [retire_origin(1)], unit)
    with store.read_unit(COMPANY) as read:
        repo = read.repository("ledger")
        assert repo.financial_boundaries() == []
        assert repo.financial_boundaries(as_of="2026-01") == []
        assert ledger.get_results([ORIGIN], read) == ()


def boundary_work(read):
    steps = 0

    def progress():
        nonlocal steps
        steps += 1
        return 0

    read._connection.set_progress_handler(progress, 1)
    try:
        rows = read.repository("ledger").financial_boundaries(as_of="2026-01")
    finally:
        read._connection.set_progress_handler(None, 0)
    return rows, steps


def test_historical_boundary_work_does_not_grow_with_unrelated_formal_results(store, monkeypatch):
    ledger = create_ledger()
    save(store, ledger, [origin("0"), continuation()])
    with store.read_unit(COMPANY) as read:
        before, before_steps = boundary_work(read)
    save(
        store,
        ledger,
        [
            expense("1", business_id=str(uuid5(NAMESPACE_URL, f"ordinary-boundary-test/{index}")))
            for index in range(96)
        ],
    )
    with store.read_unit(COMPANY) as read:
        after, after_steps = boundary_work(read)
        assert after == before
        # Real VM instructions, not elapsed time or mocked row counts. Scanning
        # the 96 added ordinary results would exceed this fixed small allowance.
        assert after_steps <= before_steps + 100
        repo, captured = read.repository("ledger"), []
        fetchall = repo._session.fetchall

        def observe(sql, args=()):
            captured.append((sql, args))
            return fetchall(sql, args)

        with monkeypatch.context() as patched:
            patched.setattr(repo._session, "fetchall", observe)
            repo.financial_boundaries(as_of="2026-01")
        assert len(captured) == 1
        sql, args = captured[0]
        plan = [row[3] for row in read._connection.execute("EXPLAIN QUERY PLAN " + sql, args)]
        assert any("adopted_result_financial_boundaries" in step for step in plan)
        assert any("adopted_result_business_slot_period" in step for step in plan)


def test_missing_boundary_index_is_rejected_without_automatic_structure_repair(store):
    database = store.list_companies()[0].database_path
    verify_database(database)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("DROP INDEX adopted_result_financial_boundaries")
        connection.commit()
    before = database.read_bytes()
    with pytest.raises(ContractError) as error:
        verify_database(database)
    assert error.value.code == "storage_corrupt"
    assert database.read_bytes() == before
    with closing(sqlite3.connect(database)) as connection:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_schema WHERE name='adopted_result_financial_boundaries'"
            ).fetchone()
            is None
        )
