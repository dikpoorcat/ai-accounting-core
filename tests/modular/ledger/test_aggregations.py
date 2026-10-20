"""Independent trusted-program plans test aggregate selection, not domain formulae."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from test_adoption import (
    BUSINESS,
    COMPANY,
    adopt_declared,
    expense,
    publication,
    source_only_change,
    withdrawal,
)
from test_adoption import store as store

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.ledger.public import create_ledger

SECOND = "50000000-0000-4000-8000-000000000002"
THIRD = "50000000-0000-4000-8000-000000000003"
FOURTH = "50000000-0000-4000-8000-000000000004"


def at_period(change, period):
    change["businesses"][0]["fact"]["period"] = period
    for plan in change["plans"]:
        plan["accounting_period"] = plan["basis"]["period"] = period
        for obligation in plan["obligations"]:
            obligation["business_period"] = period
    for item in change["publications"]:
        item["period_from"] = item["period_to"] = period
    return change


def sale(change, tax_period, *, net="10000", vat="300"):
    plan = change["plans"][0]
    plan["tax_sales"] = [
        {
            "source_business_id": plan["business_id"],
            "tax_period": tax_period,
            "net_sales_fen": net,
            "vat_fen": vat,
            "nominal_rate": "0.03",
            "tax_base_rate": "0.03",
            "payable_rate": "0.03",
            "invoice_kind": "ordinary",
            "exemption_eligible": True,
            "filing_bucket": "standard",
            "ranges": [],
        }
    ]
    change["publications"].append(
        publication(plan["business_id"], tax_period, collection="tax_sales")
    )
    return change


def journal(business_id, entries, *, period="2026-01", publishes=True):
    """A strict trusted-plan fixture; public application never accepts journal lines."""
    change = at_period(expense("1000", business_id=business_id), period)
    plan = change["plans"][0]
    plan["obligations"] = []
    plan["lines"] = [
        {
            "line_no": index,
            "account_code": account,
            "debit_fen": str(max(amount, 0)),
            "credit_fen": str(max(-amount, 0)),
            "summary": "独立账务数字",
        }
        for index, (account, amount) in enumerate(entries, 1)
    ]
    if not publishes:
        change["publications"] = []
    return change


def candidate(ledger, uow, changes):
    return ledger.begin_candidate(
        [item for change in changes for item in change["businesses"]],
        uow,
        publications=[item for change in changes for item in change["publications"]],
    )


def adopt(ledger, storage, changes):
    with storage.write_unit(COMPANY) as uow:
        for change in changes:
            item = change["businesses"][0]
            if item["expected_seq"] == 0 and item["business_id"] != BUSINESS:
                uow.repository("application").business(item["business_id"])
        adopt_declared(ledger, changes, uow)


def amount(view, name="profit_before_tax"):
    return view.fields[name]["amount_fen"]


def test_aggregates_wait_only_relevant_publishers_and_preserve_bound_sources(store):
    ledger = create_ledger()
    first = sale(expense("1000"), "2026-03")
    future = sale(at_period(expense(business_id=SECOND), "2026-04"), "2026-04")
    eit = journal(THIRD, [("5801", 100), ("222106", -100)], publishes=False)
    with store.write_unit(COMPANY) as uow:
        view = candidate(ledger, uow, [first, future, eit])
        with pytest.raises(ContractError, match="全部相关发布结果"):
            view.tax_sales("2026-03", "2026-03")
        with pytest.raises(ContractError, match="全部相关发布结果"):
            view.pre_tax("2026-01", "2026-01")
        view.stage(first["plans"][0])
        tax = view.tax_sales("2026-03", "2026-03")
        profit = view.pre_tax("2026-01", "2026-01")
        assert amount(profit) == "-1000"
        assert tax[0].source == ledger.get_result_source(
            BUSINESS, "main", uow, plan=first["plans"][0]
        )
        assert tax[0].source == profit.sources[0]
        assert tax[0].sale == first["plans"][0]["tax_sales"][0]
        assert view.tax_sales("2026-01", "2026-01") == ()
        with pytest.raises(ContractError, match="尚未收到全部"):
            view.complete()
        # Unrelated April output and the income-tax consumer have not staged.
        adopt_declared(ledger, [first], uow)
        assert ledger.tax_sales("2026-03", "2026-03", uow) == tax
        assert ledger.pre_tax("2026-01", "2026-01", uow) == profit
    with store.read_unit(COMPANY) as read:
        assert ledger.tax_sales("2026-03", "2026-03", read) == tax
        assert ledger.pre_tax("2026-01", "2026-01", read) == profit
    with pytest.raises(ContractError, match="工作单元|事务|连接"):
        view.tax_sales("2026-03", "2026-03")


def test_tax_sale_move_and_withdraw_replace_entire_old_responsibility(store):
    ledger = create_ledger()
    adopt(ledger, store, [sale(expense(), "2026-03")])
    moved = sale(expense("90000", seq=1), "2026-04", net="-10000", vat="-300")
    with store.write_unit(COMPANY) as uow:
        view = candidate(ledger, uow, [moved])
        # The retired March promise must not fall back to the old March rows.
        assert view.tax_sales("2026-03", "2026-03") == ()
        with pytest.raises(ContractError, match="全部相关发布结果"):
            view.tax_sales("2026-04", "2026-04")
        view.stage(moved["plans"][0])
        april = view.tax_sales("2026-04", "2026-04")
        assert april[0].sale["net_sales_fen"] == "-10000"
        adopt_declared(ledger, [moved], uow)
        assert ledger.tax_sales("2026-03", "2026-03", uow) == ()
        assert ledger.tax_sales("2026-04", "2026-04", uow) == april
    withdrawn = deepcopy(moved)
    withdrawn.update(plans=[], publications=[])
    item = withdrawn["businesses"][0]
    item.pop("fact")
    item.update(op="withdraw", expected_seq=2, complete_result_slots=[])
    with store.write_unit(COMPANY) as uow:
        view = candidate(ledger, uow, [withdrawn])
        assert view.tax_sales("2026-01", "2026-12") == ()
        assert amount(view.pre_tax("2026-01", "2026-12")) == "0"
        adopt_declared(ledger, [withdrawn], uow)
    with store.read_unit(COMPANY) as read:
        assert ledger.tax_sales("2026-01", "2026-12", read) == ()


def test_identical_tax_components_remain_distinct_and_retired_slots_cannot_leak(store):
    ledger = create_ledger()
    original = sale(expense("1000"), "2026-03")
    aux = deepcopy(original["plans"][0])
    aux["result_slot"] = aux["target"]["result_slot"] = aux["basis"]["result_slot"] = "aux"
    aux["obligations"] = []
    original["plans"].append(aux)
    original["businesses"][0]["complete_result_slots"].append("aux")
    original["publications"].extend(
        [
            publication(BUSINESS, slot="aux"),
            publication(BUSINESS, "2026-03", slot="aux", collection="tax_sales"),
        ]
    )
    adopt(ledger, store, [original])
    with store.read_unit(COMPANY) as read:
        old = ledger.tax_sales("2026-03", "2026-03", read)
        assert len(old) == 2
        assert old[0].sale == old[1].sale
        assert {value.source["result_slot"] for value in old} == {"main", "aux"}
    replacement = sale(expense("900", seq=1), "2026-04")
    with store.write_unit(COMPANY) as uow:
        view = candidate(ledger, uow, [replacement])
        assert view.tax_sales("2026-03", "2026-03") == ()
        view.stage(replacement["plans"][0])
        assert len(view.tax_sales("2026-01", "2026-12")) == 1
        adopt_declared(ledger, [replacement], uow)
        assert ledger.tax_sales("2026-03", "2026-03", uow) == ()


def test_shared_profit_uses_controlled_main_rows_and_excludes_eit_and_payment(store):
    ledger = create_ledger()
    # Revenue 10000 − service cost 2000 − admin 1000 − surtax 100 + VAT income 300.
    changes = [
        journal(BUSINESS, [("1122", 10000), ("5001", -10000)]),
        journal(SECOND, [("540101", 2000), ("560204", 1000), ("2202", -3000)]),
        journal(THIRD, [("5403", 100), ("222102", -100), ("222101", 300), ("630101", -300)]),
    ]
    adopt(ledger, store, changes)
    with store.read_unit(COMPANY) as read:
        before = ledger.pre_tax("2026-01", "2026-01", read)
    assert [
        amount(before, field)
        for field in ("operating_revenue", "operating_cost", "profit_before_tax")
    ] == ["10000", "2000", "7200"]
    neutral = journal(
        FOURTH, [("5801", 1000), ("222106", -1000), ("2202", 500), ("1002", -500)], publishes=False
    )
    adopt(ledger, store, [neutral])
    with store.read_unit(COMPANY) as read:
        assert ledger.pre_tax("2026-01", "2026-01", read) == before
    assert {source["business_id"] for source in before.sources} == {BUSINESS, SECOND, THIRD}


def test_profit_digest_changes_only_for_financial_slices_and_sources_stay_exact(store):
    ledger = create_ledger()
    adopt(ledger, store, [expense("1000")])
    with store.read_unit(COMPANY) as read:
        before = ledger.pre_tax("2026-01", "2026-01", read)
    detail = expense("1000", seq=1)
    detail["plans"][0]["basis"]["input"]["report_details"][0]["code"] = "management_startup"
    detail["plans"][0]["report_contributions"] = [
        {
            "statement": "profit",
            "line": 15,
            "detail_code": "management_startup",
            "amount_fen": "1000",
        }
    ]
    adopt(ledger, store, [detail])
    with store.read_unit(COMPANY) as read:
        after = ledger.pre_tax("2026-01", "2026-01", read)
        assert after.fields == before.fields
        assert after.sources != before.sources
        assert after.sources == (ledger.get_result_source(BUSINESS, "main", read),)
    adopt(ledger, store, [expense("900", seq=2)])
    with store.read_unit(COMPANY) as read:
        changed = ledger.pre_tax("2026-01", "2026-01", read)
        assert amount(changed) == "-900"
        assert changed.fields["basis_digest"] != before.fields["basis_digest"]


def test_formal_profit_resolves_journals_in_one_batch_not_once_per_business(store, monkeypatch):
    ledger = create_ledger()
    changes = [
        expense("100", business_id=f"50000000-0000-4000-8000-{n:012d}") for n in range(1, 32)
    ]
    adopt(ledger, store, changes)
    with store.read_unit(COMPANY) as read:
        repo = read.repository("ledger")
        real_vouchers = repo.vouchers
        calls = []

        def vouchers(ids):
            calls.append(tuple(ids))
            return real_vouchers(ids)

        monkeypatch.setattr(repo, "vouchers", vouchers)
        assert amount(ledger.pre_tax("2026-01", "2026-01", read)) == "-3100"
        assert len(calls) == 1
        assert len(calls[0]) == 31


def test_frozen_current_profit_resolves_inherited_targets_without_posting_deltas(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [expense("1000")], uow)
        uow.repository("ledger").freeze_heads("2026-01")
    formal = expense("1000", seq=1)
    formal["plans"][0]["obligations"][0]["allowed_settlements"] = ["money"]
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [formal], uow, correction_period="2026-02")
        uow.repository("ledger").freeze_heads("2026-02")
    changed = expense("900", seq=2)
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [changed], uow, correction_period="2026-03")
        uow.repository("ledger").freeze_heads("2026-03")
        basis = ledger.get_result_source(BUSINESS, "main", uow)
    inherited = expense("900", seq=3)
    inherited["plans"][0]["basis"]["input"]["report_details"][0]["code"] = "management_startup"
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [inherited], uow, correction_period="2026-04")
        current = ledger.pre_tax("2026-01", "2026-01", uow)
        assert amount(current) == "-900"
        assert current.sources == (ledger.get_result_source(BUSINESS, "main", uow),)
        assert amount(ledger.pre_tax("2026-02", "2026-04", uow)) == "0"
        uow.repository("ledger").freeze_heads("2026-04")
        baseline = ledger.get_result_source(BUSINESS, "main", uow)
        assert baseline != basis
    with store.read_unit(COMPANY, view="as_posted", as_of="2026-04") as read:
        for method in (ledger.pre_tax, ledger.tax_sales):
            with pytest.raises(ContractError, match="只支持完整当前目标"):
                method("2026-01", "2026-01", read)
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [withdrawal(baseline, seq=4)], uow, correction_period="2026-05")
        assert amount(ledger.pre_tax("2026-01", "2026-01", uow)) == "0"


@pytest.mark.parametrize("missing", ["tax_sales", "pre_tax"])
def test_actual_publications_must_be_covered_before_any_adoption(missing, store):
    ledger = create_ledger()
    invalid = sale(expense(business_id=SECOND), "2026-03")
    invalid["publications"] = [p for p in invalid["publications"] if p["collection"] != missing]
    with store.write_unit(COMPANY) as uow:
        view = candidate(ledger, uow, [invalid])
        with pytest.raises(ContractError, match="发布范围完整覆盖"):
            view.stage(invalid["plans"][0])
    with pytest.raises(ContractError, match="发布范围完整覆盖"):
        with store.write_unit(COMPANY) as uow:
            uow.repository("application").business(SECOND)
            adopt_declared(ledger, [expense(), invalid], uow)
    with store.read_unit(COMPANY) as read:
        assert ledger.get_results([BUSINESS, SECOND], read) == ()


@pytest.mark.parametrize("store", ["2026-07"], indirect=True)
def test_unknown_prior_profit_does_not_hide_known_current_month(store):
    ledger = create_ledger()
    adopt(ledger, store, [at_period(expense("1000"), "2026-07")])
    with store.read_unit(COMPANY) as read:
        current = ledger.pre_tax("2026-07", "2026-07", read)
        assert amount(current) == "-1000"
        annual = ledger.pre_tax("2026-01", "2026-07", read)
        assert annual.fields["profit_before_tax"] == {
            "state": "unestablished",
            "issue_indexes": [0],
        }
        assert annual.issues[0]["field"] == "/data/prior_profit"
        assert annual.sources == current.sources


def opening(business_id, period, basis):
    examples = json.loads(
        (Path(__file__).parents[3] / "docs/redesign/examples/core-acceptance.json").read_text(
            encoding="utf-8"
        )
    )
    fact = examples["requests"]["opening_zero"]["mutations"][0]["fact"]
    change = at_period(expense(business_id=business_id), period)
    fact.update(business_id=business_id, period=period)
    fact["data"]["basis"] = basis
    change["businesses"][0]["fact"] = fact
    change["domain"] = "opening"
    plan = change["plans"][0]
    plan.update(calculation_key="opening.package", lines=[], obligations=[])
    plan["target"] = {
        "kind": "opening.package",
        "result_slot": "main",
        "values": {
            "bookkeeping_start": period,
            "debit_fen": "0",
            "credit_fen": "0",
            "member_count": 0,
        },
    }
    plan["basis"].update(kind="opening.package", input=fact["data"])
    return change


@pytest.mark.parametrize("new_company", [False, True])
@pytest.mark.parametrize("store", ["2026-08"], indirect=True)
def test_formal_cutover_profit_is_selected_once_for_exact_ytd_and_quarter(new_company, store):
    ledger = create_ledger()
    changes = [
        at_period(expense("1000"), "2026-08"),
        opening(SECOND, "2026-08", "newly_established" if new_company else "existing_ledger"),
    ]
    if not new_company:
        continuation = at_period(source_only_change("reports.continuation"), "2026-08")
        plan = continuation["plans"][0]
        data = plan["basis"]["input"]
        data.update(bookkeeping_start="2026-08", opening_package_id=SECOND)
        plan["target"]["values"]["bookkeeping_start"] = "2026-08"
        # YTD revenue 5000 − cost 2000 = 3000; QTD revenue 2000 − cost 500 = 1500.
        for row in data["prior_profit"]:
            row["year_to_date_fen"] = str(
                {1: 5000, 2: 2000, 21: 3000, 30: 3000, 32: 3000}.get(row["line"], 0)
            )
            row["quarter_to_date_fen"] = str(
                {1: 2000, 2: 500, 21: 1500, 30: 1500, 32: 1500}.get(row["line"], 0)
            )
        changes.append(continuation)
    adopt(ledger, store, changes)
    with store.read_unit(COMPANY) as read:
        annual = ledger.pre_tax("2026-01", "2026-08", read)
        quarter = ledger.pre_tax("2026-07", "2026-09", read)
        assert amount(annual) == ("-1000" if new_company else "2000")
        assert amount(quarter) == ("-1000" if new_company else "500")
        assert amount(ledger.pre_tax("2026-08", "2026-08", read)) == "-1000"
        assert len(annual.sources) == 2
        if not new_company:
            partial = ledger.pre_tax("2026-06", "2026-08", read)
            assert partial.fields["profit_before_tax"]["state"] == "unestablished"
