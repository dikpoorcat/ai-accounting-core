"""Exact archived draft upgrades against isolated synthetic accounting files."""

from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from draft_bundle_fixture import synthetic_draft_bundle
from entity_fixture import seed_registration_entities
from monthly_close_fixture import close_months, ready
from test_dashboard_provenance import profile

from ai_accounting.kernel import offline_development_upgrade as development
from ai_accounting.kernel.backup import (
    BackupError,
    _retained_history_digest,
    verify_file,
    verify_portable,
)
from ai_accounting.kernel.catalog import Catalog
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.runtime import connect, initialize_file, private_file_lock
from ai_accounting.kernel.security.service import SecurityService
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.versions import install_metadata, verify_schema

COMPANY = "synthetic-development-company"
DATABASE = "synthetic-development-database"
TAXPAYER = "913100000000000001"


def create_old_company(
    path: Path,
    bundle=None,
    *,
    company_id=COMPANY,
    taxpayer_id=TAXPAYER,
    database_id=DATABASE,
    source_fingerprint=development.SOURCE_FINGERPRINT,
    prior_adjustment=False,
) -> tuple[Path, object, object]:
    """Install every object from the packaged 923584 contract, not new DDL."""
    target = bundle or synthetic_draft_bundle(
        path.parent / (path.stem + "-draft-contracts"), development=True
    )
    source = development.source_bundle(target, source_fingerprint)
    original = (development.source_bundle(target) if prior_adjustment else source)
    objects = original.current("company")["objects"]

    def initialize(connection):
        connection.execute("BEGIN IMMEDIATE")
        try:
            for kind in ("table", "index", "trigger"):
                for item in objects:
                    if item["type"] == kind:
                        connection.execute(item["sql"])
            connection.execute("INSERT INTO state VALUES(1,0,0,0,1,0)")
            connection.execute("INSERT INTO source_change_head VALUES(1,0)")
            connection.execute(
                "INSERT INTO identity VALUES(1,?,?,?)", (company_id, taxpayer_id, database_id)
            )
            install_metadata(connection, original, "company")
            if prior_adjustment:
                assert source_fingerprint == development.INDEX_SOURCE_FINGERPRINT
                _, step_target, differences = development._declared_changes(target)[0]
                development._apply_first_step(connection, step_target, differences)
                connection.execute(
                    "INSERT INTO schema_draft_history(sequence,source_fingerprint,"
                    "target_fingerprint,retained_history_digest) VALUES(1,?,?,?)",
                    (bytes.fromhex(development.SOURCE_FINGERPRINT),
                     bytes.fromhex(source_fingerprint),
                     bytes.fromhex(_retained_history_digest(connection))),
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    initialize_file(path, initialize, lambda connection: verify_schema(connection, bundle=source))
    return path, source, target


def old_business(path: Path, source):
    """Exercise actual old typed inputs, publications, one close, and vouchers."""
    engine = Engine(Store(path, source, COMPANY, DATABASE, taxpayer_id=TAXPAYER))
    proof = engine.register_evidence(
        b"anonymous development-upgrade source and owner confirmation",
        "text/plain",
        "synthetic-owner.txt",
        request_id="synthetic-upgrade-evidence",
    )["digest"]
    ready(engine, proof, first="2026-01", last="2026-01")
    close_months(Periods(engine), proof, first="2026-01", last="2026-01")

    def save(kind, subject, data):
        seed_registration_entities(engine, kind, data)
        return engine.save_fact(
            kind,
            subject,
            data,
            evidence=(proof,),
            expected_revision=0,
            request_id="synthetic-save-" + subject,
        )

    def publish(*subjects):
        preview = engine.preview(list(subjects))
        return engine.confirm(
            list(subjects),
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id="synthetic-publish-" + "-".join(subjects),
        )

    save(
        "bank_opening",
        "bank-opening",
        {
            "period": "2026-02",
            "bank_account_id": "bank-a",
            "opening_fen": 0,
            "basis": "new_account",
        },
    )
    publish("bank-opening")
    save(
        "funding",
        "capital",
        {
            "period": "2026-02",
            "owner_id": "owner",
            "amount_fen": 1000,
            "funding_kind": "capital",
            "actual_date": "2026-02-01",
            "bank_account_id": "bank-a",
        },
    )
    publish("capital")
    save(
        "expense",
        "supplier-cost",
        {
            "period": "2026-02",
            "counterparty_id": "supplier",
            "amount_fen": 300,
            "expense_class": "administration",
            "creditor_kind": "supplier",
        },
    )
    publish("supplier-cost")
    save(
        "payment",
        "supplier-payment",
        {
            "period": "2026-02",
            "actual_date": "2026-02-02",
            "direction": "outflow",
            "bank_account_id": "bank-a",
            "counterparty_id": "supplier",
            "amount_fen": 300,
            "allocations": [
                {
                    "source_kind": "expense",
                    "source_id": "supplier-cost",
                    "obligation": "primary",
                    "amount_fen": 300,
                }
            ],
        },
    )
    publish("supplier-payment")
    profile(engine, "counterparty", "beneficiary", display_name="真实最终收款人")
    save(
        "pass_through",
        "entrusted-funds",
        {
            "period": "2026-02",
            "payer_id": "payer",
            "beneficiary_id": "beneficiary",
            "amount_fen": 150,
            "rights_and_obligation_confirmed": True,
        },
    )
    publish("entrusted-funds")
    return engine, proof, save, publish


def snapshot_rows(connection, source):
    return development._all_existing_rows(connection, source.current("company"))


def create_old_root(tmp_path, *, source_fingerprints=None):
    root = tmp_path / "synthetic-root"
    target = synthetic_draft_bundle(tmp_path / "draft-contracts", development=True)
    catalog = Catalog(root, target)
    SecurityService(root / "catalog.sqlite").provision(
        "synthetic-owner", "Synthetic-Only-Password-123!"
    )
    companies = []
    source = development.source_bundle(target)
    source_fingerprints = source_fingerprints or (development.SOURCE_FINGERPRINT,) * 2
    for index, source_fingerprint in enumerate(source_fingerprints, 1):
        company_id = f"synthetic-company-{index}"
        database_id = f"synthetic-database-{index}"
        taxpayer_id = f"91310000000000000{index}"
        path = root / taxpayer_id / "company.sqlite"
        path.parent.mkdir()
        if source_fingerprint == target.current("company")["sha256"]:
            Store.create(path, target, company_id, taxpayer_id, database_id)
        else:
            create_old_company(
                path,
                target,
                company_id=company_id,
                taxpayer_id=taxpayer_id,
                database_id=database_id,
                source_fingerprint=source_fingerprint,
            )
        with catalog.connection() as connection:
            connection.execute(
                "INSERT INTO company(id,taxpayer_id,name,path,database_id) VALUES(?,?,?,?,?)",
                (company_id, taxpayer_id, f"Synthetic {index}", str(path), database_id),
            )
        companies.append((company_id, database_id, taxpayer_id, path))
    return root, source, target, companies


def test_packaged_old_contract_upgrades_without_rewriting_original_schema_history(tmp_path):
    path, source, target = create_old_company(tmp_path / "company.sqlite")
    engine, proof, _, _ = old_business(path, source)
    with closing(connect(path)) as connection:
        original = connection.execute(
            "SELECT version,hex(fingerprint) FROM schema_history"
        ).fetchall()
        rows = snapshot_rows(connection, source)
        history = _retained_history_digest(connection)
        frozen = connection.execute("SELECT manifest,digest FROM period_close").fetchall()
        allocations = connection.execute("SELECT * FROM fact_payment_allocations").fetchall()
        result = development.upgrade_company(connection, target)
        assert result["status"] == "upgraded"
        assert result["source_fingerprint"] == development.SOURCE_FINGERPRINT
        assert result["target_fingerprint"] == target.current("company")["sha256"]
        assert verify_schema(connection, bundle=target) == 0
        assert (
            connection.execute("SELECT version,hex(fingerprint) FROM schema_history").fetchall()
            == original
        )
        assert connection.execute("SELECT count(*) FROM schema_draft_history").fetchone()[0] == 2
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert snapshot_rows(connection, source) == rows
        assert _retained_history_digest(connection) == history
        assert connection.execute("SELECT manifest,digest FROM period_close").fetchall() == frozen
        assert (
            connection.execute("SELECT * FROM fact_payment_allocations").fetchall() == allocations
        )
        assert result["verification"]["status"] == "verified"


@pytest.mark.parametrize(
    "point", ["after_copy", "after_drop", "after_ddl", "after_index_ddl", "before_commit"],
)
def test_each_fault_rolls_back_whole_company_upgrade(tmp_path, point):
    path, source, target = create_old_company(tmp_path / "company.sqlite")
    old_business(path, source)

    def interrupt(seen):
        if seen == point:
            raise RuntimeError("synthetic interrupted upgrade")

    with closing(connect(path)) as connection:
        rows = snapshot_rows(connection, source)
        with pytest.raises(RuntimeError, match="synthetic interrupted upgrade"):
            development.upgrade_company(connection, target, fault=interrupt)
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert verify_schema(connection, bundle=source) == 0
        assert (
            connection.execute(
                "SELECT count(*) FROM sqlite_schema WHERE name='schema_draft_history'"
            ).fetchone()[0]
            == 0
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
        assert snapshot_rows(connection, source) == rows


def test_unknown_source_fingerprint_is_not_declared(tmp_path):
    path, source, target = create_old_company(tmp_path / "company.sqlite")
    with closing(connect(path)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "CREATE TABLE synthetic_undeclared_structure(id INTEGER PRIMARY KEY) STRICT"
        )
        connection.commit()
        with pytest.raises(KernelError) as error:
            development.upgrade_company(connection, target)
        assert error.value.code == "schema_fingerprint_mismatch"


def test_undeclared_target_and_corrupt_source_reject_before_any_ddl(tmp_path):
    path, source, target = create_old_company(tmp_path / "company.sqlite")
    undeclared = replace(target, draft_transitions={})
    with closing(connect(path)) as connection:
        with pytest.raises(KernelError) as error:
            development.upgrade_company(connection, undeclared)
        assert error.value.code == "migration_not_declared"
        assert verify_schema(connection, bundle=source) == 0
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO fact_evidence(fact_id,evidence_digest) VALUES(?,?)",
            ("missing-fact", b"x" * 32),
        )
        connection.commit()
        connection.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(BackupError) as error:
            development.upgrade_company(connection, target)
        assert error.value.code == "backup_content_invalid"
        assert (
            connection.execute(
                "SELECT count(*) FROM sqlite_schema WHERE name='schema_draft_history'"
            ).fetchone()[0]
            == 0
        )


def test_grouped_payment_required_beneficiary_and_internal_reserve_work_after_upgrade(tmp_path):
    from test_platform_movements import movement

    path, source, target = create_old_company(tmp_path / "company.sqlite")
    _, proof, _, _ = old_business(path, source)
    assert "managed_reserve_internal_movement" not in source.registry.models
    with closing(connect(path)) as connection:
        frozen = connection.execute("SELECT manifest,digest FROM period_close").fetchall()
        development.upgrade_company(connection, target)

    engine = Engine(Store(path, target, COMPANY, DATABASE, taxpayer_id=TAXPAYER))

    def save(kind, subject, data):
        seed_registration_entities(engine, kind, data)
        return engine.save_fact(
            kind,
            subject,
            data,
            evidence=(proof,),
            expected_revision=0,
            request_id="new-save-" + subject,
        )

    def publish(*subjects):
        preview = engine.preview(list(subjects))
        return engine.confirm(
            list(subjects),
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id="new-publish-" + "-".join(subjects),
        )

    for subject, amount in (("alice-expense", 100), ("bob-expense", 200)):
        save(
            "expense",
            subject,
            {
                "period": "2026-02",
                "counterparty_id": subject.split("-")[0],
                "amount_fen": amount,
                "expense_class": "administration",
                "creditor_kind": "supplier",
            },
        )
    publish("alice-expense", "bob-expense")
    save(
        "payment",
        "grouped-payment",
        {
            "period": "2026-02",
            "actual_date": "2026-02-03",
            "direction": "outflow",
            "bank_account_id": "bank-a",
            "counterparty_id": None,
            "payment_method": "bank_batch",
            "amount_fen": 300,
            "allocations": [
                {
                    "source_kind": "expense",
                    "source_id": f"{name}-expense",
                    "obligation": "primary",
                    "amount_fen": amount,
                    "recipient_id": name,
                }
                for name, amount in (("alice", 100), ("bob", 200))
            ],
        },
    )
    publish("grouped-payment")
    agency = {
        "period": "2026-02",
        "payer_id": "payer",
        "beneficiary_id": None,
        "amount_fen": 50,
        "rights_and_obligation_confirmed": True,
    }
    with pytest.raises(KernelError) as failure:
        save("pass_through", "required-beneficiary", agency)
    assert failure.value.code == "needs_information"
    assert failure.value.details["fact_issues"][0]["field"] == "beneficiary_id"
    save("pass_through", "required-beneficiary", dict(agency, beneficiary_id="beneficiary"))
    publish("required-beneficiary")
    save(
        "platform_movement",
        "platform-original",
        movement(
            proof,
            amount=10,
            direction="inflow",
            day="2026-02-04",
            location="synthetic.csv!1",
            reference="original-1",
        ),
    )
    publish("platform-original")
    save(
        "managed_reserve_internal_movement",
        "reserve-internal",
        {
            "period": "2026-02",
            "platform_account_id": "platform",
            "movement_ids": ["platform-original"],
            "boundary": "reserve_internal",
            "boundary_evidence_digest": proof,
            "reserve_boundary_confirmed": True,
        },
    )
    publish("reserve-internal")
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT counterparty_id FROM fact_payment WHERE revision_id="
                "(SELECT fact_id FROM fact_current WHERE subject_id='grouped-payment')"
            ).fetchone()[0]
            is None
        )
        assert (
            connection.execute(
                "SELECT beneficiary_id FROM fact_pass_through WHERE revision_id="
                "(SELECT fact_id FROM fact_current WHERE subject_id='required-beneficiary')"
            ).fetchone()[0]
            == "beneficiary"
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM fact_managed_reserve_internal_movement"
            ).fetchone()[0]
            == 1
        )
        assert connection.execute("SELECT manifest,digest FROM period_close").fetchall() == frozen


