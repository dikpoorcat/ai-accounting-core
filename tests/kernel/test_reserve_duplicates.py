import pytest
from test_duplicates import company as duplicate_company
from test_duplicates import evidence, material_source, owner_review, write_fact

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.duplicates import (
    DUPLICATE_CONTRACT_VERSION,
    DuplicateCandidates,
    Duplicates,
    SourceLocation,
    verify_duplicate_checks,
)
from ai_accounting.kernel.entities import Entities

company = duplicate_company


@pytest.fixture
def money_company(company):
    engine, party = company
    entities = Entities(engine)
    accounts = {
        category: entities.register_entity(
            "fund_account",
            {},
            account_type=category,
            source=f"合成{category}账户",
            request_id=f"reserve-duplicate-{category}",
        )["entity_id"]
        for category in ("bank", "cash", "platform")
    }
    return engine, party, accounts


def allocation(amount=1000):
    return {
        "source_kind": "expense",
        "source_id": "obligation",
        "obligation": "primary",
        "amount_fen": amount,
    }


def payment(engine, *, account, channel="bank", direction="outflow", amount=1000, party="party"):
    kind = {"bank": "payment", "cash": "cash_payment", "platform": "platform_payment"}[channel]
    data = {
        "period": "2026-01",
        "actual_date": "2026-01-08",
        "direction": direction,
        f"{channel}_account_id": account,
        "counterparty_id": party,
        "amount_fen": amount,
        "allocations": (allocation(amount),),
    }
    if channel == "platform":
        data["movement_ids"] = ("platform-row",)
    return engine.store.registry.models[kind].model_validate(data)


def reserve(
    engine,
    *,
    refund=False,
    channel="bank",
    amount=1000,
    party=None,
    movement_ids=(),
    account,
):
    kind = "managed_reserve_refund" if refund else "managed_reserve_expense"
    return engine.store.registry.models[kind].model_validate(
        {
            "period": "2026-01",
            "actual_date": "2026-01-08",
            f"{channel}_account_id": account,
            "movement_ids": tuple(movement_ids),
            "counterparty_id": party,
            "amount_fen": amount,
        }
    )


def payroll_payment(engine, account, recipient, amount=1000):
    return engine.store.registry.models["payroll_reserve_payment"].model_validate(
        {
            "period": "2026-01",
            "actual_date": "2026-01-08",
            "bank_account_id": account,
            "amount_fen": amount,
            "allocations": (
                {
                    "source_kind": "payroll",
                    "source_id": "payroll-source",
                    "obligation": "net",
                    "recipient_id": recipient,
                    "amount_fen": amount - 100,
                },
            ),
            "reserve_expense_fen": 100,
            "return_period": "2026-01",
            "actual_return_date": "2026-01-09",
            "return_confirmed": True,
            "complete_group_confirmed": True,
        }
    )


def prepare(duplicates, connection, subject, fact, proof, locations=()):
    return duplicates.prepare(
        connection,
        subject_id=subject,
        revision=1,
        fact=fact,
        evidence=(proof,),
        source_locations=locations,
    )


def test_contract_v2_and_cross_kind_money_without_exact_original_stays_weak(money_company):
    engine, party, accounts = money_company
    first_proof = evidence(engine, "ordinary-bank-payment")
    second_proof = evidence(engine, "reserve-bank-expense")
    ordinary = payment(engine, account=accounts["bank"], party=party)
    proposed = reserve(engine, account=accounts["bank"], party=party)
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        write_fact(engine.store, connection, "ordinary", ordinary, (first_proof,))
        prepared = prepare(
            DuplicateCandidates(engine.store),
            connection,
            "reserve",
            proposed,
            second_proof,
        )
        ddl = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name='business_duplicate_check'"
        ).fetchone()[0]

    assert DUPLICATE_CONTRACT_VERSION == 2
    assert "contract_version=2" in ddl
    assert prepared["candidate_version"] == 2
    assert prepared["status"] == "clear"
    assert prepared["strong_candidates"] == []
    assert prepared["weak_candidates"][0]["kind"] == "payment"
    assert prepared["weak_candidates"][0]["signals"][0]["code"] == ("same_actual_money_coordinates")


def test_missing_counterparty_is_weak_without_an_exact_original(money_company):
    engine, party, accounts = money_company
    first_proof = evidence(engine, "known-party-payment")
    second_proof = evidence(engine, "unknown-party-reserve")
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        write_fact(
            engine.store,
            connection,
            "ordinary",
            payment(engine, account=accounts["bank"], party=party),
            (first_proof,),
        )
        prepared = prepare(
            DuplicateCandidates(engine.store),
            connection,
            "reserve",
            reserve(engine, account=accounts["bank"]),
            second_proof,
        )

    assert prepared["status"] == "clear"
    assert prepared["strong_candidates"] == []
    assert prepared["weak_candidates"][0]["signals"][0] == {
        "code": "same_actual_money_coordinates",
        "matched_fields": [
            "funds_category",
            "funds_account_id",
            "actual_date",
            "direction",
            "amount_fen",
        ],
        "distinct_locations_proven": False,
    }


