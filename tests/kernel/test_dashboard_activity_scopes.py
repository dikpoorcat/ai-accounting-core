"""Exact payment scopes constrain money, parties and slots before pagination."""

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
from test_dashboard_provenance import profile
from test_identity_corrections import (
    confirm,
    opening_package,
    publish_subjects,
)
from test_identity_corrections import (
    identity_engine as identity_engine,
)
from test_opening_continuation import _close_without_current_business

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.response_contracts import validate_response

PERIOD = "2026-09"


def mixed_payment(book, *, same_supplier=False):
    engine, save, publish, _ = book
    allocations = []
    for subject, party, amount, creditor in (
        ("supplier-a", "vendor-a", 7000, "supplier"),
        ("employee", "alice", 10000, "employee"),
        ("supplier-b", "vendor-a" if same_supplier else "vendor-b", 9000, "supplier"),
    ):
        save("expense", subject, {"period": PERIOD, "counterparty_id": party,
             "amount_fen": amount, "expense_class": "administration", "creditor_kind": creditor})
        publish(subject)
        profile(engine, "counterparty", party, display_name=party)
        allocations.append({"source_kind": "expense", "source_id": subject,
                            "obligation": "primary", "amount_fen": amount, "recipient_id": party})
    data = {"period": PERIOD, "actual_date": "2026-09-28", "direction": "outflow",
            "bank_account_id": "bank", "counterparty_id": None, "payment_method": "bank_batch",
            "amount_fen": 26000, "allocations": allocations}
    save("payment", "mixed", data)
    publish("mixed")
    profile(engine, "business", "mixed", display_name="整单名称", purpose="整单用途",
            note="包含全部收款人")
    dashboard = Dashboard(engine)
    with dashboard._snapshot(PERIOD) as snap:
        voucher = next(row for row in snap.month_journal if row["basis"]["subject_id"] == "mixed")
    return dashboard, voucher["id"], "expense_supplier", data


def scoped(dashboard, voucher, category, **kwargs):
    return dashboard.business_status(PERIOD, "mixed", voucher_version_id=voucher,
                                     detail_scope_category=category, **kwargs)


def test_scope_selects_slots_before_pagination_and_filters_profiles(book):
    dashboard, voucher, category, _ = mixed_payment(book)
    result = scoped(dashboard, voucher, category, limit=1)
    validate_response("dashboard_business_status", result)
    data = result["data"]
    assert result["schema_version"] == 9
    assert data["detail_scope"]["amount_fen"] == 16000
    assert data["current_business_result"]["amount_fen"] == 16000
    for summary in (data["settlements"], data["current_followups"]["settlements"]):
        assert {item["key"] for item in summary["obligations"]} == {
            "expense:supplier-a:primary", "expense:supplier-b:primary",
        }
        assert sum(item["source_amount_fen"] for item in summary["obligations"]) == 16000
        assert sum(item["remaining_fen"] for item in summary["obligations"]) == 0
    profiles = data["display_profiles"]
    assert {item["entity_id"] for item in profiles["counterparties"]} == {"vendor-a", "vendor-b"}
    assert all(profiles["business"]["values"][field] is None
               for field in ("display_name", "purpose", "note"))
    page = data["collections"]["settlement_events"]["page"]
    assert page["total_count"] == page["filtered_count"] == 2
    assert page["returned_count"] == 1 and page["has_more"]
    first = data["collections"]["settlement_events"]["items"][0]
    assert first["purpose_label"] == "费用"
    second = scoped(dashboard, voucher, category, limit=1, section="settlement_events",
                    cursor=page["next_cursor"], expected_version=result["snapshot_version"])
    item = second["data"]["collections"]["settlement_events"]["items"][0]
    assert {first["source_subject_id"], item["source_subject_id"]} == {"supplier-a", "supplier-b"}
    assert not second["data"]["collections"]["settlement_events"]["page"]["has_more"]


def test_scope_cursor_rejects_another_category_and_half_scope(book):
    dashboard, voucher, category, _ = mixed_payment(book)
    result = scoped(dashboard, voucher, category, limit=1)
    with dashboard._snapshot(PERIOD) as snap:
        other = next(part["source_category"] for part in snap.activity_components[voucher]
                     if part["source_category"] != category)
    with pytest.raises(KernelError) as error:
        scoped(dashboard, voucher, other, section="settlement_events", limit=1,
               cursor=result["data"]["collections"]["settlement_events"]["page"]["next_cursor"])
    assert error.value.code == "dashboard_snapshot_changed"
    with pytest.raises(KernelError) as error:
        dashboard.business_status(PERIOD, "mixed", voucher_version_id=voucher)
    assert error.value.code == "invalid_command"
    with pytest.raises(KernelError):
        dashboard.business_status(PERIOD, "supplier-a", voucher_version_id=voucher,
                                  detail_scope_category=category)


def test_unscoped_status_retains_whole_payment_semantics(book):
    dashboard, _, _, _ = mixed_payment(book)
    result = dashboard.business_status(PERIOD, "mixed")
    validate_response("dashboard_business_status", result)
    assert result["data"]["detail_scope"] is None
    assert result["data"]["current_business_result"]["amount_fen"] == 26000
    assert result["data"]["collections"]["settlement_events"]["page"]["total_count"] == 3


