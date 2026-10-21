"""The main brief and its bounded expansions share one public month version."""

from copy import deepcopy
from uuid import uuid4

from tests.modular.application._working_case import case as case
from tests.modular.reading._support import source

PATH = "/api/dashboard/brief"


def test_replaced_voucher_token_is_stale_even_with_current_page_version(case):
    expense = case.seed()
    before = ready(case)
    query = {"company_id": case.company, "period": "2026-01"}
    with case.kernel.dashboard.get(
        PATH, query | {"section": "vouchers", "expected_version": before["snapshot_version"]}
    ) as response:
        old = response.body["data"]["collections"]["vouchers"]["items"][0]
    expense["data"]["amount_fen"] = "90000"
    result = case.app.accounting_apply(
        case.request(mutations=[{"op": "replace", "expected_seq": 1, "fact": expense}])
    )
    assert result["completion"] == "complete", result
    after = ready(case)
    latest = query | {"expected_version": after["snapshot_version"]}
    with case.kernel.dashboard.get(
        PATH, latest | {"voucher_version_id": old["voucher_version_id"]}
    ) as response:
        assert response.status == 409, response.body
        assert response.body["code"] == "dashboard_snapshot_changed"
    with case.kernel.dashboard.get(PATH, latest | {"voucher_number": old["number"]}) as response:
        assert response.status == 200, response.body
        assert response.body["data"]["focused_voucher"]["business_amount_fen"] == "90000"


def ready(case, month="2026-01"):
    query = {"company_id": case.company, "period": month}
    with case.kernel.dashboard.get(PATH, query) as response:
        assert response.status == 200, response.body
        assert response.body["state"] in {"preparing", "ready"}, response.body
    case.kernel.reading.run_pending(case.company)
    with case.kernel.dashboard.get(PATH, query) as response:
        assert response.status == 200, response.body
        assert response.body["state"] == "ready", response.body
        return response.body["response"]


def test_complete_brief_and_sections_do_not_repeat_owner_reads(case, monkeypatch):
    case.seed()
    initial = ready(case)
    data = initial["data"]
    assert initial["scope"] == "overview"
    assert data["position"] == {
        "month_revenue_fen": "0",
        "month_expense_fen": "100000",
        "month_result_fen": "-100000",
        "complete": True,
    }
    assert data["financial_position"]["liability_calculation"] == {
        "current_fen": "100000",
        "non_current_fen": "0",
    }
    assert data["financial_position"]["equation_valid"] is True
    assert data["long_term_assets"]["net_fen"] == "0"
    assert data["workforce_cost"]["total_fen"] == "0"
    assert data["workforce_cost"]["has_activity"] is False
    assert data["open_items"]["payable_fen"] == "100000"
    assert set(data["collections"]) == {"activity", "open_items"}

    def forbidden(*args, **kwargs):
        raise AssertionError("热简报不应重复生成owner全量月数据")

    for owner, method in (
        (case.kernel.ledger, "financial_summary"),
        (case.kernel.money, "account_activity"),
        (case.kernel.reading.people, "remuneration_activity"),
        (case.kernel.reading.assets, "asset_activity"),
    ):
        monkeypatch.setattr(owner, method, forbidden)
    query = {
        "company_id": case.company,
        "period": "2026-01",
        "expected_version": initial["snapshot_version"],
    }
    for section in ("activity", "open_items", "vouchers"):
        with case.kernel.dashboard.get(PATH, query | {"section": section}) as response:
            assert response.status == 200, response.body
            assert response.body["scope"] == section
            assert set(response.body["data"]) == {"collections"}
            assert set(response.body["data"]["collections"]) == {section}
    group = data["collections"]["activity"]["items"][0]
    with case.kernel.dashboard.get(
        "/api/dashboard/brief-group",
        query | {"section": "activity", "group_key": group["group_key"]},
    ) as response:
        assert response.status == 200, response.body
        assert response.body["data"]["collections"]["members"]["items"]
        vouchers = response.body["data"]["collections"]["vouchers"]["items"]
        assert len(vouchers) == 1 and vouchers[0]["number"] == "1"
    with case.kernel.dashboard.get(PATH, query | {"voucher_number": "1"}) as response:
        assert response.status == 200, response.body
        assert response.body["data"]["focused_voucher"]["number"] == "1"


def test_unknown_opening_keeps_known_expense_and_unknown_balances(case):
    value = case.expense()
    saved = case.app.accounting_apply(
        case.request(
            value,
            sources=[source("unknown-opening") | {"source_id": case.source_id}],
            objects=[
                {
                    "object_id": case.party,
                    "kind": "organization",
                    "name": "合成供应商",
                    "expected_seq": 0,
                    "data": {},
                }
            ],
        )
    )
    assert saved["completion"] == "complete", saved
    actual = ready(case)["data"]
    assert actual["position"]["month_expense_fen"] == "100000"
    assert actual["financial_position"]["assets_fen"] is None
    assert actual["financial_position"]["equation_valid"] is None
    assert actual["long_term_assets"]["net_fen"] is None
    assert actual["funds_overview"]["total_fen"] is None
    assert actual["open_items"]["complete"] is False
    assert actual["open_items"]["payable_fen"] is None
    assert actual["collections"]["open_items"]["items"][0]["outstanding_fen"] == "100000"


