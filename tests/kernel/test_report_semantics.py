"""Narrow immutable report drivers retain the full calculator's visible behavior."""

import json
import sqlite3
from types import SimpleNamespace

import pytest
from test_reports import book as _report_book
from test_reports import close_quarter, scenario

from ai_accounting.kernel import close_storage
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.maintenance import Maintenance
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.report_projection import _manifest_rows, party_balance_rows
from ai_accounting.kernel.report_semantics import (
    compact_line_fields,
    compare_report_semantics,
    expand_line_fields,
    immutable_line_fields,
    persist_report_semantics,
    prepare_report_semantics,
    project_immutable_lines,
    read_report_semantics,
    repair_report_semantics,
    require_report_semantics,
)
from ai_accounting.kernel.reports import Reports, _statements
from ai_accounting.kernel.types import YearMonth, canonical

book = _report_book


@pytest.mark.parametrize(
    ("account", "kind", "cashflow", "values", "fact", "cash_fact", "obligation", "reverse"),
    [
        (
            "5602",
            "expense",
            None,
            {"obligations": [{"key": "expense-obligation"}], "unused": "not projected"},
            {"expense_class": "administration", "unused": "not projected"},
            None,
            None,
            False,
        ),
        (
            "5403",
            "tax_assessment",
            None,
            {
                "surtax_fen": 100,
                "urban_tax_fen": 70,
                "education_tax_fen": 20,
                "local_education_tax_fen": 10,
                "unused": "not projected",
            },
            {},
            None,
            None,
            True,
        ),
        (
            "1002",
            "payment",
            "operating_payments",
            {"unused": "not projected"},
            {},
            {"expense_class": "service", "unused": "not projected"},
            "2202",
            False,
        ),
        (
            "1002",
            "payroll_reserve_payment",
            "payroll",
            {},
            {},
            {},
            "222103",
            False,
        ),
        (
            "1002",
            "funding",
            "financing_receipts",
            {},
            {"funding_kind": "capital"},
            None,
            None,
            False,
        ),
        (
            "5801",
            "income_tax_assessment",
            None,
            {},
            {"tax_year": 2026},
            None,
            None,
            False,
        ),
    ],
)
def test_compact_immutable_line_keeps_statement_and_issue_semantics(
    account, kind, cashflow, values, fact, cash_fact, obligation, reverse
):
    start = YearMonth("2026-01").ordinal
    end = YearMonth("2026-03").ordinal
    amount = -100 if reverse else 100
    row = {
        "version_id": "reverse" if reverse else "voucher",
        "reverses_id": "original" if reverse else None,
        "period": start,
        "line_no": 1,
        "account": account,
        "amount": amount,
        "cashflow": cashflow,
        "classification": None,
    }
    resolution = {
        "issues": [{"field": "synthetic_relation", "message": "exact source issue"}],
        "line_relations": [],
        "obligations": [],
    }
    facts = {"source": SimpleNamespace(**fact)}
    if cash_fact is not None:
        facts["cash-source"] = SimpleNamespace(**cash_fact)
        resolution["line_relations"].append(
            {
                "line_no": 1,
                "state": "resolved",
                "role": "funds",
                "source_calculation_id": "cash-source",
                "obligation_key": "owed",
            }
        )
        resolution["obligations"].append(
            {"source_calculation_id": "cash-source", "key": "owed", "account": obligation}
        )

    def calculation(_ident):
        return {
            "fact_id": "source-fact",
            "result_digest": "ab" * 32,
            "kind": kind,
            "decoded": {"values": values},
        }

    fields = immutable_line_fields(
        row,
        "source",
        calculation=calculation,
        source_fact=facts.__getitem__,
        relations=lambda _ident: resolution,
    )
    compact = compact_line_fields(fields)
    assert compact["relation_issues"] == resolution["issues"]
    assert "unused" not in str(compact)
    assert set(compact["values"]) <= {
        "surtax_fen",
        "urban_tax_fen",
        "education_tax_fen",
        "local_education_tax_fen",
    }
    restored = expand_line_fields(compact)
    balances = {start - 1: {}, end: {}}
    parties = {start - 1: [], end: []}
    full_issues, compact_issues = [], []
    full = _statements(
        [row | fields],
        start,
        start,
        end,
        full_issues,
        account_balances=balances,
        party_balances=parties,
    )
    narrow = _statements(
        [row | restored],
        start,
        start,
        end,
        compact_issues,
        account_balances=balances,
        party_balances=parties,
    )
    assert narrow == full
    assert compact_issues == full_issues


