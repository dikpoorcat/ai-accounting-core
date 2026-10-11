"""Read and verify private confirmation sources without touching business databases.

Archive locators are authoritative: a broken locator never falls back to a loose file.
Restoration writes canonical content into a newly created temporary directory only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from zipfile import BadZipFile, ZipFile

FORMAT = "ai-accounting/confirmation-archive/1"
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class SourceArchiveError(ValueError):
    """A source or archive failed identity, integrity, or path validation."""


def _repo_path(root: Path, value: Any) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise SourceArchiveError("Expected a repository-relative POSIX path")
    relative = PurePosixPath(value)
    if relative.is_absolute() or PureWindowsPath(value).drive or ".." in relative.parts:
        raise SourceArchiveError("Path must remain inside repository root")
    resolved_root = Path(root).resolve()
    resolved = (resolved_root / value).resolve()
    if not resolved.is_relative_to(resolved_root):
        raise SourceArchiveError("Path resolves outside repository root")
    return resolved


def _identity(source: Mapping[str, Any]) -> tuple[str, int, str]:
    digest, size = source.get("sha256"), source.get("bytes")
    if not isinstance(digest, str) or not SHA256.fullmatch(digest):
        raise SourceArchiveError("Expected a lowercase SHA-256 digest")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise SourceArchiveError("Expected a nonnegative integer byte count")
    return digest, size, f"originals/{digest}"


def _check_content(content: bytes, source: Mapping[str, Any]) -> bytes:
    digest, size, _ = _identity(source)
    if len(content) != size:
        raise SourceArchiveError("Source byte count mismatch")
    if hashlib.sha256(content).hexdigest() != digest:
        raise SourceArchiveError("Source SHA-256 mismatch")
    return content


def _members(archive: ZipFile) -> set[str]:
    names = archive.namelist()
    if len(names) != len(set(names)):
        raise SourceArchiveError("Archive contains duplicate member names")
    for name in names:
        path = PurePosixPath(name)
        if (not name or "\\" in name or path.is_absolute()
                or PureWindowsPath(name).drive or ".." in path.parts):
            raise SourceArchiveError("Archive contains an unsafe member path")
    return set(names)


def read_source(root: Path, source: Mapping[str, Any]) -> bytes:
    """Return exact original bytes, enforcing the source's declared SHA and size."""
    _, _, member = _identity(source)
    if "archive" in source:
        locator = source["archive"]
        if not isinstance(locator, Mapping) or locator.get("member") != member:
            raise SourceArchiveError("Archive member must be originals/<source sha256>")
        path = _repo_path(root, locator.get("path"))
        with ZipFile(path) as archive:
            if member not in _members(archive):
                raise SourceArchiveError("Archive source member is missing")
            content = archive.read(member)
    else:
        content = _repo_path(root, source.get("path")).read_bytes()
    return _check_content(content, source)


def _archive_contents(root: Path, archive_path: str) -> tuple[dict[str, Any], dict[str, bytes]]:
    with ZipFile(_repo_path(root, archive_path)) as archive:
        members = _members(archive)
        if "index.json" not in members:
            raise SourceArchiveError("Archive index.json is missing")
        index = json.loads(archive.read("index.json"))
        if not isinstance(index, dict) or index.get("format") != FORMAT:
            raise SourceArchiveError("Unsupported confirmation archive format")
        originals = index.get("originals")
        if not isinstance(originals, list):
            raise SourceArchiveError("Archive originals must be a list")
        contents: dict[str, bytes] = {}
        for original in originals:
            if not isinstance(original, dict):
                raise SourceArchiveError("Archive original must be an object")
            _, _, member = _identity(original)
            if original.get("member") != member or member in contents:
                raise SourceArchiveError("Invalid or duplicate canonical original member")
            aliases = original.get("aliases")
            if not isinstance(aliases, list) or not all(isinstance(a, dict) for a in aliases):
                raise SourceArchiveError("Original aliases must be a list of objects")
            if member not in members:
                raise SourceArchiveError("Indexed original member is missing")
            contents[member] = _check_content(archive.read(member), original)
        if members != {"index.json", *contents}:
            raise SourceArchiveError("Archive contains unindexed members")
    return index, contents


