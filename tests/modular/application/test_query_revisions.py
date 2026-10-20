"""Real current collections detect relevant changes without loading whole pages."""

from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError

from ._working_case import case as case


def find(case, **fields):
    return case.app.accounting_find(
        {
            "company_id": case.company,
            "family": "operations",
            "period_from": "2026-01",
            "period_to": "2026-01",
            "limit": 1,
            **fields,
        }
    )


def replace(case, fact, seq):
    request = case.request(fact)
    request["mutations"][0].update(op="replace", expected_seq=seq)
    result = case.app.accounting_apply(request)
    assert result["completion"] == "complete", result


def assert_stale(case, page, **fields):
    assert page["page"]["next_cursor"] is not None
    with pytest.raises(ContractError) as error:
        find(case, cursor=page["page"]["next_cursor"], **fields)
    assert error.value.code == "snapshot_changed"


def test_updating_low_sequence_business_invalidates_cursor_even_when_maximum_seq_stays(case):
    low = case.seed()
    high = case.expense("20000")
    assert case.app.accounting_apply(case.request(high))["completion"] == "complete"
    for seq, amount in ((1, "30000"), (2, "40000")):
        high["data"]["amount_fen"] = amount
        replace(case, high, seq)
    page = find(case)
    low["data"]["amount_fen"] = "90000"
    replace(case, low, 1)
    states = case.get(low["business_id"], high["business_id"])["businesses"]
    assert [item["state"]["seq"] for item in states] == [2, 3]
    assert find(case)["page"]["collection_version"] != page["page"]["collection_version"]
    assert_stale(case, page)


def test_removing_older_member_invalidates_cursor_when_newest_member_is_unchanged(case):
    older = case.seed()
    newest = case.expense("20000")
    assert case.app.accounting_apply(case.request(newest))["completion"] == "complete"
    page = find(case)
    request = case.request()
    request["mutations"] = [
        {"op": "withdraw", "expected_seq": 1, "business_id": older["business_id"]}
    ]
    assert case.app.accounting_apply(request)["completion"] == "complete"
    current = find(case)
    assert current["page"]["total_count"] == current["page"]["filtered_count"] == 1
    assert [item["business_id"] for item in current["items"]] == [newest["business_id"]]
    assert_stale(case, page)


def test_equal_count_member_replacement_invalidates_cursor_and_returns_new_identity(case):
    removed = case.seed()
    kept = case.expense("20000")
    assert case.app.accounting_apply(case.request(kept))["completion"] == "complete"
    page = find(case)
    added = case.expense("30000")
    request = case.request(added)
    request["mutations"].append(
        {"op": "withdraw", "expected_seq": 1, "business_id": removed["business_id"]}
    )
    assert case.app.accounting_apply(request)["completion"] == "complete"
    current = find(case, limit=10)
    assert current["page"]["total_count"] == current["page"]["filtered_count"] == 2
    assert {item["business_id"] for item in current["items"]} == {
        kept["business_id"],
        added["business_id"],
    }
    assert_stale(case, page)


def test_object_names_future_business_and_external_tasks_preserve_current_cursor(case):
    case.seed()
    second = case.expense("20000")
    assert case.app.accounting_apply(case.request(second))["completion"] == "complete"
    page = find(case)
    original = find(case, limit=10)["items"]
    requests = [
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
        ),
        case.request(case.expense("30000", "2026-04")),
        case.request(
            unresolved=[
                {
                    "issue_id": str(uuid4()),
                    "scope": "external:filing",
                    "period": "2026-01",
                    "evidence_ids": [case.source_id],
                    "missing_fields": [],
                    "question": "合成外部申报待办。",
                    "assignee": "owner",
                    "action": "external_filing",
                }
            ],
            unresolved_scope="external:filing",
            expected_scope_head=0,
        ),
    ]
    for request in requests:
        result = case.app.accounting_apply(request)
        assert result["completion"] == "complete", result
        continued = find(case, cursor=page["page"]["next_cursor"])
        assert continued["head"] > page["head"]
        assert continued["page"]["collection_version"] == page["page"]["collection_version"]
        assert continued["items"] == original[1:]


