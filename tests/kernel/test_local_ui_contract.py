"""Keep every new business kind out of the owner's English-code fallback."""

import re
from pathlib import Path

from ai_accounting.kernel.service import default_registry


def test_local_ui_has_chinese_label_for_every_registered_fact_kind():
    source = (Path(__file__).resolve().parents[2] / "frontend/src/api/localKernel.ts").read_text(
        encoding="utf-8"
    )
    mapping = source.split("export const localBusinessNames:", 1)[1].split(
        "export function localBusinessName", 1
    )[0]
    labels = dict(re.findall(r'\b([a-z_][a-z0-9_]*):\s*"([^"\n]+)"', mapping))
    assert default_registry().models.keys() <= labels.keys()
    assert all(re.search(r"[\u3400-\u9fff]", label) for label in labels.values())
