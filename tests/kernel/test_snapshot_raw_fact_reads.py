"""Raw fact reuse is limited to one read transaction and never proves content."""

import json
import sqlite3
from typing import ClassVar

import pytest
from pydantic import BaseModel
from pydantic_core import from_json
from schema_fixture import test_bundle
from test_engine import engine as engine  # noqa: F401
from test_engine import evidence, publish, save
from test_integrity_content import damage
from test_payroll import contribution_policy
from test_payroll_corrections import Company

from ai_accounting.kernel import storage
from ai_accounting.kernel.contracts import Fact, KernelError, Line, Outcome, Registry
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.integrity import verify_sources
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.types import canonical, digest


class SequenceLine(BaseModel):
    amount: int


class SequenceCharge(Fact):
    kind: ClassVar[str] = "test_sequence_charge"
    items: tuple[SequenceLine, ...]


def calculate_sequence(version, _context):
    amount = sum(item.amount for item in version.fact.items)
    return Outcome((Line("5602", debit=amount), Line("2202", credit=amount)), {"amount": amount})


def test_prevalidation_json_keeps_int64_decimal_text_and_embedded_json():
    # SQLite's raw Decimal rate is text; Pydantic converts it only after this
    # storage-value boundary. Embedded JSON text must stay text as well.
    raw = {
        "maximum_fen": 2**63 - 1,
        "minimum_fen": -(2**63),
        "rate": "0.1350",
        "literal": '{"amount":1.00,"name":"汉字"}',
        "nested": [{"amount_fen": 2**63 - 1}],
    }
    restored = from_json(storage._validation_json(raw))
    assert restored == raw
    assert digest(restored) == digest(raw)


def test_raw_fact_uses_same_snapshot_only_after_successful_typed_decode(engine, monkeypatch):
    fact_id = save(engine)["fact_id"]
    loaded = []
    original = engine.store.fact_data_many

    def counted(connection, identifiers):
        loaded.append(set(identifiers))
        return original(connection, identifiers)

    monkeypatch.setattr(engine.store, "fact_data_many", counted)
    with QueryReads.snapshot(engine) as reads:
        reads.fact_versions((fact_id,))
        assert reads._raw_fact_data.keys() == {fact_id}
        copy_of_raw = storage._snapshot_fact_raws(engine.store, reads.connection, (fact_id,))
        assert copy_of_raw == original(reads.connection, (fact_id,))
        copy_of_raw[fact_id]["amount"] += 1
        assert verify_sources(engine, reads.connection, fact_ids=(fact_id,))["facts"] == 1
        assert loaded == []
        with engine.store.connection(read_only=True) as other:
            other.execute("BEGIN")
            assert verify_sources(engine, other, fact_ids=(fact_id,))["facts"] == 1
            assert loaded == [{fact_id}]
        with pytest.raises(sqlite3.DatabaseError):
            reads.connection.execute("COMMIT")
        with pytest.raises(sqlite3.DatabaseError):
            reads.connection.commit()
        with pytest.raises(sqlite3.DatabaseError):
            reads.connection.rollback()
    assert reads._raw_fact_data == {}
    with QueryReads.snapshot(engine) as later:
        assert verify_sources(engine, later.connection, fact_ids=(fact_id,))["facts"] == 1
    assert loaded == [{fact_id}, {fact_id}]


def test_failed_typed_decode_never_seeds_raw_snapshot(engine, monkeypatch):
    fact_ids = (
        save(engine)["fact_id"],
        save(engine, subject="other-charge", request="other-save")["fact_id"],
    )
    with QueryReads.snapshot(engine) as reads:
        attempted = 0
        model = engine.store.registry.models["test_charge"]
        original = model.model_validate_json

        def reject_validation(raw):
            nonlocal attempted
            attempted += 1
            if attempted == 2:
                raise ValueError("synthetic typed decode failure")
            return original(raw)

        with monkeypatch.context() as patch:
            patch.setattr(model, "model_validate_json", staticmethod(reject_validation))
            with pytest.raises(ValueError, match="typed decode failure"):
                reads.fact_versions(fact_ids)
        assert attempted == 2
        assert reads._raw_fact_data == {}
        assert verify_sources(engine, reads.connection, fact_ids=fact_ids)["facts"] == 2


def test_scalar_batch_json_keeps_raw_amounts_and_booleans(engine):
    first = save(engine, amount=2**63 - 1)["fact_id"]
    second = engine.save_fact(
        "test_charge",
        "other-charge",
        {"period": "2026-02", "amount": 1, "suppress_posting": True},
        evidence=(evidence(engine),),
        expected_revision=0,
        request_id="other-charge",
    )["fact_id"]
    with QueryReads.snapshot(engine) as reads:
        versions = reads.fact_versions((first, second))
        raw = storage._snapshot_fact_raws(engine.store, reads.connection, (first, second))
        assert raw == engine.store.fact_data_many(reads.connection, (first, second))
        assert versions[first].fact.amount == 2**63 - 1
        assert versions[second].fact.suppress_posting is True
        assert verify_sources(engine, reads.connection, fact_ids=(first, second))["facts"] == 2


