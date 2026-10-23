"""Static prerequisite, per-request isolation and failure races on real writes."""

from copy import deepcopy
from hashlib import sha256

from ai_accounting_modular.contracts.public import canonical_json, loads_json
from ai_accounting_modular.reading import _financial_projection as projection
from ai_accounting_modular.reading import _monthly
from ai_accounting_modular.reading._financial_store import CONTROL, OWNER_PREFIX
from ai_accounting_modular.reading.public import MonthScope

from ._financial_projection_case import (
    drain,
    page,
    quarter,
    scope,
)
from ._financial_projection_case import (
    financial_case as financial_case,
)


def test_cold_month_and_quarter_have_one_explicit_producer_before_consumers(financial_case):
    case, owner = financial_case
    case.seed()
    assert page(case).state == quarter(case).state == "preparing"
    completed = case.kernel.reading.run_pending(case.company, limit=1)
    with case.kernel.storage.read_unit(case.company) as read:
        assert completed == (projection.key(read),)
        assert loads_json(completed[0])["model"] == "ledger_financial_projection"
    assert page(case).state == "preparing"
    drain(case)
    assert page(case).payload["financial"]["expense"]["amount_fen"] == "100000"
    assert quarter(case).state == "ready"
    assert len(owner.prepared) == 1 and len(owner.captured) == 2


def test_unrelated_future_posting_keeps_old_ready_artifact_and_later_fills_new_scope(
    financial_case,
):
    case, owner = financial_case
    case.seed()
    page(case)
    drain(case)
    before = page(case)
    result = case.app.accounting_apply(case.request(case.expense("70000", "2026-07")))
    assert result["completion"] == "complete"
    with case.kernel.storage.read_unit(case.company) as read:
        private = projection.key(read)
        assert projection.store(read).work()["pending"] == []
    assert case.kernel.reading.run_pending(case.company) == (private,)
    assert page(case) == before and len(owner.prepared) == 2
    page(case, "2026-07")
    drain(case)
    assert page(case, "2026-07").payload["financial"]["expense"]["amount_fen"] == "70000"


def test_failed_dependency_does_not_fail_ready_a_or_starve_independent_work(
    financial_case, monkeypatch
):
    case, owner = financial_case
    case.seed()
    page(case, "2026-02")
    drain(case)
    before = page(case, "2026-02")
    page(case)

    def unavailable(*args, **kwargs):
        raise OSError("controlled producer failure")

    monkeypatch.setattr(case.kernel.ledger, "prepare_financial_projection", unavailable)
    assert case.kernel.reading.run_pending(case.company, limit=1) == ()
    assert page(case).state == "failed" and page(case).reason == "production_failed"
    assert page(case, "2026-02") == before
    reader = case.kernel.reading
    assert reader.page("funds", MonthScope(case.company, "2026-02"), {}).state == "preparing"
    assert reader.run_pending(case.company, limit=1)
    assert reader.page("funds", MonthScope(case.company, "2026-02"), {}).state == "ready"
    monkeypatch.setattr(case.kernel.ledger, "prepare_financial_projection", owner.prepare)
    _monthly.retry(reader, scope(case), ("financial",))
    drain(case)
    assert page(case).state == "ready" and page(case, "2026-02") == before


def test_old_producer_failure_cannot_fail_new_same_head_request(financial_case, monkeypatch):
    case, owner = financial_case
    case.seed()
    page(case)

    def moved(*args, **kwargs):
        page(case, "2026-02")
        raise OSError("old work failed")

    monkeypatch.setattr(case.kernel.ledger, "prepare_financial_projection", moved)
    assert case.kernel.reading.run_pending(case.company) == ()
    assert page(case).state == page(case, "2026-02").state == "preparing"
    monkeypatch.setattr(case.kernel.ledger, "prepare_financial_projection", owner.prepare)
    drain(case)
    assert page(case).state == page(case, "2026-02").state == "ready"


def test_old_consumer_failure_cannot_fail_a_new_same_head_descriptor(financial_case, monkeypatch):
    case, owner = financial_case
    case.seed()
    page(case)
    drain(case)
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        projection.store(unit).mark_consumer(_monthly.key(scope(case)), unit.head)

    def moved(selected, read, base):
        result = owner.capture(selected, read, base)
        with case.kernel.storage.maintenance_unit(case.company) as unit:
            target = projection.store(unit)
            body = loads_json(target.get_blocks((selected.descriptor_key,))[0])
            body["proof"] = "same-head repaired descriptor"
            target._session.execute(
                "UPDATE read_item SET payload_json=? WHERE scope_key=? "
                "AND collection_key=? AND item_id=?",
                (
                    canonical_json(body),
                    projection.key(unit),
                    OWNER_PREFIX + selected.descriptor_key[0],
                    selected.descriptor_key[1],
                ),
            )
        assert result.request_key == selected.key
        raise OSError("old consumer failed after a newer descriptor")

    monkeypatch.setattr(case.kernel.ledger, "read_financial_projection", moved)
    assert case.kernel.reading.run_pending(case.company) == ()
    assert page(case).state == "preparing"
    monkeypatch.setattr(case.kernel.ledger, "read_financial_projection", owner.capture)
    drain(case)
    assert page(case).state == "ready"


