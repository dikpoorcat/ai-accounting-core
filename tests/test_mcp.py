from __future__ import annotations

import asyncio
import os
import sys
import tomllib
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp.exceptions import ToolError

from ai_accounting import mcp_server
from ai_accounting.accounting_period_schemas import (
    GetAccountingPeriodCloseApprovalRequest,
    RequestAccountingPeriodCloseApprovalWindowRequest,
)
from ai_accounting.agent_contract import (
    AI_OPERATING_PROTOCOL_VERSION,
    COMMUNICATION_RUNTIME_INSTRUCTION,
    CONFIRMATION_RUNTIME_INSTRUCTION,
    EVIDENCE_FIRST_RUNTIME_INSTRUCTION,
    IDENTITY_RUNTIME_INSTRUCTION,
    OWNER_WORKFLOW_RUNTIME_INSTRUCTION,
    PAYROLL_ACCRUAL_GATE_RUNTIME_INSTRUCTION,
    PAYROLL_TAX_IMPORT_RUNTIME_INSTRUCTION,
)
from ai_accounting.mcp_server import mcp


def test_project_mcp_config_does_not_override_application_environment() -> None:
    repository_root = Path(__file__).parents[1]
    with (repository_root / ".codex" / "config.toml").open("rb") as config_file:
        config = tomllib.load(config_file)

    accounting = config["mcp_servers"]["ai_accounting"]
    assert "env" not in accounting
    assert accounting["default_tools_approval_mode"] == "writes"


def test_mcp_exposes_only_domain_tools() -> None:
    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert names == {
        "finance_get_profile",
        "finance_get_owner_brief",
        "finance_get_owner_workflow",
        "finance_confirm_workforce_review",
        "finance_preview_payroll_contribution_assessment",
        "finance_confirm_payroll_contribution_assessment",
        "finance_confirm_period_material_completeness",
        "finance_confirm_external_obligation",
        "finance_confirm_historical_obligation_completion",
        "finance_confirm_organization_establishment",
        "finance_get_event_schema",
        "finance_list_companies",
        "finance_create_company",
        "finance_get_close_backup_configuration",
        "finance_configure_close_backup",
        "finance_preview_company_profile_change",
        "finance_confirm_company_profile_change",
        "finance_preview_company_status_change",
        "finance_confirm_company_status_change",
        "finance_register_evidence",
        "finance_import_bank_statement",
        "finance_preview_bank_statement_import",
        "finance_confirm_bank_statement_import",
        "finance_preview_bank_reconciliation_scope",
        "finance_confirm_bank_reconciliation_scope",
        "finance_preview_late_bank_evidence",
        "finance_confirm_late_bank_evidence",
        "finance_preview_bank_reconciliation",
        "finance_confirm_bank_reconciliation",
        "finance_query_bank_statement_state",
        "finance_register_employee",
        "finance_register_employee_profile_version",
        "finance_register_payroll_policy_version",
        "finance_register_payroll_opening_state",
        "finance_register_payroll_first_wage_tax_treatment",
        "finance_register_payroll_contribution_actual",
        "finance_record_payroll_contribution_supplement",
        "finance_preview_payroll",
        "finance_confirm_payroll",
        "finance_get_payroll_batch",
        "finance_generate_payroll_tax_import",
        "finance_register_labor_service_person",
        "finance_end_labor_service_person",
        "finance_preview_labor_remuneration_batch",
        "finance_confirm_labor_remuneration_batch",
        "finance_get_labor_remuneration",
        "finance_preview_unified_payout_run",
        "finance_confirm_unified_payout_run",
        "finance_pay_labor_withholding_tax",
        "finance_confirm_labor_external_declaration",
        "finance_acquire_fixed_asset",
        "finance_activate_fixed_asset",
        "finance_preview_fixed_asset_depreciation",
        "finance_confirm_fixed_asset_depreciation",
        "finance_preview_fixed_asset_depreciation_batch",
        "finance_confirm_fixed_asset_depreciation_batch",
        "finance_dispose_fixed_asset",
        "finance_get_fixed_asset",
        "finance_acquire_intangible_asset",
        "finance_preview_intangible_asset_amortization",
        "finance_confirm_intangible_asset_amortization",
        "finance_retire_intangible_asset",
        "finance_get_intangible_asset",
        "finance_draw_borrowing",
        "finance_preview_borrowing_interest",
        "finance_confirm_borrowing_interest",
        "finance_pay_borrowing_interest",
        "finance_repay_borrowing_principal",
        "finance_get_borrowing",
        "finance_generate_accounting_period",
        "finance_preview_accounting_period_close",
        "finance_request_accounting_period_close_approval_window",
        "finance_get_accounting_period_close_approval",
        "finance_confirm_accounting_period_close",
        "finance_configure_historical_test_close_mode",
        "finance_confirm_historical_test_period_close",
        "finance_get_accounting_periods",
        "finance_preview_quarterly_financial_statements",
        "finance_get_financial_statement_requirements",
        "finance_confirm_financial_statement_opening_balance",
        "finance_confirm_financial_statement_classification",
        "finance_confirm_enterprise_income_tax_quarter",
        "finance_query_context",
        "finance_record_event",
        "finance_calculate_tax_period",
        "finance_confirm_tax_period",
        "finance_reverse_event",
        "finance_get_event",
    }
    assert not any("journal_line" in name or "sql" in name for name in names)


