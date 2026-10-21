"""One real chain supplies the three complete scales and their independent prefixes."""

import json
import sys
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

from .._support import COMPANY, DATABASE
from .dataset import build as build_dataset
from .dataset import open_dataset
from .portable_cases import package, restore
from .workload import SCALES, month


def _checkpoints(scales):
    result = {}
    for size in scales:
        result.setdefault(size - 1, []).append((f"prefix-{size}", size))
        if size != scales[-1]:
            result.setdefault(size, []).append((f"full-{size}", None))
    return result


def build(root, mode="original", *, probe_scales=None, probe_business_count=None):
    """Probe arguments are explicit and never produce an official-scale manifest."""
    probe = probe_scales is not None or probe_business_count is not None
    if probe and (probe_scales is None or probe_business_count is None):
        raise ValueError("A probe must specify both its small scales and business count")
    if probe and (type(probe_business_count) is not int or probe_business_count < 1):
        raise ValueError("A probe needs a positive whole number of businesses")
    scales = tuple(probe_scales) if probe else SCALES
    if (
        not scales
        or any(type(size) is not int or size < 2 for size in scales)
        or tuple(sorted(set(scales))) != scales
    ):
        raise ValueError(
            "Suite scales must be distinct ascending whole numbers of at least two months"
        )
    root = Path(root).resolve()
    root.mkdir(parents=False, exist_ok=False)
    working = root / f"full-{scales[-1]}"
    checkpoints = _checkpoints(scales)
    delivered = {}

    def checkpoint(source, manifest):
        targets = checkpoints.get(manifest["months"], ())
        if not targets:
            return
        with TemporaryDirectory(prefix=".suite-snapshot-", dir=root) as temporary:
            portable = package(source, Path(temporary) / "snapshot.zip", mode)
            for name, intended in targets:
                snapshot = deepcopy(manifest)
                snapshot["official_scale"] = not probe and intended is None
                if intended is not None:
                    snapshot.update(
                        intended_scale_months=intended, pending_period=month(snapshot["months"])
                    )
                target = root / name
                target.mkdir(parents=False, exist_ok=False)
                restore(portable, target, snapshot, mode)
                with open_dataset(target, mode) as (kernel, _):
                    with kernel.storage.read_unit(COMPANY) as read:
                        status = kernel.records.document_status(read)
                        assert status["state"] == "ready", status
                delivered[name] = {"root": str(target), "manifest": snapshot}
                print(
                    f"合成样本已独立交付：{name}，实际 {snapshot['months']} 个月。",
                    file=sys.stderr,
                    flush=True,
                )

    final = build_dataset(
        working,
        scales[-1],
        mode,
        probe_business_count=probe_business_count,
        _checkpoint=checkpoint,
    )
    delivered[working.name] = {"root": str(working), "manifest": final}
    result = {
        "format": "modular-c14-suite/1",
        "status": "passed",
        "official": not probe,
        "company_id": COMPANY,
        "database_id": DATABASE,
        "mode": mode,
        "scales": list(scales),
        "construction_chains": 1,
        "datasets": delivered,
    }
    (root / "suite.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result
