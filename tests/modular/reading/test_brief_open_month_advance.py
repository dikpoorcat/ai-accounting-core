"""A later working month updates live progress without reloading unchanged claims."""

from uuid import uuid4

import pytest

from ai_accounting_modular.reading import _brief_open_sources, _monthly
from ai_accounting_modular.reading._collections import collection_items
from tests.modular.application._working_case import case as case
from tests.modular.domains.people.test_read_remuneration import bank_object

from .test_brief_open import apply, payment, publish
from .test_brief_open_incremental import matches_cold, scope, snapshot


@pytest.mark.parametrize("closed", [False, True])
def test_first_later_month_payment_updates_only_its_claim(case, monkeypatch, closed):
    first = case.seed()
    second = case.expense("200000")
    bank = bank_object()
    apply(case, second, objects=[bank])
    if closed:
        assert case.close(case.check())["state"] == "closed"
    publish(case)
    apply(case, payment(case, first["business_id"], bank["object_id"], "60000", "2026-02"))
    capture, seen = _brief_open_sources.capture, []

    def bounded(reader, selected, read, *, business_ids=None, **options):
        assert business_ids is not None, "advancing the month is not a full claim refresh"
        seen.extend(business_ids)
        return capture(reader, selected, read, business_ids=business_ids, **options)

    with monkeypatch.context() as patch:
        patch.setattr(_brief_open_sources, "capture", bounded)
        assert case.kernel.reading.run_pending(case.company)
    assert first["business_id"] in seen and second["business_id"] not in seen
    summary = snapshot(case).components[_monthly.BRIEF_OPEN_ITEMS]["open_items"]
    assert summary["payable_fen"] == "300000"
    assert summary["current_cutoff_period"] == "2026-02"
    with case.kernel.storage.read_unit(case.company) as read:
        value = collection_items(
            _monthly.key(scope(case)),
            _monthly.BRIEF_OPEN_ITEMS,
            "open_members",
            (first["business_id"] + ":primary",),
            read,
        )[0]
    assert value["outstanding_fen"] == "100000"
    assert value["current_outstanding_fen"] == "40000"
    matches_cold(case)


def test_unrelated_later_month_cooperation_does_not_reload_old_month_claims(case, monkeypatch):
    original = case.seed()
    publish(case)
    operator, project = str(uuid4()), str(uuid4())
    agreement = {
        "business_id": str(uuid4()),
        "kind": "investment.cooperation_contract",
        "period": "2026-02",
        "evidence_ids": [case.source_id],
        "data": {
            "profile": "direct_joint_operation_cash_labor_v1",
            "project_id": project,
            "operator_id": operator,
            "company_initial_commitment_fen": "100",
            "company_revenue_expense_share": "0.5",
            "contract_source": {"source_id": case.source_id},
        },
    }
    apply(
        case,
        agreement,
        objects=[
            {
                "object_id": operator,
                "kind": "person",
                "name": "合成经营者",
                "expected_seq": 0,
                "data": {},
            },
            {
                "object_id": project,
                "kind": "investment_product",
                "name": "合成后月合作",
                "expected_seq": 0,
                "data": {"category": "cooperation"},
            },
        ],
    )
    capture, seen = _brief_open_sources.capture, []

    def bounded(reader, selected, read, *, business_ids=None, **options):
        assert business_ids is not None, "an unrelated future contract cannot invalidate old claims"
        seen.extend(business_ids)
        return capture(reader, selected, read, business_ids=business_ids, **options)

    with monkeypatch.context() as patch:
        patch.setattr(_brief_open_sources, "capture", bounded)
        assert case.kernel.reading.run_pending(case.company)
    assert original["business_id"] not in seen
    summary = snapshot(case).components[_monthly.BRIEF_OPEN_ITEMS]["open_items"]
    assert summary["payable_fen"] == "100000"
    assert summary["current_cutoff_period"] == "2026-02"
    matches_cold(case)


def test_data_only_later_month_advances_cutoff_without_reloading_unchanged_claims(
    case, monkeypatch
):
    original = case.seed()
    bank = bank_object()
    apply(case, objects=[bank])
    publish(case)
    statement = {
        "business_id": str(uuid4()),
        "kind": "money.bank_statement",
        "period": "2026-02",
        "evidence_ids": [case.source_id],
        "data": {
            "funds_account_id": bank["object_id"],
            "opening_fen": "0",
            "closing_fen": "0",
            "rows": [],
        },
    }
    apply(case, statement)
    capture, seen = _brief_open_sources.capture, []

    def bounded(reader, selected, read, *, business_ids=None, **options):
        assert business_ids is not None
        seen.extend(business_ids)
        return capture(reader, selected, read, business_ids=business_ids, **options)

    with monkeypatch.context() as patch:
        patch.setattr(_brief_open_sources, "capture", bounded)
        assert case.kernel.reading.run_pending(case.company)
    assert original["business_id"] not in seen
    summary = snapshot(case).components[_monthly.BRIEF_OPEN_ITEMS]["open_items"]
    assert summary["payable_fen"] == "100000"
    assert summary["current_cutoff_period"] == "2026-02"
    matches_cold(case)
