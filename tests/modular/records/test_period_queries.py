"""Read complete relevant sets through bounded public pages, without scope guesses."""

from __future__ import annotations

from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.records.public import RecordsChange, RecordsContext


def _notes(case, months, *, company=None):
    storage, companies, records = case
    company = company or companies[0]
    for offset in range(0, len(months), 100):
        batch = months[offset : offset + 100]
        values = tuple(
            {
                "scope": "period:" + month,
                "expected_seq": 0,
                "expected_basis_head": "basis:" + month,
                "text": "该月份的当前正确说明",
                "evidence_ids": [],
            }
            for month in batch
        )
        with storage.write_unit(company.company_id) as unit:
            plan = records.plan_change(
                RecordsChange(notes=values),
                RecordsContext(
                    unit,
                    str(uuid4()),
                    {value["scope"]: value["expected_basis_head"] for value in values},
                ),
            )
            records.apply_current(plan, unit)


def _issue(scope, period="2026-01", *, question="请核查原始资料"):
    value = {
        "issue_id": str(uuid4()),
        "scope": scope,
        "question": question,
        "missing_fields": ["/data/amount_fen"],
        "assignee": "ai",
        "action": "professional_check",
        "evidence_ids": [],
    }
    if period is not None:
        value["period"] = period
    return value


def _issues(case, scope, values, *, head=0, company=None):
    storage, companies, records = case
    company = company or companies[0]
    with storage.write_unit(company.company_id) as unit:
        records.apply_current(
            records.plan_change(
                RecordsChange(
                    unresolved=tuple(values), unresolved_scope=scope, expected_scope_head=head
                ),
                RecordsContext(unit, str(uuid4())),
            ),
            unit,
        )


def test_notes_page_only_existing_months_in_range_with_no_full_company_loop(records_case):
    storage, (company, _), records = records_case
    _notes(records_case, ["2025-12", "2026-01", "2026-03", "2026-12", "2027-01"])
    with storage.read_unit(company.company_id) as read:
        page = records.period_notes("2026-01", "2026-03", read, limit=1)
        assert page.total == 2 and page.items[0]["scope"] == "period:2026-01"
        next_page = records.period_notes(
            "2026-01", "2026-03", read, limit=1, cursor=page.next_cursor
        )
        assert next_page.total == 2 and next_page.items[0]["scope"] == "period:2026-03"
        assert next_page.next_cursor is None
        assert records.period_notes("2026-04", "2026-11", read).items == ()


