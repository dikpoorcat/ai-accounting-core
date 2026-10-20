"""Cooperation remains ordinary operating facts joined by public result slots."""

import json
from copy import deepcopy
from hashlib import sha256
from uuid import UUID, uuid4

import pytest

from ai_accounting_modular.host.public import create_kernel
from ai_accounting_modular.storage.public import CompanySpec, OwnerProfile, create_catalog

PROJECT = str(UUID(int=701))
CASH, COUNTERPARTY, OWNER, SOURCE = (str(UUID(int=index)) for index in range(702, 706))


def fact(kind, data, *, period="2026-01", actual=None, business_id=None):
    value = {
        "business_id": business_id or str(uuid4()),
        "kind": kind,
        "period": period,
        "evidence_ids": [SOURCE],
        "data": data,
    }
    if actual is not None:
        value["effective_on"] = actual
    return value


@pytest.fixture
def system(tmp_path):
    catalog = tmp_path / "catalog.sqlite3"
    create_catalog(catalog, OwnerProfile(str(uuid4()), "合成负责人", "synthetic-only-hash"))
    kernel = create_kernel(catalog)
    company = str(uuid4())
    kernel.storage.create_company(
        CompanySpec(
            company,
            str(uuid4()),
            "913100000000000001",
            "合成合作经营公司",
            "2026-01",
            tmp_path / "company",
        )
    )
    yield kernel, company
    kernel.close()


def agreement(share="0.5"):
    return fact(
        "investment.cooperation_contract",
        {
            "profile": "direct_joint_operation_cash_labor_v1",
            "project_id": PROJECT,
            "operator_id": OWNER,
            "company_initial_commitment_fen": "1000",
            "company_revenue_expense_share": share,
            "contract_source": {"source_id": SOURCE, "location": "合成按份合同"},
        },
    )


def apply(system, *facts, initial=False, mutations=None, **extra):
    kernel, company = system
    command = {
        "contract": "ai-accounting-modular/1",
        "company_id": company,
        "command_id": str(uuid4()),
        "mutations": [{"op": "create", "expected_seq": 0, "fact": item} for item in facts],
    }
    if initial:
        command["objects"] = [
            {
                "object_id": object_id,
                "kind": kind,
                "name": "合成合作对象",
                "expected_seq": 0,
                "data": data,
            }
            for object_id, kind, data in (
                (PROJECT, "investment_product", {"category": "cooperation"}),
                (CASH, "cash_account", {"currency": "CNY"}),
                (COUNTERPARTY, "organization", {}),
                (OWNER, "person", {}),
            )
        ]
        text = "合成项目合同、履约与真实收付依据。"
        command["sources"] = [
            {
                "source_id": SOURCE,
                "kind": "owner_statement",
                "text": text,
                "sha256": sha256(text.encode()).hexdigest(),
                "archive_month": "2026-01",
            }
        ]
    if mutations is not None:
        command["mutations"] = mutations
    command.update(extra)
    return kernel.application.accounting_apply(command)


def seed(system, contract, *facts, funding_day="2026-01-02"):
    opening = fact("opening.package", {"basis": "newly_established", "members": []})
    owner_cash = fact(
        "financing.owner_funding",
        {
            "owner_id": OWNER,
            "funding_kind": "loan",
            "amount_fen": "1000",
            "funds_account_id": CASH,
        },
        actual="2026-01-01",
    )
    funding = fact(
        "investment.cooperation_funding",
        {
            "contract_id": contract["business_id"],
            "purpose": "initial",
            "amount_fen": "500",
            "funds_account_id": CASH,
            "actual_on": funding_day,
        },
    )
    result = apply(system, *facts, funding, owner_cash, contract, opening, initial=True)
    assert result["completion"] == "complete", result
    return result


def expense(contract, project="200", company="100"):
    return fact(
        "operations.expense",
        {
            "creditor_id": COUNTERPARTY,
            "creditor_kind": "supplier",
            "amount_fen": company,
            "expense_class": "service",
            "cooperation_id": contract["business_id"],
            "project_total_fen": project,
        },
    )


