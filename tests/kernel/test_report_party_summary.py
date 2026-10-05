"""Statements consume exact party nets without constructing historical display rows."""

import sqlite3
from types import SimpleNamespace

import pytest
from test_reports import book as _report_book
from test_reports import scenario

from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.query_semantics import classify_financial_position
from ai_accounting.kernel.report_projection import (
    _party_balance_position_lines,
    party_balance_rows,
)
from ai_accounting.kernel.reports import Reports, _statements, check_report_readiness
from ai_accounting.kernel.types import YearMonth, canonical

book = _report_book


def net_book(monkeypatch, months):
    """Explicit already-authenticated months isolate netting from presentation work."""

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        "CREATE TABLE opening_account(period INTEGER,account TEXT,debit INTEGER,credit INTEGER);"
        "CREATE TABLE report_party_checkpoint_seal(posting_period INTEGER,usable INTEGER);"
        "CREATE TABLE calculation_publication(posting_period INTEGER);"
        "CREATE TABLE period_close(period INTEGER);"
        "CREATE TABLE voucher_version(id TEXT PRIMARY KEY,period INTEGER);"
        "CREATE INDEX voucher_period ON voucher_version(period,id);"
        "CREATE TABLE voucher_current(version_id TEXT UNIQUE);"
    )
    connection.executemany("INSERT INTO period_close VALUES(?)", ((m,) for m in months))
    connection.execute("INSERT INTO calculation_publication VALUES(?)", (min(months),))
    seen = []

    def stored(_connection, period):
        seen.append(period)
        return {
            "party_rows": tuple((period, account, canonical(party), amount)
                                for account, party, amount in months.get(period, ())),
            "party_usable": True,
            "checkpoint_rows": (),
            "checkpoint_usable": None,
        }

    monkeypatch.setattr("ai_accounting.kernel.report_projection._stored_month", stored)
    reads = SimpleNamespace(connection=connection, _snapshot_active=True, _report_snapshot_cache={})
    return connection, reads, seen


def test_party_summary_preserves_netting_signs_accounts_and_period_obligations(monkeypatch):
    jan, feb = (YearMonth(m).ordinal for m in ("2026-01", "2026-02"))
    months = {
        jan: (
            ("2202", ("party", "same"), -100),
            ("2202", ("party", "credit"), -200),
            ("1122", ("party", "same"), 40),
            ("224101", ("statutory_payroll_obligation", "employee", "2026-01"), -30),
        ),
        feb: (
            ("2202", ("party", "same"), 150),
            ("2202", ("party", "advance"), 70),
            ("224101", ("statutory_payroll_obligation", "employee", "2026-02"), 20),
        ),
    }
    connection, reads, seen = net_book(monkeypatch, months)
    try:
        lines = _party_balance_position_lines(None, connection, feb, source="closed", reads=reads)
        assert lines == {5: 120, 33: 200, 4: 40, 39: 30, 8: 20}
        checked = len(seen)
        details = party_balance_rows(None, connection, feb, source="closed", reads=reads)
        assert len(seen) == checked  # The same exact nets back both consumers.
        assert len(details) == 6
        direct = [{"account": "1001", "amount": 500}, {"account": "4001", "amount": -400}]
        assert classify_financial_position(direct, _net_party_lines=lines) == (
            classify_financial_position([*details, *direct])
        )
        # Unknown accounts and unknown amounts retain the identical nullable totals.
        for unknown in ({"account": "unmapped", "amount": 1}, {"account": "1001", "amount": None}):
            assert classify_financial_position([*direct, unknown], _net_party_lines=lines) == (
                classify_financial_position([*direct, unknown, *details])
            )
        beginning = jan - 1
        issues, original_issues = [], []
        arguments = ([], jan, jan, feb)
        balances = {beginning: {}, feb: {"1001": 500, "4001": -400}}
        assert _statements(
            *arguments, issues, account_balances=balances,
            party_position_lines={beginning: {}, feb: lines},
        ) == _statements(
            *arguments, original_issues, account_balances=balances,
            party_balances={beginning: [], feb: details},
        )
        assert issues == original_issues
    finally:
        connection.close()


