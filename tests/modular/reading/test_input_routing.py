"""Real changed inputs, rather than the business family, select independent models."""

from copy import deepcopy
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError, PageQuery, QuerySelection
from ai_accounting_modular.ledger.public import AccountBalanceKey
from ai_accounting_modular.money.public import stable_occurrence_id
from ai_accounting_modular.reading import _monthly
from ai_accounting_modular.reading.public import MonthScope
from tests.modular.application._working_case import case as case
from tests.modular.domains.financing._support import BANK, COUNTERPARTY, payment
from tests.modular.domains.people.test_people import case as people_case
from tests.modular.domains.people.test_people import no_contributions
from tests.modular.domains.people.test_read_remuneration import add
from tests.modular.domains.people.test_relationships import relationship

from .test_funds import apply, funding, initialize
from .test_funds_integrity import original_row, pair


def selected(case, page, month="2026-01"):
    return case.kernel.reading.page(page, MonthScope(case.company, month), {})


def ready(case, page, month="2026-01"):
    value = selected(case, page, month)
    if value.state == "preparing":
        case.kernel.reading.run_pending(case.company)
        value = selected(case, page, month)
    assert value.state == "ready", value
    return value


def correct(case, fact, *, seq=1):
    result = case.app.accounting_apply(
        case.request(
            mutations=[{"op": "replace", "expected_seq": seq, "fact": fact}],
        )
    )
    assert result["completion"] == "complete", result


def paid_expense(case):
    initialize(case)
    expense = case.expense("100000")
    expense["data"]["creditor_id"] = COUNTERPARTY
    paid = payment(expense, 50000, party=COUNTERPARTY, slot="primary", day="2026-01-10")
    _, expense, paid = apply(case, funding("200000"), expense, paid)
    return expense, paid


def test_paid_source_meaning_and_sparse_display_refresh_funds_without_other_owner_reads(
    case, monkeypatch
):
    expense, paid = paid_expense(case)
    before = {name: ready(case, name) for name in ("funds", "employees", "assets")}
    old = before["funds"].payload["data"]
    assert old["bank_fen"] == "150000"
    changed = deepcopy(expense)
    changed["data"]["expense_class"] = "bank_fee"

    def unexpected(*args, **kwargs):
        raise AssertionError("a write notification recomputed an owner projection")

    with monkeypatch.context() as patch:
        for owner, method in (
            (case.kernel.money, "account_activity"),
            (case.kernel.reading.investment, "money_fund_activity"),
            (case.kernel.reading.people, "remuneration_activity"),
            (case.kernel.reading.assets, "asset_activity"),
        ):
            patch.setattr(owner, method, unexpected)
        correct(case, changed)
        assert selected(case, "funds").state == "preparing"
        assert selected(case, "employees") == before["employees"]
        assert selected(case, "assets") == before["assets"]
    current = ready(case, "funds")
    assert current.payload["data"]["bank_fen"] == "150000"
    assert current.payload["data"]["collections"]["movements"]["items"][1]["list_summary"] == (
        "支付银行手续费"
    )
    with case.kernel.storage.read_unit(case.company) as read:
        party = case.kernel.identity.get_batch([COUNTERPARTY], read)[0]
    obj = {key: party[key] for key in ("object_id", "kind", "name", "data")}
    obj.update(name="实际已付供应商的新名称", expected_seq=party["seq"])
    apply(case, objects=[obj])
    assert selected(case, "funds").state == "preparing"
    assert selected(case, "employees") == before["employees"]
    assert selected(case, "assets") == before["assets"]
    renamed = ready(case, "funds")
    assert any(
        row["subject_id"] == paid["business_id"] and row["party"] == obj["name"]
        for row in renamed.payload["data"]["collections"]["movements"]["items"]
    )
    apply(
        case,
        objects=[
            {
                "object_id": str(uuid4()),
                "kind": "organization",
                "name": "未引用的新对象",
                "data": {},
                "expected_seq": 0,
            }
        ],
    )
    assert selected(case, "funds") == renamed


def test_earlier_real_payment_changes_quiet_later_account_stock(case):
    _, paid = paid_expense(case)
    first = ready(case, "funds", "2026-02")
    assert first.payload["data"]["movement_count"] == 0
    assert first.payload["data"]["opening_fen"] == "150000"
    changed = deepcopy(paid)
    changed["data"]["amount_fen"] = "60000"
    changed["data"]["allocations"][0]["amount_fen"] = "60000"
    correct(case, changed)
    assert selected(case, "funds", "2026-02").state == "preparing"
    actual = ready(case, "funds", "2026-02").payload["data"]
    assert actual["movement_count"] == 0
    assert (actual["opening_fen"], actual["total_fen"]) == ("140000", "140000")


