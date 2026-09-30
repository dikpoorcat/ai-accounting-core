"""Changes used to narrow closed proofs must survive commit and fail atomically."""

import sqlite3

import pytest
from test_integrity_content import damage
from test_materials import Company

from ai_accounting.kernel.change_journal import (
    SourceChange,
    changes_since,
    first_created_subjects,
    head,
    heads_at,
    verify_journal,
)
from ai_accounting.kernel.contracts import KernelError


def test_current_heads_and_exact_historical_heads_follow_real_publication(tmp_path):
    company = Company(tmp_path)
    first = company.expense("expense", 1000)
    with company.engine.store.connection(read_only=True) as connection:
        checkpoint = head(connection)
        first_heads = heads_at(connection, checkpoint)
        assert first_heads["fact"]["expense"] == first["fact_id"]
        assert first_heads["calculation"]["expense"] == first["calculation_id"]
        assert verify_journal(connection) == checkpoint
    second = company.expense("expense", 1200, revision=1)
    with company.engine.store.connection(read_only=True) as connection:
        changes = changes_since(connection, checkpoint)
        fact = [row for row in changes if row.source == "fact"]
        assert len(fact) == 1
        assert (fact[0].target_id, fact[0].before_ref, fact[0].after_ref) == (
            "expense",
            first["fact_id"],
            second["fact_id"],
        )
        assert {row.source for row in changes} == {
            "fact",
            "calculation",
            "pending",
            "duplicate",
            "publication",
        }
        assert heads_at(connection, checkpoint) == first_heads
        assert heads_at(connection, head(connection))["fact"]["expense"] == second["fact_id"]
        assert verify_journal(connection) == head(connection)


def test_source_write_and_journal_rollback_together(tmp_path):
    company = Company(tmp_path)
    company.expense("expense", 1000)
    with company.engine.store.connection() as connection:
        checkpoint = head(connection)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM fact_current WHERE subject_id='expense'")
        assert head(connection) == checkpoint + 1
        connection.rollback()
        assert head(connection) == checkpoint
        assert changes_since(connection, checkpoint) == ()
        assert verify_journal(connection) == checkpoint


def test_first_creation_proof_distinguishes_revision_and_restoration(tmp_path):
    company = Company(tmp_path)
    with company.engine.store.connection(read_only=True) as connection:
        before = head(connection)
    first = company.expense("new-business", 1000)
    with company.engine.store.connection(read_only=True) as connection:
        created_at = head(connection)
        assert first_created_subjects(connection, changes_since(connection, before)) == {
            "new-business"
        }
    company.expense("new-business", 1200, revision=1)
    with company.engine.store.connection() as connection:
        assert not first_created_subjects(connection, changes_since(connection, created_at))
        connection.execute("BEGIN IMMEDIATE")
        checkpoint = head(connection)
        connection.execute("DELETE FROM fact_current WHERE subject_id='new-business'")
        connection.execute(
            "INSERT INTO fact_current VALUES('new-business',?)", (first["fact_id"],)
        )
        assert not first_created_subjects(connection, changes_since(connection, checkpoint))
        connection.rollback()


@pytest.mark.parametrize("owner,reference", [("wrong-owner", None), ("new-business", "missing")])
def test_first_creation_proof_rejects_missing_or_mismatched_source(tmp_path, owner, reference):
    company = Company(tmp_path)
    saved = company.expense("new-business", 1000)
    with company.engine.store.connection(read_only=True) as connection:
        with pytest.raises(KernelError, match="资料变化记录"):
            first_created_subjects(
                connection, [SourceChange(1, "fact", owner, None, reference or saved["fact_id"])]
            )


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE source_change SET target_id='other' WHERE sequence=1",
        "DELETE FROM source_change WHERE sequence=1",
        "UPDATE source_change_head SET sequence=sequence-1",
        "UPDATE source_change_head SET sequence=sequence+1",
        "DELETE FROM source_change_head",
        "UPDATE fact_current SET subject_id='other' WHERE subject_id='expense'",
    ],
)
def test_history_and_head_cannot_be_rewritten(tmp_path, statement):
    company = Company(tmp_path)
    company.expense("expense", 1000)
    with company.engine.store.connection() as connection:
        checkpoint = head(connection)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(statement)
        connection.rollback()
        assert head(connection) == checkpoint
        assert verify_journal(connection) == checkpoint


def test_missing_event_is_rejected_instead_of_proving_no_change(tmp_path):
    company = Company(tmp_path)
    company.expense("expense", 1000)
    damage(company.engine, "source_change", "DELETE FROM source_change WHERE sequence=2")
    with company.engine.store.connection(read_only=True) as connection:
        with pytest.raises(KernelError) as failure:
            changes_since(connection, 0)
    assert failure.value.details["reason"] in {"journal_entry_invalid", "journal_entry_missing"}


def test_unrecorded_current_change_fails_full_verification(tmp_path):
    company = Company(tmp_path)
    company.expense("expense", 1000)
    damage(company.engine, "fact_current", "DELETE FROM fact_current WHERE subject_id='expense'")
    with company.engine.store.connection(read_only=True) as connection:
        with pytest.raises(KernelError) as failure:
            verify_journal(connection)
    assert failure.value.details["reason"] == "journal_current_heads_mismatch"


def test_pending_event_cannot_claim_addition_and_removal_together(tmp_path):
    company = Company(tmp_path)
    company.expense("expense", 1000)
    damage(
        company.engine,
        "source_change",
        "UPDATE source_change SET before_ref='invented-before',after_ref='invented-after' "
        "WHERE sequence=(SELECT min(sequence) FROM source_change WHERE source='pending')",
    )
    with company.engine.store.connection(read_only=True) as connection:
        with pytest.raises(KernelError) as failure:
            changes_since(connection, 0)
    assert failure.value.details["reason"] == "journal_entry_invalid"


def test_material_sources_and_inventory_are_recorded_without_inventing_business(tmp_path):
    from ai_accounting.kernel.periods import Periods

    company = Company(tmp_path)
    source, evidence = company.source()
    periods = Periods(company.engine)
    with company.engine.store.connection(read_only=True) as connection:
        checkpoint = head(connection)
    periods.inventory(
        "2026-01",
        "transactions",
        expected=1,
        evidence=(evidence,),
        no_business=False,
        confirmation_evidence=company.proof,
        request_id=company.request(),
    )
    with company.engine.store.connection(read_only=True) as connection:
        changes = changes_since(connection, checkpoint)
        assert [row.source for row in changes] == ["inventory", "inventory_item"]
        assert changes[1].after_ref == evidence
        assert source["fact_id"] in heads_at(connection, head(connection))["fact"].values()
        assert verify_journal(connection) == head(connection)
