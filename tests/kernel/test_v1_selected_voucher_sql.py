"""The sealed v1 voucher selector keeps exact rows without a JSON cross scan."""

from test_engine import close, publish, save
from test_engine import engine as engine_fixture

from ai_accounting.kernel.close_storage_v1 import decode_close
from ai_accounting.kernel.report_projection_v1 import selected_voucher_sql
from ai_accounting.kernel.types import YearMonth

engine = engine_fixture


def _selected(connection, period, identifiers, authoritative, *, old_plan=False):
    options = {
        "voucher_ids": identifiers,
        "authoritative_vouchers": authoritative,
    }
    if old_plan:
        # This true predicate selects the unchanged generic candidate path,
        # retaining the former LEFT JOIN as an exact-row oracle.
        options["posting_start"] = "1900-01"
    sql, parameters = selected_voucher_sql(period, **options)
    return [dict(row) for row in connection.execute(sql + " ORDER BY period,number,id", parameters)]


def test_frozen_driven_selector_preserves_missing_current_reversal_and_scoped_rows(engine):
    save(engine, period="2025-12")
    save(engine, subject="other", request="other", period="2025-12")
    publish(engine, ["charge", "other"])
    close(engine, "2025-12")
    save(engine, amount=150, revision=1, request="changed", period="2025-12")
    publish(engine, request="correct-next-year", posting_period="2026-01")
    save(engine, subject="future", request="future", period="2026-02")
    publish(engine, ["future"], request="future-publish", posting_period="2026-02")

    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        close_row = connection.execute("SELECT * FROM period_close").fetchone()
        manifest = decode_close(connection, close_row)
        frozen = {item["id"]: close_row["period"] for item in manifest["vouchers"]}
        selected_ids = [row[0] for row in connection.execute("SELECT id FROM voucher_version")]
        closed_id = next(
            item["id"]
            for item in manifest["vouchers"]
            if connection.execute(
                "SELECT c.subject_id FROM voucher_version v "
                "JOIN calculation c ON c.id=v.calculation_id WHERE v.id=?",
                (item["id"],),
            ).fetchone()[0]
            == "charge"
        )
        scopes = (
            frozen,
            {key: value for key, value in frozen.items() if key != closed_id},
            {**frozen, closed_id: YearMonth("2026-01").ordinal},
            {**frozen, "absent-voucher": close_row["period"]},
            {**frozen, closed_id: None},
        )
        for authoritative in scopes:
            fast = _selected(connection, "2026-01", selected_ids * 2, authoritative)
            former = _selected(
                connection, "2026-01", selected_ids * 2, authoritative, old_plan=True
            )
            assert fast == former
            assert all(row["period"] <= YearMonth("2026-01").ordinal for row in fast)
            assert not any(row["basis_subject_id"] == "future" for row in fast)
        normal = _selected(connection, "2026-01", selected_ids, frozen)
        original = next(row for row in normal if row["id"] == closed_id)
        reversal = next(row for row in normal if row["reverses_id"] == original["id"])
        assert reversal["basis_calculation_id"] == original["basis_calculation_id"]
        missing = _selected(
            connection,
            "2026-01",
            selected_ids,
            {key: value for key, value in frozen.items() if key != closed_id},
        )
        assert closed_id not in {row["id"] for row in missing}

        sql, parameters = selected_voucher_sql(
            "2026-01", voucher_ids=selected_ids, authoritative_vouchers=frozen
        )
        plan = [row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + sql, parameters)]
        assert any("SCAN frozen" in row for row in plan)
        assert not any("SCAN frozen LEFT-JOIN" in row for row in plan)
