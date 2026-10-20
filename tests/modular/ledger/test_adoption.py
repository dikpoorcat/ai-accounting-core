"""Independent numeric plans against real company transactions and DDL."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import MAX_FEN, ContractError, ResourceCutoff
from ai_accounting_modular.ledger.public import LedgerQuery, create_ledger, repository_factory
from ai_accounting_modular.storage.public import (
    CompanySpec,
    OwnerProfile,
    RepositoryFactory,
    Storage,
    create_catalog,
    module_schema,
)

COMPANY = "10000000-0000-4000-8000-000000000001"
DATABASE = "20000000-0000-4000-8000-000000000001"
BUSINESS = "50000000-0000-4000-8000-000000000001"
PAYMENT = "50000000-0000-4000-8000-000000000003"
RECOVERY = "50000000-0000-4000-8000-000000000004"
SUPPLIER = "60000000-0000-4000-8000-000000000001"
BANK = "60000000-0000-4000-8000-000000000003"
MOVEMENT = "90000000-0000-4000-8000-000000000001"
SOURCE = "40000000-0000-4000-8000-000000000001"


class ApplicationSeed:
    """Only synthetic shared identities, not a second application implementation."""

    def __init__(self, session):
        self.session = session

    def business(self, business_id=BUSINESS, *, family="operations", kind="operations.expense"):
        self.session.execute(
            "INSERT INTO business_head VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                business_id,
                family,
                kind,
                None,
                1,
                "active",
                "2026-01",
                "2026-01",
                "0" * 64,
                "create",
                0,
                str(uuid4()),
                "0" * 64,
                1,
            ),
        )


class IdentitySeed:
    def __init__(self, session):
        self.session = session

    def supplier(self):
        self.session.execute(
            "INSERT INTO identity_object VALUES(?,?,?,?,?,?,?,?,?)",
            (
                SUPPLIER,
                "organization",
                "合成供应商",
                "a",
                1,
                "{}",
                "0" * 64,
                0,
                str(uuid4()),
            ),
        )

    def bank(self):
        self.session.execute(
            "INSERT INTO identity_object VALUES(?,?,?,?,?,?,?,?,?)",
            (
                BANK,
                "bank_account",
                "合成账户",
                "b",
                1,
                "{}",
                "0" * 64,
                0,
                str(uuid4()),
            ),
        )


class MoneySeed:
    def __init__(self, session):
        self.session = session

    def occurrence(self):
        self.session.execute(
            "INSERT INTO money_occurrence VALUES(?,?,?)", (MOVEMENT, PAYMENT, "funds")
        )


class SourceSeed:
    def __init__(self, session):
        self.session = session

    def source(self, source_id=SOURCE):
        self.session.execute(
            "INSERT INTO source(source_id,kind,sha256,statement_text) "
            "VALUES(?,'owner_statement',?,'合成核算来源')",
            (source_id, "0" * 64),
        )


@pytest.fixture
def store(tmp_path, request):
    catalog = tmp_path / "catalog.sqlite3"
    create_catalog(catalog, OwnerProfile(str(uuid4()), "合成老板", "synthetic-test-password-hash"))
    storage = Storage(
        catalog,
        repositories=[
            repository_factory(),
            RepositoryFactory(module_schema("application"), ApplicationSeed),
            RepositoryFactory(module_schema("identity"), IdentitySeed),
            RepositoryFactory(module_schema("money"), MoneySeed),
            RepositoryFactory(module_schema("records"), SourceSeed),
        ],
    )
    storage.create_company(
        CompanySpec(
            company_id=COMPANY,
            database_id=DATABASE,
            credit_code="913100000000000001",
            legal_name="合成账务测试公司",
            start_period=getattr(request, "param", "2026-01"),
            company_root=tmp_path / "company",
        )
    )
    with storage.write_unit(COMPANY) as uow:
        uow.repository("application").business()
        uow.repository("identity").supplier()
        uow.repository("records").source()
    return storage


def publication(business_id, period="2026-01", *, slot="main", collection="pre_tax"):
    return {
        "business_id": business_id,
        "result_slot": slot,
        "collection": collection,
        "period_from": period,
        "period_to": period,
    }


def with_resource_uses(changes):
    """Declare synthetic trusted-plan uses; production owners make their own promises."""
    changes = deepcopy(changes)
    for change in changes:
        for plan in change["plans"]:
            for name, relation in (
                ("settlements", "settlement"),
                ("obligation_reclassifications", "settlement"),
                ("cost_consumptions", "cost"),
            ):
                for use in plan[name]:
                    edge = {
                        "consumer_business_id": plan["business_id"],
                        "consumer_result_slot": plan["result_slot"],
                        "producer_kind": "business",
                        "producer_id": use["source"]["business_id"],
                        "producer_public_slot": use["source"]["slot"],
                        "read_mode": "use",
                        "relation_kind": relation,
                        "period_from": plan["accounting_period"],
                        "period_to": plan["accounting_period"],
                    }
                    if edge not in change["dependencies"]:
                        change["dependencies"].append(edge)
    return changes


def adopt_declared(ledger, changes, uow, **kwargs):
    return ledger.adopt(with_resource_uses(changes), uow, **kwargs)


def expense(amount="100000", *, seq=0, business_id=BUSINESS):
    data = {
        "creditor_id": SUPPLIER,
        "creditor_kind": "supplier",
        "amount_fen": amount,
        "expense_class": "administration",
        "report_details": [{"code": "management_other", "amount_fen": amount}],
    }
    fact = {
        "business_id": business_id,
        "kind": "operations.expense",
        "period": "2026-01",
        "evidence_ids": [SOURCE],
        "data": data,
    }
    plan = {
        "business_id": business_id,
        "result_slot": "main",
        "accounting_period": "2026-01",
        "calculation_key": "operations.expense",
        "calculation_revision": 1,
        "rule_refs": [
            {
                "key": "operations.expense",
                "revision": 1,
                "policy_business_ids": [],
                "official_urls": [],
            }
        ],
        "target": {
            "kind": "operations.expense",
            "result_slot": "main",
            "values": {
                "amount_fen": amount,
                "expense_or_income_class": "administration",
                "report_detail_state": "known",
            },
        },
        "basis": {
            "kind": "operations.expense",
            "result_slot": "main",
            "period": "2026-01",
            "input": data,
            "source_results": [],
            "adopted_quantities": [],
            "policies": [],
            "source_locations": [],
        },
        "lines": [
            {
                "line_no": 1,
                "account_code": "5602",
                "debit_fen": amount,
                "credit_fen": "0",
                "summary": "合成管理费用",
            },
            {
                "line_no": 2,
                "account_code": "2202",
                "debit_fen": "0",
                "credit_fen": amount,
                "party_id": SUPPLIER,
                "summary": "合成管理费用",
            },
        ],
        "obligations": [
            {
                "business_id": business_id,
                "slot": "primary",
                "party_id": SUPPLIER,
                "direction": "payable",
                "category": "trade",
                "amount_fen": amount,
                "account_code": "2202",
                "allowed_settlements": ["money", "transfer"],
                "business_period": "2026-01",
                "cashflow_weights": [{"category": "other_operating_payments", "weight": "1"}],
            }
        ],
        "settlements": [],
        "obligation_reclassifications": [],
        "cost_lots": [],
        "cost_consumptions": [],
        "fund_movements": [],
        "agency_proofs": [],
        "tax_sales": [],
        "report_contributions": [],
    }
    return {
        "domain": "operations",
        "publications": [publication(business_id)],
        "businesses": [
            {
                "business_id": business_id,
                "expected_seq": seq,
                "op": "replace" if seq else "create",
                "fact": fact,
                "natural_key": None,
                "complete_result_slots": ["main"],
            }
        ],
        "retained_results": [],
        "plans": [plan],
        "dependencies": [],
        "issues": [],
    }


def result(store, business_id=BUSINESS):
    with store.read_unit(COMPANY) as context:
        return create_ledger().get_results([business_id], context)[0]


def test_open_replace_keeps_identity_and_number_and_removes_old_payload(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as uow:
        assert adopt_declared(ledger, [expense()], uow).changed is True
    before = result(store)
    with store.write_unit(COMPANY) as uow:
        assert adopt_declared(ledger, [expense("90000", seq=1)], uow).changed is True
    after = result(store)
    assert after["reference"]["result_id"] == before["reference"]["result_id"]
    assert after["reference"]["generation"] == 2
    assert after["vouchers"][0]["number"] == before["vouchers"][0]["number"]
    assert after["vouchers"][0]["voucher_id"] == before["vouchers"][0]["voucher_id"]
    assert after["vouchers"][0]["lines"][0]["debit_fen"] == "90000"
    assert "100000" not in json.dumps(after)
    with store.read_unit(COMPANY) as context:
        page = ledger.query_obligations(LedgerQuery((BUSINESS,)), context)
        assert page.items[0]["balance"]["remaining_fen"] == "90000"
        with pytest.raises(ContractError, match="已被替换"):
            ledger.get_results([], context, result_refs=[before["reference"]])
        assert len(context.repository("ledger").current_heads([BUSINESS])) == 1


def test_invalid_plan_rolls_back_entire_transaction(store):
    ledger = create_ledger()
    bad = expense("90000", seq=1)
    bad["plans"][0]["lines"][1]["credit_fen"] = "89999"
    with pytest.raises(ContractError) as error:
        with store.write_unit(COMPANY) as uow:
            adopt_declared(ledger, [expense()], uow)
            adopt_declared(ledger, [bad], uow)
    assert error.value.code == "unbalanced_voucher"
    with store.read_unit(COMPANY) as context:
        assert ledger.get_results([BUSINESS], context) == ()


def test_caught_plan_failure_still_cannot_commit_partial_unit(store):
    ledger = create_ledger()
    invalid = expense("90000", seq=1)
    invalid["plans"][0]["lines"][1]["credit_fen"] = "1"
    with pytest.raises(ContractError) as rejected:
        with store.write_unit(COMPANY) as uow:
            adopt_declared(ledger, [expense()], uow)
            try:
                adopt_declared(ledger, [invalid], uow)
            except ContractError:
                pass
    assert rejected.value.code == "invalid_operation"
    with store.read_unit(COMPANY) as context:
        assert ledger.get_results([BUSINESS], context) == ()


def test_open_withdraw_removes_result_and_resources_without_history(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [expense()], uow)
    withdrawn = {
        "domain": "operations",
        "publications": [],
        "businesses": [
            {
                "business_id": BUSINESS,
                "expected_seq": 1,
                "op": "withdraw",
                "natural_key": None,
                "complete_result_slots": [],
            }
        ],
        "retained_results": [],
        "plans": [],
        "dependencies": [],
        "issues": [],
    }
    with store.write_unit(COMPANY) as uow:
        adopted = adopt_declared(ledger, [withdrawn], uow)
        assert adopted.changed is True
        assert adopted.retired_slots == ({"business_id": BUSINESS, "slot": "main"},)
    with store.write_unit(COMPANY) as uow:
        repeated = adopt_declared(ledger, [withdrawn], uow)
        assert repeated.changed is False
        assert repeated.results == repeated.retired_slots == ()
    with store.read_unit(COMPANY) as context:
        assert ledger.get_results([BUSINESS], context) == ()
        assert ledger.query_vouchers(LedgerQuery((BUSINESS,)), context).total == 0
        assert ledger.query_obligations(LedgerQuery((BUSINESS,)), context).total == 0


def test_full_range_counts_and_cursor_cannot_cross_commits(store):
    ledger = create_ledger()
    second = "50000000-0000-4000-8000-000000000002"
    with store.write_unit(COMPANY) as uow:
        uow.repository("application").business(second)
        adopt_declared(ledger, [expense(), expense("23456", business_id=second)], uow)
    query = LedgerQuery(limit=1)
    with store.read_unit(COMPANY) as context:
        first = ledger.query_obligations(query, context)
        assert first.total == 2 and len(first.items) == 1 and first.next_cursor
        next_page = ledger.query_obligations(replace(query, cursor=first.next_cursor), context)
        assert len(next_page.items) == 1 and next_page.next_cursor is None
        assert (
            first.items[0]["obligation"]["business_id"]
            != next_page.items[0]["obligation"]["business_id"]
        )
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [expense("90000", seq=1)], uow)
    with store.read_unit(COMPANY) as context:
        with pytest.raises(ContractError) as error:
            ledger.query_obligations(replace(query, cursor=first.next_cursor), context)
        assert error.value.code == "snapshot_changed"


def test_vouchers_are_sealed_and_dirty_check_rejects_incomplete_mutation(store):
    ledger = create_ledger()
    with pytest.raises(ContractError, match="未封存"):
        with store.write_unit(COMPANY) as uow:
            adopt_declared(ledger, [expense()], uow)
            repo = uow.repository("ledger")
            voucher = repo.vouchers([repo.current_heads([BUSINESS])[0]["result_id"]])[0]
            repo._session.execute(
                "UPDATE voucher SET sealed=0 WHERE voucher_id=?", (voucher["voucher_id"],)
            )
    with store.read_unit(COMPANY) as context:
        assert ledger.get_results([BUSINESS], context) == ()


def test_no_voucher_result_is_formally_adopted(store):
    # A genuine zero opening package is a formal result, not a zero journal.
    examples = json.loads(
        (Path(__file__).parents[3] / "docs/redesign/examples/core-acceptance.json").read_text(
            encoding="utf-8"
        )
    )
    fact = examples["requests"]["opening_zero"]["mutations"][0]["fact"]
    change = expense()
    plan = change["plans"][0]
    plan.update(
        calculation_key="opening.package",
        target={
            "kind": "opening.package",
            "result_slot": "main",
            "values": {
                "bookkeeping_start": "2026-01",
                "debit_fen": "0",
                "credit_fen": "0",
                "member_count": 0,
            },
        },
        basis={
            "kind": "opening.package",
            "result_slot": "main",
            "period": "2026-01",
            "input": fact["data"],
            "source_results": [],
            "adopted_quantities": [],
            "policies": [],
            "source_locations": [],
        },
        lines=[],
        obligations=[],
    )
    # Synthetic business identity is independent of the zero-package fixture ID.
    fact["business_id"] = BUSINESS
    change["domain"] = "opening"
    change["businesses"][0]["fact"] = fact
    with store.write_unit(COMPANY) as uow:
        assert create_ledger().adopt([change], uow).changed is True
    assert result(store)["vouchers"] == []


def test_frozen_baseline_and_repeated_open_correction_keep_only_final_target(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [expense()], uow)
        # White-box freeze proves adoption only; this does not claim the
        # application close workflow/report snapshot has been implemented.
        uow.repository("ledger").freeze_heads("2026-01")
    original = result(store)
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [expense("90000", seq=1)], uow, correction_period="2026-02")
    correction = result(store)
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [expense("80000", seq=2)], uow, correction_period="2026-02")
    final = result(store)
    assert final["reference"]["baseline_result_id"] == original["reference"]["result_id"]
    assert final["reference"]["result_id"] == correction["reference"]["result_id"]
    assert final["reference"]["generation"] == 2
    assert [v["number"] for v in final["vouchers"]] == [v["number"] for v in correction["vouchers"]]
    assert final["target"]["values"]["amount_fen"] == "80000"
    assert [v["lines"][0]["credit_fen"] for v in final["vouchers"] if v["role"] == "reversal"] == [
        "100000"
    ]
    with store.read_unit(COMPANY, view="as_posted", as_of="2026-02") as context:
        vouchers = ledger.query_vouchers(LedgerQuery((BUSINESS,)), context).items
        assert len(vouchers) == 3
        amounts = {}
        for v in vouchers:
            amounts[v["period"]] = amounts.get(v["period"], 0) + sum(
                int(x["debit_fen"]) - int(x["credit_fen"])
                for x in v["lines"]
                if x["account_code"] == "5602"
            )
        assert amounts == {"2026-01": 100000, "2026-02": -20000}
        frozen = ledger.get_results([], context, result_refs=[original["reference"]])
        assert frozen[0]["target"]["values"]["amount_fen"] == "100000"
    with store.write_unit(COMPANY) as uow:
        uow.repository("ledger").freeze_heads("2026-02")
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [expense("70000", seq=3)], uow, correction_period="2026-03")
    march = result(store)
    assert march["reference"]["baseline_result_id"] == final["reference"]["result_id"]
    reversal = next(v for v in march["vouchers"] if v["role"] == "reversal")
    assert reversal["lines"][0]["credit_fen"] == "80000"


def payment():
    change = expense(business_id=PAYMENT)
    plan = change["plans"][0]
    data = {
        "type": "settlement",
        "funds_account_id": BANK,
        "direction": "outflow",
        "amount_fen": "100000",
        "payment_method": "individual",
        "counterparty_id": SUPPLIER,
        "allocations": [
            {
                "source": {"business_id": BUSINESS, "slot": "primary"},
                "amount_fen": "100000",
                "recipient_id": SUPPLIER,
            }
        ],
    }
    fact = change["businesses"][0]["fact"]
    fact.update(kind="money.movement", effective_on="2026-01-05", data=data)
    change["domain"] = "money"
    change["publications"] = []
    plan.update(
        calculation_key="money.movement",
        target={
            "kind": "money.movement",
            "result_slot": "main",
            "values": {
                "movement_type": "settlement",
                "amount_fen": "100000",
                "settled_fen": "100000",
                "reserve_expense_fen": "0",
            },
        },
        basis={
            "kind": "money.movement",
            "result_slot": "main",
            "period": "2026-01",
            "effective_on": "2026-01-05",
            "input": data,
            "source_results": [],
            "adopted_quantities": [],
            "policies": [],
            "source_locations": [],
        },
        obligations=[],
        lines=[
            {
                "line_no": 1,
                "account_code": "2202",
                "debit_fen": "100000",
                "credit_fen": "0",
                "party_id": SUPPLIER,
                "summary": "真实付费",
            },
            {
                "line_no": 2,
                "account_code": "1002",
                "debit_fen": "0",
                "credit_fen": "100000",
                "object_id": BANK,
                "summary": "真实付费",
            },
        ],
        settlements=[
            {
                "settlement_id": "91000000-0000-4000-8000-000000000001",
                "source": {"business_id": BUSINESS, "slot": "primary"},
                "consumer": {"business_id": PAYMENT, "slot": "funds"},
                "period": "2026-01",
                "amount_fen": "100000",
                "method": "money",
                "movement_id": MOVEMENT,
                "actual_on": "2026-01-05",
            }
        ],
        fund_movements=[
            {
                "movement_id": MOVEMENT,
                "public_slot": "funds",
                "funds_account_id": BANK,
                "actual_on": "2026-01-05",
                "direction": "outflow",
                "amount_fen": "100000",
                "party_id": SUPPLIER,
                "cashflow_parts": [
                    {"category": "other_operating_payments", "amount_fen": "100000"}
                ],
            }
        ],
    )
    return change


def recovery():
    change = expense("20000", business_id=RECOVERY)
    change["publications"] = []
    plan = change["plans"][0]
    data = {
        "source": {"business_id": BUSINESS, "slot": "primary"},
        "counterparty_id": SUPPLIER,
        "amount_fen": "20000",
    }
    change["businesses"][0]["fact"].update(kind="operations.overpayment", data=data)
    plan.update(
        calculation_key="operations.overpayment",
        target={
            "kind": "operations.overpayment",
            "result_slot": "main",
            "values": {
                "amount_fen": "20000",
                "treatment": "overpayment_recovery",
            },
        },
        basis={
            "kind": "operations.overpayment",
            "result_slot": "main",
            "period": "2026-01",
            "input": data,
            "source_results": [],
            "adopted_quantities": [],
            "policies": [],
            "source_locations": [],
        },
        lines=[
            {
                "line_no": 1,
                "account_code": "1221",
                "debit_fen": "20000",
                "credit_fen": "0",
                "party_id": SUPPLIER,
                "summary": "追收多付费用",
            },
            {
                "line_no": 2,
                "account_code": "2202",
                "debit_fen": "0",
                "credit_fen": "20000",
                "party_id": SUPPLIER,
                "summary": "追收多付费用",
            },
        ],
        obligation_reclassifications=[
            {
                "relation_id": "92000000-0000-4000-8000-000000000001",
                "owner_business_id": RECOVERY,
                "source": {"business_id": BUSINESS, "slot": "primary"},
                "target": {"business_id": RECOVERY, "slot": "recovery"},
                "amount_fen": "20000",
                "mode": "reclassify_excess",
            }
        ],
    )
    ob = plan["obligations"][0]
    ob.update(
        slot="recovery",
        direction="receivable",
        category="other",
        account_code="1221",
        allowed_settlements=["money"],
        cashflow_weights=[{"category": "other_operating_payments", "weight": "1"}],
    )
    return change


def seed_payment(store):
    with store.write_unit(COMPANY) as uow:
        uow.repository("application").business(PAYMENT, family="money", kind="money.movement")
        uow.repository("identity").bank()
        uow.repository("money").occurrence()


def test_true_payment_is_preserved_and_explicit_recovery_is_atomic(store):
    ledger = create_ledger()
    seed_payment(store)
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [expense(), payment()], uow)
    with pytest.raises(ContractError) as error:
        with store.write_unit(COMPANY) as uow:
            adopt_declared(ledger, [expense("80000", seq=1)], uow)
    assert error.value.code == "dependency_conflict"
    assert result(store)["target"]["values"]["amount_fen"] == "100000"
    with store.write_unit(COMPANY) as uow:
        uow.repository("application").business(RECOVERY, kind="operations.overpayment")
        adopt_declared(ledger, [expense("80000", seq=1), recovery()], uow)
    with store.read_unit(COMPANY) as context:
        paid, owing = ledger.resolve(
            [
                {"business_id": BUSINESS, "slot": "primary"},
                {"business_id": RECOVERY, "slot": "recovery"},
            ],
            "obligation",
            context,
        )
        assert paid["balance"]["money_settled_fen"] == "100000"
        assert paid["balance"]["excess_reclassified_fen"] == "20000"
        assert paid["balance"]["remaining_fen"] == "0"
        assert owing["balance"]["remaining_fen"] == "20000"
        payment_head = context.repository("ledger").current_heads([PAYMENT])[0]
        moves = context.repository("ledger").children(
            "result_fund_movement", [payment_head["result_id"]]
        )
        assert len(moves) == 1 and moves[0]["movement_id"] == MOVEMENT
        assert moves[0]["amount_fen"] == 100000


def test_duplicate_money_allocation_is_rejected_without_new_cash(store):
    ledger = create_ledger()
    seed_payment(store)
    wrong = payment()
    duplicate = dict(wrong["plans"][0]["settlements"][0])
    duplicate["settlement_id"] = str(uuid4())
    wrong["plans"][0]["settlements"].append(duplicate)
    with pytest.raises(ContractError):
        with store.write_unit(COMPANY) as uow:
            adopt_declared(ledger, [expense("300000"), wrong], uow)
    with store.read_unit(COMPANY) as context:
        assert ledger.get_results([BUSINESS, PAYMENT], context) == ()


def test_prepared_identity_and_source_are_the_same_before_and_after_adoption(store):
    ledger = create_ledger()
    first = expense()
    with store.write_unit(COMPANY) as uow:
        prepared = ledger.prepare_result(BUSINESS, "main", uow)
        assert ledger.prepare_result(BUSINESS, "main", uow) is prepared
        staged_source = ledger.get_result_source(BUSINESS, "main", uow, plan=first["plans"][0])
        adopted = adopt_declared(ledger, [first], uow)
        assert adopted.results[0]["result_id"] == prepared.result_id
        assert adopted.results[0]["generation"] == prepared.generation == 1
        assert ledger.get_result_source(BUSINESS, "main", uow) == staged_source
    with store.write_unit(COMPANY) as uow:
        changed = expense("90000", seq=1)
        next_identity = ledger.prepare_result(BUSINESS, "main", uow)
        assert next_identity.result_id == prepared.result_id
        assert next_identity.generation == 2
        changed_source = ledger.get_result_source(BUSINESS, "main", uow, plan=changed["plans"][0])
        assert changed_source["content_digest"] != staged_source["content_digest"]
        adopt_declared(ledger, [changed], uow)
        assert ledger.get_result_source(BUSINESS, "main", uow) == changed_source


def test_source_changes_when_basis_changes_even_if_formal_amount_does_not(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [expense()], uow)
        old = ledger.get_result_source(BUSINESS, "main", uow)
    changed = expense(seq=1)
    # A classified management detail changes its basis, not its amount or account.
    changed["plans"][0]["basis"]["input"]["report_details"] = [
        {"code": "management_research", "amount_fen": "100000"}
    ]
    with store.write_unit(COMPANY) as uow:
        staged = ledger.get_result_source(BUSINESS, "main", uow, plan=changed["plans"][0])
        adopt_declared(ledger, [changed], uow)
        assert ledger.get_result_source(BUSINESS, "main", uow) == staged
        assert staged["content_digest"] != old["content_digest"]


def test_prepared_discovery_period_cannot_be_changed_silently(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [expense()], uow)
        uow.repository("ledger").freeze_heads("2026-01")
    with pytest.raises(ContractError) as rejected:
        with store.write_unit(COMPANY) as uow:
            ledger.prepare_result(BUSINESS, "main", uow, correction_period="2026-02")
            adopt_declared(ledger, [expense("90000", seq=1)], uow, correction_period="2026-03")
    assert rejected.value.code == "conflict"
    assert result(store)["target"]["values"]["amount_fen"] == "100000"


def test_plan_cannot_change_after_its_prepared_source_has_been_consumed(store):
    ledger = create_ledger()
    with pytest.raises(ContractError) as rejected:
        with store.write_unit(COMPANY) as uow:
            original = expense()
            ledger.get_result_source(BUSINESS, "main", uow, plan=original["plans"][0])
            adopt_declared(ledger, [expense("90000")], uow)
    assert rejected.value.code == "conflict"
    with store.read_unit(COMPANY) as context:
        assert ledger.get_results([BUSINESS], context) == ()


def refund(source_ref, *, amount="20000", seq=0):
    """A real refund right uses the original expense's adopted classification."""
    change = expense(amount, seq=seq, business_id=RECOVERY)
    data = {"expense_id": BUSINESS, "counterparty_id": SUPPLIER, "amount_fen": amount}
    fact = change["businesses"][0]["fact"]
    fact.update(kind="operations.expense_recovery", data=data)
    plan = change["plans"][0]
    plan["calculation_key"] = "operations.expense_recovery"
    plan["target"]["kind"] = "operations.expense_recovery"
    plan["basis"].update(
        kind="operations.expense_recovery",
        input=data,
        source_results=[source_ref],
        adopted_quantities=[
            {
                "type": "expense",
                "source": {"business_id": BUSINESS, "slot": "main"},
                "amount_fen": "100000",
                "expense_class": "administration",
                "report_detail_state": "known",
                "report_details": [{"code": "management_other", "amount_fen": "100000"}],
            }
        ],
    )
    plan["lines"][0].update(account_code="1221", party_id=SUPPLIER)
    plan["lines"][1]["account_code"] = "5602"
    ob = plan["obligations"][0]
    ob.update(
        direction="receivable", category="other", account_code="1221", allowed_settlements=["money"]
    )
    return change


