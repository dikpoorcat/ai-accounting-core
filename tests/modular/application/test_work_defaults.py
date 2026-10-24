"""Work defaults guide the AI without changing facts, tax periods or owner notes."""

from ._working_case import case as case
from .test_notes_context import note


def test_context_exposes_payroll_work_default_without_creating_any_business(case):
    with case.kernel.storage.read_unit(case.company) as read:
        before = read.head
    context = case.context(note_scopes=[], issue_scopes=[])
    assert context["work_defaults"] == {
        "payroll": {
            "accrual_period": "service_month",
            "planned_payment_period": "following_month",
        }
    }
    assert context["notes"] == []
    assert context["open_periods"] == ["2026-01"]
    with case.kernel.storage.read_unit(case.company) as read:
        assert read.head == before


def test_owner_work_arrangement_stays_available_and_is_not_parsed_into_formal_facts(case):
    case.seed(expense=False)
    text = "本公司工资按当月发放；本月因假期提前一天，具体付款仍以流水为准。"
    saved = case.app.accounting_apply(case.request(notes=[note("company", text, case.source_id)]))
    assert saved["completion"] == "complete", saved
    context = case.context()
    assert context["notes"][0]["text"] == text
    assert context["work_defaults"]["payroll"]["planned_payment_period"] == "following_month"
    assert context["closed_periods"] == []
