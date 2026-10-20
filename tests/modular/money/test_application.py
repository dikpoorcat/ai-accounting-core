"""Money accepts real operating results through the complete public assembly."""

from hashlib import sha256
from uuid import uuid4

import pytest

from ai_accounting_modular.application.public import create_application
from ai_accounting_modular.application.public import repository_factory as app_repository
from ai_accounting_modular.contracts.public import PageQuery
from ai_accounting_modular.domains.operations.public import create_operations
from ai_accounting_modular.domains.operations.public import (
    repository_factory as operations_repository,
)
from ai_accounting_modular.identity.public import create_identity
from ai_accounting_modular.identity.public import repository_factory as identity_repository
from ai_accounting_modular.ledger.public import LedgerQuery, create_ledger
from ai_accounting_modular.ledger.public import repository_factory as ledger_repository
from ai_accounting_modular.money.public import (
    MovementQuery,
    StatementQuery,
    create_money,
    stable_occurrence_id,
)
from ai_accounting_modular.money.public import repository_factory as money_repository
from ai_accounting_modular.records.public import create_records
from ai_accounting_modular.records.public import repository_factory as records_repository
from ai_accounting_modular.storage.public import CompanySpec, OwnerProfile, Storage, create_catalog


def assemble(tmp_path, *, people=False):
    catalog = tmp_path / "catalog.sqlite3"
    create_catalog(catalog, OwnerProfile(str(uuid4()), "合成老板", "synthetic-hash"))
    repositories = [
        app_repository(),
        identity_repository(),
        ledger_repository(),
        records_repository(),
        operations_repository(),
        money_repository(),
    ]
    domains = [create_operations()]
    if people:
        from ai_accounting_modular.domains.people.public import create_people
        from ai_accounting_modular.domains.people.public import (
            repository_factory as people_repository,
        )

        repositories.append(people_repository())
        domains.append(create_people())
    storage = Storage(catalog, repositories=repositories)
    company_id = str(uuid4())
    storage.create_company(
        CompanySpec(
            company_id,
            str(uuid4()),
            "913100000000000007",
            "合成真实资金测试",
            "2026-01",
            tmp_path / "company",
        )
    )
    identity, ledger = create_identity(), create_ledger()
    money = create_money(ledger=ledger, identity=identity)
    records = create_records(root_for=lambda read: storage.company_metadata(read).company_root)
    app = create_application(
        storage=storage,
        identity=identity,
        ledger=ledger,
        records=records,
        domains=[*domains, money],
        money=money,
    )
    return app, storage, company_id, money, ledger


@pytest.fixture
def system(tmp_path):
    return assemble(tmp_path)


def request(company_id, *facts, objects=(), sources=(), mutations=None):
    return {
        "contract": "ai-accounting-modular/1",
        "company_id": company_id,
        "command_id": str(uuid4()),
        "objects": list(objects),
        "sources": list(sources),
        "mutations": list(mutations)
        if mutations is not None
        else [{"op": "create", "expected_seq": 0, "fact": fact} for fact in facts],
    }


def seed(system):
    app, _, company, _, _ = system
    ids = {key: str(uuid4()) for key in ("business", "expense", "bank", "cash", "party", "source")}
    text = "合成实际资料：维修费用1000元，公司后续实际付款。"
    source = {
        "source_id": ids["source"],
        "kind": "owner_statement",
        "text": text,
        "sha256": sha256(text.encode("utf-8")).hexdigest(),
        "archive_month": "2026-01",
    }
    expense = {
        "business_id": ids["expense"],
        "kind": "operations.expense",
        "period": "2026-01",
        "evidence_ids": [ids["source"]],
        "data": {
            "creditor_id": ids["party"],
            "creditor_kind": "supplier",
            "amount_fen": "100000",
            "expense_class": "administration",
        },
    }
    objects = [
        {
            "object_id": ids["party"],
            "kind": "organization",
            "name": "合成供应商",
            "expected_seq": 0,
            "data": {},
        },
        {
            "object_id": ids["bank"],
            "kind": "bank_account",
            "name": "合成公司银行",
            "expected_seq": 0,
            "data": {
                "bank_name": "合成银行",
                "account_number": "synthetic-only",
                "currency": "CNY",
                "owner": "company",
                "opened_period": "2026-01",
                "zero_at_opening": True,
            },
        },
        {
            "object_id": ids["cash"],
            "kind": "cash_account",
            "name": "合成库存现金",
            "expected_seq": 0,
            "data": {"currency": "CNY"},
        },
    ]
    response = app.accounting_apply(request(company, expense, objects=objects, sources=[source]))
    assert response["completion"] == "complete", response
    return ids, expense


