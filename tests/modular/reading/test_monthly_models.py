"""Real monthly components share narrow versions and recover without business replay."""

from copy import deepcopy

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.reading import _monthly
from ai_accounting_modular.reading.public import MonthScope
from tests.modular.application._working_case import case as case


def scope(case, month="2026-01", **values):
    return {
        "company_id": case.company,
        "period_from": month,
        "period_to": month,
        "view": "as_posted",
        **values,
    }


def page(case, selection=None):
    return _monthly.deliver(
        case.kernel.reading,
        selection or scope(case),
        (_monthly.FINANCIAL,),
        lambda snapshot, read: {
            "financial": snapshot.components["financial"]["financial"],
            "period": snapshot.selected_period,
            "database_id": read.database_id,
        },
    )


def ready(case, selection=None):
    assert page(case, selection).state == "preparing"
    assert case.kernel.reading.run_pending(case.company)
    actual = page(case, selection)
    assert actual.state == "ready"
    return actual


def replace(case, original, amount, *, correction_period=None):
    value = deepcopy(original)
    value["data"]["amount_fen"] = amount
    fields = {"mutations": [{"op": "replace", "expected_seq": 1, "fact": value}]}
    if correction_period is not None:
        fields["correction_period"] = correction_period
    actual = case.app.accounting_apply(case.request(**fields))
    assert actual["completion"] == "complete", actual


def test_real_financial_month_uses_complete_amounts_and_hot_reads_do_not_recalculate(
    case, monkeypatch
):
    case.seed()
    actual = ready(case)
    financial = actual.payload["financial"]
    assert financial["revenue"] == {"state": "known", "amount_fen": "0"}
    assert financial["expense"] == {"state": "known", "amount_fen": "100000"}
    assert financial["net_profit"] == {"state": "known", "amount_fen": "-100000"}
    assert "sources" not in financial

    def unexpected(*args):
        raise AssertionError("hot page recalculated formal data")

    monkeypatch.setattr(case.kernel.ledger, "financial_summary", unexpected)
    assert page(case, scope(case, expected_head=actual.version)) == actual


def test_real_related_write_prepares_and_replaces_only_the_current_component(case):
    original = case.seed()
    before = ready(case)
    replace(case, original, "120000")
    assert page(case).state == "preparing"
    with pytest.raises(ContractError) as error:
        page(case, scope(case, expected_head=before.version))
    assert error.value.code == "snapshot_changed"
    case.kernel.reading.run_pending(case.company)
    current = page(case)
    assert current.version != before.version
    assert current.payload["financial"]["expense"]["amount_fen"] == "120000"
    with pytest.raises(ContractError) as error:
        page(case, scope(case, expected_head=before.version))
    assert error.value.code == "snapshot_changed"


def test_later_business_and_unrelated_object_display_do_not_rebuild_earlier_financial_month(
    case, monkeypatch
):
    case.seed()
    before = ready(case)
    later = case.app.accounting_apply(case.request(case.expense("70000", "2026-02")))
    assert later["completion"] == "complete", later
    with case.kernel.storage.read_unit(case.company) as read:
        obj = case.kernel.identity.get_batch([case.party], read)[0]
    changed = case.app.accounting_apply(
        case.request(
            objects=[
                {
                    **{key: obj[key] for key in ("object_id", "kind", "data")},
                    "name": "正确的新名称",
                    "expected_seq": obj["seq"],
                }
            ]
        )
    )
    assert changed["completion"] == "complete", changed
    monkeypatch.setattr(
        case.kernel.ledger,
        "financial_summary",
        lambda *args: pytest.fail("unrelated change rebuilt the financial month"),
    )
    assert case.kernel.reading.run_pending(case.company) == ()
    assert page(case).version == before.version


def test_frozen_month_keeps_adopted_amount_after_a_later_correction(case):
    original = case.seed()
    before = ready(case)
    checked = case.check()
    assert checked["ready"], checked
    assert case.close(checked)["state"] == "closed"
    frozen = ready(case)
    assert frozen.version != before.version
    assert frozen.payload["period"]["status"] == "closed"
    replace(case, original, "120000", correction_period="2026-02")
    assert page(case).version == frozen.version
    assert page(case).payload["financial"]["expense"]["amount_fen"] == "100000"
    corrected = ready(case, scope(case, "2026-02"))
    assert corrected.payload["financial"]["expense"]["amount_fen"] == "20000"


