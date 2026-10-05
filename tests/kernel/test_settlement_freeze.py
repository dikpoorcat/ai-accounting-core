"""Frozen settlement reads retain both historical and current posting semantics."""

import hashlib
import sqlite3
from types import SimpleNamespace

import pytest
from test_settlement_period_scopes import allocation, cash_payment, setup

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.domains.cash import CashFunding
from ai_accounting.kernel.maintenance import Maintenance
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.settlement_freeze import (
    FrozenScope,
    _empty_state,
    _page_entries,
    require_frozen_settlement_projection,
)
from ai_accounting.kernel.settlement_projection import (
    settlement_dashboard_open,
    settlement_followup_summary,
    settlement_position_rows,
    settlement_summary,
)
from ai_accounting.kernel.types import YearMonth, canonical


def _legacy(monkeypatch, function, *args, **kwargs):
    module = __import__("ai_accounting.kernel.settlement_freeze", fromlist=["sentinel"])
    target = {
        settlement_dashboard_open: "frozen_dashboard_open",
        settlement_followup_summary: "frozen_followup_summary",
        settlement_position_rows: "frozen_position_rows",
        settlement_summary: "frozen_subject_summary",
    }[function]
    with monkeypatch.context() as scoped:
        scoped.setattr(module, target, lambda *unused_args, **unused_kwargs: None)
        return function(*args, **kwargs)


def test_frozen_settlement_matches_full_projection_before_and_after_later_payment(
    tmp_path, monkeypatch
):
    company = setup(tmp_path)
    company.close("2026-01")
    company.save(
        cash_payment(
            "2026-02", 800000,
            allocation("labor_project_cost", "cost", "net", 800000),
        ),
        "balance-paid",
    )
    company.publish("balance-paid")
    with company.engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        for period in ("2026-01", "2026-02"):
            for current in (False, True):
                arguments = (connection, period)
                options = {"current": current}
                expected = _legacy(
                    monkeypatch, settlement_dashboard_open, *arguments,
                    limit=1, **options,
                )
                assert settlement_dashboard_open(*arguments, limit=1, **options) == expected
                if expected["page"]["has_more"]:
                    cursor = expected["page"]["next_cursor"]
                    expected_next = _legacy(
                        monkeypatch, settlement_dashboard_open, *arguments,
                        after=cursor, limit=1, **options,
                    )
                    assert settlement_dashboard_open(
                        *arguments, after=cursor, limit=1, **options
                    ) == expected_next
                expected_followup = _legacy(
                    monkeypatch, settlement_followup_summary, *arguments, **options
                )
                assert settlement_followup_summary(*arguments, **options) == expected_followup
                expected_compact = _legacy(
                    monkeypatch, settlement_dashboard_open, *arguments,
                    limit=1, summary_only=True, **options,
                )
                assert settlement_dashboard_open(
                    *arguments, limit=1, summary_only=True, **options
                ) == expected_compact
                if current:
                    historical = _legacy(
                        monkeypatch, settlement_dashboard_open, *arguments, limit=10
                    )
                    selected = {item["key"] for item in historical["obligations"]}
                    expected_page = _legacy(
                        monkeypatch, settlement_dashboard_open, *arguments,
                        current=True, page_keys=selected, include_settled_page=True,
                        limit=10,
                    )
                    assert settlement_dashboard_open(
                        *arguments, current=True, page_keys=selected,
                        include_settled_page=True, limit=10,
                    ) == expected_page
            expected_position = _legacy(
                monkeypatch, settlement_position_rows, connection, period,
                {"1122", "1123", "2202", "2211", "2241"},
            )
            actual_position = settlement_position_rows(
                connection, period, {"1122", "1123", "2202", "2211", "2241"}
            )
            def order(item):
                return item["account"], item["category"], item["counterparty_id"] or ""
            assert sorted(actual_position, key=order) == sorted(expected_position, key=order)
        assert require_frozen_settlement_projection(company.engine, connection)["periods"] == 1


