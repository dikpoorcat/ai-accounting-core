"""Synthetic live read samples shared with the generated browser validators.

Only temporary databases are used. Regeneration invokes today's kernel rather
than repairing historical JSON fixtures to fit a new response schema.
"""

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import test_banking as banking
from test_dashboard_bank_source_proof import closed_banks
from test_dashboard_empty_replay import (
    authenticated,
    company_call,
    finish_payment,
    replay_sources,
    reviewed_confirmation,
)
from test_dashboard_provenance import profile as display_profile
from test_payroll import payroll
from test_payroll import profile as payroll_profile
from test_payroll_corrections import Company
from test_payroll_corrections import company as correction_company
from test_payroll_preparation import company as payroll_company
from test_tax_import import contribution_rule, replace_contribution_policy

from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.domains.payroll import LaborAccrual
from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.response_contracts import http_response, validate_response


def public_bank_book(path):
    """Adapt older scenario helpers to generated IDs through the public entity API."""

    engine, save, publish, proof = banking.book.__wrapped__(path)
    entities = Entities(engine)
    identifiers = {
        "owner": entities.register_entity(
            "person", {}, source="合成银行资料", request_id="bank-owner"
        )["entity_id"],
        "bank-a": entities.register_entity(
            "fund_account",
            {},
            account_type="bank",
            source="合成银行资料",
            request_id="bank-a",
        )["entity_id"],
        "bank-b": entities.register_entity(
            "fund_account",
            {},
            account_type="bank",
            source="合成银行资料",
            request_id="bank-b",
        )["entity_id"],
    }

    def mapped_save(kind, subject, data, revision=0):
        mapped = dict(data)
        for field in ("bank_account_id", "owner_id"):
            if mapped.get(field) in identifiers:
                mapped[field] = identifiers[mapped[field]]
        return save(kind, subject, mapped, revision)

    return (engine, mapped_save, publish, proof), identifiers


