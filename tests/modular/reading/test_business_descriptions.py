"""Concrete current descriptions follow exact sources without changing accounting groups."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import QuerySelection, loads_json
from ai_accounting_modular.reading import (
    _brief_activity,
    _brief_activity_index,
    _brief_vouchers,
    _monthly,
)
from ai_accounting_modular.reading._business_descriptions import (
    Descriptions,
    DescriptionSubject,
    part_display,
    part_references,
    purpose_reference,
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


def note(case, business, text, *, purpose=None, seq=0):
    result = case.app.accounting_apply(
        case.request(
            notes=[
                {
                    "scope": "business:" + business,
                    "expected_seq": seq,
                    "text": text,
                    "purpose": purpose,
                    "evidence_ids": [case.source_id] if text or purpose else [],
                }
            ]
        )
    )
    assert result.get("completion") == "complete", result
    return result


def component_snapshot(case, component):
    with case.kernel.storage.read_unit(case.company) as read:
        snapshot = _monthly.ready_snapshot(case.kernel.reading, selection(case), (component,), read)
        return replace(snapshot, components=dict(snapshot.components))


def test_short_purpose_and_complete_description_are_independent_in_every_read(case):
    initialize(case)
    expense = case.expense("300")
    expense["data"]["creditor_id"] = COUNTERPARTY
    apply(case, expense)
    detail = "培训服务已完成，本月确认全部费用，供应商款项按合同约定随后支付。"
    note(case, expense["business_id"], detail)
    capture(case)
    row = activity(case)["items"][0]
    assert row["title"] == "管理费用"
    member = group(case, row["key"])["members"]["items"][0]
    assert member["title"] == "管理费用" and member["description"] == detail
    assert vouchers(case)["items"][0]["list_summary"] == "管理费用"
    opened = open_page(case, publish(case))["collections"]["open_items"]["items"][0]
    assert opened["description"] == "管理费用"
    assert (
        members(case, publish(case), opened["group_key"])["collections"]["members"]["items"][0][
            "description"
        ]
        == detail
    )
    note(case, expense["business_id"], detail, purpose="培训服务费", seq=1)
    assert case.kernel.reading.run_pending(case.company)
    row = activity(case)["items"][0]
    assert row["title"] == "培训服务费"
    assert group(case, row["key"])["members"]["items"][0]["description"] == detail
    assert vouchers(case)["items"][0]["list_summary"] == "培训服务费"
    data = progress(case, expense["business_id"])["data"]
    obligation = data["settlements"]["obligations"][0]
    assert (obligation["purpose"], obligation["description"], obligation["note_scope"]) == (
        "培训服务费",
        detail,
        "selected",
    )
    assert data["display_profiles"]["business"]["values"]["note"] is None


def test_mixed_payment_keeps_different_details_under_one_shared_short_purpose(case):
    initialize(case)
    first, second = case.expense("300"), case.expense("700")
    first["data"]["creditor_id"] = second["data"]["creditor_id"] = COUNTERPARTY
    paid = payment(first, 1000, slot="primary", party=COUNTERPARTY)
    paid["data"]["allocations"] = [
        {"source": {"business_id": source["business_id"], "slot": "primary"}, "amount_fen": amount}
        for source, amount in ((first, "300"), (second, "700"))
    ]
    apply(case, first, second, paid)
    details = ("第一场培训服务已经完成。", "第二场培训服务已经完成。")
    for source, detail in zip((first, second), details, strict=True):
        note(case, source["business_id"], detail, purpose="培训服务费")
    note(case, paid["business_id"], "本次合并支付两场培训费用。", purpose="付款整单短名")
    prepared = loads_json(capture(case).payload_json)
    categories = {
        item["label"]
        for category in prepared["activity_groups"]
        for item in category["type_counts"]
    }
    assert categories == {"管理费用", "支付管理费用"}
    rows = activity(case)["items"]
    assert {row["title"] for row in rows} == {"培训服务费", "支付培训服务费"}
    cash = next(row for row in rows if row["group"] == "cash")
    member = group(case, cash["key"])["members"]["items"][0]
    assert member["description"] == "支付培训服务费"
    data = progress(
        case,
        paid["business_id"],
        detail_scope_key=member["detail_scope_key"],
        voucher_version_id=member["voucher_version_id"],
    )["data"]
    obligations = data["settlements"]["obligations"]
    assert {item["description"] for item in obligations} == set(details)
    assert {item["purpose"] for item in obligations} == {"培训服务费"}
    assert {item["note_scope"] for item in obligations} == {"selected"}
    own = data["display_profiles"]["business"]["values"]
    assert own["note"] == "本次合并支付两场培训费用。" and own["note_scope"] == "whole_business"
    funds_capture(case)
    movement = funds_page(case)["data"]["collections"]["movements"]["items"][0]
    assert movement["list_summary"] == "支付培训服务费"
    assert "display_summary" not in movement
    activity_version = component_snapshot(case, "brief_activity").version
    before = activity(case)
    note(case, paid["business_id"], "合并付款的最新完整说明。", purpose="付款整单短名", seq=1)
    assert case.kernel.reading.run_pending(case.company)
    assert component_snapshot(case, "brief_activity").version != activity_version
    assert activity(case) == before
    assert "合并付款的最新完整说明。" not in str(activity(case))
    changed = progress(case, paid["business_id"])["data"]
    assert changed["display_profiles"]["business"]["values"]["note"] == "合并付款的最新完整说明。"


def test_funds_note_changes_refresh_parent_snapshot_while_progress_stays_available(case):
    initialize(case)
    receipt = fact(
        "operations.other_income",
        {"income_kind": "company_reward", "amount_fen": "100", "funds_account_id": BANK},
        actual="2026-01-20",
    )
    apply(case, receipt)
    note(case, receipt["business_id"], "奖励事项第一份完整说明。")
    funds_capture(case)
    capture(case)
    assert vouchers(case)["items"][0]["has_business_progress"] is True
    receipt_group = next(row for row in activity(case)["items"] if row["group"] == "business")
    receipt_member = group(case, receipt_group["key"])["members"]["items"][0]
    assert receipt_member["description"] == "奖励事项第一份完整说明。"
    assert receipt_member["detail_scope_key"] is None
    with case.kernel.storage.read_unit(case.company) as read:
        references = case.kernel.ledger.get_result_refs(
            [receipt["business_id"]], read, selection=QuerySelection("current")
        )
        descriptor = _brief_vouchers.descriptors_for_references(
            case.kernel.reading, references, read
        )[0]
        assert descriptor["template"]["has_business_progress"] is True
    before = component_snapshot(case, "funds").version
    original = funds_page(case)["data"]["collections"]["movements"]["items"]
    assert original[0]["has_business_progress"] is True
    note(case, receipt["business_id"], "奖励事项第二份完整说明。", seq=1)
    assert case.kernel.reading.run_pending(case.company)
    assert component_snapshot(case, "funds").version != before
    updated = funds_page(case)["data"]["collections"]["movements"]["items"]
    assert updated == original
    assert "奖励事项第二份完整说明。" not in str(updated)
    assert (
        progress(case, receipt["business_id"])["data"]["display_profiles"]["business"]["values"][
            "note"
        ]
        == "奖励事项第二份完整说明。"
    )


def test_standard_whole_business_notes_refresh_open_snapshot_without_entering_main_rows(case):
    case.seed(expense=False)
    call, _, wage = payroll_inputs(case.company, pay=False)
    assert case.app.accounting_apply(call)["completion"] == "complete"
    note(case, wage["business_id"], "本月工资第一份完整说明。")
    before = publish(case)
    rows = open_page(case, before)["collections"]["open_items"]["items"]
    note(case, wage["business_id"], "本月工资第二份完整说明。", seq=1)
    assert case.kernel.reading.run_pending(case.company)
    after = component_snapshot(case, "brief_open_items")
    assert after.version != before.version
    assert open_page(case, after)["collections"]["open_items"]["items"] == rows
    assert "本月工资第二份完整说明。" not in str(open_page(case, after))
    obligations = progress(case, wage["business_id"])["data"]["settlements"]["obligations"]
    notes = [row for row in obligations if row["description"]]
    assert [(row["description"], row["note_scope"]) for row in notes] == [
        ("本月工资第二份完整说明。", "whole_business")
    ]


def test_detail_only_updates_preserve_short_heading_and_empty_note_subscriptions(case):
    first = case.seed()
    capture(case)
    note(case, first["business_id"], "第一份完整说明。", purpose="培训费")
    assert case.kernel.reading.run_pending(case.company)
    group_key = activity(case)["items"][0]["group_key"]
    note(case, first["business_id"], "第二份完整说明。", purpose="培训费", seq=1)
    assert case.kernel.reading.run_pending(case.company)
    assert activity(case)["items"][0]["title"] == "培训费"
    assert group(case, group_key)["members"]["items"][0]["description"] == "第二份完整说明。"
    note(case, first["business_id"], "", purpose="培训费", seq=2)
    assert case.kernel.reading.run_pending(case.company)
    assert group(case, group_key)["members"]["items"][0]["description"] == "培训费"
    note(case, first["business_id"], "", seq=3)
    assert case.kernel.reading.run_pending(case.company)
    assert activity(case)["items"][0]["title"] == "管理费用"
    note(case, first["business_id"], "清空后重新补入的完整说明。", seq=4)
    assert case.kernel.reading.run_pending(case.company)
    assert activity(case)["items"][0]["title"] == "管理费用"
    assert (
        group(case, group_key)["members"]["items"][0]["description"] == "清空后重新补入的完整说明。"
    )


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
    note(case, first["business_id"], "培训费报销", purpose="培训费报销")
    note(case, second["business_id"], "差旅费报销", purpose="差旅费报销")
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
    assert "display_summary" not in movement
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

    note(case, first["business_id"], "培训费报销", purpose="培训费报销")
    with monkeypatch.context() as patch:
        patch.setattr(case.kernel.ledger, "get_adopted_contents", exact)
        assert case.kernel.reading.run_pending(case.company)
    assert set(untouched) == {first["business_id"]}
    assert activity(case)["items"][0]["title"] == "管理费用"
    with case.kernel.storage.read_unit(case.company) as read:
        repo = read.repository("reading")
        scope = selection(case)
        saved = _brief_activity_index.base(
            repo, repo.state(_monthly.key(scope), "brief_activity")
        ).payload
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
    note(case, first["business_id"], "培训费报销", purpose="培训费报销")
    revised = deepcopy(first)
    revised["data"]["amount_fen"] = "80000"
    result = case.app.accounting_apply(
        case.request(mutations=[{"op": "replace", "expected_seq": 1, "fact": revised}])
    )
    assert result["completion"] == "complete", result
    capture(case)
    assert activity(case)["items"][0]["title"] == "培训费报销"
    assert case.close(case.prepare_close())["state"] == "closed"
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
    for row in activity(case)["items"]:
        if row["title"] != "个人代扣":
            continue
        member = group(case, row["key"])["members"]["items"][0]
        data = progress(
            case,
            wage["business_id"],
            detail_scope_key=member["detail_scope_key"],
            voucher_version_id=member["voucher_version_id"],
        )["data"]
        detail = data["display_profiles"]["business"]["values"]
        assert detail["display_name"] == row["title"] and detail["purpose"] is None
        assert detail["note"] is None
        notes = [item for item in data["settlements"]["obligations"] if item["description"]]
        assert [(item["description"], item["note_scope"]) for item in notes] == [
            ("整月工资核对说明", "whole_business")
        ]
    for voucher in vouchers(case)["items"]:
        if voucher["subject_id"] != wage["business_id"]:
            continue
        data = progress(
            case,
            wage["business_id"],
            detail_scope_key="voucher:" + voucher["voucher_version_id"],
            voucher_version_id=voucher["voucher_version_id"],
        )["data"]
        detail = data["display_profiles"]["business"]["values"]
        assert detail["purpose"] is None
        assert detail["note"] is None
        assert any(
            item["description"] == "整月工资核对说明" for item in data["settlements"]["obligations"]
        )
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
    assert whole_payment["note_scope"] == "whole_business"


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
    kind, data = "people.remuneration", {"type": code}
    if code == "bank_fee":
        kind, data = "operations.expense", {"expense_class": code}
    elif code == "interest":
        kind, data = "financing.interest_period", {}
    body = SimpleNamespace(
        target={"kind": kind}, basis={"input": data}, reference={"business_id": "source"}
    )
    assert (
        bool(purpose_reference(body, BusinessLabel(code, "业务名称"), component=component))
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
    part.subjects = (
        DescriptionSubject(tax, BusinessLabel("vat", "增值税"), source_slot="primary"),
        DescriptionSubject(labor, BusinessLabel("labor", "劳务报酬"), "net_pay", "net_pay"),
    )
    body = SimpleNamespace(
        target={"kind": "money.transfer"},
        basis={"input": {"type": "offset"}},
        reference={"business_id": "offset", "result_id": "offset-result", "generation": 1},
    )
    assert part_references(body, part) == (tax.reference, labor.reference, body.reference)
    prose = SimpleNamespace(
        text=lambda reference: "完整翻译费说明",
        purpose=lambda subject: "翻译费" if subject.content is labor else None,
        scope=lambda reference: "business:" + reference["business_id"],
        digest=lambda subjects, **kwargs: "display-digest",
    )
    assert part_display(body, part, prose, "original").purpose == "往来抵销"


def test_standard_bank_interest_keeps_its_name_and_reads_detail_separately(case, monkeypatch):
    initialize(case)
    receipt = fact(
        "operations.other_income",
        {"income_kind": "bank_interest", "amount_fen": "28", "funds_account_id": BANK},
        actual="2026-01-20",
    )
    row = original_row(case, str(uuid4()), "28", "2026-01-20", "inflow")
    row["memo"] = "银行原文：季度结息"
    apply(case, receipt, *pair(BANK, [row], [receipt]))
    note(case, receipt["business_id"], "银行结息完整业务说明", purpose="不替代银行利息名称")
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
    assert "business:" + receipt["business_id"] in queried
    assert result["display_profiles"]["business"]["values"]["note"] == "银行结息完整业务说明"
    statement = funds_page(case)["data"]["collections"]["statements"]["items"][0]
    assert statement["memo"] == "银行原文：季度结息"
    activity_version = component_snapshot(case, "brief_activity").version
    funds_version = component_snapshot(case, "funds").version
    note(
        case,
        receipt["business_id"],
        "银行结息完整业务说明",
        purpose="另一个不能覆盖标准名的短事项",
        seq=1,
    )
    assert case.kernel.reading.run_pending(case.company)
    assert component_snapshot(case, "brief_activity").version == activity_version
    assert component_snapshot(case, "funds").version == funds_version


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
    text = "本月翻译服务完成，已确认报酬和个人扣税，净额随后支付。"
    note(case, labor["business_id"], text, purpose="翻译服务费")
    note(case, paid["business_id"], "本单付清翻译费")
    capture(case)
    funds_capture(case)
    assert {row["title"] for row in activity(case)["items"]} == {
        "薪酬计提",
        "支付翻译服务费",
        "个人代扣",
    }
    payroll_vouchers = [
        row for row in vouchers(case)["items"] if row["subject_id"] == labor["business_id"]
    ]
    ordinary = next(row for row in payroll_vouchers if row["business_amount_label"] == "计提金额")
    assert ordinary["type"] == "薪酬计提" and ordinary["list_summary"] == "翻译服务费"
    accrued = progress(
        case,
        labor["business_id"],
        detail_scope_key="voucher:" + ordinary["voucher_version_id"],
        voucher_version_id=ordinary["voucher_version_id"],
    )["data"]["display_profiles"]["business"]["values"]
    assert accrued["purpose"] == "翻译服务费"
    withheld = next(row for row in payroll_vouchers if row["business_amount_label"] == "代扣金额")
    assert withheld["type"] == withheld["list_summary"] == "个人代扣"
    tax_data = progress(
        case,
        labor["business_id"],
        detail_scope_key="voucher:" + withheld["voucher_version_id"],
        voucher_version_id=withheld["voucher_version_id"],
    )["data"]
    tax_progress = tax_data["display_profiles"]["business"]["values"]
    assert tax_progress["display_name"] == "个人代扣" and tax_progress["purpose"] is None
    assert tax_progress["note"] is None
    assert [
        (item["description"], item["note_scope"]) for item in tax_data["settlements"]["obligations"]
    ] == [(text, "whole_business")]
    tax_row = next(row for row in activity(case)["items"] if row["title"] == "个人代扣")
    tax_member = group(case, tax_row["key"])["members"]["items"][0]
    tax_data = progress(
        case,
        labor["business_id"],
        detail_scope_key=tax_member["detail_scope_key"],
        voucher_version_id=tax_member["voucher_version_id"],
    )["data"]
    tax_part = tax_data["display_profiles"]["business"]["values"]
    assert tax_part["display_name"] == "个人代扣" and tax_part["purpose"] is None
    assert tax_part["note"] is None
    assert [
        (item["description"], item["note_scope"]) for item in tax_data["settlements"]["obligations"]
    ] == [(text, "whole_business")]
    movement = funds_page(case)["data"]["collections"]["movements"]["items"][0]
    assert movement["list_summary"] == "支付翻译服务费"
    snapshot = publish(case)
    opened = open_page(case, snapshot)["collections"]["open_items"]["items"]
    assert any(row["description"] == "代扣个人所得税" for row in opened)
    paid_data = progress(case, paid["business_id"])["data"]
    obligation = paid_data["settlements"]["obligations"][0]
    assert (obligation["purpose"], obligation["description"], obligation["note_scope"]) == (
        "翻译服务费",
        text,
        "whole_business",
    )
    cash = next(row for row in activity(case)["items"] if row["group"] == "cash")
    assert group(case, cash["key"])["members"]["items"][0]["description"] == "支付翻译服务费"
    whole = paid_data["display_profiles"]["business"]["values"]
    assert whole["purpose"] == "支付翻译服务费"
    assert whole["note"] == "本单付清翻译费"
    assert whole["note_scope"] == "whole_business"


def test_payroll_reserve_whole_note_never_becomes_the_reserve_components_purpose(case, monkeypatch):
    _, paid = payroll(case)
    text = "1月工资及办公室备用金整单汇出"
    note(case, paid["business_id"], text)
    with monkeypatch.context() as patch:

        def no_progress(*args, **kwargs):
            pytest.fail("显示组和成员时不能预读完整业务进展。")

        patch.setattr("ai_accounting_modular.reading._business_status.detail", no_progress)
        capture(case)
        selected = [row for row in activity(case)["items"] if row["group"] == "cash"]
        selected_members = [group(case, row["key"])["members"]["items"][0] for row in selected]
    assert {row["title"] for row in selected} == {"支付员工工资", "支付备用金费用"}
    for member in selected_members:
        assert member["detail_scope_key"] == member["key"]
        detail = progress(
            case,
            paid["business_id"],
            detail_scope_key=member["detail_scope_key"],
            voucher_version_id=member["voucher_version_id"],
        )["data"]["display_profiles"]["business"]["values"]
        assert detail["purpose"] is None
        assert detail["note"] == text and detail["note_scope"] == "whole_business"
    whole = progress(case, paid["business_id"])["data"]["display_profiles"]["business"]["values"]
    assert whole["purpose"] is None
    assert whole["note"] == text and whole["note_scope"] == "whole_business"
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
    assert visible["note"] == text and visible["note_scope"] == "whole_business"
    note(case, paid["business_id"], "", seq=1)
    assert case.kernel.reading.run_pending(case.company)
    reserve = next(row for row in activity(case)["items"] if row["title"] == "支付备用金费用")
    assert group(case, reserve["key"])["members"]["items"][0]["detail_scope_key"] is None
    note(case, paid["business_id"], text, seq=2)
    assert case.kernel.reading.run_pending(case.company)
    assert group(case, reserve["key"])["members"]["items"][0]["detail_scope_key"] is not None


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
    note(case, paid["business_id"], "", purpose="办公室备用金费用")
    capture(case)
    funds_capture(case)
    assert activity(case)["items"][0]["title"] == "支付办公室备用金费用"
    movement = funds_page(case)["data"]["collections"]["movements"]["items"][0]
    assert movement["list_summary"] == "支付办公室备用金费用"
    assert "display_summary" not in movement
    whole = progress(case, paid["business_id"])["data"]["display_profiles"]["business"]["values"]
    assert whole["purpose"] == "支付办公室备用金费用" and whole["note"] is None


@pytest.mark.parametrize("event", ["acquire", "maturity"])
def test_independent_investment_obligation_keeps_its_formal_meaning_and_whole_note(case, event):
    from tests.modular.domains.financing._investment_support import acquire, dispose, terms

    initialize(case)
    agreement = terms()
    agreement["data"]["terms_source"]["source_id"] = case.source_id
    purchased = acquire(agreement, "11000" if event == "acquire" else "10000", "10")
    facts = [agreement, purchased]
    if event == "acquire":
        purchased["data"]["separate_declared_income_fen"] = "1000"
        business, slot, title, short = (
            purchased,
            "declared_income",
            "取得前已宣告投资收益",
            "基金投资取得",
        )
    else:
        matured = dispose(agreement, "10", "11000", "2026-01-05")
        matured["data"].update(
            exit_mode="maturity",
            maturity_income={
                "character": "fund_distribution",
                "amount_fen": "1000",
                "source": {"source_id": case.source_id, "location": "合成到期本金及独立收益结算单"},
            },
        )
        facts.append(matured)
        business, slot, title, short = matured, "income", "到期独立投资收益", "基金到期退出"
    primary = payment(
        business,
        500,
        party=COUNTERPARTY,
        slot="primary",
        direction="outflow" if event == "acquire" else "inflow",
        day="2026-01-07",
    )
    income = payment(
        business, 500, party=COUNTERPARTY, slot=slot, direction="inflow", day="2026-01-08"
    )
    apply(case, *facts, primary, income)
    text = "本次投资整单同时包含本金及单独的收益权利，按实际约定分别收付。"
    note(case, business["business_id"], text, purpose=short)
    capture(case)
    data = progress(case, business["business_id"])["data"]
    obligations = data["settlements"]["obligations"]
    assert next(row for row in obligations if row["name"] == slot)["purpose"] == title
    assert next(row for row in obligations if row["name"] == "primary")["purpose"] == short
    descriptions = [row for row in obligations if row["description"]]
    assert [(row["description"], row["note_scope"]) for row in descriptions] == [
        (text, "whole_business")
    ]
    received_group = next(row for row in activity(case)["items"] if row["title"] == "收到" + title)
    received_member = group(case, received_group["key"])["members"]["items"][0]
    assert received_member["subject_id"] == income["business_id"]
    assert received_member["description"] == "收到" + title
    assert received_member["detail_scope_key"] == received_member["key"]
    selected = progress(
        case,
        income["business_id"],
        detail_scope_key=received_member["detail_scope_key"],
        voucher_version_id=received_member["voucher_version_id"],
    )["data"]
    assert selected["settlements"]["obligations"][0]["purpose"] == title
    assert selected["settlements"]["obligations"][0]["note_scope"] == "whole_business"
    assert {
        row["purpose_label"] for row in selected["collections"]["settlement_events"]["items"]
    } == {title}
    funds_capture(case)
    movement = next(
        row
        for row in funds_page(case)["data"]["collections"]["movements"]["items"]
        if row["subject_id"] == income["business_id"]
    )
    assert movement["list_summary"] == "收到" + title
    snapshot = publish(case)
    assert title in {
        row["description"]
        for row in open_page(case, snapshot)["collections"]["open_items"]["items"]
    }


def test_pass_through_parts_keep_their_own_meanings_in_all_consumers(case):
    initialize(case)
    passed = fact(
        "operations.pass_through",
        {"payer_id": COUNTERPARTY, "beneficiary_id": case.party, "amount_fen": "1000"},
    )
    received = payment(passed, 500, party=COUNTERPARTY, slot="collection", direction="inflow")
    remitted = payment(passed, 500, party=case.party, slot="remittance", direction="outflow")
    apply(case, passed, received, remitted)
    text = "本次按约定代收后转付，收款和转付分别登记。"
    note(case, passed["business_id"], text, purpose="房租代收转付")
    capture(case)
    assert {row["title"] for row in activity(case)["items"]} == {
        "房租代收转付",
        "收到代收款",
        "支付代付款转付",
    }
    data = progress(case, passed["business_id"])["data"]
    assert {row["purpose"] for row in data["settlements"]["obligations"]} == {
        "代收款",
        "代付款转付",
    }
    notes = [row for row in data["settlements"]["obligations"] if row["description"]]
    assert [(row["description"], row["note_scope"]) for row in notes] == [(text, "whole_business")]
    funds_capture(case)
    assert {
        row["list_summary"] for row in funds_page(case)["data"]["collections"]["movements"]["items"]
    } == {"收到代收款", "支付代付款转付"}
