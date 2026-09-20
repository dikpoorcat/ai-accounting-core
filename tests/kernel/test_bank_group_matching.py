"""An observed bank batch can span original rows without invented recipient mapping."""

import pytest
from test_banking import book as book
from test_banking import entry, funding, match, opening, reconciliation, statement

from ai_accounting.kernel.contracts import KernelError, NeedsInformation
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.entities import Entities


def batch_payment(save, publish, *, bank="bank-a", posted=True):
    opening(save, publish)
    funding(save, publish)
    for name, amount in (("alice", 100), ("bob", 200)):
        save(
            "expense",
            name,
            {
                "period": "2026-09",
                "counterparty_id": name,
                "amount_fen": amount,
                "expense_class": "administration",
                "creditor_kind": "employee",
            },
        )
    publish("alice", "bob")
    save(
        "payment",
        "batch",
        {
            "period": "2026-09",
            "actual_date": "2026-09-10",
            "bank_account_id": bank,
            "counterparty_id": "bank-batch",
            "payment_method": "bank_batch",
            "direction": "outflow",
            "amount_fen": 300,
            "allocations": [
                {
                    "source_kind": "expense",
                    "source_id": name,
                    "obligation": "primary",
                    "amount_fen": amount,
                    "recipient_id": name,
                }
                for name, amount in (("alice", 100), ("bob", 200))
            ],
        },
    )
    if posted:
        publish("batch")


def group_matches():
    return [match(), match("first", "payment", "batch"), match("second", "payment", "batch")]


def group_entries():
    # Neither original debit amount is one employee's amount. Only the whole
    # batch membership and settlement allocation are observed and confirmed.
    return [entry(), entry("first", "2026-09-10", -130), entry("second", "2026-09-10", -170)]


def test_one_published_batch_matches_original_rows_without_recipient_subrow_mapping(book):
    engine, save, publish, _ = book
    batch_payment(save, publish)
    before = engine.ledger("2026-09")
    statement(save, publish, group_entries())
    result = reconciliation(save, publish, group_matches())
    assert result["status"] == "published"
    assert engine.ledger("2026-09") == before
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT amount FROM balance WHERE category='bank' AND balance_key='bank-a'"
            ).fetchone()[0]
            == 700
        )
        assert (
            connection.execute("SELECT count(*) FROM balance WHERE category='payable'").fetchone()[
                0
            ]
            == 0
        )
        rows = connection.execute(
            "SELECT reference,source_kind,source_id FROM fact_bank_reconciliation_matches "
            "WHERE source_kind='payment' ORDER BY reference"
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            ("first", "payment", "batch"),
            ("second", "payment", "batch"),
        ]
    assert not engine.overview("2026-09")["pending"]


def test_dashboard_keeps_original_bank_rows_and_nests_whole_batch_recipients(book):
    engine, save, publish, _ = book
    batch_payment(save, publish)
    entities = Entities(engine)
    for index, (entity_id, name) in enumerate((("alice", "张三"), ("bob", "李四"))):
        entities.update_entity_profile(
            entity_id,
            {"display_name": name},
            source="合成人员资料",
            expected_revision=1,
            request_id=f"batch-party-{index}",
        )
    statement(save, publish, group_entries())
    reconciliation(save, publish, group_matches())

    dashboard = Dashboard(engine)
    first = dashboard.funds("2026-09", limit=2)
    bank = first["data"]["bank_statement"]
    following = dashboard.funds(
        "2026-09",
        limit=2,
        after_statement=bank["page"]["next_cursor"],
        expected_version=first["snapshot_version"],
    )
    rows = [*bank["rows"], *following["data"]["bank_statement"]["rows"]]
    assert [(row["reference"], row["signed_amount_fen"]) for row in rows] == [
        ("receipt", 1000),
        ("first", -130),
        ("second", -170),
    ]
    batch_rows = [row for row in rows if row.get("batch_payment")]
    assert [row["party"] for row in batch_rows] == ["批量付款 · 2 项"] * 2
    assert all("张三、李四" not in row["party"] for row in batch_rows)
    expected = {
        "bank_row_count": 2,
        "total_fen": 300,
        "items": [
            {"party": "张三", "amount_fen": 100},
            {"party": "李四", "amount_fen": 200},
        ],
    }
    assert [row["batch_payment"] for row in batch_rows] == [expected, expected]


