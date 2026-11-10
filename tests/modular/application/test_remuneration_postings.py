"""Formal payroll turnover survives payments, corrections, and frozen old rules."""

from collections import Counter
from copy import deepcopy

import pytest

from ai_accounting_modular.agent.public import create_agent
from ai_accounting_modular.contracts.public import content_digest
from ai_accounting_modular.domains.people import public as people
from tests.modular.domains.people.test_people import PERSON
from tests.modular.domains.people.test_read_remuneration import (
    activity,
    add,
    bank_object,
    correct,
    payment,
    wage_with_actuals,
)
from tests.modular.domains.people.test_read_remuneration_tax import tax_target

from ._working_case import case as case
from .test_remuneration_command import query


def result(book, wage, **exact):
    return book.get(wage["business_id"], **exact)["businesses"][0]["results"][0]


def postings(lines):
    return Counter(
        (
            row["account_code"],
            int(row["debit_fen"]),
            int(row["credit_fen"]),
            row.get("party_id"),
        )
        for row in lines
    )


def assert_complete_payroll(vouchers, gross=600000):
    by_key = {v["journal_key"]: v for v in vouchers}
    assert set(by_key) == {"accrual", "deductions"}
    assert sum(int(r["debit_fen"]) for r in by_key["accrual"]["lines"]) == gross + 90000
    assert sum(int(r["debit_fen"]) for r in by_key["deductions"]["lines"]) == 100000
    for voucher in vouchers:
        rows = voucher["lines"]
        assert [row["line_no"] for row in rows] == list(range(1, len(rows) + 1))
        assert sum(int(r["debit_fen"]) for r in rows) == sum(int(r["credit_fen"]) for r in rows)
    lines = [row for v in vouchers for row in v["lines"]]
    assert postings(lines) == Counter(
        (
            ("560201", gross, 0, PERSON),
            ("221101", 0, gross, PERSON),
            ("560201", 60000, 0, PERSON),
            ("221102", 0, 60000, None),
            ("560201", 30000, 0, PERSON),
            ("221103", 0, 30000, None),
            ("221101", 50000, 0, PERSON),
            ("224102", 0, 50000, None),
            ("221101", 20000, 0, PERSON),
            ("224103", 0, 20000, None),
            ("221101", 30000, 0, PERSON),
            ("222103", 0, 30000, None),
        )
    )


def obligations(book, wage, period):
    with book.kernel.storage.read_unit(book.company) as read:
        positions = book.kernel.ledger.get_obligation_positions(
            period, read, business_ids=[wage["business_id"]]
        )
    return {item.definition.reference["slot"]: item.amounts for item in positions}


def financial(book, period):
    scope = {
        "company_id": book.company,
        "period_from": period,
        "period_to": period,
        "view": "as_posted",
    }
    with book.kernel.storage.read_unit(book.company) as read:
        return (
            book.kernel.ledger.financial_snapshot(scope, read),
            book.kernel.ledger.financial_summary(scope, read),
        )


def assert_revision(book, wage, revision):
    # The public adopted source digest binds the saved rule revision as well as
    # its target and basis; checking the proposed plan alone would not prove it.
    with book.kernel.storage.read_unit(book.company) as read:
        refs = book.kernel.ledger.get_result_refs([wage["business_id"]], read)
        content = book.kernel.ledger.get_adopted_contents(refs, read)[0]
    assert content.source["content_digest"] == content_digest(
        {
            "calculation_key": "people.remuneration",
            "calculation_revision": revision,
            "basis_digest": content_digest(content.basis),
            "target_digest": content_digest(content.target),
        }
    )