def test_every_registered_tool_publishes_a_closed_top_level_envelope() -> None:
    tools = asyncio.run(mcp.list_tools())
    assert tools
    assert all(tool.inputSchema.get("additionalProperties") is False for tool in tools)


def test_owner_brief_is_a_strict_read_only_tool() -> None:
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    brief = tools["finance_get_owner_brief"]

    assert brief.annotations is not None
    assert brief.annotations.readOnlyHint is True
    assert brief.annotations.destructiveHint is False
    assert brief.inputSchema["additionalProperties"] is False
    assert brief.inputSchema["required"] == ["org_id"]
    assert set(brief.inputSchema["properties"]) == {"org_id"}
    with pytest.raises(ToolError, match="VALIDATION_ERROR") as caught:
        asyncio.run(
            mcp.call_tool(
                "finance_get_owner_brief",
                {"org_id": str(uuid.uuid4()), "unexpected": "private-sentinel"},
            )
        )
    assert "private-sentinel" not in str(caught.value)


def test_historical_obligation_completion_is_a_strict_typed_write() -> None:
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    confirmation = tools["finance_confirm_historical_obligation_completion"]

    assert confirmation.annotations is not None
    assert confirmation.annotations.readOnlyHint is False
    assert confirmation.inputSchema["additionalProperties"] is False
    request_schema = confirmation.inputSchema["$defs"][
        "ConfirmHistoricalObligationCompletionRequest"
    ]
    assert request_schema["additionalProperties"] is False
    assert set(request_schema["required"]) == {
        "org_id",
        "obligation_code",
        "completion_through_identity",
        "source_snapshot_hash",
        "completion_date_status",
        "idempotency_key",
        "confirmation_note",
    }
    assert request_schema["properties"]["completion_date_status"]["const"] == ("not_established")
    assert "individual_income_tax" in request_schema["properties"]["obligation_code"]["enum"]


def test_contribution_confirmation_tracks_declaration_without_payment_fields() -> None:
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    confirmation = tools["finance_confirm_payroll_contribution_assessment"]
    request_schema = confirmation.inputSchema["$defs"][
        "ConfirmPayrollContributionAssessmentRequest"
    ]

    assert request_schema["additionalProperties"] is False
    assert request_schema["properties"]["declaration_status"]["const"] == "declared"
    assert "declaration_date" not in request_schema["required"]
    assert "payment_status" not in request_schema["properties"]
    assert "payment_date" not in request_schema["properties"]


def test_external_obligation_completion_date_is_optional() -> None:
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    confirmation = tools["finance_confirm_external_obligation"]
    request_schema = confirmation.inputSchema["$defs"]["ConfirmExternalObligationRequest"]

    assert request_schema["additionalProperties"] is False
    assert "completion_date" in request_schema["properties"]
    assert "completion_date" not in request_schema["required"]


