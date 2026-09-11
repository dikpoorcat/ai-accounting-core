"""Reserve cost capacity is shared, evidence-bound and never a second cash payment."""

import itertools

import pytest
from test_platforms import book as book
from test_platforms import current, lines, payment_data, transfer_data

from ai_accounting.kernel.command_schema import command_models, validate_command
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.reserves import Reserves

IDS = itertools.count()


def debt(save, sid="debt", amount=500, period="2026-09"):
    save(
        "expense",
        sid,
        dict(
            period=period,
            counterparty_id="employee",
            creditor_kind="employee",
            amount_fen=amount,
            expense_class="administration",
        ),
    )


def scope_data(*, costs=(), treatments=()):
    return dict(
        period="2026-09",
        platform_account_ids=["platform"],
        effective_from="2026-09",
        effective_through="2026-12",
        treatment_confirmed=True,
        cost_sources=list(costs),
        transfer_treatments=list(treatments),
    )


def bank_cost(book, amount=1000):
    engine, save, publish, _ = book
    save(
        "expense",
        "reserve-cost",
        dict(
            period="2026-09",
            counterparty_id="platform",
            creditor_kind="supplier",
            amount_fen=amount,
            expense_class="administration",
        ),
    )
    save(
        "payment",
        "reserve-paid",
        payment_data(kind="payment", source="reserve-cost", amount=amount, party="platform"),
    )
    publish("reserve-cost", "reserve-paid")
    scope = scope_data(
        costs=[
            dict(source_kind="expense", source_id="reserve-cost", bank_payment_id="reserve-paid")
        ]
    )
    save("managed_reserve_scope", "scope", scope)
    publish("scope")
    return scope


def compile_settlement(
    book,
    amount=500,
    period="2026-09",
    sid="settlement",
    source="debt",
    *,
    commit=True,
    scope_id="scope",
    sources=None,
):
    engine, _, _, proof = book
    command = dict(
        period=period,
        scope_id=scope_id,
        actual_date=period + "-20",
        recipient_id="employee",
        amount_fen=amount,
        payment_confirmed=True,
        sources=sources
        if sources is not None
        else [
            dict(source_kind="expense", source_id=source, obligation="primary", amount_fen=amount)
        ],
    )
    reserves = Reserves(engine)
    wire = command_models(engine.store.registry)
    payload = validate_command(
        wire,
        "preview_managed_reserve_settlement",
        dict(company_id="company", subject_id=sid, data=command, evidence=[proof]),
    )
    payload.pop("company_id")
    preview = reserves.preview_settlement(**payload)
    if not commit:
        return reserves, payload, preview
    confirmed = dict(
        **payload,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        expected_revision=preview["expected_revision"],
        request_id="compile-" + str(next(IDS)),
    )
    response = reserves.confirm_settlement(**confirmed)
    assert reserves.confirm_settlement(**confirmed) == response
    return response


def recover(save, amount, *, period="2026-09", sid="recovery"):
    return save(
        "expense_recovery",
        sid,
        dict(
            period=period,
            source_expense_id="reserve-cost",
            counterparty_id="platform",
            amount_fen=amount,
            recovery_right_confirmed=True,
        ),
    )


def test_compiled_settlement_reduces_old_debt_without_cash_and_blocks_direct_registration(book):
    engine, save, publish, proof = book
    bank_cost(book)
    debt(save)
    publish("debt")
    compile_settlement(book)
    publish("settlement")
    assert lines(engine, "managed_reserve_obligation_settlement", "settlement") == [
        ("224101", 500, 0, None),
        ("5602", 0, 500, None),
    ]
    with engine.store.connection(read_only=True) as connection:
        fact = engine.store.current_fact(connection, "settlement").fact
        assert fact.cost_claims[0].source_id == "reserve-cost"
        key = current(engine, "expense", "debt").values["obligations"][0]["key"]
        row = connection.execute(
            "SELECT amount FROM balance WHERE balance_key=?", (key,)
        ).fetchone()
        assert row is None or row[0] == 0
    with pytest.raises(KernelError) as error:
        engine.save_fact(
            fact.kind,
            "injected",
            fact.model_dump(mode="json"),
            evidence=[proof],
            expected_revision=0,
            request_id="inject",
        )
    assert error.value.code == "registration_command_required"
    schema = engine.store.registry.schemas()[fact.kind]
    assert schema["x-registration-command"] == "confirm_managed_reserve_settlement"


