"""Exact adopted bank partitions distinguish complete individual payments."""

import pytest
from test_banking import book as book

import ai_accounting.kernel.duplicate_freeze as freeze_module
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.duplicates import DuplicateCandidates, Duplicates, SourceLocation
from ai_accounting.kernel.materials import Materials, PeriodAllocationEntry
from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods
from ai_accounting.kernel.types import canonical

PERIOD = "2026-09"
DAY = "2026-09-08"
AMOUNTS = (10100, 10100, 15300)
SUBJECTS = ("payment-a", "payment-b", "payment-c")


def split_bank(
    book,
    *,
    statement_posted=True,
    reconciliation_posted=True,
    original_statement_evidence=True,
    full_partition=True,
    wrong_location=False,
    same_counterparty=False,
    confirmed_allocation=False,
    amount_direction="outflow",
):
    """Use normal typed registration/adoption before the late original is read."""
    engine, save, publish, business_proof = book
    materials = Materials(engine)
    save(
        "bank_opening",
        "opening",
        {
            "period": PERIOD,
            "bank_account_id": "bank-a",
            "opening_fen": 0,
            "basis": "new_account",
        },
    )
    publish("opening")
    payments = {}
    parties = [
        "party-a" if same_counterparty and index < 2 else "party-" + str(index)
        for index in range(3)
    ]
    for index, amount in enumerate(AMOUNTS):
        obligation = "expense-" + str(index)
        save(
            "expense",
            obligation,
            {
                "period": PERIOD,
                "counterparty_id": parties[index],
                "amount_fen": amount,
                "expense_class": "administration",
                "creditor_kind": "supplier",
            },
        )
    publish("expense-0", "expense-1", "expense-2")
    for index, (subject, amount) in enumerate(zip(SUBJECTS, AMOUNTS, strict=True)):
        data = {
            "period": PERIOD,
            "actual_date": DAY,
            "direction": "outflow",
            "bank_account_id": "bank-a",
            "counterparty_id": parties[index],
            "amount_fen": amount,
            "allocations": [
                {
                    "source_kind": "expense",
                    "source_id": "expense-" + str(index),
                    "obligation": "primary",
                    "amount_fen": amount,
                }
            ],
        }
        saved = save("payment", subject, data)
        payments[subject] = saved | {"data": data}
    for adopted in publish(*SUBJECTS)["results"]:
        payments[adopted["subject_id"]].update(adopted)
    raw = b"date,reference,expense,period\n2026-09-08,whole,355.00,2026-09\n"
    if wrong_location:
        raw += b"2026-09-08,other,202.00,2026-09\n"
    proof = engine.register_evidence(
        raw,
        "text/csv",
        "synthetic-bank.csv",
        request_id="bank-evidence",
    )["digest"]
    source = materials.receive(
        "bank-source",
        {
            "period": PERIOD,
            "category": "bank",
            "purpose": "business",
            "evidence_digest": proof,
            "specification": {
                "format": "csv",
                "header_rows": {"CSV": 1},
                "columns": [
                    {"column": "A", "role": "context"},
                    {"column": "B", "role": "context"},
                    {"column": "C", "role": "amount", "funds_direction": amount_direction},
                    {
                        "column": "D",
                        "role": "context" if confirmed_allocation else "recognition_period",
                    },
                ],
            },
        },
        evidence=(proof,),
        expected_revision=0,
        request_id="receive-bank",
    )
    if confirmed_allocation:
        allocate_bank_row(engine, materials, PERIOD)
    statement_data = {
        "period": PERIOD,
        "bank_account_id": "bank-a",
        "opening_fen": 0,
        "closing_fen": -sum(AMOUNTS),
        "entries": [{"reference": "whole", "actual_date": DAY, "signed_fen": -sum(AMOUNTS)}],
    }
    engine.save_fact(
        "bank_statement",
        "statement",
        statement_data,
        evidence=(proof if original_statement_evidence else business_proof,),
        expected_revision=0,
        request_id="statement",
    )
    if statement_posted:
        publish("statement")
    engine.save_fact(
        "bank_reconciliation",
        "reconciliation",
        {
            "period": PERIOD,
            "statement_id": "statement",
            "bank_account_id": "bank-a",
            "matches": [
                {"reference": "whole", "source_kind": "payment", "source_id": subject}
                for subject in SUBJECTS
            ],
        },
        evidence=(proof,),
        expected_revision=0,
        request_id="reconciliation",
    )
    if reconciliation_posted:
        publish("reconciliation")
    members = SUBJECTS if full_partition and not wrong_location else SUBJECTS[:2]
    resolution_data = {
        "period": PERIOD,
        "source_id": source["subject_id"],
        "source_fact_id": source["fact_id"],
        "location": "CSV!C3" if wrong_location else "CSV!C2",
        "treatment": "recognize",
        "amount_fen": sum(AMOUNTS[:2]) if wrong_location else sum(AMOUNTS),
        "recognition_period": PERIOD,
        "links": [
            {
                "calculation_id": payments[subject]["calculation_id"],
                "amount_field": "fact.amount_fen",
                "amount_fen": payments[subject]["data"]["amount_fen"],
            }
            for subject in members
        ],
    }
    resolution = materials.resolve(
        "bank-row",
        resolution_data,
        evidence=(proof,),
        expected_revision=0,
        request_id="resolve-bank-row",
    )
    return {
        "engine": engine,
        "materials": materials,
        "proof": proof,
        "business_proof": business_proof,
        "source": source,
        "resolution": resolution,
        "resolution_data": resolution_data,
        "payments": payments,
        "statement_data": statement_data,
        "publish": publish,
    }


