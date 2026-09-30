"""The released position reader keeps its exact classification source set."""

import json
import sqlite3
from dataclasses import replace

import pytest
from pydantic import ValidationError
from test_integrity_content import damage
from test_reports import book as book_fixture
from test_reports import scenario

from ai_accounting.kernel import close_storage_v1, content_v1
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.position_v1 import (
    PositionInputsV1,
    _position_classification_ids,
    position_v1,
)
from ai_accounting.kernel.storage import Store

book = book_fixture


def _original_ids(connection, month):
    closed = {
        row[0]
        for row in connection.execute(
            "SELECT DISTINCT r.reference_id FROM close_reference r "
            "JOIN fact_revision f ON f.id=r.reference_id JOIN subject s ON s.id=f.subject_id "
            "WHERE r.reference_type='fact' AND r.path='readiness.financial_reports.facts[*]' "
            "AND s.kind='report_classification' AND r.close_period<=?",
            (month,),
        )
    }
    closed.update(
        row[0]
        for row in connection.execute(
            "SELECT f.id FROM fact_current c JOIN fact_revision f ON f.id=c.fact_id "
            "JOIN subject s ON s.id=f.subject_id WHERE s.kind='report_classification' "
            "AND f.period<=? AND NOT EXISTS(SELECT 1 FROM period_close p WHERE p.period=f.period)",
            (month,),
        )
    )
    if not closed:
        return set()
    return {
        row[0]
        for row in connection.execute(
            "WITH selected AS (SELECT value id FROM json_each(?)), "
            "duplicate_vouchers AS (SELECT c.voucher_version_id "
            "FROM selected ids JOIN fact_report_classification c ON c.revision_id=ids.id "
            "GROUP BY c.voucher_version_id HAVING count(*)>1) "
            "SELECT ids.id FROM selected ids LEFT JOIN fact_report_classification c "
            "ON c.revision_id=ids.id WHERE c.revision_id IS NULL "
            "OR c.voucher_version_id IN (SELECT voucher_version_id FROM duplicate_vouchers) "
            "OR EXISTS(SELECT 1 FROM fact_report_classification_counterparties child "
            "WHERE child.revision_id=ids.id)",
            (json.dumps(sorted(closed)),),
        )
    }


def test_v1_position_selector_keeps_exact_closed_open_and_damage_candidates():
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        "CREATE TABLE subject(id TEXT PRIMARY KEY,kind TEXT);"
        "CREATE TABLE fact_revision(id TEXT PRIMARY KEY,subject_id TEXT,period INTEGER);"
        "CREATE TABLE fact_current(fact_id TEXT);"
        "CREATE TABLE period_close(period INTEGER PRIMARY KEY);"
        "CREATE TABLE close_reference("
        "reference_id TEXT,reference_type TEXT,path TEXT,close_period INTEGER);"
        "CREATE TABLE fact_report_classification(revision_id TEXT,voucher_version_id TEXT);"
        "CREATE TABLE fact_report_classification_counterparties(revision_id TEXT,line_no INTEGER);"
    )
    connection.executemany(
        "INSERT INTO subject VALUES(?,?)",
        [(name, "report_classification") for name in (
            "plain", "party", "duplicate_a", "duplicate_b", "missing", "bad_child",
            "open", "future_closed", "future_open", "wrong_path",
        )] + [("wrong_kind", "report_profile")],
    )
    connection.executemany(
        "INSERT INTO fact_revision VALUES(?,?,?)",
        [(name, name, period) for name, period in (
            ("plain", 1), ("party", 1), ("duplicate_a", 1), ("duplicate_b", 1),
            ("missing", 1), ("bad_child", 1), ("open", 2), ("future_closed", 3),
            ("future_open", 3), ("wrong_path", 1), ("wrong_kind", 1),
        )],
    )
    connection.executemany("INSERT INTO period_close VALUES(?)", [(1,), (3,)])
    path = "readiness.financial_reports.facts[*]"
    connection.executemany(
        "INSERT INTO close_reference VALUES(?,'fact',?,?)",
        [(name, path, period) for name, period in (
            ("plain", 1), ("party", 1), ("duplicate_a", 1), ("duplicate_b", 1),
            ("party", 1), ("missing", 1), ("bad_child", 1), ("future_closed", 3),
            ("wrong_kind", 1),
        )] + [("wrong_path", "another.path", 1)],
    )
    connection.executemany(
        "INSERT INTO fact_current VALUES(?)", [("open",), ("future_open",)],
    )
    connection.executemany(
        "INSERT INTO fact_report_classification VALUES(?,?)",
        [("plain", "v-plain"), ("party", "v-party"),
         ("duplicate_a", "v-duplicate"), ("duplicate_b", "v-duplicate"),
         ("bad_child", "v-bad"), ("open", "v-open"),
         ("future_closed", "v-future-closed"), ("future_open", "v-future-open"),
         ("wrong_path", "v-wrong")],
    )
    connection.executemany(
        "INSERT INTO fact_report_classification_counterparties VALUES(?,?)",
        [("party", 1), ("bad_child", 0), ("open", 1),
         ("future_closed", 1), ("future_open", 1), ("wrong_path", 1)],
    )
    try:
        for month in (1, 2, 3):
            assert _position_classification_ids(connection, month) == _original_ids(
                connection, month
            )
        assert _position_classification_ids(connection, 2) == {
            "party", "duplicate_a", "duplicate_b", "missing", "bad_child", "open"
        }
        assert _position_classification_ids(connection, 3) == {
            "party", "duplicate_a", "duplicate_b", "missing", "bad_child",
            "future_closed", "open"
        }
    finally:
        connection.close()