def test_more_than_five_hundred_notes_are_complete_through_pages(records_case):
    storage, (company, _), records = records_case
    months = [f"{2020 + index // 12:04}-{index % 12 + 1:02}" for index in range(503)]
    _notes(records_case, months)
    found, cursor = [], None
    with storage.read_unit(company.company_id) as read:
        while True:
            page = records.period_notes(months[0], months[-1], read, limit=97, cursor=cursor)
            assert page.total == 503 and len(page.items) <= 97
            found.extend(item["scope"] for item in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
    assert found == ["period:" + month for month in months]


def test_month_note_invalidation_is_returned_and_relevant_change_invalidates_cursor(records_case):
    storage, (company, _), records = records_case
    _notes(records_case, ["2026-01", "2026-02"])
    with storage.read_unit(company.company_id) as read:
        before = records.period_notes("2026-01", "2026-02", read, limit=1)
    with storage.write_unit(company.company_id) as unit:
        records.invalidate_notes({"period:2026-02": "new-basis"}, str(uuid4()), unit)
    with storage.read_unit(company.company_id) as read:
        current = records.period_notes("2026-02", "2026-02", read)
        assert current.items[0]["state"] == "needs_refresh"
        assert current.items[0]["seq"] == 2 and current.items[0]["text"] is None
        with pytest.raises(ContractError) as changed:
            records.period_notes("2026-01", "2026-02", read, cursor=before.next_cursor)
        assert changed.value.code == "snapshot_changed"


def test_notes_cursor_survives_unrelated_records_and_company_head_advancement(records_case):
    storage, (company, _), records = records_case
    _notes(records_case, ["2026-01", "2026-02"])
    with storage.read_unit(company.company_id) as read:
        first = records.period_notes("2026-01", "2026-02", read, limit=1)
        old_head = read.head
    _notes(records_case, ["2027-01"])
    _issues(records_case, "unrelated", [_issue("unrelated", "2027-01")])
    with storage.read_unit(company.company_id) as read:
        assert read.head == old_head + 2
        page = records.period_notes("2026-01", "2026-02", read, cursor=first.next_cursor)
        assert [item["scope"] for item in page.items] == ["period:2026-02"]


def test_issue_pages_cover_all_custom_scopes_and_unperioded_items_without_truncation(records_case):
    storage, (company, _), records = records_case
    alpha = [_issue("自定义甲", "2026-01") for _ in range(300)]
    alpha += [_issue("自定义甲", "2027-01") for _ in range(3)]
    alpha.append(_issue("自定义甲", None))
    beta = [_issue("supplier-work", "2026-02") for _ in range(301)]
    beta += [_issue("supplier-work", "2025-12"), _issue("supplier-work", None)]
    unknown = [_issue("归属待定", None) for _ in range(2)]
    for scope, values in (("自定义甲", alpha), ("supplier-work", beta), ("归属待定", unknown)):
        _issues(records_case, scope, values)
    expected = {
        item["issue_id"]
        for item in alpha + beta + unknown
        if "period" not in item or "2026-01" <= item["period"] <= "2026-02"
    }
    found, cursor, heads = [], None, {}
    with storage.read_unit(company.company_id) as read:
        while True:
            page = records.period_issues("2026-01", "2026-02", read, limit=137, cursor=cursor)
            assert page.total == len(expected) == 605
            assert len(page.items) <= 137
            found.extend(item["issue_id"] for item in page.items)
            heads.update(page.scope_heads)
            cursor = page.next_cursor
            if cursor is None:
                break
        assert len(records.current_issues("自定义甲", read)["issues"]) == 304
    assert set(found) == expected and len(found) == len(expected)
    assert heads == {"自定义甲": 1, "supplier-work": 1, "归属待定": 1}


def test_issue_scope_head_is_full_but_cursor_tracks_only_selected_content(records_case):
    storage, (company, _), records = records_case
    selected = [_issue("mixed", "2026-01") for _ in range(2)]
    future = _issue("mixed", "2027-01")
    _issues(records_case, "mixed", selected + [future])
    with storage.read_unit(company.company_id) as read:
        first = records.period_issues("2026-01", "2026-01", read, limit=1)
        assert first.scope_heads == {"mixed": 1}
        with pytest.raises(TypeError):
            first.scope_heads["mixed"] = 99
    _issues(records_case, "mixed", selected + [future | {"question": "仅修改未来无关事项"}], head=1)
    with storage.read_unit(company.company_id) as read:
        second = records.period_issues("2026-01", "2026-01", read, cursor=first.next_cursor)
        assert second.total == 2 and second.scope_heads == {"mixed": 2}
        assert {item["issue_id"] for item in first.items + second.items} == {
            item["issue_id"] for item in selected
        }
        assert len(records.current_issues("mixed", read)["issues"]) == 3
    changed = selected[0] | {"question": "本月实际缺项已改变"}
    _issues(records_case, "mixed", [changed, selected[1], future], head=2)
    with storage.read_unit(company.company_id) as read:
        with pytest.raises(ContractError) as stale:
            records.period_issues("2026-01", "2026-01", read, cursor=first.next_cursor)
        assert stale.value.code == "snapshot_changed"


@pytest.mark.parametrize("collection", ["notes", "issues"])
def test_cursors_do_not_cross_companies_or_query_ranges(records_case, collection):
    storage, (company, other), records = records_case
    for current in (company, other):
        if collection == "notes":
            _notes(records_case, ["2026-01", "2026-02"], company=current)
        else:
            _issues(
                records_case,
                "work",
                [_issue("work"), _issue("work")],
                company=current,
            )
    query = records.period_notes if collection == "notes" else records.period_issues
    with storage.read_unit(company.company_id) as read:
        first = query("2026-01", "2026-02", read, limit=1)
        with pytest.raises(ContractError):
            query("2026-01", "2026-03", read, cursor=first.next_cursor)
    with storage.read_unit(other.company_id) as read:
        with pytest.raises(ContractError) as wrong_company:
            query("2026-01", "2026-02", read, cursor=first.next_cursor)
        assert wrong_company.value.code == "snapshot_changed"


@pytest.mark.parametrize("collection", ["notes", "issues"])
@pytest.mark.parametrize("limit", [0, -1, True, 501, 1.5])
def test_invalid_page_sizes_are_not_coerced(records_case, collection, limit):
    storage, (company, _), records = records_case
    query = records.period_notes if collection == "notes" else records.period_issues
    with storage.read_unit(company.company_id) as read:
        with pytest.raises(ContractError) as invalid:
            query("2026-01", "2026-02", read, limit=limit)
        assert invalid.value.code == "invalid_input"


@pytest.mark.parametrize("collection", ["notes", "issues"])
@pytest.mark.parametrize("cursor", ["invalid", "", "====", 7, []])
def test_malformed_cursor_is_rejected(records_case, collection, cursor):
    storage, (company, _), records = records_case
    query = records.period_notes if collection == "notes" else records.period_issues
    with storage.read_unit(company.company_id) as read:
        with pytest.raises(ContractError) as invalid:
            query("2026-01", "2026-02", read, cursor=cursor)
        assert invalid.value.code == "invalid_input"


def test_empty_filtered_set_has_no_fabricated_scope_or_note(records_case):
    storage, (company, _), records = records_case
    _issues(records_case, "future-only", [_issue("future-only", "2027-01")])
    with storage.read_unit(company.company_id) as read:
        notes = records.period_notes("2026-01", "2026-12", read)
        issues = records.period_issues("2026-01", "2026-12", read)
        assert notes.items == () and notes.total == 0 and notes.next_cursor is None
        assert issues.items == () and issues.total == 0 and issues.next_cursor is None
        assert issues.scope_heads == {}


def test_filtered_payload_integrity_is_checked_from_saved_digests(records_case):
    storage, (company, _), records = records_case
    _notes(records_case, ["2026-01"])
    _issues(records_case, "work", [_issue("work")])
    with storage.write_unit(company.company_id) as unit:
        repository = unit.repository("records")
        repository._session.execute("UPDATE company_note SET note_text='broken text'")
        repository._session.execute("UPDATE current_issue SET current_question='broken question'")
    with storage.read_unit(company.company_id) as read:
        for query in (records.period_notes, records.period_issues):
            with pytest.raises(ContractError) as corrupt:
                query("2026-01", "2026-01", read)
            assert corrupt.value.code == "storage_corrupt"
