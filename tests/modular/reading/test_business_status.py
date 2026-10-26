"""One-business progress uses exact formal relations before totals and event pagination."""

from copy import deepcopy
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError, validate_contract
from ai_accounting_modular.reading import _business_status
from tests.modular.application._working_case import case as case
from tests.modular.domains.financing._support import fact
from tests.modular.domains.financing._support import payment as batch_payment
from tests.modular.domains.people.test_read_remuneration import bank_object
from tests.modular.money.test_payroll import inputs as payroll_inputs

from ._support import source
from .test_brief_activity import activity, capture, group, mixed, vouchers
from .test_brief_open import apply, members, page, payment, publish
from .test_brief_open_settlements import cooperation_due
from .test_funds import apply as apply_financing
from .test_funds import initialize, selection


def progress(case, business, month="2026-01", **query):
    delivery = _business_status.detail(
        case.kernel.reading,
        selection(case, month),
        {"subject_id": business, **query},
    )
    assert delivery.state == "ready"
    payload = delivery.payload
    validate_contract(
        "dashboard-response-v2.json",
        "dashboard_business_status",
        {
            "schema_version": 10,
            "snapshot_version": delivery.version,
            "selected_period": payload["selected_period"],
            "read_context": {
                "company_id": case.company,
                "database_id": payload["database_id"],
                "as_of": payload["data"]["as_of"],
                "read_version": delivery.version,
            },
            "read_semantics": {
                "knowledge": "current_knowledge",
                "accounting": "as_posted",
                "business_basis": "current_known",
                "display": "current",
                "system_time_replay": False,
                "recorded_at": "system_recording_time",
                "recording_period": "business_recording_period",
                "recorded_later": "business_recording_period_after_selected_period",
            },
            "data": payload["data"],
        },
    )
    return payload


def test_direct_business_needs_no_monthly_models_and_keeps_historical_and_current_separate(
    case, monkeypatch
):
    expense = case.seed()
    bank = bank_object()
    apply(
        case,
        payment(case, expense["business_id"], bank["object_id"], "60000", "2026-02"),
        objects=[bank],
    )
    contexts, original = [], case.kernel.storage.read_unit

    def read_once(company, *args, **kwargs):
        contexts.append(company)
        return original(company, *args, **kwargs)

    def no_full_journals(*args, **kwargs):
        raise AssertionError("business progress must not load complete vouchers or monthly pages")

    monkeypatch.setattr(case.kernel.storage, "read_unit", read_once)
    monkeypatch.setattr(case.kernel.ledger, "get_vouchers", no_full_journals)
    monkeypatch.setattr(case.kernel.ledger, "query_obligation_positions", no_full_journals)
    monkeypatch.setattr(case.kernel.ledger, "query_settlement_events", no_full_journals)
    payload = progress(case, expense["business_id"])
    assert contexts == [case.company]
    data = payload["data"]
    assert data["identity"]["kind"] == "operations.expense"
    original_due = data["settlements"]["obligations"][0]
    current_due = data["current_followups"]["settlements"]["obligations"][0]
    assert (
        original_due["source_amount_fen"],
        original_due["paid_fen"],
        original_due["remaining_fen"],
    ) == ("100000", "0", "100000")
    assert (current_due["paid_fen"], current_due["remaining_fen"]) == ("60000", "40000")
    assert data["settlements"]["cutoff_period"] == "2026-01"
    assert data["current_followups"]["settlements"]["cutoff_period"] == "2026-02"
    event = data["collections"]["settlement_events"]["items"][0]
    assert event["signed_amount_fen"] == "60000" and event["posting_period"] == "2026-02"
    historical = progress(case, expense["business_id"], settlement_view="historical")
    assert historical["data"]["collections"]["settlement_events"]["items"] == []
    with original(case.company) as read:
        assert read.repository("reading").scopes() == ()


