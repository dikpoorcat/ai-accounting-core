"""All formal changes reach the private producer without invalidating unrelated consumers."""

from copy import deepcopy

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.host.public import create_kernel
from ai_accounting_modular.reading import _financial_projection as projection
from ai_accounting_modular.reading import _monthly

from .test_financial_projection_integration import (
    assert_month,
    classic_snapshot,
    month_page,
    month_scope,
    settle,
)
from .test_financial_projection_integration import live_case as live_case


def apply(case, mutation):
    result = case.app.accounting_apply(case.request(mutations=[mutation]))
    assert result["completion"] == "complete", result


def pending(case):
    with case.kernel.storage.read_unit(case.company) as read:
        target = projection.store(read)
        return target.state(), target.work()


def test_cached_future_target_migration_survives_restart_before_new_range(live_case, tmp_path):
    case = live_case
    case.seed()
    future = case.expense("70000", "2026-08")
    apply(case, {"op": "create", "expected_seq": 0, "fact": future})
    month_page(case)
    settle(case)
    original = month_page(case)
    moved = deepcopy(future) | {"period": "2026-09"}
    apply(case, {"op": "replace", "expected_seq": 1, "fact": moved})
    state, work = pending(case)
    assert state["state"] == "preparing" and not work["pending"]
    assert {value["business_id"] for value in work["changes"]} == {future["business_id"]}
    assert month_page(case) == original
    case.kernel.close()
    case.kernel = create_kernel(tmp_path / "catalog.sqlite3")
    # No body or role was rebuilt between the move and this independent opening.
    # August must not inherit the stale catalog's August target, nor have that
    # absent target subtracted when September is requested afterwards.
    month_page(case, "2026-08")
    settle(case)
    assert_month(case, "2026-08", "0")
    month_page(case, "2026-09")
    settle(case)
    assert_month(case, "2026-09", "70000")
    assert month_page(case) == original
    selected_scope = month_scope(case, "2026-09") | {"period_from": "2026-07"}
    reader = case.kernel.reading
    assert reader.page("quarterly_report", selected_scope, {}).state == "preparing"
    settle(case)
    actual = reader.page("quarterly_report", selected_scope, {}).payload["snapshot"]
    with case.kernel.storage.read_unit(case.company) as read:
        assert actual == case.kernel.ledger.financial_snapshot(selected_scope, read)
        classic = classic_snapshot(selected_scope, read)
    without_version = deepcopy(actual)
    without_version["selection"].pop("selection_digest")
    assert without_version == classic


def test_private_pending_failure_keeps_changes_and_late_notification_rejects_old_plan(
    live_case, monkeypatch
):
    case = live_case
    case.seed()
    month_page(case)
    settle(case)
    before = month_page(case)
    first = case.expense("70000", "2026-07")
    apply(case, {"op": "create", "expected_seq": 0, "fact": first})
    owner = case.kernel.ledger.prepare_financial_projection

    def failed(*args, **kwargs):
        raise OSError("controlled private producer failure")

    with monkeypatch.context() as patch:
        patch.setattr(case.kernel.ledger, "prepare_financial_projection", failed)
        assert not projection.produce(case.kernel.reading, case.company)
    state, work = pending(case)
    assert state["state"] == "failed" and len(work["changes"]) == 1
    assert month_page(case) == before
    month_page(case, "2026-07")
    _monthly.retry(case.kernel.reading, month_scope(case, "2026-07"), ("financial",))
    with case.kernel.storage.read_unit(case.company) as read:
        target = projection.store(read)
        work, token = target.work(), target.token()
        revision = target.state()["revision"]
        registered = projection._requests(case.kernel.reading, work, read)
        plan = owner(
            tuple(registered[key] for key in work["pending"]),
            projection._changes(work["changes"]),
            (),
            read,
            base=target,
        )
    second = case.expense("90000", "2026-08")
    apply(case, {"op": "create", "expected_seq": 0, "fact": second})
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        target = projection.store(unit)
        assert not target.publish(token, plan)
        assert not target.fail(token, "production_failed", expected_revision=revision)
        assert {value["business_id"] for value in target.work()["changes"]} == {
            first["business_id"],
            second["business_id"],
        }
    settle(case)
    assert_month(case, "2026-07", "70000")
    assert month_page(case) == before


def test_unrelated_private_notification_rolls_back_with_failed_formal_transaction(
    live_case, monkeypatch
):
    case = live_case
    case.seed()
    month_page(case)
    settle(case)
    before, before_work = month_page(case), pending(case)
    future = case.expense("70000", "2026-07")
    mark = case.kernel.reading.mark_targets

    def failed(impact, unit):
        assert mark(impact, unit) == ()  # No consumer was made dirty.
        work = projection.store(unit).work()
        assert {value["business_id"] for value in work["changes"]} == {future["business_id"]}
        raise ContractError("invalid_operation", "合成事务在通知后失败。")

    with monkeypatch.context() as patch:
        patch.setattr(case.kernel.reading, "mark_targets", failed)
        response = case.app.accounting_apply(case.request(future))
    assert response["code"] == "invalid_operation", response
    assert pending(case) == before_work
    assert month_page(case) == before
    with case.kernel.storage.read_unit(case.company) as read:
        assert (
            read.repository("ledger").financial_projection_chains(
                ((future["business_id"], "main"),)
            )
            == []
        )
