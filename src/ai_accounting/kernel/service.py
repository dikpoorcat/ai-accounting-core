"""Typed local entry points shared by CLI and MCP. No prepared-result input route."""

from __future__ import annotations

import base64
import secrets
from contextlib import ExitStack, contextmanager
from pathlib import Path
from threading import RLock

from pydantic import ValidationError

from .asset_batches import AssetBatches
from .backup import run_backup_jobs
from .business_queries import BusinessQueries
from .catalog import Catalog
from .close_review import CloseReview
from .contracts import KernelError, Registry
from .discovery import Discovery
from .display import Display
from .duplicates import Duplicates
from .engine import PROGRAM_VERSION, Engine
from .entities import Entities
from .exports import Exports, run_export_jobs
from .identity_corrections import IdentityCorrections
from .materials import Materials
from .operating_protocol import OPERATING_PROTOCOL
from .payroll_preparation import PayrollPreparation
from .periods import Periods
from .replay_close import ReplayClose, ReplayCloseScope, ReplayScopeConfig
from .reports import Reports, run_report_jobs
from .security import SecurityService, consume_close_approval
from .tax_import import TaxImport
from .workflow import Workflow

_HTTP_DASHBOARD_COMMANDS = frozenset(
    {
        "dashboard_context",
        "dashboard_brief",
        "dashboard_brief_group",
        "dashboard_funds",
        "dashboard_employees",
        "dashboard_assets",
        "dashboard_business_status",
        "dashboard_quarterly_report",
        "dashboard_period_preparation",
        "dashboard_close_review",
    }
)


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
    from .identity_corrections import register as register_identity_corrections

    register_identity_corrections(registry)
    return registry