def test_consumer_basis_uses_the_same_new_producer_identity_after_adoption(store):
    ledger = create_ledger()
    original = expense()
    with store.write_unit(COMPANY) as uow:
        uow.repository("application").business(RECOVERY, kind="operations.expense_recovery")
        source = ledger.get_result_source(BUSINESS, "main", uow, plan=original["plans"][0])
        consumer = refund(source)
        # Persistence order does not require the consumer to create a fake ref.
        adopt_declared(ledger, [consumer, original], uow)
        assert ledger.get_result_source(BUSINESS, "main", uow) == source
    assert result(store, RECOVERY)["basis"]["source_results"] == [source]
    assert result(store, RECOVERY)["basis"]["adopted_quantities"][0]["amount_fen"] == "100000"


def test_consumer_cannot_use_persisted_old_head_when_producer_is_in_same_batch(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [expense()], uow)
        source = ledger.get_result_source(BUSINESS, "main", uow)
    with pytest.raises(ContractError) as rejected:
        with store.write_unit(COMPANY) as uow:
            uow.repository("application").business(RECOVERY, kind="operations.expense_recovery")
            adopt_declared(ledger, [expense("90000", seq=1), refund(source)], uow)
    assert rejected.value.code == "dependency_conflict"
    assert result(store)["reference"]["generation"] == 1
    with store.read_unit(COMPANY) as context:
        assert ledger.get_results([RECOVERY], context) == ()


