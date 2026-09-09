from __future__ import annotations

from datetime import date

import pytest
from _postgres_helpers import authenticated_business_database, confirmed_payroll
from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_accounting.accounting_periods import canonical_sha256
from ai_accounting.corrections import CorrectionService
from ai_accounting.event_amendment_schemas import (
    ConfirmCorrectionRequest,
    DeleteEventRequest,
    PreviewCorrectionRequest,
)
from ai_accounting.event_amendments import EventAmendmentService
from ai_accounting.models import BusinessEvent, PayrollBatch, Voucher
from ai_accounting.schemas import RegisterPayrollContributionActualRequest


def test_existing_first_wage_uses_can_be_rebuilt_without_mutating_source():
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from ai_accounting.models import PayrollFirstWageTaxTreatmentUse
    from ai_accounting.schemas import (
        PreviewPayrollRequest,
        RegisterPayrollFirstWageTaxTreatmentRequest,
    )

    with authenticated_business_database("existing_first_wage_use") as (
        engine, org_id, evidence_id, owner
    ):
        with Session(engine) as session:
            _, batch, line, _, source = confirmed_payroll(session, org_id, evidence_id, owner)
            source_id, batch_id = source.id, batch.id
            voucher_number = session.scalar(select(Voucher.voucher_number).where(
                Voucher.event_id == source_id
            ))
            session.commit()
            treatment = PreviewCorrectionRequest(org_id=org_id, source_changes=[
                RegisterPayrollFirstWageTaxTreatmentRequest(
                    org_id=org_id, employee_id=line.employee_id, tax_year=2026,
                    first_wage_month=3, treatment_state="eligible",
                    evidence_references=[evidence_id], idempotency_key="first-wage-source",
                )
            ])
            with owner.attributed_call(session, tool_name="finance_preview_correction"):
                first = CorrectionService(session).preview(treatment)
            assert first["status"] == "calculated", first
            with owner.attributed_call(session, tool_name="finance_confirm_correction"):
                first_result = CorrectionService(session).confirm(ConfirmCorrectionRequest(
                    **treatment.model_dump(), calculation_hash=first["calculation_hash"],
                    idempotency_key="first-source-confirm",
                ))
            assert first_result["status"] == "posted", first_result
            session.commit()
            source = session.get(BusinessEvent, source_id)
            replacement = PreviewPayrollRequest.model_validate(
                session.get(PayrollBatch, batch_id).calculation_input["request"]
            )
            replacement.employee_items[0].tax_reported_salary_fen = 900000
            replacement.employee_items[0].accounting_gross_salary_fen = 900000
            request = PreviewCorrectionRequest(org_id=org_id, event_replacements=[{
                "event_id": source_id, "expected_facts_hash": canonical_sha256(source.facts),
                "replacement": replacement,
            }])
            original_use = session.scalar(select(PayrollFirstWageTaxTreatmentUse).where(
                PayrollFirstWageTaxTreatmentUse.payroll_batch_id == batch_id
            ))
            assert original_use is not None
            treatment_id = original_use.treatment_id
            for table in (
                "payroll_first_wage_tax_treatments", "payroll_first_wage_tax_treatment_evidence",
                "payroll_first_wage_tax_treatment_uses",
            ):
                with pytest.raises(DBAPIError, match="PAYROLL_FIRST_WAGE_TAX_FACT_IMMUTABLE"):
                    with session.begin_nested():
                        session.execute(
                            text(f"DELETE FROM {table} WHERE org_id=:org"), {"org": org_id}
                        )
            with owner.attributed_call(session, tool_name="finance_preview_correction"):
                preview = CorrectionService(session).preview(request)
            assert preview["status"] == "calculated", preview
            with owner.attributed_call(session, tool_name="finance_confirm_correction"):
                result = CorrectionService(session).confirm(ConfirmCorrectionRequest(
                    **request.model_dump(), calculation_hash=preview["calculation_hash"],
                    idempotency_key="correct-existing-source-use",
                ))
            assert result["status"] == "posted", result
            session.commit()
            assert session.scalar(select(Voucher.voucher_number).where(
                Voucher.event_id == source_id
            )) == voucher_number
            assert session.scalar(select(PayrollFirstWageTaxTreatmentUse.treatment_id).where(
                PayrollFirstWageTaxTreatmentUse.payroll_batch_id == batch_id
            )) == treatment_id