def test_cached_raw_fact_still_checks_original_digest(engine):
    fact_id = save(engine)["fact_id"]
    damage(engine, "fact_test_charge", "UPDATE fact_test_charge SET amount=101")
    with QueryReads.snapshot(engine) as reads:
        assert reads.fact_version(fact_id).fact.amount == 101
        with pytest.raises(KernelError) as failure:
            verify_sources(engine, reads.connection, fact_ids=(fact_id,))
        assert failure.value.code == "content_integrity_failed"
        assert failure.value.details["reason"] == "fact_digest_mismatch"


def test_scalar_snapshot_bytes_hash_fast_path_and_equivalent_encoding_fallback(engine, monkeypatch):
    from ai_accounting.kernel import integrity

    fact_id = save(engine)["fact_id"]
    original = integrity.digest
    fact_hashes = []

    def counted(value):
        if isinstance(value, dict) and {"amount", "period"} <= value.keys():
            fact_hashes.append(value)
        return original(value)

    with QueryReads.snapshot(engine) as reads:
        reads.fact_version(fact_id)
        raw = storage._snapshot_fact_raws(engine.store, reads.connection, (fact_id,))[fact_id]
        assert reads._raw_fact_data[fact_id] == canonical(raw).encode("utf-8")
        with monkeypatch.context() as patch:
            patch.setattr(integrity, "digest", counted)
            assert verify_sources(engine, reads.connection, fact_ids=(fact_id,))["facts"] == 1
            assert not fact_hashes
            # Noncanonical JSON remains acceptable when it encodes the same
            # original values; the byte fast path cannot change this contract.
            reads._raw_fact_data[fact_id] = json.dumps(raw, indent=2).encode("utf-8")
            assert verify_sources(engine, reads.connection, fact_ids=(fact_id,))["facts"] == 1
            assert fact_hashes == [raw]
    assert not reads._raw_fact_data


def test_cached_raw_fact_still_checks_typed_child_order(tmp_path):
    company = Company(tmp_path / "child-order.sqlite")
    fact_id = company.save(contribution_policy(), "contributions")["fact_id"]
    table = "fact_payroll_contribution_policy_rules"
    with company.engine.store.connection(read_only=True) as connection:
        assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] > 0
    with QueryReads.snapshot(company.engine) as reads:
        reads.fact_version(fact_id)
        direct = company.engine.store.fact_data_many(reads.connection, (fact_id,))
        assert isinstance(direct[fact_id]["rules"][0]["employee_rate"], str)
        assert (
            storage._snapshot_fact_raws(company.engine.store, reads.connection, (fact_id,))
            == direct
        )
    damage(
        company.engine,
        table,
        f"UPDATE {table} SET item_no=item_no+10 WHERE revision_id=?",
        (fact_id,),
    )
    with QueryReads.snapshot(company.engine) as reads:
        assert reads.fact_version(fact_id).id == fact_id
        with pytest.raises(KernelError) as failure:
            verify_sources(company.engine, reads.connection, fact_ids=(fact_id,))
        assert failure.value.details["reason"] == "fact_child_order_mismatch"


def test_write_transaction_never_reuses_earlier_typed_decode(engine):
    fact_id = save(engine)["fact_id"]
    with engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        engine.store.facts(connection, (fact_id,))
        triggers = tuple(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='trigger' "
                "AND tbl_name='fact_test_charge'"
            )
        )
        for name in triggers:
            connection.execute(f'DROP TRIGGER "{name}"')
        connection.execute(
            "UPDATE fact_test_charge SET amount=amount+1 WHERE revision_id=?", (fact_id,)
        )
        with pytest.raises(KernelError) as failure:
            verify_sources(engine, connection, fact_ids=(fact_id,))
        assert failure.value.details["reason"] == "fact_digest_mismatch"
        connection.rollback()


def test_selected_content_reuses_success_only_inside_one_snapshot(engine):
    save(engine)
    _, result = publish(engine)
    ident = result["results"][0]["calculation_id"]
    with QueryReads.snapshot(engine) as reads:
        statements = []
        reads.connection.set_trace_callback(statements.append)
        assert reads.verify_selected_content((ident,))[ident]["values"]["amount"] == 100
        initial_queries = len(statements)
        assert initial_queries > 0
        reads.verify_selected_content((ident,))
        assert len(statements) == initial_queries
        # Content verification does not hydrate display data or dependency ancestry.
        assert reads._calculations == reads._parents == {}
    assert reads._verified_source_contents == {}
    damage(
        engine,
        "calculation",
        "UPDATE calculation SET outcome=json_set(outcome,'$.values.amount',101)",
    )
    with QueryReads.snapshot(engine) as later:
        with pytest.raises(KernelError) as failure:
            later.verify_selected_content((ident,))
        assert failure.value.code == "content_integrity_failed"


