"""Released v1 verifies nonempty private settlement proofs with its own rules."""

import json
from copy import copy

from test_settlement_period_scopes import setup

from ai_accounting.kernel import content_v1, settlement_freeze, settlement_freeze_v1
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import Registry
from ai_accounting.kernel.engine import Engine


def test_v1_rebuilds_nonempty_subject_directory_without_current_reader(tmp_path, monkeypatch):
    company = setup(tmp_path)
    company.close("2026-01")

    def future_rule(*_args, **_kwargs):
        raise AssertionError("historical settlement verification used a future rule")

    monkeypatch.setattr(settlement_freeze, "_read_root", future_rule)
    monkeypatch.setattr(settlement_freeze, "_build_prepared", future_rule)
    monkeypatch.setattr(settlement_freeze, "_authoritative_freezes", future_rule)
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
    with company.engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        root = json.loads(
            connection.execute("SELECT root_json FROM settlement_freeze_root").fetchone()[0]
        )
        assert root["format"] == "settlement-freeze/2"
        assert root["subject_directory"]
        with historical_content(1):
            assert (
                settlement_freeze_v1.require_frozen_settlement_projection(
                    historical_engine, connection
                )["periods"]
                == 1
            )
