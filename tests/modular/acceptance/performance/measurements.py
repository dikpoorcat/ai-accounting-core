"""Keep complete timing observations without inventing acceptance thresholds."""

from math import ceil, isfinite

from .._support import SPEC

_C14 = next(case for case in SPEC["cases"] if case["id"] == "C14")["measurement_plan"]
WARMUPS = _C14["warmup_samples"]
SAMPLES = _C14["samples"]


def summarize(samples, *, expected=SAMPLES):
    """Failures are None/error records, retained in samples and never trimmed/replaced."""
    complete = (
        type(expected) is int
        and expected > 0
        and len(samples) == expected
        and all(
            isinstance(item, (int, float))
            and not isinstance(item, bool)
            and isfinite(item)
            and item >= 0
            for item in samples
        )
    )
    valid = sorted(
        item
        for item in samples
        if isinstance(item, (int, float))
        and not isinstance(item, bool)
        and isfinite(item)
        and item >= 0
    )
    maximum = max(valid) if valid else None
    p95 = valid[ceil(len(valid) * 0.95) - 1] if valid else None
    p50 = valid[ceil(len(valid) * 0.5) - 1] if valid else None
    return {
        "status": "measured" if complete else "failed",
        "complete": complete,
        "expected_samples": expected,
        "samples": samples,
        "p95_ms": p95,
        "p50_ms": p50,
        "max_ms": maximum,
    }