def test_ai_operating_contract_is_published_at_runtime_and_in_discovery() -> None:
    schema = mcp_server.finance_get_event_schema()
    protocol = schema["agent_operating_protocol"]

    assert (
        "finance_confirm_historical_obligation_completion"
        in schema["module_capabilities"]["owner_workflow"]["typed_confirmation_tools"]
    )
    assert protocol["version"] == AI_OPERATING_PROTOCOL_VERSION
    assert protocol["identity"] == {
        "role": "accounting_execution_assistant",
        "audience": "local_business_owner",
        "mission": "审阅资料、整理业务事实、调用受控工具并用业务语言报告结果。",
        "boundaries": [
            "不是注册会计师或税务机关。",
            "不是自动纳税申报或报税系统。",
            "不得把自己描述成确定性记账内核本身。",
        ],
    }
    assert protocol["communication_policy"]["style"] == "execution_secretary"
    assert protocol["communication_policy"]["fixed_salutation"] is None
    assert protocol["communication_policy"]["maximum_questions_per_response"] == 1
    assert protocol["communication_policy"]["needs_information_order"] == [
        "reviewed_facts",
        "reasoned_assessment",
        "recommended_answer",
        "material_effect",
        "confirm_or_correct",
    ]
    assert protocol["communication_policy"]["assistance_policy"] == {
        "default_behavior": "investigate_reason_recommend_execute",
        "owner_role": "confirm_or_correct_material_assumptions_and_do_external_actions",
        "before_asking": [
            "inspect_all_available_evidence",
            "use_relevant_read_only_tools",
            "derive_supported_facts",
            "compare_transaction_chronology_and_linked_events",
        ],
        "when_unique": "execute_without_redundant_chat_confirmation",
        "when_one_candidate_is_best_supported": {
            "response": "present_one_complete_proposal_then_ask_confirm_or_correct",
            "include_linked_missing_fields_in_proposal": True,
            "formal_use_requires_owner_confirmation": True,
        },
        "when_no_responsible_candidate": (
            "explain_why_then_ask_one_precise_factual_question_without_inventing"
        ),
        "prohibit_form_style_field_requests": True,
    }
    assert protocol["communication_policy"]["owner_action_view"] == {
        "current_action_count": 1,
        "queue_length": "core_steps_1_to_6_plus_active_steps_7_to_9",
        "queue_source": "finance_get_owner_workflow.queue_steps",
        "next_action_requires_queue": True,
        "queue_position": "immediately_after_next_action",
        "queue_status_display": {
            "completed": "✅",
            "current": "🔄",
            "due_or_overdue": "⏰",
            "pending": "⬜",
            "not_applicable": "➖",
        },
        "show_bracketed_status_text": False,
        "completed_requires": ["finance_get_owner_workflow_completion_state"],
        "never_merge_workflow_steps": True,
        "include_only": [
            "owner_material",
            "owner_fact",
            "owner_confirmation",
            "owner_external_filing_or_payment",
        ],
        "exclude": ["ai_internal_work"],
        "show_when": ["every_response_with_next_action", "owner_requests_status"],
    }
    assert protocol["version"] == "accounting_execution_assistant_v27"
    assert protocol["owner_workflow"]["version"] == "owner_monthly_workflow_cn_2026.10"
    assert protocol["owner_workflow"]["status_source"] == "finance_get_owner_workflow"
    assert protocol["owner_workflow"]["confirmation_target_source"] == "confirmation_targets"
    assert protocol["owner_workflow"]["target_selection"] == (
        "ai_interprets_selected_company_step_and_conversation_context"
    )
    assert protocol["owner_workflow"]["external_completion_date"] == {
        "required": False,
        "when_known": "persist_as_established",
        "when_unknown": "persist_as_not_established",
        "affects_completion": False,
    }
    assert protocol["owner_workflow"]["prohibit_chat_derived_completion"] is True
    assert protocol["owner_workflow"]["visibility_rule"] == (
        "always_show_steps_1_to_6_and_show_steps_7_to_9_only_while_attention_required"
    )
    assert [step["code"] for step in protocol["owner_workflow"]["steps"]] == [
        "BANK_STATEMENTS",
        "WORKFORCE_AND_PAY_CHANGES",
        "SOCIAL_INSURANCE_AND_HOUSING_FUND",
        "INDIVIDUAL_INCOME_TAX_WITHHOLDING",
        "NON_BANK_MATERIALS",
        "PERIOD_CLOSE_APPROVAL",
        "PERIODIC_TAX_AND_FINANCIAL_REPORTING",
        "ANNUAL_ENTERPRISE_INCOME_TAX_SETTLEMENT",
        "ANNUAL_BUSINESS_REPORT",
    ]
    workforce_step = next(
        step
        for step in protocol["owner_workflow"]["steps"]
        if step["code"] == "WORKFORCE_AND_PAY_CHANGES"
    )
    assert workforce_step["completion_gate"] == {
        "typed_fact": "finance_confirm_workforce_review",
        "snapshot": "current_workforce_snapshot_hash",
        "regular_payroll_required": False,
        "monthly_payroll_input_persistence": (
            "reuse_current_draft_or_persisted_plan_or_prior_posted_when_no_change"
        ),
    }
    assert workforce_step["owner_answer_alone_completes_step"] is False
    contribution_step = next(
        step
        for step in protocol["owner_workflow"]["steps"]
        if step["code"] == "SOCIAL_INSURANCE_AND_HOUSING_FUND"
    )
    assert contribution_step["accounting_close_gate"] == (
        "current_amount_assessment_and_posted_payroll_use_same_snapshot"
    )
    assert contribution_step["status_choices"] == ["已申报", "尚未申报"]
    assert contribution_step["confirmation_fields"] == ["declared_amount_snapshot"]
    assert contribution_step["optional_confirmation_fields"] == ["declaration_date"]
    assert contribution_step["payment_tracking"] == (
        "later_bank_statement_only_not_owner_workflow_input"
    )
    assert contribution_step["not_declared_behavior"] == (
        "keep_current_without_persisting_completion"
    )
    individual_income_tax_step = next(
        step
        for step in protocol["owner_workflow"]["steps"]
        if step["code"] == "INDIVIDUAL_INCOME_TAX_WITHHOLDING"
    )
    assert (
        individual_income_tax_step["payroll_import_tool"] == "finance_generate_payroll_tax_import"
    )
    assert (
        individual_income_tax_step["entry_action"]
        == "ensure_posted_regular_payroll_then_generate_before_status_question"
    )
    assert individual_income_tax_step["if_expected_payroll_unposted"] == {
        "current_step": "SOCIAL_INSURANCE_AND_HOUSING_FUND",
        "individual_income_tax_status": "pending",
        "action": "confirm_assessment_then_post_payroll_before_tax_import",
        "prohibit_external_status_question": True,
    }
    assert individual_income_tax_step["desktop_delivery"] == {
        "destination": "os_current_user_desktop_known_folder",
        "source": "generated_result.file_path",
        "file_name": "generated_result.file_name",
        "helper": (".agents/skills/accounting-operator/scripts/copy-export-to-desktop.ps1"),
        "verify_sha256": True,
        "existing_same_hash": "reuse_as_idempotent_success",
        "existing_different_hash": "do_not_overwrite_report_collision",
    }
    assert individual_income_tax_step["generation_is_external_declaration"] is False
    assert individual_income_tax_step["export_record_is_persistent"] is True
    assert individual_income_tax_step["status_choices"] == ["已申报", "尚未申报"]
    assert individual_income_tax_step["declaration_close_gate"] == (
        "current_external_submission_confirmation"
    )
    assert individual_income_tax_step["completion_date_required"] is False
    assert individual_income_tax_step["completion_date_when_known"] == (
        "external_declaration_date"
    )
    assert individual_income_tax_step["historical_completion_tool"] == (
        "finance_confirm_historical_obligation_completion"
    )
    assert individual_income_tax_step["payment_tracking"] == (
        "later_bank_statement_only_not_owner_workflow_input"
    )
    assert (
        individual_income_tax_step["remains_current_until"]
        == "owner_confirms_external_declaration_status"
    )
    assert protocol["confirmation_policy"] == {
        "ordinary_formal_write": "host_write_tool_approval",
        "redundant_chat_confirmation": False,
        "material_inference": "owner_confirm_or_correct_before_formal_use",
        "silence_is_confirmation": False,
        "approval_rejected_or_cancelled": "stop_without_retry",
        "specialized_controls_remain_required": [
            "owner_login_window",
            "accounting_period_close_password_window",
            "preview_calculation_hash",
            "workflow_specific_confirmation",
        ],
    }
    assert [item["code"] for item in protocol["required_sequence"]] == [
        "inspect_available_materials",
        "derive_when_unique",
        "persist_historical_obligation_cutoffs",
        "identify_material_unknowns",
        "propose_best_supported_treatment",
        "persist_workforce_then_assess_contributions_before_income_tax",
        "separate_contribution_policy_actual_and_cash",
        "settle_person_paid_existing_payables_without_new_expense",
        "apply_first_wage_tax_treatment_only_with_evidence",
        "generate_period_close_management_commentary",
        "satisfy_deterministic_close_obligations",
        "satisfy_financial_statement_close_gate",
        "batch_historical_test_close_only_when_explicit",
        "launch_visible_close_approval_window",
        "verify_automatic_close_backup",
        "ask_minimum_specific_question",
        "submit_or_stop",
    ]
    assert any("不得让用户代替AI" in item for item in protocol["prohibitions"])
    assert any("方案拟定工作转交老板" in item for item in protocol["prohibitions"])
    assert any("不得在隐藏或不可见的终端通道" in item for item in protocol["prohibitions"])
    assert any("不得绕过内核关账自动备份" in item for item in protocol["prohibitions"])
    assert "除已提供并核对的材料外" in protocol["question_policy"]["final_fallback"]
    assert "完整方案" in protocol["question_policy"]["recommended_confirmation"]
    assert "字段模板" in protocol["question_policy"]["prohibited_request_style"]
    assert EVIDENCE_FIRST_RUNTIME_INSTRUCTION in mcp.instructions
    assert IDENTITY_RUNTIME_INSTRUCTION in mcp.instructions
    assert COMMUNICATION_RUNTIME_INSTRUCTION in mcp.instructions
    assert "不得只输出孤立的下一步问题" in mcp.instructions
    assert OWNER_WORKFLOW_RUNTIME_INSTRUCTION in mcp.instructions
    assert PAYROLL_ACCRUAL_GATE_RUNTIME_INSTRUCTION in mcp.instructions
    assert PAYROLL_TAX_IMPORT_RUNTIME_INSTRUCTION in mcp.instructions
    assert "不得要求工资已过账才完成第2项" in mcp.instructions
    assert "禁止直接询问个税外部申报状态" in mcp.instructions
    assert "不得先问老板是否生成" in mcp.instructions
    assert "当前用户桌面已知目录" in mcp.instructions
    assert CONFIRMATION_RUNTIME_INSTRUCTION in mcp.instructions
    assert "不得要求老板逐字段填表" in mcp.instructions
    assert "agent_operating_protocol" in mcp.instructions
    assert "management_commentary" in mcp.instructions
    assert "一至两个短句的简明综合判断" in mcp.instructions
    assert "不得把看板指标或关账清单简单拼接" in mcp.instructions
    assert "存在阻断时必须先补事实再关账" in mcp.instructions
    assert "不要求独立的工资结算复核" in mcp.instructions
    assert "finance_request_accounting_period_close_approval_window" in mcp.instructions
    assert "finance_get_accounting_period_close_approval" in mcp.instructions
    assert "AI 记账内核 - 关账密码确认" in mcp.instructions
    assert "不得直接在隐藏终端" in mcp.instructions
    assert "finance_get_close_backup_configuration" in mcp.instructions
    assert "close_backup.status=failed" in mcp.instructions
    assert "另写临时备份脚本" in mcp.instructions
    assert "finance_configure_historical_test_close_mode" in mcp.instructions
    assert "finance_confirm_historical_test_period_close" in mcp.instructions
    assert "close_backup.status=deferred" in mcp.instructions

    on_behalf = mcp_server.finance_get_event_schema("employee_reimbursement")
    assert "existing_payable" in on_behalf["event_requirements"]["existing_payable_workflow"]
    cash_payment = mcp_server.finance_get_event_schema("employee_reimbursement_payment")
    assert cash_payment["event_requirements"]["optional_details"] == [
        "settlement_method=bank|cash|owner_managed_reserve; omitted means bank"
    ]
    assert "inventory-cash" in cash_payment["event_requirements"]["cash_settlement"]
    assert (
        "original_event_id"
        in cash_payment["event_requirements"]["owner_managed_reserve_settlement"]
    )


