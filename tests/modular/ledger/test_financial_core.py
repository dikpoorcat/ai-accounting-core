"""Independent report numbers against trusted plans and the real company schema."""

import json
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest
from test_adoption import BANK, BUSINESS, COMPANY, SUPPLIER, adopt_declared
from test_adoption import store as store
from test_aggregations import journal, opening

from ai_accounting_modular.contracts.public import ContractError, validate_contract
from ai_accounting_modular.ledger._financial_cells import Issues
from ai_accounting_modular.ledger.public import LedgerQuery, create_ledger

ORIGIN = "50000000-0000-4000-8000-000000000080"
SALE = "50000000-0000-4000-8000-000000000081"
FEE = "50000000-0000-4000-8000-000000000082"
TAX = "50000000-0000-4000-8000-000000000083"


def origin(amount="130000", *, period="2026-01"):
    change = opening(ORIGIN, period, "existing_ledger")
    plan = change["plans"][0]
    plan["target"]["values"].update(debit_fen=amount, credit_fen=amount)
    plan["basis"]["adopted_quantities"] = (
        [
            {
                "type": "opening_balance",
                "member_id": str(uuid4()),
                "account_code": code,
                "debit_fen": amount if side == "debit" else "0",
                "credit_fen": amount if side == "credit" else "0",
                **extra,
            }
            for code, side, extra in (
                ("1002", "debit", {"object_id": BANK}),
                ("3001", "credit", {}),
            )
        ]
        if amount != "0"
        else []
    )
    return change


def funds(change, amount, category, *, inflow=True, movement_id=None):
    plan = change["plans"][0]
    plan["fund_movements"] = [
        {
            "movement_id": movement_id or str(uuid4()),
            "public_slot": "funds",
            "funds_account_id": BANK,
            "actual_on": plan["accounting_period"] + "-15",
            "direction": "inflow" if inflow else "outflow",
            "amount_fen": str(amount),
            **(
                {"cashflow_parts": [{"category": category, "amount_fen": str(amount)}]}
                if category
                else {}
            ),
        }
    ]
    return change


def save(storage, ledger, changes, *, freeze=None, correction=None):
    with storage.write_unit(COMPANY) as unit:
        app = unit.repository("application")
        identity = unit.repository("identity")
        if (
            identity.session.fetchone("SELECT 1 FROM identity_object WHERE object_id=?", (BANK,))
            is None
        ):
            identity.bank()
        for change in changes:
            responsibility = change["businesses"][0]
            if responsibility["expected_seq"] == 0 and responsibility["business_id"] != BUSINESS:
                app.business(responsibility["business_id"])
            for plan in change["plans"]:
                for movement in plan["fund_movements"]:
                    unit.repository("money").session.execute(
                        "INSERT OR IGNORE INTO money_occurrence VALUES(?,?,?)",
                        (movement["movement_id"], plan["business_id"], movement["public_slot"]),
                    )
        adopt_declared(ledger, changes, unit, correction_period=correction)
        if freeze:
            unit.repository("ledger").freeze_heads(freeze)


def report(storage, ledger, first="2026-01", last=None, *, view="current", **extra):
    end = last or first
    with storage.read_unit(COMPANY, view=view, as_of=end if view == "as_posted" else None) as read:
        scope = {
            "company_id": COMPANY,
            "period_from": first,
            "period_to": end,
            "view": view,
            **extra,
        }
        return ledger.financial_snapshot(scope, read)


def cell(snapshot, statement, line, column="current"):
    return snapshot[statement][str(line)][column]


def value(snapshot, statement, line, column="current"):
    return cell(snapshot, statement, line, column)["amount_fen"]


