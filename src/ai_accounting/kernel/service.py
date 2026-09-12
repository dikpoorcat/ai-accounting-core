"""Typed local entry points shared by CLI and MCP. No prepared-result input route."""

from __future__ import annotations

import base64
from contextlib import contextmanager
from pathlib import Path

from .backup import run_backup_jobs
from .business_queries import BusinessQueries
from .catalog import Catalog
from .contracts import KernelError, Registry
from .discovery import Discovery
from .display import Display
from .engine import Engine
from .exports import Exports, run_export_jobs
from .materials import Materials
from .payroll_preparation import PayrollPreparation
from .periods import Periods
from .reports import Reports, run_report_jobs
from .reserves import Reserves
from .security import SecurityService, consume_close_approval
from .tax_import import TaxImport
from .workflow import Workflow

OPERATING_PROTOCOL = {
    "company_binding": "先列出公司并明确当前company_id；公司切换不能沿用另一公司的业务身份或预览。",
    "evidence_first": "先核对已提供资料及既有事实；正式确认事实必须引用实际采用的不可变证据。",
    "typed_facts": "只提交类型化业务事实，不编造科目、借贷或缺失的核算事实。金额使用整数分。",
    "missing_information": "根据fact_issues核对可复用来源后再补充，不把错误码直接变成负责人追问。",
    "publication": "保存事实与发布结果分开；预览后用同一摘要和相关版本确认，重试沿用同一请求键。",
    "correction": "录入错误用amend_fact并明确依据；新实际行为用新身份；已闭期会计更正指定开放期。",
    "management": "管理说明、归集资料可后补，不把管理缺项当核算门禁，不以月末冒充实际日期。",
    "dashboard_management": (
        "看板名称、人员入离职资料与用途通过save_display_profile按明确来源追加管理版本；"
        "登记业务时同步保存原件已明确的人员/往来方/账户/资产名称及业务用途说明，"
        "复用稳定业务身份，记录证据文件名和具体来源位置；不只保留内部编号。"
        "资料后补同时维护私有重放补充清单，不因展示资料缺项阻断核算。"
        "不从工资生效月推断入职日。月度经营结论先preview_period_commentary读取核算上下文，"
        "再用同一context_digest调用update_period_commentary；闭期补充显示为后补说明，"
        "context_digest只用于提交并发校验；已存说明按独立content_digest及精确来源判断有效性，"
        "内容相同不能绕过过期提交。历史展示field_sources分别说明冻结与当前后补来源，"
        "recorded_at仅是系统确认时间，不能替代实际发生日或冒充负责人最早知悉日。"
        "不更改冻结凭证，不要求负责人补写AI应完成的经营说明。"
    ),
    "closing": "核对资料和处理结果及负责人确认依据；空数据库或零待匹配项不能证明无业务。",
    "closing_batches": (
        "连续历史关账先分别preview_close_range，再用approve_close_batches原生窗口一次密码确认"
        "明确的公司、起止月份及预览摘要；各公司用对应approval_id执行close_range，"
        "失败重试沿用原请求键。批准不可扩大期间；过期或预览变化须重新核对批准。"
    ),
    "material_allocation": (
        "跨月原件先核对逐项归属，再分别处理各月业务；归属不等于入账完成，未知归属不能默认接收月。"
    ),
    "external_actions": "申报、付款和导出是不同事实；文件生成不能视为实际付款或外部提交完成。",
}


def default_registry():
    from . import (
        materials,
        payroll_preparation,
        payroll_tax_declarations,
        reports,
        tax_import,
        workflow,
    )
    from .domains import (
        accounting,
        adjustments,
        assets,
        banking,
        cash,
        investments,
        labor_assets,
        managed_reserve,
        opening,
        payroll,
        payroll_reserve_payment,
        platforms,
        taxes,
        transactions,
    )

    registry = Registry()
    for module, category in (
        (payroll, "payroll"),
        (taxes, "tax"),
        (transactions, "transactions"),
        (assets, "assets"),
        (labor_assets, "assets"),
        (banking, "bank"),
        (adjustments, "transactions"),
        (cash, "transactions"),
        (platforms, "transactions"),
        (managed_reserve, "transactions"),
        (payroll_reserve_payment, "bank"),
        (investments, "transactions"),
    ):
        before = set(registry.models)
        module.register(registry)
        for kind in set(registry.models) - before:
            model = registry.models[kind]
            model.material_category = (
                "bank"
                if kind
                in {
                    "payment",
                    "cash_bank_transfer",
                    "bank_platform_transfer",
                    "managed_reserve_bank_expense",
                    "payroll_reserve_payment",
                }
                else ("financing" if kind.startswith(("loan", "borrowing")) else category)
            )
    reports.register(registry)
    workflow.register(registry)
    opening.register(registry)
    materials.register(registry)
    payroll_preparation.register(registry)
    tax_import.register(registry)
    payroll_tax_declarations.register(registry)
    accounting.register(registry)
    return registry


