"""Real same-company writing, future models and portable files beside old-page reads.

Preparation is untimed and uses an isolated restore of one verified package.
The future month is beyond the observed quarter, so all five old scopes remain
as-posted scopes. Workers never replay a failed Apply or manufacture file jobs.
"""

from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
from math import isfinite
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Condition, Event, Lock, Thread
from time import monotonic, sleep, time_ns
from unittest.mock import patch

from ai_accounting_modular.contracts.public import CONTRACT, validate_contract

from .._support import COMPANY, DATABASE, http, ready
from .dataset import count_businesses, describe, open_dataset
from .portable_cases import package, restore
from .projections import PAGES, parameters
from .workload import identity

_KINDS = {"write", "model", "backup"}
_WAIT_SECONDS = 120  # A broken fixture guard, not a performance acceptance budget.


def _epoch():
    return time_ns() / 1_000_000


def _future_month(period):
    year, month = map(int, period.split("-"))
    return f"{year + 1}-01" if month > 9 else f"{year}-{((month - 1) // 3 + 1) * 3 + 1:02d}"


def _request(period, index):
    source_id = identity("concurrency/source/" + period)
    request = {
        "contract": CONTRACT,
        "company_id": COMPANY,
        "command_id": identity(f"concurrency/{period}/command/{index}"),
        "mutations": [
            {
                "op": "create",
                "expected_seq": 0,
                "fact": {
                    "business_id": identity(f"concurrency/{period}/expense/{index}"),
                    "kind": "operations.expense",
                    "period": period,
                    "evidence_ids": [source_id],
                    "data": {
                        "creditor_id": identity("supplier"),
                        "creditor_kind": "supplier",
                        "expense_class": "administration",
                        "amount_fen": "1000",
                        "report_details": [{"code": "management_other", "amount_fen": "1000"}],
                    },
                },
            }
        ],
    }
    if index == 0:
        text = (
            f"合成并发验收：{period}向合成供应商发生独立维修费用，各项最终金额"
            "以每份事实明确的1000或1001分为准，修正均在本测试月内，"
            "均尚未付款；不引用以前月份的往来、成本、资产或资金。仅为测试资料。"
        )
        request["sources"] = [
            {
                "source_id": source_id,
                "kind": "owner_statement",
                "text": text,
                "sha256": sha256(text.encode()).hexdigest(),
                "archive_month": period,
            }
        ]
    return request


def _ready_body(kernel, page, period):
    response = http(kernel, page, parameters(COMPANY, period, page))
    assert response["http_status"] == 200, response
    body = response["body"]
    validate_contract(
        "dashboard-response-v2.json", "dashboard_" + page.replace("-", "_") + "_delivery", body
    )
    return body


def _await(check):
    deadline = monotonic() + _WAIT_SECONDS
    while not (value := check()):
        if monotonic() >= deadline:
            raise TimeoutError("synthetic concurrent work did not finish")
        sleep(0.01)
    return value


@dataclass
class PreparedConcurrency:
    root: Path
    period: str
    work_period: str
    baseline: dict
    business_count: int
    future_version: str
    future_scopes: tuple[str, ...]
    evidence: dict = field(default_factory=dict)

    @contextmanager
    def during(self, runtime):
        assert runtime.catalog_path.resolve() == self.root / "catalog.sqlite3"
        work = _Workers(self, runtime)
        self.evidence = work.evidence
        with patch.object(runtime.kernel.reading, "run_pending", work.produce):
            work.start()
            try:
                _await(lambda: work.failure or work.backup_started.is_set())
                assert not work.failure, work.evidence
                work.evidence["window_started_epoch_ms"] = _epoch()
                yield work.evidence
            finally:
                work.evidence["window_finished_epoch_ms"] = _epoch()
                work.close()
                work.verify_old_scope()