def payment(ids, value="100000", *, account="bank", day="2026-01-20"):
    return {
        "business_id": ids["business"],
        "kind": "money.movement",
        "period": day[:7],
        "effective_on": day,
        "evidence_ids": [ids["source"]],
        "data": {
            "type": "settlement",
            "funds_account_id": ids[account],
            "direction": "outflow",
            "amount_fen": value,
            "payment_method": "individual",
            "counterparty_id": ids["party"],
            "allocations": [
                {"source": {"business_id": ids["expense"], "slot": "primary"}, "amount_fen": value}
            ],
        },
    }


def get(app, company, *ids):
    return app.accounting_get(
        {
            "company_id": company,
            "business_ids": list(ids),
            "include": ["facts", "results", "sources"],
        }
    )


def test_expense_then_actual_payment_is_one_formal_bank_movement_and_retry_does_not_repeat(system):
    app, storage, company, money, ledger = system
    ids, _ = seed(system)
    call = request(company, payment(ids))
    response = app.accounting_apply(call)
    assert response["completion"] == "complete", response
    original = get(app, company, ids["business"])
    assert app.accounting_apply(call)["accounting"] == "unchanged"
    assert get(app, company, ids["business"]) == original
    with storage.read_unit(company) as read:
        movements = money.query_movements(
            {
                "company_id": read.company_id,
                "period_from": "2026-01",
                "period_to": "2026-01",
                "view": read.view,
            },
            MovementQuery(ids["bank"]),
            PageQuery(),
            read,
        )
        assert movements.filtered_count == 1
        assert movements.items[0]["movement"]["amount_fen"] == "100000"
        obligation = ledger.resolve(
            [{"business_id": ids["expense"], "slot": "primary"}], "obligation", read
        )[0]
        assert obligation["balance"]["money_settled_fen"] == "100000"
        assert obligation["balance"]["remaining_fen"] == "0"
        vouchers = ledger.query_vouchers(LedgerQuery(), read)
        assert len(vouchers.items) == 2
        assert (
            sum(
                int(line["debit_fen"])
                for v in vouchers.items
                for line in v["lines"]
                if line["account_code"] == "5602"
            )
            == 100000
        )


def test_expense_and_payment_can_arrive_together_in_reverse_input_order(system):
    app, storage, company, money, _ = system
    ids, _ = seed(system)
    expense2 = {
        "business_id": str(uuid4()),
        "kind": "operations.expense",
        "period": "2026-01",
        "evidence_ids": [ids["source"]],
        "data": {
            "creditor_id": ids["party"],
            "creditor_kind": "supplier",
            "amount_fen": "2345",
            "expense_class": "service",
        },
    }
    new_ids = {**ids, "expense": expense2["business_id"]}
    paid = payment(new_ids, "2345")
    response = app.accounting_apply(request(company, paid, expense2))
    assert response["completion"] == "complete", response
    with storage.read_unit(company) as read:
        assert (
            money.query_movements(
                {
                    "company_id": read.company_id,
                    "period_from": "2026-01",
                    "period_to": "2026-01",
                    "view": read.view,
                },
                MovementQuery(ids["bank"]),
                PageQuery(),
                read,
            ).items[0]["movement"]["amount_fen"]
            == "2345"
        )