def native_samples(root):
    samples = {}

    def add(name, command, response):
        validate_response(command, response)
        samples[name] = {"command": command, "response": response}

    service, dispatch = authenticated(root / "replay")
    add("empty_context", "dashboard_context", dispatch("dashboard_context", {}))
    company = service.catalog.create_company("91310000123456789A", "合成合同测试公司")["id"]
    call = company_call(dispatch, company)
    add("empty_workflow", "workflow", call("workflow", as_of="2026-02-25"))
    add(
        "empty_readiness",
        "period_readiness",
        call("period_readiness", period="2026-01", as_of="2026-02-25"),
    )
    add("company_without_period", "dashboard_context", call("dashboard_context"))
    add("funds_without_period", "dashboard_funds", call("dashboard_funds"))
    add("first_account_without_period", "dashboard_funds",
        call("dashboard_funds", movement_account_selection="first"))
    references, evidence, _ = replay_sources(call, "contract")
    call(
        "confirm",
        **reviewed_confirmation(
            call, [references["funding"], references["expense"]], "publish:initial:v1"
        ),
    )
    finish_payment(call, references, evidence)
    add("open_workflow", "workflow", call("workflow", as_of="2026-02-25"))
    add(
        "open_readiness",
        "period_readiness",
        call("period_readiness", period="2026-01", as_of="2026-02-25"),
    )
    add("company_with_period", "dashboard_context", call("dashboard_context"))
    brief = call("dashboard_brief", period="2026-01")
    add("brief", "dashboard_brief", brief)
    add("brief_vouchers", "dashboard_brief", call("dashboard_brief", period="2026-01", section="vouchers"))
    for section in ("activity", "open_items"):
        for index, group in enumerate(brief["data"]["collections"][section]["items"]):
            add(f"brief_{section}_members_{index}", "dashboard_brief_group", call(
                "dashboard_brief_group", period="2026-01", section=section,
                group_key=group["group_key"], expected_version=brief["snapshot_version"],
            ))
    add(
        "deferred_brief",
        "dashboard_brief",
        call("dashboard_brief", period="2026-01", preparation="deferred"),
    )
    add("cash_funds", "dashboard_funds", call("dashboard_funds", period="2026-01"))
    add("first_account_funds", "dashboard_funds",
        call("dashboard_funds", period="2026-01", movement_account_selection="first"))
    add(
        "deferred_funds",
        "dashboard_funds",
        call("dashboard_funds", period="2026-01", preparation="deferred"),
    )
    for section in (
        "accounts",
        "movements",
        "statements",
        "investment_products",
        "investment_events",
    ):
        add(
            "page_" + section,
            "dashboard_funds",
            call("dashboard_funds", period="2026-01", section=section, limit=1),
        )
    add(
        "account_filter",
        "dashboard_funds",
        call(
            "dashboard_funds",
            period="2026-01",
            movement_account_type="cash",
            movement_account_id=references["cash"],
            limit=1,
        ),
    )
    add("employees", "dashboard_employees", call("dashboard_employees", period="2026-01"))
    add("assets", "dashboard_assets", call("dashboard_assets", period="2026-01"))
    add(
        "business_status",
        "dashboard_business_status",
        call(
            "dashboard_business_status",
            period="2026-01",
            subject_id=references["expense"],
        ),
    )
    add(
        "quarterly_report",
        "dashboard_quarterly_report",
        call("dashboard_quarterly_report", year=2026, quarter=1),
    )
    add(
        "deferred_quarterly_report",
        "dashboard_quarterly_report",
        call("dashboard_quarterly_report", year=2026, quarter=1, preparation="deferred"),
    )
    add(
        "period_preparation",
        "dashboard_period_preparation",
        call(
            "dashboard_period_preparation",
            period="2026-01",
            expected_read_version=brief["read_context"]["read_version"],
            as_of=brief["read_context"]["as_of"],
        ),
    )

    bank_path = root / "banks"
    bank_path.mkdir()
    book, _ = public_bank_book(bank_path)
    engine, save, publish, _ = book
    banking.opening(save, publish)
    banking.funding(save, publish)
    banking.statement(save, publish, [banking.entry("row")])
    banking.opening(save, publish, bank="bank-b")
    banking.funding(save, publish, subject="other-funding", bank="bank-b", amount=500)
    banking.statement(
        save,
        publish,
        [banking.entry("other-row", amount=500)],
        subject="other-statement",
        bank="bank-b",
    )
    bank_response = Dashboard(engine).funds("2026-09")
    add("bank_funds", "dashboard_funds", bank_response)
    # Generated entity IDs are random; pick the account outside the first page
    # from the actual ordered response, rather than assuming bank-b sorts last.
    filtered_account = bank_response["data"]["collections"]["accounts"]["items"][-1]["account_id"]
    add(
        "filtered_bank_funds",
        "dashboard_funds",
        Dashboard(engine).funds(
            "2026-09",
            movement_account_type="bank",
            movement_account_id=filtered_account,
            statement_account_id=filtered_account,
            limit=1,
        ),
    )

    closed_path = root / "closed"
    closed_path.mkdir()
    closed_book, _ = public_bank_book(closed_path)
    engine, _, _ = closed_banks(closed_book)
    add("frozen_funds", "dashboard_funds", Dashboard(engine).funds("2026-09"))
    add("first_frozen_account_funds", "dashboard_funds",
        Dashboard(engine).funds("2026-09", movement_account_selection="first"))
    add(
        "frozen_readiness",
        "period_readiness",
        BusinessQueries(engine).period_readiness("2026-09", as_of="2026-10-20"),
    )

    wage_path = root / "wages"
    wage_path.mkdir()
    wage_book = payroll_company(wage_path)
    add("wage_mapping_missing", "dashboard_funds", Dashboard(wage_book.engine).funds("2026-01"))
    replace_contribution_policy(
        wage_book,
        tuple(contribution_rule(code) for code in ("pension", "medical", "unemployment", "injury")),
    )
    add(
        "wage_mapping_unsupported",
        "dashboard_funds",
        Dashboard(wage_book.engine).funds("2026-01"),
    )
    wage_book.close("2026-01")
    wage_book.save(payroll(period="2026-02"), "february")
    received = sorted(
        {proof for month, proof in wage_book.materials["payroll"] if month <= "2026-01"}
    )
    Periods(wage_book.engine).inventory(
        "2026-01",
        "payroll",
        evidence=received,
        expected=len(received) + 1,
        no_business=False,
        confirmation_evidence=wage_book.owner_confirmation,
        request_id=wage_book.request(),
    )
    add(
        "late_closed_missing_material",
        "dashboard_funds",
        Dashboard(wage_book.engine).funds("2026-02"),
    )

    personnel_path = root / "personnel"
    personnel_path.mkdir()
    personnel = correction_company.__wrapped__(personnel_path)
    personnel.save(payroll_profile(withholding_start_date="2026-01"), "profile", revision=1)
    personnel.confirm_payroll("january", "february")
    personnel.publish("january", "february")
    display_profile(personnel.engine, "employee", "employee", employment_start="2025-12")
    display_profile(personnel.engine, "business", "january", display_name="合成工资业务")
    personnel_dashboard = Dashboard(personnel.engine)
    payroll_brief = personnel_dashboard.brief("2026-01", section="open_items")
    add("brief_payroll_open_items", "dashboard_brief", payroll_brief)
    social_group = next(group for group in payroll_brief["data"]["collections"]["open_items"]["items"]
                        if group["description"] == "社保与公积金")
    add("brief_payroll_members", "dashboard_brief_group", personnel_dashboard.brief_group(
        "2026-01", section="open_items", group_key=social_group["group_key"],
    ))
    add("employees_month_dates", "dashboard_employees", personnel_dashboard.employees("2026-01"))
    add(
        "employees_focused", "dashboard_employees",
        personnel_dashboard.employees("2026-01", employee_id="employee", section="employees"),
    )
    personnel.close("2026-01")
    display_profile(
        personnel.engine, "employee", "employee", 1,
        employment_start="2025-11", employment_end="2026-04-20",
    )
    add("employees_mixed_dates", "dashboard_employees", personnel_dashboard.employees("2026-01"))
    add(
        "business_month_dates", "dashboard_business_status",
        personnel_dashboard.business_status("2026-01", "january"),
    )
    display_profile(
        personnel.engine, "employee", "employee", 2,
        employment_start="2025-10", employment_end="2025-11-01",
    )
    add("employees_date_conflict", "dashboard_employees", personnel_dashboard.employees("2026-01"))

    labor_path = root / "labor"
    labor_path.mkdir()
    labor = Company(labor_path / "labor.sqlite")
    labor.save(
        LaborAccrual(
            period="2026-01", person_id="person", expense_class="management",
            gross_fee_fen=500000, tax_treatment="not_withheld_not_filed",
        ),
        "labor",
    )
    labor.publish("labor")
    display_profile(labor.engine, "employee", "person", display_name="合成劳务人员")
    add(
        "employees_labor_sources", "dashboard_employees",
        Dashboard(labor.engine).employees("2026-01", section="labor_sources"),
    )
    from test_workflow import (
        completion_from_basis,
        obligation,
        review_from_completion,
        save_completion,
        setup_company,
    )

    from ai_accounting.kernel.workflow import Workflow

    external_path = root / "external"
    external_path.mkdir()
    external_book = setup_company(external_path)
    external_book.save(obligation("quarterly_tax"), "obligation")
    workflow = Workflow(external_book.engine)
    completion = completion_from_basis(
        workflow.obligation_basis("obligation"), no_reportable_activity_confirmed=True
    )
    saved = save_completion(external_book, completion)
    external_book.publish("completion")
    add("actual_tax_unreviewed", "workflow", workflow.query("2026-01", as_of="2026-02-25"))
    add(
        "external_brief",
        "dashboard_brief",
        Dashboard(external_book.engine).brief("2026-01"),
    )
    add(
        "external_readiness",
        "period_readiness",
        BusinessQueries(external_book.engine).period_readiness("2026-01", as_of="2026-02-25"),
    )
    review = review_from_completion(completion, saved["fact_id"], [], "matched")
    external_book.save(review, "review")
    external_book.publish("review")
    add("actual_tax_reviewed", "workflow", workflow.query("2026-01", as_of="2026-02-25"))
    from test_reimbursement_assets import asset
    from test_reimbursement_assets import book as asset_book

    asset_path = root / "asset-payment-summary"
    asset_path.mkdir()
    asset_engine, asset_save, asset_publish = asset_book.__wrapped__(asset_path)
    asset_save("reimbursed_asset", "computer", asset())
    asset_publish("computer")
    add("asset_payment_summary", "dashboard_assets", Dashboard(asset_engine).assets("2026-02"))
    return samples


def http_samples(samples):
    return {
        name: {
            "command": item["command"],
            "response": http_response(item["command"], item["response"]),
        }
        for name, item in samples.items()
    }


if __name__ == "__main__":
    destination = Path(sys.argv[1])
    with TemporaryDirectory(prefix="dashboard-contract-samples-") as directory:
        samples = http_samples(native_samples(Path(directory)))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(samples, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Generated {len(samples)} live response samples: {destination}")
