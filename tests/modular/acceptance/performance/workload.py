"""Real input facts for docs/06 §10: 50 employees, 5 contractors and 1000 businesses/month.

The mix is synthetic test data: 50 wage earners, 5 labor workers, bank batches,
investments, assets, projects and unpaid expenses. Opening/relationships count
toward the first 1000; objects, vouchers and derived slots never inflate it.
"""

from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID, uuid5

from ai_accounting_modular.contracts.public import CONTRACT, canonical_json

from .._support import COMPANY

SCALES = (12, 48, 120)
EMPLOYEES = 50
CONTRACTORS = 5
PEOPLE = EMPLOYEES + CONTRACTORS
BUSINESSES_PER_MONTH = 1000
START = "2026-01"
BATCH_SIZE = 400
NAMESPACE = UUID("8090b53c-a005-45c7-b992-8c44c1ca7726")
INITIAL_BANK = 100000000000


def identity(name):
    return str(uuid5(NAMESPACE, name))


def month(index):
    return f"{2026 + index // 12:04d}-{index % 12 + 1:02d}"


@dataclass(frozen=True)
class ReconciliationInput:
    business_id: str
    statement_id: str
    account_id: str
    rows: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class MonthInput:
    period: str
    requests: tuple[dict, ...]
    counts: dict[str, int]
    expense_id: str
    ending_bank_fen: int
    reconciliations: tuple[ReconciliationInput, ...]


def _object(name, kind, data=None):
    return {
        "object_id": identity(name),
        "kind": kind,
        "name": "合成" + name,
        "expected_seq": 0,
        "data": data or {},
    }