def test_empty_opening_is_a_real_completeness_change_without_journal_lines(case):
    initialize(case, opening=False)
    before = {name: ready(case, name) for name in ("funds", "employees")}
    assert before["funds"].payload["data"]["total_fen"] is None
    assert selected(case, "assets").state == "preparing"
    case.kernel.reading.run_pending(case.company)
    # The frozen asset-page contract requires known balances. Its owner model
    # is ready with an honest unknown, and can still subscribe to its missing base.
    with pytest.raises(ContractError) as unknown:
        selected(case, "assets")
    assert unknown.value.code == "needs_information"
    opening = {
        "business_id": str(uuid4()),
        "kind": "opening.package",
        "period": "2026-01",
        "evidence_ids": [case.source_id],
        "data": {"basis": "newly_established", "members": []},
    }
    apply(case, opening)
    for name in ("funds", "employees", "assets"):
        assert selected(case, name).state == "preparing"
    assert ready(case, "funds").payload["data"]["total_fen"] == "0"
    with case.kernel.storage.read_unit(case.company) as read:
        summary = case.kernel.ledger.financial_summary(
            _monthly.normalize(
                {
                    "company_id": case.company,
                    "period_from": "2026-01",
                    "period_to": "2026-01",
                    "view": "as_posted",
                }
            ),
            read,
        )
        assert summary.bank == {"state": "known", "amount_fen": "0"}
        assert (
            case.kernel.ledger.query_voucher_directory(
                {
                    "company_id": case.company,
                    "period_from": "2026-01",
                    "period_to": "2026-01",
                    "view": "as_posted",
                },
                (),
                PageQuery(20),
                read,
            ).total_count
            == 0
        )


def test_current_relationship_uses_effective_history_not_later_fact_filing_month(case):
    initialize(case)
    actual = ready(case, "employees")
    person_id = str(uuid4())
    apply(
        case,
        objects=[
            {
                "object_id": person_id,
                "kind": "person",
                "name": "真实一月入职人",
                "data": {},
                "expected_seq": 0,
            }
        ],
    )
    assert selected(case, "employees") == actual
    fact = relationship(("regular", "2026-01-01"), person=person_id, period="2026-03")
    apply(case, fact)
    assert selected(case, "employees").state == "preparing"
    employees = ready(case, "employees").payload["data"]["collections"]["employees"]["items"]
    assert any(item["employee_id"] == person_id for item in employees)


def test_closed_correction_keeps_historical_pages_and_updates_only_real_current_followup(case):
    expense = case.seed()
    ready(case, "brief")
    checked = case.check()
    assert checked["ready"], checked
    assert case.close(checked)["state"] == "closed"
    historical = {name: ready(case, name) for name in ("funds", "employees", "assets")}
    before = ready(case, "brief")
    changed = deepcopy(expense)
    changed["data"]["amount_fen"] = "90000"
    result = case.app.accounting_apply(
        case.request(
            correction_period="2026-02",
            mutations=[{"op": "replace", "expected_seq": 1, "fact": changed}],
        )
    )
    assert result["completion"] == "complete", result
    for name, value in historical.items():
        assert selected(case, name) == value
    assert selected(case, "brief").state == "preparing"
    current = ready(case, "brief")
    assert current.version != before.version
    query = _monthly.normalize(
        {
            "company_id": case.company,
            "period_from": "2026-01",
            "period_to": "2026-01",
            "view": "as_posted",
        }
    )
    with case.kernel.storage.read_unit(case.company) as read:
        snapshot = _monthly.ready_snapshot(
            case.kernel.reading,
            query,
            ("brief_open_items",),
            read,
        )
        from ai_accounting_modular.reading._collections import CollectionFilter

        item = _monthly.collection(
            snapshot,
            "brief_open_items",
            "open_members",
            CollectionFilter(),
            PageQuery(20),
            read,
        ).items[0]
        assert (item["outstanding_fen"], item["current_outstanding_fen"]) == ("100000", "90000")


