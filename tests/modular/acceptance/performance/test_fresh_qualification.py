"""The large-scale command also exercises real empty projections in each mode."""

from .dataset import build
from .qualification import run


def test_same_portable_facts_recompute_all_pages_in_both_real_ledger_implementations(tmp_path):
    root = tmp_path / "fixture"
    build(root, 1, probe_business_count=240)
    result = run(root)
    assert result["status"] == "passed"
    original = result["implementations"]["original"]["public_projection_sha256"]
    for mode in ("original", "aggregate_join"):
        fresh = result["fresh_implementations"][mode]
        assert len(fresh["initial_preparing_scopes"]) == 5
        assert fresh["produced_component_scopes"]
        assert fresh["public_projection_sha256"] == original