@contextmanager
def prepare(root, mode):
    root = Path(root).resolve()
    manifest = describe(root)
    with TemporaryDirectory(prefix="c14-concurrent-", dir=root.parent) as temporary:
        temporary = Path(temporary).resolve()
        portable = package(root, temporary / "baseline.zip", mode)
        target = temporary / "clone"
        target.mkdir()
        restore(portable, target, manifest, mode)
        period, future = manifest["period_to"], _future_month(manifest["period_to"])
        with open_dataset(target, mode) as (kernel, _):
            saved = kernel.application.accounting_apply(_request(future, 0))
            assert saved["completion"] == "complete" and saved["documents"] == "ready", saved
            future_scopes = []
            production = kernel.reading.run_pending

            def produce(company_id, *, limit=20):
                result = production(company_id, limit=limit)
                future_scopes.extend(result)
                return result

            with patch.object(kernel.reading, "run_pending", produce):
                future_version = ready(kernel, "brief", parameters(COMPANY, future, "brief"))[
                    "snapshot_version"
                ]
            assert future_scopes
            baseline = {
                page: ready(kernel, page, parameters(COMPANY, period, page)) for page in PAGES
            }
            assert count_businesses(kernel, period) == manifest["businesses_per_month"]
        yield PreparedConcurrency(
            target,
            period,
            future,
            baseline,
            manifest["businesses_per_month"],
            future_version,
            tuple(sorted(set(future_scopes))),
        )


class _Workers:
    def __init__(self, prepared, runtime):
        self.prepared, self.runtime = prepared, runtime
        self.stop, self.backup_started = Event(), Event()
        self.condition, self.mutation = Condition(), Lock()
        self.generation, self.failure = 0, None
        self.production = runtime.kernel.reading.run_pending
        self.evidence = {
            "status": "running",
            "company_id": COMPANY,
            "database_id": DATABASE,
            "observed_period": prepared.period,
            "work_period": prepared.work_period,
            "setup": "verified backup, isolated clone, future seed, old scopes ready before timing",
            "bounded_workload": "one future seed, one future expense; real open-period corrections",
            "activities": [],
        }
        self.threads = [
            Thread(target=self._run, args=(self._writes,), name="c14-future-business"),
            Thread(target=self._run, args=(self._backups,), name="c14-backup-files"),
        ]

    @contextmanager
    def _activity(self, kind, **details):
        item = dict(kind=kind, started_epoch_ms=_epoch(), status="running", **details)
        self.evidence["activities"].append(item)
        try:
            yield item
            item["status"] = "complete"
        except Exception as error:
            item.update(status="failed", error_type=type(error).__name__, message=str(error))
            raise
        finally:
            item["finished_epoch_ms"] = _epoch()

    def _run(self, operation):
        try:
            operation()
        except Exception as error:
            self.failure = {"error_type": type(error).__name__, "message": str(error)}
            self.evidence["failure"] = self.failure
            self.stop.set()
            with self.condition:
                self.condition.notify_all()

    def start(self):
        for thread in self.threads:
            thread.start()

    def close(self):
        self.stop.set()
        with self.condition:
            self.condition.notify_all()
        for thread in self.threads:
            thread.join()

    def produce(self, company_id, *, limit=20):
        started = _epoch()
        produced = self.production(company_id, limit=limit)
        if company_id == COMPANY and produced:
            self.evidence["activities"].append(
                {
                    "kind": "model",
                    "started_epoch_ms": started,
                    "finished_epoch_ms": _epoch(),
                    "status": "complete",
                    "produced_scopes": list(produced),
                }
            )
            unexpected = set(produced) - set(self.prepared.future_scopes)
            if unexpected:
                self.evidence.setdefault("unexpected_model_scopes", []).extend(sorted(unexpected))
        return produced

    def verify_old_scope(self):
        try:
            checks = {}
            for page in PAGES:
                body = _ready_body(self.runtime.kernel, page, self.prepared.period)
                checks[page] = {
                    "state": body["state"],
                    "unchanged": body["state"] == "ready"
                    and body["response"] == self.prepared.baseline[page],
                }
            self.evidence["old_page_checks"] = checks
            unchanged = all(item["unchanged"] for item in checks.values())
            unchanged = (
                unchanged
                and count_businesses(self.runtime.kernel, self.prepared.period)
                == self.prepared.business_count
            )
        except Exception as error:
            unchanged = False
            self.evidence["verification_failure"] = {
                "error_type": type(error).__name__,
                "message": str(error),
            }
        completed = Counter(
            item["kind"] for item in self.evidence["activities"] if item["status"] == "complete"
        )
        self.evidence.update(
            old_scope_unchanged=unchanged,
            completed_work=dict(completed),
            status="passed"
            if unchanged
            and not self.failure
            and not self.evidence.get("unexpected_model_scopes")
            and _KINDS <= completed.keys()
            else "failed",
        )

    def _writes(self):
        kernel, previous = self.runtime.kernel, self.prepared.future_version
        index = 1
        while not self.stop.is_set():
            request = _request(self.prepared.work_period, 1)
            if index > 1:
                request["command_id"] = identity(
                    f"concurrency/{self.prepared.work_period}/revision/{index}"
                )
                mutation = request["mutations"][0]
                mutation.update(op="replace", expected_seq=index - 1)
                amount = str(1000 + (index - 1) % 2)
                mutation["fact"]["data"].update(
                    amount_fen=amount,
                    report_details=[{"code": "management_other", "amount_fen": amount}],
                )
            with (
                self.mutation,
                self._activity("write", business_id=request["mutations"][0]["fact"]["business_id"]),
            ):
                saved = kernel.application.accounting_apply(request)
                assert saved["completion"] == "complete" and saved["documents"] == "ready", saved
            self.runtime.wake()

            def finished():
                body = _ready_body(kernel, "brief", self.prepared.work_period)
                assert body["state"] in {"preparing", "ready"}, body
                return body if body["state"] == "ready" else None

            body = _await(finished)
            version = body["response"]["snapshot_version"]
            assert version != previous
            self.evidence.setdefault("future_model_versions", []).append(version)
            previous = version
            with self.condition:
                self.generation += 1
                self.condition.notify_all()
            index += 1

    def _backups(self):
        kernel, previous_generation, task_ids = self.runtime.kernel, 0, set()
        while not self.stop.is_set():
            with self.condition:
                self.condition.wait_for(
                    lambda generation=previous_generation: (
                        self.stop.is_set() or self.generation > generation
                    )
                )
                if self.stop.is_set():
                    return
                previous_generation = self.generation
            # Serialize only capture acceptance with the small Apply/documents
            # boundary. Real ZIP production proceeds while future writes resume.
            with self._activity("backup", generation=previous_generation) as item:
                with self.mutation:
                    item["started_epoch_ms"] = _epoch()  # Exclude waiting for fixture coordination.
                    receipt = kernel.application.accounting_backup(
                        {"company_id": COMPANY, "expected_database_id": DATABASE}
                    )
                task_id = receipt["task_id"]
                assert task_id not in task_ids, "A cached file is not concurrent file production"
                task_ids.add(task_id)
                item["task_id"] = task_id
                self.backup_started.set()
                self.runtime.wake()

                def finished(task_id=task_id):
                    result = kernel.application.accounting_jobs(
                        {"company_id": COMPANY, "task_id": task_id}
                    )
                    assert result["state"] in {"pending", "running", "succeeded"}, result
                    return result if result["state"] == "succeeded" else None

                _await(finished)
                with kernel.files.verified_download(COMPANY, task_id) as download:
                    assert download.stream.read(1)
                    item.update(sha256=download.sha256, byte_size=download.byte_size)