@pytest.mark.parametrize("first", ["settlement", "recovery"])
def test_recovery_and_settlement_share_one_cost_capacity_in_both_orders(book, first):
    engine, save, publish, _ = book
    bank_cost(book, 1000)
    debt(save, amount=700)
    publish("debt")
    if first == "recovery":
        recover(save, 400)
        publish("recovery")
        with pytest.raises(KernelError) as error:
            compile_settlement(book, 700)
        assert error.value.code == "insufficient_reserve_capacity"
    else:
        compile_settlement(book, 700)
        publish("settlement")
        recover(save, 400)
        with pytest.raises(KernelError) as error:
            engine.preview(["recovery", "settlement"])
        assert error.value.code == "excess_expense_recovery"
        assert (
            current(engine, "managed_reserve_obligation_settlement", "settlement").values[
                "amount_fen"
            ]
            == 700
        )


def test_future_recovery_does_not_invalidate_prior_settlement_but_same_month_does(book):
    engine, save, publish, _ = book
    bank_cost(book, 1000)
    debt(save)
    publish("debt")
    compile_settlement(book)
    publish("settlement")
    old = current(engine, "managed_reserve_obligation_settlement", "settlement")
    recover(save, 400, period="2026-10")
    publish("recovery")
    assert current(engine, old.kind, "settlement").id == old.id
    assert not any(x["subject_id"] == "settlement" for x in engine.overview("2026-09")["pending"])
    recover(save, 200, sid="same-month")
    assert any(x["subject_id"] == "settlement" for x in engine.overview("2026-09")["pending"])


def test_preview_expiry_and_atomic_registration_failure(book):
    engine, save, publish, _ = book
    bank_cost(book)
    debt(save)
    publish("debt")
    reserves, payload, preview = compile_settlement(book, commit=False)
    recover(save, 100)
    with pytest.raises(KernelError) as error:
        reserves.confirm_settlement(
            **payload,
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            expected_revision=0,
            request_id="stale",
        )
    assert error.value.code == "preview_expired"
    reserves, payload, preview = compile_settlement(book, commit=False)
    original_fault = engine.fault

    def fault(phase, connection):
        if phase == "published":
            raise RuntimeError("synthetic after registration")

    engine.fault = fault
    with pytest.raises(RuntimeError, match="synthetic"):
        reserves.confirm_settlement(
            **payload,
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            expected_revision=0,
            request_id="fail",
        )
    engine.fault = original_fault
    with engine.store.connection(read_only=True) as connection:
        assert not connection.execute(
            "SELECT 1 FROM fact_current WHERE subject_id='settlement'"
        ).fetchone()


def test_existing_transfer_reclassifies_same_voucher_and_bank_amount(book):
    engine, save, publish, _ = book
    save("bank_platform_transfer", "transfer", transfer_data(direction="bank_to_platform"))
    publish("transfer")
    old = current(engine, "bank_platform_transfer", "transfer")
    with engine.store.connection(read_only=True) as connection:
        original_voucher = dict(
            connection.execute(
                "SELECT v.* FROM voucher v JOIN voucher_version h ON h.voucher_id=v.id "
                "WHERE h.calculation_id=?",
                (old.id,),
            ).fetchone()
        )
    scope = scope_data(
        costs=[dict(source_kind="bank_platform_transfer", source_id="transfer")],
        treatments=[dict(transfer_id="transfer", treatment="expense_on_boundary")],
    )
    save("managed_reserve_scope", "scope", scope)
    publish("scope", "transfer")
    assert lines(engine, "bank_platform_transfer", "transfer") == [
        ("5602", 1000, 0, None),
        ("1002", 0, 1000, "managed_reserve_outflow"),
    ]
    assert current(engine, "bank_platform_transfer", "transfer").fact_id == old.fact_id
    with engine.store.connection(read_only=True) as connection:
        assert (
            dict(
                connection.execute(
                    "SELECT v.* FROM voucher v JOIN voucher_version h ON h.voucher_id=v.id "
                    "WHERE h.calculation_id=?",
                    (old.id,),
                ).fetchone()
            )
            == original_voucher
        )
        assert (
            connection.execute("SELECT amount FROM balance WHERE balance_key='bank-a'").fetchone()[
                0
            ]
            == -1000
        )
        assert not connection.execute(
            "SELECT amount FROM balance WHERE balance_key='platform' AND amount != 0"
        ).fetchone()


