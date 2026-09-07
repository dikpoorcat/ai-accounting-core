from __future__ import annotations

import hashlib
import io
import shutil
import tarfile
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_enterprise_income_tax import change, confirm, root
from testcontainers.community.postgres import PostgresContainer

from ai_accounting.accounting_period_schemas import GenerateAccountingPeriodRequest
from ai_accounting.accounting_period_service import AccountingPeriodService
from ai_accounting.coa import seed_organization
from ai_accounting.enterprise_income_tax import EnterpriseIncomeTaxService
from ai_accounting.enterprise_income_tax_schemas import ConfirmEnterpriseIncomeTaxResultRequest
from ai_accounting.execution_attribution import persist_execution_attribution
from ai_accounting.identity import ExecutionContext, ExecutorKind
from ai_accounting.models import (
    EnterpriseIncomeTaxResult,
    Evidence,
    Organization,
    OrganizationDatabaseMetadata,
)
from alembic import command

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.postgres_current,
    pytest.mark.skipif(shutil.which("docker") is None, reason="Docker required"),
]
IMAGE = "postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193"


def test_migration_atomic_correction_concurrency_and_restore(tmp_path, monkeypatch):
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    evidence_path = evidence_root / "pg-cit.txt"
    evidence_bytes = b"pg-cit.txt"
    evidence_path.write_bytes(evidence_bytes)

    def make_evidence(session, org):
        evidence = Evidence(
            org_id=org.id,
            sha256=hashlib.sha256(evidence_bytes).hexdigest(),
            original_name="pg-cit.txt",
            media_type="text/plain",
            source="test",
            size_bytes=len(evidence_bytes),
            storage_path=str(evidence_path),
            metadata_json={},
        )
        session.add(evidence)
        session.flush()
        return evidence

    with PostgresContainer(IMAGE, driver="psycopg") as postgres:
        url = postgres.get_connection_url(driver="psycopg")
        config = Config("alembic.ini")
        config.attributes["database_url_override"] = url
        command.upgrade(config, "0001_business_baseline_v2")
        engine = create_engine(url)
        catalog_id = uuid.uuid4()
        with Session(engine) as session, session.begin():
            org = seed_organization(
                session,
                name="所得税迁移验证",
                taxpayer_identification_number="91330106MA1234567T",
                accounting_period_control_enabled=False,
            )
            org_id = org.id
            session.add(
                OrganizationDatabaseMetadata(
                    singleton_key=1,
                    org_id=org_id,
                    database_identity=uuid.uuid4(),
                    current_catalog_instance_id=catalog_id,
                    owner_approval_required=True,
                )
            )
        context = ExecutionContext(
            org_id=org_id,
            owner_account_id=uuid.uuid4(),
            owner_session_id=uuid.uuid4(),
            owner_credential_version=1,
            executor_kind=ExecutorKind.AI_AGENT,
            executor_name="cit-test",
            executor_version="1",
            request_correlation_id=uuid.uuid4(),
            catalog_instance_id=catalog_id,
        )
        with (
            Session(engine) as session,
            session.begin(),
            persist_execution_attribution(
                session, context=context, tool_name="finance_confirm_enterprise_income_tax_quarter"
            ),
        ):
            org = session.get(Organization, org_id)
            evidence = make_evidence(session, org)
            for month in (6, 7, 8):
                generated = AccountingPeriodService(session).generate_accounting_period(
                    GenerateAccountingPeriodRequest(
                        org_id=org_id,
                        period_month=f"2026-{month:02d}",
                        idempotency_key=f"month-{month}",
                        confirmation_note="测试生成期间",
                        evidence_references=[evidence.id],
                    )
                )
                assert generated.status == "posted", generated
            # The old quarterly tool is exercised before the forward migration.
            # It only takes an advisory lock, and does not query the new tables.
            source = root(session, org, evidence, amount=0)
            request = change(org, evidence, source)
        command.upgrade(config, "head")
        command.check(config)
        with (
            Session(engine) as session,
            session.begin(),
            persist_execution_attribution(
                session,
                context=replace(context, request_correlation_id=uuid.uuid4()),
                tool_name="finance_confirm_enterprise_income_tax_result",
            ),
        ):
            first, _ = confirm(EnterpriseIncomeTaxService(session), request)
            replacement = request.model_copy(
                update={
                    "previous_result_id": uuid.UUID(first["result_id"]),
                    "declared_tax_fen": 16000,
                    "declaration_date": date(2026, 8, 6),
                    "posting_date": date(2026, 8, 6),
                }
            )
            # model_copy does not validate UUIDs; revalidate the public envelope.
            replacement = type(request).model_validate(replacement.model_dump(mode="json"))
            preview = EnterpriseIncomeTaxService(session).preview(replacement)

        def worker(key):
            with (
                Session(engine) as session,
                session.begin(),
                persist_execution_attribution(
                    session,
                    context=replace(context, request_correlation_id=uuid.uuid4()),
                    tool_name="finance_confirm_enterprise_income_tax_result",
                ),
            ):
                return EnterpriseIncomeTaxService(session).confirm(
                    ConfirmEnterpriseIncomeTaxResultRequest.model_validate(
                        replacement.model_dump()
                        | {"calculation_hash": preview["calculation_hash"], "idempotency_key": key}
                    )
                )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(worker, ["concurrent-a", "concurrent-b"]))
        assert sum(r["status"] == "posted" for r in results) == 1, results
        with engine.begin() as connection:
            assert (
                connection.scalar(text("SELECT count(*) FROM enterprise_income_tax_results")) == 2
            )
            assert connection.scalar(
                text("SELECT finance_cit_confirmation_effective(:id, '2026-06-30')"), {"id": source}
            )
            with pytest.raises(DBAPIError, match="CIT_FACT_IMMUTABLE"), connection.begin_nested():
                connection.execute(
                    text("UPDATE enterprise_income_tax_results SET target_tax_fen=1")
                )
        # Logical dump/restore exercises the actual schema, immutable rows, foreign
        # keys, corrected vouchers and functions in a second isolated database.
        container = postgres.get_wrapped_container()

        def run(args):
            result = container.exec_run(args)
            assert result.exit_code == 0, result.output.decode(errors="replace")
            return result.output

        run(["pg_dump", "-U", postgres.username, "-Fc", "-f", "/tmp/cit.dump", postgres.dbname])
        from ai_accounting.backup import (
            BackupPrecondition,
            BackupRequest,
            DatabaseDumpMetadata,
            EvidenceSnapshot,
            create_portable_backup_archive,
            create_stopped_backup,
            verify_portable_backup_archive,
        )

        chunks, _ = container.get_archive("/tmp/cit.dump")
        with tarfile.open(fileobj=io.BytesIO(b"".join(chunks))) as tar:
            dump_bytes = tar.extractfile(tar.getmembers()[0]).read()

        class DumpAdapter:
            def dump(self, destination, metadata):
                destination.write_bytes(dump_bytes)

            def list_archive(self, archive):
                assert archive.read_bytes() == dump_bytes
                return run(["pg_restore", "--list", "/tmp/cit.dump"]).decode().splitlines()

        with Session(engine) as session:
            evidence = session.scalar(select(Evidence))
            database_id = session.scalar(select(OrganizationDatabaseMetadata.database_identity))
            snapshot = EvidenceSnapshot(
                evidence_id=str(evidence.id),
                sha256=evidence.sha256,
                size_bytes=evidence.size_bytes,
                storage_path=evidence_path,
            )
        backup_root = tmp_path / "backups"
        backup = create_stopped_backup(
            backup_root,
            BackupRequest(
                backup_id="cit-test",
                purpose="daily",
                precondition=BackupPrecondition(
                    service_stopped=True, active_business_connections=0
                ),
                evidence_root=evidence_root,
                evidence=(snapshot,),
                database=DatabaseDumpMetadata(
                    schema_revision="0002_cit_results", source_system_identifier="123456789"
                ),
                artifact_type="company",
                org_id=str(org_id),
                database_identity=str(database_id),
            ),
            DumpAdapter(),
        )
        archive_path = tmp_path / "91330106MA1234567T.finance-company.zip"
        create_portable_backup_archive(backup_root, backup.backup_directory, archive_path)
        verification = verify_portable_backup_archive(archive_path)
        assert verification.org_id == str(org_id) and verification.evidence_count == 1
        with zipfile.ZipFile(archive_path) as archive:
            assert archive.read("database.dump") == dump_bytes
            assert archive.read("evidence/" + snapshot.sha256) == evidence_bytes
        run(["createdb", "-U", postgres.username, "cit_restored"])
        run(["pg_restore", "-U", postgres.username, "-d", "cit_restored", "/tmp/cit.dump"])
        assert (
            run(
                [
                    "psql",
                    "-U",
                    postgres.username,
                    "-d",
                    "cit_restored",
                    "-Atc",
                    "SELECT count(*) FROM enterprise_income_tax_results",
                ]
            ).strip()
            == b"2"
        )
        with Session(engine) as session:
            assert len(list(session.scalars(select(EnterpriseIncomeTaxResult)))) == 2
            from ai_accounting import replay_cli

            operations = replay_cli._income_tax_operations(
                session, org_id=org_id, maps=replay_cli._stable_maps(session, org_id)
            )
        run(["createdb", "-U", postgres.username, "cit_replayed"])
        replay_engine = create_engine(engine.url.set(database="cit_replayed"))
        config.attributes["database_url_override"] = replay_engine.url.render_as_string(
            hide_password=False
        )
        command.upgrade(config, "head")
        replay_id = uuid.uuid4()
        replay_context = replace(context, org_id=replay_id, request_correlation_id=uuid.uuid4())
        with Session(replay_engine) as session, session.begin():
            org = seed_organization(
                session,
                org_id=replay_id,
                name="空库回放所得税",
                taxpayer_identification_number="91330106MA1234567T",
                accounting_period_control_enabled=False,
            )
            session.add(
                OrganizationDatabaseMetadata(
                    singleton_key=1,
                    org_id=replay_id,
                    database_identity=uuid.uuid4(),
                    current_catalog_instance_id=catalog_id,
                    owner_approval_required=True,
                )
            )
        with (
            Session(replay_engine) as session,
            session.begin(),
            persist_execution_attribution(
                session, context=replay_context, tool_name="finance_register_evidence"
            ),
        ):
            replay_evidence = make_evidence(session, session.get(Organization, replay_id))
            for month in (6, 7, 8):
                generated = AccountingPeriodService(session).generate_accounting_period(
                    GenerateAccountingPeriodRequest(
                        org_id=replay_id,
                        period_month=f"2026-{month:02d}",
                        idempotency_key=f"replay-month-{month}",
                        confirmation_note="回放生成期间",
                        evidence_references=[replay_evidence.id],
                    )
                )
                assert generated.status == "posted", generated

        def replay_call(name, payload):
            from ai_accounting.enterprise_income_tax_schemas import (
                PreviewEnterpriseIncomeTaxResultRequest,
            )
            from ai_accounting.financial_statement_schemas import (
                ConfirmEnterpriseIncomeTaxQuarterRequest,
            )
            from ai_accounting.financial_statements import FinancialStatementService

            with (
                Session(replay_engine) as session,
                session.begin(),
                persist_execution_attribution(
                    session,
                    context=replace(replay_context, request_correlation_id=uuid.uuid4()),
                    tool_name=name,
                ),
            ):
                if name == "finance_confirm_enterprise_income_tax_quarter":
                    return (
                        FinancialStatementService(session)
                        .confirm_enterprise_income_tax(
                            ConfirmEnterpriseIncomeTaxQuarterRequest.model_validate(payload)
                        )
                        .model_dump(mode="json")
                    )
                if name == "finance_preview_enterprise_income_tax_result":
                    return EnterpriseIncomeTaxService(session).preview(
                        PreviewEnterpriseIncomeTaxResultRequest.model_validate(payload)
                    )
                if name == "finance_record_event":
                    from ai_accounting.schemas import RecordEventRequest
                    from ai_accounting.service import FinanceService

                    return (
                        FinanceService(session)
                        .record_event(RecordEventRequest.model_validate(payload))
                        .model_dump(mode="json")
                    )
                assert name == "finance_confirm_enterprise_income_tax_result"
                return EnterpriseIncomeTaxService(session).confirm(
                    ConfirmEnterpriseIncomeTaxResultRequest.model_validate(payload)
                )

        monkeypatch.setattr(replay_cli, "_call_tool", replay_call)
        saved = {}
        resolver = replay_cli._ReplayResolver(engine=replay_engine, org_id=replay_id, results=saved)
        for operation in operations:
            saved[operation["key"]] = replay_cli._execute_operation(
                operation, package_company_dir=tmp_path, resolver=resolver
            )
        with replay_engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT count(*) FROM enterprise_income_tax_results")) == 2
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT target_tax_fen FROM enterprise_income_tax_results "
                        "ORDER BY revision DESC LIMIT 1"
                    )
                )
                == 16000
            )
        replay_engine.dispose()
        # Real PostgreSQL cash posting uses the same scope/evidence guards as
        # production. Two payments competing for one source must not overpay.
        from conftest import AuthenticatedOwnerAuthority, bind_authenticated_bank_account
        from test_enterprise_income_tax import payment

        from ai_accounting.bank_statement_schemas import (
            ConfirmBankReconciliationScopeRequest,
            PreviewBankReconciliationScopeRequest,
        )
        from ai_accounting.bank_statement_service import BankStatementService

        winner = next(r for r in results if r["status"] == "posted")
        with (
            Session(engine) as session,
            session.begin(),
            persist_execution_attribution(
                session,
                context=replace(context, request_correlation_id=uuid.uuid4()),
                tool_name="finance_confirm_bank_reconciliation_scope",
            ),
        ):
            evidence_id = session.scalar(select(Evidence.id))
            scope = PreviewBankReconciliationScopeRequest(
                org_id=org_id,
                action_type="initial_confirmation",
                accounts=[
                    {
                        "bank_account_code": "1002",
                        "account_name": "银行存款",
                        "start_date": "2026-01-01",
                    }
                ],
                explanation="测试明确账户范围",
                evidence_references=[evidence_id],
            )
            scope_preview = BankStatementService(session).preview_bank_reconciliation_scope(scope)
            assert scope_preview.calculation_hash, scope_preview
            scope_result = BankStatementService(session).confirm_bank_reconciliation_scope(
                ConfirmBankReconciliationScopeRequest.model_validate(
                    scope.model_dump()
                    | {
                        "calculation_hash": scope_preview.calculation_hash,
                        "idempotency_key": "scope",
                    }
                )
            )
            assert scope_result.status == "posted", scope_result

        def pay_worker(key):
            with (
                Session(engine) as session,
                session.begin(),
                persist_execution_attribution(
                    session,
                    context=replace(context, request_correlation_id=uuid.uuid4()),
                    tool_name="finance_record_event",
                ),
            ):
                bind_authenticated_bank_account(session, AuthenticatedOwnerAuthority(context))
                result, _, _ = payment(
                    session,
                    session.get(Organization, org_id),
                    session.get(Evidence, evidence_id),
                    winner["result_id"],
                    16000,
                    key=key,
                )
                return result

        with ThreadPoolExecutor(max_workers=2) as pool:
            paid = list(pool.map(pay_worker, ["cash-a", "cash-b"]))
        assert sum(r.status == "posted" for r in paid) == 1, paid
        with (
            Session(engine) as session,
            session.begin(),
            persist_execution_attribution(
                session,
                context=replace(context, request_correlation_id=uuid.uuid4()),
                tool_name="finance_confirm_enterprise_income_tax_result",
            ),
        ):
            org = session.get(Organization, org_id)
            evidence = session.get(Evidence, evidence_id)
            reduced, _ = confirm(
                EnterpriseIncomeTaxService(session),
                change(
                    org,
                    evidence,
                    source,
                    previous_result_id=winner["result_id"],
                    declared_tax_fen=14000,
                    declaration_date="2026-08-10",
                    posting_date="2026-08-10",
                ),
                key="reduce-after-cash",
            )
            assert reduced["data"]["refundable_fen"] == 2000
        with (
            Session(engine) as session,
            session.begin(),
            persist_execution_attribution(
                session,
                context=replace(context, request_correlation_id=uuid.uuid4()),
                tool_name="finance_record_event",
            ),
        ):
            bind_authenticated_bank_account(session, AuthenticatedOwnerAuthority(context))
            refunded, _, _ = payment(
                session,
                session.get(Organization, org_id),
                session.get(Evidence, evidence_id),
                reduced["result_id"],
                2000,
                refund=True,
                key="pg-refund",
                posting_date=date(2026, 8, 11),
            )
            assert refunded.status == "posted", refunded
        # Export and replay the settled chain, with normalized CSV imports and
        # stable source references. The inventory must include generated events.
        from ai_accounting.bank_statement_schemas import (
            ConfirmBankStatementFileImportRequest,
            PreviewBankStatementFileImportRequest,
        )
        from ai_accounting.config import Settings

        cash_export = tmp_path / "cash-replay"
        cash_export.mkdir()
        with Session(engine) as session:
            maps = replay_cli._stable_maps(session, org_id)
            cash_operations = replay_cli._income_tax_operations(session, org_id=org_id, maps=maps)
            effective = replay_cli._effective_events(session, org_id)
            inventory = replay_cli._income_tax_event_inventory(effective, maps)
            assert len(inventory) == len(effective) == 3
            assert {v["replay_key"] for v in inventory}.issubset(
                {v["key"] for v in cash_operations}
            )
            bank_exports = replay_cli._export_bank_transactions(
                session,
                org_id=org_id,
                company_dir=cash_export,
            )
        with (
            Session(replay_engine) as session,
            session.begin(),
            persist_execution_attribution(
                session,
                context=replace(replay_context, request_correlation_id=uuid.uuid4()),
                tool_name="finance_confirm_bank_reconciliation_scope",
            ),
        ):
            replay_evidence_id = session.scalar(select(Evidence.id))
            replay_scope = scope.model_copy(
                update={
                    "org_id": replay_id,
                    "evidence_references": [replay_evidence_id],
                }
            )
            bank_service = BankStatementService(session)
            preview = bank_service.preview_bank_reconciliation_scope(replay_scope)
            assert preview.calculation_hash, preview
            result = bank_service.confirm_bank_reconciliation_scope(
                ConfirmBankReconciliationScopeRequest.model_validate(
                    replay_scope.model_dump()
                    | {
                        "calculation_hash": preview.calculation_hash,
                        "idempotency_key": "replay-scope",
                    }
                )
            )
            assert result.status == "posted", result
        for exported in bank_exports:
            with (
                Session(replay_engine) as session,
                session.begin(),
                persist_execution_attribution(
                    session,
                    context=replace(replay_context, request_correlation_id=uuid.uuid4()),
                    tool_name="finance_confirm_bank_statement_import",
                ),
            ):
                bank_service = BankStatementService(
                    session,
                    settings=Settings(
                        finance_bank_import_dir=cash_export / "bank-transactions",
                    ),
                    current_date=date.max,
                )
                bank_request = PreviewBankStatementFileImportRequest(
                    org_id=replay_id,
                    bank_account_code=exported["bank_account_code"],
                    source_file_name=exported["bank_account_code"] + ".csv",
                    file_format="csv",
                    column_mapping={
                        v: v
                        for v in (
                            "external_id",
                            "booking_date",
                            "amount",
                            "currency",
                            "memo",
                        )
                    },
                )
                preview = bank_service.preview_bank_statement_import(bank_request)
                assert preview.calculation_hash, preview
                imported = bank_service.confirm_bank_statement_import(
                    ConfirmBankStatementFileImportRequest.model_validate(
                        bank_request.model_dump()
                        | {
                            "calculation_hash": preview.calculation_hash,
                            "idempotency_key": "replay-bank",
                        }
                    )
                )
                assert imported.status == "posted", imported
        for operation in cash_operations:
            if operation["key"] not in saved:
                saved[operation["key"]] = replay_cli._execute_operation(
                    operation,
                    package_company_dir=cash_export,
                    resolver=resolver,
                )
        with Session(replay_engine) as session, Session(engine) as source_session:
            from ai_accounting.enterprise_income_tax_schemas import QueryEnterpriseIncomeTaxRequest

            state = EnterpriseIncomeTaxService(session).query(
                QueryEnterpriseIncomeTaxRequest(org_id=replay_id)
            )["data"]
            assert len(state["history"]) == 3
            assert state["sources"][0]["recognized_tax_fen"] == 14000
            assert state["sources"][0]["balance_fen"] == 0
            assert replay_cli._account_balance_projection(session, replay_id) == (
                replay_cli._account_balance_projection(source_session, org_id)
            )
        replay_engine.dispose()
        engine.dispose()