def test_r01_complete_dto_and_independent_three_statement_numbers(store):
    ledger = create_ledger()
    save(
        store,
        ledger,
        [
            origin(),
            funds(
                journal(SALE, [("1002", 100000), ("5001", -100000)]), 100000, "customer_receipts"
            ),
            funds(
                journal(FEE, [("5602", 20000), ("1002", -20000)]),
                20000,
                "other_operating_payments",
                inflow=False,
            ),
            journal(TAX, [("5801", 4000), ("222106", -4000)], publishes=False),
        ],
    )
    actual = report(store, ledger)
    validate_contract("reporting.schema.json", "FinancialSnapshot", actual)
    assert actual["status"] == "ready"
    assert len(actual["balance_sheet"]) == 53
    assert len(actual["profit_statement"]) == 32
    assert len(actual["cash_flow_statement"]) == 22
    assert value(actual, "balance_sheet", 1, "beginning") == "130000"
    assert value(actual, "balance_sheet", 1, "ending") == "210000"
    assert value(actual, "balance_sheet", 36, "ending") == "4000"
    assert value(actual, "balance_sheet", 51, "ending") == "76000"
    assert [value(actual, "profit_statement", n) for n in (1, 14, 30, 31, 32)] == [
        "100000",
        "20000",
        "80000",
        "4000",
        "76000",
    ]
    assert [value(actual, "cash_flow_statement", n) for n in (1, 6, 20, 21, 22)] == [
        "100000",
        "20000",
        "80000",
        "130000",
        "210000",
    ]
    assert all(check["status"] == "pass" for check in actual["checks"])
    oracle = json.loads(
        (Path(__file__).parents[3] / "docs/design/examples/reporting-cases.json").read_text(
            encoding="utf-8"
        )
    )["cases"][0]["expected_snapshot"]
    for table in ("balance_sheet", "profit_statement", "cash_flow_statement"):
        assert actual[table] == oracle[table]
    with store.read_unit(COMPANY) as read:
        assert actual["pre_tax"] == ledger.pre_tax("2026-01", "2026-01", read).fields


def test_unknown_cash_keeps_balance_and_profit_known_and_history_refuses_incomplete_page(store):
    ledger = create_ledger()
    save(
        store,
        ledger,
        [origin(), funds(journal(SALE, [("1002", 100000), ("5001", -100000)]), 100000, None)],
    )
    result = report(store, ledger)
    assert result["status"] == "partial"
    assert value(result, "profit_statement", 30) == "100000"
    assert cell(result, "cash_flow_statement", 20)["state"] == "unestablished"
    assert value(result, "cash_flow_statement", 22) == "230000"
    with store.read_unit(COMPANY) as read:
        with pytest.raises(ContractError) as missing:
            ledger.query_cashflow_contributions(LedgerQuery(), read)
        assert missing.value.code == "needs_information"


def test_stable_cash_adoption_deltas_not_three_actual_receipts(store):
    ledger = create_ledger()
    first = funds(journal(SALE, [("1002", 100000), ("5001", -100000)]), 100000, "customer_receipts")
    save(store, ledger, [origin(), first], freeze="2026-01")
    changed = deepcopy(first)
    changed["businesses"][0].update(expected_seq=1, op="replace")
    plan = changed["plans"][0]
    plan["lines"][0]["debit_fen"] = plan["lines"][1]["credit_fen"] = "90000"
    plan["fund_movements"][0]["amount_fen"] = "90000"
    plan["fund_movements"][0]["cashflow_parts"][0]["amount_fen"] = "90000"
    save(store, ledger, [changed], freeze="2026-02", correction="2026-02")
    changed = deepcopy(changed)
    changed["businesses"][0]["expected_seq"] = 2
    plan = changed["plans"][0]
    plan["lines"][0]["debit_fen"] = plan["lines"][1]["credit_fen"] = "80000"
    plan["fund_movements"][0]["amount_fen"] = "80000"
    plan["fund_movements"][0]["cashflow_parts"][0]["amount_fen"] = "80000"
    save(store, ledger, [changed], correction="2026-03")
    for month, expected in (("2026-01", "100000"), ("2026-02", "-10000"), ("2026-03", "-10000")):
        actual = report(store, ledger, month, view="as_posted")
        assert value(actual, "cash_flow_statement", 1) == expected
    assert (
        value(
            report(store, ledger, "2026-03", view="as_posted"),
            "cash_flow_statement",
            1,
            "year_to_date",
        )
        == "80000"
    )
    assert value(report(store, ledger), "cash_flow_statement", 1) == "80000"
    with store.read_unit(COMPANY, view="as_posted", as_of="2026-03") as read:
        page = ledger.query_cashflow_contributions(LedgerQuery(), read)
        assert [row["signed_amount_fen"] for row in page.items] == ["100000", "-10000", "-10000"]
    with store.read_unit(COMPANY) as read:
        assert ledger.query_cashflow_contributions(LedgerQuery(), read).items == page.items


