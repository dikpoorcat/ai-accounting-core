"""Recover actual durable attempt checkpoints after a browser process failure."""

import json
from pathlib import Path


def recover(path, cases, *, interactions, warmups, samples):
    attempts, damaged_lines = {}, 0
    path = Path(path)
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
                assert isinstance(event, dict)
                attempt = event["attempt"]
                assert isinstance(attempt, dict)
                name = attempt.get("scenario", attempt.get("page"))
                kind, iteration = attempt["kind"], attempt["iteration"]
                assert event["kind"] == "attempt" and name in cases
                assert kind in {"warmup", "sample"} and type(iteration) is int and iteration > 0
                assert attempt["status"] in {
                    "preparing",
                    "complete",
                    "failed",
                    "preparation_failed",
                }
            except (ValueError, KeyError, TypeError, AssertionError):
                damaged_lines += 1
                continue
            attempts[(name, kind, iteration)] = attempt
    groups = {}
    for name in cases:
        observed = [value for (case, _, _), value in attempts.items() if case == name]
        for attempt in observed:
            if attempt["status"] == "preparing":
                attempt["status"] = "incomplete_process_exit"
                attempt["error"] = {
                    "type": "BrowserProcessExit",
                    "message": "No settled checkpoint",
                }
        complete = len(observed) == warmups + samples and all(
            item["status"] == "complete" for item in observed
        )
        groups[name] = {
            "status": "measured" if complete else "failed",
            "attempts": observed,
            "samples": [
                attempts.get((name, "sample", index), {}).get("elapsed_ms")
                for index in range(1, samples + 1)
            ],
        }
    return {
        "status": "failed",
        "failure_phase": "browser_process_exit",
        "partial_report": True,
        "damaged_checkpoint_lines": damaged_lines,
        "interactions" if interactions else "pages": groups,
    }
