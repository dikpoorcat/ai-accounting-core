"""Checkpoint copies preserve exact business rows and never inherit qualification."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from stage9_book import MixedBook
from stage9_independent_book import IndependentBook

from scripts import copy_stage9_checkpoint as copy
from scripts.snapshot_stage9_source import snapshot_source


@pytest.fixture(scope="module")
def fixed_source(tmp_path_factory):
    workspace = tmp_path_factory.mktemp("stage9-checkpoint-workspace")
    (workspace / ".tmp").mkdir()
    source = workspace / ".tmp/stage9-fixed-source"
    snapshot_source(Path(__file__).resolve().parents[2], source, workspace=workspace)
    return workspace, source


@pytest.fixture
def small_book(fixed_source, request, monkeypatch):
    workspace, source = fixed_source
    monkeypatch.setenv("STAGE9_WORKSPACE_ROOT", str(workspace))
    root = workspace / ".tmp" / ("stage9-book-" + request.node.name)
    book = IndependentBook(root, objects=2, businesses=4)
    with book.defer_historical_verification_for_construction():
        book.add_month(0, close=True)
        book.add_month(1, close=False)
    report = {
        **book.describe(requested_months=2),
        "source": str(source),
        "status": "built_not_verified",
    }
    report_path = workspace / ".tmp" / ("stage9-report-" + request.node.name + ".json")
    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return workspace, source, book, report_path


def _run(workspace, source, root, target, report_path, *, injection=""):
    driver = (
        """
import json, sys
from pathlib import Path
sys.path[:0] = [str(Path(sys.argv[1]) / 'scripts'), str(Path(sys.argv[1]) / 'src')]
import copy_stage9_checkpoint as tool
"""
        + injection
        + """
result = tool.copy_checkpoint(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]),
                              source=Path(sys.argv[1]), workspace=Path(sys.argv[5]))
print(json.dumps({'status': result['status']}))
"""
    )
    return subprocess.run(
        [
            sys.executable,
            "-c",
            driver,
            str(source),
            str(root),
            str(target),
            str(report_path),
            str(workspace),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=90,
    )


def _target(book):
    return book.root.with_name(book.root.name + "-copy")


def test_copy_preserves_rows_close_checkpoint_and_resume(small_book):
    workspace, source, book, report_path = small_book
    original = copy._sha(book.engine.store.path), copy._sha(report_path)
    checkpoint_path = book.root / book.CHECKPOINT
    checkpoint_sha = copy._sha(checkpoint_path)
    result = _run(workspace, source, book.root, _target(book), report_path)
    assert result.returncode == 0, result.stderr
    target = _target(book)
    saved = copy._json(target / "stage9-checkpoint-copy-book.json")
    assert saved["status"] == "built_not_verified"
    assert saved["content_verified"] is False
    assert saved["copy_provenance"]["old_qualification_inherited"] is False
    assert "integrity" not in saved and "verified_open_preview" not in saved
    attestation = copy._json(target / "stage9-checkpoint-copy-attestation.json")
    assert (
        attestation["source_table_digests"]["company"]
        == attestation["target_table_digests"]["company"]
    )
    assert all(
        attestation["source_table_digests"]["catalog"][name] == digest
        for name, digest in attestation["target_table_digests"]["catalog"].items()
        if name != "company"
    )
    copied_checkpoint = copy._json(target / book.CHECKPOINT)
    original_checkpoint = copy._json(checkpoint_path)
    assert {**copied_checkpoint, "company": book.company} == original_checkpoint
    assert copied_checkpoint["company"]["path"] == str(
        target / book.company["taxpayer_id"] / "company.sqlite"
    )
    resumed = IndependentBook.resume(target)
    assert resumed.snapshots == book.snapshots
    # The copied boundary is usable for extension; its source stays open.
    with resumed.defer_historical_verification_for_construction():
        resumed.close_last_month()
        resumed.add_month(2, close=False)
    assert len(resumed.month_stats) == 3
    assert book.snapshots["2016-02"]["closed"] is False
    assert (copy._sha(book.engine.store.path), copy._sha(report_path)) == original
    assert copy._sha(checkpoint_path) == checkpoint_sha


def test_complete_report_does_not_transfer_verification(small_book):
    workspace, source, book, report_path = small_book
    report = copy._json(report_path)
    report.update(
        status="complete",
        integrity={"status": "verified"},
        verified_open_preview={"digest": "not-a-copied-qualification"},
    )
    report_path.write_text(json.dumps(report), encoding="utf-8")
    result = _run(workspace, source, book.root, _target(book), report_path)
    assert result.returncode == 0, result.stderr
    saved = copy._json(_target(book) / "stage9-checkpoint-copy-book.json")
    assert saved["status"] == "built_not_verified"
    assert "integrity" not in saved and "verified_open_preview" not in saved


@pytest.mark.parametrize("change", ["identity", "epochs", "months", "source", "status"])
def test_reject_mismatched_checkpoint_or_report_before_target(small_book, change):
    workspace, source, book, report_path = small_book
    report = copy._json(report_path)
    checkpoint_path = book.root / book.CHECKPOINT
    checkpoint = copy._json(checkpoint_path)
    if change == "identity":
        report["company"]["name"] = "普通企业"
    elif change == "epochs":
        checkpoint["epochs"][1] += 1
    elif change == "months":
        report["months"][0]["closed"] = False
        checkpoint["month_stats"][0]["closed"] = False
    elif change == "source":
        report["source"] = "relative-source"
    else:
        report["status"] = "creation_failed"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    result = _run(workspace, source, book.root, _target(book), report_path)
    assert result.returncode != 0
    assert not _target(book).exists()


def test_existing_target_is_never_overwritten(small_book):
    workspace, source, book, report_path = small_book
    target = _target(book)
    target.mkdir()
    sentinel = target / "original.txt"
    sentinel.write_text("keep", encoding="utf-8")
    result = _run(workspace, source, book.root, target, report_path)
    assert result.returncode != 0
    assert "must be absent" in result.stderr
    assert list(target.iterdir()) == [sentinel]
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_source_change_during_copy_leaves_failed_candidate(small_book):
    workspace, source, book, report_path = small_book
    injection = """
