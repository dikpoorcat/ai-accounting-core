"""Confirmed reserve-internal originals never become new company money."""

import pytest
from pydantic import ValidationError
from test_deletion_boundaries import book as book
from test_platform_movements import calc, movement, readiness

from ai_accounting.kernel.business_queries import business_display_amount
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.periods import Periods


def internal(proof, **changes):
    return {
        "period": "2026-01",
        "platform_account_id": "platform",
        "movement_ids": ["in", "out"],
        "boundary": "reserve_internal",
        "boundary_evidence_digest": proof,
        "reserve_boundary_confirmed": True,
        **changes,
    }


def originals(book):
    engine, save, publish, _, _, proof = book
    save(
        "platform_movement",
        "in",
        movement(
            proof,
            amount=10,
            direction="inflow",
            day="2026-01-03",
            location="original.csv!11",
            reference="incoming",
        ),
    )
    save(
        "platform_movement",
        "out",
        movement(
            proof,
            amount=10,
            day="2026-01-04",
            location="original.csv!12",
            reference="internal-transfer",
        ),
    )
    publish("in", "out")
    return engine, save, publish, proof


def test_exact_originals_clear_only_their_own_work_without_company_money(book):
    engine, save, publish, proof = originals(book)
    save(
        "platform_movement",
        "unresolved",
        movement(proof, amount=99, location="original.csv!13", reference="other"),
    )
    publish("unresolved")
    save("managed_reserve_internal_movement", "internal", internal(proof))
    assert len(readiness(engine)) == 3
    publish("internal")
    result = calc(engine, "managed_reserve_internal_movement", "internal")
    assert business_display_amount(
        {
            "kind": result.kind,
            "fact": {"data": {}},
            "outcome": {"values": result.values},
        }
    ) == (20, "备用金内部原行金额合计（不入公司账）")
    assert result.values["accounting_effect"] == "none"
    assert result.values["original_amount_fen"] == 20
    assert "amount_fen" not in result.values
    assert [
        (r["actual_date"], r["direction"], r["amount_fen"], r["source_location"])
        for r in result.values["originals"]
    ] == [
        ("2026-01-03", "inflow", 10, "original.csv!11"),
        ("2026-01-04", "outflow", 10, "original.csv!12"),
    ]
    assert [r["source_id"] for r in readiness(engine)] == ["unresolved"]
    assert engine.overview("2026-01")["cashflow"] == []
    with engine.store.connection(read_only=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM balance").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM voucher_version").fetchone()[0] == 0


@pytest.mark.parametrize("confirmed", [None, False])
def test_missing_or_negative_boundary_does_not_dispose_sources(book, confirmed):
    engine, save, publish, proof = originals(book)
    save(
        "managed_reserve_internal_movement",
        "internal",
        internal(proof, reserve_boundary_confirmed=confirmed),
    )
    with pytest.raises(KernelError) as error:
        publish("internal")
    assert error.value.code == "needs_information"
    assert len(readiness(engine)) == 2


def test_boundary_digest_must_be_an_adopted_original(book):
    engine, save, publish, proof = originals(book)
    save(
        "managed_reserve_internal_movement",
        "internal",
        internal(proof, boundary_evidence_digest="0" * 64),
    )
    with pytest.raises(KernelError) as error:
        publish("internal")
    assert error.value.code == "needs_information"
    assert len(readiness(engine)) == 2


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"period": "2026-02"}, "platform_movement_period"),
        ({"platform_account_id": "other"}, "platform_movement_mismatch"),
        ({"movement_ids": ["missing"]}, "needs_information"),
    ],
)
def test_wrong_original_group_fails_atomically(book, changes, code):
    engine, save, publish, proof = originals(book)
    save("managed_reserve_internal_movement", "internal", internal(proof, **changes))
    with pytest.raises(KernelError) as error:
        publish("internal")
    assert error.value.code == code
    assert len(readiness(engine)) == 2
    assert not engine.overview("2026-01")["cashflow"]


def test_internal_claim_cannot_also_be_used_for_real_reserve_expense(book):
    engine, save, publish, proof = originals(book)
    save("managed_reserve_internal_movement", "internal", internal(proof))
    publish("internal")
    save(
        "managed_reserve_expense",
        "expense",
        {
            "period": "2026-01",
            "actual_date": "2026-01-04",
            "platform_account_id": "platform",
            "movement_ids": ["out"],
            "amount_fen": 10,
        },
    )
    with pytest.raises(KernelError) as error:
        publish("expense")
    assert error.value.code == "platform_movement_consumed"
    assert (
        calc(engine, "managed_reserve_internal_movement", "internal").values["accounting_effect"]
        == "none"
    )
    assert len(readiness(engine)) == 1


def test_duplicate_or_unsupported_boundary_is_rejected_at_registration(book):
    _, save, _, _, _, proof = book
    for changes in (
        {"movement_ids": ["one", "one"]},
        {"movement_ids": []},
        {"boundary": "skip_unresolved"},
    ):
        with pytest.raises((KernelError, ValidationError, ValueError)):
            save("managed_reserve_internal_movement", "internal", internal(proof, **changes))


def test_closed_originals_and_no_voucher_adoption_stay_frozen(book):
    engine, save, publish, proof = originals(book)
    close = book[3]
    save("managed_reserve_internal_movement", "internal", internal(proof))
    publish("internal")
    close("2026-01")
    frozen = Periods(engine).closed_report("2026-01")
    save(
        "managed_reserve_internal_movement",
        "internal",
        internal(proof, movement_ids=["in"]),
        revision=1,
    )
    with pytest.raises(KernelError):
        publish("internal")
    assert Periods(engine).closed_report("2026-01") == frozen
