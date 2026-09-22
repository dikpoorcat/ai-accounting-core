"""Posting segments preserve actual monthly amounts through successive corrections."""

import sqlite3
from typing import ClassVar

import pytest
from schema_fixture import test_bundle

from ai_accounting.kernel.contracts import BalanceEffect, Fact, KernelError, Line, Outcome, Registry
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.period_balances import balance_movements, balance_totals
from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods
from ai_accounting.kernel.publication import verify_publication_chain
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.types import Fen, YearMonth


class Position(Fact):
    kind: ClassVar[str] = "test_position"
    amount: Fen
    account: str = "bank-a"


def calculate(version, context):
    fact = version.fact
    lines = (
        (Line("5602", debit=fact.amount), Line("2202", credit=fact.amount)) if fact.amount else ()
    )
    return Outcome(
        lines,
        {"amount_fen": fact.amount},
        balances=(BalanceEffect(fact.account, fact.amount, "test_position"),),
    )


@pytest.fixture
def company(tmp_path):
    registry = Registry()
    registry.register(Position, calculate)
    engine = Engine(
        Store.create(tmp_path / "company.sqlite", test_bundle(registry), "company", "tax", "db")
    )
    proof = engine.register_evidence(b"synthetic", "text/plain", "proof", request_id="proof")[
        "digest"
    ]
    return engine, proof


def save(company, amount, revision=0, period="2026-01", account="bank-a", subject="position"):
    engine, proof = company
    return engine.save_fact(
        "test_position",
        subject,
        {"period": period, "amount": amount, "account": account},
        evidence=[proof],
        expected_revision=revision,
        request_id=f"save-{subject}-{revision}",
    )


def publish(company, request, posting_period=None, subject="position"):
    engine, _ = company
    preview = engine.preview([subject], posting_period=posting_period)
    result = engine.confirm(
        [subject],
        posting_period=posting_period,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id=request,
    )
    return preview, result


def close(company, month):
    engine, proof = company
    periods = Periods(engine)
    for category in MATERIAL_CATEGORIES:
        periods.inventory(
            month,
            category,
            evidence=[],
            expected=0,
            no_business=True,
            confirmation_evidence=proof,
            request_id=f"inventory-{month}-{category}",
        )
    preview = periods.preview_close(month, owner_confirmation=proof)
    periods.close(
        month,
        owner_confirmation=proof,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="close-" + month,
    )
    return periods.closed_report(month)


def amounts(company, month, *, movement=False):
    with company[0].store.connection(read_only=True) as connection:
        read = balance_movements if movement else balance_totals
        return {
            r["key"]: r["amount"]
            for r in read(connection, YearMonth(month).ordinal, "test_position")
        }


def test_fixed_baseline_repeated_correction_zero_and_key_change(company):
    save(company, 10000)
    publish(company, "initial")
    frozen = close(company, "2026-01")
    save(company, 12000, 1)
    first, _ = publish(company, "correct", "2026-03")
    assert first["results"][0]["mode"] == "closed_correction"
    save(company, 13000, 2)
    second, _ = publish(company, "replace")
    assert second["results"][0]["posting_period"] == "2026-03"
    assert amounts(company, "2026-01") == {"bank-a": 10000}
    assert amounts(company, "2026-03", movement=True) == {"bank-a": 3000}
    save(company, 0, 3)
    publish(company, "zero")
    assert amounts(company, "2026-03", movement=True) == {"bank-a": -10000}
    save(company, 14000, 4, account="bank-b")
    publish(company, "restore")
    assert amounts(company, "2026-03", movement=True) == {"bank-a": -10000, "bank-b": 14000}
    assert Periods(company[0]).closed_report("2026-01") == frozen
    close(company, "2026-03")
    save(company, 15000, 5, account="bank-b")
    publish(company, "next", "2026-05")
    assert amounts(company, "2026-05", movement=True) == {"bank-b": 1000}
    assert amounts(company, "2026-03") == {"bank-a": 0, "bank-b": 14000}
    with company[0].store.connection(read_only=True) as connection:
        verify_publication_chain(connection)


