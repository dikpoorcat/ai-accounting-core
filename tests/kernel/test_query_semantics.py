"""Shared report/dashboard query semantics stay pure and fail closed."""

import pytest

from ai_accounting.kernel.query_semantics import (
    classify_financial_position,
    report_party_splits,
    resolve_calculation_relations,
)


def calculation(
    ident,
    *,
    kind="expense",
    subject_id=None,
    lines=(),
    fact=None,
    values=None,
):
    return {
        "id": ident,
        "kind": kind,
        "subject_id": subject_id or ident,
        "period": 24312,
        "fact_id": "fact-" + ident,
        "fact_data": fact or {},
        "outcome": {"lines": list(lines), "values": values or {}},
        "result_digest": "digest-" + ident,
    }


def resolver(records, parents=None):
    parents = parents or {}
    return {
        "load_calculation": records.__getitem__,
        "load_parents": lambda ident: parents.get(ident, ()),
    }


def test_financial_position_nets_per_stable_party_and_keeps_tax_direction():
    position = classify_financial_position(
        [
            {"account": "1122", "amount": 100, "party_key": ("party", "alice")},
            {"account": "1122", "amount": -70, "party_key": ("party", "alice")},
            {"account": "1122", "amount": -50, "party_key": ("party", "bob")},
            {"account": "222101", "amount": 40},
            {"account": "222104", "amount": -60},
        ]
    )

    assert position["lines"][4] == 30
    assert position["lines"][34] == 50
    assert position["lines"][14] == 40
    assert position["lines"][36] == 60


def test_financial_position_preserves_direct_inventory_in_line_nine():
    position = classify_financial_position(
        [
            {"account": "4301", "amount": 800},
            {"account": "1403", "amount": 200},
            {"account": "1405", "amount": 300},
        ]
    )

    assert position["lines"][9] == 1300


def test_unresolved_party_only_nulls_dependent_balance_fields():
    position = classify_financial_position(
        [
            {"account": "1002", "amount": 500},
            {"account": "1122", "amount": 100, "party_state": "unresolved"},
        ]
    )

    assert position["lines"][1] == 500
    assert position["lines"][4] is None
    assert position["lines"][34] is None
    assert position["assets_fen"] is None
    assert position["liabilities_fen"] is None
    assert position["lines"][35] == 0
    assert position["equation_valid"] is None
    assert position["complete"] is False


def test_partial_bank_batch_payment_binds_each_exact_recipient_and_line():
    source = calculation(
        "source",
        kind="reimbursed_asset_batch",
        subject_id="batch",
        values={
            "obligations": [
                {
                    "key": "reimbursed_asset_batch:batch:alice",
                    "name": "alice",
                    "account": "2241",
                    "normal": "credit",
                    "amount_fen": 100,
                    "category": "payable",
                    "counterparty_id": "alice",
                },
                {
                    "key": "reimbursed_asset_batch:batch:bob",
                    "name": "bob",
                    "account": "2241",
                    "normal": "credit",
                    "amount_fen": 100,
                    "category": "payable",
                    "counterparty_id": "bob",
                },
            ]
        },
    )
    payment = calculation(
        "payment",
        kind="payment",
        fact={
            "payment_method": "bank_batch",
            "allocations": [
                {
                    "source_kind": "reimbursed_asset_batch",
                    "source_id": "batch",
                    "obligation": party,
                    "recipient_id": party,
                    "amount_fen": 50,
                }
                for party in ("alice", "bob")
            ],
        },
        lines=[
            {"account": "2241", "debit": 50, "credit": 0},
            {"account": "1002", "debit": 0, "credit": 50},
            {"account": "2241", "debit": 50, "credit": 0},
            {"account": "1002", "debit": 0, "credit": 50},
        ],
        values={
            "direction": "outflow",
            "settlements": [
                {
                    "source_calculation": "source",
                    "obligation": f"reimbursed_asset_batch:batch:{party}",
                    "amount_fen": 50,
                }
                for party in ("alice", "bob")
            ],
        },
    )

    resolved = resolve_calculation_relations(payment, **resolver({"source": source}))

    assert not resolved["issues"]
    assert [item["recipient_id"] for item in resolved["settlements"]] == ["alice", "bob"]
    assert [item["line_numbers"] for item in resolved["settlements"]] == [[1, 2], [3, 4]]
    assert [item["party_key"] for item in resolved["settlements"]] == [
        ("party", "alice"),
        ("party", "bob"),
    ]
    split = report_party_splits({"account": "2241", "amount": 50, "line_no": 3}, resolved)
    assert split["splits"] == ((("party", "bob"), 50),)