def test_open_tail_replay_is_shared_only_for_equal_historical_and_current_scope(
    tmp_path, monkeypatch
):
    company = setup(tmp_path)
    company.close("2026-01")
    import ai_accounting.kernel.settlement_freeze as freeze

    original_read_root = freeze._read_root
    original_groups = freeze._groups_from_root
    original_change_counts = freeze._change_period_counts
    roots = []
    aggregate_work = []

    def counted_root(connection, period):
        roots.append(period)
        return original_read_root(connection, period)

    def counted_groups(root):
        aggregate_work.append(("groups", len(root["groups"])))
        return original_groups(root)

    def counted_changes(root):
        aggregate_work.append(("change_periods", len(root["change_period_counts"])))
        return original_change_counts(root)

    with monkeypatch.context() as scoped:
        scoped.setattr(freeze, "_read_root", counted_root)
        scoped.setattr(freeze, "_groups_from_root", counted_groups)
        scoped.setattr(freeze, "_change_period_counts", counted_changes)
        with QueryReads.snapshot(company.engine) as reads:
            current = settlement_followup_summary(
                reads.connection, "2026-01", current=True, reads=reads
            )
            historical = settlement_followup_summary(
                reads.connection, "2026-01", reads=reads
            )
            assert current == {**historical, "current_cutoff_period": "2026-01"}
            assert reads._frozen_settlement_scopes["2026-01", True].groups is (
                reads._frozen_settlement_scopes["2026-01", False].groups
            )
            root = reads._frozen_settlement_scopes["2026-01", True].base_root
            assert aggregate_work == [
                ("groups", len(root["groups"])),
                ("change_periods", len(root["change_period_counts"])),
            ]
        assert roots == [YearMonth("2026-01").ordinal]
    company.save(
        cash_payment(
            "2026-02", 400000,
            allocation("labor_project_cost", "cost", "net", 400000),
        ),
        "balance-paid",
    )
    company.publish("balance-paid")
    aggregate_work.clear()
    with monkeypatch.context() as scoped, QueryReads.snapshot(company.engine) as reads:
        scoped.setattr(freeze, "_groups_from_root", counted_groups)
        scoped.setattr(freeze, "_change_period_counts", counted_changes)
        historical = settlement_followup_summary(
            reads.connection, "2026-02", reads=reads
        )
        current = settlement_followup_summary(
            reads.connection, "2026-02", current=True, reads=reads
        )
        scopes = reads._frozen_settlement_scopes
        assert scopes["2026-02", False].groups is scopes["2026-02", True].groups
        assert len(reads._frozen_settlement_tails) == 1
        assert reads._frozen_settlement_blocks
        assert historical["remaining_fen"] == current["remaining_fen"]
        assert historical["paid_fen"] == current["paid_fen"]
        root = scopes["2026-02", False].base_root
        assert aggregate_work == [
            ("groups", len(root["groups"])),
            ("change_periods", len(root["change_period_counts"])),
        ]
    company.save(
        cash_payment(
            "2026-03", 400000,
            allocation("labor_project_cost", "cost", "net", 400000),
        ),
        "later-payment",
    )
    company.publish("later-payment")
    with QueryReads.snapshot(company.engine) as reads:
        historical = settlement_followup_summary(
            reads.connection, "2026-02", reads=reads
        )
        current = settlement_followup_summary(
            reads.connection, "2026-02", current=True, reads=reads
        )
        scopes = reads._frozen_settlement_scopes
        assert scopes["2026-02", False].groups is not scopes["2026-02", True].groups
        assert current["paid_fen"] - historical["paid_fen"] == 400000


