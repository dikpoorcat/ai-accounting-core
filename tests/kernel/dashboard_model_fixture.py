"""Explicit production and access to synthetic asynchronous dashboard stores."""

import sqlite3
from contextlib import contextmanager

from ai_accounting.kernel.dashboard_async import (
    _anchored_model,
    _derived_store,
    _produce,
    _publication,
    _store_path,
)


def produce(engine, period):
    """Run the actual isolated producer after the fixture's source writes."""
    result = _produce(engine, period)
    assert result["status"] == "ready", result
    return result


@contextmanager
def derived_connection(engine, *, read_only=True, generation=None):
    """Use sidecar bytes for model reads and explicit synthetic corruption."""
    with _derived_store(engine, generation=generation) as (connection, _):
        if read_only:
            yield connection
            return
    # Verify identity and structure before granting a test a writable handle.
    connection = sqlite3.connect(_store_path(engine, generation=generation))
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        with connection:
            yield connection
    finally:
        connection.close()


def model_current(engine, period):
    """Return the exact authenticated published model ID and descriptor."""
    with engine.store.connection(read_only=True) as source:
        source.execute("BEGIN")
        publication = _publication(source, period)
        assert publication is not None, f"No published dashboard for {period}"
        from ai_accounting.kernel.stored_json import loads_unique

        generation = loads_unique(publication["basis"])["projection_generation"]
        with _derived_store(engine, generation=generation) as (derived, store_id):
            derived.execute("BEGIN")
            model = _anchored_model(engine, publication, derived, store_id)
            return bytes(publication["model_id"]), model.descriptor.model_dump(mode="python")
