"""Measure verified in-database evidence, portable backup and HTTP foreground reads.

Run with the repository virtual environment. Every run creates a new synthetic
company below .tmp; no existing database or archive is opened for writing.
Use --size-mib 240 and 1536 separately, with --profile mixed, compressible or
incompressible. The report records actual unique evidence bytes and ZIP size.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import http.client
import json
import os
import platform
import random
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
from collections import defaultdict
from contextlib import closing
from functools import cache
from pathlib import Path
from urllib.parse import urlencode

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

MIB = 1024 * 1024
REPOSITORY = Path(__file__).resolve().parents[1]


@cache
def _windows_memory_api():
    """Create ctypes API wrappers once; each sample still queries live RSS."""
    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("faults", ctypes.c_ulong)] + [
            (name, ctypes.c_size_t)
            for name in (
                "peak", "working", "peak_paged", "paged", "peak_nonpaged",
                "nonpaged", "pagefile", "peak_pagefile",
            )
        ]

    kernel = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    psapi = ctypes.WinDLL("Psapi.dll", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
    return (Counters, kernel.GetCurrentProcess(), psapi.GetProcessMemoryInfo,
            kernel.OpenProcess, kernel.CloseHandle)


def _windows_rss(handle):
    counters_type, _, get_memory, _, _ = _windows_memory_api()
    counters = counters_type()
    counters.cb = ctypes.sizeof(counters)
    if not get_memory(handle, ctypes.byref(counters), counters.cb):
        raise ctypes.WinError()
    return counters.working


def _linux_rss(pid):
    with Path(f"/proc/{pid}/statm").open(encoding="ascii") as stream:
        return int(stream.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")


def rss_bytes():
    if os.name == "nt":
        return _windows_rss(_windows_memory_api()[1])
    if sys.platform.startswith("linux"):
        return _linux_rss("self")
    import resource

    unit = 1 if sys.platform == "darwin" else 1024
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * unit


class BriefWorkerMemory:
    """Live RSS of the three known pool children, with handles opened only once."""

    def __init__(self, pool):
        self.pids = tuple(process.pid for process in pool._pool)
        if len(self.pids) != 3 or any(not isinstance(pid, int) or pid <= 0 for pid in self.pids):
            raise AssertionError("Expected three live brief worker PIDs")
        self.handles = {}
        if os.name == "nt":
            _, _, _, open_process, close_handle = _windows_memory_api()
            try:
                for pid in self.pids:
                    # PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ.
                    handle = open_process(0x1000 | 0x0010, False, pid)
                    if not handle:
                        raise ctypes.WinError()
                    self.handles[pid] = handle
            except BaseException:
                for handle in self.handles.values():
                    close_handle(handle)
                raise
        elif not sys.platform.startswith("linux"):
            raise OSError("Brief worker RSS requires Windows or Linux")

    def sample(self):
        if os.name == "nt":
            return {pid: _windows_rss(self.handles[pid]) for pid in self.pids}
        return {pid: _linux_rss(pid) for pid in self.pids}

    def close(self):
        if os.name == "nt":
            close_handle = _windows_memory_api()[4]
            for handle in self.handles.values():
                close_handle(handle)
            self.handles.clear()


def update_memory_peaks(peaks, phase, parent_rss, worker_rss):
    """Capture a concurrent sum, never a sum of independent per-process peaks."""
    item = peaks[phase]
    item["parent"] = max(item["parent"], parent_rss)
    for pid, rss in worker_rss.items():
        item["workers"][pid] = max(item["workers"].get(pid, 0), rss)
    item["sum"] = max(item["sum"], parent_rss + sum(worker_rss.values()))


def finalize_monitor_result(report, foreground_errors, memory_errors):
    """A late monitor failure invalidates a completed business operation report."""
    if not foreground_errors and not memory_errors:
        return None
    failures = []
    if foreground_errors:
        failures.append(f"foreground: {foreground_errors}")
    if memory_errors:
        failures.append(f"rss: {memory_errors}")
    message = "; ".join(failures)
    report["status"] = "failed"
    report.setdefault("error", {"type": "MonitorFailure", "message": message})
    return message


def payload(seed, index, length, profile):
    if profile == "compressible":
        pattern = hashlib.sha256(f"stage9:{seed}:{index}".encode()).digest()
        return (pattern * (length // len(pattern) + 1))[:length]
    return random.Random((seed << 32) + index).randbytes(length)


def evidence_totals(engine):
    with engine.store.connection(read_only=True) as connection:
        count, distinct, size = connection.execute(
            "SELECT count(*), count(DISTINCT digest), coalesce(sum(length(content)),0) "
            "FROM evidence WHERE name LIKE 'stage9-evidence-%'"
        ).fetchone()
    return {"count": count, "distinct_digests": distinct, "content_bytes": size}


def summary(values):
    if not values:
        return {"samples": 0, "milliseconds": []}
    sorted_values = sorted(values)
    return {
        "samples": len(values),
        "milliseconds": list(values),
        "median_ms": statistics.median(sorted_values),
        "p95_ms": sorted_values[(95 * len(values) + 99) // 100 - 1],
        "max_ms": sorted_values[-1],
    }


def foreground_scope(mode="http"):
    return {
        "entry": "browser_brief_refresh" if mode == "browser" else "GET /api/dashboard/brief",
        "limit": 100,
        "preparation": "complete",
        "includes_http": True,
        "includes_browser_rendering": mode == "browser",
        "purpose": f"evidence_operations_default_brief_{mode}_contention",
    }


def read_default_brief_http(port, token, company_id, period):
    query = urlencode({
        "company_id": company_id,
        "period": period,
        "limit": 100,
        "preparation": "complete",
    })
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request(
            "GET", "/api/dashboard/brief?" + query,
            headers={"Authorization": "Bearer " + token},
        )
        response = connection.getresponse()
        body = response.read()
        if response.status != 200:
            raise AssertionError(f"Foreground HTTP brief failed: status {response.status}")
        result = json.loads(body)
        if result.get("data") is None or result.get("selected_period", {}).get("key") != period:
            raise AssertionError("Foreground HTTP brief returned the wrong period")
        return len(body)
    finally:
        connection.close()


def source_inventory(source):
    manifest_path = source / "source-manifest.json"
    if not manifest_path.is_file():
        return {"status": "unsealed_working_tree"}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = manifest.get("files")
    if (
        manifest.get("status") != "complete"
        or Path(manifest.get("target", "")).resolve() != source
        or not isinstance(files, dict)
        or manifest.get("file_count") != len(files)
    ):
        raise ValueError("Incomplete Stage 9 source manifest")
    listing = json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if hashlib.sha256(listing).hexdigest() != manifest.get("sha256"):
        raise ValueError("Stage 9 source manifest digest changed")
    for name, expected in files.items():
        path = (source / name).resolve()
        if not path.is_relative_to(source) or not path.is_file():
            raise ValueError("Stage 9 source manifest path is missing or escapes source")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise ValueError("Stage 9 source file changed after snapshot")
    return {"status": "verified", "sha256": manifest["sha256"], "file_count": len(files)}


def run(root, output, *, size_mib, profile, seed=9, chunk_mib=20, source=None,
        workspace=None, foreground="http", node=None, playwright_module=None,
        browser_channel="msedge"):
    workspace = workspace_root(REPOSITORY, workspace)
    if Path(sys.prefix).resolve() != workspace / ".tmp-kernel-venv":
        raise ValueError("Use workspace virtual environment")
    root, output = synthetic_path(root, workspace), synthetic_path(output, workspace)
    temporary = workspace / ".tmp"
    if root.exists() or output.exists():
        raise ValueError("Use absent synthetic root and report paths")
    if not 1 <= size_mib <= 1536 or not 1 <= chunk_mib <= 20:
        raise ValueError("Size and per-file chunk must respect the 20 MiB evidence limit")
    if profile not in {"mixed", "compressible", "incompressible"}:
        raise ValueError("Unknown evidence profile")
    if foreground not in {"http", "browser"}:
        raise ValueError("Foreground must be http or browser")
    if foreground == "browser" and (
        node is None or playwright_module is None
        or not Path(node).is_file() or not Path(playwright_module).exists()
    ):
        raise ValueError("Browser foreground requires --node and --playwright-module")
    required = size_mib * MIB * (4 if profile != "compressible" else 3) + 512 * MIB
    if __import__("shutil").disk_usage(temporary).free < required:
        raise ValueError("Insufficient temporary disk space for database, ZIP and restored copy")

    source = configure_source(Path(source) if source is not None else REPOSITORY, workspace)
    inventory = source_inventory(source)
    import ai_accounting

    require_source_module(ai_accounting, source, "src/ai_accounting/__init__.py")
    from ai_accounting.kernel.daemon import _prepare_static_runtime

    static_runtime = _prepare_static_runtime()
    import stage9_book

    require_source_module(stage9_book, source, "tests/kernel/stage9_book.py")
    from pydantic import SecretStr
    from stage9_book import MixedBook, month_at

    from ai_accounting.kernel import backup
    from ai_accounting.kernel.http import create_server
    from ai_accounting.kernel.service import LocalService
    from ai_accounting.kernel.versions import database_format

    output.parent.mkdir(parents=True, exist_ok=True)
    book = MixedBook(root, employees=2, businesses=40)
    book.add_month(0, close=False)
    open_snapshot = book.snapshots[month_at(0)]
    database = book.engine.store.path
    with book.engine.store.connection(read_only=True) as connection:
        company_format = database_format(connection, bundle=book.engine.store.bundle)
    report = {
        "status": "populating",
        "source": str(source),
        "source_inventory": inventory,
        "measurement_harness": {
            "path": str(Path(__file__).resolve()),
            "sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "separate_from_fixed_source": Path(__file__).resolve() != (
                source / "scripts/benchmark_stage9_evidence.py"
            ).resolve(),
        },
        "workspace": str(workspace),
        "root": str(root),
        "database": str(database),
        "size_mib_requested": size_mib,
        "chunk_mib": chunk_mib,
        "profile": profile,
        "seed": seed,
        "python": sys.version.split()[0],
        "sqlite": sqlite3.sqlite_version,
        "platform": platform.platform(),
        "format": book.catalog.database_format(),
        "company_format": company_format,
        "company": book.company,
        "phases": {},
        "foreground": {"scope": foreground_scope(foreground)},
        "memory_scope": {
            "processes": "benchmark_parent_and_three_known_brief_worker_pids",
            "sampling_interval_ms": 10,
            "total_peak": (
                "maximum_of_parent_plus_workers_at_each_sample_"
                "not_sum_of_independent_peaks"
            ),
            "browser_process_included": False,
        },
        "static_runtime_prepared_before_business_root": static_runtime is not None,
    }

    def write():
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    phase = ["populate"]
    peaks = defaultdict(lambda: {"parent": 0, "workers": {}, "sum": 0})
    memory_lock = threading.Lock()
    worker_memory = [None]
    memory_errors = []
    stop_memory = threading.Event()

    def capture_memory(name):
        workers = worker_memory[0].sample() if worker_memory[0] is not None else {}
        parent = rss_bytes()
        with memory_lock:
            update_memory_peaks(peaks, name, parent, workers)

    def sample_memory():
        while not stop_memory.wait(0.01):
            try:
                capture_memory(phase[0])
            except Exception as error:
                memory_errors.append(f"{type(error).__name__}: {error}")
                stop_memory.set()

    memory_thread = threading.Thread(target=sample_memory, daemon=True)
    memory_thread.start()

    def measured(name, operation):
        phase[0] = name
        before_parent = rss_bytes()
        before_workers = worker_memory[0].sample() if worker_memory[0] else {}
        with memory_lock:
            update_memory_peaks(peaks, name, before_parent, before_workers)
        started = time.perf_counter()
        try:
            return operation()
        finally:
            elapsed = time.perf_counter() - started
            capture_memory(name)
            sampled = peaks[name]
            report["phases"][name] = {
                "seconds": elapsed,
                "rss_before_bytes": before_parent,
                "sampled_peak_rss_bytes": sampled["parent"],
                "sampled_peak_brief_worker_rss_by_pid_bytes": sampled["workers"],
                "sampled_peak_parent_plus_brief_workers_rss_bytes": sampled["sum"],
            }
            phase[0] = "idle"
            write()

    foreground_samples = defaultdict(list)
    foreground_errors = []
    stop_foreground = threading.Event()
    browser_ready = threading.Event()
    browser_finished = threading.Event()
    browser_passed = threading.Event()

    def read_dashboard():
        while not stop_foreground.wait(0.1):
            name = phase[0]
            if name not in {"verify", "backup", "restore", "restored_verify"}:
                continue
            started = time.perf_counter()
            try:
                read_default_brief_http(server.server_port, token, identity, month_at(0))
                foreground_samples[name].append((time.perf_counter() - started) * 1000)
            except Exception as error:
                foreground_errors.append(
                    {"phase": name, "type": type(error).__name__, "message": str(error)}
                )
                stop_foreground.set()

    def read_browser():
        try:
            for line in browser_process.stdout:
                message = json.loads(line)
                if message.get("kind") == "ready":
                    if (message.get("company_id"), message.get("period")) != (
                        identity, month_at(0)
                    ):
                        raise AssertionError("Browser selected a different company or period")
                    browser_ready.set()
                elif message.get("kind") == "sample":
                    elapsed = message.get("elapsed_ms")
                    if type(elapsed) not in (float, int) or elapsed < 0:
                        raise AssertionError("Invalid browser foreground sample")
                    foreground_samples[phase[0]].append(elapsed)
                elif message.get("status") == "passed":
                    browser_passed.set()
                else:
                    raise AssertionError(f"Browser foreground failed: {message}")
        except Exception as error:
            foreground_errors.append(
                {"phase": phase[0], "type": type(error).__name__, "message": str(error)}
            )
        finally:
            browser_ready.set()
            browser_finished.set()

    primary_error = False
    try:
        counts = {"compressible": 0, "incompressible": 0}
        bytes_by_profile = {"compressible": 0, "incompressible": 0}
        digests = set()

        def populate():
            for index, offset in enumerate(range(0, size_mib, chunk_mib)):
                length = min(chunk_mib, size_mib - offset) * MIB
                kind = (
                    ("compressible" if index % 2 == 0 else "incompressible")
                    if profile == "mixed"
                    else profile
                )
                content = payload(seed, index, length, kind)
                result = book.engine.register_evidence(
                    content,
                    "application/octet-stream",
                    f"stage9-evidence-{kind}-{index:04d}.bin",
                    request_id=f"stage9-evidence-{seed}-{index:04d}",
                )
                if result["digest"] in digests:
                    raise AssertionError("Duplicate evidence digest would understate stored bytes")
                digests.add(result["digest"])
                counts[kind] += 1
                bytes_by_profile[kind] += length
                del content
                report["evidence"] = {
                    "files": sum(counts.values()),
                    "counts": counts,
                    "bytes_by_profile": bytes_by_profile,
                    "registered_bytes": sum(bytes_by_profile.values()),
                }
                write()

        measured("populate", populate)
        stored = evidence_totals(book.engine)
        if stored != {
            "count": sum(counts.values()),
            "distinct_digests": len(digests),
            "content_bytes": size_mib * MIB,
        }:
            raise AssertionError(f"Stored evidence differs from generated content: {stored}")
        report["evidence"].update(stored)
        report["database_bytes"] = database.stat().st_size
        report["database_wal_bytes"] = (
            Path(str(database) + "-wal").stat().st_size
            if Path(str(database) + "-wal").exists()
            else 0
        )
        identity = book.company["id"]
        book_company_name = book.company["name"]
        del populate
        book = None  # Do not retain fixture objects during the foreground read loop.
        app = LocalService(
            root, enable_read_pool=True, enable_parallel_brief=True,
            _static_runtime=static_runtime,
        )
        if app.brief_parallel is None:
            raise AssertionError("Evidence foreground needs the resident brief worker pool")
        worker_memory[0] = BriefWorkerMemory(app.brief_parallel.pool)
        report["brief_worker_pids"] = list(worker_memory[0].pids)
        password = SecretStr("Synthetic-stage9-evidence-only-2026")
        app.security.provision("synthetic-stage9-evidence-owner", password)
        token = app.security.login(
            "synthetic-stage9-evidence-owner", password
        ).session_token.get_secret_value()
        static = source / "src/ai_accounting/static/dashboard"
        if not (static / "index.html").is_file():
            raise ValueError("Selected Stage 9 source lacks its released frontend assets")
        server, _capability = create_server(app, port=0, static_directory=static)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        report["foreground"]["parallel_brief_enabled"] = app.brief_parallel is not None
        if foreground == "browser":
            from ai_accounting.kernel.daemon import (
                ServiceClient,
                build_native_security_controller,
            )
            from ai_accounting.kernel.security.credentials import InMemoryCredentialStore

            preview = app.dispatch(
                "preview_close",
                {"company_id": identity, "period": month_at(0),
                 "owner_confirmation": open_snapshot["owner_confirmation"]},
                session_token=SecretStr(token),
            )
            if preview["status"] != "preview" or preview["manifest"]["period"] != month_at(0):
                raise AssertionError("Synthetic browser month has no current close preview")
            store = InMemoryCredentialStore()
            store.save_session_token(SecretStr(token))
            app.security_controller = build_native_security_controller(
                app, server, _capability, credential_store=store,
                window_opener=lambda _request_id: None,
            )
            metadata = {
                "protocol": 2, "database_format": app.catalog.database_format(),
                "pid": os.getpid(), "port": server.server_port,
                "capability": _capability, "catalog_id": app.security.catalog_instance_id,
                "build_id": server.build_id,
            }
            client = ServiceClient(root, metadata=metadata, credential_store=store)
            browser_stop_file = root / "stop-browser-foreground"
            harness = REPOSITORY / "frontend/tests/browser-stage9-hot-refresh.cjs"
            if not harness.is_file():
                raise ValueError("Working tree lacks the browser refresh measurement harness")
            report["foreground"]["browser_harness"] = {
                "path": str(harness),
                "sha256": hashlib.sha256(harness.read_bytes()).hexdigest(),
                "separate_from_fixed_source": harness.resolve() != (
                    source / "frontend/tests/browser-stage9-hot-refresh.cjs"
                ).resolve(),
            }
            config = {
                "origin": f"http://127.0.0.1:{server.server_port}",
                "ticket_url": client.browser_url()["url"],
                "companies": [{"id": identity, "name": book_company_name,
                               "period": month_at(0), "state": "prepared",
                               "preview_digest": preview["digest"]}],
                "channel": browser_channel,
                "playwright_module": str(Path(playwright_module).resolve()),
                "foreground_stream": True,
                "stop_file": str(browser_stop_file),
            }
            browser_process = subprocess.Popen(
                [str(Path(node).resolve()), str(harness)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", bufsize=1,
            )
            browser_process.stdin.write(json.dumps(config, ensure_ascii=False))
            browser_process.stdin.close()
            reader = threading.Thread(target=read_browser, daemon=True)
            reader.start()
            if (
                not browser_ready.wait(60) or foreground_errors
                or browser_process.poll() is not None
            ):
                raise AssertionError(
                    f"Browser foreground did not become ready: {foreground_errors}"
                )
            deadline = time.monotonic() + 60
            while len(foreground_samples["idle"]) < 10 and time.monotonic() < deadline:
                if foreground_errors or browser_process.poll() is not None:
                    break
                time.sleep(0.05)
            if len(foreground_samples["idle"]) < 10:
                raise AssertionError(f"Browser idle baseline incomplete: {foreground_errors}")
            report["foreground"]["idle"] = summary(foreground_samples["idle"][:10])
        else:
            baseline = []
            for _ in range(2):
                read_default_brief_http(server.server_port, token, identity, month_at(0))
            for _ in range(10):
                started = time.perf_counter()
                response_bytes = read_default_brief_http(
                    server.server_port, token, identity, month_at(0)
                )
                baseline.append((time.perf_counter() - started) * 1000)
            report["foreground"]["idle"] = summary(baseline)
            report["foreground"]["response_bytes"] = response_bytes
            reader = threading.Thread(target=read_dashboard, daemon=True)
            reader.start()
        report["status"] = "verifying"
        write()
        verified = measured(
            "verify", lambda: backup.verify_file(database, expected_company_id=identity)
        )
        if verified["evidence_count"] < stored["count"]:
            raise AssertionError("Complete source verification omitted evidence")
        report["verification"] = {"evidence_count": verified["evidence_count"]}
        report["status"] = "backing_up"
        write()
        archive = measured(
            "backup",
            lambda: backup.create_portable(
                database, root / "backups", request_id="stage9-evidence-backup"
            ),
        )
        archive_path = Path(archive["path"])
        if archive["manifest"]["database_format"] != company_format:
            raise AssertionError("Portable backup changed the company contract")
        report["backup"] = {
            "path": str(archive_path),
            "zip_bytes": archive_path.stat().st_size,
            "database_bytes": archive["manifest"]["database_bytes"],
            "database_sha256": archive["manifest"]["database_sha256"],
            "database_format": archive["manifest"]["database_format"],
        }
        report["status"] = "restoring"
        write()
        restored_path = root / "restored.sqlite"
        restored = measured(
            "restore",
            lambda: backup.restore_portable(
                archive_path,
                restored_path,
                expected_company_id=identity,
            ),
        )
        report["restore"] = {
            "path": str(restored_path),
            "database_bytes": restored_path.stat().st_size,
            "evidence_count": restored["evidence_count"],
        }
        if restored["database_format"] != company_format:
            raise AssertionError("Restored company contract differs from source")
        measured(
            "restored_verify",
            lambda: backup.verify_file(restored_path, expected_company_id=identity),
        )
        stop_foreground.set()
        if foreground == "browser":
            browser_stop_file.touch()
        reader.join(timeout=35 if foreground == "browser" else 5)
        if reader.is_alive():
            raise AssertionError("Foreground reader did not stop after evidence operations")
        if foreground_errors:
            raise AssertionError(
                f"Foreground reads failed during evidence operations: {foreground_errors}"
            )
        if foreground == "browser" and (
            not browser_finished.is_set() or not browser_passed.is_set()
            or browser_process.wait(timeout=5)
        ):
            raise AssertionError("Browser foreground did not stop cleanly")
        if size_mib >= 240 and any(
            not foreground_samples[name]
            for name in ("verify", "backup", "restore", "restored_verify")
        ):
            raise AssertionError("Scale evidence operation has no foreground samples")
        if memory_errors:
            raise AssertionError(f"RSS sampling failed: {memory_errors}")
        if tuple(process.pid for process in app.brief_parallel.pool._pool) != worker_memory[0].pids:
            raise AssertionError("Brief worker PIDs changed during RSS measurement")
        with closing(sqlite3.connect(restored_path)) as connection:
            actual = connection.execute(
                "SELECT count(*), count(DISTINCT digest), coalesce(sum(length(content)),0) "
                "FROM evidence WHERE name LIKE 'stage9-evidence-%'"
            ).fetchone()
        if actual != (stored["count"], stored["distinct_digests"], stored["content_bytes"]):
            raise AssertionError(f"Restored original bytes differ: {actual}")
        report["restore"]["synthetic_evidence"] = dict(zip(stored, actual, strict=True))
        report["status"] = "complete"
    except Exception as error:
        primary_error = True
        report["status"] = "failed"
        report["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        stop_foreground.set()
        if "browser_stop_file" in locals():
            browser_stop_file.touch()
        if "browser_process" in locals():
            try:
                browser_process.wait(timeout=35)
            except subprocess.TimeoutExpired:
                browser_process.kill()
                browser_process.wait(timeout=5)
        if "reader" in locals():
            reader.join(timeout=5)
        stop_memory.set()
        memory_thread.join(timeout=5)
        if worker_memory[0] is not None:
            worker_memory[0].close()
        if "server" in locals():
            server.shutdown()
            server.server_close()
        if "server_thread" in locals():
            server_thread.join(timeout=5)
        if "app" in locals():
            app.close()
        report["foreground"].update({
            name: summary(foreground_samples[name])
            for name in ("verify", "backup", "restore", "restored_verify")
        })
        report["foreground"]["errors"] = foreground_errors
        report["sampled_peak_rss_bytes"] = max(
            (item["parent"] for item in peaks.values()), default=rss_bytes()
        )
        report["sampled_peak_parent_plus_brief_workers_rss_bytes"] = max(
            (item["sum"] for item in peaks.values()), default=rss_bytes()
        )
        report["memory_errors"] = memory_errors
        monitor_failure = finalize_monitor_result(report, foreground_errors, memory_errors)
        write()
        if monitor_failure and not primary_error:
            raise AssertionError(f"Evidence monitor failed: {monitor_failure}")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=REPOSITORY)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--size-mib", type=int, required=True, help="40 for smoke; 240 or 1536 for scale runs"
    )
    parser.add_argument(
        "--profile", choices=("mixed", "compressible", "incompressible"), default="mixed"
    )
    parser.add_argument("--seed", type=int, default=9)
    parser.add_argument("--foreground", choices=("http", "browser"), default="http")
    parser.add_argument("--node", type=Path)
    parser.add_argument("--playwright-module", type=Path)
    parser.add_argument("--browser-channel", default="msedge")
    args = parser.parse_args()
    run(
        args.root, args.output, size_mib=args.size_mib, profile=args.profile, seed=args.seed,
        source=args.source, workspace=args.workspace, foreground=args.foreground,
        node=args.node, playwright_module=args.playwright_module,
        browser_channel=args.browser_channel,
    )


if __name__ == "__main__":
    main()
