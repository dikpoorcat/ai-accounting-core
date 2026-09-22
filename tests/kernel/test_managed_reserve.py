"""Managed reserve records actual expenses and refunds without a reserve subledger."""

import pytest
import test_deletion_boundaries
from pydantic import ValidationError
from test_banking import entry, opening, reconciliation, statement
from test_platforms import balances, current, lines
from test_platforms import book as book

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.domains.managed_reserve import (
    ManagedReserveExpense,
    ManagedReserveRefund,
)
from ai_accounting.kernel.types import YearMonth


def reserve_data(kind, channel, *, amount=1_000, month="2026-09", counterparty=None):
    data = {
        "period": month,
        "actual_date": month + "-10",
        "amount_fen": amount,
        f"{channel}_account_id": channel,
    }
    if counterparty is not None:
        data["counterparty_id"] = counterparty
    return data


@pytest.mark.parametrize("model", [ManagedReserveExpense, ManagedReserveRefund])
def test_exactly_one_company_funds_account_and_platform_sources(model):
    common = dict(period="2026-09", actual_date="2026-09-10", amount_fen=100)
    with pytest.raises(ValidationError, match="exactly one"):
        model(**common)
    with pytest.raises(ValidationError, match="exactly one"):
        model(**common, bank_account_id="bank", cash_account_id="cash")
    with pytest.raises(ValidationError, match="requires original movements"):
        model(**common, platform_account_id="platform")
    with pytest.raises(ValidationError, match="cannot use platform movements"):
        model(**common, bank_account_id="bank", movement_ids=("movement",))
    with pytest.raises(ValidationError, match="duplicate platform source"):
        model(
            **common,
            platform_account_id="platform",
            movement_ids=("movement", "movement"),
        )


@pytest.mark.parametrize("channel,account", [("bank", "1002"), ("cash", "1001")])
@pytest.mark.parametrize(
    "kind,expected_lines,balance",
    [
        (
            "managed_reserve_expense",
            [("5602", 1_000, 0, None), (None, 0, 1_000, "managed_reserve_outflow")],
            -1_000,
        ),
        (
            "managed_reserve_refund",
            [(None, 1_000, 0, "other_operating_receipts"), ("5602", 0, 1_000, None)],
            1_000,
        ),
    ],
)
def test_bank_and_cash_post_direct_actual_money(
    book, channel, account, kind, expected_lines, balance
):
    engine, save, publish, _ = book
    save(kind, "reserve", reserve_data(kind, channel))
    publish("reserve")
    result = current(engine, kind, "reserve")
    expected = [(account if row[0] is None else row[0], *row[1:]) for row in expected_lines]
    assert lines(engine, kind, "reserve") == expected
    assert balances(engine)[channel] == balance
    assert result.values["accounting_treatment"] == (
        "reserve_expense" if kind.endswith("expense") else "reserve_refund"
    )
    assert result.values["counterparty_id"] is None
    assert result.values["funds_category"] == channel


@pytest.mark.parametrize(
    "kind,expected_lines,balance",
    [
        (
            "managed_reserve_expense",
            [("5602", 1_000, 0, None), ("1012", 0, 1_000, "managed_reserve_outflow")],
            -1_000,
        ),
        (
            "managed_reserve_refund",
            [("1012", 1_000, 0, "other_operating_receipts"), ("5602", 0, 1_000, None)],
            1_000,
        ),
    ],
)
def test_platform_expense_and_refund_consume_matching_original(book, kind, expected_lines, balance):
    engine, save, publish, _ = book
    save(kind, "reserve", reserve_data(kind, "platform", counterparty="known-party"))
    publish("reserve")
    result = current(engine, kind, "reserve")
    assert lines(engine, kind, "reserve") == expected_lines
    assert balances(engine)["platform"] == balance
    assert result.values["movement_ids"] == ("movement-reserve",)
    assert result.values["counterparty_id"] == "known-party"


@pytest.mark.parametrize(
    "kind,signed",
    [("managed_reserve_expense", -1_000), ("managed_reserve_refund", 1_000)],
)
def test_bank_expense_and_refund_are_reconcilable_actual_money(book, kind, signed):
    engine, save, publish, _ = book
    opening(save, publish, bank="bank")
    save(kind, "reserve", reserve_data(kind, "bank"))
    publish("reserve")
    statement(save, publish, [entry("reserve-row", "2026-09-10", signed)], bank="bank")
    reconciliation(
        save,
        publish,
        [dict(reference="reserve-row", source_kind=kind, source_id="reserve")],
        bank="bank",
    )
    assert current(engine, "bank_reconciliation", "reconciliation").values["balanced"] is True


