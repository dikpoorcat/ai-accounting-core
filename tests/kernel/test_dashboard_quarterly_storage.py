"""Quarter aggregates retain their native contract and exact company scope."""

import pytest
from test_banking import book as book
from test_banking import opening
from test_dashboard_read_model_storage import native as native

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_model_runtime import stable_payload
from ai_accounting.kernel.dashboard_read_model import PageBlock, prepare_model
from ai_accounting.kernel.response_contracts import validate_response


@pytest.fixture(scope="module")
def quarterly(tmp_path_factory, native):
    engine, save, publish, _ = book.__wrapped__(tmp_path_factory.mktemp("quarter-model-storage"))
    opening(save, publish)
    response = Dashboard(engine).quarterly_report(2026, 3, preparation="deferred")
    validate_response("dashboard_quarterly_report", response)
    scope, _ = native
    scope = scope.model_copy(update={
        "company_id": engine.store.company_id,
        "database_id": engine.store.database_id,
        "as_of": response["read_context"]["as_of"],
    })
    return scope, response


def test_quarter_volatile_display_metadata_keeps_native_shape_and_financial_content(quarterly):
    scope, original = quarterly
    stable = stable_payload(original)
    assert "snapshot_version" not in stable
    assert stable["checked_at"] != original["checked_at"]
    assert stable["summary"] == original["summary"]
    assert stable["statements"] == original["statements"]
    assert stable["export"] == original["export"]
    assert stable["period"] == original["period"]
    validate_response("dashboard_quarterly_report", stable)
    prepared = prepare_model(scope, [PageBlock(
        "quarterly_report/default/overview/0", "dashboard_quarterly_report", stable,
    )], request_id="quarter-model-storage")
    assert prepared.descriptor["scope"]["period"] == "2026-09"


@pytest.mark.parametrize("field,value", (
    ("period", "2026-06"), ("period", "2025-09"),
    ("company_id", "other-company"), ("database_id", "other-database"),
))
def test_quarter_model_rejects_other_quarter_year_and_company(quarterly, field, value):
    scope, payload = quarterly
    changed = scope.model_copy(update={field: value})
    block = PageBlock("quarterly_report/default/overview/0", "dashboard_quarterly_report", payload)
    with pytest.raises(KernelError) as error:
        prepare_model(changed, [block], request_id="wrong-quarter-scope")
    assert error.value.code == "content_integrity_failed"


def test_quarter_scope_accepts_an_earlier_registered_month_in_the_same_quarter(quarterly):
    scope, payload = quarterly
    prepared = prepare_model(scope.model_copy(update={"period": "2026-07"}), [PageBlock(
        "quarterly_report/default/overview/0", "dashboard_quarterly_report", payload,
    )], request_id="quarter-before-quarter-end")
    assert prepared.descriptor["scope"]["period"] == "2026-07"


def test_quarter_aggregate_cannot_escape_integer_money_contract(quarterly):
    import copy

    scope, original = quarterly
    for invalid in (True, 0.5, 2**63):
        payload = copy.deepcopy(original)
        payload["summary"]["assets_total_fen"] = invalid
        block = PageBlock(
            "quarterly_report/default/overview/0", "dashboard_quarterly_report", payload,
        )
        with pytest.raises(KernelError) as error:
            prepare_model(scope, [block], request_id="bad-quarter-money")
        assert error.value.code == "content_integrity_failed"
