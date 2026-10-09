"""Complete business groups retain exact bounded member and voucher pages."""

import json

import pytest
from entity_fixture import seed_entities
from test_asset_batches import asset_engine as asset_engine
from test_banking import book as book
from test_banking import funding
from test_dashboard_activity_classification import _allocation, _expense, _payment

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_brief_groups import activity_group_page
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.response_contracts import validate_response


def expense(book, subject, party, amount=1, date=None):
    _, save, publish, _ = book
    data = {
        "period": "2026-09",
        "counterparty_id": party,
        "amount_fen": amount,
        "expense_class": "administration",
        "creditor_kind": "supplier",
    }
    if date:
        data["recognition_date"] = date
    save("expense", subject, data)
    publish(subject)


def test_focused_voucher_inside_page_keeps_exact_identity_after_preview_deduplication(book):
    engine, save, publish, _ = book
    funding(save, publish, subject="first", amount=100)
    funding(save, publish, subject="second", amount=200)
    dashboard = Dashboard(engine)
    initial = dashboard.brief("2026-09", section="vouchers")
    vouchers = initial["data"]["collections"]["vouchers"]["items"]
    assert len(vouchers) == 2
    for selected in vouchers:
        result = dashboard.brief(
            "2026-09", section="vouchers", voucher_version_id=selected["voucher_version_id"],
        )
        data = result["data"]
        assert data["focused_voucher"]["voucher_version_id"] == selected["voucher_version_id"]
        assert [
            item["voucher_version_id"] for item in data["collections"]["vouchers"]["items"]
        ] == [
            item["voucher_version_id"] for item in vouchers
        ]


def test_complete_group_amount_and_two_bounded_member_pages(book, monkeypatch):
    engine = book[0]
    for index in range(23):
        expense(book, f"group-{index}", "same-party", index + 1)
    expense(book, "different", "other-party", 100)
    dashboard = Dashboard(engine)
    with dashboard._snapshot("2026-09") as snap:
        groups = activity_group_page(snap)
    assert groups["page"]["total_count"] == 2
    group = next(item for item in groups["items"] if item["member_count"] == 23)
    assert group["amount_fen"] == sum(range(1, 24))
    assert group["voucher_count"] == 23
    assert group["has_month_recognition"]
    assert group["date_from"] is None
    loaded = []
    original = QueryReads.facts

    def facts(reads, ids):
        ids = set(ids)
        loaded.extend(ids)
        return original(reads, ids)

    monkeypatch.setattr(QueryReads, "facts", facts)
    first = dashboard.brief_group("2026-09", section="activity", group_key=group["group_key"])
    assert first["schema_version"] == 2
    validate_response("dashboard_brief_group", first)
    members = first["data"]["collections"]["members"]
    assert len(members["items"]) == 20
    assert len(set(loaded)) == 20
    vouchers = first["data"]["collections"]["vouchers"]
    assert {item["voucher_version_id"] for item in members["items"]} == {
        item["voucher_version_id"] for item in vouchers["items"]
    }
    assert vouchers["page"]["next_cursor"] is None
    second = dashboard.brief_group(
        "2026-09",
        section="activity",
        group_key=group["group_key"],
        cursor=members["page"]["next_cursor"],
        expected_version=first["snapshot_version"],
    )
    assert len(second["data"]["collections"]["members"]["items"]) == 3
    assert len(set(loaded)) == 23
    assert all(row["group_key"] == group["group_key"] for row in members["items"])
    other = next(item for item in groups["items"] if item["member_count"] == 1)
    with pytest.raises(KernelError) as changed:
        dashboard.brief_group(
            "2026-09",
            section="activity",
            group_key=other["group_key"],
            cursor=members["page"]["next_cursor"],
        )
    assert changed.value.code == "dashboard_snapshot_changed"


