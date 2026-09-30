"""Private duplicate candidate directory boundaries, independent of business fixtures."""

import hashlib
import json
import sqlite3
import uuid
from types import SimpleNamespace

import pytest
from stage9_book import MixedBook

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.duplicate_freeze import (
    DUPLICATE_FREEZE_DDL,
    VerifiedDuplicateRoot,
    _bucket,
    _directory,
    lookup_duplicate_keys,
)
from ai_accounting.kernel.key_membership_filter import build_keys_filter, decode_keys_filter
from ai_accounting.kernel.types import canonical


def _directory_fixture():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.create_function("unhex", 1, bytes.fromhex)
    connection.executescript(DUPLICATE_FREEZE_DDL)
    key = canonical(["origin", "expense", 24300, "signature", "evidence"])
    number = _bucket(key)
    directory = _directory(number)
    leaf = canonical([[key, [["expense-a", "fact-a", 24300]]]])
    leaf_digest = hashlib.sha256(leaf.encode()).digest()
    directory_content = canonical({number: leaf_digest.hex()})
    directory_digest = hashlib.sha256(directory_content.encode()).digest()
    connection.execute("INSERT INTO duplicate_freeze_bucket VALUES(?,?)", (leaf_digest, leaf))
    connection.execute(
        "INSERT INTO duplicate_freeze_directory VALUES(?,?)",
        (directory_digest, directory_content),
    )
    root = VerifiedDuplicateRoot(
        24300, b"x" * 32, 0, {directory: directory_digest.hex()},
        decode_keys_filter(build_keys_filter((key,))),
    )
    return connection, root, key, leaf_digest, directory_digest


def test_duplicate_directory_proves_hit_and_absence_with_two_batched_reads():
    connection, root, key, _, _ = _directory_fixture()
    missing = canonical(["origin", "expense", 24300, "other", "evidence"])
    statements = []
    connection.set_trace_callback(statements.append)
    result = lookup_duplicate_keys(connection, root, (key, missing))
    assert result[key] == (("expense-a", "fact-a", 24300),)
    assert result[missing] == ()
    reads = [sql for sql in statements if sql.startswith("SELECT")]
    assert len(reads) <= 2


def test_authenticated_filter_skips_only_proven_negative_directories():
    from ai_accounting.kernel.key_membership_filter import may_contain

    connection, root, key, _, _ = _directory_fixture()
    missing = next(
        f"missing-{index}"
        for index in range(1000)
        if not may_contain(root.key_filter, f"missing-{index}")
    )
    statements = []
    connection.set_trace_callback(statements.append)
    assert lookup_duplicate_keys(connection, root, (missing,)) == {missing: ()}
    assert not any(sql.startswith("SELECT") for sql in statements)
    assert lookup_duplicate_keys(connection, root, (key,))[key] == (
        ("expense-a", "fact-a", 24300),
    )


def test_bound_root_missing_row_is_integrity_error():
    from ai_accounting.kernel.duplicate_freeze import verified_duplicate_root

    connection, _, _, _, _ = _directory_fixture()
    header = SimpleNamespace(
        period=24300,
        logical_digest=b"x" * 32,
        root={"source_changes": {"highwater": 0}, "derived_roots": {"duplicate": "a" * 64}},
    )
    with pytest.raises(KernelError) as failure:
        verified_duplicate_root(connection, header)
    assert failure.value.details["reason"] == "bound_root_missing"


@pytest.mark.parametrize(
    "bad_filter",
    [
        {"format": "future"},
        {"key_count": True},
        {"bits_base64": "AA=="},
    ],
)
def test_root_rejects_authenticated_but_malformed_filter(bad_filter):
    from ai_accounting.kernel.duplicate_freeze import FORMAT, verified_duplicate_root

    connection, root, key, _, _ = _directory_fixture()
    value = {
        "format": FORMAT,
        "period": root.period,
        "close_digest": root.close_digest.hex(),
        "journal_highwater": 0,
        "bucket_count": 65536,
        "directories": root.directories,
        "key_filter": {**build_keys_filter((key,)), **bad_filter},
    }
    payload = canonical(value)
    digest = hashlib.sha256(payload.encode()).digest()
    connection.execute(
        "INSERT INTO duplicate_freeze_root VALUES(?,?,?,?,?)",
        (root.period, root.close_digest, 0, payload, digest),
    )
    header = SimpleNamespace(
        period=root.period,
        logical_digest=root.close_digest,
        root={
            "source_changes": {"highwater": 0},
            "derived_roots": {"duplicate": digest.hex()},
        },
    )
    with pytest.raises(KernelError) as failure:
        verified_duplicate_root(connection, header)
    assert failure.value.details["reason"] == "root_shape_invalid"


