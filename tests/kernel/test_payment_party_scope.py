"""Explicitly grouped payments and unnamed entrusted liabilities retain real sources."""

import pytest
from pydantic import ValidationError
from test_banking import book as book
from test_banking import entry, funding, match, opening, reconciliation, statement
from test_cash import close_transactions
from test_dashboard_projection import diagnostic_position
from test_query_semantics import calculation, resolver

from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.contracts import KernelError, NeedsInformation
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.domains.transactions import PassThrough, Payment
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.query_semantics import (
    classify_financial_position,
    report_party_splits,
    resolve_calculation_relations,
)
from ai_accounting.kernel.response_contracts import validate_response
from ai_accounting.kernel.types import canonical


def grouped_data():
    return {
        "period": "2026-09",
        "actual_date": "2026-09-10",
        "direction": "outflow",
        "bank_account_id": "bank-a",
        "counterparty_id": None,
        "payment_method": "bank_batch",
        "amount_fen": 300,
        "allocations": [
            {
                "source_kind": "expense",
                "source_id": name,
                "obligation": "primary",
                "amount_fen": amount,
                "recipient_id": name,
            }
            for name, amount in (("alice", 100), ("bob", 200))
        ],
    }


def payable_sources(save, publish):
    for name, amount in (("alice", 100), ("bob", 200)):
        save(
            "expense",
            name,
            {
                "period": "2026-09",
                "counterparty_id": name,
                "amount_fen": amount,
                "expense_class": "administration",
                "creditor_kind": "employee",
            },
        )
    publish("alice", "bob")


def current_vouchers(engine):
    with engine.store.connection(read_only=True) as connection:
        return [tuple(row) for row in connection.execute("SELECT * FROM voucher_current")]


def test_group_without_single_party_preserves_two_original_rows_and_each_recipient(book):
    engine, save, publish, _ = book
    opening(save, publish)
    funding(save, publish)
    payable_sources(save, publish)
    saved = save("payment", "batch", grouped_data())
    publish("batch")
    posted = engine.ledger("2026-09")
    statement(
        save,
        publish,
        [entry(), entry("first", "2026-09-10", -130), entry("second", "2026-09-10", -170)],
    )
    reconciliation(
        save,
        publish,
        [match(), match("first", "payment", "batch"), match("second", "payment", "batch")],
    )
    assert engine.ledger("2026-09") == posted
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT amount FROM balance WHERE category='bank' AND balance_key='bank-a'"
            ).fetchone()[0]
            == 700
        )
        references = connection.execute(
            "SELECT path,entity_id FROM entity_reference_recorded WHERE fact_id=? ORDER BY path",
            (saved["fact_id"],),
        ).fetchall()
        assert [tuple(row) for row in references] == [
            ("allocations.0.recipient_id", "alice"),
            ("allocations.1.recipient_id", "bob"),
            ("bank_account_id", "bank-a"),
        ]
        assert (
            connection.execute(
                "SELECT count(*) FROM fact_bank_statement_entries "
                "WHERE reference IN ('first','second')"
            ).fetchone()[0]
            == 2
        )
    view = Dashboard(engine).funds("2026-09", section="statements")
    validate_response("dashboard_funds", view)
    rows = [
        row
        for row in view["data"]["collections"]["statements"]["items"]
        if row.get("batch_payment")
    ]
    assert [row["batch_payment"]["bank_row_count"] for row in rows] == [2, 2]
    assert all(
        [item["amount_fen"] for item in row["batch_payment"]["items"]] == [100, 200] for row in rows
    )
    assert BusinessQueries(engine).business_status("batch", "2026-09")
    validate_response(
        "dashboard_business_status", Dashboard(engine).business_status("2026-09", "batch")
    )


@pytest.mark.parametrize("fault", ["missing_recipient", "wrong_recipient", "unknown_recipient"])
def test_nullable_batch_party_does_not_weaken_real_recipient_checks(book, fault):
    engine, save, publish, proof = book
    opening(save, publish)
    payable_sources(save, publish)
    before = current_vouchers(engine)
    data = grouped_data()
    if fault == "missing_recipient":
        data["allocations"][1]["recipient_id"] = None
    elif fault == "wrong_recipient":
        data["allocations"][1]["recipient_id"] = "alice"
    else:
        data["allocations"][1]["recipient_id"] = "unregistered-person"
        with pytest.raises(KernelError) as failure:
            engine.save_fact(
                "payment",
                "batch",
                data,
                evidence=[proof],
                expected_revision=0,
                request_id="unknown-recipient",
            )
        assert failure.value.code == "needs_information"
        assert current_vouchers(engine) == before
        return
    save("payment", "batch", data)
    with pytest.raises(
        NeedsInformation if fault == "missing_recipient" else KernelError
    ) as failure:
        publish("batch")
    assert failure.value.code == (
        "needs_information" if fault == "missing_recipient" else "payment_party_conflict"
    )
    assert current_vouchers(engine) == before


