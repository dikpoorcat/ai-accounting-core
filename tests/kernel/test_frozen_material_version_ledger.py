"""Reuse only successful typed-version matches within one unchanged verification."""

import json
from collections import Counter

import pytest
from test_exact_id_query_work import MATERIAL_KINDS, _database, _revision
from test_integrity_content import damage
from test_reports import book as book  # noqa: F401
from test_reports import close_quarter, scenario

from ai_accounting.kernel import close_storage, frozen_material
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.integrity import verify_close_integrity, verify_integrity
from ai_accounting.kernel.verified_source_lease import verified_source_lease


class _VersionConnection:
    """Count actual exact-ID query candidates, preserving SQLite query results."""

    def __init__(self, connection):
        self.connection = connection
        self.candidates = Counter()

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, sql, *args):
        if sql.startswith(("SELECT f.subject_id,ids.value", "SELECT t.source_id,ids.value")):
            assert "CROSS JOIN fact_seal seal" in sql
            identifiers, kind = args[0]
            self.candidates[kind] += len(json.loads(identifiers))
        return self.connection.execute(sql, *args)


def _read(connection, kind, table, identifiers, ledger):
    return dict(
        frozen_material._frozen_versions(
            connection, kind, table, identifiers, _verified_versions=ledger
        )
    )


@pytest.mark.parametrize("kind,table", MATERIAL_KINDS)
def test_repeated_and_nonmonotonic_version_sets_match_independent_reads(kind, table, request):
    raw = _database()
    try:
        _revision(raw, kind, table, "old", "subject-target")
        _revision(raw, kind, table, "new", "subject-target", revision=2)
        # More unrelated subjects and historical revisions cannot become proof entries.
        for number in range(12):
            _revision(raw, kind, table, f"noise-{number}", f"noise-subject-{number}")
        connection = _VersionConnection(raw)
        scopes = (["old"], ["old", "new"], ["new"], ["old"])
        expected = [_read(connection, kind, table, ids, None) for ids in scopes]
        original_work = connection.candidates.copy()
        connection.candidates.clear()
        with verified_source_lease(connection):
            ledger = frozen_material._verified_material_version_ledger(connection)
            actual = [_read(connection, kind, table, ids, ledger) for ids in scopes]
        assert actual == expected
        assert original_work == {kind: 5}
        assert connection.candidates == {kind: 2}
        source = "subject-target" if kind == "material_source_v2" else "source-target"
        assert actual == [
            {source: {"old"}},
            {source: {"old", "new"}},
            {source: {"new"}},
            {source: {"old"}},
        ]
        request.node.user_properties.extend(
            [("original_version_candidates", 5), ("ledger_version_candidates", 2)]
        )
    finally:
        raw.close()


def test_cached_versions_still_validate_shape_kind_and_producer():
    raw = _database()
    kind, table = MATERIAL_KINDS[0]
    try:
        _revision(raw, kind, table, "target", "source-target")
        connection = _VersionConnection(raw)
        with verified_source_lease(connection):
            ledger = frozen_material._verified_material_version_ledger(connection)
            assert _read(connection, kind, table, ["target"], ledger)
            for invalid in (("target",), [None], ["target", "target"]):
                with pytest.raises(KernelError) as error:
                    _read(connection, kind, table, invalid, ledger)
                assert error.value.code == "content_integrity_failed"
            with pytest.raises(ValueError, match="kind and typed table"):
                _read(connection, kind, MATERIAL_KINDS[1][1], ["target"], ledger)
            for fake in (
                {"source-target": ["target"]},
                object(),
                object.__new__(frozen_material._MaterialVersionLedger),
            ):
                with pytest.raises(ValueError, match="verified producer"):
                    _read(connection, kind, table, ["target"], fake)
            with pytest.raises(ValueError, match="verified producer"):
                frozen_material._MaterialVersionLedger(connection, ledger.lease, _producer=object())
            assert connection.candidates == {kind: 1}
    finally:
        raw.close()


@pytest.mark.parametrize("damage_kind", ["missing", "seal", "typed", "kind"])
def test_failed_batch_does_not_publish_any_new_matches(damage_kind):
    raw = _database()
    kind, table = MATERIAL_KINDS[2]
    try:
        _revision(raw, kind, table, "valid", "valid-subject")
        invalid = "absent"
        if damage_kind != "missing":
            invalid = "invalid"
            _revision(raw, kind, table, invalid, "invalid-subject")
            if damage_kind == "seal":
                raw.execute("DELETE FROM fact_seal WHERE fact_id='invalid'")
            elif damage_kind == "typed":
                raw.execute(f"DELETE FROM {table} WHERE revision_id='invalid'")
            else:
                raw.execute("UPDATE subject SET kind='wrong' WHERE id='invalid-subject'")
        connection = _VersionConnection(raw)
        with verified_source_lease(connection):
            ledger = frozen_material._verified_material_version_ledger(connection)
            with pytest.raises(KernelError) as error:
                _read(connection, kind, table, ["valid", invalid], ledger)
            assert error.value.code == "content_integrity_failed"
            assert _read(connection, kind, table, ["valid"], ledger) == {"source-target": {"valid"}}
            # The valid half of the failed query is queried again, never adopted.
            assert connection.candidates == {kind: 3}
    finally:
        raw.close()


