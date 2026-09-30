"""Read-only Stage 9 report-line projection experiment on synthetic books.

This deliberately lives outside the production schema. It freezes row-level
inputs and derived line contributions, then checks SQL aggregation against the
existing Reports implementation. It is not a report cache or a read path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "src"))

from ai_accounting.kernel import reports as report_module  # noqa: E402
from ai_accounting.kernel.query_reads import QueryReads  # noqa: E402
from ai_accounting.kernel.query_semantics import classify_financial_position  # noqa: E402
from ai_accounting.kernel.reports import Reports  # noqa: E402
from ai_accounting.kernel.service import LocalService  # noqa: E402
from ai_accounting.kernel.types import canonical, sum_fen  # noqa: E402


def checked_book(path: Path) -> dict:
    book = json.loads(path.read_text(encoding="utf-8"))
    root = Path(book["root"]).resolve()
    assert root.is_relative_to((REPOSITORY / ".tmp").resolve())
    assert book["status"] == "complete" and book["integrity"]["status"] == "verified"
    assert book["months"] and book["months"][-1]["closed"] is False
    return book


def captured_reports(engine, year, quarter):
    original = report_module._statements
    captured = {}
    current = None

    def capture(rows, start, year_start, end, problems, *, account_balances=None):
        before = len(problems)
        result = original(rows, start, year_start, end, problems, account_balances=account_balances)
        captured[current] = {
            "rows": [dict(row) for row in rows],
            "start": start,
            "year_start": year_start,
            "end": end,
            "balances": account_balances,
            "statements": result,
            "statement_issues": list(problems[before:]),
        }
        return result

    report_module._statements = capture
    try:
        reports = Reports(engine)
        with QueryReads.snapshot(engine) as reads:
            for current in ("open", "closed"):
                captured[current] = {}
                captured[current]["plan"] = reports._report(
                    year, quarter, source=current, connection=reads.connection, reads=reads
                )
    finally:
        report_module._statements = original
    return captured


def _single_row_effect(row):
    """Prototype-only derivation through the current statement rules."""
    if row.get("opening"):
        return [], []
    period = row["period"]
    issues = []
    statements = report_module._statements(
        [row],
        period,
        period,
        period,
        issues,
        account_balances={period - 1: {}, period: {}},
    )
    effects = []
    for statement in ("profit_statement", "cash_flow_statement"):
        for line, item in statements[statement].items():
            if statement == "cash_flow_statement" and line in {"21", "22"}:
                continue
            if item["current_fen"]:
                effects.append((statement, int(line), item["current_fen"]))
    relevant = [
        issue
        for issue in issues
        if issue["field"]
        in {"report_classification.profit_details", "report_classification.cash_details"}
    ]
    return effects, list({canonical(issue): issue for issue in relevant}.values())


def _position_parts(row):
    amount = row["amount"]
    if not amount:
        return (), False
    account = row["account"]
    if account not in report_module._POSITION_ACCOUNTS:
        return (), True
    if account not in report_module.RECLASS:
        return (), False
    splits = row.get("party_splits")
    if (
        splits is None
        and row.get("party_key") is not None
        and row.get("party_state") != "unresolved"
    ):
        splits = ((row["party_key"], amount),)
    if splits is None and row.get("party") is not None:
        party = row["party"]
        key = party if isinstance(party, tuple) else ("party", party)
        splits = ((key, amount),)
    if splits is None:
        return (), True
    normalized = []
    try:
        for item in splits:
            party, part = (
                item
                if not isinstance(item, dict)
                else (item.get("party_key"), item.get("amount_fen"))
            )
            if party is None or type(part) is not int:
                return (), True
            normalized.append((party, part))
    except (TypeError, ValueError):
        return (), True
    if sum(part for _, part in normalized) != amount:
        return (), True
    return normalized, False


def create_projection(db: sqlite3.Connection, source: str, capture: dict, source_info: dict):
    rows, effects, issues, parties = [], [], [], []
    for row_no, row in enumerate(capture["rows"]):
        calc_id = row.get("calculation_id")
        source_calc_id = source_info["originals"].get(row.get("reverses_id"), calc_id)
        source_record = source_info["calculations"].get(source_calc_id, {})
        rows.append(
            (
                source,
                row_no,
                row.get("version_id"),
                row.get("line_no"),
                row["period"],
                row["account"],
                row["amount"],
                row.get("kind"),
                row.get("cashflow"),
                row.get("reverses_id"),
                calc_id,
                source_calc_id,
                source_record.get("fact_id"),
                source_record.get("digest"),
                source_record.get("publication_id"),
                int(bool(row.get("opening"))),
            )
        )
        contributions, row_issues = _single_row_effect(row)
        effects.extend(
            (source, row_no, row["period"], name, line, amount)
            for name, line, amount in contributions
        )
        issues.extend(
            (source, row_no, row["period"], item["field"], canonical(item)) for item in row_issues
        )
        splits, invalid = _position_parts(row)
        parties.extend(
            (source, row_no, row["period"], row["account"], canonical(party), part)
            for party, part in splits
        )
        if invalid:
            issues.append(
                (
                    source,
                    row_no,
                    row["period"],
                    "position_invalid",
                    canonical(
                        {
                            "account": row["account"],
                            "amount": row["amount"],
                            "version_id": row.get("version_id"),
                            "reverses_id": row.get("reverses_id"),
                            "line_no": row.get("line_no"),
                        }
                    ),
                )
            )
    db.executemany("INSERT INTO report_line_source VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    db.executemany("INSERT INTO report_line_effect VALUES(?,?,?,?,?,?)", effects)
    db.executemany("INSERT INTO report_line_issue VALUES(?,?,?,?,?)", issues)
    db.executemany("INSERT INTO report_line_party VALUES(?,?,?,?,?,?)", parties)
    return {
        "rows": len(rows),
        "effects": len(effects),
        "issues": len(issues),
        "party_parts": len(parties),
    }


def position(db, source, cutoff, totals):
    if totals is None:
        return {line: None for line in range(1, 54)}, []
    selected = []
    for account, party, amount in db.execute(
        "SELECT account,party_key,sum(amount) FROM report_line_party "
        "WHERE source=? AND period<=? GROUP BY account,party_key",
        (source, cutoff),
    ):
        if amount:
            selected.append(
                {
                    "account": account,
                    "amount": amount,
                    "party_splits": ((tuple(json.loads(party)), amount),),
                }
            )
    for (payload,) in db.execute(
        "SELECT payload FROM report_line_issue WHERE source=? AND period<=? "
        "AND field='position_invalid' ORDER BY row_no",
        (source, cutoff),
    ):
        selected.append(json.loads(payload))
    selected.extend(
        {"account": account, "amount": amount}
        for account, amount in sorted(totals.items())
        if account not in report_module.RECLASS and account in report_module._POSITION_ACCOUNTS
    )
    classified = classify_financial_position(selected)
    return classified["lines"], classified["issues"]


def statement_effects(db, source, name, begin, end, line_names):
    values = {line: 0 for line in line_names}
    for line, amount in db.execute(
        "SELECT line,sum(amount) FROM report_line_effect WHERE source=? AND statement=? "
        "AND period>=? AND period<=? GROUP BY line",
        (source, name, begin, end),
    ):
        values[line] = amount
    return values


def projected_statements(db, source, capture):
    start, year_start, end = capture["start"], capture["year_start"], capture["end"]
    balances = capture["balances"]
    begin_balance, begin_issues = position(db, source, year_start - 1, balances[year_start - 1])
    ending_balance, ending_issues = position(db, source, end, balances[end])
    current_profit = statement_effects(
        db, source, "profit_statement", start, end, report_module.PROFIT_NAMES
    )
    ytd_profit = statement_effects(
        db, source, "profit_statement", year_start, end, report_module.PROFIT_NAMES
    )
    current_cash = statement_effects(
        db, source, "cash_flow_statement", start, end, report_module.CASH_FLOW_NAMES
    )
    ytd_cash = statement_effects(
        db, source, "cash_flow_statement", year_start, end, report_module.CASH_FLOW_NAMES
    )
    for begin, values in ((start, current_cash), (year_start, ytd_cash)):
        totals = balances[begin - 1]
        values[21] = (
            None
            if totals is None
            else sum_fen(totals.get(account, 0) for account in report_module.CASH_ACCOUNTS)
        )
        values[22] = None if values[21] is None else values[21] + values[20]
    output = {
        "balance_sheet": {
            str(line): {
                "name": name,
                "beginning_fen": begin_balance[line],
                "ending_fen": ending_balance[line],
            }
            for line, name in report_module.BALANCE_NAMES.items()
        },
        "profit_statement": {
            str(line): {
                "name": name,
                "current_fen": current_profit[line],
                "year_to_date_fen": ytd_profit[line],
            }
            for line, name in report_module.PROFIT_NAMES.items()
        },
        "cash_flow_statement": {
            str(line): {
                "name": name,
                "current_fen": current_cash[line],
                "year_to_date_fen": ytd_cash[line],
            }
            for line, name in report_module.CASH_FLOW_NAMES.items()
        },
    }
    issues = begin_issues + ending_issues
    for field, begin in (
        ("report_classification.profit_details", start),
        ("report_classification.profit_details", year_start),
        ("report_classification.cash_details", start),
        ("report_classification.cash_details", year_start),
    ):
        issues.extend(
            json.loads(row[0])
            for row in db.execute(
                "SELECT payload FROM report_line_issue WHERE source=? AND period>=? "
                "AND period<=? AND field=? ORDER BY row_no",
                (source, begin, end, field),
            )
        )
    for begin in (start, year_start):
        if db.execute(
            "SELECT 1 FROM report_line_source WHERE source=? AND period>=? AND period<=? "
            "AND account IN (SELECT value FROM json_each(?)) "
            "AND kind IN ('funds_transfer','cash_bank_transfer','bank_platform_transfer') "
            "AND cashflow<>'managed_reserve_outflow' GROUP BY version_id "
            "HAVING sum(amount)<>0 LIMIT 1",
            (source, begin, end, canonical(sorted(report_module.CASH_ACCOUNTS))),
        ).fetchone():
            issues.append(report_module.issue("cash_transfer", "内部资金划转未完整抵销"))
    return output, issues


def source_info(connection, captures):
    reverses = {
        row["reverses_id"]
        for capture in captures.values()
        for row in capture["rows"]
        if row.get("reverses_id")
    }
    originals = {
        row["id"]: row["calculation_id"]
        for row in connection.execute(
            "SELECT id,calculation_id FROM voucher_version WHERE id IN "
            "(SELECT value FROM json_each(?))",
            (canonical(sorted(reverses)),),
        )
    }
    ids = {
        originals.get(row.get("reverses_id"), row.get("calculation_id"))
        for capture in captures.values()
        for row in capture["rows"]
    } - {None}
    calculations = {
        row["id"]: {
            "fact_id": row["fact_id"],
            "digest": row["digest"].hex(),
            "publication_id": row["publication_id"],
        }
        for row in connection.execute(
            "SELECT c.id,c.fact_id,c.digest,p.id publication_id FROM json_each(?) ids "
            "JOIN calculation c ON c.id=ids.value "
            "LEFT JOIN calculation_publication p ON p.calculation_id=c.id",
            (canonical(sorted(ids)),),
        )
    }
    assert len(calculations) == len(ids)
    return {"originals": originals, "calculations": calculations}


def measured_query(db, action):
    instructions = [0]

    def progress():
        instructions[0] += 1000
        return 0

    db.set_progress_handler(progress, 1000)
    started = time.perf_counter()
    try:
        value = action()
    finally:
        db.set_progress_handler(None, 0)
    return value, (time.perf_counter() - started) * 1000, instructions[0]


def authoritative_row_probe(engine, db, source):
    """Measure exact source-pointer checks; this is not a full seal verifier."""
    projected = list(
        db.execute(
            "SELECT version_id,line_no,period,account,amount,source_calculation_id,"
            "source_fact_id,source_digest,publication_id,opening "
            "FROM report_line_source WHERE source=? ORDER BY row_no",
            (source,),
        )
    )
    calculation_ids = sorted({row[5] for row in projected if row[5]})
    voucher_ids = sorted({row[0] for row in projected if row[0] and not row[9]})

    def verify(connection):
        calculations = {
            row["id"]: (row["fact_id"], row["digest"].hex(), row["publication_id"])
            for row in connection.execute(
                "SELECT c.id,c.fact_id,c.digest,p.id publication_id FROM json_each(?) ids "
                "JOIN calculation c ON c.id=ids.value "
                "LEFT JOIN calculation_publication p ON p.calculation_id=c.id",
                (canonical(calculation_ids),),
            )
        }
        lines = {
            (row["id"], row["line_no"]): (row["period"], row["account"], row["amount"])
            for row in connection.execute(
                "SELECT v.id,v.period,l.line_no,l.account,l.debit-l.credit amount "
                "FROM json_each(?) ids JOIN voucher_version v ON v.id=ids.value "
                "JOIN voucher_line l ON l.version_id=v.id",
                (canonical(voucher_ids),),
            )
        }
        mismatches = []
        for row in projected:
            (
                version_id,
                line_no,
                period,
                account,
                amount,
                calc_id,
                fact_id,
                dgst,
                pub_id,
                opening,
            ) = row
            if calc_id and calculations.get(calc_id) != (fact_id, dgst, pub_id):
                mismatches.append(("calculation", calc_id))
            if not opening and lines.get((version_id, line_no)) != (period, account, amount):
                mismatches.append(("voucher_line", version_id, line_no))
        return {
            "calculation_ids": len(calculations),
            "voucher_lines": len(lines),
            "mismatches": mismatches[:20],
        }

    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        result, elapsed, vm = measured_query(connection, lambda: verify(connection))
    result.update(source_pointer_ms=elapsed, sqlite_vm_steps_approx=vm)
    assert not result["mismatches"], result
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--book-report", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    book = checked_book(args.book_report)
    output = args.output.resolve()
    assert output.is_relative_to((REPOSITORY / ".tmp").resolve())
    assert not output.exists(), "Use a new temporary SQLite output"
    app = LocalService(Path(book["root"]).resolve())
    started = time.perf_counter()
    try:
        company = next(c for c in app.catalog.companies() if c["id"] == book["company"]["id"])
        assert company["database_id"] == book["company"]["database_id"]
        engine = app.engine(company["id"])
        selected_period = book["months"][-1]["period"]
        year, month = (int(value) for value in selected_period.split("-"))
        captures = captured_reports(engine, year, (month - 1) // 3 + 1)
        with engine.store.connection(read_only=True) as source:
            source.execute("BEGIN")
            info = source_info(source, captures)
        db = sqlite3.connect(output)
        try:
            db.executescript("""
                CREATE TABLE report_line_source(source TEXT,row_no INTEGER,version_id TEXT,
                  line_no INTEGER,period INTEGER,account TEXT,amount INTEGER,kind TEXT,
                  cashflow TEXT,reverses_id TEXT,calculation_id TEXT,
                  source_calculation_id TEXT,source_fact_id TEXT,source_digest TEXT,
                  publication_id TEXT,opening INTEGER,PRIMARY KEY(source,row_no));
                CREATE TABLE report_line_effect(source TEXT,row_no INTEGER,period INTEGER,
                  statement TEXT,line INTEGER,amount INTEGER);
                CREATE INDEX effect_scope ON report_line_effect(source,statement,period,line);
                CREATE TABLE report_line_issue(source TEXT,row_no INTEGER,period INTEGER,
                  field TEXT,payload TEXT);
                CREATE INDEX issue_scope ON report_line_issue(source,field,period,row_no);
                CREATE TABLE report_line_party(source TEXT,row_no INTEGER,period INTEGER,
                  account TEXT,party_key TEXT,amount INTEGER);
                CREATE INDEX party_scope ON report_line_party(source,period,account,party_key);
            """)
            counts = {}
            for source, capture in captures.items():
                counts[source] = create_projection(db, source, capture, info)
            db.commit()
            comparisons = {}
            for source, capture in captures.items():
                (statements, issues), elapsed, vm = measured_query(
                    db,
                    lambda source=source, capture=capture: projected_statements(
                        db, source, capture
                    ),
                )
                expected = capture["statements"]
                differences = [
                    (name, line, field, item[field], statements[name][line][field])
                    for name, lines in expected.items()
                    for line, item in lines.items()
                    for field in item
                    if field.endswith("_fen")
                    if item[field] != statements[name][line][field]
                ]
                actual_issues = {canonical(issue) for issue in issues}
                expected_issues = {canonical(issue) for issue in capture["statement_issues"]}
                comparisons[source] = {
                    "statement_mismatches": differences[:20],
                    "statement_issue_missing": sorted(expected_issues - actual_issues)[:20],
                    "statement_issue_extra": sorted(actual_issues - expected_issues)[:20],
                    "expected_statement_issues": len(expected_issues),
                    "projected_statement_issues": len(actual_issues),
                    "sql_read_ms": elapsed,
                    "sqlite_vm_steps_approx": vm,
                    "source_fact_ids": len(capture["plan"]["report_fact_ids"]),
                    "source_calculations": len(
                        {
                            row[0]
                            for row in db.execute(
                                "SELECT source_calculation_id FROM report_line_source "
                                "WHERE source=? AND source_calculation_id IS NOT NULL",
                                (source,),
                            )
                        }
                    ),
                    "original_status": capture["plan"]["status"],
                }
                assert not differences, comparisons[source]
                assert actual_issues == expected_issues, comparisons[source]
                comparisons[source]["authoritative_pointer_probe"] = authoritative_row_probe(
                    engine, db, source
                )
            db_bytes = output.stat().st_size
            row_digest = hashlib.sha256(
                canonical(
                    [
                        tuple(row)
                        for row in db.execute(
                            "SELECT * FROM report_line_source ORDER BY source,row_no"
                        )
                    ]
                ).encode("utf-8")
            ).hexdigest()
            report = {
                "status": "validated",
                "book": str(args.book_report.resolve()),
                "output": str(output),
                "company_id": company["id"],
                "build_and_check_ms": (time.perf_counter() - started) * 1000,
                "sqlite_bytes": db_bytes,
                "row_digest": row_digest,
                "counts": counts,
                "comparisons": comparisons,
            }
            report_path = output.with_suffix(".json")
            assert not report_path.exists()
            report_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(json.dumps(report, ensure_ascii=False), flush=True)
        finally:
            db.close()
    finally:
        app.close()


if __name__ == "__main__":
    main()
