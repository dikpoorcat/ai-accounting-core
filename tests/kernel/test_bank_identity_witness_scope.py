"""Exact positive bank witnesses bound old identities, never money/source scope."""

import json

import pytest
import test_banking as banking
from stage9_metrics import measure_work
from test_funds_historical_account_identities import prepared, public, source
from test_integrity_content import damage

from ai_accounting.kernel import dashboard_funds
from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_funds import FundsRead
from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.identity_corrections import IdentityCorrections
from ai_accounting.kernel.integrity import verify_integrity
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.types import YearMonth, canonical

bank_book = banking.book
PERIOD = "2026-11"


def history(book):
    engine, save, publish = prepared(book)
    for bank, amount in (("bank-a", 1000), ("zero-bank", 0)):
        statement = "oct-statement-" + bank
        banking.statement(save, publish, [], subject=statement, bank=bank,
                          month="2026-10", initial=amount)
        banking.reconciliation(save, publish, [], subject="oct-reconciliation-" + bank,
                               statement_id=statement, bank=bank, month="2026-10")
    banking.close_month(banking.inventories(engine, book[3], "2026-10", {"bank"}),
                       book[3], "2026-10")
    banking.inventories(engine, book[3], PERIOD, set())
    return engine, save, publish


def full_scope(monkeypatch):
    monkeypatch.setattr(dashboard_funds, "_bank_identity_witness_heads", lambda _snap: None)


def states(engine, period=PERIOD):
    with Dashboard(engine)._snapshot(period) as snap:
        read = FundsRead(snap)
        return {ident: dict(item) for ident, item in read.states.items()}


def test_positive_witnesses_keep_zero_inactive_accounts_and_reduce_real_work(
    bank_book, monkeypatch, record_property,
):
    engine, _, _ = history(bank_book)
    Entities(engine).update_entity_profile(
        "zero-bank", {"active": False}, source="explicit synthetic inactivity",
        expected_revision=1, request_id="inactive-zero",
    )
    narrow_states = states(engine)
    narrow_work, narrow = measure_work(engine, lambda: public(engine, PERIOD))
    with monkeypatch.context() as complete:
        full_scope(complete)
        full_states = states(engine)
        full_work, baseline = measure_work(engine, lambda: public(engine, PERIOD))
    assert narrow == baseline
    assert narrow["funds"]["data"]["bank_account_count"] == 2
    assert narrow["funds"]["data"]["bank_statement"]["missing_account_count"] == 2
    assert narrow["brief"]["data"]["funds_overview"]["bank_fen"] == 1000
    assert len(narrow_states) == 4  # Complete two bank starts plus two exact witnesses.
    assert len(full_states) == 10
    for counter in ("calculation_result_rows_loaded", "calculation_result_bytes_loaded",
                    "calculation_result_json_decodes", "returned_value_bytes"):
        assert narrow_work["counters"][counter] < full_work["counters"][counter]
    record_property("bank_identity_scope_work", json.dumps({
        "narrow_states": len(narrow_states), "complete_states": len(full_states),
        "narrow": narrow_work["counters"], "complete": full_work["counters"],
    }, sort_keys=True))


@pytest.mark.parametrize("boundary", ["draft", "missing_role", "wrong_role", "unowned",
                                      "disabled", "v1"])
def test_incomplete_or_unowned_guards_retain_complete_scope(bank_book, monkeypatch, boundary):
    engine, save, publish = history(bank_book)
    if boundary == "draft":
        banking.funding(save, publish, subject="draft", bank="draft-bank", posted=False,
                        day="2026-11-01")
    elif boundary in {"missing_role", "wrong_role"}:
        row = source(engine, "oct-statement-bank-a")
        sql = ("DELETE FROM entity_reference_recorded WHERE fact_id=?" if boundary == "missing_role"
               else "UPDATE entity_reference_recorded SET role='counterparty' WHERE fact_id=?")
        damage(engine, "entity_reference_recorded", sql, (row["fact_id"],))
    with monkeypatch.context() as complete:
        full_scope(complete)
        expected = public(engine, PERIOD)
    if boundary == "v1":
        with historical_content(1), Dashboard(engine)._snapshot(PERIOD) as snap:
            assert dashboard_funds._bank_identity_witness_heads(snap) is None
            assert len(FundsRead(snap).states) == 10
        return
    with Dashboard(engine)._snapshot(PERIOD) as snap:
        if boundary == "unowned":
            snap.reads = QueryReads(engine, snap.connection)
            snap.queries = BusinessQueries(engine, reads=snap.reads)
        elif boundary == "disabled":
            snap.reads._snapshot_active = False
        assert dashboard_funds._bank_identity_witness_heads(snap) is None
        read = FundsRead(snap)
        assert len(read.states) == 10
        read.account_summary()
        assert "draft-bank" not in {ident for _category, ident in read.account_rows}
    if boundary not in {"unowned", "disabled"}:
        assert public(engine, PERIOD) == expected


