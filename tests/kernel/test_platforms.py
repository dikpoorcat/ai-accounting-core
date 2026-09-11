"""Company platform money uses actual sources and common settlement capacity."""

import itertools
from collections import defaultdict

import pytest
from pydantic import ValidationError
from test_banking import entry, match, opening, reconciliation, statement
from test_business_domains import surtax_policy, vat_policy

from ai_accounting.kernel.command_schema import command_models, validate_command
from ai_accounting.kernel.contracts import Context, KernelError, NeedsInformation, Read
from ai_accounting.kernel.domains import assets
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.service import default_registry
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.types import YearMonth


@pytest.fixture
def book(tmp_path):
    registry = default_registry()
    engine = Engine(
        Store.create(tmp_path / "synthetic.sqlite", registry, "company", "911100000000000001", "db")
    )
    evidence = engine.register_evidence(
        b"Synthetic platform receipts and explicit obligations",
        "text/plain",
        "proof",
        request_id="proof",
    )["digest"]
    counter = itertools.count()
    wire = command_models(registry)

    def save(kind, subject, data, revision=0):
        # Existing media scenarios supply explicit actual money. Preserve each as a
        # separate synthetic original row; dedicated movement tests exercise mismatches.
        if (
            kind in {"platform_payment", "platform_funding", "bank_platform_transfer"}
            and "movement_ids" not in data
        ):
            source = "movement-" + subject
            data["movement_ids"] = [source]
            direction = data.get("direction", "inflow")
            direction = {"bank_to_platform": "inflow", "platform_to_bank": "outflow"}.get(
                direction, direction
            )
            engine.save_fact(
                "platform_movement",
                source,
                {
                    "period": data["period"],
                    "actual_date": data["actual_date"],
                    "platform_account_id": data["platform_account_id"],
                    "direction": direction,
                    "amount_fen": data["amount_fen"],
                    "source_evidence_digest": evidence,
                    "source_location": "synthetic-row:" + subject,
                    "transaction_reference": subject,
                },
                evidence=[evidence],
                expected_revision=0,
                request_id="source-" + str(next(counter)),
            )
        validate_command(
            wire,
            "save_fact",
            {
                "company_id": "company",
                "kind": kind,
                "subject_id": subject,
                "data": data,
                "evidence": [evidence],
                "expected_revision": revision,
                "request_id": "validate-public-fact",
            },
        )
        return engine.save_fact(
            kind,
            subject,
            data,
            evidence=(evidence,),
            expected_revision=revision,
            request_id="save-" + str(next(counter)),
        )

    def publish(*subjects):
        subjects = list(subjects)
        with engine.store.connection(read_only=True) as connection:
            for subject in tuple(subjects):
                fact = engine.store.current_fact(connection, subject).fact
                for source in getattr(fact, "movement_ids", ()):
                    if not engine.store.select(
                        connection, Read("calculation", "platform_movement", "@" + source)
                    ):
                        subjects.append(source)
        preview = engine.preview(list(subjects))
        return engine.confirm(
            list(subjects),
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id="publish-" + str(next(counter)),
        )

    return engine, save, publish, evidence


def current(engine, kind, subject):
    with engine.store.connection(read_only=True) as connection:
        return engine.store.select(connection, Read("calculation", kind, "@" + subject))[0]


def balances(engine):
    with engine.store.connection(read_only=True) as connection:
        return defaultdict(
            int,
            {
                row["balance_key"]: row["amount"]
                for row in connection.execute("SELECT balance_key,amount FROM balance")
            },
        )


def lines(engine, kind, subject):
    result = current(engine, kind, subject)
    with engine.store.connection(read_only=True) as connection:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT l.account,l.debit,l.credit,l.cashflow FROM voucher_line l "
                "JOIN voucher_version v ON v.id=l.version_id WHERE v.calculation_id=?",
                (result.id,),
            )
        ]


def funding_data(*, kind="capital", amount=1000):
    return {
        "period": "2026-09",
        "actual_date": "2026-09-01",
        "funding_kind": kind,
        "owner_id": "owner",
        "amount_fen": amount,
        "platform_account_id": "platform",
    }


