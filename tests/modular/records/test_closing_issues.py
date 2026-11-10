"""Closing obligations retain their own month and complete replacement scope."""

from uuid import uuid4

import pytest
from test_period_reset import issue, save_work, store

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.records.public import RecordsChange, RecordsContext


def test_closing_issues_union_deduplicates_and_paginates_without_unperioded_work(records_case):
    storage, (company, _), records = records_case
    evidence = store(records_case, "跨期工作安排", "2026-09")
    source = evidence["source_id"]
    outside = issue("company", source, "2026-11")
    no_month = issue("company", source)
    target = issue("company", source, "2026-09")
    cross = issue("company", source, "2026-10") | {
        "action": "external_filing",
        "required_before_close": "2026-09",
    }
    both = issue("company", source, "2026-09") | {
        "action": "external_filing",
        "required_before_close": "2026-09",
    }
    save_work(records_case, issues=(outside, no_month, target, cross, both), scope="company")
    with storage.read_unit(company.company_id) as read:
        first = records.period_closing_issues("2026-09", "2026-09", read, limit=1)
        assert first.total == 3 and first.next_cursor
        second = records.period_closing_issues(
            "2026-09", "2026-09", read, limit=2, cursor=first.next_cursor
        )
        actual = first.items + second.items
        assert {item["issue_id"] for item in actual} == {
            item["issue_id"] for item in (target, cross, both)
        }
        assert second.next_cursor is None
        assert records.current_issues("company", read)["issues"] == sorted(
            (outside, no_month, target, cross, both), key=lambda item: item["issue_id"]
        )
    outside = outside | {"question": "范围外事项的说明更新，不改变本次受核集合。"}
    with storage.write_unit(company.company_id) as unit:
        records.apply_current(
            records.plan_change(
                RecordsChange(
                    unresolved=(outside, no_month, target, cross, both),
                    unresolved_scope="company",
                    expected_scope_head=1,
                ),
                RecordsContext(unit, str(uuid4())),
            ),
            unit,
        )
    with storage.read_unit(company.company_id) as read:
        resumed = records.period_closing_issues(
            "2026-09", "2026-09", read, cursor=first.next_cursor
        )
        assert resumed.items == second.items
        assert resumed.scope_heads == {"company": 2}
    with storage.write_unit(company.company_id) as unit:
        changed = cross | {"required_before_close": "2026-10"}
        records.apply_current(
            records.plan_change(
                RecordsChange(
                    unresolved=(outside, no_month, target, changed, both),
                    unresolved_scope="company",
                    expected_scope_head=2,
                ),
                RecordsContext(unit, str(uuid4())),
            ),
            unit,
        )
    with storage.read_unit(company.company_id) as read, pytest.raises(ContractError) as error:
        records.period_closing_issues("2026-09", "2026-09", read, cursor=first.next_cursor)
    assert error.value.code == "snapshot_changed"


@pytest.mark.parametrize("cancelled", ["2026-09", "2026-10"])
def test_month_cancel_preserves_cross_period_obligation_and_its_sources(records_case, cancelled):
    storage, (company, _), records = records_case
    evidence = store(records_case, "季度外部申报安排", "2026-10")
    cross = issue("company", evidence["source_id"], "2026-10") | {
        "action": "external_filing",
        "required_before_close": "2026-09",
    }
    local = issue("company", evidence["source_id"], cancelled)
    save_work(records_case, issues=(cross, local), scope="company")
    with storage.write_unit(company.company_id) as unit:
        records.clear_period_issues(cancelled, str(uuid4()), unit)
    with storage.read_unit(company.company_id) as read:
        assert records.current_issues("company", read) == {
            "scope": "company",
            "head": 2,
            "issues": [cross],
        }
        assert records.source_discard_protection(
            (evidence["source_id"],), read, retained_source_ids=(), protected_paths=()
        ) == (evidence["source_id"],)


@pytest.mark.parametrize("impact_month", [None, "2026-01", "2026-02"])
def test_known_accounting_period_requires_explicit_impact_without_copying_it(
    records_case, impact_month
):
    storage, (company, _), records = records_case
    evidence = store(records_case, "已知一月费用缺少金额，实际影响月由会计核查。", "2026-01")
    missing = issue("company", evidence["source_id"]) | {
        "action": "clarify_reality",
        "missing_fields": ["/data/amount_fen"],
        "known_fact": {
            "kind": "operations.expense",
            "period": "2026-01",
            "data": {
                "creditor_id": str(uuid4()),
                "creditor_kind": "supplier",
                "expense_class": "administration",
            },
        },
    }
    if impact_month is not None:
        missing["period"] = impact_month
    if impact_month is None:
        with pytest.raises(ContractError) as error:
            save_work(records_case, issues=(missing,), scope="company")
        assert error.value.code == "invalid_input"
        detail = error.value.fact_issues[0]
        assert detail["code"] == "issue_period_required"
        assert detail["field"] == "/unresolved/0/period"
        assert detail["reusable_sources"] == [{"source_id": evidence["source_id"]}]
        with storage.read_unit(company.company_id) as read:
            assert records.current_issues("company", read)["issues"] == []
    else:
        save_work(records_case, issues=(missing,), scope="company")
        with storage.read_unit(company.company_id) as read:
            assert records.period_closing_issues(impact_month, impact_month, read).items == (
                missing,
            )


def test_no_known_period_or_no_accounting_gap_does_not_invent_monthly_blocker(records_case):
    storage, (company, _), records = records_case
    evidence = store(records_case, "现实事项仍待会计查清归属。")
    undated = issue("company", evidence["source_id"]) | {"missing_fields": ["/data/amount_fen"]}
    external = issue("company", evidence["source_id"]) | {
        "action": "external_payment",
        "missing_fields": [],
        "known_fact": {"kind": "operations.expense", "period": "2026-01", "data": {}},
    }
    save_work(records_case, issues=(undated, external), scope="company")
    with storage.read_unit(company.company_id) as read:
        assert records.period_closing_issues("2026-01", "2026-01", read).items == ()
        assert len(records.current_issues("company", read)["issues"]) == 2