def test_close_approval_window_and_result_are_exposed_as_mcp_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid.uuid4()
    period_id = uuid.uuid4()
    calculation_hash = "a" * 64
    approval_id = uuid.uuid4()
    expires_at = datetime.now(UTC) + timedelta(minutes=30)
    launched: list[dict[str, str]] = []

    class _Session:
        def __init__(self) -> None:
            self.approval: object | None = None

        def __enter__(self) -> _Session:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def scalar(self, _statement: object) -> object:
            return self.approval or SimpleNamespace(
                id=period_id,
                org_id=org_id,
                status="open",
                end_date=date(2022, 9, 30),
            )

    class _Launcher:
        def request(self, **kwargs: str) -> bool:
            launched.append(kwargs)
            return True

    session = _Session()
    monkeypatch.setattr(mcp_server, "SessionLocal", lambda: session)
    monkeypatch.setattr(
        mcp_server,
        "_accounting_period_service",
        lambda _session: SimpleNamespace(
            preview_accounting_period_close=lambda _request: SimpleNamespace(
                status=SimpleNamespace(value="calculated"),
                calculation_hash=calculation_hash,
            )
        ),
    )
    monkeypatch.setattr(
        mcp_server,
        "_active_owner_login_name",
        lambda _session, *, org_id: "owner",
    )
    monkeypatch.setattr(mcp_server, "_OWNER_CLOSE_APPROVAL_WINDOW_LAUNCHER", _Launcher())

    request = RequestAccountingPeriodCloseApprovalWindowRequest(
        org_id=org_id,
        period_id=period_id,
        calculation_hash=calculation_hash,
    )
    result = mcp_server.finance_request_accounting_period_close_approval_window(request)

    assert result == {
        "status": "requested",
        "period_id": str(period_id),
        "calculation_hash": calculation_hash,
        "window_title": "AI 记账内核 - 关账密码确认",
    }
    assert launched == [
        {
            "org_id": str(org_id),
            "period_id": str(period_id),
            "calculation_hash": calculation_hash,
            "login_name": "owner",
        }
    ]

    session.approval = SimpleNamespace(id=approval_id, expires_at=expires_at)
    marker = mcp_server._ACTIVE_EXECUTION_CONTEXT.set(
        SimpleNamespace(
            org_id=org_id,
            catalog_instance_id=uuid.uuid4(),
            owner_account_id=uuid.uuid4(),
            owner_session_id=uuid.uuid4(),
            owner_credential_version=1,
        )
    )
    try:
        approval = mcp_server.finance_get_accounting_period_close_approval(
            GetAccountingPeriodCloseApprovalRequest(
                org_id=org_id,
                period_id=period_id,
                calculation_hash=calculation_hash,
            )
        )
    finally:
        mcp_server._ACTIVE_EXECUTION_CONTEXT.reset(marker)

    assert approval == {
        "status": "ready",
        "period_id": str(period_id),
        "calculation_hash": calculation_hash,
        "owner_approval_id": str(approval_id),
        "expires_at": expires_at.isoformat(),
    }