def test_scoped_frozen_summary_matches_verified_historical_and_current_sql(tmp_path, monkeypatch):
    import ai_accounting.kernel.settlement_projection as projection

    company = setup(tmp_path)
    company.close("2026-01")
    with QueryReads.snapshot(company.engine) as reads:
        expected = _legacy(
            monkeypatch, settlement_summary, reads.connection, "2026-01",
            subject_ids={"cost"}, current=True,
        )
        original = projection._summary_relation
        calls = []

        def counted(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        with monkeypatch.context() as scoped:
            scoped.setattr(projection, "_summary_relation", counted)
            settlement_summary(reads.connection, "2026-01", subject_ids={"cost"}, reads=reads)
            current = settlement_summary(
                reads.connection, "2026-01", subject_ids={"cost"}, current=True,
                reads=reads,
            )
        assert current == expected
        assert len(calls) == 0
    company.save(
        cash_payment(
            "2026-02", 400000,
            allocation("labor_project_cost", "cost", "net", 400000),
        ),
        "later-payment",
    )
    company.publish("later-payment")
    with QueryReads.snapshot(company.engine) as reads:
        expected = _legacy(
            monkeypatch, settlement_summary, reads.connection, "2026-01",
            subject_ids={"cost"}, current=True,
        )
        calls = []
        with monkeypatch.context() as scoped:
            scoped.setattr(projection, "_summary_relation", counted)
            historical = settlement_summary(
                reads.connection, "2026-01", subject_ids={"cost"}, reads=reads
            )
            current = settlement_summary(
                reads.connection, "2026-01", subject_ids={"cost"}, current=True,
                reads=reads,
            )
        assert current == expected
        assert current["obligations"] != historical["obligations"]
        assert len(calls) == 0


def test_scoped_current_still_checks_unrelated_future_publication_period(tmp_path):
    company = setup(tmp_path)
    company.close("2026-01")
    company.save(
        CashFunding(
            period=YearMonth("2026-02"), actual_date="2026-02-15",
            owner_id="owner", cash_account_id="cash", funding_kind="capital",
            amount_fen=100000,
        ),
        "future-capital",
    )
    company.publish("future-capital")
    with company.engine.store.connection() as connection:
        period = YearMonth("2026-02").ordinal
        assert connection.execute(
            "SELECT 1 FROM settlement_projection_seal WHERE posting_period=?", (period,)
        ).fetchone()
        connection.execute(
            "UPDATE settlement_projection_seal SET digest=zeroblob(32) "
            "WHERE posting_period=?", (period,)
        )
    with QueryReads.snapshot(company.engine) as reads:
        settlement_summary(reads.connection, "2026-01", subject_ids={"cost"}, reads=reads)
        with pytest.raises(KernelError) as damaged:
            settlement_summary(
                reads.connection, "2026-01", subject_ids={"cost"}, current=True,
                reads=reads,
            )
        assert damaged.value.code == "content_integrity_failed"


def test_scoped_publication_proof_is_reused_only_in_one_snapshot(tmp_path, monkeypatch):
    import ai_accounting.kernel.publication as publication

    company = setup(tmp_path)
    company.close("2026-01")
    with QueryReads.snapshot(company.engine) as reads:
        historical = settlement_summary(
            reads.connection, "2026-01", subject_ids={"cost"}, reads=reads,
        )
        assert reads._verified_publication_ids
        with monkeypatch.context() as scoped:
            scoped.setattr(
                publication, "verify_record",
                lambda *_: pytest.fail("already verified publication was rehashed"),
            )
            current = settlement_summary(
                reads.connection, "2026-01", subject_ids={"cost"}, current=True,
                reads=reads,
            )
        assert current["obligations"] == historical["obligations"]
    assert reads._verified_publication_ids == {}


def test_subject_proof_matches_sql_across_two_closes_and_open_tail(tmp_path, monkeypatch):
    company = setup(tmp_path)
    company.close("2026-01")
    company.save(
        cash_payment(
            "2026-02", 400000,
            allocation("labor_project_cost", "cost", "net", 400000),
        ),
        "second-payment",
    )
    company.publish("second-payment")
    company.close("2026-02")
    company.save(
        cash_payment(
            "2026-03", 400000,
            allocation("labor_project_cost", "cost", "net", 400000),
        ),
        "third-payment",
    )
    company.publish("third-payment")
    with QueryReads.snapshot(company.engine) as reads:
        for period in ("2026-01", "2026-02", "2026-03"):
            for current in (False, True):
                for subjects in ({"advance"}, {"cost"}, {"advance", "cost"}, {"none"}):
                    kwargs = {"subject_ids": subjects, "current": current, "reads": reads}
                    expected = _legacy(
                        monkeypatch, settlement_summary, reads.connection, period, **kwargs,
                    )
                    assert settlement_summary(reads.connection, period, **kwargs) == expected
        assert require_frozen_settlement_projection(company.engine, reads.connection)[
            "periods"
        ] == 2


def test_scoped_summary_rejects_missing_hit_subject_chunk(tmp_path):
    import json

    company = setup(tmp_path)
    company.close("2026-01")
    with QueryReads.snapshot(company.engine) as reads:
        assert settlement_summary(
            reads.connection, "2026-01", subject_ids={"cost"}, reads=reads,
        )["obligations"]
    with company.engine.store.connection() as connection:
        root = json.loads(connection.execute(
            "SELECT root_json FROM settlement_freeze_root"
        ).fetchone()[0])
        header = root["subject_directory"][0]
        leaf = json.loads(connection.execute(
            "SELECT payload FROM settlement_freeze_block WHERE digest=unhex(?)",
            (header[4],),
        ).fetchone()[0])
        subject = next(item for item in leaf if item[0] == "cost")
        connection.execute(
            "DELETE FROM settlement_freeze_block WHERE digest=unhex(?)",
            (subject[1][0][1],),
        )
    with QueryReads.snapshot(company.engine) as reads:
        with pytest.raises(KernelError) as damaged:
            settlement_summary(
                reads.connection, "2026-01", subject_ids={"cost"}, reads=reads,
            )
    assert damaged.value.code == "content_integrity_failed"
    assert company.engine.rebuild_projections(request_id="repair-subject-chunk")[
        "changed"
    ] is True
    with company.engine.store.connection(read_only=True) as connection:
        assert settlement_summary(connection, "2026-01", subject_ids={"cost"})[
            "obligations"
        ]
        require_frozen_settlement_projection(company.engine, connection)


def test_complete_freeze_verification_rejects_deleted_source_contribution(tmp_path):
    company = setup(tmp_path)
    company.close("2026-01")
    with company.engine.store.connection() as connection:
        connection.execute(
            "DELETE FROM settlement_change WHERE obligation_key=? "
            "AND change_kind='source'",
            ("labor_project_cost:cost:net",),
        )
    with company.engine.store.connection(read_only=True) as connection:
        with pytest.raises(KernelError) as damaged:
            require_frozen_settlement_projection(company.engine, connection)
    assert damaged.value.code == "content_integrity_failed"


def test_scoped_summary_rejects_modified_hit_publication(tmp_path):
    company = setup(tmp_path)
    company.close("2026-01")
    with company.engine.store.connection() as connection:
        connection.execute("DROP TRIGGER immutable_calculation_publication_UPDATE")
        connection.execute(
            "UPDATE calculation_publication SET mode='open_replace' "
            "WHERE subject_id='cost'"
        )
        with pytest.raises(KernelError) as damaged:
            settlement_summary(
                connection, "2026-01", subject_ids={"cost"},
            )
    assert damaged.value.code == "content_integrity_failed"


def test_subject_proof_keeps_identity_reassignment_and_direct_keyless_movements(monkeypatch):
    import ai_accounting.kernel.settlement_freeze as freeze

    jan = YearMonth("2026-01").ordinal
    feb = YearMonth("2026-02").ordinal
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE settlement_freeze_block(digest BLOB,payload TEXT)")
    connection.execute(
        "CREATE TABLE settlement_state_revision(digest BLOB,obligation_key TEXT,payload TEXT)"
    )
    connection.execute("CREATE TABLE calculation(id TEXT,digest BLOB)")
    connection.executemany(
        "INSERT INTO calculation VALUES(?,?)",
        (("c-old", bytes.fromhex("00" * 32)), ("c-new", bytes.fromhex("11" * 32))),
    )

    def block(items):
        payload = canonical(items)
        value = freeze._sha(payload)
        connection.execute("INSERT INTO settlement_freeze_block VALUES(?,?)", (value, payload))
        return value.hex()

    def revision(state):
        payload = canonical(state)
        value = freeze._sha(payload)
        connection.execute(
            "INSERT INTO settlement_state_revision VALUES(?,?,?)",
            (value, state["obligation_key"], payload),
        )
        return value.hex()

    old = _empty_state("k")
    old.update(
        first_source_period=jan, last_change_period=jan, source_event_count=1,
        source_amount=100, source_subject_id="former",
        events=[["p-old", 1, "c-old", "00" * 32]],
    )
    old_digest = revision(old)
    latest = dict(old)
    latest.update(
        last_change_period=feb, source_event_count=2, source_amount=200,
        source_subject_id="successor", previous_revision=old_digest,
        events=[["p-new", 1, "c-new", "11" * 32]],
    )
    latest_digest = revision(latest)
    all_leaf = [["k", latest_digest, jan]]
    all_header = [0, "k", "k", 1, block(all_leaf)]
    former_sources = [jan, block(["k"]), 1]
    successor_sources = [feb, block(["k"]), 1]
    former_events = [jan, block([
        ["p-old", 1, "k", "source", "resolved"],
        ["p-null", 1, None, "payment", "unresolved"],
    ]), 2]
    former_later = [feb, block([
        ["p-cross", 1, "other-key", "payment", "resolved"],
    ]), 1]
    successor_events = [feb, block([
        ["p-new", 1, "k", "source", "resolved"],
    ]), 1]
    subjects = [
        ["former", [former_sources], [former_events, former_later]],
        ["successor", [successor_sources], [successor_events]],
    ]
    subject_header = [0, "former", "successor", 2, block(subjects)]
    scope = FrozenScope(
        cutoff=jan, through=feb, base_period=feb,
        base_root={
            "subject_directory": [subject_header],
            "directories": {"all": [all_header]},
        },
        groups={}, period_amounts={}, overrides={}, current=True,
    )
    monkeypatch.setattr(freeze, "_scope", lambda *args, **kwargs: scope)
    monkeypatch.setattr(
        freeze, "_verified_publication_sources", lambda *args, **kwargs: None
    )
    former = freeze.frozen_subject_summary(
        connection, "2026-01", subject_ids={"former"}, current=True,
    )
    assert former["obligations"][0]["source_business"]["subject_id"] == "successor"
    assert former["business_count"] == 4
    assert former["movement_count"] == 2
    assert former["complete"] is False
    successor = freeze.frozen_subject_summary(
        connection, "2026-01", subject_ids={"successor"}, current=True,
    )
    assert successor["obligations"] == []
    assert successor["business_count"] == 1
    open_state = dict(latest)
    open_state.update(
        paid=-30, movement_count=1, last_change_period=feb + 1,
    )
    scope.overrides["k"] = open_state
    scope.tail_rows.append({
        "publication_id": "p-reversal", "item_no": 1,
        "obligation_key": "k", "source_subject_id": None,
        "change_kind": "payment", "state": "resolved",
        "posting_period": feb + 1,
    })
    with_reversal = freeze.frozen_subject_summary(
        connection, "2026-01", subject_ids={"former"}, current=True,
    )
    assert with_reversal["obligations"][0]["paid_fen"] == -30
    assert with_reversal["obligations"][0]["remaining_fen"] == 230
    assert with_reversal["movement_count"] == 3
    assert with_reversal["business_count"] == 5
    connection.execute(
        "UPDATE calculation SET digest=? WHERE id='c-old'",
        (bytes.fromhex("ff" * 32),),
    )
    with pytest.raises(KernelError) as damaged_event:
        freeze.frozen_subject_summary(
            connection, "2026-01", subject_ids={"former"}, current=True,
        )
    assert damaged_event.value.code == "content_integrity_failed"
    connection.execute(
        "UPDATE calculation SET digest=? WHERE id='c-old'",
        (bytes.fromhex("00" * 32),),
    )
    mismatched = [
        ["former", [[jan, block(["different-key"]), 1]], [former_events, former_later]],
        subjects[1],
    ]
    scope.base_root["subject_directory"] = [
        [0, "former", "successor", 2, block(mismatched)]
    ]
    with pytest.raises(KernelError) as damaged:
        freeze.frozen_subject_summary(
            connection, "2026-01", subject_ids={"former"}, current=True,
        )
    assert damaged.value.code == "content_integrity_failed"
    connection.close()


def test_subject_proof_batches_month_chunks_in_one_sql_read():
    import ai_accounting.kernel.settlement_freeze as freeze

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE settlement_freeze_block(digest BLOB,payload TEXT)")

    def block(items):
        payload = canonical(items)
        digest = freeze._sha(payload)
        connection.execute("INSERT INTO settlement_freeze_block VALUES(?,?)", (digest, payload))
        return digest.hex()

    first = YearMonth("2016-01").ordinal
    last = first + 119
    sources = [[first, block(["k"]), 1]]
    direct = []
    for posting in range(first, last + 1):
        items = [[
            f"p-{posting}", 1, "k" if posting == first else None,
            "source" if posting == first else "payment", "resolved",
        ]]
        direct.append([posting, block(items), 1])
    leaf = [["worker", sources, direct]]
    scope = FrozenScope(
        cutoff=last, through=last, base_period=last,
        base_root={"subject_directory": [[0, "worker", "worker", 1, block(leaf)]]},
        groups={}, period_amounts={}, overrides={}, current=False,
    )
    statements = []
    connection.set_trace_callback(statements.append)
    keys, events = freeze._subject_proof(connection, scope, {"worker"})
    assert keys == {"k"}
    assert len(events) == 120
    assert sum("json_each" in statement for statement in statements) == 1
    connection.close()


def test_frozen_page_locates_only_bounded_leaves_among_many_open_keys(monkeypatch):
    import ai_accounting.kernel.settlement_freeze as freeze

    period = YearMonth("2026-01").ordinal
    keys = [f"k{number:05}" for number in range(6400)]
    leaves = [keys[index:index + 32] for index in range(0, len(keys), 32)]
    blocks = {
        ordinal: [[key, "00" * 32, period] for key in leaf]
        for ordinal, leaf in enumerate(leaves)
    }
    directory = [
        [ordinal, leaf[0], leaf[-1], len(leaf), f"{ordinal:064x}"]
        for ordinal, leaf in enumerate(leaves)
    ]

    def changed(key, *, amount=100, first=period, unknown=False):
        state = _empty_state(key)
        state.update(
            source_event_count=1,
            first_source_period=first,
            source_amount=amount,
            bad_source=unknown,
        )
        return state

    scope = FrozenScope(
        cutoff=period,
        through=period,
        base_period=period,
        base_root={"directories": {"all": directory, "open": directory}},
        groups={
            (period, "receivable", "1122", None, "labor"): [
                len(keys), 1, len(keys) - 2, 1, 0, 0, 0, 0, 0, 0,
                0, 100 * (len(keys) - 3), 0, 0, 0,
            ]
        },
        period_amounts={},
        overrides={
            "k00000": changed("k00000", amount=0),
            "k00001": changed("k00001", first=period + 1),
            "k00002": changed("k00002", unknown=True),
            "k00002a": changed("k00002a"),
            "k00003": changed("k00003", amount=0),
        },
        current=False,
    )
    loaded = []

    def read_block(_connection, _period, header):
        loaded.append(header[0])
        return blocks[header[0]]

    monkeypatch.setattr(freeze, "_read_block", read_block)
    monkeypatch.setattr(freeze, "_scope", lambda *_args, **_kwargs: scope)
    monkeypatch.setattr(
        freeze, "_read_state",
        lambda *_args: pytest.fail("summary-only page must not hydrate states"),
    )
    first = freeze.frozen_dashboard_open(None, "2026-01", limit=5, summary_only=True)
    assert first["page"] == {
        "total_count": len(keys) - 2,
        "filtered_count": len(keys) - 2,
        "returned_count": 5,
        "has_more": True,
        "next_cursor": "k00006",
    }
    assert len(loaded) == 1
    loaded.clear()
    distant = _page_entries(
        None, scope, include_settled=False, page_keys=None,
        after="k05000", limit=5,
    )
    assert list(distant) == [f"k{number:05}" for number in range(5001, 5007)]
    assert set(loaded) == {156}
    loaded.clear()
    with pytest.raises(KernelError) as invalid:
        _page_entries(
            None, scope, include_settled=False, page_keys=None,
            after="k05000-not-a-key", limit=5,
        )
    assert invalid.value.code == "dashboard_snapshot_changed"
    assert len(loaded) <= 1
    settled = _page_entries(
        None, scope, include_settled=True, page_keys={"k00000", "k00001", "k00002"}
    )
    assert set(settled) == {"k00000", "k00002"}


def test_unused_reference_is_full_integrity_duty_and_open_tail_still_checked(
    tmp_path, monkeypatch
):
    import ai_accounting.kernel.settlement_freeze as freeze

    company = setup(tmp_path)
    monkeypatch.setattr(freeze, "_LEAF_SIZE", 1)
    company.close("2026-01")
    with company.engine.store.connection(read_only=True) as connection:
        expected = settlement_dashboard_open(connection, "2026-01", limit=1)
        expected_summary = settlement_followup_summary(connection, "2026-01")
    with company.engine.store.connection() as connection:
        rows = connection.execute(
            "SELECT ordinal FROM settlement_freeze_ref "
            "WHERE kind='all' ORDER BY ordinal"
        ).fetchall()
        assert len(rows) >= 2
        connection.execute(
            "DELETE FROM settlement_freeze_ref WHERE kind='all' AND ordinal=?",
            (rows[-1][0],),
        )
    with company.engine.store.connection(read_only=True) as connection:
        assert settlement_dashboard_open(connection, "2026-01", limit=1) == expected
        assert settlement_followup_summary(connection, "2026-01") == expected_summary
        with pytest.raises(KernelError, match="冻结清偿依据不匹配"):
            require_frozen_settlement_projection(company.engine, connection)
        close_epochs = company.engine.store.epochs(connection)
    # The actual close transaction independently verifies the old mirror before
    # preparing its next root; an ordinary successful read cannot authorize it.
    with pytest.raises(KernelError, match="冻结清偿依据不匹配"):
        Periods(company.engine).close(
            "2026-02", owner_confirmation=company.owner_confirmation,
            preview_digest="00" * 32, epochs=close_epochs, request_id="reject-broken-freeze",
        )
    assert company.count("period_close") == 1
    repaired = company.engine.rebuild_projections(request_id="repair-frozen-settlement")
    assert repaired["changed"] is True
    with company.engine.store.connection(read_only=True) as connection:
        assert settlement_dashboard_open(connection, "2026-01", limit=1)["page"][
            "returned_count"
        ] == 1
        require_frozen_settlement_projection(company.engine, connection)

    (tmp_path / "other").mkdir()
    other = setup(tmp_path / "other")
    other.close("2026-01")
    other.save(
        cash_payment(
            "2026-02", 800000,
            allocation("labor_project_cost", "cost", "net", 800000),
        ),
        "balance-paid",
    )
    other.publish("balance-paid")
    with other.engine.store.connection() as connection:
        connection.execute(
            "DELETE FROM settlement_change WHERE posting_period="
            "(SELECT max(posting_period) FROM settlement_change)"
        )
    with other.engine.store.connection(read_only=True) as connection:
        with pytest.raises(KernelError, match="清偿读取投影的期间摘要不匹配"):
            settlement_dashboard_open(connection, "2026-01", current=True)


def test_unused_reference_growth_does_not_expand_ordinary_read_work(tmp_path):
    company = setup(tmp_path)
    company.close("2026-01")

    def read():
        statements = []
        with company.engine.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            connection.set_trace_callback(statements.append)
            result = (
                settlement_followup_summary(connection, "2026-01"),
                settlement_dashboard_open(connection, "2026-01", limit=1),
                settlement_summary(connection, "2026-01", subject_ids={"cost"}),
                settlement_position_rows(connection, "2026-01", {"1122", "2202", "2241"}),
            )
            connection.set_trace_callback(None)
        return result, statements

    expected, before = read()
    with company.engine.store.connection() as connection:
        period, kind, first, last, count, digest = connection.execute(
            "SELECT period,kind,first_key,last_key,row_count,block_digest "
            "FROM settlement_freeze_ref WHERE kind='all' ORDER BY ordinal LIMIT 1"
        ).fetchone()
        maximum = connection.execute(
            "SELECT max(ordinal) FROM settlement_freeze_ref WHERE period=? AND kind=?",
            (period, kind),
        ).fetchone()[0]
        connection.executemany(
            "INSERT INTO settlement_freeze_ref VALUES(?,?,?,?,?,?,?)",
            ((period, kind, maximum + number, first, last, count, digest)
             for number in range(1, 101)),
        )
    actual, after = read()
    assert actual == expected
    assert after == before
    assert not any("settlement_freeze_ref" in statement for statement in after)
    assert any("settlement_freeze_block" in statement for statement in after)
    assert any("settlement_state_revision" in statement for statement in after)
    with company.engine.store.connection(read_only=True) as connection:
        with pytest.raises(KernelError, match="冻结清偿依据不匹配"):
            require_frozen_settlement_projection(company.engine, connection)
    assert company.engine.rebuild_projections(request_id="repair-extra-freeze-refs")["changed"]
    assert read() == (expected, before)


@pytest.mark.parametrize("kind", ["all", "open", "subject"])
@pytest.mark.parametrize("entries", [
    [None], [[]], [[0, "a", "z", 1]], [[False, "a", "z", 1, "00" * 32]],
    [[0, 1, "z", 1, "00" * 32]], [[0, "z", "a", 1, "00" * 32]],
    [[0, "a", "z", True, "00" * 32]], [[0, "a", "z", 0, "00" * 32]],
    [[0, "a", "z", 1, "gg" * 32]],
    [[0, "a", "z", 1, "00" * 32], [1, "z", "zz", 1, "11" * 32]],
])
def test_authenticated_root_rejects_malformed_directory_headers(monkeypatch, kind, entries):
    """Even an already anchored body must reject malformed consumed headers."""
    from ai_accounting.kernel import content_history_context, settlement_freeze

    period = YearMonth("2026-01").ordinal
    close_digest = bytes.fromhex("aa" * 32)
    root = {"format": "settlement-freeze/2", "period": period,
            "close_digest": close_digest.hex(), "directories": {"all": [], "open": []},
            "subject_directory": []}
    if kind == "subject":
        root["subject_directory"] = entries
    else:
        root["directories"][kind] = entries
    payload = canonical(root)
    root_digest = hashlib.sha256(payload.encode()).digest()
    # Supply the independent parent result to isolate this post-authentication
    # shape boundary; no business writer or integrity verifier is bypassed.
    reader = SimpleNamespace(verified_header=lambda *_args: root_digest,
                             derived_root=lambda header, _name: header)
    monkeypatch.setattr(content_history_context, "close_reader", lambda: reader)
    with sqlite3.connect(":memory:") as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE period_close(period,manifest,digest)")
        connection.execute(
            "CREATE TABLE settlement_freeze_root(period,close_digest,root_json,root_digest)"
        )
        connection.execute("INSERT INTO period_close VALUES(?,?,?)", (period, "{}", close_digest))
        connection.execute("INSERT INTO settlement_freeze_root VALUES(?,?,?,?)",
                           (period, close_digest, payload, root_digest))
        with pytest.raises(KernelError) as invalid:
            settlement_freeze.frozen_root(connection, period)
        assert invalid.value.code == "content_integrity_failed"


def test_second_frozen_period_keeps_first_period_paid_and_reuses_state(tmp_path, monkeypatch):
    company = setup(tmp_path)
    company.close("2026-01")
    company.save(
        cash_payment(
            "2026-02", 800000,
            allocation("labor_project_cost", "cost", "net", 800000),
        ),
        "balance-paid",
    )
    company.publish("balance-paid")
    company.close("2026-02")
    with company.engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        for period in ("2026-01", "2026-02"):
            for current in (False, True):
                expected = _legacy(
                    monkeypatch, settlement_dashboard_open, connection, period,
                    current=current, limit=10,
                )
                assert settlement_dashboard_open(
                    connection, period, current=current, limit=10
                ) == expected
                assert settlement_followup_summary(
                    connection, period, current=current
                ) == _legacy(
                    monkeypatch, settlement_followup_summary, connection, period,
                    current=current,
                )
        frozen = connection.execute(
            "SELECT count(*) FROM settlement_state_revision"
        ).fetchone()[0]
        assert frozen < 2 * connection.execute(
            "SELECT count(*) FROM settlement_change"
        ).fetchone()[0]


def test_nonhit_state_damage_is_caught_by_full_integrity_and_repaired(tmp_path):
    company = setup(tmp_path)
    company.close("2026-01")
    with company.engine.store.connection(read_only=True) as connection:
        open_key = settlement_dashboard_open(connection, "2026-01", limit=1)[
            "obligations"
        ][0]["key"]
        nonhit = connection.execute(
            "SELECT digest FROM settlement_state_revision WHERE obligation_key<>? LIMIT 1",
            (open_key,),
        ).fetchone()[0]
    with company.engine.store.connection() as connection:
        connection.execute(
            "UPDATE settlement_state_revision SET payload='{}' WHERE digest=?", (nonhit,)
        )
    with company.engine.store.connection(read_only=True) as connection:
        assert settlement_dashboard_open(connection, "2026-01", limit=1)["page"][
            "returned_count"
        ] == 1
    with pytest.raises(KernelError) as damaged:
        Maintenance(company.engine).verify_integrity()
    assert damaged.value.code == "content_integrity_failed"
    assert company.engine.rebuild_projections(request_id="repair-nonhit-state")["changed"]
    assert Maintenance(company.engine).verify_integrity()["status"] == "verified"


def test_frozen_page_rejects_damaged_hit_block_and_state(tmp_path):
    company = setup(tmp_path)
    company.close("2026-01")
    with company.engine.store.connection(read_only=True) as connection:
        key = settlement_dashboard_open(connection, "2026-01", limit=1)["obligations"][0][
            "key"
        ]
        block = connection.execute(
            "SELECT b.digest,b.payload FROM settlement_freeze_ref r "
            "JOIN settlement_freeze_block b ON b.digest=r.block_digest "
            "WHERE r.kind='open' AND r.first_key<=? AND r.last_key>=?",
            (key, key),
        ).fetchone()
        state = connection.execute(
            "SELECT digest FROM settlement_state_revision WHERE obligation_key=?",
            (key,),
        ).fetchone()
        assert block is not None and state is not None
    with company.engine.store.connection() as connection:
        connection.execute(
            "UPDATE settlement_freeze_block SET payload='[]' WHERE digest=?",
            (block["digest"],),
        )
    with company.engine.store.connection(read_only=True) as connection:
        with pytest.raises(KernelError) as damaged:
            settlement_dashboard_open(connection, "2026-01", limit=1)
        assert damaged.value.code == "content_integrity_failed"
    with company.engine.store.connection() as connection:
        connection.execute(
            "UPDATE settlement_freeze_block SET payload=? WHERE digest=?",
            (block["payload"], block["digest"]),
        )
        connection.execute(
            "UPDATE settlement_state_revision SET payload='{}' WHERE digest=?",
            (state["digest"],),
        )
    with company.engine.store.connection(read_only=True) as connection:
        with pytest.raises(KernelError) as damaged:
            settlement_dashboard_open(connection, "2026-01", limit=1)
        assert damaged.value.code == "content_integrity_failed"


def test_complete_integrity_rejects_deleted_terminal_open_publication(tmp_path):
    company = setup(tmp_path)
    company.close("2026-01")
    company.save(
        cash_payment(
            "2026-02", 800000,
            allocation("labor_project_cost", "cost", "net", 800000),
        ),
        "balance-paid",
    )
    company.publish("balance-paid")
    with company.engine.store.connection() as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN IMMEDIATE")
        triggers = list(connection.execute(
            "SELECT name,sql FROM sqlite_schema WHERE type='trigger' "
            "AND tbl_name IN ('calculation_publication','audit')"
        ))
        for row in triggers:
            connection.execute('DROP TRIGGER "' + row[0] + '"')
        month = YearMonth("2026-02").ordinal
        connection.execute("DELETE FROM settlement_change WHERE posting_period=?", (month,))
        connection.execute(
            "DELETE FROM settlement_projection_seal WHERE posting_period=?", (month,)
        )
        connection.execute(
            "DELETE FROM calculation_publication WHERE posting_period=?", (month,)
        )
        connection.execute("DELETE FROM audit WHERE id=(SELECT max(id) FROM audit)")
        for row in triggers:
            connection.execute(row[1])
        connection.commit()
    with pytest.raises(KernelError) as damaged:
        Maintenance(company.engine).verify_integrity()
    assert damaged.value.code == "content_integrity_failed"