def test_postgres_linked_payment_keeps_bank_match_once(tmp_path):
    from ai_accounting.bank_statement_schemas import (
        ConfirmBankStatementFileImportRequest,
        PreviewBankStatementFileImportRequest,
    )
    from ai_accounting.bank_statement_service import BankStatementService
    from ai_accounting.component_schemas import RecordEventRequest
    from ai_accounting.config import Settings
    from ai_accounting.models import BankTransaction, BankTransactionMatch, OpenItem
    from ai_accounting.schemas import ConfirmPayrollRequest, PreviewPayrollRequest
    from ai_accounting.service import FinanceService

    with authenticated_business_database("linked_correction") as (
        engine,
        org_id,
        evidence_id,
        owner,
    ):
        with Session(engine) as session:
            from ai_accounting.models import Organization

            org = session.get(Organization, org_id)
            from conftest import prepare_authenticated_bank_account

            prepare_authenticated_bank_account(
                session,
                org,
                booking_date=date(2026, 3, 1),
                authority=owner,
                evidence_id=evidence_id,
            )
            prepare_authenticated_bank_account(
                session,
                org,
                booking_date=date(2026, 4, 1),
                authority=owner,
                evidence_id=evidence_id,
            )
            org, batch, line, _, source = confirmed_payroll(session, org_id, evidence_id, owner)
            salary = session.scalar(
                select(OpenItem).where(
                    OpenItem.source_event_id == source.id, OpenItem.payable_category == "salary"
                )
            )
            employer = session.scalar(
                select(OpenItem).where(
                    OpenItem.source_event_id == source.id,
                    OpenItem.payable_category == "employer_social",
                )
            )
            (tmp_path / "salary.csv").write_text(
                "date,amount,reference\n2026-03-06,-999.00,payroll-partial\n", encoding="utf-8"
            )
            bank_service = BankStatementService(
                session,
                settings=Settings(
                    finance_bank_import_dir=tmp_path, finance_evidence_dir=tmp_path / "evidence"
                ),
            )
            bank_request = PreviewBankStatementFileImportRequest(
                org_id=org_id,
                bank_account_code="1002",
                source_file_name="salary.csv",
                file_format="csv",
                column_mapping={
                    "booking_date": "date",
                    "amount": "amount",
                    "external_id": "reference",
                },
            )
            bank_preview = bank_service.preview_bank_statement_import(bank_request)
            assert bank_preview.status == "calculated", bank_preview
            with owner.attributed_call(session, tool_name="finance_confirm_bank_statement_import"):
                imported = bank_service.confirm_bank_statement_import(
                    ConfirmBankStatementFileImportRequest(
                        **bank_request.model_dump(),
                        calculation_hash=bank_preview.calculation_hash,
                        idempotency_key="salary-bank",
                    )
                )
            assert imported.status == "posted", imported
            bank = session.scalar(
                select(BankTransaction).where(BankTransaction.external_id == "payroll-partial")
            )
            request = RecordEventRequest.model_validate(
                {
                    "org_id": org_id,
                    "idempotency_key": "salary-payment",
                    "posting_date": "2026-03-06",
                    "evidence_references": [evidence_id],
                    "components": [
                        {
                            "key": "salary",
                            "kind": "salary_settlement",
                            "business_date": "2026-03-06",
                            "amount_fen": 98900,
                            "allocations": [{"open_item_id": salary.id, "amount_fen": 100000}],
                            "withholding_allocations": [
                                {
                                    "open_item_id": salary.id,
                                    "employee_social_insurance_items": {"pension": 1000},
                                    "individual_income_tax_fen": 100,
                                }
                            ],
                        },
                        {
                            "key": "social",
                            "kind": "payable_settlement",
                            "business_date": "2026-03-06",
                            "allocations": [{"open_item_id": employer.id, "amount_fen": 1000}],
                        },
                    ],
                    "funds": [
                        {
                            "key": "bank",
                            "account_code": "1002",
                            "direction": "payment",
                            "payment_date": "2026-03-06",
                            "amount_fen": 99900,
                            "bank_transaction_references": [{"id": bank.id}],
                            "allocations": [
                                {"component_key": "salary", "amount_fen": 98900},
                                {"component_key": "social", "amount_fen": 1000},
                            ],
                        }
                    ],
                }
            )
            with owner.attributed_call(session, tool_name="finance_record_event"):
                payment = FinanceService(session).record_event(request)
            assert payment.status == "posted", payment
            later_request = PreviewPayrollRequest.model_validate(
                batch.calculation_input["request"]
            ).model_copy(
                update={
                    "idempotency_key": "april",
                    "payroll_period": "2026-04",
                    "posting_date": date(2026, 4, 5),
                }
            )
            with owner.attributed_call(session, tool_name="finance_preview_payroll"):
                later = FinanceService(session).preview_payroll(later_request)
            assert later.status == "calculated", later
            with owner.attributed_call(session, tool_name="finance_confirm_payroll"):
                later_posted = FinanceService(session).confirm_payroll(
                    ConfirmPayrollRequest(
                        org_id=org_id,
                        batch_id=later.batch_id,
                        calculation_hash=later.calculation_hash,
                        idempotency_key="april-post",
                    )
                )
            assert later_posted.status == "posted", later_posted
            session.commit()
            numbers = {v.id: v.voucher_number for v in session.scalars(select(Voucher))}
            facts = session.get(BusinessEvent, payment.event_id).facts
            correction = PreviewCorrectionRequest(
                org_id=org_id,
                source_changes=[
                    RegisterPayrollContributionActualRequest(
                        org_id=org_id,
                        employee_id=line.employee_id,
                        idempotency_key="pension-new",
                        contribution_period="2026-03",
                        evidence_references=[evidence_id],
                        items=[
                            {
                                "contribution_group": "social_insurance",
                                "insurance_kind": "pension",
                                "actual_state": "declared",
                                "employee_amount_fen": 79000,
                                "employer_amount_fen": 158000,
                            }
                        ],
                    )
                ],
            )
            with owner.attributed_call(session, tool_name="finance_preview_correction"):
                preview = CorrectionService(session).preview(correction)
            assert preview["status"] == "calculated", preview
            assert len(preview["data"]["changes"]) == 3
            with owner.attributed_call(session, tool_name="finance_confirm_correction"):
                result = CorrectionService(session).confirm(
                    ConfirmCorrectionRequest(
                        **correction.model_dump(),
                        calculation_hash=preview["calculation_hash"],
                        idempotency_key="linked",
                    )
                )
            assert result["status"] == "posted", result
            session.commit()
            assert {v.id: v.voucher_number for v in session.scalars(select(Voucher))} == numbers
            assert session.get(BusinessEvent, payment.event_id).facts == facts
            assert session.get(BankTransaction, bank.id).matched_event_id == payment.event_id
            matches = list(
                session.scalars(
                    select(BankTransactionMatch).where(
                        BankTransactionMatch.bank_transaction_id == bank.id
                    )
                )
            )
            assert len(matches) == 1 and matches[0].event_id == payment.event_id


