"""Owner settlement details preserve each formally adopted recipient and amount."""

from collections import Counter

import pytest
from test_payroll import contribution_policy, income_tax_policy, opening, payroll, profile
from test_payroll_corrections import Company

from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_owner import (
    settlement_event_party_identity,
    settlement_event_view,
    settlement_purpose_label,
)
from ai_accounting.kernel.domains import banking, transactions
from ai_accounting.kernel.entities import Entities

PERIOD = "2026-02"
PEOPLE = ("one", "two", "three", "four")


@pytest.mark.parametrize("kind,name,component,expected", [
    ("payroll", "net", None, "实发工资"),
    ("payroll_bounded", "net", None, "实发工资"),
    ("annual_bonus", "net", None, "实发奖金"),
    ("annual_bonus", "tax", None, "代扣个人所得税"),
    ("labor", "net", None, "实发劳务款"),
    ("labor", "tax", None, "代扣个人所得税"),
    ("labor_accrual", "net", None, "实发劳务款"),
    ("labor_project_cost", "net", None, "实发劳务款"),
    ("labor", "primary", None, "实发劳务款"),
    ("pass_through", "collection", None, "代收款"),
    ("pass_through", "remittance", None, "代付款"),
    ("opening_payroll_payable", "primary", "net", "实发工资"),
    ("opening_payroll_payable", "primary", "withheld_tax", "代扣个人所得税"),
    ("opening_payroll_payable", "primary", "employee_social", "个人社保"),
    ("opening_payroll_payable", "primary", "employee_housing", "个人公积金"),
    ("opening_payroll_payable", "primary", "employer_social", "单位社保"),
    ("opening_payroll_payable", "primary", "employer_housing", "单位公积金"),
])
def test_settlement_purpose_follows_exact_source_kind_and_opening_component(
    kind, name, component, expected,
):
    item = {"source_business": {"kind": kind}, "obligation_name": name}
    if component is not None:
        item["purpose_component"] = component
    assert settlement_purpose_label(item) == expected


@pytest.fixture
def batch_company(tmp_path):
    company = Company(tmp_path / "recipient-details.sqlite")
    company.save(contribution_policy(), "contributions")
    company.save(income_tax_policy(), "income-tax")
    for index, person in enumerate(PEOPLE):
        company.save(profile(employee_id=person, effective_to="2026-01"), "profile-" + person)
        company.save(opening(employee_id=person), "opening-" + person)
        gross = 1_000_000 + index * 10_000
        company.save(payroll(
            employee_id=person, profile_id="profile-" + person,
            accounting_gross_salary_fen=gross, tax_reported_salary_fen=gross,
        ), "wage-" + person)
    company.confirm_payroll(*("wage-" + person for person in PEOPLE))
    company.publish(*("wage-" + person for person in PEOPLE))
    allocations = tuple(transactions.Allocation(
        source_kind="payroll", source_id="wage-" + person, obligation="net",
        recipient_id=person, amount_fen=company.current("wage-" + person).values["net_fen"],
    ) for person in PEOPLE)
    company.save(transactions.Payment(
        period=PERIOD, actual_date="2026-02-10", direction="outflow",
        bank_account_id="bank", counterparty_id=None, payment_method="bank_batch",
        amount_fen=sum(item.amount_fen for item in allocations), allocations=allocations,
    ), "batch")
    company.publish("batch")
    return company


def _names(company, *, same_name=False):
    names = {person: "合成员工" + person for person in PEOPLE}
    if same_name:
        names["one"] = names["two"] = "合成同名员工"
    for person, name in names.items():
        Entities(company.engine).update_entity_profile(
            person, {"display_name": name}, source="合成实名资料",
            expected_revision=1, request_id=company.request(),
        )
    return names


def _events(dashboard, *, view="current", limit=1):
    response = dashboard.business_status(PERIOD, "batch", settlement_view=view, limit=limit)
    items = []
    while True:
        assert response["schema_version"] == 9
        collection = response["data"]["collections"]["settlement_events"]
        assert collection["page"]["total_count"] == 4
        items.extend(collection["items"])
        if not collection["page"]["has_more"]:
            break
        response = dashboard.business_status(
            PERIOD, "batch", settlement_view=view, section="settlement_events", limit=limit,
            cursor=collection["page"]["next_cursor"], expected_version=response["snapshot_version"],
        )
    return items


@pytest.mark.parametrize("view", ["historical", "current"])
@pytest.mark.parametrize("same_name", [False, True])
def test_batch_details_keep_four_exact_recipients_and_amounts_across_pages(
    batch_company, view, same_name,
):
    company = batch_company
    names = _names(company, same_name=same_name)
    dashboard = Dashboard(company.engine)
    before = company.engine.ledger(PERIOD)
    details = _events(dashboard, view=view)
    assert len({item["id"] for item in details}) == 4
    assert Counter(item["party"] for item in details) == Counter(names.values())
    assert {(item["source_subject_id"], item["party"], item["signed_amount_fen"])
            for item in details} == {
        ("wage-" + person, names[person], company.current("wage-" + person).values["net_fen"])
        for person in PEOPLE
    }
    assert all("recipient_id" not in item and "source_kind" not in item for item in details)
    brief = dashboard.brief(PERIOD, preparation="deferred")["data"]
    assert brief["activity_count"] == brief["voucher_count"] == 1
    assert len(brief["collections"]["activity"]["items"]) == 1
    assert brief["collections"]["activity"]["items"][0]["amount_fen"] == sum(
        item["signed_amount_fen"] for item in details
    )
    assert company.engine.ledger(PERIOD) == before