def test_individual_and_omitted_party_remain_invalid_and_group_total_stays_exact():
    data = grouped_data()
    data["payment_method"] = "individual"
    with pytest.raises(ValidationError, match="actual counterparty"):
        Payment.model_validate_json(canonical(data))
    data = grouped_data()
    del data["counterparty_id"]
    with pytest.raises(ValidationError):
        Payment.model_validate_json(canonical(data))
    data = grouped_data()
    data["amount_fen"] = 301
    with pytest.raises(ValidationError, match="exactly account"):
        Payment.model_validate_json(canonical(data))


def agency_data(**changes):
    return {
        "period": "2026-09",
        "payer_id": "payer",
        "beneficiary_id": None,
        "amount_fen": 1000,
        "rights_and_obligation_confirmed": True,
        **changes,
    }


def agency_payment(*, direction, party, amount, obligation, month="2026-09"):
    return {
        "period": month,
        "actual_date": month + "-20",
        "direction": direction,
        "bank_account_id": "bank-a",
        "counterparty_id": party,
        "amount_fen": amount,
        "allocations": [
            {
                "source_kind": "pass_through",
                "source_id": "agency",
                "obligation": obligation,
                "amount_fen": amount,
            }
        ],
    }


def test_explicit_unnamed_rightsholder_keeps_liability_without_fake_entity(book):
    engine, save, publish, _ = book
    saved = save("pass_through", "agency", agency_data())
    publish("agency")
    with engine.store.connection(read_only=True) as connection:
        assert [
            tuple(row)
            for row in connection.execute(
                "SELECT path,entity_id FROM entity_reference_recorded WHERE fact_id=?",
                (saved["fact_id"],),
            )
        ] == [("payer_id", "payer")]
        assert (
            connection.execute("SELECT 1 FROM fact_scope WHERE scope_key='party:None'").fetchone()
            is None
        )
        balances = [
            tuple(row)
            for row in connection.execute("SELECT category,amount FROM balance ORDER BY category")
        ]
        assert balances == [("payable", 1000), ("receivable", 1000)]
    save(
        "payment",
        "collection",
        agency_payment(direction="inflow", party="payer", amount=1000, obligation="collection"),
    )
    publish("collection")
    save(
        "payment",
        "remittance",
        agency_payment(
            direction="outflow", party="actual-recipient", amount=400, obligation="remittance"
        ),
    )
    publish("remittance")
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT amount FROM balance WHERE balance_key='pass_through:agency:remittance'"
            ).fetchone()[0]
            == 600
        )
        assert engine.store.fact(connection, saved["fact_id"]).fact.beneficiary_id is None
    assert BusinessQueries(engine).business_status("agency", "2026-09")
    dashboard = Dashboard(engine)
    brief = dashboard.brief("2026-09")
    validate_response("dashboard_brief", brief)
    assert brief["data"]["position"] == {
        "month_revenue_fen": 0, "month_expense_fen": 0,
        "month_result_fen": 0, "complete": True,
    }
    funds = dashboard.funds("2026-09")["data"]
    assert funds["inflow_fen"] == 1000 and funds["outflow_fen"] == 400
    assert funds["total_fen"] == 600
    position = diagnostic_position(engine, "2026-09")
    assert position["complete"] and position["equation_valid"] is True
    assert position["assets_fen"] == position["liabilities_fen"] == 600
    assert position["issues"] == []
    validate_response("dashboard_business_status", dashboard.business_status("2026-09", "agency"))


@pytest.mark.parametrize("confirmed", [None, False])
def test_unknown_beneficiary_never_defaults_entrusted_rights_or_obligation(book, confirmed):
    engine, save, publish, _ = book
    save("pass_through", "agency", agency_data(rights_and_obligation_confirmed=confirmed))
    before = current_vouchers(engine)
    with pytest.raises(NeedsInformation) as failure:
        publish("agency")
    assert failure.value.code == "needs_information"
    assert current_vouchers(engine) == before


def test_unknown_beneficiary_must_be_explicit_and_known_recipient_still_binding(book):
    data = agency_data()
    del data["beneficiary_id"]
    with pytest.raises(ValidationError):
        PassThrough.model_validate(data)
    engine, save, publish, _ = book
    save("pass_through", "agency", agency_data(beneficiary_id="beneficiary"))
    publish("agency")
    before = current_vouchers(engine)
    save(
        "payment",
        "wrong-payee",
        agency_payment(
            direction="outflow", party="other-person", amount=400, obligation="remittance"
        ),
    )
    with pytest.raises(KernelError) as failure:
        publish("wrong-payee")
    assert failure.value.code == "payment_party_conflict"
    assert current_vouchers(engine) == before


