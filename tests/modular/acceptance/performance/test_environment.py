"""A long measurement must identify the code it started with, not just its last revision."""

import json

import pytest

from . import __main__ as cli
from . import qualification
from .environment import code_identity


@pytest.mark.parametrize("changed", (False, True))
def test_qualification_records_start_and_rejects_code_changes(monkeypatch, capsys, changed):
    calls = []
    initial = {"source_sha256": "before", "git_sha": "start-revision"}

    def environment():
        calls.append("before")
        return initial

    def run(root):
        calls.append("measurement")
        return {"status": "passed"}

    def finished():
        calls.append("after")
        return {"source_sha256": "after" if changed else "before"}

    monkeypatch.setattr(cli, "environment", environment)
    monkeypatch.setattr(cli, "code_identity", finished)
    monkeypatch.setattr(qualification, "run", run)
    monkeypatch.setattr("sys.argv", ["performance", "verify", "synthetic-case"])
    if changed:
        with pytest.raises(SystemExit, match="不能作为验收依据"):
            cli.main()
    else:
        cli.main()
    report = json.loads(capsys.readouterr().out)
    assert calls == ["before", "measurement", "after"]
    assert report["environment"] == initial
    assert report["source_unchanged"] is not changed
    assert report["code_after"] == finished()
    assert report["result"]["status"] == "passed"


def test_code_identity_covers_the_actual_measurement_and_dependency_files():
    identity = code_identity()
    assert set(identity) == {
        "source_sha256",
        "frontend_source_sha256",
        "measurement_sha256",
        "dependencies_sha256",
    }
    assert set(identity["dependencies_sha256"]) == {"pyproject.toml", "uv.lock"}
    digests = [
        *(value for key, value in identity.items() if key != "dependencies_sha256"),
        *identity["dependencies_sha256"].values(),
    ]
    assert all(len(value) == 64 and set(value) <= set("0123456789abcdef") for value in digests)