def transfer_data(*, direction="platform_to_bank", amount=1000):
    return {
        "period": "2026-09",
        "actual_date": "2026-09-02",
        "direction": direction,
        "bank_account_id": "bank-a",
        "platform_account_id": "platform",
        "amount_fen": amount,
    }


def payment_data(
    *,
    kind="platform_payment",
    amount=1000,
    source_kind="expense",
    source="expense",
    day="2026-09-03",
    direction="outflow",
    party="supplier",
    obligation="primary",
):
    account_key = {
        "payment": "bank_account_id",
        "cash_payment": "cash_account_id",
        "platform_payment": "platform_account_id",
    }[kind]
    return {
        "period": day[:7],
        "actual_date": day,
        "direction": direction,
        account_key: {"payment": "bank-a", "cash_payment": "cashbox"}.get(kind, "platform"),
        "counterparty_id": party,
        "amount_fen": amount,
        "allocations": [
            {
                "source_kind": source_kind,
                "source_id": source,
                "obligation": obligation,
                "amount_fen": amount,
            }
        ],
    }


def expense(save, amount=1000):
    return save(
        "expense",
        "expense",
        {
            "period": "2026-09",
            "counterparty_id": "supplier",
            "amount_fen": amount,
            "expense_class": "administration",
            "creditor_kind": "supplier",
        },
    )


def test_platform_capital_then_bank_transfer_is_one_capital_and_one_bank_receipt(book):
    engine, save, publish, _ = book
    opening(save, publish)
    save("platform_funding", "capital", funding_data())
    save("bank_platform_transfer", "to-bank", transfer_data())
    publish("capital", "to-bank")
    statement(save, publish, [entry(day="2026-09-02")])
    reconciliation(save, publish, [match(kind="bank_platform_transfer", source="to-bank")])
    assert balances(engine)["platform"] == 0
    assert balances(engine)["bank-a"] == 1000
    assert lines(engine, "platform_funding", "capital") == [
        ("1012", 1000, 0, "financing_receipts"),
        ("3001", 0, 1000, None),
    ]
    assert lines(engine, "bank_platform_transfer", "to-bank") == [
        ("1002", 1000, 0, None),
        ("1012", 0, 1000, None),
    ]
    assert current(engine, "bank_reconciliation", "reconciliation").values["matched_count"] == 1


def test_bank_recharge_and_next_day_platform_payment_keep_two_real_dates(book):
    engine, save, publish, _ = book
    opening(save, publish)
    save(
        "funding",
        "bank-capital",
        {
            **{k: v for k, v in funding_data().items() if k != "platform_account_id"},
            "bank_account_id": "bank-a",
        },
    )
    save("bank_platform_transfer", "recharge", transfer_data(direction="bank_to_platform"))
    expense(save)
    save("platform_payment", "supplier-paid", payment_data())
    publish("bank-capital", "recharge", "expense", "supplier-paid")
    statement(save, publish, [entry(), entry("recharge", "2026-09-02", -1000)])
    reconciliation(
        save,
        publish,
        [match(source="bank-capital"), match("recharge", "bank_platform_transfer", "recharge")],
    )
    assert balances(engine)["expense:expense:primary"] == 0
    assert balances(engine)["platform"] == 0
    assert balances(engine)["bank-a"] == 0
    assert (
        current(engine, "platform_payment", "supplier-paid").values["actual_date"] == "2026-09-03"
    )
    assert (
        current(engine, "bank_platform_transfer", "recharge").values["actual_date"] == "2026-09-02"
    )
    assert lines(engine, "bank_platform_transfer", "recharge") == [
        ("1012", 1000, 0, None),
        ("1002", 0, 1000, None),
    ]