def agent(contract, source, project, *, slot="primary", inflow=False, day="2026-01-10"):
    return fact(
        "investment.cooperation_collection" if inflow else "investment.cooperation_payment",
        {
            "contract_id": contract["business_id"],
            "source": {"business_id": source["business_id"], "slot": slot},
            "project_amount_fen": project,
        },
        period=day[:7],
        actual=day,
    )


def saved(system, item):
    kernel, company = system
    business = kernel.application.accounting_get(
        {
            "company_id": company,
            "business_ids": [item["business_id"]],
            "include": ["facts", "results", "sources"],
        }
    )["businesses"][0]
    return next(
        result for result in business["results"] if result["target"]["result_slot"] == "main"
    )


def quantity(result):
    return next(
        item
        for item in result["basis"]["adopted_quantities"]
        if item["type"] == "cooperation_trade"
    )


def states(system, *refs):
    kernel, company = system
    with kernel.storage.read_unit(company) as read:
        return tuple(
            item["balance"] for item in kernel.ledger.resolve(list(refs), "obligation", read)
        )


@pytest.mark.parametrize("closed", [False, True])
def test_share_correction_preserves_actual_agency_and_recovers_real_project_excess(system, closed):
    contract = agreement()
    charged = expense(contract)
    paid = agent(contract, charged, "200")
    seed(system, contract, paid, charged)
    before_paid = saved(system, paid)
    kernel, company = system
    if closed:
        checked = kernel.application.accounting_period(
            {
                "company_id": company,
                "action": "check",
                "period": "2026-01",
            }
        )
        assert checked["ready"], json.dumps(checked, ensure_ascii=False)
        closure = kernel.application.accounting_period(
            {
                "company_id": company,
                "action": "close",
                "period": "2026-01",
                "expected_head": checked["head"],
            }
        )
        assert closure["state"] == "closed", closure
    month = "2026-02" if closed else "2026-01"
    corrected = deepcopy(charged)
    corrected["data"].update(amount_fen="80", project_total_fen="160")
    recovery = fact(
        "operations.overpayment",
        {
            "source": {"business_id": charged["business_id"], "slot": "primary"},
            "amount_fen": "20",
            "project_total_fen": "40",
        },
        period=month,
        actual=month + "-11",
    )
    result = apply(
        system,
        mutations=[
            {"op": "create", "expected_seq": 0, "fact": recovery},
            {"op": "replace", "expected_seq": 1, "fact": corrected},
        ],
        **({"correction_period": month} if closed else {}),
    )
    assert result["completion"] == "complete", json.dumps(result, ensure_ascii=False)
    after_paid = saved(system, paid)
    assert before_paid["target"] == after_paid["target"]
    assert after_paid["target"]["values"]["company_amount_fen"] == "100"
    q = quantity(saved(system, recovery))
    assert (q["project_amount_fen"], q["company_net_amount_fen"], q["project_range_from_fen"]) == (
        "-40",
        "-20",
        "160",
    )
    returned = agent(contract, recovery, "40", slot="recovery", inflow=True, day=month + "-12")
    result = apply(system, returned)
    assert result["completion"] == "complete", result
    assert saved(system, returned)["target"]["values"]["company_amount_fen"] == "20"
    source, claim = states(
        system,
        {"business_id": charged["business_id"], "slot": "primary"},
        {"business_id": recovery["business_id"], "slot": "recovery"},
    )
    assert source["agency_settled_fen"] == "100" and source["money_settled_fen"] == "0"
    assert source["remaining_fen"] == claim["remaining_fen"] == "0"
    with kernel.storage.read_unit(company) as read:
        movements = kernel.ledger.fund_movements(CASH, "2026-01", month, read)
        assert len(movements) == 2
    if closed:
        after_expense = saved(system, charged)
        corrections = [
            voucher for voucher in after_expense["vouchers"] if voucher["period"] == "2026-02"
        ]
        assert corrections