def allocate_bank_row(engine, materials, period):
    passage = "The bank original amount position belongs to " + period + "."
    proof = engine.register_evidence(
        passage.encode(),
        "text/plain",
        "synthetic-period-confirmation.txt",
        request_id="period-confirmation-" + period,
    )["digest"]
    entries = (
        PeriodAllocationEntry(
            sheet="CSV",
            column="C",
            first_row=2,
            last_row=2,
            recognition_period=period,
            basis="confirmed_period",
            basis_evidence_digest=proof,
            basis_location="confirmation paragraph one",
            basis_excerpt=passage,
        ),
    )
    preview = materials.preview_period_allocation("bank-source", entries)
    assert preview["status"] == "ready", preview
    return materials.confirm_period_allocation(
        "bank-source",
        entries,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        expected_revision=preview["expected_revision"],
        request_id="allocate-bank-" + period,
    )


def close_split_month(state):
    periods = Periods(state["engine"])
    periods.inventory_batch(
        [
            {
                "period": PERIOD,
                "category": category,
                "evidence": (
                    [state["proof"]]
                    if category == "bank"
                    else [state["business_proof"]] if category == "transactions" else []
                ),
                "expected": int(category in {"transactions", "bank"}),
                "no_business": category not in {"transactions", "bank"},
                "confirmation_evidence": state["business_proof"],
            }
            for category in MATERIAL_CATEGORIES
        ],
        request_id="split-inventories",
    )
    preview = periods.preview_close(PERIOD, owner_confirmation=state["business_proof"])
    periods.close(
        PERIOD,
        owner_confirmation=state["business_proof"],
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="close-split-month",
    )
    return periods, periods.closed_report(PERIOD)


def assert_frozen_pair_reenters_current_scan(state, monkeypatch):
    engine = state["engine"]
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        assert connection.execute("SELECT count(*) FROM duplicate_freeze_root").fetchone()[0] == 1
        duplicates = DuplicateCandidates(engine.store)
        fast = duplicates.close_readiness(connection, "2026-10")
        with monkeypatch.context() as patch:
            patch.setattr(freeze_module, "narrowed_duplicate_candidates", lambda *_a, **_k: None)
            full = duplicates.close_readiness(connection, "2026-10")
    assert canonical(fast) == canonical(full)
    assert any(
        {item["subject_id"], item["candidate_subject_id"]} == set(SUBJECTS[:2])
        and item["code"] == "duplicate_review_required"
        and item["review_period"] == "2026-10"
        for item in fast
    ), fast


def unresolved(state, *, closing=False):
    engine = state["engine"]
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        duplicates = DuplicateCandidates(engine.store)
        return (
            duplicates.close_readiness(connection, PERIOD)
            if closing
            else duplicates.unresolved(connection)
        )


def assert_equal_pair_is_strong(state):
    issues = unresolved(state)
    assert any(
        {item["subject_id"], item["candidate_subject_id"]} == set(SUBJECTS[:2])
        and item["code"] == "duplicate_review_required"
        for item in issues
    ), issues


@pytest.mark.parametrize("closing", [False, True], ids=("unresolved", "close-readiness"))
def test_three_complete_payments_share_one_adopted_original_without_duplicate_review(book, closing):
    state = split_bank(book)
    assert state["materials"].check(PERIOD)["status"] == "complete"
    assert unresolved(state, closing=closing) == []


@pytest.mark.parametrize(
    "options",
    [
        {"reconciliation_posted": False},
        {"statement_posted": False, "reconciliation_posted": False},
        {"original_statement_evidence": False},
        {"full_partition": False},
        {"wrong_location": True},
        {"same_counterparty": True},
        {"amount_direction": None},
        {"amount_direction": "inflow"},
    ],
    ids=(
        "saved-reconciliation",
        "saved-statement",
        "missing-original-proof",
        "incomplete-partition",
        "another-original-row",
        "same-counterparty",
        "unknown-original-direction",
        "conflicting-original-direction",
    ),
)
def test_bank_or_material_proof_gaps_keep_the_equal_payment_pair_strong(book, options):
    assert_equal_pair_is_strong(split_bank(book, **options))