def test_real_correction_invalidates_precise_group_version_and_closed_month_stays_frozen(case):
    original = case.seed()
    assert case.close(case.check())["state"] == "closed"
    january = ready(case)
    corrected = deepcopy(original)
    corrected["data"]["amount_fen"] = "80000"
    saved = case.app.accounting_apply(
        case.request(
            mutations=[{"op": "replace", "expected_seq": 1, "fact": corrected}],
            correction_period="2026-02",
        )
    )
    assert saved["completion"] == "complete", saved
    with case.kernel.dashboard.get(
        "/api/dashboard/brief-group",
        {
            "company_id": case.company,
            "period": "2026-01",
            "section": "open_items",
            "group_key": january["data"]["collections"]["open_items"]["items"][0]["group_key"],
            "expected_version": january["snapshot_version"],
        },
    ) as response:
        assert response.status == 409, response.body
    latest = ready(case)
    assert latest["data"]["position"] == january["data"]["position"]
    assert latest["data"]["month_state"] == "closed"
    assert latest["data"]["owner_review_request"] is None
    assert (
        latest["data"]["collections"]["open_items"]["items"][0]["current_outstanding_fen"]
        == "80000"
    )
    february = ready(case, "2026-02")
    assert february["data"]["position"]["month_expense_fen"] == "-20000"


def test_empty_company_and_bad_queries_do_not_create_fake_months(case):
    with case.kernel.dashboard.get(PATH, {"company_id": case.company}) as response:
        assert response.status == 200, response.body
        assert response.body["response"]["data"] is None
        assert response.body["response"]["selected_period"] is None
    for extra in (
        {"section": "wrong"},
        {"voucher_number": "0"},
        {"section": "vouchers", "voucher_number": "1"},
        {"activity_key": str(uuid4()), "voucher_number": "1"},
    ):
        with case.kernel.dashboard.get(
            PATH, {"company_id": case.company, "period": "2026-01", **extra}
        ) as response:
            assert response.status == 400, response.body


def test_workforce_reuses_salary_totals_and_keeps_project_labor_out_of_expense(case):
    from tests.modular.domains.people.test_people import PERSON, no_contributions
    from tests.modular.domains.people.test_people import case as remuneration
    from tests.modular.domains.people.test_read_remuneration import add

    case.seed(expense=False)
    wage, capital = remuneration("B17Z"), remuneration("B17P")
    capital["data"].update(person_id=PERSON, gross_fen="80000")
    capital["data"]["tax_basis"]["tax_fen"] = "0"
    add(case, no_contributions(), wage, capital)
    actual = ready(case)["data"]
    workforce = actual["workforce_cost"]
    assert workforce["has_activity"] is True
    assert workforce["total_fen"] == "100000"
    assert workforce["capitalized_labor_fen"] == "80000"
    assert workforce["employee"]["controlled_total_fen"] == "100000"
    assert workforce["employee"]["settlement_adjustment_fen"] == "0"
    assert workforce["personal_labor"]["has_activity"] is False
    assert actual["long_term_assets"]["net_fen"] == "80000"


def test_current_note_and_owner_tasks_are_replaced_without_retaining_old_bodies(case):
    from tests.modular.application.test_notes_context import note

    original = case.seed()
    scope = "period:2026-01"
    issues = [
        {
            "issue_id": str(uuid4()),
            "scope": scope,
            "period": "2026-01",
            "evidence_ids": [],
            "missing_fields": [],
            "question": question,
            "assignee": assignee,
            "action": "professional_check",
        }
        for assignee, question in (
            ("owner", "请提供这笔支出的实际用途。"),
            ("ai", "会计AI核对资料中的金额差异。"),
        )
    ]
    saved = case.app.accounting_apply(
        case.request(
            notes=[note(scope, "一月维修费用已登记。", case.source_id, basis=case.note_head())],
            unresolved=issues,
            unresolved_scope=scope,
            expected_scope_head=0,
        )
    )
    assert saved["completion"] == "complete", saved
    before = ready(case)
    assert (
        before["data"]["management_commentary_details"]["current"]["text"] == "一月维修费用已登记。"
    )
    assert len(before["data"]["owner_tasks"]) == len(before["data"]["risks"]) == 1
    changed = deepcopy(original)
    changed["data"]["amount_fen"] = "80000"
    saved = case.app.accounting_apply(
        case.request(
            mutations=[{"op": "replace", "expected_seq": 1, "fact": changed}],
            unresolved=[],
            unresolved_scope=scope,
            expected_scope_head=1,
        )
    )
    assert saved["completion"] == "complete", saved
    after = ready(case)
    assert after["data"]["management_commentary_details"] == {
        "status": "stale",
        "current": None,
        "latest": None,
        "supplements": [],
    }
    assert after["data"]["owner_tasks"] == after["data"]["risks"] == []
    assert after["snapshot_version"] != before["snapshot_version"]
