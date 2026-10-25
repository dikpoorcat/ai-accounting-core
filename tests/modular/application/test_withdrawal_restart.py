"""An open-period redo uses original materials without reviving withdrawn books."""

from copy import deepcopy
from hashlib import sha256
from uuid import uuid4

from ai_accounting_modular.contracts.public import LedgerScope, PageQuery
from ai_accounting_modular.ledger.public import AccountBalanceKey, LedgerQuery
from ai_accounting_modular.money.public import StatementQuery, stable_occurrence_id

from ._working_case import case as case
from .conftest import source as evidence


def _fact(kind, source, data, *, actual=None):
    value = {
        "business_id": str(uuid4()),
        "kind": kind,
        "period": "2026-01",
        "evidence_ids": [source],
        "data": data,
    }
    if actual is not None:
        value["effective_on"] = actual
    return value


def _payroll_group(source, employee, owner, bank, rows):
    employment = _fact(
        "people.employment",
        source,
        {
            "employee_id": employee,
            "effective_from": "2026-01",
            "social_insurance_participating": False,
            "housing_fund_participating": False,
            "contribution_shortfall": "reject",
        },
    )
    wage = _fact(
        "people.remuneration",
        source,
        {
            "type": "wage",
            "person_id": employee,
            "gross_fen": "100000",
            "expense_area": "administration",
            "employment_id": employment["business_id"],
        },
    )
    funding = _fact(
        "financing.owner_funding",
        source,
        {
            "owner_id": owner,
            "funding_kind": "capital",
            "amount_fen": "200000",
            "funds_account_id": bank,
        },
        actual="2026-01-01",
    )
    paid = _fact(
        "money.movement",
        source,
        {
            "type": "settlement",
            "funds_account_id": bank,
            "direction": "outflow",
            "amount_fen": "100000",
            "payment_method": "individual",
            "counterparty_id": employee,
            "allocations": [
                {
                    "source": {"business_id": wage["business_id"], "slot": "net_pay"},
                    "amount_fen": "100000",
                }
            ],
        },
        actual="2026-01-28",
    )
    statement = _fact(
        "money.bank_statement",
        source,
        {
            "funds_account_id": bank,
            "opening_fen": "0",
            "closing_fen": "100000",
            "rows": deepcopy(rows),
        },
    )
    reconciliation = _fact(
        "money.reconciliation",
        source,
        {
            "statement_id": statement["business_id"],
            "matches": [
                {
                    "statement_row_ids": [row["row_id"]],
                    "fund_movement_ids": [stable_occurrence_id(fact["business_id"], "funds")],
                }
                for row, fact in zip(rows, (funding, paid), strict=True)
            ],
        },
    )
    return employment, wage, funding, paid, statement, reconciliation


def _seed_payroll(case):
    case.seed(expense=False)
    employee, owner, bank, source = (str(uuid4()) for _ in range(4))
    text = (
        "合成公司一月开户，期初为零。股东1日投入2000元，"
        "员工一月行政工资1000元，不参与社保及公积金，无扣税，28日银行全额支付。"
        "银行原件共两行，期末1000元。"
    )
    rows = [
        {
            "row_id": str(uuid4()),
            "source": {"source_id": source},
            "actual_on": actual,
            "direction": direction,
            "amount_fen": amount,
        }
        for actual, direction, amount in (
            ("2026-01-01", "inflow", "200000"),
            ("2026-01-28", "outflow", "100000"),
        )
    ]
    objects = [
        *(
            {
                "object_id": object_id,
                "kind": "person",
                "name": name,
                "expected_seq": 0,
                "data": {},
            }
            for object_id, name in ((employee, "合成员工"), (owner, "合成股东"))
        ),
        {
            "object_id": bank,
            "kind": "bank_account",
            "name": "合成工资银行",
            "expected_seq": 0,
            "data": {
                "bank_name": "合成银行",
                "account_number": "synthetic-withdrawal-restart",
                "currency": "CNY",
                "owner": "company",
                "opened_period": "2026-01",
                "zero_at_opening": True,
            },
        },
    ]
    group = _payroll_group(source, employee, owner, bank, rows)
    request = case.request(
        *group,
        objects=objects,
        sources=[
            {
                "source_id": source,
                "kind": "owner_statement",
                "text": text,
                "sha256": sha256(text.encode()).hexdigest(),
                "archive_month": "2026-01",
            }
        ],
    )
    assert case.app.accounting_apply(request)["completion"] == "complete"
    return group, (source, employee, owner, bank, rows), request


def _withdraw(case, facts, **fields):
    request = case.request(**fields)
    request["mutations"] = [
        {"op": "withdraw", "expected_seq": 1, "business_id": fact["business_id"]} for fact in facts
    ]
    return case.app.accounting_apply(request)


