"""Concrete current descriptions follow exact sources without changing accounting groups."""

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import loads_json
from ai_accounting_modular.reading import _brief_activity, _brief_activity_index, _monthly
from ai_accounting_modular.reading._business_descriptions import (
    Descriptions,
    description_reference,
    part_display,
    part_references,
)
from ai_accounting_modular.reading._business_labels import BusinessLabel
from tests.modular.application._working_case import case as case
from tests.modular.domains.financing._support import BANK, COUNTERPARTY, fact, payment
from tests.modular.money.test_payroll import inputs as payroll_inputs

from .test_brief_activity import activity, capture, group, vouchers
from .test_brief_open import members, publish
from .test_brief_open import page as open_page
from .test_business_status import progress
from .test_funds import apply, initialize, selection
from .test_funds import capture as funds_capture
from .test_funds import page as funds_page
from .test_funds_integrity import original_row, pair
from .test_funds_money_origins import payroll


def note(case, business, text, *, seq=0):
    result = case.app.accounting_apply(
        case.request(
            notes=[
                {
                    "scope": "business:" + business,
                    "expected_seq": seq,
                    "text": text,
                    "evidence_ids": [case.source_id] if text else [],
                }
            ]
        )
    )
    assert result.get("completion") == "complete", result
    return result


def test_batch_helper_reads_only_current_exact_heads_and_notes():
    refs = [
        {
            "business_id": str(uuid4()),
            "result_id": str(uuid4()),
            "result_slot": "main",
            "generation": 1,
        }
        for _ in range(1001)
    ]
    calls = []

    def heads(ids, read, *, selection):
        assert selection.view == "current"
        calls.append(("heads", len(ids), read))
        return [ref for ref in refs if ref["business_id"] in ids]

    def notes(scopes, read):
        calls.append(("notes", len(scopes), read))
        return [{"scope": scope, "state": "current", "text": "培训费报销"} for scope in scopes]

    reader = SimpleNamespace(
        ledger=SimpleNamespace(get_result_refs=heads), records=SimpleNamespace(current_notes=notes)
    )
    read = object()
    prose = Descriptions(reader, refs, read)
    assert [count for kind, count, _ in calls if kind == "heads"] == [500, 500, 1]
    assert [count for kind, count, _ in calls if kind == "notes"] == [500, 500, 1]
    assert all(snapshot is read for _, _, snapshot in calls)
    assert prose.text(refs[0]) == "培训费报销"
    assert prose.text(refs[0] | {"generation": 2}) is None


def test_same_group_keeps_all_source_descriptions_and_formal_summaries(case):
    initialize(case)
    first, second = case.expense("300"), case.expense("700")
    first["data"]["creditor_id"] = second["data"]["creditor_id"] = COUNTERPARTY
    paid = payment(first, 400, slot="primary", party=COUNTERPARTY)
    paid["data"]["allocations"] = [
        {"source": {"business_id": source["business_id"], "slot": "primary"}, "amount_fen": value}
        for source, value in ((first, "100"), (second, "300"))
    ]
    apply(case, first, second, paid)
    capture(case)
    before = activity(case)
    original_summaries = {
        value["voucher_version_id"]: value["summary"] for value in vouchers(case)["items"]
    }
    initial_open = open_page(case, publish(case))["collections"]["open_items"]["items"][0]
    note(case, first["business_id"], "培训费报销")
    note(case, second["business_id"], "差旅费报销")
    note(case, paid["business_id"], "整单付款说明")
    assert case.kernel.reading.run_pending(case.company)
    after = activity(case)
    assert [
        (value["key"], value["amount_fen"], value["member_count"]) for value in after["items"]
    ] == [(value["key"], value["amount_fen"], value["member_count"]) for value in before["items"]]
    assert {value["title"] for value in after["items"]} == {"管理费用", "支付管理费用"}
    assert {
        value["voucher_version_id"]: value["summary"] for value in vouchers(case)["items"]
    } == original_summaries
    assert {
        value["label"]
        for category in loads_json(capture(case).payload_json)["activity_groups"]
        for value in category["type_counts"]
    } == {"管理费用", "支付管理费用"}
    snapshot = publish(case)
    opened = open_page(case, snapshot)["collections"]["open_items"]["items"][0]
    assert opened["group_key"] == initial_open["group_key"]
    assert opened["description"] == "管理费用"
    detail = members(case, snapshot, opened["group_key"])["collections"]["members"]["items"]
    assert {value["description"] for value in detail} == {"培训费报销", "差旅费报销"}
    funds_capture(case)
    movement = funds_page(case)["data"]["collections"]["movements"]["items"][0]
    assert movement["type"] == "支付管理费用"
    assert movement["list_summary"] == "支付管理费用"
    assert (
        "培训费报销" in movement["display_summary"] and "差旅费报销" in movement["display_summary"]
    )
    assert "整单付款说明" not in movement["list_summary"]
    paid_group = next(value for value in after["items"] if value["group"] == "cash")
    selected = group(case, paid_group["key"])["members"]["items"][0]
    business = progress(
        case,
        paid["business_id"],
        detail_scope_key=selected["detail_scope_key"],
        voucher_version_id=selected["voucher_version_id"],
    )["data"]
    assert set(
        business["display_profiles"]["business"]["values"]["purpose"]
        .removeprefix("支付")
        .split("；")
    ) == {
        "培训费报销",
        "差旅费报销",
    }
    assert {
        value["purpose_label"] for value in business["collections"]["settlement_events"]["items"]
    } == {"培训费报销", "差旅费报销"}
    assert business["display_profiles"]["business"]["values"]["note"] == "整单付款说明"
    assert business["display_profiles"]["business"]["values"]["note_scope"] == "whole_business"


