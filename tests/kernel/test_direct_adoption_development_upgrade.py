"""The index-only draft step preserves both valid c9 installation ancestries."""

import shutil
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from test_development_upgrade import (
    COMPANY,
    DATABASE,
    TAXPAYER,
    create_old_company,
    create_old_root,
    old_business,
)

from ai_accounting.kernel import offline_development_upgrade as development
from ai_accounting.kernel.backup import (
    BackupError,
    _retained_history_digest,
    verify_file,
    verify_portable,
)
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.runtime import connect
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.versions import verify_schema


def table_rows(connection, contract):
    return {
        item["name"]: [tuple(row) for row in connection.execute(
            'SELECT * FROM "' + item["name"] + '" ORDER BY rowid'
        )]
        for item in contract["objects"]
        if item["type"] == "table" and item["name"] != "schema_draft_history"
    }


@pytest.mark.parametrize("prior_adjustment", [False, True])
def test_index_step_preserves_every_original_value_and_existing_history(tmp_path, prior_adjustment):
    path, source, target = create_old_company(
        tmp_path / "company.sqlite", source_fingerprint=development.INDEX_SOURCE_FINGERPRINT,
        prior_adjustment=prior_adjustment,
    )
    old_business(path, source)
    assert "managed_reserve_internal_movement" in source.registry.models
    with closing(connect(path)) as connection:
        rows = table_rows(connection, source.current())
        history = [tuple(row) for row in connection.execute("SELECT * FROM schema_draft_history")]
        retained = _retained_history_digest(connection)
        seen = []
        result = development.upgrade_company(connection, target, fault=seen.append)
        assert result["source_fingerprint"] == development.INDEX_SOURCE_FINGERPRINT
        assert result["steps"] == [{
            "sequence": len(history) + 1,
            "source_fingerprint": development.INDEX_SOURCE_FINGERPRINT,
            "target_fingerprint": development.INDEX_TARGET_FINGERPRINT,
        }]
        assert table_rows(connection, source.current()) == rows
        actual = [tuple(row) for row in connection.execute("SELECT * FROM schema_draft_history")]
        assert actual[:-1] == history
        assert _retained_history_digest(connection) == retained
        assert "after_copy" not in seen and "after_drop" not in seen
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert verify_schema(connection, bundle=target) == 0
        assert development.upgrade_company(connection, target) == {"status": "verified_skip"}
    assert verify_file(path, _bundle=target)["verification"]["status"] == "verified"


@pytest.mark.parametrize("point", ["after_index_ddl", "before_commit"])
def test_index_step_failure_rolls_back_index_and_preserves_prior_receipt(tmp_path, point):
    path, source, target = create_old_company(
        tmp_path / "company.sqlite", source_fingerprint=development.INDEX_SOURCE_FINGERPRINT,
        prior_adjustment=True,
    )
    with closing(connect(path)) as connection:
        before = development._all_existing_rows(connection, source.current())

        def interrupt(seen):
            if seen == point:
                raise RuntimeError("synthetic index interruption")

        with pytest.raises(RuntimeError, match="synthetic index interruption"):
            development.upgrade_company(connection, target, fault=interrupt)
        assert development._all_existing_rows(connection, source.current()) == before
        assert verify_schema(connection, bundle=source) == 0
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_index_step_rechecks_exact_source_after_taking_write_lock(tmp_path):
    path, source, target = create_old_company(
        tmp_path / "company.sqlite", source_fingerprint=development.INDEX_SOURCE_FINGERPRINT,
    )

    def race(point):
        if point == "before_begin":
            with closing(connect(path)) as competitor:
                competitor.execute("CREATE INDEX synthetic_race ON subject(kind)")

    with closing(connect(path)) as connection:
        with pytest.raises(KernelError) as error:
            development.upgrade_company(connection, target, fault=race)
        assert error.value.code == "schema_fingerprint_mismatch"
        assert connection.execute("SELECT count(*) FROM schema_draft_history").fetchone()[0] == 0
        assert connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE name='close_reference_direct_adoption'"
        ).fetchone() is None


