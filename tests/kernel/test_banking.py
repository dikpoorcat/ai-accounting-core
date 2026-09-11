"""Bank reconciliation against actual published business facts in real SQLite."""

import itertools
import sqlite3

import pytest
from material_fixture import supporting_text

from ai_accounting.kernel.contracts import KernelError, NeedsInformation
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods
from ai_accounting.kernel.service import default_registry
from ai_accounting.kernel.storage import Store


@pytest.fixture
def book(tmp_path):
    engine = Engine(
        Store.create(
            tmp_path / "company.sqlite",
            default_registry(),
            "company",
            "911100000000000001",
            "database",
        )
    )
    proof = engine.register_evidence(
        b"Synthetic bank test evidence", "text/plain", "fixture", request_id="evidence"
    )["digest"]
    supporting_text(engine, proof)
    counter = itertools.count()

    def save(kind, subject, data, revision=0):
        return engine.save_fact(
            kind,
            subject,
            data,
            evidence=(proof,),
            expected_revision=revision,
            request_id="save-" + str(next(counter)),
        )

    def publish(*subjects):
        preview = engine.preview(list(subjects))
        return engine.confirm(
            list(subjects),
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id="publish-" + str(next(counter)),
        )

    return engine, save, publish, proof


def opening(save, publish, bank="bank-a", month="2026-09", amount=0, basis="new_account"):
    subject = "opening-" + bank
    save(
        "bank_opening",
        subject,
        {"period": month, "bank_account_id": bank, "opening_fen": amount, "basis": basis},
    )
    publish(subject)


def funding(
    save, publish, *, subject="funding", amount=1000, bank="bank-a", day="2026-09-01", posted=True
):
    save(
        "funding",
        subject,
        {
            "period": day[:7],
            "owner_id": "owner",
            "amount_fen": amount,
            "funding_kind": "capital",
            "actual_date": day,
            "bank_account_id": bank,
        },
    )
    if posted:
        publish(subject)


def statement(
    save,
    publish,
    entries,
    *,
    subject="statement",
    bank="bank-a",
    month="2026-09",
    initial=0,
    revision=0,
):
    data = {
        "period": month,
        "bank_account_id": bank,
        "opening_fen": initial,
        "closing_fen": initial + sum(entry["signed_fen"] for entry in entries),
        "entries": entries,
    }
    saved = save("bank_statement", subject, data, revision)
    publish(subject)
    return saved, data


def entry(reference="receipt", day="2026-09-01", amount=1000):
    return {"reference": reference, "actual_date": day, "signed_fen": amount}


def reconciliation(
    save,
    publish,
    matches,
    *,
    subject="reconciliation",
    statement_id="statement",
    bank="bank-a",
    month="2026-09",
    posted=True,
):
    save(
        "bank_reconciliation",
        subject,
        {
            "period": month,
            "statement_id": statement_id,
            "bank_account_id": bank,
            "matches": matches,
        },
    )
    if posted:
        return publish(subject)


def match(reference="receipt", kind="funding", source="funding"):
    return {"reference": reference, "source_kind": kind, "source_id": source}


