"""Pending objects and tax matters reflect published synthetic business facts."""

import pytest
from historical_pass_through_fixture import prior_pass_through_registration
from test_banking import book as book
from test_banking import close_month, inventories
from test_business_domains import surtax_policy, vat_policy
from test_dashboard_provenance import profile
from test_payroll import labor, labor_policy

from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.response_contracts import validate_response

PERIOD = "2026-09"


def _publish_pending_business(book):
    engine, save, publish, _ = book
    profile(engine, "counterparty", "beneficiary", display_name="乙最终收款人")
    for subject, beneficiary in (("entrusted-unknown", None), ("entrusted-named", "beneficiary")):
        with prior_pass_through_registration():
            save(
                "pass_through",
                subject,
                {
                    "period": PERIOD,
                    "payer_id": "payer",
                    "beneficiary_id": beneficiary,
                    "amount_fen": 3000,
                    "rights_and_obligation_confirmed": True,
                },
            )
            publish(subject)
    save(
        "expense",
        "unnamed-supplier",
        {
            "period": PERIOD,
            "counterparty_id": "unnamed",
            "amount_fen": 1200,
            "expense_class": "administration",
            "creditor_kind": "supplier",
        },
    )
    save(
        "income_tax_assessment",
        "enterprise-tax",
        {
            "period": PERIOD,
            "year": 2026,
            "assessment_basis": "confirmed_provision",
            "cumulative_assessed_fen": 700,
        },
    )
    save(
        "vat_policy",
        "vat",
        {
            "period": "2026-01",
            "policy": vat_policy().model_dump(mode="json"),
        },
    )
    save(
        "surtax_policy",
        "surtax",
        {
            "period": "2026-01",
            "policy": surtax_policy().model_dump(mode="json"),
        },
    )
    save(
        "service_sale",
        "sale",
        {
            "period": PERIOD,
            "customer_id": "customer",
            "gross_fen": 101000,
            "vat_policy_id": "vat",
            "exemption_eligible": False,
            "tax_obligation_period": PERIOD,
        },
    )
    save("labor_income_tax_policy", "labor-policy", labor_policy().model_dump(mode="json"))
    save(
        "labor",
        "contractor-fee",
        labor(
            period=PERIOD,
            income_date="2026-09-30",
            person_id="contractor",
        ).model_dump(mode="json"),
    )
    publish("unnamed-supplier", "enterprise-tax", "sale", "contractor-fee")
    save(
        "tax_assessment",
        "sales-tax",
        {
            "period": PERIOD,
            "period_start": "2026-09-01",
            "period_end": "2026-09-30",
            "vat_policy_id": "vat",
            "surtax_policy_id": "surtax",
        },
    )
    publish("sales-tax")
    for identifier, name in (
        ("payer", "甲付款方"),
        ("contractor", "丙劳务人员"),
        ("customer", "丁客户"),
    ):
        profile(engine, "counterparty", identifier, display_name=name)


def _read_pages(dashboard, *, limit):
    response = dashboard.brief(PERIOD, section="open_items", preparation="deferred", limit=limit)
    rows = []
    while True:
        validate_response("dashboard_brief", response)
        collection = response["data"]["collections"]["open_items"]
        rows.extend(collection["items"])
        page = collection["page"]
        if not page["has_more"]:
            assert len(rows) == page["total_count"]
            assert len({row["id"] for row in rows}) == len(rows)
            members = []
            for group in rows:
                cursor = None
                while True:
                    response_detail = dashboard.brief_group(
                        PERIOD,
                        section="open_items",
                        group_key=group["group_key"],
                        limit=limit,
                        cursor=cursor,
                        expected_version=response["snapshot_version"],
                    )
                    validate_response("dashboard_brief_group", response_detail)
                    detail = response_detail["data"]["collections"]["members"]
                    assert detail["page"]["total_count"] == group["member_count"]
                    assert all(
                        member["group_key"] == group["group_key"] for member in detail["items"]
                    )
                    members.extend(detail["items"])
                    if not detail["page"]["has_more"]:
                        break
                    cursor = detail["page"]["next_cursor"]
            assert len(members) == sum(group["member_count"] for group in rows)
            return members
        response = dashboard.brief(
            PERIOD,
            section="open_items",
            preparation="deferred",
            limit=limit,
            cursor=page["next_cursor"],
            expected_version=response["snapshot_version"],
        )


@pytest.mark.parametrize("closed", [False, True])
def test_pending_objects_and_tax_names_match_across_pages_and_month_freeze(book, closed):
    engine, save, publish, proof = book
    _publish_pending_business(book)
    if closed:
        assert (
            close_month(
                inventories(engine, proof, PERIOD, {"transactions", "payroll", "tax"}),
                proof,
                PERIOD,
            )["status"]
            == "closed"
        )

    dashboard = Dashboard(engine)
    ledger = engine.ledger(PERIOD)
    paged = _read_pages(dashboard, limit=1)
    complete = _read_pages(dashboard, limit=100)
    fields = ("id", "party", "description", "outstanding_fen")
    assert [tuple(row[field] for field in fields) for row in paged] == [
        tuple(row[field] for field in fields) for row in complete
    ]
    assert [(row["party"], row["description"]) for row in paged] == sorted(
        (row["party"], row["description"]) for row in paged
    )
    by_id = {row["id"]: row for row in paged}
    expected = {
        "pass_through:entrusted-unknown:collection": ("甲付款方", "代收代付", 3000),
        "pass_through:entrusted-unknown:remittance": ("最终收款人未具名", "代收代付", 3000),
        "pass_through:entrusted-named:collection": ("甲付款方", "代收代付", 3000),
        "pass_through:entrusted-named:remittance": ("乙最终收款人", "代收代付", 3000),
        "expense:unnamed-supplier:primary": ("未提供姓名或名称", "费用", 1200),
        "income_tax_assessment:enterprise-tax:tax": ("税务机关", "企业所得税", 700),
        "tax_assessment:sales-tax:vat": ("税务机关", "增值税", 1000),
        "tax_assessment:sales-tax:surtax": ("税务机关", "附加税", 60),
        "service_sale:sale:primary": ("丁客户", "服务收入", 101000),
        "labor:contractor-fee:net": ("丙劳务人员", "劳务报酬", 840000),
        "labor:contractor-fee:tax": ("丙劳务人员", "代扣个人所得税", 160000),
    }
    assert set(by_id) == set(expected)
    for key, display in expected.items():
        row = by_id[key]
        assert (row["party"], row["description"], row["outstanding_fen"]) == display
    assert engine.ledger(PERIOD) == ledger

    # A later receipt updates present progress without changing the September debt.
    save(
        "payment",
        "later-collection",
        {
            "period": "2026-10",
            "actual_date": "2026-10-02",
            "direction": "inflow",
            "bank_account_id": "bank",
            "counterparty_id": "payer",
            "amount_fen": 1000,
            "allocations": [
                {
                    "source_kind": "pass_through",
                    "source_id": "entrusted-unknown",
                    "obligation": "collection",
                    "amount_fen": 1000,
                }
            ],
        },
    )
    publish("later-collection")
    later = {row["id"]: row for row in _read_pages(Dashboard(engine), limit=1)}
    historical = later["pass_through:entrusted-unknown:collection"]
    assert (historical["party"], historical["outstanding_fen"], historical["status"]) == (
        "甲付款方",
        3000,
        "open",
    )
    assert (historical["current_outstanding_fen"], historical["current_status"]) == (
        2000,
        "partial",
    )
    assert later["pass_through:entrusted-unknown:remittance"]["party"] == "最终收款人未具名"
    assert engine.ledger(PERIOD) == ledger