def test_note_only_incremental_update_and_clear_preserve_other_roots(case, monkeypatch):
    first = case.seed()
    second = case.expense("200")
    apply(case, second)
    capture(case)
    publish(case)
    untouched = []
    original = case.kernel.ledger.get_adopted_contents

    def exact(refs, read):
        untouched.extend(ref["business_id"] for ref in refs)
        return original(refs, read)

    note(case, first["business_id"], "培训费报销")
    with monkeypatch.context() as patch:
        patch.setattr(case.kernel.ledger, "get_adopted_contents", exact)
        assert case.kernel.reading.run_pending(case.company)
    assert set(untouched) == {first["business_id"]}
    assert activity(case)["items"][0]["title"] == "管理费用"
    with case.kernel.storage.read_unit(case.company) as read:
        repo = read.repository("reading")
        scope = selection(case)
        saved = loads_json(repo.payload(_monthly.key(scope), "brief_activity")["payload_json"])
        fresh = loads_json(_brief_activity.prepare(case.kernel.reading, scope, read).payload_json)
        assert _brief_activity_index.semantic(saved) == _brief_activity_index.semantic(fresh)
    note(case, first["business_id"], "", seq=1)
    assert case.kernel.reading.run_pending(case.company)
    assert activity(case)["items"][0]["title"] == "管理费用"
    assert (
        progress(case, first["business_id"])["data"]["display_profiles"]["business"]["values"][
            "purpose"
        ]
        is None
    )


def test_current_description_survives_amount_update_but_does_not_relabel_old_adoption(case):
    first = case.seed()
    note(case, first["business_id"], "培训费报销")
    revised = deepcopy(first)
    revised["data"]["amount_fen"] = "80000"
    result = case.app.accounting_apply(
        case.request(mutations=[{"op": "replace", "expected_seq": 1, "fact": revised}])
    )
    assert result["completion"] == "complete", result
    capture(case)
    assert activity(case)["items"][0]["title"] == "培训费报销"
    assert case.close(case.check())["state"] == "closed"
    revised["data"]["amount_fen"] = "60000"
    result = case.app.accounting_apply(
        case.request(
            mutations=[{"op": "replace", "expected_seq": 2, "fact": revised}],
            correction_period="2026-02",
        )
    )
    assert result["completion"] == "complete", result
    capture(case)
    assert activity(case)["items"][0]["title"] == "管理费用"
    capture(case, "2026-02")
    changed = activity(case, "2026-02")["items"]
    assert (
        next(value for value in changed if value["amount_fen"] == "-80000")["title"]
        == "冲正原业务 · 管理费用"
    )
    assert (
        next(value for value in changed if value["amount_fen"] == "60000")["title"]
        == "更正原业务 · 培训费报销"
    )


