import pytest

from ai_accounting_modular.contracts.public import ContractError, validate_contract
from ai_accounting_modular.reading import _financial_projection

from ._support import PARTY, assemble, expense, payment, scope


def ready(book, quarter=1):
    status, initial = book.preview(quarter)
    assert status == 200 and initial["state"] == "preparing"
    assert "response" not in initial
    assert book.reading.run_pending(book.company)
    status, delivery = book.preview(quarter)
    assert status == 200 and delivery["state"] == "ready", delivery
    return delivery


def test_real_expense_payment_three_tables_match_all_214_formal_cells(tmp_path):
    book = assemble(tmp_path)
    cost = expense()
    book.submit(cost, payment(cost))
    delivery = ready(book)
    response = delivery["response"]
    assert response["summary"] == {
        "assets_total_fen": "-6000",
        "liabilities_total_fen": "4000",
        "liabilities_equity_total_fen": "-6000",
        "current_net_profit_fen": "-10000",
        "year_to_date_net_profit_fen": "-10000",
        "current_cash_change_fen": "-6000",
        "ending_cash_fen": "-6000",
    }
    with book.storage.read_unit(book.company) as read:
        snapshot = book.ledger.financial_snapshot(scope(book.company), read)
    pairs = {
        "balance_sheet": (("ending", "ending_fen"), ("beginning", "beginning_fen")),
        "profit_statement": (("current", "current_fen"), ("year_to_date", "year_to_date_fen")),
        "cash_flow_statement": (("current", "current_fen"), ("year_to_date", "year_to_date_fen")),
    }
    count = 0
    for statement in response["statements"]:
        for row in statement["rows"]:
            formal = snapshot[statement["key"]][str(row["line"])]
            assert row["name"] == formal["name"]
            for cell, old in pairs[statement["key"]]:
                expected = formal[cell].get("amount_fen")
                assert row["values"][old] == expected
                count += 1
    assert count == 214
    validate_contract("dashboard-response-v2.json", "dashboard_quarterly_report", response)
    assert response["export"] == {
        "available": False,
        "file_name": "2026年第1季度财务报表.xlsx",
        "preview_digest": None,
        "epochs": None,
    }


def test_missing_details_are_partial_ready_not_endless_preparing_or_zero(tmp_path):
    book = assemble(tmp_path)
    cost = expense()
    cost["data"].pop("report_details")
    book.submit(cost)
    delivery = ready(book)
    report = delivery["response"]
    assert delivery["state"] == "ready"
    assert report["status"] == report["readiness_state"] == "blocked"
    assert report["headline"] == "部分报表资料待核对"
    assert report["summary"]["current_net_profit_fen"] == "-10000"
    profit = next(s for s in report["statements"] if s["key"] == "profit_statement")
    assert any(value is None for row in profit["rows"] for value in row["values"].values())
    assert report["export"]["available"] is False


def test_hot_read_uses_no_formal_recalculation_and_global_changes_keep_scope_version(
    tmp_path, monkeypatch
):
    book = assemble(tmp_path)
    book.submit(expense())
    initial = ready(book)
    book.submit(expense("future", "9000", "2026-04"))

    def unexpected(*args, **kwargs):
        pytest.fail("热读重算了三表")

    for method in (
        "financial_snapshot",
        "prepare_financial_projection",
        "read_financial_projection",
    ):
        monkeypatch.setattr(book.ledger, method, unexpected)
    assert book.mark("2026-04") == ()
    _, current = book.preview()
    assert current["state"] == "ready"
    assert current["model_version"] == initial["model_version"]


def test_related_change_prepares_then_keeps_or_changes_semantic_version(tmp_path):
    book = assemble(tmp_path)
    cost = expense()
    book.submit(cost)
    before = ready(book)
    book.mark("2026-01")
    assert book.preview()[1]["state"] == "preparing"
    book.reading.run_pending(book.company)
    assert book.preview()[1]["model_version"] == before["model_version"]
    cost = expense(amount="12000")
    book.submit(mutations=[{"op": "replace", "expected_seq": 1, "fact": cost}])
    book.mark("2026-01")
    book.reading.run_pending(book.company)
    current = book.preview()[1]
    assert current["model_version"] != before["model_version"]
    assert current["response"]["summary"]["current_net_profit_fen"] == "-12000"
    with pytest.raises(ContractError, match="已变化"):
        book.reading.page(
            "quarterly_report", scope(book.company) | {"expected_head": before["model_version"]}, {}
        )


def test_25_real_payments_do_not_limit_the_complete_quarter_amount(tmp_path):
    book = assemble(tmp_path)
    costs = [expense("expense-" + str(i), "10000") for i in range(25)]
    book.submit(
        *costs, *(payment(cost, "6000", name="payment-" + str(i)) for i, cost in enumerate(costs))
    )
    report = ready(book)["response"]
    assert report["summary"]["current_net_profit_fen"] == "-250000"
    assert report["summary"]["current_cash_change_fen"] == "-150000"


def test_context_empty_selected_company_and_established_periods(tmp_path):
    book = assemble(tmp_path, established=False)
    status, context = book.get("/api/dashboard/context", company_id=book.company)
    assert status == 200
    assert context["current_company"]["company_id"] == book.company
    assert context["periods"] == context["quarters"] == []
    assert context["default_period"] is context["default_quarter"] is None
    established = assemble(tmp_path / "other")
    _, context = established.get("/api/dashboard/context", company_id=established.company)
    assert context["default_period"] == "2026-01"
    assert context["default_quarter"] == "2026-Q1"


