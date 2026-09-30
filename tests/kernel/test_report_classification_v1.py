"""Historical open report proof does not borrow future classification selectors."""

from copy import copy

from test_reports import book as _book
from test_reports import scenario

from ai_accounting.kernel import content_v1, report_projection_v1, reports
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import Registry
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.history_reads_v1 import V1Reads
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.report_projection import require_report_projection

book = _book


def test_nonempty_open_report_uses_v1_reference_selection(book, monkeypatch):
    engine = book[0]
    scenario(book)
    descriptor = content_v1.registry_descriptor(engine.store.registry)
    historical_registry = Registry()
    historical_registry.models = {
        kind: content_v1._v1_model(kind, spec) for kind, spec in descriptor["models"].items()
    }
    historical_registry.reference_declarations = descriptor["references"]
    historical_registry.content_version = 1
    store = copy(engine.store)
    store.registry = historical_registry
    historical_engine = Engine(store)
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        expected = require_report_projection(engine, connection)
        calculation_ids = [
            row[0]
            for row in connection.execute("SELECT calculation_id FROM calculation_publication")
        ]
        current_sources = QueryReads(engine, connection)
        historical_sources = V1Reads(historical_engine, connection)
        assert historical_sources.calculations(calculation_ids) == current_sources.calculations(
            calculation_ids
        )
        assert historical_sources.raw_calculations(
            calculation_ids
        ) == current_sources.raw_calculations(calculation_ids)

        def future_rule(*_args, **_kwargs):
            raise AssertionError("v1 source verification used future report rules")

        monkeypatch.setattr(reports, "_report_references", future_rule)
        monkeypatch.setattr(reports, "_report_classifications", future_rule)
        with historical_content(1):
            assert (
                report_projection_v1.require_report_projection(historical_engine, connection)
                == expected
            )
