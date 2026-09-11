"""Consecutive close freezes each month atomically without synthetic publications."""

import json
import sqlite3

import pytest
from test_new_company_reports import profile
from test_opening_continuation import book as book  # noqa: F401
from test_reports import book as reporting_book  # noqa: F401
from test_reports import scenario

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods
from ai_accounting.kernel.reports import Reports
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.types import YearMonth, canonical, digest


def ready(engine, proof, first="2026-01", last="2026-03", *, omit=()):
    periods = Periods(engine)
    for ordinal in range(YearMonth(first).ordinal, YearMonth(last).ordinal + 1):
        month = str(YearMonth.from_ordinal(ordinal))
        for category in MATERIAL_CATEGORIES:
            if (month, category) not in omit:
                periods.inventory(
                    month,
                    category,
                    evidence=[],
                    expected=0,
                    no_business=True,
                    confirmation_evidence=proof,
                    request_id=f"inventory-{month}-{category}",
                )


def snapshot(engine):
    with engine.store.connection(read_only=True) as connection:
        return {
            "epochs": engine.store.epochs(connection),
            **{
                table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
                for table in ("period_close", "request", "audit", "jobs", "voucher_version")
            },
        }


def arguments(preview, **changes):
    return {
        "from_period": preview["from_period"],
        "through_period": preview["through_period"],
        "owner_confirmation": preview["owner_confirmation"],
        "preview_digest": preview["digest"],
        "epochs": preview["epochs"],
        "request_id": "range-close",
        **changes,
    }


def authorizer(calls):
    def authorize(connection, first, last, preview_digest, epochs):
        assert connection.in_transaction
        proof = {
            "approval_id": "synthetic-batch",
            "from_period": first,
            "through_period": last,
            "preview_digest": preview_digest,
            "epochs": epochs,
        }
        calls.append(proof)
        # Transactional surrogate for the company-local security receipt. Security
        # integration tests exercise the real immutable, unique consumption table.
        connection.execute(
            "INSERT INTO audit(request_id,action,payload) VALUES(?,?,?)",
            ("range-close", "synthetic_authorization", canonical(proof)),
        )
        return proof

    return authorize


def test_range_freezes_zero_formation_reports_once_and_leaves_august_open(book, tmp_path):
    engine, save, _, package, proof = book
    package([])
    profile(save, "2026-01")
    for quarter in (1, 2):
        save(
            "report_income_tax_confirmation",
            f"tax-{quarter}",
            {
                "period": f"2026-{quarter * 3:02d}",
                "treatment": "zero",
                "cumulative_assessed_fen": 0,
                "explanation": "Explicit synthetic zero tax",
            },
        )
    ready(engine, proof, last="2026-07")
    calls = []
    periods = Periods(engine, authorize_close_range=authorizer(calls))
    before = snapshot(engine)
    preview = periods.preview_close_range("2026-01", "2026-07", owner_confirmation=proof)
    assert snapshot(engine) == before
    assert preview["month_count"] == 7 and not preview["closed_prefix"]
    assert [item["period"] for item in preview["manifests"]] == [
        f"2026-{month:02d}" for month in range(1, 8)
    ]
    result = periods.close_range(**arguments(preview, backup_directory=str(tmp_path / "backups")))
    assert len(calls) == 1 and len(result["results"]) == 7
    previous = None
    for item, prepared in zip(result["results"], preview["manifests"], strict=True):
        frozen = periods.closed_report(item["period"])
        assert frozen["previous_close_digest"] == previous
        assert frozen["password_confirmation"] == calls[0]
        assert digest(frozen).hex() == item["digest"]
        for field in ("facts", "calculations", "inventories", "readiness", "trial_balance"):
            assert frozen[field] == prepared[field]
        previous = item["digest"]
    assert preview["manifests"][0]["facts"]
    with pytest.raises(KernelError, match="尚未关账"):
        periods.closed_report("2026-08")
    after = snapshot(engine)
    assert after["epochs"] == {
        **before["epochs"],
        "accounting": before["epochs"]["accounting"] + 1,
        "material": before["epochs"]["material"] + 1,
    }
    assert len(after["jobs"]) == 1
    with engine.store.connection(read_only=True) as connection:
        job = connection.execute(
            "SELECT * FROM jobs WHERE id=?", (result["backup_job"],)
        ).fetchone()
        payload = json.loads(job["payload"])
        assert job["status"] == "pending" and job["kind"] == "portable_backup"
        assert payload["close_period"] == "2026-07" and payload["close_digest"] == previous
        assert payload["rollover"] is True
        assert (
            connection.execute(
                "SELECT count(*) FROM audit WHERE action='close_range_month'"
            ).fetchone()[0]
            == 7
        )
    assert after["voucher_version"] == before["voucher_version"]