def test_payment_relation_rejects_wrong_funds_line_instead_of_guessing():
    source = calculation(
        "source",
        subject_id="sale",
        values={
            "obligations": [
                {
                    "key": "sale:primary",
                    "name": "primary",
                    "account": "1122",
                    "normal": "debit",
                    "amount_fen": 100,
                    "category": "receivable",
                    "counterparty_id": "customer",
                }
            ]
        },
    )
    payment = calculation(
        "payment",
        kind="payment",
        fact={
            "payment_method": "individual",
            "counterparty_id": "customer",
            "allocations": [
                {
                    "source_kind": "expense",
                    "source_id": "sale",
                    "obligation": "primary",
                    "amount_fen": 40,
                }
            ],
        },
        lines=[
            {"account": "1012", "debit": 40, "credit": 0},
            {"account": "1122", "debit": 0, "credit": 40},
        ],
        values={
            "direction": "inflow",
            "settlements": [
                {"source_calculation": "source", "obligation": "sale:primary", "amount_fen": 40}
            ],
        },
    )

    resolved = resolve_calculation_relations(payment, **resolver({"source": source}))

    assert resolved["settlements"][0]["state"] == "unresolved"
    assert resolved["line_relations"][0]["party_key"] is None
    assert {item["field"] for item in resolved["issues"]} == {"query_source.settlement"}
    split = report_party_splits({"account": "1122", "amount": -40, "line_no": 2}, resolved)
    assert split["state"] == "unresolved"
    assert split["splits"] is None


def test_missing_frozen_source_is_a_local_issue_without_current_fallback():
    payment = calculation(
        "payment",
        kind="payment",
        fact={
            "payment_method": "individual",
            "counterparty_id": "supplier",
            "allocations": [
                {
                    "source_kind": "expense",
                    "source_id": "expense",
                    "obligation": "primary",
                    "amount_fen": 40,
                }
            ],
        },
        lines=[
            {"account": "2202", "debit": 40, "credit": 0},
            {"account": "1002", "debit": 0, "credit": 40},
        ],
        values={
            "direction": "outflow",
            "settlements": [
                {
                    "source_calculation": "missing-source",
                    "obligation": "expense:primary",
                    "amount_fen": 40,
                }
            ],
        },
    )

    resolved = resolve_calculation_relations(payment, **resolver({}))

    assert resolved["settlements"][0]["state"] == "unresolved"
    assert {item["field"] for item in resolved["issues"]} == {
        "query_source.calculation_id",
        "query_source.settlement",
    }


def test_payment_tax_transfer_relates_only_nonzero_exact_frozen_lines():
    sale = calculation("sale", kind="service_sale", subject_id="sale-business")
    receipt = calculation(
        "receipt",
        kind="payment",
        fact={"payment_method": "individual", "counterparty_id": "customer", "allocations": []},
        lines=[
            {"account": "222104", "debit": 6, "credit": 0},
            {"account": "222101", "debit": 0, "credit": 6},
        ],
        values={
            "direction": "inflow",
            "settlements": [],
            "tax_transfers": [
                {
                    "source_id": "sale-business",
                    "source_calculation": "sale",
                    "vat_fen": 6,
                },
                {
                    "source_id": "sale-business",
                    "source_calculation": "sale",
                    "vat_fen": 0,
                },
            ],
        },
    )

    resolved = resolve_calculation_relations(receipt, **resolver({"sale": sale}))

    tax = [item for item in resolved["line_relations"] if item["role"] == "tax_transfer"]
    assert [(item["line_no"], item["amount_fen"], item["state"]) for item in tax] == [
        (1, 6, "resolved"),
        (2, -6, "resolved"),
    ]
    assert {item["source_calculation_id"] for item in tax} == {"sale"}