def qualify_overlap(evidence, attempts):
    """Verify actual formal browser intervals against completed real work intervals."""
    samples = [
        item
        for item in attempts
        if item.get("kind") == "sample" and item.get("status") == "complete"
    ]
    records, all_kinds = [], set()
    for sample in samples:
        start, end = sample.get("start_epoch_ms"), sample.get("end_epoch_ms")
        valid = (
            all(
                isinstance(value, int | float) and not isinstance(value, bool) and isfinite(value)
                for value in (start, end)
            )
            and end > start
        )
        kinds = {
            item["kind"]
            for item in evidence.get("activities", [])
            if valid
            and item["status"] == "complete"
            and item["started_epoch_ms"] < end
            and item["finished_epoch_ms"] > start
        }
        all_kinds.update(kinds)
        records.append(
            {
                "scenario": sample.get("scenario", sample.get("page")),
                "iteration": sample.get("iteration"),
                "start_epoch_ms": start,
                "end_epoch_ms": end,
                "work_kinds": sorted(kinds),
            }
        )
    return {
        "status": "passed"
        if evidence.get("status") == "passed"
        and records
        and all(item["work_kinds"] for item in records)
        and _KINDS <= all_kinds
        else "failed",
        "formal_samples": len(records),
        "overlapping_work_kinds": sorted(all_kinds),
        "samples": records,
    }
