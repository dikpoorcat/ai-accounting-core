"""One background worker per resident service; durable jobs survive process death."""

import threading

from .backup import _worker_lock, run_backup_jobs
from .engine import Engine
from .exports import run_export_jobs
from .reports import run_report_jobs
from .tax_import import run_tax_import_jobs


class JobRunner:
    def __init__(self, catalog, *, interval=5.0):
        self.catalog, self.interval = catalog, interval
        self.stop_event = threading.Event()
        self.thread = None
        self.last_error = None

    def run_once(self):
        outcomes = []
        for company in self.catalog.companies():
            try:
                store = self.catalog.bind(company["id"])
                with _worker_lock(store.path) as acquired:
                    if not acquired:
                        continue
                    with store.connection() as connection:
                        connection.execute(
                            "UPDATE jobs SET status='failed',"
                            "last_error='interrupted_retry_exhausted' "
                            "WHERE status='running' AND attempts>=3"
                        )
                engine = Engine(store)
                outcomes.extend(run_backup_jobs(store.path, limit=1))
                outcomes.extend(run_export_jobs(engine, limit=1))
                outcomes.extend(run_report_jobs(engine, limit=1))
                outcomes.extend(run_tax_import_jobs(engine, limit=1))
            except Exception as exc:
                self.last_error = getattr(exc, "code", type(exc).__name__)
                outcomes.append(
                    {"company_id": company["id"], "status": "failed", "code": self.last_error}
                )
        return outcomes

    def start(self):
        if self.thread and self.thread.is_alive():
            return

        def run():
            while not self.stop_event.is_set():
                try:
                    self.run_once()
                except Exception as exc:
                    self.last_error = getattr(exc, "code", type(exc).__name__)
                self.stop_event.wait(self.interval)

        self.thread = threading.Thread(target=run, name="accounting-background-jobs", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)
