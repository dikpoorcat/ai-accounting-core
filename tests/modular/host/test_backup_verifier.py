"""A real database capture must pass saved-owner proofs, beyond a valid ZIP checksum."""

import hashlib
import io
import sqlite3
import stat
import zipfile
from contextlib import closing, contextmanager
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError, canonical_json, loads_json
from ai_accounting_modular.host.public import create_kernel
from ai_accounting_modular.records.public import RecordsChange, RecordsContext
from ai_accounting_modular.storage.public import (
    CompanySpec,
    OwnerProfile,
    create_catalog,
    module_schema,
    verify_database,
)


def pack(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    output.seek(0)
    return output


@pytest.fixture
def captured(tmp_path):
    catalog = tmp_path / "catalog.sqlite3"
    create_catalog(catalog, OwnerProfile(str(uuid4()), "合成负责人", "synthetic-only-hash"))
    kernel = create_kernel(catalog)
    company = kernel.storage.create_company(
        CompanySpec(
            str(uuid4()),
            str(uuid4()),
            "913100000000000001",
            "离线核验合成公司",
            "2026-01",
            tmp_path / "company",
        )
    )
    supplier, evidence, business = [str(uuid4()) for _ in range(3)]
    text = "合成资料：新设公司期初为零，一月未付维修费100元。"
    fact = {
        "business_id": business,
        "kind": "operations.expense",
        "period": "2026-01",
        "evidence_ids": [evidence],
        "data": {
            "creditor_id": supplier,
            "creditor_kind": "supplier",
            "amount_fen": "10000",
            "expense_class": "administration",
        },
    }
    outcome = kernel.application.accounting_apply(
        {
            "contract": "ai-accounting-modular/1",
            "company_id": company.company_id,
            "command_id": str(uuid4()),
            "objects": [
                {
                    "object_id": supplier,
                    "kind": "organization",
                    "name": "合成供应商",
                    "expected_seq": 0,
                    "data": {},
                }
            ],
            "sources": [
                {
                    "source_id": evidence,
                    "kind": "owner_statement",
                    "text": text,
                    "sha256": hashlib.sha256(text.encode()).hexdigest(),
                }
            ],
            "mutations": [
                {
                    "op": "create",
                    "expected_seq": 0,
                    "fact": {
                        "business_id": str(uuid4()),
                        "kind": "opening.package",
                        "period": "2026-01",
                        "evidence_ids": [evidence],
                        "data": {"basis": "newly_established", "members": []},
                    },
                },
                {"op": "create", "expected_seq": 0, "fact": fact},
            ],
        }
    )
    assert outcome["completion"] == "complete", outcome
    incoming = company.company_root / "originals/pending.txt"
    incoming.parent.mkdir(parents=True, exist_ok=True)
    incoming.write_bytes(b"an original not yet used by a business")
    with kernel.storage.read_unit(company.company_id) as read:
        source = kernel.records.prepare_source(
            {"kind": "file", "relative_path": "originals/pending.txt"}, read
        )
    with kernel.storage.write_unit(company.company_id) as unit:
        kernel.records.apply_current(
            kernel.records.plan_change(
                RecordsChange(sources=(source,)), RecordsContext(unit, str(uuid4()))
            ),
            unit,
        )
    try:
        with kernel.application.capture_backup_snapshot(
            company.company_id, company.database_id
        ) as plan:
            root = company.company_root / "runtime/tasks" / plan["capture_id"]
            entries = {
                item["archive_path"]: (
                    root if item["source_root"] == "capture" else company.company_root
                )
                .joinpath(item["relative_path"])
                .read_bytes()
                for item in plan["inputs"]
            }
            entries["manifest.json"] = canonical_json(plan["manifest"]).encode()
        yield kernel, company, entries, source, fact
    finally:
        kernel.close()


def manifest(entries):
    return loads_json(entries["manifest.json"])


def save_manifest(entries, value):
    entries["manifest.json"] = canonical_json(value).encode()


def change_database(entries, path, edit):
    path.write_bytes(entries["runtime/company.sqlite3"])
    with closing(sqlite3.connect(path)) as connection:
        edit(connection)
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    # Corruption below remains structurally valid: the owner proof must detect it.
    verify_database(path)
    raw = path.read_bytes()
    entries["runtime/company.sqlite3"] = raw
    value = manifest(entries)
    item = next(item for item in value["files"] if item["kind"] == "database")
    item.update(sha256=hashlib.sha256(raw).hexdigest(), byte_size=len(raw))
    save_manifest(entries, value)


@contextmanager
def corrupt_formal_rows(connection):
    """Model row damage while restoring the exact original structural protection."""
    connection.execute("PRAGMA foreign_keys=OFF")
    schema = module_schema("ledger")
    triggers = [item for item in schema.objects if item.type == "trigger"]
    for item in triggers:
        connection.execute('DROP TRIGGER "' + item.name + '"')
    try:
        yield schema
    finally:
        for item in triggers:
            connection.execute(item.sql)


def test_complete_backup_is_verified_without_source_catalog_or_material_directory(
    captured, tmp_path
):
    kernel, company, entries, _, _ = captured
    package = pack(entries)
    raw_digest = hashlib.sha256(package.getvalue()).hexdigest()
    # The verifier's assembly uses ports, never its original catalog or original source root.
    kernel.close()
    company.company_root.rename(tmp_path / "source-away")
    kernel.storage.catalog_path.rename(tmp_path / "catalog-away.sqlite3")
    from ai_accounting_modular.host.public import create_offline_application

    verifier = create_offline_application().backup_verifier
    with verifier.open(package) as checked:
        path = checked.company_root
        assert checked.sha256 == raw_digest
        assert checked.manifest == manifest(entries)
        detached = checked.manifest
        detached["database_head"] += 1
        assert checked.manifest["database_head"] == manifest(entries)["database_head"]
        assert checked.manifest["company_id"] == company.company_id
    assert not path.exists()


@pytest.mark.parametrize("mode", ["check", "run"])
@pytest.mark.parametrize("damage", ["version", "parent"])
def test_restore_refuses_noncurrent_contract_even_with_matching_archive_checksums(
    captured, tmp_path, mode, damage
):
    _, _, entries, _, _ = captured
    contract = loads_json(entries["contracts/company.json"])
    if damage == "version":
        contract["version"] = 2
    else:
        contract["parent_fingerprint"] = "a" * 64
    raw = canonical_json(contract).encode("utf-8")
    entries["contracts/company.json"] = raw
    value = manifest(entries)
    descriptor = next(item for item in value["files"] if item["kind"] == "contract")
    descriptor.update(sha256=hashlib.sha256(raw).hexdigest(), byte_size=len(raw))
    save_manifest(entries, value)
    source = tmp_path / "different-structure.finance-company.zip"
    source.write_bytes(pack(entries).getvalue())
    before = source.read_bytes()
    target = tmp_path / "not-created"
    from ai_accounting_modular.host.public import create_offline_application

    result = create_offline_application().accounting_isolated(
        {
            "action": "restore",
            "mode": mode,
            "source_path": str(source),
            "new_target_directory": str(target),
        }
    )
    assert result["state"] == "failed" and result["error"] == "backup_corrupt", result
    assert "完整结构合同不一致" in result["message"]
    assert source.read_bytes() == before
    assert not target.exists() and not list(tmp_path.glob(".accounting-build-*"))


@pytest.mark.parametrize("fault", ["missing", "retargeted", "extra"])
def test_saved_direct_directory_must_match_facts_before_it_is_restorable(captured, tmp_path, fault):
    kernel, _, entries, _, fact = captured

    def edit(connection):
        if fault == "missing":
            connection.execute(
                "DELETE FROM current_relation WHERE consumer_business_id=?",
                (fact["business_id"],),
            )
        elif fault == "retargeted":
            connection.execute(
                "UPDATE current_relation SET producer_id=? WHERE consumer_business_id=?",
                (str(uuid4()), fact["business_id"]),
            )
        else:
            connection.execute(
                "INSERT INTO current_relation VALUES(?,?,?,?)",
                (fact["business_id"], "business", str(uuid4()), "reference"),
            )

    change_database(entries, tmp_path / "damaged-relations.sqlite3", edit)
    with pytest.raises(ContractError, match="直接关系目录"):
        kernel.application.backup_verifier(pack(entries))


@pytest.mark.parametrize(
    "fault",
    [
        "head",
        "database_id",
        "company",
        "set",
        "contract",
        "bytes",
        "extra",
        "missing_original",
        "missing_readme",
    ],
)
def test_well_formed_archive_does_not_make_bad_content_restorable(captured, fault):
    kernel, _, entries, source, _ = captured
    value = manifest(entries)
    if fault == "head":
        value["database_head"] += 1
    elif fault == "database_id":
        value["database_id"] = str(uuid4())
    elif fault == "company":
        value["company_identity"]["legal_name"] = "另一公司"
    elif fault == "set":
        value["document_set_id"] = "0" * 64
    elif fault == "contract":
        entries["contracts/company.json"] = b"{}"
        item = next(item for item in value["files"] if item["kind"] == "contract")
        item.update(sha256=hashlib.sha256(b"{}").hexdigest(), byte_size=2)
    elif fault == "bytes":
        entries[source["relative_path"]] = b"wrong original"
    elif fault == "extra":
        path, data = "originals/unregistered.txt", b"not registered"
        entries[path] = data
        value["files"].append(
            {
                "path": path,
                "kind": "original",
                "sha256": hashlib.sha256(data).hexdigest(),
                "byte_size": len(data),
            }
        )
    else:
        absent = source["relative_path"] if fault == "missing_original" else "standard/README.md"
        del entries[absent]
        value["files"] = [item for item in value["files"] if item["path"] != absent]
    save_manifest(entries, value)
    with pytest.raises(ContractError):
        kernel.application.backup_verifier(pack(entries))


def test_valid_db_checksums_do_not_hide_stale_current_fact_body(captured, tmp_path):
    kernel, _, entries, _, fact = captured

    def edit(connection):
        changed = fact | {"data": fact["data"] | {"amount_fen": "12000"}}
        connection.execute(
            "UPDATE op_fact SET data_json=? WHERE business_id=?",
            (canonical_json(changed), fact["business_id"]),
        )

    change_database(entries, tmp_path / "changed.sqlite3", edit)
    with pytest.raises(ContractError, match="正文|索引"):
        kernel.application.backup_verifier(pack(entries))


def test_verified_database_cannot_adopt_a_future_fact_version(captured, tmp_path):
    kernel, _, entries, _, fact = captured

    def edit(connection):
        connection.execute(
            "UPDATE adopted_result SET adopted_fact_seq=adopted_fact_seq+1 WHERE business_id=?",
            (fact["business_id"],),
        )

    change_database(entries, tmp_path / "future.sqlite3", edit)
    with pytest.raises(ContractError, match="版本"):
        kernel.application.backup_verifier(pack(entries))


@pytest.mark.parametrize("loss", ["all", "expense"])
def test_valid_files_and_facts_do_not_hide_missing_promised_formal_results(
    captured, tmp_path, loss
):
    kernel, _, entries, _, fact = captured

    def edit(connection):
        with corrupt_formal_rows(connection) as schema:
            if loss == "all":
                for table in sorted(schema.tables):
                    connection.execute('DELETE FROM "' + table + '"')
            else:
                result_ids = [
                    row[0]
                    for row in connection.execute(
                        "SELECT result_id FROM adopted_result WHERE business_id=?",
                        (fact["business_id"],),
                    )
                ]
                argument = (canonical_json(result_ids),)
                connection.execute(
                    "DELETE FROM voucher_line WHERE voucher_id IN "
                    "(SELECT voucher_id FROM voucher WHERE result_id IN "
                    "(SELECT value FROM json_each(?)))",
                    argument,
                )
                for table in sorted(schema.tables):
                    columns = {
                        row[1] for row in connection.execute('PRAGMA table_info("' + table + '")')
                    }
                    if "result_id" in columns:
                        connection.execute(
                            'DELETE FROM "'
                            + table
                            + '" WHERE result_id IN (SELECT value FROM json_each(?))',
                            argument,
                        )
                connection.execute(
                    "DELETE FROM result_slot_current WHERE business_id=?", (fact["business_id"],)
                )

    change_database(entries, tmp_path / "lost-results.sqlite3", edit)
    with pytest.raises(ContractError, match="完整结果"):
        kernel.application.backup_verifier(pack(entries))


@pytest.mark.parametrize("slots", ['["main","main"]', "[1]", '["missing"]'])
def test_invalid_or_unfulfilled_saved_responsibility_is_not_restorable(captured, tmp_path, slots):
    kernel, _, entries, _, fact = captured
    change_database(
        entries,
        tmp_path / "wrong-promise.sqlite3",
        lambda connection: connection.execute(
            "UPDATE business_head SET result_slots_json=? WHERE business_id=?",
            (slots, fact["business_id"]),
        ),
    )
    with pytest.raises(ContractError, match="责任|完整结果"):
        kernel.application.backup_verifier(pack(entries))


@pytest.mark.parametrize("damage", ["journal", "obligation", "report", "balanced_amount"])
def test_retained_targets_do_not_hide_lost_or_changed_formal_outputs(captured, tmp_path, damage):
    kernel, _, entries, _, _ = captured

    def edit(connection):
        with corrupt_formal_rows(connection):
            if damage == "journal":
                connection.execute("DELETE FROM voucher_line")
                connection.execute("DELETE FROM voucher")
            elif damage == "obligation":
                connection.execute("DELETE FROM obligation")
            elif damage == "report":
                connection.execute("DELETE FROM result_report_contribution")
            else:
                connection.execute(
                    "UPDATE voucher_line SET debit_fen=debit_fen*2,credit_fen=credit_fen*2"
                )

    change_database(entries, tmp_path / "damaged-output.sqlite3", edit)
    with pytest.raises(ContractError, match="摘要|输出"):
        kernel.application.backup_verifier(pack(entries))


@pytest.mark.parametrize(
    "fault",
    [
        "parent",
        "absolute",
        "backslash",
        "duplicate",
        "case",
        "symlink",
        "directory",
        "unlisted_wal",
    ],
)
def test_archive_names_cannot_write_outside_exact_material_set(captured, fault):
    kernel, _, entries, _, _ = captured
    package = pack(entries)
    with zipfile.ZipFile(package, "a") as archive:
        if fault == "duplicate":
            with pytest.warns(UserWarning, match="Duplicate name"):
                archive.writestr("manifest.json", entries["manifest.json"])
        elif fault == "symlink":
            info = zipfile.ZipInfo("originals/link")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "../../outside")
        else:
            name = {
                "parent": "../outside",
                "absolute": "/outside",
                "backslash": "originals\\outside",
                "case": "MANIFEST.JSON",
                "directory": "standard/extra/",
                "unlisted_wal": "runtime/company.sqlite3-wal",
            }[fault]
            archive.writestr(name, b"outside")
    package.seek(0)
    with pytest.raises(ContractError):
        kernel.application.backup_verifier(package)