def test_previous_projection_format_requires_explicit_retry_without_replaying_business(case):
    initialize(case)
    actual = ready(case, "funds")
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        unit.repository("reading")._session.execute(
            "UPDATE read_scope_head SET format_version=2 WHERE component_key='funds'"
        )
    with case.kernel.storage.read_unit(case.company) as read:
        head = read.head
    assert selected(case, "funds").state == "failed"
    case.kernel.reading.retry("funds", MonthScope(case.company, "2026-01"), {})
    rebuilt = ready(case, "funds")
    assert rebuilt.payload == actual.payload
    with case.kernel.storage.read_unit(case.company) as read:
        assert read.head == head
        assert read.repository("reading").scopes()[0]["format_version"] == _monthly.FORMAT_VERSION


def test_closed_withdrawal_marks_discovery_stock_without_rewriting_frozen_month(case, monkeypatch):
    expense = case.seed()
    assert case.close(case.check())["state"] == "closed"
    ready(case, "brief")
    ready(case, "brief", "2026-02")
    seen, original = [], case.kernel.ledger.adopt

    def adopt(*args, **kwargs):
        result = original(*args, **kwargs)
        seen.extend(result.formal_changes)
        return result

    monkeypatch.setattr(case.kernel.ledger, "adopt", adopt)
    result = case.app.accounting_apply(
        case.request(
            correction_period="2026-02",
            explanation="原管理费用属于误记，实际没有发生。",
            mutations=[
                {"op": "withdraw", "expected_seq": 1, "business_id": expense["business_id"]}
            ],
        )
    )
    assert result["completion"] == "complete", result
    assert len(seen) == 1
    assert seen[0].posted_periods == ("2026-02",)
    assert set(seen[0].result_kinds) == {"operations.expense", "system.withdrawal"}
    assert "2202" in seen[0].account_codes
    assert selected(case, "brief", "2026-02").state == "preparing"
    with case.kernel.storage.read_unit(case.company) as read:
        positions = [
            case.kernel.ledger.account_balances(
                (AccountBalanceKey("2202"),),
                month,
                read,
                selection=QuerySelection("as_posted", month),
            )[0]
            for month in ("2026-01", "2026-02")
        ]
        assert positions[0].ending["amount_fen"] == "-100000"
        assert positions[1].ending["amount_fen"] == "0"


def test_bank_business_month_and_activity_posting_month_do_not_refresh_other_month_flows(case):
    initialize(case)
    first, second = funding("100"), funding("200", "2026-01-02")
    rows = [
        original_row(case, str(uuid4()), value, day, "inflow")
        for value, day in (
            ("100", "2026-01-02"),
            ("200", "2026-01-02"),
        )
    ]
    _, _, statement, check = apply(case, first, second, *pair(BANK, rows, [first, second]))
    january, february = ready(case, "funds"), ready(case, "funds", "2026-02")
    changed = deepcopy(check)
    changed["data"]["matches"] = [
        {
            "statement_row_ids": [row["row_id"] for row in rows],
            "fund_movement_ids": [
                stable_occurrence_id(item["business_id"], "funds") for item in (first, second)
            ],
        }
    ]
    correct(case, changed)
    assert selected(case, "funds").state == "preparing"
    assert selected(case, "funds", "2026-02") == february
    assert ready(case, "funds").version != january.version
    assert february.payload["data"]["movement_count"] == 0
    assert february.payload["data"]["bank_fen"] == "300"

    expense = apply(case, case.expense("1000"))[0]
    ready(case, "brief", "2026-02")
    key = _monthly.key(
        _monthly.normalize(
            {
                "company_id": case.company,
                "period_from": "2026-02",
                "period_to": "2026-02",
                "view": "as_posted",
            }
        )
    )
    with case.kernel.storage.read_unit(case.company) as read:
        previous = read.repository("reading").state(key, "brief_activity")
    expense["data"]["amount_fen"] = "900"
    correct(case, expense)
    with case.kernel.storage.read_unit(case.company) as read:
        assert read.repository("reading").state(key, "brief_activity") == previous
        assert read.repository("reading").state(key, "financial")["state"] == "preparing"


def test_quiet_month_roster_keeps_its_actual_formal_source_for_later_settlement_dependencies(case):
    case.seed(expense=False)
    wage = people_case("B17Z")
    wage["data"]["gross_fen"] = "0"
    add(case, no_contributions(), wage)
    scope = _monthly.normalize(
        {
            "company_id": case.company,
            "period_from": "2026-02",
            "period_to": "2026-02",
            "view": "as_posted",
        }
    )
    with case.kernel.storage.read_unit(case.company) as read:
        activity = case.kernel.reading.people.remuneration_activity(scope, read)
    assert activity.entries == activity.settlements == activity.positions == ()
    assert activity.roster and activity.sources == ()
    assert wage["business_id"] in activity.inputs.business_ids
