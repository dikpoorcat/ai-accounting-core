"""The employee list evaluates line counts only for its selected posting month."""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard_reads import adopted_head_metadata, payroll_head_metadata
from ai_accounting.kernel.types import YearMonth


def complete_selector_sources(connection, limits):
    """Supply real identity lanes and frozen locators to the SQL-only fixture."""
    connection.executescript(
        "ALTER TABLE calculation ADD COLUMN subject_id TEXT;"
        "ALTER TABLE calculation_publication ADD COLUMN id INTEGER;"
        "ALTER TABLE calculation_publication ADD COLUMN mode TEXT;"
        "CREATE TABLE fact_revision(id TEXT PRIMARY KEY,subject_id TEXT,period INTEGER,"
        "revision INTEGER,UNIQUE(subject_id,revision));"
        "CREATE TABLE fact_current(subject_id TEXT PRIMARY KEY,fact_id TEXT UNIQUE);"
        "CREATE TABLE close_reference(close_period INTEGER,position TEXT,"
        "reference_type TEXT,reference_id TEXT,path TEXT,"
        "PRIMARY KEY(close_period,path,position,reference_type));"
        "CREATE INDEX IF NOT EXISTS subject_kind ON subject(kind,id);"
        "DROP INDEX IF EXISTS calculation_kind_period;"
        "CREATE INDEX calculation_kind_period ON calculation(kind,period,id);"
        "CREATE INDEX calculation_subject ON calculation(subject_id,id);"
        "CREATE INDEX fact_period ON fact_revision(period,subject_id,id);"
        "CREATE INDEX fact_id_subject_cover ON fact_revision(id,subject_id);"
        "DROP INDEX IF EXISTS publication_subject;"
        "CREATE INDEX publication_subject ON calculation_publication(subject_id,posting_period,id);"
        "DROP INDEX IF EXISTS publication_calculation;"
        "DROP INDEX IF EXISTS publication_posting;"
        "CREATE INDEX publication_posting ON calculation_publication(posting_period,subject_id,id);"
        "CREATE INDEX close_reference_lookup ON "
        "close_reference(reference_type,reference_id,close_period);"
        "CREATE INDEX close_reference_direct_adoption ON close_reference("
        "reference_type,reference_id,path,close_period,position) "
        "WHERE path='adopted_results[*].fact_id' OR path='adopted_results[*].calculation_id';"
    )
    refresh_selector_sources(connection, limits)


def refresh_selector_sources(connection, limits):
    connection.execute("UPDATE calculation_publication SET id=rowid,mode='initial'")
    connection.execute(
        "UPDATE calculation SET subject_id=(SELECT p.subject_id FROM "
        "calculation_publication p WHERE p.calculation_id=calculation.id LIMIT 1)"
    )
    connection.execute(
        "INSERT OR IGNORE INTO fact_revision SELECT fact_id,subject_id,period,"
        "row_number() OVER(PARTITION BY subject_id ORDER BY id) FROM calculation"
    )
    connection.execute("DELETE FROM fact_current")
    connection.execute(
        "INSERT INTO fact_current SELECT cc.subject_id,c.fact_id "
        "FROM calculation_current cc JOIN calculation c ON c.id=cc.calculation_id"
    )
    connection.execute("DELETE FROM close_reference")
    position = 0
    for period, highwater in limits.items():
        for row in connection.execute(
            "SELECT p.calculation_id,c.fact_id FROM calculation_publication p "
            "JOIN calculation c ON c.id=p.calculation_id "
            "WHERE p.posting_period=? AND p.sequence<=? AND NOT EXISTS("
            "SELECT 1 FROM calculation_publication n WHERE n.subject_id=p.subject_id "
            "AND n.posting_period=p.posting_period AND n.sequence>p.sequence AND n.sequence<=?)",
            (period, highwater, highwater),
        ).fetchall():
            position += 1
            connection.executemany(
                "INSERT INTO close_reference VALUES(?,?,?,?,?)",
                [(period, position, "calculation", row[0], "adopted_results[*].calculation_id"),
                 (period, position, "fact", row[1], "adopted_results[*].fact_id")],
            )


def verify_fixture_close_references(connection, references):
    if not references:
        return
    identifiers = {row["reference_id"] for row in references}
    sources = {row[0]: (row[1], row[2]) for row in connection.execute(
        "SELECT c.id,c.fact_id,p.posting_period FROM json_each(?) ids "
        "CROSS JOIN calculation c ON c.id=ids.value "
        "JOIN calculation_publication p ON p.calculation_id=c.id",
        (json.dumps(sorted(identifiers)),),
    )}
    assert sources.keys() == identifiers
    positions = sorted({(row["close_period"], row["position"]) for row in references})
    facts = {(row[0], row[1]): row[2] for row in connection.execute(
        "WITH requested(period,position) AS (VALUES "
        + ",".join("(?,?)" for _ in positions) + ") "
        "SELECT r.close_period,r.position,r.reference_id FROM requested k "
        "CROSS JOIN close_reference r ON r.close_period=k.period AND r.position=k.position "
        "WHERE r.path='adopted_results[*].fact_id'",
        tuple(value for position in positions for value in position),
    )}
    assert facts.keys() == set(positions)
    for row in references:
        assert row["path"] == "adopted_results[*].calculation_id"
        fact, period = sources[row["reference_id"]]
        assert period == row["close_period"]
        assert facts[row["close_period"], row["position"]] == fact


