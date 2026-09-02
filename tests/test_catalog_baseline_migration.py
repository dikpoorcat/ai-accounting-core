from __future__ import annotations

from importlib import import_module

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory


def test_catalog_migration_tree_has_one_non_downgradable_baseline() -> None:
    scripts = ScriptDirectory.from_config(Config("catalog_alembic.ini"))
    catalog_baseline = import_module(
        "catalog_alembic.versions.0001_catalog_baseline_v2"
    )

    assert scripts.get_heads() == ["0001_catalog_baseline_v2"]
    assert [revision.revision for revision in scripts.walk_revisions()] == [
        "0001_catalog_baseline_v2"
    ]
    with pytest.raises(RuntimeError, match="CATALOG_DATABASE_HAS_NO_AUTOMATIC_DOWNGRADE"):
        catalog_baseline.downgrade()
