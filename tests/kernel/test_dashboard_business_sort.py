"""Business ordering is global while voucher-number paging stays independent."""

import base64
import json
from types import SimpleNamespace

import pytest
from entity_fixture import seed_entities
from test_banking import book as _bank_book
from test_dashboard_provenance import profile
from test_payroll_corrections import Company

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_pages import decode_cursor, seal_page
from ai_accounting.kernel.dashboard_sort import business_sort_metadata, date_object_key, open_item_order
from ai_accounting.kernel.dashboard_reads import Journal
from ai_accounting.kernel.domains.transactions import Expense

bank_book = _bank_book


def _read_all(dashboard, endpoint, response, section, **filters):
    result = list(response["data"]["collections"][section]["items"])
    page = response["data"]["collections"][section]["page"]
    while page["has_more"]:
        response = getattr(dashboard, endpoint)(
            "2026-09", section=("activity" if endpoint == "brief_group" else section), cursor=page["next_cursor"], limit=20,
            expected_version=response["snapshot_version"], **filters,
        )
        result.extend(response["data"]["collections"][section]["items"])
        page = response["data"]["collections"][section]["page"]
    return result


def test_business_date_object_order_crosses_pages_and_vouchers_have_separate_stream(bank_book, monkeypatch):
    engine, _, _, proof = bank_book
    seed_entities(engine, [("bank-a", "fund_account", "bank"),
                           ("owner-a", "person", None), ("owner-b", "person", None)])
    profile(engine, "counterparty", "owner-a", display_name="乙")
    profile(engine, "counterparty", "owner-b", display_name="甲")
    facts = [{"kind": "funding", "subject_id": f"fund-{i:03}", "expected_revision": 0,
              "evidence": [proof], "data": {
                  "period": "2026-09", "owner_id": "owner-a" if i % 2 == 0 else "owner-b",
                  "amount_fen": i + 1, "funding_kind": "capital", "bank_account_id": "bank-a",
                  "actual_date": f"2026-09-{25 - i // 2:02}",
              }} for i in range(44)]
    engine.save_facts(facts, request_id="sort-facts")
    subjects = [fact["subject_id"] for fact in facts]
    preview = engine.preview(subjects)
    engine.confirm(subjects, preview_digest=preview["digest"], epochs=preview["epochs"],
                   request_id="sort-publish")
    dashboard = Dashboard(engine)
    hydrated = []
    original_hydrate = Journal.hydrate
    def hydrate(self, rows, **options):
        hydrated.append(len(rows))
        return original_hydrate(self, rows, **options)
    monkeypatch.setattr(Journal, "hydrate", hydrate)
    first = dashboard.brief("2026-09")
    groups = _read_all(dashboard, "brief", first, "activity")
    assert len(groups) == 2 and sum(row["member_count"] for row in groups) == 44
    assert "vouchers" not in first["data"]["collections"]
    first_members = dashboard.brief_group("2026-09", section="activity", group_key=groups[0]["group_key"])
    activity = _read_all(dashboard, "brief_group", first_members, "members",
                         group_key=groups[0]["group_key"])
    assert len(activity) == 22
    assert [(row["date"], row["party"]) for row in activity] == sorted(
        (row["date"], row["party"]) for row in activity
    )
    preview_cards = first_members["data"]["collections"]["vouchers"]["items"]
    assert [row["voucher_version_id"] for row in preview_cards] == [
        row["voucher_version_id"] for row in activity[:20]
    ]
    assert [int(row["number"]) for row in preview_cards] != list(range(1, 21))
    independent = dashboard.brief("2026-09", section="vouchers")
    numbered = _read_all(dashboard, "brief", independent, "vouchers")
    assert [int(row["number"]) for row in numbered] == list(range(1, 45))
    paired_cursor = first_members["data"]["collections"]["members"]["page"]["next_cursor"]
    assert json.loads(base64.urlsafe_b64decode(paired_cursor))["sort"] == "business-date-object/2"
    assert json.loads(base64.urlsafe_b64decode(
        independent["data"]["collections"]["vouchers"]["page"]["next_cursor"]
    ))["sort"] == "voucher-number/1"
    following = dashboard.brief_group("2026-09", section="activity", group_key=groups[0]["group_key"],
                                     cursor=paired_cursor, expected_version=first["snapshot_version"])
    assert [row["voucher_version_id"] for row in following["data"]["collections"]["vouchers"]["items"]] == [
        row["voucher_version_id"] for row in activity[20:40]
    ]
    funds = dashboard.funds("2026-09", movement_account_type="bank", movement_account_id="bank-a")
    movements = _read_all(dashboard, "funds", funds, "movements",
                          movement_account_type="bank", movement_account_id="bank-a")
    assert [(row["date"], row["party"]) for row in movements] == sorted(
        (row["date"], row["party"]) for row in movements
    )
    assert hydrated and max(hydrated) == 20


