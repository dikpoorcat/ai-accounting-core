"""Exact synthetic draft ancestry, without upgrading or rewriting released history."""

import json
import sqlite3
from copy import deepcopy
from dataclasses import replace

import pytest
from schema_fixture import TEST_FAMILY, full_contract, write_contract

from ai_accounting.kernel.contracts import KernelError, Registry
from ai_accounting.kernel.development_contracts import get_contract_sha, load_development_contracts
from ai_accounting.kernel.schema_bundle import APPLICATION_ID, load_bundle
from ai_accounting.kernel.versions import (
    DRAFT_HISTORY_DDL,
    HISTORY_DDL,
    META_DDL,
    execute_statements,
    install_metadata,
    verify_draft_history,
    verify_schema,
)

IDENTITY = """CREATE TABLE identity(id INTEGER PRIMARY KEY CHECK(id=1),
 company_id TEXT NOT NULL,taxpayer_id TEXT NOT NULL,database_id TEXT NOT NULL) STRICT;"""
SOURCE = META_DDL + HISTORY_DDL + IDENTITY
TARGET = SOURCE + DRAFT_HISTORY_DDL


@pytest.fixture
def incremental_drafts(tmp_path):
    old = full_contract(
        "CREATE TABLE a(id INTEGER); CREATE INDEX obsolete ON a(id);", kind="company"
    )
    new = full_contract("CREATE TABLE a(id TEXT); CREATE TABLE b(id INTEGER);", kind="company")
    last = full_contract(
        "CREATE TABLE a(id TEXT); CREATE TABLE b(id INTEGER); CREATE TABLE c(id TEXT);",
        kind="company",
    )
    delta = {key: value for key, value in new.items() if key != "objects"}
    delta.update(
        base_sha256=old["sha256"],
        add=[row for row in new["objects"] if row["name"] == "b"],
        remove=[{"type": "index", "name": "obsolete"}],
        replace=[row for row in new["objects"] if row["name"] == "a"],
    )
    tail = {key: value for key, value in last.items() if key != "objects"}
    tail.update(
        base_sha256=new["sha256"],
        add=[row for row in last["objects"] if row["name"] == "c"],
        remove=[],
        replace=[],
    )
    return tmp_path, old, new, last, delta, tail


def load_incremental_drafts(directory, values):
    for fingerprint, value in values.items():
        (directory / (fingerprint + ".json")).write_text(
            json.dumps(value, ensure_ascii=False),
            encoding="utf-8",
        )
    return load_development_contracts(
        directory, tuple(values), family=TEST_FAMILY, application_id=APPLICATION_ID
    )["company"]


def test_development_parent_chain_expands_to_independent_sql_contracts(incremental_drafts):
    directory, old, new, last, delta, tail = incremental_drafts
    values = {last["sha256"]: tail, new["sha256"]: delta, old["sha256"]: old}
    expanded = load_incremental_drafts(directory, values)
    assert expanded == {item["sha256"]: item for item in (old, new, last)}
    assert json.loads((directory / (new["sha256"] + ".json")).read_text()) == delta


