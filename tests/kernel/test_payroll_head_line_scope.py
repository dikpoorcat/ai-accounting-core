"""The employee list evaluates line counts only for its selected posting month."""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard_reads import adopted_head_metadata, payroll_head_metadata
from ai_accounting.kernel.types import YearMonth


def test_real_head_selector_does_not_decode_growing_old_outcomes():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        "CREATE TABLE period_close(period INTEGER);"
        "CREATE TABLE calculation_publication("
        "subject_id TEXT,calculation_id TEXT,posting_period INTEGER,sequence INTEGER);"
        "CREATE TABLE calculation("
        "id TEXT,fact_id TEXT,kind TEXT,period INTEGER,outcome TEXT);"
        "CREATE TABLE calculation_current(subject_id TEXT,calculation_id TEXT);"
    )
    old_month = YearMonth("2026-01").ordinal
    month = YearMonth("2026-02").ordinal
    connection.execute("INSERT INTO period_close VALUES(?)", (old_month,))

    def add_heads(prefix, count, posting_month, line_count):
        for index in range(count):
            ident = f"{prefix}-{index}"
            connection.execute(
                "INSERT INTO calculation_publication VALUES(?,?,?,1)",
                (ident, ident, posting_month),
            )
            connection.execute(
                "INSERT INTO calculation VALUES(?,?,?,?,?)",
                (
                    ident,
                    f"fact-{ident}",
                    "payroll",
                    posting_month,
                    json.dumps({"lines": [{}] * line_count}),
                ),
            )
            if posting_month == month:
                connection.execute("INSERT INTO calculation_current VALUES(?,?)", (ident, ident))

    add_heads("old", 200, old_month, 40)
    add_heads("current-zero", 1, month, 0)
    add_heads("current-posted", 1, month, 2)

    class Reads:
        def authoritative_close_rows(self, *, periods, through_period):
            assert periods == [old_month] and through_period == month
            return [{"period": old_month}]

        def close_header(self, row):
            assert row["period"] == old_month
            return SimpleNamespace(root={"small": {"publication_sequence": 1}})

        def verify_sql_outcomes(self, identifiers):
            actual = {
                row[0]
                for row in connection.execute(
                    "SELECT id FROM calculation WHERE id IN (SELECT value FROM json_each(?))",
                    (json.dumps(sorted(identifiers)),),
                )
            }
            assert actual == set(identifiers)

    snapshot = SimpleNamespace(connection=connection, month=month, reads=Reads())
    decoded = []

    def count_line_decode(outcome, path):
        assert path == "$.lines"
        decoded.append(outcome)
        return len(json.loads(outcome)["lines"])

    # Count the SQL function that actually opens an outcome, rather than
    # matching the SQL text or testing a copy of the selector.
    connection.create_function("json_array_length", 2, count_line_decode)
    complete = adopted_head_metadata(snapshot, {"payroll"})
    assert len(complete) == len(decoded) == 202
    assert {row["line_count"] for row in complete if row["posting_period"] == old_month} == {40}

    decoded.clear()
    selected = payroll_head_metadata(snapshot, {"payroll"}, line_count_period="2026-02")
    assert len(selected) == 202
    assert len(decoded) == 2
    assert {row["line_count"] for row in selected if row["posting_period"] == old_month} == {None}
    assert {
        row["subject_id"]: row["line_count"] for row in selected if row["posting_period"] == month
    } == {"current-zero-0": 0, "current-posted-0": 2}
    assert {(row["subject_id"], row["fact_id"], row["posting_period"]) for row in complete} == {
        (row["subject_id"], row["fact_id"], row["posting_period"]) for row in selected
    }

    add_heads("more-old", 500, old_month, 40)
    decoded.clear()
    grown = payroll_head_metadata(snapshot, {"payroll"}, line_count_period="2026-02")
    assert len(grown) == 702
    assert len(decoded) == 2

    decoded.clear()
    retained = adopted_head_metadata(snapshot, {"payroll"})
    assert len(retained) == len(decoded) == 702
    connection.close()