@pytest.mark.parametrize("closed", [False, True])
def test_open_obligations_order_by_complete_object_before_page_in_open_and_frozen(tmp_path, closed, monkeypatch):
    company = Company(tmp_path / "ordered.sqlite")
    # Reverse identity/registration order makes a key-ordered page visibly wrong.
    for i in range(23):
        party = f"party-{i:02}"
        company.save(Expense(period="2026-09", counterparty_id=party, amount_fen=i + 1,
                             expense_class="administration", creditor_kind="supplier"), f"expense-{i:02}")
        profile(company.engine, "counterparty", party, display_name=f"对象{22-i:02}")
    company.publish(*(f"expense-{i:02}" for i in range(23)))
    if closed:
        company.close("2026-09")
        import ai_accounting.kernel.settlement_freeze as freeze
        state_batches, source_batches = [], []
        original_states, original_sources = freeze._read_states, freeze._verified_page_sources
        def states(connection, period, digests, **options):
            state_batches.append(len(digests))
            return original_states(connection, period, digests, **options)
        def sources(connection, period, rows):
            source_batches.append(len(rows))
            return original_sources(connection, period, rows)
        monkeypatch.setattr(freeze, "_read_states", states)
        monkeypatch.setattr(freeze, "_verified_page_sources", sources)
    dashboard = Dashboard(company.engine)
    first = dashboard.brief("2026-09", section="open_items")
    rows = _read_all(dashboard, "brief", first, "open_items")
    assert [row["party"] for row in rows] == [f"对象{i:02}" for i in range(23)]
    assert sum(row["outstanding_fen"] for row in rows) == sum(range(1, 24))
    if closed:
        assert not state_batches
        # Complete group membership needs the exact source proof for all 23
        # narrow states; no full state payload is hydrated for root summaries.
        assert source_batches and max(source_batches) == 23


def test_month_confirmation_follows_actual_days_without_making_up_a_date():
    metadata = lambda date: {"date": date, "party": "甲", "identities": ("person",)}
    actual = date_object_key(metadata("2026-09-30"), "2026-09", "actual")
    monthly = date_object_key(metadata(None), "2026-09", "monthly")
    later = date_object_key(metadata("2026-10-01"), "2026-10", "later")
    missing = date_object_key(metadata(None), None, "missing")
    assert actual < monthly < later < missing


def test_open_sort_uses_formal_identity_group_and_matter_without_hidden_month(monkeypatch):
    import ai_accounting.kernel.dashboard_sort as sorting
    rows, scalars = [], {}
    # Six same-named employees and four components each: a real 20-row page
    # splits one group; the group retains one stable anchor through continuation.
    for person in range(6):
        employee = f"employee-{person:02}"
        for month in ("2026-01", "2026-05") if person == 0 else ("2026-03",):
            fact = f"fact-{employee}-{month}"
            scalars[fact] = {"employee_id": employee, "period": month}
            for component in ("employee_social", "employer_social", "employee_housing", "employer_housing", "net", "tax"):
                rows.append({"obligation_key": f"{fact}:{component}", "component": component,
                             "source_kind": "payroll", "source_fact_id": fact,
                             "counterparty_id": "tax-authority"})
    monkeypatch.setattr(sorting, "fact_sort_scalars", lambda *_: scalars)
    snapshot = SimpleNamespace(store=SimpleNamespace(company_id="company"),
                               metadata=SimpleNamespace(prime_profiles=lambda *_: None),
                               party=lambda party: "同名员工",
                               profiles={kind: {f"employee-{person:02}": {"display_name": "同名员工"}
                                                for person in range(6)}
                                         for kind in ("employee", "counterparty")},
                               current_profiles={kind: {f"employee-{person:02}": {"display_name": "同名员工"}
                                                        for person in range(6)}
                                                 for kind in ("employee", "counterparty")})
    ordered = open_item_order(snapshot, reversed(rows))
    by_key = {row["obligation_key"]: row for row in rows}
    group_positions = {}
    for index, key in enumerate(ordered):
        row = by_key[key]
        if row["component"] in sorting.CONTRIBUTIONS:
            group_positions.setdefault(row["source_fact_id"], []).append(index)
    assert all(indices == list(range(indices[0], indices[0] + 4)) for indices in group_positions.values())
    assert any(indices[0] < 20 <= indices[-1] for indices in group_positions.values())
    # The stable formal group identity in this fixture puts May before January;
    # ordering by the undisplayed month would instead reverse these two groups.
    assert group_positions["fact-employee-00-2026-05"][0] < group_positions["fact-employee-00-2026-01"][0]
    matters = ["社保与公积金" if by_key[key]["component"] in sorting.CONTRIBUTIONS
               else sorting.PAYROLL_MATTERS[by_key[key]["component"]] for key in ordered]
    assert matters == sorted(matters)
    social_employees = [scalars[by_key[key]["source_fact_id"]]["employee_id"] for key in ordered
                        if by_key[key]["component"] in sorting.CONTRIBUTIONS]
    assert social_employees == sorted(social_employees)


