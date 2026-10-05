"""Kind scopes select immutable subjects before proving calculation identity."""

import sqlite3

import pytest
import test_cash as cash
from test_engine import close, publish, save
from test_engine import engine as engine  # noqa: F401
from test_integrity_content import damage
from test_payroll_corrections import company as company  # noqa: F401
from test_reimbursement_assets import asset
from test_reimbursement_assets import book as _asset_book

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_reads import metric_rows
from ai_accounting.kernel.query_reads import selected_voucher_sql
from ai_accounting.kernel.types import YearMonth, canonical

book = cash.book
asset_book = _asset_book


def change_kind(engine, subject, kind):
    damage(engine, "calculation", "UPDATE calculation SET kind=? WHERE subject_id=?",
           (kind, subject))


def selected_rows(snap, *, kinds, subjects=None, accounts=None, month_only=False):
    # Inspect candidate membership separately from the public journal's earlier
    # content proof. This SQL never supplies an accepted accounting result.
    sql, parameters = selected_voucher_sql(
        snap.period, kinds=kinds, subject_ids=subjects,
        posting_period=snap.period if month_only else None,
    )
    if accounts is not None:
        sql = (f"SELECT j.* FROM ({sql}) j WHERE EXISTS(SELECT 1 FROM voucher_line l "
               "WHERE l.version_id=j.id AND l.account IN (SELECT value FROM json_each(?)))")
        parameters.append(canonical(sorted(accounts)))
    return snap.connection.execute(sql, parameters).fetchall()


@pytest.mark.parametrize("filters", [{}, {"subjects": {"charge"}}, {"accounts": {"5602"}}])
def test_kind_scope_keeps_damaged_actual_voucher_for_source_rejection(engine, filters):
    save(engine)
    publish(engine)
    with Dashboard(engine)._snapshot("2026-01") as snap:
        assert len(snap.month_journal.select(kinds={"test_charge"}, **filters)) == 1
        assert snap.month_journal.select(kinds={"test_charge"}, **filters).account_amounts() == {
            "5602": [100, 0], "2202": [0, 100],
        }
    change_kind(engine, "charge", "test_source")
    with Dashboard(engine)._snapshot("2026-01") as snap:
        assert len(selected_rows(snap, kinds={"test_charge"}, month_only=True, **filters)) == 1
        with pytest.raises(KernelError) as failure:
            selected = snap.month_journal.select(kinds={"test_charge"}, **filters)
            assert len(selected) == 1
            metric_rows(selected, ("amount",))
        assert failure.value.code == "content_integrity_failed"


@pytest.mark.parametrize("entry", ["brief", "quarterly_report", "business_status"])
def test_actual_owner_amount_and_detail_routes_reject_changed_kind(book, entry):
    engine, save, publish, _ = book
    cash.expense(save)
    publish("expense")
    dashboard = Dashboard(engine)

    def read():
        if entry == "quarterly_report":
            return dashboard.quarterly_report(2026, 3, preparation="deferred")
        if entry == "business_status":
            return dashboard.business_status("2026-09", "expense")
        return dashboard.brief("2026-09")

    read()
    change_kind(engine, "expense", "income")
    with pytest.raises(KernelError) as failure:
        read()
    assert failure.value.code == "content_integrity_failed"


def test_changed_calculation_kind_cannot_enter_another_subject_kind_scope(engine):
    save(engine)
    publish(engine)
    change_kind(engine, "charge", "test_source")
    with Dashboard(engine)._snapshot("2026-01") as snap:
        assert selected_rows(snap, kinds={"test_source"}, month_only=True) == []
        assert selected_rows(snap, kinds=set(), month_only=True) == []
        with pytest.raises(KernelError) as failure:
            len(snap.month_journal.select(kinds={"test_source"}))
        assert failure.value.code == "content_integrity_failed"


@pytest.mark.parametrize("closed", [False, True])
def test_review_and_closed_original_reversal_keep_kind_scope(engine, closed):
    save(engine)
    publish(engine)
    save(engine, revision=1, request="review")
    publish(engine, request="publish-review")
    if closed:
        close(engine)
        save(engine, revision=2, request="correct", amount=150)
        publish(engine, request="publish-correct", posting_period="2026-02")
    with Dashboard(engine)._snapshot("2026-02" if closed else "2026-01") as snap:
        selected = snap.journal.select(kinds={"test_charge"})
        assert len(selected) == (3 if closed else 1)
        expected = ({"5602": [150, 100], "2202": [100, 150]} if closed
                    else {"5602": [100, 0], "2202": [0, 100]})
        assert snap.month_journal.select(kinds={"test_charge"}).account_amounts() == expected
    change_kind(engine, "charge", "test_source")
    with Dashboard(engine)._snapshot("2026-02" if closed else "2026-01") as snap:
        assert len(selected_rows(snap, kinds={"test_charge"})) == (3 if closed else 1)
        with pytest.raises(KernelError) as failure:
            selected = snap.journal.select(kinds={"test_charge"})
            assert len(selected) == (3 if closed else 1)
            snap.month_journal.select(kinds={"test_charge"}).account_amounts()
        assert failure.value.code == "content_integrity_failed"


