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
from test_payroll import payroll
from test_payroll_preparation import company as payroll_company
from test_tax_import import contribution_rule, replace_contribution_policy

from ai_accounting.kernel.dashboard import Dashboard
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
    add("company_without_period", "dashboard_context", call("dashboard_context"))
    add("funds_without_period", "dashboard_funds", call("dashboard_funds"))
    references, evidence, _ = replay_sources(call, "contract")
    call(
        "confirm",
        **reviewed_confirmation(
            call, [references["funding"], references["expense"]], "publish:initial:v1"
        ),
    )
    finish_payment(call, references, evidence)
    add("company_with_period", "dashboard_context", call("dashboard_context"))
    brief = call("dashboard_brief", period="2026-01")
    add("brief", "dashboard_brief", brief)
    add(
        "deferred_brief",
        "dashboard_brief",
        call("dashboard_brief", period="2026-01", preparation="deferred"),
    )
    add("cash_funds", "dashboard_funds", call("dashboard_funds", period="2026-01"))
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
