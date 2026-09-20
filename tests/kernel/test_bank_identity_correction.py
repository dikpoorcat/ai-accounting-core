"""Bank sources and idle-month work follow precise corrected opening identities."""

import json

from test_banking import book as book  # noqa: F401
from test_banking import reconciliation, statement
from test_identity_corrections import confirm, opening_package
from test_opening_continuation import _close_without_current_business

from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.periods import Periods


def test_frozen_bank_opening_correction_connects_reconciliation_and_next_month(book):
    engine, save, publish, proof = book
    accounts = [
        Entities(engine).register_entity(
            "fund_account", {}, account_type="bank", source="explicit bank records", request_id=name
        )["entity_id"]
        for name in ("original-bank", "correct-bank")
    ]
    data = dict(
        period="2026-01", package_id="opening", bank_account_id=accounts[0], balance_fen=100
    )
    opening_package(
        engine,
        proof,
        [
            ("opening_bank", "ledger-bank", dict(bank_account_id=accounts[0], balance_fen=100)),
            (
                "opening_equity",
                "capital",
                dict(
                    equity_kind="paid_in_capital",
                    balance_fen=100,
                    holder_or_basis_id="documented-capital",
                ),
            ),
        ],
    )
    _, statement_data = statement(save, publish, [], bank=accounts[0], month="2026-01", initial=100)
    reconciliation(save, publish, [], bank=accounts[0], month="2026-01")
    _close_without_current_business(engine, "2026-01", proof)
    with engine.store.connection(read_only=True) as connection:
        frozen = tuple(connection.execute("SELECT manifest,digest FROM period_close").fetchone())
        reconciliation_data = engine.store.current_fact(
            connection, "reconciliation"
        ).fact.model_dump(mode="json")
    confirm(
        engine,
        dict(
            changes=[
                dict(
                    subject_id="ledger-bank",
                    expected_revision=1,
                    action="reassign",
                    data=data | {"bank_account_id": accounts[1]},
                ),
                dict(
                    subject_id="statement",
                    expected_revision=1,
                    action="reassign",
                    data=statement_data | {"bank_account_id": accounts[1]},
                ),
                dict(
                    subject_id="reconciliation",
                    expected_revision=1,
                    action="reassign",
                    data=reconciliation_data | {"bank_account_id": accounts[1]},
                ),
            ],
            evidence=[proof],
            reason="same bank account was registered twice",
            posting_period="2026-02",
        ),
    )
    with engine.store.connection(read_only=True) as connection:
        assert (
            tuple(connection.execute("SELECT manifest,digest FROM period_close").fetchone())
            == frozen
        )
        readiness = Periods(engine).collect_current_readiness(connection, "2026-02")
        bank_issues = [item for item in readiness["issues"] if "bank_account_id" in item]
        assert bank_issues and {item["bank_account_id"] for item in bank_issues} == {accounts[1]}
        current = connection.execute(
            "SELECT c.outcome FROM calculation_current h JOIN calculation c "
            "ON c.id=h.calculation_id WHERE h.subject_id='reconciliation'"
        ).fetchone()
        assert json.loads(current[0])["values"]["bank_account_id"] == accounts[1]
    statement(
        save, publish, [], subject="feb-statement", bank=accounts[1], month="2026-02", initial=100
    )
    reconciliation(
        save,
        publish,
        [],
        subject="feb-reconciliation",
        statement_id="feb-statement",
        bank=accounts[1],
        month="2026-02",
    )
    with engine.store.connection(read_only=True) as connection:
        readiness = Periods(engine).collect_current_readiness(connection, "2026-02")
        assert not [item for item in readiness["issues"] if "bank_account_id" in item]
        assert (
            tuple(connection.execute("SELECT manifest,digest FROM period_close").fetchone())
            == frozen
        )