@pytest.mark.parametrize("change", ["publication", "calculation", "fact_seal", "scalar",
                                    "entity_type", "role_and_scalar"])
def test_unselected_damaged_candidate_cannot_disappear(bank_book, change):
    engine, _, _ = history(bank_book)
    row = source(engine, "oct-statement-bank-a")
    assert row["id"] not in states(engine)
    if change in {"publication", "calculation", "fact_seal"}:
        table, field, ident = {
            "publication": ("calculation_publication", "calculation_id", row["id"]),
            "calculation": ("calculation", "id", row["id"]),
            "fact_seal": ("fact_seal", "fact_id", row["fact_id"]),
        }[change]
        damage(engine, table, f"DELETE FROM {table} WHERE {field}=?", (ident,),
               foreign_keys=False)
    elif change == "entity_type":
        damage(engine, "entity", "UPDATE entity SET account_type='cash' WHERE id='bank-a'")
    else:
        damage(engine, "fact_bank_statement",
               "UPDATE fact_bank_statement SET bank_account_id='zero-bank' WHERE revision_id=?",
               (row["fact_id"],))
        if change == "role_and_scalar":
            damage(engine, "entity_reference_recorded",
                   "UPDATE entity_reference_recorded SET entity_id='zero-bank' WHERE fact_id=?",
                   (row["fact_id"],))
            # This unused old source cannot erase bank-a: its exact positive
            # witness remains independent. Full proof still rejects the source.
            assert public(engine, PERIOD)["funds"]["data"]["bank_account_count"] == 2
            with engine.store.connection(read_only=True) as connection, pytest.raises(KernelError):
                verify_integrity(engine, connection)
            return
    # The funds page consumes bank identity; brief money does not claim this
    # separate zero-account source proof. Its monetary sources stay complete.
    with pytest.raises(KernelError) as failure:
        Dashboard(engine).funds(PERIOD)
    assert failure.value.code == "content_integrity_failed"


def test_consumed_witness_body_still_rejects_self_consistent_mutation(bank_book):
    engine, _, _ = history(bank_book)
    row = source(engine)
    assert row["id"] in states(engine)
    from ai_accounting.kernel.types import canonical, digest

    outcome = json.loads(row["outcome"])
    outcome["values"]["bank_account_id"] = "zero-bank"
    damage(engine, "calculation", "UPDATE calculation SET outcome=?,digest=? WHERE id=?",
           (canonical(outcome), digest(outcome), row["id"]))
    with pytest.raises(KernelError) as failure:
        public(engine, PERIOD)
    assert failure.value.code == "content_integrity_failed"


def test_unused_old_body_is_not_a_display_proof_but_core_stays_complete(bank_book):
    engine, _, _ = history(bank_book)
    before = public(engine, PERIOD)
    row = source(engine, "oct-reconciliation-bank-a")
    assert row["id"] not in states(engine)
    damage(engine, "calculation", "UPDATE calculation SET outcome='{}' WHERE id=?", (row["id"],))
    assert public(engine, PERIOD) == before
    with engine.store.connection(read_only=True) as connection, pytest.raises(KernelError):
        verify_integrity(engine, connection)
    # Its own month requires substantive bank coverage and the exact body.
    with pytest.raises(KernelError):
        Dashboard(engine).funds("2026-10")