@pytest.mark.parametrize(
    "damage",
    [
        "missing_parent",
        "undeclared_parent",
        "self_parent",
        "cycle",
        "parent_kind",
        "parent_family",
        "parent_application",
        "parent_version",
        "unknown_field",
        "duplicate_add",
        "duplicate_remove",
        "duplicate_replace",
        "overlap",
        "add_existing",
        "remove_missing",
        "replace_missing",
        "changed_sql",
        "identity",
        "invalid_parent_type",
        "invalid_object",
    ],
)
def test_development_increment_rejects_forged_or_ambiguous_contracts(incremental_drafts, damage):
    directory, old, new, _, delta, _ = deepcopy(incremental_drafts)
    values = {old["sha256"]: old, new["sha256"]: delta}
    if damage in {"missing_parent", "undeclared_parent"}:
        values.pop(old["sha256"])
        if damage == "undeclared_parent":
            (directory / (old["sha256"] + ".json")).write_text(json.dumps(old), encoding="utf-8")
    elif damage == "self_parent":
        delta["base_sha256"] = new["sha256"]
    elif damage == "cycle":
        values[old["sha256"]] = {key: value for key, value in old.items() if key != "objects"}
        values[old["sha256"]].update(base_sha256=new["sha256"], add=[], remove=[], replace=[])
    elif damage.startswith("parent_"):
        field, value = {
            "parent_kind": ("kind", "catalog"),
            "parent_family": ("family", "other"),
            "parent_application": ("application_id", False),
            "parent_version": ("version", 1),
        }[damage]
        old[field] = value
    elif damage == "unknown_field":
        delta["base_version"] = 0
    elif damage.startswith("duplicate_"):
        operation = damage.removeprefix("duplicate_")
        delta[operation].append(deepcopy(delta[operation][0]))
    elif damage == "overlap":
        delta["replace"].append(deepcopy(delta["add"][0]))
    elif damage == "add_existing":
        delta["add"].append(deepcopy(delta["replace"][0]))
    elif damage == "remove_missing":
        delta["remove"][0]["name"] = "missing"
    elif damage == "replace_missing":
        delta["replace"][0]["name"] = "missing"
    elif damage == "changed_sql":
        delta["replace"][0]["sql"] += " "
    elif damage == "identity":
        delta["sha256"] = "0" * 64
    elif damage == "invalid_parent_type":
        delta["base_sha256"] = []
    elif damage == "invalid_object":
        delta["add"][0]["type"] = "unknown"
    with pytest.raises((ValueError, RuntimeError)):
        load_incremental_drafts(directory, values)


@pytest.mark.parametrize("duplicate", ["header", "nested"])
def test_development_decoder_rejects_duplicate_json_members(incremental_drafts, duplicate):
    directory, old, _, _, _, _ = incremental_drafts
    raw = json.dumps(old)
    if duplicate == "header":
        raw = '{"sh\\u0061256": "forged",' + raw[1:]
    else:
        raw = raw.replace('"sql":', '"sql": "forged", "sql":', 1)
    (directory / (old["sha256"] + ".json")).write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError, match="damaged"):
        load_development_contracts(
            directory, (old["sha256"],), family=TEST_FAMILY, application_id=APPLICATION_ID
        )


@pytest.fixture
def drafts(tmp_path):
    old = full_contract(SOURCE, kind="company")
    new = full_contract(TARGET, kind="company")
    write_contract(tmp_path, new)
    write_contract(tmp_path, full_contract(META_DDL + HISTORY_DDL, kind="catalog"))
    archive = tmp_path / "development" / "company"
    archive.mkdir(parents=True)
    import json

    (archive / (old["sha256"] + ".json")).write_text(json.dumps(old), "utf-8")
    packaged = load_development_contracts(
        archive,
        (old["sha256"],),
        family=TEST_FAMILY,
        application_id=APPLICATION_ID,
    )
    bundle = load_bundle(
        Registry(),
        tmp_path,
        family=TEST_FAMILY,
        application_id=APPLICATION_ID,
        status="draft",
        current_versions={"company": 0, "catalog": 0},
        development_contracts=packaged,
        draft_transitions={"company": ((old["sha256"], new["sha256"]),)},
    )
    source_bundle = replace(bundle, contracts={**bundle.contracts, "company": {0: old}})
    return bundle, source_bundle, old, new


def create(script, bundle):
    connection = sqlite3.connect(":memory:")
    execute_statements(connection, script)
    connection.execute("BEGIN")
    connection.execute(
        "INSERT INTO identity VALUES(1,'synthetic-company','synthetic-tax','synthetic-db')"
    )
    install_metadata(connection, bundle, "company")
    connection.commit()
    return connection


def adjusted(drafts):
    bundle, source, old, new = drafts
    connection = create(SOURCE, source)
    execute_statements(connection, DRAFT_HISTORY_DDL)
    connection.execute(
        "INSERT INTO schema_draft_history(sequence,source_fingerprint,target_fingerprint,"
        "retained_history_digest) "
        "VALUES(1,?,?,?)",
        (bytes.fromhex(old["sha256"]), bytes.fromhex(new["sha256"]), b"r" * 32),
    )
    connection.commit()
    return connection


def test_new_draft_has_current_original_history_and_empty_adjustments(drafts):
    bundle, _, _, new = drafts
    with create(TARGET, bundle) as connection:
        assert verify_schema(connection, bundle=bundle) == 0
        assert connection.execute("SELECT count(*) FROM schema_draft_history").fetchone()[0] == 0
        assert (
            connection.execute("SELECT hex(fingerprint) FROM schema_history").fetchone()[0].lower()
            == new["sha256"]
        )


