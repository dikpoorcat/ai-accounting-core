"""Full verifiers own only missing snapshots and retain caller transactions."""

import sqlite3
from contextlib import closing

import pytest
from test_engine import engine as engine, publish, save  # noqa: F401
from test_integrity_content import damage
from test_development_upgrade import create_old_company
from monthly_close_fixture import ready, close_months
from test_migration_steps import declarations, migration_bundle, snapshot, source
from test_stage9_backup_upgrade_history import _released_zip
from test_v1_stored_json_isolation import released_book, _block_current_decoders  # noqa: F401
from test_reports import book as book, scenario, close_quarter  # noqa: F401

from ai_accounting.kernel import backup, integrity, offline_development_upgrade as development
from ai_accounting.kernel import report_projection, report_semantics
from ai_accounting.kernel import settlement_projection, versions
from ai_accounting.kernel import runtime
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.close_review import CloseReview
from ai_accounting.kernel.runtime import connect
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.schema_bundle import verify_company_with_registry


def full_check(boundary, engine, connection):
    if boundary == "direct":
        return integrity.verify_integrity(engine, connection)
    if boundary == "registered":
        return verify_company_with_registry(connection, engine.store.bundle, engine.store.registry)
    return backup._verify_connection(
        connection, engine.store.bundle, allow_previous=False,
        expected_company_id=engine.store.company_id,
        expected_database_id=engine.store.database_id,
    )["verification"]


@pytest.mark.parametrize("boundary", ["direct", "registered", "backup"])
def test_external_commit_cannot_mix_full_verification_snapshot(engine, monkeypatch, boundary):
    original = integrity._check_sources
    observed = []

    def concurrent_commit(selected, connection, *args, **kwargs):
        before = connection.execute("SELECT count(*) FROM evidence").fetchone()[0]
        engine.register_evidence(
            b"concurrent synthetic evidence", "text/plain", "race.txt", request_id="race-proof"
        )
        after = connection.execute("SELECT count(*) FROM evidence").fetchone()[0]
        observed.append((connection.in_transaction, before, after))
        return original(selected, connection, *args, **kwargs)

    monkeypatch.setattr(integrity, "_check_sources", concurrent_commit)
    with engine.store.connection(read_only=True) as connection:
        trace = []
        connection.set_trace_callback(trace.append)
        assert not connection.in_transaction
        result = full_check(boundary, engine, connection)
        assert result["counts"]["evidence"] == 0
        assert observed == [(True, 0, 0)]
        assert not connection.in_transaction
        assert connection.execute("SELECT count(*) FROM evidence").fetchone()[0] == 1
        assert trace.count("BEGIN") == trace.count("ROLLBACK") == 1


@pytest.mark.parametrize("boundary", ["direct", "registered", "backup"])
@pytest.mark.parametrize("begin", [None, "BEGIN", "BEGIN IMMEDIATE"])
@pytest.mark.parametrize("failed", [False, True])
def test_verification_restores_only_owned_transaction(engine, monkeypatch, boundary, begin, failed):
    if failed:
        def reject(*args, **kwargs):
            raise RuntimeError("synthetic verifier failure")
        monkeypatch.setattr(integrity, "_check_sources", reject)
    with engine.store.connection() as connection:
        if begin:
            connection.execute(begin)
        if begin == "BEGIN IMMEDIATE":
            connection.execute("UPDATE state SET management=management+1 WHERE id=1")
        before = connection.execute("SELECT management FROM state").fetchone()[0]
        trace = []
        connection.set_trace_callback(trace.append)
        if failed:
            with pytest.raises(RuntimeError, match="synthetic verifier failure"):
                full_check(boundary, engine, connection)
        else:
            assert full_check(boundary, engine, connection)["status"] == "verified"
        assert connection.in_transaction is bool(begin)
        assert connection.execute("SELECT management FROM state").fetchone()[0] == before
        assert "COMMIT" not in trace
        assert trace.count("BEGIN") == trace.count("ROLLBACK") == (0 if begin else 1)
        if begin:
            connection.rollback()
        assert not connection.in_transaction
        if begin == "BEGIN IMMEDIATE":
            assert connection.execute("SELECT management FROM state").fetchone()[0] == before - 1