def test_complete_21_payments_precede_20_item_page_and_unrelated_business_does_not_invalidate(case):
    expense = case.seed()
    bank = bank_object()
    apply(
        case,
        *[payment(case, expense["business_id"], bank["object_id"], "1") for _ in range(21)],
        objects=[bank],
    )
    first = progress(case, expense["business_id"])
    data = first["data"]
    due = data["settlements"]["obligations"][0]
    assert (due["paid_fen"], due["remaining_fen"]) == ("21", "99979")
    collection = data["collections"]["settlement_events"]
    assert collection["page"]["total_count"] == collection["page"]["filtered_count"] == 21
    assert len(collection["items"]) == 20 and collection["page"]["has_more"]
    apply(case, case.expense("700"))
    second = progress(
        case,
        expense["business_id"],
        section="settlement_events",
        cursor=collection["page"]["next_cursor"],
        expected_version=first["snapshot_version"],
    )
    assert second["snapshot_version"] == first["snapshot_version"]
    assert len(second["data"]["collections"]["settlement_events"]["items"]) == 1
    assert not second["data"]["collections"]["settlement_events"]["page"]["has_more"]
    apply(case, payment(case, expense["business_id"], bank["object_id"], "1"))
    with pytest.raises(ContractError) as error:
        progress(
            case,
            expense["business_id"],
            cursor=collection["page"]["next_cursor"],
            expected_version=first["snapshot_version"],
        )
    assert error.value.code == "snapshot_changed"


def test_precise_mixed_payment_activity_clips_purpose_relations_before_events_and_totals(case):
    purchase, expense, paid = mixed(case)
    capture(case)
    part = next(
        item
        for item in activity(case)["items"]
        if item["kind"] == "money.movement" and item["amount_fen"] == "300"
    )
    member = group(case, part["key"])["members"]["items"][0]
    query = {
        "detail_scope_key": member["detail_scope_key"],
        "voucher_version_id": member["voucher_version_id"],
    }
    payload = progress(case, paid["business_id"], **query)
    data = payload["data"]
    assert data["detail_scope"]["amount_fen"] == "300"
    assert len(data["settlements"]["obligations"]) == 1
    assert data["settlements"]["obligations"][0]["source_amount_fen"] == "300"
    events = data["collections"]["settlement_events"]
    assert events["page"]["total_count"] == 1
    assert [item["signed_amount_fen"] for item in events["items"]] == ["300"]
    whole = progress(case, paid["business_id"])["data"]
    assert len(whole["settlements"]["obligations"]) == 2
    assert {
        item["signed_amount_fen"] for item in whole["collections"]["settlement_events"]["items"]
    } == {"300", "700"}
    with pytest.raises(ContractError) as error:
        progress(case, expense["business_id"], **query)
    assert error.value.code == "snapshot_changed"
    with pytest.raises(ContractError) as error:
        progress(case, paid["business_id"], **(query | {"voucher_version_id": "foreign"}))
    assert error.value.code == "snapshot_changed"


def test_exact_old_correction_leg_keeps_its_original_month_and_does_not_adopt_new_party(case):
    expense = case.seed()
    assert case.close(case.check())["state"] == "closed"
    other = str(uuid4())
    revised = deepcopy(expense)
    revised["data"].update(creditor_id=other, amount_fen="80000")
    apply(
        case,
        objects=[
            {
                "object_id": other,
                "kind": "organization",
                "name": "新供应商",
                "expected_seq": 0,
                "data": {},
            }
        ],
        mutations=[{"op": "replace", "expected_seq": 1, "fact": revised}],
        correction_period="2026-02",
    )
    capture(case, "2026-02")
    corrected = next(
        item for item in activity(case, "2026-02")["items"] if item["amount_fen"] == "-100000"
    )
    member = group(case, corrected["key"], "2026-02")["members"]["items"][0]
    assert member["detail_scope_key"] == member["key"]
    payload = progress(
        case,
        expense["business_id"],
        "2026-02",
        detail_scope_key=member["detail_scope_key"],
        voucher_version_id=member["voucher_version_id"],
    )
    data = payload["data"]
    assert data["settlements"]["cutoff_period"] == "2026-01"
    assert data["settlements"]["obligations"][0]["source_amount_fen"] == "100000"
    current = data["current_followups"]["settlements"]["obligations"][0]
    assert current["settlement_status"] == "reversed" and current["remaining_fen"] == "0"
    assert {item["entity_id"] for item in data["display_profiles"]["counterparties"]} == {
        case.party
    }
    reverse = next(item for item in vouchers(case, "2026-02")["items"] if item["state"] == "冲正")
    voucher_detail = progress(
        case,
        expense["business_id"],
        "2026-02",
        detail_scope_key="voucher:" + reverse["voucher_version_id"],
        voucher_version_id=reverse["voucher_version_id"],
    )
    assert voucher_detail["data"]["settlements"] == data["settlements"]
    assert voucher_detail["data"]["detail_scope"]["amount_fen"] == "-100000"


