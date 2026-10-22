"""Storage clears snapshot-owned dictionaries on every transaction exit."""

import gc
import sqlite3
import weakref

import pytest

from ai_accounting_modular.contracts.public import ContractError


class SnapshotValue:
    def __init__(self, repository):
        self.repository = repository


@pytest.mark.parametrize(
    "mode,ending",
    [
        (mode, ending)
        for mode in ("read", "write", "maintenance", "preview")
        for ending in ("success", "body_failure", "check_failure")
        if mode != "read" or ending != "check_failure"
    ],
)
def test_snapshot_cache_releases_cycles_and_preserves_transaction_outcome(
    storage_case, mode, ending
):
    storage, company = storage_case
    saved, checked = {}, []
    result, failure = object(), RuntimeError("original transaction failure")
    external = {"nested": ["retained result"]}

    def execute():
        with getattr(storage, f"{mode}_unit")(company.company_id) as context:
            repository = context.repository("records" if mode == "maintenance" else "identity")
            session = repository.session
            cache = session.snapshot_cache()
            assert type(cache) is dict and session.snapshot_cache() is cache
            value = SnapshotValue(repository)
            repository.cache = cache
            cache.update(owner=value, data=external)
            saved.update(
                context=context,
                session=session,
                cache=cache,
                repository=weakref.ref(repository),
                value=weakref.ref(value),
            )
            if mode in {"write", "preview"}:
                repository.add(name="snapshot cache transaction")
            elif mode == "maintenance":
                repository.publish("c" * 64)
            del repository, value

            if mode != "read":

                def check():
                    assert session.snapshot_cache() is cache and cache["data"] is external
                    checked.append("checked")
                    if ending == "check_failure":
                        raise failure

                context.check_before_commit(check)
            if ending == "body_failure":
                raise failure
        return result

    enabled = gc.isenabled()
    gc.disable()
    try:
        if ending == "success":
            assert execute() is result
        else:
            with pytest.raises(RuntimeError) as error:
                execute()
            assert error.value is failure
        # Keep both the closed context and dictionary alive. Clearing this
        # exact dictionary must break the owner's cycle without cyclic GC.
        assert saved["cache"] == {}
        assert saved["repository"]() is None and saved["value"]() is None
        assert saved["context"]._snapshot_caches == []
        assert saved["context"]._repositories == {}
        assert external == {"nested": ["retained result"]}
        assert checked == ([] if mode == "read" or ending == "body_failure" else ["checked"])
        with pytest.raises(ContractError) as ended:
            saved["session"].snapshot_cache()
        assert ended.value.code == "invalid_operation"
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            saved["context"]._connection.execute("SELECT 1")
    finally:
        if enabled:
            gc.enable()

    with storage.read_unit(company.company_id) as read:
        assert read.head == int(mode == "write" and ending == "success")
        persisted = read.repository("identity").names()
        assert persisted == (
            ["snapshot cache transaction"] if mode == "write" and ending == "success" else []
        )
        document = read.repository("records").session.fetchone(
            "SELECT published_set_id FROM document_set WHERE singleton=1"
        )
        assert (None if document is None else document[0]) == (
            "c" * 64 if mode == "maintenance" and ending == "success" else None
        )


def test_snapshot_cache_is_lazy_unique_per_session_and_isolated_between_reads(storage_case):
    storage, company = storage_case
    with storage.read_unit(company.company_id) as read:
        first_session = read.repository("identity").session
        second_session = read.repository("records").session
        first = first_session.snapshot_cache()
        assert type(first) is dict and read._snapshot_caches == [first]
        for _ in range(3):
            assert first_session.snapshot_cache() is first
        assert len(read._snapshot_caches) == 1
        second = second_session.snapshot_cache()
        assert type(second) is dict and second is not first
        assert len(read._snapshot_caches) == 2
        assert read._snapshot_caches[0] is first and read._snapshot_caches[1] is second
        first["same-key"], second["same-key"] = "first owner", "second owner"
        assert first["same-key"] != second["same-key"]
    assert first == second == {} and read._snapshot_caches == []
    first["after-close"] = "independent dictionary mutation"
    with storage.read_unit(company.company_id) as following:
        following_session = following.repository("identity").session
        assert following._snapshot_caches == []
        current = following_session.snapshot_cache()
        assert type(current) is dict and current == {} and current is not first
    assert first == {"after-close": "independent dictionary mutation"}


def test_closed_session_cannot_create_its_first_snapshot_cache(storage_case):
    storage, company = storage_case
    with storage.read_unit(company.company_id) as read:
        session = read.repository("identity").session
    with pytest.raises(ContractError) as error:
        session.snapshot_cache()
    assert error.value.code == "invalid_operation"
    assert read._snapshot_caches == []
