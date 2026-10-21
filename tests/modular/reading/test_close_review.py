"""The month review is a read-only, complete projection of the ready brief."""

from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import (
    ContractError,
    canonical_json,
    content_digest,
    loads_json,
    validate_contract,
)
from ai_accounting_modular.reading import _brief, _close_review, _monthly
from ai_accounting_modular.reading.public import MonthScope
from tests.modular.application._working_case import case as case
from tests.modular.domains.financing._support import BANK, fact

from ._support import source
from .test_brief_activity import mixed
from .test_brief_http import ready
from .test_funds import apply, initialize


def review(case, month="2026-01", **query):
    response = case.kernel.reading.detail("close_review", MonthScope(case.company, month), query)
    validate_contract("dashboard-response-v2.json", "dashboard_close_review", response)
    return response


def snapshot(case):
    with case.kernel.storage.read_unit(case.company) as read:
        return _monthly.ready_snapshot(
            case.kernel.reading,
            _monthly.normalize(
                {
                    "company_id": case.company,
                    "period_from": "2026-01",
                    "period_to": "2026-01",
                    "view": "as_posted",
                }
            ),
            _brief.COMPONENTS,
            read,
        )


def test_complete_review_is_same_brief_version_not_first_20_and_never_checks_or_writes_accounting(
    case, monkeypatch
):
    case.seed()
    objects, facts = [], []
    for index in range(24):
        party = str(uuid4())
        objects.append(
            {
                "object_id": party,
                "kind": "organization",
                "name": "实际供应商" + str(index),
                "expected_seq": 0,
                "data": {},
            }
        )
        expense = case.expense("100")
        expense["data"]["creditor_id"] = party
        facts.append(expense)
    saved = case.app.accounting_apply(case.request(*facts, objects=objects))
    assert saved["completion"] == "complete", saved
    brief = ready(case)
    assert brief["data"]["collections"]["activity"]["page"]["returned_count"] == 20
    assert brief["data"]["activity_count"] == 25
    digest = brief["data"]["owner_review_request"]["preview_digest"]
    assert digest != brief["snapshot_version"]
    assert "review_businesses" not in brief["data"]
    with case.kernel.storage.read_unit(case.company) as read:
        before = read.head, read.repository("reading").scopes()
    contexts, read_unit = [], case.kernel.storage.read_unit

    def read_once(*args, **kwargs):
        contexts.append(args[0])
        return read_unit(*args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("核对不能写入、检查关账许可或重复取整月owner/凭证正文")

    monkeypatch.setattr(case.kernel.storage, "read_unit", read_once)
    monkeypatch.setattr(case.kernel.storage, "maintenance_unit", forbidden)
    monkeypatch.setattr(case.app, "accounting_period", forbidden)
    monkeypatch.setattr(case.kernel.ledger, "financial_summary", forbidden)
    monkeypatch.setattr(case.kernel.ledger, "get_vouchers", forbidden)
    monkeypatch.setattr(case.kernel.money, "account_activity", forbidden)
    response = review(case, preview_digest=digest)
    assert contexts == [case.company]
    assert response["state"] == "prepared" and response["preview_digest"] == digest
    content = response["owner_review"]
    assert content["amounts"] == {
        "month_revenue_fen": "0",
        "month_expense_fen": "102400",
        "month_result_fen": "-102400",
        "funds_total_fen": "0",
        "actual_receipts_fen": "0",
        "actual_payments_fen": "0",
    }
    assert content["businesses"] == [
        {
            "label": "管理费用",
            "action": "business",
            "reversal": False,
            "count": 25,
            "amount_label": "业务金额",
            "business_amount_fen": "102400",
        }
    ]
    assert brief["data"]["financial_position"]["issues"]
    assert content["status"] == "ai_reviewing" and content["followup_count"] == 0
    assert content["materials"] == []
    with read_unit(case.company) as read:
        assert (read.head, read.repository("reading").scopes()) == before


def test_missing_models_and_unknown_required_amounts_are_unprepared_without_registration(case):
    case.seed()
    assert review(case)["state"] == "unprepared"
    with case.kernel.storage.read_unit(case.company) as read:
        assert read.repository("reading").scopes() == ()
    ready(case)
    saved = snapshot(case)
    components = deepcopy(saved.components)
    components["financial"]["financial"]["revenue"] = {"state": "unknown"}
    unknown = replace(saved, components=components)
    assert _close_review.review(unknown) is None
    assert _close_review.request(unknown) is None


def test_unknown_opening_keeps_nullable_funds_and_actual_issues_without_fabricating_material_counts(
    case,
):
    expense = case.expense()
    saved = case.app.accounting_apply(
        case.request(
            expense,
            sources=[source("review-no-opening") | {"source_id": case.source_id}],
            objects=[
                {
                    "object_id": case.party,
                    "kind": "organization",
                    "name": "实际供应商",
                    "expected_seq": 0,
                    "data": {},
                }
            ],
        )
    )
    assert saved["completion"] == "complete", saved
    brief = ready(case)
    assert brief["data"]["owner_review_request"] is not None
    response = review(case)
    assert response["state"] == "prepared"
    content = response["owner_review"]
    assert content["amounts"]["funds_total_fen"] is None
    assert content["amounts"]["month_expense_fen"] == "100000"
    assert content["status"] == "ai_reviewing" and content["followup_count"] == 0
    assert content["materials"] == []


def test_mixed_payment_preserves_purpose_labels_but_actual_payment_is_counted_once(case):
    mixed(case)
    ready(case)
    content = review(case)["owner_review"]
    assert content["amounts"]["month_expense_fen"] == "700"
    assert content["amounts"]["actual_payments_fen"] == "1000"
    paid = {
        item["label"]: (item["count"], item["business_amount_fen"])
        for item in content["businesses"]
        if item["amount_label"] == "实际付款"
    }
    assert paid == {"支付货币基金申购": (1, "300"), "支付管理费用": (1, "700")}


def test_correction_summaries_keep_signed_original_and_replacement_without_new_cash(case):
    original = case.seed()
    assert case.close(case.check())["state"] == "closed"
    corrected = deepcopy(original)
    corrected["data"]["amount_fen"] = "80000"
    saved = case.app.accounting_apply(
        case.request(
            mutations=[{"op": "replace", "expected_seq": 1, "fact": corrected}],
            correction_period="2026-02",
        )
    )
    assert saved["completion"] == "complete", saved
    ready(case, "2026-02")
    content = review(case, "2026-02")["owner_review"]
    assert content["amounts"]["month_expense_fen"] == "-20000"
    assert content["amounts"]["actual_payments_fen"] == "0"
    assert {
        (item["action"], item["reversal"], item["business_amount_fen"])
        for item in content["businesses"]
    } == {("correction", True, "-100000"), ("correction", False, "80000")}


def test_current_issue_ids_count_once_and_financial_problems_stay_ai_reviewing(case):
    case.seed()
    issue_id = str(uuid4())
    issue = {
        "issue_id": issue_id,
        "scope": "period:2026-01",
        "period": "2026-01",
        "evidence_ids": [],
        "missing_fields": [],
        "question": "请核对真实业务用途。",
        "assignee": "owner",
        "action": "clarify_reality",
    }
    saved = case.app.accounting_apply(
        case.request(
            unresolved=[issue],
            unresolved_scope=issue["scope"],
            expected_scope_head=0,
        )
    )
    assert saved["completion"] == "complete", saved
    ready(case)
    actual = review(case)["owner_review"]
    assert actual["status"] == "ai_reviewing" and actual["followup_count"] == 1
    saved = snapshot(case)
    components = deepcopy(saved.components)
    components["brief_context"]["risks"].append({"key": issue_id})
    assert _close_review.review(replace(saved, components=components))["followup_count"] == 1
    components["brief_context"].update(risks=[], owner_tasks=[])
    components["financial"]["financial"]["issues"] = []
    assert _close_review.review(replace(saved, components=components))["status"] == "ready"
    components["financial"]["financial"]["issues"] = [{"message": "真实账务仍需核查。"}]
    actual = _close_review.review(replace(saved, components=components))
    assert actual["status"] == "ai_reviewing" and actual["followup_count"] == 0


def test_real_bank_originals_awaiting_reconciliation_keep_ai_reviewing_without_fake_issue(case):
    initialize(case)
    statement = fact(
        "money.bank_statement",
        {"funds_account_id": BANK, "opening_fen": "0", "closing_fen": "0", "rows": []},
    )
    apply(case, statement)
    ready(case)
    saved = snapshot(case)
    assert saved.components["funds"]["data"]["attention_account_count"] == 1
    actual = review(case)["owner_review"]
    assert actual["status"] == "ai_reviewing" and actual["followup_count"] == 0
    assert actual["materials"] == []
    apply(
        case,
        fact(
            "money.reconciliation",
            {"statement_id": statement["business_id"], "matches": []},
        ),
    )
    ready(case)
    assert snapshot(case).components["funds"]["data"]["attention_account_count"] == 0
    actual = review(case)["owner_review"]
    assert actual["status"] == "ready" and actual["followup_count"] == 0


def test_related_changes_require_ready_brief_then_reject_old_digest_and_unrelated_changes_keep_it(
    case,
):
    original = case.seed()
    initial = ready(case)
    old = initial["data"]["owner_review_request"]["preview_digest"]
    changed = deepcopy(original)
    changed["data"]["amount_fen"] = "120000"
    saved = case.app.accounting_apply(
        case.request(
            mutations=[{"op": "replace", "expected_seq": 1, "fact": changed}],
        )
    )
    assert saved["completion"] == "complete", saved
    assert review(case, preview_digest=old)["state"] == "unprepared"
    current = ready(case)
    digest = current["data"]["owner_review_request"]["preview_digest"]
    assert digest != old
    stale = review(case, preview_digest=old)
    assert stale["state"] == "stale" and stale["preview_digest"] == old
    assert stale["owner_review"] is None
    saved = case.app.accounting_apply(
        case.request(
            objects=[
                {
                    "object_id": str(uuid4()),
                    "kind": "organization",
                    "name": "未被使用的档案",
                    "expected_seq": 0,
                    "data": {},
                }
            ]
        )
    )
    assert saved["completion"] == "complete", saved
    case.kernel.reading.run_pending(case.company)
    assert review(case, preview_digest=digest)["state"] == "prepared"
    assert review(case)["preview_digest"] == digest


@pytest.mark.parametrize("boundary,state", [("2026-01", "closed"), ("2026-02", "covered")])
def test_real_closed_or_covered_header_needs_no_prepared_review(case, boundary, state):
    case.seed()
    if boundary == "2026-02":
        saved = case.app.accounting_apply(case.request(case.expense("1", boundary)))
        assert saved["completion"] == "complete", saved
    assert case.close(case.check(boundary))["state"] == "closed"
    expected = "a" * 64
    with case.kernel.storage.read_unit(case.company) as read:
        closure = case.kernel.ledger.period_closure("2026-01", read)
        assert read.repository("reading").scopes() == ()
    response = review(case, preview_digest=expected)
    assert response["state"] == state and response["owner_review"] is None
    if state == "closed":
        assert response["preview_digest"] == expected
        assert response["close_digest"] == closure.head
        assert response["covered_by"] is None
    else:
        assert response["preview_digest"] is response["close_digest"] is None
        assert response["covered_by"] == {"period": boundary, "digest": closure.head}


@pytest.mark.parametrize("query", [{"section": "activity"}, {"preview_digest": "bad"}])
def test_review_query_has_no_approval_or_pagination_inputs(case, query):
    with pytest.raises(ContractError) as error:
        review(case, **query)
    assert error.value.code == "invalid_input"


def test_old_month_projection_is_not_read_or_repaired_until_explicit_retry(case, monkeypatch):
    case.seed()
    ready(case)
    saved = snapshot(case)
    with case.kernel.storage.read_unit(case.company) as read:
        model = _monthly.capture(case.kernel.reading, _monthly.BRIEF_ACTIVITY, saved.scope, read)
    old = loads_json(model.payload_json)
    del old["review_businesses"]
    old_model = replace(model, payload_json=canonical_json(old), content_digest=content_digest(old))
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        repository = unit.repository("reading")
        repository._session.execute(
            "UPDATE read_scope_head SET format_version=1 WHERE scope_key=?",
            (saved.scope_key,),
        )
        assert repository.publish(saved.scope_key, _monthly.BRIEF_ACTIVITY, unit.head, 1, old_model)
        before = unit.head, repository.scopes()

    def no_repair(*args, **kwargs):
        raise AssertionError("只读核对不得登记、标记失败或重建旧投影")

    with monkeypatch.context() as patch:
        patch.setattr(case.kernel.storage, "maintenance_unit", no_repair)
        assert review(case)["state"] == "unprepared"
    with case.kernel.storage.read_unit(case.company) as read:
        assert (read.head, read.repository("reading").scopes()) == before
    scope = MonthScope(case.company, "2026-01")
    failed = case.kernel.reading.page("brief", scope, {})
    assert (failed.state, failed.reason) == ("failed", "corrupt")
    assert case.kernel.reading.run_pending(case.company) == ()
    case.kernel.reading.retry("brief", scope)
    assert case.kernel.reading.run_pending(case.company)
    current = case.kernel.reading.page("brief", scope, {})
    assert current.state == "ready"
    assert review(case)["state"] == "prepared"
    with case.kernel.storage.read_unit(case.company) as read:
        assert read.head == before[0]
        assert {state["format_version"] for state in read.repository("reading").scopes()} == {2}
