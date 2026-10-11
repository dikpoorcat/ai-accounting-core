"""Byte anchors become JSON1 proof only after exact syntax validation."""

import hashlib
import json
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from test_integrity_content import damage
from test_published_source_bindings import _replace_bound_content
from test_published_source_bindings import sources as sources  # noqa: F401
from test_reports import book as book  # noqa: F401

from ai_accounting.kernel import query_reads, stored_json
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.report_open_contribution import (
    read_open_contributions,
    verify_published_source_bindings,
)


def forge_bound_raw(engine, row, raw):
    checksum = hashlib.sha256(raw.encode()).digest()
    original_connection = engine.store.connection

    @contextmanager
    def synthetic_corruption_connection(*args, **kwargs):
        with original_connection(*args, **kwargs) as connection:
            # Only this synthetic damage write bypasses the table JSON CHECK;
            # schema/trigger restoration remains the normal damage helper's job.
            connection.execute('PRAGMA ignore_check_constraints=ON')
            try:
                yield connection
            finally:
                connection.execute('PRAGMA ignore_check_constraints=OFF')

    with patch.object(engine.store, 'connection', synthetic_corruption_connection):
        damage(engine, 'calculation', 'UPDATE calculation SET outcome=?,digest=? WHERE id=?',
               (raw, checksum, row['id']))
    body = json.loads(row['content'])
    body['result_digest'] = checksum.hex()
    for binding in body['source_bindings']:
        if binding[0] == row['id']:
            binding[1] = checksum.hex()
    _replace_bound_content(engine, row, body)


@pytest.mark.parametrize('raw', [
    '{"values":{"x":1,"\\u0078":2}}',
    '{"values":{"x":1,"x":2}}',
    # SQLite's expression indexes can parse JSON5, but the original Python
    # JSON contract rejects this unquoted member even with a matching SHA.
    '{values:{}}',
])
def test_matching_anchor_sha_does_not_prove_sql_syntax(sources, raw):
    engine, rows = sources
    row = rows['cost']
    forge_bound_raw(engine, row, raw)
    with QueryReads.snapshot(engine) as reads:
        assert not verify_published_source_bindings(reads, {row['id']})
        assert row['id'] in reads._anchored_source_bytes
        assert reads._verified_sql_outcomes == set()
        assert reads._verified_source_contents == {}
        with pytest.raises(KernelError, match='JSON'):
            reads.verify_sql_outcomes({row['id']})
        assert reads._verified_sql_outcomes == set()
    with QueryReads.snapshot(engine) as reads:
        with pytest.raises(KernelError, match='JSON'):
            verify_published_source_bindings(reads, {row['id']}, _sql_outcomes=True)
        assert reads._anchored_source_bytes == {}
        assert reads._verified_sql_outcomes == set()
        assert reads._raw_calculation_outcomes == {}


@pytest.mark.parametrize('first', ['sql', 'bytes', 'full_content'])
def test_success_reuses_exact_raw_without_second_body_read(sources, monkeypatch, first):
    engine, rows = sources
    ident = rows['cost']['id']
    original = stored_json.verify_outcome_bytes
    syntax_ids = []

    def syntax(raw, expected, selected, **kwargs):
        syntax_ids.append(selected)
        return original(raw, expected, selected, **kwargs)

    monkeypatch.setattr(stored_json, 'verify_outcome_bytes', syntax)
    with QueryReads.snapshot(engine) as reads:
        statements = []
        reads.connection.set_trace_callback(statements.append)
        if first == 'bytes':
            verify_published_source_bindings(reads, {ident})
            raw = reads._raw_calculation_outcomes[ident]
            assert reads._verified_sql_outcomes == set()
        elif first == 'full_content':
            reads.verify_selected_content({ident})
        assert not verify_published_source_bindings(reads, {ident}, _sql_outcomes=True)
        first_sql = list(statements)
        reads.verify_sql_outcomes({ident})
        verify_published_source_bindings(reads, {ident}, _sql_outcomes=True)
        assert statements == first_sql
        assert not any('SELECT c.id,c.outcome,c.digest' in sql for sql in statements)
        if first == 'full_content':
            assert syntax_ids == []
        else:
            assert syntax_ids == [ident]
            assert reads._verified_source_contents == {}
        if first == 'bytes':
            assert reads._raw_calculation_outcomes[ident] is raw
    assert reads._raw_calculation_outcomes == {}
    assert reads._verified_sql_outcomes == set()


