"""Content damage is rejected even when the original trigger SQL is restored."""

from __future__ import annotations

from typing import ClassVar

import pytest
from schema_fixture import test_bundle
from test_engine import close, evidence, publish, save
from test_engine import engine as engine  # noqa: F401

from ai_accounting.kernel.contracts import BalanceEffect, Fact, KernelError, Line, Outcome, Registry
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.integrity import (
    verify_close_integrity,
    verify_integrity,
    verify_publication,
    verify_sources,
)
from ai_accounting.kernel.projections import compare_projections, repair_projections
from ai_accounting.kernel.read_indexes import repair_read_indexes, sync_close
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.types import PositiveFen, YearMonth, canonical, digest


def damage(engine, table, sql, parameters=(), *, foreign_keys=True):
    """Deliberately bypass only the named synthetic table, then restore its DDL."""
    with engine.store.connection() as connection:
        if not foreign_keys:
            connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN IMMEDIATE")
        triggers = list(
            connection.execute(
                "SELECT name,sql FROM sqlite_schema WHERE type='trigger' AND tbl_name=?",
                (table,),
            )
        )
        for name, _ in triggers:
            connection.execute(f'DROP TRIGGER "{name}"')
        connection.execute(sql, parameters)
        for _, sql in triggers:
            connection.execute(sql)
        connection.commit()


def verify(engine, **options):
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        return verify_integrity(engine, connection, **options)


@pytest.mark.parametrize("kind", ["total", "account", "cashflow", "order", "outcome", "fact"])
def test_complete_content_detects_damage_with_original_schema(engine, kind):
    save(engine)
    publish(engine)
    assert verify(engine)["status"] == "verified"
    table, sql = {
        "total": ("voucher_version", "UPDATE voucher_version SET total=total+1"),
        "account": ("voucher_line", "UPDATE voucher_line SET account='5601' WHERE debit>0"),
        "cashflow": ("voucher_line", "UPDATE voucher_line SET cashflow='payment' WHERE debit>0"),
        "order": ("voucher_line", "UPDATE voucher_line SET line_no=line_no+10"),
        "outcome": (
            "calculation",
            "UPDATE calculation SET outcome=json_set(outcome,'$.values.amount',101)",
        ),
        "fact": ("fact_test_charge", "UPDATE fact_test_charge SET amount=101"),
    }[kind]
    damage(engine, table, sql)
    with pytest.raises(KernelError) as failure:
        verify(engine)
    assert failure.value.code == "content_integrity_failed"


def test_historical_versions_are_checked_without_recalculating_them(engine):
    save(engine)
    _, first = publish(engine)
    old_id = first["results"][0]["calculation_id"]
    save(engine, amount=150, revision=1, request="new")
    publish(engine, request="new-publish")
    assert verify(engine)["counts"]["vouchers"] == 2
    damage(
        engine,
        "voucher_line",
        "UPDATE voucher_line SET account='5601' WHERE debit>0 AND version_id IN "
        "(SELECT id FROM voucher_version WHERE calculation_id=?)",
        (old_id,),
    )
    with pytest.raises(KernelError):
        verify(engine)


def test_review_reuses_old_voucher_then_closed_correction_reverses_it(engine):
    save(engine)
    publish(engine)
    close(engine)
    save(engine, revision=1, request="review")
    publish(engine, request="publish-review")
    assert verify(engine)["counts"]["vouchers"] == 1
    save(engine, amount=150, revision=2, request="change")
    publish(engine, request="correction", correction_period="2026-02")
    assert verify(engine)["counts"]["vouchers"] == 3
    damage(
        engine,
        "voucher_line",
        "UPDATE voucher_line SET account='5601' WHERE debit>0 AND version_id IN "
        "(SELECT id FROM voucher_version WHERE reverses_id IS NOT NULL)",
    )
    with pytest.raises(KernelError) as failure:
        verify(engine)
    assert failure.value.details["reason"] == "voucher_lines_mismatch"