def test_selected_content_reuses_exact_outcome_text_without_second_body_select(engine):
    save(engine)
    _, result = publish(engine)
    ident = result["results"][0]["calculation_id"]
    with QueryReads.snapshot(engine) as reads:
        reads.calculation(ident)
        stored = (
            reads.connection.execute("SELECT outcome FROM calculation WHERE id=?", (ident,))
            .fetchone()[0]
            .encode("utf-8")
        )
        assert reads._raw_calculation_outcomes == {ident: stored}
        statements = []
        reads.connection.set_trace_callback(statements.append)
        assert reads.verify_selected_content((ident,))[ident]["values"]["amount"] == 100
        content_headers = [
            sql for sql in statements if "fact_subject" in sql and "calculation_sealed" in sql
        ]
        assert len(content_headers) == 1
        assert "c.outcome" not in content_headers[0]
        assert len(stored) > 0
    assert reads._raw_calculation_outcomes == {}
    with QueryReads.snapshot(engine) as later:
        statements = []
        later.connection.set_trace_callback(statements.append)
        later.verify_selected_content((ident,))
        assert any(
            "c.outcome" in sql
            for sql in statements
            if "fact_subject" in sql and "calculation_sealed" in sql
        )


def test_selected_content_reused_outcome_still_rejects_changed_valid_json(engine):
    save(engine)
    _, result = publish(engine)
    ident = result["results"][0]["calculation_id"]
    damage(
        engine,
        "calculation",
        "UPDATE calculation SET outcome=json_set(outcome,'$.values.amount',101)",
    )
    with QueryReads.snapshot(engine) as reads:
        assert reads.calculation(ident)["outcome"]["values"]["amount"] == 101
        with pytest.raises(KernelError) as failure:
            reads.verify_selected_content((ident,))
        assert failure.value.code == "content_integrity_failed"
        assert reads._verified_source_contents == {}


@pytest.mark.parametrize(
    ("invalid_outcome", "reason"),
    (("not-json", "invalid_json"), ("[]", "invalid_object")),
)
def test_selected_content_rejects_invalid_outcome_shape_without_caching(
    engine, invalid_outcome, reason
):
    save(engine)
    _, result = publish(engine)
    ident = result["results"][0]["calculation_id"]
    with engine.store.connection() as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute("BEGIN IMMEDIATE")
        schema_objects = tuple(
            connection.execute(
                "SELECT type,name,sql FROM sqlite_schema WHERE tbl_name='calculation' "
                "AND sql IS NOT NULL AND type IN ('trigger','index')"
            )
        )
        for kind, name, _ in schema_objects:
            connection.execute(f'DROP {kind.upper()} "{name}"')
        connection.execute("UPDATE calculation SET outcome=? WHERE id=?", (invalid_outcome, ident))
        reads = QueryReads(engine, connection)
        with pytest.raises(KernelError) as failure:
            reads.verify_selected_content((ident,))
        assert failure.value.code == "content_integrity_failed"
        assert failure.value.details["reason"] == reason
        assert reads._verified_source_contents == {}
        connection.rollback()


def test_selected_content_decodes_cached_raw_not_mutable_display_object(engine):
    save(engine)
    _, result = publish(engine)
    ident = result["results"][0]["calculation_id"]
    with QueryReads.snapshot(engine) as reads:
        display = reads.calculation(ident)
        display["outcome"]["values"]["amount"] = 999
        verified = reads.verify_selected_content((ident,))
        assert verified[ident]["values"]["amount"] == 100
        assert display["outcome"]["values"]["amount"] == 999


def test_failed_calculation_batch_does_not_seed_raw_outcomes(engine, monkeypatch):
    from ai_accounting.kernel import query_reads

    save(engine)
    save(engine, subject="second", request="second")
    _, result = publish(engine, ["charge", "second"])
    ids = [item["calculation_id"] for item in result["results"]]
    with QueryReads.snapshot(engine) as reads:
        actual_loads = json.loads
        decoded = 0

        def fail_second_outcome(raw):
            nonlocal decoded
            decoded += 1
            if decoded == 2:
                raise ValueError("synthetic outcome decode failure")
            return actual_loads(raw)

        with monkeypatch.context() as patch:
            patch.setattr(query_reads.json, "loads", fail_second_outcome)
            with pytest.raises(ValueError, match="outcome decode failure"):
                reads.calculations(ids)
        assert decoded == 2
        assert reads._raw_calculation_outcomes == {}
        assert reads._calculations == {}
        assert reads._verified_source_contents == {}


