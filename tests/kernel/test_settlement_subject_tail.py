"""Subject hydration keeps full tail authentication and exact obligation semantics."""

import sqlite3

import pytest
from test_settlement_late_reviews import corrupt, forge_obligation_identity, prepared, review
from test_settlement_period_scopes import allocation, cash_payment, setup

from ai_accounting.kernel import settlement_freeze as freeze
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.domains.labor_assets import LaborProjectCost
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.settlement_projection import settlement_summary
from ai_accounting.kernel.types import YearMonth


def _cost(period, project, amount=24000):
    return LaborProjectCost(
        period=YearMonth(period), person_id="designer", project_id=project,
        gross_fee_fen=amount, tax_treatment="not_withheld_not_filed",
        capitalization_conditions_confirmed=True,
    )


def _company(tmp_path):
    company = setup(tmp_path)
    company.save(_cost("2026-01", "other-project"), "other")
    company.publish("other")
    company.close("2026-01")
    company.save(
        cash_payment("2026-02", 100000, allocation("labor_project_cost", "cost", "net", 100000)),
        "target-payment",
    )
    company.save(
        cash_payment("2026-02", 2000, allocation("labor_project_cost", "other", "net", 2000)),
        "other-payment",
    )
    company.publish("target-payment", "other-payment")
    for index in range(4):
        subject = f"unrelated-{index}"
        company.save(_cost("2026-02", f"unrelated-project-{index}"), subject)
        company.publish(subject)
    return company


