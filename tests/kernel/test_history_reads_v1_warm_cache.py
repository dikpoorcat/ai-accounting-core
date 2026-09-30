"""Released v1 selects bounded calculation IDs without scanning a warm source cache."""

from copy import copy

from test_settlement_period_scopes import setup

from ai_accounting.kernel import content_v1, settlement_projection_v1
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import Registry
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.history_reads_v1 import V1Reads


class NoKeyScanDict(dict):
    def keys(self):
        raise AssertionError("bounded historical source lookup scanned the warm cache")

    def __iter__(self):
        raise AssertionError("bounded historical source lookup iterated the warm cache")


def test_v1_calculation_hit_and_miss_do_not_scan_unrelated_warm_cache(tmp_path):
    company = setup(tmp_path)
    company.close("2026-01")
    descriptor = content_v1.registry_descriptor(company.engine.store.registry)
    historical_registry = Registry()
    historical_registry.models = {
        kind: content_v1._v1_model(kind, spec) for kind, spec in descriptor["models"].items()
    }
    historical_registry.reference_declarations = descriptor["references"]
    historical_registry.content_version = 1
    historical_store = copy(company.engine.store)
    historical_store.registry = historical_registry
    historical_engine = Engine(historical_store)

    with company.engine.store.connection(read_only=True) as connection, historical_content(1):
        connection.execute("BEGIN")
        ids = [
            row[0] for row in connection.execute("SELECT id FROM calculation ORDER BY id LIMIT 2")
        ]
        assert len(ids) == 2
        for raw in (False, True):
            reads = V1Reads(historical_engine, connection)
            fresh = V1Reads(historical_engine, connection)
            method = reads.raw_calculations if raw else reads.calculations
            expected_method = fresh.raw_calculations if raw else fresh.calculations
            assert method((ids[0],)) == expected_method((ids[0],))
            cache_name = "_raw_calculations" if raw else "_calculations"
            cache = getattr(reads, cache_name)
            setattr(
                reads,
                cache_name,
                NoKeyScanDict(cache, **{f"unrelated-{i}": None for i in range(2000)}),
            )
            assert method((ids[0], ids[1])) == expected_method((ids[0], ids[1]))
        assert settlement_projection_v1.require_settlement_projection(
            historical_engine, connection
        )["periods"] == 1