def test_unrelated_same_month_business_update_preserves_object_filtered_cursor(case):
    case.seed()
    related, unrelated = case.expense("20000"), case.expense("30000")
    other = str(uuid4())
    unrelated["data"]["creditor_id"] = other
    result = case.app.accounting_apply(
        case.request(
            related,
            unrelated,
            objects=[
                {
                    "object_id": other,
                    "kind": "organization",
                    "expected_seq": 0,
                    "name": "合成其他供应商",
                    "data": {},
                }
            ],
        )
    )
    assert result["completion"] == "complete", result
    page = find(case, object_id=case.party)
    assert page["page"]["total_count"] == 3 and page["page"]["filtered_count"] == 2
    unrelated["data"]["amount_fen"] = "40000"
    replace(case, unrelated, 1)
    continued = find(case, object_id=case.party, cursor=page["page"]["next_cursor"])
    assert continued["page"]["collection_version"] == page["page"]["collection_version"]
    assert continued["page"]["total_count"] == 3
    assert continued["page"]["filtered_count"] == 2


def bank_account(account_id):
    return {
        "object_id": account_id,
        "kind": "bank_account",
        "name": "合成银行",
        "expected_seq": 0,
        "data": {
            "bank_name": "合成银行",
            "account_number": account_id,
            "currency": "CNY",
            "owner": "company",
            "opened_period": "2026-01",
            "zero_at_opening": True,
        },
    }


def empty_statement(account_id, period):
    statement = {
        "business_id": str(uuid4()),
        "kind": "money.bank_statement",
        "period": period,
        "evidence_ids": [],
        "data": {
            "funds_account_id": account_id,
            "opening_fen": "0",
            "closing_fen": "0",
            "rows": [],
        },
    }
    reconciliation = {
        "business_id": str(uuid4()),
        "kind": "money.reconciliation",
        "period": period,
        "evidence_ids": [],
        "data": {"statement_id": statement["business_id"], "matches": []},
    }
    return statement, reconciliation


def test_recalculated_object_membership_invalidates_equal_count_collection_without_fact_changes(
    case,
):
    case.seed(expense=False)
    first_account, second_account = str(uuid4()), str(uuid4())
    first, first_check = empty_statement(first_account, "2026-01")
    second, second_check = empty_statement(second_account, "2026-01")
    following, following_check = empty_statement(first_account, "2026-02")
    result = case.app.accounting_apply(
        case.request(
            first,
            first_check,
            second,
            second_check,
            following,
            following_check,
            objects=[bank_account(first_account), bank_account(second_account)],
        )
    )
    assert result["completion"] == "complete", result
    scope = {
        "family": "money",
        "kind": "money.reconciliation",
        "object_id": first_account,
        "period_to": "2026-02",
    }
    page = find(case, **scope)
    assert page["page"]["filtered_count"] == 2
    first["data"]["funds_account_id"] = second_account
    second["data"]["funds_account_id"] = first_account
    request = case.request(first, second)
    for mutation in request["mutations"]:
        mutation.update(op="replace", expected_seq=1)
    result = case.app.accounting_apply(request)
    assert result["completion"] == "complete", result
    current = find(case, **scope, limit=10)
    assert current["page"]["filtered_count"] == 2
    assert {item["business_id"] for item in current["items"]} == {
        second_check["business_id"],
        following_check["business_id"],
    }
    assert all(item["seq"] == 1 for item in current["items"])
    assert case.get(first_check["business_id"])["businesses"][0]["fact"] == first_check
    assert_stale(case, page, **scope)


def test_sql_keyset_pages_fetch_only_limit_plus_one_metadata_rows(case, monkeypatch):
    first = case.seed()
    others = [case.expense(str(amount)) for amount in range(1000, 1006)]
    assert case.app.accounting_apply(case.request(*others))["completion"] == "complete"
    expected = sorted(fact["business_id"] for fact in [first, *others])
    observed = []
    with case.kernel.storage.read_unit(case.company) as read:
        session = read.repository("application")._session
        original = session.fetchall

        def capture(sql, parameters=()):
            rows = original(sql, parameters)
            observed.append((sql.upper(), tuple(parameters), len(rows)))
            return rows

        monkeypatch.setattr(session, "fetchall", capture)
        query = {
            "company_id": case.company,
            "family": "operations",
            "period_from": "2026-01",
            "period_to": "2026-01",
            "limit": 2,
        }
        items = []
        while True:
            page = case.app.find_businesses(query, read)
            assert page["items"]
            if items:
                assert page["items"][0]["business_id"] > items[-1]["business_id"]
            items.extend(page["items"])
            assert page["page"]["total_count"] == page["page"]["filtered_count"] == 7
            if not page["page"]["has_more"]:
                break
            query["cursor"] = page["page"]["next_cursor"]
    assert [item["business_id"] for item in items] == expected
    assert len(observed) == 4
    for index, (sql, parameters, count) in enumerate(observed):
        assert "LIMIT" in sql and "OFFSET" not in sql
        assert count <= 3
        if index:
            assert ">" in sql and expected[index * 2 - 1] in parameters