@pytest.mark.parametrize("boundary", ["direct", "registered", "backup"])
def test_owned_snapshot_still_rejects_damaged_content(engine, boundary):
    save(engine)
    publish(engine)
    damage(engine, "monthly_account", "UPDATE monthly_account SET debit=debit+1 WHERE debit>0")
    with engine.store.connection() as connection:
        with pytest.raises((integrity.KernelError, backup.BackupError)):
            full_check(boundary, engine, connection)
        assert not connection.in_transaction


def test_caller_verified_lease_survives_nested_full_check(engine):
    from ai_accounting.kernel.verified_source_lease import (
        verified_source_lease, current_verified_lease,
    )
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        with verified_source_lease(connection) as outer:
            assert full_check("backup", engine, connection)["status"] == "verified"
            assert current_verified_lease(connection) is outer
            outer.require(connection)
            assert connection.in_transaction


def test_five_page_consumers_and_close_preview_do_not_enter_full_snapshot_wrapper(
    book, monkeypatch,
):
    company, _, _, close = book
    close("2026-01")
    with company.store.connection(read_only=True) as connection:
        proof = connection.execute("SELECT digest FROM evidence WHERE name='proof'").fetchone()[0].hex()
    def blocked(*args, **kwargs):
        raise AssertionError("page/close preview entered full verification snapshot")
    ready(company, proof, first="2026-02", last="2026-02")
    monkeypatch.setattr(runtime, "verification_snapshot", blocked)
    dashboard = Dashboard(company)
    assert dashboard.brief("2026-01", limit=20)["selected_period"]["key"] == "2026-01"
    assert dashboard.funds(
        "2026-01", movement_account_type="cash", movement_account_id="cash", limit=20,
    )["selected_period"]["key"] == "2026-01"
    assert dashboard.employees("2026-01", limit=20)["selected_period"]["key"] == "2026-01"
    assert dashboard.assets("2026-01", limit=20)["selected_period"]["key"] == "2026-01"
    dashboard.quarterly_report(2026, 1, preparation="deferred")
    assert CloseReview(None, company).read("2026-01")["state"] == "closed"
    assert Periods(company).preview_close("2026-02", owner_confirmation=proof)["status"] == "preview"


def test_draft_initial_and_skip_checks_have_one_snapshot(tmp_path, monkeypatch):
    path, source_bundle, target = create_old_company(
        tmp_path / "company.sqlite", source_fingerprint=development.INDEX_SOURCE_FINGERPRINT,
    )
    checked = []
    original = development._verify_connection

    def recorded(connection, *args, **kwargs):
        checked.append(connection.in_transaction)
        return original(connection, *args, **kwargs)

    monkeypatch.setattr(development, "_verify_connection", recorded)
    with closing(connect(path)) as connection:
        assert development.upgrade_company(connection, target)["status"] == "upgraded"
        assert not connection.in_transaction
        assert checked == [True, True, True]
        checked.clear()
        assert development.upgrade_company(connection, target) == {"status": "verified_skip"}
        assert checked == [True] and not connection.in_transaction
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