@pytest.mark.parametrize("changed", ["generation", "digest", "identity"])
def test_consumer_requires_a_valid_persisted_source(changed, store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [expense()], uow)
        source = ledger.get_result_source(BUSINESS, "main", uow)
    if changed == "generation":
        source["result_generation"] += 1
    elif changed == "digest":
        source["content_digest"] = "f" * 64
    else:
        source["result_id"] = str(uuid4())
    with pytest.raises(ContractError) as rejected:
        with store.write_unit(COMPANY) as uow:
            uow.repository("application").business(RECOVERY, kind="operations.expense_recovery")
            adopt_declared(ledger, [refund(source)], uow)
    assert rejected.value.code == "dependency_conflict"


def test_actual_cash_identity_is_unique_even_without_settlements(store):
    ledger = create_ledger()
    seed_payment(store)
    first = payment()
    data = {
        "type": "managed_reserve",
        "funds_account_id": BANK,
        "direction": "outflow",
        "amount_fen": "100000",
    }
    plan = first["plans"][0]
    first["businesses"][0]["fact"]["data"] = data
    plan["basis"]["input"] = data
    plan["target"]["values"].update(
        movement_type="managed_reserve", settled_fen="0", reserve_expense_fen="100000"
    )
    plan["settlements"] = []
    plan["lines"][0]["account_code"] = "5602"
    first["publications"] = [publication(PAYMENT)]
    second = deepcopy(first)
    second_id = "50000000-0000-4000-8000-000000000099"
    second["businesses"][0]["business_id"] = second_id
    second["businesses"][0]["fact"]["business_id"] = second_id
    second["plans"][0]["business_id"] = second_id
    second["publications"] = [publication(second_id)]
    with pytest.raises(ContractError) as rejected:
        with store.write_unit(COMPANY) as uow:
            uow.repository("application").business(second_id, family="money", kind="money.movement")
            adopt_declared(ledger, [first, second], uow)
    assert rejected.value.code == "dependency_conflict"
    with store.read_unit(COMPANY) as context:
        assert ledger.get_results([PAYMENT, second_id], context) == ()