def test_replacement_preserves_occurrence_and_voucher_number_and_withdrawal_removes_current_money(
    system,
):
    app, storage, company, money, ledger = system
    ids, _ = seed(system)
    paid = payment(ids, "60000")
    assert app.accounting_apply(request(company, paid))["completion"] == "complete"
    with storage.read_unit(company) as read:
        old = ledger.get_results([ids["business"]], read)[0]
    changed = payment(ids, "40000", account="cash")
    response = app.accounting_apply(
        request(company, mutations=[{"op": "replace", "expected_seq": 1, "fact": changed}])
    )
    assert response["completion"] == "complete", response
    with storage.read_unit(company) as read:
        current = ledger.get_results([ids["business"]], read)[0]
        assert current["reference"]["voucher_number"] == old["reference"]["voucher_number"]
        assert (
            money.query_movements(
                {
                    "company_id": read.company_id,
                    "period_from": "2026-01",
                    "period_to": "2026-01",
                    "view": read.view,
                },
                MovementQuery(ids["bank"]),
                PageQuery(),
                read,
            ).filtered_count
            == 0
        )
        actual = money.query_movements(
            {
                "company_id": read.company_id,
                "period_from": "2026-01",
                "period_to": "2026-01",
                "view": read.view,
            },
            MovementQuery(ids["cash"]),
            PageQuery(),
            read,
        ).items[0]["movement"]
        assert actual["movement_id"] == stable_occurrence_id(ids["business"], "funds")
        assert actual["amount_fen"] == "40000"
    response = app.accounting_apply(
        request(
            company,
            mutations=[{"op": "withdraw", "business_id": ids["business"], "expected_seq": 2}],
        )
    )
    assert response["completion"] == "complete", response
    with storage.read_unit(company) as read:
        assert (
            money.query_movements(
                {
                    "company_id": read.company_id,
                    "period_from": "2026-01",
                    "period_to": "2026-01",
                    "view": read.view,
                },
                MovementQuery(ids["cash"]),
                PageQuery(),
                read,
            ).filtered_count
            == 0
        )
        balance = ledger.resolve(
            [{"business_id": ids["expense"], "slot": "primary"}], "obligation", read
        )[0]["balance"]
        assert balance["remaining_fen"] == "100000"


def test_bank_and_cash_share_capacity_and_overpay_rolls_back_the_entire_second_unit(system):
    app, storage, company, money, ledger = system
    ids, _ = seed(system)
    assert app.accounting_apply(request(company, payment(ids, "60000")))["completion"] == "complete"
    before = get(app, company, ids["expense"], ids["business"])
    second = payment({**ids, "business": str(uuid4())}, "50000", account="cash")
    response = app.accounting_apply(request(company, second))
    assert response["completion"] == "not_saved", response
    assert response["error"] == "dependency_conflict"
    assert get(app, company, ids["expense"], ids["business"]) == before
    with storage.read_unit(company) as read:
        assert (
            money.query_movements(
                {
                    "company_id": read.company_id,
                    "period_from": "2026-01",
                    "period_to": "2026-01",
                    "view": read.view,
                },
                MovementQuery(ids["cash"]),
                PageQuery(),
                read,
            ).filtered_count
            == 0
        )
        assert (
            ledger.resolve(
                [{"business_id": ids["expense"], "slot": "primary"}], "obligation", read
            )[0]["balance"]["remaining_fen"]
            == "40000"
        )