def test_scope_rejects_using_paid_supplier_expense_as_reserve_cost(book):
    engine, save, publish, _ = book
    debt(save, "supplier-cost")
    save(
        "platform_payment",
        "supplier-paid",
        payment_data(source="supplier-cost", amount=500, party="employee"),
    )
    publish("supplier-cost", "supplier-paid")
    save(
        "managed_reserve_scope",
        "scope",
        scope_data(costs=[dict(source_kind="expense", source_id="supplier-cost")]),
    )
    with pytest.raises(KernelError) as error:
        engine.preview(["scope"])
    assert error.value.code == "reserve_cost_payment"


def raw_movement(save, proof, sid, amount, direction, day="2026-09-03"):
    save(
        "platform_movement",
        sid,
        dict(
            period=day[:7],
            actual_date=day,
            platform_account_id="platform",
            direction=direction,
            amount_fen=amount,
            source_evidence_digest=proof,
            source_location=sid,
            transaction_reference=sid,
        ),
    )


def test_full_existing_cost_path_and_capital_keep_exact_results_and_cannot_be_reused(book):
    engine, save, publish, proof = book
    from test_platforms import funding_data

    save(
        "bank_platform_transfer",
        "recharge",
        transfer_data(direction="bank_to_platform", amount=3000),
    )
    debt(save, "service", amount=1150)
    save(
        "platform_payment",
        "service-paid",
        payment_data(source="service", amount=1150, party="employee"),
    )
    raw_movement(save, proof, "out-reserve", 1850, "outflow")
    save(
        "platform_expense_confirmation",
        "pool",
        dict(
            period="2026-09",
            platform_account_id="platform",
            expense_class="administration",
            outgoing_movement_ids=["out-reserve"],
            confirmed_amount_fen=1850,
            treatment_confirmed=True,
        ),
    )
    funding = funding_data(amount=600)
    funding["actual_date"] = "2026-09-02"
    save("platform_funding", "capital", funding)
    save("bank_platform_transfer", "capital-to-bank", transfer_data(amount=600))
    publish("recharge", "service", "service-paid", "pool", "capital", "capital-to-bank")
    before = {
        sid: current(engine, kind, sid)
        for kind, sid in (
            ("bank_platform_transfer", "recharge"),
            ("platform_funding", "capital"),
            ("bank_platform_transfer", "capital-to-bank"),
        )
    }
    scope = scope_data(
        costs=[dict(source_kind="platform_expense_confirmation", source_id="pool")],
        treatments=[
            dict(
                transfer_id="recharge",
                treatment="already_expensed",
                expense_ids=["service", "pool"],
                payment_ids=["service-paid"],
            ),
            dict(
                transfer_id="capital-to-bank",
                treatment="capital_pass_through",
                funding_id="capital",
            ),
        ],
    )
    save("managed_reserve_scope", "scope", scope)
    publish("scope", "recharge", "capital-to-bank")
    for sid, old in before.items():
        new = current(engine, old.kind, sid)
        assert new.values == old.values
        assert new.fact_id == old.fact_id
    assert (
        current(engine, "managed_reserve_scope", "scope").values["cost_sources"][0]["amount_fen"]
        == 1850
    )
    # Reusing the supplier cost/payment or reserve amount for a second transfer is not a proof.
    save(
        "bank_platform_transfer",
        "second-recharge",
        transfer_data(direction="bank_to_platform", amount=3000),
    )
    scope["transfer_treatments"].append(
        dict(
            transfer_id="second-recharge",
            treatment="already_expensed",
            expense_ids=["service", "pool"],
            payment_ids=["service-paid"],
        )
    )
    save("managed_reserve_scope", "scope", scope, revision=1)
    with pytest.raises(KernelError) as error:
        engine.preview(["scope", "second-recharge"])
    assert error.value.code == "reserve_scope_overlap"


