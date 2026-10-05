"""Record hashes can be shared; current/frozen selection must still be checked."""

import pytest
from test_engine import engine as engine  # noqa: F401
from test_engine import publish, save

from ai_accounting.kernel import publication
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.query_reads import QueryReads, verify_current_voucher_publications


def records(engine):
    save(engine)
    save(engine, subject="second", request="second")
    publish(engine, ["charge", "second"])
    with engine.store.connection(read_only=True) as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM calculation_publication")]


def test_current_selection_and_record_reader_share_only_exact_success(engine, monkeypatch):
    selected = records(engine)
    verified = []
    original = publication.verify_record

    def counted(row):
        verified.append(row["id"])
        return original(row)

    monkeypatch.setattr(publication, "verify_record", counted)
    with QueryReads.snapshot(engine) as reads:
        ids = {row[0] for row in reads.connection.execute("SELECT version_id FROM voucher_current")}
        returned = verify_current_voucher_publications(reads.connection, ids)
        assert returned.keys() == {row["calculation_id"] for row in selected}
        assert all(
            dict(returned[row["calculation_id"]]) == {**row, "has_successor": 0}
            for row in selected
        )
        assert set(verified) == {row["id"] for row in selected}
        assert len(verified) == 2
        reads.verify_publication_records(selected)
        verify_current_voucher_publications(reads.connection, ids)
        assert len(verified) == 2
        assert len(reads._verified_publication_ids) == 2
    assert reads._verified_publication_ids == {}
    with QueryReads.snapshot(engine) as fresh:
        fresh.verify_publication_records(selected)
    assert len(verified) == 4


def test_record_batch_failure_does_not_publish_partial_success(engine):
    selected = records(engine)
    broken = {**selected[-1], "mode": "not-the-saved-mode"}
    with QueryReads.snapshot(engine) as reads:
        with pytest.raises(KernelError) as failure:
            reads.verify_publication_records([selected[0], broken])
        assert failure.value.code == "content_integrity_failed"
        assert reads._verified_publication_ids == {}
        reads.verify_publication_records(selected)
        with pytest.raises(KernelError):
            reads.verify_publication_records([{**selected[0], "posting_period": 0}])


def test_unmanaged_records_recheck_each_time(engine, monkeypatch):
    selected = records(engine)
    checked = []
    original = publication.verify_record

    def counted(row):
        checked.append(row["id"])
        return original(row)

    monkeypatch.setattr(publication, "verify_record", counted)
    with engine.store.connection(read_only=True) as connection:
        reads = QueryReads(engine, connection)
        reads.verify_publication_records(selected)
        reads.verify_publication_records(selected)
        assert reads._verified_publication_ids == {}
    assert len(checked) == 4
