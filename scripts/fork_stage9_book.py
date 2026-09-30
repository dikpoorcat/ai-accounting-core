"""Fork a completed synthetic prefix through the normal portable backup path.

This saves repeated fixture generation for 12/48/120 month comparisons. It does
not change business identities or content and is never installed in the runtime.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

if __package__:
    from .stage9_source import (
        configure_source,
        require_source_module,
        synthetic_path,
        workspace_root,
    )
else:
    from stage9_source import (
        configure_source,
        require_source_module,
        synthetic_path,
        workspace_root,
    )


def _source_identity(book):
    return hashlib.sha256(
        json.dumps(
            [book.facts, book.snapshots], sort_keys=True, separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def _file_digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fork(
    source: Path, target: Path, archive_directory: Path, *, repository: Path, code_source: Path
):
    source, target, archive_directory = (
        synthetic_path(path, repository) for path in (source, target, archive_directory)
    )
    if target.exists() or archive_directory.exists():
        raise ValueError("Fork target and archive directory must not exist")
    import stage9_book
    import stage9_independent_book

    import ai_accounting

    require_source_module(ai_accounting, code_source, "src/ai_accounting/__init__.py")
    require_source_module(stage9_book, code_source, "tests/kernel/stage9_book.py")
    require_source_module(
        stage9_independent_book, code_source, "tests/kernel/stage9_independent_book.py"
    )
    from stage9_book import MixedBook
    from stage9_independent_book import IndependentBook

    from ai_accounting.kernel.backup import create_portable
    from ai_accounting.kernel.catalog import Catalog
    from ai_accounting.kernel.schema_bundle import production_bundle

    checkpoint = (
        IndependentBook.CHECKPOINT
        if (source / IndependentBook.CHECKPOINT).exists()
        else "stage9-builder.json"
    )
    book_type = IndependentBook if checkpoint == IndependentBook.CHECKPOINT else MixedBook
    book = book_type.resume(source)
    expected = {
        "mixed_cumulative": ("阶段九合成规模企业", "91310000123456789S"),
        "independent_local_pairs": ("阶段九合成独立业务企业", "91310000123456789I"),
    }
    if (
        (book.company["name"], book.company["taxpayer_id"])
        != expected.get(book.distribution)
        or not book.month_stats
        or book.month_stats[-1]["closed"]
    ):
        raise ValueError("Fork requires a completed synthetic book with its final month open")
    before = _file_digest(source / checkpoint)
    source_identity = _source_identity(book)
    company = dict(book.company)
    database = book.engine.store.path
    del book
    archive = create_portable(
        database, archive_directory, request_id="stage9-synthetic-prefix"
    )
    # Reject concurrent fixture advancement before creating the new catalog.
    book_type.resume(source)
    if _file_digest(source / checkpoint) != before:
        raise ValueError("Synthetic source advanced while its backup was prepared")
    catalog = Catalog(target, production_bundle())
    catalog.restore_company(
        archive["path"], taxpayer_id=company["taxpayer_id"], name=company["name"]
    )
    with (source / checkpoint).open(encoding="utf-8") as handle:
        state = json.load(handle)
    if _file_digest(source / checkpoint) != before:
        raise ValueError("Synthetic source advanced while its restore was prepared")
    state["company"] = catalog.companies()[0]
    with (target / checkpoint).open("x", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False)
    del state
    resumed = book_type.resume(target)
    if _source_identity(resumed) != source_identity:
        raise AssertionError("Synthetic fork changed historical source identities")
    return {
        "source": str(source),
        "code_source": str(code_source),
        "workspace": str(repository),
        "target": str(target),
        "archive": archive["path"],
        "archive_database_format": archive["database_format"],
        "archive_verification": archive["verification"],
        "archive_manifest": archive["manifest"],
        "months": len(resumed.month_stats),
        "company": resumed.company,
        "status": "verified_prefix_fork",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--code-source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--archive-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        repository = workspace_root(Path(__file__).resolve().parents[1], args.workspace)
        for path in (args.output, args.source, args.target, args.archive_directory):
            synthetic_path(path, repository)
    except ValueError as exc:
        parser.error(str(exc))
    if args.output.exists():
        parser.error("Preserve the previous report; choose a new output path")
    code_source = configure_source(args.code_source, repository)
    from ai_accounting.kernel.daemon import _prepare_static_runtime

    # As in the service, prepare only static models before loading any book.
    _prepare_static_runtime()
    result = fork(
        args.source, args.target, args.archive_directory,
        repository=repository, code_source=code_source,
    )
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