@pytest.mark.parametrize("close_count", [1, 2])
def test_autocommit_verifier_reuses_settlement_and_frozen_report_work(
    tmp_path, monkeypatch, close_count,
):
    path, source_bundle, _ = create_old_company(
        tmp_path / "company.sqlite", source_fingerprint=development.INDEX_SOURCE_FINGERPRINT,
    )
    company = Engine(Store(path, source_bundle, "synthetic-development-company", "synthetic-development-database"))
    proof = company.register_evidence(
        b"synthetic monthly no-business confirmation", "text/plain", "owner.txt", request_id="owner-proof",
    )["digest"]
    last = "2026-01" if close_count == 1 else "2026-02"
    ready(company, proof, first="2026-01", last=last)
    close_months(Periods(company), proof, first="2026-01", last=last)
    if close_count == 2:
        for number in range(12):
            company.register_evidence(
                f"unrelated synthetic evidence {number}".encode(), "text/plain", "unrelated.txt",
                request_id=f"unrelated-{number}",
            )
    counts = {}

    def counted(module, name):
        original = getattr(module, name)
        key = module.__name__ + "." + name
        def call(*args, **kwargs):
            counts[key] = counts.get(key, 0) + 1
            return original(*args, **kwargs)
        monkeypatch.setattr(module, name, call)
        return key

    settlement = counted(settlement_projection, "compare_settlement_projection")
    reports = counted(report_projection, "_manifest_rows")
    semantics = counted(report_semantics, "_prepared_from_source")
    with closing(connect(path)) as connection:
        result = backup._verify_connection(connection, source_bundle, allow_previous=False)
        assert result["verification"]["status"] == "verified"
        assert result["verification"]["counts"]["closes"] == close_count
        assert not connection.in_transaction
    assert counts.get(settlement, 0) == 1
    assert counts.get(reports, 0) == close_count
    # Semantics and flow import the same real _manifest_rows inside their calls;
    # the one report-row rebuild above therefore also counts their fallback.
    assert counts.get(semantics, 0) == close_count


def test_formal_source_callback_entire_history_read_is_one_snapshot(tmp_path):
    path = tmp_path / "migration.sqlite"
    with closing(sqlite3.connect(path, isolation_level=None)) as connection:
        source(connection)
        connection.execute("PRAGMA journal_mode=WAL")
        readings = []

        def checked(candidate):
            assert candidate.in_transaction
            before = candidate.execute("SELECT amount FROM ledger WHERE id=7").fetchone()[0]
            if not readings:
                with closing(sqlite3.connect(path, isolation_level=None)) as writer:
                    writer.execute("UPDATE ledger SET amount=26 WHERE id=7")
            after = candidate.execute("SELECT amount FROM ledger WHERE id=7").fetchone()[0]
            readings.append((before, after))
            if before != 25:
                raise RuntimeError("source changed before write lock")

        with pytest.raises(RuntimeError, match="source changed before write lock"):
            versions.upgrade(connection, bundle=migration_bundle(), verify_source_content=checked)
        assert readings == [(25, 25), (26, 26)]
        assert not connection.in_transaction
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert versions.verify_schema(connection, bundle=migration_bundle(), allow_previous=True) == 1


@pytest.mark.parametrize("extra_history", [False, True])
def test_nonempty_frozen_values_and_actual_decode_work_survive_unrelated_history(
    book, monkeypatch, record_property, extra_history,
):
    from ai_accounting.kernel import close_storage
    company = book[0]
    reports = scenario(book)
    close_quarter(book)
    frozen_before = [Periods(company).closed_report(month) for month in (
        "2026-01", "2026-02", "2026-03",
    )]
    before = reports.report(2026, 1)
    statements = before["statements"]
    assert statements["balance_sheet"]["1"]["ending_fen"] == 40000
    assert statements["balance_sheet"]["51"]["ending_fen"] == -10000
    assert statements["profit_statement"]["14"]["current_fen"] == 10000
    assert statements["cash_flow_statement"]["6"]["current_fen"] == 10000
    if extra_history:
        for number in range(12):
            company.register_evidence(
                f"unrelated synthetic evidence {number}".encode(), "text/plain", "unrelated.txt",
                request_id=f"unrelated-{number}",
            )
            Periods(company).management(
                "capital", note=f"unrelated management revision {number}",
                payment_period=None, payment_category=None, expected_revision=number,
                request_id=f"management-{number}",
            )
    assert reports.report(2026, 1)["statements"] == statements
    assert [Periods(company).closed_report(month) for month in (
        "2026-01", "2026-02", "2026-03",
    )] == frozen_before
    counts = {"full_closes": 0, "outcomes": 0, "report_rows": 0}
    original_close, original_outcome = close_storage.decode_close, integrity._outcome
    original_rows = report_projection._manifest_rows
    def decoded_close(*args, **kwargs):
        counts["full_closes"] += 1
        return original_close(*args, **kwargs)
    def decoded_outcome(*args, **kwargs):
        counts["outcomes"] += 1
        return original_outcome(*args, **kwargs)
    def rebuilt_rows(*args, **kwargs):
        result = original_rows(*args, **kwargs)
        counts["report_rows"] += len(result)
        return result
    monkeypatch.setattr(close_storage, "decode_close", decoded_close)
    monkeypatch.setattr(integrity, "_outcome", decoded_outcome)
    monkeypatch.setattr(report_projection, "_manifest_rows", rebuilt_rows)
    with company.store.connection(read_only=True) as connection:
        calculation_count = connection.execute("SELECT count(*) FROM calculation").fetchone()[0]
        frozen_rows = connection.execute(
            "SELECT count(*) FROM report_line_source WHERE scope='closed'"
        ).fetchone()[0]
        assert calculation_count > 0 and frozen_rows > 0
        result = full_check("backup", company, connection)
        assert not connection.in_transaction
    assert result["status"] == "verified"
    assert result["counts"]["closes"] == counts["full_closes"] == 3
    assert result["counts"]["calculations"] == counts["outcomes"] == calculation_count
    assert counts["report_rows"] == frozen_rows
    for name, value in counts.items():
        record_property(name, value)
    record_property("unrelated_evidence_and_management_versions", 12 if extra_history else 0)