def test_missing_middle_month_blocks_preview_and_entire_confirmation(book):
    engine, _, _, _, proof = book
    ready(engine, proof, omit={("2026-02", "tax")})
    calls = []
    periods = Periods(engine, authorize_close_range=authorizer(calls))
    before = snapshot(engine)
    with pytest.raises(KernelError) as failed:
        periods.preview_close_range("2026-01", "2026-03", owner_confirmation=proof)
    assert failed.value.code == "period_not_ready"
    assert failed.value.details["closing_period"] == "2026-02"
    assert failed.value.details["fact_issues"][0]["closing_period"] == "2026-02"
    with pytest.raises(KernelError):
        periods.close_range(
            "2026-01",
            "2026-03",
            owner_confirmation=proof,
            preview_digest="00" * 32,
            epochs=before["epochs"],
            request_id="range-close",
        )
    assert snapshot(engine) == before and not calls


@pytest.mark.parametrize(
    "stage", ["close_range_month:2026-01", "close_range_month:2026-02", "commit"]
)
def test_range_failure_rolls_back_all_months_authorization_audit_and_backup(book, tmp_path, stage):
    engine, _, _, _, proof = book
    ready(engine, proof)
    calls = []
    periods = Periods(engine, authorize_close_range=authorizer(calls))
    preview = periods.preview_close_range("2026-01", "2026-03", owner_confirmation=proof)
    before = snapshot(engine)

    def fail(actual, connection):
        if actual != stage:
            return
        if stage == "commit":
            connection.set_authorizer(
                lambda code, arg, *_: (
                    sqlite3.SQLITE_DENY
                    if code == sqlite3.SQLITE_TRANSACTION and arg == "COMMIT"
                    else sqlite3.SQLITE_OK
                )
            )
        else:
            raise RuntimeError("Synthetic interrupted batch")

    engine.fault = fail
    request = arguments(preview, backup_directory=str(tmp_path / "backups"))
    with pytest.raises((RuntimeError, sqlite3.DatabaseError)):
        periods.close_range(**request)
    assert snapshot(engine) == before
    engine.fault = lambda *_: None
    assert periods.close_range(**request)["status"] == "closed"
    assert len(calls) == 2


def test_closed_prefix_is_unchanged_and_replay_never_consumes_again(book, tmp_path):
    engine, _, _, _, proof = book
    ready(engine, proof)
    periods = Periods(engine)
    first = periods.preview_close("2026-01", owner_confirmation=proof)
    closed = periods.close(
        "2026-01",
        owner_confirmation=proof,
        preview_digest=first["digest"],
        epochs=first["epochs"],
        request_id="single-close",
    )
    original = snapshot(engine)["period_close"][0]
    calls = []
    periods.authorize_close_range = authorizer(calls)
    preview = periods.preview_close_range("2026-01", "2026-03", owner_confirmation=proof)
    assert preview["closed_prefix"] == [{"period": "2026-01", "digest": closed["digest"]}]
    assert preview["month_count"] == 2
    request = arguments(preview, backup_directory=str(tmp_path / "backups"))
    result = periods.close_range(**request)
    after = snapshot(engine)
    assert after["period_close"][0] == original
    assert periods.closed_report("2026-02")["previous_close_digest"] == closed["digest"]
    assert periods.close_range(**request) == result
    assert snapshot(engine) == after and len(calls) == 1
    with pytest.raises(KernelError) as conflict:
        periods.close_range(**(request | {"through_period": "2026-04"}))
    assert conflict.value.code == "idempotency_conflict"
    complete = periods.preview_close_range("2026-01", "2026-03", owner_confirmation=proof)
    assert complete["status"] == "already_closed" and complete["month_count"] == 0
    with pytest.raises(KernelError) as duplicate:
        periods.close_range(**arguments(complete, request_id="another-close"))
    assert duplicate.value.code == "already_closed"
    assert snapshot(engine) == after and len(calls) == 1


@pytest.mark.parametrize(
    "first,last,code",
    [
        ("2026-03", "2026-02", "invalid_close_range"),
        ("2026-03", "2026-04", "close_range_not_contiguous"),
        ("2025-12", "2026-02", "closed_range_gap"),
    ],
)
def test_range_cannot_skip_open_months_or_reopen_before_closed_prefix(book, first, last, code):
    engine, _, _, _, proof = book
    ready(engine, proof, last="2026-01")
    periods = Periods(engine)
    preview = periods.preview_close_range("2026-01", "2026-01", owner_confirmation=proof)
    periods.close_range(**arguments(preview))
    before = snapshot(engine)
    with pytest.raises(KernelError) as rejected:
        periods.preview_close_range(first, last, owner_confirmation=proof)
    assert rejected.value.code == code and snapshot(engine) == before