def vat_policy():
    return fact(
        "taxes.vat_policy",
        {
            "policy": {
                "version": "synthetic-vat",
                "official_url": "https://example.gov.cn/synthetic",
                "effective_from": "2026-01-01",
                "effective_to": "2026-12-31",
            },
            "nominal_rate": "0.03",
            "tax_base_rate": "0.03",
            "payable_rate": "0.03",
            "form_version": "small_taxpayer_2026",
            "threshold_fen": "0",
            "threshold_period": "month",
            "threshold_operator": "at_or_below",
            "exemption_scope": "eligible_non_excluded_sales",
            "special_invoice_may_waive_exemption": True,
            "special_invoice_may_waive_rate_reduction": True,
        },
    )


def sale(contract, policy, *, gross="103", project="200"):
    return fact(
        "operations.service_sale",
        {
            "customer_id": COUNTERPARTY,
            "gross_fen": gross,
            "vat": {
                "policy_id": policy["business_id"],
                "exemption_eligible": False,
                "invoice_kind": "ordinary",
                "waive_exemption": False,
                "waive_rate_reduction": False,
            },
            "tax_point": {
                "type": "explicit",
                "tax_period": "2026-01",
                "source": {"source_id": SOURCE},
            },
            "cooperation_id": contract["business_id"],
            "project_total_fen": project,
        },
    )


def movement(source, net, tax, *, refund=False, day="2026-01-15"):
    return fact(
        "money.movement",
        {
            "type": "settlement",
            "funds_account_id": CASH,
            "direction": "outflow" if refund else "inflow",
            "amount_fen": str(net + tax),
            "payment_method": "individual",
            "counterparty_id": COUNTERPARTY,
            "allocations": [
                {
                    "source": {"business_id": source["business_id"], "slot": slot},
                    "amount_fen": str(value),
                }
                for slot, value in (
                    ("refund" if refund else "primary", net),
                    ("vat_refund" if refund else "vat", tax),
                )
                if value
            ],
        },
        period=day[:7],
        actual=day,
    )


def test_vat_and_net_trade_resources_clear_in_one_actual_payment_and_one_refund(system):
    contract, policy = agreement(), vat_policy()
    sold = sale(contract, policy)
    collected = movement(sold, 100, 3)
    seed(system, contract, collected, sold, policy)
    original = saved(system, sold)
    assert quantity(original)["company_net_amount_fen"] == "100"
    primary, vat = states(
        system,
        {"business_id": sold["business_id"], "slot": "primary"},
        {"business_id": sold["business_id"], "slot": "vat"},
    )
    assert primary["amount_fen"] == "100" and vat["amount_fen"] == "3"
    assert primary["remaining_fen"] == vat["remaining_fen"] == "0"
    returned = fact(
        "operations.sale_return",
        {
            "sale_id": sold["business_id"],
            "customer_id": COUNTERPARTY,
            "gross_fen": "103",
            "credit_note_vat_fen": "3",
            "project_total_fen": "200",
        },
        period="2026-02",
    )
    refunded = movement(returned, 100, 3, refund=True, day="2026-02-10")
    result = apply(system, refunded, returned)
    assert result["completion"] == "complete", result
    q = quantity(saved(system, returned))
    assert q["project_amount_fen"] == "-200" and q["company_net_amount_fen"] == "-100"
    assert [
        item["remaining_fen"]
        for item in states(
            system,
            {"business_id": returned["business_id"], "slot": "refund"},
            {"business_id": returned["business_id"], "slot": "vat_refund"},
        )
    ] == ["0", "0"]


