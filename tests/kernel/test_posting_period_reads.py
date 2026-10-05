"""Period locators scale with months; consumers retain the source proof."""

import json
import sqlite3
from pathlib import Path

import pytest
from test_settlement_period_scopes import setup

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.posting_period_reads import posting_periods
from ai_accounting.kernel.settlement_projection import settlement_followup_summary
from ai_accounting.kernel.types import YearMonth

TABLES = (
    "calculation_publication", "period_balance", "period_balance_seal",
    "settlement_change", "settlement_projection_seal",
)


def _connection(months=0, copies=1):
    connection = sqlite3.connect(":memory:")
    # Narrow work fixture, with the exact production leading-period indexes.
    # No invented covering index or source-proof shortcut is used.
    for table in TABLES:
        connection.execute(
            f"CREATE TABLE {table}(posting_period INTEGER NOT NULL,subject_id TEXT,"
            "id TEXT,category TEXT,obligation_key TEXT)"
        )
    contract = json.loads((
        Path(__file__).parents[2]
        / "src/ai_accounting/kernel/schema_contracts/company/draft.json"
    ).read_text(encoding="utf-8"))
    for item in contract["objects"]:
        if item["name"] in {
            "publication_posting", "period_balance_period", "settlement_change_period",
        }:
            connection.execute(item["sql"])
    # These mirror the actual seal primary keys, which also lead with period.
    connection.execute(
        "CREATE UNIQUE INDEX seal_balance_pk ON period_balance_seal(posting_period,category)"
    )
    connection.execute(
        "CREATE UNIQUE INDEX seal_settlement_pk ON settlement_projection_seal(posting_period)"
    )
    for table in TABLES:
        width = 1 if table.endswith("seal") else copies
        connection.executemany(
            f"INSERT INTO {table} VALUES(?,?,?,?,?)",
            ((month, str(i), str(i), str(i), str(i))
             for month in range(1, months + 1) for i in range(width)),
        )
    return connection


def _work(connection, *, after=None, through=None, baseline=False):
    vm = [0]
    loaded = [0]

    class ObservedRead:
        def execute(self, sql, parameters=()):
            for row in connection.execute(sql, parameters):
                loaded[0] += 1
                yield row

    observed = ObservedRead()
    connection.set_progress_handler(lambda: vm.__setitem__(0, vm[0] + 1) or 0, 1)
    try:
        if baseline:
            result = {row[0] for row in observed.execute(
                " UNION ".join(f"SELECT posting_period FROM {table}" for table in TABLES)
            )}
        else:
            result = posting_periods(observed, TABLES, after=after, through=through)
    finally:
        connection.set_progress_handler(None, 0)
    return result, vm[0], loaded[0]


def test_period_discovery_work_depends_on_months_not_repeated_rows():
    measurements = []
    for months in (12, 48, 120):
        with _connection(months, 1) as small, _connection(months, 1000) as large:
            expected = set(range(1, months + 1))
            small_rows, small_vm, small_loaded = _work(small)
            large_rows, large_vm, large_loaded = _work(large)
            assert small_rows == large_rows == expected
            assert small_loaded == large_loaded == months * len(TABLES)
            assert large_vm < small_vm * 2
            baseline_rows, baseline_vm, baseline_loaded = _work(large, baseline=True)
            assert baseline_rows == expected
            assert baseline_loaded == months
            assert baseline_vm > large_vm * 10
            measurements.append(large_vm)
            print(
                f"months={months} seek_vm={large_vm} union_vm={baseline_vm} "
                f"rows={len(large_rows)} loaded_month_rows={large_loaded}"
            )
    assert measurements[1] < measurements[0] * 5
    assert measurements[2] < measurements[1] * 3


def test_empty_bounds_and_independent_orphan_months():
    with _connection() as connection:
        assert posting_periods(connection, TABLES) == set()
        for table, period in zip(TABLES, (1, 3, 5, 7, 9), strict=True):
            connection.execute(f"INSERT INTO {table} VALUES(?,NULL,NULL,'x',NULL)", (period,))
        assert posting_periods(connection, TABLES) == {1, 3, 5, 7, 9}
        assert posting_periods(connection, TABLES, after=3, through=7) == {5, 7}
        assert posting_periods(connection, TABLES, through=3) == {1, 3}
        assert posting_periods(connection, TABLES, after=7) == {9}
        assert posting_periods(connection, TABLES, after=7, through=3) == set()
        with pytest.raises(ValueError):
            posting_periods(connection, ("calculation_publication WHERE 1=1",))


@pytest.mark.parametrize("current", [False, True])
def test_real_settlement_summary_rejects_orphan_seal_month(tmp_path, current):
    company = setup(tmp_path)
    with company.engine.store.connection() as connection:
        connection.execute(
            "INSERT INTO settlement_projection_seal VALUES(?,?,?)",
            (YearMonth("2026-02").ordinal, 0, bytes(32)),
        )
    with company.engine.store.connection(read_only=True) as connection:
        with pytest.raises(KernelError):
            settlement_followup_summary(connection, "2026-02", current=current)