def test_unmanaged_calculation_does_not_retain_raw_outcome(engine):
    save(engine)
    _, result = publish(engine)
    ident = result["results"][0]["calculation_id"]
    with engine.store.connection() as connection:
        reads = QueryReads(engine, connection)
        reads.calculation(ident)
        assert reads._raw_calculation_outcomes == {}
        statements = []
        connection.set_trace_callback(statements.append)
        reads.verify_selected_content((ident,))
        assert any(
            "c.outcome" in sql
            for sql in statements
            if "fact_subject" in sql and "calculation_sealed" in sql
        )


def test_failed_selected_content_batch_never_caches_partial_success(engine):
    save(engine)
    save(engine, subject="second", request="second")
    _, result = publish(engine, ["charge", "second"])
    ids = [item["calculation_id"] for item in result["results"]]
    damage(
        engine,
        "calculation",
        "UPDATE calculation SET outcome=json_set(outcome,'$.values.amount',101) WHERE id=?",
        (ids[-1],),
    )
    with QueryReads.snapshot(engine) as reads:
        reads.calculations(ids)
        with pytest.raises(KernelError) as failure:
            reads.verify_selected_content(ids)
        assert failure.value.code == "content_integrity_failed"
        assert reads._verified_source_contents == {}


def test_selected_content_rejects_removed_fact_body_without_caching(engine):
    fact_id = save(engine)["fact_id"]
    _, result = publish(engine)
    ident = result["results"][0]["calculation_id"]
    damage(
        engine,
        "fact_test_charge",
        "DELETE FROM fact_test_charge WHERE revision_id=?",
        (fact_id,),
    )
    with QueryReads.snapshot(engine) as reads:
        with pytest.raises(KernelError) as failure:
            reads.verify_selected_content((ident,))
        assert failure.value.code == "content_integrity_failed"
        assert reads._verified_source_contents == {}


def test_selected_content_does_not_trust_unmanaged_display_cache(engine):
    save(engine)
    _, result = publish(engine)
    ident = result["results"][0]["calculation_id"]
    with engine.store.connection() as connection:
        reads = QueryReads(engine, connection)
        reads.calculation(ident)
        reads.verify_selected_content((ident,))
        assert reads._verified_source_contents == {}
        damage(
            engine,
            "calculation",
            "UPDATE calculation SET outcome=json_set(outcome,'$.values.amount',101)",
        )
        with pytest.raises(KernelError) as failure:
            reads.verify_selected_content((ident,))
        assert failure.value.code == "content_integrity_failed"


def test_selected_content_keeps_canonical_json_equivalence(engine):
    save(engine)
    _, result = publish(engine)
    ident = result["results"][0]["calculation_id"]
    with engine.store.connection(read_only=True) as connection:
        raw = connection.execute("SELECT outcome FROM calculation WHERE id=?", (ident,)).fetchone()[
            0
        ]
    pretty = json.dumps(json.loads(raw), ensure_ascii=False, indent=2)
    assert pretty != raw
    damage(engine, "calculation", "UPDATE calculation SET outcome=? WHERE id=?", (pretty, ident))
    with QueryReads.snapshot(engine) as reads:
        assert reads.calculation(ident)["outcome"]["values"]["amount"] == 100
        assert reads._raw_calculation_outcomes[ident] == pretty.encode("utf-8")
        assert reads.verify_selected_content((ident,))[ident]["values"]["amount"] == 100


def test_selected_content_rejects_changed_sequence_positions(tmp_path):
    registry = Registry()
    registry.register(SequenceCharge, calculate_sequence)
    engine = Engine(
        storage.Store.create(
            tmp_path / "sequence.sqlite",
            test_bundle(registry),
            "company-a",
            "91310000123456789A",
            "db-a",
        )
    )
    engine.save_fact(
        "test_sequence_charge",
        "charge",
        {"period": "2026-01", "items": [{"amount": 100}, {"amount": 200}]},
        evidence=(evidence(engine),),
        expected_revision=0,
        request_id="save",
    )
    _, result = publish(engine)
    ident = result["results"][0]["calculation_id"]
    table = "fact_test_sequence_charge_items"
    damage(engine, table, f"UPDATE {table} SET item_no=item_no+10")
    with QueryReads.snapshot(engine) as reads:
        reads.calculation(ident)
        with pytest.raises(KernelError) as failure:
            reads.verify_selected_content((ident,))
        assert failure.value.code == "content_integrity_failed"
        assert failure.value.details["reason"] == "fact_child_order_mismatch"