@pytest.mark.parametrize("wrong", ["bank", "day", "direction", "amount"])
def test_bank_platform_matching_rejects_wrong_actual_source(book, wrong):
    _, save, publish, _ = book
    bank = "wrong-bank" if wrong == "bank" else "bank-a"
    opening(save, publish, bank=bank)
    save("bank_platform_transfer", "transfer", transfer_data())
    publish("transfer")
    day = "2026-09-03" if wrong == "day" else "2026-09-02"
    amount = -1000 if wrong == "direction" else 999 if wrong == "amount" else 1000
    statement(save, publish, [entry(day=day, amount=amount)], bank=bank)
    with pytest.raises(KernelError) as error:
        reconciliation(
            save, publish, [match(kind="bank_platform_transfer", source="transfer")], bank=bank
        )
    assert error.value.code == (
        "bank_account_mismatch" if wrong == "bank" else "bank_match_difference"
    )


def test_unpublished_bank_transfer_cannot_hide_but_platform_only_funding_is_not_bank_cash(book):
    _, save, publish, _ = book
    opening(save, publish)
    save("platform_funding", "platform-capital", funding_data())
    save("bank_platform_transfer", "unpublished-transfer", transfer_data())
    statement(save, publish, [])
    with pytest.raises(NeedsInformation) as error:
        reconciliation(save, publish, [])
    assert error.value.issues[0]["field"] == "actual_funds"


def test_platform_bank_and_cash_share_obligation_capacity(book):
    engine, save, publish, _ = book
    expense(save)
    for kind, amount in [("payment", 400), ("cash_payment", 200), ("platform_payment", 400)]:
        save(kind, kind, payment_data(kind=kind, amount=amount))
    publish("expense", "payment", "cash_payment", "platform_payment")
    assert balances(engine)["expense:expense:primary"] == 0
    save("platform_payment", "one-too-many", payment_data(amount=1))
    with pytest.raises(NeedsInformation) as error:
        publish("one-too-many")
    assert error.value.issues[0]["field"] == "overpayment"
    assert balances(engine)["expense:expense:primary"] == 0


def test_platform_loan_repaid_by_bank_preserves_original_funding_fact(book):
    engine, save, publish, _ = book
    original = save("platform_funding", "owner-loan", funding_data(kind="loan"))
    save(
        "payment",
        "loan-repaid",
        payment_data(
            kind="payment", source_kind="platform_funding", source="owner-loan", party="owner"
        ),
    )
    publish("owner-loan", "loan-repaid")
    assert balances(engine)["platform_funding:owner-loan:primary"] == 0
    with engine.store.connection(read_only=True) as connection:
        assert engine.store.current_fact(connection, "owner-loan").id == original["fact_id"]


def test_non_cash_settlement_and_platform_payment_do_not_duplicate_capacity(book):
    engine, save, publish, _ = book
    expense(save)
    save(
        "refundable_deposit",
        "deposit",
        {
            "period": "2026-09",
            "counterparty_id": "supplier",
            "amount_fen": 500,
            "refund_right_confirmed": True,
        },
    )
    saved = save(
        "settlement",
        "offset",
        {
            "period": "2026-09",
            "settlement_kind": "debt_offset",
            "offset_right_confirmed": True,
            "first": {
                "source_kind": "expense",
                "source_id": "expense",
                "obligation": "primary",
                "amount_fen": 500,
            },
            "second": {
                "source_kind": "refundable_deposit",
                "source_id": "deposit",
                "obligation": "refund",
                "amount_fen": 500,
            },
        },
    )
    save("platform_payment", "rest", payment_data(amount=500))
    publish("expense", "deposit", "offset", "rest")
    assert balances(engine)["expense:expense:primary"] == 0
    with engine.store.connection(read_only=True) as connection:
        assert engine.store.current_fact(connection, "offset").id == saved["fact_id"]
    save("platform_payment", "excess", payment_data(amount=1))
    with pytest.raises(KernelError) as error:
        publish("excess")
    assert error.value.code == "overallocated_obligation"


