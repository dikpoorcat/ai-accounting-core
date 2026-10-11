"""Exact physical headers are shared; independent consumer proofs remain."""

import sqlite3

import pytest
from test_banking import book as book
from test_banking import close_month, inventories
from test_dashboard_open_groups import expenses, snapshot
from test_integrity_content import damage

from ai_accounting.kernel import dashboard_open_groups as groups
from ai_accounting.kernel import query_reads as query_module
from ai_accounting.kernel import settlement_freeze as freeze
from ai_accounting.kernel import settlement_projection as projection
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.query_reads import QueryReads


def test_frozen_sources_and_locators_load_headers_once_keep_both_checks_and_fact_proof(
    book, monkeypatch, record_property,
):
    engine, _, _, proof = book
    expenses(book, 40)
    assert close_month(inventories(engine, proof, "2026-09", {"transactions"}), proof, "2026-09")[
        "status"
    ] == "closed"
    work = {"loaded_header_rows": 0, "header_text_bytes": 0, "fact_proof_ids": []}
    original_read = query_module._read_exact_source_header_rows
    original_fact_proof = QueryReads._verify_selected_fact_bodies

    def loaded(*a, **k):
        rows = original_read(*a, **k)
        work["loaded_header_rows"] += len(rows)
        work["header_text_bytes"] += sum(
            len(value.encode("utf-8"))
            for row in rows.values() for value in row if isinstance(value, str)
        )
        return rows

    def facts(self, headers):
        headers = tuple(headers)
        work["fact_proof_ids"].append({row["fact_id"] for row in headers})
        return original_fact_proof(self, headers)

    monkeypatch.setattr(query_module, "_read_exact_source_header_rows", loaded)
    monkeypatch.setattr(QueryReads, "_verify_selected_fact_bodies", facts)
    with snapshot(engine, "2026-09") as snap:
        source = projection.settlement_dashboard_open_sources(
            snap.connection, snap.period, reads=snap.reads,
        )
        assert work["loaded_header_rows"] == 40
        assert len(snap.reads._exact_source_headers) == 40
        headers, scalars = groups._locators(snap, source["obligations"])
        assert work["loaded_header_rows"] == 40
        assert len(headers) == len(scalars) == 40
        assert work["fact_proof_ids"][-1] == {
            item["source_fact_id"] for item in source["obligations"]
        }
        # Reusing a physical row must not accept a different freeze commitment.
        state = next(iter(snap.reads._frozen_settlement_scalars.values()))._asdict()
        state["source_digest"] = "0" * 64
        with pytest.raises(KernelError, match="冻结清偿依据不匹配"):
            freeze._verified_page_sources(
                snap.connection, snap.month, [state], reads=snap.reads, include_headers=True,
            )
        # The locator validator independently binds the displayed formal source.
        changed = source["obligations"][0] | {
            "source_business": {"kind": "expense", "subject_id": "other-source"},
        }
        with pytest.raises(KernelError, match="精确业务来源不匹配"):
            groups._source_headers(snap, [changed])
        assert work["loaded_header_rows"] == 40
        with pytest.raises(TypeError):
            headers[next(iter(headers))]["subject_id"] = "caller-change"
        actual = groups._source_headers(snap, source["obligations"])
        assert all(item["subject_id"] != "caller-change" for item in actual.values())
        assert all(actual[ident] is headers[ident] for ident in headers)
        record_property("actual_header_rows", work["loaded_header_rows"])
        record_property("actual_header_text_bytes", work["header_text_bytes"])
    with QueryReads.snapshot(engine) as reads:
        assert reads._exact_source_headers == {}


def test_failed_freeze_source_batch_does_not_publish_any_header(book):
    engine, *_ = book
    expenses(book, 3)
    with snapshot(engine, "2026-09") as snap:
        states = [
            {"source_calculation_id": row["id"], "source_digest": row["digest"].hex(),
             "source_kind": row["kind"], "source_fact_id": row["fact_id"]}
            for row in snap.connection.execute(
                "SELECT id,digest,kind,fact_id FROM calculation WHERE kind='expense'",
            )
        ]
        changed = [*states[:-1], states[-1] | {"source_digest": "0" * 64}]
        with pytest.raises(KernelError):
            freeze._verified_page_sources(
                snap.connection, snap.month, changed, reads=snap.reads, include_headers=True,
            )
        assert snap.reads._exact_source_headers == {}
        freeze._verified_page_sources(
            snap.connection, snap.month, states, reads=snap.reads, include_headers=True,
        )
        assert len(snap.reads._exact_source_headers) == 3


def test_failed_physical_fact_proof_does_not_publish_locator_headers(book):
    engine, *_ = book
    expenses(book, 3)
    with snapshot(engine, "2026-09") as snap:
        sources = projection.settlement_dashboard_open_sources(
            snap.connection, snap.period, reads=snap.reads,
        )["obligations"]
    damage(engine, "fact_expense", "UPDATE fact_expense SET amount_fen=amount_fen+1")
    with snapshot(engine, "2026-09") as snap:
        with pytest.raises(KernelError, match="事实内容封签不一致"):
            groups._source_headers(snap, sources)
        assert snap.reads._exact_source_headers == {}


