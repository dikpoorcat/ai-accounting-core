from concurrent.futures import ThreadPoolExecutor
from datetime import date
from hashlib import sha256
from threading import Event

import pytest
from _postgres_helpers import catalog_owner_authority
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_accounting_period_postgres_invariants import (
    _approve_close,
    _confirm_partial_year_zero_opening,
)
from test_financial_statements_postgres import _isolated_business_engine

from ai_accounting.accounting_period_schemas import (
    ConfirmAccountingPeriodCloseRequest,
    GenerateAccountingPeriodRequest,
    PreviewAccountingPeriodCloseRequest,
)
from ai_accounting.accounting_period_service import AccountingPeriodService
from ai_accounting.bank_statement_schemas import (
    ConfirmBankReconciliationScopeRequest,
    PreviewBankReconciliationScopeRequest,
)
from ai_accounting.bank_statement_service import BankStatementService
from ai_accounting.coa import seed_organization
from ai_accounting.company_notes import read_company_notes
from ai_accounting.config import Settings
from ai_accounting.material_schemas import (
    RegisterPeriodMaterialsRequest,
    UpdatePeriodMaterialInventoryRequest,
)
from ai_accounting.material_service import MaterialService, lock_material_company
from ai_accounting.models import AccountingPeriod, Evidence, Organization, PeriodMaterialInventory

pytestmark = pytest.mark.postgres


def test_forward_migration_adds_role_to_existing_company(monkeypatch):
    from _postgres_helpers import isolated_postgres_url
    from alembic.config import Config
    from sqlalchemy import create_engine

    from ai_accounting import coa
    from ai_accounting.models import Account
    from alembic import command

    with isolated_postgres_url("material_upgrade") as url:
        config = Config("alembic.ini")
        config.attributes["database_url_override"] = url
        command.upgrade(config, "0005_payroll_provenance")
        engine = create_engine(url)
        try:
            with Session(engine) as session, monkeypatch.context() as old_chart:
                old_chart.setattr(
                    coa,
                    "DEFAULT_ACCOUNTS",
                    [row for row in coa.DEFAULT_ACCOUNTS if row[0] != "122105"],
                )
                org = coa.seed_organization(
                    session, name="迁移测试", taxpayer_identification_number="91330106MA1234567T"
                )
                org_id = org.id
                session.commit()
            command.upgrade(config, "head")
            command.check(config)
            with Session(engine) as session:
                account = session.scalar(
                    select(Account).where(
                        Account.org_id == org_id, Account.system_role == "pass_through_receivable"
                    )
                )
                assert account.code == "122105" and account.category == "asset"
        finally:
            engine.dispose()