@pytest.mark.parametrize("current", [False, True])
def test_selected_slot_filter_precedes_shared_collection_page(book, current):
    dashboard, voucher, _, _ = mixed_payment(book)
    with dashboard._snapshot(PERIOD) as snap:
        result = snap.queries.business_collection(
            snap.connection, "mixed", PERIOD, section="settlement_events", limit=1,
            current=current, allowed_slots={(voucher, 0), (voucher, 2)},
        )
        assert result["page"]["total_count"] == 2
        assert result["items"][0]["source_business"]["subject_id"] == "supplier-a"
        second = snap.queries.business_collection(
            snap.connection, "mixed", PERIOD, section="settlement_events", limit=1,
            current=current, allowed_slots={(voucher, 0), (voucher, 2)},
            after=result["page"]["next_cursor"],
        )
        assert second["items"][0]["source_business"]["subject_id"] == "supplier-b"
        assert not second["page"]["has_more"]


def test_frozen_part_and_later_reversal_keep_exact_money_and_basis(book):
    engine, save, publish, proof = book
    opening(save, publish, bank="bank")
    funding(save, publish, subject="capital", amount=30000, bank="bank")
    dashboard, voucher, category, payment = mixed_payment(book, same_supplier=True)
    statement(save, publish, [entry("capital", amount=30000),
                             entry("mixed", "2026-09-28", -26000)], bank="bank")
    reconciliation(save, publish, [
        {"reference": "capital", "source_kind": "funding", "source_id": "capital"},
        {"reference": "mixed", "source_kind": "payment", "source_id": "mixed"},
    ], bank="bank")
    assert close_month(inventories(engine, proof, PERIOD, {"bank", "transactions", "financing"}),
                       proof, PERIOD)["status"] == "closed"
    allocations = payment["allocations"]
    save("payment", "mixed", {**payment, "allocations": allocations[::-1]}, revision=1)
    preview = engine.preview(["mixed"], posting_period="2026-10")
    engine.confirm(["mixed"], posting_period="2026-10", preview_digest=preview["digest"],
                   epochs=preview["epochs"], request_id="scoped-later-correction")
    result = scoped(dashboard, voucher, category)
    validate_response("dashboard_business_status", result)
    assert result["data"]["detail_scope"]["amount_fen"] == 16000
    assert result["data"]["current_business_result"] is None
    assert result["data"]["frozen_adoption"]["amount_fen"] == 16000
    assert result["data"]["current_followups"]["settlements"]["cutoff_period"] == "2026-10"
    with dashboard._snapshot("2026-10") as snap:
        reversal = next(row for row in snap.month_journal if row["reverses_id"] == voucher)
    reversed_result = dashboard.business_status(
        "2026-10", "mixed", voucher_version_id=reversal["id"], detail_scope_category=category,
    )
    validate_response("dashboard_business_status", reversed_result)
    data = reversed_result["data"]
    assert data["detail_scope"]["amount_fen"] == -16000
    assert data["current_business_result"] is None and data["frozen_adoption"] is None
    items = data["collections"]["settlement_events"]["items"]
    assert {item["source_subject_id"] for item in items} == {"supplier-a", "supplier-b"}
    assert sum(item["signed_amount_fen"] for item in items) == -16000


def test_scope_uses_the_exact_frozen_opening_identity_binding(identity_engine):
    engine, proof, original_party, adopted_party = identity_engine
    account = Entities(engine).register_entity(
        "fund_account", {}, account_type="cash", source="synthetic", request_id="cash-account",
    )["entity_id"]
    fields = {"counterparty_id": original_party, "nature": "customer_receivable",
              "outstanding_fen": 20000, "business_reference": "confirmed-invoice"}
    opening_package(engine, proof, [
        ("opening_obligation", "receivable", fields),
        ("opening_equity", "capital", {
            "equity_kind": "retained_earnings", "balance_fen": 20000,
            "holder_or_basis_id": original_party,
        }),
    ])
    _close_without_current_business(engine, "2026-01", proof)
    preview, _ = confirm(engine, {
        "changes": [{"subject_id": "receivable", "expected_revision": 1, "action": "reassign",
                     "data": {"period": "2026-01", "package_id": "opening", **fields,
                              "counterparty_id": adopted_party}}],
        "evidence": [proof], "reason": "explicit synthetic identity correction",
        "posting_period": "2026-02",
    })
    binding = next(item["calculation_id"] for item in preview["results"]
                   if item["kind"] == "opening_identity_binding")
    engine.save_fact("cash_payment", "receipt", {
        "period": "2026-02", "actual_date": "2026-02-05", "direction": "inflow",
        "cash_account_id": account, "counterparty_id": adopted_party, "amount_fen": 12000,
        "allocations": [{"source_kind": "opening_obligation", "source_id": "receivable",
                         "obligation": "primary", "amount_fen": 12000}],
    }, evidence=(proof,), expected_revision=0, request_id="save-receipt")
    result = engine.preview(["receipt"])
    assert result["results"][0]["values"]["settlements"][0]["binding_calculation_id"] == binding
    publish_subjects(engine, ["receipt"], "publish-receipt")
    dashboard = Dashboard(engine)
    with dashboard._snapshot("2026-02") as snap:
        voucher = next(row for row in snap.month_journal if row["basis"]["subject_id"] == "receipt")
    response = dashboard.business_status("2026-02", "receipt", voucher_version_id=voucher["id"],
                                         detail_scope_category="income_customer")
    validate_response("dashboard_business_status", response)
    data = response["data"]
    assert data["detail_scope"]["amount_fen"] == 12000
    assert {item["entity_id"] for item in data["display_profiles"]["counterparties"]} == {
        adopted_party,
    }
    assert data["settlements"]["obligations"][0]["remaining_fen"] == 8000
    assert data["collections"]["settlement_events"]["items"][0]["source_subject_id"] == "receivable"