def test_party_summary_work_does_not_expand_historic_obligation_keys(monkeypatch):
    jan = YearMonth("2026-01").ordinal
    months = {jan: tuple(
        ("224101", ("statutory_payroll_obligation", f"employee-{employee}", str(month)), -10)
        for month in range(48) for employee in range(50)
    )}
    connection, reads, _seen = net_book(monkeypatch, months)
    decoded = []
    import ai_accounting.kernel.report_projection as projection

    original = projection._freeze_party_key

    def freeze(value):
        decoded.append(value)
        return original(value)

    monkeypatch.setattr(projection, "_freeze_party_key", freeze)
    try:
        summary = _party_balance_position_lines(None, connection, jan, source="closed", reads=reads)
        assert summary == {39: 24000}
        assert decoded == []
        assert len(party_balance_rows(None, connection, jan, source="closed", reads=reads)) == 2400
        assert len(decoded) == 4 * 2400
        assert _party_balance_position_lines(
            None, connection, jan, source="closed", reads=reads
        ) == summary
        assert len(decoded) == 4 * 2400
    finally:
        connection.close()


@pytest.mark.parametrize("source", ["open", "closed"])
def test_public_report_and_readiness_keep_results_without_party_display_rows(
    book, monkeypatch, source
):
    import ai_accounting.kernel.report_projection as projection

    engine = book[0]
    scenario(book)
    for month in ("2026-01", "2026-02", "2026-03"):
        book[3](month)
    with monkeypatch.context() as original:
        original.setattr(projection, "_party_balance_position_lines", lambda *_a, **_k: None)
        expected = Reports(engine).report(2026, 1, source=source)

    def no_display_decode(_value):
        pytest.fail("A statement/readiness request expanded a historic party display key")

    monkeypatch.setattr(projection, "_freeze_party_key", no_display_decode)
    # CLI and MCP's `report` action use this same public Reports entry point.
    report = Reports(engine).report(2026, 1, source=source)
    assert report["statements"] == expected["statements"]
    assert report["fact_issues"] == expected["fact_issues"]
    assert report["status"] == expected["status"]
    Dashboard(engine).quarterly_report(2026, 1)
    with QueryReads.snapshot(engine) as reads:
        assert check_report_readiness(
            engine.store, reads.connection, YearMonth("2026-03"), reads=reads
        ) == expected["fact_issues"]


def test_unestablished_party_summary_retains_none_fallback(monkeypatch):
    jan = YearMonth("2026-01").ordinal
    connection, reads, _seen = net_book(monkeypatch, {jan: ()})
    try:
        connection.execute("INSERT INTO opening_account VALUES(?, '2202', 0, 10)", (jan,))
        assert _party_balance_position_lines(None, connection, jan, reads=reads) is None
        assert party_balance_rows(None, connection, jan, reads=reads) is None
        assert ("report_party_nets", jan, "open") not in reads._report_snapshot_cache
    finally:
        connection.close()


