"""Activation displays use adopted asset costs and leave the actual journal unchanged."""

from copy import deepcopy

import pytest

from ai_accounting_modular.ledger.public import VoucherLocator
from tests.modular.application._working_case import case as case
from tests.modular.domains.assets.test_application import request as asset_request
from tests.modular.domains.assets.test_assets import bulk_fixed_assets
from tests.modular.domains.assets.test_reading import save

from .test_brief_activity import activity, capture, validate, vouchers


def activation_voucher(case, month="2026-01"):
    return next(
        item for item in vouchers(case, month)["items"] if item["kind"] == "assets.activation"
    )


def formal_activation(case, business_id):
    reference = next(
        item["reference"]
        for item in case.get(business_id)["businesses"][0]["results"]
        if item["target"]["result_slot"] == "main"
    )
    with case.kernel.storage.read_unit(case.company) as read:
        (entry,) = case.kernel.ledger.get_effective_voucher_directory((reference,), read)
        (voucher,) = case.kernel.ledger.get_vouchers(
            (
                VoucherLocator(
                    voucher_id=entry.voucher_id,
                    result_id=entry.result["result_id"],
                    generation=entry.result["generation"],
                ),
            ),
            read,
        )
    return entry, voucher


@pytest.mark.parametrize(
    "types,summary,total",
    [
        (["fixed"] * 9, "9项固定资产启用", "900"),
        (["intangible"], "1项无形资产启用", "100"),
        (["fixed", "intangible"], "1项固定资产、1项无形资产启用", "200"),
    ],
)
def test_activation_has_a_specific_summary_and_cost_without_rewriting_journal(
    case, types, summary, total
):
    case.seed(expense=False)
    acquired, enabled = bulk_fixed_assets(len(types))
    for asset, asset_type in zip(acquired["data"]["assets"], types, strict=True):
        asset["asset_type"] = asset_type
    save(case.app, asset_request(case.company, acquired, enabled))
    original = formal_activation(case, enabled["business_id"])
    capture(case)
    actual = activation_voucher(case)
    assert actual["summary"] == summary
    assert actual["business_amount_label"] == "启用资产成本"
    assert actual["business_amount_fen"] == total
    assert actual["business_amount_state"] == "known"
    validate("OwnerBriefVoucher", actual)
    selected = next(item for item in activity(case)["items"] if item["kind"] == "assets.activation")
    assert (selected["amount_label"], selected["amount_fen"]) == ("启用资产成本", total)
    assert formal_activation(case, enabled["business_id"]) == original
    assert original[0].summary == "资产及项目核算"
    assert all(line["source_label"] == "资产及项目核算" for line in actual["lines"])


def test_closed_activation_correction_uses_each_exact_cost_and_keeps_frozen_voucher(case):
    case.seed(expense=False)
    acquired, enabled = bulk_fixed_assets(2)
    save(case.app, asset_request(case.company, acquired, enabled))
    checked = case.check()
    assert checked["ready"], checked
    assert case.close(checked)["state"] == "closed"
    frozen = formal_activation(case, enabled["business_id"])
    capture(case)
    original = activation_voucher(case)

    revised = deepcopy(acquired)
    revised["data"]["assets"][0]["cost_fen"] = "140"
    revised["data"]["total_cost_fen"] = "240"
    revised["data"]["creditors"][0]["amount_fen"] = "240"
    changed = case.app.accounting_apply(
        case.request(
            correction_period="2026-02",
            mutations=[{"op": "replace", "expected_seq": 1, "fact": revised}],
        )
    )
    assert changed["completion"] == "complete", changed
    capture(case, "2026-02")
    actual = [
        item for item in vouchers(case, "2026-02")["items"] if item["kind"] == "assets.activation"
    ]
    assert {(item["state"], item["business_amount_fen"]) for item in actual} == {
        ("冲正", "-200"),
        ("已入账", "240"),
    }
    assert {item["summary"] for item in actual} == {"2项固定资产启用"}
    assert {item["business_amount_label"] for item in actual} == {"启用资产成本"}
    for item in actual:
        validate("OwnerBriefVoucher", item)
    capture(case)
    assert activation_voucher(case) == original
    with case.kernel.storage.read_unit(case.company) as read:
        (saved,) = case.kernel.ledger.get_vouchers(
            (
                VoucherLocator(
                    voucher_id=frozen[0].voucher_id,
                    result_id=frozen[0].result["result_id"],
                    generation=frozen[0].result["generation"],
                ),
            ),
            read,
        )
    assert saved == frozen[1]
