"""Report preparation resolves synthetic live publications without business writes."""

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from test_platforms import book as book
from test_platforms import current

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.replay_report_classification import (
    ReplayReportClassificationItem,
    _prepare_details,
    compile_report_classifications,
    prepare_source_report_classifications,
)


@pytest.fixture(autouse=True)
def adopted_opening(book):
    _engine, save, publish, _proof = book
    save(
        "opening_package",
        "opening",
        {
            "period": "2026-09",
            "package_id": "opening",
            "members": [],
            "counts": {
                "bank": 0,
                "cash": 0,
                "counterparties": 0,
                "assets": 0,
                "loans": 0,
                "taxes": 0,
                "payroll_payables": 0,
                "payroll_cumulative": 0,
                "equity": 0,
            },
            "completeness_confirmed": True,
        },
    )
    publish("opening")


def test_expense_and_actual_payment_cash_line_use_source_semantics(book):
    engine, save, publish, _ = book
    save(
        "expense",
        "expense",
        {
            "period": "2026-09",
            "counterparty_id": "supplier",
            "amount_fen": 1000,
            "expense_class": "administration",
            "creditor_kind": "supplier",
        },
    )
    publish("expense")
    expense_calc = current(engine, "expense", "expense")
    profit = prepare_source_report_classifications(
        engine,
        [
            request(
                "expense",
                expense_calc.id,
                profit=[{"detail_code": "management_other", "amount_fen": 1000}],
            )
        ],
    )[0]
    assert profit["status"] == "ready"
    assert profit["data"]["profit_details"][0]["line_no"] == 1
    save(
        "payment",
        "paid",
        {
            "period": "2026-09",
            "actual_date": "2026-09-11",
            "direction": "outflow",
            "bank_account_id": "bank",
            "counterparty_id": "supplier",
            "amount_fen": 1000,
            "allocations": [
                {
                    "source_kind": "expense",
                    "source_id": "expense",
                    "obligation": "primary",
                    "amount_fen": 1000,
                }
            ],
        },
    )
    publish("paid")
    payment_calc = current(engine, "payment", "paid")
    cash = prepare_source_report_classifications(
        engine, [request("paid", payment_calc.id, cash=[{"category": 6, "amount_fen": 1000}])]
    )[0]
    assert cash["status"] == "ready"
    assert cash["data"]["cash_details"][0]["line_no"] == 2
    with engine.store.connection(read_only=True) as connection:
        definition = connection.execute(
            "SELECT parent_account,detail_code FROM fund_account_definition WHERE entity_id='bank'"
        ).fetchone()
        assert definition["parent_account"] == "1002"
        assert definition["detail_code"].startswith("1002.")


def request(subject, calculation_id, *, profit=(), cash=(), period="2026-09"):
    return {
        "source_id": "synthetic-source",
        "source_subject_id": subject,
        "source_calculation_id": calculation_id,
        "period": period,
        "profit_details": list(profit),
        "cash_details": list(cash),
        "counterparty_details": [],
    }


@pytest.mark.parametrize(
    "kind,expected_line", [("managed_reserve_expense", 1), ("managed_reserve_refund", 2)]
)
def test_exact_live_profit_detail_handles_both_expense_and_credit_refund(book, kind, expected_line):
    engine, save, publish, _ = book
    save(
        kind,
        "reserve",
        {
            "period": "2026-09",
            "actual_date": "2026-09-10",
            "amount_fen": 1000,
            "bank_account_id": "bank",
        },
    )
    publish("reserve")
    calc = current(engine, kind, "reserve")
    result = prepare_source_report_classifications(
        engine,
        [
            request(
                "reserve", calc.id, profit=[{"detail_code": "management_other", "amount_fen": 1000}]
            )
        ],
    )[0]
    assert result["status"] == "ready"
    assert result["data"]["profit_details"] == [
        {"line_no": expected_line, "detail_code": "management_other", "amount_fen": 1000}
    ]
    assert result["data"]["voucher_version_id"]
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM subject WHERE kind='report_classification'"
            ).fetchone()[0]
            == 0
        )


def test_actual_cash_direction_and_business_category_are_preserved(book):
    engine, save, publish, _ = book
    save(
        "managed_reserve_expense",
        "reserve",
        {
            "period": "2026-09",
            "actual_date": "2026-09-10",
            "amount_fen": 1000,
            "bank_account_id": "bank",
        },
    )
    publish("reserve")
    calc = current(engine, "managed_reserve_expense", "reserve")
    correct = prepare_source_report_classifications(
        engine, [request("reserve", calc.id, cash=[{"category": 6, "amount_fen": 1000}])]
    )[0]
    assert correct["status"] == "ready"
    assert correct["data"]["cash_details"] == [{"line_no": 2, "category": 6, "amount_fen": 1000}]
    wrong = prepare_source_report_classifications(
        engine, [request("reserve", calc.id, cash=[{"category": 2, "amount_fen": 1000}])]
    )[0]
    assert wrong["status"] == "needs_information"
    assert wrong["data"] is None


def test_amount_threshold_does_not_select_larger_cash_line(book):
    engine, save, publish, _ = book
    save(
        "managed_reserve_expense",
        "reserve",
        {
            "period": "2026-09",
            "actual_date": "2026-09-10",
            "amount_fen": 1000,
            "bank_account_id": "bank",
        },
    )
    publish("reserve")
    calc = current(engine, "managed_reserve_expense", "reserve")
    result = prepare_source_report_classifications(
        engine, [request("reserve", calc.id, cash=[{"category": 6, "amount_fen": 900}])]
    )[0]
    assert result["status"] == "needs_information"
    assert result["fact_issues"][0]["candidate_count"] == 0


