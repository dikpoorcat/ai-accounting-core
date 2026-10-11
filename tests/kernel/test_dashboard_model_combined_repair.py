"""One maintenance transaction restores a domain read target and its UI model."""

import pytest
from test_banking import book as book  # noqa: F401
from test_dashboard_bank_source_proof import closed_banks
from test_dashboard_model_integrity import (
    MONTH,
    assert_original_records_unchanged,
    comparable,
    current_content,
    database_state,
    raw_read,
    rejected,
    retained_records,
    revision,
)
from test_integrity_content import damage, verify

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.maintenance import Maintenance
from ai_accounting.kernel.types import YearMonth


def authoritative_rows(engine):
    """Retain the stored facts, outcomes, voucher lines and frozen manifest verbatim."""
    with engine.store.connection(read_only=True) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND name GLOB 'fact_*'"
            )
        } | {
            "calculation",
            "calculation_publication",
            "dependency_fact",
            "dependency_calculation",
            "voucher",
            "voucher_version",
            "voucher_line",
            "period_close",
        }
        return {
            table: tuple(tuple(row) for row in connection.execute(
                f'SELECT * FROM "{table}" ORDER BY rowid'
            ))
            for table in sorted(tables)
        }


@pytest.mark.parametrize("target", ("projections", "read_indexes"))
def test_combined_derived_damage_preserves_sources_and_advances_one_repair_version(book, target):
    engine, _, _ = closed_banks(book)
    maintenance = Maintenance(engine)
    maintenance.rebuild_dashboard_read_model(MONTH, request_id="bootstrap-combined-model")
    dashboard = Dashboard(engine)
    expected = dashboard.brief(MONTH)
    assert expected["data"]["funds_overview"]["total_fen"] == 1500
    assert expected["data"]["financial_position"]["bank_fen"] == 1500
    assert expected["data"]["financial_position"]["capital_fen"] == 1500
    assert dashboard.funds(MONTH)["data"]["bank_statement"]["transaction_count"] == 3
    assert verify(engine)["status"] == "verified"
    authority_before = authoritative_rows(engine)
    receipts_before = retained_records(engine)
    initial_revision = revision(engine)
    with engine.store.connection(read_only=True) as connection:
        initial_epochs = engine.store.epochs(connection)
    _, _, blocks = current_content(engine)

    # Both targets really are broken before either public maintenance command
    # runs. A model-only rebuild must not certify the damaged domain target.
    if target == "projections":
        damage(engine, "monthly_account", "UPDATE monthly_account SET debit=debit+1")
        repair = maintenance.rebuild_projections
    else:
        damage(
            engine,
            "read_index_source",
            "UPDATE read_index_source SET source_digest=zeroblob(32)",
        )
        repair = maintenance.repair_read_indexes
    damage(
        engine,
        "dashboard_read_model_block",
        "UPDATE dashboard_read_model_block SET payload='{}' WHERE digest=?",
        (blocks["brief/default/overview/0"],),
    )
    rejected(lambda: dashboard.brief(MONTH))
    rejected(lambda: verify(engine))
    rejected(lambda: maintenance.rebuild_dashboard_read_model(
        MONTH, request_id="cannot-repair-model-before-domain"
    ))

    result = repair(request_id="repair-combined-" + target)
    assert result["changed"] is True
    assert result["read_repair_revision"] == initial_revision + 1
    repaired = dashboard.brief(MONTH)
    with raw_read():
        independent = dashboard.brief(MONTH)
    assert comparable(repaired) == comparable(independent)
    assert repaired["data"]["funds_overview"] == expected["data"]["funds_overview"]
    assert repaired["data"]["financial_position"] == expected["data"]["financial_position"]
    funds = dashboard.funds(MONTH)["data"]
    assert funds["total_fen"] == 1500
    assert funds["bank_statement"]["transaction_count"] == 3
    assert funds["bank_statement"]["review_state"] == "complete"
    assert funds["bank_statement"]["coverage_state"] == "complete"
    assert authoritative_rows(engine) == authority_before
    assert_original_records_unchanged(receipts_before, engine)
    with engine.store.connection(read_only=True) as connection:
        assert engine.store.epochs(connection) == initial_epochs
    assert verify(engine)["status"] == "verified"

    assert repair(request_id="repair-combined-" + target) == result
    unchanged = repair(request_id="unchanged-combined-" + target)
    assert unchanged["changed"] is False
    assert unchanged["read_repair_revision"] == initial_revision + 1
    assert revision(engine) == initial_revision + 1
    assert authoritative_rows(engine) == authority_before
    assert verify(engine)["status"] == "verified"


