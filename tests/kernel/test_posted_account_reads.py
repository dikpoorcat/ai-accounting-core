"""Account summaries consume the frozen source, not its reverse lookup directory."""

import pytest
from test_engine import close, publish, save
from test_engine import engine as engine_fixture

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard_reads import posted_account_totals
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.types import YearMonth

engine = engine_fixture


def test_summary_reuses_authoritative_close_without_scanning_its_reverse_index(engine, monkeypatch):
    save(engine)
    publish(engine)
    close(engine)
    save(engine, "feb", amount=250, period="2026-02", request="feb")
    publish(engine, ["feb"], request="publish-feb")
    month = YearMonth("2026-02").ordinal

    def unrelated_directory(*_args, **_kwargs):
        raise AssertionError("account summary does not consume reverse close references")

    with QueryReads.snapshot(engine) as reads:
        monkeypatch.setattr(reads, "close_rows", unrelated_directory)
        assert posted_account_totals(reads.connection, month, reads=reads) == {
            "2202": -350,
            "5602": 350,
        }
        assert posted_account_totals(reads.connection, month, source="closed", reads=reads) == {
            "2202": -100,
            "5602": 100,
        }

    with engine.store.connection() as connection:
        trigger = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='immutable_close_reference_DELETE'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER immutable_close_reference_DELETE")
        connection.execute("DELETE FROM close_reference")
        connection.execute(trigger)
    with QueryReads.snapshot(engine) as reads:
        assert posted_account_totals(reads.connection, month, reads=reads) == {
            "2202": -350,
            "5602": 350,
        }
        # Consumers of the derived directory still reject its missing contents.
        with pytest.raises(KernelError, match="引用"):
            reads.close_rows(periods=[YearMonth("2026-01").ordinal])
