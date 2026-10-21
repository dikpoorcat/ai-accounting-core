"""Use the existing Vue/DOM harness with the new resident and synthetic catalog.

No old kernel or launch configuration participates. The existing harness already
validates response contracts, complete visible content and two continuous frames;
its real 500ms refresh / 600ms interaction endpoints are reused without a copy.
"""

import json
import subprocess
from contextlib import nullcontext
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from ai_accounting_modular.host.runtime.public import create_runtime

from .._support import COMPANY, ready
from .browser_progress import recover
from .dataset import DATABASE, NAME, PASSWORD, describe, fixed_clock
from .ledger_variant import install_ledger_implementation
from .measurements import BUDGETS, SAMPLES, WARMUPS, summarize
from .projections import PAGES, parameters

WORKSPACE = Path(__file__).resolve().parents[4]
HARNESS = WORKSPACE / "frontend/tests/browser-stage9-hot-refresh.cjs"
REQUIRED_INTERACTIONS = frozenset(
    [
        "employees_filter_in_period",
        "employees_filter_all",
        "employees_exact_target",
        "assets_filter_fixed",
        "assets_filter_all",
        "assets_exact_target",
        "funds_view_bank",
        "funds_view_book",
        "funds_account_1",
        "funds_account_2",
        "brief_activity_selected_category_complete",
        "brief_open_items_selected_category_complete",
        "employees_expanded_detail_refresh",
        "brief_vouchers_next_page",
        "brief_vouchers_all_complete",
        "brief_voucher_exact_target",
        "brief_activity_members_first_open",
        "brief_open_items_members_first_open",
        "brief_business_progress_first_open",
        "employees_labor_sources_first_open",
        "employees_labor_payments_first_open",
        "funds_bank_batch_first_open",
    ]
    + [
        f"reports_{index}_{mode}"
        for index in range(1, 4)
        for mode in ("statement", "template_on", "template_off")
    ]
)


