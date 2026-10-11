from __future__ import annotations

import hashlib
import json
import shutil
from zipfile import ZipFile

import pytest

from scripts.source_archive import (
    FORMAT,
    SourceArchiveError,
    main,
    read_archived_path,
    read_source,
    restore_archive,
    verify_archive,
    verify_facts_sources,
)


def make_archive(root, content=b"original\r\n", *, extra=None):
    digest = hashlib.sha256(content).hexdigest()
    member = f"originals/{digest}"
    source = {
        "path": "confirmation.md", "key": "confirmation-1",
        "sha256": digest, "bytes": len(content),
        "archive": {"path": "company.zip", "member": member},
    }
    index = {"format": FORMAT, "originals": [{
        "sha256": digest, "bytes": len(content), "member": member,
        "aliases": [{"path": "confirmation.md", "name": "first"},
                    {"path": "historical.md", "name": "second"}],
    }]}
    with ZipFile(root / "company.zip", "w") as archive:
        archive.writestr("index.json", json.dumps(index))
        archive.writestr(member, content)
        if extra:
            archive.writestr(extra, b"bad")
    return source


def test_original_bytes_survive_deleted_and_replaced_loose_files(tmp_path):
    source = make_archive(tmp_path)
    assert read_source(tmp_path, source) == b"original\r\n"
    (tmp_path / "confirmation.md").write_bytes(b"newest human-readable summary")
    assert read_source(tmp_path, source) == b"original\r\n"
    assert verify_archive(tmp_path, "company.zip") == {
        "originals": 1, "aliases": 2, "bytes": 10,
    }
    facts = {"facts": [{"source": source}, {"sources": [dict(source, key="second")]}]}
    (tmp_path / "facts.json").write_text(json.dumps(facts), encoding="utf-8")
    assert verify_facts_sources(tmp_path, "facts.json") == {
        "sources": 2, "archived": 2, "originals": 1, "bytes": 20,
    }


@pytest.mark.parametrize("field,value", [("bytes", 11), ("sha256", "0" * 64)])
def test_bad_identity_has_no_fallback(tmp_path, field, value):
    source = make_archive(tmp_path)
    (tmp_path / "confirmation.md").write_bytes(b"original\r\n")
    source[field] = value
    if field == "sha256":
        source["archive"]["member"] = f"originals/{value}"
    with pytest.raises(SourceArchiveError):
        read_source(tmp_path, source)


@pytest.mark.parametrize("locator", [
    {"path": "../outside.zip", "member": "originals/unused"},
    {"path": "C:/outside.zip", "member": "originals/unused"},
    {"path": "company.zip", "member": "../escape"},
    None,
])
def test_reject_invalid_locator(tmp_path, locator):
    source = make_archive(tmp_path)
    if isinstance(locator, dict) and locator["member"] == "originals/unused":
        locator["member"] = source["archive"]["member"]
    source["archive"] = locator
    with pytest.raises(SourceArchiveError):
        read_source(tmp_path, source)


def test_digest_mismatch_in_existing_member(tmp_path):
    source = make_archive(tmp_path)
    with ZipFile(tmp_path / "company.zip", "r") as archive:
        index = archive.read("index.json")
    with ZipFile(tmp_path / "company.zip", "w") as archive:
        archive.writestr("index.json", index)
        archive.writestr(source["archive"]["member"], b"corrupt!\r\n")
    with pytest.raises(SourceArchiveError, match="SHA-256"):
        read_source(tmp_path, source)
    with pytest.raises(SourceArchiveError, match="SHA-256"):
        verify_archive(tmp_path, "company.zip")


@pytest.mark.parametrize("extra", ["../escape", "C:/escape", "originals/unindexed"])
def test_restore_rejects_unsafe_or_unindexed_members(tmp_path, extra):
    make_archive(tmp_path, extra=extra)
    with pytest.raises(SourceArchiveError):
        restore_archive(tmp_path, "company.zip")