@pytest.mark.parametrize("kind,component,data,settlement_party,expected", [
    ("payroll", "net", {"employee_id": "employee"}, "recipient", ("employee", "实发工资")),
    ("payroll_bounded", "tax", {"employee_id": "employee"}, "authority",
     ("employee", "代扣个人所得税")),
    ("payroll", "employee_social", {"employee_id": "employee"}, "authority",
     ("employee", "个人社保")),
    ("payroll", "employer_housing", {"employee_id": "employee"}, "authority",
     ("employee", "单位公积金")),
    ("opening_payroll_payable", "net", {"employee_id": "employee"}, "recipient",
     ("employee", "实发工资")),
    ("annual_bonus", "net", {"employee_id": "employee"}, "recipient", ("employee", "实发奖金")),
    ("annual_bonus", "tax", {"employee_id": "employee"}, "authority", ("employee", "奖金代扣个税")),
    ("payroll", "net", {}, "recipient", ("recipient", "实发工资")),
    ("payroll", "tax", {"employee_id": None}, "authority", ("authority", "代扣个人所得税")),
    ("expense", "primary", {}, "supplier", ("supplier", "费用")),
    ("labor_project_cost", "net", {}, "person", ("person", "劳务报酬")),
    ("pass_through", "remittance", {}, None, (None, "代收代付")),
])
def test_open_sort_display_matches_existing_selected_page(
    kind, component, data, settlement_party, expected,
):
    from ai_accounting.kernel.dashboard import _open_item_display

    party, missing, matter = _open_item_display(kind, component, data, settlement_party)
    assert (party, matter) == expected
    assert missing == ("最终收款人未具名" if kind == "pass_through" else "未提供")


def test_open_sort_uses_adopted_opening_component_and_bonus_matter(monkeypatch):
    import ai_accounting.kernel.dashboard_sort as sorting

    scalars = {
        "opening": {"employee_id": "employee", "component": "net", "payroll_period": "2026-08"},
        "bonus": {"employee_id": "employee", "period": "2026-09"},
    }
    monkeypatch.setattr(sorting, "fact_sort_scalars", lambda *_: scalars)
    rows = [
        {"obligation_key": "opening", "source_kind": "opening_payroll_payable",
         "source_fact_id": "opening", "component": "primary", "counterparty_id": "recipient"},
        {"obligation_key": "bonus-tax", "source_kind": "annual_bonus", "source_fact_id": "bonus",
         "component": "tax", "counterparty_id": "authority"},
        {"obligation_key": "bonus-net", "source_kind": "annual_bonus", "source_fact_id": "bonus",
         "component": "net", "counterparty_id": "recipient"},
    ]
    primed = []
    snapshot = SimpleNamespace(
        store=SimpleNamespace(company_id="company"),
        metadata=SimpleNamespace(
            prime_profiles=lambda kind, parties: primed.append((kind, set(parties))),
        ),
        party=lambda party: {"employee": "员工", "authority": "税局", "recipient": "收款人"}[party],
        profiles={kind: {"employee": {"display_name": "员工"}}
                  for kind in ("employee", "counterparty")},
        current_profiles={kind: {"employee": {"display_name": "员工"}}
                          for kind in ("employee", "counterparty")},
    )
    assert open_item_order(snapshot, rows) == ["bonus-tax", "bonus-net", "opening"]
    assert primed == [("employee", {"employee"}), ("counterparty", {"employee"})]