def test_expected_selection_ignores_unrelated_company_head_and_rejects_actual_change(store):
    ledger = create_ledger()
    save(store, ledger, [origin()])
    first = report(store, ledger)
    save(store, ledger, [journal(FEE, [("5602", 100), ("2202", -100)], period="2026-04")])
    after = report(store, ledger, expected_head=first["selection"]["selection_digest"])
    assert after["head"] != first["head"]
    assert after["selection"] == first["selection"]
    save(store, ledger, [journal(SALE, [("1002", 100), ("5001", -100)])])
    with pytest.raises(ContractError) as changed:
        report(store, ledger, expected_head=first["selection"]["selection_digest"])
    assert changed.value.code == "snapshot_changed"


@pytest.mark.parametrize("case", ["R03", "R04", "R05", "R06", "R13", "R17", "R20"])
def test_fixed_numeric_mapping_oracles(case, store):
    ledger = create_ledger()
    changes = [origin()]
    expected = {}
    if case == "R03":
        changes.append(journal(SALE, [("4301", 12000), ("189901", 8000), ("3001", -20000)]))
        expected = {
            ("balance_sheet", 26, "ending"): "12000",
            ("balance_sheet", 28, "ending"): "8000",
            ("balance_sheet", 9, "ending"): "0",
        }
    elif case == "R04":
        changes.extend(
            [
                funds(
                    journal(SALE, [("1101", 100000), ("1002", -100000)]),
                    100000,
                    "investment_acquisition",
                    inflow=False,
                ),
                funds(
                    journal(FEE, [("1002", 103000), ("1101", -100000), ("5111", -3000)]),
                    103000,
                    "investment_recovery",
                ),
            ]
        )
        expected = {
            ("profit_statement", 20, "current"): "3000",
            ("cash_flow_statement", 8, "current"): "103000",
            ("cash_flow_statement", 9, "current"): "0",
        }
    elif case == "R05":
        outgoing = funds(
            journal(SALE, [("2501", 100000), ("2231", 3000), ("1002", -103000)]),
            103000,
            "loan_repayment",
            inflow=False,
        )
        outgoing["plans"][0]["fund_movements"][0]["cashflow_parts"] = [
            {"category": "loan_repayment", "amount_fen": "100000"},
            {"category": "interest_payments", "amount_fen": "3000"},
        ]
        changes.append(outgoing)
        expected = {
            ("cash_flow_statement", 16, "current"): "100000",
            ("cash_flow_statement", 17, "current"): "3000",
            ("cash_flow_statement", 19, "current"): "-103000",
        }
    elif case == "R06":
        changes.append(journal(SALE, [("5602", 3333), ("1602", -3333)]))
        transfer = funds(
            journal(FEE, [("1002", 50000), ("1002", -50000)]), 50000, "none", inflow=False
        )
        first = transfer["plans"][0]["fund_movements"][0]
        first["public_slot"] = "from_funds"
        first["transfer_group_id"] = str(uuid4())
        second = deepcopy(first)
        second.update(
            public_slot="to_funds",
            movement_id=str(uuid4()),
            direction="inflow",
            funds_account_id="60000000-0000-4000-8000-000000000099",
        )
        transfer["plans"][0]["fund_movements"].append(second)
        with store.write_unit(COMPANY) as unit:
            unit.repository("identity").session.execute(
                "INSERT INTO identity_object VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    second["funds_account_id"],
                    "bank_account",
                    "合成第二账户",
                    "bank-second",
                    1,
                    "{}",
                    "0" * 64,
                    0,
                    str(uuid4()),
                ),
            )
        changes.append(transfer)
        expected = {
            ("cash_flow_statement", 20, "current"): "0",
            ("profit_statement", 14, "current"): "3333",
            ("cash_flow_statement", 6, "current"): "0",
        }
    elif case == "R13":
        changes.append(
            funds(
                journal(SALE, [("122106", 100000), ("1002", -100000)]),
                100000,
                "other_operating_payments",
                inflow=False,
            )
        )
        changes[1]["plans"][0]["lines"][0]["party_id"] = SUPPLIER
        changes.append(
            journal(FEE, [("122106", 20000), ("1122", -20000), ("5401", 6000), ("122106", -6000)])
        )
        for line in changes[2]["plans"][0]["lines"]:
            if line["account_code"] in ("122106", "1122"):
                line["party_id"] = SUPPLIER
        expected = {
            ("cash_flow_statement", 6, "current"): "100000",
            ("cash_flow_statement", 11, "current"): "0",
            ("cash_flow_statement", 1, "current"): "0",
            ("cash_flow_statement", 3, "current"): "0",
        }
    elif case == "R17":
        incoming = funds(
            journal(SALE, [("1002", 10000), ("6301", -10000)]), 10000, "asset_disposal"
        )
        changes.append(incoming)
        for business_id, paid, parts in (
            (FEE, 1, [("tax_payments", 1)]),
            (TAX, 1, [("asset_disposal", 1)]),
            (BUSINESS, 298, [("tax_payments", 199), ("asset_disposal", 99)]),
        ):
            payment = funds(
                journal(business_id, [("222101", paid), ("1002", -paid)]),
                paid,
                "tax_payments",
                inflow=False,
            )
            payment["plans"][0]["fund_movements"][0]["cashflow_parts"] = [
                {"category": category, "amount_fen": str(value)} for category, value in parts
            ]
            changes.append(payment)
        expected = {
            ("cash_flow_statement", 5, "current"): "200",
            ("cash_flow_statement", 10, "current"): "9900",
            ("cash_flow_statement", 20, "current"): "9700",
        }
    else:
        item = journal(SALE, [("222101", 500), ("222106", -100), ("1221", 200), ("3001", -600)])
        item["plans"][0]["lines"][2]["party_id"] = SUPPLIER
        changes.append(item)
        expected = {
            ("balance_sheet", 36, "ending"): "-400",
            ("balance_sheet", 14, "ending"): "0",
            ("balance_sheet", 8, "ending"): "200",
        }
    save(store, ledger, changes)
    actual = report(store, ledger)
    for (statement, line, column), wanted in expected.items():
        assert value(actual, statement, line, column) == wanted


