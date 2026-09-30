"""Whole original bank rows can cover several complete typed funds sources."""

import pytest
from test_banking import book as book
from test_banking import entry, funding, match, opening, reconciliation, statement

from ai_accounting.kernel.contracts import KernelError, NeedsInformation
from ai_accounting.kernel.integrity import verify_integrity


def composite_funds(
    save,
    publish,
    *,
    reserve_bank="bank-a",
    reserve_day="2026-09-10",
    reserve_amount=500000,
    reserve_kind="managed_reserve_expense",
    reserve_posted=True,
    reserve_parts=("reserve",),
):
    opening(save, publish)
    funding(save, publish, amount=700000)
    save(
        "expense",
        "water",
        {
            "period": "2026-09",
            "counterparty_id": "employee",
            "amount_fen": 110000,
            "expense_class": "administration",
            "creditor_kind": "employee",
        },
    )
    publish("water")
    save(
        "payment",
        "water-payment",
        {
            "period": "2026-09",
            "actual_date": "2026-09-10",
            "bank_account_id": "bank-a",
            "counterparty_id": "employee",
            "direction": "outflow",
            "amount_fen": 110000,
            "allocations": [
                {
                    "source_kind": "expense",
                    "source_id": "water",
                    "obligation": "primary",
                    "amount_fen": 110000,
                }
            ],
        },
    )
    publish("water-payment")
    for name in reserve_parts:
        save(
            reserve_kind,
            name,
            {
                "period": reserve_day[:7],
                "actual_date": reserve_day,
                "bank_account_id": reserve_bank,
                "amount_fen": reserve_amount // len(reserve_parts),
            },
        )
    if reserve_posted:
        publish(*reserve_parts)


def original_entries(*, whole_amount=-610000):
    # These are the two actual bank observations. Neither is a manufactured
    # component row or an employee-to-bank-row allocation.
    return [entry(amount=700000), entry("whole", "2026-09-10", whole_amount)]


def composite_matches(*, reserve_kind="managed_reserve_expense", reserve_parts=("reserve",)):
    return [
        match(),
        match("whole", "payment", "water-payment"),
        *(match("whole", reserve_kind, name) for name in reserve_parts),
    ]


def balances(engine):
    with engine.store.connection(read_only=True) as connection:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT category,balance_key,amount FROM balance ORDER BY category,balance_key"
            )
        ]


def assert_unadopted(engine, before_ledger, before_balances):
    assert engine.ledger("2026-09") == before_ledger
    assert balances(engine) == before_balances
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM calculation_current WHERE subject_id='reconciliation'"
            ).fetchone()
            is None
        )


@pytest.mark.parametrize("reserve_parts", [("reserve",), ("reserve-a", "reserve-b")])
def test_complete_sources_match_one_unsplit_original_without_new_funds_or_voucher(
    book, reserve_parts
):
    engine, save, publish, _ = book
    composite_funds(save, publish, reserve_parts=reserve_parts)
    before = engine.ledger("2026-09")
    saved, _ = statement(save, publish, original_entries())
    result = reconciliation(save, publish, composite_matches(reserve_parts=reserve_parts))

    assert result["status"] == "published"
    assert engine.ledger("2026-09") == before
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT amount FROM balance WHERE category='bank' AND balance_key='bank-a'"
            ).fetchone()[0]
            == 90000
        )
        assert (
            connection.execute("SELECT count(*) FROM balance WHERE category='payable'").fetchone()[
                0
            ]
            == 0
        )
        rows = connection.execute(
            "SELECT reference,signed_fen FROM fact_bank_statement_entries "
            "WHERE revision_id=? ORDER BY item_no",
            (saved["fact_id"],),
        ).fetchall()
        assert [tuple(row) for row in rows] == [("receipt", 700000), ("whole", -610000)]
        references = connection.execute(
            "SELECT reference,source_kind,source_id FROM fact_bank_reconciliation_matches "
            "WHERE reference='whole' ORDER BY source_kind,source_id"
        ).fetchall()
        assert [tuple(row) for row in references] == [
            *(("whole", "managed_reserve_expense", name) for name in reserve_parts),
            ("whole", "payment", "water-payment"),
        ]
        current = connection.execute(
            "SELECT calculation_id FROM calculation_current WHERE subject_id='reconciliation'"
        ).fetchone()[0]
        upstreams = {
            row[0]
            for row in connection.execute(
                "SELECT r.subject_id FROM dependency_fact d "
                "JOIN fact_revision r ON r.id=d.fact_id WHERE d.calculation_id=?",
                (current,),
            )
        }
        assert {"funding", "water-payment", *reserve_parts} <= upstreams
        connection.execute("BEGIN")
        verify_integrity(engine, connection)
    assert not engine.overview("2026-09")["pending"]


