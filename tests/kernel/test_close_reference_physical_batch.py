"""Exact frozen reference groups batch first reads without narrowing proofs."""

import sqlite3
from types import SimpleNamespace

import pytest
from stage9_metrics import measure_work
from test_close_physical_batch import PhysicalStore, _sha
from test_employee_adopted_period_scopes import prepare
from test_engine import engine as engine_fixture
from test_integrity_content import damage

from ai_accounting.kernel import close_storage, read_indexes
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.types import canonical

engine = engine_fixture


@pytest.fixture
def physical_references():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        "CREATE TABLE period_close(period INTEGER PRIMARY KEY);"
        "CREATE TABLE calculation(id TEXT PRIMARY KEY,subject_id TEXT);"
        "CREATE TABLE fact_revision(id TEXT PRIMARY KEY,subject_id TEXT);"
        "CREATE TABLE voucher_version(id TEXT PRIMARY KEY,calculation_id TEXT);"
        + close_storage.CLOSE_STORAGE_DDL
    )
    groups = []
    for period in range(120):
        subject, calculation, fact, version = (f"{prefix}-{period}" for prefix in "scfv")
        connection.execute("INSERT INTO period_close VALUES(?)", (period,))
        connection.execute("INSERT INTO calculation VALUES(?,?)", (calculation, subject))
        connection.execute("INSERT INTO fact_revision VALUES(?,?)", (fact, subject))
        connection.execute("INSERT INTO voucher_version VALUES(?,?)", (version, calculation))
        bucket = close_storage._bucket(subject)
        descriptors = {}
        for field, content in (
            ("adopted_results", {"subject_id": subject, "calculation_id": calculation,
                                 "fact_id": fact}),
            ("vouchers", {"id": version, "calculation_id": calculation}),
        ):
            body = canonical([[0, content]])
            connection.execute(
                "INSERT INTO close_storage_block VALUES(?,?,?,?,?,?)",
                (period, field, bucket, 0, body, _sha(body)),
            )
            directory = canonical([[0, _sha(body).hex(), 1]])
            connection.execute(
                "INSERT INTO close_storage_directory VALUES(?,?,?,?,?)",
                (period, field, bucket, directory, _sha(directory)),
            )
            descriptors[field] = [[bucket, _sha(directory).hex(), 1]]
        subroot = canonical({"directories": descriptors})
        connection.execute("INSERT INTO close_storage_subroot VALUES(?,?,?,?)",
                           (period, "accounting", subroot, _sha(subroot)))
        material_bucket = close_storage._bucket(fact)
        body = canonical([[0, fact]])
        directory = canonical([[0, _sha(body).hex(), 1]])
        material = canonical({"directories": {
            "material_coverage.fact_ids": [[material_bucket, _sha(directory).hex(), 1]]
        }})
        connection.execute("INSERT INTO close_storage_subroot VALUES(?,?,?,?)",
                           (period, "material", material, _sha(material)))
        connection.execute("INSERT INTO close_storage_directory VALUES(?,?,?,?,?)",
                           (period, "material_coverage.fact_ids", material_bucket,
                            directory, _sha(directory)))
        connection.execute("INSERT INTO close_storage_block VALUES(?,?,?,?,?,?)",
                           (period, "material_coverage.fact_ids", material_bucket,
                            0, body, _sha(body)))
        header = close_storage.CloseHeader(period, b"l" * 32, b"s" * 32,
                                          {"subroots": {"accounting": _sha(subroot).hex(),
                                                        "material": _sha(material).hex()}})
        groups.append((header, [
            ("adopted_results[*].calculation_id", 0, calculation),
            ("adopted_results[*].fact_id", 0, fact),
            ("vouchers[*].id", 0, version),
            ("vouchers[*].calculation_id", 0, calculation),
        ]))
    yield SimpleNamespace(store=PhysicalStore(connection)), groups
    connection.close()


