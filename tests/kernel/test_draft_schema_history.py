"""Exact synthetic draft ancestry, without upgrading or rewriting released history."""

import sqlite3
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