def test_same_payment_money_with_different_allocations_stays_weak(money_company):
    engine, party, accounts = money_company
    first_proof = evidence(engine, "payment-allocation-a")
    second_proof = evidence(engine, "payment-allocation-b")
    first = payment(engine, account=accounts["bank"], party=party)
    second = engine.store.registry.models["payment"].model_validate(
        {
            **first.model_dump(mode="json"),
            "allocations": (allocation(1000) | {"source_id": "another-obligation"},),
        }
    )
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        write_fact(engine.store, connection, "payment-a", first, (first_proof,))
        prepared = prepare(
            DuplicateCandidates(engine.store),
            connection,
            "payment-b",
            second,
            second_proof,
        )

    assert prepared["status"] == "clear"
    assert prepared["strong_candidates"] == []
    assert prepared["weak_candidates"][0]["signals"][0]["code"] == ("same_actual_money_coordinates")


def test_exact_bank_original_is_strong_even_when_reserve_counterparty_is_unknown(
    money_company,
):
    engine, party, accounts = money_company
    proof = evidence(engine, "same-bank-row")
    source = material_source(engine, proof, subject="bank-source")
    location = SourceLocation(
        source_id="bank-source",
        source_fact_id=source["fact_id"],
        evidence_digest=proof,
        location="sheet-1!row-2",
    )
    duplicates = DuplicateCandidates(engine.store)
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        ordinary = payment(engine, account=accounts["bank"], party=party)
        first = write_fact(engine.store, connection, "ordinary", ordinary, (proof,))
        initial = prepare(duplicates, connection, "ordinary", ordinary, proof, (location,))
        duplicates.record_check(connection, prepared=initial, result_fact_id=first.id, review=None)
        prepared = prepare(
            duplicates,
            connection,
            "reserve",
            reserve(engine, account=accounts["bank"]),
            proof,
            (location,),
        )

    signal = prepared["strong_candidates"][0]["signals"][0]
    assert signal["code"] == "same_exact_material_location"
    assert signal["source_locations"] == [{"evidence_digest": proof, "location": "sheet-1!row-2"}]


def test_cash_refund_and_platform_expense_match_only_their_real_channel(money_company):
    engine, party, accounts = money_company
    proofs = [evidence(engine, name) for name in ("cash-a", "cash-b", "platform-a", "platform-b")]
    source = material_source(engine, proofs[0], subject="cash-source")
    location = SourceLocation(
        source_id="cash-source",
        source_fact_id=source["fact_id"],
        evidence_digest=proofs[0],
        location="sheet-1!row-2",
    )
    duplicates = DuplicateCandidates(engine.store)
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        cash_payment = payment(
            engine,
            account=accounts["cash"],
            channel="cash",
            direction="inflow",
            party=party,
        )
        saved = write_fact(
            engine.store,
            connection,
            "cash-payment",
            cash_payment,
            (proofs[0],),
        )
        initial = prepare(
            duplicates,
            connection,
            "cash-payment",
            cash_payment,
            proofs[0],
            (location,),
        )
        duplicates.record_check(connection, prepared=initial, result_fact_id=saved.id, review=None)
        cash = prepare(
            duplicates,
            connection,
            "cash-refund",
            reserve(
                engine,
                account=accounts["cash"],
                channel="cash",
                refund=True,
                party=party,
            ),
            proofs[0],
            (location,),
        )
        write_fact(
            engine.store,
            connection,
            "platform-payment",
            payment(
                engine,
                account=accounts["platform"],
                channel="platform",
                party=party,
            ),
            (proofs[2],),
        )
        platform = prepare(
            duplicates,
            connection,
            "platform-reserve",
            reserve(
                engine,
                account=accounts["platform"],
                channel="platform",
                movement_ids=("platform-row",),
            ),
            proofs[3],
        )

    assert cash["strong_candidates"][0]["kind"] == "cash_payment"
    assert platform["strong_candidates"][0]["signals"][0] == {
        "code": "same_complete_actual_money",
        "matched_fields": [
            "funds_category",
            "funds_account_id",
            "actual_date",
            "direction",
            "amount_fen",
            "movement_ids",
        ],
    }