@pytest.mark.parametrize("reverse", [False, True])
def test_split_project_returns_use_one_cumulative_share_instead_of_rounding_twice(system, reverse):
    contract, policy = agreement(), vat_policy()
    sold = sale(contract, policy, gross="1", project="2")
    first, second = [
        fact(
            "operations.sale_return",
            {
                "sale_id": sold["business_id"],
                "customer_id": COUNTERPARTY,
                "gross_fen": gross,
                "credit_note_vat_fen": "0",
                "project_total_fen": "1",
            },
            period="2026-02",
            business_id=str(UUID(int=index)),
        )
        for index, gross in ((8001, "1"), (8002, "0"))
    ]
    facts = [sold, first, second, policy]
    if reverse:
        facts.reverse()
    seed(system, contract, *facts)
    a, b = quantity(saved(system, first)), quantity(saved(system, second))
    assert (a["company_net_amount_fen"], b["company_net_amount_fen"]) == ("-1", "0")
    assert (a["project_range_from_fen"], b["project_range_from_fen"]) == ("0", "1")
    assert saved(system, second)["vouchers"] == []


def test_zero_company_share_keeps_project_fact_and_zero_identity_without_fake_tax_or_cash(system):
    contract, policy = agreement("0.1"), vat_policy()
    sold = sale(contract, policy, gross="0", project="1")
    received = agent(contract, sold, "1", inflow=True)
    seed(system, contract, sold, received, policy)
    current = saved(system, sold)
    assert current["target"]["values"]["gross_fen"] == "0"
    assert quantity(current)["project_amount_fen"] == "1"
    assert current["vouchers"] == saved(system, received)["vouchers"] == []
    (net,) = states(system, {"business_id": sold["business_id"], "slot": "primary"})
    assert net["amount_fen"] == net["agency_settled_fen"] == "0"


def test_wrong_company_share_is_not_silently_replaced_with_zero(system):
    contract, policy = agreement("0.1"), vat_policy()
    sold = sale(contract, policy, gross="1", project="1")
    result = apply(system, sold, policy, contract, initial=True)
    assert result["completion"] == "not_saved" and result["error"] == "invalid_input", result
    assert any(item["field"] == "/data/gross_fen" for item in result["fact_issues"])


@pytest.mark.parametrize("revenue", [False, True])
def test_excess_on_a_negative_trade_reverses_cash_direction_without_second_recognition(
    system, revenue
):
    contract, policy = agreement(), vat_policy()
    original = sale(contract, policy) if revenue else expense(contract)
    original_agent = agent(contract, original, "200", inflow=revenue, day="2026-01-05")
    returned = fact(
        "operations.sale_return" if revenue else "operations.expense_recovery",
        {
            "sale_id" if revenue else "expense_id": original["business_id"],
            "customer_id" if revenue else "counterparty_id": COUNTERPARTY,
            "gross_fen" if revenue else "amount_fen": "20",
            "project_total_fen": "40",
            **({"credit_note_vat_fen": "0"} if revenue else {}),
        },
    )
    refunded = agent(
        contract,
        returned,
        "40",
        slot="refund" if revenue else "primary",
        inflow=not revenue,
        day="2026-01-06",
    )
    seed(
        system,
        contract,
        *([policy] if revenue else []),
        refunded,
        returned,
        original_agent,
        original,
    )
    corrected = deepcopy(returned)
    corrected["data"].update(project_total_fen="20")
    corrected["data"]["gross_fen" if revenue else "amount_fen"] = "10"
    excess = fact(
        "operations.overpayment",
        {
            "source": {
                "business_id": returned["business_id"],
                "slot": "refund" if revenue else "primary",
            },
            "amount_fen": "10",
            "project_total_fen": "20",
        },
        actual="2026-01-07",
    )
    real_correction = agent(
        contract, excess, "20", slot="recovery", inflow=revenue, day="2026-01-08"
    )
    result = apply(
        system,
        mutations=[
            {"op": "create", "expected_seq": 0, "fact": real_correction},
            {"op": "replace", "expected_seq": 1, "fact": corrected},
            {"op": "create", "expected_seq": 0, "fact": excess},
        ],
    )
    assert result["completion"] == "complete", json.dumps(result, ensure_ascii=False)
    q = quantity(saved(system, excess))
    assert q["project_amount_fen"] == "20" and q["company_net_amount_fen"] == "10"
    assert q["project_range_from_fen"] == "20"
    business = system[0].application.accounting_get(
        {
            "company_id": system[1],
            "business_ids": [contract["business_id"]],
            "include": ["results"],
        }
    )["businesses"][0]
    position = next(
        item["target"]["values"]
        for item in business["results"]
        if item["target"]["result_slot"] == "position:2026-01"
    )
    field = "revenue" if revenue else "expense"
    assert position[f"project_{field}_fen"] == "180"
    assert position[f"company_{field}_fen"] == "90"