def test_payroll_components_keep_their_own_meaning_under_whole_business_prose(case):
    case.seed(expense=False)
    call, gross, wage = payroll_inputs(case.company, pay=False)
    assert case.app.accounting_apply(call)["completion"] == "complete"
    paid = fact(
        "money.movement",
        {
            "type": "settlement",
            "funds_account_id": gross["data"]["funds_account_id"],
            "direction": "outflow",
            "amount_fen": "100000",
            "payment_method": "individual",
            "allocations": [
                {
                    "source": {"business_id": wage["business_id"], "slot": "employee_social"},
                    "amount_fen": "100000",
                }
            ],
        },
        actual="2026-01-20",
    )
    apply(case, paid)
    note(case, wage["business_id"], "整月工资核对说明")
    note(case, paid["business_id"], "整单扣款说明")
    capture(case)
    assert "整月工资核对说明" not in str(activity(case))
    whole = progress(case, wage["business_id"])["data"]["display_profiles"]["business"]["values"]
    assert whole["purpose"] is None and whole["note"] is None
    cash = next(value for value in activity(case)["items"] if value["group"] == "cash")
    assert cash["title"] == "支付个人承担社保费"
    member = group(case, cash["key"])["members"]["items"][0]
    data = progress(
        case,
        paid["business_id"],
        detail_scope_key=member["detail_scope_key"],
        voucher_version_id=member["voucher_version_id"],
    )["data"]
    assert data["display_profiles"]["business"]["values"]["purpose"] is None
    assert data["display_profiles"]["business"]["values"]["note"] == "整单扣款说明"
    assert data["display_profiles"]["business"]["values"]["note_scope"] == "whole_business"
    assert [
        value["purpose_label"] for value in data["collections"]["settlement_events"]["items"]
    ] == ["个人社保"]
    funds_capture(case)
    movement = funds_page(case)["data"]["collections"]["movements"]["items"][0]
    assert movement["list_summary"] == "支付社保费"
    whole_payment = progress(case, paid["business_id"])["data"]["display_profiles"]["business"][
        "values"
    ]
    assert whole_payment["purpose"] is None
    assert whole_payment["note"] == "整单扣款说明"
    assert whole_payment["note_scope"] == "selected"


@pytest.mark.parametrize(
    "code,component,allowed",
    [
        ("wage", None, False),
        ("bonus", "net_pay", False),
        ("labor", None, True),
        ("labor", "net_pay", True),
        ("withholding", "tax", False),
        ("social", "employee_social", False),
        ("employer_housing", "employer_housing", False),
        ("income_tax", None, False),
        ("surtax", None, False),
        ("bank_interest", None, False),
        ("asset_consumption", None, False),
        ("bank_fee", None, True),
        ("interest", None, True),
    ],
)
def test_description_policy_uses_the_selected_subject_instead_of_the_outer_kind(
    code, component, allowed
):
    body = SimpleNamespace(
        target={"kind": "people.remuneration"}, basis={}, reference={"business_id": "source"}
    )
    assert (
        bool(description_reference(body, BusinessLabel(code, "业务名称"), component=component))
        == allowed
    )


def test_offset_uses_each_obligation_subject_and_keeps_its_non_cash_action():
    tax = SimpleNamespace(
        target={"kind": "taxes.assessment"},
        basis={"input": {"type": "vat"}},
        reference={"business_id": "tax", "result_id": "tax-result", "generation": 1},
    )
    labor = SimpleNamespace(
        target={"kind": "people.remuneration"},
        basis={"input": {"type": "labor"}},
        reference={"business_id": "labor", "result_id": "labor-result", "generation": 1},
    )
    part = SimpleNamespace(
        label=BusinessLabel("offset", "往来抵销"),
        phase="offset",
        scope={
            "allocations": [
                {
                    "producer": {"result_id": "tax-result", "result_generation": 1},
                    "component": None,
                },
                {
                    "producer": {"result_id": "labor-result", "result_generation": 1},
                    "component": "net_pay",
                },
            ]
        },
    )
    cache = SimpleNamespace(saved={("tax-result", 1): tax, ("labor-result", 1): labor})
    assert part_references(None, part, cache) == (labor.reference,)
    prose = SimpleNamespace(
        text=lambda reference: "翻译费", scope=lambda reference: "business:劳动"
    )
    assert part_display(None, part, cache, prose, "original")[0] == "往来抵销 · 翻译费"


def test_standard_bank_interest_does_not_read_accounting_prose(case, monkeypatch):
    initialize(case)
    receipt = fact(
        "operations.other_income",
        {"income_kind": "bank_interest", "amount_fen": "28", "funds_account_id": BANK},
        actual="2026-01-20",
    )
    row = original_row(case, str(uuid4()), "28", "2026-01-20", "inflow")
    row["memo"] = "银行原文：季度结息"
    apply(case, receipt, *pair(BANK, [row], [receipt]))
    note(case, receipt["business_id"], "银行结息核对过程和反复确认的计算说明")
    queried = []
    original = case.kernel.records.current_notes

    def current_notes(scopes, read):
        queried.extend(scopes)
        return original(scopes, read)

    monkeypatch.setattr(case.kernel.records, "current_notes", current_notes)
    capture(case)
    funds_capture(case)
    result = progress(case, receipt["business_id"])["data"]
    assert activity(case)["items"][0]["title"] == "银行利息收入"
    assert (
        funds_page(case)["data"]["collections"]["movements"]["items"][0]["list_summary"]
        == "银行利息收入"
    )
    assert result["display_profiles"]["business"]["values"]["purpose"] is None
    assert "business:" + receipt["business_id"] not in queried
    original = funds_page(case)["data"]["collections"]["statements"]["items"][0]
    assert original["memo"] == "银行原文：季度结息"