def reconciled_unit(ids):
    transfer = {
        "business_id": str(uuid4()),
        "kind": "money.transfer",
        "period": "2026-01",
        "effective_on": "2026-01-10",
        "evidence_ids": [ids["source"]],
        "data": {
            "type": "physical",
            "from_account_id": ids["cash"],
            "to_account_id": ids["bank"],
            "amount_fen": "100000",
        },
    }
    paid = payment(ids)
    rows = [
        {
            "row_id": str(uuid4()),
            "source": {"source_id": ids["source"], "location": "原件实际出款行"},
            "actual_on": "2026-01-20",
            "direction": "outflow",
            "amount_fen": "100000",
        },
        {
            "row_id": str(uuid4()),
            "source": {"source_id": ids["source"], "location": "原件实际入款行"},
            "actual_on": "2026-01-10",
            "direction": "inflow",
            "amount_fen": "100000",
        },
    ]
    statement = {
        "business_id": str(uuid4()),
        "kind": "money.bank_statement",
        "period": "2026-01",
        "evidence_ids": [],
        "data": {
            "funds_account_id": ids["bank"],
            "opening_fen": "0",
            "closing_fen": "0",
            "rows": rows,
        },
    }
    check = {
        "business_id": str(uuid4()),
        "kind": "money.reconciliation",
        "period": "2026-01",
        "evidence_ids": [],
        "data": {
            "statement_id": statement["business_id"],
            "matches": [
                {
                    "statement_row_ids": [rows[0]["row_id"]],
                    "fund_movement_ids": [stable_occurrence_id(ids["business"], "funds")],
                },
                {
                    "statement_row_ids": [rows[1]["row_id"]],
                    "fund_movement_ids": [
                        stable_occurrence_id(transfer["business_id"], "to_funds")
                    ],
                },
            ],
        },
    }
    return transfer, paid, statement, check


def test_original_rows_and_full_reconciliation_same_unit_do_not_duplicate_any_money(system):
    app, storage, company, money, ledger = system
    ids, _ = seed(system)
    transfer, paid, statement, check = reconciled_unit(ids)
    call = request(company, check, statement, paid, transfer)
    response = app.accounting_apply(call)
    assert response["completion"] == "complete", response
    with storage.read_unit(company) as read:
        assert money.export_current([statement["business_id"]], read)[0] == statement
        page1 = money.query_statement_rows(
            {
                "company_id": read.company_id,
                "period_from": "2026-01",
                "period_to": "2026-01",
                "view": read.view,
            },
            StatementQuery(ids["bank"]),
            PageQuery(limit=1),
            read,
        )
        page2 = money.query_statement_rows(
            {
                "company_id": read.company_id,
                "period_from": "2026-01",
                "period_to": "2026-01",
                "view": read.view,
            },
            StatementQuery(ids["bank"]),
            PageQuery(limit=1, cursor=page1.next_cursor),
            read,
        )
        assert page1.filtered_count == page2.filtered_count == 2
        assert page1.items[0].actual_on == "2026-01-10"
        assert page2.items[0].actual_on == "2026-01-20"
        actual = money.query_movements(
            {
                "company_id": read.company_id,
                "period_from": "2026-01",
                "period_to": "2026-01",
                "view": read.view,
            },
            MovementQuery(ids["bank"]),
            PageQuery(),
            read,
        )
        assert actual.filtered_count == 2
        assert sum(int(item["movement"]["amount_fen"]) for item in actual.items) == 200000
        assert ledger.query_vouchers(LedgerQuery(), read).total == 3
        results = money.reconciliation(
            {
                "company_id": company,
                "family": "money",
                "period_from": "2026-01",
                "period_to": "2026-01",
                "limit": 20,
            },
            read,
        )
        assert results[0]["result"]["target"]["values"]["difference_fen"] == "0"
    snapshot = get(
        app,
        company,
        transfer["business_id"],
        paid["business_id"],
        statement["business_id"],
        check["business_id"],
    )
    assert app.accounting_apply(call)["accounting"] == "unchanged"
    assert (
        get(
            app,
            company,
            transfer["business_id"],
            paid["business_id"],
            statement["business_id"],
            check["business_id"],
        )
        == snapshot
    )