@pytest.mark.parametrize("lane", ["accounting", "material", "management"])
def test_range_checks_all_epochs_including_frozen_management_notes(book, lane):
    engine, save, _, package, proof = book
    package([])
    ready(engine, proof)
    periods = Periods(engine)
    preview = periods.preview_close_range("2026-01", "2026-03", owner_confirmation=proof)
    if lane == "accounting":
        save(
            "report_income_tax_confirmation",
            "tax",
            {
                "period": "2026-03",
                "treatment": "zero",
                "cumulative_assessed_fen": 0,
                "explanation": "Synthetic newly confirmed tax",
            },
        )
    elif lane == "material":
        periods.inventory(
            "2026-02",
            "tax",
            evidence=[],
            expected=0,
            no_business=True,
            confirmation_evidence=proof,
            request_id="reconfirmed-inventory",
        )
    else:
        periods.management(
            "opening",
            note="Optional note",
            payment_period=None,
            payment_category=None,
            expected_revision=0,
            request_id="management",
        )
    before = snapshot(engine)
    with pytest.raises(KernelError) as stale:
        periods.close_range(**arguments(preview))
    assert stale.value.code == "preview_expired"
    assert snapshot(engine) == before


def test_range_digest_binds_order_and_exact_boundary_before_authorization(book):
    engine, _, _, _, proof = book
    ready(engine, proof)
    calls = []
    periods = Periods(engine, authorize_close_range=authorizer(calls))
    preview = periods.preview_close_range("2026-01", "2026-03", owner_confirmation=proof)
    for changes in ({"through_period": "2026-02"}, {"preview_digest": "00" * 32}):
        with pytest.raises(KernelError) as mismatch:
            periods.close_range(**arguments(preview, **changes))
        assert mismatch.value.code == "preview_expired"
    assert not calls and not snapshot(engine)["period_close"]


def test_range_does_not_skip_earlier_unpublished_business(book):
    engine, save, _, _, proof = book
    save(
        "expense",
        "unhandled",
        {
            "period": "2025-12",
            "amount_fen": 100,
            "expense_class": "administration",
            "creditor_kind": "supplier",
            "counterparty_id": "supplier",
        },
    )
    ready(engine, proof)
    with pytest.raises(KernelError) as rejected:
        Periods(engine).preview_close_range("2026-01", "2026-03", owner_confirmation=proof)
    assert rejected.value.code == "earlier_period_open"
    assert rejected.value.details["closing_period"] == "2026-01"
    assert rejected.value.details["period"] == "2025-12"


def test_range_freezes_same_business_and_reports_as_sequential_close(request, tmp_path):
    report_fixture = request.getfixturevalue("reporting_book")
    engine = report_fixture[0]
    reports = scenario(report_fixture)
    with engine.store.connection(read_only=True) as connection:
        proof = connection.execute("SELECT digest FROM evidence").fetchone()[0].hex()
    ready(engine, proof)
    periods = Periods(engine)
    for month in ("2026-01", "2026-02", "2026-03"):
        periods.inventory(
            month,
            "transactions",
            evidence=[proof],
            expected=1,
            no_business=False,
            confirmation_evidence=proof,
            request_id="busy-" + month,
        )
    open_report = reports.report(2026, 1)
    assert open_report["status"] == "ready", open_report["fact_issues"]
    path = tmp_path / "synthetic-sequential.sqlite"
    with engine.store.connection(read_only=True) as source, sqlite3.connect(path) as destination:
        source.backup(destination)
    reference = Periods(
        Engine(
            Store(path, engine.store.registry, engine.store.company_id, engine.store.database_id)
        )
    )
    for month in ("2026-01", "2026-02", "2026-03"):
        preview = reference.preview_close(month, owner_confirmation=proof)
        reference.close(
            month,
            owner_confirmation=proof,
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id="sequential-" + month,
        )
    preview = periods.preview_close_range("2026-01", "2026-03", owner_confirmation=proof)
    periods.close_range(**arguments(preview))
    for month in ("2026-01", "2026-02", "2026-03"):
        one, batch = reference.closed_report(month), periods.closed_report(month)
        assert batch["vouchers"]
        assert {key: value for key, value in one.items() if key != "previous_close_digest"} == {
            key: value
            for key, value in batch.items()
            if key not in {"previous_close_digest", "close_range"}
        }
    final = reports.preview_export(2026, 1)
    single_final = Reports(reference.engine).preview_export(2026, 1)
    assert final["statements"] == single_final["statements"] == open_report["statements"]
    assert final["report_fact_ids"] == single_final["report_fact_ids"]
    assert all(check["passed"] for check in final["checks"])