def test_projection_uses_original_basis_and_only_profit_cash_lines():
    record = {
        "fact_id": "original-fact",
        "result_digest": "cd" * 32,
        "kind": "expense",
        "fact_data": {},
        "outcome": {"values": {"expense_class": "administration", "payload": "not retained"}},
    }
    rows = [
        {
            "posting_period": 101,
            "version_id": "reversal",
            "line_no": number,
            "account": account,
            "basis_calculation_id": "original-calculation",
        }
        for number, account in ((1, "5602"), (2, "1002"), (3, "2202"))
    ]
    resolution = {"issues": [], "line_relations": [], "obligations": []}
    projected = project_immutable_lines(
        rows,
        calculation=lambda ident: record if ident == "original-calculation" else None,
        relations=lambda ident: resolution if ident == "original-calculation" else None,
    )
    assert [item["line_no"] for item in projected] == [1, 2]
    assert {item["semantic"]["source_calculation_id"] for item in projected} == {
        "original-calculation"
    }
    assert "payload" not in str(projected)


def _isolated_semantic_copy(engine, path):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    with engine.store.connection(read_only=True) as source:
        source.backup(connection)
    return connection


def test_isolated_closed_projection_commit_read_compare_and_repair(book, tmp_path):
    engine = book[0]
    scenario(book)
    close_quarter(book)
    with _isolated_semantic_copy(engine, tmp_path / "semantic.sqlite") as connection:
        prepared = []
        for close in connection.execute("SELECT * FROM period_close ORDER BY period").fetchall():
            manifest = close_storage.decode_close(connection, close)
            sources = _manifest_rows(connection, close["period"], manifest)
            item = prepare_report_semantics(
                engine, connection, close["period"], sources, close["digest"]
            )
            header = close_storage.verified_header(connection, close)
            assert close_storage.derived_root(header, "report_semantics") == item.root_digest
            prepared.append(item)
        connection.execute("DELETE FROM report_semantic_line")
        connection.execute("DELETE FROM report_semantic_seal")
        for item in prepared:
            persist_report_semantics(connection, item)
        assert require_report_semantics(engine, connection)["periods"] == 3
        feb = YearMonth("2026-02").ordinal
        semantic = read_report_semantics(connection, feb)
        assert semantic
        assert all(key[1] > 0 for key in semantic)
        assert any(row["kind"] == "expense" for row in semantic.values())
        assert compare_report_semantics(engine, connection)["changed"] is False

        key = next(iter(semantic))
        connection.execute(
            "UPDATE report_semantic_line SET content=? "
            "WHERE posting_period=? AND version_id=? AND line_no=?",
            (canonical(semantic[key] | {"kind": "tampered"}), feb, *key),
        )
        with pytest.raises(KernelError, match="冻结根"):
            read_report_semantics(connection, feb)
        with pytest.raises(KernelError, match="显式维修"):
            require_report_semantics(engine, connection)
        result = repair_report_semantics(engine, connection)
        assert result == {"changed": True, "rows": result["rows"], "periods": 3}
        assert read_report_semantics(connection, feb) == semantic
        assert require_report_semantics(engine, connection)["periods"] == 3

        connection.execute("DELETE FROM report_semantic_seal WHERE posting_period=?", (feb,))
        with pytest.raises(KernelError, match="冻结根"):
            read_report_semantics(connection, feb)
        assert repair_report_semantics(engine, connection)["changed"] is True
        assert read_report_semantics(connection, feb) == semantic


def test_isolated_projection_cannot_repair_wrong_frozen_root(book, tmp_path, monkeypatch):
    engine = book[0]
    scenario(book)
    close_quarter(book)
    with _isolated_semantic_copy(engine, tmp_path / "wrong-root.sqlite") as connection:
        close = connection.execute("SELECT * FROM period_close ORDER BY period LIMIT 1").fetchone()
        manifest = close_storage.decode_close(connection, close)
        sources = _manifest_rows(connection, close["period"], manifest)
        item = prepare_report_semantics(
            engine, connection, close["period"], sources, close["digest"]
        )
        original = close_storage.derived_root

        def wrong_root(header, name):
            if name == "report_semantics" and header.period == close["period"]:
                return bytes(32)
            return original(header, name)

        monkeypatch.setattr(close_storage, "derived_root", wrong_root)
        with pytest.raises(KernelError, match="冻结根"):
            persist_report_semantics(connection, item)
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(KernelError, match="不可变冻结来源"):
            repair_report_semantics(engine, connection)


