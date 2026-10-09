"""Pending amounts retain the adopted business meaning across pages and months."""

from collections import defaultdict

import pytest
from material_fixture import supporting_text
from test_banking import book as book
from test_banking import close_month, inventories
from test_integrity_content import damage
from test_opening_continuation import book as _opening_book
from test_reimbursement_acceptance import acceptance, current, source, wage
from test_reimbursement_acceptance import book as _acceptance_book

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.response_contracts import validate_response

PERIOD = "2026-09"
opening_book, acceptance_book = _opening_book, _acceptance_book


def _expense(save, publish, subject, party, amount, creditor_kind="employee"):
    saved = save(
        "expense",
        subject,
        {
            "period": PERIOD,
            "counterparty_id": party,
            "amount_fen": amount,
            "expense_class": "administration",
            "creditor_kind": creditor_kind,
        },
    )
    publish(subject)
    return saved


def _native_obligations(book):
    _, save, publish, _ = book
    _expense(save, publish, "employee-cost", "employee", 10000)
    _expense(save, publish, "supplier-cost", "supplier", 5000, "supplier")
    save(
        "reimbursed_deposit",
        "deposit",
        {
            "period": PERIOD,
            "counterparty_id": "landlord",
            "employee_id": "employee",
            "amount_fen": 30000,
            "company_acceptance_confirmed": True,
            "refund_right_confirmed": True,
        },
    )
    save(
        "labor_project_cost",
        "project-labor",
        {
            "period": PERIOD,
            "person_id": "designer",
            "project_id": "design-project",
            "gross_fee_fen": 8000,
            "tax_treatment": "not_withheld_not_filed",
            "capitalization_conditions_confirmed": True,
        },
    )
    publish("deposit", "project-labor")
    for payer_kind, amount in (("employee", 7000), ("owner", 9000)):
        original = payer_kind + "-supplier-cost"
        _expense(save, publish, original, "supplier", amount, "supplier")
        subject = payer_kind + "-advance"
        save(
            "employee_advance",
            subject,
            {
                "period": PERIOD,
                # The same person can advance funds in either explicit role.
                # Both debts share account, kind, party and obligation name.
                "payer_id": "shared-payer",
                "payer_kind": payer_kind,
                "payment_on_behalf_confirmed": True,
                "actual_creditor_payment_date": "2026-09-20",
                "sources": [
                    {
                        "source_kind": "expense",
                        "source_id": original,
                        "obligation": "primary",
                        "amount_fen": amount,
                    }
                ],
            },
        )
        publish(subject)
    return {
        "expense:employee-cost:primary": ("employee_payables", 10000),
        "expense:supplier-cost:primary": ("supplier_payables", 5000),
        "reimbursed_deposit:deposit:reimbursement": ("employee_payables", 30000),
        "reimbursed_deposit:deposit:refund": ("refundable_deposit_receivables", 30000),
        "labor_project_cost:project-labor:net": ("labor_payables", 8000),
        "employee_advance:employee-advance:primary": ("employee_payables", 7000),
        "employee_advance:owner-advance:primary": ("other_payables", 9000),
    }


def _category_totals(expected):
    totals = defaultdict(lambda: [0, 0])
    for category, amount in expected.values():
        totals[category][0] += 1
        totals[category][1] += amount
    return dict(totals)


