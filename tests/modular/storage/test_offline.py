"""Explicit-file access shares online transactions without a catalog or fake identity."""

import sqlite3
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError, ReadContext, UnitOfWork
from ai_accounting_modular.storage.public import (
    CompanySpec,
    RepositoryFactory,
    company_metadata,
    company_structure_contract,
    create_company_database,
    module_schema,
    open_database_unit,
    restore_database_copy,
    verify_database,
)


class Names:
    def __init__(self, session):
        self.session = session

    def add(self, name):
        self.session.execute(
            "INSERT INTO identity_object VALUES(?,'person',?,?,1,'{}',?,0,?)",
            (str(uuid4()), name, name, "a" * 64, str(uuid4())),
        )

    def all(self):
        return [row["name"] for row in self.session.fetchall("SELECT name FROM identity_object")]


class Documents:
    def __init__(self, session):
        self.session = session

    def publish(self, digest):
        self.session.execute(
            "INSERT INTO document_set VALUES(1,?,'standard/current.json')", (digest,)
        )

    def current(self):
        return self.session.fetchone("SELECT published_set_id FROM document_set")

    def change_source(self):
        self.session.execute(
            "INSERT INTO source(source_id,kind,sha256,statement_text) "
            "VALUES(?,'owner_statement',?,'事实')",
            (str(uuid4()), "b" * 64),
        )


FACTORIES = (
    RepositoryFactory(module_schema("identity"), Names),
    RepositoryFactory(module_schema("records"), Documents, Documents),
)


def _spec(root):
    return CompanySpec(
        str(uuid4()), str(uuid4()), "913100000000000001", "离线合成公司", "2026-01", root
    )


@pytest.fixture
def company(tmp_path):
    return create_company_database(_spec(tmp_path / "standalone"))


def _open(company, *, mode="read", **kwargs):
    return open_database_unit(
        company.database_path,
        company_root=company.company_root,
        repositories=FACTORIES,
        mode=mode,
        **kwargs,
    )


def test_creation_and_read_need_no_catalog_owner_or_registration(company, tmp_path):
    assert sorted(path.name for path in tmp_path.iterdir()) == ["standalone"]
    assert sorted(path.name for path in company.company_root.iterdir()) == ["runtime"]
    assert company.accounting_standard == "small_enterprise" and company.currency == "CNY"
    assert company.backup_directory == company.company_root / "backups"
    before = sha256(company.database_path.read_bytes()).hexdigest()
    with _open(company) as read:
        assert isinstance(read, ReadContext) and not isinstance(read, UnitOfWork)
        assert read.head == 0
        assert (read.company_id, read.database_id) == (company.company_id, company.database_id)
        assert company_metadata(read) == company
        assert read.repository("identity").all() == []
        assert not any(hasattr(read, name) for name in ("connection", "execute", "commit"))
    assert sha256(company.database_path.read_bytes()).hexdigest() == before
    assert (
        verify_database(company.database_path).structure_fingerprint
        == company_structure_contract()["fingerprint"]
    )


def test_write_and_empty_unit_use_the_same_head_and_repository_lifetime(company):
    with _open(company, mode="write") as unit:
        assert isinstance(unit, UnitOfWork) and unit.head == 0 and unit.next_head == 1
        names = unit.repository("identity")
        names.add("离线甲")
        names.add("离线乙")
    with _open(company, mode="write") as empty:
        assert empty.head == 1
    with _open(company) as read:
        assert read.head == 1
        assert set(read.repository("identity").all()) == {"离线甲", "离线乙"}
    with pytest.raises(ContractError, match="已结束"):
        names.all()
    with pytest.raises(ContractError, match="已结束"):
        company_metadata(unit)


@pytest.mark.parametrize(
    "failure,error,message",
    [
        ("body", RuntimeError, "synthetic failure"),
        ("final_check", RuntimeError, "synthetic final check"),
        ("caught_owner_violation", ContractError, "部分结果"),
    ],
)
def test_offline_failure_cannot_commit_partial_business_rows(company, failure, error, message):
    with pytest.raises(error, match=message):
        with _open(company, mode="write") as unit:
            names = unit.repository("identity")
            names.add("不能留下")
            if failure == "body":
                raise RuntimeError("synthetic failure")
            if failure == "final_check":
                unit.check_before_commit(
                    lambda: (_ for _ in ()).throw(RuntimeError("synthetic final check"))
                )
            else:
                with pytest.raises(ContractError):
                    names.session.execute("UPDATE company_profile SET legal_name='越权'")
    with _open(company) as read:
        assert read.head == 0 and read.repository("identity").all() == []
        assert company_metadata(read).legal_name == company.legal_name