def test_payroll_whole_bank_exit_matches_reserve_by_exact_original(money_company):
    engine, party, accounts = money_company
    proof = evidence(engine, "payroll-bank-row")
    source = material_source(engine, proof, subject="payroll-bank-source")
    location = SourceLocation(
        source_id="payroll-bank-source",
        source_fact_id=source["fact_id"],
        evidence_digest=proof,
        location="sheet-1!row-2",
    )
    duplicates = DuplicateCandidates(engine.store)
    payroll = payroll_payment(engine, accounts["bank"], party)
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        saved = write_fact(engine.store, connection, "payroll-payment", payroll, (proof,))
        initial = prepare(duplicates, connection, "payroll-payment", payroll, proof, (location,))
        duplicates.record_check(connection, prepared=initial, result_fact_id=saved.id, review=None)
        prepared = prepare(
            duplicates,
            connection,
            "reserve",
            reserve(engine, account=accounts["bank"]),
            proof,
            (location,),
        )

    assert prepared["strong_candidates"][0]["kind"] == "payroll_reserve_payment"
    assert prepared["strong_candidates"][0]["signals"][0]["code"] == (
        "same_exact_material_location"
    )


def test_reserve_expense_never_matches_expense_recognition(money_company):
    engine, party, accounts = money_company
    proof = evidence(engine, "expense-and-payment")
    source = material_source(engine, proof, subject="expense-source")
    location = SourceLocation(
        source_id="expense-source",
        source_fact_id=source["fact_id"],
        evidence_digest=proof,
        location="sheet-1!row-2",
    )
    expense = engine.store.registry.models["expense"].model_validate(
        {
            "period": "2026-01",
            "counterparty_id": party,
            "amount_fen": 1000,
            "expense_class": "administration",
            "creditor_kind": "supplier",
        }
    )
    duplicates = DuplicateCandidates(engine.store)
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        saved = write_fact(engine.store, connection, "expense", expense, (proof,))
        initial = prepare(duplicates, connection, "expense", expense, proof, (location,))
        duplicates.record_check(connection, prepared=initial, result_fact_id=saved.id, review=None)
        prepared = prepare(
            duplicates,
            connection,
            "reserve",
            reserve(engine, account=accounts["bank"], party=party),
            proof,
            (location,),
        )

    assert prepared["strong_candidates"] == prepared["weak_candidates"] == []


def test_batch_cross_kind_candidate_can_be_reviewed_and_reused(money_company):
    engine, party, accounts = money_company
    proof = evidence(engine, "batch-cross-kind")
    source = material_source(engine, proof, subject="batch-cross-kind-source")
    location = SourceLocation(
        source_id="batch-cross-kind-source",
        source_fact_id=source["fact_id"],
        evidence_digest=proof,
        location="sheet-1!row-2",
    )
    ordinary = payment(engine, account=accounts["bank"], party=party)
    proposed = reserve(engine, account=accounts["bank"], party=party)
    proposals = [
        {
            "subject_id": "ordinary",
            "revision": 1,
            "fact": ordinary,
            "evidence": (proof,),
            "source_locations": (location,),
        },
        {
            "subject_id": "reserve",
            "revision": 1,
            "fact": proposed,
            "evidence": (proof,),
            "source_locations": (location,),
        },
    ]
    duplicates = DuplicateCandidates(engine.store)
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        prepared = duplicates.prepare_batch(connection, proposals)
        saved = write_fact(engine.store, connection, "ordinary", ordinary, (proof,))
        duplicates.record_check(
            connection, prepared=prepared[0], result_fact_id=saved.id, review=None
        )
        result = duplicates.record_check(
            connection,
            prepared=prepared[1],
            result_fact_id=None,
            review=owner_review(
                prepared[1],
                proof,
                action="reuse_existing",
                candidate_subject_id="ordinary",
            ),
            fact_ids_by_subject={"ordinary": saved.id},
        )
        verify_duplicate_checks(connection)

    assert prepared[1]["strong_candidates"][0]["kind"] == "payment"
    assert result["action"] == "reuse_existing"
    assert result["selected_fact_id"] == saved.id


