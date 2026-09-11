"""A new review interval inherits cost capacity without reopening the old interval."""

import pytest
from test_managed_reserve import book as book
from test_managed_reserve import compile_settlement, current, debt, lines, scope_data

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.types import YearMonth


def empty_scope(save, publish, sid, month, *, through=None, predecessor=None, accounts=None):
    data = scope_data() | dict(
        period=month,
        effective_from=month,
        effective_through=through or month,
        predecessor_scope_id=predecessor,
        platform_account_ids=accounts or ["platform"],
    )
    save("managed_reserve_scope", sid, data)
    publish(sid)
    return data


def cost_scope(save, publish, sid, month, amount, *, predecessor=None, through=None):
    source_id = sid + "-cost"
    save(
        "managed_reserve_bank_expense",
        source_id,
        dict(
            period=month,
            scope_id=sid,
            actual_date=month + "-02",
            bank_account_id="bank-a",
            amount_fen=amount,
        ),
    )
    data = scope_data(
        costs=[dict(source_kind="managed_reserve_bank_expense", source_id=source_id)]
    ) | dict(
        period=month,
        effective_from=month,
        effective_through=through or month,
        predecessor_scope_id=predecessor,
    )
    save("managed_reserve_scope", sid, data)
    publish(sid, source_id)
    return source_id, data


def close_month(book, period, *, active_transactions):
    from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods

    engine, _, _, proof = book
    periods = Periods(engine)
    for category in MATERIAL_CATEGORIES:
        active = category == "bank" or (category == "transactions" and active_transactions)
        periods.inventory(
            period,
            category,
            evidence=[proof] if active else [],
            expected=int(active),
            no_business=not active,
            confirmation_evidence=proof,
            request_id=f"inventory-{period}-{category}",
        )
    preview = periods.preview_close(period, owner_confirmation=proof)
    periods.close(
        period,
        owner_confirmation=proof,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="close-" + period,
    )
    return periods.closed_report(period)


def test_closed_july_and_august_capacity_combines_with_september_cost_in_one_settlement(book):
    from material_fixture import supporting_text
    from test_banking import entry, opening, reconciliation, statement

    from ai_accounting.kernel.periods import Periods

    engine, save, publish, proof = book
    supporting_text(engine, proof, period="2026-07")
    opening(save, publish, month="2026-07")
    old_cost, _ = cost_scope(save, publish, "july-scope", "2026-07", 1000, through="2026-08")
    debt(save, "july-debt", 1200, "2026-07")
    publish("july-debt")
    compile_settlement(book, 700, "2026-07", "july-paid", "july-debt", scope_id="july-scope")
    publish("july-paid")
    statement(save, publish, [entry("jul-out", "2026-07-02", -1000)], month="2026-07")
    reconciliation(
        save,
        publish,
        [dict(reference="jul-out", source_kind="managed_reserve_bank_expense", source_id=old_cost)],
        month="2026-07",
    )
    frozen_july = close_month(book, "2026-07", active_transactions=True)
    statement(save, publish, [], subject="aug-statement", month="2026-08", initial=-1000)
    reconciliation(
        save,
        publish,
        [],
        subject="aug-reconciliation",
        statement_id="aug-statement",
        month="2026-08",
    )
    frozen_august = close_month(book, "2026-08", active_transactions=False)
    with engine.store.connection(read_only=True) as connection:
        old_facts = dict(connection.execute("SELECT subject_id,fact_id FROM fact_current"))
        old_results = dict(
            connection.execute("SELECT subject_id,calculation_id FROM calculation_current")
        )
        old_closes = [
            tuple(r) for r in connection.execute("SELECT * FROM period_close ORDER BY period")
        ]

    new_cost, _ = cost_scope(
        save, publish, "september-scope", "2026-09", 600, predecessor="july-scope"
    )
    debt(save, "september-debt", 300, "2026-09")
    publish("september-debt")
    sources = [
        dict(source_kind="expense", source_id="july-debt", obligation="primary", amount_fen=500),
        dict(
            source_kind="expense", source_id="september-debt", obligation="primary", amount_fen=300
        ),
    ]
    compile_settlement(
        book, 800, "2026-09", "one-september-payment", scope_id="september-scope", sources=sources
    )
    publish("one-september-payment")
    settlement = current(engine, "managed_reserve_obligation_settlement", "one-september-payment")
    assert [(c["source_id"], c["amount_fen"]) for c in settlement.values["cost_claims"]] == [
        (old_cost, 300),
        (new_cost, 500),
    ]
    assert lines(engine, settlement.kind, settlement.subject_id) == [
        ("224101", 500, 0, None),
        ("224101", 300, 0, None),
        ("5602", 0, 800, None),
    ]
    adopted = current(engine, "managed_reserve_scope", "september-scope").values["cost_sources"]
    assert [x["adopting_scope_id"] for x in adopted] == ["july-scope", "september-scope"]
    with engine.store.connection(read_only=True) as connection:
        current_facts = dict(connection.execute("SELECT subject_id,fact_id FROM fact_current"))
        current_results = dict(
            connection.execute("SELECT subject_id,calculation_id FROM calculation_current")
        )
        assert all(current_facts[key] == value for key, value in old_facts.items())
        assert all(current_results[key] == value for key, value in old_results.items())
        assert [
            tuple(r) for r in connection.execute("SELECT * FROM period_close ORDER BY period")
        ] == old_closes
        assert not connection.execute("SELECT 1 FROM pending").fetchone()
        assert (
            connection.execute("SELECT amount FROM balance WHERE balance_key='bank-a'").fetchone()[
                0
            ]
            == -1600
        )
        for sid in ("july-debt", "september-debt"):
            key = current(engine, "expense", sid).values["obligations"][0]["key"]
            row = connection.execute(
                "SELECT amount FROM balance WHERE balance_key=?", (key,)
            ).fetchone()
            assert row is None or row[0] == 0
    assert Periods(engine).closed_report("2026-07") == frozen_july
    assert Periods(engine).closed_report("2026-08") == frozen_august