def test_open_member_without_voucher_keeps_person_and_real_recipient_separate(case):
    person, recipient = str(uuid4()), str(uuid4())
    bank = bank_object()
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
                    {"type": "bank", "funds_account_id": bank["object_id"], "balance_fen": "100"},
                    {
                        "type": "payroll_payable",
                        "person_id": person,
                        "recipient_id": recipient,
                        "payroll_period": "2025-12",
                        "component": "employee_social",
                        "amount_fen": "100",
                    },
                )
            ],
        },
    }
    apply(
        case,
        package,
        objects=[
            bank,
            {
                "object_id": person,
                "kind": "person",
                "name": "员工甲",
                "expected_seq": 0,
                "data": {},
            },
            {
                "object_id": recipient,
                "kind": "organization",
                "name": "实际机构",
                "expected_seq": 0,
                "data": {},
            },
        ],
        sources=[source("opening-progress") | {"source_id": case.source_id}],
    )
    snapshot = publish(case)
    row = page(case, snapshot)["collections"]["open_items"]["items"][0]
    member = members(case, snapshot, row["group_key"])["collections"]["members"]["items"][0]
    assert member["voucher_version_id"] is None
    data = progress(
        case, package["business_id"], detail_scope_key="open:" + member["id"], voucher_version_id=""
    )["data"]
    assert data["detail_scope"]["voucher_version_id"] == ""
    assert data["settlements"]["obligations"][0]["source_period"] == "2025-12"
    assert {item["entity_id"] for item in data["display_profiles"]["employees"]} == {person}
    assert {item["entity_id"] for item in data["display_profiles"]["counterparties"]} == {recipient}


def test_social_payment_scope_does_not_expand_to_employer_social_or_salary(case):
    case.seed(expense=False)
    call, gross, wage = payroll_inputs(case.company, pay=False)
    assert case.app.accounting_apply(call)["completion"] == "complete"
    paid = fact(
        "money.movement",
        {
            "type": "settlement",
            "funds_account_id": gross["data"]["funds_account_id"],
            "direction": "outflow",
            "amount_fen": "300000",
            "payment_method": "individual",
            "allocations": [
                {"source": {"business_id": wage["business_id"], "slot": slot}, "amount_fen": amount}
                for slot, amount in (("employee_social", "100000"), ("employer_social", "200000"))
            ],
        },
        actual="2026-01-20",
    )
    apply_financing(case, paid)
    capture(case)
    row = next(item for item in activity(case)["items"] if item["title"] == "支付个人承担社保费")
    member = group(case, row["key"])["members"]["items"][0]
    data = progress(
        case,
        paid["business_id"],
        detail_scope_key=member["detail_scope_key"],
        voucher_version_id=member["voucher_version_id"],
    )["data"]
    assert data["detail_scope"]["amount_fen"] == "100000"
    assert [(item["name"], item["paid_fen"]) for item in data["settlements"]["obligations"]] == [
        ("employee_social", "100000")
    ]
    assert [
        item["signed_amount_fen"] for item in data["collections"]["settlement_events"]["items"]
    ] == ["100000"]
    assert {item["entity_id"] for item in data["display_profiles"]["employees"]} == {
        wage["data"]["person_id"]
    }
    assert (
        progress(case, wage["business_id"])["data"]["current_business_result"]["amount_fen"]
        == "1000000"
    )