def test_complete_open_payroll_redo_reuses_materials_and_releases_old_accounting(case):
    original, materials, old_request = _seed_payroll(case)
    source, _, _, bank, rows = materials
    original_ids = {fact["business_id"] for fact in original}
    original_movements = {
        stable_occurrence_id(fact["business_id"], "funds") for fact in original[2:4]
    }
    assert case.check()["ready"]
    assert _withdraw(case, original)["completion"] == "complete"
    for business in case.get(*original_ids)["businesses"]:
        assert business["state"]["state"] == "withdrawn"
        assert "fact" not in business and business["results"] == []
    with case.kernel.storage.read_unit(case.company) as read:
        assert case.kernel.ledger.fund_movements(bank, "2026-01", "2026-01", read) == ()
        assert read.repository("money").groups(original[-1]["business_id"]) == []
        assert case.kernel.records.resolve_sources([source], read)[0]["source_id"] == source

    # A late old create still hits its identity tombstone; redo is a new request.
    rejected = case.app.accounting_apply(old_request)
    assert rejected["completion"] == "not_saved" and rejected["error"] == "conflict"
    redone = _payroll_group(*materials)
    new_ids = {fact["business_id"] for fact in redone}
    assert new_ids.isdisjoint(original_ids)
    request = case.request(*redone)
    result = case.app.accounting_apply(request)
    assert result["completion"] == "complete", result
    assert case.app.accounting_apply(request)["changes"] == "none"
    assert case.check()["ready"]

    with case.kernel.storage.read_unit(case.company) as read:
        movements = case.kernel.ledger.fund_movements(bank, "2026-01", "2026-01", read)
        assert len(movements) == 2
        assert {item.value["movement_id"] for item in movements}.isdisjoint(original_movements)
        assert {item.source["business_id"] for item in movements} == {
            redone[2]["business_id"],
            redone[3]["business_id"],
        }
        bank_balance, unpaid_wage = case.kernel.ledger.account_balances(
            [AccountBalanceKey("1002", bank), AccountBalanceKey("221101")], "2026-01", read
        )
        assert bank_balance.ending == {"state": "known", "amount_fen": "100000"}
        assert unpaid_wage.ending == {"state": "known", "amount_fen": "0"}
        statements = case.kernel.money.query_statement_rows(
            LedgerScope(
                company_id=case.company, period_from="2026-01", period_to="2026-01", view="current"
            ),
            StatementQuery(account_id=bank),
            PageQuery(),
            read,
        )
        assert {row.row_id for row in statements.items} == {row["row_id"] for row in rows}
        assert {row.business_id for row in statements.items} == {redone[-2]["business_id"]}
        assert all(row.match_group_id is not None for row in statements.items)
        vouchers = case.kernel.ledger.query_vouchers(
            LedgerQuery(period_from="2026-01", period_to="2026-01"), read
        ).items
        assert len(vouchers) == 3
        assert (
            case.kernel.ledger.query_vouchers(
                LedgerQuery(business_ids=tuple(original_ids)), read
            ).items
            == ()
        )


def test_withdraw_does_not_break_a_still_active_payment_or_statement_dependency(case):
    original, _, _ = _seed_payroll(case)
    before = case.get(*(fact["business_id"] for fact in original))
    for selected in (original[1], original[-2]):
        result = _withdraw(case, (selected,))
        assert result["completion"] == "not_saved", result
        assert result["error"] == "dependency_conflict", result
        assert case.get(*(fact["business_id"] for fact in original)) == before


def test_withdraw_does_not_silently_remove_a_later_month_payment(case):
    _, materials, _ = _seed_payroll(case)
    bank = materials[3]
    source = evidence("合成补充原件：一月供应商维修1000元，二月3日从公司银行实际全额支付。")
    expense = case.expense()
    expense["evidence_ids"] = [source["source_id"]]
    payment = _fact(
        "money.movement",
        source["source_id"],
        {
            "type": "settlement",
            "funds_account_id": bank,
            "direction": "outflow",
            "amount_fen": "100000",
            "payment_method": "individual",
            "counterparty_id": case.party,
            "allocations": [
                {
                    "source": {"business_id": expense["business_id"], "slot": "primary"},
                    "amount_fen": "100000",
                }
            ],
        },
        actual="2026-02-03",
    )
    payment["period"] = "2026-02"
    saved = case.app.accounting_apply(case.request(expense, payment, sources=[source]))
    assert saved["completion"] == "complete", saved
    before = case.get(expense["business_id"], payment["business_id"])
    failed = _withdraw(case, (expense,))
    assert failed["completion"] == "not_saved" and failed["error"] == "dependency_conflict"
    assert case.get(expense["business_id"], payment["business_id"]) == before


def test_closed_withdrawal_and_redo_still_require_an_open_correction_period(case):
    expense = case.seed()
    checked = case.check()
    assert checked["ready"]
    assert case.close(checked)["state"] == "closed"
    original = case.get(expense["business_id"])
    reason = "合成验收：原费用误记，需要撤回后重新核实正确事实。"
    rejected = _withdraw(case, (expense,), explanation=reason)
    assert rejected["completion"] == "not_saved" and rejected["error"] == "closed_period_required"
    assert case.get(expense["business_id"]) == original
    assert (
        _withdraw(case, (expense,), explanation=reason, correction_period="2026-02")["completion"]
        == "complete"
    )
    withdrawn = case.get(expense["business_id"])["businesses"][0]
    assert withdrawn["results"][0]["target"]["kind"] == "system.withdrawal"
    frozen = original["businesses"][0]["results"][0]
    exact = case.get(
        expense["business_id"],
        result_id=frozen["reference"]["result_id"],
        generation=frozen["reference"]["generation"],
    )["businesses"][0]["results"][0]
    assert exact == frozen
    fresh = case.expense()
    result = case.app.accounting_apply(case.request(fresh))
    assert result["completion"] == "not_saved" and result["error"] == "closed_period_required"
    assert case.get(fresh["business_id"])["businesses"] == []