def test_corrupt_current_payload_fails_stably_and_explicit_retry_rebuilds_only_the_read(case):
    case.seed()
    original = ready(case)
    normalized = _monthly.normalize(scope(case))
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        unit.repository("reading")._session.execute(
            "UPDATE read_component SET payload_json='{}' WHERE scope_key=? AND component_key=?",
            (_monthly.key(normalized), _monthly.FINANCIAL),
        )
        head = unit.head
    assert page(case).state == "failed"
    assert page(case).reason == "corrupt"
    assert case.kernel.reading.run_pending(case.company) == ()
    _monthly.retry(case.kernel.reading, scope(case), (_monthly.FINANCIAL,))
    recovered = ready(case)
    assert recovered == original
    with case.kernel.storage.read_unit(case.company) as read:
        assert read.head == head


def test_failure_is_not_unknown_money_and_worker_retry_does_not_reapply_business(case, monkeypatch):
    case.seed()
    assert page(case).state == "preparing"
    original = case.kernel.ledger.financial_summary

    def unavailable(*args):
        raise RuntimeError("synthetic private failure")

    monkeypatch.setattr(case.kernel.ledger, "financial_summary", unavailable)
    assert case.kernel.reading.run_pending(case.company) == ()
    failed = page(case)
    assert (failed.state, failed.reason, failed.payload) == ("failed", "production_failed", None)
    monkeypatch.setattr(case.kernel.ledger, "financial_summary", original)
    _monthly.retry(case.kernel.reading, scope(case), (_monthly.FINANCIAL,))
    assert ready(case).payload["financial"]["expense"]["amount_fen"] == "100000"


def test_late_month_capture_cannot_replace_a_newer_wanted_target(case, monkeypatch):
    original_fact = case.seed()
    assert page(case).state == "preparing"
    original = case.kernel.ledger.financial_summary

    def moved(selection, read):
        captured = original(selection, read)
        replace(case, original_fact, "120000")
        return captured

    monkeypatch.setattr(case.kernel.ledger, "financial_summary", moved)
    assert case.kernel.reading.run_pending(case.company) == ()
    assert page(case).state == "preparing"
    monkeypatch.setattr(case.kernel.ledger, "financial_summary", original)
    assert ready(case).payload["financial"]["expense"]["amount_fen"] == "120000"


@pytest.mark.parametrize(
    "values", ({"view": "current"}, {"as_of": "2026-02"}, {"period_to": "2026-02"})
)
def test_month_scope_is_explicit_and_does_not_silently_reinterpret_selection(case, values):
    case.seed()
    with pytest.raises(ContractError) as error:
        page(case, scope(case, **values))
    assert error.value.code == "invalid_input"


def test_empty_company_has_no_invented_default_month_or_component(case):
    empty = page(case, MonthScope(case.company))
    assert (empty.state, empty.version) == ("ready", None)
    assert empty.payload["selected_period"] is None
    assert empty.payload["snapshot_version"] is None
    assert empty.payload["data"] is None
    with case.kernel.storage.read_unit(case.company) as read:
        assert read.repository("reading").scopes() == ()
    assert case.kernel.reading.run_pending(case.company) == ()


def test_default_month_uses_the_same_real_working_scope_as_context(case):
    case.seed()
    result = case.app.accounting_apply(case.request(case.expense("10000", "2026-02")))
    assert result["completion"] == "complete", result
    request = MonthScope(case.company)
    before = page(case, request)
    assert before.state == "preparing" and before.scope["period_to"] == "2026-02"
    assert case.kernel.reading.run_pending(case.company)
    actual = page(case, request)
    assert actual.payload["period"]["key"] == "2026-02"
    assert actual.payload["financial"]["expense"]["amount_fen"] == "10000"
    context = case.kernel.reading.page("context", {"company_id": case.company}, {})
    assert context["default_period"] == actual.payload["period"]["key"]


def test_unavailable_component_format_fails_instead_of_preparing_forever(case):
    case.seed()
    assert page(case).state == "preparing"
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        unit.repository("reading")._session.execute(
            "UPDATE read_scope_head SET format_version=999 WHERE scope_key=?",
            (_monthly.key(_monthly.normalize(scope(case))),),
        )
    assert case.kernel.reading.run_pending(case.company) == ()
    assert (page(case).state, page(case).reason) == ("failed", "corrupt")