def test_close_approval_window_bootstraps_when_mcp_session_has_expired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid.uuid4()
    period_id = uuid.uuid4()
    calculation_hash = "b" * 64
    owner = SimpleNamespace(org_id=uuid.uuid4(), status="active", login_name="owner")
    period = SimpleNamespace(
        id=period_id,
        org_id=org_id,
        status="open",
        end_date=date(2022, 10, 31),
    )
    catalog_scalar_results = iter([owner, owner])
    business_scalar_results = iter([period])
    launched: list[dict[str, str]] = []

    class _Session:
        def __init__(self, scalar_results: Iterator[object]) -> None:
            self.info: dict[str, object] = {}
            self._scalar_results = scalar_results

        def __enter__(self) -> _Session:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def scalar(self, _statement: object) -> object:
            return next(self._scalar_results)

    catalog_session = _Session(catalog_scalar_results)
    business_session = _Session(business_scalar_results)

    class _CatalogFactory:
        def __call__(self) -> _Session:
            return catalog_session

        def begin(self) -> _Session:
            return catalog_session

    class _Launcher:
        def request(self, **kwargs: str) -> bool:
            launched.append(kwargs)
            return True

    monkeypatch.setattr(
        mcp_server,
        "get_settings",
        lambda: SimpleNamespace(
            finance_environment="production",
            multi_company_enabled=True,
        ),
    )
    monkeypatch.setattr(
        mcp_server,
        "SessionLocal",
        mcp_server._ContextAwareSessionFactory(_CatalogFactory()),
    )
    monkeypatch.setattr(
        mcp_server,
        "company_router",
        SimpleNamespace(
            resolve=lambda _session, _org_id, *, for_write: SimpleNamespace(),
            factory_for=lambda _registry: lambda: business_session,
        ),
    )
    monkeypatch.setattr(
        mcp_server,
        "_load_current_session_token",
        lambda: pytest.fail("close-approval window must not require an active MCP session"),
    )
    monkeypatch.setattr(
        mcp_server,
        "_accounting_period_service",
        lambda _session: SimpleNamespace(
            preview_accounting_period_close=lambda _request: SimpleNamespace(
                status=SimpleNamespace(value="calculated"),
                calculation_hash=calculation_hash,
            )
        ),
    )
    monkeypatch.setattr(mcp_server, "_OWNER_CLOSE_APPROVAL_WINDOW_LAUNCHER", _Launcher())

    tool = mcp._tool_manager.get_tool("finance_request_accounting_period_close_approval_window")
    assert tool is not None
    result = tool.fn(
        RequestAccountingPeriodCloseApprovalWindowRequest(
            org_id=org_id,
            period_id=period_id,
            calculation_hash=calculation_hash,
        )
    )

    assert result["status"] == "requested"
    assert launched == [
        {
            "org_id": str(org_id),
            "period_id": str(period_id),
            "calculation_hash": calculation_hash,
            "login_name": "owner",
        }
    ]


