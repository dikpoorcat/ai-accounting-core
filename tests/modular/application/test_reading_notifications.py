"""Production composition marks real read targets in the accounting transaction."""

from copy import deepcopy
from uuid import uuid4

from ai_accounting_modular.contracts.public import ContractError

from ._working_case import case as case
from .conftest import source
from .test_notes_context import note


def scope(case, quarter=1):
    return {
        "company_id": case.company,
        "period_from": f"2026-{quarter * 3 - 2:02d}",
        "period_to": f"2026-{quarter * 3:02d}",
        "view": "as_posted",
    }


def ready(case, quarter=1):
    reading = case.kernel.reading
    delivery = reading.page("quarterly_report", scope(case, quarter), {})
    if delivery.state == "preparing":
        reading.run_pending(case.company)
        delivery = reading.page("quarterly_report", scope(case, quarter), {})
    assert delivery.state == "ready", delivery
    return delivery


def spy(case, monkeypatch):
    calls = []
    original = case.kernel.reading.mark_targets

    def capture(impact, unit):
        calls.append(impact)
        return original(impact, unit)

    monkeypatch.setattr(case.kernel.reading, "mark_targets", capture)
    return calls


def ranges(values):
    return [(value.period_from, value.period_to) for value in values]


def formal_periods(impact, field):
    return sorted({period for change in impact.formal_changes for period in getattr(change, field)})


def test_open_change_refreshes_real_model_and_retry_and_future_month_preserve_it(case, monkeypatch):
    fact = case.seed()
    initial = ready(case)
    ready(case, 2)
    calls = spy(case, monkeypatch)
    corrected = deepcopy(fact)
    corrected["data"]["amount_fen"] = "120000"
    request = case.request(corrected)
    request["mutations"][0].update(op="replace", expected_seq=1)
    response = case.app.accounting_apply(request)
    assert response["reading"] == "preparing", response
    assert formal_periods(calls[-1], "business_periods") == ["2026-01"]
    assert formal_periods(calls[-1], "posted_periods") == ["2026-01"]
    assert calls[-1].formal_changes[0].result_kinds == ("operations.expense",)
    assert calls[-1].formal_changes[0].account_codes
    assert case.kernel.reading.page("quarterly_report", scope(case), {}).state == "preparing"
    changed = ready(case)
    assert changed.version != initial.version
    assert (
        changed.payload["snapshot"]["profit_statement"]["32"]["current"]["amount_fen"] == "-120000"
    )
    count = len(calls)
    assert case.app.accounting_apply(request)["changes"] == "none"
    assert len(calls) == count and ready(case).version == changed.version
    future = case.app.accounting_apply(case.request(case.expense("9000", "2026-04")))
    assert future["reading"] == "preparing"
    assert formal_periods(calls[-1], "posted_periods") == ["2026-04"]
    assert case.kernel.reading.page("quarterly_report", scope(case), {}).state == "ready"
    assert ready(case).version == changed.version


def test_management_and_note_notifications_do_not_refresh_financial_component(case, monkeypatch):
    case.seed()
    initial = ready(case)
    calls = spy(case, monkeypatch)
    renamed = case.app.accounting_apply(
        case.request(
            objects=[
                {
                    "object_id": case.party,
                    "kind": "organization",
                    "expected_seq": 1,
                    "name": "合成供应商新名称",
                    "data": {},
                }
            ]
        )
    )
    assert renamed["accounting"] == renamed["reading"] == "unchanged", renamed
    assert calls[-1].objects == ((case.party, "organization"),)
    assert not calls[-1].formal_changes
    extra = source("合成补充来源只补资料，不改变已明确的1000元维修费。")
    request = case.request(sources=[extra])
    count = len(calls)
    result = case.app.accounting_apply(request)
    assert result["accounting"] == result["reading"] == "unchanged", result
    assert len(calls) == count
    request = case.request(
        notes=[
            note("company", "公司经营维修。", case.source_id),
            note("period:2026-01", "一月维修费1000元。", case.source_id, basis=case.note_head()),
        ]
    )
    assert case.app.accounting_apply(request)["completion"] == "complete"
    assert calls[-1].note_scopes == ("company", "period:2026-01")
    unchanged_company = note("company", "公司经营维修。", case.source_id, seq=1)
    revised_month = note(
        "period:2026-01", "一月应付维修费1000元。", case.source_id, seq=1, basis=case.note_head()
    )
    assert (
        case.app.accounting_apply(case.request(notes=[unchanged_company, revised_month]))[
            "completion"
        ]
        == "complete"
    )
    assert calls[-1].note_scopes == ("period:2026-01",)
    assert not calls[-1].formal_changes
    assert ready(case).version == initial.version