def test_adopted_head_limits_are_decoded_once_across_12_48_120_months():
    # Exercise the production selector. A small authenticated close-limit list
    # must not be reparsed for every candidate publication as history grows.
    work = {}
    for months in (12, 48, 120):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(
            "CREATE TABLE period_close(period INTEGER PRIMARY KEY);"
            "CREATE TABLE calculation_publication("
            "subject_id TEXT,calculation_id TEXT,posting_period INTEGER,sequence INTEGER);"
            "CREATE INDEX publication_calculation ON calculation_publication(calculation_id);"
            "CREATE INDEX publication_posting ON "
            "calculation_publication(posting_period,subject_id);"
            "CREATE TABLE calculation("
            "id TEXT PRIMARY KEY,fact_id TEXT,kind TEXT,period INTEGER,outcome TEXT);"
            "CREATE INDEX calculation_kind_period ON calculation(kind,period);"
            "CREATE TABLE calculation_current(subject_id TEXT PRIMARY KEY,calculation_id TEXT);"
        )
        first = YearMonth("2016-01").ordinal
        last = first + months
        limits = {}
        publications = []
        calculations = []
        sequence = 0
        for offset in range(months):
            period = first + offset
            connection.execute("INSERT INTO period_close VALUES(?)", (period,))
            for index in range(50):
                subject = f"wage-{offset}-{index}"
                ident = f"calc-{offset}-{index}"
                sequence += 1
                publications.append((subject, ident, period, sequence))
                calculations.append((ident, f"fact-{ident}", "payroll", period, '{"lines":[{}]}'))
            # An adopted no-impact revision must replace the earlier posting.
            if offset == 0:
                sequence += 1
                publications.append(("wage-0-0", "revised-zero", period, sequence))
                calculations.append(
                    ("revised-zero", "fact-revised", "payroll", period, '{"lines":[]}')
                )
            # Unrelated kinds must not enlarge the returned employee set.
            for index in range(50):
                ident = f"other-{offset}-{index}"
                sequence += 1
                publications.append((ident, ident, period, sequence))
                calculations.append(
                    (ident, f"fact-{ident}", "bank_reconciliation", period, '{"lines":[]}')
                )
            limits[period] = sequence
        connection.executemany("INSERT INTO calculation_publication VALUES(?,?,?,?)", publications)
        connection.executemany("INSERT INTO calculation VALUES(?,?,?,?,?)", calculations)
        connection.execute(
            "INSERT INTO calculation_publication VALUES(?,?,?,?)",
            ("open-zero", "open-zero", last, sequence + 1),
        )
        connection.execute(
            "INSERT INTO calculation VALUES(?,?,?,?,?)",
            ("open-zero", "fact-open-zero", "payroll", last, '{"lines":[]}'),
        )
        connection.execute(
            "INSERT INTO calculation_current VALUES(?,?)", ("open-zero", "open-zero")
        )
        connection.executemany(
            "INSERT INTO calculation_publication VALUES(?,?,?,?)",
            [
                ("open-corrected", "open-before", last, sequence + 3),
                ("open-corrected", "open-after", last, sequence + 4),
            ],
        )
        connection.executemany(
            "INSERT INTO calculation VALUES(?,?,?,?,?)",
            [
                ("open-before", "fact-open-before", "payroll", last, '{"lines":[{}]}'),
                ("open-after", "fact-open-after", "payroll", last, '{"lines":[{},{}]}'),
            ],
        )
        connection.execute(
            "INSERT INTO calculation_current VALUES(?,?)", ("open-corrected", "open-after")
        )
        # A post-close revision is excluded even if it exists in the table.
        connection.execute(
            "INSERT INTO calculation_publication VALUES(?,?,?,?)",
            ("wage-0-1", "after-close", first, sequence + 2),
        )
        connection.execute(
            "INSERT INTO calculation VALUES(?,?,?,?,?)",
            ("after-close", "fact-after-close", "payroll", first, '{"lines":[]}'),
        )

        class Reads:
            def __init__(self, highwaters, through):
                self.highwaters, self.through = highwaters, through

            def authoritative_close_rows(self, *, periods, through_period):
                assert periods in (list(self.highwaters), []) and through_period == self.through
                return [{"period": period} for period in periods]

            def close_header(self, row):
                return SimpleNamespace(
                    root={"small": {"publication_sequence": self.highwaters[row["period"]]}}
                )

            def verify_sql_outcomes(self, identifiers, connection=connection):
                actual = {
                    row[0]
                    for row in connection.execute(
                        "SELECT id FROM calculation WHERE id IN (SELECT value FROM json_each(?))",
                        (json.dumps(sorted(identifiers)),),
                    )
                }
                assert actual == set(identifiers)

        snapshot = SimpleNamespace(connection=connection, month=last, reads=Reads(limits, last))
        extracts = []

        def extract_limit(raw, path, observed=extracts):
            assert path in {"$[0]", "$[1]"}
            observed.append(path)
            return json.loads(raw)[int(path[2])]

        connection.create_function("json_extract", 2, extract_limit)
        steps = [0]
        connection.set_progress_handler(
            lambda count=steps: count.__setitem__(0, count[0] + 100) or 0, 100
        )
        selected = payroll_head_metadata(
            snapshot, {"payroll"}, line_count_period=str(YearMonth.from_ordinal(last))
        )
        connection.set_progress_handler(None, 0)
        by_subject = {row["subject_id"]: row for row in selected}
        assert len(selected) == len(by_subject) == 50 * months + 2
        assert by_subject["wage-0-0"]["id"] == "revised-zero"
        assert by_subject["wage-0-0"]["line_count"] is None
        assert by_subject["wage-0-1"]["id"] == "calc-0-1"
        assert by_subject["open-zero"]["line_count"] == 0
        assert by_subject["open-corrected"]["id"] == "open-after"
        assert by_subject["open-corrected"]["line_count"] == 2
        assert all(row["kind"] == "payroll" for row in selected)
        assert len(extracts) <= 3 * months
        work[months] = steps[0]
        current = adopted_head_metadata(
            snapshot,
            {"payroll"},
            posting_period=str(YearMonth.from_ordinal(last)),
            line_count_period=str(YearMonth.from_ordinal(last)),
        )
        assert {row["subject_id"]: row["id"] for row in current} == {
            "open-zero": "open-zero",
            "open-corrected": "open-after",
        }
        connection.close()

    # This counts the production selector, including outcome selection. The
    # selected wage count grows linearly; repeatedly scanning all close limits
    # for each candidate would make the 48→120 growth quadratic.
    assert work[48] < 4 * work[12]
    assert work[120] < 4 * work[48]
    assert work[48] < 600_000
    assert work[120] < 2_500_000


def test_adopted_head_rejects_noninteger_verified_close_limit():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    month = YearMonth("2026-01").ordinal
    connection.execute("CREATE TABLE period_close(period INTEGER PRIMARY KEY)")
    connection.execute("INSERT INTO period_close VALUES(?)", (month,))

    class Reads:
        def authoritative_close_rows(self, *, periods, through_period):
            assert periods == [month] and through_period == month
            return [{"period": month}]

        def close_header(self, row):
            assert row["period"] == month
            return SimpleNamespace(root={"small": {"publication_sequence": "7"}})

    snapshot = SimpleNamespace(connection=connection, month=month, reads=Reads())
    with pytest.raises(KernelError) as failure:
        adopted_head_metadata(snapshot, {"payroll"})
    assert failure.value.code == "content_integrity_failed"
    connection.close()