def test_formal_overpayment_keeps_recovery_and_original_payable_sources_separate():
    payable = calculation(
        "payable",
        kind="expense",
        subject_id="expense",
        values={
            "obligations": [
                {
                    "key": "expense:primary",
                    "name": "primary",
                    "account": "2202",
                    "normal": "credit",
                    "amount_fen": 100,
                    "category": "payable",
                    "counterparty_id": "supplier",
                }
            ]
        },
    )
    overpayment = calculation(
        "overpayment",
        kind="overpayment",
        subject_id="recovery",
        fact={
            "source_kind": "expense",
            "source_id": "expense",
            "obligation_name": "primary",
        },
        lines=[
            {"account": "1221", "debit": 20, "credit": 0},
            {"account": "2202", "debit": 0, "credit": 20},
        ],
        values={
            "overpayment_fen": 20,
            "obligations": [
                {
                    "key": "overpayment:recovery:primary",
                    "name": "primary",
                    "account": "1221",
                    "normal": "debit",
                    "amount_fen": 20,
                    "category": "receivable",
                    "counterparty_id": "supplier",
                }
            ],
        },
    )
    resolved = resolve_calculation_relations(
        overpayment,
        **resolver({"payable": payable}, {"overpayment": ("payable",)}),
    )

    by_line = {item["line_no"]: item for item in resolved["line_relations"]}
    assert by_line[1]["source_calculation_id"] == "overpayment"
    assert by_line[1]["role"] == "created_obligation"
    assert by_line[2]["source_calculation_id"] == "payable"
    assert by_line[2]["role"] == "overpayment_source"


def test_accepted_source_uses_its_frozen_pointer_and_recipient():
    payable = calculation(
        "payable",
        kind="expense",
        subject_id="expense",
        values={
            "obligations": [
                {
                    "key": "expense:primary",
                    "name": "primary",
                    "account": "2202",
                    "normal": "credit",
                    "amount_fen": 80,
                    "category": "payable",
                    "counterparty_id": "supplier",
                }
            ]
        },
    )
    acceptance = calculation(
        "acceptance",
        kind="reimbursement_acceptance",
        fact={
            "payer_id": "owner",
            "sources": [
                {
                    "source_kind": "expense",
                    "source_id": "expense",
                    "obligation": "primary",
                    "amount_fen": 80,
                    "recipient_id": "supplier",
                }
            ],
        },
        lines=[
            {"account": "2202", "debit": 80, "credit": 0},
            {"account": "2241", "debit": 0, "credit": 80},
        ],
        values={
            "accepted_sources": [
                {
                    "source_calculation_id": "payable",
                    "source_fact_id": "fact-payable",
                    "obligation": "expense:primary",
                    "amount_fen": 80,
                    "recipient_id": "supplier",
                }
            ],
            "obligations": [
                {
                    "key": "acceptance:primary",
                    "name": "primary",
                    "account": "2241",
                    "normal": "credit",
                    "amount_fen": 80,
                    "category": "payable",
                    "counterparty_id": "owner",
                }
            ],
        },
    )

    resolved = resolve_calculation_relations(acceptance, **resolver({"payable": payable}))

    accepted = next(item for item in resolved["settlements"] if item["mode"] == "accepted")
    assert accepted["state"] == "resolved"
    assert accepted["source_calculation_id"] == "payable"
    assert accepted["source_fact_id"] == "fact-payable"
    assert accepted["recipient_id"] == "supplier"
    assert accepted["party_key"] == ("party", "supplier")