@pytest.mark.parametrize("months", [12, 48])
@pytest.mark.parametrize("mixed_families", [False, True])
def test_initial_reference_work_is_exact_and_batched(
    physical_references, monkeypatch, months, mixed_families, record_property
):
    synthetic, groups = physical_references
    if mixed_families:
        groups = [
            (header, [*references, ("material_coverage.fact_ids[*]", 0, f"f-{header.period}")])
            for header, references in groups
        ]
    monkeypatch.setattr(close_storage, "_owned_header_snapshot", lambda connection: True)

    def read(batch):
        with synthetic.store.connection(read_only=True) as connection:
            parts = {}
            result = (
                close_storage.reference_leaves_many(connection, groups[:months], parts=parts)
                if batch else tuple(close_storage.reference_leaves(
                    connection, header, references, parts=parts
                ) for header, references in groups[:months])
            )
            return result, parts

    before, expected = measure_work(synthetic, lambda: read(False))
    after, actual = measure_work(synthetic, lambda: read(True))
    assert actual == expected
    old, new = before["counters"], after["counters"]
    record_property("reference_physical_work", {"months": months, "old": old, "batch": new})
    assert old["sql_calls"] == (11 if mixed_families else 8) * months
    assert new["sql_calls"] == (7 if mixed_families else 6)
    assert new["returned_rows"] == old["returned_rows"]
    assert new["returned_value_bytes"] <= old["returned_value_bytes"]
    assert new["stdlib_json_input_bytes"] == old["stdlib_json_input_bytes"]
    assert new["stdlib_json_loads"] == old["stdlib_json_loads"]
    assert new["sqlite_vm_steps"] <= old["sqlite_vm_steps"] * 2


def _references(connection):
    return [dict(row) for row in connection.execute(
        "SELECT * FROM close_reference WHERE path IN "
        "('adopted_results[*].calculation_id','adopted_results[*].fact_id',"
        "'vouchers[*].id','vouchers[*].calculation_id') ORDER BY close_period,path,position"
    )]


def test_public_reference_verifier_groups_first_reads_and_later_accounting_reuses_them(
    engine, monkeypatch
):
    scopes = prepare(engine)
    original = close_storage.reference_leaves_many
    observed = []

    def counted(connection, groups, *, parts):
        groups = tuple(groups)
        observed.append(tuple(header.period for header, _ in groups))
        return original(connection, groups, parts=parts)

    monkeypatch.setattr(close_storage, "reference_leaves_many", counted)
    with QueryReads.snapshot(engine) as reads:
        references = _references(reads.connection)
        reads.verify_close_references(references)  # No preexisting header cache.
        assert len(observed) == 1 and len(observed[0]) == 2
        proven_periods = {reference["close_period"] for reference in references}
        assert set(reads._close_headers) == proven_periods
        reads.verify_close_references(references)
        assert len(observed) == 1
        statements = []
        reads.connection.set_trace_callback(statements.append)
        proven_rows = reads.authoritative_close_rows(periods=proven_periods)
        assert {row["period"] for row in proven_rows} == proven_periods
        assert len(statements) == 1 and "FROM period_close" in statements[0]
        rows = reads.authoritative_close_rows(periods=scopes)
        statements.clear()
        selected = reads.close_accounting_many(rows, subjects=set().union(*scopes.values()))
        assert [len(part.adopted_results) for part in selected] == [1, 1, 0]
        assert not any("close_storage_directory" in sql or "close_storage_block" in sql
                       for sql in statements)


def test_reference_success_only_publishes_to_active_header_map(engine):
    prepare(engine)
    with QueryReads.snapshot(engine) as reads:
        other_headers = {}
        read_indexes.verify_close_references(
            reads.connection, _references(reads.connection), _verified_headers=other_headers,
        )
        assert not other_headers
        assert not reads._close_headers
        assert not reads._verified_close_references


def test_header_batch_reuses_exact_hits_and_only_reads_missing_support(engine):
    scopes = prepare(engine)
    first = min(scopes)
    with QueryReads.snapshot(engine) as reads:
        reads.authoritative_close_rows(periods=[first])
        previous = dict(reads._close_headers)
        rows = list(reads.connection.execute("SELECT * FROM period_close ORDER BY period"))
        statements = []
        reads.connection.set_trace_callback(statements.append)
        headers = close_storage.verified_headers(reads.connection, rows)
        assert headers[0] is previous[first]
        assert tuple(header.period for header in headers) == tuple(sorted(scopes))
        assert len(statements) == 2
        assert f"'[{', '.join(str(period) for period in sorted(scopes)[1:])}]'" in statements[0]
        assert reads._close_headers == previous  # The physical helper never publishes.