def test_two_step_chain_is_explicit_and_rejects_shortcut_declarations(tmp_path):
    path, source, target = create_old_company(tmp_path / "company.sqlite")
    shortcut = replace(target, draft_transitions={"company": frozenset({
        (development.SOURCE_FINGERPRINT, development.INDEX_TARGET_FINGERPRINT),
        (development.INDEX_SOURCE_FINGERPRINT, development.INDEX_TARGET_FINGERPRINT),
    })})
    with closing(connect(path)) as connection:
        with pytest.raises(KernelError) as error:
            development.upgrade_company(connection, shortcut)
        assert error.value.code == "migration_not_declared"
        assert verify_schema(connection, bundle=source) == 0
        result = development.upgrade_company(connection, target)
        assert [(item["sequence"], item["source_fingerprint"], item["target_fingerprint"])
                for item in result["steps"]] == [
            (1, development.SOURCE_FINGERPRINT, development.INDEX_SOURCE_FINGERPRINT),
            (2, development.INDEX_SOURCE_FINGERPRINT, development.INDEX_TARGET_FINGERPRINT),
        ]


def test_new_target_installation_has_no_fabricated_adjustment_history(tmp_path):
    from draft_bundle_fixture import synthetic_draft_bundle

    target = synthetic_draft_bundle(tmp_path / "contracts", development=True)
    store = Store.create(tmp_path / "new.sqlite", target, COMPANY, TAXPAYER, DATABASE)
    with store.connection() as connection:
        assert connection.execute("SELECT count(*) FROM schema_draft_history").fetchone()[0] == 0
        assert (
            connection.execute("SELECT hex(fingerprint) FROM schema_history").fetchone()[0].lower()
            == development.INDEX_TARGET_FINGERPRINT
        )
        assert verify_schema(connection, bundle=target) == 0


@pytest.mark.parametrize("source_fingerprint", [
    development.INDEX_SOURCE_FINGERPRINT, development.SOURCE_FINGERPRINT,
])
def test_upgrade_scans_each_segment_before_and_after_without_duplicate_baseline(
    tmp_path, monkeypatch, record_property, source_fingerprint,
):
    path, source, target = create_old_company(
        tmp_path / "company.sqlite", source_fingerprint=source_fingerprint,
    )
    old_business(path, source)
    original = development._all_existing_rows
    scanned = []

    def observed(connection, contract):
        result = original(connection, contract)
        scanned.append((contract["sha256"], result))
        return result

    monkeypatch.setattr(development, "_all_existing_rows", observed)
    with closing(connect(path)) as connection:
        before = table_rows(connection, source.current())
        result = development.upgrade_company(connection, target)
        assert table_rows(connection, source.current()) == before
        assert result["original_rows_digest"] == scanned[0][1][0]
        assert verify_schema(connection, bundle=target) == 0
    expected = ([source_fingerprint, source_fingerprint] if
                source_fingerprint == development.INDEX_SOURCE_FINGERPRINT else
                [source_fingerprint, source_fingerprint,
                 development.INDEX_SOURCE_FINGERPRINT, development.INDEX_SOURCE_FINGERPRINT])
    assert [fingerprint for fingerprint, _ in scanned] == expected
    assert scanned[0][1] == scanned[1][1]
    if len(scanned) == 4:
        assert scanned[2][1] == scanned[3][1]
        # The second segment must bind the first real receipt and new tables.
        assert scanned[2][1][1]["schema_draft_history"] == 1
    record_property("upgrade_original_rows", sum(scanned[0][1][1].values()))
    record_property("upgrade_retention_rows_scanned", sum(
        sum(value[1].values()) for _, value in scanned
    ))