def test_new_consumer_cannot_reference_a_source_withdrawn_in_same_unit(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [expense()], uow)
        source = ledger.get_result_source(BUSINESS, "main", uow)
    withdraw = {
        "domain": "operations",
        "publications": [],
        "businesses": [
            {
                "business_id": BUSINESS,
                "expected_seq": 1,
                "op": "withdraw",
                "natural_key": None,
                "complete_result_slots": [],
            }
        ],
        "retained_results": [],
        "plans": [],
        "dependencies": [],
        "issues": [],
    }
    with pytest.raises(ContractError) as rejected:
        with store.write_unit(COMPANY) as uow:
            uow.repository("application").business(RECOVERY, kind="operations.expense_recovery")
            adopt_declared(ledger, [withdraw, refund(source)], uow)
    assert rejected.value.code == "dependency_conflict"
    assert result(store)["reference"]["generation"] == 1
    with store.read_unit(COMPANY) as context:
        assert ledger.get_results([RECOVERY], context) == ()


def test_voucher_month_is_posting_month_for_current_and_as_posted_views(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [expense()], uow)
        uow.repository("ledger").freeze_heads("2026-01")
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [expense("90000", seq=1)], uow, correction_period="2026-02")
    for view, as_of in (("current", None), ("as_posted", "2026-02")):
        with store.read_unit(COMPANY, view=view, as_of=as_of) as context:
            january = ledger.query_vouchers(
                LedgerQuery(period_from="2026-01", period_to="2026-01"), context
            )
            february = ledger.query_vouchers(
                LedgerQuery(period_from="2026-02", period_to="2026-02"), context
            )
            assert january.total == 1
            assert february.total == 2
            assert {item["period"] for item in february.items} == {"2026-02"}


def test_resolve_without_references_does_not_query_company_resources(store):
    class NoRepositoryContext:
        def repository(self, owner):
            raise AssertionError("empty references must not read the company")

    assert create_ledger().resolve([], "obligation", NoRepositoryContext()) == ()


@pytest.mark.parametrize("supplement_source", [False, True])
def test_frozen_no_impact_recalculation_reuses_head_source_and_voucher_without_writes(
    supplement_source, store
):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [expense()], uow)
        uow.repository("ledger").freeze_heads("2026-01")
    before = result(store)
    with store.read_unit(COMPANY) as context:
        head = context.head
        old_source = ledger.get_result_source(BUSINESS, "main", context)
    repeated = expense(seq=1)
    repeated["businesses"][0]["op"] = "recalculate"
    if supplement_source:
        repeated["plans"][0]["basis"]["source_locations"] = [
            {"source_id": SOURCE, "location": "合成原件第1页"}
        ]
    with store.write_unit(COMPANY) as uow:
        ledger.prepare_result(BUSINESS, "main", uow, correction_period="2026-02")
        planned_source = ledger.get_result_source(BUSINESS, "main", uow, plan=repeated["plans"][0])
        assert planned_source == old_source
        adopted = adopt_declared(ledger, [repeated], uow, correction_period="2026-02")
        assert adopted.changed is False
        assert adopted.results == (before["reference"],)
    assert result(store) == before
    with store.read_unit(COMPANY) as context:
        assert context.head == head
        assert ledger.query_vouchers(LedgerQuery(), context).total == 1
        assert len(context.repository("ledger").formal_heads_through(None)) == 1


def test_non_accounting_date_supplement_to_frozen_fact_does_not_create_correction(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [expense()], uow)
        uow.repository("ledger").freeze_heads("2026-01")
    before = result(store)
    supplement = expense(seq=1)
    supplement["businesses"][0]["fact"]["effective_on"] = "2026-01-08"
    # A domain copies a date into Basis only when accounting actually uses it.
    # This expense's optional management date stays in the current Fact.
    with store.write_unit(COMPANY) as uow:
        staged = ledger.get_result_source(BUSINESS, "main", uow, plan=supplement["plans"][0])
        assert staged["result_id"] == before["reference"]["result_id"]
        adopt_declared(ledger, [supplement], uow)
    # The caller owns the supplemented current fact; frozen accounting evidence
    # remains the actual original adoption, with no review diary or new journal.
    assert result(store) == before


def test_no_impact_frozen_producer_can_be_used_by_new_consumer_in_same_unit(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [expense()], uow)
        uow.repository("ledger").freeze_heads("2026-01")
    before = result(store)
    repeated = expense(seq=1)
    repeated["businesses"][0]["op"] = "recalculate"
    with store.write_unit(COMPANY) as uow:
        uow.repository("application").business(RECOVERY, kind="operations.expense_recovery")
        source = ledger.get_result_source(BUSINESS, "main", uow, plan=repeated["plans"][0])
        adopted = adopt_declared(ledger, [refund(source), repeated], uow)
        assert adopted.changed is True
        assert len(adopted.results) == 2
    assert result(store) == before
    assert result(store, RECOVERY)["basis"]["source_results"] == [source]


def test_same_amount_with_changed_formal_classification_is_not_no_impact(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [expense()], uow)
        uow.repository("ledger").freeze_heads("2026-01")
    before = result(store)
    changed = expense(seq=1)
    changed["plans"][0]["target"]["values"]["report_detail_state"] = "unestablished"
    changed["plans"][0]["basis"]["input"].pop("report_details")
    with store.write_unit(COMPANY) as uow:
        staged = ledger.get_result_source(BUSINESS, "main", uow, plan=changed["plans"][0])
        assert staged["result_id"] != before["reference"]["result_id"]
        assert adopt_declared(ledger, [changed], uow, correction_period="2026-02").changed is True
    after = result(store)
    assert after["reference"]["result_id"] == staged["result_id"]
    assert after["target"]["values"]["amount_fen"] == "100000"
    assert after["target"]["values"]["report_detail_state"] == "unestablished"
    assert after["reference"]["baseline_result_id"] == before["reference"]["result_id"]
    assert after["vouchers"] == []
    with store.read_unit(COMPANY) as context:
        assert ledger.query_vouchers(LedgerQuery(), context).total == 1


