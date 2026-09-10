from types import SimpleNamespace
from uuid import uuid4

import pytest

from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.dashboard_brief import (
    ACTIVITY_GROUPS,
    _build_activity_groups,
    _component_view,
    _source_references,
)
from ai_accounting.dashboard_common import COMPONENT_PRESENTATIONS, component_presentation
from ai_accounting.dashboard_funds import _bank_activity_party


def test_all_supported_components_have_chinese_dashboard_names() -> None:
    components = RecordEventRequest.model_json_schema()["properties"]["components"]["items"]
    supported_kinds = set(components["discriminator"]["mapping"])
    assert set(COMPONENT_PRESENTATIONS) == supported_kinds | {"funds"}
    for kind in supported_kinds | {"funds"}:
        group, label = component_presentation(kind)
        assert group in ACTIVITY_GROUPS
        assert label and all("\u4e00" <= character <= "\u9fff" for character in label)


@pytest.mark.parametrize("kind", ["future_component", "", "payroll_accrual_v2"])
def test_unknown_components_never_expose_technical_codes_as_names(kind: str) -> None:
    assert component_presentation(kind) == ("other", "其他业务")


def test_mixed_event_is_projected_into_every_component_business_area() -> None:
    item = {
        "date": "2026-08-08",
        "number": "记-001",
        "is_reversal": False,
        "list_summary": "服务收入并支付费用",
        "summary": "一笔真实资金动作包含两类业务事实",
        "amount_fen": 15000,
        "state": "已入账",
        "parties": ["客户甲", "供应商乙"],
        "evidence": ["bank.csv"],
        "funds": [{"id": "funds", "kind": "funds"}],
        "components": [
            {
                "id": "sale",
                "group": "income_customer",
                "label": "服务收入",
                "amount_fen": 10000,
                "parties": ["客户甲"],
            },
            {
                "id": "expense",
                "group": "expense_supplier",
                "label": "费用",
                "amount_fen": 5000,
                "parties": ["供应商乙"],
            },
        ],
    }

    groups = _build_activity_groups([(SimpleNamespace(), item)])

    assert [group["key"] for group in groups] == ["income_customer", "expense_supplier"]
    assert groups[0]["rows"][0]["components"] == [item["components"][0]]
    assert groups[1]["rows"][0]["components"] == [item["components"][1]]
    assert all(group["rows"][0]["funds"] == item["funds"] for group in groups)


def test_component_projection_keeps_facts_derived_parties_and_sources() -> None:
    component_id = uuid4()
    component = SimpleNamespace(
        id=component_id,
        key="settlement",
        kind="payable_settlement",
        facts={
            "description": "清偿两项应付款",
            "depends_on": ["expense"],
            "allocations": [{"open_item_id": str(uuid4()), "source_component_key": "expense"}],
        },
        derived={"settled_fen": 5000},
    )
    lines = [
        {
            "component_id": str(component_id),
            "debit_fen": 5000,
            "credit_fen": 0,
            "party": "供应商甲",
        },
        {
            "component_id": str(component_id),
            "debit_fen": 0,
            "credit_fen": 5000,
            "party": "供应商乙",
        },
    ]

    result = _component_view(component, lines=lines)

    assert result["facts"] == component.facts
    assert result["derived"] == component.derived
    assert set(result["parties"]) == {"供应商甲", "供应商乙"}
    assert {ref["type"] for ref in result["source_references"]} == {
        "component_key",
        "open_item_id",
    }


def test_funds_source_projection_includes_every_allocated_component() -> None:
    references = _source_references(
        {
            "allocations": [
                {"component_key": "salary", "amount_fen": 8000},
                {"component_key": "labor", "amount_fen": 2000},
            ]
        }
    )

    assert references == [
        {"type": "allocated_component_key", "value": "salary"},
        {"type": "allocated_component_key", "value": "labor"},
    ]


def test_bank_activity_party_does_not_infer_identity_from_memo() -> None:
    transaction = SimpleNamespace(
        counterparty_name="网商银行",
        memo="网商银行转入 支付宝",
    )

    assert _bank_activity_party(transaction) == "网商银行"