def test_missing_coverage_is_preparation_and_not_a_failed_money_result(financial_case, monkeypatch):
    case, owner = financial_case
    case.seed()
    page(case)
    assert projection.produce(case.kernel.reading, case.company)
    monkeypatch.setattr(case.kernel.ledger, "read_financial_projection", lambda *args: None)
    assert case.kernel.reading.run_pending(case.company) == ()
    assert page(case).state == "preparing"
    monkeypatch.setattr(case.kernel.ledger, "read_financial_projection", owner.capture)
    drain(case)
    assert page(case).state == "ready"


def test_corrupt_pending_does_not_block_real_business_and_retry_rebuilds(financial_case):
    case, _ = financial_case
    case.seed()
    page(case)
    drain(case)
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        unit.repository("reading")._session.execute(
            "UPDATE read_item SET payload_json='{}' WHERE scope_key=? AND collection_key=?",
            (projection.key(unit), CONTROL),
        )
    actual = case.app.accounting_apply(case.request(case.expense("40000")))
    assert actual["completion"] == "complete"
    assert page(case).state == "failed" and page(case).reason == "corrupt"
    _monthly.retry(case.kernel.reading, scope(case), ("financial",))
    drain(case)
    assert page(case).payload["financial"]["expense"]["amount_fen"] == "140000"


def test_multiple_closed_corrections_merge_signals_without_losing_formal_legs(financial_case):
    case, owner = financial_case
    original = case.seed()
    page(case)
    quarter(case)
    drain(case)
    assert case.close(case.check())["state"] == "closed"
    changed = deepcopy(original)
    changed["data"]["amount_fen"] = "120000"
    result = case.app.accounting_apply(
        case.request(
            mutations=[{"op": "replace", "expected_seq": 1, "fact": changed}],
            correction_period="2026-02",
        )
    )
    assert result["completion"] == "complete", result
    assert case.close(case.check("2026-02"))["state"] == "closed"
    changed["data"]["amount_fen"] = "150000"
    result = case.app.accounting_apply(
        case.request(
            mutations=[{"op": "replace", "expected_seq": 2, "fact": changed}],
            correction_period="2026-03",
        )
    )
    assert result["completion"] == "complete", result
    page(case, "2026-02")
    page(case, "2026-03")
    with case.kernel.storage.read_unit(case.company) as read:
        work = projection.store(read).work()
        changes = [
            value for value in work["changes"] if value["business_id"] == original["business_id"]
        ]
        assert len(changes) == 1
        assert {"2026-02", "2026-03"} <= set(changes[0]["posted_periods"])
    drain(case)
    assert page(case).payload["financial"]["expense"]["amount_fen"] == "100000"
    assert page(case, "2026-02").payload["financial"]["expense"]["amount_fen"] == "20000"
    assert page(case, "2026-03").payload["financial"]["expense"]["amount_fen"] == "30000"
    assert owner.prepared[-1][1]


def test_known_current_expense_is_not_blocked_or_zeroed_by_unknown_opening(financial_case):
    case, _ = financial_case
    text = "合成费用1000元未付款；没有期初确认资料。"
    result = case.app.accounting_apply(
        case.request(
            case.expense(),
            objects=[
                {
                    "object_id": case.party,
                    "kind": "organization",
                    "name": "合成供应商",
                    "expected_seq": 0,
                    "data": {},
                }
            ],
            sources=[
                {
                    "source_id": case.source_id,
                    "kind": "owner_statement",
                    "text": text,
                    "sha256": sha256(text.encode()).hexdigest(),
                    "archive_month": "2026-01",
                }
            ],
        )
    )
    assert result["completion"] == "complete", result
    page(case)
    quarter(case)
    drain(case)
    value = page(case).payload["financial"]
    assert value["expense"] == {"state": "known", "amount_fen": "100000"}
    assert value["bank"]["state"] == "unestablished" and "amount_fen" not in value["bank"]
    assert quarter(case).state == "ready"


def test_only_financial_formats_upgrade_and_explicit_retry_keeps_other_models(financial_case):
    case, _ = financial_case
    case.seed()
    reader = case.kernel.reading
    page(case)
    quarter(case)
    assert reader.page("funds", MonthScope(case.company, "2026-01"), {}).state == "preparing"
    drain(case)
    before = reader.page("funds", MonthScope(case.company, "2026-01"), {})
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        unit.repository("reading")._session.execute(
            "UPDATE read_scope_head SET format_version=CASE WHEN scope_key=? THEN 7 ELSE 2 END "
            "WHERE component_key='financial'",
            (_monthly.key(scope(case)),),
        )
    assert page(case).state == "preparing"
    assert quarter(case).state == "failed"
    assert reader.page("funds", MonthScope(case.company, "2026-01"), {}) == before
    _monthly.retry(reader, scope(case), ("financial",))
    reader.retry(
        "quarterly_report",
        {
            "company_id": case.company,
            "period_from": "2026-01",
            "period_to": "2026-03",
            "view": "as_posted",
        },
    )
    drain(case)
    assert page(case).state == quarter(case).state == "ready"
    assert reader.page("funds", MonthScope(case.company, "2026-01"), {}) == before