@pytest.mark.parametrize(
    "params",
    [
        [("company_id", "x"), ("year", "2026"), ("quarter", "1")],
        [("year", "2026"), ("quarter", "1")],
        [("company_id", "x"), ("year", "2026"), ("year", "2027"), ("quarter", "1")],
    ],
)
def test_invalid_quarter_url_is_400_not_a_partial_page(tmp_path, params):
    book = assemble(tmp_path)
    with book.dashboard.get("/api/dashboard/quarterly-report", params) as response:
        assert response.status == 400
        assert response.body["code"] == "invalid_command"


def test_display_change_does_not_recalculate_unrelated_financial_component(tmp_path):
    book = assemble(tmp_path)
    before = ready(book)
    book.submit(
        objects=[
            {
                "object_id": PARTY,
                "kind": "organization",
                "name": "交易方新名字",
                "expected_seq": 1,
                "data": {},
            }
        ]
    )
    assert book.preview()[1]["model_version"] == before["model_version"]


def test_old_model_cannot_publish_over_a_newer_target(tmp_path, monkeypatch):
    book = assemble(tmp_path)
    book.submit(expense())
    book.preview()
    assert _financial_projection.produce(book.reading, book.company)
    original = book.ledger.read_financial_projection

    def moving(scope_value, read, base):
        snapshot = original(scope_value, read, base)
        book.mark("2026-01")
        return snapshot

    monkeypatch.setattr(book.ledger, "read_financial_projection", moving)
    assert book.reading.run_pending(book.company) == ()
    assert book.preview()[1]["state"] == "preparing"
    monkeypatch.setattr(book.ledger, "read_financial_projection", original)
    assert book.reading.run_pending(book.company)
    assert book.preview()[1]["state"] == "ready"


def test_late_failed_worker_cannot_erase_another_workers_success(tmp_path, monkeypatch):
    book = assemble(tmp_path)
    book.preview()
    assert _financial_projection.produce(book.reading, book.company)
    original = book.ledger.read_financial_projection

    def late_failure(*args):
        monkeypatch.setattr(book.ledger, "read_financial_projection", original)
        assert book.reading.run_pending(book.company)
        raise RuntimeError("synthetic old worker failed")

    monkeypatch.setattr(book.ledger, "read_financial_projection", late_failure)
    assert book.reading.run_pending(book.company) == ()
    assert book.preview()[1]["state"] == "ready"


def test_production_failure_is_stable_and_explicit_retry_rebuilds(tmp_path, monkeypatch):
    book = assemble(tmp_path)
    book.preview()
    original = book.ledger.prepare_financial_projection

    def failure(*args, **kwargs):
        raise RuntimeError("synthetic failure with private details")

    monkeypatch.setattr(book.ledger, "prepare_financial_projection", failure)
    book.reading.run_pending(book.company)
    status, failed = book.preview()
    assert status == 200 and failed["state"] == "failed"
    assert failed["reason"] == "production_failed"
    assert "response" not in failed
    assert book.reading.run_pending(book.company) == ()
    monkeypatch.setattr(book.ledger, "prepare_financial_projection", original)
    book.reading.retry("quarterly_report", scope(book.company))
    assert book.preview()[1]["state"] == "preparing"
    assert book.reading.run_pending(book.company)
    assert book.preview()[1]["state"] == "ready"


def test_report_total_markers_and_column_labels_preserve_existing_styles(tmp_path):
    book = assemble(tmp_path)
    tables = ready(book)["response"]["statements"]
    expected = ({15, 29, 30, 41, 47, 52, 53}, {19, 21, 30, 32}, {7, 13, 19, 20, 22})
    for table, markers in zip(tables, expected, strict=True):
        assert {row["line"] for row in table["rows"] if row["is_total"]} == markers
    assert tables[1]["columns"][1]["label"] == tables[2]["columns"][1]["label"] == "本年累计"


def test_close_then_later_correction_keeps_the_frozen_quarter(tmp_path):
    book = assemble(tmp_path)
    original = expense()
    book.submit(original)
    before = ready(book)
    with book.storage.write_unit(book.company) as unit:
        check = book.ledger.check_period("2026-03", unit)
        book.ledger.close_period("2026-03", check.head, unit)
        from ai_accounting_modular.reading.public import PeriodRange, ReadingImpact

        book.reading.mark_targets(
            ReadingImpact(
                closure_ranges=(PeriodRange("2026-01", "2026-03"),),
            ),
            unit,
        )
    book.reading.run_pending(book.company)
    _, closed = book.preview()
    assert closed["response"]["close_state"] == "closed"
    assert closed["response"]["draft"] is False
    assert closed["response"]["summary"] == before["response"]["summary"]
    book.submit(
        mutations=[{"op": "replace", "expected_seq": 1, "fact": expense(amount="12000")}],
        correction_period="2026-04",
    )
    assert book.mark("2026-04") == ()
    assert book.preview()[1]["model_version"] == closed["model_version"]


def test_corrupt_read_component_fails_without_affecting_formal_books(tmp_path):
    book = assemble(tmp_path)
    book.submit(expense())
    before = ready(book)
    with book.storage.read_unit(book.company) as read:
        formal_head = read.head
    with book.storage.maintenance_unit(book.company) as unit:
        unit.repository("reading")._session.execute("UPDATE read_component SET payload_json='{}'")
    status, delivery = book.preview()
    assert status == 200 and delivery["state"] == "failed"
    assert delivery["reason"] == "corrupt" and "response" not in delivery
    with book.storage.read_unit(book.company) as read:
        assert read.head == formal_head
        assert (
            book.ledger.financial_snapshot(scope(book.company), read)["profit_statement"]["32"][
                "current"
            ]["amount_fen"]
            == "-10000"
        )
    book.reading.retry("quarterly_report", scope(book.company))
    book.reading.run_pending(book.company)
    assert book.preview()[1]["model_version"] == before["model_version"]
