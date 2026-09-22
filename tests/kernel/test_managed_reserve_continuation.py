"""Managed-reserve refunds are independent actual events across periods."""

from test_managed_reserve import book as book
from test_managed_reserve import reserve_data
from test_platforms import balances, current

from ai_accounting.kernel.types import YearMonth


def test_later_refunds_do_not_read_cost_capacity_or_fifo(book):
    engine, save, publish, _ = book
    save(
        "managed_reserve_refund",
        "refund-september",
        reserve_data("managed_reserve_refund", "bank", amount=700),
    )
    save(
        "managed_reserve_refund",
        "refund-october",
        reserve_data("managed_reserve_refund", "bank", amount=900, month="2026-10"),
    )
    publish("refund-september", "refund-october")
    assert current(engine, "managed_reserve_refund", "refund-september").values["amount_fen"] == 700
    assert current(engine, "managed_reserve_refund", "refund-october").values["amount_fen"] == 900
    assert balances(engine)["bank"] == 1_600


def test_refund_may_exceed_prior_recorded_expense_without_balance_gate(book):
    engine, save, publish, _ = book
    save(
        "managed_reserve_expense",
        "expense",
        reserve_data("managed_reserve_expense", "cash", amount=100),
    )
    publish("expense")
    save(
        "managed_reserve_refund",
        "refund",
        reserve_data("managed_reserve_refund", "cash", amount=300, month="2026-10"),
    )
    publish("refund")
    assert balances(engine)["cash"] == 200


def test_120_month_refunds_have_no_historical_cost_dependencies(book):
    engine, save, publish, _ = book
    start = YearMonth("2016-01").ordinal
    subjects = []
    for offset in range(120):
        month = str(YearMonth.from_ordinal(start + offset))
        channel = "bank" if offset % 2 == 0 else "cash"
        subject = f"refund-{offset:03d}"
        save(
            "managed_reserve_refund",
            subject,
            reserve_data("managed_reserve_refund", channel, amount=100, month=month),
        )
        subjects.append(subject)
    publish(*subjects)
    with engine.store.connection(read_only=True) as connection:
        for months in (12, 48, 120):
            through = start + months - 1
            count = connection.execute(
                "SELECT count(*) FROM calculation_current cc "
                "JOIN calculation c ON c.id=cc.calculation_id "
                "WHERE c.kind='managed_reserve_refund' AND c.period<=?",
                (through,),
            ).fetchone()[0]
            assert count == months
        calculation_ids = [
            row[0]
            for row in connection.execute(
                "SELECT cc.calculation_id FROM calculation_current cc "
                "JOIN calculation c ON c.id=cc.calculation_id "
                "WHERE c.kind='managed_reserve_refund'"
            )
        ]
        placeholders = ",".join("?" for _ in calculation_ids)
        assert (
            connection.execute(
                "SELECT count(*) FROM dependency_fact d JOIN calculation c "
                "ON c.id=d.calculation_id WHERE d.fact_id!=c.fact_id "
                f"AND d.calculation_id IN ({placeholders})",
                calculation_ids,
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                f"SELECT count(*) FROM dependency_calculation "
                f"WHERE calculation_id IN ({placeholders})",
                calculation_ids,
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                f"SELECT count(*) FROM dependency_scope WHERE calculation_id IN ({placeholders})",
                calculation_ids,
            ).fetchone()[0]
            == 0
        )
    assert balances(engine)["bank"] == 6_000
    assert balances(engine)["cash"] == 6_000