def test_local_verification_checks_frozen_inputs_and_unpublished_fact(engine):
    fact = save(engine)
    _, result = publish(engine)
    ident = result["results"][0]["calculation_id"]
    with engine.store.connection(read_only=True) as connection:
        assert verify_publication(engine, connection, [ident])["status"] == "verified"
        assert verify_sources(engine, connection, fact_ids=[fact["fact_id"]])["facts"] == 1
    damage(engine, "fact_test_charge", "UPDATE fact_test_charge SET amount=101")
    with engine.store.connection(read_only=True) as connection, pytest.raises(KernelError):
        verify_publication(engine, connection, [ident])


def test_no_line_result_and_reserved_voucher_are_legitimate(engine):
    save(engine)
    publish(engine)
    engine.save_fact(
        "test_charge",
        "charge",
        {"period": "2026-01", "amount": 100, "suppress_posting": True},
        evidence=(evidence(engine),),
        expected_revision=1,
        request_id="suppress",
    )
    publish(engine, request="suppress-publish")
    assert verify(engine)["status"] == "verified"
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM voucher_current").fetchone()[0] == 0
        assert not compare_projections(connection)["changed"]


def test_projection_damage_does_not_prevent_source_verification_and_repair(engine):
    save(engine)
    publish(engine)
    with engine.store.connection() as connection:
        connection.execute("UPDATE monthly_account SET debit=debit+10,credit=credit+10")
    with pytest.raises(KernelError) as failure:
        verify(engine)
    assert failure.value.details["reason"] == "projection_mismatch"
    assert verify(engine, include_projections=False)["status"] == "verified"
    with engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        assert repair_projections(connection)["changed"]
        assert not repair_projections(connection)["changed"]
        connection.commit()
    assert verify(engine)["status"] == "verified"


@pytest.mark.parametrize(
    "stage", ["after_unseal", "after_rebuild", "before_restore", "after_verify"]
)
def test_read_index_repair_restores_triggers_and_rolls_back_on_fault(engine, stage):
    save(engine)
    publish(engine)
    close(engine)
    damage(engine, "close_reference", "DELETE FROM close_reference")
    assert verify(engine, include_indexes=False)["status"] == "verified"
    with engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        before = list(connection.iterdump())

        def fail(current, _connection):
            if current == stage:
                raise RuntimeError("injected")

        with pytest.raises(RuntimeError):
            repair_read_indexes(connection, bundle=engine.store.bundle, fault=fail)
        assert list(connection.iterdump()) == before
        assert repair_read_indexes(connection, bundle=engine.store.bundle)["changed"]
        assert not repair_read_indexes(connection, bundle=engine.store.bundle)["changed"]
        connection.commit()
    assert verify(engine)["status"] == "verified"


class Opening(Fact):
    kind: ClassVar[str] = "test_opening"
    amount: PositiveFen


def opening_engine(tmp_path):
    registry = Registry()
    registry.register(
        Opening,
        lambda version, _: Outcome(
            (),
            {},
            balances=(BalanceEffect("bank", version.fact.amount, "cash"),),
            opening=True,
            opening_lines=(
                Line("1002", debit=version.fact.amount),
                Line("4001", credit=version.fact.amount),
            ),
        ),
    )
    return Engine(
        Store.create(
            tmp_path / "opening.sqlite",
            test_bundle(registry),
            "opening",
            "91310000123456789A",
            "opening-db",
        )
    )


def frozen_opening(engine, *, selected=True, amount=100):
    saved = engine.save_fact(
        "test_opening",
        "opening",
        {"period": "2026-01", "amount": 100},
        evidence=(evidence(engine),),
        expected_revision=0,
        request_id="opening",
    )
    _, result = publish(engine, ["opening"])
    cid = result["results"][0]["calculation_id"]
    period = "2026-01" if selected else "2026-02"
    manifest = {
        "period": period,
        "company_id": engine.store.company_id,
        "database_id": engine.store.database_id,
        "previous_close_digest": None,
        "vouchers": [],
        "calculations": [cid] if selected else [],
        "facts": [saved["fact_id"]] if selected else [],
        "trial_balance": [
            {"account": "1002", "debit": amount, "credit": 0},
            {"account": "4001", "debit": 0, "credit": amount},
        ],
    }
    with engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        month = YearMonth(period).ordinal
        connection.execute(
            "INSERT INTO period_close VALUES(?,?,?)", (month, canonical(manifest), digest(manifest))
        )
        sync_close(connection, month)
        connection.commit()


