"""Deterministic, catalog-owned company lifecycle workflows."""

from __future__ import annotations

import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from alembic.config import Config
from sqlalchemy import create_engine, func, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from alembic import command

from .accounting_periods import canonical_sha256, china_current_date
from .coa import seed_organization
from .company_router import (
    CompanyDatabaseRouter,
    CompanyRoutingError,
    assert_provisioning_role,
    catalog_instance_id,
    grant_runtime_database_access,
    router,
)
from .company_schemas import (
    ConfirmCompanyProfileChangeRequest,
    ConfirmCompanyStatusChangeRequest,
    CreateCompanyRequest,
    PreviewCompanyProfileChangeRequest,
    PreviewCompanyStatusChangeRequest,
)
from .execution_attribution import persist_execution_attribution
from .identity import ExecutionContext
from .models import (
    AccountingPeriod,
    CompanyLifecycleAction,
    CompanyRegistry,
    Evidence,
    Organization,
    OrganizationDatabaseMetadata,
    OrganizationProfileVersion,
    OrganizationProfileVersionEvidence,
    TaxPeriod,
    utcnow,
)

_ROOT = Path(__file__).resolve().parents[2]


class CompanyLifecycleError(ValueError):
    """Stable public failure that never includes SQL or credentials."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class CompanyService:
    def __init__(
        self,
        catalog_session: Session,
        *,
        context: ExecutionContext,
        database_router: CompanyDatabaseRouter | None = None,
    ) -> None:
        self.catalog_session = catalog_session
        self.context = context
        self.router = database_router or router

    def list_companies(self, *, include_archived: bool = False) -> dict[str, Any]:
        statuses = ["provisioning", "active", "changing", "attention_required"]
        if include_archived:
            statuses.append("archived")
        companies = self.catalog_session.scalars(
            select(CompanyRegistry)
            .where(CompanyRegistry.status.in_(statuses))
            .order_by(
                CompanyRegistry.is_primary.desc(),
                CompanyRegistry.display_name,
                CompanyRegistry.org_id,
            )
        ).all()
        return {
            "status": "ok",
            "companies": [self._registry_payload(item) for item in companies],
        }

    def create_company(self, request: CreateCompanyRequest) -> dict[str, Any]:
        catalog_id = catalog_instance_id(self.catalog_session)
        org_id = uuid.uuid5(catalog_id, f"finance-create-company:{request.idempotency_key}")
        database_identity = uuid.uuid5(org_id, "finance-company-database")
        database_name = f"finance_company_{org_id.hex}"
        payload = request.model_dump(mode="json")
        payload_hash = canonical_sha256(payload)
        self._lock_catalog_key(
            f"company-action:{org_id}:create:{request.idempotency_key}"
        )
        self._lock_catalog_key(
            f"company-taxpayer:{request.taxpayer_identification_number}"
        )
        self._lock_catalog_key("company-primary")
        duplicate_org_id = self.catalog_session.scalar(
            select(CompanyRegistry.org_id).where(
                CompanyRegistry.taxpayer_identification_number
                == request.taxpayer_identification_number,
                CompanyRegistry.org_id != org_id,
            )
        )
        if duplicate_org_id is not None:
            return self._rejected("TAXPAYER_IDENTIFICATION_NUMBER_ALREADY_EXISTS")
        existing_action = self._existing_action(
            org_id=org_id,
            action_type="create",
            idempotency_key=request.idempotency_key,
        )
        if existing_action is not None:
            if existing_action.request_payload_hash != payload_hash:
                return self._rejected("IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_REQUEST")
            if existing_action.status == "completed":
                return self._replay(existing_action, payload_hash)
            registry = self.catalog_session.get(CompanyRegistry, org_id)
            if registry is None:
                return self._rejected("COMPANY_PROVISIONING_STATE_MISSING")
            action = existing_action
            action.status = "started"
            action.error_code = None
            action.completed_at = None
            registry.status = "provisioning"
            registry.archived_at = None
            registry.updated_at = utcnow()
        else:
            registry = CompanyRegistry(
                org_id=org_id,
                database_name=database_name,
                database_identity=database_identity,
                status="provisioning",
                display_name=request.name,
                taxpayer_identification_number=request.taxpayer_identification_number,
                profile_effective_from=request.effective_from,
                filing_cycle=request.filing_cycle,
                urban_maintenance_rate=request.urban_maintenance_rate,
            )
            action = self._new_action(
                org_id=org_id,
                action_type="create",
                idempotency_key=request.idempotency_key,
                payload_hash=payload_hash,
                input_facts=payload,
            )
            self.catalog_session.add(registry)
            self.catalog_session.flush()
            self.catalog_session.add(action)
        self.catalog_session.flush()
        try:
            self._create_physical_database(database_name)
            self._upgrade_business_database(
                database_name,
                org_id=org_id,
                database_identity=database_identity,
                catalog_id=catalog_id,
            )
            self._initialize_business_database(
                registry=registry,
                request=request,
                action=action,
                catalog_id=catalog_id,
            )
            self._grant_runtime_access(database_name)
            self.router.dispose()
            has_primary = self.catalog_session.scalar(
                select(CompanyRegistry.org_id)
                .where(
                    CompanyRegistry.org_id != registry.org_id,
                    CompanyRegistry.is_primary.is_(True),
                )
                .limit(1)
            )
            if request.make_primary or has_primary is None:
                self.catalog_session.execute(
                    update(CompanyRegistry)
                    .where(
                        CompanyRegistry.org_id != registry.org_id,
                        CompanyRegistry.is_primary.is_(True),
                    )
                    .values(is_primary=False, updated_at=utcnow())
                )
                registry.is_primary = True
            registry.status = "active"
            registry.updated_at = utcnow()
            action.status = "completed"
            action.completed_at = utcnow()
            self.catalog_session.flush()
            return {
                "status": "created",
                "company": self._registry_payload(registry),
                "lifecycle_action_id": str(action.id),
            }
        except (CompanyLifecycleError, CompanyRoutingError) as exc:
            registry.status = "attention_required"
            registry.updated_at = utcnow()
            action.status = "failed"
            action.error_code = exc.code
            action.completed_at = utcnow()
            self.catalog_session.flush()
            return self._rejected(exc.code, org_id=org_id)
        except (OSError, SQLAlchemyError):
            registry.status = "attention_required"
            registry.updated_at = utcnow()
            action.status = "failed"
            action.error_code = "COMPANY_PROVISIONING_FAILED"
            action.completed_at = utcnow()
            self.catalog_session.flush()
            return self._rejected("COMPANY_PROVISIONING_FAILED", org_id=org_id)

    def preview_profile_change(
        self, request: PreviewCompanyProfileChangeRequest
    ) -> dict[str, Any]:
        registry = self.router.resolve(self.catalog_session, request.org_id, for_write=True)
        with self.router.factory_for(registry)() as business_session:
            facts = self._profile_preview_facts(business_session, request)
        calculation_hash = canonical_sha256(facts)
        return {
            "status": "calculated",
            "calculation_hash": calculation_hash,
            "company": facts["target_profile"],
            "affected_future_periods": facts["affected_future_periods"],
        }

    def confirm_profile_change(
        self, request: ConfirmCompanyProfileChangeRequest
    ) -> dict[str, Any]:
        payload_hash = canonical_sha256(request.model_dump(mode="json"))
        self._lock_catalog_key(
            f"company-action:{request.org_id}:profile_change:{request.idempotency_key}"
        )
        self._lock_catalog_key(
            f"company-taxpayer:{request.taxpayer_identification_number}"
        )
        existing = self._existing_action(
            org_id=request.org_id,
            action_type="profile_change",
            idempotency_key=request.idempotency_key,
        )
        action: CompanyLifecycleAction | None = None
        if existing is not None:
            if existing.request_payload_hash != payload_hash:
                return self._rejected("IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_REQUEST")
            if existing.status == "completed":
                return self._replay(existing, payload_hash)
            if existing.status != "failed":
                return self._rejected("LIFECYCLE_ACTION_IN_PROGRESS")
            registry = self.catalog_session.scalar(
                select(CompanyRegistry)
                .where(CompanyRegistry.org_id == request.org_id)
                .with_for_update()
            )
            if registry is None or registry.status != "attention_required":
                return self._rejected("COMPANY_PROFILE_RETRY_STATE_INVALID")
            action = existing
        else:
            registry = self.router.resolve(
                self.catalog_session, request.org_id, for_write=True
            )
        with self.router.factory_for(registry)() as business_session:
            preview_facts = self._profile_preview_facts(business_session, request)
        if canonical_sha256(preview_facts) != request.calculation_hash:
            return self._rejected("CALCULATION_HASH_MISMATCH")
        if action is None:
            action = self._new_action(
                org_id=request.org_id,
                action_type="profile_change",
                idempotency_key=request.idempotency_key,
                payload_hash=payload_hash,
                input_facts=request.model_dump(mode="json"),
                calculation_hash=request.calculation_hash,
            )
            self.catalog_session.add(action)
        else:
            action.status = "started"
            action.error_code = None
            action.completed_at = None
        self.catalog_session.flush()
        registry.status = "changing"
        registry.updated_at = utcnow()
        self.catalog_session.flush()
        try:
            with self.router.factory_for(registry).begin() as business_session:
                facts = self._profile_preview_facts(business_session, request)
                if canonical_sha256(facts) != request.calculation_hash:
                    raise CompanyLifecycleError("CALCULATION_HASH_MISMATCH")
                attribution_context = replace(self.context, org_id=request.org_id)
                with persist_execution_attribution(
                    business_session,
                    context=attribution_context,
                    tool_name="finance_confirm_company_profile_change",
                ) as attribution:
                    profile = OrganizationProfileVersion(
                        org_id=request.org_id,
                        effective_from=request.effective_from,
                        name=request.name,
                        taxpayer_identification_number=request.taxpayer_identification_number,
                        taxpayer_type="small_scale",
                        filing_cycle=request.filing_cycle,
                        jurisdiction="CN",
                        urban_maintenance_rate=request.urban_maintenance_rate,
                        accounting_standard="small_enterprise",
                        confirmation_note=request.confirmation_note,
                        lifecycle_action_id=action.id,
                        execution_attribution_id=attribution.id,
                    )
                    business_session.add(profile)
                    business_session.flush()
                    business_session.add_all(
                        OrganizationProfileVersionEvidence(
                            org_id=request.org_id,
                            profile_version_id=profile.id,
                            evidence_id=evidence_id,
                        )
                        for evidence_id in sorted(
                            set(request.evidence_references), key=lambda item: item.hex
                        )
                    )
                    organization = business_session.get(Organization, request.org_id)
                    if organization is None:
                        raise CompanyLifecycleError("ORGANIZATION_NOT_FOUND")
                    organization.name = request.name
                    organization.taxpayer_identification_number = (
                        request.taxpayer_identification_number
                    )
                    organization.filing_cycle = request.filing_cycle
                    organization.urban_maintenance_rate = request.urban_maintenance_rate
            registry.display_name = request.name
            registry.taxpayer_identification_number = request.taxpayer_identification_number
            registry.profile_effective_from = request.effective_from
            registry.filing_cycle = request.filing_cycle
            registry.urban_maintenance_rate = request.urban_maintenance_rate
            registry.status = "active"
            registry.updated_at = utcnow()
            action.status = "completed"
            action.completed_at = utcnow()
            self.catalog_session.flush()
            return {
                "status": "confirmed",
                "company": self._registry_payload(registry),
                "lifecycle_action_id": str(action.id),
            }
        except CompanyLifecycleError as exc:
            registry.status = "attention_required"
            registry.updated_at = utcnow()
            action.status = "failed"
            action.error_code = exc.code
            action.completed_at = utcnow()
            self.catalog_session.flush()
            return self._rejected(exc.code, org_id=request.org_id)
        except (OSError, SQLAlchemyError):
            registry.status = "attention_required"
            registry.updated_at = utcnow()
            action.status = "failed"
            action.error_code = "COMPANY_PROFILE_CHANGE_FAILED"
            action.completed_at = utcnow()
            self.catalog_session.flush()
            return self._rejected("COMPANY_PROFILE_CHANGE_FAILED", org_id=request.org_id)

    def preview_status_change(
        self, request: PreviewCompanyStatusChangeRequest
    ) -> dict[str, Any]:
        registry = self.catalog_session.scalar(
            select(CompanyRegistry)
            .where(CompanyRegistry.org_id == request.org_id)
            .with_for_update()
        )
        if registry is None:
            return self._rejected("ORGANIZATION_NOT_FOUND")
        if registry.status not in {"active", "archived"}:
            return self._rejected("COMPANY_STATUS_CHANGE_NOT_ALLOWED")
        if registry.status == request.target_status:
            return self._rejected("COMPANY_ALREADY_IN_TARGET_STATUS")
        facts = {
            "org_id": str(request.org_id),
            "current_status": registry.status,
            "target_status": request.target_status,
            "confirmation_note": request.confirmation_note,
        }
        return {
            "status": "calculated",
            "calculation_hash": canonical_sha256(facts),
            "change": facts,
        }

    def confirm_status_change(
        self, request: ConfirmCompanyStatusChangeRequest
    ) -> dict[str, Any]:
        payload_hash = canonical_sha256(request.model_dump(mode="json"))
        self._lock_catalog_key(
            f"company-action:{request.org_id}:status_change:{request.idempotency_key}"
        )
        existing = self._existing_action(
            org_id=request.org_id,
            action_type="status_change",
            idempotency_key=request.idempotency_key,
        )
        if existing is not None:
            return self._replay(existing, payload_hash)
        preview = self.preview_status_change(request)
        if preview["status"] != "calculated":
            return preview
        if preview["calculation_hash"] != request.calculation_hash:
            return self._rejected("CALCULATION_HASH_MISMATCH")
        registry = self.catalog_session.scalar(
            select(CompanyRegistry)
            .where(CompanyRegistry.org_id == request.org_id)
            .with_for_update()
        )
        assert registry is not None
        action = self._new_action(
            org_id=request.org_id,
            action_type="status_change",
            idempotency_key=request.idempotency_key,
            payload_hash=payload_hash,
            input_facts=request.model_dump(mode="json"),
            calculation_hash=request.calculation_hash,
        )
        action.status = "completed"
        action.completed_at = utcnow()
        self.catalog_session.add(action)
        registry.status = request.target_status
        registry.archived_at = utcnow() if request.target_status == "archived" else None
        registry.updated_at = utcnow()
        self.catalog_session.flush()
        return {
            "status": "confirmed",
            "company": self._registry_payload(registry),
            "lifecycle_action_id": str(action.id),
        }

    def _profile_preview_facts(
        self,
        business_session: Session,
        request: PreviewCompanyProfileChangeRequest | ConfirmCompanyProfileChangeRequest,
    ) -> dict[str, Any]:
        if request.effective_from <= china_current_date() or request.effective_from.day != 1:
            raise CompanyLifecycleError("PROFILE_EFFECTIVE_DATE_MUST_BE_FUTURE_PERIOD_BOUNDARY")
        duplicate_taxpayer = self.catalog_session.scalar(
            select(CompanyRegistry.org_id).where(
                CompanyRegistry.taxpayer_identification_number
                == request.taxpayer_identification_number,
                CompanyRegistry.org_id != request.org_id,
            )
        )
        if duplicate_taxpayer is not None:
            raise CompanyLifecycleError("TAXPAYER_IDENTIFICATION_NUMBER_ALREADY_EXISTS")
        current = business_session.scalar(
            select(OrganizationProfileVersion)
            .where(OrganizationProfileVersion.org_id == request.org_id)
            .order_by(OrganizationProfileVersion.effective_from.desc())
            .limit(1)
        )
        if current is None:
            raise CompanyLifecycleError("ORGANIZATION_PROFILE_NOT_INITIALIZED")
        if request.effective_from <= current.effective_from:
            raise CompanyLifecycleError("PROFILE_EFFECTIVE_DATE_NOT_AFTER_LATEST_VERSION")
        tax_profile_changes = (
            current.filing_cycle != request.filing_cycle
            or current.urban_maintenance_rate != request.urban_maintenance_rate
        )
        if (
            tax_profile_changes
            and "quarterly" in {current.filing_cycle, request.filing_cycle}
            and request.effective_from.month not in {1, 4, 7, 10}
        ):
            raise CompanyLifecycleError("PROFILE_EFFECTIVE_DATE_NOT_TAX_PERIOD_BOUNDARY")
        if business_session.scalar(
            select(OrganizationProfileVersion.id).where(
                OrganizationProfileVersion.org_id == request.org_id,
                OrganizationProfileVersion.effective_from == request.effective_from,
            )
        ):
            raise CompanyLifecycleError("PROFILE_EFFECTIVE_DATE_ALREADY_EXISTS")
        closed_overlap = business_session.scalar(
            select(func.count(AccountingPeriod.id)).where(
                AccountingPeriod.org_id == request.org_id,
                AccountingPeriod.status == "closed",
                AccountingPeriod.end_date >= request.effective_from,
            )
        )
        confirmed_tax_overlap = business_session.scalar(
            select(func.count(TaxPeriod.id)).where(
                TaxPeriod.org_id == request.org_id,
                TaxPeriod.status == "posted",
                TaxPeriod.end_date >= request.effective_from,
            )
        )
        if closed_overlap or confirmed_tax_overlap:
            raise CompanyLifecycleError("PROFILE_CHANGE_WOULD_AFFECT_CONFIRMED_PERIOD")
        evidence_ids = sorted(set(request.evidence_references), key=lambda item: item.hex)
        evidence_count = business_session.scalar(
            select(func.count(Evidence.id)).where(
                Evidence.org_id == request.org_id,
                Evidence.id.in_(evidence_ids),
            )
        )
        if evidence_count != len(evidence_ids):
            raise CompanyLifecycleError("EVIDENCE_REFERENCE_NOT_FOUND")
        future_periods = business_session.scalars(
            select(AccountingPeriod)
            .where(
                AccountingPeriod.org_id == request.org_id,
                AccountingPeriod.status == "open",
                AccountingPeriod.start_date >= request.effective_from,
            )
            .order_by(AccountingPeriod.start_date)
        ).all()
        return {
            "org_id": str(request.org_id),
            "current_profile_version_id": str(current.id),
            "target_profile": {
                "name": request.name,
                "taxpayer_identification_number": request.taxpayer_identification_number,
                "effective_from": request.effective_from.isoformat(),
                "filing_cycle": request.filing_cycle,
                "urban_maintenance_rate": str(request.urban_maintenance_rate),
                "taxpayer_type": "small_scale",
                "jurisdiction": "CN",
                "accounting_standard": "small_enterprise",
                "confirmation_note": request.confirmation_note,
                "evidence_references": [str(item) for item in evidence_ids],
            },
            "affected_future_periods": [
                {
                    "start_date": period.start_date.isoformat(),
                    "end_date": period.end_date.isoformat(),
                }
                for period in future_periods
            ],
        }

    def _initialize_business_database(
        self,
        *,
        registry: CompanyRegistry,
        request: CreateCompanyRequest,
        action: CompanyLifecycleAction,
        catalog_id: uuid.UUID,
    ) -> None:
        database_url = self.router.company_url(
            registry.database_name, migration=True
        ).render_as_string(hide_password=False)
        engine = create_engine(database_url)
        try:
            with Session(engine) as session, session.begin():
                metadata = session.get(OrganizationDatabaseMetadata, 1)
                if metadata is not None:
                    if (
                        metadata.org_id != registry.org_id
                        or metadata.database_identity != registry.database_identity
                    ):
                        raise CompanyLifecycleError("COMPANY_DATABASE_IDENTITY_MISMATCH")
                    return
                if session.scalar(select(func.count(Organization.id))) != 0:
                    raise CompanyLifecycleError("COMPANY_DATABASE_NOT_EMPTY")
                organization = seed_organization(
                    session,
                    org_id=registry.org_id,
                    name=request.name,
                    taxpayer_identification_number=request.taxpayer_identification_number,
                    filing_cycle=request.filing_cycle,
                    jurisdiction="CN",
                    urban_maintenance_rate=request.urban_maintenance_rate,
                )
                session.add(
                    OrganizationDatabaseMetadata(
                        singleton_key=1,
                        org_id=organization.id,
                        database_identity=registry.database_identity,
                        current_catalog_instance_id=catalog_id,
                        owner_approval_required=True,
                    )
                )
                session.flush()
                initial_context = replace(self.context, org_id=registry.org_id)
                with persist_execution_attribution(
                    session,
                    context=initial_context,
                    tool_name="finance_create_company",
                ) as attribution:
                    session.add(
                        OrganizationProfileVersion(
                            org_id=organization.id,
                            effective_from=request.effective_from,
                            name=request.name,
                            taxpayer_identification_number=(
                                request.taxpayer_identification_number
                            ),
                            taxpayer_type="small_scale",
                            filing_cycle=request.filing_cycle,
                            jurisdiction="CN",
                            urban_maintenance_rate=request.urban_maintenance_rate,
                            accounting_standard="small_enterprise",
                            confirmation_note=request.confirmation_note,
                            lifecycle_action_id=action.id,
                            execution_attribution_id=attribution.id,
                        )
                    )
        finally:
            engine.dispose()

    def _create_physical_database(self, database_name: str) -> None:
        settings = self.router.settings
        if not self.router.enabled or settings.finance_provisioning_database_url is None:
            raise CompanyLifecycleError("COMPANY_PROVISIONING_NOT_CONFIGURED")
        self.router.validate_database_name(database_name)
        engine = create_engine(
            settings.finance_provisioning_database_url,
            isolation_level="AUTOCOMMIT",
        )
        try:
            with engine.connect() as connection:
                if settings.finance_environment == "production":
                    assert_provisioning_role(connection)
                exists = connection.scalar(
                    text("SELECT 1 FROM pg_database WHERE datname = :database_name"),
                    {"database_name": database_name},
                )
                if exists is None:
                    connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
        finally:
            engine.dispose()

    def _grant_runtime_access(self, database_name: str) -> None:
        settings = self.router.settings
        if settings.finance_environment != "production":
            return
        migration_url = self.router.company_url(
            database_name, migration=True
        ).render_as_string(hide_password=False)
        runtime_url = self.router.company_url(database_name)
        if runtime_url.username is None:
            raise CompanyLifecycleError("COMPANY_RUNTIME_ACCOUNT_INVALID")
        engine = create_engine(migration_url)
        try:
            with engine.begin() as connection:
                grant_runtime_database_access(connection, runtime_url.username)
        finally:
            engine.dispose()

    def _upgrade_business_database(
        self,
        database_name: str,
        *,
        org_id: uuid.UUID,
        database_identity: uuid.UUID,
        catalog_id: uuid.UUID,
    ) -> None:
        config = Config(str(_ROOT / "alembic.ini"))
        database_url = self.router.company_url(
            database_name, migration=True
        ).render_as_string(hide_password=False)
        config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
        config.attributes["database_url_override"] = database_url
        config.attributes["company_org_id"] = org_id
        config.attributes["company_database_identity"] = database_identity
        config.attributes["catalog_instance_id"] = catalog_id
        config.attributes["identity_split_verified"] = True
        command.upgrade(config, "head")

    def _new_action(
        self,
        *,
        org_id: uuid.UUID,
        action_type: str,
        idempotency_key: str,
        payload_hash: str,
        input_facts: dict[str, Any],
        calculation_hash: str | None = None,
    ) -> CompanyLifecycleAction:
        return CompanyLifecycleAction(
            org_id=org_id,
            action_type=action_type,
            idempotency_key=idempotency_key,
            request_payload_hash=payload_hash,
            status="started",
            input_facts=input_facts,
            calculation_hash=calculation_hash,
            owner_account_id=self.context.owner_account_id,
            owner_session_id=self.context.owner_session_id,
            owner_credential_version=self.context.owner_credential_version,
            executor_kind=self.context.executor_kind.value,
            executor_name=self.context.executor_name,
            executor_version=self.context.executor_version,
        )

    def _existing_action(
        self,
        *,
        org_id: uuid.UUID,
        action_type: str,
        idempotency_key: str,
    ) -> CompanyLifecycleAction | None:
        return self.catalog_session.scalar(
            select(CompanyLifecycleAction).where(
                CompanyLifecycleAction.org_id == org_id,
                CompanyLifecycleAction.action_type == action_type,
                CompanyLifecycleAction.idempotency_key == idempotency_key,
            )
        )

    def _lock_catalog_key(self, key: str) -> None:
        if self.catalog_session.get_bind().dialect.name == "postgresql":
            self.catalog_session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": key},
            )

    def _replay(self, action: CompanyLifecycleAction, payload_hash: str) -> dict[str, Any]:
        if action.request_payload_hash != payload_hash:
            return self._rejected("IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_REQUEST")
        if action.status == "failed":
            return self._rejected(action.error_code or "LIFECYCLE_ACTION_FAILED")
        if action.status != "completed":
            return self._rejected("LIFECYCLE_ACTION_IN_PROGRESS")
        registry = self.catalog_session.get(CompanyRegistry, action.org_id)
        return {
            "status": "confirmed" if action.action_type != "create" else "created",
            "idempotent_replay": True,
            "company": self._registry_payload(registry) if registry is not None else None,
            "lifecycle_action_id": str(action.id),
        }

    @staticmethod
    def _registry_payload(registry: CompanyRegistry) -> dict[str, Any]:
        return {
            "org_id": str(registry.org_id),
            "name": registry.display_name,
            "taxpayer_identification_number": registry.taxpayer_identification_number,
            "status": registry.status,
            "is_primary": registry.is_primary,
            "profile_effective_from": registry.profile_effective_from.isoformat(),
            "filing_cycle": registry.filing_cycle,
            "urban_maintenance_rate": str(registry.urban_maintenance_rate),
            "archived_at": registry.archived_at.isoformat() if registry.archived_at else None,
        }

    @staticmethod
    def _rejected(code: str, *, org_id: uuid.UUID | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {"status": "rejected", "errors": [code]}
        if org_id is not None:
            result["org_id"] = str(org_id)
        return result
