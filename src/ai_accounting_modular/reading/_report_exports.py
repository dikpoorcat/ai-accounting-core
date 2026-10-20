"""Create one current report delivery and observe or download that exact task."""

from contextlib import contextmanager

from ai_accounting_modular.contracts.public import ContractError, validate_contract
from ai_accounting_modular.storage.public import company_metadata

from . import _models, _report_plans
from ._types import ReportDownload


def _files(reader):
    if reader.files is None:
        raise ContractError("report_export_unavailable", "当前运行实例尚未接通报表文件生成。")
    return reader.files


def export(reader, scope, head, request_id, *, epochs):
    validate_contract("core.schema.json", "Digest", head)
    _report_plans.validate_epochs(epochs)
    normalized = _models.normalize(scope)
    with reader.storage.read_unit(scope["company_id"]) as read:
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
        receipt = _files(reader).ensure_current(plan, head, request_id)
    except ContractError as error:
        if error.code == "plan_expired":
            raise ContractError("preview_expired", "生成接纳前报表已变化，请重新读取。") from error
        raise
    return {"status": "queued", "job_id": receipt["task_id"], "preview_digest": head}


def task(reader, company_id, job_id):
    service = _files(reader)
    try:
        receipt = service.get(company_id, job_id)
        if receipt["kind"] != "report_export":
            raise ContractError("not_found", "当前公司没有这个报表任务。")
        receipt = service.get(company_id, job_id, check_current=True)
    except ContractError as error:
        if error.code in {"not_found", "invalid_contract"}:
            raise ContractError("unknown_report_job", "当前公司没有这个报表任务。") from error
        raise
    if receipt["state"] == "expired":
        raise ContractError("preview_expired", "此报表计划已经失效，请重新读取报表后生成。")
    with reader.storage.read_unit(company_id) as read:
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
def download(reader, company_id, job_id):
    task(reader, company_id, job_id)
    try:
        with _files(reader).verified_download(company_id, job_id) as verified:
            yield ReportDownload(
                verified.stream, verified.file_name, verified.sha256, verified.byte_size
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
