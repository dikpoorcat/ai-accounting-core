"""Measure the actual build on an explicitly generated Stage 9 synthetic book."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

if __package__:
    from .stage9_source import (
        configure_source,
        require_source_module,
        synthetic_path,
        workspace_root,
    )
else:
    from stage9_source import (
        configure_source,
        require_source_module,
        synthetic_path,
        workspace_root,
    )


def sanitized_browser_stderr(value: str, *, secrets=()) -> str:
    """Keep bounded Node failure diagnostics without persisting tickets or tokens."""
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[redacted]")
    value = re.sub(r"https?://[^\s\"']+", "[browser URL]", value)
    value = re.sub(r"(?i)\b(?:Bearer\s+)[^\s\"']+", "Bearer [redacted]", value)
    value = re.sub(r"(?i)\b(ticket|token|capability|password)\s*[:=]\s*[^\s,\"']+",
                   r"\1=[redacted]", value)
    value = re.sub(r"[A-Za-z]:\\[^\s\"']+", "[local path]", value)
    return value[-2000:]


def wait_for_timing_release(ready_file, start_file, *, output, timeout=900):
    """Let construction stop after preview checks, before launching the timed browser."""
    if ready_file is None:
        return
    if start_file.exists():
        raise ValueError("Timing release must be created after the ready signal")
    signal = {"status": "timing_ready", "pid": os.getpid(), "output": str(output)}
    with ready_file.open("x", encoding="utf-8") as handle:
        json.dump(signal, handle)
    print(json.dumps(signal), flush=True)
    deadline = time.monotonic() + timeout
    while not start_file.is_file():
        if time.monotonic() >= deadline:
            raise TimeoutError("No timing release after the synthetic preview was ready")
        time.sleep(0.2)


def validate_book_report(
    book, *, company_name: str, require_verified=True, require_current_preview=True
) -> str:
    """Reject a checkpoint or a mislabeled partial book before opening a browser."""
    if (require_verified and book.get("status") != "complete") or book.get("company", {}).get(
        "name"
    ) != company_name:
        raise ValueError("Expected the completed synthetic company report")
    if require_verified and (
        book.get("integrity", {}).get("status") != "verified"
        or book["integrity"].get("limitations")
    ):
        raise ValueError("Synthetic book integrity must be verified")
    months, snapshots = book.get("months"), book.get("snapshots")
    requested = book.get("requested_months")
    if not isinstance(months, list) or not isinstance(snapshots, dict):
        raise ValueError("Synthetic book months and snapshots are required")
    if (
        not isinstance(requested, int)
        or requested < 1
        or len(months) != requested
        or len(snapshots) != requested
    ):
        raise ValueError("Synthetic book has not reached requested_months")
    expected_businesses = book.get("monthly_business_count")
    if not isinstance(expected_businesses, int) or expected_businesses < 1:
        raise ValueError("Synthetic monthly business count is invalid")
    periods = [month.get("period") for month in months]
    if len(set(periods)) != requested or set(periods) != set(snapshots):
        raise ValueError("Synthetic month and snapshot periods differ")
    for index, month in enumerate(months):
        snapshot = snapshots[month["period"]]
        if (
            month.get("business_count") != expected_businesses
            or month.get("closed") is not (index < requested - 1)
            or snapshot.get("closed") is not month["closed"]
            or not snapshot.get("owner_confirmation")
            or not snapshot.get("preview_digest")
        ):
            raise ValueError("Synthetic month is incomplete or has an invalid close state")
    if "business_count" in book and book["business_count"] != requested * expected_businesses:
        raise ValueError("Synthetic business count does not match complete months")
    if require_verified and require_current_preview:
        current = book.get("verified_open_preview")
        if (
            not isinstance(current, dict)
            or set(current)
            != {
                "source", "company_id", "database_id", "period",
                "construction_preview_digest", "digest", "epochs", "state",
            }
            or current["source"] != book.get("source")
            or current["company_id"] != book["company"].get("id")
            or current["database_id"] != book["company"].get("database_id")
            or current["period"] != periods[-1]
            or current["construction_preview_digest"]
            != snapshots[periods[-1]]["preview_digest"]
            or not isinstance(current["digest"], str)
            or not current["digest"]
            or not isinstance(current["state"], list)
            or len(current["state"]) != 6
            or any(type(value) is not int for value in current["state"])
            or current["epochs"] != dict(
                zip(
                    ("accounting", "material", "management"),
                    current["state"][1:4],
                    strict=True,
                )
            )
        ):
            raise ValueError("Current synthetic open preview was not independently verified")
    return periods[-1]


def measurement_scope(
    book, *, repeats: int, warmups: int, instrument: bool, navigation_only,
    static_build="release",
):
    """Label raw timing scope without claiming the Stage 9 release has passed."""
    if navigation_only:
        return f"navigation_{navigation_only}"
    if static_build != "release" or instrument or repeats != 30 or warmups < 3:
        return "diagnostic"
    months = book.get("requested_months")
    businesses = book.get("monthly_business_count")
    distribution = book.get("distribution")
    if months in {12, 48, 120} and businesses == 1000:
        if distribution == "mixed_cumulative" and book.get("employee_count") == 50:
            return "main_page_candidate"
        if distribution == "independent_local_pairs" and book.get("registered_object_count") == 50:
            return "independent_page_candidate"
    if (
        distribution == "mixed_cumulative"
        and businesses == 5000
        and book.get("employee_count") == 200
    ):
        return "pressure_diagnostic"
    return "diagnostic"


def prepare_browser_static_runtime():
    """Freeze package-owned static types before loading any synthetic report."""
    from ai_accounting.kernel.daemon import _prepare_static_runtime

    started = time.perf_counter()
    static_runtime = _prepare_static_runtime()
    return static_runtime, (time.perf_counter() - started) * 1000


def prepare_browser_service(root, static_runtime, static_startup_ms):
    """Open the catalog with the exact models prepared by the resident daemon."""
    from ai_accounting.kernel.service import LocalService

    started = time.perf_counter()
    app = LocalService(
        root,
        enable_read_pool=True,
        enable_parallel_brief=True,
        _static_runtime=static_runtime,
    )
    return app, static_startup_ms + (time.perf_counter() - started) * 1000


def browser_source_paths(source, *, static_build="release"):
    """Use the shipped release assets unless a diagnostic explicitly selects dist."""
    if static_build not in {"release", "dist"}:
        raise ValueError("Unknown Stage 9 frontend static build")
    static = (
        source / "src/ai_accounting/static/dashboard"
        if static_build == "release"
        else source / "frontend/dist"
    )
    harness = source / "frontend/tests/browser-stage9-hot-refresh.cjs"
    if not (static / "index.html").is_file() or not harness.is_file():
        raise ValueError(
            f"Selected Stage 9 source needs its {static_build} static build and browser harness"
        )
    return static, harness


def require_report_source(book, source):
    """A verified synthetic report cannot silently switch implementation trees."""
    recorded = book.get("source")
    if not isinstance(recorded, str) or not Path(recorded).is_absolute():
        raise ValueError("Synthetic book report must record its absolute source")
    if Path(recorded).resolve() != source:
        raise ValueError("Synthetic book source differs from selected Stage 9 source")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--book-report", type=Path, required=True)
    parser.add_argument(
        "--switch-book-report",
        type=Path,
        help="second verified synthetic company in the same catalog",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--node", type=Path, required=True)
    parser.add_argument("--playwright-module", type=Path, required=True)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--workspace", type=Path, help="workspace containing the synthetic .tmp")
    parser.add_argument("--browser-channel", default="msedge")
    parser.add_argument(
        "--static-build", choices=("release", "dist"), default="release",
        help="serve shipped release assets; dist is an explicit diagnostic build",
    )
    parser.add_argument("--repeats", type=int, default=30)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--timing-ready-file", type=Path)
    parser.add_argument("--timing-start-file", type=Path)
    parser.add_argument(
        "--navigation-only",
        choices=("cold", "switch"),
        help="measure first-page opening or company switching separately from hot refresh",
    )
    parser.add_argument(
        "--instrument",
        action="store_true",
        help="diagnostic HTTP/dispatch timings; keep separate from acceptance timing",
    )
    args = parser.parse_args()
    if (args.timing_ready_file is None) != (args.timing_start_file is None):
        parser.error("Both timing gate paths must be provided together")
    if args.navigation_only and args.instrument:
        parser.error("navigation-only measurements must not enable HTTP instrumentation")
    if args.navigation_only == "switch" and args.switch_book_report is None:
        parser.error("switch measurement requires --switch-book-report")
    try:
        workspace = workspace_root(Path(__file__).resolve().parents[1], args.workspace)
        book_report = synthetic_path(args.book_report, workspace)
        switch_report = (
            synthetic_path(args.switch_book_report, workspace)
            if args.switch_book_report is not None
            else None
        )
        output = synthetic_path(args.output, workspace)
        gate_paths = [
            synthetic_path(path, workspace)
            for path in (args.timing_ready_file, args.timing_start_file) if path is not None
        ]
        if gate_paths and (
            len(set(gate_paths + [book_report, output])) != 4
            or any(path.exists() for path in gate_paths)
        ):
            raise ValueError("Timing gate paths must be distinct, new synthetic files")
        source = configure_source(args.source, workspace)
    except ValueError as exc:
        parser.error(str(exc))
    if Path(sys.prefix).resolve() != workspace / ".tmp-kernel-venv":
        parser.error("Use workspace virtual environment")

    import stage9_book
    import stage9_independent_book

    import ai_accounting

    require_source_module(ai_accounting, source, "src/ai_accounting/__init__.py")
    require_source_module(stage9_book, source, "tests/kernel/stage9_book.py")
    require_source_module(
        stage9_independent_book, source, "tests/kernel/stage9_independent_book.py"
    )

    from pydantic import SecretStr

    from ai_accounting.kernel.daemon import ServiceClient, build_native_security_controller
    from ai_accounting.kernel.http import create_server
    from ai_accounting.kernel.security.credentials import InMemoryCredentialStore
    from ai_accounting.kernel.security.transport import NativeHttpClient

    static_runtime, static_startup_ms = prepare_browser_static_runtime()
    book = json.loads(book_report.read_text(encoding="utf-8"))
    require_report_source(book, source)
    root = Path(book["root"]).resolve()
    synthetic_path(root, workspace)
    expected_name = (
        "阶段九合成独立业务企业"
        if book.get("distribution") == "independent_local_pairs"
        else "阶段九合成规模企业"
    )
    selected_period = validate_book_report(book, company_name=expected_name)
    switch_book = None
    if switch_report is not None:
        switch_book = json.loads(switch_report.read_text(encoding="utf-8"))
        require_report_source(switch_book, source)
        switch_period = validate_book_report(switch_book, company_name="阶段九合成切换企业")
        assert Path(switch_book["root"]).resolve() == root, (
            "Switch company must share this synthetic catalog"
        )
        assert switch_book["primary_company_id"] == book["company"]["id"]
        assert switch_book["company"]["id"] != book["company"]["id"]
    static, harness = browser_source_paths(source, static_build=args.static_build)
    assert not output.exists(), "Preserve previous raw measurements"

    def build_hashes():
        return {
            str(path.relative_to(static)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(static.rglob("*"))
            if path.is_file()
        }

    hashes = build_hashes()
    app, startup_ms = prepare_browser_service(root, static_runtime, static_startup_ms)
    companies_by_id = {company["id"]: company for company in app.catalog.companies()}
    company = companies_by_id[book["company"]["id"]]
    assert company["id"] == book["company"]["id"]
    assert company["database_id"] == book["company"]["database_id"]
    assert len(companies_by_id) == (2 if switch_book else 1), (
        "Unexpected companies in benchmark catalog"
    )
    if switch_book:
        switch_company = companies_by_id[switch_book["company"]["id"]]
        assert switch_company["database_id"] == switch_book["company"]["database_id"]
    # Test-only identity, used solely within the generated synthetic catalog.
    with app.catalog.connection(read_only=True) as connection:
        owners = [row[0] for row in connection.execute("SELECT login_name FROM security_owner")]
    if not owners:
        password = SecretStr("Synthetic-stage9-browser-only-2026")
        app.security.provision("synthetic-stage9-owner", password)
    elif owners == ["synthetic-stage9-owner"]:
        password = SecretStr("Synthetic-stage9-browser-only-2026")
    elif owners == ["synthetic-stage9-native-profile"]:
        password = SecretStr("Synthetic-stage9-native-profile-only-2026")
    else:
        raise ValueError("Browser benchmark only accepts known synthetic owner identities")
    store = InMemoryCredentialStore()
    server, capability = create_server(app, port=0, static_directory=static)
    http_requests, dispatch_calls = [], []
    records_lock = threading.Lock()
    phase = ["setup"]
    timing_origin = time.perf_counter()
    handler = server.RequestHandlerClass
    original_send_response = handler.send_response

    def record_status(self, status, message=None):
        self._benchmark_status = status
        return original_send_response(self, status, message)

    if args.instrument:
        handler.send_response = record_status
    for method in ("do_GET", "do_POST") if args.instrument else ():
        original = getattr(handler, method)

        def observed(self, _original=original, _method=method):
            start = time.perf_counter()
            self._benchmark_status = None
            try:
                return _original(self)
            finally:
                end = time.perf_counter()
                with records_lock:
                    http_requests.append(
                        {
                            "phase": phase[0],
                            "method": _method[3:],
                            "path": urlsplit(self.path).path,
                            "status": self._benchmark_status,
                            "start_ms": (start - timing_origin) * 1000,
                            "end_ms": (end - timing_origin) * 1000,
                            "milliseconds": (end - start) * 1000,
                        }
                    )

        setattr(handler, method, observed)
    original_dispatch = app.dispatch

    def observed_dispatch(command, payload, **kwargs):
        start = time.perf_counter()
        status = "succeeded"
        try:
            return original_dispatch(command, payload, **kwargs)
        except BaseException as exc:
            status = getattr(exc, "code", type(exc).__name__)
            raise
        finally:
            end = time.perf_counter()
            with records_lock:
                dispatch_calls.append(
                    {
                        "phase": phase[0],
                        "command": command,
                        "status": status,
                        "start_ms": (start - timing_origin) * 1000,
                        "end_ms": (end - timing_origin) * 1000,
                        "milliseconds": (end - start) * 1000,
                    }
                )

    if args.instrument:
        app.dispatch = observed_dispatch
    app.security_controller = build_native_security_controller(
        app,
        server,
        capability,
        credential_store=store,
        window_opener=lambda _request_id: None,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    native = NativeHttpClient(
        port=server.server_port,
        capability=capability,
        catalog_instance_id=app.security.catalog_instance_id,
    )
    result = {"status": "failed"}
    try:
        request = app.security_controller.request(kind="login")
        native.call("native_execute", request["request_id"], password=password)
        native.call("native_update", request["request_id"], status="succeeded")
        token = store.load_session_token()

        def selected_company(current_book, current_company, period):
            snapshot = current_book["snapshots"][period]
            current = current_book["verified_open_preview"]
            assert snapshot["closed"] is False, "Selected benchmark month must remain open"
            with app.catalog.bind(current_company["id"]).connection(read_only=True) as connection:
                connection.execute("BEGIN")
                state = connection.execute("SELECT * FROM state WHERE id=1").fetchone()
                identity = connection.execute(
                    "SELECT company_id,taxpayer_id,database_id FROM identity WHERE id=1"
                ).fetchone()
                assert state is not None and list(state) == current["state"], (
                    "Synthetic company state or repair revision changed after its verified report"
                )
                assert identity is not None and dict(identity) == {
                    "company_id": current_company["id"],
                    "taxpayer_id": current_company["taxpayer_id"],
                    "database_id": current_company["database_id"],
                }, "Synthetic company identity changed after its verified report"
            preview = app.dispatch(
                "preview_close",
                {
                    "company_id": current_company["id"],
                    "period": period,
                    "owner_confirmation": snapshot["owner_confirmation"],
                },
                session_token=token,
            )
            assert preview["status"] == "preview" and preview["manifest"]["period"] == period
            assert preview["digest"], "Selected month requires an actual valid close preview"
            assert preview["digest"] == current["digest"] and preview["epochs"] == current[
                "epochs"
            ], (
                "Synthetic close preview changed after its current-source verification"
            )
            return {
                **current_company,
                "period": period,
                "state": "prepared",
                "preview_digest": preview["digest"],
            }

        primary_selection = selected_company(book, company, selected_period)
        selections = [primary_selection]
        if switch_book:
            selections.append(selected_company(switch_book, switch_company, switch_period))
        metadata = {
            "protocol": 2,
            "database_format": app.catalog.database_format(),
            "pid": os.getpid(),
            "port": server.server_port,
            "capability": capability,
            "catalog_id": app.security.catalog_instance_id,
            "build_id": server.build_id,
        }
        client = ServiceClient(root, metadata=metadata, credential_store=store)
        wait_for_timing_release(
            args.timing_ready_file, args.timing_start_file, output=output
        )
        config = {
            "origin": f"http://127.0.0.1:{server.server_port}",
            "ticket_url": client.browser_url()["url"],
            "companies": selections,
            "channel": args.browser_channel,
            "playwright_module": str(args.playwright_module.resolve()),
            "repeats": args.repeats,
            "warmups": args.warmups,
            "navigation_only": args.navigation_only,
        }
        phase[0] = "browser"
        browser_started_wall_ms = time.time() * 1000
        process = subprocess.run(
            [
                str(args.node.resolve()),
                str(harness),
            ],
            input=json.dumps(config, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=7200,
        )
        # The browser runner sanitizes errors. Never persist ticket-bearing input.
        diagnostic = sanitized_browser_stderr(
            process.stderr,
            secrets=(config["ticket_url"], token.get_secret_value(), password.get_secret_value()),
        )
        try:
            result = json.loads(process.stdout) if process.stdout else None
        except json.JSONDecodeError:
            result = None
        if not isinstance(result, dict):
            result = {
                "status": "failed", "message": "Browser runner produced no valid report",
                "returncode": process.returncode, "node_stderr_sanitized": diagnostic,
            }
        elif process.returncode and result.get("status") == "passed":
            result = {**result, "status": "failed", "returncode": process.returncode,
                      "node_stderr_sanitized": diagnostic}
        elif process.returncode and diagnostic:
            result["node_stderr_sanitized"] = diagnostic
        navigation = result.get("navigation")
        if isinstance(navigation, dict) and isinstance(
            navigation.get("first_render_epoch_ms"), (int, float)
        ):
            navigation["browser_launch_to_first_render_ms"] = (
                navigation["first_render_epoch_ms"] - browser_started_wall_ms
            )
        phase[0] = "teardown"
        assert hashes == build_hashes(), "Build changed during timing"
    finally:
        token = store.load_session_token()
        if token is not None:
            app.security.logout(token)
        store.delete_session_token()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        app.close()
        result.update(
            synthetic_root=str(root),
            source=str(source),
            measurement_mode=(
                f"navigation_{args.navigation_only}"
                if args.navigation_only
                else "instrumented_diagnostic"
                if args.instrument
                else "page_timing"
            ),
            sample_scope=measurement_scope(
                book,
                repeats=args.repeats,
                warmups=args.warmups,
                instrument=args.instrument,
                navigation_only=args.navigation_only,
                static_build=args.static_build,
            ),
            backend_build_id=server.build_id,
            startup_ms=startup_ms,
            build_assets_sha256=hashes,
            frontend_static_build=args.static_build,
            frontend_asset_directory=str(static),
            synthetic_credentials_revoked=True,
            employee_count=book["employee_count"],
            registered_object_count=book.get("registered_object_count"),
            monthly_business_count=book["monthly_business_count"],
            business_count=book.get("business_count"),
            distribution=book.get("distribution"),
            month_count=len(book["snapshots"]),
            default_page_limit=100,
            http_requests=http_requests,
            dispatch_calls=dispatch_calls,
        )
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "output": str(output)}))
    if result["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