def test_restore_uses_canonical_names_in_fresh_temp_directory(tmp_path, capsys):
    source = make_archive(tmp_path)
    (tmp_path / "confirmation.md").write_bytes(b"new summary")
    destination = restore_archive(tmp_path, "company.zip")
    try:
        assert not destination.is_relative_to(tmp_path)
        assert (destination / source["archive"]["member"]).read_bytes() == b"original\r\n"
        for target in (destination / "originals").iterdir():
            assert hashlib.sha256(target.read_bytes()).hexdigest() == target.name
        assert not (destination / "confirmation.md").exists()
        assert (tmp_path / "confirmation.md").read_bytes() == b"new summary"
        assert main(["--root", str(tmp_path), "verify", "--archive", "company.zip"]) == 0
        assert json.loads(capsys.readouterr().out)["archives"][0]["aliases"] == 2
    finally:
        shutil.rmtree(destination)


def test_loose_source_still_validates_bytes(tmp_path):
    source = make_archive(tmp_path)
    del source["archive"]
    (tmp_path / source["path"]).write_bytes(b"original\r\n")
    assert read_source(tmp_path, source) == b"original\r\n"
    (tmp_path / source["path"]).write_bytes(b"replaced")
    with pytest.raises(SourceArchiveError):
        read_source(tmp_path, source)


@pytest.mark.parametrize("facts", [{"sources": [{}]}, {"source": {"path": "a.md", "key": "a"}},
                                   {"sources": "broken"}])
def test_malformed_fact_source_is_not_silently_skipped(tmp_path, facts):
    with pytest.raises(SourceArchiveError):
        verify_facts_sources(tmp_path, facts)


def test_source_key_references_are_not_original_descriptors(tmp_path):
    source = make_archive(tmp_path)
    facts = {
        "sources": [source],
        "company_note": {"sources": ["confirmation-1"]},
        "facts": [{"data": {"sources": ["confirmation-1"], "source": {"key": "ref"}}},
                  {"source": source}],
    }
    assert verify_facts_sources(tmp_path, facts) == {
        "sources": 2, "archived": 2, "originals": 1, "bytes": 20,
    }


def test_historical_path_resolution_accepts_repeated_aliases(tmp_path):
    make_archive(tmp_path)
    with ZipFile(tmp_path / "company.zip") as archive:
        index = json.loads(archive.read("index.json"))
        content = archive.read(index["originals"][0]["member"])
    aliases = index["originals"][0]["aliases"]
    aliases.append(dict(aliases[0]))
    aliases.append({"path": "../../outside\\historical.md"})
    with ZipFile(tmp_path / "company.zip", "w") as archive:
        archive.writestr("index.json", json.dumps(index))
        archive.writestr(index["originals"][0]["member"], content)
    assert read_archived_path(tmp_path, "company.zip", "confirmation.md") == content
    assert read_archived_path(tmp_path, "company.zip", "../../outside\\historical.md") == content
    with pytest.raises(SourceArchiveError, match="absent"):
        read_archived_path(tmp_path, "company.zip", "absent.md")


def test_historical_path_resolution_rejects_different_content_for_same_path(tmp_path):
    make_archive(tmp_path)
    with ZipFile(tmp_path / "company.zip") as archive:
        index = json.loads(archive.read("index.json"))
        content = archive.read(index["originals"][0]["member"])
    second = b"different original"
    digest = hashlib.sha256(second).hexdigest()
    index["originals"].append({
        "sha256": digest, "bytes": len(second), "member": f"originals/{digest}",
        "aliases": [{"path": "confirmation.md"}],
    })
    with ZipFile(tmp_path / "company.zip", "w") as archive:
        archive.writestr("index.json", json.dumps(index))
        archive.writestr(index["originals"][0]["member"], content)
        archive.writestr(index["originals"][1]["member"], second)
    with pytest.raises(SourceArchiveError, match="ambiguous"):
        read_archived_path(tmp_path, "company.zip", "confirmation.md")


def test_read_cli_preserves_utf8_bytes_and_rejects_binary(tmp_path, capfdbinary):
    make_archive(tmp_path, content="原件\r\n".encode())
    assert main(["--root", str(tmp_path), "read", "company.zip", "--path", "confirmation.md"]) == 0
    assert capfdbinary.readouterr().out == "原件\r\n".encode()
    make_archive(tmp_path, content=b"\xff")
    with pytest.raises(SystemExit) as failure:
        main(["--root", str(tmp_path), "read", "company.zip", "--path", "confirmation.md"])
    assert failure.value.code == 1
    assert capfdbinary.readouterr().out == b""