def test_v1_position_selector_accepts_bundle_without_classification_type():
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        "CREATE TABLE subject(id TEXT PRIMARY KEY,kind TEXT);"
        "CREATE TABLE fact_revision(id TEXT PRIMARY KEY,subject_id TEXT,period INTEGER);"
        "CREATE TABLE fact_current(fact_id TEXT);"
        "CREATE TABLE period_close(period INTEGER PRIMARY KEY);"
        "CREATE TABLE close_reference("
        "reference_id TEXT,reference_type TEXT,path TEXT,close_period INTEGER);"
    )
    try:
        assert _position_classification_ids(connection, 1) == set()
        connection.execute("INSERT INTO subject VALUES('classification','report_classification')")
        connection.execute("INSERT INTO fact_revision VALUES('fact','classification',1)")
        connection.execute("INSERT INTO fact_current VALUES('fact')")
        # A candidate with no typed table is corruption, not the valid old bundle.
        with pytest.raises(sqlite3.OperationalError, match="fact_report_classification"):
            _position_classification_ids(connection, 1)
    finally:
        connection.close()


@pytest.mark.parametrize("damage_kind", ["missing_typed", "bad_child"])
def test_v1_position_selected_damaged_classification_still_rejects(
    book, damage_kind, tmp_path, monkeypatch, request
):
    engine, save, _, close = book
    scenario(book, classification=False)
    with engine.store.connection(read_only=True) as connection:
        voucher_id, line_no = connection.execute(
            "SELECT v.id,l.line_no FROM voucher_version v "
            "JOIN calculation c ON c.id=v.calculation_id "
            "JOIN voucher_line l ON l.version_id=v.id "
            "WHERE c.subject_id='cost' AND l.account='2202'"
        ).fetchone()
    save(
        "report_classification", "party-class-" + voucher_id,
        {"period": "2026-02", "voucher_version_id": voucher_id,
         "profit_details": [
             {"line_no": 1, "detail_code": "management_entertainment", "amount_fen": 10000}
         ],
         "counterparties": [{"line_no": line_no, "counterparty_id": "supplier"}]},
    )
    close("2026-01")
    close("2026-02")
    with engine.store.connection(read_only=True) as connection:
        fact_id = connection.execute(
            "SELECT f.id FROM fact_current c JOIN fact_revision f ON f.id=c.fact_id "
            "JOIN subject s ON s.id=f.subject_id WHERE s.id=?",
            ("party-class-" + voucher_id,),
        ).fetchone()[0]
    contract_dir = tmp_path / "schema_contracts"
    contract_dir.mkdir()
    (contract_dir / "content-v1.json").write_text(
        json.dumps(content_v1.content_contract(engine.store.registry), ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(content_v1, "__file__", str(tmp_path / "content_v1.py"))
    content_v1.v1_registry.cache_clear()
    request.addfinalizer(content_v1.v1_registry.cache_clear)
    historical_store = Store(
        engine.store.path,
        replace(engine.store.bundle, registry=content_v1.v1_registry()),
        engine.store.company_id,
        engine.store.database_id,
    )
    historical_engine = Engine(historical_store)

    def read_position():
        with engine.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT * FROM period_close WHERE period=(SELECT max(period) FROM period_close)"
            ).fetchone()
            manifest = close_storage_v1.decode_close(connection, row)
            with historical_content(1):
                assert fact_id in _position_classification_ids(connection, row["period"])
                return position_v1(PositionInputsV1(historical_engine, connection, manifest))

    baseline = read_position()
    assert baseline["complete"]
    if damage_kind == "missing_typed":
        damage(
            engine, "fact_report_classification",
            "DELETE FROM fact_report_classification WHERE revision_id=?",
            (fact_id,), foreign_keys=False,
        )
        expected = (KernelError, KeyError)
    else:
        damage(
            engine, "fact_report_classification_counterparties",
            "UPDATE fact_report_classification_counterparties SET counterparty_id='' "
            "WHERE revision_id=?",
            (fact_id,),
        )
        expected = (ValidationError, ValueError)
    with pytest.raises(expected):
        read_position()
