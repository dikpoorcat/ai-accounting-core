"""The third exact draft step adds empty read storage and preserves old sources."""

from contextlib import closing
from dataclasses import replace

import pytest
from test_development_upgrade import create_old_company, old_business
from test_direct_adoption_development_upgrade import table_rows

from ai_accounting.kernel import offline_development_upgrade as development
from ai_accounting.kernel.backup import _retained_history_digest, verify_file
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard_read_model import DASHBOARD_READ_MODEL_DDL
from ai_accounting.kernel.runtime import connect
from ai_accounting.kernel.versions import contract, verify_schema


def test_read_model_step_preserves_every_business_identity_and_history_value(tmp_path):
    path, source, target = create_old_company(
        tmp_path / "company.sqlite",
        source_fingerprint=development.READ_MODEL_SOURCE_FINGERPRINT,
    )
    old_business(path, source)
    with closing(connect(path)) as connection:
        before = table_rows(connection, source.current())
        original_history = [
            tuple(row) for row in connection.execute("SELECT * FROM schema_history")
        ]
        original_draft_history = [
            tuple(row) for row in connection.execute("SELECT * FROM schema_draft_history")
        ]
        retained = _retained_history_digest(connection)
        result = development.upgrade_company(connection, target)
        assert result["steps"] == [
            {
                "sequence": len(original_draft_history) + 1,
                "source_fingerprint": development.READ_MODEL_SOURCE_FINGERPRINT,
                "target_fingerprint": development.READ_MODEL_TARGET_FINGERPRINT,
            }
        ]
        assert table_rows(connection, source.current()) == before
        assert [
            tuple(row) for row in connection.execute("SELECT * FROM schema_history")
        ] == original_history
        actual_history = [
            tuple(row) for row in connection.execute("SELECT * FROM schema_draft_history")
        ]
        assert actual_history[:-1] == original_draft_history
        assert _retained_history_digest(connection) == retained
        for table in (
            "dashboard_read_model_commit",
            "dashboard_read_model_block",
            "dashboard_read_model_current",
        ):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        assert verify_schema(connection, bundle=target) == 0
        assert development.upgrade_company(connection, target) == {"status": "verified_skip"}
    assert verify_file(path, _bundle=target)["verification"]["status"] == "verified"


_OBJECT_BOUNDARIES = [
    "after_read_model_object:" + item["type"] + ":" + item["name"]
    for item in contract(DASHBOARD_READ_MODEL_DDL)
]


@pytest.mark.parametrize("point", [*_OBJECT_BOUNDARIES, "after_read_model_ddl", "before_commit"])
def test_read_model_ddl_failure_rolls_back_every_new_object_and_retains_source(tmp_path, point):
    path, source, target = create_old_company(
        tmp_path / "company.sqlite",
        source_fingerprint=development.READ_MODEL_SOURCE_FINGERPRINT,
    )
    with closing(connect(path)) as connection:
        before = development._all_existing_rows(connection, source.current())

        def interrupt(boundary):
            if boundary == point:
                raise RuntimeError("synthetic read model DDL interruption")

        with pytest.raises(RuntimeError, match="synthetic read model DDL interruption"):
            development.upgrade_company(connection, target, fault=interrupt)
        assert development._all_existing_rows(connection, source.current()) == before
        assert verify_schema(connection, bundle=source) == 0
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        for item in contract(DASHBOARD_READ_MODEL_DDL):
            assert (
                connection.execute(
                    "SELECT 1 FROM sqlite_schema WHERE name=?", (item["name"],)
                ).fetchone()
                is None
            )


def test_read_model_step_rejects_changed_original_rows_and_rolls_back(tmp_path):
    path, source, target = create_old_company(
        tmp_path / "company.sqlite",
        source_fingerprint=development.READ_MODEL_SOURCE_FINGERPRINT,
    )
    with closing(connect(path)) as connection:
        before = development._all_existing_rows(connection, source.current())

        def corrupt(boundary):
            if boundary == "after_read_model_ddl":
                connection.execute("UPDATE state SET management=management+1 WHERE id=1")

        with pytest.raises(KernelError) as error:
            development.upgrade_company(connection, target, fault=corrupt)
        assert error.value.code == "migration_integrity_failed"
        assert development._all_existing_rows(connection, source.current()) == before
        assert verify_schema(connection, bundle=source) == 0


def test_three_declared_steps_reject_read_model_shortcuts(tmp_path):
    path, source, target = create_old_company(tmp_path / "company.sqlite")
    declared = development._declared_changes(target)
    assert [(before["sha256"], after["sha256"]) for before, after, _ in declared] == [
        (development.SOURCE_FINGERPRINT, development.INDEX_SOURCE_FINGERPRINT),
        (development.INDEX_SOURCE_FINGERPRINT, development.INDEX_TARGET_FINGERPRINT),
        (development.READ_MODEL_SOURCE_FINGERPRINT, development.READ_MODEL_TARGET_FINGERPRINT),
    ]
    third = declared[2][2]
    assert third == [
        {
            "type": item["type"],
            "name": item["name"],
            "old_sql": None,
            "new_sql": item["sql"],
        }
        for item in contract(DASHBOARD_READ_MODEL_DDL)
    ]
    shortcut = replace(
        target,
        draft_transitions={
            "company": frozenset(
                {
                    (development.SOURCE_FINGERPRINT, development.READ_MODEL_TARGET_FINGERPRINT),
                }
            )
        },
    )
    with closing(connect(path)) as connection:
        with pytest.raises(KernelError) as error:
            development.upgrade_company(connection, shortcut)
        assert error.value.code == "migration_not_declared"
        assert verify_schema(connection, bundle=source) == 0
