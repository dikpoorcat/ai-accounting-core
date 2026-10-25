"""Concrete current descriptions follow exact sources without changing accounting groups."""

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

from ai_accounting_modular.contracts.public import loads_json
from ai_accounting_modular.reading import _brief_activity, _brief_activity_index, _monthly
from ai_accounting_modular.reading._business_descriptions import Descriptions
from tests.modular.application._working_case import case as case
from tests.modular.domains.financing._support import COUNTERPARTY, fact, payment
from tests.modular.money.test_payroll import inputs as payroll_inputs

from .test_brief_activity import activity, capture, group, vouchers
from .test_brief_open import members, publish
from .test_brief_open import page as open_page
from .test_business_status import progress
from .test_funds import apply, initialize, selection
from .test_funds import capture as funds_capture
from .test_funds import page as funds_page


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
    assert all(
        set(value["title"].split("；")) == {"培训费报销", "差旅费报销"} for value in after["items"]
    )
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
    assert set(opened["description"].split("；")) == {"培训费报销", "差旅费报销"}
    detail = members(case, snapshot, opened["group_key"])["collections"]["members"]["items"]
    assert {value["description"] for value in detail} == {"培训费报销", "差旅费报销"}
    funds_capture(case)
    movement = funds_page(case)["data"]["collections"]["movements"]["items"][0]
    assert movement["type"] == "支付管理费用"
    assert "培训费报销" in movement["list_summary"] and "差旅费报销" in movement["list_summary"]
    assert "整单付款说明" not in movement["list_summary"]
    paid_group = next(value for value in after["items"] if value["group"] == "cash")
    selected = group(case, paid_group["key"])["members"]["items"][0]
    business = progress(
        case,
        paid["business_id"],
        detail_scope_key=selected["detail_scope_key"],
        voucher_version_id=selected["voucher_version_id"],
    )["data"]
    assert set(business["display_profiles"]["business"]["values"]["purpose"].split("；")) == {
        "培训费报销",
        "差旅费报销",
    }
    assert {
        value["purpose_label"] for value in business["collections"]["settlement_events"]["items"]
    } == {"培训费报销", "差旅费报销"}


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
    assert set(activity(case)["items"][0]["title"].split("；")) == {"培训费报销", "管理费用"}
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
    assert [
        value["purpose_label"] for value in data["collections"]["settlement_events"]["items"]
    ] == ["个人社保"]
    funds_capture(case)
    movement = funds_page(case)["data"]["collections"]["movements"]["items"][0]
    assert movement["list_summary"] == "支付社保费"