def test_real_head_selector_does_not_decode_growing_old_outcomes():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        "CREATE TABLE period_close(period INTEGER);"
        "CREATE TABLE subject(id TEXT PRIMARY KEY,kind TEXT);"
        "CREATE TABLE calculation_publication("
        "subject_id TEXT,calculation_id TEXT UNIQUE,posting_period INTEGER,sequence INTEGER);"
        "CREATE TABLE calculation("
        "id TEXT PRIMARY KEY,fact_id TEXT,kind TEXT,period INTEGER,outcome TEXT);"
        "CREATE TABLE calculation_current(subject_id TEXT PRIMARY KEY,calculation_id TEXT);"
    )
    old_month = YearMonth("2026-01").ordinal
    month = YearMonth("2026-02").ordinal
    connection.execute("INSERT INTO period_close VALUES(?)", (old_month,))

    def add_heads(prefix, count, posting_month, line_count):
        for index in range(count):
            ident = f"{prefix}-{index}"
            connection.execute("INSERT INTO subject VALUES(?,?)", (ident, "payroll"))
            connection.execute(
                "INSERT INTO calculation_publication"
                "(subject_id,calculation_id,posting_period,sequence) VALUES(?,?,?,1)",
                (ident, ident, posting_month),
            )
            connection.execute(
                "INSERT INTO calculation(id,fact_id,kind,period,outcome) VALUES(?,?,?,?,?)",
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
    complete_selector_sources(connection, {old_month: 1})

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

        def verify_close_references(self, references):
            verify_fixture_close_references(connection, references)

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
    refresh_selector_sources(connection, {old_month: 1})
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
            "CREATE TABLE subject(id TEXT PRIMARY KEY,kind TEXT);"
            "CREATE INDEX subject_kind ON subject(kind,id);"
            "CREATE TABLE calculation_publication("
            "subject_id TEXT,calculation_id TEXT UNIQUE,posting_period INTEGER,sequence INTEGER);"
            "CREATE INDEX publication_calculation ON calculation_publication(calculation_id);"
            # Mirror the real publication_subject index now that authoritative
            # business subjects, rather than mutable c.kind, locate this scope.
            "CREATE INDEX publication_subject ON "
            "calculation_publication(subject_id,posting_period);"
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
        connection.executemany(
            "INSERT INTO calculation_publication"
            "(subject_id,calculation_id,posting_period,sequence) VALUES(?,?,?,?)", publications,
        )
        connection.executemany(
            "INSERT INTO calculation(id,fact_id,kind,period,outcome) VALUES(?,?,?,?,?)",
            calculations,
        )
        connection.executemany(
            "INSERT INTO subject VALUES(?,?)",
            sorted(
                {
                    (row[0], "bank_reconciliation" if row[0].startswith("other-") else "payroll")
                    for row in publications
                }
            ),
        )
        connection.executemany(
            "INSERT INTO subject VALUES(?,?)",
            [("open-zero", "payroll"), ("open-corrected", "payroll")],
        )
        connection.execute(
            "INSERT INTO calculation_publication"
            "(subject_id,calculation_id,posting_period,sequence) VALUES(?,?,?,?)",
            ("open-zero", "open-zero", last, sequence + 1),
        )
        connection.execute(
            "INSERT INTO calculation(id,fact_id,kind,period,outcome) VALUES(?,?,?,?,?)",
            ("open-zero", "fact-open-zero", "payroll", last, '{"lines":[]}'),
        )
        connection.execute(
            "INSERT INTO calculation_current VALUES(?,?)", ("open-zero", "open-zero")
        )
        connection.executemany(
            "INSERT INTO calculation_publication"
            "(subject_id,calculation_id,posting_period,sequence) VALUES(?,?,?,?)",
            [
                ("open-corrected", "open-before", last, sequence + 3),
                ("open-corrected", "open-after", last, sequence + 4),
            ],
        )
        connection.executemany(
            "INSERT INTO calculation(id,fact_id,kind,period,outcome) VALUES(?,?,?,?,?)",
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
            "INSERT INTO calculation_publication"
            "(subject_id,calculation_id,posting_period,sequence) VALUES(?,?,?,?)",
            ("wage-0-1", "after-close", first, sequence + 2),
        )
        connection.execute(
            "INSERT INTO calculation(id,fact_id,kind,period,outcome) VALUES(?,?,?,?,?)",
            ("after-close", "fact-after-close", "payroll", first, '{"lines":[]}'),
        )
        complete_selector_sources(connection, limits)

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

            def verify_close_references(self, references, connection=connection):
                verify_fixture_close_references(connection, references)

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
        print(json.dumps({"months": months, "selector_vm_steps": steps[0],
                          "selected_heads": len(selected), "limit_extracts": len(extracts)}))
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
    # Complete frozen locators extend the old partial-schema read scope. Guard
    # its growth, not obsolete absolute VM budgets or whole-page latency.
    assert work[48] < 4 * work[12]
    assert work[120] < 4 * work[48]


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