def _read_and_check_pages(engine, expected, *, period=PERIOD, limit=1):
    dashboard = Dashboard(engine)
    response = dashboard.brief(period, section="open_items", preparation="deferred", limit=limit)
    seen = {}
    groups = {}
    totals = _category_totals(expected)
    while True:
        validate_response("dashboard_brief", response)
        data = response["data"]
        summary = data["open_items"]
        assert {
            category["key"]: [category["count"], category["outstanding_fen"]]
            for category in summary["categories"]
        } == totals
        for direction in ("receivable", "payable"):
            amounts = (
                [
                    amount
                    for category, amount in expected.values()
                    if category.endswith("receivables") or category == "supplier_advances"
                ]
                if direction == "receivable"
                else [
                    amount
                    for category, amount in expected.values()
                    if not category.endswith("receivables") and category != "supplier_advances"
                ]
            )
            assert summary[direction + "_count"] == len(amounts)
            assert summary[direction + "_fen"] == sum(amounts)
        collection = data["collections"]["open_items"]
        assert collection["page"]["total_count"] == summary["group_count"]
        assert (
            sum(category["group_count"] for category in summary["categories"])
            == summary["group_count"]
        )
        for item in collection["items"]:
            assert item["id"] == item["group_key"]
            assert item["id"] not in groups
            groups[item["id"]] = item
            members = []
            cursor = None
            while True:
                detail = dashboard.brief_group(
                    period,
                    section="open_items",
                    group_key=item["group_key"],
                    expected_version=response["snapshot_version"],
                    cursor=cursor,
                    limit=limit,
                )
                validate_response("dashboard_brief_group", detail)
                member_collection = detail["data"]["collections"]["members"]
                vouchers = {
                    voucher["voucher_version_id"]: voucher
                    for voucher in detail["data"]["collections"]["vouchers"]["items"]
                }
                assert member_collection["page"]["total_count"] == item["member_count"]
                for member in member_collection["items"]:
                    assert member["id"] not in seen
                    assert member["group_key"] == item["group_key"]
                    assert (member["category_key"], member["outstanding_fen"]) == expected[
                        member["id"]
                    ]
                    assert member["recognition"]["period"] == member["source_period"]
                    if member["voucher_version_id"] is not None:
                        voucher = vouchers[member["voucher_version_id"]]
                        assert voucher["subject_id"] == member["subject_id"]
                        assert voucher["recognition"] == member["recognition"]
                    seen[member["id"]] = member
                    members.append(member)
                if not member_collection["page"]["has_more"]:
                    break
                cursor = member_collection["page"]["next_cursor"]
            assert len(members) == item["member_count"]
            assert {member["category_key"] for member in members} == {item["category_key"]}
            for amount in (
                "source_amount_fen",
                "paid_fen",
                "other_settled_fen",
                "outstanding_fen",
                "current_outstanding_fen",
            ):
                assert item[amount] == sum(member[amount] for member in members)
        if not collection["page"]["has_more"]:
            break
        response = dashboard.brief(
            period,
            section="open_items",
            preparation="deferred",
            cursor=collection["page"]["next_cursor"],
            expected_version=response["snapshot_version"],
            limit=limit,
        )
    assert set(seen) == set(expected)
    assert len(groups) == summary["group_count"]
    for category in summary["categories"]:
        category_groups = [
            item for item in groups.values() if item["category_key"] == category["key"]
        ]
        assert len(category_groups) == category["group_count"]
        assert sum(item["member_count"] for item in category_groups) == category["count"]
        assert (
            sum(item["outstanding_fen"] for item in category_groups) == category["outstanding_fen"]
        )
    return seen


def _assert_business_details(engine, expected, rows, *, period=PERIOD):
    dashboard = Dashboard(engine)
    for subject in sorted({item["subject_id"] for item in rows.values()}):
        detail = dashboard.business_status(period, subject, settlement_view="historical")["data"]
        obligations = {item["key"]: item for item in detail["settlements"]["obligations"]}
        source_keys = {key for key, item in rows.items() if item["subject_id"] == subject}
        assert set(obligations) == source_keys
        for key, item in obligations.items():
            assert (item["category_key"], item["remaining_fen"]) == expected[key]


@pytest.mark.parametrize("closed", [False, True])
def test_pending_classification_matches_totals_pages_and_business_detail(book, closed):
    engine, _, _, proof = book
    expected = _native_obligations(book)
    if closed:
        assert (
            close_month(
                inventories(engine, proof, PERIOD, {"transactions", "payroll", "assets"}),
                proof,
                PERIOD,
            )["status"]
            == "closed"
        )
    ledger = engine.ledger(PERIOD)
    rows = _read_and_check_pages(engine, expected)
    _assert_business_details(engine, expected, rows)
    assert engine.ledger(PERIOD) == ledger