def test_close_approval_poll_waits_without_opening_generic_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid.uuid4()
    period_id = uuid.uuid4()
    calculation_hash = "c" * 64
    owner = SimpleNamespace(org_id=org_id, status="active", login_name="owner")

    class _Session:
        info: dict[str, object] = {}

        def __enter__(self) -> _Session:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def scalar(self, _statement: object) -> object:
            return owner

    class _Factory:
        def __call__(self) -> _Session:
            return _Session()

        def begin(self) -> _Session:
            return _Session()

    monkeypatch.setattr(
        mcp_server,
        "get_settings",
        lambda: SimpleNamespace(
            finance_environment="production",
            multi_company_enabled=False,
        ),
    )
    monkeypatch.setattr(
        mcp_server,
        "SessionLocal",
        mcp_server._ContextAwareSessionFactory(_Factory()),
    )
    monkeypatch.setattr(mcp_server, "_load_current_session_token", lambda: None)
    monkeypatch.setattr(
        mcp_server,
        "_OWNER_LOGIN_WINDOW_LAUNCHER",
        SimpleNamespace(
            request=lambda **_kwargs: pytest.fail(
                "approval polling must not open the generic login window"
            )
        ),
    )

    tool = mcp._tool_manager.get_tool("finance_get_accounting_period_close_approval")
    assert tool is not None
    result = tool.fn(
        GetAccountingPeriodCloseApprovalRequest(
            org_id=org_id,
            period_id=period_id,
            calculation_hash=calculation_hash,
        )
    )

    assert result == {
        "status": "pending",
        "period_id": str(period_id),
        "calculation_hash": calculation_hash,
    }