def test_exact_old_source_is_decodable_only_with_explicit_source_bundle(drafts):
    bundle, source, old, _ = drafts
    with create(SOURCE, source) as connection:
        assert verify_schema(connection, bundle=source) == 0
        assert get_contract_sha(bundle, "company", old["sha256"]) == old
        with pytest.raises(KernelError) as error:
            verify_schema(connection, bundle=bundle, allow_previous=True)
        assert error.value.code == "schema_fingerprint_mismatch"


def test_adjustment_preserves_original_history_and_only_checks_small_chain(drafts):
    bundle, _, old, _ = drafts
    with adjusted(drafts) as connection:
        statements = []
        connection.set_trace_callback(statements.append)
        assert verify_schema(connection, bundle=bundle) == 0
        verify_draft_history(connection, bundle, "company")
        assert (
            connection.execute("SELECT hex(fingerprint) FROM schema_history").fetchone()[0].lower()
            == old["sha256"]
        )
        assert not any("fact_revision" in sql or "voucher" in sql for sql in statements)
        for sql in (
            "UPDATE schema_history SET fingerprint=zeroblob(32)",
            "UPDATE schema_draft_history SET retained_history_digest=zeroblob(32)",
            "DELETE FROM schema_draft_history",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(sql)


@pytest.mark.parametrize(
    "problem", ["missing", "sequence", "unknown", "wrong_parent", "undeclared"]
)
def test_draft_ancestry_rejects_incomplete_or_forged_chain(drafts, problem):
    bundle, source, old, new = drafts
    connection = create(SOURCE, source)
    execute_statements(connection, DRAFT_HISTORY_DDL)
    if problem != "missing":
        first = bytes.fromhex(old["sha256"])
        last = bytes.fromhex(new["sha256"])
        if problem == "unknown":
            last = b"x" * 32
        if problem == "wrong_parent":
            first = last
        connection.execute(
            "INSERT INTO schema_draft_history(sequence,source_fingerprint,target_fingerprint,"
            "retained_history_digest) "
            "VALUES(?,?,?,?)",
            (2 if problem == "sequence" else 1, first, last, b"r" * 32),
        )
    if problem == "undeclared":
        bundle = replace(bundle, draft_transitions={})
    connection.commit()
    with connection, pytest.raises(KernelError) as error:
        verify_schema(connection, bundle=bundle)
    assert error.value.code == "schema_history_mismatch"


def test_archive_loader_rejects_wrong_expected_identity_and_missing_source(drafts, tmp_path):
    with pytest.raises(ValueError):
        load_development_contracts(
            tmp_path / "development" / "company",
            ("a" * 64,),
            family=TEST_FAMILY,
            application_id=APPLICATION_ID,
        )
    bundle, _, old, _ = drafts
    assert get_contract_sha(bundle, "catalog", old["sha256"]) is None


def test_released_contract_cannot_use_nonempty_draft_history(tmp_path):
    write_contract(tmp_path, full_contract(TARGET, kind="company", version=1, status="released"))
    write_contract(
        tmp_path,
        full_contract(
            META_DDL + HISTORY_DDL,
            kind="catalog",
            version=1,
            status="released",
        ),
    )
    released = load_bundle(
        Registry(),
        tmp_path,
        family=TEST_FAMILY,
        application_id=APPLICATION_ID,
        status="released",
        current_versions={"company": 1, "catalog": 1},
    )
    with create(TARGET, released) as connection:
        assert verify_schema(connection, bundle=released) == 1
        connection.execute(
            "INSERT INTO schema_draft_history(sequence,source_fingerprint,target_fingerprint,"
            "retained_history_digest) "
            "VALUES(1,?,?,?)",
            (b"s" * 32, b"t" * 32, b"r" * 32),
        )
        connection.commit()
        with pytest.raises(KernelError) as error:
            verify_schema(connection, bundle=released)
        assert error.value.code == "schema_history_mismatch"


def test_current_draft_needs_no_archived_source_declaration(drafts):
    bundle, _, _, _ = drafts
    current_only = replace(bundle, development_contracts={}, draft_transitions={})
    with create(TARGET, current_only) as connection:
        assert verify_schema(connection, bundle=current_only) == 0
