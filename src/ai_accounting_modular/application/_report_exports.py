"""Generate the current report or deliver one exact verified report task."""

from ai_accounting_modular.contracts.public import ContractError, validate_contract


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
        if application.save_report is None:
            raise ContractError("unsupported", "当前实例未装配本机桌面交付。")
        with reports.download(request["company_id"], request["task_id"]) as download:
            saved = application.save_report(download)
            result = {
                **saved,
                "company_id": download.company_id,
                "database_id": download.database_id,
                "task_id": download.task_id,
            }
    return validate_contract("core.schema.json", "ReportExportResult", result)
