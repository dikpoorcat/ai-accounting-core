"""Hard process exits preserve genuine completed, failed and missing observations."""

import json
import subprocess
from pathlib import Path

from .browser import _execute
from .browser_progress import recover


def test_timeout_retains_real_slow_sample_and_missing_sample(tmp_path, monkeypatch):
    def expired(command, **options):
        config = json.loads(options["input"])
        attempts = [
            {
                "page": "brief",
                "kind": "sample",
                "iteration": 1,
                "status": "preparing",
                "elapsed_ms": None,
            },
            {
                "page": "brief",
                "kind": "sample",
                "iteration": 1,
                "status": "complete",
                "elapsed_ms": 520,
                "start_epoch_ms": 1000,
                "end_epoch_ms": 1520,
            },
            {
                "page": "brief",
                "kind": "sample",
                "iteration": 2,
                "status": "preparing",
                "elapsed_ms": None,
            },
        ]
        Path(config["progress_path"]).write_text(
            "\n".join(json.dumps({"kind": "attempt", "attempt": item}) for item in attempts)
            + '\n{"kind":',
            encoding="utf-8",
        )
        raise subprocess.TimeoutExpired(command, 7200)

    monkeypatch.setattr(subprocess, "run", expired)
    result, code, timed_out = _execute(
        "unused-node", {"warmups": 0, "repeats": 2}, tmp_path / "fixture", interactions=False
    )
    assert timed_out and code is None and result["failure_phase"] == "browser_timeout"
    assert result["damaged_checkpoint_lines"] == 1
    assert result["pages"]["brief"]["samples"] == [520, None]
    assert result["pages"]["brief"]["attempts"][0]["status"] == "complete"
    assert result["pages"]["brief"]["attempts"][1]["status"] == "incomplete_process_exit"
    assert result["pages"]["funds"]["samples"] == [None, None]


def test_missing_checkpoint_is_explicit_incomplete_and_does_not_invent_a_sample(tmp_path):
    result = recover(tmp_path / "absent", ("a",), interactions=True, warmups=3, samples=30)
    assert result["status"] == "failed" and result["partial_report"] is True
    assert result["interactions"]["a"]["attempts"] == []
    assert result["interactions"]["a"]["samples"] == [None] * 30