def test_inventory_race_with_close_rechecks_after_lock_and_preserves_sources(tmp_path, monkeypatch):
    settings = Settings(finance_storage_dir=tmp_path / "storage", finance_evidence_dir=tmp_path)
    monkeypatch.setattr("ai_accounting.company_notes.get_settings", lambda: settings)
    monkeypatch.setattr("ai_accounting.material_reader.get_settings", lambda: settings)
    path = tmp_path / "设立说明.txt"
    path.write_bytes("7月成立，本月无经营活动，无银行账户。".encode())
    with _isolated_business_engine() as engine, Session(engine) as session:
        org = seed_organization(
            session,
            name="资料并发测试",
            taxpayer_identification_number="91330106MA1234567T",
            accounting_period_control_enabled=False,
        )
        session.commit()
        with catalog_owner_authority(session, org) as authority:
            with authority.attributed_call(session, tool_name="finance_register_evidence"):
                evidence = Evidence(
                    org_id=org.id,
                    original_name=path.name,
                    storage_path=str(path),
                    sha256=sha256(path.read_bytes()).hexdigest(),
                    size_bytes=path.stat().st_size,
                    source="test",
                )
                session.add(evidence)
                session.flush()
            with authority.attributed_call(session, tool_name="finance_generate_accounting_period"):
                result = AccountingPeriodService(session).generate_accounting_period(
                    GenerateAccountingPeriodRequest(
                        org_id=org.id,
                        period_month="2026-07",
                        idempotency_key="july",
                        evidence_references=[evidence.id],
                    )
                )
            assert result.status == "posted", result
            period_id = result.period_id
            with authority.attributed_call(
                session, tool_name="finance_confirm_bank_reconciliation_scope"
            ):
                bank = BankStatementService(session)
                request = PreviewBankReconciliationScopeRequest(
                    org_id=org.id,
                    action_type="initial_confirmation",
                    accounts=[],
                    confirm_zero_accounts=True,
                    explanation="确认无实际银行账户",
                    evidence_references=[evidence.id],
                )
                preview = bank.preview_bank_reconciliation_scope(request)
                confirmed = bank.confirm_bank_reconciliation_scope(
                    ConfirmBankReconciliationScopeRequest(
                        **request.model_dump(),
                        calculation_hash=preview.calculation_hash,
                        idempotency_key="scope",
                    )
                )
                assert confirmed.status == "posted", confirmed
            _confirm_partial_year_zero_opening(
                session, authority, org_id=org.id, evidence_id=evidence.id
            )
            service = MaterialService(session)
            register = RegisterPeriodMaterialsRequest(
                org_id=org.id,
                period_id=period_id,
                expected_revision=0,
                idempotency_key="intake",
                sources=[
                    {
                        "evidence_id": evidence.id,
                        "passages": {"全文": path.read_text(encoding="utf-8")},
                    }
                ],
            )
            assert service.register(register)["revision"] == 1
            service.update(
                UpdatePeriodMaterialInventoryRequest(
                    org_id=org.id,
                    period_id=period_id,
                    expected_revision=1,
                    idempotency_key="review",
                    reviewed_notes_hash=read_company_notes(org)["sha256"],
                    resolutions=[
                        {
                            "item_key": f"{evidence.id}:全文",
                            "treatment": "supporting",
                            "basis": "设立及零活动说明",
                        }
                    ],
                )
            )
            session.commit()
            preview_request = PreviewAccountingPeriodCloseRequest(
                org_id=org.id, period_id=period_id, closing_date=date(2026, 7, 31)
            )
            period_service = AccountingPeriodService(session)
            preview = period_service.preview_accounting_period_close(preview_request)
            assert not preview.data["blocker_codes"], preview.data["blocker_codes"]
            with authority.attributed_call(
                session, tool_name="finance_confirm_accounting_period_close"
            ) as attribution:
                approval = _approve_close(
                    session,
                    attribution,
                    period_id=period_id,
                    calculation_hash=preview.calculation_hash,
                )
            close_request = ConfirmAccountingPeriodCloseRequest(
                **preview_request.model_dump(),
                calculation_hash=preview.calculation_hash,
                owner_approval_id=approval,
                idempotency_key="close",
                management_commentary="公司本月新设，已提供材料未见经营活动。",
                management_commentary_context_hash=preview.data["assistant_review_checklist"][
                    "management_commentary"
                ]["context_hash"],
            )
            session.commit()
            org_id = org.id
            session.commit()
            started = Event()

            def close():
                with Session(engine) as closer:
                    with authority.attributed_call(
                        closer, tool_name="finance_confirm_accounting_period_close"
                    ):
                        started.set()
                        outcome = AccountingPeriodService(closer).confirm_accounting_period_close(
                            close_request
                        )
                    closer.commit()
                    return outcome

            # The competing intake owns the same company lock. Final close must wait and re-read.
            with ThreadPoolExecutor(max_workers=1) as executor:
                lock_material_company(session, org_id)
                future = executor.submit(close)
                assert started.wait(10)
                revised = register.model_copy(
                    update={"expected_revision": 2, "idempotency_key": "new-review"}
                )
                service.register(revised)
                session.commit()
                closed = future.result(timeout=30)
            assert closed.status != "posted", closed
            session.expire_all()
            assert session.get(AccountingPeriod, period_id).status == "open"
            assert service.latest(org_id, period_id).revision == 3
            assert not service.check(org_id, period_id)["satisfied"]
            assert service.register(revised)["idempotent_replay"]
            session.commit()
            # Immutable database history is enforced even for direct SQL writes.
            with pytest.raises(DBAPIError, match="MATERIAL_INVENTORY_IMMUTABLE"):
                with engine.begin() as connection:
                    connection.execute(text("UPDATE period_material_inventories SET revision=100"))
            with Session(engine) as other:
                with pytest.raises(ValueError, match="MATERIAL_INVENTORY_STALE"):
                    MaterialService(other).register(
                        register.model_copy(update={"idempotency_key": "stale"})
                    )
                assert len(list(other.scalars(select(PeriodMaterialInventory)))) == 3

            # A fresh review can close; the saved inventory then rejects new versions.
            service.update(
                UpdatePeriodMaterialInventoryRequest(
                    org_id=org_id,
                    period_id=period_id,
                    expected_revision=3,
                    idempotency_key="rereview",
                    reviewed_notes_hash=read_company_notes(org)["sha256"],
                )
            )
            fresh = period_service.preview_accounting_period_close(preview_request)
            with authority.attributed_call(
                session, tool_name="finance_confirm_accounting_period_close"
            ) as attribution:
                approval = _approve_close(
                    session,
                    attribution,
                    period_id=period_id,
                    calculation_hash=fresh.calculation_hash,
                )
                result = period_service.confirm_accounting_period_close(
                    close_request.model_copy(
                        update={
                            "idempotency_key": "close-current",
                            "owner_approval_id": approval,
                            "calculation_hash": fresh.calculation_hash,
                            "management_commentary_context_hash": fresh.data[
                                "assistant_review_checklist"
                            ]["management_commentary"]["context_hash"],
                        }
                    )
                )
            assert result.status == "posted", result
            session.commit()
            from ai_accounting.company_notes import update_company_notes

            historical = service.check(org_id, period_id)
            notes = read_company_notes(org)
            update_company_notes(org, notes["sha256"], notes["content"] + "\n补充下月联系说明。\n")
            after_notes_edit = service.check(org_id, period_id)
            assert after_notes_edit["satisfied"]
            assert after_notes_edit["snapshot_hash"] == historical["snapshot_hash"]
            assert service.register(register)["idempotent_replay"]
            with pytest.raises(ValueError, match="MATERIAL_PERIOD_NOT_OPEN"):
                service.register(
                    register.model_copy(
                        update={"expected_revision": 4, "idempotency_key": "after-close"}
                    )
                )


