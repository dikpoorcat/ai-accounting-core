"""One report port shared by programmatic exports and the dashboard."""

from contextlib import contextmanager
from uuid import uuid4

from ai_accounting_modular.contracts.public import ContractError, validate_contract
from ai_accounting_modular.storage.public import company_metadata

from . import _financial_projection, _models, _report_plans
from ._types import ReportDownload


class ReportExports:
    def __init__(self, *, storage, ledger, files=None):
        self.storage, self.ledger, self.files = storage, ledger, files

    def page(self, scope):
        return _models.page(self, scope)

    def retry(self, scope):
        normalized = _models.normalize(scope)
        with self.storage.maintenance_unit(scope["company_id"]) as unit:
            _financial_projection.retry(
                self,
                normalized,
                "snapshot",
                _models.key(normalized),
                _models.FORMAT_VERSION,
                unit,
            )

    def _files(self):
        if self.files is None:
            raise ContractError("report_export_unavailable", "当前运行实例尚未接通报表文件生成。")
        return self.files

    def _accept(self, scope, head, request_id, *, epochs):
        """Accept only the exact preview retained in the caller's complete request."""
        validate_contract("core.schema.json", "Digest", head)
        _report_plans.validate_epochs(epochs)
        normalized = _models.normalize(scope)
        with self.storage.read_unit(scope["company_id"]) as read:
            metadata = company_metadata(read)
            repository = read.repository("reading")
            state = repository.state(_models.key(normalized), _models.FINANCIAL)
            if state is None or state["state"] != "ready":
                raise ContractError("preview_expired", "季度报表已变化或正在准备，请重新读取。")
            delivery = _models._delivery(
                normalized,
                state,
                read,
                repository.payload(_models.key(normalized), _models.FINANCIAL),
                metadata,
            )
            preview = delivery.payload["export"]
            if not preview["available"]:
                raise ContractError(
                    "report_export_unavailable",
                    preview.get("reason", "本季度尚无可完整准确导出的报表。"),
                )
            if (head, epochs) != (preview["preview_digest"], preview["epochs"]):
                raise ContractError("preview_expired", "报表预览或版本已经变化，请重新读取后生成。")
            plan = _report_plans.plan(delivery.payload["snapshot"], metadata)
        try:
            receipt = self._files().ensure_current(plan, head, request_id)
        except ContractError as error:
            if error.code == "plan_expired":
                raise ContractError(
                    "preview_expired", "生成接纳前报表已变化，请重新读取。"
                ) from error
            raise
        return receipt

    def export(self, scope, head, request_id, *, epochs):
        receipt = self._accept(scope, head, request_id, epochs=epochs)
        return {"status": "queued", "job_id": receipt["task_id"], "preview_digest": head}

    def _failure_reason(self, scope, delivery):
        with self.storage.read_unit(scope["company_id"]) as read:
            repository = read.repository("reading")
            state = repository.state(_models.key(_models.normalize(scope)), _models.FINANCIAL)
            if state is not None and state["state"] == "failed":
                return state["error_code"]
            producer = _financial_projection.store(read).state()
            if producer is not None and producer["state"] == "failed":
                return producer["error_code"]
        return delivery.reason

    def generate_current(self, company_id, year, quarter):
        """Explicitly request a new export of the currently available quarter."""
        validate_contract(
            "core.schema.json",
            "ReportRequest",
            {"company_id": company_id, "year": year, "quarter": quarter},
        )
        last = f"{year:04d}-{quarter * 3:02d}"
        scope = {
            "company_id": company_id,
            "period_from": f"{year:04d}-{quarter * 3 - 2:02d}",
            "period_to": last,
            "view": "as_posted",
            "as_of": last,
        }
        delivery = self.page(scope)
        failure = None
        if delivery.state == "failed":
            failure = self._failure_reason(scope, delivery)
            self.retry(scope)
            delivery = self.page(scope)
        if delivery.state != "ready":
            reason = (
                self._failure_reason(scope, delivery) if delivery.state == "failed" else failure
            )
            return {"state": delivery.state} | ({"reason": reason} if reason is not None else {})
        snapshot, preview = delivery.payload["snapshot"], delivery.payload["export"]
        if snapshot["status"] != "ready":
            return {"state": "partial", "issues": snapshot["issues"], "checks": snapshot["checks"]}
        if not preview["available"]:
            return {
                "state": "failed",
                "reason": preview.get("reason", "当前运行实例尚未接通报表文件生成。"),
            }
        task = self._accept(
            scope, preview["preview_digest"], str(uuid4()), epochs=preview["epochs"]
        )
        return {"state": task["state"], "task": task}

    def _receipt(self, company_id, job_id, *, check_current):
        service = self._files()
        try:
            receipt = service.get(company_id, job_id)
            if receipt["kind"] != "report_export":
                raise ContractError("not_found", "当前公司没有这个报表任务。")
            if check_current:
                receipt = service.get(company_id, job_id, check_current=True)
        except ContractError as error:
            if error.code in {"not_found", "invalid_contract"}:
                raise ContractError("unknown_report_job", "当前公司没有这个报表任务。") from error
            raise
        return receipt

    def task(self, company_id, job_id):
        receipt = self._receipt(company_id, job_id, check_current=True)
        if receipt["state"] == "expired":
            raise ContractError("preview_expired", "此报表计划已经失效，请重新读取报表后生成。")
        with self.storage.read_unit(company_id) as read:
            database_id = read.database_id
        return {
            "schema_version": 1,
            "company_id": company_id,
            "database_id": database_id,
            "job_id": job_id,
            "status": receipt["state"],
            "attempts": receipt["attempts"],
            "error_code": receipt.get("error_code"),
            "error_message": receipt.get("message"),
        }

    @contextmanager
    def download(self, company_id, job_id):
        # Keep the first full file check in Files: a status precheck would consume
        # a corruption error and turn it into a generic not-ready response.
        self._receipt(company_id, job_id, check_current=False)
        try:
            with self._files().verified_download(company_id, job_id) as verified:
                yield ReportDownload(
                    verified.stream,
                    verified.file_name,
                    verified.sha256,
                    verified.byte_size,
                    verified.company_id,
                    verified.database_id,
                    verified.task_id,
                )
        except ContractError as error:
            codes = {
                "not_found": "unknown_report_job",
                "plan_expired": "preview_expired",
                "file_not_ready": "report_job_not_ready",
                "file_invalid": "report_download_invalid",
            }
            if error.code in codes:
                raise ContractError(codes[error.code], error.message) from error
            raise


def create_report_exports(*, storage, ledger, files=None):
    return ReportExports(storage=storage, ledger=ledger, files=files)
