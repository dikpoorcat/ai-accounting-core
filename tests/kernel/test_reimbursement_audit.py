"""Independent regression checks for accepted batches and existing lifecycle consumers."""

import pytest
from test_reimbursement_assets import (
    accepted_batch,
    activation,
    asset,
    batch_card,
    pay,
    result,
)
from test_reimbursement_assets import (
    book as book,
)

from ai_accounting.kernel.contracts import KernelError


def test_batch_cost_correction_recalculates_activation_and_consumption_without_changing_payment(
    book,
):
    engine, save, publish = book
    save("reimbursed_asset_batch", "batch", accepted_batch())
    save("reimbursed_asset", "computer", batch_card())
    save("reimbursed_asset", "chair", batch_card(30000))
    save("asset_activation", "activation", activation())
    save("asset_consumption", "march", {"period": "2026-03", "asset_id": "computer"})
    save("payment", "paid", pay("reimbursed_asset_batch", "batch", "alice", "alice", 90000))
    initial = publish("batch", "computer", "chair", "activation", "march", "paid")
    before_numbers = {item["subject_id"]: item["voucher_number"] for item in initial["results"]}
    with engine.store.connection(read_only=True) as connection:
        payment_fact = engine.store.current_fact(connection, "paid")
    save(
        "reimbursed_asset_batch",
        "batch",
        accepted_batch(
            cost_fen=180000,
            assets=[
                {"asset_id": "computer", "asset_type": "fixed", "cost_fen": 150000},
                {"asset_id": "chair", "asset_type": "fixed", "cost_fen": 30000},
            ],
            creditors=[
                {"employee_id": "alice", "amount_fen": 90000},
                {"employee_id": "bob", "amount_fen": 90000},
            ],
        ),
        revision=1,
    )
    with pytest.raises(KernelError) as error:
        publish("batch")
    assert error.value.code == "asset_acceptance_conflict"
    assert result(engine, "march")["values"]["consumption_fen"] == 10000
    save("reimbursed_asset", "computer", batch_card(150000), revision=1)
    corrected = publish("batch", "computer")
    for item in corrected["results"]:
        assert item["voucher_number"] == before_numbers[item["subject_id"]]
    assert result(engine, "march")["values"]["consumption_fen"] == 12500
    assert result(engine, "activation")["values"]["cost_fen"] == 150000
    assert result(engine, "paid")["values"]["amount_fen"] == 90000
    with engine.store.connection(read_only=True) as connection:
        assert engine.store.current_fact(connection, "paid") == payment_fact
        carrying = connection.execute(
            "SELECT amount FROM balance WHERE balance_key='asset:computer:carrying'"
        ).fetchone()[0]
    assert carrying == 137500


def test_unpublished_direct_purchase_cannot_replace_a_published_batch_asset(book):
    engine, save, publish = book
    save("reimbursed_asset_batch", "batch", accepted_batch())
    publish("batch")
    save(
        "asset",
        "computer",
        {
            "period": "2026-02",
            "asset_type": "fixed",
            "supplier_id": "supplier",
            "acquisition_date": "2026-02-28",
            "cost_fen": 120000,
            "acquisition_basis": "direct_purchase",
        },
    )
    with pytest.raises(KernelError) as error:
        publish("computer")
    assert error.value.code == "duplicate_asset_acceptance"
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT amount FROM balance WHERE balance_key='asset:computer:carrying'"
            ).fetchone()[0]
            == 120000
        )


@pytest.mark.parametrize("batch_first", [True, False])
def test_direct_employee_asset_and_batch_cannot_both_claim_same_identity(book, batch_first):
    _, save, publish = book
    first = ("reimbursed_asset_batch", "batch", accepted_batch())
    second = ("reimbursed_asset", "computer", asset())
    if not batch_first:
        first, second = second, first
    save(*first)
    publish(first[1])
    save(*second)
    with pytest.raises(KernelError) as error:
        publish(second[1])
    assert error.value.code == "duplicate_asset_acceptance"


def test_unactivated_unpaid_batch_can_be_withdrawn_in_dependency_order(book):
    engine, save, publish = book
    save("reimbursed_asset_batch", "batch", accepted_batch())
    save("reimbursed_asset", "computer", batch_card())
    save("reimbursed_asset", "chair", batch_card(30000))
    publish("batch", "computer", "chair")
    with pytest.raises(KernelError) as error:
        engine.preview_delete("batch")
    assert error.value.code == "has_dependents"
    for subject in ("computer", "chair", "batch"):
        preview = engine.preview_delete(subject)
        engine.delete(
            subject,
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id="delete-" + subject,
        )
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM balance").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM fact_revision").fetchone()[0] == 3


def test_batch_narrow_source_scope_does_not_make_unrelated_direct_card_a_consumer(book):
    engine, save, publish = book
    save("reimbursed_asset", "unrelated", asset())
    publish("unrelated")
    save("reimbursed_asset_batch", "batch", accepted_batch())
    publish("batch")
    assert engine.preview_delete("unrelated")["status"] == "preview"
