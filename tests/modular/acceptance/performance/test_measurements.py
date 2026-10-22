from .measurements import summarize


def test_nearest_rank_sample_29_and_slow_failures_are_preserved():
    samples = list(range(1, 31))
    result = summarize(samples, expected=30)
    assert result["p50_ms"] == 15 and result["p95_ms"] == 29 and result["max_ms"] == 30
    assert result["samples"] == samples and result["status"] == "measured"
    slow = summarize([10000, 30000, 60000])
    assert slow["status"] == "measured" and slow["samples"] == [10000, 30000, 60000]
    assert "budget" not in slow
    assert summarize([1, 1, None])["status"] == "failed"
    assert summarize([1, 1])["status"] == "failed"
    assert summarize([1, 1, float("inf")])["status"] == "failed"
    assert summarize([1, 1, True])["status"] == "failed"
    assert summarize([], expected=0)["status"] == "failed"