def test_public_cross_kind_reuse_returns_existing_type_without_new_reserve(money_company):
    engine, party, accounts = money_company
    proof = evidence(engine, "public-cross-kind-reuse")
    source = material_source(engine, proof, subject="public-cross-kind-source")
    location = SourceLocation(
        source_id="public-cross-kind-source",
        source_fact_id=source["fact_id"],
        evidence_digest=proof,
        location="sheet-1!row-2",
    )
    ordinary = payment(engine, account=accounts["bank"], party=party)
    proposed = reserve(engine, account=accounts["bank"], party=party)
    saved = engine.save_fact(
        "payment",
        "ordinary",
        ordinary.model_dump(mode="json"),
        evidence=(proof,),
        source_locations=(location,),
        expected_revision=0,
        request_id="save-ordinary-for-reserve-reuse",
    )
    prepared = Duplicates(engine).prepare_fact_registration(
        "managed_reserve_expense",
        "reserve",
        proposed.model_dump(mode="json"),
        evidence=(proof,),
        source_locations=(location,),
        expected_revision=0,
    )
    reused = engine.save_fact(
        "managed_reserve_expense",
        "reserve",
        proposed.model_dump(mode="json"),
        evidence=(proof,),
        source_locations=(location,),
        expected_revision=0,
        request_id="reuse-ordinary-for-reserve",
        review=owner_review(
            prepared,
            proof,
            action="reuse_existing",
            candidate_subject_id="ordinary",
        ).model_dump(mode="json"),
    )

    assert reused["status"] == "reused"
    assert reused["fact_id"] == saved["fact_id"]
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT 1 FROM subject WHERE id='reserve'").fetchone() is None
        selected_kind = connection.execute(
            "SELECT s.kind FROM fact_revision f JOIN subject s ON s.id=f.subject_id WHERE f.id=?",
            (reused["fact_id"],),
        ).fetchone()[0]
    assert selected_kind == "payment"


def test_reuse_rejects_candidate_after_its_current_version_changes(money_company):
    engine, party, accounts = money_company
    proof = evidence(engine, "stale-cross-kind")
    source = material_source(engine, proof, subject="stale-cross-kind-source")
    location = SourceLocation(
        source_id="stale-cross-kind-source",
        source_fact_id=source["fact_id"],
        evidence_digest=proof,
        location="sheet-1!row-2",
    )
    ordinary = payment(engine, account=accounts["bank"], party=party)
    duplicates = DuplicateCandidates(engine.store)
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        first = write_fact(engine.store, connection, "ordinary", ordinary, (proof,))
        initial = prepare(
            duplicates,
            connection,
            "ordinary",
            ordinary,
            proof,
            (location,),
        )
        duplicates.record_check(connection, prepared=initial, result_fact_id=first.id, review=None)
        prepared = prepare(
            duplicates,
            connection,
            "reserve",
            reserve(engine, account=accounts["bank"], party=party),
            proof,
            (location,),
        )
        write_fact(
            engine.store,
            connection,
            "ordinary",
            payment(engine, account=accounts["bank"], amount=1100, party=party),
            (proof,),
            revision=2,
        )
        with pytest.raises(KernelError) as expired:
            duplicates.record_check(
                connection,
                prepared=prepared,
                result_fact_id=None,
                review=owner_review(
                    prepared,
                    proof,
                    action="reuse_existing",
                    candidate_subject_id="ordinary",
                ),
            )

    assert expired.value.code == "duplicate_candidate_expired"


def test_cross_type_review_controls_publish_and_close_recheck(money_company):
    engine, party, accounts = money_company
    first_proof = evidence(engine, "close-payment")
    second_proof = evidence(engine, "close-reserve")
    duplicates = DuplicateCandidates(engine.store)
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        write_fact(
            engine.store,
            connection,
            "ordinary",
            payment(
                engine,
                account=accounts["platform"],
                channel="platform",
                party=party,
            ),
            (first_proof,),
        )
        write_fact(
            engine.store,
            connection,
            "reserve",
            reserve(
                engine,
                account=accounts["platform"],
                channel="platform",
                party=party,
                movement_ids=("platform-row",),
            ),
            (second_proof,),
        )
        issues = duplicates.close_readiness(connection, "2026-01")
        with pytest.raises(KernelError) as blocked:
            duplicates.require_publishable(connection, ("reserve",))

    assert len(issues) == 1
    assert issues[0]["subject_id"] in {"ordinary", "reserve"}
    assert issues[0]["candidate_subject_id"] in {"ordinary", "reserve"}
    assert blocked.value.code == "duplicate_review_required"


def test_different_platform_rows_are_weak_and_do_not_interrupt(money_company):
    engine, party, accounts = money_company
    first_proof = evidence(engine, "platform-distinct-a")
    second_proof = evidence(engine, "platform-distinct-b")
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        write_fact(
            engine.store,
            connection,
            "ordinary",
            payment(
                engine,
                account=accounts["platform"],
                channel="platform",
                party=party,
            ),
            (first_proof,),
        )
        prepared = prepare(
            DuplicateCandidates(engine.store),
            connection,
            "reserve",
            reserve(
                engine,
                account=accounts["platform"],
                channel="platform",
                party=party,
                movement_ids=("another-platform-row",),
            ),
            second_proof,
        )

    assert prepared["status"] == "clear"
    assert prepared["strong_candidates"] == []
    assert prepared["weak_candidates"][0]["signals"][0]["code"] == ("same_actual_money_coordinates")
    assert prepared["weak_candidates"][0]["signals"][0]["distinct_locations_proven"]