def test_original_multi_person_bank_batch_keeps_all_its_people_and_excludes_other_purpose(case):
    initialize(case)
    persons = [str(uuid4()), str(uuid4())]
    wages = [
        fact(
            "people.remuneration",
            {
                "type": "labor",
                "person_id": person,
                "gross_fen": value,
                "destination": {"type": "expense", "area": "administration"},
                "withholding_state": "not_withheld_not_filed",
            },
        )
        for person, value in zip(persons, ("300", "700"), strict=True)
    ]
    expense = case.expense("1100")
    paid = batch_payment(wages[0], 2100, slot="net_pay")
    paid["data"].update(
        payment_method="bank_batch",
        allocations=[
            {
                "source": {"business_id": wage["business_id"], "slot": "net_pay"},
                "amount_fen": value,
                "recipient_id": person,
            }
            for wage, person, value in zip(wages, persons, ("300", "700"), strict=True)
        ]
        + [
            {
                "source": {"business_id": expense["business_id"], "slot": "primary"},
                "amount_fen": "1100",
                "recipient_id": case.party,
            }
        ],
    )
    paid["data"].pop("counterparty_id")
    apply_financing(
        case,
        *wages,
        expense,
        paid,
        objects=[
            {
                "object_id": person,
                "kind": "person",
                "name": "实际劳务人员" + str(index),
                "expected_seq": 0,
                "data": {},
            }
            for index, person in enumerate(persons)
        ],
    )
    capture(case)
    row = next(item for item in activity(case)["items"] if item["title"] == "支付劳务报酬")
    member = group(case, row["key"])["members"]["items"][0]
    data = progress(
        case,
        paid["business_id"],
        detail_scope_key=member["detail_scope_key"],
        voucher_version_id=member["voucher_version_id"],
    )["data"]
    assert data["detail_scope"]["amount_fen"] == "1000"
    assert len(data["settlements"]["obligations"]) == 2
    assert {item["paid_fen"] for item in data["settlements"]["obligations"]} == {"300", "700"}
    assert {
        item["signed_amount_fen"] for item in data["collections"]["settlement_events"]["items"]
    } == {"300", "700"}
    assert data["collections"]["settlement_events"]["page"]["total_count"] == 2
    assert {item["entity_id"] for item in data["display_profiles"]["employees"]} == set(persons)
    assert data["display_profiles"]["counterparties"] == []


def test_cooperation_supplement_uses_real_position_with_unknown_payment_history(case):
    agreement, _, _, _ = cooperation_due(case)
    snapshot = publish(case)
    row = next(
        item
        for item in page(case, snapshot)["collections"]["open_items"]["items"]
        if item["description"].startswith("合作投资公司应补")
    )
    member = members(case, snapshot, row["group_key"])["collections"]["members"]["items"][0]
    data = progress(
        case,
        agreement["business_id"],
        detail_scope_key="open:" + member["id"],
        voucher_version_id=member["voucher_version_id"] or "",
    )["data"]
    assert len(data["settlements"]["obligations"]) == 1
    amount = data["settlements"]["obligations"][0]
    assert amount["remaining_fen"] == "20" and amount["settlement_status"] == "current"
    assert amount["source_amount_fen"] is amount["paid_fen"] is amount["other_settled_fen"] is None
    assert not data["settlements"]["checking"]
    assert data["collections"]["settlement_events"]["items"] == []


def test_unknown_and_unprepared_exact_scope_does_not_create_month_models(case):
    expense = case.seed()
    with pytest.raises(ContractError) as error:
        progress(
            case,
            expense["business_id"],
            detail_scope_key="voucher:opaque",
            voucher_version_id="opaque",
        )
    assert error.value.code == "snapshot_changed"
    with case.kernel.storage.read_unit(case.company) as read:
        assert read.repository("reading").scopes() == ()
    with pytest.raises(ContractError) as error:
        progress(case, expense["business_id"], cursor="not-json", expected_version="0" * 64)
    assert error.value.code == "snapshot_changed"


def test_closed_withdrawal_preserves_original_due_and_does_not_invent_payment(case):
    expense = case.seed()
    closed = case.close(case.check())
    assert closed["state"] == "closed"
    apply(
        case,
        mutations=[{"op": "withdraw", "expected_seq": 1, "business_id": expense["business_id"]}],
        correction_period="2026-02",
        explanation="已核实该笔费用误记，没有对应现实业务。",
    )
    data = progress(case, expense["business_id"])["data"]
    assert data["latest_source"] == {"period": "2026-01", "deleted": True}
    assert data["review"] == {"status": "withdrawn"}
    assert data["current_business_result"] is None
    assert data["closure"]["state"] == "exact_close"
    assert data["frozen_adoption"]["amount_fen"] == "100000"
    assert data["settlements"]["obligations"][0]["remaining_fen"] == "100000"
    assert data["collections"]["settlement_events"]["items"] == []


def test_later_close_reports_actual_sealing_boundary_instead_of_inventing_january_close(case):
    expense = case.seed()
    apply(case, case.expense("1", "2026-02"))
    closed = case.close(case.check("2026-02"))
    assert closed["state"] == "closed"
    data = progress(case, expense["business_id"])["data"]
    assert data["closure"]["state"] == "covered_by_later_close"
    assert data["closure"]["sealing_boundary"] == "2026-02"
    assert len(data["closure"]["sealing_digest"]) == 64
    assert data["frozen_adoption"]["close_period"] == "2026-02"