def test_fixed_and_unowned_source_headers_read_afresh_without_borrowing_current_success(
    book, monkeypatch,
):
    engine, *_ = book
    expenses(book, 2)
    loaded_rows = []
    original_read = query_module._read_exact_source_headers

    def loaded(*a, **k):
        rows = original_read(*a, **k)
        loaded_rows.append(len(rows))
        return rows

    monkeypatch.setattr(query_module, "_read_exact_source_headers", loaded)
    original_rows = query_module._read_exact_source_header_rows

    def native_loaded(*a, **k):
        rows = original_rows(*a, **k)
        loaded_rows.append(len(rows))
        return rows

    monkeypatch.setattr(query_module, "_read_exact_source_header_rows", native_loaded)
    with snapshot(engine, "2026-09") as snap:
        identifiers = {row[0] for row in snap.connection.execute(
            "SELECT id FROM calculation WHERE kind='expense'",
        )}
        with query_module._exact_source_header_batch(
            snap.connection, identifiers, reads=snap.reads,
        ) as headers:
            assert headers.keys() == identifiers
        assert len(snap.reads._exact_source_headers) == 2
        with historical_content(1):
            for _ in range(2):
                with query_module._exact_source_header_batch(
                    snap.connection, identifiers, reads=snap.reads,
                ) as headers:
                    assert headers.keys() == identifiers
        # Returning to the current reader can use its own earlier physical read.
        with query_module._exact_source_header_batch(
            snap.connection, identifiers, reads=snap.reads,
        ) as headers:
            assert headers.keys() == identifiers
        assert loaded_rows == [2, 2, 2]
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        reads = QueryReads(engine, connection)
        for _ in range(2):
            with query_module._exact_source_header_batch(
                connection, identifiers, reads=reads,
            ) as headers:
                assert headers.keys() == identifiers
        assert reads._exact_source_headers == {}
        connection.rollback()
    assert loaded_rows == [2, 2, 2, 2, 2]


@pytest.mark.parametrize("count", [3, 40])
def test_owned_batches_reuse_all_twelve_physical_fields_without_header_copies(
    book, count, monkeypatch, record_property,
):
    engine, *_ = book
    expenses(book, count)
    physical = []
    original = query_module._read_exact_source_header_rows

    def loaded(connection, identifiers):
        result = original(connection, identifiers)
        physical.extend(result.values())
        return result

    monkeypatch.setattr(query_module, "_read_exact_source_header_rows", loaded)
    with snapshot(engine, "2026-09") as snap:
        identifiers = {row[0] for row in snap.connection.execute(
            "SELECT id FROM calculation WHERE kind='expense'",
        )}
        public = query_module._read_exact_source_headers(snap.connection, identifiers)
        assert len(identifiers) == count
        consumed = 0
        for _ in range(4):
            with query_module._exact_source_header_batch(
                snap.connection, identifiers, reads=snap.reads,
            ) as headers:
                assert headers.keys() == identifiers
                for ident, row in headers.items():
                    assert type(row) is sqlite3.Row
                    assert len(row.keys()) == 12
                    assert dict(row) == public[ident]
                    assert any(row is actual for actual in physical)
                    consumed += 1
        assert len(physical) == count
        assert consumed == 4 * count
        assert all(snap.reads._exact_source_headers[row["id"]] is row for row in physical)
        # Public output remains an independent mutable dictionary boundary.
        next(iter(public.values()))["subject_id"] = "public-change"
        assert all(row["subject_id"] != "public-change" for row in physical)
        record_property("physical_header_rows", len(physical))
        record_property("independent_validator_header_visits", consumed)
        record_property("native_header_dictionary_materializations", 0)


@pytest.mark.parametrize("changed_field", ["source_digest", "source_kind", "source_fact_id"])
def test_duplicate_source_binding_keeps_each_distinct_judgement_and_converts_digest_once(
    book, monkeypatch, changed_field, record_property,
):
    engine, *_ = book
    expenses(book, 3)
    with snapshot(engine, "2026-09") as snap:
        states = [
            {"source_calculation_id": row["id"], "source_digest": row["digest"].hex(),
             "source_kind": row["kind"], "source_fact_id": row["fact_id"]}
            for row in snap.connection.execute(
                "SELECT id,digest,kind,fact_id FROM calculation WHERE kind='expense'",
            )
        ]
        digests = []

        def converted(value):
            digests.append(value)
            return bytes(value)

        monkeypatch.setattr(freeze, "bytes", converted, raising=False)
        freeze._verified_page_sources(
            snap.connection, snap.month, states * 4, reads=snap.reads, include_headers=True,
        )
        assert len(digests) == len(states) == 3
        assert len(snap.reads._exact_source_headers) == 3
        bad = states[-1] | {changed_field: "0" * 64 if changed_field == "source_digest" else "bad"}
        digests.clear()
        with pytest.raises(KernelError, match="冻结清偿依据不匹配"):
            freeze._verified_page_sources(
                snap.connection, snap.month, [*states * 4, bad],
                reads=snap.reads, include_headers=True,
            )
        assert len(digests) == 3
        record_property("actual_adopted_source_headers", len(snap.reads._exact_source_headers))
        record_property("source_digest_conversions", len(digests))