@pytest.mark.parametrize(
    "first,second",
    [
        (entry("first", "2026-09-10", -130), entry("second", "2026-09-10", -169)),
        (entry("first", "2026-09-10", -300), entry("second", "2026-09-10", -300)),
        # Net amount alone cannot conceal an opposite-direction movement.
        (entry("first", "2026-09-10", -400), entry("second", "2026-09-10", 100)),
        (entry("first", "2026-09-10", -130), entry("second", "2026-09-11", -170)),
    ],
    ids=("unbalanced", "full-amount-twice", "netted-opposite-direction", "different-date"),
)
def test_group_rejects_amount_direction_and_date_mismatch_atomically(book, first, second):
    engine, save, publish, _ = book
    batch_payment(save, publish)
    before = engine.ledger("2026-09")
    statement(save, publish, [entry(), first, second])
    with pytest.raises(KernelError) as failure:
        reconciliation(save, publish, group_matches())
    assert failure.value.code == "bank_match_difference"
    assert engine.ledger("2026-09") == before
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM calculation_current WHERE subject_id='reconciliation'"
            ).fetchone()
            is None
        )


def test_group_cannot_borrow_a_published_payment_from_another_account(book):
    engine, save, publish, _ = book
    batch_payment(save, publish, bank="bank-b")
    before = engine.ledger("2026-09")
    statement(save, publish, group_entries())
    with pytest.raises(KernelError) as failure:
        reconciliation(save, publish, group_matches())
    assert failure.value.code == "bank_account_mismatch"
    assert engine.ledger("2026-09") == before


@pytest.mark.parametrize(
    "matches",
    [
        [match(), match("first", "payment", "batch")],
        [*group_matches(), match("first", "payment", "batch")],
        [*group_matches(), match("first", "funding", "funding")],
        [match(), match("first", "payment", "batch"), match("unknown", "payment", "batch")],
    ],
    ids=("partial", "duplicate-same-source", "duplicate-other-source", "unknown-reference"),
)
def test_every_original_row_is_required_exactly_once_even_with_groups(book, matches):
    _, save, publish, _ = book
    batch_payment(save, publish)
    statement(save, publish, group_entries())
    with pytest.raises(NeedsInformation) as failure:
        reconciliation(save, publish, matches)
    assert failure.value.issues[0]["field"] == "matches"


def test_group_cannot_present_unpublished_payment_as_formal_funds(book):
    _, save, publish, _ = book
    batch_payment(save, publish, posted=False)
    statement(save, publish, group_entries())
    with pytest.raises(NeedsInformation) as failure:
        reconciliation(save, publish, group_matches())
    assert failure.value.issues[0]["field"] == "actual_funds"


def test_balanced_group_does_not_hide_another_unmatched_actual_movement(book):
    _, save, publish, _ = book
    batch_payment(save, publish)
    funding(save, publish, subject="unmatched", amount=50, day="2026-09-10")
    statement(save, publish, group_entries())
    with pytest.raises(NeedsInformation) as failure:
        reconciliation(save, publish, group_matches())
    assert failure.value.issues[0]["field"] == "matches"


def test_explicit_grouping_also_preserves_inflow_direction(book):
    _, save, publish, _ = book
    opening(save, publish)
    funding(save, publish)
    statement(save, publish, [entry("first", amount=400), entry("second", amount=600)])
    assert reconciliation(save, publish, [match("first"), match("second")])["status"] == "published"