@pytest.mark.parametrize("damage", ["directory", "leaf", "missing_leaf"])
def test_duplicate_directory_rejects_damaged_hit(damage):
    connection, root, key, leaf_digest, directory_digest = _directory_fixture()
    if damage == "directory":
        connection.execute(
            "UPDATE duplicate_freeze_directory SET content='{}' WHERE digest=?",
            (directory_digest,),
        )
    elif damage == "leaf":
        connection.execute(
            "UPDATE duplicate_freeze_bucket SET content='[]' WHERE digest=?",
            (leaf_digest,),
        )
    else:
        connection.execute("DELETE FROM duplicate_freeze_bucket WHERE digest=?", (leaf_digest,))
    with pytest.raises(KernelError) as error:
        lookup_duplicate_keys(connection, root, (key,))
    assert error.value.code == "content_integrity_failed"


@pytest.mark.parametrize(
    "invalid",
    [None, True, 17, 1.25, [], {}, "a" * 63, "a" * 65, "A" * 64, "g" * 64, "a" * 64 + "\n"],
)
def test_duplicate_directory_rejects_invalid_digest_even_when_root_matches(invalid):
    connection, root, key, _, _ = _directory_fixture()
    with connection:
        content = canonical({_bucket(key): invalid})
        value = hashlib.sha256(content.encode()).digest()
        connection.execute("INSERT INTO duplicate_freeze_directory VALUES(?,?)", (value, content))
        root = VerifiedDuplicateRoot(
            root.period, root.close_digest, 0, {_directory(_bucket(key)): value.hex()},
            root.key_filter,
        )
        with pytest.raises(KernelError) as failure:
            lookup_duplicate_keys(connection, root, (key,))
        assert failure.value.details["reason"] == "directory_shape_invalid"
    connection.close()


@pytest.mark.parametrize("leaf", ["not-number", "-1", "65536", "1.5"])
def test_duplicate_directory_rejects_invalid_leaf_range(leaf):
    connection, root, key, _, _ = _directory_fixture()
    with connection:
        content = canonical({leaf: "a" * 64})
        value = hashlib.sha256(content.encode()).digest()
        connection.execute("INSERT INTO duplicate_freeze_directory VALUES(?,?)", (value, content))
        root = VerifiedDuplicateRoot(
            root.period, root.close_digest, 0, {_directory(_bucket(key)): value.hex()},
            root.key_filter,
        )
        with pytest.raises(KernelError) as failure:
            lookup_duplicate_keys(connection, root, (key,))
        assert failure.value.details["reason"] == "directory_shape_invalid"
    connection.close()


def test_real_close_binds_duplicate_directory_and_independent_rebuild(tmp_path, monkeypatch):
    from ai_accounting.kernel.close_storage import verified_header
    from ai_accounting.kernel.duplicate_freeze import (
        compare_duplicate_freeze,
        verified_duplicate_root,
    )
    from ai_accounting.kernel.duplicates import DuplicateCandidates

    book = MixedBook(tmp_path / "stage9-duplicate-freeze", employees=1, businesses=26)
    book.add_month(0)
    with book.engine.store.connection(read_only=True) as connection:
        row = connection.execute("SELECT * FROM period_close").fetchone()
        header = verified_header(connection, row)
        root = verified_duplicate_root(connection, header)
        assert root is not None
        assert root.journal_highwater == header.root["source_changes"]["highwater"]
        noneligible = connection.execute(
            "SELECT c.fact_id FROM fact_current c JOIN subject s ON s.id=c.subject_id "
            "WHERE s.kind='report_profile' LIMIT 1"
        ).fetchone()[0]
        batches = []
        original = book.engine.store.facts

        def counted(connection, fact_ids):
            batches.append(set(fact_ids))
            return original(connection, fact_ids)

        with monkeypatch.context() as patch:
            patch.setattr(book.engine.store, "facts", counted)
            assert not compare_duplicate_freeze(connection, book.engine)
        assert len(batches) == 1
        assert noneligible in batches[0]
        assert DuplicateCandidates(book.engine.store).close_readiness(connection, "2016-01") == []