import sqlite3
original = tool._copy_database
def changed(original_connection, destination, **kwargs):
    result = original(original_connection, destination, **kwargs)
    if kwargs['kind'] == 'company':
        with sqlite3.connect(Path(sys.argv[2]) / 'catalog.sqlite') as writer:
            writer.execute("UPDATE company SET name='并行修改'")
    return result
tool._copy_database = changed
"""
    result = _run(workspace, source, book.root, _target(book), report_path, injection=injection)
    assert result.returncode != 0
    assert "Live source database changed" in result.stderr
    audit = copy._json(_target(book) / "stage9-checkpoint-copy-failed.json")
    assert audit["status"] == "failed_content_unverified"
    assert not (_target(book) / "stage9-checkpoint-copy-book.json").exists()


def test_corrupted_copy_leaves_failed_candidate(small_book):
    workspace, source, book, report_path = small_book
    injection = """
import sqlite3
original = tool._copy_database
def changed(original_connection, destination, **kwargs):
    result = original(original_connection, destination, **kwargs)
    if kwargs['kind'] == 'company':
        with sqlite3.connect(destination) as writer:
            writer.execute('UPDATE state SET accounting=accounting+1')
            result = tool._digests(writer)
    return result
tool._copy_database = changed
"""
    result = _run(workspace, source, book.root, _target(book), report_path, injection=injection)
    assert result.returncode != 0
    assert "Copied rowid or saved content differs" in result.stderr
    assert (_target(book) / "stage9-checkpoint-copy-failed.json").is_file()
    assert not (_target(book) / "stage9-checkpoint-copy-book.json").exists()


def test_path_namespace_and_reparse_are_rejected(tmp_path, monkeypatch):
    (tmp_path / ".tmp").mkdir()
    with pytest.raises(ValueError, match="direct Stage 9"):
        copy._safe_path(tmp_path / "data/stage9-real", tmp_path)
    with pytest.raises(ValueError, match="direct Stage 9"):
        copy._safe_path(tmp_path / ".tmp/stage9-nested/book", tmp_path)
    original = Path.lstat

    def reparse(path):
        info = original(path)
        if path.name == ".tmp":
            from types import SimpleNamespace

            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info

    monkeypatch.setattr(Path, "lstat", reparse)
    with pytest.raises(ValueError, match="reparse"):
        copy._safe_path(tmp_path / ".tmp/stage9-copy", tmp_path)


def test_mixed_copy_uses_same_checkpoint_identity(fixed_source, monkeypatch):
    workspace, source = fixed_source
    monkeypatch.setenv("STAGE9_WORKSPACE_ROOT", str(workspace))
    book = MixedBook(workspace / ".tmp/stage9-mixed-copy", employees=1, businesses=26)
    with book.defer_historical_verification_for_construction():
        book.add_month(0, close=False)
    report = {
        **book.describe(),
        "requested_months": 1,
        "source": str(source),
        "status": "built_not_verified",
    }
    report_path = workspace / ".tmp/stage9-mixed-copy-report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    result = _run(workspace, source, book.root, _target(book), report_path)
    assert result.returncode == 0, result.stderr
    resumed = MixedBook.resume(_target(book))
    assert resumed.company["id"] == book.company["id"]
    assert resumed.business_subjects == book.business_subjects