@pytest.mark.parametrize("error", ["amount", "obligation", "source_fact_id"])
def test_reimbursement_acceptance_preserves_and_rejects_wrong_frozen_declarations(error):
    payable = calculation(
        "payable",
        kind="expense",
        subject_id="expense",
        values={
            "obligations": [
                {
                    "key": "expense:primary",
                    "name": "primary",
                    "account": "2202",
                    "normal": "credit",
                    "amount_fen": 100,
                    "category": "payable",
                    "counterparty_id": "supplier",
                }
            ]
        },
    )
    acceptance = calculation(
        "acceptance",
        kind="reimbursement_acceptance",
        fact={
            "sources": [
                {
                    "source_kind": "expense",
                    "source_id": "expense",
                    "obligation": "wrong-primary" if error == "obligation" else "primary",
                    "amount_fen": 70 if error == "amount" else 80,
                    "recipient_id": "supplier",
                }
            ]
        },
        lines=[
            {"account": "2202", "debit": 80, "credit": 0},
            {"account": "2241", "debit": 0, "credit": 80},
        ],
        values={
            "accepted_sources": [
                {
                    "source_calculation_id": "payable",
                    "source_fact_id": "wrong-fact-id"
                    if error == "source_fact_id"
                    else "fact-payable",
                    "obligation": "expense:primary",
                    "amount_fen": 80,
                    "recipient_id": "supplier",
                }
            ]
        },
    )

    resolved = resolve_calculation_relations(acceptance, **resolver({"payable": payable}))
    accepted = resolved["settlements"][0]

    assert accepted["state"] == "unresolved"
    assert accepted["amount_fen"] == 80
    assert accepted["source_fact_id"] == (
        "wrong-fact-id" if error == "source_fact_id" else "fact-payable"
    )
    assert accepted["obligation_key"] == "expense:primary"
    assert accepted["obligation_name"] == ("wrong-primary" if error == "obligation" else "primary")
    assert accepted["party_key"] is None
    assert {item["field"] for item in resolved["issues"]} == {"query_source.accepted"}


@pytest.mark.parametrize("error", ["amount", "source_fact_id"])
def test_managed_reserve_acceptance_requires_fact_and_frozen_amount_to_match(error):
    payable = calculation(
        "payable",
        kind="expense",
        subject_id="expense",
        values={
            "obligations": [
                {
                    "key": "expense:primary",
                    "name": "primary",
                    "account": "2202",
                    "normal": "credit",
                    "amount_fen": 100,
                    "category": "payable",
                    "counterparty_id": "supplier",
                }
            ]
        },
    )
    settlement = calculation(
        "reserve-settlement",
        kind="managed_reserve_obligation_settlement",
        fact={
            "recipient_id": "supplier",
            "sources": [
                {
                    "source_kind": "expense",
                    "source_id": "expense",
                    "obligation": "primary",
                    "amount_fen": 70 if error == "amount" else 80,
                }
            ],
        },
        lines=[
            {"account": "2202", "debit": 80, "credit": 0},
            {"account": "5602", "debit": 0, "credit": 80},
        ],
        values={
            "accepted_sources": [
                {
                    "source_calculation_id": "payable",
                    "obligation": "expense:primary",
                    "amount_fen": 80,
                }
                | ({"source_fact_id": "wrong-fact-id"} if error == "source_fact_id" else {})
            ]
        },
    )

    resolved = resolve_calculation_relations(settlement, **resolver({"payable": payable}))

    assert resolved["settlements"][0]["state"] == "unresolved"
    assert {item["field"] for item in resolved["issues"]} == {"query_source.accepted"}


def test_explicit_classification_cannot_collapse_an_exact_aggregate_split():
    grouped = calculation(
        "grouped",
        lines=[
            {"account": "1601", "debit": 200, "credit": 0},
            {"account": "2241", "debit": 0, "credit": 200},
        ],
        values={
            "obligations": [
                {
                    "key": party,
                    "name": party,
                    "account": "2241",
                    "normal": "credit",
                    "amount_fen": 100,
                    "category": "payable",
                    "counterparty_id": party,
                }
                for party in ("alice", "bob")
            ]
        },
    )
    resolved = resolve_calculation_relations(grouped, **resolver({}))

    split = report_party_splits(
        {"account": "2241", "amount": -200, "line_no": 2},
        resolved,
        explicit_party_id="alice",
    )

    assert split["splits"] == ((("party", "alice"), -100), (("party", "bob"), -100))
    assert {item["field"] for item in split["issues"]} == {"counterparty_id"}