class LocalService:
    def __init__(
        self,
        root: str | Path,
        *,
        enable_read_pool: bool = False,
        replay_scope: dict | None = None,
        _static_runtime=None,
    ):
        from .schema_bundle import production_bundle

        if _static_runtime is None:
            self.bundle = production_bundle()
            prepared_models = None
        else:
            self.bundle, prepared_models = _static_runtime
            if self.bundle is not production_bundle():
                raise ValueError("daemon runtime does not match the production bundle")
        self.registry = self.bundle.registry
        self.catalog = Catalog(root, self.bundle)
        from .inspection_cache import InspectionCache

        self.inspection_cache = InspectionCache(self.catalog.root, build_id=PROGRAM_VERSION)
        self.read_pool = None
        self._catalog_observer = None
        self._catalog_observer_connection = None
        try:
            if enable_read_pool:
                from .resident_reads import ResidentReadPool

                self.read_pool = ResidentReadPool(maximum=4)
            self.security = SecurityService(self.catalog.path)
            self.replay_close_scope = (
                ReplayCloseScope.from_config(replay_scope, self.security.catalog_instance_id)
                if replay_scope is not None
                else None
            )
            self.replay_close_previews = {}
            self.replay_close_lock = RLock()
            self.close_previews = {}
            self.active_close_previews = {}
            self._close_preview_key = secrets.token_bytes(32)
            if prepared_models is None:
                from .command_schema import command_models

                prepared_models = command_models(self.registry)
            self.command_models = prepared_models
            if enable_read_pool:
                # This connection has no read transaction and carries no business
                # result. It only keeps SQLite's catalog connection lifecycle
                # stable while each request opens and validates its own connection.
                observer = ExitStack()
                try:
                    connection = observer.enter_context(
                        self.catalog.connection(read_only=True, _cross_thread=True)
                    )
                    if connection.in_transaction:
                        raise RuntimeError("Catalog observer must not hold a transaction")
                except BaseException:
                    observer.close()
                    raise
                self._catalog_observer = observer
                self._catalog_observer_connection = connection
        except BaseException:
            self.close()
            raise

    def engine(self, company_id, *, dashboard_read=False):
        return Engine(
            self.catalog.bind(
                company_id,
                read_pool=self.read_pool if dashboard_read else None,
            )
        )

    def close(self):
        if hasattr(self, "close_previews"):
            with self.security.authorization_gate:
                self.close_previews.clear()
                self.active_close_previews.clear()
        with ExitStack() as cleanup:
            if self.read_pool is not None:
                cleanup.callback(self.read_pool.close)
            if self._catalog_observer is not None:
                observer, self._catalog_observer = self._catalog_observer, None
                self._catalog_observer_connection = None
                cleanup.callback(observer.close)

    def require_active_close_preview(self, company_id, database_id, period, preview_digest):
        """Read the exact active preview under the same gate used by close commits."""
        from .close_preview import ClosePreviewIntent

        with self.security.authorization_gate:
            preview = self.close_previews.get((company_id, database_id, preview_digest))
            if (
                self.active_close_previews.get((company_id, database_id, period)) != preview_digest
                or preview is None
            ):
                raise KernelError("preview_expired", "该关账预览已失效，请重新准备并核对")
            if not isinstance(preview, ClosePreviewIntent):
                raise KernelError("content_integrity_failed", "活动关账预览内容不完整")
            review = preview.require(
                self._close_preview_key,
                company_id=company_id,
                database_id=database_id,
                period=period,
                preview_digest=preview_digest,
            )
            return preview, review

    def _remember_close_preview(self, engine, result, owner_confirmation):
        from .close_preview import make_close_preview_intent
        from .read_state import repair_revision

        company_id, database_id = engine.store.company_id, engine.store.database_id
        period, preview_digest = result["manifest"]["period"], result["digest"]
        with self.security.authorization_gate:
            # A concurrent commit may have completed after the read snapshot.
            # Never reactivate that old preview after a successful close.
            with engine.store.connection(read_only=True) as connection:
                connection.execute("BEGIN")
                current = {
                    **engine.store.epochs(connection),
                    "read_repair_revision": repair_revision(connection),
                }
                if current != result["manifest"]["read_version"]:
                    raise KernelError("preview_expired", "关账预览已变化，请重新准备并核对")
            intent = make_close_preview_intent(
                self._close_preview_key,
                manifest=result["manifest"],
                preview_digest=preview_digest,
                epochs=result["epochs"],
                owner_confirmation=owner_confirmation,
            )
            # Check the store binding too; the signed preview must originate
            # from this company, not merely contain internally matching fields.
            intent.require(
                self._close_preview_key,
                company_id=company_id,
                database_id=database_id,
                period=period,
                preview_digest=preview_digest,
            )
            key = (company_id, database_id, preview_digest)
            active_key = (company_id, database_id, period)
            previous = self.active_close_previews.get(active_key)
            if previous is not None and previous != preview_digest:
                self.close_previews.pop((company_id, database_id, previous), None)
            self.close_previews[key] = intent
            self.active_close_previews[active_key] = preview_digest
            while len(self.close_previews) > 128:
                removed_key = next(iter(self.close_previews))
                removed = self.close_previews.pop(removed_key)
                active_key = (*removed_key[:2], removed.period)
                if self.active_close_previews.get(active_key) == removed_key[2]:
                    self.active_close_previews.pop(active_key)

    def dashboard_context(self, company_id: str | None = None):
        from .dashboard import Dashboard

        companies = self.catalog.companies()
        if not companies:
            if company_id:
                raise KernelError("unknown_company", "公司尚未登记")
            return {
                "schema_version": 3,
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
        store = self.catalog._bound_store(company, read_pool=self.read_pool)
        return Dashboard(
            Engine(store),
            company_name=company["name"],
            companies=companies,
        ).context()

    def dispatch(
        self, command: str, payload: dict, *, session_token=None, response_format="native"
    ):
        """Closed set of business commands, with company binding on every request."""
        from .command_schema import validate_command
        from .response_contracts import RESPONSE_ADAPTERS, validate_response
        from .work_context_contract import WORK_CONTEXT_ADAPTER

        if response_format not in {"native", "http", "http_json"} or (
            response_format in {"http", "http_json"} and command not in _HTTP_DASHBOARD_COMMANDS
        ):
            raise KernelError("invalid_command", "响应格式不适用于此命令")

        payload = validate_command(self.command_models, command, payload, registry=self.registry)
        if command == "schema":
            return self._dispatch(command, payload)
        authority = self.security.authorize(session_token, request_id=payload.get("request_id"))
        if command in {"create_company", "restore_company", "configure_backup"}:
            with self.security.authorization_gate:
                self.security.validate_authority(authority)
                return self._dispatch(command, payload, authority=authority)
        result = self._dispatch(command, payload, authority=authority)
        try:
            if command == "work_context":
                try:
                    result = WORK_CONTEXT_ADAPTER.validate_python(result)
                except ValidationError:
                    raise KernelError(
                        "response_contract_mismatch", "读取结果不符合接口合同"
                    ) from None
            else:
                result = validate_response(command, result)
            if response_format == "http_json":
                return RESPONSE_ADAPTERS[command].dump_json(result)
            if response_format == "http":
                return RESPONSE_ADAPTERS[command].dump_python(result, mode="json")
            return result
        except Exception:
            if command in {"save_work_draft", "delete_work_draft"}:
                # The file operation already succeeded. A response failure
                # cannot be reported as a rejected, safely repeatable write.
                raise KernelError(
                    "work_draft_result_unconfirmed",
                    "工作稿已变更但响应结果未确认，请重读版本",
                    action="read_work_draft",
                ) from None
            raise

    @contextmanager
    def _commit_authority(self, authority):
        # Read snapshots and pure computation remain parallel. Revocation and
        # the final publication are ordered under the short shared identity gate.
        with self.security.authorization_gate:
            self.security.validate_authority(authority)
            yield

    def _dispatch(self, command, payload, *, authority=None):
        if command == "schema":
            from .close_contract import ADOPTION_ROLES, CLOSE_FORMAT, CLOSE_FORMAT_VERSION
            from .response_contracts import RESPONSE_ADAPTERS
            from .schema_discovery import discover_schema
            from .security.native import NativeRequest
            from .work_context_contract import WORK_CONTEXT_ADAPTER

            metadata = {
                "build_id": PROGRAM_VERSION,
                "error_handling": {
                    "version": 1,
                    "needs_information": {
                        "status": "needs_information",
                        "issues_field": "fact_issues",
                        "issue_semantics": "核算／管理语义、允许精度和可复用来源",
                        "resolution": (
                            "只在已知下一步时返回 command、fact_kind 或 candidates 单一目标"
                        ),
                    },
                    "rejected": {
                        "status": "rejected",
                        "code_field": "code",
                        "rule": (
                            "按agent_operating_protocol.core.owner_questions.missing_information处理"
                        ),
                    },
                    "lost_response": (
                        "遵守agent_operating_protocol.core.recovery"
                    ),
                    "changed_content": "遵守agent_operating_protocol.core.recovery",
                    "company_operations": "目录级创建、恢复查询 operations",
                    "jobs": "读取error_code和error_message；恢复及交付遵守deliveries专项规则",
                },
                "publication_contract": {
                    "immutable": True,
                    "chain": "one_unforked_chain_per_subject",
                    "preview_item_fields": ["source_period", "posting_period", "mode"],
                    "withdraw_preview_fields": [
                        "source_period",
                        "posting_period",
                        "mode",
                    ],
                    "fields": [
                        "id",
                        "sequence",
                        "subject_id",
                        "previous_publication_id",
                        "calculation_id",
                        "mode",
                        "posting_period",
                        "baseline_calculation_id",
                        "voucher_id",
                    ],
                    "modes": [
                        "initial",
                        "open_replace",
                        "closed_correction",
                        "review_no_impact",
                        "withdrawn",
                    ],
                    "posting_period_parameter": (
                        "开放所属月按所属月入账；所属月已关账的迟到业务和冻结结果更正"
                        "必须明确指定开放入账月；自动重算的开放依赖保留原入账月，"
                        "显式业务与指定月冲突时拒绝；无影响复核不能移动入账月。"
                    ),
                },
                "period_close_contract": {
                    "format": CLOSE_FORMAT,
                    "format_version": CLOSE_FORMAT_VERSION,
                    "immutable": True,
                    "required_fields": [
                        "format",
                        "format_version",
                        "period",
                        "company_id",
                        "database_id",
                        "previous_close_period",
                        "previous_close_digest",
                        "publication_sequence",
                        "adopted_results",
                        "vouchers",
                        "opening_calculation_id",
                        "asset_batch_adoptions",
                        "asset_card_adoptions",
                        "inventories",
                        "owner_confirmation",
                        "readiness",
                        "management_snapshot",
                        "material_coverage",
                        "trial_balance",
                        "report_classification",
                        "read_version",
                        "approval",
                        "owner_review",
                    ],
                    "optional_fields": [],
                    "adopted_result_fields": [
                        "publication_id",
                        "calculation_id",
                        "result_digest",
                        "subject_id",
                        "fact_id",
                        "source_period",
                        "posting_period",
                        "role",
                    ],
                    "adoption_roles": sorted(ADOPTION_ROLES),
                    "voucher_fields": [
                        "id",
                        "voucher_id",
                        "calculation_id",
                        "total",
                        "number",
                        "adopted_calculation_id",
                        "result_digest",
                        "reverses_id",
                    ],
                    "read_version_fields": [
                        "accounting",
                        "material",
                        "management",
                        "read_repair_revision",
                    ],
                    "authority": "direct_adoptions_and_exact_voucher_versions",
                    "dependency_semantics": "transitive_basis_follows_immutable_dependencies",
                },
                "database_formats": {
                    kind: self.bundle.database_format(kind) for kind in ("catalog", "company")
                },
                "security_operations": ["request", "status", "cancel", "session_status"],
                "replay_close_contract": {
                    "format": "ai-accounting-kernel/2/replay-close-scope/1",
                    "enabled": self.replay_close_scope is not None,
                    "scope_digest": (
                        self.replay_close_scope.scope_digest if self.replay_close_scope else None
                    ),
                    "authorization_method": "replay_scope",
                    "startup": "daemon --replay-scope <private-scope.json>",
                    "maximum_months": 120,
                    "normal_close_password_required": True,
                },
                "agent_operating_protocol": OPERATING_PROTOCOL,
                "format": 1,
                "fact_semantics": "核算字段决定计算；管理说明单独版本化；实际收付日不得由月份代替",
                "commands": sorted(self.command_models),
            }
            if payload["view"] == "full":
                metadata["security_request_schema"] = NativeRequest.model_json_schema()
                metadata["replay_close_contract"]["scope_schema"] = (
                    ReplayScopeConfig.model_json_schema()
                )
            return discover_schema(
                metadata, self.registry, self.command_models, payload,
                response_adapters={**RESPONSE_ADAPTERS, "work_context": WORK_CONTEXT_ADAPTER},
            )
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

        def draft_store(engine):
            from .work_drafts import WorkDraftStore

            return WorkDraftStore(
                self.catalog.root,
                authority.catalog_instance_id,
                engine.store.company_id,
                engine.store.database_id,
                registry=self.registry,
                commands=self.command_models,
                request_result=engine.request_result,
                request_result_snapshot=engine._request_result_snapshot,
            )

        if command in {
            "list_work_drafts", "read_work_draft", "save_work_draft", "delete_work_draft"
        }:
            # Bind every request to the current catalog record and actual
            # database identity. Draft IO never changes business audit/epochs.
            drafts = draft_store(Engine(self.catalog.bind(company_id)))
            action = getattr(drafts, {
                "list_work_drafts": "list", "read_work_draft": "read",
                "save_work_draft": "save", "delete_work_draft": "delete",
            }[command])
            if command in {"save_work_draft", "delete_work_draft"}:
                with self._commit_authority(authority):
                    return action(**data)
            return action(**data)
        engine = self.engine(company_id, dashboard_read=command in _HTTP_DASHBOARD_COMMANDS)
        engine.commit_guard = lambda: self._commit_authority(authority)
        engine.audit_actor = {
            "catalog_id": authority.catalog_instance_id,
            "owner_id": authority.owner_id,
            "session_id": authority.session_id,
            "credential_version": authority.credential_version,
        }
        if command == "work_context":
            from .work_context import WorkContext

            # Reuse the one current company/database binding. The normal page
            # path never opens a draft file, including subsequent pages.
            return WorkContext(
                engine,
                work_drafts=draft_store(engine) if data.get("include_work_draft") else None,
            ).query(**data)
        if command in {"preview_replay_close_range", "confirm_replay_close_range"}:
            replay = ReplayClose(self, engine, authority)
            if command == "preview_replay_close_range":
                return replay.preview(**data)
            with self.replay_close_lock:
                return replay.confirm(**data)
        if command == "close" and data.get("backup_directory") is None:
            setting = self.catalog.company_settings(company_id)
            data["backup_directory"] = setting["backup_directory"] or str(
                self.catalog.root / "backups" / engine.store.path.parent.name
            )
        approval_id = data.pop("approval_id", None) if command == "close" else None

        def authorize_close(connection, period, preview_digest, epochs):
            self.security.validate_authority(authority)
            self.require_active_close_preview(
                company_id, engine.store.database_id, period, preview_digest
            )
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

        periods = Periods(engine, authorize_close=authorize_close)
        exports = Exports(engine)
        reports = Reports(engine)
        workflow = Workflow(engine)
        materials = Materials(engine, inspection_cache=self.inspection_cache)
        discovery = Discovery(engine)
        duplicates = Duplicates(engine)
        entities = Entities(engine)
        identities = IdentityCorrections(engine)
        display = Display(engine)
        from .dashboard import Dashboard

        dashboard = Dashboard(
            engine, owner_review_request=CloseReview(self, engine).owner_review_request
        )
        business_queries = BusinessQueries(engine)
        payroll_preparation = PayrollPreparation(engine)
        asset_batches = AssetBatches(engine)
        tax_import = TaxImport(engine)
        from .maintenance import Maintenance

        maintenance = Maintenance(engine)
        actions = {
            "prepare_fact_registration": duplicates.prepare_fact_registration,
            "find_entities": entities.find_entities,
            "register_entity": entities.register_entity,
            "update_entity_profile": entities.update_entity_profile,
            "register_employee": entities.register_employee,
            "change_employee_employment": entities.change_employee_employment,
            "employee_employment_history": entities.employee_employment_history,
            "correct_fund_account_identity": entities.correct_fund_account_identity,
            "preview_identity_correction": identities.preview_identity_correction,
            "confirm_identity_correction": identities.confirm_identity_correction,
            "save_fact": engine.save_fact,
            "amend_fact": engine.amend_fact,
            "save_facts": engine.save_facts,
            "preview": engine.preview,
            "confirm": engine.confirm,
            "overview": engine.overview,
            "jobs": engine.jobs,
            "request_result": engine.request_result,
            "retry_job": engine.retry_job,
            "ledger": engine.ledger,
            "trace": engine.trace,
            "business_status": business_queries.business_status,
            "period_readiness": business_queries.period_readiness,
            "management": periods.management,
            "inventory": periods.inventory,
            "preview_close": periods.preview_close,
            "close": periods.close,
            "closed_report": periods.closed_report,
            "rebuild": engine.rebuild_projections,
            "verify_integrity": maintenance.verify_integrity,
            "repair_read_indexes": maintenance.repair_read_indexes,
            "rebuild_dashboard_read_model": maintenance.rebuild_dashboard_read_model,
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
            "dashboard_brief_group": dashboard.brief_group,
            "dashboard_funds": dashboard.funds,
            "dashboard_employees": dashboard.employees,
            "dashboard_assets": dashboard.assets,
            "dashboard_business_status": dashboard.business_status,
            "dashboard_quarterly_report": dashboard.quarterly_report,
            "dashboard_period_preparation": dashboard.period_preparation,
            "dashboard_close_review": CloseReview(self, engine).read,
            "find_facts": discovery.find_facts,
            "payroll_reuse_basis": payroll_preparation.reuse_basis,
            "prepare_payroll": payroll_preparation.prepare,
            "confirm_payroll_preparation": payroll_preparation.confirm,
            "prepare_asset_activation_batch": asset_batches.prepare_activation_batch,
            "confirm_asset_activation_batch": asset_batches.confirm_activation_batch,
            "prepare_asset_consumption_month": asset_batches.prepare_consumption_month,
            "confirm_asset_consumption_month": asset_batches.confirm_consumption_month,
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
                "backups": run_backup_jobs(engine.store.path, _bundle=engine.store.bundle, **data),
                "exports": run_export_jobs(engine, **data),
                "reports": run_report_jobs(engine, **data),
            }
        if command == "run_export_jobs":
            return run_export_jobs(engine, **data)
        if command == "run_report_jobs":
            return run_report_jobs(engine, **data)
        if command not in actions:
            raise KernelError("unknown_command", "不支持该业务命令")
        if command == "close":
            with self.security.authorization_gate:
                result = actions[command](**data)
                active_key = (company_id, engine.store.database_id, data["period"])
                if self.active_close_previews.get(active_key) == data["preview_digest"]:
                    self.active_close_previews.pop(active_key)
                self.close_previews.pop(
                    (company_id, engine.store.database_id, data["preview_digest"]), None
                )
                return result
        result = actions[command](**data)
        if command == "preview_close":
            self._remember_close_preview(engine, result, data["owner_confirmation"])
            result = {
                **result,
                "review_locator": {
                    "company_id": company_id,
                    "database_id": engine.store.database_id,
                    "period": data["period"],
                    "preview_digest": result["digest"],
                },
            }
        return result
