"""Generate the current report or deliver one exact verified report task."""

from ai_accounting_modular.contracts.public import ContractError, validate_contract

from . import _export_delivery


def run(application, request):
    validate_contract("core.schema.json", "ReportExportRequest", request)
    reports = application.reports
    if reports is None:
        raise ContractError("unsupported", "当前实例未装配报表导出。")
    if request["action"] == "generate":
        company, year, quarter = (request[key] for key in ("company_id", "year", "quarter"))
        result = reports.generate_current(company, year, quarter)
        result = {"company_id": company, "year": year, "quarter": quarter, **result}
    else:
        result = _export_delivery.save(
            application, reports.download(request["company_id"], request["task_id"])
        )
    return validate_contract("core.schema.json", "ReportExportResult", result)