class LocalService:
    def __init__(self, root: str | Path):
        self.registry = default_registry()
        self.catalog = Catalog(root, self.registry)
        self.security = SecurityService(self.catalog.path)
        self.close_previews = {}
        self.close_range_previews = {}
        from .command_schema import command_models

        self.command_models = command_models(self.registry)

    def engine(self, company_id):
        return Engine(self.catalog.bind(company_id))

    def dashboard_context(self, company_id: str | None = None):
        from .dashboard import Dashboard

        companies = self.catalog.companies()
        if not companies:
            if company_id:
                raise KernelError("unknown_company", "公司尚未登记")
            return {
                "schema_version": 2,
                "company": None,
                "companies": [],
                "current_company": None,
                "periods": [],
                "quarters": [],
                "default_period": None,
                "default_quarter": None,
            }
        company = next((item for item in companies if item["id"] == company_id), None)
        if company_id and company is None:
            raise KernelError("unknown_company", "公司尚未登记")
        company = company or companies[0]
        return Dashboard(
            self.engine(company["id"]), company_name=company["name"], companies=companies
        ).context()

    def dispatch(self, command: str, payload: dict, *, session_token=None):
        """Closed set of business commands, with company binding on every request."""
        from .command_schema import validate_command

        payload = validate_command(self.command_models, command, payload)
        if command == "schema":
            return self._dispatch(command, payload)
        authority = self.security.authorize(session_token, request_id=payload.get("request_id"))
        if command in {"create_company", "restore_company", "configure_backup"}:
            with self.security.authorization_gate:
                self.security.validate_authority(authority)
                return self._dispatch(command, payload, authority=authority)
        return self._dispatch(command, payload, authority=authority)

    @contextmanager
    def _commit_authority(self, authority):
        # Read snapshots and pure computation remain parallel. Revocation and
        # the final publication are ordered under the short shared identity gate.
        with self.security.authorization_gate:
            self.security.validate_authority(authority)
            yield

    def _dispatch(self, command, payload, *, authority=None):
        if command == "schema":
            from .security.native import NativeRequest

            return {
                "facts": self.registry.schemas(),
                "command_schemas": {
                    name: model.json_schema() for name, model in self.command_models.items()
                },
                "security_request_schema": NativeRequest.model_json_schema(),
                "security_operations": ["request", "status", "cancel", "session_status"],
                "agent_operating_protocol": OPERATING_PROTOCOL,
                "format": 1,
                "fact_semantics": "核算字段决定计算；管理说明单独版本化；实际收付日不得由月份代替",
                "commands": sorted(self.command_models),
            }
        if command == "companies":
            return self.catalog.companies()
        if command == "dashboard_context":
            return self.dashboard_context(**payload)
        if command == "operations":
            return self.catalog.operations()
        if command == "create_company":
            return self.catalog.create_company(**payload)
        if command == "restore_company":
            return self.catalog.restore_company(**payload)
        if command in {"company_settings", "configure_backup"}:
            self.catalog.bind(payload["company_id"])
            return getattr(self.catalog, command)(**payload)
        data = dict(payload)
        company_id = data.pop("company_id")
        engine = self.engine(company_id)
        engine.commit_guard = lambda: self._commit_authority(authority)
        engine.audit_actor = {
            "catalog_id": authority.catalog_instance_id,
            "owner_id": authority.owner_id,
            "session_id": authority.session_id,
            "credential_version": authority.credential_version,
        }
        if command in {"close", "close_range"} and data.get("backup_directory") is None:
            setting = self.catalog.company_settings(company_id)
            data["backup_directory"] = setting["backup_directory"] or str(
                self.catalog.root / "backups" / engine.store.path.parent.name
            )
        approval_id = data.pop("approval_id", None) if command in {"close", "close_range"} else None

        def authorize_close(connection, period, preview_digest, epochs):
            self.security.validate_authority(authority)
            return consume_close_approval(
                connection,
                approval_id,
                authority=authority,
                company_id=engine.store.company_id,
                database_id=engine.store.database_id,
                period=period,
                preview_digest=preview_digest,
                epochs=epochs,
                now=self.security.now(),
            )

        def authorize_close_range(connection, from_period, through_period, preview_digest, epochs):
            from .security.batches import consume_batch_approval

            return consume_batch_approval(
                connection,
                approval_id,
                service=self.security,
                authority=authority,
                company_id=engine.store.company_id,
                database_id=engine.store.database_id,
                from_period=from_period,
                through_period=through_period,
                preview_digest=preview_digest,
                epochs=epochs,
            )

        periods = Periods(
            engine, authorize_close=authorize_close, authorize_close_range=authorize_close_range
        )
        exports = Exports(engine)
        reports = Reports(engine)
        workflow = Workflow(engine)
        materials = Materials(engine)
        discovery = Discovery(engine)
        display = Display(engine)
        from .dashboard import Dashboard

        dashboard = Dashboard(engine)
        business_queries = BusinessQueries(engine)
        payroll_preparation = PayrollPreparation(engine)
        tax_import = TaxImport(engine)
        reserves = Reserves(engine)
        actions = {
            "save_fact": engine.save_fact,
            "amend_fact": engine.amend_fact,
            "save_facts": engine.save_facts,
            "preview": engine.preview,
            "confirm": engine.confirm,
            "overview": engine.overview,
            "jobs": engine.jobs,
            "retry_job": engine.retry_job,
            "ledger": engine.ledger,
            "trace": engine.trace,
            "business_status": business_queries.business_status,
            "period_readiness": business_queries.period_readiness,
            "management": periods.management,
            "inventory": periods.inventory,
            "preview_close": periods.preview_close,
            "close": periods.close,
            "preview_close_range": periods.preview_close_range,
            "close_range": periods.close_range,
            "closed_report": periods.closed_report,
            "rebuild": engine.rebuild_projections,
            "report": reports.report,
            "preview_report_export": reports.preview_export,
            "confirm_report_export": reports.confirm_export,
            "confirm_browser_report_export": reports.confirm_browser_export,
            "workflow": workflow.query,
            "obligation_basis": workflow.obligation_basis,
            "prepare_obligations": workflow.prepare_obligations,
            "confirm_obligations": workflow.confirm_obligations,
            "inspect_material": materials.inspect,
            "receive_material": materials.receive,
            "resolve_material": materials.resolve,
            "resolve_material_group": materials.resolve_group,
            "material_completeness": materials.check,
            "preview_material_allocation": materials.preview_period_allocation,
            "confirm_material_allocation": materials.confirm_period_allocation,
            "company_context": discovery.company_context,
            "update_company_note": discovery.update_company_note,
            "save_display_profile": display.save_display_profile,
            "display_profiles": display.display_profiles,
            "preview_period_commentary": display.preview_period_commentary,
            "update_period_commentary": display.update_period_commentary,
            "dashboard_brief": dashboard.brief,
            "dashboard_funds": dashboard.funds,
            "dashboard_employees": dashboard.employees,
            "dashboard_assets": dashboard.assets,
            "dashboard_business_status": dashboard.business_status,
            "dashboard_quarterly_report": dashboard.quarterly_report,
            "find_facts": discovery.find_facts,
            "payroll_reuse_basis": payroll_preparation.reuse_basis,
            "prepare_payroll": payroll_preparation.prepare,
            "confirm_payroll_preparation": payroll_preparation.confirm,
            "preview_managed_reserve_settlement": reserves.preview_settlement,
            "confirm_managed_reserve_settlement": reserves.confirm_settlement,
            "preview_tax_import": tax_import.preview,
            "confirm_tax_import": tax_import.confirm,
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
            return engine.queue_backup(**data)
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
        result = actions[command](**data)
        if command == "preview_close":
            key = (company_id, engine.store.database_id, result["digest"])
            self.close_previews[key] = {**result, "owner_confirmation": data["owner_confirmation"]}
            while len(self.close_previews) > 128:
                self.close_previews.pop(next(iter(self.close_previews)))
        elif command == "preview_close_range":
            key = (company_id, engine.store.database_id, result["digest"])
            self.close_range_previews[key] = {
                **result,
                "owner_confirmation": data["owner_confirmation"],
            }
            while len(self.close_range_previews) > 128:
                self.close_range_previews.pop(next(iter(self.close_range_previews)))
        return result