def test_type_and_actual_object_identity_split_groups(book):
    expense(book, "first", "first-party", 100)
    expense(book, "second", "second-party", 200)
    engine, save, publish, _ = book
    from test_dashboard_provenance import profile

    profile(engine, "counterparty", "beneficiary", display_name="具名权利人")
    save(
        "pass_through",
        "entrusted",
        {
            "period": "2026-09",
            "payer_id": "first-party",
            "beneficiary_id": "beneficiary",
            "amount_fen": 300,
            "rights_and_obligation_confirmed": True,
        },
    )
    publish("entrusted")
    dashboard = Dashboard(engine)
    with dashboard._snapshot("2026-09") as snap:
        groups = activity_group_page(snap)["items"]
    assert len(groups) == 3
    assert sorted(row["amount_fen"] for row in groups) == [100, 200, 300]
    assert {row["kind"] for row in groups} == {"expense", "pass_through"}


def test_formal_individual_payments_for_one_object_and_category_keep_bounded_members(book):
    engine, save, publish, _ = book
    _expense(save, publish, "employee-cost", "alice", 2000, "employee")
    for subject, date in (("first-paid", "2026-09-28"), ("second-paid", "2026-09-29")):
        _payment(book, subject, "alice", [_allocation("expense", "employee-cost", 1000)], date=date)
    dashboard = Dashboard(engine)
    data = dashboard.brief("2026-09")["data"]
    payments = [item for item in data["collections"]["activity"]["items"]
                if item["kind"] == "payment"]
    assert len(payments) == 1
    group = payments[0]
    assert group["group"] == "employee_reimbursement"
    assert group["member_count"] == group["voucher_count"] == 2
    assert group["amount_fen"] == 2000
    first = dashboard.brief_group(
        "2026-09", section="activity", group_key=group["group_key"], limit=1,
    )
    collection = first["data"]["collections"]["members"]
    assert collection["page"]["total_count"] == 2
    second = dashboard.brief_group(
        "2026-09", section="activity", group_key=group["group_key"], limit=1,
        cursor=collection["page"]["next_cursor"], expected_version=first["snapshot_version"],
    )
    member = second["data"]["collections"]["members"]["items"][0]
    assert collection["items"][0]["key"] != member["key"]
    assert member["detail_scope_category"] == "employee_reimbursement"
    assert data["activity_count"] == data["voucher_count"] == 3 and data["group_count"] == 2


def test_default_group_summary_keeps_counts_and_independent_vouchers(book):
    for index in range(22):
        expense(book, f"default-{index}", "same-party")
    dashboard = Dashboard(book[0])
    brief = dashboard.brief("2026-09")
    validate_response("dashboard_brief", brief)
    assert brief["schema_version"] == 17
    assert brief["data"]["activity_count"] == brief["data"]["voucher_count"] == 22
    assert brief["data"]["group_count"] == 1
    assert "vouchers" not in brief["data"]["collections"]
    group = brief["data"]["collections"]["activity"]["items"][0]
    assert group["member_count"] == 22 and group["amount_fen"] == 22
    independent = dashboard.brief("2026-09", section="vouchers")
    assert len(independent["data"]["collections"]["vouchers"]["items"]) == 20
    focus = dashboard.brief("2026-09", voucher_number=22)
    assert focus["data"]["focused_activity"]["group_key"] == group["group_key"]
    assert focus["data"]["focused_activity"]["voucher_number"] == 22


def test_complete_scalar_group_does_not_hydrate_fact_or_result_bodies(book, monkeypatch):
    engine, save, publish, _ = book
    for index in range(24):
        funding(save, publish, subject=f"scalar-{index}", amount=index + 1)

    def unexpected(*args, **kwargs):
        pytest.fail("group summaries must use authenticated scalar inputs")

    def facts(reads, identifiers):
        identifiers = set(identifiers)
        if identifiers:
            unexpected()
        return {}

    monkeypatch.setattr(QueryReads, "facts", facts)
    monkeypatch.setattr(QueryReads, "verify_selected_content", unexpected)
    with Dashboard(engine)._snapshot("2026-09") as snap:
        items = activity_group_page(snap)["items"]
    assert len(items) == 1
    assert items[0]["amount_fen"] == 300
    assert items[0]["member_count"] == 24
    assert items[0]["date_from"] == items[0]["date_to"] == "2026-09-01"
    assert items[0]["has_month_recognition"] is False


