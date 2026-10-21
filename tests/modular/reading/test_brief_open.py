"""Complete formal open balances stay separate from current progress and member pages."""

from copy import deepcopy
from hashlib import sha256
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import (
    ContractError,
    content_digest,
    loads_json,
    validate_contract,
)
from ai_accounting_modular.reading import _brief_open, _monthly
from ai_accounting_modular.reading._context import period
from tests.modular.application._working_case import case as case
from tests.modular.domains.people.test_read_remuneration import bank_object


def apply(case, *facts, **fields):
    result = case.app.accounting_apply(case.request(*facts, **fields))
    assert result.get("completion") == "complete", result
    return result


def publish(case, month="2026-01"):
    scope = _monthly.normalize(
        {"company_id": case.company, "period_from": month, "period_to": month, "view": "as_posted"}
    )
    with case.kernel.storage.read_unit(case.company) as read:
        model = _brief_open.prepare(case.kernel.reading, scope, read)
        selected = period(month, case.kernel.ledger.closed_through(read))
        database = read.database_id
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        repository = unit.repository("reading")
        component = _monthly.BRIEF_OPEN_ITEMS
        repository.register(_monthly.key(scope), component, unit.head, _monthly.FORMAT_VERSION)
        repository.mark(_monthly.key(scope), component, unit.head)
        assert repository.publish(
            _monthly.key(scope), component, unit.head, _monthly.FORMAT_VERSION, model
        )
    return _monthly.MonthSnapshot(
        _monthly.key(scope),
        scope,
        content_digest(
            {"open": model.content_digest, "scope": scope, "status": selected["status"]}
        ),
        {_monthly.BRIEF_OPEN_ITEMS: loads_json(model.payload_json)},
        selected,
        case.company,
        database,
    )


def page(case, snapshot, **query):
    with case.kernel.storage.read_unit(case.company) as read:
        return _brief_open.page(snapshot, read, query)