def test_all_open_and_month_bank_sources_keep_substantive_content(bank_book, monkeypatch):
    engine, save, publish = history(bank_book)
    banking.statement(save, publish, [], subject="nov-statement", month=PERIOD, initial=1000)
    banking.reconciliation(save, publish, [], subject="nov-reconciliation", month=PERIOD,
                           statement_id="nov-statement")
    narrow = public(engine, PERIOD)
    current = states(engine)
    assert {item["subject_id"] for item in current.values()} >= {
        "nov-statement", "nov-reconciliation", "statement-bank-a", "statement-zero-bank"
    }
    with monkeypatch.context() as complete:
        full_scope(complete)
        assert public(engine, PERIOD) == narrow
    row = source(engine, "nov-reconciliation")
    damage(engine, "calculation", "UPDATE calculation SET outcome='{}' WHERE id=?", (row["id"],))
    with pytest.raises(KernelError):
        public(engine, PERIOD)


@pytest.mark.parametrize("damage_head", ["redirected", "missing"])
def test_open_terminal_cannot_hide_behind_an_existing_frozen_account(bank_book, damage_head):
    engine, save, publish = history(bank_book)
    banking.statement(save, publish, [], subject="nov-statement", month=PERIOD, initial=1000)
    if damage_head == "redirected":
        old = source(engine, "nov-statement")
        banking.statement(save, publish, [], subject="nov-statement", month=PERIOD,
                          initial=1000, revision=1)
        assert old["id"] != source(engine, "nov-statement")["id"]
        damage(engine, "calculation_current",
               "UPDATE calculation_current SET calculation_id=? WHERE subject_id='nov-statement'",
               (old["id"],))
    else:
        damage(engine, "calculation_current",
               "DELETE FROM calculation_current WHERE subject_id='nov-statement'")
    with pytest.raises(KernelError) as failure:
        public(engine, PERIOD)
    assert failure.value.code == "content_integrity_failed"


def test_real_bank_identity_correction_keeps_complete_scope_and_former_accounts(
    bank_book, monkeypatch,
):
    engine, save, publish = history(bank_book)
    banking.funding(save, publish, subject="nov-funding", amount=15, day="2026-11-01")
    with engine.store.connection(read_only=True) as connection:
        data = engine.store.current_fact(connection, "nov-funding").fact.model_dump(mode="json")
    command = IdentityCorrections(engine)
    options = dict(changes=[dict(subject_id="nov-funding", expected_revision=1,
                                action="reassign", data={**data, "bank_account_id": "zero-bank"})],
                   evidence=[bank_book[3]], reason="explicit bank attribution correction")
    preview = command.preview_identity_correction(**options)
    command.confirm_identity_correction(**options, preview_digest=preview["digest"],
                                        epochs=preview["epochs"], request_id="bank-correction")
    with Dashboard(engine)._snapshot(PERIOD) as snap:
        assert dashboard_funds._bank_identity_witness_heads(snap) is None
        read = FundsRead(snap)
        assert len(read.states) == 10
        read.account_summary()
        assert {ident for category, ident in read.account_rows if category == "bank"} == {
            "bank-a", "zero-bank"
        }
    expected = public(engine, PERIOD)
    with monkeypatch.context() as complete:
        full_scope(complete)
        assert public(engine, PERIOD) == expected


def test_role_locator_work_does_not_scan_unrelated_facts_or_months(
    bank_book, record_property,
):
    engine, save, publish = prepared(bank_book)
    with engine.store.connection(read_only=True) as connection:
        ids = sorted(row[0] for row in connection.execute(
            "SELECT id FROM calculation WHERE kind IN ('bank_statement','bank_reconciliation')"
        ))
    parameters = (canonical(ids),)
    query = dashboard_funds._BANK_IDENTITY_SCALARS_SQL
    original = query.replace("INDEXED BY sqlite_autoindex_entity_reference_recorded_1 ", "")

    def locate(sql=query):
        with engine.store.connection(read_only=True) as connection:
            return [tuple(row) for row in connection.execute(sql, parameters)]

    before, expected = measure_work(engine, locate)
    old_before, old_expected = measure_work(engine, lambda: locate(original))
    assert expected == old_expected and len(expected) == 4
    for index in range(600):
        # Actual retained typed facts create the same role as the requested
        # banks, but are different business objects in twelve future months.
        banking.funding(save, publish, subject=f"unrelated-bank-role-{index}", posted=False,
                        amount=100 + index, day=f"2030-{index % 12 + 1:02d}-01")
    after, actual = measure_work(engine, locate)
    old_after, old_actual = measure_work(engine, lambda: locate(original))
    assert actual == old_actual == expected
    for counter in ("sql_calls", "returned_rows", "returned_value_bytes"):
        assert after["counters"][counter] == before["counters"][counter]
    assert after["counters"]["sqlite_vm_steps"] <= before["counters"]["sqlite_vm_steps"] + 100
    assert (old_after["counters"]["sqlite_vm_steps"]
            > old_before["counters"]["sqlite_vm_steps"] + 10_000)
    with engine.store.connection(read_only=True) as connection:
        plan = [row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + query, parameters)]
    assert any("sqlite_autoindex_entity_reference_recorded_1 (fact_id=?)" in row for row in plan)
    assert not any("entity_recorded_role" in row for row in plan)
    record_property("bank_role_locator_growth_work", json.dumps({
        "requested_ids": len(ids), "parameter": parameters[0], "future_facts": 600,
        "before": before["counters"], "after": after["counters"],
        "old_before": old_before["counters"], "old_after": old_after["counters"],
        "plan": plan,
    }, sort_keys=True))