@pytest.mark.parametrize(
    "model,expected", [(ManagedReserveExpense, "outflow"), (ManagedReserveRefund, "inflow")]
)
def test_material_direction_preserves_explicit_columns_and_signed_net(model, expected):
    fact = model(
        period="2026-09",
        actual_date="2026-09-10",
        bank_account_id="bank",
        amount_fen=100,
    )
    signed = -100 if expected == "outflow" else 100
    fact.validate_material_amount(
        "fact.amount_fen", signed, source_amounts=(signed,), source_directions=("signed_net",)
    )
    fact.validate_material_amount(
        "result.amount_fen", 100, source_amounts=(100,), source_directions=(expected,)
    )
    wrong = "inflow" if expected == "outflow" else "outflow"
    with pytest.raises(KernelError) as caught:
        fact.validate_material_amount(
            "fact.amount_fen", 100, source_amounts=(100,), source_directions=(wrong,)
        )
    assert caught.value.code == "material_funds_direction_mismatch"


def test_actual_date_must_belong_to_posting_month():
    with pytest.raises(ValidationError, match="must belong"):
        ManagedReserveExpense(
            period="2026-09",
            actual_date="2026-10-01",
            bank_account_id="bank",
            amount_fen=100,
        )


@pytest.mark.parametrize("amount", [True, 100.0])
def test_amount_requires_a_strict_integer_fen(amount):
    with pytest.raises(ValidationError):
        ManagedReserveRefund(
            period="2026-09",
            actual_date="2026-09-10",
            cash_account_id="cash",
            amount_fen=amount,
        )


def test_actual_reserve_money_cannot_be_withdrawn(book):
    engine, save, publish, _ = book
    save(
        "managed_reserve_refund",
        "reserve",
        reserve_data("managed_reserve_refund", "cash"),
    )
    publish("reserve")
    with pytest.raises(KernelError) as caught:
        engine.preview_delete("reserve")
    assert caught.value.code == "immutable_fact"


def test_publish_failure_rolls_back_voucher_and_funds_projection(book):
    engine, save, _, _ = book
    save(
        "managed_reserve_refund",
        "reserve",
        reserve_data("managed_reserve_refund", "cash"),
    )
    preview = engine.preview(["reserve"])
    with engine.store.connection(read_only=True) as connection:
        before_counts = tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("calculation", "voucher_version")
        )
    before = balances(engine), before_counts

    def fail(point, connection):
        if point == "published":
            raise RuntimeError("synthetic failure before commit")

    engine.fault = fail
    with pytest.raises(RuntimeError, match="synthetic failure"):
        engine.confirm(
            ["reserve"],
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id="reserve-rollback",
        )
    with engine.store.connection(read_only=True) as connection:
        after_counts = tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("calculation", "voucher_version")
        )
    assert (balances(engine), after_counts) == before


@pytest.mark.parametrize(
    "kind,sign", [("managed_reserve_expense", -1), ("managed_reserve_refund", 1)]
)
def test_closed_money_is_frozen_and_continuous_corrections_keep_one_open_delta(
    tmp_path, kind, sign
):
    lifecycle_book = test_deletion_boundaries.book.__wrapped__(tmp_path)
    engine, save, publish, close, _, _ = lifecycle_book
    data = reserve_data(kind, "cash", amount=100, month="2026-01")
    save(kind, "reserve", data)
    publish("reserve")
    close("2026-01")
    with engine.store.connection(read_only=True) as connection:
        frozen_manifest = connection.execute(
            "SELECT manifest FROM period_close WHERE period=?",
            (YearMonth("2026-01").ordinal,),
        ).fetchone()[0]
    save(
        kind,
        "reserve",
        data | {"amount_fen": 130},
        revision=1,
        amend=True,
    )
    preview = engine.preview(["reserve"], posting_period="2026-02")
    assert preview["results"][0]["mode"] == "closed_correction"
    publish("reserve", posting_period="2026-02")
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT manifest FROM period_close WHERE period=?",
                (YearMonth("2026-01").ordinal,),
            ).fetchone()[0]
            == frozen_manifest
        )
    assert balances(engine)["cash"] == sign * 130
    save(kind, "reserve", data | {"amount_fen": 150}, revision=2, amend=True)
    publish("reserve", posting_period="2026-02")
    assert balances(engine)["cash"] == sign * 150
    assert {
        row["account"]: row["debit"] - row["credit"]
        for row in engine.overview("2026-02")["accounts"]
    } == {"1001": sign * 50, "5602": -sign * 50}
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT manifest FROM period_close WHERE period=?",
                (YearMonth("2026-01").ordinal,),
            ).fetchone()[0]
            == frozen_manifest
        )