def test_read_rejects_writes_and_maintenance_only_changes_derived_tables(company):
    with _open(company) as read:
        with pytest.raises(ContractError):
            read.repository("identity").add("越权")
    with _open(company, mode="maintenance") as unit:
        assert unit.next_head == unit.head == 0
        unit.repository("records").publish("c" * 64)
    with _open(company) as read:
        assert read.head == 0
        assert read.repository("records").current()[0] == "c" * 64
    for failure in ("formal_source", "formal_owner"):
        with pytest.raises(ContractError):
            with _open(company, mode="maintenance") as unit:
                if failure == "formal_source":
                    unit.repository("records").change_source()
                else:
                    unit.repository("identity")
    with _open(company) as read:
        assert read.head == 0 and read.repository("records").current()[0] == "c" * 64


@pytest.mark.parametrize("outer", ["online", "offline"])
def test_online_and_offline_share_the_real_database_write_key(storage_case, outer):
    storage, company = storage_case
    first = (
        storage.write_unit(company.company_id)
        if outer == "online"
        else _open(company, mode="write")
    )
    second = (
        _open(company, mode="maintenance")
        if outer == "online"
        else storage.preview_unit(company.company_id)
    )
    with first:
        with pytest.raises(ContractError, match="嵌套"):
            with second:
                pytest.fail("same-file nested writer must fail before SQLite busy timeout")
    with _open(company, mode="write") as unit:
        unit.repository("identity").add("锁已释放")


def test_same_database_ids_at_two_independent_paths_do_not_create_a_false_nested_lock(
    company, tmp_path
):
    path = tmp_path / "restored" / "runtime/company.sqlite3"
    restore_database_copy(company.database_path, path)
    with _open(company, mode="write") as source:
        with open_database_unit(
            path, company_root=tmp_path / "restored", repositories=FACTORIES, mode="write"
        ) as target:
            source.repository("identity").add("源")
            target.repository("identity").add("独立副本")
    with _open(company) as read:
        assert read.repository("identity").all() == ["源"]
    with open_database_unit(
        path, company_root=tmp_path / "restored", repositories=FACTORIES
    ) as read:
        assert read.repository("identity").all() == ["独立副本"]


def test_explicit_material_root_is_independent_of_selected_snapshot_location(company, tmp_path):
    snapshot = tmp_path / "captured.sqlite3"
    restore_database_copy(company.database_path, snapshot)
    materials, backups = tmp_path / "relocated-materials", tmp_path / "configured-backups"
    with open_database_unit(snapshot, company_root=materials, backup_directory=backups) as read:
        metadata = company_metadata(read)
    assert metadata.company_id == company.company_id
    assert metadata.database_path == snapshot
    assert metadata.company_root == materials and metadata.backup_directory == backups
    assert not materials.exists() and not backups.exists()


def test_offline_read_preserves_the_established_wal_snapshot_and_sees_new_commits(company):
    with _open(company) as before:
        assert before.head == 0 and before.repository("identity").all() == []
        with _open(company, mode="write") as unit:
            unit.repository("identity").add("仍在WAL中的真实提交")
        assert before.head == 0 and before.repository("identity").all() == []
        with _open(company) as after:
            assert after.head == 1
            assert after.repository("identity").all() == ["仍在WAL中的真实提交"]


def test_catalog_can_be_unavailable_while_its_selected_database_remains_usable(storage_case):
    storage, company = storage_case
    storage.catalog_path.write_bytes(b"unavailable catalog")
    with _open(company) as read:
        assert company_metadata(read).company_id == company.company_id
    with _open(company, mode="write") as unit:
        unit.repository("identity").add("不需要旧目录或老板身份")
    with _open(company) as read:
        assert read.repository("identity").all() == ["不需要旧目录或老板身份"]