def test_later_adoption_in_same_unit_cannot_leave_a_new_consumer_dangling(store):
    ledger = create_ledger()
    withdraw = {
        "domain": "operations",
        "publications": [],
        "businesses": [
            {
                "business_id": BUSINESS,
                "expected_seq": 1,
                "op": "withdraw",
                "natural_key": None,
                "complete_result_slots": [],
            }
        ],
        "retained_results": [],
        "plans": [],
        "dependencies": [],
        "issues": [],
    }
    with pytest.raises(ContractError) as rejected:
        with store.write_unit(COMPANY) as uow:
            uow.repository("application").business(RECOVERY, kind="operations.expense_recovery")
            original = expense()
            source = ledger.get_result_source(BUSINESS, "main", uow, plan=original["plans"][0])
            adopt_declared(ledger, [original, refund(source)], uow)
            adopt_declared(ledger, [withdraw], uow)
    assert rejected.value.code == "dependency_conflict"
    with store.read_unit(COMPANY) as context:
        assert ledger.get_results([BUSINESS, RECOVERY], context) == ()


@pytest.mark.parametrize("collection", ["allowed_settlements", "cashflow_weights"])
def test_unordered_resource_composition_does_not_create_frozen_correction(collection, store):
    ledger = create_ledger()
    original = expense()
    obligation = original["plans"][0]["obligations"][0]
    if collection == "cashflow_weights":
        # General obligation composition is not an ordered sequence. Each
        # domain separately verifies which categories its business may use.
        obligation[collection] = [
            {"category": "other_operating_payments", "weight": "3"},
            {"category": "materials_services_payments", "weight": "2"},
        ]
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [original], uow)
        uow.repository("ledger").freeze_heads("2026-01")
    before = result(store)
    reordered = deepcopy(original)
    reordered["businesses"][0].update(expected_seq=1, op="recalculate")
    reordered["plans"][0]["obligations"][0][collection].reverse()
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [reordered], uow, correction_period="2026-02")
    assert result(store) == before
    with store.read_unit(COMPANY) as context:
        assert ledger.query_vouchers(LedgerQuery(), context).total == 1


def test_obligation_classification_has_one_weight_per_category(store):
    change = expense()
    change["plans"][0]["obligations"][0]["cashflow_weights"] = [
        {"category": "employee_payments", "weight": "1"},
        {"category": "employee_payments", "weight": "2"},
    ]
    with pytest.raises(ContractError, match="类别不能重复"):
        with store.write_unit(COMPANY) as uow:
            create_ledger().adopt([change], uow)
    with store.read_unit(COMPANY) as read:
        assert create_ledger().get_results([BUSINESS], read) == ()


def withdrawal(baseline, *, seq=1):
    withdrawn = expense(seq=seq)
    withdrawn["businesses"][0].pop("fact")
    withdrawn["businesses"][0]["op"] = "withdraw"
    plan = withdrawn["plans"][0]
    plan.update(
        calculation_key="system.withdrawal",
        target={
            "kind": "system.withdrawal",
            "business_kind": "operations.expense",
            "result_slot": "main",
            "state": "withdrawn",
        },
        basis={
            "kind": "system.withdrawal",
            "result_slot": "main",
            "period": "2026-01",
            "baseline_ref": baseline,
            "explanation": "合成资料确认原来没有该笔费用",
            "source_locations": [],
        },
        lines=[],
        obligations=[],
    )
    return withdrawn


def test_frozen_withdrawal_uses_exact_baseline_and_can_be_frozen_in_same_unit(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [expense()], uow)
        uow.repository("ledger").freeze_heads("2026-01")
        baseline = ledger.get_result_source(BUSINESS, "main", uow)
    withdrawn = withdrawal(baseline)
    with store.write_unit(COMPANY) as uow:
        assert adopt_declared(ledger, [withdrawn], uow, correction_period="2026-02").changed is True
        uow.repository("ledger").freeze_heads("2026-02")
    current = result(store)
    assert current["target"]["state"] == "withdrawn"
    assert [v["role"] for v in current["vouchers"]] == ["reversal"]
    assert current["basis"]["baseline_ref"] == baseline
    with store.read_unit(COMPANY) as context:
        assert ledger.query_vouchers(LedgerQuery(), context).total == 2
        assert ledger.query_obligations(LedgerQuery(), context).total == 0


def source_only_change(kind, *, seq=0):
    """Concrete source-valued plans whose Targets intentionally summarize input."""
    business_id = "50000000-0000-4000-8000-000000000090"
    change = expense(seq=seq, business_id=business_id)
    if kind == "taxes.surtax_policy":
        identity = {
            "version": "synthetic-1",
            "official_url": "https://www.chinatax.gov.cn/",
            "effective_from": "2026-01-01",
        }
        data = {
            "policy": identity,
            "urban_rate": "0.07",
            "education_rate": "0.03",
            "local_education_rate": "0.02",
            "payable_fraction": "0.5",
        }
        values = {
            "policy_version": identity["version"],
            "effective_from": identity["effective_from"],
            "official_url": identity["official_url"],
        }
    else:
        data = {
            "accounting_standard": "small_enterprise",
            "bookkeeping_start": "2026-01",
            "opening_package_id": str(uuid4()),
            "year_beginning_balance": [{"line": n, "beginning_fen": "0"} for n in range(1, 54)],
            "prior_profit": [
                {"line": n, "quarter_to_date_fen": "0", "year_to_date_fen": "0"}
                for n in range(1, 33)
            ],
            "prior_cash": [
                {"line": n, "quarter_to_date_fen": "0", "year_to_date_fen": "0"}
                for n in range(1, 23)
            ],
        }
        values = {
            "bookkeeping_start": "2026-01",
            "accounting_standard": "small_enterprise",
            "balance_row_count": 53,
            "profit_row_count": 32,
            "cash_row_count": 22,
        }
    change["domain"] = kind.split(".")[0]
    change["businesses"][0]["fact"].update(kind=kind, data=data)
    plan = change["plans"][0]
    plan.update(
        calculation_key=kind,
        rule_refs=[{"key": kind, "revision": 1, "policy_business_ids": [], "official_urls": []}],
        target={"kind": kind, "result_slot": "main", "values": values},
        lines=[],
        obligations=[],
    )
    plan["basis"].update(kind=kind, input=data)
    return change


@pytest.mark.parametrize("kind", ["taxes.surtax_policy", "reports.continuation"])
def test_source_values_changed_behind_same_target_are_new_formal_heads_without_vouchers(
    kind, store
):
    ledger = create_ledger()
    original = source_only_change(kind)
    business_id = original["businesses"][0]["business_id"]
    with store.write_unit(COMPANY) as uow:
        uow.repository("application").business(business_id, family=original["domain"], kind=kind)
        adopt_declared(ledger, [original], uow)
        uow.repository("ledger").freeze_heads("2026-01")
    before = result(store, business_id)
    changed = deepcopy(original)
    changed["businesses"][0].update(expected_seq=1, op="replace")
    data = changed["plans"][0]["basis"]["input"]
    if kind == "taxes.surtax_policy":
        data["urban_rate"] = "0.05"
    else:
        data["prior_profit"][0].update(quarter_to_date_fen="1000", year_to_date_fen="1000")
    with store.write_unit(COMPANY) as uow:
        ledger.prepare_result(business_id, "main", uow, correction_period="2026-02")
        staged = ledger.get_result_source(business_id, "main", uow, plan=changed["plans"][0])
        assert adopt_declared(ledger, [changed], uow, correction_period="2026-02").changed is True
        assert ledger.get_result_source(business_id, "main", uow) == staged
    after = result(store, business_id)
    assert after["target"] == before["target"]
    assert after["basis"]["input"] == data
    assert after["reference"]["result_id"] != before["reference"]["result_id"]
    assert after["reference"]["baseline_result_id"] == before["reference"]["result_id"]
    assert after["vouchers"] == []
    with store.read_unit(COMPANY) as context:
        assert ledger.query_vouchers(LedgerQuery((business_id,)), context).total == 0