def test_stale_adopted_statement_does_not_prove_distinct_payments(book):
    state = split_bank(book)
    data = state["statement_data"] | {
        "entries": [
            state["statement_data"]["entries"][0] | {"description": "corrected original extraction"}
        ]
    }
    state["engine"].save_fact(
        "bank_statement",
        "statement",
        data,
        evidence=(state["proof"],),
        expected_revision=1,
        request_id="new-statement-version",
    )
    assert_equal_pair_is_strong(state)


def test_new_same_object_and_obligation_proposal_cannot_borrow_existing_partition(book):
    state = split_bank(book)
    location = SourceLocation(
        source_id="bank-source",
        source_fact_id=state["source"]["fact_id"],
        evidence_digest=state["proof"],
        location="CSV!C2",
    )
    data = state["payments"]["payment-a"]["data"]
    prepared = Duplicates(state["engine"]).prepare_fact_registration(
        "payment",
        "repeated-payment",
        data,
        evidence=(state["business_proof"],),
        expected_revision=0,
        source_locations=(location,),
    )
    assert prepared["strong_candidates"]
    with pytest.raises(KernelError) as error:
        state["engine"].save_fact(
            "payment",
            "repeated-payment",
            data,
            evidence=(state["business_proof"],),
            expected_revision=0,
            source_locations=(location,),
            request_id="repeated-payment",
        )
    assert error.value.code == "duplicate_review_required"


def test_whole_original_copy_does_not_consume_or_replace_the_current_partition(book):
    state = split_bank(book)
    engine, materials = state["engine"], state["materials"]
    proof = engine.register_evidence(
        b"name,amount,period\ncopy,355.00,2026-09\n",
        "text/csv",
        "synthetic-copy.csv",
        request_id="copy-evidence",
    )["digest"]
    source = materials.receive(
        "copy-source",
        {
            "period": PERIOD,
            "category": "bank",
            "purpose": "business",
            "evidence_digest": proof,
            "specification": {
                "format": "csv",
                "columns": [
                    {"column": "A", "role": "context"},
                    {"column": "B", "role": "amount", "funds_direction": "outflow"},
                    {"column": "C", "role": "recognition_period"},
                ],
            },
        },
        evidence=(proof,),
        expected_revision=0,
        request_id="receive-copy",
    )
    data = {
        "period": PERIOD,
        "source_id": source["subject_id"],
        "source_fact_id": source["fact_id"],
        "location": "CSV!B2",
        "treatment": "duplicate",
        "amount_fen": sum(AMOUNTS),
        "recognition_period": PERIOD,
        "links": [],
        "duplicate_source_id": "bank-source",
        "duplicate_location": "CSV!C2",
        "reason": "This exact original restates the whole adopted row.",
    }
    materials.resolve(
        "copy-row", data, evidence=(proof,), expected_revision=0, request_id="resolve-copy"
    )
    assert materials.check(PERIOD)["status"] == "complete"
    assert unresolved(state) == []
    duplicate_recognition = data | {
        "treatment": "recognize",
        "links": state["resolution_data"]["links"],
        "duplicate_source_id": None,
        "duplicate_location": None,
    }
    materials.resolve(
        "copy-row",
        duplicate_recognition,
        evidence=(proof,),
        expected_revision=1,
        request_id="copy-cannot-recognize-twice",
    )
    checked = materials.check(PERIOD)
    assert checked["status"] == "needs_information"
    assert "material_business_overallocated" in {item["code"] for item in checked["issues"]}
    assert_equal_pair_is_strong(state)


@pytest.mark.parametrize("changed_kind", ["bank_statement", "bank_reconciliation"])
def test_frozen_split_requires_current_bank_adoption_after_a_real_revision(
    book, monkeypatch, changed_kind
):
    state = split_bank(book)
    periods, frozen = close_split_month(state)
    subject = "statement" if changed_kind == "bank_statement" else "reconciliation"
    with state["engine"].store.connection(read_only=True) as connection:
        current = state["engine"].store.current_fact(connection, subject)
        data = current.fact.model_dump(mode="json")
    evidence = (state["proof"], state["business_proof"])
    if changed_kind == "bank_statement":
        data["entries"][0]["description"] = "Checked original reference description"
    state["engine"].save_fact(
        changed_kind,
        subject,
        data,
        evidence=evidence,
        expected_revision=1,
        request_id="frozen-bank-revision-" + changed_kind,
    )
    assert_frozen_pair_reenters_current_scan(state, monkeypatch)
    assert periods.closed_report(PERIOD) == frozen


def test_frozen_split_is_rechecked_after_typed_original_period_reassignment(book, monkeypatch):
    state = split_bank(book, confirmed_allocation=True)
    assert unresolved(state) == []
    periods, frozen = close_split_month(state)
    allocate_bank_row(state["engine"], state["materials"], "2026-10")
    assert_frozen_pair_reenters_current_scan(state, monkeypatch)
    assert periods.closed_report(PERIOD) == frozen
