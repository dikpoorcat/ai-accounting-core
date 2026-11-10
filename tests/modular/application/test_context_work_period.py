"""The chosen accounting month is a read selection, not company-wide state."""

from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError

from ._working_case import case as case


def test_work_month_selects_its_materials_without_advancing_or_remembering_selection(case):
    case.seed()
    assert (
        case.app.accounting_apply(case.request(case.expense(period="2026-03")))["completion"]
        == "complete"
    )
    notes = [
        {
            "scope": "period:" + month,
            "expected_seq": 0,
            "expected_basis_head": case.note_head(month),
            "text": month + "当前办理资料。",
            "evidence_ids": [case.source_id],
        }
        for month in ("2026-01", "2026-02", "2026-04")
    ]
    assert case.app.accounting_apply(case.request(notes=notes))["completion"] == "complete"
    with case.kernel.storage.read_unit(case.company) as read:
        before_head, before_scope = read.head, case.app.work_scope(read)

    for selected, material_month in (
        ("2026-02", "2026-02"),
        ("2026-01", "2026-01"),
        ("2026-04", "2026-04"),
        (None, "2026-01"),
    ):
        current = case.context(**({"work_period": selected} if selected else {}))
        assert current["state"] == "selected"
        assert current["work_period"] == selected
        assert current["company_identity"]["working_period_to"] == "2026-03"
        assert current["open_periods"] == ["2026-01", "2026-02", "2026-03"]
        assert [item["text"] for item in current["notes"]] == [material_month + "当前办理资料。"]
        assert [item["scope"] for item in current["note_basis_heads"]] == [
            "period:" + material_month
        ]
        assert [item["scope"] for item in current["issue_scopes"]] == [
            "company",
            "period:" + material_month,
        ]

    with case.kernel.storage.read_unit(case.company) as read:
        assert read.head == before_head
        assert case.app.work_scope(read) == before_scope


@pytest.mark.parametrize("override", ["note_scopes", "issue_scopes"])
def test_explicit_scopes_override_only_their_materials_and_not_the_work_month(case, override):
    case.seed(expense=False)
    current = case.context(work_period="2026-02", **{override: ["company", "period:2026-01"]})
    assert current["work_period"] == "2026-02"
    assert [item["scope"] for item in current["note_basis_heads"]] == [
        "period:2026-01" if override == "note_scopes" else "period:2026-02"
    ]
    assert [item["scope"] for item in current["issue_scopes"]] == [
        "company",
        "period:2026-01" if override == "issue_scopes" else "period:2026-02",
    ]


def test_empty_scopes_do_not_load_monthly_materials(case, monkeypatch):
    case.seed(expense=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("Explicit empty scopes must not read month reports or issues")

    monkeypatch.setattr(case.kernel.ledger, "financial_snapshot", forbidden)
    monkeypatch.setattr(case.kernel.records, "current_issues", forbidden)
    current = case.context(work_period="2026-02", note_scopes=[], issue_scopes=[])
    assert current["work_period"] == "2026-02"
    assert current["notes"] == current["issues"] == current["issue_scopes"] == []
    assert current["note_basis_heads"] == []


def test_closed_month_can_be_selected_without_changing_closure_or_default_candidate(case):
    case.seed(expense=False)
    assert case.close(case.check())["state"] == "closed"
    with case.kernel.storage.read_unit(case.company) as read:
        before_head = read.head
    closed = case.context(work_period="2026-01")
    assert closed["work_period"] == "2026-01"
    assert closed["closed_periods"] == ["2026-01"]
    assert closed["open_periods"] == ["2026-02"]
    assert [item["scope"] for item in closed["note_basis_heads"]] == ["period:2026-01"]
    discovery = case.context()
    assert discovery["work_period"] is None
    assert [item["scope"] for item in discovery["issue_scopes"]] == [
        "company",
        "period:2026-02",
    ]
    with case.kernel.storage.read_unit(case.company) as read:
        assert read.head == before_head
        assert case.app.work_scope(read).closed_through == "2026-01"


@pytest.mark.parametrize("scope", ["company", "custom:follow_up"])
def test_selected_month_keeps_other_period_issues_in_the_complete_replace_scope(case, scope):
    case.seed(expense=False)
    issues = [
        {
            "issue_id": str(uuid4()),
            "scope": scope,
            "period": month,
            "evidence_ids": [case.source_id],
            "missing_fields": [],
            "question": month + "外部申报尚待办理。",
            "assignee": "owner",
            "action": "external_filing",
        }
        for month in ("2026-01", "2026-02")
    ]
    issues.sort(key=lambda item: item["issue_id"])
    saved = case.app.accounting_apply(
        case.request(unresolved=issues, unresolved_scope=scope, expected_scope_head=0)
    )
    assert saved["completion"] == "complete", saved
    current = case.context(work_period="2026-01", note_scopes=[], issue_scopes=[scope])
    assert current["issues"] == issues
    selected_scope = current["issue_scopes"][0]
    assert selected_scope == {"scope": scope, "head": 1, "issues": issues}

    remaining = [item for item in selected_scope["issues"] if item["period"] != "2026-01"]
    result = case.app.accounting_apply(
        case.request(
            unresolved=remaining, unresolved_scope=scope, expected_scope_head=selected_scope["head"]
        )
    )
    assert result["completion"] == "complete", result
    later = case.context(work_period="2026-02", note_scopes=[], issue_scopes=[scope])
    assert later["issues"] == remaining
    assert len(remaining) == 1 and remaining[0]["period"] == "2026-02"
    assert later["issue_scopes"] == [{"scope": scope, "head": 2, "issues": remaining}]


def test_work_month_without_a_company_fails_before_company_discovery(case):
    with pytest.raises(ContractError) as error:
        case.app.accounting_context({"work_period": "2026-01"})
    assert error.value.code == "invalid_input"