def test_wrong_period_and_pending_source_block_preparation(book):
    engine, save, publish, _ = book
    save(
        "expense",
        "reserve",
        {
            "period": "2026-09",
            "amount_fen": 1000,
            "counterparty_id": "supplier",
            "expense_class": "administration",
            "creditor_kind": "supplier",
        },
    )
    publish("reserve")
    calc = current(engine, "expense", "reserve")
    item = request(
        "reserve", calc.id, profit=[{"detail_code": "management_other", "amount_fen": 1000}]
    )
    wrong = prepare_source_report_classifications(engine, [{**item, "period": "2026-08"}])[0]
    assert wrong["fact_issues"][0]["code"] == "classification_period_conflict"
    save(
        "expense",
        "reserve",
        {
            "period": "2026-09",
            "amount_fen": 1100,
            "counterparty_id": "supplier",
            "expense_class": "administration",
            "creditor_kind": "supplier",
        },
        revision=1,
    )
    pending = prepare_source_report_classifications(engine, [item])[0]
    assert pending["status"] == "needs_information"
    assert pending["fact_issues"][0]["code"] == "classification_source_not_current"


def test_unknown_or_stale_calculation_is_a_structured_issue(book):
    engine = book[0]
    result = prepare_source_report_classifications(
        engine,
        [
            request(
                "other-company-source",
                "old-calculation",
                profit=[{"detail_code": "management_other", "amount_fen": 1000}],
            )
        ],
    )[0]
    assert result["status"] == "needs_information"
    assert result["data"] is None


def test_ambiguous_same_amount_expense_rows_do_not_choose_first():
    item = ReplayReportClassificationItem.model_validate_json(
        json.dumps(
            request(
                "subject", "calc", profit=[{"detail_code": "management_other", "amount_fen": 1000}]
            )
        )
    )
    calculation = {"kind": "expense", "fact_data": {}, "outcome": {"values": {}}}
    rows = [
        {"line_no": number, "account": "5602", "debit": 1000, "credit": 0, "cashflow": None}
        for number in (1, 2)
    ]
    result = _prepare_details(SimpleNamespace(), item, calculation, "new-version", rows)
    assert result["status"] == "needs_information"
    assert result["fact_issues"][0]["candidate_count"] == 2


def test_wire_model_forbids_old_lines_accounts_and_version_ids():
    item = request(
        "subject", "calc", profit=[{"detail_code": "management_other", "amount_fen": 1000}]
    )
    for extra in (
        {"voucher_version_id": "old-version"},
        {"account": "5602"},
        {"direction": "debit"},
    ):
        with pytest.raises(ValidationError):
            ReplayReportClassificationItem.model_validate_json(json.dumps({**item, **extra}))
    item["profit_details"][0]["line_no"] = 1
    with pytest.raises(ValidationError):
        ReplayReportClassificationItem.model_validate_json(json.dumps(item))


def test_physical_voucher_rows_must_match_verified_source_content(book, monkeypatch):
    from ai_accounting.kernel.query_reads import QueryReads

    engine, save, publish, _ = book
    save(
        "managed_reserve_expense",
        "reserve",
        {
            "period": "2026-09",
            "actual_date": "2026-09-10",
            "amount_fen": 1000,
            "bank_account_id": "bank",
        },
    )
    publish("reserve")
    calc = current(engine, "managed_reserve_expense", "reserve")
    original = QueryReads.voucher_lines

    def corrupt_rows(reads, identifiers):
        rows = original(reads, identifiers)
        for lines in rows.values():
            lines[0] = {**lines[0], "debit": lines[0]["debit"] + 1}
        return rows

    monkeypatch.setattr(QueryReads, "voucher_lines", corrupt_rows)
    with pytest.raises(KernelError, match="凭证行"):
        prepare_source_report_classifications(
            engine,
            [
                request(
                    "reserve",
                    calc.id,
                    profit=[{"detail_code": "management_other", "amount_fen": 1001}],
                )
            ],
        )


def test_compiler_strips_old_positions_and_saves_only_actual_prepared_data():
    from ai_accounting.kernel.replay_document import ReplayDocumentBuilder

    document = {
        "facts": [
            {
                "key": "classification",
                "kind": "report_classification",
                "data": {
                    "period": "2026-09",
                    "voucher_version_id": "$ref:expense:voucher_version_id",
                    "profit_details": [
                        {"line_no": 99, "detail_code": "management_other", "amount_fen": 1000}
                    ],
                    "counterparties": [],
                    "cash_details": [],
                },
                "source_keys": ["proof"],
            }
        ]
    }
    builder = ReplayDocumentBuilder(
        document,
        company_id="fresh-company",
        database_id="fresh-db",
        owner_confirmation="a" * 64,
        request_id="fresh-request",
    )
    builder.source_digests["proof"] = "a" * 64
    builder.publication_steps["expense"] = "synthetic-publication"
    compile_report_classifications(builder, document)
    assert not builder.issues
    preparation, saved = builder.steps
    assert preparation["payload"]["items"][0]["profit_details"] == [
        {"detail_code": "management_other", "amount_fen": 1000}
    ]
    assert preparation["depends_on"] == ["synthetic-publication"]
    assert saved["payload"]["facts"][0]["data"] == "$ref:classification-prepare:classification:data"
    assert saved["depends_on"] == [preparation["step_id"]]
    assert (
        builder.bindings["classification-prepare:classification", "data"]["field"] == "items.0.data"
    )