def test_root_preserves_catalog_owner_and_verifies_each_company_backup(tmp_path):
    root, source, target, companies = create_old_root(tmp_path)
    with closing(connect(root / "catalog.sqlite", read_only=True)) as connection:
        catalog_digest = development._catalog_retained_digest(connection)
    result = development.upgrade_root(root, bundle=target)
    assert result["catalog"] == "verified_unchanged"
    assert len(result["companies"]) == 2
    for item, (company_id, database_id, taxpayer_id, path) in zip(
        result["companies"], companies, strict=True
    ):
        assert item["status"] == "upgraded"
        assert Path(item["backup"]).is_file()
        verified_backup = verify_portable(item["backup"], _bundle=source)
        assert verified_backup["verification"]["status"] == "verified"
        assert verified_backup["identity"] == {
            "company_id": company_id,
            "database_id": database_id,
            "taxpayer_id": taxpayer_id,
        }
        assert verify_file(path, _bundle=target)["verification"]["status"] == "verified"
    assert (Path(result["backup_directory"]) / "catalog.sqlite").is_file()
    with closing(connect(root / "catalog.sqlite", read_only=True)) as connection:
        assert development._catalog_retained_digest(connection) == catalog_digest

    repeated = development.upgrade_root(root, bundle=target)
    assert [item["status"] for item in repeated["companies"]] == ["verified_skip", "verified_skip"]