def test_boundary_rows_are_zero_accounting_complete_single_use_sources(book):
    from test_platform_movements import readiness

    from ai_accounting.kernel.domains.platforms import PlatformBoundaryDisposition

    engine, save, publish, proof = book
    save("managed_reserve_scope", "scope", scope_data())
    raw_movement(save, proof, "tiny-in", 10, "inflow")
    raw_movement(save, proof, "tiny-out", 10, "outflow", "2026-09-04")
    publish("scope", "tiny-in", "tiny-out")
    assert readiness(engine, "2026-09")
    data = dict(
        period="2026-09",
        scope_id="scope",
        platform_account_id="platform",
        movement_ids=["tiny-in", "tiny-out"],
    )
    save("platform_boundary_disposition", "boundary", data)
    publish("boundary")
    assert not readiness(engine, "2026-09")
    assert lines(engine, "platform_boundary_disposition", "boundary") == []
    result = current(engine, "platform_boundary_disposition", "boundary")
    assert result.values["inflow_fen"] == result.values["outflow_fen"] == 10
    fact = PlatformBoundaryDisposition.model_validate_json(__import__("json").dumps(data))
    fact.validate_material_amount(
        "result.outflow_fen", 10, source_amounts=(10,), source_directions=("outflow",)
    )
    with pytest.raises(KernelError):
        fact.validate_material_amount(
            "result.inflow_fen", 10, source_amounts=(10,), source_directions=("outflow",)
        )
    save("platform_boundary_disposition", "duplicate", data)
    with pytest.raises(KernelError) as error:
        engine.preview(["duplicate"])
    assert error.value.code == "platform_movement_consumed"


def test_new_expense_transfer_cashflow_is_operating_once_and_settlement_has_no_cash(book):
    from ai_accounting.kernel.reports import Reports

    engine, save, publish, _ = book
    save("bank_platform_transfer", "transfer", transfer_data(direction="bank_to_platform"))
    publish("transfer")
    save(
        "managed_reserve_scope",
        "scope",
        scope_data(
            costs=[dict(source_kind="bank_platform_transfer", source_id="transfer")],
            treatments=[dict(transfer_id="transfer", treatment="expense_on_boundary")],
        ),
    )
    publish("scope", "transfer")
    debt(save)
    publish("debt")
    compile_settlement(book)
    publish("settlement")
    report = Reports(engine).report(2026, 3)
    assert report["statements"]["cash_flow_statement"]["6"]["current_fen"] == 1000
    assert not any(
        issue["field"].startswith("report_source") for issue in report.get("fact_issues", [])
    ), [x for x in report.get("fact_issues", []) if x["field"].startswith("report_source")]


def test_settlement_cannot_precede_debt_or_use_unknown_receipt_and_cost_claims_are_not_input(book):
    from pydantic import ValidationError

    engine, save, publish, _ = book
    bank_cost(book)
    debt(save, period="2026-10")
    publish("debt")
    with pytest.raises(KernelError) as error:
        compile_settlement(book)
    assert error.value.code == "reserve_debt_source"
    from ai_accounting.kernel.domains.managed_reserve import ReserveSettlementInput

    data = dict(
        period="2026-10",
        scope_id="scope",
        actual_date="2026-10-01",
        recipient_id="employee",
        amount_fen=500,
        payment_confirmed=True,
        sources=[
            dict(source_kind="expense", source_id="debt", obligation="primary", amount_fen=500)
        ],
        cost_claims=[dict(source_kind="expense", source_id="reserve-cost", amount_fen=500)],
    )
    with pytest.raises(ValidationError):
        ReserveSettlementInput.model_validate_json(__import__("json").dumps(data))


def direct_bank_cost(book, amount=1000):
    engine, save, publish, _ = book
    save(
        "managed_reserve_bank_expense",
        "direct-cost",
        dict(
            period="2026-09",
            scope_id="scope",
            actual_date="2026-09-02",
            bank_account_id="bank-a",
            amount_fen=amount,
        ),
    )
    save(
        "managed_reserve_scope",
        "scope",
        scope_data(
            costs=[dict(source_kind="managed_reserve_bank_expense", source_id="direct-cost")]
        ),
    )
    publish("scope", "direct-cost")