@pytest.mark.parametrize(
    ("tool_name", "arguments", "forbidden_value"),
    [
        (
            "finance_get_event_schema",
            {"actor": "caller-actor"},
            "caller-actor",
        ),
        (
            "finance_get_profile",
            {
                "org_id": "11111111-1111-1111-1111-111111111111",
                "session_token": "profile-token",
            },
            "profile-token",
        ),
        (
            "finance_query_context",
            {
                "org_id": "11111111-1111-1111-1111-111111111111",
                "actor": "query-actor",
            },
            "query-actor",
        ),
        (
            "finance_get_event",
            {
                "org_id": "11111111-1111-1111-1111-111111111111",
                "event_id": "22222222-2222-2222-2222-222222222222",
                "session_token": "event-token",
            },
            "event-token",
        ),
    ],
)
def test_read_tool_runtime_rejects_caller_identity_and_session_extras(
    tool_name: str,
    arguments: dict[str, object],
    forbidden_value: str,
) -> None:
    with pytest.raises(ToolError, match="VALIDATION_ERROR") as caught:
        asyncio.run(mcp.call_tool(tool_name, arguments))
    assert forbidden_value not in str(caught.value)


def test_stdio_server_initializes_and_lists_tools() -> None:
    async def run() -> tuple[set[str], str]:
        repository_root = Path(__file__).parents[1]
        site_packages = Path(sys.prefix) / "Lib" / "site-packages"
        environment = os.environ.copy()
        # Tool-discovery smoke tests must not join the machine's production
        # service/backup lease or depend on a developer's local .env mode.
        environment["FINANCE_ENVIRONMENT"] = "development"
        environment["PYTHONPATH"] = os.pathsep.join(
            filter(
                None,
                [
                    str(repository_root / "src"),
                    str(site_packages),
                    str(site_packages / "win32"),
                    str(site_packages / "win32" / "lib"),
                    str(site_packages / "pywin32_system32"),
                    environment.get("PYTHONPATH"),
                ],
            )
        )
        parameters = StdioServerParameters(
            # On Windows the virtualenv launcher starts a second process that
            # stdio_client cannot reliably reap.  The base interpreter plus the
            # virtualenv dependencies keeps this smoke test self-contained.
            command=getattr(sys, "_base_executable", sys.executable),
            args=["-m", "ai_accounting.mcp_server"],
            cwd=repository_root,
            env=environment,
        )
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                initialized = await session.initialize()
                response = await session.list_tools()
                return (
                    {tool.name for tool in response.tools},
                    initialized.instructions or "",
                )

    names, instructions = asyncio.run(run())
    assert "finance_request_accounting_period_close_approval_window" in instructions
    assert "finance_get_close_backup_configuration" in instructions
    assert "close_backup.status=failed" in instructions
    assert "finance_record_event" in names
    assert "finance_get_event" in names
    assert {
        "finance_register_employee",
        "finance_register_employee_profile_version",
        "finance_register_payroll_policy_version",
        "finance_register_payroll_opening_state",
        "finance_preview_payroll",
        "finance_confirm_payroll",
        "finance_get_payroll_batch",
        "finance_acquire_fixed_asset",
        "finance_activate_fixed_asset",
        "finance_preview_fixed_asset_depreciation",
        "finance_confirm_fixed_asset_depreciation",
        "finance_preview_fixed_asset_depreciation_batch",
        "finance_confirm_fixed_asset_depreciation_batch",
        "finance_dispose_fixed_asset",
        "finance_get_fixed_asset",
        "finance_acquire_intangible_asset",
        "finance_preview_intangible_asset_amortization",
        "finance_confirm_intangible_asset_amortization",
        "finance_retire_intangible_asset",
        "finance_get_intangible_asset",
        "finance_draw_borrowing",
        "finance_preview_borrowing_interest",
        "finance_confirm_borrowing_interest",
        "finance_pay_borrowing_interest",
        "finance_repay_borrowing_principal",
        "finance_get_borrowing",
    } <= names