def members(case, snapshot, group_key, **query):
    with case.kernel.storage.read_unit(case.company) as read:
        result = _brief_open.members(
            case.kernel.reading, snapshot, read, {"group_key": group_key, **query}
        )
    validate_contract(
        "dashboard-response-v2.json",
        "dashboard_brief_group",
        {
            "schema_version": 3,
            "snapshot_version": snapshot.version,
            "selected_period": snapshot.selected_period,
            "read_context": {
                "company_id": case.company,
                "database_id": snapshot.database_id,
                "as_of": "2026-10-21",
                "read_version": snapshot.version,
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
            "data": result,
        },
    )
    return result


def payment(case, source, bank, amount, month="2026-01"):
    return {
        "business_id": str(uuid4()),
        "kind": "money.movement",
        "period": month,
        "effective_on": month + "-20",
        "evidence_ids": [case.source_id],
        "data": {
            "type": "settlement",
            "funds_account_id": bank,
            "direction": "outflow",
            "payment_method": "individual",
            "amount_fen": amount,
            "allocations": [
                {"source": {"business_id": source, "slot": "primary"}, "amount_fen": amount}
            ],
        },
    }


def test_cold_summary_groups_without_lines_then_hydrates_only_visible_precise_vouchers(
    case, monkeypatch
):
    first = case.seed()
    second = case.expense("200000")
    apply(case, second)
    original = case.kernel.ledger.get_vouchers

    def no_lines(*args):
        pytest.fail("cold group summaries must not load journal lines")

    monkeypatch.setattr(case.kernel.ledger, "get_vouchers", no_lines)
    snapshot = publish(case)
    result = page(case, snapshot)
    assert result["open_items"]["complete"]
    assert result["open_items"]["payable_fen"] == "300000"
    assert result["open_items"]["receivable_fen"] == "0"
    groups = result["collections"]["open_items"]["items"]
    assert len(groups) == 1 and groups[0]["member_count"] == 2
    assert groups[0]["description"] == "管理费用"
    assert result["open_items"]["categories"][0]["loaded_count"] == 2
    monkeypatch.setattr(case.kernel.ledger, "get_vouchers", original)
    detail = members(case, snapshot, groups[0]["group_key"], limit=1)
    values = detail["collections"]["members"]
    assert values["page"]["total_count"] == values["page"]["filtered_count"] == 2
    assert len(values["items"]) == 1 and values["page"]["has_more"]
    item = values["items"][0]
    assert item["date"] is None and item["recognition"]["precision"] == "month"
    assert not any(key.startswith("_") for key in item)
    voucher = detail["collections"]["vouchers"]["items"][0]
    assert item["voucher_version_id"] == voucher["voucher_version_id"]
    assert voucher["lines"] and voucher["subject_id"] == item["subject_id"]
    next_page = members(
        case, snapshot, groups[0]["group_key"], cursor=values["page"]["next_cursor"]
    )
    assert next_page["collections"]["members"]["items"][0]["id"] != item["id"]
    with case.kernel.storage.read_unit(case.company) as read:
        exact = _brief_open.member_scope(snapshot, read, item["id"])
    assert exact["reference"]["business_id"] in {first["business_id"], second["business_id"]}
    assert exact["obligations"][0]["producer"] == exact["source"]


def test_missing_opening_is_unknown_but_known_items_still_display(case):
    expense = case.expense("700")
    apply(
        case,
        expense,
        objects=[
            {
                "object_id": case.party,
                "kind": "organization",
                "name": "有据供应商",
                "expected_seq": 0,
                "data": {},
            }
        ],
        sources=[
            {
                "source_id": case.source_id,
                "kind": "owner_statement",
                "text": "合成缺期初费用",
                "sha256": sha256("合成缺期初费用".encode()).hexdigest(),
                "archive_month": "2026-01",
            }
        ],
    )
    result = page(case, publish(case))
    assert result["open_items"]["complete"] is False
    assert result["open_items"]["payable_fen"] is result["open_items"]["receivable_fen"] is None
    assert result["open_items"]["categories"][0]["outstanding_fen"] == "700"
    assert result["collections"]["open_items"]["items"][0]["outstanding_fen"] == "700"


def test_later_real_payment_refreshes_followup_without_changing_january_amount(case):
    expense = case.seed()
    january = page(case, publish(case))
    bank = bank_object()
    apply(
        case,
        payment(case, expense["business_id"], bank["object_id"], "60000", "2026-02"),
        objects=[bank],
    )
    snapshot = publish(case)
    after = page(case, snapshot)
    assert after["open_items"]["cutoff_period"] == "2026-01"
    assert after["open_items"]["current_cutoff_period"] == "2026-02"
    before_group = january["collections"]["open_items"]["items"][0]
    group = after["collections"]["open_items"]["items"][0]
    assert group["group_key"] == before_group["group_key"]
    assert (group["source_amount_fen"], group["paid_fen"], group["outstanding_fen"]) == (
        "100000",
        "0",
        "100000",
    )
    assert (group["current_status"], group["current_outstanding_fen"]) == ("partial", "40000")
    february = page(case, publish(case, "2026-02"))["collections"]["open_items"]["items"][0]
    assert (february["paid_fen"], february["outstanding_fen"]) == ("60000", "40000")
    with pytest.raises(ContractError) as error:
        members(case, snapshot, "open:missing")
    assert error.value.code == "not_found"


def test_closed_correction_preserves_old_counterparty_and_reports_it_reversed(case):
    expense = case.seed()
    assert case.close(case.check())["state"] == "closed"
    other = str(uuid4())
    revised = deepcopy(expense)
    revised["data"].update(creditor_id=other, amount_fen="80000")
    saved = case.app.accounting_apply(
        case.request(
            objects=[
                {
                    "object_id": other,
                    "kind": "organization",
                    "name": "更正后供应商",
                    "expected_seq": 0,
                    "data": {},
                }
            ],
            mutations=[{"op": "replace", "expected_seq": 1, "fact": revised}],
            correction_period="2026-02",
        )
    )
    assert saved["completion"] == "complete", saved
    january = page(case, publish(case))["collections"]["open_items"]["items"][0]
    assert january["party"] == "合成供应商"
    assert january["outstanding_fen"] == "100000"
    assert (january["current_status"], january["current_outstanding_fen"]) == ("reversed", "0")
    february = page(case, publish(case, "2026-02"))["collections"]["open_items"]["items"][0]
    assert february["party"] == "更正后供应商" and february["outstanding_fen"] == "80000"


def test_unrelated_fully_paid_business_does_not_change_the_complete_open_projection(
    case, monkeypatch
):
    case.seed()
    before = publish(case)
    unrelated = case.expense("700")
    bank = bank_object()
    apply(
        case,
        unrelated,
        payment(case, unrelated["business_id"], bank["object_id"], "700"),
        objects=[bank],
    )
    original = case.kernel.ledger.get_adopted_contents

    def limited(refs, read):
        assert all(ref["business_id"] != unrelated["business_id"] for ref in refs)
        return original(refs, read)

    monkeypatch.setattr(case.kernel.ledger, "get_adopted_contents", limited)
    after = publish(case)
    assert after.components == before.components and after.version == before.version
    assert page(case, after) == page(case, before)
