"""Run every C01–C13 fixed case/variant against both real ledger implementations."""

import json

import pytest

from ._runner import Scenario
from ._support import SPEC
from .performance.ledger_variant import MODES

SCENARIOS = [
    (case, variant)
    for case in SPEC["cases"]
    if case["id"] != "C14"
    for variant in case.get("variants", [case])
]


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    "case,variant",
    SCENARIOS,
    ids=[
        case["id"] + ("-" + variant["id"] if variant is not case else "")
        for case, variant in SCENARIOS
    ],
)
def test_fixed_core_example(case, variant, mode, tmp_path):
    scenario = Scenario(case, tmp_path / "scenario", mode)
    try:
        scenario.seed()
        scenario.initial_snapshot = scenario.snapshot()
        for step in variant["steps"]:
            try:
                scenario.execute(step)
            except Exception as error:
                pytest.fail(
                    f"{case['id']} / {variant.get('id', case['id'])} / "
                    f"{step['id']} ({step['invoke']})\n"
                    f"固定输入：{json.dumps(step, ensure_ascii=False)}\n实际差异：{error}",
                    pytrace=True,
                )
    finally:
        scenario.close()
