"""Minimal exact-body descriptors for synthetic physical-SQL projection tests."""

from types import SimpleNamespace

from ai_accounting.kernel.storage import Store


def scalar_store(models):
    registry = SimpleNamespace(models=models)
    registry.fact_bodies = lambda kind: ((registry.models[kind], "fact_" + kind),)
    store = SimpleNamespace(registry=registry)
    store._fact_body_groups = lambda connection, rows: Store._fact_body_groups(
        store, connection, rows
    )
    return store
