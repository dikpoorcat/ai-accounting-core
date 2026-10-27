"""Current model revisions distinguish lifecycle changes without changing content."""

import pytest

from ai_accounting_modular.reading import _brief_activity_index, _brief_open_index
from ai_accounting_modular.reading._collections import prepare_model
from ai_accounting_modular.reading._publication_guard import CaptureGuard

from ._support import assemble


@pytest.fixture
def book(tmp_path):
    return assemble(tmp_path, established=False)


def state(book, component="revision-case"):
    with book.storage.read_unit(book.company) as read:
        return read.repository("reading").state("synthetic-model", component)


def test_revision_cas_distinguishes_same_head_rebuild_and_noops(book):
    model = prepare_model({"synthetic": "unchanged"})
    with book.storage.maintenance_unit(book.company) as unit:
        repository = unit.repository("reading")
        repository.register("synthetic-model", "revision-case", 0, 1)
        repository.register("synthetic-model", "revision-case", 0, 1)
        assert repository.state("synthetic-model", "revision-case")["revision"] == 0
        repository.mark("synthetic-model", "revision-case", 1)
        repository.mark("synthetic-model", "revision-case", 1)
        assert repository.state("synthetic-model", "revision-case")["revision"] == 1
        assert repository.publish(
            "synthetic-model",
            "revision-case",
            1,
            1,
            model,
            guard=CaptureGuard.capture(repository.state("synthetic-model", "revision-case"), 1),
            current_head=1,
        )
    initial = state(book)
    assert initial["revision"] == 2 and initial["state"] == "ready"
    with book.storage.maintenance_unit(book.company) as unit:
        repository = unit.repository("reading")
        assert not repository.fail(
            "synthetic-model",
            "revision-case",
            1,
            "synthetic_failure",
            expected_state="ready",
            expected_revision=1,
        )
        assert repository.state("synthetic-model", "revision-case") == initial
        assert repository.fail(
            "synthetic-model",
            "revision-case",
            1,
            "synthetic_failure",
            expected_state="ready",
            expected_revision=2,
        )
        assert repository.state("synthetic-model", "revision-case")["revision"] == 3
        repository.retry("synthetic-model", "revision-case")
        repository.retry("synthetic-model", "revision-case")
        assert repository.state("synthetic-model", "revision-case")["revision"] == 4
        assert repository.publish(
            "synthetic-model",
            "revision-case",
            1,
            1,
            model,
            guard=CaptureGuard.capture(repository.state("synthetic-model", "revision-case"), 1),
            current_head=1,
        )
    rebuilt = state(book)
    assert rebuilt["revision"] == 5
    assert {key: value for key, value in rebuilt.items() if key != "revision"} == {
        key: value for key, value in initial.items() if key != "revision"
    }
    with book.storage.maintenance_unit(book.company) as unit:
        repository = unit.repository("reading")
        assert not repository.fail(
            "synthetic-model",
            "revision-case",
            1,
            "synthetic_failure",
            expected_state="ready",
            expected_revision=initial["revision"],
        )
    assert state(book) == rebuilt


@pytest.mark.parametrize("owner", [_brief_activity_index, _brief_open_index])
def test_failed_scope_advances_only_for_a_new_wanted_head(book, owner):
    with book.storage.maintenance_unit(book.company) as unit:
        repository = unit.repository("reading")
        repository.register("synthetic-model", owner.COMPONENT, 1, owner.FORMAT_VERSION)
        assert repository.fail(
            "synthetic-model", owner.COMPONENT, 1, "synthetic_failure", expected_revision=0
        )
        advance = (
            repository.activity_advance_failed
            if owner is _brief_activity_index
            else repository.open_advance_failed
        )
        advance("synthetic-model", 3)
        saved = repository.state("synthetic-model", owner.COMPONENT)
        assert saved["revision"] == 2 and saved["wanted_head"] == 3
        advance("synthetic-model", 3)
        advance("synthetic-model", 2)
        assert repository.state("synthetic-model", owner.COMPONENT) == saved
    assert state(book, owner.COMPONENT) == saved


def test_lifecycle_revision_changes_roll_back_with_their_state(book):
    with book.storage.maintenance_unit(book.company) as unit:
        unit.repository("reading").register("synthetic-model", "revision-case", 0, 1)
    before = state(book)
    with pytest.raises(RuntimeError, match="after real revision writes"):
        with book.storage.maintenance_unit(book.company) as unit:
            repository = unit.repository("reading")
            repository.mark("synthetic-model", "revision-case", 1)
            assert repository.fail(
                "synthetic-model", "revision-case", 1, "synthetic_failure", expected_revision=1
            )
            repository.retry("synthetic-model", "revision-case")
            assert repository.state("synthetic-model", "revision-case")["revision"] == 3
            raise RuntimeError("after real revision writes")
    assert state(book) == before
