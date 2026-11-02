"""Investment categories filter complete indexed holdings before bounded paging."""

import pytest
from jsonschema import Draft202012Validator

from ai_accounting_modular.contracts.public import ContractError, schema_document

from .test_funds import capture, check_data, page
from .test_funds import case as case
from .test_investments import populate
from .test_monthly_http import get


@pytest.mark.parametrize("contract", ["dashboard_funds", "dashboard_funds_delivery"])
def test_product_contract_requires_identity_and_fields_for_its_business_family(contract):
    schema = schema_document("dashboard-response-v2.json", contract)
    validator = Draft202012Validator(
        {"$defs": schema["$defs"], "$ref": "#/$defs/InvestmentProduct"}
    )
    financial = {
        "fund_id": "synthetic-fund",
        "investment_id": "synthetic-holding",
        "name": "合成理财",
        "product_type": "bank_wealth",
        "category_label": "银行理财",
        "closing_cost_fen": "10000",
        "actual_loss_fen": "0",
        "pending_receipts_fen": "100",
        "recognized_income_fen": "100",
        "accrued_income_fen": "100",
        "actual_payments_fen": "10000",
        "actual_receipts_fen": "0",
        "activity_only": False,
    }
    cooperation = {
        "fund_id": "synthetic-project",
        "investment_id": "synthetic-contract",
        "name": "合成合作",
        "product_type": "cooperation",
        "category_label": "合作项目",
        "closing_cost_fen": None,
        "actual_loss_fen": "0",
        "cooperation_equity_fen": "10000",
        "cooperation_held_fen": "5000",
        "cooperation_operator_due_fen": "0",
        "cooperation_company_due_fen": "0",
    }
    validator.validate(financial)
    validator.validate(cooperation)
    for product in (financial, cooperation):
        for field in ("investment_id", "product_type", "category_label"):
            missing = dict(product)
            del missing[field]
            assert not validator.is_valid(missing), (contract, product["product_type"], field)
    for product, fields in (
        (
            financial,
            (
                "pending_receipts_fen",
                "recognized_income_fen",
                "accrued_income_fen",
                "actual_payments_fen",
                "actual_receipts_fen",
                "activity_only",
            ),
        ),
        (
            cooperation,
            (
                "cooperation_equity_fen",
                "cooperation_held_fen",
                "cooperation_operator_due_fen",
                "cooperation_company_due_fen",
            ),
        ),
    ):
        for field in fields:
            missing = dict(product)
            del missing[field]
            assert not validator.is_valid(missing), (contract, product["product_type"], field)


def test_filters_page_complete_categories_and_preserve_company_totals(case):
    populate(case)
    capture(case)
    full = page(case, limit=500)["data"]
    expected = full["collections"]["investment_products"]["items"]
    assert len(expected) == 7
    default = page(case, limit=2)["data"]["collections"]["investment_products"]
    assert (
        default
        == page(case, limit=2, investment_filter="all")["data"]["collections"][
            "investment_products"
        ]
    )

    for selection, count in (("all", 7), ("financial", 6), ("cooperation", 1)):
        expected_ids = [
            item["investment_id"]
            for item in expected
            if selection == "all"
            or (item["product_type"] == "cooperation") == (selection == "cooperation")
        ]
        cursor, cursors, found = None, set(), []
        while True:
            query = {"section": "investment_products", "investment_filter": selection, "limit": 2}
            if cursor is not None:
                query["cursor"] = cursor
            response = page(case, **query)["data"]
            check_data(response)
            assert response["investments"] == full["investments"]
            assert response["investments"]["closing_cost_fen"] == "60000"
            products = response["collections"]["investment_products"]
            meta = products["page"]
            assert meta["total_count"] == 7
            assert meta["filtered_count"] == count
            assert meta["returned_count"] == len(products["items"]) <= 2
            found.extend(item["investment_id"] for item in products["items"])
            if not meta["has_more"]:
                assert meta["next_cursor"] is None
                break
            cursor = meta["next_cursor"]
            assert cursor is not None and cursor not in cursors
            cursors.add(cursor)
        assert found == expected_ids
        assert len(found) == len(set(found)) == count


def test_product_cursor_cannot_be_reused_under_another_investment_filter(case):
    populate(case)
    capture(case)
    first = page(case, section="investment_products", investment_filter="financial", limit=1)
    products = first["data"]["collections"]["investment_products"]
    cursor = products["page"]["next_cursor"]
    assert cursor is not None
    for investment_filter in ("all", "cooperation"):
        with pytest.raises(ContractError) as error:
            page(
                case,
                section="investment_products",
                investment_filter=investment_filter,
                limit=1,
                cursor=cursor,
            )
        assert error.value.code == "snapshot_changed"
    next_page = page(
        case, section="investment_products", investment_filter="financial", limit=1, cursor=cursor
    )["data"]["collections"]["investment_products"]
    assert next_page["page"]["total_count"] == 7
    assert next_page["page"]["filtered_count"] == 6
    assert next_page["items"][0]["investment_id"] != products["items"][0]["investment_id"]


def test_http_filter_uses_same_published_snapshot_and_complete_category_counts(case):
    populate(case)
    capture(case)
    status, ready = get(case, ("period", "2026-01"), ("investment_filter", "all"))
    assert status == 200 and ready["state"] == "ready", ready
    response = ready["response"]
    assert response["schema_version"] == 12
    version = response["snapshot_version"]
    for investment_filter, count in (("financial", 6), ("cooperation", 1)):
        status, filtered = get(
            case,
            ("period", "2026-01"),
            ("section", "investment_products"),
            ("investment_filter", investment_filter),
            ("expected_version", version),
            ("limit", "1"),
        )
        assert status == 200, filtered
        assert filtered["snapshot_version"] == version
        assert filtered["data"]["investments"] == response["data"]["investments"]
        meta = filtered["data"]["collections"]["investment_products"]["page"]
        assert meta["total_count"] == 7
        assert meta["filtered_count"] == count
        assert meta["returned_count"] == 1