def test_independent_opening_and_balances_do_not_create_current_activity(tmp_path):
    engine = opening_engine(tmp_path)
    frozen_opening(engine)
    assert verify(engine)["status"] == "verified"
    with engine.store.connection(read_only=True) as connection:
        projected = compare_projections(connection)
        assert projected["expected"]["monthly_account"] == []
        assert projected["expected"]["monthly_cashflow"] == []
        assert projected["expected"]["balance"] == [("cash", "bank", 100)]
        assert len(projected["expected"]["opening_account"]) == 2


def test_explicit_opening_with_wrong_frozen_amount_is_damage_not_limited(tmp_path):
    engine = opening_engine(tmp_path)
    frozen_opening(engine, amount=101)
    with pytest.raises(KernelError) as failure:
        verify(engine)
    assert failure.value.details["reason"] == "trial_balance_source_mismatch"


def test_old_missing_opening_adoption_reports_limited_without_guessing(tmp_path):
    engine = opening_engine(tmp_path)
    frozen_opening(engine, selected=False)
    report = verify(engine)
    assert report["status"] == "limited"
    assert report["limitations"] == [
        {"code": "historical_opening_adoption_unestablished", "period": "2026-02"}
    ]


def test_missing_empty_read_is_detected_from_saved_calculation_identity(engine):
    save(engine)
    publish(engine)
    damage(engine, "dependency_scope", "DELETE FROM dependency_scope")
    with pytest.raises(KernelError) as failure:
        verify(engine)
    assert failure.value.details["reason"] == "calculation_input_digest_mismatch"


def test_raw_fact_verification_does_not_apply_current_model_validation(engine, monkeypatch):
    save(engine)
    publish(engine)

    def forbidden(*args, **kwargs):
        raise AssertionError("historical fact must not be revalidated")

    monkeypatch.setattr(
        engine.store.registry.models["test_charge"], "model_validate_json", forbidden
    )
    assert verify(engine)["status"] == "verified"


def test_orphan_directory_foreign_key_is_repairable_but_sources_remain_verified(engine):
    save(engine)
    publish(engine)
    close(engine)
    damage(
        engine,
        "close_reference",
        "UPDATE close_reference SET close_period=close_period+1",
        foreign_keys=False,
    )
    assert verify(engine, include_indexes=False)["status"] == "verified"
    with engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        assert repair_read_indexes(connection, bundle=engine.store.bundle)["changed"]
        connection.commit()
    assert verify(engine)["status"] == "verified"


@pytest.mark.parametrize("stage", ["projection_cleared", "projection_rebuilt"])
def test_projection_repair_fault_rolls_back_every_table(engine, stage):
    save(engine)
    publish(engine)
    with engine.store.connection() as connection:
        connection.execute("UPDATE monthly_account SET debit=debit+10,credit=credit+10")
        connection.execute("BEGIN IMMEDIATE")
        before = list(connection.iterdump())

        def fail(current, _connection):
            if stage == current:
                raise RuntimeError("injected")

        with pytest.raises(RuntimeError):
            repair_projections(connection, fault=fail)
        connection.rollback()
        assert list(connection.iterdump()) == before


def test_source_audit_damage_cannot_be_blessed_by_directory_repair(engine):
    save(engine)
    publish(engine)
    damage(
        engine,
        "audit",
        "UPDATE audit SET payload=json_set(payload,'$.revision',2) WHERE action='confirm_fact'",
    )
    with pytest.raises(KernelError) as failure:
        verify(engine, include_indexes=False, include_projections=False)
    assert failure.value.details["reason"] == "audit_request_result_mismatch"