def test_r02_party_balances_do_not_net_different_creditors(store):
    ledger = create_ledger()
    parties = [f"60000000-0000-4000-8000-{i:012d}" for i in (21, 22, 23, 24)]
    with store.write_unit(COMPANY) as unit:
        for party in parties:
            unit.repository("identity").session.execute(
                "INSERT INTO identity_object VALUES(?,?,?,?,?,?,?,?,?)",
                (party, "organization", "合成交易方", party, 1, "{}", "0" * 64, 0, str(uuid4())),
            )
    change = journal(
        SALE, [("1122", 10000), ("1122", -3000), ("2202", 2000), ("2202", -7000), ("3001", -2000)]
    )
    for line, party in zip(change["plans"][0]["lines"][:4], parties, strict=True):
        line["party_id"] = party
    save(store, ledger, [origin(), change])
    actual = report(store, ledger)
    assert [value(actual, "balance_sheet", line, "ending") for line in (4, 34, 5, 33)] == [
        "10000",
        "3000",
        "2000",
        "7000",
    ]


def test_r14_fault_injected_unmapped_balance_and_unknown_money_are_not_zero(store):
    ledger = create_ledger()
    save(
        store,
        ledger,
        [origin(), funds(journal(SALE, [("1002", 1000), ("5001", -1000)]), 1000, None)],
    )
    with store.write_unit(COMPANY) as unit:
        session = unit.repository("ledger")._session
        session.execute("UPDATE voucher SET sealed=0")
        session.execute("UPDATE voucher_line SET account_code='9999' WHERE account_code='5001'")
        session.execute("UPDATE voucher SET sealed=1")
    actual = report(store, ledger)
    assert actual["status"] == "partial"
    assert cell(actual, "balance_sheet", 30, "ending")["state"] == "unestablished"
    assert cell(actual, "cash_flow_statement", 20)["state"] == "unestablished"
    assert value(actual, "cash_flow_statement", 22) == "131000"
    with store.read_unit(COMPANY) as read:
        with pytest.raises(ContractError) as error:
            ledger.pre_tax("2026-01", "2026-01", read)
        assert error.value.code == "storage_corrupt"