def test_closed_payroll_correction_keeps_automatic_open_dependants_in_their_months(tmp_path):
    from test_payroll import (
        actual,
        contribution_policy,
        income_tax_policy,
        opening,
        payroll,
        profile,
    )
    from test_payroll_corrections import Company

    company = Company(tmp_path / "payroll.sqlite")
    for fact, subject in (
        (profile(), "profile"),
        (contribution_policy(), "contributions"),
        (income_tax_policy(), "income-tax"),
        (opening(), "opening"),
        (payroll(), "january"),
        (payroll(period="2026-02"), "february"),
        (payroll(period="2026-03"), "march"),
    ):
        company.save(fact, subject)
    company.confirm_payroll("january", "february", "march")
    company.publish("january", "february", "march")
    frozen = company.close("2026-01")
    original_february = company.current("february")
    company.save(actual(), "actual")
    preview, _ = company.publish("actual", posting_period="2026-04")
    decisions = {item["subject_id"]: item for item in preview["results"]}
    assert decisions["january"]["posting_period"] == "2026-04"
    assert decisions["january"]["mode"] == "closed_correction"
    assert decisions["february"]["posting_period"] == "2026-02"
    assert decisions["march"]["posting_period"] == "2026-03"
    assert company.current("february").values != original_february.values
    assert not company.pending()
    assert Periods(company.engine).closed_report("2026-01") == frozen


def test_open_period_correction_and_withdrawal_leave_complete_chain(company):
    save(company, 100)
    publish(company, "initial")
    with pytest.raises(KernelError) as wrong:
        company[0].preview(["position"], posting_period="2026-02")
    assert wrong.value.code == "posting_period_conflict"
    save(company, 120, 1, period="2026-02")
    publish(company, "move")
    assert amounts(company, "2026-01") == {}
    assert amounts(company, "2026-02") == {"bank-a": 120}
    engine = company[0]
    preview = engine.preview_delete("position")
    engine.delete(
        "position", preview_digest=preview["digest"], epochs=preview["epochs"], request_id="remove"
    )
    assert amounts(company, "2026-02") == {}
    with engine.store.connection() as connection:
        assert [
            r[0]
            for r in connection.execute(
                "SELECT mode FROM calculation_publication ORDER BY sequence"
            )
        ] == ["initial", "open_replace", "withdrawn"]
        verify_publication_chain(connection)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM calculation_publication")


def test_late_initial_requires_explicit_month_and_conflicting_open_correction_fails(company):
    save(company, 100)
    publish(company, "initial")
    close(company, "2026-01")
    save(company, 20, subject="late")
    with pytest.raises(KernelError) as missing:
        company[0].preview(["late"])
    assert missing.value.code == "posting_period_required"
    preview, _ = publish(company, "late", "2026-03", subject="late")
    assert preview["results"][0]["mode"] == "initial"
    save(company, 30, 1, subject="late")
    with pytest.raises(KernelError) as conflict:
        company[0].preview(["late"], posting_period="2026-04")
    assert conflict.value.code == "posting_period_conflict"
    publish(company, "late-replace", subject="late")
    assert amounts(company, "2026-01") == {"bank-a": 100}
    assert amounts(company, "2026-03", movement=True) == {"bank-a": 30}


def test_period_projection_damage_repair_and_failure_rollback(company):
    save(company, 100)
    publish(company, "initial")
    engine = company[0]
    with engine.store.connection() as connection:
        connection.execute("UPDATE period_balance SET amount=99")
    with pytest.raises(KernelError):
        amounts(company, "2026-01")
    result = engine.rebuild_projections(request_id="repair")
    assert result["changed"] is True
    assert amounts(company, "2026-01") == {"bank-a": 100}
    assert engine.rebuild_projections(request_id="nochange")["changed"] is False
    save(company, 200, 1)
    preview = engine.preview(["position"])

    def fail(stage, connection):
        if stage == "published":
            raise RuntimeError("stop after projection")

    engine.fault = fail
    with pytest.raises(RuntimeError):
        engine.confirm(
            ["position"],
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id="fail",
        )
    assert amounts(company, "2026-01") == {"bank-a": 100}
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM calculation_publication").fetchone()[0] == 1