@pytest.mark.parametrize("closed", [False, True])
def test_opening_natures_remain_distinct_inside_the_same_kind_account_and_party_group(
    opening_book, closed
):
    engine, _, _, package, proof = opening_book
    definitions = (
        ("employee_reimbursement", 10000, "employee_payables"),
        ("owner_reimbursement", 20000, "other_payables"),
        ("deposit_payable", 30000, "other_payables"),
        ("other_payable", 40000, "other_payables"),
        ("deposit_receivable", 50000, "refundable_deposit_receivables"),
        ("other_receivable", 60000, "other_receivables"),
    )
    members = [
        (
            "opening_obligation",
            nature,
            {
                "counterparty_id": "shared-party",
                "nature": nature,
                "outstanding_fen": amount,
                "business_reference": "prior-" + nature,
            },
        )
        for nature, amount, _ in definitions
    ]
    members.append(
        (
            "opening_equity",
            "equity",
            {
                "equity_kind": "retained_earnings",
                "balance_fen": 10000,
                "holder_or_basis_id": "prior-book",
            },
        )
    )
    package(members, period=PERIOD)
    if closed:
        supporting_text(engine, proof, period=PERIOD)
        assert (
            close_month(inventories(engine, proof, PERIOD, {"transactions"}), proof, PERIOD)[
                "status"
            ]
            == "closed"
        )
    expected = {
        "opening_obligation:" + nature + ":primary": (category, amount)
        for nature, amount, category in definitions
    }
    rows = _read_and_check_pages(engine, expected)
    _assert_business_details(engine, expected, rows)


@pytest.mark.parametrize("closed", [False, True])
def test_historical_employee_debt_keeps_adopted_classification_after_payment_and_new_draft(
    book, closed
):
    engine, save, publish, proof = book
    _expense(save, publish, "employee-cost", "employee", 10000)
    if closed:
        close_month(inventories(engine, proof, PERIOD, {"transactions"}), proof, PERIOD)
    save(
        "payment",
        "partial-reimbursement",
        {
            "period": "2026-10",
            "actual_date": "2026-10-02",
            "direction": "outflow",
            "bank_account_id": "bank",
            "counterparty_id": "employee",
            "amount_fen": 4000,
            "allocations": [
                {
                    "source_kind": "expense",
                    "source_id": "employee-cost",
                    "obligation": "primary",
                    "amount_fen": 4000,
                }
            ],
        },
    )
    publish("partial-reimbursement")
    save(
        "expense",
        "employee-cost",
        {
            "period": PERIOD,
            "counterparty_id": "employee",
            "amount_fen": 10000,
            "expense_class": "administration",
            "creditor_kind": "supplier",
        },
        revision=1,
    )
    expected = {"expense:employee-cost:primary": ("employee_payables", 10000)}
    rows = _read_and_check_pages(engine, expected)
    row = rows["expense:employee-cost:primary"]
    assert row["status"] == "open"
    assert row["current_status"] == "partial"
    assert row["current_outstanding_fen"] == 6000
    detail = Dashboard(engine).business_status(PERIOD, "employee-cost", as_of="2026-10-09")["data"]
    historical = detail["settlements"]["obligations"][0]
    current = detail["current_followups"]["settlements"]["obligations"][0]
    assert (historical["category_key"], historical["remaining_fen"]) == ("employee_payables", 10000)
    assert (current["category_key"], current["remaining_fen"]) == ("employee_payables", 6000)