@pytest.mark.parametrize("combined_bonus", [False, True])
def test_postgres_source_correction_and_reasonless_delete(monkeypatch, combined_bonus, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from ai_accounting import replay_cli
    from ai_accounting.service import FinanceService

    with authenticated_business_database("atomic_correction") as (
        engine,
        org_id,
        evidence_id,
        owner,
    ):
        with Session(engine) as session:
            org, batch, line, evidence, source = confirmed_payroll(
                session, org_id, evidence_id, owner
            )
            batch_id, source_id = batch.id, source.id
            voucher = session.scalar(select(Voucher).where(Voucher.event_id == source_id))
            original_number = voucher.voucher_number
            if combined_bonus:
                from ai_accounting.schemas import ConfirmPayrollRequest, PreviewPayrollRequest
                from ai_accounting.service import FinanceService

                bonus_request = PreviewPayrollRequest(
                    org_id=org_id,
                    idempotency_key="bonus-preview",
                    batch_kind="annual_bonus",
                    payroll_period="2026-03",
                    posting_date=date(2026, 3, 6),
                    payment_date=date(2026, 3, 6),
                    tax_method="combined",
                    employee_items=[
                        {
                            "employee_id": line.employee_id,
                            "annual_bonus_fen": 100000,
                            "regular_payroll_batch_id": batch.id,
                        }
                    ],
                    evidence_references=[evidence_id],
                )
                with owner.attributed_call(session, tool_name="finance_preview_payroll"):
                    bonus = FinanceService(session).preview_payroll(bonus_request)
                assert bonus.status == "calculated", bonus
                with owner.attributed_call(session, tool_name="finance_confirm_payroll"):
                    bonus_posted = FinanceService(session).confirm_payroll(
                        ConfirmPayrollRequest(
                            org_id=org_id,
                            batch_id=bonus.batch_id,
                            calculation_hash=bonus.calculation_hash,
                            idempotency_key="bonus-confirm",
                        )
                    )
                assert bonus_posted.status == "posted", bonus_posted
            session.commit()
            change = RegisterPayrollContributionActualRequest(
                org_id=org_id,
                idempotency_key="correct-pension",
                employee_id=line.employee_id,
                contribution_period="2026-03",
                evidence_references=[evidence_id],
                items=[
                    {
                        "contribution_group": "social_insurance",
                        "insurance_kind": "pension",
                        "actual_state": "declared",
                        "employee_amount_fen": 79000,
                        "employer_amount_fen": 158000,
                    }
                ],
            )
            # Even a caller bypassing the Python gate cannot leave a stale posted
            # consumer. No database trigger or permission is disabled here.
            with monkeypatch.context() as bypass:
                bypass.setattr("ai_accounting.corrections.source_change_gate", lambda *_: None)
                with pytest.raises(DBAPIError, match="SOURCE_CHANGE_REQUIRES_CORRECTION"):
                    with session.begin_nested():
                        with owner.attributed_call(
                            session, tool_name="finance_register_payroll_contribution_actual"
                        ):
                            registered = FinanceService(
                                session
                            ).register_payroll_contribution_actual(change)
                            assert registered["status"] == "registered", registered
                        session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            request = PreviewCorrectionRequest(org_id=org_id, source_changes=[change])
            with owner.attributed_call(session, tool_name="finance_preview_correction"):
                preview = CorrectionService(session).preview(request)
            assert preview["status"] == "calculated", preview
            from ai_accounting.models import BusinessCorrection

            for tool_name, error in (
                ("finance_preview_correction", "CORRECTION_INCOMPLETE"),
                ("finance_record_event", "BUSINESS_EXECUTION_ATTRIBUTION_REQUIRED"),
            ):
                with pytest.raises(DBAPIError, match=error):
                    with session.begin_nested():
                        with owner.attributed_call(session, tool_name=tool_name):
                            session.add(
                                BusinessCorrection(
                                    org_id=org_id,
                                    idempotency_key="incomplete",
                                    request_hash="0" * 64,
                                    calculation_hash="0" * 64,
                                    before_state={"event_ids": [str(source_id)]},
                                )
                            )
                            session.flush()
                            session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            confirmation = ConfirmCorrectionRequest(
                **request.model_dump(),
                calculation_hash=preview["calculation_hash"],
                idempotency_key="correct",
            )
            session.rollback()

            def write_once(_):
                with Session(engine) as writer:
                    with owner.attributed_call(writer, tool_name="finance_confirm_correction"):
                        result = CorrectionService(writer).confirm(confirmation)
                    writer.commit()
                    return result

            with ThreadPoolExecutor(max_workers=2) as pool:
                concurrent = list(pool.map(write_once, range(2)))
            assert all(result["status"] == "posted" for result in concurrent), concurrent
            assert sum(bool(result.get("idempotent_replay")) for result in concurrent) == 1

            # Exercise the real new-protocol replay dispatch against this isolated
            # database, including the committed-write/lost-checkpoint case.
            def replay_call(name, payload):
                assert name == "finance_confirm_correction"
                with Session(engine) as writer:
                    with owner.attributed_call(writer, tool_name=name):
                        result = CorrectionService(writer).confirm(
                            ConfirmCorrectionRequest.model_validate(payload)
                        )
                    writer.commit()
                    return result

            monkeypatch.setattr(replay_cli, "_call_tool", replay_call)
            resolver = replay_cli._ReplayResolver(engine=engine, org_id=org_id, results={})
            assert resolver.existing_calculation_hash("correct") is None
            assert resolver.existing_calculation_hash("correct", correction=True) == preview[
                "calculation_hash"
            ]
            replayed = replay_cli._preview_confirm(
                {
                    "key": "correction",
                    "kind": "preview_confirm",
                    "preview_tool": "finance_preview_correction",
                    "preview_request": request.model_dump(mode="json"),
                    "confirm_tool": "finance_confirm_correction",
                    "confirm_request": {"idempotency_key": "correct"},
                },
                resolver,
            )
            assert replayed["idempotent_replay"] is True
            history = replay_cli._correction_history(
                session, org_id, "0005_payroll_provenance"
            )
            assert len(history["corrections"]) == 1
            assert len(history["event_amendments"]) == (2 if combined_bonus else 1)
            assert {str(row["correction_id"]) for row in history["event_amendments"]} == {
                replayed["correction_id"]
            }
            replay_cli._write_json(tmp_path / "correction-history.json", history)
            with pytest.raises(DBAPIError, match="CORRECTION_AUDIT_IMMUTABLE"):
                with session.begin_nested():
                    session.execute(
                        text("UPDATE business_corrections SET reason='rewrite' WHERE org_id=:org"),
                        {"org": org_id},
                    )
            session.expire_all()
            assert session.get(PayrollBatch, batch_id).business_event_id == source_id
            voucher = session.scalar(select(Voucher).where(Voucher.event_id == source_id))
            assert voucher.voucher_number == original_number
            if combined_bonus:
                bonus_event = session.get(BusinessEvent, bonus_posted.event_id)
                with owner.attributed_call(session, tool_name="finance_delete_event"):
                    deleted_bonus = EventAmendmentService(session).amend(
                        DeleteEventRequest(
                            org_id=org_id,
                            event_id=bonus_event.id,
                            idempotency_key="delete-bonus",
                            expected_facts_hash=canonical_sha256(bonus_event.facts),
                        )
                    )
                assert deleted_bonus["status"] == "deleted", deleted_bonus
            with owner.attributed_call(session, tool_name="finance_delete_event"):
                deleted = EventAmendmentService(session).amend(
                    DeleteEventRequest(
                        org_id=org_id,
                        event_id=source_id,
                        idempotency_key="delete",
                        expected_facts_hash=canonical_sha256(
                            session.get(BusinessEvent, source_id).facts
                        ),
                    )
                )
            assert deleted["status"] == "deleted", deleted
            session.commit()


def test_postgres_closed_route_and_direct_write_guard():
    import pytest
    from conftest import prepare_authenticated_bank_account
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    from test_essential_postgres import _approve_close, _confirm_zero_opening, _record_pass_through

    from ai_accounting.accounting_period_schemas import (
        ConfirmAccountingPeriodCloseRequest,
        PreviewAccountingPeriodCloseRequest,
    )
    from ai_accounting.accounting_period_service import AccountingPeriodService
    from ai_accounting.component_schemas import RecordEventRequest
    from ai_accounting.event_amendment_schemas import CorrectionEventReplacement
    from ai_accounting.models import AccountingPeriod, AccountingPeriodClose, Organization
    from ai_accounting.schemas import ReverseEventRequest
    from ai_accounting.service import FinanceService

    with authenticated_business_database("correction_closed") as (
        engine,
        org_id,
        evidence_id,
        owner,
    ):
        with Session(engine) as session:
            org = session.get(Organization, org_id)
            for day in (date(2026, 7, 1), date(2026, 8, 1)):
                prepare_authenticated_bank_account(
                    session,
                    org,
                    booking_date=day,
                    authority=owner,
                    evidence_id=evidence_id,
                    accounts=[],
                )
            with owner.attributed_call(session, tool_name="finance_record_event"):
                posted = _record_pass_through(
                    session,
                    org_id,
                    evidence_id,
                    key="closed-source",
                    amount=10000,
                    posting_date=date(2026, 7, 5),
                )
            assert posted.status == "posted", posted
            _confirm_zero_opening(session, owner, org_id, evidence_id)
            session.commit()
            with owner.attributed_call(session, tool_name="finance_reverse_event"):
                rejected = FinanceService(session).reverse_event(
                    ReverseEventRequest(
                        org_id=org_id,
                        event_id=posted.event_id,
                        posting_date=date(2026, 8, 5),
                        idempotency_key="no-open-reversal",
                    )
                )
                assert rejected.errors == ["OPEN_PERIOD_REQUIRES_AMENDMENT"]
                with pytest.raises(DBAPIError, match="OPEN_PERIOD_REQUIRES_AMENDMENT"):
                    with session.begin_nested():
                        session.execute(
                            text(
                                "UPDATE business_events SET status='reversed',"
                                "reversed_by_event_id=id WHERE id=:id"
                            ),
                            {"id": posted.event_id},
                        )
            period = session.scalar(
                select(AccountingPeriod).where(
                    AccountingPeriod.org_id == org_id,
                    AccountingPeriod.calendar_year == 2026,
                    AccountingPeriod.calendar_month == 7,
                )
            )
            period_service = AccountingPeriodService(session, current_date=date(2026, 8, 11))
            close_request = PreviewAccountingPeriodCloseRequest(
                org_id=org_id, period_id=period.id, closing_date=date(2026, 7, 31)
            )
            preview = period_service.preview_accounting_period_close(close_request)
            assert preview.status == "calculated", preview
            with owner.attributed_call(
                session, tool_name="finance_confirm_accounting_period_close"
            ) as attribution:
                approval = _approve_close(session, attribution, period.id, preview.calculation_hash)
                closed = period_service.confirm_accounting_period_close(
                    ConfirmAccountingPeriodCloseRequest(
                        **close_request.model_dump(),
                        calculation_hash=preview.calculation_hash,
                        owner_approval_id=approval,
                        idempotency_key="close-july",
                    )
                )
            assert closed.status == "posted", closed
            session.commit()
            close_hash = session.get(AccountingPeriodClose, closed.close_id).calculation_hash
            source = session.get(BusinessEvent, posted.event_id)
            request = PreviewCorrectionRequest(
                org_id=org_id,
                event_replacements=[
                    CorrectionEventReplacement(
                        event_id=source.id,
                        expected_facts_hash=canonical_sha256(source.facts),
                        replacement=RecordEventRequest.model_validate(source.facts),
                    )
                ],
            )
            with owner.attributed_call(session, tool_name="finance_preview_correction"):
                blocked = CorrectionService(session).preview(request)
            assert blocked["errors"] == ["CORRECTION_CLOSED_DEPENDENCY"], blocked
            assert blocked["blocking_records"][0]["period"] == "2026-07"
            with owner.attributed_call(session, tool_name="finance_reverse_event"):
                reversed_result = FinanceService(session).reverse_event(
                    ReverseEventRequest(
                        org_id=org_id,
                        event_id=source.id,
                        posting_date=date(2026, 8, 5),
                        idempotency_key="closed-reversal",
                    )
                )
            assert reversed_result.status == "posted", reversed_result
            session.commit()
            assert (
                session.get(AccountingPeriodClose, closed.close_id).calculation_hash == close_hash
            )
