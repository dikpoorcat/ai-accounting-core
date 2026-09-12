"""A source's complete settlement totals survive bounded historical event pages."""

import pytest
from test_opening_continuation import book as _opening_book

from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard

opening_book = _opening_book


def test_many_payments_page_from_employee_source_into_exact_historical_business(
    opening_book, monkeypatch
):
    engine, save, publish, package, _ = opening_book
    package(
        [
            ("opening_bank", "bank-start", {"bank_account_id": "bank", "balance_fen": 2000}),
            (
                "opening_payroll_payable",
                "prior-net",
                {
                    "employee_id": "employee",
                    "recipient_id": "employee",
                    "payroll_period": "2025-12",
                    "component": "net",
                    "outstanding_fen": 2000,
                },
            ),
        ]
    )
    for index in range(7):
        period = "2026-01" if index < 6 else "2026-02"
        amount = 100 if index < 6 else 50
        save(
            "payment",
            f"paid-{index}",
            {
                "period": period,
                "actual_date": period + "-15",
                "direction": "outflow",
                "bank_account_id": "bank",
                "counterparty_id": "employee",
                "amount_fen": amount,
                "allocations": [
                    {
                        "source_kind": "opening_payroll_payable",
                        "source_id": "prior-net",
                        "obligation": "primary",
                        "amount_fen": amount,
                    }
                ],
            },
        )
    publish(*[f"paid-{index}" for index in range(7)])
    summary_modes = []
    original = BusinessQueries.settlements

    def watched(self, *args, **kwargs):
        summary_modes.append(kwargs.get("summary", False))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(BusinessQueries, "settlements", watched)
    dashboard = Dashboard(engine)
    response = dashboard.employees("2026-01", limit=2)
    source = response["data"]["employees"]["items"][0]["payroll_sources"][0]
    assert source["subject_id"] == "prior-net"
    assert source["settlement_view"] == "historical"
    assert source["movements_scope"] == "business_related_settlement_events"
    assert source["obligations"][0]["paid_fen"] == 600
    assert source["obligations"][0]["remaining_fen"] == 1400
    assert source["current_followups"]["obligations"][0]["paid_fen"] == 650
    assert source["movements_page"]["total_count"] == 6
    assert source["movements_page"]["returned_count"] == len(source["movements"]) == 2
    assert summary_modes and all(summary_modes)
    seen = {item["id"] for item in source["movements"]}
    cursor = source["movements_page"]["next_cursor"]
    assert cursor
    while cursor:
        following = dashboard.business_status(
            "2026-01",
            "prior-net",
            section="settlement_events",
            limit=2,
            cursor=cursor,
            expected_version=response["snapshot_version"],
            settlement_view="historical",
        )["data"]
        page = following["collections"]["settlement_events"]
        assert following["settlements"]["movements"] == []
        assert following["settlements"]["movement_count"] == 6
        assert page["page"]["total_count"] == 6
        assert len(page["items"]) <= 2
        assert not seen.intersection(item["id"] for item in page["items"])
        assert {item["posting_period"] for item in page["items"]} == {"2026-01"}
        seen.update(item["id"] for item in page["items"])
        cursor = page["page"]["next_cursor"]
    assert len(seen) == 6
    current = dashboard.business_status("2026-01", "prior-net", section="settlement_events")["data"]
    assert current["settlement_view"] == "current"
    assert current["collections"]["settlement_events"]["page"]["total_count"] == 7
    assert current["settlements"]["movement_count"] == 6
    with pytest.raises(KernelError, match="分页") as failure:
        dashboard.business_status(
            "2026-01",
            "prior-net",
            section="settlement_events",
            limit=2,
            cursor=source["movements_page"]["next_cursor"],
            expected_version=response["snapshot_version"],
            settlement_view="current",
        )
    assert failure.value.code == "dashboard_snapshot_changed"
    with pytest.raises(ValueError, match="historical"):
        dashboard.business_status("2026-01", "prior-net", settlement_view="unknown")