def test_formal_only_corrections_inherit_effective_journal_across_frozen_heads(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [expense()], uow)
        uow.repository("ledger").freeze_heads("2026-01")
    original = result(store)
    formal = expense(seq=1)
    formal["plans"][0]["obligations"][0]["allowed_settlements"] = ["money"]
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [formal], uow, correction_period="2026-02")
        uow.repository("ledger").freeze_heads("2026-02")
    inherited = result(store)
    assert inherited["vouchers"] == []
    assert inherited["reference"]["result_id"] != original["reference"]["result_id"]
    changed = expense("90000", seq=2)
    changed["plans"][0]["obligations"][0]["allowed_settlements"] = ["money"]
    with store.write_unit(COMPANY) as uow:
        staged = ledger.get_result_source(BUSINESS, "main", uow, plan=changed["plans"][0])
        adopt_declared(ledger, [changed], uow, correction_period="2026-03")
        assert ledger.get_result_source(BUSINESS, "main", uow) == staged
        uow.repository("ledger").freeze_heads("2026-03")
    march = result(store)
    assert march["reference"]["baseline_result_id"] == inherited["reference"]["result_id"]
    reversal = next(v for v in march["vouchers"] if v["role"] == "reversal")
    assert reversal["reverses_voucher_id"] == original["vouchers"][0]["voucher_id"]
    assert reversal["lines"][0]["credit_fen"] == "100000"
    replacement = next(v for v in march["vouchers"] if v["role"] == "replacement")
    assert replacement["lines"][0]["debit_fen"] == "90000"

    april = deepcopy(changed)
    april["businesses"][0]["expected_seq"] = 3
    april["plans"][0]["basis"]["input"]["report_details"][0]["code"] = "management_startup"
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [april], uow, correction_period="2026-04")
        uow.repository("ledger").freeze_heads("2026-04")
    assert result(store)["vouchers"] == []
    may = expense("80000", seq=4)
    may["plans"][0]["obligations"][0]["allowed_settlements"] = ["money"]
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [may], uow, correction_period="2026-05")
    after = result(store)
    reversal = next(v for v in after["vouchers"] if v["role"] == "reversal")
    assert reversal["reverses_voucher_id"] == replacement["voucher_id"]
    assert reversal["lines"][0]["credit_fen"] == "90000"
    assert (
        next(v for v in after["vouchers"] if v["role"] == "replacement")["lines"][0]["debit_fen"]
        == "80000"
    )
    with store.read_unit(COMPANY) as context:
        assert ledger.query_vouchers(LedgerQuery(), context).total == 5


def test_open_correction_can_return_to_baseline_then_withdraw_and_restore_without_double_reversal(
    store,
):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [expense()], uow)
        uow.repository("ledger").freeze_heads("2026-01")
    original = result(store)
    changed = expense("90000", seq=1)
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [changed], uow, correction_period="2026-02")
    correcting = result(store)
    returned = expense(seq=2)
    returned["plans"][0]["obligations"][0]["allowed_settlements"] = ["money"]
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [returned], uow)
        uow.repository("ledger").freeze_heads("2026-02")
        baseline = ledger.get_result_source(BUSINESS, "main", uow)
    inherited = result(store)
    assert inherited["reference"]["result_id"] == correcting["reference"]["result_id"]
    assert inherited["reference"]["generation"] == 2
    assert inherited["vouchers"] == []
    with store.read_unit(COMPANY) as context:
        assert ledger.query_vouchers(LedgerQuery(), context).total == 1
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [withdrawal(baseline, seq=3)], uow, correction_period="2026-03")
        uow.repository("ledger").freeze_heads("2026-03")
    withdrawn = result(store)
    assert [v["role"] for v in withdrawn["vouchers"]] == ["reversal"]
    assert withdrawn["vouchers"][0]["reverses_voucher_id"] == original["vouchers"][0]["voucher_id"]
    assert withdrawn["vouchers"][0]["lines"][0]["credit_fen"] == "100000"
    restored = expense("80000", seq=4)
    restored["businesses"][0]["op"] = "restore"
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [restored], uow, correction_period="2026-04")
    after = result(store)
    assert [v["role"] for v in after["vouchers"]] == ["replacement"]
    assert after["vouchers"][0]["lines"][0]["debit_fen"] == "80000"
    with store.read_unit(COMPANY) as context:
        assert ledger.query_vouchers(LedgerQuery(), context).total == 3


@pytest.mark.parametrize("quantity_changes", [False, True])
def test_frozen_consumer_compares_adopted_values_separately_from_source_locators(
    quantity_changes, store
):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [expense()], uow)
        uow.repository("ledger").freeze_heads("2026-01")
        original_source = ledger.get_result_source(BUSINESS, "main", uow)
    consumer = refund(original_source)
    consumer["businesses"][0]["fact"]["period"] = "2026-02"
    consumer["plans"][0]["accounting_period"] = "2026-02"
    consumer["publications"] = [publication(RECOVERY, "2026-02")]
    consumer["plans"][0]["basis"]["period"] = "2026-02"
    consumer["plans"][0]["obligations"][0]["business_period"] = "2026-02"
    with store.write_unit(COMPANY) as uow:
        uow.repository("application").business(RECOVERY, kind="operations.expense_recovery")
        adopt_declared(ledger, [consumer], uow)
        uow.repository("ledger").freeze_heads("2026-02")
        old_consumer_source = ledger.get_result_source(RECOVERY, "main", uow)
    before = result(store, RECOVERY)
    producer = expense("120000" if quantity_changes else "100000", seq=1)
    producer["plans"][0]["obligations"][0]["allowed_settlements"] = ["money"]
    recalculated = deepcopy(consumer)
    recalculated["businesses"][0].update(expected_seq=1, op="recalculate")
    with store.write_unit(COMPANY) as uow:
        new_source = ledger.get_result_source(BUSINESS, "main", uow, plan=producer["plans"][0])
        assert new_source["result_id"] != original_source["result_id"]
        recalculated["plans"][0]["basis"]["source_results"] = [new_source]
        if quantity_changes:
            quantity = recalculated["plans"][0]["basis"]["adopted_quantities"][0]
            quantity["amount_fen"] = "120000"
            quantity["report_details"][0]["amount_fen"] = "120000"
        staged = ledger.get_result_source(RECOVERY, "main", uow, plan=recalculated["plans"][0])
        assert (staged != old_consumer_source) is quantity_changes
        adopt_declared(ledger, [recalculated, producer], uow, correction_period="2026-03")
        assert ledger.get_result_source(RECOVERY, "main", uow) == staged
    after = result(store, RECOVERY)
    if quantity_changes:
        assert after["target"] == before["target"]
        assert after["reference"]["baseline_result_id"] == before["reference"]["result_id"]
        assert after["basis"]["adopted_quantities"][0]["amount_fen"] == "120000"
        assert after["vouchers"] == []
    else:
        assert after == before
    assert before["basis"]["source_results"] == [original_source]
    assert len(result(store)["vouchers"]) == (2 if quantity_changes else 0)
    with store.read_unit(COMPANY) as context:
        assert ledger.query_vouchers(LedgerQuery(), context).total == (4 if quantity_changes else 2)


def test_no_responsibility_has_no_formal_change(store):
    with store.write_unit(COMPANY) as uow:
        adopted = create_ledger().adopt([], uow)
        assert adopted.changed is False
        assert adopted.results == adopted.retired_slots == ()