def test_statement_rows_are_typed_strict_children_and_preserve_old_revision(book):
    engine, save, publish, _ = book
    opening(save, publish)
    funding(save, publish)
    first, data = statement(save, publish, [entry()])
    reconciliation(save, publish, [match()])
    updated = data | {"entries": [entry() | {"description": "corrected extraction description"}]}
    changed = save("bank_statement", "statement", updated, 1)
    assert set(changed["pending"]) == {"statement", "reconciliation"}
    publish("statement")
    with engine.store.connection(read_only=True) as connection:
        columns = {
            row["name"]: row["type"]
            for row in connection.execute("PRAGMA table_info(fact_bank_statement_entries)")
        }
        assert columns["signed_fen"] == "INTEGER"
        assert columns["actual_date"] == "TEXT"
        assert (
            next(
                row
                for row in connection.execute("PRAGMA table_list")
                if row["name"] == "fact_bank_statement_entries"
            )["strict"]
            == 1
        )
        rows = connection.execute(
            "SELECT revision_id,signed_fen,description FROM fact_bank_statement_entries "
            "ORDER BY description"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["revision_id"] == first["fact_id"]
        assert rows[0]["description"] is None
        assert rows[1]["description"] == "corrected extraction description"
    with engine.store.connection() as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE fact_bank_statement_entries SET signed_fen=1")


@pytest.mark.parametrize(
    "bank,day,amount,code",
    [
        ("bank-b", "2026-09-01", 1000, "bank_account_mismatch"),
        ("bank-a", "2026-09-02", 1000, "bank_match_difference"),
        ("bank-a", "2026-09-01", -1000, "bank_match_difference"),
        ("bank-a", "2026-09-01", 999, "bank_match_difference"),
    ],
)
def test_matching_checks_actual_bank_date_direction_and_amount(book, bank, day, amount, code):
    _, save, publish, _ = book
    opening(save, publish, bank=bank)
    funding(save, publish)
    statement(save, publish, [entry(day=day, amount=amount)], bank=bank)
    with pytest.raises(KernelError) as failure:
        reconciliation(save, publish, [match()], bank=bank)
    assert failure.value.code == code


def test_unmatched_transfer_cannot_disappear_from_expected_funds(book):
    _, save, publish, _ = book
    opening(save, publish)
    funding(save, publish)
    save(
        "funds_transfer",
        "transfer",
        {
            "period": "2026-09",
            "actual_date": "2026-09-02",
            "source_bank_account_id": "bank-a",
            "destination_bank_account_id": "bank-b",
            "amount_fen": 200,
        },
    )
    publish("transfer")
    statement(save, publish, [entry()])
    with pytest.raises(NeedsInformation) as failure:
        reconciliation(save, publish, [match()])
    assert failure.value.issues[0]["field"] == "matches"


def test_bank_income_and_both_transfer_sides_match_once_each(book):
    engine, save, publish, _ = book
    opening(save, publish)
    opening(save, publish, "bank-b")
    funding(save, publish)
    save(
        "bank_income",
        "interest",
        {
            "period": "2026-09",
            "actual_date": "2026-09-02",
            "bank_account_id": "bank-a",
            "amount_fen": 50,
            "income_kind": "bank_interest",
            "counterparty_id": "bank",
            "entitlement_confirmed": True,
        },
    )
    save(
        "funds_transfer",
        "transfer",
        {
            "period": "2026-09",
            "actual_date": "2026-09-03",
            "source_bank_account_id": "bank-a",
            "destination_bank_account_id": "bank-b",
            "amount_fen": 200,
        },
    )
    publish("interest", "transfer")
    statement(
        save,
        publish,
        [entry(), entry("interest", "2026-09-02", 50), entry("transfer-out", "2026-09-03", -200)],
    )
    reconciliation(
        save,
        publish,
        [
            match(),
            match("interest", "bank_income", "interest"),
            match("transfer-out", "funds_transfer", "transfer"),
        ],
    )
    statement(
        save,
        publish,
        [entry("transfer-in", "2026-09-03", 200)],
        subject="statement-b",
        bank="bank-b",
    )
    reconciliation(
        save,
        publish,
        [match("transfer-in", "funds_transfer", "transfer")],
        subject="reconciliation-b",
        statement_id="statement-b",
        bank="bank-b",
    )
    with engine.store.connection(read_only=True) as connection:
        assert dict(
            connection.execute("SELECT balance_key,amount FROM balance WHERE category='bank'")
        ) == {"bank-a": 850, "bank-b": 200}


def test_fact_only_funds_cannot_be_presented_as_formally_reconciled(book):
    _, save, publish, _ = book
    opening(save, publish)
    funding(save, publish, posted=False)
    statement(save, publish, [entry()])
    with pytest.raises(NeedsInformation) as failure:
        reconciliation(save, publish, [match()])
    assert failure.value.issues[0]["field"] == "actual_funds"


def test_source_amount_cannot_be_counted_in_full_for_two_bank_rows(book):
    _, save, publish, _ = book
    opening(save, publish)
    funding(save, publish)
    statement(save, publish, [entry("first"), entry("second")])
    with pytest.raises(KernelError) as failure:
        reconciliation(save, publish, [match("first"), match("second")])
    assert failure.value.code == "bank_match_difference"


def test_empty_database_never_supplies_a_bank_opening_by_default(book):
    _, save, publish, _ = book
    statement(save, publish, [])
    with pytest.raises(NeedsInformation) as failure:
        reconciliation(save, publish, [])
    assert failure.value.issues[0]["field"] == "bank_opening"


def test_opening_must_agree_with_explicit_prior_published_sources(book):
    _, save, publish, _ = book
    funding(save, publish, day="2026-08-10")
    opening(save, publish, amount=1000, basis="existing_ledger")
    statement(save, publish, [], initial=900)
    with pytest.raises(KernelError) as failure:
        reconciliation(save, publish, [])
    assert failure.value.code == "bank_opening_difference"


def test_monthly_bank_reconciliation_is_continuous(book):
    engine, save, publish, _ = book
    opening(save, publish)
    funding(save, publish)
    statement(save, publish, [entry()])
    reconciliation(save, publish, [match()])
    statement(save, publish, [], subject="october-statement", month="2026-10", initial=1000)
    reconciliation(
        save,
        publish,
        [],
        subject="october-reconciliation",
        statement_id="october-statement",
        month="2026-10",
    )
    statement(save, publish, [], subject="december-statement", month="2026-12", initial=1000)
    with pytest.raises(NeedsInformation) as failure:
        reconciliation(
            save,
            publish,
            [],
            subject="december-reconciliation",
            statement_id="december-statement",
            month="2026-12",
        )
    assert failure.value.issues[0]["field"] == "previous_reconciliation"
    assert len(engine.ledger("2026-09")) == 1


def test_one_bank_batch_matches_multiple_actual_recipients(book):
    _, save, publish, _ = book
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
            "bank_account_id": "bank-a",
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
    publish("batch")
    statement(save, publish, [entry(), entry("batch", "2026-09-10", -300)])
    result = reconciliation(save, publish, [match(), match("batch", "payment", "batch")])
    assert result["status"] == "published"


def test_bank_batch_requires_actual_recipient_for_every_allocation(book):
    _, save, publish, _ = book
    save(
        "expense",
        "alice",
        {
            "period": "2026-09",
            "counterparty_id": "alice",
            "amount_fen": 100,
            "expense_class": "administration",
            "creditor_kind": "employee",
        },
    )
    publish("alice")
    save(
        "payment",
        "batch",
        {
            "period": "2026-09",
            "actual_date": "2026-09-10",
            "bank_account_id": "bank-a",
            "counterparty_id": "bank-batch",
            "payment_method": "bank_batch",
            "direction": "outflow",
            "amount_fen": 100,
            "allocations": [
                {
                    "source_kind": "expense",
                    "source_id": "alice",
                    "obligation": "primary",
                    "amount_fen": 100,
                }
            ],
        },
    )
    with pytest.raises(NeedsInformation) as failure:
        publish("batch")
    assert failure.value.issues[0]["field"] == "allocations.recipient_id"


def test_actual_bank_batch_allocation_can_be_completed_without_rewriting_cash(book):
    engine, save, publish, _ = book
    save(
        "expense",
        "alice",
        {
            "period": "2026-09",
            "counterparty_id": "alice",
            "amount_fen": 100,
            "expense_class": "administration",
            "creditor_kind": "employee",
        },
    )
    publish("alice")
    original = {
        "period": "2026-09",
        "actual_date": "2026-09-10",
        "bank_account_id": "bank-a",
        "counterparty_id": "bank-batch",
        "payment_method": "bank_batch",
        "direction": "outflow",
        "amount_fen": 100,
        "allocations": [
            {
                "source_kind": "expense",
                "source_id": "alice",
                "obligation": "primary",
                "amount_fen": 100,
            }
        ],
    }
    first = save("payment", "batch", original)
    with pytest.raises(NeedsInformation):
        publish("batch")
    complete = original | {"allocations": [original["allocations"][0] | {"recipient_id": "alice"}]}
    second = save("payment", "batch", complete, 1)
    publish("batch")
    assert second["revision"] == 2
    assert first["fact_id"] != second["fact_id"]
    for changed in (
        {"actual_date": "2026-09-11"},
        {"bank_account_id": "bank-b"},
        {"direction": "inflow"},
        {"counterparty_id": "other-bank-batch"},
        {"amount_fen": 101, "allocations": [complete["allocations"][0] | {"amount_fen": 101}]},
    ):
        with pytest.raises(KernelError) as failure:
            save("payment", "batch", complete | changed, 2)
        assert failure.value.code == "immutable_fact"
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM fact_payment").fetchone()[0] == 2
        assert (
            connection.execute(
                "SELECT amount FROM balance WHERE category='bank' AND balance_key='bank-a'"
            ).fetchone()[0]
            == -100
        )


def test_unrelated_bank_funds_do_not_invalidate_a_completed_reconciliation(book):
    engine, save, publish, _ = book
    opening(save, publish)
    statement(save, publish, [])
    reconciliation(save, publish, [])
    funding(save, publish, bank="unrelated-bank")
    assert not engine.overview("2026-09")["pending"]


def test_interest_and_principal_in_one_real_payment_publish_without_dependency_cycle(book):
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
    publish("drawdown")
    save(
        "loan_interest",
        "interest",
        {
            "period": "2026-09",
            "agreement_id": "agreement",
            "drawdown_id": "drawdown",
            "period_start": "2026-09-01",
            "period_end_exclusive": "2026-09-16",
        },
    )
    save(
        "payment",
        "repayment",
        {
            "period": "2026-09",
            "actual_date": "2026-09-16",
            "bank_account_id": "bank-a",
            "counterparty_id": "lender",
            "direction": "outflow",
            "amount_fen": 101500,
            "allocations": [
                {
                    "source_kind": "loan_drawdown",
                    "source_id": "drawdown",
                    "obligation": "principal",
                    "amount_fen": 100000,
                },
                {
                    "source_kind": "loan_interest",
                    "source_id": "interest",
                    "obligation": "interest",
                    "amount_fen": 1500,
                },
            ],
        },
    )
    assert publish("interest", "repayment")["status"] == "published"
    opening(save, publish)
    statement(
        save,
        publish,
        [entry("drawdown", amount=1000000), entry("repayment", "2026-09-16", -101500)],
    )
    reconciliation(
        save,
        publish,
        [
            match("drawdown", "loan_drawdown", "drawdown"),
            match("repayment", "payment", "repayment"),
        ],
    )
    assert not engine.overview("2026-09")["pending"]


def test_close_requires_reconciliation_and_freezes_statement_versions(book):
    engine, save, publish, proof = book
    periods = Periods(engine)
    opening(save, publish)
    funding(save, publish)
    _, statement_data = statement(save, publish, [entry()])
    for category in MATERIAL_CATEGORIES:
        active = category in {"transactions", "bank"}
        periods.inventory(
            "2026-09",
            category,
            evidence=[proof] if active else [],
            expected=int(active),
            no_business=not active,
            confirmation_evidence=proof,
            request_id="inventory-" + category,
        )
    with pytest.raises(KernelError) as failure:
        periods.preview_close("2026-09", owner_confirmation=proof)
    assert any(
        issue["field"] == "bank_reconciliation" for issue in failure.value.details["fact_issues"]
    )
    reconciliation(save, publish, [match()])
    preview = periods.preview_close("2026-09", owner_confirmation=proof)
    periods.close(
        "2026-09",
        owner_confirmation=proof,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="close",
    )
    frozen = periods.closed_report("2026-09")
    save(
        "bank_statement",
        "statement",
        statement_data | {"entries": [entry() | {"description": "later extraction"}]},
        1,
    )
    assert periods.closed_report("2026-09") == frozen


@pytest.mark.parametrize(
    "entries,closing",
    [
        ([entry(day="2026-10-01")], 1000),
        ([entry(), entry()], 2000),
        ([entry(amount=0)], 0),
        ([entry(amount=1000.0)], 1000),
    ],
)
def test_statement_rejects_cross_month_duplicate_zero_and_float_rows(book, entries, closing):
    _, save, _, _ = book
    with pytest.raises(KernelError):
        save(
            "bank_statement",
            "invalid",
            {
                "period": "2026-09",
                "bank_account_id": "bank-a",
                "opening_fen": 0,
                "closing_fen": closing,
                "entries": entries,
            },
        )


def inventories(engine, proof, month, active):
    periods = Periods(engine)
    for category in MATERIAL_CATEGORIES:
        periods.inventory(
            month,
            category,
            evidence=[proof] if category in active else [],
            expected=int(category in active),
            no_business=category not in active,
            confirmation_evidence=proof,
            request_id=f"inventory-{month}-{category}-{len(active)}",
        )
    return periods


def close_month(periods, proof, month):
    preview = periods.preview_close(month, owner_confirmation=proof)
    return periods.close(
        month,
        owner_confirmation=proof,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="close-" + month,
    )


@pytest.mark.parametrize("target,due", [("2026-02", "2026-02"), ("2026-04", "2026-03")])
def test_asset_readiness_detects_missing_months_even_without_new_asset_facts(book, target, due):
    engine, save, publish, proof = book
    save(
        "asset",
        "asset",
        {
            "period": "2026-01",
            "asset_type": "fixed",
            "supplier_id": "supplier",
            "acquisition_date": "2026-01-10",
            "cost_fen": 1000,
            "acquisition_basis": "direct_purchase",
        },
    )
    save(
        "asset_activation",
        "activation",
        {
            "period": "2026-01",
            "asset_id": "asset",
            "in_use_date": "2026-01-10",
            "useful_life_months": 2,
            "residual_fen": 0,
            "benefit_area": "administration",
            "rounding_policy": "floor_final_remainder",
        },
    )
    publish("asset", "activation")
    close_month(inventories(engine, proof, "2026-01", {"assets"}), proof, "2026-01")
    periods = inventories(engine, proof, target, {"assets"})
    with pytest.raises(KernelError) as failure:
        periods.preview_close(target, owner_confirmation=proof)
    issue = next(
        item
        for item in failure.value.details["fact_issues"]
        if item["field"] == "asset_consumption"
    )
    assert issue["asset_id"] == "asset"
    assert issue["period"] == due


def test_fully_depreciated_asset_does_not_permanently_block_later_months(book):
    engine, save, publish, proof = book
    save(
        "asset",
        "asset",
        {
            "period": "2026-01",
            "asset_type": "fixed",
            "supplier_id": "supplier",
            "acquisition_date": "2026-01-10",
            "cost_fen": 1000,
            "acquisition_basis": "direct_purchase",
        },
    )
    save(
        "asset_activation",
        "activation",
        {
            "period": "2026-01",
            "asset_id": "asset",
            "in_use_date": "2026-01-10",
            "useful_life_months": 1,
            "residual_fen": 0,
            "benefit_area": "administration",
            "rounding_policy": "floor_final_remainder",
        },
    )
    publish("asset", "activation")
    close_month(inventories(engine, proof, "2026-01", {"assets"}), proof, "2026-01")
    save("asset_consumption", "february", {"period": "2026-02", "asset_id": "asset"})
    publish("february")
    close_month(inventories(engine, proof, "2026-02", {"assets"}), proof, "2026-02")
    assert (
        close_month(inventories(engine, proof, "2026-03", set()), proof, "2026-03")["status"]
        == "closed"
    )


def test_idle_bank_account_still_needs_next_month_statement_and_reconciliation(book):
    engine, save, publish, proof = book
    opening(save, publish)
    statement(save, publish, [])
    reconciliation(save, publish, [])
    close_month(inventories(engine, proof, "2026-09", {"bank"}), proof, "2026-09")
    periods = inventories(engine, proof, "2026-10", set())
    with pytest.raises(KernelError) as failure:
        periods.preview_close("2026-10", owner_confirmation=proof)
    assert {issue["field"] for issue in failure.value.details["fact_issues"]} >= {
        "bank_statement",
        "bank_reconciliation",
    }
    statement(save, publish, [], subject="october-statement", month="2026-10")
    reconciliation(
        save,
        publish,
        [],
        subject="october-reconciliation",
        statement_id="october-statement",
        month="2026-10",
    )
    assert (
        close_month(inventories(engine, proof, "2026-10", {"bank"}), proof, "2026-10")["status"]
        == "closed"
    )


@pytest.mark.parametrize("fully_repaid", [False, True, "cash", "owner"])
def test_loan_readiness_requires_zero_interest_but_releases_fully_settled_principal(
    book, fully_repaid
):
    engine, save, publish, proof = book
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
            "principal_fen": 1,
            "actual_date": "2026-09-01",
            "bank_account_id": "bank-a",
        },
    )
    publish("drawdown")
    opening(save, publish)
    statement(save, publish, [entry("drawdown", amount=1)])
    reconciliation(save, publish, [match("drawdown", "loan_drawdown", "drawdown")])
    periods = inventories(engine, proof, "2026-09", {"bank", "financing"})
    with pytest.raises(KernelError) as failure:
        periods.preview_close("2026-09", owner_confirmation=proof)
    assert any(issue["field"] == "loan_interest" for issue in failure.value.details["fact_issues"])
    save(
        "loan_interest",
        "september-interest",
        {
            "period": "2026-09",
            "agreement_id": "agreement",
            "drawdown_id": "drawdown",
            "period_start": "2026-09-01",
            "period_end_exclusive": "2026-10-01",
        },
    )
    publish("september-interest")
    close_month(periods, proof, "2026-09")
    if fully_repaid is True:
        save(
            "payment",
            "repayment",
            {
                "period": "2026-10",
                "actual_date": "2026-10-01",
                "direction": "outflow",
                "bank_account_id": "bank-a",
                "counterparty_id": "lender",
                "amount_fen": 1,
                "allocations": [
                    {
                        "source_kind": "loan_drawdown",
                        "source_id": "drawdown",
                        "obligation": "principal",
                        "amount_fen": 1,
                    }
                ],
            },
        )
        publish("repayment")
    elif fully_repaid == "cash":
        save(
            "cash_funding",
            "cash-funding",
            {
                "period": "2026-10",
                "actual_date": "2026-10-01",
                "owner_id": "owner",
                "funding_kind": "capital",
                "cash_account_id": "cashbox",
                "amount_fen": 1,
            },
        )
        save(
            "cash_payment",
            "cash-repayment",
            {
                "period": "2026-10",
                "actual_date": "2026-10-01",
                "direction": "outflow",
                "cash_account_id": "cashbox",
                "counterparty_id": "lender",
                "amount_fen": 1,
                "allocations": [
                    {
                        "source_kind": "loan_drawdown",
                        "source_id": "drawdown",
                        "obligation": "principal",
                        "amount_fen": 1,
                    }
                ],
            },
        )
        publish("cash-funding", "cash-repayment")
    elif fully_repaid == "owner":
        save(
            "employee_advance",
            "owner-repayment",
            {
                "period": "2026-10",
                "payer_id": "owner",
                "payer_kind": "owner",
                "payment_on_behalf_confirmed": True,
                "actual_creditor_payment_date": "2026-10-01",
                "sources": [
                    {
                        "source_kind": "loan_drawdown",
                        "source_id": "drawdown",
                        "obligation": "principal",
                        "amount_fen": 1,
                    }
                ],
            },
        )
        publish("owner-repayment")
    assert not engine.overview("2026-09")["pending"]
    statement(
        save,
        publish,
        [entry("repayment", "2026-10-01", -1)] if fully_repaid is True else [],
        subject="october-statement",
        month="2026-10",
        initial=1,
    )
    reconciliation(
        save,
        publish,
        [match("repayment", "payment", "repayment")] if fully_repaid is True else [],
        subject="october-reconciliation",
        statement_id="october-statement",
        month="2026-10",
    )
    active = {"bank", "financing"} | (
        {"transactions"} if fully_repaid in ("cash", "owner") else set()
    )
    periods = inventories(engine, proof, "2026-10", active)
    if fully_repaid:
        assert close_month(periods, proof, "2026-10")["status"] == "closed"
    else:
        with pytest.raises(KernelError) as failure:
            periods.preview_close("2026-10", owner_confirmation=proof)
        issue = next(
            item
            for item in failure.value.details["fact_issues"]
            if item["field"] == "loan_interest"
        )
        assert issue["period_start"] == "2026-10-01"