@pytest.mark.parametrize("source_fingerprint,point", [
    (development.INDEX_SOURCE_FINGERPRINT, "after_index_ddl"),
    (development.SOURCE_FINGERPRINT, "after_ddl"),
    (development.SOURCE_FINGERPRINT, "after_index_ddl"),
])
def test_each_segment_still_rejects_changed_original_data_and_rolls_back(
    tmp_path, source_fingerprint, point,
):
    path, source, target = create_old_company(
        tmp_path / "company.sqlite", source_fingerprint=source_fingerprint,
    )
    old_business(path, source)
    with closing(connect(path)) as connection:
        original = development._all_existing_rows(connection, source.current())

        def corrupt(boundary):
            if boundary == point:
                connection.execute("UPDATE state SET management=management+1 WHERE id=1")

        with pytest.raises(KernelError) as error:
            development.upgrade_company(connection, target, fault=corrupt)
        assert error.value.code == "migration_integrity_failed"
        assert development._all_existing_rows(connection, source.current()) == original
        assert verify_schema(connection, bundle=source) == 0
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_root_preserves_mixed_exact_sources_and_skips_current_target(tmp_path):
    fingerprints = (development.SOURCE_FINGERPRINT, development.INDEX_SOURCE_FINGERPRINT,
                    development.INDEX_TARGET_FINGERPRINT)
    root, _, target, companies = create_old_root(tmp_path, source_fingerprints=fingerprints)
    with closing(connect(root / "catalog.sqlite", read_only=True)) as connection:
        catalog = development._catalog_retained_digest(connection)
    result = development.upgrade_root(root, bundle=target)
    assert [item["status"] for item in result["companies"]] == [
        "upgraded", "upgraded", "verified_skip",
    ]
    for source_fingerprint, item in zip(fingerprints, result["companies"], strict=True):
        source = (target if source_fingerprint == development.INDEX_TARGET_FINGERPRINT
                  else development.source_bundle(target, source_fingerprint))
        assert verify_portable(item["backup"], _bundle=source)["database_format"] \
            == source.database_format()
    repeated = development.upgrade_root(root, bundle=target)
    assert [item["status"] for item in repeated["companies"]] == ["verified_skip"] * 3
    with closing(connect(root / "catalog.sqlite", read_only=True)) as connection:
        assert development._catalog_retained_digest(connection) == catalog
    for _, _, _, path in companies:
        assert verify_file(path, _bundle=target)["verification"]["status"] == "verified"