@pytest.mark.parametrize(
    "claim, project, expected",
    [
        ("20", "40", "needs_information"),
        ("120", "240", "dependency_conflict"),
    ],
)
def test_unknown_agency_day_is_requested_only_if_it_can_establish_the_claim(
    system, claim, project, expected
):
    contract = agreement()
    charged = expense(contract)
    paid = agent(contract, charged, "200")
    paid.pop("effective_on")
    # The unknown day must affect the claim, not whether project cash existed.
    seed(system, contract, paid, charged, funding_day="2026-01-01")
    corrected = deepcopy(charged)
    corrected["data"].update(project_total_fen="160", amount_fen="80")
    recovery = fact(
        "operations.overpayment",
        {
            "source": {"business_id": charged["business_id"], "slot": "primary"},
            "amount_fen": claim,
            "project_total_fen": project,
        },
        actual="2026-01-11",
    )
    result = apply(
        system,
        mutations=[
            {"op": "replace", "expected_seq": 1, "fact": corrected},
            {"op": "create", "expected_seq": 0, "fact": recovery},
        ],
    )
    assert result["completion"] == "not_saved" and result["error"] == expected, result
    if expected == "needs_information":
        assert any(
            item["business_id"] == paid["business_id"] and item["field"] == "/effective_on"
            for item in result["fact_issues"]
        )


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("closed", [False, True])
def test_zero_recovery_inherits_no_empty_classification_and_keeps_actual_project_receipt(
    system, explicit, closed
):
    contract = agreement()
    charged = expense(contract, project="2", company="1")
    paid = agent(contract, charged, "2", day="2026-01-06")
    first, second = [
        fact(
            "operations.expense_recovery",
            {
                "expense_id": charged["business_id"],
                "counterparty_id": COUNTERPARTY,
                "amount_fen": company,
                "project_total_fen": "1",
            },
            business_id=str(UUID(int=identifier)),
            actual=day,
        )
        for identifier, company, day in ((8101, "1", "2026-01-07"), (8102, "0", "2026-01-08"))
    ]
    received = agent(contract, second, "1", inflow=True, day="2026-01-09")
    if explicit:
        received["data"]["company_amount_fen"] = "0"
    seed(system, contract, received, second, first, paid, charged)
    before_receipt = saved(system, received)
    assert before_receipt["target"]["values"]["company_amount_fen"] == "0"
    (zero,) = states(system, {"business_id": second["business_id"], "slot": "primary"})
    assert zero["amount_fen"] == zero["remaining_fen"] == "0"
    kernel, company = system
    with kernel.storage.read_unit(company) as read:
        (view,) = kernel.ledger.resolve(
            [{"business_id": second["business_id"], "slot": "primary"}], "obligation", read
        )
        assert view["obligation"]["cashflow_weights"] == [
            {"category": "materials_services_payments", "weight": "1"}
        ]
    if closed:
        checked = kernel.application.accounting_period(
            {
                "company_id": company,
                "action": "check",
                "period": "2026-01",
            }
        )
        assert checked["ready"], json.dumps(checked, ensure_ascii=False)
        assert (
            kernel.application.accounting_period(
                {
                    "company_id": company,
                    "action": "close",
                    "period": "2026-01",
                    "expected_head": checked["head"],
                }
            )["state"]
            == "closed"
        )
    altered_first, altered_second = deepcopy(first), deepcopy(second)
    altered_first["data"].update(project_total_fen="0", amount_fen="0")
    altered_second["data"]["amount_fen"] = "1"
    result = apply(
        system,
        mutations=[
            {"op": "replace", "expected_seq": 1, "fact": altered_second},
            {"op": "replace", "expected_seq": 1, "fact": altered_first},
        ],
        **({"correction_period": "2026-02"} if closed else {}),
    )
    assert result["completion"] == "complete", json.dumps(result, ensure_ascii=False)
    current = saved(system, received)
    assert current["target"]["values"]["project_amount_fen"] == "1"
    assert current["target"]["values"]["company_amount_fen"] == ("0" if explicit else "1")
    assert current["target"]["values"]["actual_on"] == "2026-01-09"
    original_fact = kernel.application.accounting_get(
        {
            "company_id": company,
            "business_ids": [received["business_id"]],
            "include": ["facts"],
        }
    )["businesses"][0]["fact"]
    assert original_fact == received
    (remaining,) = states(system, {"business_id": second["business_id"], "slot": "primary"})
    assert remaining["remaining_fen"] == ("1" if explicit else "0")
    if closed and not explicit:
        assert current["vouchers"] and {voucher["period"] for voucher in current["vouchers"]} == {
            "2026-02",
        }
        with kernel.storage.read_unit(company) as read:
            reference = before_receipt["reference"]
            frozen = kernel.ledger.get_result(reference["result_id"], reference["generation"], read)
            assert frozen["target"] == before_receipt["target"]
            assert frozen["vouchers"] == []