def test_bank_exit_is_not_a_supplier_debt_and_reconciles_original_outgoing(book):
    from test_banking import entry, opening, reconciliation, statement

    from ai_accounting.kernel.domains.managed_reserve import ManagedReserveBankExpense

    engine, save, publish, _ = book
    opening(save, publish)
    direct_bank_cost(book)
    assert lines(engine, "managed_reserve_bank_expense", "direct-cost") == [
        ("5602", 1000, 0, None),
        ("1002", 0, 1000, "managed_reserve_outflow"),
    ]
    assert not current(engine, "managed_reserve_bank_expense", "direct-cost").values.get(
        "obligations"
    )
    statement(save, publish, [entry("out", "2026-09-02", -1000)])
    reconciliation(
        save,
        publish,
        [
            dict(
                reference="out", source_kind="managed_reserve_bank_expense", source_id="direct-cost"
            )
        ],
    )
    assert current(engine, "bank_reconciliation", "reconciliation").values["balanced"]
    with engine.store.connection(read_only=True) as connection:
        fact = engine.store.current_fact(connection, "direct-cost").fact
    assert isinstance(fact, ManagedReserveBankExpense)
    fact.validate_material_amount(
        "fact.amount_fen", 1000, source_amounts=(1000,), source_directions=("outflow",)
    )
    with pytest.raises(KernelError):
        fact.validate_material_amount(
            "fact.amount_fen", 1000, source_amounts=(1000,), source_directions=("inflow",)
        )
    with pytest.raises(KernelError):
        engine.save_fact(
            fact.kind,
            "direct-cost",
            fact.model_dump(mode="json") | dict(amount_fen=999),
            evidence=[book[3]],
            expected_revision=1,
            request_id="rewrite-cash",
        )


def test_direct_bank_cost_capacity_is_shared_with_later_expense_recovery(book):
    engine, save, publish, _ = book
    direct_bank_cost(book)
    debt(save, amount=700)
    publish("debt")
    compile_settlement(book, amount=700)
    publish("settlement")
    save(
        "expense_recovery",
        "return",
        dict(
            period="2026-10",
            source_expense_id="direct-cost",
            counterparty_id="actual-return",
            amount_fen=400,
            recovery_right_confirmed=True,
        ),
    )
    with pytest.raises(KernelError) as error:
        engine.preview(["return"])
    assert error.value.code == "excess_expense_recovery"


def test_boundary_disposition_closes_both_original_material_rows_without_fake_cash(tmp_path):
    from test_materials import Company
    from test_platform_material_dimensions import publish, save

    company = Company(tmp_path)
    source, proof = company.source(b"name,amount,period\nin,0.10,2026-01\nout,-0.10,2026-01\n")
    save(
        company,
        "managed_reserve_scope",
        "scope",
        scope_data() | dict(period="2026-01", effective_from="2026-01"),
    )
    for sid, direction in (("in", "inflow"), ("out", "outflow")):
        save(
            company,
            "platform_movement",
            sid,
            dict(
                period="2026-01",
                actual_date="2026-01-02",
                platform_account_id="platform",
                direction=direction,
                amount_fen=10,
                source_evidence_digest=proof,
                source_location=sid,
            ),
        )
    save(
        company,
        "platform_boundary_disposition",
        "boundary",
        dict(
            period="2026-01",
            scope_id="scope",
            platform_account_id="platform",
            movement_ids=["in", "out"],
        ),
    )
    publish(company, "scope", "in", "out", "boundary")
    with company.engine.store.connection(read_only=True) as connection:
        fact = company.engine.store.current_fact(connection, "boundary")
        calc = current(company.engine, fact.fact.kind, fact.subject_id)
    for location, dimension, amount in (
        ("CSV!B2", "inflow_fen", 10),
        ("CSV!B3", "outflow_fen", -10),
    ):
        company.resolve(
            source,
            location,
            [
                dict(
                    fact_kind=fact.fact.kind,
                    subject_id=fact.subject_id,
                    fact_id=fact.id,
                    calculation_id=calc.id,
                    amount_field="result." + dimension,
                    amount_fen=amount,
                    recognition_period="2026-01",
                )
            ],
            amount_fen=amount,
        )
    assert company.materials.check("2026-01")["status"] == "complete"