def test_named_recipient_without_a_name_has_an_explicit_placeholder(batch_company):
    company = batch_company
    before = company.engine.ledger(PERIOD)
    details = _events(Dashboard(company.engine), limit=20)
    assert len({item["id"] for item in details}) == 4
    assert {item["party"] for item in details} == {"收款人名称未提供"}
    assert company.engine.ledger(PERIOD) == before


@pytest.mark.parametrize("identity,expected", [
    ({"recipient_id": "actual", "creditor_id": "creditor", "party_key": ["party", "key"]},
     ("actual", "收款人名称未提供")),
    ({"creditor_id": "creditor", "party_key": ["party", "key"]},
     ("creditor", "往来方名称未提供")),
    ({"party_key": ["party", "key"]}, ("key", "往来方名称未提供")),
    ({"party_key": ["statutory_payroll_obligation", "obligation"]}, (None, "")),
    ({}, (None, "")),
])
def test_event_party_uses_explicit_identity_precedence_and_keeps_reversal_sign(identity, expected):
    assert settlement_event_party_identity(identity) == expected
    raw = {
        **identity, "id": "original-event", "settlement_business": {"subject_id": "batch",
        "kind": "payment"}, "source_business": {"subject_id": "wage", "kind": "payroll"},
        "posting_period": "2026-03", "direction": -1, "signed_amount_fen": -907400,
        "relation_state": "resolved", "obligation_name": "net", "mode": "payment",
    }
    projected = settlement_event_view(raw, party="实名员工" if expected[0] else "")
    assert projected["direction"] == -1 and projected["signed_amount_fen"] == -907400
    assert projected["party"] == ("实名员工" if expected[0] else "")
    assert projected["purpose_label"] == "实发工资"
    assert "recipient_id" not in projected and "creditor_id" not in projected


def test_closed_batch_names_and_personal_amounts_use_exact_receipt_after_later_drafts(
    batch_company,
):
    company = batch_company
    names = _names(company)
    company.close("2026-01")
    amount = company.current("batch", "payment").values["amount_fen"]
    company.save(banking.BankOpening(
        period=PERIOD, bank_account_id="bank", opening_fen=0, basis="new_account",
    ), "bank-opening")
    company.save(transactions.Funding(
        period=PERIOD, owner_id="owner", amount_fen=amount, funding_kind="capital",
        actual_date="2026-02-01", bank_account_id="bank",
    ), "funding")
    company.publish("bank-opening", "funding")
    company.save(banking.BankStatement(
        period=PERIOD, bank_account_id="bank", opening_fen=0, closing_fen=0,
        entries=(
            {"reference": "funding", "actual_date": "2026-02-01", "signed_fen": amount},
            {"reference": "batch", "actual_date": "2026-02-10", "signed_fen": -amount},
        ),
    ), "statement")
    company.save(banking.BankReconciliation(
        period=PERIOD, statement_id="statement", bank_account_id="bank",
        matches=(
            {"reference": "funding", "source_kind": "funding", "source_id": "funding"},
            {"reference": "batch", "source_kind": "payment", "source_id": "batch"},
        ),
    ), "reconciliation")
    company.publish("statement", "reconciliation")
    company.close(PERIOD)
    dashboard = Dashboard(company.engine)
    original = _events(dashboard, view="historical")
    ledger = company.engine.ledger(PERIOD)
    raw_before = BusinessQueries(company.engine).business_status("batch", PERIOD)
    exact = {(item["source_calculation_id"], item["recipient_id"], item["signed_amount_fen"])
             for item in raw_before["settlements"]["movements"]}
    company.save(payroll(
        employee_id="one", profile_id="profile-one", accounting_gross_salary_fen=2_000_000,
        tax_reported_salary_fen=2_000_000,
    ), "wage-one", revision=1)
    Entities(company.engine).update_entity_profile(
        "one", {"display_name": "后来更名员工"}, source="合成后续实名资料",
        expected_revision=2, request_id=company.request(),
    )
    later = _events(dashboard, view="historical")
    assert later == original
    assert {item["party"] for item in later} == set(names.values())
    raw_after = BusinessQueries(company.engine).business_status("batch", PERIOD)
    assert {(item["source_calculation_id"], item["recipient_id"], item["signed_amount_fen"])
            for item in raw_after["settlements"]["movements"]} == exact
    assert company.engine.ledger(PERIOD) == ledger