def test_open_fact_source_supplement_and_reference_seq_sync_reuse_formal_result(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as uow:
        assert adopt_declared(ledger, [expense()], uow).changed is True
    before = result(store)
    supplemented = expense(seq=1)
    extra_source = str(uuid4())
    supplemented["businesses"][0]["fact"]["evidence_ids"].append(extra_source)
    # The source is extra current business information. The accounting program
    # still uses the original unchanged necessary Basis and formal output.
    with store.write_unit(COMPANY) as uow:
        uow.repository("records").source(extra_source)
        adopted = adopt_declared(ledger, [supplemented], uow)
        assert adopted.changed is False
        assert adopted.results == (before["reference"],)
        assert adopted.retired_slots == ()
        assert (
            uow.repository("ledger").result(before["reference"]["result_id"])["adopted_fact_seq"]
            == 2
        )
    assert result(store) == before


def test_open_necessary_basis_locator_update_is_a_real_adoption_with_same_voucher(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [expense()], uow)
    before = result(store)
    supplemented = expense(seq=1)
    supplemented["plans"][0]["basis"]["source_locations"] = [
        {"source_id": SOURCE, "location": "必要来源的明确原件位置"}
    ]
    with store.write_unit(COMPANY) as uow:
        adopted = adopt_declared(ledger, [supplemented], uow)
        assert adopted.changed is True
    after = result(store)
    assert after["reference"]["result_id"] == before["reference"]["result_id"]
    assert after["reference"]["generation"] == 2
    for field in ("voucher_id", "number", "lines"):
        assert after["vouchers"][0][field] == before["vouchers"][0][field]


@pytest.mark.parametrize("frozen", [False, True])
def test_real_object_rename_recalculation_reuses_complete_formal_result(frozen, store, tmp_path):
    from ai_accounting_modular.identity.public import (
        IdentityContext,
        create_identity,
    )
    from ai_accounting_modular.identity.public import repository_factory as identity_factory

    # A second handle uses the actual identity owner on the same synthetic
    # company. Only this new well-formed object is read by that owner.
    storage = Storage(
        tmp_path / "catalog.sqlite3",
        repositories=[repository_factory(), identity_factory()],
    )
    identity, ledger = create_identity(), create_ledger()
    supplier = {
        "object_id": str(uuid4()),
        "kind": "organization",
        "name": "改名前的合成供应商",
        "data": {},
        "expected_seq": 0,
    }
    change = expense()
    plan = change["plans"][0]
    change["businesses"][0]["fact"]["data"]["creditor_id"] = supplier["object_id"]
    plan["lines"][1]["party_id"] = supplier["object_id"]
    plan["obligations"][0]["party_id"] = supplier["object_id"]
    with storage.write_unit(COMPANY) as uow:
        initial = identity.plan_change([supplier], IdentityContext(uow, str(uuid4())))
        identity.apply_current(initial, uow)
        assert adopt_declared(ledger, [change], uow).changed is True
        if frozen:
            uow.repository("ledger").freeze_heads("2026-01")
    before = result(storage)
    renamed = {**supplier, "name": "改名后的合成供应商", "expected_seq": 1}
    repeated = deepcopy(change)
    repeated["businesses"][0].update(expected_seq=1, op="recalculate")
    with storage.write_unit(COMPANY) as uow:
        name_plan = identity.plan_change([renamed], IdentityContext(uow, str(uuid4())))
        assert name_plan.changed_ids == (supplier["object_id"],)
        identity.apply_current(name_plan, uow)
        adopted = adopt_declared(ledger, [repeated], uow)
        assert adopted.changed is False
        assert adopted.results == (before["reference"],)
        assert adopted.retired_slots == ()
    assert result(storage) == before
    with storage.read_unit(COMPANY) as context:
        current = identity.get_batch([supplier["object_id"]], context)[0]
        assert current["name"] == renamed["name"]
        assert current["seq"] == 2


# Candidate queries deliberately use legal plans with independently specified
# expected quantities; they do not write temporary accounting results.


def _candidate(ledger, uow, changes, *, reverse=False, correction_period=None):
    candidate = ledger.begin_candidate(
        [responsibility for change in changes for responsibility in change["businesses"]],
        uow,
        correction_period=correction_period,
        publications=[publication for change in changes for publication in change["publications"]],
    )
    plans = [plan for change in changes for plan in change["plans"]]
    for plan in reversed(plans) if reverse else plans:
        candidate.stage(plan)
    return candidate.complete()


def _payment_at(
    amount="100000",
    *,
    period="2026-01",
    day="05",
    seq=0,
    business_id=PAYMENT,
    movement_id=MOVEMENT,
):
    change = payment()
    responsibility, plan = change["businesses"][0], change["plans"][0]
    responsibility.update(op="replace" if seq else "create", expected_seq=seq)
    fact = responsibility["fact"]
    responsibility["business_id"] = fact["business_id"] = plan["business_id"] = business_id
    fact.update(period=period, effective_on=f"{period}-{day}")
    fact["data"]["amount_fen"] = amount
    fact["data"]["allocations"][0]["amount_fen"] = amount
    plan["accounting_period"] = period
    plan["basis"].update(period=period, effective_on=fact["effective_on"])
    plan["target"]["values"].update(amount_fen=amount, settled_fen=amount)
    for row in plan["lines"]:
        row["debit_fen"] = amount if row["debit_fen"] != "0" else "0"
        row["credit_fen"] = amount if row["credit_fen"] != "0" else "0"
    plan["settlements"][0].update(period=period, actual_on=fact["effective_on"], amount_fen=amount)
    plan["settlements"][0]["consumer"]["business_id"] = business_id
    plan["settlements"][0]["movement_id"] = movement_id
    plan["settlements"][0]["settlement_id"] = str(uuid4())
    plan["fund_movements"][0]["movement_id"] = movement_id
    plan["fund_movements"][0].update(actual_on=fact["effective_on"], amount_fen=amount)
    plan["fund_movements"][0]["cashflow_parts"][0]["amount_fen"] = amount
    return change


def test_candidate_definitions_hide_replaced_old_slots_and_require_complete_set(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [expense()], uow)
    replacement = expense("80000", seq=1)
    with store.write_unit(COMPANY) as uow:
        candidate = ledger.begin_candidate(
            replacement["businesses"], uow, publications=replacement["publications"]
        )
        ref = {"business_id": BUSINESS, "slot": "primary"}
        with pytest.raises(ContractError, match="尚未完成"):
            candidate.resource_definitions([ref], "obligation")
        with pytest.raises(ContractError, match="全部"):
            candidate.complete()
        candidate.stage(replacement["plans"][0])
        definition = candidate.resource_definitions([ref], "obligation")[0]
        assert definition.value["amount_fen"] == "80000"
        assert definition.source == ledger.get_result_source(
            BUSINESS, "main", uow, plan=replacement["plans"][0]
        )
        with pytest.raises(ContractError, match="不完整余额"):
            candidate.resource_states([ref], "obligation", ResourceCutoff("2026-01"))
        candidate.complete()
        assert (
            candidate.resource_states([ref], "obligation", ResourceCutoff("2026-01"))[
                0
            ].remaining_fen
            == 80000
        )
        with pytest.raises(ContractError, match="不能继续"):
            candidate.stage(replacement["plans"][0])
    # Candidate evaluation alone never overwrites the formal result.
    assert result(store)["target"]["values"]["amount_fen"] == "100000"


def test_candidate_month_day_signed_balance_order_and_exact_sources(store):
    ledger = create_ledger()
    seed_payment(store)
    changes = [expense("80000"), _payment_at(period="2026-02", day="07")]
    ref = {"business_id": BUSINESS, "slot": "primary"}
    with store.write_unit(COMPANY) as uow:
        forward = _candidate(ledger, uow, changes)
        reverse = _candidate(ledger, uow, changes, reverse=True)
        jan = forward.resource_states([ref], "obligation", ResourceCutoff("2026-01"))[0]
        early = forward.resource_states(
            [ref], "obligation", ResourceCutoff("2026-02", "2026-02-06")
        )[0]
        paid = forward.resource_states(
            [ref], "obligation", ResourceCutoff("2026-02", "2026-02-07")
        )[0]
        assert jan.remaining_fen == early.remaining_fen == 80000
        assert paid.money_settled_fen == 100000 and paid.remaining_fen == -20000
        assert (
            paid
            == reverse.resource_states(
                [ref], "obligation", ResourceCutoff("2026-02", "2026-02-07")
            )[0]
        )
        assert len(paid.uses) == 1
        assert paid.uses[0].source == ledger.get_result_source(
            PAYMENT, "main", uow, plan=changes[1]["plans"][0]
        )
        excluded = forward.resource_states(
            [ref], "obligation", ResourceCutoff("2026-02"), excluding_consumer=(PAYMENT, "main")
        )[0]
        assert excluded.remaining_fen == 80000 and not excluded.uses
        with pytest.raises(ContractError):
            forward.resource_definitions([{"business_id": BUSINESS, "slot": "funds"}], "funds")


def test_candidate_replacement_and_withdrawal_remove_previous_consumption(store):
    ledger = create_ledger()
    seed_payment(store)
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [expense(), payment()], uow)
    ref = {"business_id": BUSINESS, "slot": "primary"}
    replacement = _payment_at("40000", seq=1)
    withdrawn = {
        "domain": "money",
        "publications": [],
        "businesses": [
            {
                "business_id": PAYMENT,
                "expected_seq": 1,
                "op": "withdraw",
                "natural_key": None,
                "complete_result_slots": [],
            }
        ],
        "retained_results": [],
        "plans": [],
        "dependencies": [],
        "issues": [],
    }
    with store.write_unit(COMPANY) as uow:
        candidate = _candidate(ledger, uow, [replacement])
        state = candidate.resource_states([ref], "obligation", ResourceCutoff("2026-01"))[0]
        assert state.money_settled_fen == 40000 and state.remaining_fen == 60000
        assert len(state.uses) == 1
        without = _candidate(ledger, uow, [withdrawn])
        state = without.resource_states([ref], "obligation", ResourceCutoff("2026-01"))[0]
        assert state.money_settled_fen == 0 and state.remaining_fen == 100000
        with pytest.raises(ContractError, match="不存在"):
            without.resource_definitions([{"business_id": PAYMENT, "slot": "funds"}], "funds")
    assert result(store, PAYMENT)["target"]["values"]["amount_fen"] == "100000"


def test_candidate_reclassification_uses_economic_month_not_posting_correction_month(store):
    ledger = create_ledger()
    seed_payment(store)
    with store.write_unit(COMPANY) as uow:
        uow.repository("application").business(RECOVERY, kind="operations.overpayment")
        adopt_declared(ledger, [expense("80000"), payment(), recovery()], uow)
        uow.repository("ledger").freeze_heads("2026-01")
    changed = recovery()
    changed["businesses"][0].update(op="replace", expected_seq=1)
    changed["plans"][0]["basis"]["adopted_quantities"] = [
        {
            "type": "obligation",
            "source": {"business_id": BUSINESS, "slot": "primary"},
            "account_code": "2202",
            "direction": "payable",
            "amount_fen": "80000",
            "party_id": SUPPLIER,
        }
    ]
    frozen_recovery = result(store, RECOVERY)["reference"]["result_id"]
    with store.write_unit(COMPANY) as uow:
        candidate = _candidate(ledger, uow, [changed], correction_period="2026-03")
        state = candidate.resource_states(
            [{"business_id": BUSINESS, "slot": "primary"}], "obligation", ResourceCutoff("2026-01")
        )[0]
        assert state.excess_reclassified_fen == 20000 and state.remaining_fen == 0
        reclassification = next(use for use in state.uses if use.kind == "reclassification")
        assert reclassification.source["result_id"] != frozen_recovery
        assert {use.value.get("mode") for use in state.uses} == {None, "reclassify_excess"}