def run(
    root,
    mode,
    *,
    node,
    playwright_module,
    static_directory=None,
    interactions=False,
    probe=False,
    concurrent=False,
    _concurrent_work=None,
):
    if concurrent:
        from .concurrency import prepare

        with prepare(root, mode) as prepared:
            return run(
                prepared.root,
                mode,
                node=node,
                playwright_module=playwright_module,
                static_directory=static_directory,
                interactions=interactions,
                probe=probe,
                _concurrent_work=prepared.during,
            )
    root = Path(root).resolve()
    manifest = describe(root)
    if not probe:
        assert manifest["official_scale"] is True
    static = (
        Path(static_directory)
        if static_directory is not None
        else WORKSPACE / "src/ai_accounting_modular/static/dashboard"
    ).resolve()
    assert (static / "index.html").is_file(), "An existing release build is required"
    with install_ledger_implementation(mode), fixed_clock(manifest) as clock:
        runtime = create_runtime(
            root / "catalog.sqlite3", static_directory=static, window_opener=lambda _: None
        )
        try:
            runtime.kernel.dashboard.now = clock.now
            with runtime.kernel.storage.read_unit(COMPANY) as read:
                metadata = runtime.kernel.storage.company_metadata(read)
                assert metadata.database_id == DATABASE and metadata.legal_name == NAME
                assert metadata.company_root.resolve() == root / "company"
            for page in PAGES:
                ready(runtime.kernel, page, parameters(COMPANY, manifest["period_to"], page))
            runtime.start()
            window = runtime.security.request({"kind": "login"})
            assert runtime.security.native(
                "execute", {"request_id": window["request_id"], "password": PASSWORD}
            )["login_completed"]
            config = {
                "origin": f"http://127.0.0.1:{runtime.server.server_port}",
                "playwright_module": str(Path(playwright_module).resolve()),
                "channel": "msedge",
                "companies": [
                    {"id": COMPANY, "period": manifest["period_to"], "state": "prepared"}
                ],
                "warmups": 1 if probe else WARMUPS,
                "repeats": 1 if probe else SAMPLES,
                "refresh_target_ms": BUDGETS["hot_refresh"]["every_lt_ms"],
                "interaction_only": interactions,
            }
            work = _concurrent_work(runtime) if _concurrent_work else nullcontext(None)
            with work as concurrent_evidence:
                # Concurrent fixture setup can outlive a short browser ticket.
                # Issue it only when the actual browser process is about to start.
                config["ticket_url"] = runtime.browser_url()
                result, returncode, timed_out = _execute(
                    node, config, root, interactions=interactions
                )
            result = _without_secrets(result, config)
            group = result.get("interactions", {}) if interactions else result.get("pages", {})
            if concurrent_evidence is not None:
                from .concurrency import qualify_overlap

                attempts = result.get("refresh_attempts") if not interactions else None
                if attempts is None:
                    attempts = [
                        item for value in group.values() for item in value.get("attempts", ())
                    ]
                concurrent_evidence["overlap"] = qualify_overlap(concurrent_evidence, attempts)
            budget = BUDGETS["local_interaction" if interactions else "hot_refresh"]
            summaries = {
                name: summarize(item.get("samples", []), budget, expected=1 if probe else SAMPLES)
                for name, item in group.items()
            }
            complete = bool(summaries) and (
                REQUIRED_INTERACTIONS <= summaries.keys()
                if interactions
                else set(summaries) == {"brief", "funds", "employees", "assets", "reports"}
            )
            passed = (
                complete
                and returncode == 0
                and result.get("status") == "passed"
                and (concurrent_evidence is None or concurrent_evidence.get("status") == "passed")
                and (
                    concurrent_evidence is None
                    or concurrent_evidence["overlap"].get("status") == "passed"
                )
                and all(item["status"] == "passed" for item in summaries.values())
            )
            return {
                "format": "modular-c14-browser/1",
                "status": "passed" if passed else "failed",
                "official": not probe,
                "measurement": "Vue DOM plus two continuous stable frames",
                "mode": mode,
                "scale_months": manifest["months"],
                "employees": manifest["employees"],
                "contractors": manifest["contractors"],
                "total_people": manifest["people"],
                "harness_sha256": sha256(HARNESS.read_bytes()).hexdigest(),
                "summaries": summaries,
                "result": result,
                "returncode": returncode,
                "process_timeout": timed_out,
                "concurrent_work": concurrent_evidence,
            }
        finally:
            runtime.close()


def _execute(node, config, root, *, interactions):
    # A new private temporary directory keeps the ticket only in stdin/memory.
    # JSONL contains attempt facts after measurement, never config or capability.
    with TemporaryDirectory(prefix="c14-browser-progress-", dir=root.parent) as temporary:
        temporary = Path(temporary).resolve()
        assert temporary.parent == root.parent
        progress = temporary / "attempts.jsonl"
        config = config | {"progress_path": str(progress)}
        timed_out = False
        try:
            completed = subprocess.run(
                [str(node), str(HARNESS)],
                input=json.dumps(config),
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=7200,
                check=False,
            )
            raw, returncode = completed.stdout, completed.returncode
        except subprocess.TimeoutExpired as error:
            timed_out, raw, returncode = True, error.stdout or "", None
        # Never relay raw stdout/stderr; parse known reports and redact tickets.
        try:
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError("Browser output is not a report object")
        except (ValueError, TypeError):
            result = recover(
                progress,
                sorted(REQUIRED_INTERACTIONS)
                if interactions
                else ("brief", "funds", "employees", "assets", "reports"),
                interactions=interactions,
                warmups=config["warmups"],
                samples=config["repeats"],
            )
            result["failure_phase"] = "browser_timeout" if timed_out else "browser_process"
        return result, returncode, timed_out


def _without_secrets(value, config):
    if isinstance(value, dict):
        return {key: _without_secrets(item, config) for key, item in value.items()}
    if isinstance(value, list):
        return [_without_secrets(item, config) for item in value]
    if isinstance(value, str):
        return (
            value.replace(config["ticket_url"], "[synthetic ticket]")
            .replace(config["ticket_url"].split("#", 1)[-1], "[synthetic ticket]")
            .replace(config["ticket_url"].rsplit("=", 1)[-1], "[synthetic ticket]")
        )
    return value