def test_postgres_empty_database_replay_rebuilds_material_links(tmp_path, monkeypatch):
    from contextlib import ExitStack

    from ai_accounting import replay_cli
    from ai_accounting.accounting_periods import canonical_sha256
    from ai_accounting.company_notes import update_company_notes
    from ai_accounting.component_schemas import RecordEventRequest
    from ai_accounting.component_service import ComponentService
    from ai_accounting.material_replay import execute_material_operation, export_material_operations
    from ai_accounting.models import BusinessEventComponent

    settings = Settings(finance_storage_dir=tmp_path / "storage", finance_evidence_dir=tmp_path)
    monkeypatch.setattr("ai_accounting.company_notes.get_settings", lambda: settings)
    monkeypatch.setattr("ai_accounting.material_reader.get_settings", lambda: settings)
    path = tmp_path / "报销.csv"
    path.write_text("姓名,金额\n杨彪,3076.87\n", encoding="utf-8")
    with ExitStack() as stack:
        states = []
        for label in ("source", "target"):
            engine = stack.enter_context(_isolated_business_engine())
            session = stack.enter_context(Session(engine))
            org = seed_organization(
                session,
                name="回放资料测试",
                taxpayer_identification_number="91330106MA1234567T",
                accounting_period_control_enabled=False,
            )
            session.commit()
            authority = stack.enter_context(catalog_owner_authority(session, org))
            with authority.attributed_call(session, tool_name="finance_generate_accounting_period"):
                result = AccountingPeriodService(session).generate_accounting_period(
                    GenerateAccountingPeriodRequest(
                        org_id=org.id, period_month="2026-08", idempotency_key="august"
                    )
                )
            assert result.status == "posted", result
            with authority.attributed_call(session, tool_name="finance_register_evidence"):
                evidence = Evidence(
                    org_id=org.id,
                    original_name=path.name,
                    storage_path=str(path),
                    sha256=sha256(path.read_bytes()).hexdigest(),
                    size_bytes=path.stat().st_size,
                    source="test",
                )
                session.add(evidence)
                session.flush()
            if label == "source":
                MaterialService(session).register(
                    RegisterPeriodMaterialsRequest(
                        org_id=org.id,
                        period_id=result.period_id,
                        expected_revision=0,
                        idempotency_key="intake",
                        sources=[
                            {
                                "evidence_id": evidence.id,
                                "columns": [
                                    {"column": "A", "role": "context"},
                                    {"column": "B", "role": "amount"},
                                ],
                            }
                        ],
                    )
                )
            key = (
                "expense-source"
                if label == "source"
                else replay_cli._replay_idempotency(
                    replay_cli._semantic_replay_key("expense-source")
                )
            )
            with authority.attributed_call(session, tool_name="finance_record_event"):
                posting = ComponentService(session).record(
                    RecordEventRequest(
                        org_id=org.id,
                        idempotency_key=key,
                        posting_date="2026-08-31",
                        evidence_references=[evidence.id],
                        components=[
                            {
                                "key": "expense",
                                "kind": "expense",
                                "recognition_period": "2026-08",
                                "amount_fen": 307687,
                                "expense_class": "general_expense",
                                "payment_basis": "supplier_credit",
                            }
                        ],
                    )
                )
            assert posting.status == "posted", posting
            component = session.scalar(
                select(BusinessEventComponent).where(
                    BusinessEventComponent.event_id == posting.event_id
                )
            )
            if label == "source":
                MaterialService(session).update(
                    UpdatePeriodMaterialInventoryRequest(
                        org_id=org.id,
                        period_id=result.period_id,
                        expected_revision=1,
                        idempotency_key="review",
                        reviewed_notes_hash=read_company_notes(org)["sha256"],
                        resolutions=[
                            {
                                "item_key": f"{evidence.id}:CSV!B2",
                                "treatment": "recognize",
                                "business_kind": "expense",
                                "recognition_period": "2026-08",
                                "links": [
                                    {
                                        "source": {
                                            "event_key": "expense-source",
                                            "component_key": "expense",
                                        },
                                        "amount_fen": 307687,
                                        "expected_facts_hash": canonical_sha256(component.facts),
                                    }
                                ],
                            }
                        ],
                    )
                )
            session.commit()
            states.append(
                (engine, session, org, authority, result.period_id, evidence, posting, component)
            )
        source, target = states
        operations = export_material_operations(
            source[1], source[2].id, replay_cli._stable_maps(source[1], source[2].id), tmp_path
        )
        replay_cli._verify_operation_references(
            [
                {"kind": "tool", "key": replay_cli._semantic_replay_key("expense-source")},
                *operations,
            ],
            evidence_hashes={source[5].sha256},
            org_id=str(source[2].id),
        )
        resolver = replay_cli._ReplayResolver(
            engine=target[0],
            org_id=target[2].id,
            results={
                replay_cli._semantic_replay_key("expense-source"): {
                    "event_id": str(target[6].event_id)
                }
            },
        )

        def call_tool(name, raw):
            with Session(target[0]) as current:
                org = current.get(Organization, target[2].id)
                with target[3].attributed_call(current, tool_name=name):
                    if name == "finance_get_company_notes":
                        response = {"status": "ok", "company_notes": read_company_notes(org)}
                    elif name == "finance_update_company_notes":
                        response = update_company_notes(org, raw["expected_sha256"], raw["content"])
                    elif name == "finance_get_period_material_completeness":
                        response = {
                            "status": "ok",
                            **MaterialService(current).check(org.id, target[4]),
                        }
                    elif name == "finance_register_period_materials":
                        response = MaterialService(current).register(
                            RegisterPeriodMaterialsRequest.model_validate(raw)
                        )
                    elif name == "finance_update_period_material_inventory":
                        response = MaterialService(current).update(
                            UpdatePeriodMaterialInventoryRequest.model_validate(raw)
                        )
                    else:
                        raise AssertionError(name)
                current.commit()
                return response

        monkeypatch.setattr(replay_cli, "_call_tool", call_tool)
        for operation in operations:
            result = execute_material_operation(operation, tmp_path, resolver)
        assert result["completeness"]["satisfied"], result
        target[1].expire_all()
        item = MaterialService(target[1]).check(target[2].id, target[4])["items"][0]
        assert item["key"].startswith(str(target[5].id))
        assert item["resolution"]["links"][0]["source"]["component_id"] == str(target[7].id)
        from ai_accounting.material_schemas import MaterialComponentLink

        foreign_link = MaterialComponentLink(
            source={"component_id": source[7].id},
            amount_fen=10000,
            expected_facts_hash=canonical_sha256(source[7].facts),
        )
        assert (
            MaterialService(target[1])._link(target[2].id, foreign_link)[1]
            == "MATERIAL_COMPONENT_NOT_FOUND"
        )
        assert execute_material_operation(operations[-1], tmp_path, resolver)["idempotent_replay"]
        path.write_text("篡改了原文件", encoding="utf-8")
        with pytest.raises(replay_cli.ReplayError, match="REPLAY_MATERIAL_INCOMPLETE"):
            execute_material_operation(operations[-1], tmp_path, resolver)
