"""Prepare reusable synthetic dashboard scale fixtures, without acceptance timing.

Use a fixed source snapshot with a release frontend. Construction, checkpoint
copies, independent verification and browser smoke checks run in separate
processes. Every process receipt and failed attempt is retained under .tmp.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
import time
from pathlib import Path

if __package__:
    from .snapshot_stage9_source import _digest, _inventory
    from .stage9_source import synthetic_path, workspace_root
else:
    from snapshot_stage9_source import _digest, _inventory
    from stage9_source import synthetic_path, workspace_root


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def report_digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    pending = path.with_suffix(path.suffix + ".pending")
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    pending.replace(path)


def fixed_source(source):
    source = source.resolve(strict=True)
    manifest = read_json(source / "source-manifest.json")
    files = _inventory(source)
    if (manifest.get("status") != "complete" or manifest.get("files") != files
            or manifest.get("file_count") != len(files) or manifest.get("sha256") != _digest(files)
            or Path(manifest.get("target", "")).resolve() != source):
        raise ValueError("Preparation requires an unchanged complete source snapshot")
    return manifest["sha256"]


def sample_cases():
    return [
        {"key": f"{kind}{months}", "kind": kind, "months": months,
         "people": 50, "businesses": 1000,
         "purpose": "scale_acceptance"}
        for kind in ("main", "independent") for months in (12, 48, 120)
    ] + [{"key": "pressure12", "kind": "main", "months": 12,
          "people": 200, "businesses": 5000, "purpose": "pressure_diagnostic"}]


def require_diagnostic_smoke(result, exit_code):
    """A slow smoke is usable preparation, but a partial/error report is not."""
    status = result.get("status")
    if status not in {"passed", "over_target"} or exit_code != (0 if status == "passed" else 1):
        raise ValueError("Browser smoke has a technical failure or unexpected exit")
    pages = result.get("pages", {})
    if (result.get("sample_count") != 1 or result.get("warmups") != 3
            or set(pages) != {"brief", "funds", "employees", "assets", "reports"}
            or any(len(page.get("samples", [])) != 1 for page in pages.values())):
        raise ValueError("Browser smoke did not complete all five default refreshes")
    for page in pages.values():
        value = page["samples"][0]
        if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
            raise ValueError("Browser smoke contains an invalid completed sample")


class Preparation:
    def __init__(self, *, workspace, source, run_id, node, playwright_module):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", run_id):
            raise ValueError("Use a short lowercase run id")
        self.workspace = workspace
        self.source = source
        self.run_id = run_id
        self.node = node.resolve(strict=True)
        self.playwright_module = playwright_module.resolve(strict=True)
        self.python = workspace / ".tmp-kernel-venv/Scripts/python.exe"
        if Path(sys.executable).resolve() != self.python:
            raise ValueError("Use the repository .tmp-kernel-venv Python")
        self.sha = fixed_source(source)
        self.path = self.file("preparation", ".json")
        if self.path.exists():
            self.report = read_json(self.path)
            if (self.report.get("source_sha256") != self.sha
                    or self.report.get("source") != str(source)
                    or self.report.get("workspace") != str(workspace)):
                raise ValueError("Existing preparation belongs to a different source or workspace")
        else:
            self.report = {"status": "planned", "source": str(source), "source_sha256": self.sha,
                           "workspace": str(workspace), "run_id": run_id,
                           "cases": sample_cases(), "processes": [], "samples": {},
                           "smoke": {}, "acceptance_timing_performed": False}
            self.save()

    def file(self, key, suffix=""):
        return synthetic_path(self.workspace / ".tmp" / f"stage9-{self.run_id}-{key}{suffix}",
                              self.workspace)

    def save(self):
        write_json(self.path, self.report)

    def command(self, key, script, arguments, *, expected_status, report_path,
                diagnostic_smoke=False):
        """Retain actual exit state and raw output even when a preparation fails."""
        previous = [r for r in self.report["processes"] if r["key"] == key]
        if previous and previous[-1]["status"] == "succeeded":
            if fixed_source(self.source) != self.sha:
                raise ValueError("Fixed source changed before preparation reuse")
            if diagnostic_smoke:
                require_diagnostic_smoke(read_json(report_path), previous[-1]["exit_code"])
            elif read_json(report_path).get("status") != expected_status:
                raise ValueError("Previously successful preparation output changed")
            if previous[-1].get("report_sha256") != report_digest(report_path):
                raise ValueError("Previously successful preparation output bytes changed")
            return
        attempt = len(previous) + 1
        if report_path.exists():
            raise ValueError(f"Preserve existing output before retrying {key}; choose a new run id")
        stdout = self.file(f"{key}-attempt{attempt}", ".stdout.txt")
        stderr = self.file(f"{key}-attempt{attempt}", ".stderr.txt")
        receipt = {"key": key, "attempt": attempt, "status": "running",
                   "stdout": str(stdout), "stderr": str(stderr), "output": str(report_path)}
        self.report["status"] = "preparing"
        self.report["processes"].append(receipt)
        self.save()
        started = time.monotonic()
        cmd = [str(self.python), str(self.source / "scripts" / script),
               "--source", str(self.source), "--workspace", str(self.workspace),
               *map(str, arguments)]
        print(json.dumps({"phase": key, "status": "started"}), flush=True)
        try:
            with (stdout.open("x", encoding="utf-8") as out,
                  stderr.open("x", encoding="utf-8") as err):
                with subprocess.Popen(cmd, stdout=out, stderr=err, cwd=self.workspace) as child:
                    receipt["pid"] = child.pid
                    self.save()
                    while True:
                        try:
                            exit_code = child.wait(timeout=30)
                            break
                        except subprocess.TimeoutExpired:
                            progress = {"phase": key, "status": "running",
                                        "elapsed_seconds": round(time.monotonic() - started, 1)}
                            if report_path.exists():
                                try:
                                    data = read_json(report_path)
                                    progress.update(months_completed=len(data.get("months", [])),
                                                    worker_status=data.get("status"))
                                except (ValueError, OSError):
                                    pass
                            print(json.dumps(progress), flush=True)
            receipt.update(exit_code=exit_code, elapsed_seconds=time.monotonic() - started)
            result = read_json(report_path) if report_path.exists() else {}
            if diagnostic_smoke:
                require_diagnostic_smoke(result, exit_code)
                receipt["diagnostic_over_target"] = result["status"] == "over_target"
            else:
                if exit_code != 0:
                    raise RuntimeError(f"{key} exited {exit_code}; see saved stderr")
                if result.get("status") != expected_status:
                    raise ValueError(f"{key} did not produce {expected_status}")
            if fixed_source(self.source) != self.sha:
                raise ValueError("Fixed source changed during preparation")
            receipt.update(status="succeeded", report_sha256=report_digest(report_path))
        except BaseException as error:
            receipt.update(status="failed", elapsed_seconds=time.monotonic() - started,
                           error={"type": type(error).__name__, "message": str(error)})
            raise
        finally:
            self.save()
        print(json.dumps({"phase": key, "status": "succeeded", "exit_code": exit_code}), flush=True)

    def construct(self, case, root, output, *, resume=False):
        independent = case["kind"] == "independent"
        script = "benchmark_stage9_independent.py" if independent else "benchmark_stage9.py"
        arguments = ["--root", root, "--output", output, "--months", case["months"],
                     "--objects" if independent else "--employees", case["people"],
                     "--businesses", case["businesses"], "--build-only",
                     "--defer-historical-verification"]
        if resume:
            arguments.append("--resume")
        self.command(f"build-{case['key']}", script, arguments,
                     expected_status="built_not_verified", report_path=output)

    def verify(self, case, root, output):
        independent = case["kind"] == "independent"
        script = "benchmark_stage9_independent.py" if independent else "benchmark_stage9.py"
        arguments = ["--root", root, "--output", output, "--months", case["months"],
                     "--objects" if independent else "--employees", case["people"],
                     "--businesses", case["businesses"], "--verify-only",
                     "--resume" if independent else "--read-existing"]
        self.command(f"verify-{case['key']}", script, arguments,
                     expected_status="complete", report_path=output)

    def smoke(self):
        """Exercise constructors and five default refreshes; no scale acceptance."""
        for kind in ("main", "independent"):
            case = {"key": f"smoke-{kind}", "kind": kind, "months": 2,
                    "people": 1 if kind == "main" else 2,
                    "businesses": 26 if kind == "main" else 10}
            root = self.file(case["key"])
            built = self.file(case["key"] + "-built", ".json")
            self.construct(case, root, built)
            copied_root = self.file(case["key"] + "-copy")
            self.copy(case, root, copied_root, built)
            qualified = self.file(case["key"] + "-verified", ".json")
            self.verify(case, copied_root, qualified)
            browser = self.file(case["key"] + "-browser", ".json")
            self.command(f"browser-{case['key']}", "benchmark_stage9_browser.py",
                         ["--book-report", qualified, "--output", browser, "--node", self.node,
                          "--playwright-module", self.playwright_module,
                          "--repeats", 1, "--warmups", 3],
                         expected_status="passed", report_path=browser, diagnostic_smoke=True)
            self.report["smoke"][kind] = {"status": "passed", "book_report": str(qualified),
                                          "browser_report": str(browser),
                                          "browser_status": read_json(browser)["status"],
                                          "scope": "five_default_refreshes_diagnostic"}
            self.save()

    def copy(self, case, source_root, target_root, source_report):
        report = target_root / "stage9-checkpoint-copy-book.json"
        self.command(f"copy-{case['key']}", "copy_stage9_checkpoint.py",
                     ["--source-root", source_root, "--target-root", target_root,
                      "--source-report", source_report],
                     expected_status="built_not_verified", report_path=report)
        return read_json(report)

    def completion_status(self):
        if not all(self.report["samples"].get(case["key"], {}).get("status") == "verified"
                   for case in sample_cases()):
            return "built_not_verified"
        if not all(self.report["smoke"].get(kind, {}).get("status") == "passed"
                   for kind in ("main", "independent")):
            return "samples_verified"
        return "ready"

    def build(self):

        for kind in ("main", "independent"):
            accumulator = self.file(f"{kind}120")
            for index, case in enumerate(c for c in sample_cases() if c["kind"] == kind
                                         and c["purpose"] == "scale_acceptance"):
                built = self.file(case["key"] + "-construction", ".json")
                self.construct(case, accumulator, built, resume=index > 0)
                target = self.file(case["key"])
                copied = (built if case["months"] == 120
                          else self.file(case["key"] + "-built", ".json"))
                if case["months"] != 120 and not copied.exists():
                    result = self.copy(case, accumulator, target, built)
                    write_json(copied, result)
                if self.report["samples"].get(case["key"], {}).get("status") != "verified":
                    self.report["samples"][case["key"]] = {**case, "root": str(target),
                                                        "built_report": str(copied),
                                                        "status": "built_not_verified"}
                self.save()
        case = sample_cases()[-1]
        root, built = self.file(case["key"]), self.file(case["key"] + "-built", ".json")
        self.construct(case, root, built)
        if self.report["samples"].get(case["key"], {}).get("status") != "verified":
            self.report["samples"][case["key"]] = {**case, "root": str(root),
                                                "built_report": str(built),
                                                "status": "built_not_verified"}
        self.report["status"] = self.completion_status()
        self.save()

    def verify_all(self):
        for case in sample_cases():
            sample = self.report["samples"][case["key"]]
            qualified = self.file(case["key"] + "-verified", ".json")
            self.verify(case, Path(sample["root"]), qualified)
            sample.update(status="verified", book_report=str(qualified))
            self.save()
        self.report["status"] = self.completion_status()
        self.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--node", type=Path, required=True)
    parser.add_argument("--playwright-module", type=Path, required=True)
    parser.add_argument("--phase", choices=("plan", "smoke", "build", "verify", "all"),
                        default="plan")
    args = parser.parse_args()
    workspace = workspace_root(Path(__file__).resolve().parents[1], args.workspace)
    source = synthetic_path(args.source, workspace)
    preparation = Preparation(workspace=workspace, source=source, run_id=args.run_id,
                              node=args.node, playwright_module=args.playwright_module)
    try:
        if args.phase in {"smoke", "all"}:
            preparation.smoke()
        if args.phase in {"build", "all"}:
            preparation.build()
        if args.phase in {"verify", "all"}:
            preparation.verify_all()
    except BaseException as error:
        preparation.report.update(status="failed", error={"type": type(error).__name__,
                                                         "message": str(error)})
        preparation.save()
        raise
    print(json.dumps({"status": preparation.report["status"], "report": str(preparation.path)}))


if __name__ == "__main__":
    main()