def verify_archive(root: Path, archive_path: str) -> dict[str, int]:
    """Verify the full index and every stored original, including unreferenced ones."""
    index, contents = _archive_contents(root, archive_path)
    return {
        "originals": len(contents),
        "aliases": sum(len(item["aliases"]) for item in index["originals"]),
        "bytes": sum(map(len, contents.values())),
    }


def read_archived_path(root: Path, archive_path: str, historical_path: str) -> bytes:
    """Resolve an exact historical alias as metadata, never as a filesystem path.

    Repeated aliases of the same content are valid; one path naming different
    originals is ambiguous and must be resolved explicitly by source SHA instead.
    """
    if not isinstance(historical_path, str):
        raise SourceArchiveError("Historical path must be a string")
    index, contents = _archive_contents(root, archive_path)
    matches = {
        original["member"]
        for original in index["originals"]
        if any(alias.get("path") == historical_path for alias in original["aliases"])
    }
    if not matches:
        raise SourceArchiveError("Historical path is absent from archive aliases")
    if len(matches) != 1:
        raise SourceArchiveError("Historical path names ambiguous original content")
    return contents[matches.pop()]


def _sources(value: Any, *, root: bool = True) -> Iterator[Mapping[str, Any]]:
    if isinstance(value, dict):
        if "archive" in value or ("path" in value and {"sha256", "bytes", "key"} & value.keys()):
            yield value
            return
        for key, child in value.items():
            if root and key == "sources":
                if not isinstance(child, list) or not all(isinstance(s, dict) for s in child):
                    raise SourceArchiveError("Fact sources must be a list of source objects")
                yield from child
            else:
                yield from _sources(child, root=False)
    elif isinstance(value, list):
        for child in value:
            yield from _sources(child, root=False)


def verify_facts_sources(root: Path, facts_json: Any) -> dict[str, int]:
    """Verify source records in parsed facts or a repository-relative JSON file."""
    if isinstance(facts_json, (str, Path)):
        facts_json = json.loads(_repo_path(root, str(facts_json)).read_text(encoding="utf-8"))
    total = archived = size = 0
    identities = set()
    for source in _sources(facts_json):
        content = read_source(root, source)
        total += 1
        archived += "archive" in source
        size += len(content)
        identities.add(source["sha256"])
    return {"sources": total, "archived": archived, "originals": len(identities), "bytes": size}


def restore_archive(root: Path, archive_path: str) -> Path:
    """Verify first, then restore canonical originals in a fresh system temp directory.

    The caller owns the returned directory. Historical alias paths are never used
    as output paths, so restoration cannot overwrite working confirmation files.
    """
    _, contents = _archive_contents(root, archive_path)
    destination = Path(tempfile.mkdtemp(prefix="confirmation-archive-"))
    originals = destination / "originals"
    originals.mkdir()
    for member, content in contents.items():
        target = destination / member
        target.write_bytes(content)
        restored = target.read_bytes()
        if (len(restored) != len(content)
                or hashlib.sha256(restored).hexdigest() != target.name):
            raise SourceArchiveError("Restored content verification failed")
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    subcommands = parser.add_subparsers(dest="command", required=True)
    verify = subcommands.add_parser("verify", help="Verify fact sources and/or full archives")
    verify.add_argument("--facts", action="append", default=[])
    verify.add_argument("--archive", action="append", default=[])
    restore = subcommands.add_parser("restore", help="Verify and restore into a new temp directory")
    restore.add_argument("archive")
    read = subcommands.add_parser(
        "read", help="Read UTF-8 original bytes by exact historical alias",
    )
    read.add_argument("archive")
    read.add_argument("--path", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "read":
            import sys

            content = read_archived_path(args.root, args.archive, args.path)
            content.decode("utf-8")  # Refuse non-UTF-8 without partial stdout output.
            sys.stdout.buffer.write(content)
        elif args.command == "restore":
            print(json.dumps({"restored_to": str(restore_archive(args.root, args.archive))}))
        else:
            if not args.facts and not args.archive:
                parser.error("verify requires --facts or --archive")
            results = {
                "facts": [verify_facts_sources(args.root, path) for path in args.facts],
                "archives": [verify_archive(args.root, path) for path in args.archive],
            }
            print(json.dumps(results))
    except (ValueError, OSError, BadZipFile, KeyError) as error:
        parser.exit(1, f"Source verification failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
