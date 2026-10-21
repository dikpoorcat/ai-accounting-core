"""Opening and closed changes use their own frozen facts and effective vouchers."""

from uuid import uuid4

from tests.modular.application._working_case import case as case
from tests.modular.domains.assets.test_application import request as asset_request
from tests.modular.domains.assets.test_assets import case as asset_case
from tests.modular.domains.people.test_application import replacement
from tests.modular.domains.people.test_people import case as business_case
from tests.modular.domains.people.test_people import declaration, no_contributions
from tests.modular.domains.people.test_read_remuneration import add
from tests.modular.reading._support import source

from .test_brief_open import apply, members, page, publish


def test_opening_preserves_declared_nature_original_reference_and_tax_month_without_vouchers(case):
    package = {
        "business_id": str(uuid4()),
        "kind": "opening.package",
        "period": "2026-01",
        "evidence_ids": [case.source_id],
        "data": {
            "basis": "existing_ledger",
            "members": [
                {"member_id": str(uuid4()), "evidence_ids": [case.source_id], "data": data}
                for data in (
                    {
                        "type": "obligation",
                        "counterparty_id": case.party,
                        "nature": "customer_receivable",
                        "amount_fen": "100",
                        "original_business_reference": "合成旧合同应收款",
                    },
                    {
                        "type": "obligation",
                        "counterparty_id": case.party,
                        "nature": "supplier_administration_payable",
                        "amount_fen": "40",
                        "original_business_reference": "合成旧管理服务款",
                    },
                    {
                        "type": "tax",
                        "authority_id": case.party,
                        "tax_kind": "vat",
                        "balance_kind": "payable",
                        "amount_fen": "60",
                        "tax_period": "2025-12",
                    },
                )
            ],
        },
    }
    apply(
        case,
        package,
        objects=[
            {
                "object_id": case.party,
                "kind": "organization",
                "name": "同一实际对象",
                "expected_seq": 0,
                "data": {},
            }
        ],
        sources=[source("open-source") | {"source_id": case.source_id}],
    )
    snapshot = publish(case)
    data = page(case, snapshot)
    assert data["open_items"]["complete"]
    assert data["open_items"]["receivable_fen"] == data["open_items"]["payable_fen"] == "100"
    groups = data["collections"]["open_items"]["items"]
    assert len(groups) == 3
    by_title = {}
    for group in groups:
        result = members(case, snapshot, group["group_key"])["collections"]
        assert result["vouchers"]["items"] == []
        by_title[group["description"]] = result["members"]["items"][0]
    assert by_title["管理费用"]["purpose"] == "合成旧管理服务款"
    assert by_title["期初客户应收款"]["purpose"] == "合成旧合同应收款"
    assert by_title["增值税"]["purpose"] == "2025-12 税款"


def test_closed_withdrawal_retains_january_but_current_progress_is_withdrawn(case):
    expense = case.seed()
    assert case.close(case.check())["state"] == "closed"
    saved = case.app.accounting_apply(
        case.request(
            mutations=[
                {"op": "withdraw", "expected_seq": 1, "business_id": expense["business_id"]}
            ],
            correction_period="2026-02",
            explanation="已核实该笔费用误记，没有对应现实业务。",
        )
    )
    assert saved["completion"] == "complete", saved
    january = page(case, publish(case))["collections"]["open_items"]["items"][0]
    assert january["outstanding_fen"] == "100000"
    assert (january["current_status"], january["current_outstanding_fen"]) == ("withdrawn", "0")
    assert page(case, publish(case, "2026-02"))["open_items"]["total_count"] == 0


def test_no_voucher_adoption_inherits_the_actual_older_frozen_voucher(case):
    case.seed(expense=False)
    wage = business_case("B17Z")
    add(case, no_contributions(), wage)
    january = publish(case)
    group = page(case, january)["collections"]["open_items"]["items"][0]
    before = members(case, january, group["group_key"])["collections"]
    original = before["vouchers"]["items"][0]
    assert case.close(case.check())["state"] == "closed"
    declared, reference = declaration(wage, "0")
    wage["data"]["tax_basis"]["pooled_source"] = reference
    wire = replacement(case.company, wage)
    wire["correction_period"] = "2026-02"
    wire["mutations"].append({"op": "create", "expected_seq": 0, "fact": declared})
    saved = case.app.accounting_apply(wire)
    assert saved["completion"] == "complete", saved
    snapshot = publish(case, "2026-02")
    group = page(case, snapshot)["collections"]["open_items"]["items"][0]
    after = members(case, snapshot, group["group_key"])["collections"]
    assert after["vouchers"]["items"] == [original]
    assert after["members"]["items"][0]["voucher_version_id"] == original["voucher_version_id"]
    assert after["members"]["items"][0]["voucher_number"] == int(original["number"])


def test_referenced_asset_display_changes_version_but_keeps_the_formal_voucher(case):
    case.seed(expense=False)
    acquired = asset_case("B20")
    wire = asset_request(case.company, acquired)
    asset = next(obj for obj in wire["objects"] if obj["kind"] == "asset")
    asset["data"]["code"] = "合成设备-001"
    result = case.app.accounting_apply(wire)
    assert result["completion"] == "complete", result
    formal = case.get(acquired["business_id"])["businesses"]
    before = publish(case)
    group = page(case, before)["collections"]["open_items"]["items"][0]
    old_voucher = members(case, before, group["group_key"])["collections"]["vouchers"]["items"][0]
    assert old_voucher["asset"]["code"] == "合成设备-001"
    revised = {
        **asset,
        "name": "修订后设备名",
        "expected_seq": 1,
        "data": {**asset["data"], "code": "合成设备-002"},
    }
    changed = apply(case, objects=[revised])
    assert changed["accounting"] == "unchanged"
    assert case.get(acquired["business_id"])["businesses"] == formal
    after = publish(case)
    assert before.version != after.version
    new_voucher = members(case, after, group["group_key"])["collections"]["vouchers"]["items"][0]
    assert new_voucher["asset"]["code"] == "合成设备-002"
    assert new_voucher["asset"]["name"] == "修订后设备名"
    assert new_voucher["voucher_version_id"] == old_voucher["voucher_version_id"]
    assert [
        (line["code"], line["debit_fen"], line["credit_fen"]) for line in new_voucher["lines"]
    ] == [(line["code"], line["debit_fen"], line["credit_fen"]) for line in old_voucher["lines"]]
