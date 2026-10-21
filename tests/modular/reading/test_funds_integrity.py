"""Funds adapters preserve original row identity and actual payment components."""

from uuid import uuid4

from ai_accounting_modular.money.public import stable_occurrence_id
from tests.modular.application._working_case import case as case
from tests.modular.domains.financing._support import BANK, CASH, fact, objects
from tests.modular.money.test_payroll import inputs as payroll_inputs

from .test_funds import apply, capture, check_data, funding, initialize, page


def original_row(case, identity, value, day, direction):
    return {
        "row_id": identity,
        "source": {"source_id": case.source_id, "location": "合成原行"},
        "actual_on": day,
        "direction": direction,
        "amount_fen": value,
    }


def pair(account, rows, movements):
    closing = sum(
        int(row["amount_fen"]) * (1 if row["direction"] == "inflow" else -1) for row in rows
    )
    statement = fact(
        "money.bank_statement",
        {
            "funds_account_id": account,
            "opening_fen": "0",
            "closing_fen": str(closing),
            "rows": rows,
        },
    )
    check = fact(
        "money.reconciliation",
        {
            "statement_id": statement["business_id"],
            "matches": [
                {
                    "statement_row_ids": [row["row_id"]],
                    "fund_movement_ids": [stable_occurrence_id(movement["business_id"], "funds")],
                }
                for row, movement in zip(rows, movements, strict=True)
            ],
        },
    )
    return statement, check


def test_two_accounts_keep_exact_original_rows_and_independent_bank_filter(case):
    initialize(case)
    other_id = str(uuid4())
    account = next(obj for obj in objects() if obj["object_id"] == BANK) | {
        "object_id": other_id,
        "name": "第二个合成银行",
    }
    account["data"].update(opened_period="2026-01", zero_at_opening=True)
    first, second = funding("100"), funding("200")
    second["data"]["funds_account_id"] = other_id
    first_row, second_row = str(uuid4()), str(uuid4())
    apply(
        case,
        first,
        second,
        *pair(BANK, [original_row(case, first_row, "100", "2026-01-02", "inflow")], [first]),
        *pair(other_id, [original_row(case, second_row, "200", "2026-01-02", "inflow")], [second]),
        objects=[account],
    )
    capture(case)
    data = page(case)["data"]
    check_data(data)
    rows = data["collections"]["statements"]["items"]
    assert len(rows) == len({row["id"] for row in rows}) == 2
    assert {row["id"] for row in rows} == {first_row, second_row}
    assert {row["amount_fen"] for row in rows} == {"100", "200"}
    assert data["bank_statement"]["inflow_fen"] == "300"
    selected = page(case, section="statements", statement_account_id=other_id)["data"][
        "collections"
    ]["statements"]
    assert selected["page"]["total_count"] == 2
    assert selected["page"]["filtered_count"] == 1
    assert selected["items"][0]["amount_fen"] == "200"
    assert all(row["memo"] == row["party"] == "" for row in rows)


def test_first_account_uses_first_formal_month_not_identity_id_or_current_activity(case):
    initialize(case)
    cash = funding("100", "2026-01-04")
    cash["data"]["funds_account_id"] = CASH
    apply(case, cash, funding("200", "2026-02-02"))
    capture(case)
    first = page(case, movement_account_selection="first")["data"]
    assert first["selected_movement_account"] == {"type": "cash", "account_id": CASH}
    assert first["collections"]["accounts"]["items"][0]["account_id"] == CASH
    assert first["collections"]["movements"]["items"][0]["amount_fen"] == "100"
    capture(case, "2026-02")
    february = page(case, "2026-02", movement_account_selection="first")["data"]
    assert february["selected_movement_account"]["account_id"] == CASH
    assert february["collections"]["movements"]["items"] == []
    assert february["total_fen"] == "300"


def test_payroll_batch_keeps_net_wage_and_proven_reserve_inside_one_real_bank_payment(case):
    case.seed(expense=False)
    call, paid, wage = payroll_inputs(case.company)
    next(obj for obj in call["objects"] if obj["kind"] == "bank_account")["data"].update(
        opened_period="2026-01", zero_at_opening=True
    )
    received = funding("1000000")
    received["data"].update(
        owner_id=wage["data"]["person_id"], funds_account_id=paid["data"]["funds_account_id"]
    )
    received["evidence_ids"] = [case.source_id]
    call["mutations"].append({"op": "create", "expected_seq": 0, "fact": received})
    actual = case.app.accounting_apply(call)
    assert actual["completion"] == "complete", actual
    account = paid["data"]["funds_account_id"]
    rows = [
        original_row(case, str(uuid4()), "1000000", received["effective_on"], "inflow"),
        original_row(case, str(uuid4()), "1000000", paid["effective_on"], "outflow"),
    ]
    apply(case, *pair(account, rows, [received, paid]))
    capture(case)
    data = page(case)["data"]
    check_data(data)
    assert (data["inflow_fen"], data["outflow_fen"], data["movement_count"]) == (
        "1000000",
        "1000000",
        2,
    )
    row = next(
        row for row in data["collections"]["statements"]["items"] if row["direction"] == "outflow"
    )
    assert row["amount_fen"] == row["batch_payment"]["total_fen"] == "1000000"
    assert row["batch_payment"]["items"] == [
        {"party": "合成职工", "amount_fen": "830000"},
        {"party": "工资代发备用金费用", "amount_fen": "170000"},
    ]