def test_ledger_connection_scope_and_nested_lease_are_required():
    raw, other = _database(), _database()
    kind, table = MATERIAL_KINDS[0]
    try:
        _revision(raw, kind, table, "target", "source-target")
        with pytest.raises(ValueError, match="original verification scope"):
            frozen_material._verified_material_version_ledger(raw)
        with verified_source_lease(raw):
            ledger = frozen_material._verified_material_version_ledger(raw)
            assert _read(raw, kind, table, ["target"], ledger)
            with pytest.raises(ValueError, match="another connection"):
                _read(other, kind, table, ["target"], ledger)
            with verified_source_lease(raw):
                with pytest.raises(ValueError, match="another verification scope"):
                    _read(raw, kind, table, ["target"], ledger)
                inner = frozen_material._verified_material_version_ledger(raw)
                assert _read(raw, kind, table, ["target"], inner)
            assert _read(raw, kind, table, ["target"], ledger)
        with verified_source_lease(raw):
            with pytest.raises(ValueError, match="another verification scope"):
                _read(raw, kind, table, ["target"], ledger)
    finally:
        raw.close()
        other.close()


@pytest.mark.parametrize("change", ["write", "rolled_back_write", "schema", "commit", "rollback"])
def test_ledger_rejects_changed_or_restarted_transaction(change):
    raw = _database()
    kind, table = MATERIAL_KINDS[0]
    try:
        _revision(raw, kind, table, "target", "source-target")
        raw.commit()
        raw.execute("BEGIN")
        with verified_source_lease(raw):
            ledger = frozen_material._verified_material_version_ledger(raw)
            assert _read(raw, kind, table, ["target"], ledger)
            if change in {"write", "rolled_back_write"}:
                raw.execute("SAVEPOINT business_write")
                raw.execute("UPDATE subject SET kind=kind WHERE id='source-target'")
                if change == "rolled_back_write":
                    raw.execute("ROLLBACK TO business_write")
            elif change == "schema":
                raw.execute("CREATE TABLE changed_schema(id INTEGER)")
            else:
                getattr(raw, change)()
                raw.execute("BEGIN")
            with pytest.raises(ValueError, match="snapshot changed|transaction has ended"):
                _read(raw, kind, table, ["target"], ledger)
    finally:
        raw.close()


def _manifest_work(engine):
    summed, union = Counter(), {kind: set() for kind, _ in MATERIAL_KINDS}
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        for row in connection.execute("SELECT * FROM period_close ORDER BY period"):
            manifest = close_storage.decode_close(connection, row)
            for label, (kind, _) in frozen_material._KINDS.items():
                ids = manifest["material_coverage"][frozen_material._PROOF_FIELDS[label]]
                summed[kind] += len(ids)
                union[kind].update(ids)
    return summed, Counter({kind: len(ids) for kind, ids in union.items()})


@pytest.mark.parametrize("boundary", ["full", "close"])
def test_real_three_month_verifier_reduces_only_successful_matches(
    book, monkeypatch, request, boundary
):
    engine = book[0]
    scenario(book)
    close_quarter(book)
    summed, union = _manifest_work(engine)
    assert sum(summed.values()) > sum(union.values()) > 0

    def run():
        with engine.store.connection(read_only=True) as raw:
            raw.execute("BEGIN")
            connection = _VersionConnection(raw)
            result = (
                verify_integrity(engine, connection)
                if boundary == "full"
                else verify_close_integrity(engine, connection, "2026-03")
            )
            return result, connection.candidates

    actual, actual_work = run()
    # The off control executes the original exact-ID queries for every manifest;
    # all source, material, close, projection and root checks still run.
    with monkeypatch.context() as control:
        control.setattr(
            frozen_material, "_verified_material_version_ledger", lambda connection: None
        )
        original, original_work = run()
    assert actual == original
    assert original_work == summed
    assert actual_work == union
    request.node.user_properties.extend(
        [
            ("original_version_candidates", sum(original_work.values())),
            ("ledger_version_candidates", sum(actual_work.values())),
            ("original_by_kind", json.dumps(dict(original_work), sort_keys=True)),
            ("ledger_by_kind", json.dumps(dict(actual_work), sort_keys=True)),
        ]
    )


def test_close_writer_never_adopts_the_read_ledger(book, monkeypatch):
    engine = book[0]
    scenario(book)
    writer_calls = []
    original = close_storage._material_summaries
    original_write = close_storage.write_close
    writing = False

    def write(*args, **kwargs):
        nonlocal writing
        writing = True
        try:
            return original_write(*args, **kwargs)
        finally:
            writing = False

    def checked(connection, manifest, *, _verified_material_versions=None):
        if writing:
            writer_calls.append(_verified_material_versions)
        return original(
            connection, manifest, _verified_material_versions=_verified_material_versions
        )

    monkeypatch.setattr(close_storage, "write_close", write)
    monkeypatch.setattr(close_storage, "_material_summaries", checked)
    close_quarter(book)
    assert writer_calls == [None, None, None]
    assert _manifest_work(engine)[0]


def test_stored_source_summary_does_not_populate_typed_version_proof(book):
    engine = book[0]
    scenario(book)
    close_quarter(book)
    with engine.store.connection(read_only=True) as connection:
        row = connection.execute("SELECT * FROM period_close ORDER BY period LIMIT 1").fetchone()
        first = close_storage.decode_close(connection, row)
        target = first["material_coverage"]["resolution_versions"][0]
    damage(
        engine,
        "fact_material_resolution_v2",
        "UPDATE fact_material_resolution_v2 SET source_id='forged-source' WHERE revision_id=?",
        (target,),
    )
    for reuse in (False, True):
        with engine.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            with verified_source_lease(connection):
                ledger = (
                    frozen_material._verified_material_version_ledger(connection) if reuse else None
                )
                row = connection.execute(
                    "SELECT * FROM period_close ORDER BY period LIMIT 1"
                ).fetchone()
                with pytest.raises(KernelError) as error:
                    close_storage.decode_close(connection, row, _verified_material_versions=ledger)
                assert error.value.code == "content_integrity_failed"