def test_composite_original_inflow_keeps_two_complete_capital_sources(book):
    engine, save, publish, _ = book
    opening(save, publish)
    funding(save, publish, subject="first", amount=200000)
    funding(save, publish, subject="second", amount=500000)
    before = engine.ledger("2026-09")
    statement(save, publish, [entry(amount=700000)])
    result = reconciliation(
        save,
        publish,
        [match(source="first"), match(source="second")],
    )
    assert result["status"] == "published"
    assert engine.ledger("2026-09") == before
    assert ("bank", "bank-a", 700000) in balances(engine)


@pytest.mark.parametrize(
    "options,whole_amount,code",
    [
        ({"reserve_bank": "bank-b"}, -610000, "bank_account_mismatch"),
        ({"reserve_day": "2026-09-11"}, -610000, "bank_match_difference"),
        ({"reserve_day": "2026-10-10"}, -610000, "bank_match_difference"),
        ({"reserve_amount": 499999}, -610000, "bank_match_difference"),
        ({}, -610001, "bank_match_difference"),
        # A matching net total cannot conceal components with opposite signs.
        ({"reserve_kind": "managed_reserve_refund"}, 390000, "bank_match_difference"),
    ],
    ids=(
        "other-bank",
        "different-date",
        "different-month",
        "one-fen-short",
        "wrong-row-total",
        "opposite-signs",
    ),
)
def test_composite_rejects_account_date_month_sign_and_amount_atomically(
    book, options, whole_amount, code
):
    engine, save, publish, _ = book
    composite_funds(save, publish, **options)
    statement(save, publish, original_entries(whole_amount=whole_amount))
    before_ledger, before_balances = engine.ledger("2026-09"), balances(engine)
    with pytest.raises(KernelError) as failure:
        reconciliation(
            save,
            publish,
            composite_matches(reserve_kind=options.get("reserve_kind", "managed_reserve_expense")),
        )
    assert failure.value.code == code
    assert_unadopted(engine, before_ledger, before_balances)


@pytest.mark.parametrize(
    "matches",
    [
        [*composite_matches(), match("whole", "payment", "water-payment")],
        [match(), match("whole", "payment", "water-payment")],
        [
            match(),
            match("unknown", "payment", "water-payment"),
            match("whole", "managed_reserve_expense", "reserve"),
        ],
    ],
    ids=("duplicate-identical-pair", "missing-component", "unknown-original-reference"),
)
def test_bad_reference_or_incomplete_component_mapping_does_not_adopt(book, matches):
    engine, save, publish, _ = book
    composite_funds(save, publish)
    statement(save, publish, original_entries())
    before_ledger, before_balances = engine.ledger("2026-09"), balances(engine)
    with pytest.raises(KernelError):
        reconciliation(save, publish, matches)
    assert_unadopted(engine, before_ledger, before_balances)


def test_identical_source_pair_is_information_error_even_when_totals_could_repeat(book):
    _, save, publish, _ = book
    composite_funds(save, publish)
    statement(save, publish, original_entries())
    with pytest.raises(NeedsInformation) as failure:
        reconciliation(
            save,
            publish,
            [*composite_matches(), match("whole", "managed_reserve_expense", "reserve")],
        )
    assert failure.value.issues[0]["field"] == "matches"


@pytest.mark.parametrize("missing", [False, True], ids=("unpublished", "missing-source"))
def test_composite_cannot_use_unadopted_or_absent_funds(book, missing):
    engine, save, publish, _ = book
    composite_funds(save, publish, reserve_posted=missing)
    statement(save, publish, original_entries())
    before_ledger, before_balances = engine.ledger("2026-09"), balances(engine)
    matches = composite_matches(reserve_parts=("absent",)) if missing else composite_matches()
    with pytest.raises(NeedsInformation):
        reconciliation(save, publish, matches)
    assert_unadopted(engine, before_ledger, before_balances)


def test_balanced_composite_cannot_omit_another_actual_movement(book):
    engine, save, publish, _ = book
    composite_funds(save, publish)
    funding(save, publish, subject="unmatched", amount=1, day="2026-09-10")
    statement(save, publish, original_entries())
    before_ledger, before_balances = engine.ledger("2026-09"), balances(engine)
    with pytest.raises(NeedsInformation) as failure:
        reconciliation(save, publish, composite_matches())
    assert failure.value.issues[0]["field"] == "matches"
    assert_unadopted(engine, before_ledger, before_balances)


def test_composite_member_cannot_also_span_other_original_rows(book):
    engine, save, publish, _ = book
    composite_funds(save, publish)
    statement(
        save,
        publish,
        [
            entry(amount=700000),
            entry("first", "2026-09-10", -60000),
            entry("whole", "2026-09-10", -550000),
        ],
    )
    before_ledger, before_balances = engine.ledger("2026-09"), balances(engine)
    with pytest.raises(NeedsInformation) as failure:
        reconciliation(
            save,
            publish,
            [
                match(),
                match("first", "payment", "water-payment"),
                match("whole", "payment", "water-payment"),
                match("whole", "managed_reserve_expense", "reserve"),
            ],
        )
    assert failure.value.issues[0]["field"] == "matches"
    assert_unadopted(engine, before_ledger, before_balances)
