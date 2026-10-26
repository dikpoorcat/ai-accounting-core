"""Public real-company regressions for timing and atomic original-row moves."""

import json
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import PageQuery
from ai_accounting_modular.money.public import MovementQuery, StatementQuery

from .test_application import assemble, get, payment, request, seed


@pytest.mark.parametrize(
    "production,attribution,succeeds",
    [
        ("2026-01", "2026-02", True),
        ("2026-02", "2026-01", False),
    ],
)
def test_tax_payment_uses_formal_production_time_and_preserves_tax_attribution(
    tmp_path,
    production,
    attribution,
    succeeds,
):
    system = assemble(tmp_path, people=True)
    app, storage, company, money, ledger = system
    ids, _ = seed(system)
    cases = json.loads(
        (Path(__file__).parents[3] / "docs/design/examples/business-facts-cases.json").read_text(
            encoding="utf-8"
        )
    )
    bonus = deepcopy(
        next(case["when"] for case in cases["valid_cases"] if case["case_id"] == "B17B")
    )
    bonus.update(business_id=str(uuid4()), period=production, evidence_ids=[ids["source"]])
    person = str(uuid4())
    bonus["data"]["person_id"] = person
    bonus["data"]["tax_basis"].update(tax_period=attribution, source={"source_id": ids["source"]})
    created = app.accounting_apply(
        request(
            company,
            bonus,
            objects=[
                {
                    "object_id": person,
                    "kind": "person",
                    "name": "合成奖金领取人",
                    "expected_seq": 0,
                    "data": {},
                }
            ],
        )
    )
    assert created["completion"] == "complete", created
    paid = payment({**ids, "expense": bonus["business_id"]}, "10000")
    paid["data"]["allocations"][0]["source"]["slot"] = "tax"
    paid["data"].pop("counterparty_id")
    before = get(app, company, bonus["business_id"])
    result = app.accounting_apply(request(company, paid))
    assert result["completion"] == ("complete" if succeeds else "not_saved"), result
    with storage.read_unit(company) as read:
        obligation = ledger.resolve(
            [{"business_id": bonus["business_id"], "slot": "tax"}], "obligation", read
        )[0]
        assert obligation["obligation"]["business_period"] == attribution
        assert obligation["balance"]["adopted_result"]["business_period"] == production
        assert obligation["balance"]["remaining_fen"] == ("0" if succeeds else "10000")
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
        assert actual.filtered_count == (1 if succeeds else 0)
        if succeeds:
            assert actual.items[0]["movement"]["amount_fen"] == "10000"
            assert actual.items[0]["movement"]["actual_on"] == "2026-01-20"
    if not succeeds:
        assert result["error"] == "dependency_conflict"
        assert get(app, company, bonus["business_id"]) == before


def statement(kind, ids, account_id, month, row_id, value, *, business_id=None):
    data = {
        "rows": [
            {
                "row_id": row_id,
                "source": {"source_id": ids["source"]},
                "actual_on": month + "-10",
                "direction": "inflow",
                "amount_fen": value,
            }
        ]
    }
    if kind == "bank":
        data.update(funds_account_id=account_id, opening_fen="0", closing_fen=value)
    else:
        data["platform_account_id"] = account_id
    return {
        "business_id": business_id or str(uuid4()),
        "kind": "money.bank_statement" if kind == "bank" else "money.platform_activity",
        "period": month,
        "evidence_ids": [],
        "data": data,
    }


def row_system(tmp_path, kind):
    system = assemble(tmp_path)
    app, _, company, _, _ = system
    ids, _ = seed(system)
    account_id = ids["bank"]
    if kind == "platform":
        account_id = str(uuid4())
        created = app.accounting_apply(
            request(
                company,
                objects=[
                    {
                        "object_id": account_id,
                        "kind": "platform_account",
                        "name": "合成公司平台",
                        "expected_seq": 0,
                        "data": {
                            "platform": "合成平台",
                            "account_key": "synthetic-only",
                            "currency": "CNY",
                            "owner": "company",
                        },
                    }
                ],
            )
        )
        assert created["completion"] == "complete", created
    first = statement(kind, ids, account_id, "2026-01", str(uuid4()), "100")
    second = statement(kind, ids, account_id, "2026-02", str(uuid4()), "200")
    created = app.accounting_apply(request(company, first, second))
    assert created["completion"] == "complete", created
    return system, ids, account_id, first, second