def test_root_refuses_active_resident_lock(tmp_path):
    root, source, target, companies = create_old_root(tmp_path)
    with private_file_lock(root / ".resident.lock") as acquired:
        assert acquired
        with pytest.raises(KernelError) as error:
            development.upgrade_root(root, bundle=target)
        assert error.value.code == "service_active"
    for _, _, _, path in companies:
        with closing(connect(path, read_only=True)) as connection:
            assert verify_schema(connection, bundle=source) == 0


@pytest.mark.parametrize("fail_on_company", [1, 2])
def test_root_partial_failure_reports_exact_prefix_and_retry(tmp_path, fail_on_company):
    root, source, target, companies = create_old_root(tmp_path)
    with closing(connect(root / "catalog.sqlite", read_only=True)) as connection:
        catalog_digest = development._catalog_retained_digest(connection)
    reached = 0

    def interrupt(point):
        nonlocal reached
        if point == "after_ddl":
            reached += 1
            if reached == fail_on_company:
                raise RuntimeError("synthetic company interruption")

    with pytest.raises(KernelError) as failure:
        development.upgrade_root(root, bundle=target, fault=interrupt)
    assert failure.value.code == (
        "development_upgrade_failed" if fail_on_company == 1 else "development_upgrade_partial"
    )
    details = failure.value.details
    assert details["failed_company_id"] == companies[fail_on_company - 1][0]
    assert [item["company_id"] for item in details["completed_companies"]] == [
        company[0] for company in companies[: fail_on_company - 1]
    ]
    backup_dir = Path(details["backup_directory"])
    assert (backup_dir / "catalog.sqlite").is_file()
    for index, (company_id, _database_id, taxpayer_id, path) in enumerate(companies):
        archive = backup_dir / f"{taxpayer_id}.finance-company.zip"
        assert archive.is_file(), "all source archives precede the first company DDL"
        assert verify_portable(archive, _bundle=source)["identity"]["company_id"] == company_id
        expected = target if index < fail_on_company - 1 else source
        assert verify_file(path, _bundle=expected)["verification"]["status"] == "verified"
    with closing(connect(root / "catalog.sqlite", read_only=True)) as connection:
        assert development._catalog_retained_digest(connection) == catalog_digest

    retried = development.upgrade_root(root, bundle=target)
    assert [item["status"] for item in retried["companies"]] == (
        ["upgraded", "upgraded"] if fail_on_company == 1 else ["verified_skip", "upgraded"]
    )
    with closing(connect(root / "catalog.sqlite", read_only=True)) as connection:
        assert development._catalog_retained_digest(connection) == catalog_digest