@pytest.mark.parametrize("first_target", ("projections", "read_indexes"))
def test_three_damaged_read_targets_allow_existing_repair_sequence(book, first_target):
    engine, _, _ = closed_banks(book)
    maintenance = Maintenance(engine)
    maintenance.rebuild_dashboard_read_model(MONTH, request_id="bootstrap-three-target-model")
    dashboard = Dashboard(engine)
    expected = dashboard.brief(MONTH)
    assert expected["data"]["funds_overview"]["total_fen"] == 1500
    assert expected["data"]["financial_position"]["capital_fen"] == 1500
    assert verify(engine)["status"] == "verified"
    authority_before = authoritative_rows(engine)
    receipts_before = retained_records(engine)
    initial_revision = revision(engine)
    month = YearMonth(MONTH).ordinal
    with engine.store.connection(read_only=True) as connection:
        initial_epochs = engine.store.epochs(connection)
        close_digest = connection.execute(
            "SELECT digest FROM period_close WHERE period=?", (month,)
        ).fetchone()[0]
        assert connection.execute(
            "SELECT debit,credit FROM monthly_account WHERE period=? AND account='1002'",
            (month,),
        ).fetchone()[:] == (1500, 0)
        assert connection.execute(
            "SELECT source_digest FROM read_index_source "
            "WHERE source_kind='close' AND source_id=?", (str(month),)
        ).fetchone()[0] == close_digest
    _, _, blocks = current_content(engine)

    # These are existing derived records with exact authoritative counterparts.
    # The close manifest/digest and the original 1500-fen vouchers stay intact.
    damage(
        engine,
        "monthly_account",
        "UPDATE monthly_account SET debit=debit+1 WHERE period=? AND account='1002'",
        (month,),
    )
    damage(
        engine,
        "read_index_source",
        "UPDATE read_index_source SET source_digest=zeroblob(32) "
        "WHERE source_kind='close' AND source_id=? AND source_digest=?",
        (str(month), close_digest),
    )
    damage(
        engine,
        "dashboard_read_model_block",
        "UPDATE dashboard_read_model_block SET payload='{}' WHERE digest=?",
        (blocks["brief/default/overview/0"],),
    )
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute(
            "SELECT debit,credit FROM monthly_account WHERE period=? AND account='1002'",
            (month,),
        ).fetchone()[:] == (1501, 0)
        assert connection.execute(
            "SELECT source_digest FROM read_index_source "
            "WHERE source_kind='close' AND source_id=?", (str(month),)
        ).fetchone()[0] == bytes(32)
    assert authoritative_rows(engine) == authority_before

    repairs = {
        "projections": maintenance.rebuild_projections,
        "read_indexes": maintenance.repair_read_indexes,
    }
    second_target = "read_indexes" if first_target == "projections" else "projections"
    expected_revision = initial_revision
    for index, target in enumerate((first_target, second_target)):
        before_operation = database_state(engine)
        request_id = "repair-three-targets-" + target
        try:
            result = repairs[target](request_id=request_id)
        except KernelError:
            # A refusal is still a regression failure, after checking that it
            # did not commit a partial repair, receipt, DDL or version advance.
            assert database_state(engine) == before_operation
            assert revision(engine) == expected_revision
            assert authoritative_rows(engine) == authority_before
            raise
        if index == 0:
            assert result["changed"] is True
        expected_revision += int(result["changed"])
        assert result["read_repair_revision"] == expected_revision
        assert revision(engine) == expected_revision
        assert repairs[target](request_id=request_id) == result
        assert revision(engine) == expected_revision
        assert authoritative_rows(engine) == authority_before
        assert_original_records_unchanged(receipts_before, engine)
        with engine.store.connection(read_only=True) as connection:
            assert engine.store.epochs(connection) == initial_epochs
            if target == "projections":
                assert connection.execute(
                    "SELECT debit,credit FROM monthly_account "
                    "WHERE period=? AND account='1002'", (month,)
                ).fetchone()[:] == (1500, 0)
            else:
                assert connection.execute(
                    "SELECT source_digest FROM read_index_source "
                    "WHERE source_kind='close' AND source_id=?", (str(month),)
                ).fetchone()[0] == close_digest

    repaired = dashboard.brief(MONTH)
    with raw_read():
        independent = dashboard.brief(MONTH)
    assert comparable(repaired) == comparable(independent)
    assert repaired["data"]["funds_overview"] == expected["data"]["funds_overview"]
    assert repaired["data"]["financial_position"] == expected["data"]["financial_position"]
    funds = dashboard.funds(MONTH)["data"]
    assert funds["total_fen"] == 1500
    assert funds["bank_statement"]["transaction_count"] == 3
    assert funds["bank_statement"]["review_state"] == "complete"
    assert funds["bank_statement"]["coverage_state"] == "complete"
    assert authoritative_rows(engine) == authority_before
    assert_original_records_unchanged(receipts_before, engine)
    assert verify(engine)["status"] == "verified"
    for target, repair in repairs.items():
        unchanged = repair(request_id="unchanged-three-targets-" + target)
        assert unchanged["changed"] is False
        assert unchanged["read_repair_revision"] == expected_revision
    model = maintenance.rebuild_dashboard_read_model(
        MONTH, request_id="model-after-domain-repairs"
    )
    assert model["changed"] is False
    assert model["read_repair_revision"] == expected_revision
    assert revision(engine) == expected_revision
    assert verify(engine)["status"] == "verified"