@pytest.mark.parametrize("change", ["manifest", "digest", "period"])
def test_header_batch_inexact_row_retains_original_rejection(engine, change):
    scopes = prepare(engine)
    with QueryReads.snapshot(engine) as reads:
        rows = reads.authoritative_close_rows(periods=scopes)
        previous = dict(reads._close_headers)
        altered = dict(rows[-1])
        altered[change] = {
            "manifest": altered["manifest"] + " ",
            "digest": b"x" * 32,
            "period": altered["period"] + 1,
        }[change]
        statements = []
        reads.connection.set_trace_callback(statements.append)
        for _ in range(2):
            with pytest.raises(KernelError) as failure:
                close_storage.verified_headers(reads.connection, [*rows[:-1], altered])
            assert failure.value.code == "content_integrity_failed"
            assert reads._close_headers == previous
        assert len(statements) == 4  # Exact earlier rows need no additional support reads.


def test_header_batch_late_missing_marker_keeps_only_prior_success(engine):
    scopes = prepare(engine)
    first, last = min(scopes), max(scopes)
    damage(engine, "read_index_source",
           "DELETE FROM read_index_source WHERE source_kind='close' AND source_id=?",
           (str(last),), foreign_keys=False)
    with QueryReads.snapshot(engine) as reads:
        reads.authoritative_close_rows(periods=[first])
        previous = dict(reads._close_headers)
        rows = list(reads.connection.execute("SELECT * FROM period_close ORDER BY period"))
        for _ in range(2):
            with pytest.raises(KernelError) as failure:
                close_storage.verified_headers(reads.connection, rows)
            assert failure.value.code == "content_integrity_failed"
            assert failure.value.details["reason"] == "source_digest_or_marker_mismatch"
            assert reads._close_headers == previous


@pytest.mark.parametrize("change", ["position", "related", "source", "block"])
def test_reference_group_failure_never_publishes_checked_prefix(engine, change):
    prepare(engine)
    with QueryReads.snapshot(engine) as reads:
        references = _references(reads.connection)
    last = max(row["close_period"] for row in references)
    if change == "source":
        missing = next(row for row in references
                       if row["close_period"] == last and row["reference_type"] == "calculation")
        missing["reference_id"] = "missing-calculation"
    elif change == "block":
        damage(engine, "close_storage_block",
               "UPDATE close_storage_block SET content='[]' WHERE period=? AND field='vouchers'",
               (last,))
    else:
        selected = next(row for row in references
                        if row["close_period"] == last and row["path"] == "vouchers[*].id")
        selected["position" if change == "position" else "related_id"] = (
            "999999" if change == "position" else "wrong-related"
        )
    with QueryReads.snapshot(engine) as reads:
        for _ in range(2):
            with pytest.raises(KernelError) as failure:
                reads.verify_close_references(references)
            assert failure.value.code == (
                "read_index_integrity_failed" if change == "related"
                else "content_integrity_failed"
            )
            if change == "related":
                assert failure.value.details["reason"] == "reference_leaf_mismatch"
            assert not reads._verified_close_storage_parts
            assert not reads._verified_close_references
            assert not reads._close_headers


@pytest.mark.parametrize("mode", ["unowned", "v1", "single", "invalid_period"])
def test_noneligible_reference_reads_do_not_use_current_many(engine, monkeypatch, mode):
    prepare(engine)

    def forbidden(*args, **kwargs):
        raise AssertionError("noneligible reference batch")

    monkeypatch.setattr(close_storage, "reference_leaves_many", forbidden)
    if mode == "unowned":
        with engine.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            reads = QueryReads(engine, connection)
            reads.verify_close_references(_references(connection))
        return
    with QueryReads.snapshot(engine) as reads:
        references = _references(reads.connection)
        if mode == "v1":
            with historical_content(1):
                reads.verify_close_references(references)
        elif mode == "single":
            first = min(row["close_period"] for row in references)
            reads.verify_close_references([
                row for row in references if row["close_period"] == first
            ])
        else:
            references[-1]["close_period"] = True
            with pytest.raises(KernelError) as failure:
                reads.verify_close_references(references)
            assert failure.value.code == "read_index_integrity_failed"
            assert failure.value.details["reason"] == "source_digest_or_marker_mismatch"
            assert not reads._verified_close_references
            assert not reads._verified_close_storage_parts