@pytest.mark.parametrize("closed", [False, True])
def test_corrupt_adopted_source_cannot_supply_pending_classification(book, closed):
    engine, save, publish, proof = book
    _expense(save, publish, "employee-cost", "employee", 10000)
    if closed:
        close_month(inventories(engine, proof, PERIOD, {"transactions"}), proof, PERIOD)
    with engine.store.connection(read_only=True) as connection:
        calculation = connection.execute(
            "SELECT calculation_id FROM calculation_current WHERE subject_id='employee-cost'"
        ).fetchone()[0]
    damage(
        engine,
        "calculation",
        "UPDATE calculation SET outcome=json_set(outcome,'$.values.creditor_kind','supplier') "
        "WHERE id=?",
        (calculation,),
    )
    with pytest.raises(KernelError) as failure:
        Dashboard(engine).brief(PERIOD, section="open_items", preparation="deferred", limit=1)
    assert failure.value.code == "content_integrity_failed"


@pytest.mark.parametrize("closed", [False, True])
def test_opening_category_rejects_a_damaged_adopted_nature_in_a_mixed_group(opening_book, closed):
    engine, _, _, package, proof = opening_book
    package(
        [
            (
                "opening_obligation",
                nature,
                {
                    "counterparty_id": "shared-party",
                    "nature": nature,
                    "outstanding_fen": amount,
                    "business_reference": "prior-" + nature,
                },
            )
            for nature, amount in (("deposit_receivable", 1000), ("other_receivable", 2000))
        ]
        + [
            (
                "opening_equity",
                "equity",
                {
                    "equity_kind": "retained_earnings",
                    "balance_fen": 3000,
                    "holder_or_basis_id": "prior-book",
                },
            )
        ],
        period=PERIOD,
    )
    if closed:
        supporting_text(engine, proof, period=PERIOD)
        close_month(inventories(engine, proof, PERIOD, {"transactions"}), proof, PERIOD)
    with engine.store.connection(read_only=True) as connection:
        calculation = connection.execute(
            "SELECT calculation_id FROM calculation_current WHERE subject_id='deposit_receivable'"
        ).fetchone()[0]
    damage(
        engine,
        "calculation",
        "UPDATE calculation SET outcome=json_set(outcome,'$.values.nature','other_receivable') "
        "WHERE id=?",
        (calculation,),
    )
    with pytest.raises(KernelError) as failure:
        Dashboard(engine).brief(PERIOD, section="open_items", preparation="deferred", limit=1)
    assert failure.value.code == "content_integrity_failed"


@pytest.mark.parametrize("closed", [False, True])
def test_reimbursement_acceptance_roles_split_a_shared_pending_group(acceptance_book, closed):
    engine, save, publish, close, _, _ = acceptance_book
    wage(engine, save, publish)
    original_obligations = current(engine, "wage")["values"]["obligations"]
    close("2026-01")
    for payer_kind in ("employee", "owner"):
        subject = payer_kind + "-accepted"
        save(
            "reimbursement_acceptance",
            subject,
            acceptance(
                payer_id="shared-payer",
                payer_kind=payer_kind,
                sources=[source("employee_social", 10000)],
            ),
        )
        publish(subject)
    if closed:
        assert close("2026-02")["status"] == "closed"
    expected = {
        item["key"]: (
            "tax_payables" if item["name"] == "tax" else "payroll_payables",
            item["amount_fen"] - (20000 if item["name"] == "employee_social" else 0),
        )
        for item in original_obligations
        if item["amount_fen"] > (20000 if item["name"] == "employee_social" else 0)
    }
    expected.update(
        {
            "reimbursement_acceptance:employee-accepted:primary": ("employee_payables", 10000),
            "reimbursement_acceptance:owner-accepted:primary": ("other_payables", 10000),
        }
    )
    rows = _read_and_check_pages(engine, expected, period="2026-02")
    for subject, category in (
        ("employee-accepted", "employee_payables"),
        ("owner-accepted", "other_payables"),
    ):
        key = "reimbursement_acceptance:" + subject + ":primary"
        assert rows[key]["category_key"] == category
        detail = Dashboard(engine).business_status("2026-02", subject)["data"]
        assert [
            (item["category_key"], item["remaining_fen"])
            for item in detail["settlements"]["obligations"]
        ] == [(category, 10000)]