@pytest.mark.parametrize("target", ("projections", "read_indexes"))
def test_combined_repair_model_publication_failure_rolls_back_both_domain_targets(book, target):
    engine, _, _ = closed_banks(book)
    Maintenance(engine).rebuild_dashboard_read_model(MONTH, request_id="bootstrap-rollback-model")
    authority_before = authoritative_rows(engine)
    initial_revision = revision(engine)
    month = YearMonth(MONTH).ordinal
    with engine.store.connection(read_only=True) as connection:
        close_digest = connection.execute(
            "SELECT digest FROM period_close WHERE period=?", (month,)
        ).fetchone()[0]
    _, _, blocks = current_content(engine)
    damage(
        engine,
        "monthly_account",
        "UPDATE monthly_account SET debit=debit+1 WHERE period=? AND account='1002'",
        (month,),
    )
    damage(
        engine,
        "read_index_source",
        "UPDATE read_index_source SET source_digest=zeroblob(32) "
        "WHERE source_kind='close' AND source_id=? AND source_digest=?",
        (str(month), close_digest),
    )
    damage(
        engine,
        "dashboard_read_model_block",
        "UPDATE dashboard_read_model_block SET payload='{}' WHERE digest=?",
        (blocks["brief/default/overview/0"],),
    )
    damaged_state = database_state(engine)

    def interrupt(stage, _connection):
        if stage == "dashboard_persisted":
            raise RuntimeError("synthetic combined repair publication interruption")

    failed = Maintenance(Engine(engine.store, fault=interrupt))
    repair = failed.rebuild_projections if target == "projections" else failed.repair_read_indexes
    with pytest.raises(RuntimeError, match="synthetic combined repair publication interruption"):
        repair(request_id="retry-combined-publication")
    assert database_state(engine) == damaged_state
    assert revision(engine) == initial_revision
    assert authoritative_rows(engine) == authority_before

    maintenance = Maintenance(engine)
    retry = (
        maintenance.rebuild_projections
        if target == "projections"
        else maintenance.repair_read_indexes
    )
    result = retry(request_id="retry-combined-publication")
    assert result["changed"] is True
    assert result["read_repair_revision"] == initial_revision + 1
    assert authoritative_rows(engine) == authority_before
    assert Dashboard(engine).funds(MONTH)["data"]["total_fen"] == 1500
    assert verify(engine)["status"] == "verified"