@pytest.mark.parametrize("point", ["after_ddl", "after_validate", "before_commit"])
def test_future_migration_fault_preserves_source_and_foreign_keys(point):
    with closing(sqlite3.connect(":memory:")) as connection:
        source(connection)
        before = snapshot(connection)
        states = []
        def check(candidate):
            states.append(candidate.in_transaction)
        def fail(seen):
            if seen == point:
                raise RuntimeError("synthetic migration fault")
        with pytest.raises(RuntimeError, match="synthetic migration fault"):
            versions.upgrade(
                connection, bundle=migration_bundle(declarations()),
                verify_source_content=check, verify_target=check, fault=fail,
            )
        assert states and all(states)
        assert snapshot(connection) == before
        assert not connection.in_transaction
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_portable_forward_restore_source_callbacks_are_snapshots(tmp_path, monkeypatch):
    source_path, archive, target, company = _released_zip(tmp_path)
    original = versions._execute_steps
    callback_states = []
    def observed(connection, steps, **options):
        callback = options["verify_source"]
        def checked(candidate):
            callback_states.append(candidate.in_transaction)
            return callback(candidate)
        return original(connection, steps, **(options | {"verify_source": checked}))
    monkeypatch.setattr(versions, "_execute_steps", observed)
    before = backup._digest(archive)
    result = backup.restore_portable(
        archive, tmp_path / "restored.sqlite", _bundle=target,
        expected_company_id=company["id"],
    )
    assert callback_states == [True, True]
    assert result["verification"]["status"] == "verified"
    assert result["source_database_format"]["version"] == 1
    assert result["database_format"]["version"] == 2
    assert backup._digest(archive) == before


def test_fixed_v1_direct_registered_verifier_keeps_decoder_and_owned_snapshot(
    released_book, monkeypatch,
):
    book, bundle = released_book
    # Load the actual historical consumers before forbidding current decoders.
    assert backup.verify_file(book.engine.store.path, _bundle=bundle)["verification"]["status"] == "verified"
    with monkeypatch.context() as future:
        _block_current_decoders(future)
        from ai_accounting.kernel import close_storage_v1
        calls = []
        original = close_storage_v1.decode_close
        def recorded(connection, *args, **kwargs):
            calls.append(connection.in_transaction)
            return original(connection, *args, **kwargs)
        future.setattr(close_storage_v1, "decode_close", recorded)
        with book.engine.store.connection(read_only=True) as connection:
            assert not connection.in_transaction
            result = bundle.company_verifiers[1](connection, bundle)
            assert result["status"] == "verified" and result["counts"]["closes"] > 0
            assert calls and all(calls)
            assert not connection.in_transaction