@pytest.mark.parametrize(
    "month,accounts,code",
    [
        ("2026-08", ["platform"], "reserve_scope_continuity"),
        ("2026-10", ["platform"], "reserve_scope_continuity"),
        ("2026-09", ["other-platform"], "reserve_scope_accounts"),
    ],
)
def test_continuation_requires_contiguous_months_and_exact_platform_identity(
    book, month, accounts, code
):
    engine, save, publish, _ = book
    empty_scope(save, publish, "old", "2026-07", through="2026-08")
    data = scope_data() | dict(
        period=month,
        effective_from=month,
        effective_through=month,
        predecessor_scope_id="old",
        platform_account_ids=accounts,
    )
    save("managed_reserve_scope", "next", data)
    with pytest.raises(KernelError) as error:
        engine.preview(["next"])
    assert error.value.code == code


def test_continuation_rejects_two_successors_and_repeated_adoption(book):
    engine, save, publish, _ = book
    cost, _ = cost_scope(save, publish, "old", "2026-08", 1000)
    first = empty_scope(save, publish, "first", "2026-09", predecessor="old")
    save("managed_reserve_scope", "fork", first)
    with pytest.raises(KernelError) as error:
        engine.preview(["fork", "first"])
    assert error.value.code == "reserve_scope_overlap"
    assert (
        current(engine, "managed_reserve_scope", "first").values["cost_sources"][0]["source_id"]
        == cost
    )


def test_inherited_cost_cannot_be_declared_again_as_a_new_source(book):
    engine, save, publish, _ = book
    cost, _ = cost_scope(save, publish, "old", "2026-08", 1000)
    data = scope_data(
        costs=[dict(source_kind="managed_reserve_bank_expense", source_id=cost)]
    ) | dict(
        effective_through="2026-09",
        predecessor_scope_id="old",
    )
    save("managed_reserve_scope", "next", data)
    with pytest.raises(KernelError) as error:
        engine.preview(["next"])
    assert error.value.code in {"reserve_scope_overlap", "reserve_duplicate_cost"}


def test_scope_cycles_cannot_publish(book):
    engine, save, _, _ = book
    for sid, previous in (("a", "b"), ("b", "a")):
        save("managed_reserve_scope", sid, scope_data() | dict(predecessor_scope_id=previous))
    with pytest.raises(KernelError) as error:
        engine.preview(["a", "b"])
    assert "cycl" in error.value.code


def test_scope_reads_only_direct_predecessor_merged_directory(book, monkeypatch):
    engine, save, publish, _ = book
    cost, _ = cost_scope(save, publish, "scope-0", "2026-01", 1000)
    for index in range(1, 8):
        month = str(YearMonth.from_ordinal(YearMonth("2026-01").ordinal + index))
        empty_scope(save, publish, f"scope-{index}", month, predecessor=f"scope-{index - 1}")
    save(
        "managed_reserve_scope",
        "scope-8",
        scope_data() | dict(effective_through="2026-09", predecessor_scope_id="scope-7"),
    )
    calls = []
    original = engine.store.select

    def tracked(connection, read):
        if read.kind == "managed_reserve_scope":
            calls.append((read.source, read.key))
        return original(connection, read)

    monkeypatch.setattr(engine.store, "select", tracked)
    preview = engine.preview(["scope-8"])
    assert sorted(calls) == [("calculation", "@scope-7"), ("fact", "@scope-7")]
    values = next(x["values"] for x in preview["results"] if x["subject_id"] == "scope-8")
    assert values["cost_sources"][0]["source_id"] == cost
    assert values["cost_sources"][0]["adopting_scope_id"] == "scope-0"


@pytest.mark.parametrize("first", ["recovery", "settlement"])
def test_inherited_capacity_shares_original_recovery_claims_in_both_orders(book, first):
    engine, save, publish, _ = book
    cost, _ = cost_scope(save, publish, "old", "2026-08", 1000)
    empty_scope(save, publish, "new", "2026-09", predecessor="old")
    debt(save, amount=800)
    publish("debt")
    recovery = dict(
        period="2026-09",
        source_expense_id=cost,
        counterparty_id="actual-return",
        amount_fen=300,
        recovery_right_confirmed=True,
    )
    if first == "recovery":
        save("expense_recovery", "return", recovery)
        publish("return")
        with pytest.raises(KernelError) as error:
            compile_settlement(book, 800, scope_id="new")
        assert error.value.code == "insufficient_reserve_capacity"
    else:
        compile_settlement(book, 800, scope_id="new")
        publish("settlement")
        save("expense_recovery", "return", recovery)
        with pytest.raises(KernelError) as error:
            engine.preview(["return", "settlement"])
        assert error.value.code == "excess_expense_recovery"