def test_same_owner_capital_and_loan_remain_distinct_business_natures(book):
    engine, save, publish, _ = book
    funding(save, publish, subject="capital", amount=100)
    save(
        "funding",
        "loan",
        {
            "period": "2026-09",
            "owner_id": "owner",
            "amount_fen": 200,
            "funding_kind": "loan",
            "actual_date": "2026-09-02",
            "bank_account_id": "bank-a",
        },
    )
    publish("loan")
    with Dashboard(engine)._snapshot("2026-09") as snap:
        items = activity_group_page(snap)["items"]
    assert {row["title"]: row["amount_fen"] for row in items} == {"股东投入": 100, "借款到账": 200}


def test_open_member_vouchers_follow_first_exact_occurrence_in_member_order(book):
    expense(book, "z-source", "same-party", 100)
    expense(book, "a-source", "same-party", 200)
    dashboard = Dashboard(book[0])
    root = dashboard.brief("2026-09", section="open_items")
    group = root["data"]["collections"]["open_items"]["items"][0]
    response = dashboard.brief_group("2026-09", section="open_items", group_key=group["group_key"])
    validate_response("dashboard_brief_group", response)
    collections = response["data"]["collections"]
    assert len(collections["vouchers"]["items"]) == 2
    assert [row["voucher_version_id"] for row in collections["vouchers"]["items"]] == list(
        dict.fromkeys(
            row["voucher_version_id"]
            for row in collections["members"]["items"]
            if row["voucher_version_id"] is not None
        )
    )


@pytest.mark.parametrize(
    "property,value",
    [("counterparty_id", "damaged"), ("counterparty_id", None), ("key", "damaged")],
)
def test_activity_identity_authenticates_exact_ancestor_before_grouping(tmp_path, property, value):
    from test_integrity_content import damage
    from test_payroll_corrections import Company

    from ai_accounting.kernel.domains.adjustments import EmployeeAdvance
    from ai_accounting.kernel.domains.transactions import Allocation, Expense

    company = Company(tmp_path / "ancestor.sqlite")
    company.save(
        Expense(
            period="2026-01",
            counterparty_id="supplier",
            amount_fen=100,
            expense_class="administration",
            creditor_kind="supplier",
        ),
        "old-expense",
    )
    company.publish("old-expense")
    company.save(
        EmployeeAdvance(
            period="2026-02",
            payer_id="owner",
            payer_kind="owner",
            payment_on_behalf_confirmed=True,
            actual_creditor_payment_date="2026-02-01",
            sources=(
                Allocation(
                    source_kind="expense",
                    source_id="old-expense",
                    obligation="primary",
                    amount_fen=100,
                ),
            ),
        ),
        "owner-paid",
    )
    company.publish("owner-paid")
    dashboard = Dashboard(company.engine)
    with dashboard._snapshot("2026-02") as snap:
        assert activity_group_page(snap)["items"][0]["member_count"] == 1
    encoded = "NULL" if value is None else "'" + value + "'"
    damage(
        company.engine,
        "calculation",
        "UPDATE calculation SET outcome=json_set(outcome,"
        f"'$.values.obligations[0].{property}',{encoded}) WHERE subject_id='old-expense'",
    )
    with pytest.raises(KernelError) as changed:
        with dashboard._snapshot("2026-02") as snap:
            activity_group_page(snap)
    assert changed.value.code == "content_integrity_failed"


