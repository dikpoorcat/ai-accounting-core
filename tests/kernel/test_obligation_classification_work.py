"""History growth does not repeat the same pure classification interpretation."""

import sqlite3

import pytest

import ai_accounting.kernel.obligation_classification as classification


@pytest.mark.parametrize("months", (12, 48, 120))
def test_independent_history_keeps_complete_amounts_and_bounded_rule_work(monkeypatch, months):
    patterns = (
        ("receivable", "1122", "service_sale", "customer_receivables"),
        ("receivable", "1123", "supplier_advance", "supplier_advances"),
        ("payable", "2202", "expense", "supplier_payables"),
        ("payable", "2211", "payroll", "payroll_payables"),
        ("payable", "2241", "labor_project_cost", "labor_payables"),
    )
    sources = [
        {"key": f"item-{month}-{index}", "category": direction, "account": account,
         "source_business": {"kind": kind, "subject_id": f"subject-{month}-{index}"},
         "source_calculation_id": f"result-{month}-{index}",
         "source_amount_fen": month + index + 1,
         "remaining_fen": None if index == 4 else month + index + 1}
        for month in range(months)
        for index, (direction, account, kind, _) in enumerate(patterns)
    ]
    calls = []
    original = classification.obligation_category

    def counted(*values, **options):
        calls.append((*values, options.get("semantic")))
        return original(*values, **options)

    monkeypatch.setattr(classification, "obligation_category", counted)
    with sqlite3.connect(":memory:") as connection:
        result = classification.classify_obligations(connection, sources)
        assert len(calls) == len(patterns)
        assert len(result) == months * len(patterns)
        assert sum(item["source_amount_fen"] for item in result) == sum(
            item["source_amount_fen"] for item in sources
        )
        for index, (before, after) in enumerate(zip(sources, result, strict=True)):
            assert {key: value for key, value in after.items() if key != "category_key"} == before
            assert after["category_key"] == patterns[index % len(patterns)][3]
        # The interpretation table ends with the call; it does not promise
        # another request has verified either the source or the current rules.
        assert classification.classify_obligations(connection, sources) == result
        assert len(calls) == 2 * len(patterns)
