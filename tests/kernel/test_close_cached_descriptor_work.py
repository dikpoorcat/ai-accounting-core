"""Cached bucket groups omit only a previously completed owned descriptor walk."""

import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from test_close_descriptor_work import CountedDescriptors

from ai_accounting.kernel import close_storage
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.types import canonical


@pytest.fixture
def make_storage():
    connections = []

    def make(count, *, broken=None):
        connection = sqlite3.connect(":memory:")
        connections.append(connection)
        connection.row_factory = sqlite3.Row
        connection.executescript(
            "CREATE TABLE period_close(period INTEGER PRIMARY KEY);"
            + close_storage.CLOSE_STORAGE_DDL
        )
        headers = []
        for period in (1, 2):
            connection.execute("INSERT INTO period_close VALUES(?)", (period,))
            descriptors = []
            for bucket in range(count):
                entries = [[bucket, {"id": f"{period}-{bucket}"}]]
                if broken == "positions" and period == 2 and bucket == count - 1:
                    entries.append(entries[0])
                body = canonical(entries)
                block_digest = close_storage._sha(body)
                directory = canonical([[0, block_digest.hex(), len(entries)]])
                connection.execute(
                    "INSERT INTO close_storage_block VALUES(?,?,?,?,?,?)",
                    (period, "adopted_results", bucket, 0, body, block_digest),
                )
                connection.execute(
                    "INSERT INTO close_storage_directory VALUES(?,?,?,?,?)",
                    (period, "adopted_results", bucket, directory, close_storage._sha(directory)),
                )
                descriptors.append([bucket, close_storage._sha(directory).hex(), len(entries)])
            subroot = canonical({"directories": {"adopted_results": descriptors}})
            if broken == "duplicate_descriptor":
                descriptors.append([0, "0" * 64, 999])
                subroot = canonical({"directories": {"adopted_results": descriptors}})
            connection.execute(
                "INSERT INTO close_storage_subroot VALUES(?,?,?,?)",
                (period, "accounting", subroot, close_storage._sha(subroot)),
            )
            headers.append(
                close_storage.CloseHeader(
                    period,
                    b"l" * 32,
                    bytes([period]) * 32,
                    {"subroots": {"accounting": close_storage._sha(subroot).hex()}},
                )
            )
        if broken == "digest":
            connection.execute(
                "UPDATE close_storage_block SET content='[]' WHERE period=2 AND bucket=?",
                (count - 1,),
            )
        connection.commit()
        connection.execute("PRAGMA query_only=ON")

        class Store:
            registry = SimpleNamespace(content_version=2)

            @contextmanager
            def connection(self, *, read_only=False):
                assert read_only
                try:
                    yield connection
                finally:
                    connection.rollback()

        return SimpleNamespace(store=Store()), headers

    yield make
    for connection in connections:
        connection.close()


def group(connection, header, buckets):
    # Count the actual descriptor object only after normal digest authentication.
    subroot = close_storage._family(connection, header, "accounting")
    subroot["directories"]["adopted_results"] = CountedDescriptors(
        subroot["directories"]["adopted_results"]
    )
    return header, subroot, "adopted_results", buckets


def descriptors(selected):
    return selected[1]["directories"][selected[2]]


@pytest.mark.parametrize("count", [32, 128, 256])
def test_cache_hit_descriptor_work_does_not_grow(make_storage, count):
    engine, headers = make_storage(count)
    with QueryReads.snapshot(engine) as reads:
        selected = group(reads.connection, headers[0], [0])
        parts = reads._verified_close_storage_parts
        close_storage._prime_buckets_many(reads.connection, [selected], parts)
        assert descriptors(selected).visits == count
        before = dict(parts)
        descriptors(selected).visits = 0
        statements = []
        reads.connection.set_trace_callback(statements.append)
        close_storage._prime_buckets_many(reads.connection, [selected], parts)
        assert descriptors(selected).visits == 0
        assert not statements and parts == before


def test_mixed_cached_and_new_groups_only_walk_the_new_field(make_storage):
    engine, headers = make_storage(32)
    with QueryReads.snapshot(engine) as reads:
        cached = group(reads.connection, headers[0], [0])
        fresh = group(reads.connection, headers[1], [0])
        parts = reads._verified_close_storage_parts
        close_storage._prime_buckets_many(reads.connection, [cached], parts)
        descriptors(cached).visits = 0
        close_storage._prime_buckets_many(reads.connection, [cached, fresh], parts)
        assert descriptors(cached).visits == 0
        assert descriptors(fresh).visits == 32
        assert parts[2, headers[1].storage_digest, "bucket", "adopted_results", 0] == {
            0: {"id": "2-0"}
        }


@pytest.mark.parametrize("count", [32, 128, 256])
def test_new_bucket_lookup_does_not_revisit_unrelated_descriptors(make_storage, count):
    engine, headers = make_storage(count)
    with QueryReads.snapshot(engine) as reads:
        selected = group(reads.connection, headers[0], [0])
        parts = reads._verified_close_storage_parts
        close_storage._prime_buckets_many(reads.connection, [selected], parts)
        assert descriptors(selected).visits == count
        descriptors(selected).visits = 0
        close_storage._prime_buckets_many(
            reads.connection, [(*selected[:3], [1, count - 1, 300])], parts,
        )
        assert descriptors(selected).visits == 0
        prefix = (headers[0].period, headers[0].storage_digest, "bucket", "adopted_results")
        assert parts[*prefix, 1] == {1: {"id": "1-1"}}
        assert parts[*prefix, count - 1] == {count - 1: {"id": f"1-{count - 1}"}}
        assert parts[*prefix, 300] == {}


