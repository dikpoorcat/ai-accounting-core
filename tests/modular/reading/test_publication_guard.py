"""Successful production is bound to a lifecycle and newly discovered inputs."""

from dataclasses import replace

import pytest

from ai_accounting_modular.contracts.public import ReadInputs
from ai_accounting_modular.reading import _monthly
from ai_accounting_modular.reading._collections import prepare_model
from ai_accounting_modular.reading._publication_guard import CaptureGuard

from ._support import assemble, expense


def rows(repository):
    return tuple(
        tuple(tuple(row) for row in repository._session.fetchall(sql))
        for sql in (
            "SELECT * FROM read_scope_head ORDER BY scope_key,component_key",
            "SELECT * FROM read_component ORDER BY scope_key,component_key",
            "SELECT * FROM read_item ORDER BY scope_key,collection_key,item_id",
        )
    )


def test_old_success_cannot_publish_after_same_head_failure_retry_or_success(tmp_path):
    book = assemble(tmp_path, established=False)
    with book.storage.maintenance_unit(book.company) as unit:
        repo = unit.repository("reading")
        repo.register("synthetic-model", "guarded", unit.head, 1)
        guard = CaptureGuard.capture(repo.state("synthetic-model", "guarded"), unit.head)
        assert repo.fail(
            "synthetic-model",
            "guarded",
            unit.head,
            "production_failed",
            expected_revision=guard.revision,
        )
        repo.retry("synthetic-model", "guarded")
        before = rows(repo)
        model = prepare_model({"amount_fen": "100"})
        assert not repo.publish(
            "synthetic-model", "guarded", unit.head, 1, model, guard=guard, current_head=unit.head
        )
        assert rows(repo) == before
        fresh = CaptureGuard.capture(repo.state("synthetic-model", "guarded"), unit.head)
        assert repo.publish(
            "synthetic-model", "guarded", unit.head, 1, model, guard=fresh, current_head=unit.head
        )
        before = rows(repo)
        assert not repo.publish(
            "synthetic-model", "guarded", unit.head, 1, model, guard=fresh, current_head=unit.head
        )
        assert rows(repo) == before


@pytest.mark.parametrize("new_dependency", [False, True])
def test_only_expanded_subscriptions_need_an_unchanged_company_snapshot(tmp_path, new_dependency):
    book = assemble(tmp_path)
    scope = _monthly.normalize(
        {
            "company_id": book.company,
            "period_from": "2026-01",
            "period_to": "2026-01",
            "view": "as_posted",
        }
    )
    key = _monthly.key(scope)
    original = replace(
        prepare_model({"amount_fen": "100"}), inputs=ReadInputs(note_scopes=("business:old",))
    )
    with book.storage.maintenance_unit(book.company) as unit:
        repo = unit.repository("reading")
        repo.register(key, "funds", unit.head, 1)
        guard = CaptureGuard.capture(repo.state(key, "funds"), unit.head)
        assert repo.publish(
            key, "funds", unit.head, 1, original, guard=guard, current_head=unit.head
        )
    # A real unrelated business commit makes the old mature target pending.
    book.submit(expense())
    with book.storage.maintenance_unit(book.company) as unit:
        repo = unit.repository("reading")
        repo.mark(key, "funds", unit.head)
    with book.storage.read_unit(book.company) as read:
        guard = CaptureGuard.capture(read.repository("reading").state(key, "funds"), read.head)
        model = (
            replace(original, inputs=ReadInputs(note_scopes=("business:old", "business:new")))
            if new_dependency
            else original
        )
    # Unrelated identity data advances the company head without matching these inputs.
    book.submit(expense(name="second"))
    with book.storage.maintenance_unit(book.company) as unit:
        repo = unit.repository("reading")
        before = rows(repo)
        assert repo.publish(
            key, "funds", guard.wanted_head, 1, model, guard=guard, current_head=unit.head
        ) is (not new_dependency)
        if new_dependency:
            assert rows(repo) == before
            assert repo.state(key, "funds")["state"] == "preparing"


def test_changed_pending_work_advances_the_lifecycle_at_the_same_wanted_head(tmp_path):
    book = assemble(tmp_path, established=False)
    with book.storage.maintenance_unit(book.company) as unit:
        repo = unit.repository("reading")
        for component, mark in (
            ("brief_activity", repo.activity_mark),
            ("brief_open_items", repo.open_mark),
        ):
            repo.register("synthetic-model", component, unit.head, 1)
            before = repo.state("synthetic-model", component)
            mark("synthetic-model", unit.head, full=True)
            assert repo.state("synthetic-model", component)["revision"] > before["revision"]
