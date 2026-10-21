from .measurements import BUDGETS, summarize


def test_nearest_rank_sample_29_and_slow_failures_are_preserved():
    samples = list(range(1, 31))
    result = summarize(samples, BUDGETS["commit_to_ready"])
    assert result["p50_ms"] == 15 and result["p95_ms"] == 29 and result["max_ms"] == 30
    assert result["samples"] == samples and result["status"] == "passed"
    assert summarize([499] * 29 + [500], BUDGETS["hot_refresh"])["status"] == "over_budget"
    assert summarize([1] * 29 + [None], BUDGETS["hot_refresh"])["status"] == "failed"
    assert summarize([1] * 29, BUDGETS["hot_refresh"])["status"] == "failed"
    assert summarize([1] * 29 + [float("inf")], BUDGETS["hot_refresh"])["status"] == "failed"
