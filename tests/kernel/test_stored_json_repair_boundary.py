"""Repair and backup must reject ambiguous authority without rewriting it."""

import json

import pytest
import test_banking as banking

from ai_accounting.kernel import backup
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.maintenance import Maintenance

book = banking.book


def _saved_state(engine):
    tables = (
        "calculation",
        "calculation_publication",
        "balance",
        "monthly_account",
        "monthly_cashflow",
        "period_balance",
        "request",
        "audit",
    )
    with engine.store.connection(read_only=True) as connection:
        return {
            table: sorted(tuple(row) for row in connection.execute(f"SELECT * FROM {table}"))
            for table in tables
        }


@pytest.mark.parametrize("operation", ["projections", "indexes", "backup"])
def test_ambiguous_authority_cannot_be_repaired_or_published_as_backup(book, tmp_path, operation):
    engine, save, publish, _ = book
    banking.funding(save, publish)
    with engine.store.connection() as connection:
        row = connection.execute(
            "SELECT c.id,c.outcome FROM calculation_current h "
            "JOIN calculation c ON c.id=h.calculation_id WHERE h.subject_id='funding'"
        ).fetchone()
        damaged = '{"balances":[],' + row["outcome"][1:]
        # The old logical-digest check accepted this exact corruption.
        assert json.loads(damaged) == json.loads(row["outcome"])
        trigger = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name='immutable_calculation_UPDATE'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER immutable_calculation_UPDATE")
        connection.execute("UPDATE calculation SET outcome=? WHERE id=?", (damaged, row["id"]))
        connection.execute(trigger)
    before = _saved_state(engine)
    revision = Maintenance(engine)
    if operation == "backup":
        output = tmp_path / "rejected-backup"
        with pytest.raises(backup.BackupError, match="content_integrity_failed"):
            backup.create_portable(engine.store.path, output, request_id="ambiguous-backup")
        assert not list(output.glob("*.finance-company.zip"))
    else:
        method = (
            revision.rebuild_projections
            if operation == "projections"
            else revision.repair_read_indexes
        )
        with pytest.raises(KernelError) as failure:
            method(request_id="ambiguous-repair")
        assert failure.value.code == "content_integrity_failed"
    assert _saved_state(engine) == before
