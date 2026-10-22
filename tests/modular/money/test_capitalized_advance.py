"""Asset-purpose advances settle capitalized costs without buying the asset twice."""

from hashlib import sha256
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import PageQuery
from ai_accounting_modular.host.public import create_kernel
from ai_accounting_modular.ledger.public import LedgerQuery
from ai_accounting_modular.money.public import MovementQuery
from ai_accounting_modular.storage.public import CompanySpec, OwnerProfile, create_catalog


def case(tmp_path, cost_kind, purpose="intangible_asset", *, capitalized=True):
    catalog = tmp_path / "catalog.sqlite3"
    create_catalog(catalog, OwnerProfile(str(uuid4()), "合成负责人", "synthetic-hash"))
    kernel = create_kernel(catalog)
    company, database = str(uuid4()), str(uuid4())
    kernel.storage.create_company(
        CompanySpec(
            company,
            database,
            "913100000000000001",
            "合成资本成本公司",
            "2026-01",
            tmp_path / "company",
        )
    )
    ids = {
        name: str(uuid4())
        for name in (
            "party",
            "owner",
            "project",
            "bank",
            "source",
            "cost",
            "advance",
            "paid",
            "offset",
        )
    }
    text = (
        "合成事实：预付80元，已成立的无形资产资本化成本160元，"
        "使用预付款抵其中80元；没有第二次付款。"
    )
    source = {
        "source_id": ids["source"],
        "kind": "owner_statement",
        "text": text,
        "sha256": sha256(text.encode()).hexdigest(),
        "archive_month": "2026-01",
    }
    objects = [
        {
            "object_id": ids["party"],
            "kind": "person" if cost_kind == "labor" else "organization",
            "name": "合成承接方",
            "data": {},
            "expected_seq": 0,
        },
        {
            "object_id": ids["project"],
            "kind": "project",
            "name": "合成外购无形项目",
            "data": {},
            "expected_seq": 0,
        },
        {
            "object_id": ids["owner"],
            "kind": "person",
            "name": "合成出资人",
            "data": {},
            "expected_seq": 0,
        },
        {
            "object_id": ids["bank"],
            "kind": "bank_account",
            "name": "合成公司银行",
            "data": {
                "bank_name": "合成银行",
                "account_number": "synthetic-only",
                "owner": "company",
                "currency": "CNY",
                "opened_period": "2026-01",
                "zero_at_opening": True,
            },
            "expected_seq": 0,
        },
    ]

    def fact(name, kind, data, day=None):
        return {
            "business_id": ids.get(name, str(uuid4())),
            "kind": kind,
            "period": "2026-01",
            "evidence_ids": [ids["source"]],
            "data": data,
            **({"effective_on": day} if day else {}),
        }

    cost = (
        fact(
            "cost",
            "people.remuneration",
            {
                "type": "labor",
                "person_id": ids["party"],
                "gross_fen": "16000",
                "withholding_state": "not_withheld_not_filed",
                "destination": {
                    "type": "project",
                    "project_id": ids["project"],
                    "project_nature": "purchased_intangible",
                }
                if capitalized
                else {"type": "expense", "area": "administration"},
            },
        )
        if cost_kind == "labor"
        else fact(
            "cost",
            "assets.project_cost",
            {
                "project_id": ids["project"],
                "project_nature": "purchased_intangible",
                "supplier_id": ids["party"],
                "amount_fen": "16000",
            },
        )
    )
    advance = fact(
        "advance",
        "operations.advance",
        {
            "side": "supplier",
            "counterparty_id": ids["party"],
            "purpose": purpose,
            "amount_fen": "8000",
        },
    )
    funding = fact(
        "funding",
        "financing.owner_funding",
        {
            "owner_id": ids["owner"],
            "funding_kind": "loan",
            "funds_account_id": ids["bank"],
            "amount_fen": "8000",
        },
        "2026-01-02",
    )
    paid = fact(
        "paid",
        "money.movement",
        {
            "type": "settlement",
            "funds_account_id": ids["bank"],
            "direction": "outflow",
            "amount_fen": "8000",
            "payment_method": "individual",
            "counterparty_id": ids["party"],
            "allocations": [
                {"source": {"business_id": ids["advance"], "slot": "primary"}, "amount_fen": "8000"}
            ],
        },
        "2026-01-03",
    )
    offset = fact(
        "offset",
        "money.transfer",
        {
            "type": "offset",
            "offset_kind": "advance_application",
            "first": {
                "source": {"business_id": ids["advance"], "slot": "advance"},
                "amount_fen": "8000",
            },
            "second": {
                "source": {
                    "business_id": ids["cost"],
                    "slot": "net_pay" if cost_kind == "labor" else "primary",
                },
                "amount_fen": "8000",
            },
        },
    )
    request = {
        "contract": "ai-accounting-modular/1",
        "company_id": company,
        "command_id": str(uuid4()),
        "sources": [source],
        "objects": objects,
        "mutations": [
            {"op": "create", "expected_seq": 0, "fact": value}
            for value in (offset, paid, advance, funding, cost)
        ],
    }
    return kernel, company, ids, request


