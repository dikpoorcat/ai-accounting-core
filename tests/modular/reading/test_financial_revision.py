"""Failure generations stay separate from unchanged financial content and tokens."""

from ai_accounting_modular.reading import _financial_projection as projection
from ai_accounting_modular.reading import _monthly
from ai_accounting_modular.reading._financial_store import CONTROL

from ._financial_projection_case import drain, page, scope
from ._financial_projection_case import financial_case as financial_case


def test_same_token_requeued_work_rejects_old_failure_and_accepts_current_failure(financial_case):
    case, _ = financial_case
    case.seed()
    page(case)
    with case.kernel.storage.read_unit(case.company) as read:
        target = projection.store(read)
        token, before = target.token(), target.state()
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        target = projection.store(unit)
        target.save_work(target.work(), unit.head, retry=True)
        current = target.state()
        assert target.token() == token
        assert current["revision"] == before["revision"] + 1
        assert current | {"revision": before["revision"]} == before
        assert not target.fail(token, "production_failed", expected_revision=before["revision"])
        assert target.state() == current
        assert target.fail(token, "production_failed", expected_revision=current["revision"])
        assert target.state() == current | {
            "state": "failed",
            "error_code": "production_failed",
            "revision": current["revision"] + 1,
        }


def test_produce_failure_uses_revision_from_its_original_read(financial_case, monkeypatch):
    case, _ = financial_case
    case.seed()
    page(case)
    with case.kernel.storage.read_unit(case.company) as read:
        target = projection.store(read)
        before, token = target.state(), target.token()
    replacement = []

    def requeued_then_failed(*args, **kwargs):
        with case.kernel.storage.maintenance_unit(case.company) as unit:
            target = projection.store(unit)
            assert target.fail(token, "production_failed", expected_revision=before["revision"])
        _monthly.retry(case.kernel.reading, scope(case), ("financial",))
        with case.kernel.storage.read_unit(case.company) as read:
            target = projection.store(read)
            assert target.token() == token
            replacement.append(target.state())
        raise OSError("the previous producer failed after an identical same-head retry")

    with monkeypatch.context() as patch:
        patch.setattr(case.kernel.ledger, "prepare_financial_projection", requeued_then_failed)
        assert not projection.produce(case.kernel.reading, case.company)
    with case.kernel.storage.read_unit(case.company) as read:
        target = projection.store(read)
        assert len(replacement) == 1 and target.state() == replacement[0]
        assert replacement[0]["revision"] > before["revision"]
        assert target.token() == token
    drain(case)
    ready = page(case)
    assert ready.state == "ready"
    assert ready.payload["financial"]["expense"]["amount_fen"] == "100000"


def test_damaged_queue_uses_its_own_post_save_work_revision(financial_case):
    case, _ = financial_case
    case.seed()
    page(case)
    drain(case)
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        target = projection.store(unit)
        before = target.state()
        target._session.execute(
            "UPDATE read_item SET payload_json='{}' WHERE scope_key=? "
            "AND collection_key=? AND item_id='work'",
            (target.scope_key, CONTROL),
        )
        selected = projection.request(case.kernel.reading, scope(case), "summary_values", unit)
        projection.queue(case.kernel.reading, selected, unit, repair=True)
        current = target.state()
        assert current["state"] == "failed" and current["error_code"] == "storage_corrupt"
        assert current["revision"] == before["revision"] + 2
        assert target.work()["rebuild"] and target.work()["pending"] == [selected.key]


def test_consumer_revision_advances_without_changing_public_content_version(financial_case):
    case, _ = financial_case
    case.seed()
    page(case)
    drain(case)
    before = page(case)
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        repository = unit.repository("reading")
        key = _monthly.key(scope(case))
        previous = repository.state(key, "financial")
        projection.store(unit).mark_consumer(key, unit.head)
        pending = repository.state(key, "financial")
        assert pending["state"] == "preparing"
        assert pending["revision"] == previous["revision"] + 1
    drain(case)
    assert page(case) == before
    with case.kernel.storage.read_unit(case.company) as read:
        ready = read.repository("reading").state(key, "financial")
        assert ready["revision"] == pending["revision"] + 1
