"""Resume owner-maintained current targets, never replay an accounting submission."""

from functools import partial
from threading import Event, Thread

from ai_accounting_modular.contracts.public import ContractError


class Background:
    def __init__(self, runtime, *, interval=1.0):
        self.runtime, self.interval = runtime, interval
        self.stop_event, self.wake_event = Event(), Event()
        self.thread = Thread(target=self._run, name="modular-current-work", daemon=False)
        self.recovered = set()
        self.source_discard_cursors = {}

    def start(self):
        self.thread.start()

    def cycle(self):
        kernel = self.runtime.kernel
        try:
            companies = kernel.storage.list_companies()
        except Exception as error:
            self.runtime.failure(error)
            return
        for company in companies:
            if self.stop_event.is_set():
                break
            company_id = company.company_id
            try:
                if company.company_id not in self.recovered:
                    kernel.files.recover(company_id)
                    self.recovered.add(company_id)
            except Exception as error:
                self.runtime.failure(error)

            operations = [partial(kernel.reading.run_pending, company_id, limit=5)]
            if company_id in self.recovered:
                operations = [
                    partial(self._documents, company_id),
                    *operations,
                    partial(kernel.files.run_pending, company_id),
                    partial(self._source_discards, company_id),
                ]
            for operation in operations:
                try:
                    operation()
                except Exception as error:
                    self.runtime.failure(error)

    def _documents(self, company_id):
        app = self.runtime.kernel.application
        status = app.accounting_documents({"company_id": company_id, "action": "status"})
        if status["state"] != "ready":
            app.accounting_documents({"company_id": company_id, "action": "finish"})

    def _source_discards(self, company_id):
        result = self.runtime.kernel.application.finish_source_discards(
            company_id, after_marker_id=self.source_discard_cursors.get(company_id), limit=500
        )
        if result.next_marker_id is None:
            self.source_discard_cursors.pop(company_id, None)
        else:
            self.source_discard_cursors[company_id] = result.next_marker_id
        for error in result.errors:
            self.runtime.failure(ContractError(error.code, error.message))

    def _run(self):
        while not self.stop_event.is_set():
            self.wake_event.clear()
            self.cycle()
            self.wake_event.wait(self.interval)

    def close(self):
        self.stop_event.set()
        self.wake_event.set()
        if self.thread.is_alive():
            self.thread.join()