def test_fact_driven_role_lookup_retains_wrong_path_and_duplicate_fallback(bank_book):
    engine, _, _ = prepared(bank_book)
    row = source(engine)
    damage(engine, "entity_reference_recorded",
           "INSERT INTO entity_reference_recorded SELECT fact_id,'wrong-bank-path',entity_id,"
           "role,kind,period,source_digest FROM entity_reference_recorded WHERE fact_id=?",
           (row["fact_id"],))
    with Dashboard(engine)._snapshot("2026-10") as snap:
        # Seeking only the expected path would incorrectly discard this
        # damaged candidate. The fact-prefix lookup still returns both paths.
        assert dashboard_funds._bank_identity_witness_heads(snap) is None
        found = list(snap.connection.execute(dashboard_funds._BANK_IDENTITY_SCALARS_SQL,
                                            (canonical([row["id"]]),)))
        assert len(found) == 2
        assert {item["path"] for item in found} == {"bank_account_id", "wrong-bank-path"}


def test_positive_witness_selector_reads_only_exact_posting_nodes(bank_book, monkeypatch):
    engine, _, _ = history(bank_book)
    original = BusinessQueries._selected_accounting
    calls = []

    def selected(self, connection, subject_id, period, **options):
        calls.append((set(subject_id), options.get("posting_period")))
        return original(self, connection, subject_id, period, **options)

    monkeypatch.setattr(BusinessQueries, "_selected_accounting", selected)
    with Dashboard(engine)._snapshot(PERIOD) as snap:
        heads = dashboard_funds._bank_identity_witness_heads(snap)
        assert {head["subject_id"] for head in heads} == {
            "opening-bank-a", "opening-zero-bank", "statement-bank-a", "statement-zero-bank",
        }
        read = FundsRead(snap)
        assert len(read.states) == 4
        assert set(snap.reads._close_accounting_slices) == {
            (head["posting_period"], frozenset(item["subject_id"] for item in heads))
            for head in heads
        }
    assert calls == [({head["subject_id"] for head in heads}, "2026-09")]


@pytest.mark.parametrize("damage_head", ["redirected", "missing"])
def test_independent_bank_start_cannot_hide_its_open_terminal(bank_book, damage_head):
    engine, save, publish, _ = bank_book
    banking.opening(save, publish)
    row = source(engine, "opening-bank-a")
    if damage_head == "redirected":
        # A real same-month no-impact review retains its typed start identity.
        from ai_accounting.kernel import engine as engine_module
        from unittest.mock import patch

        with patch.object(engine_module, "PROGRAM_VERSION", "synthetic-bank-start-review"):
            assert publish("opening-bank-a")["results"][0]["impact"] == "review_no_impact"
        assert source(engine, "opening-bank-a")["id"] != row["id"]
        damage(engine, "calculation_current",
               "UPDATE calculation_current SET calculation_id=? WHERE subject_id='opening-bank-a'",
               (row["id"],))
    else:
        damage(engine, "calculation_current",
               "DELETE FROM calculation_current WHERE subject_id='opening-bank-a'")
    with pytest.raises(KernelError) as failure:
        Dashboard(engine).funds("2026-09")
    assert failure.value.code == "content_integrity_failed"