def test_real_closed_report_matches_full_open_authority_before_close(book, monkeypatch):
    engine = book[0]
    scenario(book)
    reference = Reports(engine).report(2026, 1, source="open")
    close_quarter(book)
    with engine.store.connection(read_only=True) as connection:
        cash_only_source = connection.execute(
            "SELECT c.id FROM calculation c JOIN subject s ON s.id=c.subject_id "
            "WHERE s.id='capital'"
        ).fetchone()[0]
    primed = []
    original_prime = QueryReads.prime_calculations

    def track_prime(reads, identifiers, *, ancestors=True):
        primed.extend(identifiers)
        return original_prime(reads, identifiers, ancestors=ancestors)

    monkeypatch.setattr(QueryReads, "prime_calculations", track_prime)
    frozen = Reports(engine).report(2026, 1, source="closed")
    assert frozen["statements"] == reference["statements"]
    assert frozen["checks"] == reference["checks"]
    assert frozen["fact_issues"] == reference["fact_issues"]
    assert cash_only_source not in primed


def test_real_integrity_and_explicit_repair_rebuild_semantics_from_source(book):
    engine = book[0]
    scenario(book)
    close_quarter(book)
    with engine.store.connection() as connection:
        row = connection.execute(
            "SELECT posting_period,version_id,line_no,content FROM report_semantic_line "
            "ORDER BY posting_period,version_id,line_no LIMIT 1"
        ).fetchone()
        damaged = json.loads(row["content"])
        damaged["kind"] = "damaged"
        connection.execute(
            "UPDATE report_semantic_line SET content=? "
            "WHERE posting_period=? AND version_id=? AND line_no=?",
            (canonical(damaged), row["posting_period"], row["version_id"], row["line_no"]),
        )
        connection.commit()
    with pytest.raises(KernelError, match="报表语义"):
        Maintenance(engine).verify_integrity()
    repaired = Maintenance(engine).rebuild_projections(request_id="repair-semantic-test")
    assert repaired["changed"] is True
    with engine.store.connection(read_only=True) as connection:
        assert require_report_semantics(engine, connection)["periods"] == 3
        assert (
            connection.execute(
                "SELECT content FROM report_semantic_line "
                "WHERE posting_period=? AND version_id=? AND line_no=?",
                (row["posting_period"], row["version_id"], row["line_no"]),
            ).fetchone()[0]
            == row["content"]
        )


def test_report_rejects_selected_voucher_omitted_from_rooted_cash_lines(book, monkeypatch):
    import ai_accounting.kernel.reports as report_module

    engine = book[0]
    scenario(book)
    close_quarter(book)
    with engine.store.connection(read_only=True) as connection:
        voucher_id = connection.execute(
            "SELECT v.id FROM voucher_version v JOIN calculation c ON c.id=v.calculation_id "
            "WHERE c.subject_id='capital'"
        ).fetchone()[0]
    original = report_module._report_vouchers

    def omitted(*args, **kwargs):
        sql, params = original(*args, **kwargs)
        return "SELECT * FROM (" + sql + ") WHERE id<>?", (*params, voucher_id)

    monkeypatch.setattr(report_module, "_report_vouchers", omitted)
    # Force the authenticated monthly summary to its exact row fallback; this
    # checks that a changed selection still cannot omit a frozen source line.
    import ai_accounting.kernel.report_flow as flow_module

    monkeypatch.setattr(flow_module, "read_report_flow", lambda *_args, **_kwargs: None)
    with pytest.raises(KernelError, match="缺少已冻结凭证行"):
        Reports(engine).report(2026, 1, source="closed")


def test_semantic_read_reuses_successfully_verified_report_month(book, monkeypatch):
    import ai_accounting.kernel.report_projection as projection

    engine = book[0]
    scenario(book)
    close_quarter(book)
    checked = []
    original = projection._stored_month

    def check_month(connection, period):
        checked.append(period)
        return original(connection, period)

    monkeypatch.setattr(projection, "_stored_month", check_month)
    with QueryReads.snapshot(engine) as reads:
        cutoff = YearMonth("2026-03").ordinal
        feb = YearMonth("2026-02").ordinal
        assert (
            party_balance_rows(engine, reads.connection, cutoff, source="closed", reads=reads)
            is not None
        )
        before = len(checked)
        assert before == 0
        assert read_report_semantics(reads.connection, feb, reads=reads)
        assert checked == [feb]
