"""Saved JSON has one interpretation before it can filter accounting sources."""

import hashlib
import json

import pytest
import test_banking as banking
import test_deletion_boundaries as settlements
import test_duplicates as duplicate_fixtures
import test_opening_continuation as opening_fixtures
import test_payroll_corrections as payroll_fixtures

from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_reads import adopted_head_metadata
from ai_accounting.kernel.duplicates import DuplicateCandidates
from ai_accounting.kernel.entities import Entities, employee_entities
from ai_accounting.kernel.integrity import verify_integrity
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.types import canonical

bank_book = banking.book
domain_book = settlements.book
duplicate_company = duplicate_fixtures.company
payroll_company = payroll_fixtures.company
opening_book = opening_fixtures.book


def _replace_immutable(connection, trigger_name, table, column, identity, raw, *, raw_digest=False):
    trigger = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE name=?", (trigger_name,)
    ).fetchone()[0]
    connection.execute("DROP TRIGGER " + trigger_name)
    if raw_digest:
        connection.execute(
            f"UPDATE {table} SET {column}=?,digest=? WHERE id=?",
            (raw, hashlib.sha256(raw.encode()).digest(), identity),
        )
    else:
        connection.execute(f"UPDATE {table} SET {column}=? WHERE id=?", (raw, identity))
    connection.execute(trigger)


@pytest.mark.parametrize("variant", ["balances", "values", "nested_escaped"])
@pytest.mark.parametrize("rewrite_row_digest", [False, True])
def test_saved_outcome_cannot_hide_sql_funds_source_with_either_digest(
    bank_book, variant, rewrite_row_digest
):
    engine, save, publish, _ = bank_book
    banking.funding(save, publish)
    before = Dashboard(engine).funds("2026-09", preparation="deferred")["data"]
    assert before["collections"]["movements"]["page"]["total_count"] == 1
    with engine.store.connection() as connection:
        calculation_id = connection.execute(
            "SELECT calculation_id FROM calculation_current WHERE subject_id='funding'"
        ).fetchone()[0]
        raw = connection.execute(
            "SELECT outcome FROM calculation WHERE id=?", (calculation_id,)
        ).fetchone()[0]
        if variant == "balances":
            changed = '{"balances":[],' + raw[1:]
        elif variant == "values":
            changed = '{"values":{"bank_account_id":"other-bank"},' + raw[1:]
        else:
            changed = raw.replace(
                '"bank_account_id":"bank-a"',
                '"bank_account_id":"other-bank","bank\\u005faccount_id":"bank-a"',
                1,
            )
            assert changed != raw
        assert json.loads(changed) == json.loads(raw)
        _replace_immutable(
            connection,
            "immutable_calculation_UPDATE",
            "calculation",
            "outcome",
            calculation_id,
            changed,
            raw_digest=rewrite_row_digest,
        )
    with pytest.raises(KernelError) as page:
        Dashboard(engine).funds("2026-09", preparation="deferred")
    assert page.value.code == "content_integrity_failed"
    with engine.store.connection(read_only=True) as connection:
        with pytest.raises(KernelError) as readiness:
            Periods(engine).collect_current_readiness(connection, "2026-09")
        with pytest.raises(KernelError) as complete:
            verify_integrity(engine, connection)
    assert readiness.value.code == complete.value.code == "content_integrity_failed"


def test_employee_profile_negative_membership_checks_saved_json_before_filter(bank_book):
    engine, *_ = bank_book
    person = Entities(engine).register_entity(
        "person",
        {"display_name": "合成员工", "employment_status": "active"},
        source="synthetic profile",
        request_id="person",
    )["entity_id"]
    with engine.store.connection(read_only=True) as connection:
        assert person in employee_entities(connection, "2026-09", registry=engine.store.registry)
    with engine.store.connection() as connection:
        row = connection.execute(
            "SELECT id,content FROM entity_profile_revision WHERE entity_id=?", (person,)
        ).fetchone()
        changed = '{"employment_status":"unknown",' + row["content"][1:]
        assert json.loads(changed) == json.loads(row["content"])
        _replace_immutable(
            connection,
            "immutable_entity_profile_revision_UPDATE",
            "entity_profile_revision",
            "content",
            row["id"],
            changed,
        )
    with engine.store.connection(read_only=True) as connection:
        with pytest.raises(KernelError) as selected:
            employee_entities(connection, "2026-09", registry=engine.store.registry)
        with pytest.raises(KernelError) as complete:
            verify_integrity(engine, connection)
    assert selected.value.code == "content_integrity_failed"
    assert complete.value.code in {"content_integrity_failed", "entity_profile_corrupt"}