def test_reused_descriptor_index_preserves_first_duplicate(make_storage):
    engine, headers = make_storage(32, broken="duplicate_descriptor")
    with QueryReads.snapshot(engine) as reads:
        selected = group(reads.connection, headers[0], [1])
        parts = reads._verified_close_storage_parts
        close_storage._prime_buckets_many(reads.connection, [selected], parts)
        descriptors(selected).visits = 0
        close_storage._prime_buckets_many(reads.connection, [(*selected[:3], [0])], parts)
        assert descriptors(selected).visits == 0
        assert parts[1, headers[0].storage_digest, "bucket", "adopted_results", 0] == {
            0: {"id": "1-0"}
        }


@pytest.mark.parametrize("broken", ["digest", "positions"])
def test_late_group_failure_publishes_neither_buckets_nor_iteration(make_storage, broken):
    engine, headers = make_storage(32, broken=broken)
    with QueryReads.snapshot(engine) as reads:
        good = group(reads.connection, headers[0], [0])
        bad = group(reads.connection, headers[1], [31])
        parts = reads._verified_close_storage_parts
        for _ in range(2):
            with pytest.raises(KernelError):
                close_storage._prime_buckets_many(reads.connection, [good, bad], parts)
            assert parts == {}
        close_storage._prime_buckets_many(reads.connection, [good], parts)
        prior = dict(parts)
        with pytest.raises(KernelError):
            close_storage._prime_buckets_many(reads.connection, [good, bad], parts)
        assert parts == prior


def test_iteration_proof_does_not_hide_an_unread_bad_bucket(make_storage):
    engine, headers = make_storage(32, broken="digest")
    with QueryReads.snapshot(engine) as reads:
        selected = group(reads.connection, headers[1], [0])
        parts = reads._verified_close_storage_parts
        close_storage._prime_buckets_many(reads.connection, [selected], parts)
        before = dict(parts)
        later = (*selected[:3], [31])
        with pytest.raises(KernelError):
            close_storage._prime_buckets_many(reads.connection, [later], parts)
        assert parts == before


def test_first_negative_group_walks_the_complete_directory(make_storage):
    engine, headers = make_storage(32)
    with QueryReads.snapshot(engine) as reads:
        selected = group(reads.connection, headers[0], [])
        close_storage._prime_buckets_many(
            reads.connection, [selected], reads._verified_close_storage_parts
        )
        assert descriptors(selected).visits == 32
        descriptors(selected).visits = 0
        close_storage._prime_buckets_many(
            reads.connection, [selected], reads._verified_close_storage_parts
        )
        assert descriptors(selected).visits == 0


@pytest.mark.parametrize(
    "malformed,error", [("missing", KeyError), ("null", TypeError), ("empty_entry", IndexError)]
)
def test_first_negative_group_keeps_original_malformed_errors(make_storage, malformed, error):
    engine, headers = make_storage(1)
    with QueryReads.snapshot(engine) as reads:
        selected = group(reads.connection, headers[0], [])
        fields = selected[1]["directories"]
        if malformed == "missing":
            del fields["adopted_results"]
        else:
            fields["adopted_results"] = None if malformed == "null" else [[]]
        for _ in range(2):
            with pytest.raises(error):
                close_storage._prime_buckets_many(
                    reads.connection, [selected], reads._verified_close_storage_parts
                )
            assert reads._verified_close_storage_parts == {}


@pytest.mark.parametrize(
    "boundary",
    [
        "object",
        "root",
        "token",
        "connection",
        "unowned",
        "fixed_v1",
        "registry_v1",
    ],
)
@pytest.mark.parametrize("later_buckets", [[0], [1]])
def test_iteration_reuse_requires_the_exact_owned_object_root_and_token(
    make_storage,
    monkeypatch,
    boundary,
    later_buckets,
):
    engine, headers = make_storage(32)
    parts = {}
    with QueryReads.snapshot(engine) as reads:
        selected = group(reads.connection, headers[0], [0])
        close_storage._prime_buckets_many(reads.connection, [selected], parts)
        descriptors(selected).visits = 0
        selected = (*selected[:3], later_buckets)
        if boundary == "object":
            copied = CountedDescriptors(list(descriptors(selected)))
            selected[1]["directories"]["adopted_results"] = copied
            close_storage._prime_buckets_many(reads.connection, [(*selected[:3], [])], parts)
            assert copied.visits == 32
        elif boundary == "root":
            other = close_storage.CloseHeader(1, b"l" * 32, b"x" * 32, headers[0].root)
            close_storage._prime_buckets_many(
                reads.connection, [(other, *selected[1:3], [])], parts
            )
            assert descriptors(selected).visits == 32
        elif boundary == "fixed_v1":
            with historical_content(1):
                close_storage._prime_buckets_many(reads.connection, [selected], parts)
            assert descriptors(selected).visits == 32
        elif boundary == "registry_v1":
            monkeypatch.setattr(engine.store.registry, "content_version", 1)
            close_storage._prime_buckets_many(reads.connection, [selected], parts)
            assert descriptors(selected).visits == 32
        elif boundary == "connection":
            other_engine, _ = make_storage(32)
            with other_engine.store.connection(read_only=True) as other:
                close_storage._prime_buckets_many(other, [selected], parts)
            assert descriptors(selected).visits == 32
    if boundary == "token":
        with QueryReads.snapshot(engine) as reads:
            close_storage._prime_buckets_many(reads.connection, [selected], parts)
            assert descriptors(selected).visits == 32
    elif boundary == "unowned":
        with engine.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            close_storage._prime_buckets_many(connection, [selected], parts)
            assert descriptors(selected).visits == 32