def test_zero_trial_row_is_compatible_but_duplicate_account_is_not(tmp_path):
    engine = opening_engine(tmp_path)
    frozen_opening(engine)
    with engine.store.connection(read_only=True) as connection:
        row = connection.execute("SELECT * FROM period_close").fetchone()
        import json

        manifest = json.loads(row["manifest"])
    manifest["trial_balance"].append({"account": "2202", "debit": 0, "credit": 0})
    damage(
        engine,
        "period_close",
        "UPDATE period_close SET manifest=?,digest=?",
        (canonical(manifest), digest(manifest)),
    )
    assert verify(engine, include_indexes=False)["status"] == "verified"
    manifest["trial_balance"].append({"account": "2202", "debit": 0, "credit": 0})
    damage(
        engine,
        "period_close",
        "UPDATE period_close SET manifest=?,digest=?",
        (canonical(manifest), digest(manifest)),
    )
    with pytest.raises(KernelError) as failure:
        verify(engine, include_indexes=False)
    assert failure.value.details["reason"] == "duplicate_trial_account"


def test_publication_projection_delta_rejects_a_missing_write(engine, monkeypatch):
    save(engine)
    monkeypatch.setattr(engine, "_journal_projection", lambda *args: None)
    with pytest.raises(KernelError) as failure:
        publish(engine)
    assert failure.value.details["reason"] == "projection_change_mismatch"
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM calculation").fetchone()[0] == 0


def test_local_verification_does_not_read_unrelated_history(engine):
    save(engine)
    _, result = publish(engine)
    ident = result["results"][0]["calculation_id"]
    counts = []

    def measure():
        with engine.store.connection(read_only=True) as connection:
            statements = []
            connection.set_trace_callback(statements.append)
            verify_publication(engine, connection, [ident])
            return len(statements)

    counts.append(measure())
    for index in range(12):
        subject = f"unrelated-{index}"
        save(engine, subject, request=f"save-{index}", period="2026-02")
        publish(engine, [subject], request=f"publish-{index}")
    counts.append(measure())
    assert counts[0] == counts[1]


@pytest.mark.parametrize("table", ["balance", "opening_account"])
def test_independent_opening_projections_are_repaired_from_current_publication(tmp_path, table):
    engine = opening_engine(tmp_path)
    frozen_opening(engine)
    with engine.store.connection() as connection:
        connection.execute(f"DELETE FROM {table}")
    assert verify(engine, include_projections=False)["status"] == "verified"
    with pytest.raises(KernelError):
        verify(engine)
    with engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        assert repair_projections(connection)["changed"]
        connection.commit()
    assert verify(engine)["status"] == "verified"


def test_cashflow_projection_is_compared_and_restored_independently(engine):
    engine.store.registry.evaluators["test_charge"] = lambda version, _: Outcome(
        (
            Line("5602", debit=version.fact.amount, cashflow="operating"),
            Line("1002", credit=version.fact.amount),
        ),
        {},
    )
    save(engine)
    publish(engine)
    with engine.store.connection() as connection:
        connection.execute("UPDATE monthly_cashflow SET amount=amount+1")
    with pytest.raises(KernelError) as failure:
        verify(engine)
    assert failure.value.details["differences"][0]["table"] == "monthly_cashflow"
    with engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        repair_projections(connection)
        connection.commit()
    assert verify(engine)["status"] == "verified"


def test_new_close_rejects_damage_to_previous_manifest_even_when_digest_column_is_unchanged(engine):
    save(engine)
    publish(engine)
    close(engine)
    damage(
        engine,
        "period_close",
        "UPDATE period_close SET manifest=json_set(manifest,'$.owner_confirmation','damaged')",
    )
    with (
        engine.store.connection(read_only=True) as connection,
        pytest.raises(KernelError) as failure,
    ):
        verify_close_integrity(engine, connection, "2026-02")
    assert failure.value.details["reason"] == "manifest_digest_mismatch"


def test_new_close_checks_precise_previous_voucher_amount_not_only_manifest_hash(engine):
    save(engine)
    publish(engine)
    close(engine)
    damage(engine, "voucher_version", "UPDATE voucher_version SET total=total+1")
    with (
        engine.store.connection(read_only=True) as connection,
        pytest.raises(KernelError) as failure,
    ):
        verify_close_integrity(engine, connection, "2026-02")
    assert failure.value.details["reason"] == "voucher_total_mismatch"