def test_labor_specific_purpose_follows_net_payment_but_not_withholding(case):
    initialize(case)
    person = str(uuid4())
    labor = fact(
        "people.remuneration",
        {
            "type": "labor",
            "person_id": person,
            "gross_fen": "50000",
            "destination": {"type": "expense", "area": "administration"},
            "withholding_state": "deduction_established",
            "tax_basis": {
                "tax_period": "2026-01",
                "tax_fen": "10000",
                "basis_type": "final_statement",
                "source": {"source_id": case.source_id, "location": "合成劳务扣税结果"},
            },
        },
    )
    labor["evidence_ids"] = [case.source_id]
    apply(
        case,
        labor,
        objects=[
            {
                "object_id": person,
                "kind": "person",
                "name": "合成译员",
                "expected_seq": 0,
                "data": {},
            }
        ],
    )
    paid = payment(labor, 40000, party=person, slot="net_pay")
    apply(case, paid)
    note(case, labor["business_id"], "翻译服务费")
    note(case, paid["business_id"], "本单付清翻译费")
    capture(case)
    funds_capture(case)
    assert {row["title"] for row in activity(case)["items"]} == {"翻译服务费", "支付翻译服务费"}
    ordinary = next(
        row for row in vouchers(case)["items"] if row["subject_id"] == labor["business_id"]
    )
    assert ordinary["type"] == "劳务报酬" and ordinary["list_summary"] == "翻译服务费"
    movement = funds_page(case)["data"]["collections"]["movements"]["items"][0]
    assert movement["list_summary"] == "支付翻译服务费"
    snapshot = publish(case)
    opened = open_page(case, snapshot)["collections"]["open_items"]["items"]
    assert any(row["description"] == "代扣个人所得税" for row in opened)
    whole = progress(case, paid["business_id"])["data"]["display_profiles"]["business"]["values"]
    assert whole["purpose"] == "支付翻译服务费"
    assert whole["note"] == "本单付清翻译费"
    assert whole["note_scope"] == "selected"


def test_payroll_reserve_whole_note_never_becomes_the_reserve_components_purpose(case):
    _, paid = payroll(case)
    text = "1月工资及办公室备用金整单汇出"
    note(case, paid["business_id"], text)
    capture(case)
    selected = [row for row in activity(case)["items"] if row["group"] == "cash"]
    assert {row["title"] for row in selected} == {"支付员工工资", "支付备用金费用"}
    for row in selected:
        member = group(case, row["key"])["members"]["items"][0]
        detail = progress(
            case,
            paid["business_id"],
            detail_scope_key=member["detail_scope_key"] or member["key"],
            voucher_version_id=member["voucher_version_id"],
        )["data"]["display_profiles"]["business"]["values"]
        assert detail["purpose"] is None
        assert detail["note"] == text and detail["note_scope"] == "whole_business"
    whole = progress(case, paid["business_id"])["data"]["display_profiles"]["business"]["values"]
    assert whole["purpose"] is None
    assert whole["note"] == text and whole["note_scope"] == "selected"
    funds_capture(case)
    movement = next(
        item
        for item in funds_page(case)["data"]["collections"]["movements"]["items"]
        if item["subject_id"] == paid["business_id"]
    )
    assert movement["has_business_progress"] is True
    voucher = next(
        item for item in vouchers(case)["items"] if item["subject_id"] == paid["business_id"]
    )
    assert voucher["has_business_progress"] is True
    visible = progress(
        case,
        paid["business_id"],
        detail_scope_key="voucher:" + voucher["voucher_version_id"],
        voucher_version_id=voucher["voucher_version_id"],
    )["data"]["display_profiles"]["business"]["values"]
    assert visible["note"] == text and visible["note_scope"] == "selected"


def test_unallocated_actual_payment_keeps_the_same_action_in_every_view(case):
    initialize(case)
    paid = fact(
        "money.movement",
        {
            "type": "managed_reserve",
            "funds_account_id": BANK,
            "direction": "outflow",
            "amount_fen": "100",
        },
        actual="2026-01-16",
    )
    apply(case, paid)
    note(case, paid["business_id"], "办公室备用金费用")
    capture(case)
    funds_capture(case)
    assert activity(case)["items"][0]["title"] == "支付办公室备用金费用"
    movement = funds_page(case)["data"]["collections"]["movements"]["items"][0]
    assert movement["list_summary"] == movement["display_summary"] == "支付办公室备用金费用"
    whole = progress(case, paid["business_id"])["data"]["display_profiles"]["business"]["values"]
    assert whole["purpose"] == "支付办公室备用金费用" and whole["note"] is None