@pytest.mark.parametrize(
    "kind,data",
    [
        ("platform_payment", payment_data()),
        ("platform_funding", funding_data()),
        ("bank_platform_transfer", transfer_data()),
    ],
)
@pytest.mark.parametrize(
    "change",
    [
        {"amount_fen": 1.5},
        {"amount_fen": True},
        {"actual_date": "2026-09"},
        {"actual_date": "2026-10-01"},
    ],
)
def test_public_wire_rejects_invalid_amount_date_atomically(book, kind, data, change):
    engine, _, _, evidence = book
    source = {
        "kind": "expense",
        "subject_id": "must-not-save",
        "data": {
            "period": "2026-09",
            "counterparty_id": "supplier",
            "amount_fen": 1,
            "expense_class": "administration",
            "creditor_kind": "supplier",
        },
        "evidence": [evidence],
        "expected_revision": 0,
    }
    invalid = {
        "kind": kind,
        "subject_id": "invalid",
        "data": {**data, "movement_ids": ["source-row"], **change},
        "evidence": [evidence],
        "expected_revision": 0,
    }
    with pytest.raises((ValidationError, ValueError, KernelError)):
        validate_command(
            command_models(engine.store.registry),
            "save_facts",
            {"company_id": "company", "facts": [source, invalid], "request_id": "invalid"},
        )
    with pytest.raises((ValidationError, ValueError, KernelError)):
        engine.save_facts([source, invalid], request_id="invalid")
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM fact_revision").fetchone()[0] == 0


def test_receipt_tax_point_once_across_media_and_recording_correction_preserves_money(book):
    engine, save, publish, evidence = book
    save("vat_policy", "vat", {"period": "2026-01", "policy": vat_policy().model_dump(mode="json")})
    save(
        "service_sale",
        "sale",
        {
            "period": "2026-08",
            "customer_id": "customer",
            "gross_fen": 10100,
            "vat_policy_id": "vat",
            "exemption_eligible": False,
            "tax_obligation_period": "2026-09",
        },
    )
    first = payment_data(
        amount=3000,
        source_kind="service_sale",
        source="sale",
        day="2026-09-02",
        direction="inflow",
        party="customer",
    )
    original = save("platform_payment", "first", first)
    second = payment_data(
        kind="payment",
        amount=4000,
        source_kind="service_sale",
        source="sale",
        day="2026-09-03",
        direction="inflow",
        party="customer",
    )
    bank = save("payment", "second", second)
    save(
        "cash_payment",
        "third",
        payment_data(
            kind="cash_payment",
            amount=3100,
            source_kind="service_sale",
            source="sale",
            day="2026-09-04",
            direction="inflow",
            party="customer",
        ),
    )
    publish("sale", "first", "second", "third")
    assert current(engine, "platform_payment", "first").values["tax_transfers"][0]["vat_fen"] == 100
    assert not current(engine, "payment", "second").values["tax_transfers"]
    assert not current(engine, "cash_payment", "third").values["tax_transfers"]
    save(
        "surtax_policy",
        "surtax",
        {"period": "2026-01", "policy": surtax_policy().model_dump(mode="json")},
    )
    save(
        "tax_assessment",
        "vat-month",
        {
            "period": "2026-09",
            "period_start": "2026-09-01",
            "period_end": "2026-09-30",
            "vat_policy_id": "vat",
            "surtax_policy_id": "surtax",
        },
    )
    publish("vat-month")
    assert current(engine, "tax_assessment", "vat-month").values["payable_vat_fen"] == 100
    with engine.store.connection(read_only=True) as connection:
        source_data = engine.store.current_fact(connection, "movement-first").fact.model_dump(
            mode="json"
        )
    engine.amend_fact(
        "platform_movement",
        "movement-first",
        {**source_data, "actual_date": "2026-09-05"},
        evidence=[evidence],
        expected_revision=1,
        recording_error_confirmed=True,
        request_id="correct-original-recording",
    )
    engine.amend_fact(
        "platform_payment",
        "first",
        {**first, "actual_date": "2026-09-05"},
        evidence=[evidence],
        expected_revision=1,
        recording_error_confirmed=True,
        request_id="correct-recording",
    )
    correction = publish("movement-first")
    assert {item["subject_id"] for item in correction["results"]} == {
        "first",
        "second",
        "third",
        "vat-month",
        "movement-first",
    }
    assert not current(engine, "platform_payment", "first").values["tax_transfers"]
    assert current(engine, "payment", "second").values["tax_transfers"][0]["vat_fen"] == 100
    with engine.store.connection(read_only=True) as connection:
        assert (
            str(engine.store.fact(connection, original["fact_id"]).fact.actual_date) == "2026-09-02"
        )
        assert engine.store.current_fact(connection, "second").id == bank["fact_id"]
        assert engine.store.fact(connection, original["fact_id"]).fact.amount_fen == 3000
    assert balances(engine)["service_sale:sale:primary"] == 0
    assert current(engine, "tax_assessment", "vat-month").values["payable_vat_fen"] == 100


