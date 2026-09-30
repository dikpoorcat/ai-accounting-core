"""The contribution source read must stay bounded as old vouchers grow."""

import sqlite3

from ai_accounting.kernel import report_open_contribution, report_open_contribution_v1


def _selected_rows(connection, reader):
    steps = [0]

    def count_steps():
        steps[0] += 100
        return 0

    connection.set_progress_handler(count_steps, 100)
    try:
        rows = reader._publication_rows(connection, "selected")
    finally:
        connection.set_progress_handler(None, 0)
    return [tuple(row.values()) if isinstance(row, dict) else tuple(row) for row in rows], steps[0]


def test_open_contribution_rows_are_bounded_by_selected_calculation():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(
            "CREATE TABLE voucher_version("
            "id TEXT PRIMARY KEY,calculation_id TEXT,reverses_id TEXT);"
            "CREATE INDEX voucher_calculation ON voucher_version(calculation_id);"
            "CREATE INDEX voucher_version_reverses ON voucher_version(reverses_id);"
            "CREATE TABLE voucher_line("
            "version_id TEXT,line_no INTEGER,account TEXT,debit INTEGER,"
            "credit INTEGER,cashflow TEXT,PRIMARY KEY(version_id,line_no));"
        )
        connection.executemany(
            "INSERT INTO voucher_version VALUES(?,?,?)",
            (("a", "selected", None), ("b", "selected", None), ("c", "selected", "a")),
        )
        connection.executemany(
            "INSERT INTO voucher_line VALUES(?,?,?,?,?,?)",
            (
                ("a", 1, "1002", 300, 0, "in"),
                ("a", 2, "6001", 0, 300, None),
                ("b", 1, "1002", 100, 0, "in"),
                ("c", 1, "1002", 500, 0, "in"),
            ),
        )
        expected = [
            ("a", "selected", None, 1, "1002", 300, 0, "in"),
            ("a", "selected", None, 2, "6001", 0, 300, None),
            ("b", "selected", None, 1, "1002", 100, 0, "in"),
        ]
        before = {}
        for reader in (report_open_contribution, report_open_contribution_v1):
            before[reader.__name__] = _selected_rows(connection, reader)
            assert before[reader.__name__][0] == expected

        connection.executemany(
            "INSERT INTO voucher_version VALUES(?,?,?)",
            ((f"old-{index:05d}", f"unrelated-{index:05d}", None) for index in range(4000)),
        )
        connection.executemany(
            "INSERT INTO voucher_line VALUES(?,?,?,?,?,?)",
            ((f"old-{index:05d}", 1, "1002", index, 0, None) for index in range(4000)),
        )
        for reader in (report_open_contribution, report_open_contribution_v1):
            rows, steps = _selected_rows(connection, reader)
            assert rows == expected
            assert steps < before[reader.__name__][1] + 500
    finally:
        connection.close()
