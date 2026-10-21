"""Negative checks keep the acceptance driver from silently approving missing evidence."""

import sqlite3
from types import SimpleNamespace

import pytest

from . import _worker
from ._observations import subset
from ._runner import resolve
from ._worker import Worker


def test_unknown_oracle_field_and_sandbox_escape_are_rejected(tmp_path):
    with pytest.raises(AssertionError, match="no oracle field may be skipped"):
        subset({"expense_total_fen": "80000"}, {"unimplemented_check": True})
    with pytest.raises(AssertionError):
        resolve("$sandbox/../outside", {}, tmp_path)


@pytest.mark.parametrize("uri", [False, True])
def test_offline_probe_rejects_real_source_sqlite_path_and_uri(tmp_path, monkeypatch, uri):
    source = tmp_path / "source with spaces.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE synthetic(value INTEGER)")

    def attempt(_request):
        selected = source.as_uri() + "?mode=ro" if uri else source
        with sqlite3.connect(selected, uri=uri):
            pass
        return {"state": "created"}

    monkeypatch.setattr(
        _worker,
        "create_offline_application",
        lambda: SimpleNamespace(accounting_isolated=attempt),
    )
    worker = object.__new__(Worker)
    with pytest.raises(AssertionError, match="attempted to read source database"):
        worker.isolated(
            {
                "action": "restore",
                "mode": "run",
                "source_path": str(source),
                "new_target_directory": str(tmp_path / "target"),
            },
            [str(source)],
        )