def test_cash_begin_does_not_request_unrelated_intermediate_loan_classification(store):
    ledger = create_ledger()
    incoming = funds(
        journal(SALE, [("1002", 100), ("2501", -100)], period="2026-03"), 100, "loan_receipts"
    )
    outgoing = funds(
        journal(FEE, [("2501", 100), ("1002", -100)], period="2026-04"),
        100,
        "loan_repayment",
        inflow=False,
    )
    save(store, ledger, [origin("0"), incoming, outgoing])
    actual = report(store, ledger, "2026-04")
    assert value(actual, "cash_flow_statement", 21) == "100"
    assert value(actual, "cash_flow_statement", 22) == "0"
    assert actual["status"] == "ready"


def test_cash_classification_changes_selection_but_not_pretax_basis(store):
    ledger = create_ledger()
    incoming = funds(journal(SALE, [("1002", 1000), ("5001", -1000)]), 1000, None)
    save(store, ledger, [origin(), incoming])
    before = report(store, ledger)
    changed = deepcopy(incoming)
    changed["businesses"][0].update(expected_seq=1, op="replace")
    changed["plans"][0]["fund_movements"][0]["cashflow_parts"] = [
        {"category": "customer_receipts", "amount_fen": "1000"}
    ]
    save(store, ledger, [changed])
    after = report(store, ledger)
    assert after["status"] == "ready"
    assert after["selection"]["selection_digest"] != before["selection"]["selection_digest"]
    assert after["pre_tax"] == before["pre_tax"]


def test_same_transaction_explicit_scope_reads_both_views_without_changing_defaults(store):
    ledger = create_ledger()
    original = funds(journal(SALE, [("1002", 1000), ("5001", -1000)]), 1000, "customer_receipts")
    save(store, ledger, [origin(), original], freeze="2026-01")
    changed = deepcopy(original)
    changed["businesses"][0].update(expected_seq=1, op="replace")
    changed["plans"][0]["lines"][0]["debit_fen"] = "900"
    changed["plans"][0]["lines"][1]["credit_fen"] = "900"
    movement = changed["plans"][0]["fund_movements"][0]
    movement["amount_fen"] = movement["cashflow_parts"][0]["amount_fen"] = "900"
    save(store, ledger, [changed], correction="2026-02")
    scope = {"company_id": COMPANY, "period_from": "2026-01", "period_to": "2026-01"}
    with store.read_unit(COMPANY, view="as_posted", as_of="2026-02") as read:
        current = ledger.financial_snapshot({**scope, "view": "current"}, read)
        historic = ledger.financial_snapshot({**scope, "view": "as_posted"}, read)
        assert value(current, "cash_flow_statement", 1) == "900"
        assert value(historic, "cash_flow_statement", 1) == "1000"
        assert current["head"] == historic["head"] == read.head
        assert read.view == "as_posted" and read.as_of == "2026-02"


def test_report_aggregates_beyond_page_size_and_groups_repeated_missing_meanings(store):
    ledger = create_ledger()
    changes = [origin()]
    for _ in range(501):
        changes.append(funds(journal(str(uuid4()), [("1002", 1), ("5001", -1)]), 1, None))
    save(store, ledger, changes)
    actual = report(store, ledger)
    assert actual["status"] == "partial"
    assert value(actual, "profit_statement", 1) == "501"
    assert value(actual, "balance_sheet", 1, "ending") == "130501"
    assert len(actual["issues"]) == 20
    with store.read_unit(COMPANY) as read:
        assert ledger.pre_tax("2026-01", "2026-01", read).fields == actual["pre_tax"]


def test_field_issues_keep_one_cause_or_summarize_different_formal_causes():
    issues = Issues()
    field = "/profit_statement/10/current"
    for _ in range(2):
        assert issues.unknown(
            field, "房产税明细", "缺少房产税金额。", code="property_tax_missing"
        ) == {
            "state": "unestablished",
            "issue_indexes": [0],
        }
    assert len(issues.values) == 1
    assert issues.values[0]["message"] == "缺少房产税金额。"
    for number in range(1001):
        indexes = issues.unknown(field, "来源明细", "不同来源的明细。", code=f"missing_{number}")
        assert indexes["issue_indexes"] == [0]
    assert len(issues.values) == 1
    assert issues.values[0]["code"] == "report_basis_missing"
    assert issues.values[0]["message"] == (
        "本字段存在未明确的正式分类或采用依据，请查看相关业务明细。"
    )