def test_multiple_formal_objects_have_the_same_complete_names_in_sort_and_rows(bank_book):
    engine, save, publish, _ = bank_book
    seed_entities(engine, [("party-a", "person", None), ("party-b", "person", None)])
    profile(engine, "counterparty", "party-a", display_name="同名")
    profile(engine, "counterparty", "party-b", display_name="同名")
    save("pass_through", "multi", {"period": "2026-09", "payer_id": "party-a",
          "beneficiary_id": "party-b", "amount_fen": 100, "rights_and_obligation_confirmed": True})
    publish("multi")
    response = Dashboard(engine).brief("2026-09")
    activity = response["data"]["collections"]["activity"]["items"][0]
    assert activity["party"] == "同名、同名"
    assert activity["date_from"] is None and activity["has_month_recognition"]
    with Dashboard(engine)._snapshot("2026-09") as snapshot:
        headers = list(snapshot.month_journal.verified_rows() or snapshot.connection.execute(*snapshot.month_journal.sql()))
        identifier = headers[0]["basis_calculation_id"]
        metadata = business_sort_metadata(snapshot, [identifier])[identifier]
        assert metadata["party"] == activity["party"]
        assert metadata["identities"] == ("party-a", "party-b")


def test_sort_objects_match_typed_individual_batch_and_advanced_debt(bank_book):
    engine, save, publish, _ = bank_book
    for party in ("party-a", "party-b", "party-c"):
        save("expense", "expense-" + party, {
            "period": "2026-09", "counterparty_id": party, "amount_fen": 1000,
            "expense_class": "administration", "creditor_kind": "supplier",
        })
        profile(engine, "counterparty", party, display_name=party)
    publish("expense-party-a", "expense-party-b", "expense-party-c")
    save("payment", "individual", {
        "period": "2026-09", "actual_date": "2026-09-01", "direction": "outflow",
        "bank_account_id": "bank-a", "counterparty_id": "party-a", "amount_fen": 100,
        "allocations": [{"source_kind": "expense", "source_id": "expense-party-a",
                         "obligation": "primary", "amount_fen": 100}],
    })
    publish("individual")
    save("payment", "batch", {
        "period": "2026-09", "actual_date": "2026-09-02", "direction": "outflow",
        "bank_account_id": "bank-a", "counterparty_id": None,
        "payment_method": "bank_batch", "amount_fen": 100,
        "allocations": [{"source_kind": "expense", "source_id": "expense-" + party,
                         "obligation": "primary", "amount_fen": 50, "recipient_id": party}
                        for party in ("party-a", "party-b")],
    })
    publish("batch")
    save("employee_advance", "advanced", {
        "period": "2026-09", "payer_id": "owner", "payer_kind": "owner",
        "payment_on_behalf_confirmed": True, "actual_creditor_payment_date": "2026-09-03",
        "sources": [{"source_kind": "expense", "source_id": "expense-party-c",
                     "obligation": "primary", "amount_fen": 50}],
    })
    profile(engine, "counterparty", "owner", display_name="owner")
    publish("advanced")
    dashboard = Dashboard(engine)
    from test_dashboard_activity_classification import _loaded_members

    activity = _loaded_members(engine, dashboard.brief("2026-09")["data"])
    displayed = {row["subject_id"]: row for row in activity}
    assert displayed["individual"]["party"] == "party-a"
    assert displayed["batch"]["party"] == "party-a、party-b"
    assert displayed["advanced"]["party"] == "owner、party-c"
    with dashboard._snapshot("2026-09") as snapshot:
        rows = list(snapshot.month_journal.verified_rows() or snapshot.connection.execute(
            *snapshot.month_journal.sql()
        ))
        metadata = business_sort_metadata(snapshot, {row["basis_calculation_id"] for row in rows})
        for row in rows:
            selected = snapshot.calculation(row["basis_calculation_id"])
            assert metadata[selected["id"]]["party"] == displayed[selected["subject_id"]]["party"]
    movements = dashboard.funds("2026-09")["data"]["collections"]["movements"]["items"]
    assert [(row["subject_id"], row["date"], row["party"], row["amount_fen"])
            for row in movements] == [
        ("individual", "2026-09-01", "party-a", 100),
        ("batch", "2026-09-02", "party-a、party-b", 100),
    ]


@pytest.mark.parametrize("payload", [[], None, "text", {"key": 1, "scope": "old"}])
def test_old_or_malformed_cursor_is_structured_rejection(payload):
    snapshot = SimpleNamespace(store=SimpleNamespace(company_id="c", database_id="d"),
                               period="2026-09", snapshot_version="v", as_of=None)
    cursor = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    with pytest.raises(KernelError) as rejected:
        decode_cursor(snapshot, "brief", "activity", cursor, {})
    assert rejected.value.code == "dashboard_snapshot_changed"
    number = seal_page(snapshot, "brief", "vouchers", {"next_cursor": 1}, {})["next_cursor"]
    with pytest.raises(KernelError):
        decode_cursor(snapshot, "brief", "vouchers", number, {}, sort_profile="business-paired-date/1")
