"""Copied DELETE books attach an idle WAL client before real continuation."""

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from scripts.snapshot_stage9_source import snapshot_source


@pytest.fixture(scope="module")
def wal_workspace():
    workspace = Path(__file__).resolve().parents[2]
    assert Path(sys.prefix).resolve() == workspace / ".tmp-kernel-venv"
    (workspace / ".tmp").mkdir(exist_ok=True)
    prefix = workspace / ".tmp" / ("stage9-wal-regression-" + uuid.uuid4().hex)
    source = prefix.with_name(prefix.name + "-source")
    snapshot_source(workspace, source, workspace=workspace)
    # Retain source, subprocess logs and qualification receipts for review.
    return workspace, prefix, source


def _command(workspace, source, prefix, phase, script, arguments, *, injection=""):
    driver = prefix.with_name(prefix.name + f"-{phase}.py")
    driver.write_text(
        "import runpy, sys\nfrom pathlib import Path\n"
        "sys.path.insert(0, str(Path(sys.argv[1]) / 'scripts'))\n"
        "from stage9_source import configure_source\n"
        "source = configure_source(Path(sys.argv[1]), Path(sys.argv[2]))\n"
        "script = sys.argv[3]\n"
        "arguments = sys.argv[4:]\n"
        + injection
        + "\nsys.argv = [str(source / 'scripts' / script), *arguments]\n"
        "runpy.run_path(str(source / 'scripts' / script), run_name='__main__')\n",
        encoding="utf-8",
    )
    command = [
        sys.executable,
        "-B",
        str(driver),
        str(source),
        str(workspace),
        script,
        "--source",
        str(source),
        "--workspace",
        str(workspace),
        *map(str, arguments),
    ]
    completed = subprocess.run(
        command,
        cwd=workspace,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=180,
    )
    for suffix, text in ((".stdout.txt", completed.stdout), (".stderr.txt", completed.stderr)):
        prefix.with_name(prefix.name + f"-{phase}" + suffix).write_text(text, encoding="utf-8")
    prefix.with_name(prefix.name + f"-{phase}-exit.json").write_text(
        json.dumps({"argv": command, "actual_exit_code": completed.returncode}, indent=2),
        encoding="utf-8",
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("independent", [False, True], ids=["main", "independent"])
def test_delete_copy_continues_with_idle_keeper_and_fresh_readonly_qualification(
    wal_workspace,
    independent,
):
    workspace, common, source = wal_workspace
    prefix = common.with_name(common.name + ("-independent" if independent else "-main"))
    script = "benchmark_stage9_independent.py" if independent else "benchmark_stage9.py"
    dimensions = (
        ["--objects", "2", "--businesses", "4"]
        if independent
        else [
            "--employees",
            "1",
            "--businesses",
            "26",
        ]
    )
    original = prefix.with_name(prefix.name + "-original")
    copied = prefix.with_name(prefix.name + "-copy")
    built = prefix.with_name(prefix.name + "-built.json")
    extended = prefix.with_name(prefix.name + "-extended.json")
    qualified = prefix.with_name(prefix.name + "-qualified.json")
    checkpoint_name = "stage9-independent-builder.json" if independent else "stage9-builder.json"
    _command(
        workspace,
        source,
        prefix,
        "initial",
        script,
        [
            "--root",
            original,
            "--output",
            built,
            "--months",
            "1",
            *dimensions,
            "--build-only",
            "--defer-historical-verification",
        ],
    )
    _command(
        workspace,
        source,
        prefix,
        "copy",
        "copy_stage9_checkpoint.py",
        [
            "--source-root",
            original,
            "--target-root",
            copied,
            "--source-report",
            built,
        ],
    )
    copied_checkpoint = copied / checkpoint_name
    original_checkpoint_bytes = (original / checkpoint_name).read_bytes()
    initial_copy = json.loads((copied / "stage9-checkpoint-copy-book.json").read_text("utf-8"))
    assert initial_copy["status"] == "built_not_verified"
    assert "integrity" not in initial_copy and "verified_open_preview" not in initial_copy
    evidence = prefix.with_name(prefix.name + "-keeper.json")
    injection = f"""
import sqlite3
from contextlib import closing, contextmanager
import json
import copy_stage9_checkpoint as copying
from stage9_book import MixedBook
original_keeper = MixedBook.construction_wal_keeper
evidence_path = Path({str(evidence)!r})
@contextmanager
def observe_keeper(book):
    checkpoint = book.root / {checkpoint_name!r}
    checkpoint_bytes = checkpoint.read_bytes()
    with closing(sqlite3.connect(book.engine.store.path.as_uri() + '?mode=ro', uri=True)) as raw:
        assert raw.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
        before = copying._digests(raw)
    with original_keeper(book) as keeper:
        assert keeper.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        assert keeper.execute('PRAGMA query_only').fetchone()[0] == 1
        assert keeper.execute('PRAGMA synchronous').fetchone()[0] == 2
        assert not keeper.in_transaction
        assert copying._digests(keeper) == before
        assert checkpoint.read_bytes() == checkpoint_bytes
        evidence_path.write_text(json.dumps({{'mode': 'wal', 'in_transaction': False,
            'all_rows_unchanged_at_attach': True, 'checkpoint_unchanged_at_attach': True}}))
        yield keeper
        assert not keeper.in_transaction
    try:
        keeper.execute('SELECT * FROM state')
    except sqlite3.ProgrammingError:
        pass
    else:
        raise AssertionError('Keeper was not closed')
MixedBook.construction_wal_keeper = observe_keeper
"""
    _command(
        workspace,
        source,
        prefix,
        "extend",
        script,
        [
            "--root",
            copied,
            "--output",
            extended,
            "--months",
            "2",
            *dimensions,
            "--resume",
            "--build-only",
            "--defer-historical-verification",
        ],
        injection=injection,
    )
    assert json.loads(evidence.read_text())["all_rows_unchanged_at_attach"] is True
    assert (original / checkpoint_name).read_bytes() == original_checkpoint_bytes
    extension = json.loads(extended.read_text("utf-8"))
    assert extension["status"] == "built_not_verified"
    assert [month["closed"] for month in extension["months"]] == [True, False]
    before_qualification = copied_checkpoint.read_bytes()
    guard = """
from contextlib import contextmanager
from ai_accounting.kernel.catalog import Catalog
from ai_accounting.kernel.storage import Store
from stage9_book import MixedBook
def unexpected_keeper(*args, **kwargs):
    raise AssertionError('Readonly qualification invoked construction keeper')
MixedBook.construction_wal_keeper = unexpected_keeper
def only_read(original):
    @contextmanager
    def guarded(self, *, read_only=False, **options):
        assert read_only, 'Readonly qualification opened a writable connection'
        with original(self, read_only=True, **options) as connection:
            yield connection
    return guarded
Store.connection = only_read(Store.connection)
Catalog.connection = only_read(Catalog.connection)
"""
    _command(
        workspace,
        source,
        prefix,
        "qualify",
        script,
        [
            "--root",
            copied,
            "--output",
            qualified,
            "--months",
            "2",
            *dimensions,
            "--resume" if independent else "--read-existing",
            "--verify-only",
        ],
        injection=guard,
    )
    result = json.loads(qualified.read_text("utf-8"))
    assert result["status"] == "complete"
    assert result["integrity"]["status"] == "verified"
    assert result["integrity"]["counts"]["closes"] == 1
    assert result["verified_open_preview"]["period"] == "2016-02"
    assert result["verified_open_preview"]["state"] == json.loads(before_qualification)["epochs"]
    assert copied_checkpoint.read_bytes() == before_qualification
    assert (original / checkpoint_name).read_bytes() == original_checkpoint_bytes