def test_closed_reserve_settlement_is_frozen_and_future_capacity_use_is_bounded(book):
    from material_fixture import supporting_text
    from test_banking import entry, opening, reconciliation, statement

    from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods

    engine, save, publish, proof = book
    supporting_text(engine, proof, period="2026-09")
    opening(save, publish)
    direct_bank_cost(book)
    debt(save)
    publish("debt")
    compile_settlement(book)
    publish("settlement")
    statement(save, publish, [entry("out", "2026-09-02", -1000)])
    reconciliation(
        save,
        publish,
        [
            dict(
                reference="out", source_kind="managed_reserve_bank_expense", source_id="direct-cost"
            )
        ],
    )
    periods = Periods(engine)
    for category in MATERIAL_CATEGORIES:
        active = category in {"bank", "transactions"}
        periods.inventory(
            "2026-09",
            category,
            evidence=[proof] if active else [],
            expected=int(active),
            no_business=not active,
            confirmation_evidence=proof,
            request_id="inventory-" + category,
        )
    preview = periods.preview_close("2026-09", owner_confirmation=proof)
    periods.close(
        "2026-09",
        owner_confirmation=proof,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="close-september",
    )
    frozen = periods.closed_report("2026-09")
    save(
        "expense_recovery",
        "future-return",
        dict(
            period="2026-10",
            source_expense_id="direct-cost",
            counterparty_id="confirmed-return",
            amount_fen=400,
            recovery_right_confirmed=True,
        ),
    )
    publish("future-return")
    assert periods.closed_report("2026-09") == frozen
    assert not engine.overview("2026-09")["pending"]
    # A real open-month return does not silently edit either historic bank outflow or reimbursement.
    assert lines(engine, "managed_reserve_bank_expense", "direct-cost")[1][2] == 1000
    assert lines(engine, "managed_reserve_obligation_settlement", "settlement")[1][2] == 500


def test_explicit_recording_correction_restores_cost_and_debt_capacity_atomically(book):
    engine, save, publish, _ = book
    direct_bank_cost(book)
    debt(save)
    publish("debt")
    compile_settlement(book)
    publish("settlement")
    original = current(engine, "managed_reserve_obligation_settlement", "settlement")
    reserves, payload, preview = compile_settlement(book, amount=400, commit=False)
    args = dict(
        **payload,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        expected_revision=1,
        request_id="wrong-actual-rewrite",
    )
    with pytest.raises(KernelError):
        reserves.confirm_settlement(**args)
    reserves.confirm_settlement(
        **(args | dict(request_id="explicit-recording-correction", recording_error_confirmed=True))
    )
    publish("settlement")
    assert current(engine, original.kind, "settlement").values["amount_fen"] == 400
    assert lines(engine, "managed_reserve_bank_expense", "direct-cost")[1][2] == 1000
    save(
        "expense_recovery",
        "restored-capacity",
        dict(
            period="2026-10",
            source_expense_id="direct-cost",
            counterparty_id="return",
            amount_fen=600,
            recovery_right_confirmed=True,
        ),
    )
    publish("restored-capacity")
    with engine.store.connection(read_only=True) as c:
        assert c.execute("SELECT 1 FROM calculation WHERE id=?", (original.id,)).fetchone()
        key = current(engine, "expense", "debt").values["obligations"][0]["key"]
        assert (
            c.execute("SELECT amount FROM balance WHERE balance_key=?", (key,)).fetchone()[0] == 100
        )


def test_management_note_does_not_expire_accounting_reserve_preview(book):
    from ai_accounting.kernel.periods import Periods

    engine, save, publish, _ = book
    direct_bank_cost(book)
    debt(save)
    publish("debt")
    reserves, payload, preview = compile_settlement(book, commit=False)
    Periods(engine).management(
        "debt",
        note="optional management description",
        payment_period=None,
        payment_category=None,
        expected_revision=0,
        request_id="optional-note",
    )
    result = reserves.confirm_settlement(
        **payload,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        expected_revision=0,
        request_id="confirm-after-management",
    )
    assert result["status"] == "confirmed"