def test_reconciliation_observes_a_new_unmatched_movement_and_rolls_it_back(system):
    app, storage, company, money, _ = system
    ids, _ = seed(system)
    transfer, paid, statement, check = reconciled_unit(ids)
    assert (
        app.accounting_apply(request(company, check, statement, paid, transfer))["completion"]
        == "complete"
    )
    before = get(app, company, check["business_id"], statement["business_id"])
    extra = {
        "business_id": str(uuid4()),
        "kind": "money.movement",
        "period": "2026-01",
        "effective_on": "2026-01-22",
        "evidence_ids": [ids["source"]],
        "data": {
            "type": "managed_reserve",
            "funds_account_id": ids["bank"],
            "direction": "inflow",
            "amount_fen": "1",
        },
    }
    response = app.accounting_apply(request(company, extra))
    assert response["completion"] == "not_saved", response
    assert response["error"] == "dependency_conflict"
    assert get(app, company, check["business_id"], statement["business_id"]) == before
    with storage.read_unit(company) as read:
        assert (
            money.query_movements(
                {
                    "company_id": read.company_id,
                    "period_from": "2026-01",
                    "period_to": "2026-01",
                    "view": read.view,
                },
                MovementQuery(ids["bank"]),
                PageQuery(),
                read,
            ).filtered_count
            == 2
        )


def test_operating_advance_offset_clears_two_rights_without_creating_another_payment(system):
    app, storage, company, money, ledger = system
    ids, _ = seed(system)
    advance = {
        "business_id": str(uuid4()),
        "kind": "operations.advance",
        "period": "2026-01",
        "evidence_ids": [ids["source"]],
        "data": {
            "side": "supplier",
            "counterparty_id": ids["party"],
            "amount_fen": "100000",
            "purpose": "operating",
        },
    }
    offset = {
        "business_id": str(uuid4()),
        "kind": "money.transfer",
        "period": "2026-01",
        "evidence_ids": [ids["source"]],
        "data": {
            "type": "offset",
            "offset_kind": "advance_application",
            "first": {
                "source": {"business_id": advance["business_id"], "slot": "advance"},
                "amount_fen": "100000",
            },
            "second": {
                "source": {"business_id": ids["expense"], "slot": "primary"},
                "amount_fen": "100000",
            },
        },
    }
    result = app.accounting_apply(request(company, offset, advance))
    assert result["completion"] == "complete", result
    with storage.read_unit(company) as read:
        assert (
            money.query_movements(
                {
                    "company_id": read.company_id,
                    "period_from": "2026-01",
                    "period_to": "2026-01",
                    "view": read.view,
                },
                MovementQuery(ids["bank"]),
                PageQuery(),
                read,
            ).filtered_count
            == 0
        )
        balances = ledger.resolve(
            [
                {"business_id": advance["business_id"], "slot": "advance"},
                {"business_id": ids["expense"], "slot": "primary"},
                {"business_id": advance["business_id"], "slot": "primary"},
            ],
            "obligation",
            read,
        )
        assert [item["balance"]["remaining_fen"] for item in balances] == ["0", "0", "100000"]
        voucher = ledger.get_results([offset["business_id"]], read)[0]["vouchers"][0]
        assert [
            (row["account_code"], row["debit_fen"], row["credit_fen"]) for row in voucher["lines"]
        ] == [("1123", "0", "100000"), ("2202", "100000", "0")]


def test_asset_purpose_prepayment_cannot_be_offset_against_an_ordinary_fee(system):
    app, _, company, _, _ = system
    ids, _ = seed(system)
    advance = {
        "business_id": str(uuid4()),
        "kind": "operations.advance",
        "period": "2026-01",
        "evidence_ids": [ids["source"]],
        "data": {
            "side": "supplier",
            "counterparty_id": ids["party"],
            "amount_fen": "100000",
            "purpose": "fixed_asset",
        },
    }
    offset = {
        "business_id": str(uuid4()),
        "kind": "money.transfer",
        "period": "2026-01",
        "evidence_ids": [ids["source"]],
        "data": {
            "type": "offset",
            "offset_kind": "advance_application",
            "first": {
                "source": {"business_id": advance["business_id"], "slot": "advance"},
                "amount_fen": "100000",
            },
            "second": {
                "source": {"business_id": ids["expense"], "slot": "primary"},
                "amount_fen": "100000",
            },
        },
    }
    before = get(app, company, ids["expense"])
    result = app.accounting_apply(request(company, offset, advance))
    assert result["completion"] == "not_saved", result
    assert result["error"] == "dependency_conflict"
    assert get(app, company, ids["expense"]) == before