def test_exact_identity_nodes_do_not_expand_with_empty_frozen_months(
    bank_book, monkeypatch, record_property,
):
    # Only two accounts / empty monthly bank statements and reconciliations.
    # Established accounts require these actual states even with no new money.
    # This is a posting-node scope
    # regression over real freezes, not the 12/48/120 owner workload or latency.
    engine, save, publish = history(bank_book)
    first = YearMonth("2026-09").ordinal
    prepared_months = 2
    samples = []

    def selected_states(period):
        with Dashboard(engine)._snapshot(period) as snap:
            assert snap.connection.execute("SELECT max(period) FROM period_close").fetchone()[0] \
                == snap.month - 1
            read = FundsRead(snap)
            return {
                "states": {ident: (row["subject_id"], row["fact_id"], row["result_digest"])
                           for ident, row in read.states.items()},
                "accounting_periods": sorted({key[0] for key in snap.reads._close_accounting_slices}),
            }

    for months in (12, 48, 120):
        while prepared_months < months:
            month = str(YearMonth.from_ordinal(first + prepared_months))
            for bank, initial in (("bank-a", 1000), ("zero-bank", 0)):
                statement = "idle-statement-" + month + "-" + bank
                banking.statement(save, publish, [], subject=statement, bank=bank,
                                  month=month, initial=initial)
                banking.reconciliation(save, publish, [],
                                       subject="idle-reconciliation-" + month + "-" + bank,
                                       statement_id=statement, bank=bank, month=month)
            # Inventory is recorded before freezing this very month. No later
            # inventory is used to reconstruct or affirm a frozen source.
            banking.close_month(banking.inventories(engine, bank_book[3], month, {"bank"}),
                               bank_book[3], month)
            prepared_months += 1
        period = str(YearMonth.from_ordinal(first + months))
        banking.inventories(engine, bank_book[3], period, set())
        narrow_work, narrow = measure_work(engine, lambda: selected_states(period))
        with monkeypatch.context() as complete:
            full_scope(complete)
            full_work, full = measure_work(engine, lambda: selected_states(period))
        typed_states = 2 + months * 4
        assert len(narrow["states"]) == 4 and len(full["states"]) == typed_states
        assert all(full["states"][ident] == value for ident, value in narrow["states"].items())
        assert narrow["accounting_periods"] == [first]
        assert full["accounting_periods"] == list(range(first, first + months))
        for counter in ("returned_value_bytes", "calculation_result_json_decodes"):
            assert narrow_work["counters"].get(counter, 0) < full_work["counters"].get(counter, 0)
        role = [row for row in narrow_work["sql"]
                if row["statement"] == " ".join(dashboard_funds._BANK_IDENTITY_SCALARS_SQL.split())]
        assert len(role) == 1 and role[0]["calls"] == 1 and role[0]["returned_rows"] == typed_states
        samples.append({
            "frozen_months": months, "typed_states": typed_states, "accounts": 2,
            "narrow": narrow_work["counters"], "complete": full_work["counters"],
            "narrow_accounting_periods": narrow["accounting_periods"],
            "complete_accounting_period_count": len(full["accounting_periods"]),
            "role_lookup": {key: role[0].get(key, 0) for key in
                            ("calls", "returned_rows", "returned_value_bytes", "sqlite_vm_steps")},
        })
    with engine.store.connection(read_only=True) as connection:
        ids = sorted(row[0] for row in connection.execute(
            "SELECT id FROM calculation WHERE kind IN "
            "('bank_opening','bank_statement','bank_reconciliation')"
        ))
        plan = [row[3] for row in connection.execute(
            "EXPLAIN QUERY PLAN " + dashboard_funds._BANK_IDENTITY_SCALARS_SQL,
            (canonical(ids),),
        )]
    assert any("sqlite_autoindex_entity_reference_recorded_1 (fact_id=?)" in row for row in plan)
    assert not any("entity_recorded_role" in row for row in plan)
    record_property("bank_exact_witness_node_growth", json.dumps({
        "scope": "two actual bank accounts; real monthly empty bank states and freezes",
        "samples": samples, "role_plan": plan,
    }, sort_keys=True))
