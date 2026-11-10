"""Work arrangements persist independently from accounting explanations."""

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.records.public import open_standard_bundle

from ._working_case import case as case


def test_accounting_change_invalidates_only_financial_note_and_keeps_work_arrangements(case):
    case.seed(expense=False)
    work = {
        "scope": "work:2026-01",
        "expected_seq": 0,
        "text": "本月不用代发表；已同意在社保反馈前先完成已说明的后续核算。",
        "evidence_ids": [case.source_id],
    }
    financial = {
        "scope": "period:2026-01",
        "expected_seq": 0,
        "expected_basis_head": case.note_head(),
        "text": "期初及已登记金额核对完成。",
        "evidence_ids": [case.source_id],
    }
    assert (
        case.app.accounting_apply(case.request(notes=[work, financial]))["completion"] == "complete"
    )
    before = case.context(work_period="2026-01")["notes"]
    original_work = next(note for note in before if note["scope"] == work["scope"])
    assert case.app.accounting_apply(case.request(case.expense()))["completion"] == "complete"
    after = {note["scope"]: note for note in case.context(work_period="2026-01")["notes"]}
    assert after[work["scope"]] == original_work
    assert after[financial["scope"]]["state"] == "needs_refresh"
    with case.kernel.storage.read_unit(case.company) as read:
        root = case.kernel.storage.company_metadata(read).company_root
    bundle = open_standard_bundle(root / "standard/current.json")
    assert bundle.notes == ({key: work[key] for key in ("scope", "text", "evidence_ids")},)


def test_discovery_returns_latest_current_work_without_selecting_it_and_scopes_override(
    case, monkeypatch
):
    case.seed(expense=False)
    notes = [
        {
            "scope": "work:" + month,
            "expected_seq": 0,
            "text": month + "本月办理安排",
            "evidence_ids": [case.source_id],
        }
        for month in ("2026-01", "2026-02")
    ]
    assert case.app.accounting_apply(case.request(notes=notes))["completion"] == "complete"
    discovered = case.context()
    assert discovered["work_period"] is None
    assert [note["scope"] for note in discovered["notes"]] == ["work:2026-02"]
    assert [note["scope"] for note in case.context(work_period="2026-01")["notes"]] == [
        "work:2026-01"
    ]
    assert discovered["issue_scopes"] == [
        {"scope": "company", "head": 0, "issues": []},
        {"scope": "period:2026-01", "head": 0, "issues": []},
    ]

    def forbidden(*args, **kwargs):
        raise AssertionError("显式说明范围不能额外读取最近工作安排。")

    monkeypatch.setattr(case.kernel.records, "latest_work_note", forbidden)
    assert case.context(note_scopes=[])["notes"] == []
    assert [note["scope"] for note in case.context(note_scopes=["work:2026-01"])["notes"]] == [
        "work:2026-01"
    ]


def test_work_note_clear_preserves_concurrency_and_removes_final_document_reference(case):
    case.seed(expense=False)
    note = {
        "scope": "work:2026-01",
        "expected_seq": 0,
        "text": "本月明确不用代发文件。",
        "evidence_ids": [case.source_id],
    }
    assert case.app.accounting_apply(case.request(notes=[note]))["completion"] == "complete"
    clear = {"scope": note["scope"], "expected_seq": 1, "text": "", "evidence_ids": []}
    assert case.app.accounting_apply(case.request(notes=[clear]))["completion"] == "complete"
    assert case.app.accounting_apply(case.request(notes=[clear]))["completion"] == "complete"
    current = case.context(note_scopes=[note["scope"]])["notes"][0]
    assert current["state"] == "needs_refresh" and current["text"] is None
    assert current["seq"] == 2 and current["basis_head"] is None and current["evidence_ids"] == []
    assert case.context()["notes"] == []
    failed = case.app.accounting_apply(case.request(notes=[note | {"text": "旧请求不能覆盖清空"}]))
    assert failed["completion"] == "not_saved" and failed["error"] == "conflict"
    with pytest.raises(ContractError):
        case.app.accounting_context({"company_id": case.company, "note_scopes": ["work:invalid"]})
