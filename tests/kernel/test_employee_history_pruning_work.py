"""Unrelated posted history does not revive employee history detail reads."""

from collections import Counter

from test_opening_continuation import book as opening_book_fixture

from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.query_reads import QueryReads

opening_book = opening_book_fixture


def test_employee_month_pages_do_not_decode_unrelated_posted_history(opening_book, monkeypatch):
    engine, save, publish, package, _ = opening_book
    package([
        ("opening_bank", "bank-start", {"bank_account_id": "bank", "balance_fen": 2000}),
        ("opening_payroll_payable", "prior-net", {
            "employee_id": "employee", "recipient_id": "employee",
            "payroll_period": "2025-12", "component": "net", "outstanding_fen": 2000,
        }),
    ])
    for subject, period, amount in (
        ("paid-january", "2026-01", 600), ("paid-february", "2026-02", 50),
    ):
        save("payment", subject, {
            "period": period, "actual_date": period + "-10", "direction": "outflow",
            "bank_account_id": "bank", "counterparty_id": "employee", "amount_fen": amount,
            "allocations": [{"source_kind": "opening_payroll_payable", "source_id": "prior-net",
                             "obligation": "primary", "amount_fen": amount}],
        })
    publish("paid-january", "paid-february")
    original_decode = QueryReads._stored_outcome
    original_calculations = QueryReads.calculations
    original_raw_calculations = QueryReads.raw_calculations
    outcome_subjects = {}
    decoded, loaded = Counter(), Counter()

    def refresh_outcome_subjects():
        with engine.store.connection(read_only=True) as connection:
            for row in connection.execute("SELECT subject_id,outcome FROM calculation"):
                outcome_subjects.setdefault(row["outcome"], set()).add(row["subject_id"])

    def decode(raw):
        result = original_decode(raw)
        decoded.update(outcome_subjects.get(raw, ()))
        return result

    def calculations(reads, identifiers):
        identifiers = set(identifiers)
        missing = identifiers - reads._calculations.keys()
        result = original_calculations(reads, identifiers)
        loaded.update(result[ident]["subject_id"] for ident in missing)
        return result

    def raw_calculations(reads, identifiers):
        identifiers = set(identifiers)
        missing = identifiers - reads._raw_calculations.keys()
        result = original_raw_calculations(reads, identifiers)
        loaded.update(result[ident]["subject_id"] for ident in missing)
        return result

    monkeypatch.setattr(QueryReads, "_stored_outcome", staticmethod(decode))
    monkeypatch.setattr(QueryReads, "calculations", calculations)
    monkeypatch.setattr(QueryReads, "raw_calculations", raw_calculations)

    def observe_pages():
        refresh_outcome_subjects()
        observations = []
        for options in ({}, {"employee_id": "employee"}):
            decoded.clear()
            loaded.clear()
            data = Dashboard(engine).employees("2026-02", **options, employee_filter="all")["data"]
            assert set(data["collections"]) == {"employees", "labor_sources"}
            person = data["collections"]["employees"]["items"][0]
            assert person["direct_net_payments_fen"] == 50
            assert person["outstanding_net_fen"] == 1350
            assert data["employees"]["outstanding_net_fen"] == 1350
            observations.append((data["employees"], person, decoded.copy(), loaded.copy()))
        return observations

    before = observe_pages()
    unrelated = {f"old-expense-{index}" for index in range(3)}
    for index, subject in enumerate(sorted(unrelated)):
        save("expense", subject, {
            "period": "2026-01", "amount_fen": 100 + index,
            "counterparty_id": f"other-supplier-{index}",
            "expense_class": "administration", "creditor_kind": "supplier",
        })
    publish(*sorted(unrelated))
    after = observe_pages()
    for baseline, grown in zip(before, after, strict=True):
        assert grown[:2] == baseline[:2]
        assert grown[2:] == baseline[2:]
        assert unrelated.isdisjoint(grown[2])
        assert unrelated.isdisjoint(grown[3])
        # The old net liability remains in the necessary reducer, but its full
        # calculation presentation is not loaded to build a discarded card.
        assert "prior-net" not in grown[3]
