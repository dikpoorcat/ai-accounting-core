"""Temporary financial-position views release sources without a GC collection."""

import json
import sqlite3
import weakref
from dataclasses import replace

import pytest
from monthly_close_fixture import ready
from test_reports import book as book_fixture

from ai_accounting.kernel import close_review, close_review_integrity_v1, content_v1
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.storage import Store

book = book_fixture


@pytest.mark.parametrize("historical", [False, True])
@pytest.mark.parametrize("fail_position", [False, True])
def test_owner_review_releases_position_after_success_or_failure(
    book, tmp_path, monkeypatch, historical, fail_position
):
    engine = book[0]
    with engine.store.connection(read_only=True) as connection:
        proof = connection.execute("SELECT hex(digest) FROM evidence LIMIT 1").fetchone()[0].lower()
    ready(engine, proof, first="2026-01", last="2026-01")
    preview = Periods(engine).preview_close("2026-01", owner_confirmation=proof)
    manifest = preview["manifest"]
    expected = manifest["owner_review"]
    if historical:
        from ai_accounting.kernel import position_v1
        from ai_accounting.kernel.close_storage import decode_close

        book[3]("2026-01")
        with engine.store.connection(read_only=True) as connection:
            row = connection.execute("SELECT * FROM period_close ORDER BY period LIMIT 1").fetchone()
            manifest = decode_close(connection, row)
            expected = manifest["owner_review"]

        directory = tmp_path / "schema_contracts"
        directory.mkdir()
        (directory / "content-v1.json").write_text(
            json.dumps(content_v1.content_contract(engine.store.registry), ensure_ascii=False),
            encoding="utf-8",
        )
        monkeypatch.setattr(content_v1, "__file__", str(tmp_path / "content_v1.py"))
        content_v1.v1_registry.cache_clear()
        registry = content_v1.v1_registry()
        selected_engine = Engine(Store(
            engine.store.path, replace(engine.store.bundle, registry=registry),
            engine.store.company_id, engine.store.database_id,
        ))
        module, factory_name, position_name = position_v1, "PositionInputsV1", "position_v1"
        builder = close_review_integrity_v1.build_owner_review
    else:
        from ai_accounting.kernel import dashboard

        selected_engine = engine
        module, factory_name, position_name = dashboard, "_Snapshot", "_position"
        builder = close_review.build_owner_review
    original_factory, original_position = getattr(module, factory_name), getattr(module, position_name)
    observed = []

    def capture(*args, **kwargs):
        view = original_factory(*args, **kwargs)
        observed.append(weakref.ref(view))
        return view

    def position(view):
        assert view.engine is selected_engine and view.connection.in_transaction
        result = original_position(view)
        assert view.engine is selected_engine, "active position work must retain its sources"
        if fail_position:
            raise RuntimeError("synthetic position failure")
        return result

    monkeypatch.setattr(module, factory_name, capture)
    monkeypatch.setattr(module, position_name, position)
    try:
        with engine.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            if historical:
                scope = historical_content(1)
            else:
                from contextlib import nullcontext
                scope = nullcontext()
            with scope:
                if fail_position:
                    with pytest.raises(RuntimeError, match="synthetic position failure") as failure:
                        builder(connection, selected_engine, manifest,
                                _frozen_followups=expected["followup_summary"])
                    # The exception traceback may legitimately retain the view object;
                    # its business graph must already be gone before that traceback dies.
                    assert observed[0]() is not None and observed[0]().__dict__ == {}
                    del failure
                else:
                    result = builder(connection, selected_engine, manifest,
                                     _frozen_followups=expected["followup_summary"])
                    assert result == expected, "release cannot alter current or frozen review values"
            assert len(observed) == 1
            assert observed[0]() is None, "view cycle must not await a later cyclic GC"
            assert connection.in_transaction, "release must not end the caller's transaction"
            assert connection.execute("SELECT 1").fetchone()[0] == 1
    finally:
        if historical:
            content_v1.v1_registry.cache_clear()


@pytest.mark.parametrize("historical", [False, True])
def test_position_constructor_source_failure_does_not_retain_a_read_graph(
    book, monkeypatch, historical
):
    from ai_accounting.kernel.dashboard import _Snapshot
    from ai_accounting.kernel.position_v1 import PositionInputsV1

    engine = book[0]
    observed, helpers = [], []
    base = PositionInputsV1 if historical else _Snapshot

    class ObservedView(base):
        def __new__(cls, *args, **kwargs):
            view = object.__new__(cls)
            observed.append(weakref.ref(view))
            return view

    if not historical:
        from ai_accounting.kernel import dashboard
        original = dashboard.ClosedPeriods

        def closed_periods(snapshot):
            helper = original(snapshot)
            helpers.append(weakref.ref(helper))
            return helper

        monkeypatch.setattr(dashboard, "ClosedPeriods", closed_periods)

    # Deny an actual SQLite source read rather than replacing an initializer
    # or cleanup method. Current construction has already built cyclic helpers;
    # fixed-v1 has read caches but has not yet installed its reverse journal.
    denied_table = "period_close" if historical else "monthly_account"

    def deny_source(action, table, *_):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table == denied_table else sqlite3.SQLITE_OK

    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        connection.set_authorizer(deny_source)
        with pytest.raises(sqlite3.DatabaseError) as failure:
            if historical:
                ObservedView(engine, connection, {
                    "period": "2026-01", "trial_balance": [], "vouchers": [],
                })
            else:
                ObservedView(engine, connection, "2026-01")
        assert denied_table in str(failure.value), "the original source failure must propagate"
        assert len(observed) == 1
        if not historical:
            assert len(helpers) == 1, "a reverse helper exists before the denied source read"
            assert observed[0]().__dict__ == {}, "even the exception traceback must not retain business sources"
            assert helpers[0]() is None, "the helper cycle is broken before traceback disposal"
        del failure
        assert observed[0]() is None, "failed construction must not wait for cyclic GC"
        connection.set_authorizer(None)
        assert connection.in_transaction
        assert connection.execute("SELECT 1").fetchone()[0] == 1
