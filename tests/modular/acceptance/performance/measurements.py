"""Keep failed/slow observations and apply docs/06's nearest-rank budgets."""

from math import ceil, isfinite

from .._support import SPEC

_C14 = next(case for case in SPEC["cases"] if case["id"] == "C14")["steps"][-1]["expect"]
WARMUPS = _C14["warmup_samples"]
SAMPLES = _C14["formal_samples"]
BUDGETS = {
    "hot_refresh": {"every_lt_ms": _C14["ready_refresh_max_ms_exclusive"]},
    "local_interaction": {"every_lt_ms": _C14["specified_interaction_max_ms_exclusive"]},
    "commit_to_ready": {
        "p95_lte_ms": _C14["post_commit_model_ready_p95_ms_max"],
        "max_lte_ms": _C14["post_commit_model_ready_max_ms"],
    },
    "month_archive_and_ready": {
        "p95_lte_ms": _C14["month_archive_and_model_p95_ms_max"],
        "max_lte_ms": _C14["month_archive_and_model_max_ms"],
    },
}


def summarize(samples, budget, *, expected=SAMPLES):
    """Failures are None/error records, retained in samples and never trimmed/replaced."""
    complete = len(samples) == expected and all(
        isinstance(item, (int, float))
        and not isinstance(item, bool)
        and isfinite(item)
        and item >= 0
        for item in samples
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
    passed = complete
    if "every_lt_ms" in budget:
        passed = passed and maximum < budget["every_lt_ms"]
    if "p95_lte_ms" in budget:
        passed = passed and p95 <= budget["p95_lte_ms"]
    if "max_lte_ms" in budget:
        passed = passed and maximum <= budget["max_lte_ms"]
    return {
        "status": "passed" if passed else "over_budget" if complete else "failed",
        "complete": complete,
        "expected_samples": expected,
        "samples": samples,
        "p95_ms": p95,
        "p50_ms": p50,
        "max_ms": maximum,
        "budget": budget,
    }