def _wide(monkeypatch, connection, period, **options):
    original = freeze._scope

    def full_scope(connection, period, **kwargs):
        kwargs.pop("subject_ids", None)
        return original(connection, period, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(freeze, "_scope", full_scope)
        return settlement_summary(connection, period, **options)


def test_subject_tail_matches_independent_sql_and_wide_roots(
    tmp_path, monkeypatch, record_property,
):
    company = _company(tmp_path)
    for period in ("2026-01", "2026-02"):
        for current in (False, True):
            for counts in (False, True):
                for subjects in ({"cost"}, {"target-payment"}, {"cost", "other"}):
                    options = {
                        "subject_ids": subjects, "current": current,
                        "include_history_counts": counts,
                    }
                    with QueryReads.snapshot(company.engine) as reads:
                        actual = settlement_summary(
                            reads.connection, period, reads=reads, **options,
                        )
                        # A limited read cannot populate maps whose scope is global.
                        assert reads._frozen_settlement_scopes == {}
                        assert reads._frozen_settlement_tails == {}
                    with QueryReads.snapshot(company.engine) as reads:
                        expected = _wide(
                            monkeypatch, reads.connection, period, reads=reads, **options,
                        )
                    with QueryReads.snapshot(company.engine) as reads:
                        with monkeypatch.context() as patch:
                            patch.setattr(freeze, "frozen_subject_summary", lambda *a, **kw: None)
                            independent = settlement_summary(
                                reads.connection, period, reads=reads, **options,
                            )
                    assert actual == expected == independent
    with QueryReads.snapshot(company.engine) as reads:
        scope = freeze._scope(reads.connection, "2026-02", current=False, reads=reads)
        wide_rows = len(scope.tail_rows)
        wide_keys = len(scope.overrides)
        expected_rows = reads.connection.execute(
            "SELECT count(*) FROM settlement_change WHERE posting_period=? "
            "AND (source_subject_id='cost' OR obligation_key='labor_project_cost:cost:net')",
            (YearMonth("2026-02").ordinal,),
        ).fetchone()[0]
    with QueryReads.snapshot(company.engine) as reads:
        narrow = freeze._scope(
            reads.connection, "2026-02", current=False, reads=reads, subject_ids={"cost"},
        )
        assert len(narrow.tail_rows) == expected_rows == 1
        assert set(narrow.overrides) == {"labor_project_cost:cost:net"}
        assert wide_rows > len(narrow.tail_rows) and wide_keys > len(narrow.overrides)
        actual = settlement_summary(
            reads.connection, "2026-02", subject_ids={"cost"}, reads=reads,
        )
        assert actual["obligations"][0]["paid_fen"] == 100000
        assert actual["obligations"][0]["remaining_fen"] == 700000
    record_property("wide_tail_rows", wide_rows)
    record_property("selected_tail_rows", expected_rows)
    record_property("wide_override_keys", wide_keys)
    record_property("selected_override_keys", 1)


@pytest.mark.parametrize("selected", [False, True])
def test_subject_state_damage_has_selected_boundary_and_full_repair(tmp_path, selected):
    company = _company(tmp_path)
    with company.engine.store.connection(read_only=True) as connection:
        expected = settlement_summary(connection, "2026-02", subject_ids={"cost"})
    key = "labor_project_cost:" + ("cost" if selected else "other") + ":net"
    with company.engine.store.connection() as connection:
        connection.execute(
            "UPDATE settlement_state_revision SET payload='{}' WHERE obligation_key=?", (key,),
        )
    with company.engine.store.connection(read_only=True) as connection:
        if selected:
            with pytest.raises(KernelError, match="冻结清偿依据不匹配"):
                settlement_summary(connection, "2026-02", subject_ids={"cost"})
        else:
            assert settlement_summary(connection, "2026-02", subject_ids={"cost"}) == expected
        with pytest.raises(KernelError, match="冻结清偿依据不匹配"):
            freeze.require_frozen_settlement_projection(company.engine, connection)
    repaired = company.engine.rebuild_projections(request_id="repair-subject-state")
    assert repaired["changed"] is True
    with company.engine.store.connection(read_only=True) as connection:
        freeze.require_frozen_settlement_projection(company.engine, connection)
        assert settlement_summary(connection, "2026-02", subject_ids={"cost"}) == expected


def test_subject_tail_still_rejects_unrelated_source_seal_and_missing_rows(tmp_path):
    company = _company(tmp_path)
    with company.engine.store.connection() as connection:
        for statement in (
            "UPDATE settlement_projection_seal SET digest=zeroblob(32) "
            "WHERE posting_period=(SELECT max(posting_period) FROM settlement_projection_seal)",
            "UPDATE settlement_change SET source_digest=zeroblob(32) "
            "WHERE source_subject_id='unrelated-0'",
            "DELETE FROM settlement_change WHERE source_subject_id='unrelated-0'",
        ):
            connection.execute("SAVEPOINT damage")
            connection.execute(statement)
            with pytest.raises(KernelError) as damaged:
                settlement_summary(connection, "2026-01", subject_ids={"cost"}, current=True)
            assert damaged.value.code == "content_integrity_failed"
            connection.execute("ROLLBACK TO damage")
            connection.execute("RELEASE damage")


def test_tail_selection_retains_cross_subject_negative_unknown_and_direct_events():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        "CREATE TABLE settlement_change(publication_id TEXT,item_no INTEGER,"
        "posting_period INTEGER,obligation_key TEXT,source_subject_id TEXT,"
        "change_kind TEXT,amount INTEGER,state TEXT,source_calculation_id TEXT);"
        "CREATE TABLE calculation_publication(id TEXT,sequence INTEGER,calculation_id TEXT);"
        "CREATE TABLE calculation(id TEXT,kind TEXT,fact_id TEXT);"
    )
    rows = [
        ("source", 1, 1, "k", "old-owner", "source", 100, "resolved", "c"),
        ("reassigned", 1, 2, "k", "new-owner", "source", -100, "resolved", "c"),
        ("payment", 1, 2, "k", None, "payment", 30, "resolved", "c"),
        ("reversal", 1, 3, "k", None, "payment", -30, "resolved", "c"),
        ("unknown", 1, 3, "k", "another", "other", None, "unresolved", "c"),
        ("direct", 1, 3, None, "old-owner", "payment", None, "unresolved", "c"),
        ("cross-key", 1, 3, "other-key", "old-owner", "other", 4, "resolved", "c"),
        ("unrelated", 1, 3, "discarded", "another", "source", 50, "resolved", "c"),
    ]
    connection.executemany("INSERT INTO settlement_change VALUES(?,?,?,?,?,?,?,?,?)", rows)
    connection.executemany(
        "INSERT INTO calculation_publication VALUES(?,?,?)",
        [(row[0], index, "c") for index, row in enumerate(rows)],
    )
    connection.execute("INSERT INTO calculation VALUES('c','labor_project_cost','f')")
    selected = freeze._read_tail_rows(
        connection, {1, 2, 3}, subject_ids={"old-owner"}, obligation_keys={"k"},
    )
    assert [row["publication_id"] for row in selected] == [row[0] for row in rows[:-1]]
    state = freeze._empty_state("k")
    for row in selected:
        if row["obligation_key"] == "k":
            row["source_digest"] = b"x" * 32
            row.update(category=None, account=None, counterparty_id=None, component=None)
            freeze._apply(state, row, row["posting_period"])
    assert state["source_amount"] == 0
    assert state["paid"] == 0
    assert state["bad_other"] is True
    assert freeze._remaining(state) is None
    assert state["movement_count"] == 3
    assert state["source_subject_id"] == "new-owner"
    connection.close()


def test_subject_cutoff_before_latest_close_and_global_superset(tmp_path, monkeypatch):
    company = _company(tmp_path)
    company.close("2026-02")
    company.save(
        cash_payment("2026-03", 80000, allocation("labor_project_cost", "cost", "net", 80000)),
        "march-payment",
    )
    company.publish("march-payment")
    for period in ("2026-01", "2026-02", "2026-03"):
        for current in (False, True):
            for counts in (False, True):
                options = {
                    "subject_ids": {"cost"}, "current": current,
                    "include_history_counts": counts,
                }
                with company.engine.store.connection(read_only=True) as connection:
                    connection.execute("BEGIN")
                    actual = settlement_summary(connection, period, **options)
                    assert actual == _wide(monkeypatch, connection, period, **options)
                    with monkeypatch.context() as patch:
                        patch.setattr(freeze, "frozen_subject_summary", lambda *a, **kw: None)
                        assert actual == settlement_summary(connection, period, **options)
    with QueryReads.snapshot(company.engine) as reads:
        full = freeze._scope(reads.connection, "2026-01", current=True, reads=reads)
        before = dict(reads._frozen_settlement_scopes)
        selected = freeze._scope(
            reads.connection, "2026-01", current=True, reads=reads, subject_ids={"cost"},
        )
        assert selected is full
        assert reads._frozen_settlement_scopes == before
        assert selected.base_period == YearMonth("2026-02").ordinal