def test_source_month_change_moves_whole_open_correction_segment(company):
    save(company, 100)
    publish(company, "initial")
    close(company, "2026-01")
    save(company, 120, 1)
    publish(company, "correct", "2026-03")
    save(company, 130, 2, period="2026-04")
    preview, _ = publish(company, "move")
    assert preview["results"][0]["posting_period"] == "2026-04"
    assert amounts(company, "2026-03", movement=True) == {}
    assert amounts(company, "2026-04", movement=True) == {"bank-a": 30}
    assert company[0].ledger("2026-03") == []
    assert len(company[0].ledger("2026-04")) == 2
    from ai_accounting.kernel.maintenance import Maintenance

    Maintenance(company[0]).verify_integrity()


def test_closed_result_source_moved_to_open_month_restricts_target(company):
    save(company, 100)
    publish(company, "initial")
    close(company, "2026-01")
    save(company, 120, 1, period="2026-04")
    with pytest.raises(KernelError) as conflict:
        company[0].preview(["position"], posting_period="2026-05")
    assert conflict.value.code == "posting_period_conflict"
    publish(company, "correct", "2026-04")
    assert amounts(company, "2026-04", movement=True) == {"bank-a": 20}


def test_explicit_unchanged_root_does_not_block_batch_correction(company):
    save(company, 100)
    save(company, 50, subject="unchanged")
    publish(company, "initial")
    publish(company, "unchanged", subject="unchanged")
    close(company, "2026-01")
    save(company, 120, 1)
    engine = company[0]
    preview = engine.preview(["position", "unchanged"], posting_period="2026-03")
    routes = {item["subject_id"]: item["posting_period"] for item in preview["results"]}
    assert routes == {"position": "2026-03", "unchanged": "2026-01"}
    engine.confirm(
        ["position", "unchanged"],
        posting_period="2026-03",
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="batch",
    )
    with pytest.raises(KernelError) as conflict:
        engine.preview(["unchanged"], posting_period="2026-03")
    assert conflict.value.code == "posting_period_conflict"


def test_publication_does_not_silently_repair_broken_period_projection(company):
    save(company, 100)
    publish(company, "initial")
    save(company, 120, 1)
    engine = company[0]
    preview = engine.preview(["position"])
    with engine.store.connection() as connection:
        connection.execute("UPDATE period_balance SET amount=101")
    with pytest.raises(KernelError) as failure:
        engine.confirm(
            ["position"],
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id="must-fail",
        )
    assert failure.value.code == "content_integrity_failed"
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM calculation_publication").fetchone()[0] == 1
        assert connection.execute("SELECT amount FROM period_balance").fetchone()[0] == 101
    engine.rebuild_projections(request_id="explicit-repair")
    publish(company, "retry")
    assert amounts(company, "2026-01") == {"bank-a": 120}


def test_missing_projection_and_seals_cannot_hide_a_published_period(company):
    save(company, 100)
    publish(company, "initial")
    with company[0].store.connection() as connection:
        connection.execute("DELETE FROM period_balance")
        connection.execute("DELETE FROM period_balance_seal")
    with pytest.raises(KernelError) as failure:
        amounts(company, "2026-01")
    assert failure.value.code == "content_integrity_failed"


def test_ordinary_balance_read_rejects_changed_publication_content(company):
    from ai_accounting.kernel.query_reads import QueryReads

    save(company, 100)
    publish(company, "initial")
    engine = company[0]
    with engine.store.connection() as connection:
        trigger = connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
            "AND name='immutable_calculation_publication_UPDATE'"
        ).fetchone()
        connection.execute('DROP TRIGGER "' + trigger[0] + '"')
        connection.execute("UPDATE calculation_publication SET voucher_id=NULL")
        connection.execute(trigger[1])
    for shared_snapshot in (False, True):
        with pytest.raises(KernelError) as failure:
            if shared_snapshot:
                with QueryReads.snapshot(engine) as reads:
                    balance_totals(reads.connection, YearMonth("2026-01").ordinal, reads=reads)
            else:
                amounts(company, "2026-01")
        assert failure.value.code == "content_integrity_failed"
        assert failure.value.details["reason"] == "publication_digest"