def test_adopted_payroll_head_rejects_hidden_lines_before_sql_count(payroll_company):
    payroll_company.publish("january")
    engine = payroll_company.engine
    with engine.store.connection() as connection:
        calculation_id = connection.execute(
            "SELECT calculation_id FROM calculation_current WHERE subject_id='january'"
        ).fetchone()[0]
        raw = connection.execute(
            "SELECT outcome FROM calculation WHERE id=?", (calculation_id,)
        ).fetchone()[0]
        assert json.loads(raw)["lines"]
        changed = '{"lines":[],' + raw[1:]
        assert json.loads(changed) == json.loads(raw)
        _replace_immutable(
            connection,
            "immutable_calculation_UPDATE",
            "calculation",
            "outcome",
            calculation_id,
            changed,
            raw_digest=True,
        )
    with Dashboard(engine)._snapshot("2026-01") as snapshot:
        with pytest.raises(KernelError) as selected:
            adopted_head_metadata(
                snapshot, {"payroll"}, posting_period="2026-01", line_count_period="2026-01"
            )
    assert selected.value.code == "content_integrity_failed"


def test_second_opening_confirm_rejects_hidden_existing_opening(opening_book):
    engine, save, publish, package, _ = opening_book
    package(opening_fixtures.complete_members(save))
    with engine.store.connection() as connection:
        calculation_id = connection.execute(
            "SELECT calculation_id FROM calculation_current WHERE subject_id='opening'"
        ).fetchone()[0]
        raw = connection.execute(
            "SELECT outcome FROM calculation WHERE id=?", (calculation_id,)
        ).fetchone()[0]
        assert json.loads(raw)["opening"] is True
        changed = '{"opening":false,' + raw[1:]
        assert json.loads(changed) == json.loads(raw)
        _replace_immutable(
            connection,
            "immutable_calculation_UPDATE",
            "calculation",
            "outcome",
            calculation_id,
            changed,
            raw_digest=True,
        )
    save(
        "opening_package",
        "second-opening",
        {
            "period": "2026-01",
            "package_id": "second-opening",
            "counts": dict.fromkeys(opening_fixtures.CATEGORIES.values(), 0),
            "members": [],
            "completeness_confirmed": True,
        },
    )
    with pytest.raises(KernelError) as second:
        publish("second-opening")
    assert second.value.code == "content_integrity_failed"


def test_duplicate_review_manifest_cannot_hide_strong_pair_from_readiness(duplicate_company):
    from ai_accounting.kernel.duplicate_checks_v1 import _require_check_record as verify_v1

    engine, party = duplicate_company
    proof = duplicate_fixtures.evidence(engine, "double-key-pair")
    fact = duplicate_fixtures.expense(engine, party)
    duplicates = DuplicateCandidates(engine.store)
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        duplicate_fixtures.write_fact(engine.store, connection, "expense-a", fact, (proof,))
        prepared = duplicates.prepare(
            connection, subject_id="expense-b", revision=1, fact=fact, evidence=(proof,)
        )
        assert prepared["strong_candidates"]
        second = duplicate_fixtures.write_fact(
            engine.store, connection, "expense-b", fact, (proof,)
        )
        recorded = duplicates.record_check(
            connection,
            prepared=prepared,
            result_fact_id=second.id,
            review=duplicate_fixtures.owner_review(prepared, proof),
        )
        duplicate_fixtures.fill_missing_test_checks(engine.store, connection)
        assert duplicates.close_readiness(connection, "2026-01") == []
        check_id = recorded["check_id"]
        original = connection.execute(
            "SELECT manifest FROM business_duplicate_check WHERE id=?", (check_id,)
        ).fetchone()[0]
        changed = '{"strong_candidates":[],' + original[1:]
        assert json.loads(changed) == json.loads(original)
        connection.execute("DROP TRIGGER business_duplicate_check_no_update")
        connection.execute(
            "UPDATE business_duplicate_check SET manifest=? WHERE id=?", (changed, check_id)
        )
        with pytest.raises(KernelError) as readiness:
            duplicates.close_readiness(connection, "2026-01")
        with pytest.raises(KernelError) as historical:
            verify_v1(
                connection.execute(
                    "SELECT * FROM business_duplicate_check WHERE id=?", (check_id,)
                ).fetchone()
            )
    assert readiness.value.code == "content_integrity_failed"
    assert historical.value.code == "duplicate_review_corrupt"