def test_missing_object_names_use_batched_metadata_fallback(book):
    engine, _, _, proof = book
    owners = [f"unnamed-owner-{index}" for index in range(24)]
    seed_entities(
        engine, [(owner, "person", None) for owner in owners] + [("bank-a", "fund_account", "bank")]
    )
    facts = [
        {
            "kind": "funding",
            "subject_id": owner,
            "expected_revision": 0,
            "evidence": [proof],
            "data": {
                "period": "2026-09",
                "owner_id": owner,
                "amount_fen": 1,
                "funding_kind": "capital",
                "bank_account_id": "bank-a",
                "actual_date": "2026-09-01",
            },
        }
        for owner in owners
    ]
    engine.save_facts(facts, request_id="unnamed-facts")
    preview = engine.preview(owners)
    engine.confirm(
        owners,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="unnamed-publish",
    )
    statements = []
    with Dashboard(engine)._snapshot("2026-09") as snap:
        snap.connection.set_trace_callback(statements.append)
        result = activity_group_page(snap)
        snap.connection.set_trace_callback(None)
        assert result["page"]["total_count"] == 24
        assert set(owners) <= snap.metadata.current_payees.missing
        assert set(owners) <= snap.metadata.tax_candidates.missing
    assert sum("SELECT p.* FROM payee_revision p" in statement for statement in statements) == 1
    assert (
        sum(
            "JOIN fact_scope s" in statement and "tax-identity:" in statement
            for statement in statements
        )
        == 1
    )


def test_asset_batch_members_without_own_publications_remain_valid_group_ancestry(asset_engine):
    from test_asset_batches import activate, month

    engine, proof = asset_engine
    activate(engine, proof)
    month(engine, proof, "2026-01", "consumption")
    with Dashboard(engine)._snapshot("2026-01") as snap:
        items = activity_group_page(snap)["items"]
    assert {row["kind"] for row in items} >= {
        "asset",
        "asset_activation_batch",
        "asset_consumption_month",
    }


@pytest.mark.parametrize("count", [24, 240])
def test_default_and_member_growth_have_bounded_bodies(book, monkeypatch, count):
    engine, _, _, proof = book
    seed_entities(engine, [("owner", "person", None), ("bank-a", "fund_account", "bank")])
    facts = [
        {
            "kind": "funding",
            "subject_id": f"growth-{index:04}",
            "expected_revision": 0,
            "evidence": [proof],
            "data": {
                "period": "2026-09",
                "owner_id": "owner",
                "amount_fen": index + 1,
                "funding_kind": "capital",
                "bank_account_id": "bank-a",
                "actual_date": "2026-09-01",
            },
        }
        for index in range(count)
    ]
    engine.save_facts(facts, request_id="growth-facts")
    subjects = [row["subject_id"] for row in facts]
    preview = engine.preview(subjects)
    engine.confirm(
        subjects,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="growth-publish",
    )
    fact_ids, outcome_ids = set(), set()
    original_facts, original_content = QueryReads.facts, QueryReads.verify_selected_content

    def observed_facts(reads, identifiers):
        identifiers = set(identifiers)
        fact_ids.update(identifiers)
        return original_facts(reads, identifiers)

    def observed_content(reads, identifiers):
        identifiers = set(identifiers)
        outcome_ids.update(identifiers - reads._verified_source_contents.keys())
        return original_content(reads, identifiers)

    monkeypatch.setattr(QueryReads, "facts", observed_facts)
    monkeypatch.setattr(QueryReads, "verify_selected_content", observed_content)
    dashboard = Dashboard(engine)
    root = dashboard.brief("2026-09")
    root_facts, root_outcomes = len(fact_ids), len(outcome_ids)
    assert root_facts == 0
    assert root["data"]["group_count"] == 1
    group = root["data"]["collections"]["activity"]["items"][0]
    assert group["member_count"] == count
    assert group["amount_fen"] == count * (count + 1) // 2
    fact_ids.clear()
    outcome_ids.clear()
    response = dashboard.brief_group("2026-09", section="activity", group_key=group["group_key"])
    assert len(fact_ids) == 20
    assert len(response["data"]["collections"]["members"]["items"]) == 20
    from ai_accounting.kernel.response_contracts import http_response

    metrics = {
        "count": count,
        "default_fact_bodies": root_facts,
        "default_verify_content_ids": root_outcomes,
        "member_fact_bodies": len(fact_ids),
        "member_verify_content_ids": len(outcome_ids),
        "group_bytes": len(json.dumps(group, ensure_ascii=False).encode()),
        "member_bytes": len(
            json.dumps(
                http_response("dashboard_brief_group", response), ensure_ascii=False
            ).encode()
        ),
    }
    assert metrics["group_bytes"] < 800 and metrics["member_bytes"] < 50000
    print("activity growth:", json.dumps(metrics))