def test_persisted_gross_and_deductions_keep_net_slots_and_open_voucher_number(case):
    case.seed(expense=False)
    wage, employment, actual = wage_with_actuals()
    add(case, wage, employment, actual)
    original = result(case, wage)
    assert_complete_payroll(original["vouchers"])
    assert_revision(case, wage, 3)
    assert {
        slot: (item.amount_fen, item.remaining_fen)
        for slot, item in obligations(case, wage, "2026-01").items()
    } == {
        "net_pay": (500000, 500000),
        "tax": (30000, 30000),
        "employee_social": (50000, 50000),
        "employee_housing": (20000, 20000),
        "employer_social": (60000, 60000),
        "employer_housing": (30000, 30000),
    }
    agent = create_agent(case.app)
    current = query(agent, case, wage, "2026-01")["items"][0]
    assert current["payable_fen"] == current["remaining_fen"] == "500000"
    assert current["amounts"]["cost_fen"] == "690000"

    changed = deepcopy(wage)
    changed["data"]["gross_fen"] = "650000"
    correct(case, changed)
    corrected = result(case, wage)
    assert_complete_payroll(corrected["vouchers"], 650000)
    assert corrected["reference"]["result_id"] == original["reference"]["result_id"]
    assert corrected["reference"]["generation"] == original["reference"]["generation"] + 1
    assert len(corrected["vouchers"]) == 2
    assert {v["journal_key"]: (v["voucher_id"], v["number"]) for v in corrected["vouchers"]} == {
        v["journal_key"]: (v["voucher_id"], v["number"]) for v in original["vouchers"]
    }
    assert query(agent, case, wage, "2026-01")["items"][0]["payable_fen"] == "550000"
    assert correct(case, changed)["accounting"] == "unchanged"
    assert result(case, wage) == corrected


def test_later_partial_payments_and_tax_target_do_not_reaccrue_payroll(case):
    case.seed(expense=False)
    wage, employment, actual = wage_with_actuals()
    add(case, wage, employment, actual)
    checked = case.prepare_close()
    assert checked["ready"], checked
    assert case.close(checked)["state"] == "closed"
    frozen = result(case, wage)
    agent = create_agent(case.app)
    january = query(agent, case, wage, "2026-01")

    bank = bank_object()
    partial = payment(bank["object_id"], "2026-02", (wage["business_id"], "100000"))
    partial["data"]["counterparty_id"] = PERSON
    add(case, partial, objects=[bank])
    target = tax_target(wage, "40000", tax_period="2026-01")
    add(case, target)
    tax = result(case, target)
    assert postings(tax["vouchers"][0]["lines"]) == Counter(
        (("221101", 10000, 0, PERSON), ("222103", 0, 10000, None))
    )
    current = query(agent, case, wage)["items"][0]
    assert current["amounts"]["tax_fen"] == "40000"
    assert current["amounts"]["net_fen"] == "490000"
    assert current["payable_fen"] == current["remaining_fen"] == "390000"
    position = obligations(case, wage, "2026-02")["net_pay"]
    assert (position.amount_fen, position.money_settled_fen, position.transferred_out_fen) == (
        500000,
        100000,
        10000,
    )
    assert add(case, target)["accounting"] == "unchanged"
    assert result(case, wage) == frozen
    assert query(agent, case, wage, "2026-01") == january
    february = activity(case, "2026-02")
    assert february.summary.amounts.total_expense_fen == 0
    assert february.summary.net_settlements.money_outflow_fen == 100000
    report, _ = financial(case, "2026-02")
    assert report["profit_statement"]["30"]["current"]["amount_fen"] == "0"
    assert report["profit_statement"]["30"]["year_to_date"]["amount_fen"] == "-690000"

    final = payment(bank["object_id"], "2026-03", (wage["business_id"], "390000"))
    final["data"]["counterparty_id"] = PERSON
    add(case, final)
    assert query(agent, case, wage, "2026-03")["items"][0]["payable_fen"] == "0"
    assert result(case, wage) == frozen


def legacy_net_calculator(calculator):
    def evaluate(fact, context):
        plan = calculator(fact, context)
        assert fact["data"]["gross_fen"] == "600000"
        # Reproduce this synthetic revision-1 journal, not a revision-3 journal
        # relabelled as old. Only the test installation uses this calculator.
        old = (
            ("560201", "690000", "0", PERSON),
            ("221101", "0", "500000", PERSON),
            ("222103", "0", "30000", None),
            ("224102", "0", "50000", None),
            ("224103", "0", "20000", None),
            ("221102", "0", "60000", None),
            ("221103", "0", "30000", None),
        )
        lines = [
            {
                "line_no": index,
                "account_code": account,
                "debit_fen": debit,
                "credit_fen": credit,
                "summary": "职工薪酬",
                **({"party_id": person} if person else {}),
            }
            for index, (account, debit, credit, person) in enumerate(old, 1)
        ]
        plan["journals"] = [{"journal_key": "main", "summary": "职工薪酬", "lines": lines}]
        plan["calculation_revision"] = 1
        plan["rule_refs"][0].update(revision=1, official_urls=[])
        return plan

    return evaluate