@pytest.mark.parametrize("kind", ["bank", "platform"])
def test_two_open_months_can_atomically_exchange_misassigned_original_row_identities(
    tmp_path, kind
):
    system, ids, account_id, first, second = row_system(tmp_path, kind)
    app, storage, company, money, _ = system
    before = get(app, company, first["business_id"], second["business_id"])
    changed = [
        statement(
            kind,
            ids,
            account_id,
            "2026-01",
            second["data"]["rows"][0]["row_id"],
            "200",
            business_id=first["business_id"],
        ),
        statement(
            kind,
            ids,
            account_id,
            "2026-02",
            first["data"]["rows"][0]["row_id"],
            "100",
            business_id=second["business_id"],
        ),
    ]
    call = request(
        company, mutations=[{"op": "replace", "expected_seq": 1, "fact": fact} for fact in changed]
    )
    result = app.accounting_apply(call)
    assert result["completion"] == "complete", result
    with storage.read_unit(company) as read:
        actual = {
            fact["business_id"]: fact
            for fact in money.export_current([first["business_id"], second["business_id"]], read)
        }
        assert actual == {fact["business_id"]: fact for fact in changed}
        rows = money.query_statement_rows(
            {
                "company_id": read.company_id,
                "period_from": "2026-01",
                "period_to": "2026-02",
                "view": read.view,
            },
            StatementQuery(account_id),
            PageQuery(),
            read,
        )
        assert rows.filtered_count == 2
        assert [(row.row_id, row.actual_on, str(row.amount_fen)) for row in rows.items] == [
            (second["data"]["rows"][0]["row_id"], "2026-01-10", "200"),
            (first["data"]["rows"][0]["row_id"], "2026-02-10", "100"),
        ]
        assert (
            money.query_movements(
                {
                    "company_id": read.company_id,
                    "period_from": "2026-01",
                    "period_to": "2026-02",
                    "view": read.view,
                },
                MovementQuery(account_id),
                PageQuery(),
                read,
            ).filtered_count
            == 0
        )
    saved = get(app, company, first["business_id"], second["business_id"])
    assert saved["head"] == before["head"] + 1
    assert {item["state"]["seq"] for item in saved["businesses"]} == {2}
    assert app.accounting_apply(call)["accounting"] == "unchanged"
    assert app.accounting_apply({**call, "command_id": str(uuid4())})["accounting"] == "unchanged"
    assert get(app, company, first["business_id"], second["business_id"]) == saved


@pytest.mark.parametrize("kind", ["bank", "platform"])
def test_withdrawal_and_row_reassignment_are_order_independent_in_one_unit(tmp_path, kind):
    system, ids, account_id, first, second = row_system(tmp_path, kind)
    app, storage, company, money, _ = system
    replacement = statement(
        kind, ids, account_id, "2026-03", first["data"]["rows"][0]["row_id"], "100"
    )
    result = app.accounting_apply(
        request(
            company,
            mutations=[
                {"op": "create", "expected_seq": 0, "fact": replacement},
                {"op": "withdraw", "expected_seq": 1, "business_id": first["business_id"]},
            ],
        )
    )
    assert result["completion"] == "complete", result
    with storage.read_unit(company) as read:
        facts = money.export_current(
            [first["business_id"], second["business_id"], replacement["business_id"]], read
        )
        assert {fact["business_id"] for fact in facts} == {
            second["business_id"],
            replacement["business_id"],
        }
        rows = money.query_statement_rows(
            {
                "company_id": read.company_id,
                "period_from": "2026-01",
                "period_to": "2026-03",
                "view": read.view,
            },
            StatementQuery(account_id),
            PageQuery(),
            read,
        )
        assert rows.filtered_count == 2
        moved = next(row for row in rows.items if row.row_id == first["data"]["rows"][0]["row_id"])
        assert (moved.business_id, moved.actual_on, str(moved.amount_fen)) == (
            replacement["business_id"],
            "2026-03-10",
            "100",
        )


@pytest.mark.parametrize("kind", ["bank", "platform"])
@pytest.mark.parametrize("conflict", ["outside", "unchanged_participant", "duplicate_final"])
def test_row_conflicts_return_structured_issues_and_preserve_the_complete_original_set(
    tmp_path, kind, conflict
):
    system, ids, account_id, first, second = row_system(tmp_path, kind)
    app, storage, company, money, _ = system
    before = get(app, company, first["business_id"], second["business_id"])
    row_id = str(uuid4()) if conflict == "duplicate_final" else second["data"]["rows"][0]["row_id"]
    target = statement(
        kind, ids, account_id, "2026-01", row_id, "200", business_id=first["business_id"]
    )
    mutations = [{"op": "replace", "expected_seq": 1, "fact": target}]
    if conflict == "unchanged_participant":
        mutations.append({"op": "replace", "expected_seq": 1, "fact": deepcopy(second)})
    elif conflict == "duplicate_final":
        mutations.append(
            {
                "op": "replace",
                "expected_seq": 1,
                "fact": statement(
                    kind,
                    ids,
                    account_id,
                    "2026-02",
                    row_id,
                    "200",
                    business_id=second["business_id"],
                ),
            }
        )
    result = app.accounting_apply(request(company, mutations=mutations))
    assert result["completion"] == "not_saved", result
    assert result["error"] == "dependency_conflict"
    assert result["fact_issues"][0]["code"] == "original_row_conflict"
    assert result["fact_issues"][0]["field"] == "/data/rows/0/row_id"
    assert get(app, company, first["business_id"], second["business_id"]) == before
    with storage.read_unit(company) as read:
        actual = {
            fact["business_id"]: fact
            for fact in money.export_current([first["business_id"], second["business_id"]], read)
        }
        assert actual == {first["business_id"]: first, second["business_id"]: second}