@pytest.mark.parametrize("section", [None, "employees"])
def test_actual_employee_routes_reject_changed_posted_kind(company, section):
    company.publish("january", "february")
    dashboard = Dashboard(company.engine)
    options = {"section": section, "employee_id": "employee"} if section else {}
    dashboard.employees("2026-01", **options)
    change_kind(company.engine, "january", "income")
    with pytest.raises(KernelError) as failure:
        dashboard.employees("2026-01", **options)
    assert failure.value.code == "content_integrity_failed"


@pytest.mark.parametrize("section", [None, "assets"])
def test_actual_asset_routes_reject_changed_acquisition_kind(asset_book, section):
    engine, save, publish = asset_book
    save("reimbursed_asset", "asset-source", asset())
    publish("asset-source")
    dashboard = Dashboard(engine)
    options = {"section": section, "asset_id": "computer"} if section else {}
    dashboard.assets("2026-02", **options)
    change_kind(engine, "asset-source", "income")
    with pytest.raises(KernelError) as failure:
        dashboard.assets("2026-02", **options)
    assert failure.value.code == "content_integrity_failed"


def test_actual_selector_work_is_bounded_by_subject_kind(record_property):
    # Minimal SQLite fixture uses the actual generated selector and production
    # key/index shapes. It measures selection, not downstream content proof.
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        "CREATE TABLE subject(id TEXT PRIMARY KEY,kind TEXT NOT NULL) STRICT;"
        "CREATE INDEX subject_kind ON subject(kind,id);"
        "CREATE TABLE calculation(id TEXT PRIMARY KEY,subject_id TEXT,kind TEXT,"
        "fact_id TEXT,period INTEGER,digest BLOB) STRICT;"
        "CREATE INDEX calculation_subject ON calculation(subject_id,id);"
        "CREATE INDEX calculation_kind_period ON calculation(kind,period,id);"
        "CREATE TABLE voucher(id TEXT PRIMARY KEY,number INTEGER) STRICT;"
        "CREATE TABLE voucher_version(id TEXT PRIMARY KEY,voucher_id TEXT,"
        "calculation_id TEXT,reverses_id TEXT,period INTEGER,total INTEGER) STRICT;"
        "CREATE INDEX voucher_calculation ON voucher_version(calculation_id);"
        "CREATE TABLE voucher_current(version_id TEXT PRIMARY KEY) STRICT;"
        "CREATE TABLE calculation_current(subject_id TEXT PRIMARY KEY,calculation_id TEXT) STRICT;"
        "CREATE TABLE calculation_publication(calculation_id TEXT PRIMARY KEY,"
        "voucher_id TEXT,posting_period INTEGER) STRICT;"
        "CREATE TABLE period_close(period INTEGER PRIMARY KEY) STRICT;"
    )
    ordinal = YearMonth("2026-01").ordinal

    def insert(start, count, kind):
        for index in range(start, start + count):
            ident = str(index)
            connection.execute("INSERT INTO subject VALUES(?,?)", (ident, kind))
            connection.execute("INSERT INTO calculation VALUES(?,?,?,?,?,?)",
                               (ident, ident, kind, ident, ordinal, bytes(32)))
            connection.execute("INSERT INTO voucher VALUES(?,?)", (ident, index))
            connection.execute("INSERT INTO voucher_version VALUES(?,?,?,NULL,?,1)",
                               (ident, ident, ident, ordinal))
            connection.execute("INSERT INTO voucher_current VALUES(?)", (ident,))
            connection.execute("INSERT INTO calculation_current VALUES(?,?)", (ident, ident))
            connection.execute("INSERT INTO calculation_publication VALUES(?,?,?)",
                               (ident, ident, ordinal))

    sql, params = selected_voucher_sql("2026-01", kinds={"expense"}, no_close_references=True)

    def measured():
        steps = [0]

        def progress():
            steps[0] += 1
            return 0

        connection.set_progress_handler(progress, 1)
        try:
            rows = connection.execute(sql, params).fetchall()
        finally:
            connection.set_progress_handler(None, 0)
        return rows, steps[0]

    try:
        insert(0, 24, "expense")
        baseline, before = measured()
        insert(24, 5000, "payroll")
        expanded, after = measured()
        assert expanded == baseline and len(expanded) == 24
        assert after <= before + 20
        record_property("small_selector_vm_steps", before)
        record_property("unrelated_5000_selector_vm_steps", after)
    finally:
        connection.close()
