"""Activity groups follow published business facts and exact settlement obligations."""

from collections import Counter

import pytest
from test_banking import book as book
from test_banking import (
    close_month,
    entry,
    funding,
    inventories,
    opening,
    reconciliation,
    statement,
)
from test_business_domains import surtax_policy, vat_policy
from test_dashboard_provenance import profile
from test_integrity_content import damage

from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard, _group, _settlement_source

PERIOD = "2026-09"


def _expense(save, publish, subject, party, amount, creditor_kind):
    save(
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


def _allocation(kind, subject, amount, obligation="primary", recipient=None):
    result = {
        "source_kind": kind,
        "source_id": subject,
        "obligation": obligation,
        "amount_fen": amount,
    }
    if recipient is not None:
        result["recipient_id"] = recipient
    return result


def _payment(
    book, subject, party, allocations, *, channel="bank", direction="outflow", date="2026-09-28",
):
    _, save, publish, proof = book
    data = {
        "period": PERIOD,
        "actual_date": date,
        "direction": direction,
        "counterparty_id": party,
        "amount_fen": sum(item["amount_fen"] for item in allocations),
        "allocations": allocations,
    }
    if channel == "bank":
        kind, account_field = "payment", "bank_account_id"
    elif channel == "cash":
        kind, account_field = "cash_payment", "cash_account_id"
    else:
        kind, account_field = "platform_payment", "platform_account_id"
        movement = subject + "-movement"
        save(
            "platform_movement",
            movement,
            {
                "period": PERIOD,
                "actual_date": data["actual_date"],
                "direction": direction,
                "platform_account_id": "platform",
                "transaction_reference": movement,
                "amount_fen": data["amount_fen"],
                "source_evidence_digest": proof,
                "source_location": "row:" + movement,
            },
        )
        publish(movement)
        data["movement_ids"] = [movement]
    data[account_field] = channel
    save(kind, subject, data)
    publish(subject)


def _reimbursement_sources(book):
    _, save, publish, _ = book
    _expense(save, publish, "supplier-cost", "supplier", 7000, "supplier")
    _expense(save, publish, "alice-cost", "alice", 10000, "employee")
    _expense(save, publish, "bob-cost", "bob", 12000, "employee")
    save(
        "reimbursed_asset",
        "computer",
        {
            "period": PERIOD,
            "asset_id": "computer",
            "asset_type": "fixed",
            "cost_fen": 50000,
            "company_acceptance_confirmed": True,
            "creditors": [
                {"employee_id": "alice", "amount_fen": 20000},
                {"employee_id": "bob", "amount_fen": 30000},
            ],
        },
    )
    save(
        "reimbursed_deposit",
        "deposit",
        {
            "period": PERIOD,
            "counterparty_id": "landlord",
            "employee_id": "alice",
            "amount_fen": 30000,
            "company_acceptance_confirmed": True,
            "refund_right_confirmed": True,
        },
    )
    publish("computer", "deposit")
    return {
        "supplier-cost": "expense_supplier",
        "alice-cost": "employee_reimbursement",
        "bob-cost": "employee_reimbursement",
        "computer": "assets",
        "deposit": "fund_movement",
    }


def _reimbursement_payments(book, channel="bank"):
    expected = _reimbursement_sources(book)
    _payment(
        book,
        "alice-reimbursement",
        "alice",
        [
            _allocation("expense", "alice-cost", 10000),
            _allocation("reimbursed_asset", "computer", 20000, "alice"),
            _allocation("reimbursed_deposit", "deposit", 30000, "reimbursement"),
        ],
        channel=channel,
    )
    _payment(
        book,
        "bob-reimbursement",
        "bob",
        [
            _allocation("expense", "bob-cost", 12000),
            _allocation("reimbursed_asset", "computer", 30000, "bob"),
        ],
        channel=channel,
    )
    _payment(
        book,
        "deposit-return",
        "landlord",
        [_allocation("reimbursed_deposit", "deposit", 30000, "refund")],
        channel=channel,
        direction="inflow",
    )
    expected.update(
        {
            "alice-reimbursement": "employee_reimbursement",
            "bob-reimbursement": "employee_reimbursement",
            "deposit-return": "fund_movement",
        }
    )
    return expected


def _loaded_members(engine, data):
    dashboard = Dashboard(engine)
    result = []
    for group in data["collections"]["activity"]["items"]:
        response = dashboard.brief_group(
            PERIOD, section="activity", group_key=group["group_key"],
        )
        while True:
            collection = response["data"]["collections"]["members"]
            result.extend(collection["items"])
            if not collection["page"]["has_more"]:
                break
            response = dashboard.brief_group(
                PERIOD, section="activity", group_key=group["group_key"],
                cursor=collection["page"]["next_cursor"],
                expected_version=response["snapshot_version"],
            )
    return result


def _category(item):
    return item["detail_scope_category"] or item["group"]


def _assert_groups(engine, data, expected, *, complete=True):
    activities = _loaded_members(engine, data)
    expected = {
        ident if isinstance(ident, tuple) else (ident, group): group
        for ident, group in expected.items()
    }
    components = {(item["subject_id"], _category(item)): item for item in activities}
    if complete:
        assert set(components) == set(expected)
    assert {ident: item["group"] for ident, item in components.items()} == {
        ident: expected[ident] for ident in components
    }
    assert {item["key"]: item["event_count"] for item in data["activity_groups"]} == dict(
        Counter(expected.values())
    )
    for group in data["activity_groups"]:
        assert sum(item["count"] for item in group["type_counts"]) == group["event_count"]
    assert len(activities) == len(components)


def test_expense_group_follows_typed_creditor_kind(book):
    engine, save, publish, _ = book
    _expense(save, publish, "supplier-cost", "supplier", 7000, "supplier")
    _expense(save, publish, "employee-cost", "alice", 10000, "employee")
    data = Dashboard(engine).brief(PERIOD, preparation="deferred")["data"]
    _assert_groups(
        engine, data,
        {"supplier-cost": "expense_supplier", "employee-cost": "employee_reimbursement"},
    )


@pytest.mark.parametrize("channel", ["bank", "cash", "platform"])
def test_reimbursement_payments_and_deposit_return_follow_their_exact_obligations(book, channel):
    engine, _, _, _ = book
    expected = _reimbursement_payments(book, channel)
    before = engine.ledger(PERIOD)
    data = Dashboard(engine).brief(PERIOD, preparation="deferred")["data"]
    _assert_groups(engine, data, expected)
    assert engine.ledger(PERIOD) == before


def test_whole_month_groups_stay_complete_when_only_one_voucher_is_loaded(book):
    engine, _, _, _ = book
    expected = _reimbursement_payments(book)
    dashboard = Dashboard(engine)
    response = dashboard.brief(PERIOD, preparation="deferred", limit=1)
    loaded = set()
    while True:
        data = response["data"]
        _assert_groups(engine, data, expected, complete=False)
        items = data["collections"]["activity"]["items"]
        assert len(items) == 1
        subjects = {item["subject_id"] for item in _loaded_members(engine, data)}
        assert not loaded & subjects
        loaded.update(subjects)
        page = data["collections"]["activity"]["page"]
        assert page["total_count"] == data["group_count"]
        if not page["has_more"]:
            break
        response = dashboard.brief(
            PERIOD,
            preparation="deferred",
            section="activity",
            cursor=page["next_cursor"],
            expected_version=response["snapshot_version"],
            limit=1,
        )
    assert loaded == set(expected)


def test_payment_with_supplier_and_employee_sources_has_two_exact_category_parts(book):
    engine, save, publish, _ = book
    _expense(save, publish, "supplier-cost", "supplier", 7000, "supplier")
    _expense(save, publish, "employee-cost", "alice", 10000, "employee")
    save(
        "payment",
        "mixed-batch",
        {
            "period": PERIOD,
            "actual_date": "2026-09-28",
            "direction": "outflow",
            "bank_account_id": "bank",
            "counterparty_id": None,
            "payment_method": "bank_batch",
            "amount_fen": 17000,
            "allocations": [
                _allocation("expense", "supplier-cost", 7000, recipient="supplier"),
                _allocation("expense", "employee-cost", 10000, recipient="alice"),
            ],
        },
    )
    publish("mixed-batch")
    data = Dashboard(engine).brief(PERIOD, preparation="deferred")["data"]
    _assert_groups(
        engine, data,
        {
            "supplier-cost": "expense_supplier",
            "employee-cost": "employee_reimbursement",
            ("mixed-batch", "expense_supplier"): "expense_supplier",
            ("mixed-batch", "employee_reimbursement"): "employee_reimbursement",
        },
    )
    assert data["activity_count"] == 4
    assert data["voucher_count"] == 3
    parts = [item for item in _loaded_members(engine, data) if item["subject_id"] == "mixed-batch"]
    assert {_category(item): item["amount_fen"] for item in parts} == {
        "expense_supplier": 7000, "employee_reimbursement": 10000,
    }


def test_closed_month_expense_and_payment_keep_their_published_source_after_a_later_draft(book):
    engine, save, publish, proof = book
    opening(save, publish, bank="bank")
    funding(save, publish, subject="capital", amount=10000, bank="bank")
    _expense(save, publish, "employee-cost", "alice", 10000, "employee")
    _payment(book, "employee-paid", "alice", [_allocation("expense", "employee-cost", 10000)])
    statement(
        save,
        publish,
        [entry("capital", amount=10000), entry("employee-paid", "2026-09-28", -10000)],
        bank="bank",
    )
    reconciliation(
        save,
        publish,
        [
            {"reference": "capital", "source_kind": "funding", "source_id": "capital"},
            {"reference": "employee-paid", "source_kind": "payment", "source_id": "employee-paid"},
        ],
        bank="bank",
    )
    assert (
        close_month(
            inventories(engine, proof, PERIOD, {"transactions", "bank", "financing"}), proof, PERIOD
        )["status"]
        == "closed"
    )
    original = engine.ledger(PERIOD)
    save(
        "expense",
        "employee-cost",
        {
            "period": PERIOD,
            "counterparty_id": "alice",
            "amount_fen": 10000,
            "expense_class": "administration",
            "creditor_kind": "supplier",
        },
        revision=1,
    )
    data = Dashboard(engine).brief(PERIOD, preparation="deferred")["data"]
    _assert_groups(
        engine, data,
        {
            "capital": "financing_owner",
            "employee-cost": "employee_reimbursement",
            "employee-paid": "employee_reimbursement",
        },
    )
    assert engine.ledger(PERIOD) == original


@pytest.mark.parametrize("source_scope", ["off_page_expense", "prior_month_payment_source"])
@pytest.mark.parametrize("corruption", ["duplicate_key", "digest"])
def test_bounded_activity_summary_rejects_damaged_off_page_classification_sources(
    book, source_scope, corruption
):
    engine, save, publish, _ = book
    funding(save, publish, subject="first-voucher", amount=10000, bank="bank")
    if source_scope == "off_page_expense":
        _expense(save, publish, "employee-cost", "alice", 10000, "employee")
        expected = {
            "first-voucher": "financing_owner",
            "employee-cost": "employee_reimbursement",
        }
    else:
        save(
            "expense",
            "employee-cost",
            {
                "period": "2026-08",
                "counterparty_id": "alice",
                "amount_fen": 10000,
                "expense_class": "administration",
                "creditor_kind": "employee",
            },
        )
        publish("employee-cost")
        _payment(book, "employee-paid", "alice", [_allocation("expense", "employee-cost", 10000)])
        expected = {
            "first-voucher": "financing_owner",
            "employee-paid": "employee_reimbursement",
        }
    before = Dashboard(engine).brief(PERIOD, preparation="deferred", limit=1)["data"]
    _assert_groups(engine, before, expected, complete=False)
    assert _loaded_members(engine, before)[0]["subject_id"] == "first-voucher"
    with engine.store.connection(read_only=True) as connection:
        source = connection.execute(
            "SELECT c.id,c.outcome FROM calculation c "
            "JOIN calculation_current selected ON selected.calculation_id=c.id "
            "WHERE selected.subject_id='employee-cost'"
        ).fetchone()
    if corruption == "duplicate_key":
        # The final values object is unchanged; a permissive decoder would
        # accept this duplicate key and reproduce the original content digest.
        duplicate = '{"values":{},' + source["outcome"][1:]
        damage(
            engine,
            "calculation",
            "UPDATE calculation SET outcome=? WHERE id=?",
            (duplicate, source["id"]),
        )
    else:
        damage(
            engine,
            "calculation",
            "UPDATE calculation SET digest=zeroblob(32) WHERE id=?",
            (source["id"],),
        )
    with pytest.raises(KernelError) as failure:
        Dashboard(engine).brief(PERIOD, preparation="deferred", limit=1)
    assert failure.value.code == "content_integrity_failed"


def test_retained_bank_verification_payment_is_income(book):
    engine, save, publish, _ = book
    save(
        "bank_income",
        "verification-income",
        {
            "period": PERIOD,
            "actual_date": "2026-09-21",
            "bank_account_id": "bank",
            "counterparty_id": "verification-provider",
            "amount_fen": 1,
            "income_kind": "retained_verification_payment",
            "entitlement_confirmed": True,
        },
    )
    publish("verification-income")
    data = Dashboard(engine).brief(PERIOD, preparation="deferred")["data"]
    _assert_groups(engine, data, {"verification-income": "income_customer"})
    assert data["collections"]["activity"]["items"][0]["amount_fen"] == 1


def test_overpayment_confirmation_and_its_actual_refund_keep_fund_movement_group(book):
    engine, save, publish, _ = book
    _expense(save, publish, "supplier-cost", "supplier", 1000, "supplier")
    save(
        "payment",
        "supplier-overpaid",
        {
            "period": PERIOD,
            "actual_date": "2026-09-28",
            "direction": "outflow",
            "bank_account_id": "bank",
            "counterparty_id": "supplier",
            "amount_fen": 1100,
            "allocations": [_allocation("expense", "supplier-cost", 1100)],
        },
    )
    save(
        "overpayment",
        "recovery",
        {
            "period": PERIOD,
            "source_kind": "expense",
            "source_id": "supplier-cost",
            "obligation_name": "primary",
            "counterparty_id": "supplier",
            "amount_fen": 100,
            "recovery_right_confirmed": True,
        },
    )
    publish("supplier-overpaid", "recovery")
    _payment(
        book,
        "overpayment-returned",
        "supplier",
        [_allocation("overpayment", "recovery", 100)],
        direction="inflow",
    )
    data = Dashboard(engine).brief(PERIOD, preparation="deferred")["data"]
    _assert_groups(
        engine, data,
        {
            "supplier-cost": "expense_supplier",
            "supplier-overpaid": "expense_supplier",
            "recovery": "fund_movement",
            "overpayment-returned": "fund_movement",
        },
    )
    paid = next(item for item in _loaded_members(engine, data)
                if item["subject_id"] == "supplier-overpaid")
    assert paid["amount_fen"] == 1100


def _receipt_sources(book, *, period="2026-08"):
    engine, save, publish, _ = book
    profile(engine, "counterparty", "customer", display_name="真实最终收款客户")
    save("vat_policy", "vat", {
        "period": "2026-01", "policy": vat_policy().model_dump(mode="json"),
    })
    save("service_sale", "sale", {
        "period": period, "customer_id": "customer", "gross_fen": 10100,
        "vat_policy_id": "vat", "exemption_eligible": False,
        "tax_obligation_period": period,
    })
    save("pass_through", "entrusted", {
        "period": period, "payer_id": "customer", "beneficiary_id": "customer",
        "amount_fen": 3000, "rights_and_obligation_confirmed": True,
    })
    publish("sale", "entrusted")


def _classification_path(dashboard, path):
    """Exercise each reader explicitly while retaining real publication proofs."""
    with dashboard._snapshot(PERIOD) as snap:
        source_ids = {row[0] for row in snap.connection.execute(
            "SELECT c.id FROM calculation c JOIN calculation_current selected "
            "ON selected.calculation_id=c.id WHERE selected.subject_id IN ('sale','entrusted')",
        )}
        if path == "sql":
            assert snap.month_journal.verified_rows() is None
        else:
            snap.month_journal.account_amounts()
            assert snap.month_journal.verified_rows() is not None
            if path == "decoded":
                snap.reads.verify_selected_content(source_ids)
                assert source_ids <= snap.reads._verified_source_contents.keys()
            else:
                # Drop decoded bodies only; their authenticated byte proofs
                # and immutable publication anchors remain intact.
                for ident in source_ids:
                    snap.reads._verified_source_contents.pop(ident, None)
                assert not source_ids & snap.reads._verified_source_contents.keys()
        queries = []
        snap.connection.set_trace_callback(queries.append)
        components = snap.activity_components
        snap.connection.set_trace_callback(None)
        assert components
        source_body_reads = [
            sql for sql in queries
            if "json_type(outcome,'$.values.obligations')" in sql
            and any(ident in sql for ident in source_ids)
        ]
        assert bool(source_body_reads) == (path != "decoded")
        if source_body_reads:
            assert len(source_body_reads) == 1
            assert all(ident in source_body_reads[0] for ident in source_ids)
            assert "json_extract(outcome,'$.values')" not in source_body_reads[0]
        return components


@pytest.mark.parametrize("channel", ["bank", "cash", "platform"])
@pytest.mark.parametrize("path", ["decoded", "missing", "sql"])
def test_mixed_customer_receipt_splits_exact_categories_across_readers_and_channels(
    book, channel, path,
):
    engine, _, _, _ = book
    _receipt_sources(book)
    _payment(book, "mixed-receipt", "customer", [
        _allocation("service_sale", "sale", 10100),
        _allocation("pass_through", "entrusted", 3000, "collection"),
    ], channel=channel, direction="inflow")
    dashboard = Dashboard(engine)
    before = engine.ledger(PERIOD)
    components = _classification_path(dashboard, path)
    assert len(components) == 1
    parts = next(iter(components.values()))
    assert {item["source_category"]: item["amount_fen"] for item in parts} == {
        "income_customer": 10100, "pass_through": 3000,
    }
    assert len({item["key"] for item in parts}) == 2
    assert {index for item in parts for index in item["slots"]} == {0, 1}
    response = dashboard.brief(PERIOD, preparation="deferred", limit=1)
    _assert_groups(engine, response["data"], {
        ("mixed-receipt", "income_customer"): "income_customer",
        ("mixed-receipt", "pass_through"): "pass_through",
    }, complete=False)
    activity = response["data"]["collections"]["activity"]["items"][0]
    assert activity["amount_fen"] in {10100, 3000}
    assert response["data"]["collections"]["activity"]["page"]["total_count"] == 2
    assert response["data"]["activity_count"] == 2
    assert response["data"]["voucher_count"] == 1
    voucher = dashboard.brief_group(
        PERIOD, section="activity", group_key=activity["group_key"],
    )["data"]["collections"]["vouchers"]["items"][0]
    assert voucher["business_amount_fen"] == 13100
    assert {
        line["code"]: line["credit_fen"] for line in voucher["lines"] if line["credit_fen"]
    } == {
        "1122": 10100, "122105": 3000,
    }
    # The two adopted parts remain distinct, including when details cross a page.
    details = dashboard.business_status(PERIOD, "mixed-receipt", limit=1)
    events = []
    while True:
        collection = details["data"]["collections"]["settlement_events"]
        assert collection["page"]["total_count"] == 2
        events.extend(collection["items"])
        if not collection["page"]["has_more"]:
            break
        details = dashboard.business_status(
            PERIOD, "mixed-receipt", section="settlement_events", limit=1,
            cursor=collection["page"]["next_cursor"], expected_version=details["snapshot_version"],
        )
    assert {(item["name"], item["signed_amount_fen"]) for item in events} == {
        ("primary", 10100), ("collection", 3000),
    }
    assert engine.ledger(PERIOD) == before


@pytest.mark.parametrize("with_collection", [False, True])
def test_receipt_with_unrelated_and_third_sources_preserves_categories(book, with_collection):
    engine, save, publish, _ = book
    _receipt_sources(book)
    save("advance", "advance", {
        "period": "2026-08", "side": "customer", "counterparty_id": "customer",
        "amount_fen": 2000, "contractual_obligation_established": True,
        "vat_due_on_advance": False,
    })
    publish("advance")
    allocations = [
        _allocation("service_sale", "sale", 10100), _allocation("advance", "advance", 2000),
    ]
    if with_collection:
        allocations.append(_allocation("pass_through", "entrusted", 3000, "collection"))
    _payment(book, "mixed-other", "customer", allocations, direction="inflow")
    expected = {
        ("mixed-other", "income_customer"): "income_customer", ("mixed-other", "other"): "other",
    }
    if with_collection:
        expected["mixed-other", "pass_through"] = "pass_through"
    data = Dashboard(engine).brief(PERIOD, preparation="deferred")["data"]
    _assert_groups(engine, data, expected)
    assert {_category(item): item["amount_fen"]
            for item in _loaded_members(engine, data)} == {
        "income_customer": 10100, "other": 2000,
    } | ({"pass_through": 3000} if with_collection else {})


def test_mixed_receipt_group_totals_cover_all_activity_pages(book):
    engine, _, _, _ = book
    _receipt_sources(book, period=PERIOD)
    _payment(book, "mixed-receipt", "customer", [
        _allocation("service_sale", "sale", 10100),
        _allocation("pass_through", "entrusted", 3000, "collection"),
    ], direction="inflow")
    expected = {
        ("sale", "income_customer"): "income_customer",
        ("entrusted", "pass_through"): "pass_through",
        ("mixed-receipt", "income_customer"): "income_customer",
        ("mixed-receipt", "pass_through"): "pass_through",
    }
    dashboard = Dashboard(engine)
    response = dashboard.brief(PERIOD, preparation="deferred", limit=1)
    loaded = set()
    while True:
        data = response["data"]
        _assert_groups(engine, data, expected, complete=False)
        collection = data["collections"]["activity"]
        assert collection["page"]["total_count"] == 4
        assert data["activity_count"] == 4
        assert data["voucher_count"] == 3
        subjects = {(item["subject_id"], _category(item))
                    for item in _loaded_members(engine, data)}
        assert not loaded & subjects
        loaded.update(subjects)
        if not collection["page"]["has_more"]:
            break
        response = dashboard.brief(
            PERIOD, preparation="deferred", section="activity", limit=1,
            cursor=collection["page"]["next_cursor"], expected_version=response["snapshot_version"],
        )
    assert loaded == set(expected)


def test_closed_mixed_receipt_keeps_exact_adopted_sources_after_later_drafts(book):
    engine, save, publish, proof = book
    opening(save, publish, bank="bank")
    _receipt_sources(book, period=PERIOD)
    _payment(book, "mixed-receipt", "customer", [
        _allocation("service_sale", "sale", 10100),
        _allocation("pass_through", "entrusted", 3000, "collection"),
    ], direction="inflow")
    save("surtax_policy", "surtax", {
        "period": "2026-01", "policy": surtax_policy().model_dump(mode="json"),
    })
    save("tax_assessment", "assessment", {
        "period": PERIOD, "period_start": "2026-09-01", "period_end": "2026-09-30",
        "vat_policy_id": "vat", "surtax_policy_id": "surtax",
    })
    publish("assessment")
    statement(save, publish, [entry("mixed-receipt", "2026-09-28", 13100)], bank="bank")
    reconciliation(save, publish, [{
        "reference": "mixed-receipt", "source_kind": "payment", "source_id": "mixed-receipt",
    }], bank="bank")
    assert close_month(
        inventories(engine, proof, PERIOD, {"transactions", "bank", "tax"}), proof, PERIOD,
    )["status"] == "closed"
    original_ledger = engine.ledger(PERIOD)
    before = BusinessQueries(engine).business_status("mixed-receipt", PERIOD)
    adopted_parts = {
        (item["source_calculation_id"], item["obligation_key"], item["signed_amount_fen"])
        for item in before["settlements"]["movements"]
    }
    assert {amount for _, _, amount in adopted_parts} == {10100, 3000}
    save("service_sale", "sale", {
        "period": PERIOD, "customer_id": "customer", "gross_fen": 20200,
        "vat_policy_id": "vat", "exemption_eligible": False, "tax_obligation_period": PERIOD,
    }, revision=1)
    save("pass_through", "entrusted", {
        "period": PERIOD, "payer_id": "customer", "beneficiary_id": "customer",
        "amount_fen": 6000, "rights_and_obligation_confirmed": True,
    }, revision=1)
    data = Dashboard(engine).brief(PERIOD, preparation="deferred")["data"]
    receipt = [item for item in _loaded_members(engine, data)
               if item["subject_id"] == "mixed-receipt"]
    assert {_category(item): item["amount_fen"] for item in receipt} == {
        "income_customer": 10100, "pass_through": 3000,
    }
    after = BusinessQueries(engine).business_status("mixed-receipt", PERIOD)
    assert {(item["source_calculation_id"], item["obligation_key"], item["signed_amount_fen"])
            for item in after["settlements"]["movements"]} == adopted_parts
    detail = Dashboard(engine).business_status(
        PERIOD, "mixed-receipt", settlement_view="historical",
    )
    events = detail["data"]["collections"]["settlement_events"]["items"]
    assert {(item["name"], item["signed_amount_fen"]) for item in events} == {
        ("primary", 10100), ("collection", 3000),
    }
    assert engine.ledger(PERIOD) == original_ledger


def test_pure_entrusted_collection_and_remittance_use_pass_through_group(book):
    engine, _, _, _ = book
    _receipt_sources(book)
    for subject, obligation, direction in (
        ("collection", "collection", "inflow"), ("remittance", "remittance", "outflow"),
    ):
        _payment(book, subject, "customer", [
            _allocation("pass_through", "entrusted", 3000, obligation),
        ], direction=direction)
    _assert_groups(engine, Dashboard(engine).brief(PERIOD, preparation="deferred")["data"], {
        "collection": "pass_through", "remittance": "pass_through",
    })


def test_entrusted_refund_liability_and_actual_return_keep_pass_through_category(book):
    engine, save, publish, _ = book
    _receipt_sources(book)
    _payment(book, "collected", "customer", [
        _allocation("pass_through", "entrusted", 3000, "collection"),
    ], direction="inflow")
    save("pass_through_return", "refund-confirmed", {
        "period": PERIOD, "source_id": "entrusted", "amount_fen": 1000,
        "refund_right_confirmed": True,
    })
    publish("refund-confirmed")
    _payment(book, "returned", "customer", [
        _allocation("pass_through_return", "refund-confirmed", 1000),
    ])
    data = Dashboard(engine).brief(PERIOD)["data"]
    _assert_groups(engine, data, {
        "collected": "pass_through", "refund-confirmed": "pass_through",
        "returned": "pass_through",
    })


def test_income_return_and_entrusted_remittance_mixed_payment_splits_categories(book):
    engine, save, publish, _ = book
    _receipt_sources(book)
    save("sale_return", "return", {
        "period": "2026-08", "sale_id": "sale", "customer_id": "customer",
        "returned_gross_fen": 1010, "credit_note_vat_fen": 10,
    })
    publish("return")
    _payment(book, "mixed-outflow", "customer", [
        _allocation("sale_return", "return", 1010),
        _allocation("pass_through", "entrusted", 3000, "remittance"),
    ])
    _assert_groups(
        engine, Dashboard(engine).brief(PERIOD, preparation="deferred")["data"], {
            ("mixed-outflow", "income_customer"): "income_customer",
            ("mixed-outflow", "pass_through"): "pass_through",
        },
    )


@pytest.mark.parametrize("kind", ["payment", "cash_payment", "platform_payment"])
@pytest.mark.parametrize("direction,name", [("inflow", "collection"), ("outflow", "remittance")])
def test_mixed_receipt_reversal_always_remains_correction(kind, direction, name):
    sources = [
        _settlement_source("service_sale"),
        _settlement_source("pass_through", obligation_name=name),
    ]
    assert _group(kind, True, settlement_sources=sources, direction=direction) == "correction"


@pytest.mark.parametrize("name", [None, "collection", "remittance", "unknown"])
def test_whole_voucher_category_does_not_fold_income_and_entrusted_collection(name):
    if name not in {"collection", "remittance"}:
        with pytest.raises(KernelError) as failure:
            _settlement_source("pass_through", obligation_name=name)
        assert failure.value.code == "content_integrity_failed"
        return
    sources = [
        _settlement_source("service_sale"),
        _settlement_source("pass_through", obligation_name=name),
    ]
    assert _group("payment", settlement_sources=sources, direction="inflow") == "other"


@pytest.mark.parametrize("creditor_kind", [None, "", "other", True, [], {}])
def test_expense_confirmation_rejects_missing_or_unknown_creditor_meaning(creditor_kind):
    with pytest.raises(KernelError) as error:
        _group("expense", creditor_kind=creditor_kind)
    assert error.value.code == "content_integrity_failed"


@pytest.mark.parametrize(
    "corruption", ["duplicate_key", "digest", "kind", "obligation_name", "obligation_key"],
)
def test_cross_month_mixed_receipt_rejects_damaged_entrusted_source(book, corruption):
    engine, _, _, _ = book
    _receipt_sources(book)
    _payment(book, "mixed-receipt", "customer", [
        _allocation("service_sale", "sale", 10100),
        _allocation("pass_through", "entrusted", 3000, "collection"),
    ], direction="inflow")
    with engine.store.connection(read_only=True) as connection:
        source = connection.execute(
            "SELECT c.id,c.outcome FROM calculation c JOIN calculation_current selected "
            "ON selected.calculation_id=c.id WHERE selected.subject_id='entrusted'",
        ).fetchone()
    if corruption == "digest":
        damage(
            engine, "calculation", "UPDATE calculation SET digest=zeroblob(32) WHERE id=?",
            (source["id"],),
        )
    elif corruption == "kind":
        damage(
            engine, "calculation", "UPDATE calculation SET kind='advance' WHERE id=?",
            (source["id"],),
        )
    else:
        body = (
            '{"values":{},' + source["outcome"][1:] if corruption == "duplicate_key"
            else source["outcome"].replace('"name":"collection"', '"name":"remittance"')
            if corruption == "obligation_name"
            else source["outcome"].replace('"key":"pass_through:entrusted:collection"',
                                          '"key":"pass_through:entrusted:unrelated"')
        )
        assert body != source["outcome"]
        damage(
            engine, "calculation", "UPDATE calculation SET outcome=? WHERE id=?",
            (body, source["id"]),
        )
    with pytest.raises(KernelError) as failure:
        Dashboard(engine).brief(PERIOD, preparation="deferred", limit=1)
    assert failure.value.code == "content_integrity_failed"