def test_open_source_correction_and_withdrawal_match_complete_reducer(tmp_path, monkeypatch):
    company = setup(tmp_path)
    company.close("2026-01")
    company.save(_cost("2026-02", "corrected-project"), "corrected")
    company.publish("corrected")

    def check(amount):
        for counts in (False, True):
            with company.engine.store.connection(read_only=True) as connection:
                connection.execute("BEGIN")
                options = {"subject_ids": {"corrected"}, "include_history_counts": counts}
                actual = settlement_summary(connection, "2026-02", **options)
                assert actual == _wide(monkeypatch, connection, "2026-02", **options)
                with monkeypatch.context() as patch:
                    patch.setattr(freeze, "frozen_subject_summary", lambda *a, **kw: None)
                    assert actual == settlement_summary(connection, "2026-02", **options)
                if amount is None:
                    assert actual["obligations"] == []
                else:
                    assert actual["obligations"][0]["source_amount_fen"] == amount

    check(24000)
    company.save(_cost("2026-02", "corrected-project", 26000), "corrected", revision=1)
    company.publish("corrected")
    check(26000)
    plan = company.engine.preview_delete("corrected")
    company.engine.delete(
        "corrected", preview_digest=plan["digest"], epochs=plan["epochs"],
        request_id=company.request(),
    )
    check(None)


@pytest.mark.parametrize("damage", ["mode", "meaning", "seal"])
def test_subject_tail_retains_late_review_checks(tmp_path, monkeypatch, damage):
    company = prepared(tmp_path)
    identifier = review(company, monkeypatch, 1)
    with company.engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        for counts in (False, True):
            options = {
                "subject_ids": {"january"}, "current": True,
                "include_history_counts": counts,
            }
            actual = settlement_summary(connection, "2026-01", **options)
            assert actual == _wide(monkeypatch, connection, "2026-01", **options)
    if damage == "meaning":
        corrupt(company, lambda connection: forge_obligation_identity(connection, identifier))
    elif damage == "mode":
        corrupt(company, lambda connection: connection.execute(
            "UPDATE calculation_publication SET mode='open_replace' WHERE calculation_id=?",
            (identifier,),
        ))
    else:
        corrupt(company, lambda connection: connection.execute(
            "DELETE FROM settlement_projection_seal WHERE posting_period=?",
            (YearMonth("2026-01").ordinal,),
        ))
    with company.engine.store.connection(read_only=True) as connection:
        with pytest.raises(KernelError) as rejected:
            settlement_summary(connection, "2026-01", subject_ids={"january"}, current=True)
        assert rejected.value.code == "content_integrity_failed"
        if damage != "seal":
            assert rejected.value.details["reason"] == (
                "late_review_accounting_mismatch" if damage == "meaning"
                else "published_into_frozen_period"
            )


def test_subject_tail_reuses_authenticated_root_and_selected_blocks(
    tmp_path, monkeypatch, record_property,
):
    company = _company(tmp_path)
    roots, blocks = [], []
    original_root, original_block = freeze._read_root, freeze._read_block

    def root_read(connection, period):
        roots.append(period)
        return original_root(connection, period)

    def block_read(connection, period, header):
        blocks.append((period, tuple(header)))
        return original_block(connection, period, header)

    with QueryReads.snapshot(company.engine) as reads:
        # The ordinary global reader actually authenticates the January root;
        # its scope cannot supply February's selected result or its open tail.
        freeze.frozen_followup_summary(reads.connection, "2026-01", reads=reads)
        assert ("2026-02", False) not in reads._frozen_settlement_scopes
        monkeypatch.setattr(freeze, "_read_root", root_read)
        monkeypatch.setattr(freeze, "_read_block", block_read)
        actual = settlement_summary(
            reads.connection, "2026-02", subject_ids={"cost"}, reads=reads,
        )
        assert actual["obligations"][0]["remaining_fen"] == 700000
        assert reads._frozen_settlement_tails == {}
        assert ("2026-02", False) not in reads._frozen_settlement_scopes
    record_property("additional_root_reads", len(roots))
    record_property("selected_block_reads", len(blocks))
    record_property("unique_selected_blocks", len(set(blocks)))
    assert roots == []
    assert len(blocks) == len(set(blocks)) == 1