@pytest.mark.parametrize("fault", ["wrong_payer", "over_remittance", "wrong_direction"])
def test_unnamed_beneficiary_does_not_relax_payer_direction_or_capacity(book, fault):
    engine, save, publish, _ = book
    save("pass_through", "agency", agency_data())
    publish("agency")
    before = current_vouchers(engine)
    data = agency_payment(
        direction="inflow" if fault != "over_remittance" else "outflow",
        party="other-person" if fault == "wrong_payer" else "payer",
        amount=1001 if fault == "over_remittance" else 100,
        obligation="collection" if fault == "wrong_payer" else "remittance",
    )
    save("payment", "invalid-settlement", data)
    with pytest.raises(KernelError) as failure:
        publish("invalid-settlement")
    assert (
        failure.value.code
        == {
            "wrong_payer": "payment_party_conflict",
            "over_remittance": "needs_information",
            "wrong_direction": "payment_direction_conflict",
        }[fault]
    )
    assert current_vouchers(engine) == before


def test_later_true_recipient_settlement_does_not_rewrite_frozen_unnamed_source(book):
    engine, save, publish, proof = book
    save("pass_through", "agency", agency_data())
    publish("agency")
    frozen = close_transactions(engine, proof, "2026-09")
    save(
        "payment",
        "later-remittance",
        agency_payment(
            direction="outflow",
            party="actual-recipient",
            amount=400,
            obligation="remittance",
            month="2026-10",
        ),
    )
    publish("later-remittance")
    assert Periods(engine).closed_report("2026-09") == frozen
    assert engine.overview("2026-09")["pending"] == []


def anonymous_calculation(subject, amount=1000, **fact_changes):
    return calculation(
        subject,
        kind="pass_through",
        fact=agency_data(amount_fen=amount, **fact_changes),
        lines=[
            {"account": "122105", "debit": amount, "credit": 0},
            {"account": "224105", "debit": 0, "credit": amount},
        ],
        values={
            "obligations": [
                {
                    "name": "remittance",
                    "key": f"pass_through:{subject}:remittance",
                    "amount_fen": amount,
                    "account": "224105",
                    "normal": "credit",
                    "category": "payable",
                    "counterparty_id": None,
                }
            ]
        },
    )


def test_anonymous_responsibilities_use_distinct_sources_even_with_opposite_report_signs():
    rows = []
    keys = []
    # Opposite source signs can occur during explicit corrections. They must
    # not be collapsed into one anonymous person by the report reclassifier.
    for subject, signed in (("first-agency", -1000), ("second-agency", 700)):
        source = anonymous_calculation(subject, abs(signed))
        relations = resolve_calculation_relations(source, **resolver({subject: source}))
        original_row = {"account": "224105", "amount": -abs(signed), "line_no": 2}
        party = report_party_splits(original_row, relations)
        assert party["state"] == "resolved" and party["party_id"] is None
        assert party["issues"] == []
        assert party["splits"] == ((party["party_key"], -abs(signed)),)
        keys.append(party["party_key"])
        rows.append({"account": "224105", "amount": signed, "party_key": party["party_key"]})
    assert keys[0] != keys[1]
    position = classify_financial_position(rows)
    assert position["lines"][8] == 700
    assert position["lines"][39] == 1000


@pytest.mark.parametrize("fault", ["legacy_missing_field", "unconfirmed", "other_business"])
def test_source_key_resolution_does_not_reinterpret_unsupported_frozen_unknowns(fault):
    source = anonymous_calculation("agency")
    if fault == "legacy_missing_field":
        del source["fact_data"]["beneficiary_id"]
    elif fault == "unconfirmed":
        source["fact_data"]["rights_and_obligation_confirmed"] = False
    else:
        source["kind"] = "expense"
    resolution = resolve_calculation_relations(source, **resolver({"agency": source}))
    party = report_party_splits({"account": "224105", "amount": -1000, "line_no": 2}, resolution)
    assert party["state"] == "unresolved"
    assert party["party_key"] is None


def test_position_issue_response_retains_exact_locator_without_arbitrary_fields():
    from pydantic import TypeAdapter

    from ai_accounting.kernel.response_contracts import BriefPositionIssue

    issue = {
        "field": "financial_position.party",
        "message": "未建立精确往来范围",
        "semantics": "accounting",
        "account": "224105",
        "version_id": "synthetic-version",
        "voucher_version_id": None,
        "line_no": 2,
        "affected_lines": [8, 39],
    }
    adapter = TypeAdapter(BriefPositionIssue)
    assert adapter.validate_python(issue) == issue
    with pytest.raises(ValidationError):
        adapter.validate_python({**issue, "arbitrary_field": "not allowed"})
