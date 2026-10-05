"""A composite bank match still presents one original row and its adopted sources."""

import test_banking as banking

from ai_accounting.kernel.dashboard import Dashboard, _Snapshot
from ai_accounting.kernel.dashboard_funds import FundsRead

bank_book = banking.book
MONTH = "2026-09"


def test_composite_bank_row_counts_once_and_binds_both_frozen_sources(bank_book):
    engine, save, publish, proof = bank_book
    banking.opening(save, publish)
    banking.funding(save, publish, amount=7000)
    save(
        "expense",
        "water-expense",
        {
            "period": MONTH,
            "counterparty_id": "water-provider",
            "amount_fen": 1100,
            "expense_class": "administration",
            "creditor_kind": "supplier",
        },
    )
    publish("water-expense")
    save(
        "payment",
        "water-payment",
        {
            "period": MONTH,
            "actual_date": "2026-09-10",
            "direction": "outflow",
            "bank_account_id": "bank-a",
            "counterparty_id": "water-provider",
            "amount_fen": 1100,
            "allocations": [
                {
                    "source_kind": "expense",
                    "source_id": "water-expense",
                    "obligation": "primary",
                    "amount_fen": 1100,
                }
            ],
        },
    )
    save(
        "managed_reserve_expense",
        "reserve-transfer",
        {
            "period": MONTH,
            "actual_date": "2026-09-10",
            "bank_account_id": "bank-a",
            "counterparty_id": "reserve-provider",
            "amount_fen": 5000,
        },
    )
    publish("water-payment", "reserve-transfer")
    banking.statement(
        save,
        publish,
        [banking.entry("funding", amount=7000), banking.entry("combined", "2026-09-10", -6100)],
    )
    banking.reconciliation(
        save,
        publish,
        [
            banking.match("funding"),
            banking.match("combined", "payment", "water-payment"),
            banking.match("combined", "managed_reserve_expense", "reserve-transfer"),
        ],
    )
    banking.close_month(
        banking.inventories(engine, proof, MONTH, {"bank", "transactions"}), proof, MONTH
    )

    dashboard = Dashboard(engine)
    first = dashboard.funds(MONTH, section="statements", limit=1)
    summary = first["data"]["bank_statement"]
    assert summary["transaction_count"] == 2
    assert summary["review_state"] == "complete"
    assert (summary["inflow_fen"], summary["outflow_fen"]) == (7000, 6100)
    page = first["data"]["collections"]["statements"]
    assert page["page"]["total_count"] == page["page"]["filtered_count"] == 2
    assert page["page"]["has_more"]
    second = dashboard.funds(
        MONTH,
        section="statements",
        limit=1,
        cursor=page["page"]["next_cursor"],
        expected_version=first["snapshot_version"],
    )["data"]["collections"]["statements"]
    assert second["page"]["total_count"] == second["page"]["filtered_count"] == 2
    assert not second["page"]["has_more"]
    assert page["items"][0]["id"] != second["items"][0]["id"]
    combined = next(
        item for item in [*page["items"], *second["items"]] if item["signed_amount_fen"] == -6100
    )
    assert not {"state", "reference", "source_check", "party_sources"} & combined.keys()
    assert combined["amount_fen"] == 6100
    assert combined["party"] == "组合付款 · 2 项"
    assert combined["batch_payment"]["bank_row_count"] == 1
    assert combined["batch_payment"]["total_fen"] == 6100
    assert sorted(item["amount_fen"] for item in combined["batch_payment"]["items"]) == [
        1100,
        5000,
    ]
    with engine.store.connection(read_only=True) as connection:
        snap = _Snapshot(engine, connection, MONTH)
        read = FundsRead(snap)
        summary = read.bank_summary(
            page_request={"after": None, "limit": 10, "where": "1=1", "filters": []}
        )
        assert summary["matched_count"] == 2
        rows = read.shared_pages["statements"][0]
        read.prepare_bank_items(rows)
        original = next(row for row in rows if row["reference"] == "combined")
        assert original["state"] == "matched"
        selected = read.bank_match_calculations[original["page_key"]]
        assert {item["fact"]["data"]["counterparty_id"] for item in selected} == {
            "water-provider",
            "reserve-provider",
        }
        assert {item["subject_id"] for item in selected} == {
            "water-payment",
            "reserve-transfer",
        }
        assert {item["id"] for item in selected} == {
            row[0]
            for row in connection.execute(
                "SELECT upstream_id FROM dependency_calculation WHERE calculation_id=? "
                "AND upstream_id IN (SELECT id FROM calculation WHERE subject_id IN "
                "('water-payment','reserve-transfer'))",
                (original["reconciliation_calculation_id"],),
            )
        }