def test_production_real_stdio_keeps_schema_public_and_data_tools_fail_closed() -> None:
    async def run() -> tuple[object, object, set[str], dict[str, object]]:
        repository_root = Path(__file__).parents[1]
        site_packages = Path(sys.prefix) / "Lib" / "site-packages"
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join(
            filter(
                None,
                [
                    str(repository_root / "src"),
                    str(site_packages),
                    str(site_packages / "win32"),
                    str(site_packages / "win32" / "lib"),
                    str(site_packages / "pywin32_system32"),
                    environment.get("PYTHONPATH"),
                ],
            )
        )
        script = """
from contextlib import nullcontext
from types import SimpleNamespace
from ai_accounting import mcp_server

class EmptyCredentialStore:
    def load_session_token(self):
        return None

class FakeSession:
    def __init__(self):
        self.info = {}

    def scalar(self, _statement):
        return SimpleNamespace(login_name="owner")

class FakeTransaction:
    def __enter__(self):
        return FakeSession()

    def __exit__(self, *_args):
        return None

class FakeSessionFactory:
    def begin(self):
        return FakeTransaction()

mcp_server.get_settings = lambda: SimpleNamespace(
    finance_environment="production",
    finance_service_lock_file="injected-test-service.lock",
)
mcp_server.WindowsCredentialStore = EmptyCredentialStore
mcp_server.WindowsCurrentUserOnlyAclVerifier = object
mcp_server.acquire_windows_service_lease = lambda *args, **kwargs: nullcontext()
mcp_server.SessionLocal = FakeSessionFactory()
mcp_server._OWNER_LOGIN_WINDOW_LAUNCHER = SimpleNamespace(
    request=lambda **_kwargs: True
)
mcp_server.main()
"""
        parameters = StdioServerParameters(
            command=getattr(sys, "_base_executable", sys.executable),
            args=["-c", script],
            cwd=repository_root,
            env=environment,
        )
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                schema = await session.call_tool("finance_get_event_schema", {})
                data = await session.call_tool(
                    "finance_get_profile",
                    {"org_id": str(uuid.uuid4())},
                )
                tools = await session.list_tools()
                by_name = {tool.name: tool for tool in tools.tools}
                return (
                    schema,
                    data,
                    set(by_name),
                    by_name["finance_preview_bank_statement_import"].inputSchema,
                )

    schema, data, names, import_schema = asyncio.run(run())
    assert schema.isError is False
    assert data.isError is False
    assert data.structuredContent == {
        "status": "rejected",
        "errors": ["AUTHENTICATION_REQUIRED"],
    }
    assert "finance_import_bank_statement" not in names
    assert {
        "finance_preview_bank_statement_import",
        "finance_confirm_bank_statement_import",
        "finance_query_bank_statement_state",
    } <= names
    assert import_schema["additionalProperties"] is False
    request_schema = import_schema["$defs"]["PreviewBankStatementFileImportRequest"]
    assert request_schema["additionalProperties"] is False
    assert request_schema["properties"]["file_format"]["const"] == "csv"
    assert "sheet_name" not in request_schema["properties"]
    assert "file_path" not in request_schema["properties"]
    assert "statement_bytes" not in request_schema["properties"]


def test_formal_server_initializes_store_without_caching_token_inside_service_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_accounting import mcp_server

    calls: list[str] = []

    class _CredentialStore:
        def __init__(self) -> None:
            calls.append("credential-init")

        def load_session_token(self) -> None:
            calls.append("credential-read")
            return None

    class _Lease:
        def __enter__(self) -> None:
            calls.append("lease-enter")

        def __exit__(self, *_args: object) -> None:
            calls.append("lease-exit")

    monkeypatch.setattr(
        mcp_server,
        "get_settings",
        lambda: SimpleNamespace(
            finance_environment="production",
            finance_service_lock_file=Path("service.lock"),
        ),
    )
    monkeypatch.setattr(mcp_server, "WindowsCredentialStore", _CredentialStore)
    monkeypatch.setattr(mcp_server, "WindowsCurrentUserOnlyAclVerifier", object)
    monkeypatch.setattr(
        mcp_server,
        "acquire_windows_service_lease",
        lambda *_args, **_kwargs: _Lease(),
    )
    monkeypatch.setattr(
        mcp_server.mcp,
        "run",
        lambda *, transport: calls.append(f"run:{transport}"),
    )

    mcp_server.main()

    assert calls == ["lease-enter", "credential-init", "run:stdio", "lease-exit"]