def plan_month(index, *, business_count=BUSINESSES_PER_MONTH, opening_bank=INITIAL_BANK):
    period, source_id = month(index), identity("source/" + month(index))
    text = (
        f"合成性能验收{period}：工资50人每人100000分、劳务5人每人20000分，"
        "最终税额均有据为0分；工资人员明确不参加社保公积金。"
        "工资49人实际已付，另1人尚未付款；劳务5人实际已付。"
        "其他业务的供应、取得、启用及银行原行以所附确定事实为准。不是现实企业资料。"
    )
    source = {
        "source_id": source_id,
        "kind": "owner_statement",
        "text": text,
        "sha256": sha256(text.encode()).hexdigest(),
        "archive_month": period,
    }
    supplier, bank, bank2, product = (
        identity(name) for name in ("supplier", "bank", "bank2", "product")
    )
    objects, initial, facts, payments = [], [], [], []

    def fact(name, kind, data, actual=None):
        value = {
            "business_id": identity(period + "/" + name),
            "kind": kind,
            "period": period,
            "evidence_ids": [source_id],
            "data": data,
        }
        if actual:
            value["effective_on"] = actual
        return value

    def allocation(item, slot, amount, recipient=None):
        value = {
            "source": {"business_id": item["business_id"], "slot": slot},
            "amount_fen": str(amount),
        }
        if recipient:
            value["recipient_id"] = recipient
        return value

    def payment(name, allocations, *, counterparty=None):
        data = {
            "type": "settlement",
            "funds_account_id": bank,
            "direction": "outflow",
            "payment_method": "individual" if counterparty else "bank_batch",
            "amount_fen": str(sum(int(item["amount_fen"]) for item in allocations)),
            "allocations": allocations,
        }
        if counterparty:
            data["counterparty_id"] = counterparty
        payments.append(fact(name, "money.movement", data, period + "-20"))

    if index == 0:
        objects = [
            _object("supplier", "organization"),
            _object(
                "product", "investment_product", {"category": "money_fund", "issuer_id": supplier}
            ),
        ]
        objects += [
            _object(
                name,
                "bank_account",
                {
                    "bank_name": "合成银行",
                    "account_number": "SYNTHETIC-" + name,
                    "currency": "CNY",
                    "owner": "company",
                },
            )
            for name in ("bank", "bank2")
        ]
        objects += [_object(f"person/{n:02d}", "person") for n in range(PEOPLE)]
        members = [
            {
                "member_id": identity("opening/" + name),
                "evidence_ids": [source_id],
                "data": {
                    "type": "bank",
                    "funds_account_id": identity(name),
                    "balance_fen": str(INITIAL_BANK if name == "bank" else 0),
                },
            }
            for name in ("bank", "bank2")
        ]
        members.append(
            {
                "member_id": identity("opening/equity"),
                "evidence_ids": [source_id],
                "data": {
                    "type": "equity",
                    "equity_kind": "retained_earnings",
                    "holder_or_basis_id": supplier,
                    "balance_fen": str(INITIAL_BANK),
                },
            }
        )
        initial.append(
            fact("opening", "opening.package", {"basis": "existing_ledger", "members": members})
        )
        for n in range(PEOPLE):
            person = identity(f"person/{n:02d}")
            initial.append(
                fact(
                    f"relationship/{n}",
                    "people.relationship",
                    {
                        "person_id": person,
                        "segments": [
                            {
                                "state": "regular" if n < EMPLOYEES else "labor",
                                "effective_from": START,
                            }
                        ],
                    },
                )
            )
            if n < EMPLOYEES:
                initial.append(
                    fact(
                        f"employment/{n}",
                        "people.employment",
                        {
                            "employee_id": person,
                            "effective_from": START,
                            "social_insurance_participating": False,
                            "housing_fund_participating": False,
                            "contribution_shortfall": "reject",
                        },
                    )
                )
    projects = [identity(period + f"/project/{n}") for n in range(5)]
    objects += [
        {
            "object_id": value,
            "kind": "project",
            "name": f"合成{period}项目{n}",
            "expected_seq": 0,
            "data": {"purpose": "合成软件取得成本"},
        }
        for n, value in enumerate(projects)
    ]
    wage_allocations, labor_allocations = [], []
    for n in range(PEOPLE):
        person = identity(f"person/{n:02d}")
        tax = {
            "tax_period": period,
            "tax_fen": "0",
            "basis_type": "final_statement",
            "source": {"source_id": source_id},
        }
        if n < EMPLOYEES:
            data = {
                "type": "wage",
                "person_id": person,
                "gross_fen": "100000",
                "expense_area": "administration",
                "employment_id": identity(START + f"/employment/{n}"),
                "tax_basis": tax,
            }
        else:
            destination = (
                {
                    "type": "project",
                    "project_id": projects[0],
                    "project_nature": "purchased_intangible",
                }
                if n == PEOPLE - 1
                else {"type": "expense", "area": "administration"}
            )
            data = {
                "type": "labor",
                "person_id": person,
                "gross_fen": "20000",
                "destination": destination,
                "withholding_state": "deduction_established",
                "tax_basis": tax,
            }
        item = fact(f"remuneration/{n}", "people.remuneration", data)
        facts.append(item)
        (wage_allocations if n < EMPLOYEES else labor_allocations).append(
            allocation(item, "net_pay", 100000 if n < EMPLOYEES else 20000, person)
        )
    payment("wage-payment", wage_allocations[:-1])
    payment("labor-payment", labor_allocations)
    asset_allocations, enabled = [], []
    for n in range(20):
        asset_id = identity(period + f"/asset/{n}")
        asset_type = "fixed" if n < 10 else "intangible"
        objects.append(
            {
                "object_id": asset_id,
                "kind": "asset",
                "name": f"合成{period}资产{n:02d}",
                "expected_seq": 0,
                "data": {
                    "asset_type": asset_type,
                    "category": "合成设备" if n < 10 else "合成软件",
                },
            }
        )
        item = fact(
            f"acquire/{n}",
            "assets.acquisition",
            {
                "type": "direct",
                "asset": {"asset_id": asset_id, "asset_type": asset_type, "cost_fen": "10001"},
                "supplier_id": supplier,
            },
        )
        facts.append(item)
        asset_allocations.append(allocation(item, "primary", 10001))
        enabled.append(
            {
                "asset_id": asset_id,
                "useful_life_months": 60,
                "residual_fen": "1" if asset_type == "fixed" else "0",
                "benefit_area": "administration",
                "rounding": "floor_final_remainder",
            }
        )
    facts.append(fact("activation", "assets.activation", {"items": enabled}))
    payment("assets-payment", asset_allocations, counterparty=supplier)
    project_allocations = []
    for n, project in enumerate(projects):
        item = fact(
            f"project-cost/{n}",
            "assets.project_cost",
            {
                "project_id": project,
                "project_nature": "purchased_intangible",
                "supplier_id": supplier,
                "amount_fen": "1000",
            },
        )
        facts.append(item)
        project_allocations.append(allocation(item, "primary", 1000))
    payment("projects-payment", project_allocations, counterparty=supplier)
    for n in range(5):
        item = fact(
            f"fund-purchase/{n}",
            "investment.money_fund_purchase",
            {
                "fund_id": product,
                "counterparty_id": supplier,
                "classification": "readily_redeemable_held_not_over_one_year",
                "cost_basis": "documented_cost",
                "purchase_price_fen": "100000",
                "acquisition_fees_fen": "0",
                "excludes_declared_unpaid_distributions": True,
            },
        )
        facts.append(item)
        payment(f"fund-payment/{n}", [allocation(item, "primary", 100000)], counterparty=supplier)
    payments.append(
        fact(
            "transfer",
            "money.transfer",
            {
                "type": "physical",
                "from_account_id": bank,
                "to_account_id": bank2,
                "amount_fen": "1000",
            },
            period + "-20",
        )
    )
    # Reserve one expense payment, two statements and two actual reconciliations.
    # Reconciliation occurrence IDs are captured from public Money after commit.
    filler_count = business_count - len(initial) - len(facts) - len(payments) - 5
    if filler_count < 20:
        raise ValueError("The probe needs 20 expense facts in addition to the fixed mix")
    expenses = [
        fact(
            f"expense/{n}",
            "operations.expense",
            {
                "creditor_id": supplier,
                "creditor_kind": "supplier",
                "amount_fen": "1000",
                "expense_class": "administration",
                "report_details": [{"code": "management_other", "amount_fen": "1000"}],
            },
        )
        for n in range(filler_count)
    ]
    facts.extend(expenses)
    payment(
        "expense-payment",
        [allocation(item, "primary", 1000) for item in expenses[:20]],
        counterparty=supplier,
    )
    rows = [
        {
            "row_id": identity(item["business_id"] + "/bank-row"),
            "source": {"source_id": source_id, "location": "合成银行原行"},
            "actual_on": period + "-20",
            "direction": "outflow",
            "amount_fen": item["data"]["amount_fen"],
            "memo": item["kind"] + "合成实际用途",
            "counterparty_text": "合成实际收款方",
        }
        for item in payments
    ]
    closing = opening_bank - sum(int(row["amount_fen"]) for row in rows)
    payments.append(
        fact(
            "statement",
            "money.bank_statement",
            {
                "funds_account_id": bank,
                "opening_fen": str(opening_bank),
                "closing_fen": str(closing),
                "rows": rows,
            },
        )
    )
    payments.append(
        fact(
            "statement2",
            "money.bank_statement",
            {
                "funds_account_id": bank2,
                "opening_fen": str(index * 1000),
                "closing_fen": str((index + 1) * 1000),
                "rows": [
                    {
                        "row_id": identity(period + "/transfer-in"),
                        "source": {"source_id": source_id, "location": "合成转入原行"},
                        "actual_on": period + "-20",
                        "direction": "inflow",
                        "amount_fen": "1000",
                        "memo": "公司账户内部转入",
                    }
                ],
            },
        )
    )
    reconciliations = (
        ReconciliationInput(
            identity(period + "/reconcile"),
            identity(period + "/statement"),
            bank,
            tuple(
                (row["row_id"], item["business_id"])
                for row, item in zip(rows, payments[:-2], strict=True)
            ),
        ),
        ReconciliationInput(
            identity(period + "/reconcile2"),
            identity(period + "/statement2"),
            bank2,
            ((identity(period + "/transfer-in"), identity(period + "/transfer")),),
        ),
    )
    all_facts = initial + facts + payments
    assert len(all_facts) == len({item["business_id"] for item in all_facts}) == business_count - 2
    requests = []
    for stage, items in (("facts", initial + facts), ("payments", payments)):
        for offset in range(0, len(items), BATCH_SIZE):
            request = {
                "contract": CONTRACT,
                "company_id": COMPANY,
                "command_id": identity(period + f"/{stage}/{offset}"),
                "mutations": [
                    {"op": "create", "expected_seq": 0, "fact": item}
                    for item in items[offset : offset + BATCH_SIZE]
                ],
            }
            if not requests:
                request.update(objects=objects, sources=[source])
            assert len(canonical_json(request).encode()) < 8 * 1024 * 1024
            requests.append(request)
    return MonthInput(
        period,
        tuple(requests),
        dict(Counter(item["kind"] for item in all_facts)) | {"money.reconciliation": 2},
        expenses[0]["business_id"],
        closing,
        reconciliations,
    )