def checkpoint_net_book(monkeypatch, *, historic_obligations=0):
    """Explicit frozen payloads at the successful read_report_flow boundary.

    This isolates actual checkpoint-row traversal, not SQL/authentication work.
    Each month still passes through that reader before its amounts are used.
    """
    import ai_accounting.kernel.report_flow as flow

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        "CREATE TABLE opening_account(period INTEGER,account TEXT,debit INTEGER,credit INTEGER);"
        "CREATE TABLE report_party_checkpoint_seal(posting_period INTEGER,usable INTEGER);"
        "CREATE TABLE calculation_publication(posting_period INTEGER);"
        "CREATE TABLE period_close(period INTEGER);"
        "CREATE TABLE voucher_version(id TEXT PRIMARY KEY,period INTEGER);"
        "CREATE INDEX voucher_period ON voucher_version(period,id);"
        "CREATE TABLE voucher_current(version_id TEXT UNIQUE);"
    )
    december = YearMonth("2025-12").ordinal
    connection.executemany(
        "INSERT INTO period_close VALUES(?)", ((december + n,) for n in range(3))
    )
    connection.execute("INSERT INTO report_party_checkpoint_seal VALUES(?,1)", (december,))
    connection.execute("INSERT INTO calculation_publication VALUES(?)", (december,))
    traversed, seen = [], []
    party = canonical(("party", "supplier"))
    checkpoint_rows = (
        (december, "2202", party, -100),
        *(
            (
                december, "224101",
                canonical(("statutory_payroll_obligation", f"employee-{index % 50}",
                           str(YearMonth.from_ordinal(december - index // 50)))),
                -10,
            )
            for index in range(historic_obligations)
        ),
    )

    class CheckpointRows:
        def __iter__(self):
            for row in checkpoint_rows:
                traversed.append(row)
                yield row

    months = {
        december: {
            "checkpoint_usable": True, "checkpoint_rows": CheckpointRows(),
            "party_usable": True, "party_rows": (),
        },
        december + 1: {
            "checkpoint_usable": None, "checkpoint_rows": (),
            "party_usable": True, "party_rows": ((december + 1, "2202", party, 150),),
        },
        december + 2: {
            "checkpoint_usable": None, "checkpoint_rows": (),
            "party_usable": True, "party_rows": ((december + 2, "2202", party, -80),),
        },
    }

    def read(_connection, period, *, reads):
        assert _connection is connection
        seen.append(period)
        value = months[period]
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(flow, "read_report_flow", read)
    reads = SimpleNamespace(connection=connection, _snapshot_active=True, _report_snapshot_cache={})
    return connection, reads, december, months, seen, traversed


def test_checkpoint_base_traversed_once_across_cutoffs_and_sources(monkeypatch):
    connection, reads, december, _months, seen, traversed = checkpoint_net_book(monkeypatch)
    try:
        assert _party_balance_position_lines(
            object(), connection, december, source="closed", reads=reads
        ) == {33: 100}
        assert _party_balance_position_lines(
            object(), connection, december + 1, source="open", reads=reads
        ) == {5: 50}
        assert _party_balance_position_lines(
            object(), connection, december + 2, source="closed", reads=reads
        ) == {33: 30}
        assert len(traversed) == 1
        assert seen == [december, december + 1, december + 2]
        # Every cutoff starts from the frozen -100, not a previous mutable total.
        assert list(reads._report_snapshot_cache[
            "report_party_checkpoint_totals", december
        ].values()) == [-100]
    finally:
        connection.close()


@pytest.mark.parametrize("history_months", [12, 48, 120])
def test_checkpoint_base_growth_keeps_all_obligations_without_retraversal(
    monkeypatch, history_months
):
    obligations = history_months * 50
    connection, reads, december, _months, seen, traversed = checkpoint_net_book(
        monkeypatch, historic_obligations=obligations
    )
    try:
        assert _party_balance_position_lines(
            object(), connection, december, source="closed", reads=reads
        ) == {33: 100, 39: obligations * 10}
        assert len(traversed) == obligations + 1
        assert _party_balance_position_lines(
            object(), connection, december + 1, source="open", reads=reads
        ) == {5: 50, 39: obligations * 10}
        assert _party_balance_position_lines(
            object(), connection, december + 2, source="closed", reads=reads
        ) == {33: 30, 39: obligations * 10}
        assert len(traversed) == obligations + 1
        assert seen == [december, december + 1, december + 2]
    finally:
        connection.close()


@pytest.mark.parametrize("failure", ["unknown", "exception"])
def test_failed_later_month_does_not_change_checkpoint_base(monkeypatch, failure):
    from ai_accounting.kernel.contracts import KernelError

    connection, reads, december, months, seen, traversed = checkpoint_net_book(monkeypatch)
    try:
        assert _party_balance_position_lines(
            object(), connection, december, reads=reads
        ) == {33: 100}
        healthy = months[december + 2]
        months[december + 2] = (
            {**healthy, "party_usable": False}
            if failure == "unknown"
            else KernelError("content_integrity_failed", "synthetic frozen month rejected")
        )
        if failure == "unknown":
            assert _party_balance_position_lines(
                object(), connection, december + 2, reads=reads
            ) is None
        else:
            with pytest.raises(KernelError, match="synthetic frozen month rejected"):
                _party_balance_position_lines(object(), connection, december + 2, reads=reads)
        assert seen == [december, december + 1, december + 2]
        assert ("report_party_nets", december + 2, "open") not in reads._report_snapshot_cache
        assert list(reads._report_snapshot_cache[
            "report_party_checkpoint_totals", december
        ].values()) == [-100]
        if failure == "unknown":
            # An authenticated unknown is stable inside this snapshot. It is
            # not a successful net balance and cannot become zero or change
            # merely because the test changes a later reader's return value.
            assert _party_balance_position_lines(
                object(), connection, december + 2, reads=reads
            ) is None
            assert seen == [december, december + 1, december + 2]
            reads = SimpleNamespace(
                connection=connection, _snapshot_active=True, _report_snapshot_cache={}
            )
        months[december + 2] = healthy
        assert _party_balance_position_lines(
            object(), connection, december + 2, reads=reads
        ) == {33: 30}
        assert seen[-1] == december + 2
        if failure == "unknown":
            assert seen == [december, december + 1, december + 2] * 2
            assert len(traversed) == 2  # Repair is read from a new snapshot.
        else:
            assert seen == [december, december + 1, december + 2, december + 2]
            assert len(traversed) == 1  # A raised reader error was never cached.
        assert list(reads._report_snapshot_cache[
            "report_party_checkpoint_totals", december
        ].values()) == [-100]
    finally:
        connection.close()


def test_checkpoint_base_is_not_reused_without_owned_snapshot(monkeypatch):
    connection, reads, december, _months, seen, traversed = checkpoint_net_book(monkeypatch)
    reads._snapshot_active = False
    try:
        assert _party_balance_position_lines(
            object(), connection, december, reads=reads
        ) == {33: 100}
        assert _party_balance_position_lines(
            object(), connection, december + 1, reads=reads
        ) == {5: 50}
        assert len(traversed) == 2
        assert seen == [december, december, december + 1]
        assert reads._report_snapshot_cache == {}
    finally:
        connection.close()


def test_unusable_checkpoint_never_publishes_a_base(monkeypatch):
    connection, reads, december, months, seen, traversed = checkpoint_net_book(monkeypatch)
    months[december]["checkpoint_usable"] = False
    try:
        assert _party_balance_position_lines(object(), connection, december, reads=reads) is None
        assert seen == [december]
        assert traversed == []
        assert ("report_party_checkpoint_totals", december) not in reads._report_snapshot_cache
        assert ("report_party_nets", december, "open") not in reads._report_snapshot_cache
    finally:
        connection.close()


def test_checkpoint_base_is_not_reused_by_another_snapshot(monkeypatch):
    connection, reads, december, _months, seen, traversed = checkpoint_net_book(monkeypatch)
    try:
        assert _party_balance_position_lines(
            object(), connection, december, reads=reads
        ) == {33: 100}
        other = SimpleNamespace(
            connection=connection, _snapshot_active=True, _report_snapshot_cache={}
        )
        assert _party_balance_position_lines(
            object(), connection, december + 1, reads=other
        ) == {5: 50}
        assert len(traversed) == 2
        assert seen == [december, december, december + 1]
        assert reads._report_snapshot_cache is not other._report_snapshot_cache
    finally:
        connection.close()


class _MeasuredPartyBase(dict):
    def __init__(self, values):
        super().__init__(values)
        self.work = {"copy_rows": 0, "iter_rows": 0, "items_rows": 0, "gets": 0}

    def copy(self):
        self.work["copy_rows"] += len(self)
        return super().copy()

    def __iter__(self):
        for key in super().__iter__():
            self.work["iter_rows"] += 1
            yield key

    def items(self):
        for item in super().items():
            self.work["items_rows"] += 1
            yield item

    def get(self, key, default=None):
        self.work["gets"] += 1
        return super().get(key, default)


@pytest.mark.parametrize("history_months", [12, 48, 120])
def test_owned_summary_tail_does_not_expand_unchanged_party_base(monkeypatch, history_months):
    from ai_accounting.kernel import account_definitions, report_projection

    obligations = history_months * 50
    connection, reads, december, _months, seen, traversed = checkpoint_net_book(
        monkeypatch, historic_obligations=obligations
    )
    # This is explicitly an isolated verified-month boundary, not an assertion
    # that SimpleNamespace is a valid production QueryReads snapshot.
    monkeypatch.setattr(report_projection, "_owns_current_selector_snapshot",
                        lambda actual, selected: actual is reads and selected is connection)
    classifications = []

    class MeasuredReclass(dict):
        def __getitem__(self, account):
            classifications.append(account)
            return super().__getitem__(account)

    monkeypatch.setattr(
        account_definitions, "RECLASS", MeasuredReclass(account_definitions.RECLASS)
    )
    try:
        assert _party_balance_position_lines(
            object(), connection, december, source="closed", reads=reads
        ) == {33: 100, 39: obligations * 10}
        assert len(traversed) == obligations + 1
        base_key = ("report_party_checkpoint_totals", december)
        measured = _MeasuredPartyBase(reads._report_snapshot_cache[base_key])
        reads._report_snapshot_cache[base_key] = measured
        classifications.clear()

        assert _party_balance_position_lines(
            object(), connection, december + 1, source="open", reads=reads
        ) == {5: 50, 39: obligations * 10}
        assert _party_balance_position_lines(
            object(), connection, december + 2, source="closed", reads=reads
        ) == {33: 30, 39: obligations * 10}
        # Actual base traversal/materialization and classification work, not
        # only raw checkpoint iterator or function-call instrumentation.
        assert measured.work == {"copy_rows": 0, "iter_rows": 0, "items_rows": 0, "gets": 2}
        assert classifications == ["2202"] * 6
        assert len(traversed) == obligations + 1
        assert seen == [december, december + 1, december + 2]
        assert measured[("2202", canonical(("party", "supplier")))] == -100
        assert not any(key[0] == "report_party_nets" for key in reads._report_snapshot_cache)

        # A subsequent actual detail consumer must still materialize and retain
        # every immutable obligation; summaries must not publish incomplete nets.
        rows = party_balance_rows(object(), connection, december + 2,
                                  source="closed", reads=reads)
        assert len(rows) == obligations + 1
        assert measured.work["copy_rows"] == obligations + 1
    finally:
        connection.close()


def test_summary_keeps_distinct_party_signs_zero_and_repeated_deltas(monkeypatch):
    from ai_accounting.kernel import report_projection

    connection, reads, december, months, _seen, _traversed = checkpoint_net_book(monkeypatch)
    monkeypatch.setattr(report_projection, "_owns_current_selector_snapshot",
                        lambda actual, selected: actual is reads and selected is connection)
    supplier = canonical(("party", "supplier"))
    customer = canonical(("party", "other"))
    months[december]["checkpoint_rows"] = (
        (december, "2202", supplier, -100),
        (december, "2202", customer, 60),
    )
    months[december + 1]["party_rows"] = (
        (december + 1, "2202", supplier, 100),  # zero
        (december + 1, "2202", supplier, 30),   # positive again
        (december + 1, "2202", supplier, -50),  # negative again
    )
    try:
        assert _party_balance_position_lines(
            object(), connection, december + 1, reads=reads
        ) == {5: 60, 33: 20}
        assert _party_balance_position_lines(
            object(), connection, december, source="closed", reads=reads
        ) == {5: 60, 33: 100}
        assert _party_balance_position_lines(
            object(), connection, december + 1, source="closed", reads=reads
        ) == {5: 60, 33: 20}
    finally:
        connection.close()


@pytest.mark.parametrize("failure", ["unknown", "exception"])
def test_failed_tail_does_not_publish_summary_or_mutate_classified_base(monkeypatch, failure):
    from ai_accounting.kernel import report_projection
    from ai_accounting.kernel.contracts import KernelError

    connection, reads, december, months, _seen, _traversed = checkpoint_net_book(monkeypatch)
    monkeypatch.setattr(report_projection, "_owns_current_selector_snapshot",
                        lambda actual, selected: actual is reads and selected is connection)
    try:
        assert _party_balance_position_lines(
            object(), connection, december, reads=reads
        ) == {33: 100}
        key = ("report_party_checkpoint_position_lines", december)
        before = reads._report_snapshot_cache[key].copy()
        months[december + 2] = (
            {**months[december + 2], "party_usable": False}
            if failure == "unknown"
            else KernelError("content_integrity_failed", "synthetic frozen month rejected")
        )
        if failure == "unknown":
            assert _party_balance_position_lines(
                object(), connection, december + 2, reads=reads
            ) is None
        else:
            with pytest.raises(KernelError, match="synthetic frozen month rejected"):
                _party_balance_position_lines(object(), connection, december + 2, reads=reads)
        assert (
            "report_party_position_lines", december + 2, "open"
        ) not in reads._report_snapshot_cache
        assert ("report_party_nets", december + 2, "open") not in reads._report_snapshot_cache
        assert reads._report_snapshot_cache[key] == before
        assert list(reads._report_snapshot_cache[
            "report_party_checkpoint_totals", december
        ].values()) == [-100]
    finally:
        connection.close()


def test_account_filter_precedes_shared_report_line_aggregation(monkeypatch):
    from ai_accounting.kernel import report_projection

    connection, reads, december, months, _seen, _traversed = checkpoint_net_book(monkeypatch)
    monkeypatch.setattr(report_projection, "_owns_current_selector_snapshot",
                        lambda actual, selected: actual is reads and selected is connection)
    party = canonical(("party", "supplier"))
    months[december]["checkpoint_rows"] = (
        (december, "224101", party, -30),
        (december, "224102", party, -70),
    )
    try:
        assert _party_balance_position_lines(
            object(), connection, december, source="closed", reads=reads,
            _accounts={"224101"}, _validate_party_keys=True,
        ) == {39: 30}
        assert _party_balance_position_lines(
            object(), connection, december, source="closed", reads=reads,
            _accounts={"224102"}, _validate_party_keys=True,
        ) == {39: 70}
        assert _party_balance_position_lines(
            object(), connection, december, source="closed", reads=reads
        ) == {39: 100}
    finally:
        connection.close()


@pytest.mark.parametrize("raw_key", ["{malformed", "{}", "null", '[ "party", "supplier" ]'])
def test_dashboard_party_json_shape_keeps_exact_full_rows_semantics(monkeypatch, raw_key):
    from ai_accounting.kernel import report_projection

    connection, reads, december, months, _seen, _traversed = checkpoint_net_book(monkeypatch)
    monkeypatch.setattr(report_projection, "_owns_current_selector_snapshot",
                        lambda actual, selected: actual is reads and selected is connection)
    months[december]["checkpoint_rows"] = ((december, "2202", raw_key, -100),)
    try:
        assert _party_balance_position_lines(
            object(), connection, december, source="closed", reads=reads,
            _accounts={"2202"}, _validate_party_keys=True,
        ) is report_projection._PARTY_POSITION_SUMMARY_UNSAFE
        # Dashboard must now call its former party_balance_rows/classify route.
        # That exact route retains JSONDecodeError, unknown and alias netting.
        assert not any(
            key[0] == "report_party_position_lines" for key in reads._report_snapshot_cache
        )
    finally:
        connection.close()


def test_bad_unrelated_json_is_not_hidden_by_dashboard_account_filter(monkeypatch):
    from ai_accounting.kernel import report_projection

    connection, reads, december, months, _seen, _traversed = checkpoint_net_book(monkeypatch)
    monkeypatch.setattr(report_projection, "_owns_current_selector_snapshot",
                        lambda actual, selected: actual is reads and selected is connection)
    months[december]["checkpoint_rows"] = (
        (december, "2202", canonical(("party", "supplier")), -100),
        (december, "224101", "{malformed", -30),
    )
    try:
        assert _party_balance_position_lines(
            object(), connection, december, source="closed", reads=reads,
            _accounts={"2202"}, _validate_party_keys=True,
        ) is report_projection._PARTY_POSITION_SUMMARY_UNSAFE
    finally:
        connection.close()


@pytest.mark.parametrize("raw_alias", ['["party",true]', '["party",1.0]', '[ "party", 1 ]'])
def test_dashboard_summary_preserves_decoded_identity_alias_netting(monkeypatch, raw_alias):
    from ai_accounting.kernel import report_projection

    connection, reads, december, months, _seen, _traversed = checkpoint_net_book(monkeypatch)
    monkeypatch.setattr(report_projection, "_owns_current_selector_snapshot",
                        lambda actual, selected: actual is reads and selected is connection)
    months[december]["checkpoint_rows"] = (
        (december, "2202", '["party",1]', -100),
        (december, "2202", raw_alias, 100),
    )
    try:
        assert _party_balance_position_lines(
            object(), connection, december, source="closed", reads=reads,
            _accounts={"2202"}, _validate_party_keys=True,
        ) is report_projection._PARTY_POSITION_SUMMARY_UNSAFE
        full = party_balance_rows(object(), connection, december, source="closed", reads=reads)
        # These raw strings differ but the established decoded Python identity
        # is equal. Preserve its zero balance and the original error semantics.
        assert classify_financial_position(full) == classify_financial_position([])
    finally:
        connection.close()


def test_unowned_summary_account_filter_keeps_shared_line_accounts_separate(monkeypatch):
    from ai_accounting.kernel import report_projection

    connection, reads, december, months, _seen, _traversed = checkpoint_net_book(monkeypatch)
    party = canonical(("party", "supplier"))
    months[december]["checkpoint_rows"] = (
        (december, "224101", party, -30),
        (december, "224102", party, -70),
    )
    try:
        assert not report_projection._owns_current_selector_snapshot(reads, connection)
        assert _party_balance_position_lines(
            object(), connection, december, source="closed", reads=reads,
            _accounts={"224101"},
        ) == {39: 30}
        assert _party_balance_position_lines(
            object(), connection, december, source="closed", reads=reads,
            _accounts={"224102"},
        ) == {39: 70}
        assert not any(
            key[0] == "report_party_position_lines" for key in reads._report_snapshot_cache
        )
    finally:
        connection.close()


def test_unsupported_checkpoint_key_that_cancels_retains_full_net_behavior(monkeypatch):
    from ai_accounting.kernel import report_projection

    connection, reads, december, months, _seen, _traversed = checkpoint_net_book(monkeypatch)
    monkeypatch.setattr(report_projection, "_owns_current_selector_snapshot",
                        lambda actual, selected: actual is reads and selected is connection)
    party = canonical(("party", "supplier"))
    months[december]["checkpoint_rows"] = ((december, "not-a-reclass-account", party, 30),)
    months[december + 1]["party_rows"] = ((december + 1, "not-a-reclass-account", party, -30),)
    try:
        assert _party_balance_position_lines(
            object(), connection, december + 1, source="closed", reads=reads
        ) == {}
        with pytest.raises(KeyError, match="not-a-reclass-account"):
            _party_balance_position_lines(
                object(), connection, december, source="closed", reads=reads
            )
        assert not any(
            key[0] == "report_party_position_lines" for key in reads._report_snapshot_cache
        )
    finally:
        connection.close()


@pytest.mark.parametrize("source", ["open", "closed"])
def test_real_owned_snapshot_summary_matches_full_and_isolated_readers(book, monkeypatch, source):
    from ai_accounting.kernel import report_projection
    from ai_accounting.kernel.content_history_context import historical_content

    engine = book[0]
    scenario(book)
    for month in ("2026-01", "2026-02", "2026-03"):
        book[3](month)
    cutoff = YearMonth("2026-03").ordinal
    with QueryReads.snapshot(engine) as reads:
        assert report_projection._owns_current_selector_snapshot(reads, reads.connection)
        result = _party_balance_position_lines(
            engine, reads.connection, cutoff, source=source, reads=reads
        )
        assert result is not None
        assert ("report_party_position_lines", cutoff, source) in reads._report_snapshot_cache
        assert ("report_party_nets", cutoff, source) not in reads._report_snapshot_cache
        rows = party_balance_rows(engine, reads.connection, cutoff, source=source, reads=reads)
        assert classify_financial_position(
            [], _net_party_lines=result
        ) == classify_financial_position(rows)
        with engine.store.connection(read_only=True) as other:
            assert _party_balance_position_lines(
                engine, other, cutoff, source=source, reads=reads, _validate_party_keys=True,
            ) is report_projection._PARTY_POSITION_SUMMARY_UNSAFE
        with QueryReads.snapshot(engine):
            assert _party_balance_position_lines(
                engine, reads.connection, cutoff, source=source, reads=reads,
                _validate_party_keys=True,
            ) is report_projection._PARTY_POSITION_SUMMARY_UNSAFE
        with historical_content(1):
            assert _party_balance_position_lines(
                engine, reads.connection, cutoff, source=source, reads=reads,
                _validate_party_keys=True,
            ) is report_projection._PARTY_POSITION_SUMMARY_UNSAFE
        original_version = getattr(engine.store.registry, "content_version", None)
        with monkeypatch.context() as patch:
            patch.setattr(engine.store.registry, "content_version", 1, raising=False)
            assert _party_balance_position_lines(
                engine, reads.connection, cutoff, source=source, reads=reads,
                _validate_party_keys=True,
            ) is report_projection._PARTY_POSITION_SUMMARY_UNSAFE
        assert getattr(engine.store.registry, "content_version", None) == original_version
        assert _party_balance_position_lines(
            None, reads.connection, cutoff, source=source, reads=reads, _validate_party_keys=True,
        ) is report_projection._PARTY_POSITION_SUMMARY_UNSAFE
        assert _party_balance_position_lines(
            engine, reads.connection, cutoff, source=source, reads=reads,
        ) == result


def test_real_sources_dashboard_unknown_projection_keeps_full_row_result(book, monkeypatch):
    from ai_accounting.kernel import report_projection, settlement_projection
    from ai_accounting.kernel.dashboard import _position

    engine = book[0]
    scenario(book, classification=False, tax=False)
    calls = []
    original = report_projection._party_balance_position_lines
    position_rows = settlement_projection.settlement_position_rows

    def unknown_position_rows(*args, **kwargs):
        # Isolate the existing fallback contract: the narrow position projection
        # cannot provide a party. The facts, publications and report-flow proof
        # still come from the real book; no authoritative business is rewritten.
        return [
            {**row, "counterparty_id": None, "unknown": True}
            for row in position_rows(*args, **kwargs)
        ]

    monkeypatch.setattr(settlement_projection, "settlement_position_rows", unknown_position_rows)

    def observed(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append((kwargs.get("_accounts"), kwargs.get("_validate_party_keys"), result))
        return result

    monkeypatch.setattr(report_projection, "_party_balance_position_lines", observed)
    with Dashboard(engine)._snapshot("2026-02") as snap:
        actual = _position(snap)
    assert calls and any(accounts and validated for accounts, validated, _ in calls)
    assert any(isinstance(result, dict) for _, _, result in calls)
    monkeypatch.setattr(report_projection, "_party_balance_position_lines",
                        lambda *_a, **_k: report_projection._PARTY_POSITION_SUMMARY_UNSAFE)
    with Dashboard(engine)._snapshot("2026-02") as snap:
        expected = _position(snap)
    assert actual == expected
    assert actual["assets_fen"] == 50000
    assert actual["liabilities_fen"] == 10000