def test_v1_authority_reuses_already_decoded_fact_without_bulk_rescan():
    from ai_accounting.kernel.duplicate_freeze_v1 import _Authority

    authority = object.__new__(_Authority)
    cached = object()
    newly_loaded = object()
    authority._versions = {f"fact-{index}": object() for index in range(10_000)}
    authority._versions["cached"] = cached
    requested = []

    def load_one(fact_ids):
        requested.append(tuple(fact_ids))
        authority._versions["missing"] = newly_loaded

    authority.versions = load_one
    for _ in range(100):
        assert authority.version("cached") is cached
    assert requested == []
    assert authority.version("missing") is newly_loaded
    assert authority.version("missing") is newly_loaded
    assert requested == [("missing",)]


def test_nonempty_v1_directory_and_check_reader_survive_current_rule_changes(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import ai_accounting.kernel.change_journal as current_journal
    import ai_accounting.kernel.close_storage as current_close
    import ai_accounting.kernel.duplicate_freeze as current_freeze
    import ai_accounting.kernel.duplicates as current_duplicates
    import ai_accounting.kernel.key_membership_filter as current_filter
    import ai_accounting.kernel.storage as current_storage
    from ai_accounting.kernel.content_history_context import (
        duplicate_reader,
        historical_content,
        journal_reader,
    )
    from ai_accounting.kernel.content_v1 import _v1_model, registry_descriptor

    book = MixedBook(tmp_path / "stage9-duplicate-v1", employees=1, businesses=26)
    book.add_month(0)
    models = registry_descriptor(book.engine.store.registry)["models"]
    historical_engine = SimpleNamespace(
        store=SimpleNamespace(
            registry=SimpleNamespace(
                models={kind: _v1_model(kind, spec) for kind, spec in models.items()}
            )
        )
    )
    with book.engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM business_duplicate_check").fetchone()[0]
        assert connection.execute("SELECT count(*) FROM duplicate_freeze_bucket").fetchone()[0]
        with monkeypatch.context() as patch:

            def future_rule(*_args, **_kwargs):
                raise AssertionError("a v1 reader called a future current rule")

            patch.setattr(current_freeze, "_signals", future_rule)
            patch.setattr(current_freeze, "build_keys_filter", future_rule)
            patch.setattr(current_freeze, "decode_keys_filter", future_rule)
            patch.setattr(current_freeze, "may_contain", future_rule)
            patch.setattr(current_filter, "build_keys_filter", future_rule)
            patch.setattr(current_filter, "decode_keys_filter", future_rule)
            patch.setattr(current_filter, "may_contain", future_rule)
            patch.setattr(current_duplicates, "_signature", future_rule)
            patch.setattr(current_duplicates, "_require_check_record", future_rule)
            patch.setattr(current_duplicates, "verify_duplicate_checks", future_rule)
            patch.setattr(current_journal, "changes_since", future_rule)
            patch.setattr(current_close, "verified_header", future_rule)
            patch.setattr(current_storage, "decode_fields", future_rule)
            with historical_content(1):
                journal_reader().verify_journal(connection)
                duplicate_reader().verify_duplicate_checks(connection)
                assert not duplicate_reader().compare_duplicate_freeze(
                    connection, historical_engine
                )


@pytest.mark.parametrize(
    ("raw", "specification", "expected"),
    [
        (
            b"original receipt 12.00",
            {
                "format": "text",
                "passages": [{"location": "page-one", "page": 1, "excerpt": "receipt"}],
                "all_pages_reviewed": True,
            },
            "page-one",
        ),
        (
            b"name,amount\nAlice,12.00\n",
            {
                "format": "csv",
                "columns": [{"column": "B", "role": "amount"}],
            },
            "CSV!B2",
        ),
    ],
)
def test_v1_source_position_decoder_is_independent_of_current_parser(
    raw, specification, expected, monkeypatch
):
    from ai_accounting.kernel import material_inspection_v1, materials

    old_spec = material_inspection_v1.Specification.model_validate_json(canonical(specification))
    current_spec = materials.Specification.model_validate_json(canonical(specification))
    assert canonical(material_inspection_v1.inspect_bytes(raw, old_spec)) == canonical(
        materials.inspect_bytes(raw, current_spec)
    )
    monkeypatch.setattr(
        materials,
        "inspect_bytes",
        lambda *_args: (_ for _ in ()).throw(AssertionError("current parser called")),
    )
    assert any(
        item["location"] == expected
        for item in material_inspection_v1.inspect_bytes(raw, old_spec)["items"]
    )


def test_after_close_origin_change_matches_full_scan_and_moves_review_period(tmp_path, monkeypatch):
    import ai_accounting.kernel.duplicate_freeze as freeze_module
    from ai_accounting.kernel.contracts import FactVersion
    from ai_accounting.kernel.duplicates import DuplicateCandidates, _candidate_digest
    from ai_accounting.kernel.types import digest

    book = MixedBook(tmp_path / "stage9-duplicate-delta", employees=1, businesses=26)
    book.add_month(0)
    store = book.engine.store
    duplicates = DuplicateCandidates(store)
    with store.connection() as connection:
        connection.execute("BEGIN")
        candidates = connection.execute(
            "SELECT f.id FROM fact_current c JOIN fact_revision f ON f.id=c.fact_id "
            "JOIN subject s ON s.id=f.subject_id "
            "WHERE s.kind IN ('expense','service_sale','project_cost',"
            "'payment','cash_payment','platform_payment','funding',"
            "'cash_funding','platform_funding')"
        )
        old = None
        checked = None
        for row in candidates:
            candidate = store.fact(connection, row[0])
            proposed = duplicates.prepare(
                connection,
                subject_id="late-expense-copy",
                revision=1,
                fact=candidate.fact,
                evidence=candidate.evidence,
            )
            if proposed["strong_candidates"]:
                old, checked = candidate, proposed
                break
        assert old is not None and checked is not None
        added = FactVersion(
            uuid.uuid4().hex,
            "late-expense-copy",
            1,
            old.fact,
            old.evidence,
        )
        store.write_fact(connection, added, digest(added.fact.model_dump(mode="json")))
        checked["strong_candidates"] = []
        checked["candidate_digest"] = _candidate_digest(
            checked["proposed"], checked["source_locations"], []
        )
        duplicates.record_check(connection, prepared=checked, result_fact_id=added.id, review=None)
        connection.commit()
    with store.connection(read_only=True) as connection:
        fast = duplicates.close_readiness(connection, "2016-02")
        with monkeypatch.context() as patch:
            patch.setattr(freeze_module, "narrowed_duplicate_candidates", lambda *_a, **_k: None)
            full = duplicates.close_readiness(connection, "2016-02")
    assert canonical(fast) == canonical(full)
    assert any(item["review_period"] == "2016-02" for item in fast)
    from ai_accounting.kernel.close_storage import verified_header
    from ai_accounting.kernel.duplicate_freeze import (
        _signals,
        compare_duplicate_freeze,
        verified_duplicate_root,
    )
    from ai_accounting.kernel.duplicates import _material_locations

    with store.connection() as connection:
        connection.execute("BEGIN")
        header = verified_header(
            connection, connection.execute("SELECT * FROM period_close").fetchone()
        )
        root = verified_duplicate_root(connection, header)
        locations = _material_locations(connection, (old.id, added.id))
        keys = _signals(old, locations[old.id]) & _signals(added, locations[added.id])
        hit = next(key for key in keys if freeze_module.lookup_duplicate_key(connection, root, key))
        directory_number = _directory(_bucket(hit))
        directory = connection.execute(
            "SELECT content FROM duplicate_freeze_directory WHERE digest=?",
            (bytes.fromhex(root.directories[directory_number]),),
        ).fetchone()[0]
        leaf_digest = bytes.fromhex(json.loads(directory)[_bucket(hit)])
        connection.execute(
            "UPDATE duplicate_freeze_bucket SET content='[]' WHERE digest=?",
            (leaf_digest,),
        )
        connection.commit()
    with store.connection(read_only=True) as connection:
        with pytest.raises(KernelError) as error:
            duplicates.close_readiness(connection, "2016-02")
        assert error.value.code == "content_integrity_failed"
    with store.connection() as connection:
        connection.execute("BEGIN")
        assert compare_duplicate_freeze(connection, book.engine, repair=True)
        connection.commit()
    with store.connection(read_only=True) as connection:
        assert canonical(duplicates.close_readiness(connection, "2016-02")) == canonical(full)


def test_open_month_origin_skips_frozen_lookup_and_matches_full_scan(tmp_path, monkeypatch):
    import ai_accounting.kernel.duplicate_freeze as freeze_module
    from ai_accounting.kernel.duplicates import DuplicateCandidates

    book = MixedBook(tmp_path / "stage9-duplicate-open-origin", employees=1, businesses=26)
    book.add_month(0)
    book.add_month(1, close=False)
    duplicates = DuplicateCandidates(book.engine.store)
    original = freeze_module.lookup_duplicate_keys
    original_signals = freeze_module._signal_parts
    looked_up = []
    generated_future_origins = []

    def trace(connection, root, keys, *, cache=None):
        looked_up.extend((root.period, json.loads(key)) for key in keys)
        return original(connection, root, keys, cache=cache)

    def signals(version, locations):
        keys = list(original_signals(version, locations))
        generated_future_origins.extend(
            key for key in keys if key[0] == "origin"
        )
        return keys

    with book.engine.store.connection(read_only=True) as connection:
        close_period = connection.execute("SELECT period FROM period_close").fetchone()[0]
        with monkeypatch.context() as patch:
            patch.setattr(freeze_module, "lookup_duplicate_keys", trace)
            patch.setattr(freeze_module, "_signal_parts", signals)
            fast = duplicates.close_readiness(connection, "2016-02")
        with monkeypatch.context() as patch:
            patch.setattr(freeze_module, "narrowed_duplicate_candidates", lambda *_a, **_k: None)
            full = duplicates.close_readiness(connection, "2016-02")
    assert canonical(fast) == canonical(full)
    assert generated_future_origins
    assert any(key[2] > close_period for key in generated_future_origins)
    assert all(key[2] <= period for period, key in looked_up if key[0] == "origin")


def test_readiness_reuses_typed_facts_only_after_source_verification(tmp_path, monkeypatch):
    from test_integrity_content import damage

    import ai_accounting.kernel.duplicate_freeze as freeze_module
    from ai_accounting.kernel.query_reads import QueryReads
    from ai_accounting.kernel.types import YearMonth

    book = MixedBook(tmp_path / "stage9-duplicate-typed-reuse", employees=1, businesses=26)
    book.add_month(0)
    book.add_month(1, close=False)
    engine = book.engine
    month = YearMonth("2016-02").ordinal
    with engine.store.connection(read_only=True) as connection:
        baseline = freeze_module.narrowed_duplicate_candidates(engine, connection, month)
        expense = connection.execute(
            "SELECT f.id FROM fact_current c JOIN fact_revision f ON f.id=c.fact_id "
            "JOIN subject s ON s.id=f.subject_id WHERE s.kind='expense' AND f.period=? LIMIT 1",
            (month,),
        ).fetchone()[0]

    def no_second_decode(_raw):
        raise AssertionError("material and duplicate checks decoded the same fact twice")

    with QueryReads.snapshot(engine) as reads:
        reads.fact_versions(baseline[1])
        with monkeypatch.context() as patch:
            patch.setattr(freeze_module, "_validation_json", no_second_decode)
            actual = freeze_module.narrowed_duplicate_candidates(
                engine, reads.connection, month, query_reads=reads
            )
        assert actual == baseline
    assert not reads._fact_versions

    damage(
        engine, "fact_expense",
        "UPDATE fact_expense SET amount_fen=amount_fen+1 WHERE revision_id=?", (expense,),
    )
    with QueryReads.snapshot(engine) as reads:
        # A well-typed cached object is not a source-content proof.
        reads.fact_versions(baseline[1])
        with pytest.raises(KernelError) as failure:
            freeze_module.narrowed_duplicate_candidates(
                engine, reads.connection, month, query_reads=reads
            )
        assert failure.value.code == "content_integrity_failed"


def test_candidate_fact_selection_is_bounded_by_period_and_changed_subjects():
    from ai_accounting.kernel.duplicate_freeze import _current_candidate_facts
    from ai_accounting.kernel.duplicates import ELIGIBLE_KINDS

    connection = sqlite3.connect(":memory:")
    connection.executescript(
        "CREATE TABLE subject(id TEXT PRIMARY KEY,kind TEXT NOT NULL);"
        "CREATE INDEX subject_kind ON subject(kind,id);"
        "CREATE TABLE fact_revision(id TEXT PRIMARY KEY,subject_id TEXT NOT NULL,"
        "period INTEGER NOT NULL);"
        "CREATE INDEX fact_period ON fact_revision(period,subject_id,id);"
        "CREATE TABLE fact_current(subject_id TEXT PRIMARY KEY,fact_id TEXT NOT NULL UNIQUE);"
    )
    rows = (
        [(f"old-{i}", "expense", 100) for i in range(5000)]
        + [(f"open-{i}", "expense", 101) for i in range(1000)]
        + [(f"other-{i}", "material_resolution_v2", 101) for i in range(1000)]
        + [(f"changed-other-{i}", "material_resolution_v2", 100) for i in range(1000)]
        + [("future-0", "expense", 102)]
    )
    connection.executemany(
        "INSERT INTO subject VALUES(?,?)", ((ident, kind) for ident, kind, _ in rows)
    )
    connection.executemany(
        "INSERT INTO fact_revision VALUES(?,?,?)",
        ((f"fact-{ident}", ident, period) for ident, _, period in rows),
    )
    connection.executemany(
        "INSERT INTO fact_current VALUES(?,?)", ((ident, f"fact-{ident}") for ident, _, _ in rows)
    )
    changed = {
        ident for ident, _, _ in rows if ident.startswith(("open-", "other-", "changed-other-"))
    }
    changed.add("old-0")
    changed.add("future-0")
    kinds = canonical(sorted(ELIGIBLE_KINDS))
    ticks = [0]

    def progress():
        ticks[0] += 1
        return 0

    connection.set_progress_handler(progress, 100)
    old_rows = connection.execute(
        "SELECT f.id,f.subject_id FROM fact_current c "
        "JOIN fact_revision f ON f.id=c.fact_id JOIN subject s ON s.id=f.subject_id "
        "WHERE f.period>? AND f.period<=? AND s.kind IN (SELECT value FROM json_each(?))",
        (100, 101, kinds),
    ).fetchall()
    old_rows += connection.execute(
        "SELECT f.id,f.subject_id FROM json_each(?) ids "
        "JOIN fact_current c ON c.subject_id=ids.value "
        "JOIN fact_revision f ON f.id=c.fact_id JOIN subject s ON s.id=f.subject_id "
        "WHERE f.period<=? AND s.kind IN (SELECT value FROM json_each(?))",
        (canonical(sorted(changed)), 101, kinds),
    ).fetchall()
    expected = dict((subject_id, fact_id) for fact_id, subject_id in old_rows)
    old_vm = ticks[0] * 100
    ticks[0] = 0
    try:
        actual = _current_candidate_facts(connection, 100, 101, changed)
    finally:
        connection.set_progress_handler(None, 0)
    assert actual == expected
    assert len(actual) == 1001
    assert ticks[0] * 100 < old_vm // 3
    connection.execute("INSERT INTO fact_revision VALUES('misowned','old-1',100)")
    connection.execute("UPDATE fact_current SET fact_id='misowned' WHERE subject_id='old-0'")
    with pytest.raises(KernelError) as error:
        _current_candidate_facts(connection, 100, 101, changed)
    assert error.value.code == "content_integrity_failed"
