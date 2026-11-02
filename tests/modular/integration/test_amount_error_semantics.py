"""Summing sourced amounts preserves missing facts and rejects actual overflow."""

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.domains.assets._common import total as asset_total
from ai_accounting_modular.domains.financing._common import total as financing_total
from ai_accounting_modular.domains.operations._common import sum_amounts as operations_total
from ai_accounting_modular.domains.people._common import total as people_total
from ai_accounting_modular.domains.taxes._common import total as taxes_total
from ai_accounting_modular.ledger._balances import sum_amounts as resource_total
from ai_accounting_modular.ledger.opening._common import total as opening_total
from ai_accounting_modular.ledger.reporting._facts import total as reporting_total
from ai_accounting_modular.money._common import total as money_total

TOTALS = (
    asset_total,
    financing_total,
    operations_total,
    people_total,
    taxes_total,
    opening_total,
    reporting_total,
    money_total,
    resource_total,
)


@pytest.mark.parametrize("calculate", TOTALS, ids=lambda method: method.__module__)
def test_missing_source_fact_survives_amount_collection(calculate):
    problem = {
        "code": "missing_fact",
        "business_id": "00000000-0000-0000-0000-000000000301",
        "field": "/data/entitlements",
        "meaning": "有源股息权益对应批次的真实取得日",
        "message": "12个月边界所采用的投资批次缺少真实取得日。",
        "allowed_precision": ["day"],
        "reusable_sources": [
            {"source_id": "00000000-0000-0000-0000-000000000302", "location": "合成权益来源证明"}
        ],
        "blocking_scope": "business",
    }
    original = ContractError("needs_information", problem["message"], fact_issues=[problem])

    def sourced_amounts():
        yield 100
        raise original

    with pytest.raises(ContractError) as raised:
        calculate(sourced_amounts())
    assert raised.value is original
    assert raised.value.to_response() == {
        "status": "rejected",
        "code": "needs_information",
        "message": problem["message"],
        "fact_issues": [problem],
    }


@pytest.mark.parametrize("calculate", TOTALS, ids=lambda method: method.__module__)
@pytest.mark.parametrize(
    "values", [(2**63 - 1, 1), (-(2**63), -1)], ids=["above-int64", "below-int64"]
)
def test_actual_sum_outside_int64_remains_invalid_input(calculate, values):
    with pytest.raises(ContractError) as raised:
        calculate(iter(values))
    assert raised.value.code == "invalid_input"
    assert raised.value.fact_issues == ()
