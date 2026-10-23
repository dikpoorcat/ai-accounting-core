"""No amount mapping is different from an explicitly inapplicable single amount."""

from types import SimpleNamespace

import pytest

from ai_accounting_modular.reading._brief_activity_amounts import amount


def test_unknown_business_kind_does_not_guess_money_from_generic_value_names():
    content = SimpleNamespace(
        target={
            "kind": "future.unspecified_business",
            "values": {"amount_fen": "999", "gross_fen": "888", "vat_fen": "777"},
        }
    )
    actual = amount(content)
    assert (actual.state, actual.amount_fen, actual.label) == ("unknown", None, "业务金额")


@pytest.mark.parametrize(
    "kind,label",
    [
        ("investment.cooperation_operator_transfer", "无单一公司金额"),
        ("investment.cooperation_position", "无单一业务金额"),
        ("investment.cooperation_exit", "无单一业务金额"),
        ("opening.package", "无单一业务金额"),
    ],
)
def test_explicitly_multidimensional_business_does_not_claim_an_unknown_amount(kind, label):
    content = SimpleNamespace(target={"kind": kind, "values": {"amount_fen": "999"}})
    actual = amount(content)
    assert (actual.state, actual.amount_fen, actual.label) == ("not_applicable", None, label)