def test_true_project_refund_of_zero_company_cost_keeps_no_zero_expense_quantity(system):
    contract = agreement("0.1")
    charged = expense(contract, project="1", company="0")
    paid = agent(contract, charged, "1", day="2026-01-06")
    recovery = fact(
        "operations.expense_recovery",
        {
            "expense_id": charged["business_id"],
            "counterparty_id": COUNTERPARTY,
            "amount_fen": "0",
            "project_total_fen": "1",
        },
        actual="2026-01-07",
    )
    received = agent(contract, recovery, "1", inflow=True, day="2026-01-08")
    seed(system, contract, received, recovery, paid, charged)
    current = saved(system, recovery)
    assert quantity(current)["project_amount_fen"] == "-1"
    assert quantity(current)["company_net_amount_fen"] == "0"
    assert current["vouchers"] == []
    assert not any(
        item["type"] in {"expense", "cumulative_range"}
        for item in current["basis"]["adopted_quantities"]
    )


def test_zero_company_project_excess_preserves_known_cashflow_nature_without_cash(system):
    contract = agreement("0.1")
    charged = expense(contract, project="1", company="0")
    paid = agent(contract, charged, "1", day="2026-01-06")
    seed(system, contract, paid, charged)
    corrected = deepcopy(charged)
    corrected["data"]["project_total_fen"] = "0"
    claim = fact(
        "operations.overpayment",
        {
            "source": {"business_id": charged["business_id"], "slot": "primary"},
            "amount_fen": "0",
            "project_total_fen": "1",
        },
        actual="2026-01-07",
    )
    refunded = agent(contract, claim, "1", slot="recovery", inflow=True, day="2026-01-08")
    result = apply(
        system,
        mutations=[
            {"op": "create", "expected_seq": 0, "fact": refunded},
            {"op": "create", "expected_seq": 0, "fact": claim},
            {"op": "replace", "expected_seq": 1, "fact": corrected},
        ],
    )
    assert result["completion"] == "complete", result
    assert saved(system, claim)["vouchers"] == saved(system, refunded)["vouchers"] == []
    kernel, company = system
    with kernel.storage.read_unit(company) as read:
        (view,) = kernel.ledger.resolve(
            [{"business_id": claim["business_id"], "slot": "recovery"}], "obligation", read
        )
    assert view["obligation"]["cashflow_weights"] == [
        {"category": "materials_services_payments", "weight": "1"}
    ]
    assert view["balance"]["amount_fen"] == view["balance"]["agency_settled_fen"] == "0"