@pytest.mark.parametrize("cost_kind", ["labor", "project_cost"])
def test_asset_advance_settles_recognized_capitalized_cost_once(tmp_path, cost_kind):
    kernel, company, ids, request = case(tmp_path, cost_kind)
    try:
        result = kernel.application.accounting_apply(request)
        assert result["completion"] == "complete", result
        with kernel.storage.read_unit(company) as read:
            balances = kernel.ledger.resolve(
                [
                    {"business_id": ids["advance"], "slot": "advance"},
                    {
                        "business_id": ids["cost"],
                        "slot": "net_pay" if cost_kind == "labor" else "primary",
                    },
                ],
                "obligation",
                read,
            )
            assert [row["balance"]["remaining_fen"] for row in balances] == ["0", "8000"]
            offset = kernel.ledger.get_results([ids["offset"]], read)[0]
            assert [
                (line["account_code"], line["debit_fen"], line["credit_fen"])
                for line in offset["vouchers"][0]["lines"]
            ] == [
                ("1123", "0", "8000"),
                ("224104" if cost_kind == "labor" else "2202", "8000", "0"),
            ]
            movements = kernel.money.query_movements(
                {
                    "company_id": company,
                    "period_from": "2026-01",
                    "period_to": "2026-01",
                    "view": "current",
                },
                MovementQuery(ids["bank"]),
                PageQuery(),
                read,
            )
            assert movements.filtered_count == 2
            assert sorted(item["movement"]["amount_fen"] for item in movements.items) == [
                "8000",
                "8000",
            ]
            assert kernel.ledger.query_vouchers(LedgerQuery(), read).total == 5
            report = kernel.ledger.financial_snapshot(
                {
                    "company_id": company,
                    "period_from": "2026-01",
                    "period_to": "2026-01",
                    "view": "current",
                },
                read,
            )
            assert report["pre_tax"]["profit_before_tax"]["amount_fen"] == "0"
        assert kernel.application.accounting_apply(request)["accounting"] == "unchanged"
    finally:
        kernel.close()


@pytest.mark.parametrize(
    ("cost_kind", "purpose", "capitalized"),
    [
        ("labor", "fixed_asset", True),
        ("project_cost", "fixed_asset", True),
        ("labor", "intangible_asset", False),
        ("labor", "operating", True),
    ],
)
def test_prepaid_purpose_cannot_be_reclassified_by_the_offset(
    tmp_path, cost_kind, purpose, capitalized
):
    kernel, company, _, request = case(tmp_path, cost_kind, purpose, capitalized=capitalized)
    try:
        result = kernel.application.accounting_apply(request)
        assert result["completion"] == "not_saved", result
        assert result["error"] == "dependency_conflict"
        with kernel.storage.read_unit(company) as read:
            assert read.head == 0
            assert kernel.ledger.query_vouchers(LedgerQuery(), read).total == 0
    finally:
        kernel.close()