def test_later_sale_return_reads_platform_tax_transfer(book):
    engine, save, publish, _ = book
    save("vat_policy", "vat", {"period": "2026-01", "policy": vat_policy().model_dump(mode="json")})
    save(
        "service_sale",
        "sale",
        {
            "period": "2026-08",
            "customer_id": "customer",
            "gross_fen": 10100,
            "vat_policy_id": "vat",
            "exemption_eligible": False,
            "tax_obligation_period": "2026-09",
        },
    )
    save(
        "platform_payment",
        "received",
        payment_data(
            amount=10100,
            source_kind="service_sale",
            source="sale",
            direction="inflow",
            party="customer",
        ),
    )
    publish("sale", "received")
    save(
        "sale_return",
        "return",
        {
            "period": "2026-10",
            "sale_id": "sale",
            "customer_id": "customer",
            "returned_gross_fen": 1010,
            "credit_note_vat_fen": 10,
        },
    )
    publish("return")
    accounts = {row[0]: row[1:3] for row in lines(engine, "sale_return", "return")}
    assert accounts["222101"] == (10, 0)
    assert "222104" not in accounts


def test_platform_principal_repayment_changes_interest_and_period_readiness(book):
    engine, save, publish, _ = book
    save(
        "loan_agreement",
        "agreement",
        {
            "period": "2026-09",
            "lender_id": "lender",
            "lender_is_licensed": True,
            "currency": "CNY",
            "annual_rate_percent": "3.65",
            "day_count_basis": "actual_365",
            "maturity_date": "2027-09-01",
            "loan_term": "short_term",
        },
    )
    save(
        "loan_drawdown",
        "drawdown",
        {
            "period": "2026-09",
            "agreement_id": "agreement",
            "principal_fen": 1000000,
            "actual_date": "2026-09-01",
            "bank_account_id": "bank-a",
        },
    )
    save("platform_funding", "capital", funding_data(amount=1000000))
    save(
        "platform_payment",
        "repaid",
        payment_data(
            amount=1000000,
            source_kind="loan_drawdown",
            source="drawdown",
            obligation="principal",
            party="lender",
            day="2026-09-16",
        ),
    )
    for subject, start, end in [
        ("interest-before", "2026-09-01", "2026-09-16"),
        ("interest-after", "2026-09-16", "2026-10-01"),
    ]:
        save(
            "loan_interest",
            subject,
            {
                "period": "2026-09",
                "agreement_id": "agreement",
                "drawdown_id": "drawdown",
                "period_start": start,
                "period_end_exclusive": end,
            },
        )
    publish("drawdown", "capital", "repaid", "interest-before", "interest-after")
    assert current(engine, "loan_interest", "interest-before").values["interest_fen"] == 1500
    after = current(engine, "loan_interest", "interest-after").values
    assert after["principal_fen"] == after["interest_fen"] == 0
    assert balances(engine)["loan_drawdown:drawdown:principal"] == 0
    with engine.store.connection(read_only=True) as connection:
        month = YearMonth("2026-10")
        ctx = Context(
            {read: engine.store.select(connection, read) for read in assets.required_reads(month)}
        )
        assert not assets.required_work(month, ctx)
