"""Synthetic construction checkpoints survive a short Windows publish lock."""

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import stage9_checkpoint
from stage9_book import MixedBook
from stage9_independent_book import IndependentBook


class _State:
    def fetchone(self):
        return (1, 2)


class _Connection:
    def execute(self, statement):
        assert statement == "SELECT * FROM state"
        return _State()


class _Store:
    @contextmanager
    def connection(self, *, read_only):
        assert read_only is True
        yield _Connection()


@pytest.fixture(params=[MixedBook, IndependentBook], ids=["mixed", "independent"])
def book(request, tmp_path):
    fixture = request.param.__new__(request.param)
    fixture.root = tmp_path
    fixture.engine = SimpleNamespace(store=_Store())
    for field in MixedBook._CHECKPOINT_FIELDS:
        setattr(fixture, field, None)
    fixture.business_subjects = []
    fixture.deferred_close_checks = {}
    fixture.objects_count = 2
    fixture.suppliers = []
    fixture.sequence = 1
    return fixture


def _checkpoint_paths(book):
    name = getattr(book, "CHECKPOINT", "stage9-builder.json")
    return book.root / name, book.root / f"{name}.new"


def test_checkpoint_retries_one_permission_conflict_and_publishes(book, monkeypatch):
    book.checkpoint()
    target, temporary = _checkpoint_paths(book)
    original = target.read_bytes()
    book.sequence = 2

    replace = stage9_checkpoint.os.replace
    attempts = []
    sleeps = []

    def briefly_locked(source, destination):
        attempts.append((source, destination))
        if len(attempts) == 1:
            assert target.read_bytes() == original
            assert temporary.is_file()
            raise PermissionError(13, "sharing conflict")
        replace(source, destination)

    monkeypatch.setattr(stage9_checkpoint.os, "replace", briefly_locked)
    monkeypatch.setattr(stage9_checkpoint.time, "sleep", sleeps.append)
    book.checkpoint()

    assert len(attempts) == 2
    assert sleeps == [0.1]
    assert target.read_bytes() != original
    assert not temporary.exists()
    assert json.loads(target.read_text(encoding="utf-8"))["sequence"] == 2


def test_checkpoint_permanent_permission_failure_preserves_both_files(book, monkeypatch):
    book.checkpoint()
    target, temporary = _checkpoint_paths(book)
    original = target.read_bytes()
    book.sequence = 2
    attempts = []

    def always_locked(source, destination):
        attempts.append((source, destination))
        raise PermissionError(13, "sharing conflict")

    monkeypatch.setattr(stage9_checkpoint.os, "replace", always_locked)
    monkeypatch.setattr(stage9_checkpoint.time, "sleep", lambda _: None)
    with pytest.raises(PermissionError, match="sharing conflict"):
        book.checkpoint()

    assert len(attempts) == 20
    assert target.read_bytes() == original
    assert json.loads(temporary.read_text(encoding="utf-8"))["sequence"] == 2