def test_resource_definitions_read_formal_funds_without_balance_or_private_tables(store):
    ledger = create_ledger()
    seed_payment(store)
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [expense(), payment()], uow)
    with store.read_unit(COMPANY) as context:
        definition = ledger.resource_definitions(
            [{"business_id": PAYMENT, "slot": "funds"}], "funds", context
        )[0]
        assert definition.value["movement_id"] == MOVEMENT
        assert definition.value["amount_fen"] == "100000"
        assert definition.value["direction"] == "outflow"
        assert definition.source == ledger.get_result_source(PAYMENT, "main", context)


def test_candidate_overflow_is_structured_and_invalid_adoption_rolls_back(store):
    ledger = create_ledger()
    seed_payment(store)
    second_id = "50000000-0000-4000-8000-000000000099"
    second_movement = "90000000-0000-4000-8000-000000000099"
    # A second independent real event; equal amounts are not duplicate identity.
    second = _payment_at(str(MAX_FEN), business_id=second_id, movement_id=second_movement)
    with store.write_unit(COMPANY) as uow:
        candidate = _candidate(
            ledger, uow, [expense(str(MAX_FEN)), _payment_at(str(MAX_FEN)), second]
        )
        with pytest.raises(ContractError) as error:
            candidate.resource_states(
                [{"business_id": BUSINESS, "slot": "primary"}],
                "obligation",
                ResourceCutoff("2026-01"),
            )
        assert error.value.code == "invalid_input" and "64" in error.value.message
    with pytest.raises(ContractError):
        with store.write_unit(COMPANY) as uow:
            candidate = _candidate(ledger, uow, [expense("80000"), payment()])
            assert (
                candidate.resource_states(
                    [{"business_id": BUSINESS, "slot": "primary"}],
                    "obligation",
                    ResourceCutoff("2026-01"),
                )[0].remaining_fen
                == -20000
            )
            adopt_declared(ledger, [expense("80000"), payment()], uow)
    with store.read_unit(COMPANY) as context:
        assert ledger.get_results([BUSINESS, PAYMENT], context) == ()


def test_candidate_excludes_only_one_consumer_and_funds_scope_is_complete(store):
    ledger = create_ledger()
    seed_payment(store)
    sibling_id = "50000000-0000-4000-8000-000000000099"
    sibling_movement = "90000000-0000-4000-8000-000000000099"
    first = _payment_at("30000")
    sibling = _payment_at("20000", business_id=sibling_id, movement_id=sibling_movement)
    with store.write_unit(COMPANY) as uow:
        candidate = _candidate(ledger, uow, [expense(), first, sibling], reverse=True)
        state = candidate.resource_states(
            [{"business_id": BUSINESS, "slot": "primary"}],
            "obligation",
            ResourceCutoff("2026-01"),
            excluding_consumer=(PAYMENT, "main"),
        )[0]
        assert state.money_settled_fen == 20000 and state.remaining_fen == 80000
        movements = candidate.fund_movements(BANK, "2026-01", "2026-01")
        assert [item.value["movement_id"] for item in movements] == [MOVEMENT, sibling_movement]
        assert sum(int(item.value["amount_fen"]) for item in movements) == 50000
        assert candidate.fund_movements(BANK, "2026-02", "2026-02") == ()
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [expense(), first], uow)
    replacement = _payment_at("40000", period="2026-02", seq=1)
    with store.write_unit(COMPANY) as uow:
        candidate = _candidate(ledger, uow, [replacement])
        assert candidate.fund_movements(BANK, "2026-01", "2026-01") == ()
        feb = candidate.fund_movements(BANK, "2026-02", "2026-02")
        assert len(feb) == 1 and feb[0].value["amount_fen"] == "40000"
        adopt_declared(ledger, [replacement], uow)
        # The complete overlay remains coherent after adopt in this same unit.
        assert candidate.fund_movements(BANK, "2026-02", "2026-02") == feb
    with store.read_unit(COMPANY) as context:
        assert ledger.fund_movements(BANK, "2026-01", "2026-01", context) == ()
        assert ledger.fund_movements(BANK, "2026-02", "2026-02", context) == feb


def test_candidate_rejects_duplicate_actual_occurrence_and_source_future_month(store):
    ledger = create_ledger()
    seed_payment(store)
    duplicate = _payment_at("20000", business_id="50000000-0000-4000-8000-000000000099")
    with store.write_unit(COMPANY) as uow:
        with pytest.raises(ContractError, match="重复采用"):
            _candidate(ledger, uow, [expense(), _payment_at("30000"), duplicate])
    future = expense()
    future["businesses"][0]["fact"]["period"] = "2026-02"
    plan = future["plans"][0]
    plan["accounting_period"] = plan["basis"]["period"] = "2026-02"
    plan["obligations"][0]["business_period"] = "2026-02"
    future["publications"] = [publication(BUSINESS, "2026-02")]
    with pytest.raises(ContractError, match="不能早于来源"):
        with store.write_unit(COMPANY) as uow:
            adopt_declared(ledger, [future, payment()], uow)
    with store.read_unit(COMPANY) as context:
        assert ledger.get_results([BUSINESS, PAYMENT], context) == ()


def test_resource_attributed_period_does_not_replace_formal_production_time(store):
    ledger = create_ledger()
    source = expense()
    source["plans"][0]["obligations"][0]["business_period"] = "2025-12"
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [source], uow)
        candidate = ledger.begin_candidate([], uow).complete()
        ref = {"business_id": BUSINESS, "slot": "primary"}
        assert (
            candidate.resource_states([ref], "obligation", ResourceCutoff("2026-01"))[
                0
            ].remaining_fen
            == 100000
        )
        with pytest.raises(ContractError, match="尚未成立"):
            candidate.resource_states([ref], "obligation", ResourceCutoff("2025-12"))
    with store.read_unit(COMPANY) as context:
        definition = ledger.resource_definitions([ref], "obligation", context)[0]
        assert definition.value["business_period"] == "2025-12"


def test_known_nonmoney_day_filters_same_month_and_preserves_persisted_semantics(store):
    ledger = create_ledger()
    seed_payment(store)
    claim = recovery()
    claim["businesses"][0]["fact"]["effective_on"] = "2026-01-20"
    claim["plans"][0]["basis"]["effective_on"] = "2026-01-20"
    changes = [expense("80000"), payment(), claim]
    ref = {"business_id": BUSINESS, "slot": "primary"}
    before = deepcopy(changes)
    with store.write_unit(COMPANY) as uow:
        uow.repository("application").business(RECOVERY, kind="operations.overpayment")
        candidate = _candidate(ledger, uow, changes, reverse=True)
        early = candidate.resource_states(
            [ref], "obligation", ResourceCutoff("2026-01", "2026-01-10")
        )[0]
        late = candidate.resource_states(
            [ref], "obligation", ResourceCutoff("2026-01", "2026-01-20")
        )[0]
        assert early.excess_reclassified_fen == 0 and early.remaining_fen == -20000
        assert late.excess_reclassified_fen == 20000 and late.remaining_fen == 0
        adopt_declared(ledger, changes, uow)
    assert changes == before
    with store.write_unit(COMPANY) as uow:
        saved = ledger.begin_candidate([], uow).complete()
        assert (
            saved.resource_states([ref], "obligation", ResourceCutoff("2026-01", "2026-01-10"))[0]
            == early
        )
        assert (
            saved.resource_states([ref], "obligation", ResourceCutoff("2026-01", "2026-01-20"))[0]
            == late
        )


def test_month_only_nonmoney_change_needs_day_only_for_ambiguous_partial_month(store):
    ledger = create_ledger()
    seed_payment(store)
    with store.write_unit(COMPANY) as uow:
        candidate = _candidate(ledger, uow, [expense("80000"), payment(), recovery()])
        ref = {"business_id": BUSINESS, "slot": "primary"}
        for cutoff in (
            ResourceCutoff("2026-01"),
            ResourceCutoff("2026-01", "2026-01-31"),
            ResourceCutoff("2026-02", "2026-02-01"),
        ):
            assert candidate.resource_states([ref], "obligation", cutoff)[0].remaining_fen == 0
        with pytest.raises(ContractError) as error:
            candidate.resource_states([ref], "obligation", ResourceCutoff("2026-01", "2026-01-10"))
        assert error.value.code == "needs_information"
        assert error.value.fact_issues[0]["business_id"] == RECOVERY
        assert error.value.fact_issues[0]["field"] == "/effective_on"
        assert error.value.fact_issues[0]["allowed_precision"] == ["day"]


def test_actual_money_cannot_precede_known_necessary_source_day(store):
    ledger = create_ledger()
    seed_payment(store)
    original = expense()
    original["businesses"][0]["fact"]["effective_on"] = "2026-01-10"
    original["plans"][0]["basis"]["effective_on"] = "2026-01-10"
    with pytest.raises(ContractError, match="必要实际日"):
        with store.write_unit(COMPANY) as uow:
            adopt_declared(ledger, [original, payment()], uow)
    with store.read_unit(COMPANY) as context:
        assert ledger.get_results([BUSINESS, PAYMENT], context) == ()