def test_closed_baseline_is_not_refreshed_by_a_later_quarter_correction(case, monkeypatch):
    fact = case.seed()
    ready(case)
    case.close(case.check())
    frozen = ready(case)
    ready(case, 2)
    calls = spy(case, monkeypatch)
    fact["data"]["amount_fen"] = "80000"
    request = case.request(fact, correction_period="2026-04")
    request["mutations"][0].update(op="replace", expected_seq=1)
    response = case.app.accounting_apply(request)
    assert response["completion"] == "complete" and response["reading"] == "preparing", response
    assert formal_periods(calls[-1], "business_periods") == ["2026-01"]
    assert formal_periods(calls[-1], "posted_periods") == ["2026-04"]
    assert case.kernel.reading.page("quarterly_report", scope(case), {}).state == "ready"
    assert ready(case).version == frozen.version
    assert (
        ready(case).payload["snapshot"]["profit_statement"]["32"]["current"]["amount_fen"]
        == "-100000"
    )


def test_close_marks_the_newly_covered_range_once_and_retry_reuses_it(case, monkeypatch):
    case.seed()
    ready(case)
    calls = spy(case, monkeypatch)
    closed = case.close(case.check("2026-03"))
    assert closed["state"] == "closed"
    assert ranges(calls[-1].closure_ranges) == [("2026-01", "2026-03")]
    assert not calls[-1].formal_changes
    assert case.kernel.reading.page("quarterly_report", scope(case), {}).state == "preparing"
    ready(case)
    count = len(calls)
    assert case.close(case.check("2026-03"))["state"] == "closed"
    assert len(calls) == count


def test_read_target_failure_rolls_back_fact_adoption_and_target_together(case, monkeypatch):
    fact = case.seed()
    initial = ready(case)
    before = case.get(fact["business_id"])
    original = case.kernel.reading.mark_targets

    def fail_after_mark(impact, unit):
        assert original(impact, unit)
        raise ContractError("invalid_operation", "合成读取目标写入失败。")

    monkeypatch.setattr(case.kernel.reading, "mark_targets", fail_after_mark)
    corrected = deepcopy(fact)
    corrected["data"]["amount_fen"] = "120000"
    request = case.request(corrected)
    request["mutations"][0].update(op="replace", expected_seq=1)
    response = case.app.accounting_apply(request)
    assert response["code"] == "invalid_operation", response
    assert case.get(fact["business_id"]) == before
    assert ready(case).version == initial.version


def test_relation_closure_above_one_public_reference_batch_is_complete(case, monkeypatch):
    case.seed(expense=False)
    evidence = source(
        "合成批次：同一机构501份独立人民币短期借款约定，尚未提款，固定年利率3.6%，"
        "Actual/360，2026-12-31到期还本。"
    )
    contracts = [
        {
            "business_id": str(uuid4()),
            "kind": "financing.loan_contract",
            "period": "2026-01",
            "evidence_ids": [evidence["source_id"]],
            "data": {
                "lender_id": case.party,
                "lender_type": "licensed_financial_institution",
                "currency": "CNY",
                "rate_type": "fixed",
                "annual_rate": "0.036",
                "day_count": "actual_360",
                "matures_on": "2026-12-31",
                "repayment": "bullet",
                "term": "short_term",
            },
        }
        for _ in range(501)
    ]
    first = case.app.accounting_apply(case.request(*contracts[:500], sources=[evidence]))
    assert first["completion"] == "complete", first
    assert case.app.accounting_apply(case.request(contracts[-1]))["completion"] == "complete"
    batches = []
    original = case.kernel.ledger.get_result_refs

    def observe(ids, read):
        assert len(ids) <= 500
        batches.append(tuple(ids))
        return original(ids, read)

    monkeypatch.setattr(case.kernel.ledger, "get_result_refs", observe)
    response = case.app.accounting_apply(
        case.request(
            objects=[
                {
                    "object_id": case.party,
                    "kind": "organization",
                    "expected_seq": 1,
                    "name": "合成贷款机构新名称",
                    "data": {},
                }
            ]
        )
    )
    assert response["completion"] == "complete" and response["accounting"] == "unchanged", response
    assert [len(batch) for batch in batches] == [500, 1]
    assert {identity for batch in batches for identity in batch} == {
        fact["business_id"] for fact in contracts
    }
