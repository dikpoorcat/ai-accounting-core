"""The public AI call verifies source bytes before obtaining its business writer."""

from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from .conftest import initial_request


def file_request(app, company):
    request = initial_request(company)
    with app.storage.read_unit(company) as read:
        root = app.storage.company_metadata(read).company_root
    source_id, raw = str(uuid4()), b"actual approved source bytes"
    relative = f"originals/2026-01/{source_id}/original.bin"
    path = root / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    request["sources"] = [
        {
            "source_id": source_id,
            "kind": "file",
            "relative_path": relative,
            "original_name": "original.bin",
            "media_type": "application/octet-stream",
            "sha256": sha256(raw).hexdigest(),
            "byte_size": len(raw),
            "archive_month": "2026-01",
        }
    ]
    request["mutations"][0]["fact"]["evidence_ids"] = [source_id]
    return request, path


def test_wire_holds_verified_inputs_until_commit_and_never_reads_original_bytes_in_writer(
    assembled, monkeypatch
):
    app, company = assembled
    request, original = file_request(app, company)
    proof_active = writer_active = False
    prepare_sources, write_unit, path_open = (
        app.records.prepare_sources,
        app.storage.write_unit,
        Path.open,
    )

    @contextmanager
    def prepare(*args, **kwargs):
        nonlocal proof_active
        assert not writer_active
        with prepare_sources(*args, **kwargs) as proof:
            proof_active = True
            try:
                yield proof
            finally:
                proof_active = False

    @contextmanager
    def writer(*args, **kwargs):
        nonlocal writer_active
        assert proof_active
        writer_active = True
        try:
            with write_unit(*args, **kwargs) as unit:
                yield unit
        finally:
            writer_active = False

    def opening(path, *args, **kwargs):
        if path == original:
            assert not writer_active, "原件正文不能在业务写锁内读取"
        return path_open(path, *args, **kwargs)

    monkeypatch.setattr(app.records, "prepare_sources", prepare)
    monkeypatch.setattr(app.storage, "write_unit", writer)
    monkeypatch.setattr(Path, "open", opening)
    result = app.accounting_apply(request)
    assert result["completion"] == "complete", result
    assert not proof_active and not writer_active


def test_source_replaced_between_preparation_and_writer_rejects_the_whole_submission(
    assembled, monkeypatch
):
    app, company = assembled
    request, original = file_request(app, company)
    write_unit = app.storage.write_unit

    @contextmanager
    def replace_before_writer(*args, **kwargs):
        original.rename(original.with_suffix(".preserved"))
        original.write_bytes(b"different replacement bytes")
        with write_unit(*args, **kwargs) as unit:
            yield unit

    monkeypatch.setattr(app.storage, "write_unit", replace_before_writer)
    result = app.accounting_apply(request)
    assert result.get("completion") != "complete", result
    with app.storage.read_unit(company) as read:
        assert read.head == 0
        assert app.identity.get_batch([request["objects"][0]["object_id"]], read) == ()
        assert app.records.resolve_sources([request["sources"][0]["source_id"]], read) == ()
        assert app.ledger.latest_period(read) is None