@pytest.mark.parametrize(
    "sql",
    [
        "DROP INDEX identity_object_kind_sort",
        "UPDATE module_schema SET contract_digest='" + "f" * 64 + "' WHERE module_key='ledger'",
        "UPDATE system_meta SET database_id='" + "z" * 36 + "'",
        "UPDATE company_profile SET company_id='" + "z" * 36 + "'",
        "UPDATE company_profile SET currency='USD'",
    ],
)
def test_offline_open_uses_the_same_exact_structure_and_stored_identity_checks(company, sql):
    with closing(sqlite3.connect(company.database_path)) as connection:
        # Model a corrupt external file, including a value normal SQLite writes forbid.
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(sql)
        connection.commit()
    before = company.database_path.read_bytes()
    with pytest.raises(ContractError) as exc:
        with _open(company):
            pass
    assert exc.value.code == "storage_corrupt"
    assert company.database_path.read_bytes() == before


def test_metadata_reads_current_profile_fields_instead_of_the_creation_spec(company):
    with closing(sqlite3.connect(company.database_path)) as connection:
        connection.execute(
            "UPDATE company_profile SET legal_name=?,profile_seq=2 WHERE singleton=1",
            ("更正后的公司名称",),
        )
        connection.commit()
    with _open(company) as read:
        actual = company_metadata(read)
    assert actual.legal_name == "更正后的公司名称" and actual.profile_seq == 2
    assert actual.accounting_standard == "small_enterprise" and actual.currency == "CNY"


@pytest.mark.parametrize("mode", ["read", "write", "maintenance"])
def test_missing_database_is_never_implicitly_created(tmp_path, mode):
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(ContractError, match="不存在"):
        with open_database_unit(path, company_root=tmp_path, mode=mode):
            pass
    assert not path.exists()


@pytest.mark.parametrize(
    "mode,view,as_of",
    [
        ("other", "current", None),
        ("read", "other", None),
        ("read", "current", "2026-01"),
        ("read", "as_posted", None),
        ("read", "as_posted", "2026-13"),
    ],
)
def test_invalid_access_scope_is_rejected(company, mode, view, as_of):
    with pytest.raises(ContractError):
        with _open(company, mode=mode, view=view, as_of=as_of):
            pass


def test_as_posted_context_and_repository_contract_validation(company):
    with _open(company, view="as_posted", as_of="2026-02") as read:
        assert read.view == "as_posted" and read.as_of == "2026-02"
    factory = FACTORIES[0]
    for factories in (
        (factory, factory),
        (replace(factory, schema=replace(factory.schema, version=2)),),
    ):
        with pytest.raises(ContractError):
            with open_database_unit(
                company.database_path, company_root=company.company_root, repositories=factories
            ):
                pass


def test_structure_export_is_complete_and_detached_from_its_single_packaged_source(company):
    contract = company_structure_contract()
    assert set(contract) == {
        "format_family",
        "kind",
        "version",
        "fingerprint",
        "objects",
    }
    assert contract["format_family"] == "ai-accounting-modular/1"
    assert contract["kind"] == "company" and contract["version"] == 3
    assert all(
        set(item) == {"owner", "type", "name", "table", "sql"} for item in contract["objects"]
    )
    assert {item["name"] for item in contract["objects"]} >= {"source", "voucher_frozen_update"}
    original = company_structure_contract()
    contract["objects"][0]["sql"] = "foreign SQL"
    contract["objects"].clear()
    contract["fingerprint"] = "f" * 64
    assert company_structure_contract() == original
    assert verify_database(company.database_path).structure_fingerprint == original["fingerprint"]


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
def test_independent_creation_preserves_existing_files_and_sidecars(tmp_path, suffix):
    spec = _spec(tmp_path / "target")
    path = Path(spec.company_root) / ("runtime/company.sqlite3" + suffix)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"")
    with pytest.raises(ContractError) as exc:
        create_company_database(spec)
    assert exc.value.code == "target_exists" and path.read_bytes() == b""


@pytest.mark.parametrize(
    "field,value",
    [
        ("company_id", "not-a-uuid"),
        ("database_id", "not-a-uuid"),
        ("credit_code", "91310000000000000a"),
        ("start_period", "2026-13"),
    ],
)
def test_invalid_independent_identity_creates_nothing(tmp_path, field, value):
    spec = replace(_spec(tmp_path / "invalid"), **{field: value})
    with pytest.raises(ContractError):
        create_company_database(spec)
    assert not Path(spec.company_root).exists()