def test_late_cached_full_byte_failure_does_not_publish_new_sql_success(sources):
    engine, rows = sources
    old = rows['cost']
    new = rows['capital']
    forge_bound_raw(engine, old, '{"values":{"x":1,"\\u0078":2}}')
    with QueryReads.snapshot(engine) as reads:
        assert old['id'] in read_open_contributions(
            engine, reads.connection, {old['id']}, reads=reads)
        assert old['id'] in reads._anchored_source_bytes
        assert reads._verified_sql_outcomes == set()
        with pytest.raises(KernelError, match='JSON'):
            verify_published_source_bindings(
                reads, {old['id'], new['id']}, _sql_outcomes=True)
        assert reads._verified_sql_outcomes == set()
        assert new['id'] not in reads._anchored_source_bytes
        assert new['id'] not in reads._raw_calculation_outcomes
        assert not reads._report_snapshot_cache.get('report_published_own_sources')


@pytest.mark.parametrize('entry', ['anchor', 'cached_upgrade'])
def test_changed_token_never_publishes_sql_success(sources, monkeypatch, entry):
    engine, rows = sources
    ident = rows['cost']['id']
    original = stored_json.verify_outcome_bytes
    with QueryReads.snapshot(engine) as reads:
        if entry == 'cached_upgrade':
            verify_published_source_bindings(reads, {ident})

        def changed(raw, expected, selected, **kwargs):
            result = original(raw, expected, selected, **kwargs)
            reads._snapshot_token = object()
            return result

        monkeypatch.setattr(stored_json, 'verify_outcome_bytes', changed)
        verify_published_source_bindings(reads, {ident}, _sql_outcomes=True)
        assert reads._verified_sql_outcomes == set()
        assert reads._verified_source_contents == {}


@pytest.mark.parametrize('scope', ['fixed', 'unowned'])
def test_fixed_or_unowned_does_not_reuse_or_publish_current_sql_cache(sources, scope):
    engine, rows = sources
    ident = rows['cost']['id']
    with QueryReads.snapshot(engine) as reads:
        verify_published_source_bindings(reads, {ident})
        statements = []
        reads.connection.set_trace_callback(statements.append)
        context = (historical_content(1) if scope == 'fixed' else
                   patch.object(query_reads, '_owns_current_selector_snapshot', return_value=False))
        with context:
            reads.verify_sql_outcomes({ident})
            reads.verify_sql_outcomes({ident})
        assert sum('SELECT c.id,c.outcome,c.digest' in sql for sql in statements) == 2
        assert reads._verified_sql_outcomes == set()


def test_missing_derived_body_keeps_complete_fallback(sources):
    engine, rows = sources
    row = rows['cost']
    damage(engine, 'report_open_contribution',
           'DELETE FROM report_open_contribution WHERE publication_id=?',
           (row['publication_id'],))
    with QueryReads.snapshot(engine) as reads:
        fallback = verify_published_source_bindings(reads, {row['id']}, _sql_outcomes=True)
        assert fallback == {row['id']}
        assert reads._verified_sql_outcomes == set()
        reads.verify_saved_input_identity(fallback)
        reads.verify_sql_outcomes(fallback)


def test_cached_byte_upgrades_publish_nothing_when_last_syntax_fails(sources, monkeypatch):
    engine, rows = sources
    selected = {rows['cost']['id'], rows['capital']['id']}
    original = stored_json.verify_outcome_bytes
    with QueryReads.snapshot(engine) as reads:
        verify_published_source_bindings(reads, selected)
        visits = []

        def fail_last(raw, expected, ident, **kwargs):
            visits.append(ident)
            if len(visits) == 2:
                raise KernelError('content_integrity_failed', 'late syntax failure')
            return original(raw, expected, ident, **kwargs)

        monkeypatch.setattr(stored_json, 'verify_outcome_bytes', fail_last)
        with pytest.raises(KernelError, match='late syntax'):
            verify_published_source_bindings(reads, selected, _sql_outcomes=True)
        assert len(visits) == 2
        assert reads._verified_sql_outcomes == set()


def test_byte_to_sql_upgrade_does_not_iterate_unrelated_raw_or_content_cache(sources):
    engine, rows = sources
    ident = rows['cost']['id']

    class ExactMembership(dict):
        def keys(self):
            raise AssertionError('SQL upgrade scanned unrelated cached IDs')

        def __iter__(self):
            raise AssertionError('SQL upgrade scanned unrelated cached IDs')

    with QueryReads.snapshot(engine) as reads:
        verify_published_source_bindings(reads, {ident})
        reads._raw_calculation_outcomes = ExactMembership(reads._raw_calculation_outcomes)
        reads._verified_source_contents = ExactMembership()
        reads.verify_sql_outcomes({ident})
        assert reads._verified_sql_outcomes == {ident}
        assert reads._verified_source_contents == {}
