"""Typed local entry points shared by CLI and MCP. No prepared-result input route."""

from __future__ import annotations

import base64
from pathlib import Path

from .backup import create_portable, run_backup_jobs
from .catalog import Catalog
from .contracts import KernelError, Registry
from .engine import Engine
from .exports import Exports, run_export_jobs
from .periods import Periods
from .reports import Reports, run_report_jobs
from .workflow import Workflow

OPERATING_PROTOCOL = {
    "company_binding": "先列出公司并明确当前company_id；公司切换不能沿用另一公司的业务身份或预览。",
    "evidence_first": "先核对已提供资料及既有事实；正式确认事实必须引用实际采用的不可变证据。",
    "typed_facts": "只提交类型化业务事实，不编造科目、借贷或缺失的核算事实。金额使用整数分。",
    "missing_information": "根据fact_issues核对可复用来源后再补充，不把错误码直接变成负责人追问。",
    "publication": "保存事实与发布结果分开；预览后用同一摘要和相关版本确认，重试沿用同一请求键。",
    "correction": "录入错误用amend_fact并明确依据；新实际行为用新身份；已闭期会计更正指定开放期。",
    "management": "管理说明、归集资料可后补，不把管理缺项当核算门禁，不以月末冒充实际日期。",
    "closing": "核对资料和处理结果及负责人确认依据；空数据库或零待匹配项不能证明无业务。",
    "external_actions": "申报、付款和导出是不同事实；文件生成不能视为实际付款或外部提交完成。",
}


def default_registry():
    from . import reports, workflow
    from .domains import adjustments, assets, banking, cash, payroll, taxes, transactions

    registry = Registry()
    for module, category in (
        (payroll, "payroll"),
        (taxes, "tax"),
        (transactions, "transactions"),
        (assets, "assets"),
        (banking, "bank"),
        (adjustments, "transactions"),
        (cash, "transactions"),
    ):
        before = set(registry.models)
        module.register(registry)
        for kind in set(registry.models) - before:
            model = registry.models[kind]
            model.material_category = (
                "bank"
                if kind in {"payment", "cash_bank_transfer"}
                else ("financing" if kind.startswith(("loan", "borrowing")) else category)
            )
    reports.register(registry)
    workflow.register(registry)
    return registry


class LocalService:
    def __init__(self, root: str | Path):
        self.registry = default_registry()
        self.catalog = Catalog(root, self.registry)

    def engine(self, company_id):
        return Engine(self.catalog.bind(company_id))

    def dispatch(self, command: str, payload: dict):
        """Closed set of business commands, with company binding on every request."""
        if command == "schema":
            from .command_schema import command_schemas

            return {
                "facts": self.registry.schemas(),
                "command_schemas": command_schemas(self.registry),
                "agent_operating_protocol": OPERATING_PROTOCOL,
                "format": 1,
                "fact_semantics": "核算字段决定计算；管理说明单独版本化；实际收付日不得由月份代替",
                "commands": [
                    "companies",
                    "create_company",
                    "restore_company",
                    "evidence",
                    "save_fact",
                    "amend_fact",
                    "save_facts",
                    "preview",
                    "confirm",
                    "overview",
                    "ledger",
                    "trace",
                    "management",
                    "inventory",
                    "preview_close",
                    "close",
                    "closed_report",
                    "backup",
                    "run_jobs",
                    "jobs",
                    "rebuild",
                    "preview_delete",
                    "delete",
                    "save_payee",
                    "preview_export",
                    "confirm_export",
                    "run_export_jobs",
                    "report",
                    "preview_report_export",
                    "confirm_report_export",
                    "run_report_jobs",
                    "workflow",
                    "obligation_basis",
                ],
            }
        if command == "companies":
            return self.catalog.companies()
        if command == "create_company":
            return self.catalog.create_company(**payload)
        if command == "restore_company":
            return self.catalog.restore_company(**payload)
        data = dict(payload)
        company_id = data.pop("company_id")
        engine = self.engine(company_id)
        periods = Periods(engine)
        exports = Exports(engine)
        reports = Reports(engine)
        workflow = Workflow(engine)
        actions = {
            "save_fact": engine.save_fact,
            "amend_fact": engine.amend_fact,
            "save_facts": engine.save_facts,
            "preview": engine.preview,
            "confirm": engine.confirm,
            "overview": engine.overview,
            "jobs": engine.jobs,
            "ledger": engine.ledger,
            "trace": engine.trace,
            "management": periods.management,
            "inventory": periods.inventory,
            "preview_close": periods.preview_close,
            "close": periods.close,
            "closed_report": periods.closed_report,
            "rebuild": engine.rebuild_projections,
            "report": reports.report,
            "preview_report_export": reports.preview_export,
            "confirm_report_export": reports.confirm_export,
            "workflow": workflow.query,
            "obligation_basis": workflow.obligation_basis,
        }
        actions.update({"preview_delete": engine.preview_delete, "delete": engine.delete})
        actions.update(
            {
                "save_payee": exports.save_payee,
                "preview_export": exports.preview,
                "confirm_export": exports.confirm,
            }
        )
        if command == "evidence":
            content = base64.b64decode(data.pop("content_base64"), validate=True)
            return engine.register_evidence(content, **data)
        if command == "backup":
            return create_portable(engine.store.path, **data)
        if command == "run_jobs":
            return {
                "backups": run_backup_jobs(engine.store.path, **data),
                "exports": run_export_jobs(engine, **data),
                "reports": run_report_jobs(engine, **data),
            }
        if command == "run_export_jobs":
            return run_export_jobs(engine, **data)
        if command == "run_report_jobs":
            return run_report_jobs(engine, **data)
        if command not in actions:
            raise KernelError("unknown_command", "不支持该业务命令")
        return actions[command](**data)
