"""Read-only deployment checks, before current models touch a company database."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection

_ROOT = Path(__file__).resolve().parents[2]


class DatabaseSchemaError(ValueError):
    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        self.code = (
            "DATABASE_SCHEMA_UPGRADE_REQUIRED"
            if state["upgrade_available"]
            else "DATABASE_SCHEMA_UNSUPPORTED"
        )
        super().__init__(self.code)

    def result(self) -> dict[str, Any]:
        return {
            "status": "rejected",
            "errors": [self.code],
            "data": {
                "failure_kind": "deployment_mismatch",
                "database_schema": self.state,
                "next_action": "deploy_database_migrations"
                if self.state["upgrade_available"]
                else "inspect_database_deployment",
            },
        }


@lru_cache(maxsize=2)
def _revision_chain(catalog: bool) -> tuple[str, ...]:
    config = Config(str(_ROOT / ("catalog_alembic.ini" if catalog else "alembic.ini")))
    config.set_main_option(
        "script_location", str(_ROOT / ("catalog_alembic" if catalog else "alembic"))
    )
    scripts = ScriptDirectory.from_config(config)
    if len(scripts.get_heads()) != 1:
        raise RuntimeError("DATABASE_SCHEMA_SINGLE_HEAD_REQUIRED")
    return tuple(revision.revision for revision in scripts.walk_revisions())


def schema_state(connection: Connection, *, catalog: bool = False) -> dict[str, Any]:
    """Use Alembic's version table; never probe newly introduced ORM columns."""
    chain = _revision_chain(catalog)
    actual = tuple(sorted(MigrationContext.configure(connection).get_current_heads()))
    return {
        "database_kind": "catalog" if catalog else "business",
        "actual_revisions": list(actual),
        "required_revision": chain[0],
        "ready": actual == (chain[0],),
        # An empty, unknown or retired database is not an upgrade candidate.
        "upgrade_available": len(actual) == 1 and actual[0] in chain[1:],
    }


def require_current_schema(connection: Connection, *, catalog: bool = False) -> None:
    state = schema_state(connection, catalog=catalog)
    if not state["ready"]:
        raise DatabaseSchemaError(state)
