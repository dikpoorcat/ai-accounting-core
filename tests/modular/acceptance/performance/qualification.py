"""Qualify both preserved ready models and genuine recomputation after restore."""

import json
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from ai_accounting_modular.contracts.public import validate_contract

from .._support import COMPANY, http
from .dataset import open_dataset
from .environment import frontend_identity
from .ledger_variant import MODES
from .portable_cases import package, restore
from .projections import PAGES, capture, parameters


def _delivery(kernel, month, page):
    response = http(kernel, page, parameters(COMPANY, month, page))
    assert response["http_status"] == 200, response
    body = response["body"]
    validate_contract(
        "dashboard-response-v2.json", "dashboard_" + page.replace("-", "_") + "_delivery", body
    )
    return body


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def run(root):
    """All evidence comes from actual public reads, backup and isolated restores.

    Reusing ready models proves a replacement leaves consumers alone. Separately
    restoring the same portable package preserves formal identities while the
    real restore discards projections, forcing both factories to compute them.
    No production invalidation or test-only table manipulation participates.
    """
    root = Path(root).resolve()
    frontend_before = frontend_identity()
    baseline = None
    result = {"status": "passed", "implementations": {}, "fresh_implementations": {}}
    for mode in MODES:
        with open_dataset(root, mode) as (kernel, manifest):
            months = tuple(dict.fromkeys((manifest["period_from"], manifest["period_to"])))
            if baseline is not None:
                for month in months:
                    for page in PAGES:
                        assert _delivery(kernel, month, page)["state"] == "ready"
                assert kernel.reading.run_pending(COMPANY) == ()
            actual = {month: capture(kernel, COMPANY, month) for month in months}
            if baseline is not None:
                assert actual == baseline
            baseline = actual
            result["implementations"][mode] = {"public_projection_sha256": _digest(actual)}
    with TemporaryDirectory(prefix="c14-fresh-", dir=root.parent) as temporary:
        temporary = Path(temporary).resolve()
        assert temporary.parent == root.parent
        portable = package(root, temporary / "baseline.zip", "original")
        for mode in MODES:
            target = temporary / mode
            target.mkdir()
            restore(portable, target, manifest, mode)
            with open_dataset(target, mode) as (kernel, _):
                preparing = []
                for month in months:
                    for page in PAGES:
                        assert _delivery(kernel, month, page)["state"] == "preparing", (
                            "A restored qualification case must have no reusable model",
                            mode,
                            month,
                            page,
                        )
                        preparing.append({"period": month, "page": page})
                generated = kernel.reading.run_pending(COMPANY, limit=20)
                assert generated, "Fresh qualification executed no real producer"
                actual = {month: capture(kernel, COMPANY, month) for month in months}
                assert actual == baseline
                assert kernel.reading.run_pending(COMPANY) == ()
                result["fresh_implementations"][mode] = {
                    "initial_preparing_scopes": preparing,
                    "produced_component_scopes": list(generated),
                    "public_projection_sha256": _digest(actual),
                }
    frontend_after = frontend_identity()
    assert frontend_after == frontend_before
    result["main_template_and_style_sha256"] = frontend_after
    return result