def test_settlement_page_rejects_hidden_saved_slot_before_subject_filter(domain_book):
    engine, _save, publish, *_ = domain_book
    settlements.prepare_payment(domain_book)
    publish("payment")
    with engine.store.connection() as connection:
        calculation_id = connection.execute(
            "SELECT calculation_id FROM calculation_current WHERE subject_id='payment'"
        ).fetchone()[0]
        raw = connection.execute(
            "SELECT outcome FROM calculation WHERE id=?", (calculation_id,)
        ).fetchone()[0]
        changed = '{"values":{"settlements":[]},' + raw[1:]
        assert json.loads(changed) == json.loads(raw)
        _replace_immutable(
            connection,
            "immutable_calculation_UPDATE",
            "calculation",
            "outcome",
            calculation_id,
            changed,
            raw_digest=True,
        )
        with pytest.raises(KernelError) as page:
            BusinessQueries(engine).business_collection(
                connection, "expense", "2026-01", section="settlement_events"
            )
    assert page.value.code == "content_integrity_failed"


@pytest.mark.parametrize(
    "variant", ["values", "escaped_values", "obligations", "escaped_obligations"]
)
def test_global_settlement_subjects_reject_hidden_obligation_before_negative_filter(
    domain_book, variant
):
    engine, _save, publish, *_ = domain_book
    settlements.prepare_payment(domain_book)
    publish("payment")
    with engine.store.connection() as connection:
        calculation_id = connection.execute(
            "SELECT calculation_id FROM calculation_current WHERE subject_id='expense'"
        ).fetchone()[0]
        raw = connection.execute(
            "SELECT outcome FROM calculation WHERE id=?", (calculation_id,)
        ).fetchone()[0]
        assert json.loads(raw)["values"]["obligations"]
        if variant in {"values", "escaped_values"}:
            key = "values" if variant == "values" else "val\\u0075es"
            changed = '{"' + key + '":{"obligations":[]},' + raw[1:]
        else:
            key = "obligations" if variant == "obligations" else "oblig\\u0061tions"
            changed = raw.replace('"obligations":', '"' + key + '":[],"obligations":', 1)
            assert changed != raw
        assert json.loads(changed) == json.loads(raw)
        _replace_immutable(
            connection,
            "immutable_calculation_UPDATE",
            "calculation",
            "outcome",
            calculation_id,
            changed,
            raw_digest=True,
        )
        with pytest.raises(KernelError) as global_page:
            BusinessQueries(engine).business_collection(
                connection, None, "2026-01", section="settlement_events"
            )
        with pytest.raises(KernelError) as unbounded:
            QueryReads(engine, connection).settlement_subjects()
    assert global_page.value.code == "content_integrity_failed"
    assert unbounded.value.code == "content_integrity_failed"


def test_global_settlement_subjects_accepts_equivalent_noncanonical_outcome(domain_book):
    engine, _save, publish, *_ = domain_book
    settlements.prepare_payment(domain_book)
    publish("payment")
    with engine.store.connection() as connection:
        calculation_id = connection.execute(
            "SELECT calculation_id FROM calculation_current WHERE subject_id='expense'"
        ).fetchone()[0]
        raw = connection.execute(
            "SELECT outcome FROM calculation WHERE id=?", (calculation_id,)
        ).fetchone()[0]
        changed = json.dumps(json.loads(raw), ensure_ascii=False, indent=2)
        assert changed != raw and canonical(json.loads(changed)) == raw
        _replace_immutable(
            connection,
            "immutable_calculation_UPDATE",
            "calculation",
            "outcome",
            calculation_id,
            changed,
        )
        result = BusinessQueries(engine).business_collection(
            connection, None, "2026-01", section="settlement_events"
        )
    assert result


def test_equivalent_noncanonical_result_remains_readable(bank_book):
    engine, save, publish, _ = bank_book
    banking.funding(save, publish)
    with engine.store.connection() as connection:
        calculation_id = connection.execute(
            "SELECT calculation_id FROM calculation_current WHERE subject_id='funding'"
        ).fetchone()[0]
        raw = connection.execute(
            "SELECT outcome FROM calculation WHERE id=?", (calculation_id,)
        ).fetchone()[0]
        changed = json.dumps(json.loads(raw), ensure_ascii=False, indent=2, sort_keys=False)
        assert changed != raw and canonical(json.loads(changed)) == raw
        _replace_immutable(
            connection,
            "immutable_calculation_UPDATE",
            "calculation",
            "outcome",
            calculation_id,
            changed,
        )
    data = Dashboard(engine).funds("2026-09", preparation="deferred")["data"]
    assert data["collections"]["movements"]["page"]["total_count"] == 1
    with engine.store.connection(read_only=True) as connection:
        verify_integrity(engine, connection)