@pytest.mark.parametrize(
    "legacy,gross,delta",
    [(False, "650000", 50000), (True, "600000", 0)],
    ids=["complete-turnover", "revision-one-net"],
)
def test_frozen_payroll_keeps_its_old_journal_until_explicit_correction(
    case, monkeypatch, legacy, gross, delta
):
    case.seed(expense=False)
    wage, employment, actual = wage_with_actuals()
    with monkeypatch.context() as installation:
        if legacy:
            installation.setitem(
                people._CALCULATORS,
                "people.remuneration",
                legacy_net_calculator(people._CALCULATORS["people.remuneration"]),
            )
        add(case, wage, employment, actual)
    assert_revision(case, wage, 1 if legacy else 3)
    checked = case.prepare_close()
    assert checked["ready"], checked
    assert case.close(checked)["state"] == "closed"
    original = result(case, wage)
    old_voucher = original["vouchers"][0]
    if legacy:
        assert len(old_voucher["lines"]) == 7
        assert not any(
            row["account_code"] == "221101" and row["debit_fen"] != "0"
            for row in old_voucher["lines"]
        )
    else:
        assert_complete_payroll(original["vouchers"])
    original_report, original_summary = financial(case, "2026-01")

    def no_recalculation(*_args):
        pytest.fail("Reading frozen payroll must not apply today's accounting rule")

    with monkeypatch.context() as reads:
        reads.setitem(people._CALCULATORS, "people.remuneration", no_recalculation)
        assert result(case, wage) == original
        payable = query(create_agent(case.app), case, wage, "2026-01")["items"][0]
        assert payable["payable_fen"] == "500000"
        assert financial(case, "2026-01")[0] == original_report

    changed = deepcopy(wage)
    changed["data"]["gross_fen"] = gross
    correct(case, changed, period="2026-02")
    current = result(case, wage)
    assert_revision(case, changed, 3)
    assert current["reference"]["business_period"] == "2026-01"
    assert current["reference"]["posting_period"] == "2026-02"
    assert current["reference"]["baseline_result_id"] == original["reference"]["result_id"]
    reversals = [v for v in current["vouchers"] if v["role"] == "reversal"]
    replacements = [v for v in current["vouchers"] if v["role"] == "replacement"]
    assert len(reversals) == len(original["vouchers"])
    assert len(current["vouchers"]) == (3 if legacy else 4)
    for reversal in reversals:
        source = next(
            v for v in original["vouchers"] if v["voucher_id"] == reversal["reverses_voucher_id"]
        )
        assert reversal["journal_key"] == source["journal_key"]
        assert reversal["lines"] == [
            dict(row, debit_fen=row["credit_fen"], credit_fen=row["debit_fen"])
            for row in source["lines"]
        ]
    assert_complete_payroll(replacements, int(gross))
    exact = result(
        case,
        wage,
        result_id=original["reference"]["result_id"],
        generation=original["reference"]["generation"],
    )
    assert exact == original
    assert correct(case, changed, period="2026-02")["accounting"] == "unchanged"
    assert result(case, changed) == current

    january, _ = financial(case, "2026-01")
    for table in ("profit_statement", "balance_sheet", "cash_flow_statement"):
        assert january[table] == original_report[table]
    february, summary = financial(case, "2026-02")
    assert february["profit_statement"]["30"]["current"]["amount_fen"] == str(-delta)
    assert february["profit_statement"]["30"]["year_to_date"]["amount_fen"] == str(-690000 - delta)
    assert original_summary.liabilities == {"state": "known", "amount_fen": "690000"}
    assert summary.liabilities == {"state": "known", "amount_fen": str(690000 + delta)}
    assert query(create_agent(case.app), case, wage)["items"][0]["payable_fen"] == str(
        500000 + delta
    )
