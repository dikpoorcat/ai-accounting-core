"""HTTP-neutral adapter for the frozen existing dashboard protocol."""

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote

from ai_accounting_modular.contracts.public import ContractError, validate_contract
from ai_accounting_modular.reading.public import SourceDownload

from . import _reports, _requests

__all__ = ["Dashboard", "HttpResponse", "create_dashboard"]
LOCAL_TIME = timezone(timedelta(hours=8), "Asia/Shanghai")


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: object


def _json(body, status=200):
    return HttpResponse(
        status,
        {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"},
        body,
    )


def _validate(key, value):
    try:
        return validate_contract("dashboard-response-v1.json", key, value)
    except ContractError as error:
        raise ContractError(
            "response_contract_mismatch", "看板响应与已确定的格式不一致。"
        ) from error


def _error(error, *, report=False):
    code = error.code
    if code in {"invalid_input", "invalid_contract"}:
        code = "invalid_command"
    if not report and code in {"conflict", "snapshot_changed"}:
        code = "dashboard_snapshot_changed"
    status = 500
    if code == "invalid_command":
        status = 400
    elif code in {
        "review_materials_not_found",
        "review_source_not_found",
        "not_found",
        "unknown_company",
        "unknown_report_job",
    }:
        status = 404
    elif code in {
        "dashboard_snapshot_changed",
        "source_unavailable",
        "source_content_mismatch",
        "preview_expired",
        "report_job_not_ready",
        "report_download_invalid",
        "conflict",
        "report_export_unavailable",
    }:
        status = 409
    elif code in {"busy", "temporary_failure"}:
        status = 503
    result = error.to_response() | {"code": code}
    return _json(result, status)


class Dashboard:
    def __init__(self, reading, *, now=None):
        self.reading = reading
        self.now = now or (lambda: datetime.now(LOCAL_TIME))

    @contextmanager
    def get(self, path, parameters=()):
        """Keep this context open while sending a source's verified binary stream."""
        with ExitStack() as lifetime:
            try:
                if path.startswith("/api/local/report-export/") and path.endswith("/download"):
                    company_id, job_id = _requests.report_task(path, parameters, "/download")
                    value = lifetime.enter_context(self.reading.report_download(company_id, job_id))
                    response = HttpResponse(
                        200,
                        {
                            "Content-Type": (
                                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                            ),
                            "Content-Disposition": "attachment; filename*=UTF-8''"
                            + quote(value.file_name, safe=""),
                            "Content-Length": str(value.byte_size),
                            "X-Content-SHA256": value.sha256,
                            "X-Content-Type-Options": "nosniff",
                            "Cache-Control": "no-store",
                        },
                        value.stream,
                    )
                elif path.startswith("/api/review/sources/") and path.endswith("/content"):
                    token = path[len("/api/review/sources/") : -len("/content")]
                    source_id = unquote(token)
                    if not source_id or any(c in source_id for c in ("/", "\\", "\x00")):
                        raise ContractError("invalid_command", "来源身份无效。")
                    query = _requests.materials(parameters, source_id=source_id)
                    value = lifetime.enter_context(self.reading.source_content(query))
                    if not isinstance(value, SourceDownload):
                        response = _json(value)
                    else:
                        receipt = value.receipt
                        response = HttpResponse(
                            200,
                            {
                                "Content-Type": "application/octet-stream",
                                "Content-Disposition": "attachment; filename*=UTF-8''"
                                + quote(receipt["file_name"], safe=""),
                                "Content-Length": str(receipt["byte_size"]),
                                "X-Content-Type-Options": "nosniff",
                                "Cache-Control": "no-store",
                                "X-Accounting-Company-Id": receipt["company_id"],
                                "X-Accounting-Database-Id": receipt["database_id"],
                                "X-Accounting-Source-Id": receipt["source_id"],
                                "X-Content-SHA256": receipt["sha256"],
                            },
                            value.stream,
                        )
                else:
                    response = _json(self._get_json(path, parameters))
            except ContractError as error:
                response = _error(error, report=path.startswith("/api/local/report-export/"))
            yield response

    def post(self, path, body):
        try:
            if path != "/api/local/report-export":
                raise ContractError("not_found", "此写入入口不存在。")
            scope = _requests.report_export(body)
            receipt = self.reading.export_report(
                scope, body["preview_digest"], body["request_id"], epochs=body["epochs"]
            )
            return _json(_validate("report_export_receipt", receipt))
        except ContractError as error:
            return _error(error, report=True)

    def _get_json(self, path, parameters):
        if path.startswith("/api/local/report-export/") and path.endswith("/status"):
            company_id, job_id = _requests.report_task(path, parameters, "/status")
            return _validate("report_export_status", self.reading.report_task(company_id, job_id))
        if path == "/api/dashboard/context":
            values = _requests.parameters(parameters, {"company_id"})
            if "company_id" in values:
                _requests.company(values["company_id"])
            data = self.reading.page("context", values, {})
            return _validate(
                "dashboard_context",
                {
                    "schema_version": 3,
                    "company": data["current_company"]["name"] if data["current_company"] else None,
                    **data,
                },
            )
        if path == "/api/dashboard/quarterly-report":
            scope = _requests.quarterly(parameters)
            delivery = self.reading.page("quarterly_report", scope, {})
            year, month = int(scope["period_from"][:4]), int(scope["period_from"][5:])
            outer = {
                "schema_version": 6,
                "company_id": scope["company_id"],
                "period": f"{year}-Q{(month - 1) // 3 + 1}",
                "state": delivery.state,
                "model_version": delivery.version,
            }
            if delivery.state == "ready":
                outer["response"] = _validate(
                    "dashboard_quarterly_report", _reports.response(delivery, self.now())
                )
            elif delivery.state == "preparing":
                outer["retry_after_ms"] = 100
            else:
                outer["reason"] = delivery.reason
            return _validate("dashboard_quarterly_report_delivery", outer)
        if path == "/api/review/materials":
            query = _requests.materials(parameters)
            return self.reading.detail(
                "review_materials", {"company_id": query["company_id"]}, query
            )
        raise ContractError("not_found", "此读取入口不存在或尚未接入。")


def create_dashboard(reading, *, now=None):
    return Dashboard(reading, now=now)