def test_catalog_backup_is_standalone_without_copy_wal(tmp_path, monkeypatch):
    import sqlite3

    root, _, target, _ = create_old_root(
        tmp_path, source_fingerprints=(development.INDEX_SOURCE_FINGERPRINT,),
    )
    original = development.copy_to_unpublished_database
    samples = []
    source_rows = []

    def observed(source, saved):
        source_rows.extend(tuple(row) for row in source.execute("SELECT * FROM company"))
        path = Path(saved.execute("PRAGMA database_list").fetchone()[2])

        def progress(*_):
            wal = Path(str(path) + "-wal")
            samples.append(wal.stat().st_size if wal.exists() else 0)

        return original(source, saved, progress=progress)

    monkeypatch.setattr(development, "copy_to_unpublished_database", observed)
    result = development.upgrade_root(root, bundle=target)
    assert samples and max(samples) == 0
    catalog = Path(result["backup_directory"]) / "catalog.sqlite"
    with closing(connect(
        catalog, read_only=True,
        validator=lambda connection: verify_schema(connection, bundle=target, kind="catalog"),
    )) as connection:
        assert [tuple(row) for row in connection.execute("SELECT * FROM company")] == source_rows
    catalog_wal = Path(str(catalog) + "-wal")
    assert not catalog_wal.exists() or catalog_wal.stat().st_size == 0
    standalone = tmp_path / "standalone-catalog.sqlite"
    standalone.write_bytes(catalog.read_bytes())
    with closing(sqlite3.connect(standalone.as_uri() + "?mode=ro", uri=True)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert [tuple(row) for row in connection.execute("SELECT * FROM company")] == source_rows
        verify_schema(connection, bundle=target, kind="catalog")


def test_package_inventory_declares_both_exact_development_sources():
    from ai_accounting.kernel.contract_files import contract_file_names
    from ai_accounting.kernel.schema_bundle import production_bundle

    bundle = production_bundle()
    assert contract_file_names(bundle) == (
        "catalog/draft.json", "company/draft.json",
        "development/company/" + development.SOURCE_FINGERPRINT + ".json",
        "development/company/" + development.INDEX_SOURCE_FINGERPRINT + ".json",
    )


@pytest.mark.parametrize("field", ["company_id", "database_id", "taxpayer_id"])
def test_same_schema_identity_race_is_rejected_inside_write_transaction(tmp_path, field):
    path, source, target = create_old_company(
        tmp_path / "company.sqlite", source_fingerprint=development.INDEX_SOURCE_FINGERPRINT,
    )

    def race(point):
        if point == "before_begin":
            with closing(connect(path)) as competitor:
                competitor.execute("BEGIN IMMEDIATE")
                trigger = competitor.execute(
                    "SELECT sql FROM sqlite_schema WHERE name='immutable_identity_UPDATE'"
                ).fetchone()[0]
                competitor.execute("DROP TRIGGER immutable_identity_UPDATE")
                competitor.execute(f"UPDATE identity SET {field}=? WHERE id=1", ("raced-identity",))
                competitor.execute(trigger)
                competitor.commit()

    with closing(connect(path)) as connection:
        with pytest.raises(BackupError) as error:
            development.upgrade_company(connection, target, fault=race)
        assert error.value.code == "backup_identity_mismatch"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert verify_schema(connection, bundle=source) == 0
        assert connection.execute("SELECT count(*) FROM schema_draft_history").fetchone()[0] == 0


@pytest.mark.parametrize("victim", [0, 1])
def test_root_rejects_same_schema_other_company_file_after_backups(
    tmp_path, monkeypatch, victim,
):
    fingerprints = (development.INDEX_SOURCE_FINGERPRINT, development.INDEX_TARGET_FINGERPRINT)
    root, _, target, companies = create_old_root(tmp_path, source_fingerprints=fingerprints)
    other = tmp_path / "other-company.sqlite"
    if victim == 1:
        Store.create(other, target, "other-company", "913100000000000099", "other-database")
    else:
        create_old_company(
            other, target, source_fingerprint=development.INDEX_SOURCE_FINGERPRINT,
            company_id="other-company", taxpayer_id="913100000000000099",
            database_id="other-database",
        )
    create_portable = development.create_portable
    backed_up = []

    def replace_after_all_backups(path, *args, **kwargs):
        result = create_portable(path, *args, **kwargs)
        backed_up.append(path)
        if len(backed_up) == len(companies):
            # Both paths are this test's newly created synthetic files. No
            # open connection remains when the company file is replaced.
            shutil.copyfile(other, companies[victim][3])
        return result

    monkeypatch.setattr(development, "create_portable", replace_after_all_backups)
    with pytest.raises(KernelError) as error:
        development.upgrade_root(root, bundle=target)
    assert error.value.code == ("development_upgrade_failed" if victim == 0
                                else "development_upgrade_partial")
    assert error.value.details["cause_code"] == "backup_identity_mismatch"
    assert error.value.details["failed_company_id"] == companies[victim][0]
    expected = (target if victim == 1 else development.source_bundle(
        target, development.INDEX_SOURCE_FINGERPRINT,
    ))
    with closing(connect(companies[victim][3], read_only=True)) as connection:
        assert verify_schema(connection, bundle=expected) == 0
        assert connection.execute("SELECT company_id FROM identity").fetchone()[0] == (
            "other-company"
        )
        assert connection.execute("SELECT count(*) FROM schema_draft_history").fetchone()[0] == 0
    for company_id, database_id, taxpayer_id, _ in companies:
        source = target if company_id == companies[1][0] else development.source_bundle(
            target, development.INDEX_SOURCE_FINGERPRINT,
        )
        archive = Path(error.value.details["backup_directory"]) / (
            taxpayer_id + ".finance-company.zip"
        )
        assert verify_portable(
            archive, _bundle=source, expected_company_id=company_id,
            expected_database_id=database_id, expected_taxpayer_id=taxpayer_id,
        )["verification"]["status"] == "verified"
