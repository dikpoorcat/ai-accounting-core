"""Certified group pages and exact locators preserve bounded native business output."""

import builtins

import pytest
from test_banking import book as book  # noqa: F401
from test_banking import close_month, inventories
from test_dashboard_model_transactions import comparable, raw_read, source_body_work
from test_dashboard_open_groups import expenses, snapshot
from test_dashboard_open_reviewed_vouchers import _review
from test_integrity_content import damage
from test_opening_continuation import book as opening_book_fixture
from test_payroll_corrections import company as company_fixture

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_model_materializer import build_model
from ai_accounting.kernel.maintenance import Maintenance
from ai_accounting.kernel.response_contracts import validate_response

company = company_fixture
opening_book = opening_book_fixture


def _forbid_native_grouping(monkeypatch):
    from ai_accounting.kernel import dashboard_brief_groups, dashboard_open_groups

    def forbidden(*args, **kwargs):
        raise AssertionError("certified member reads must not rebuild complete native groups")

    monkeypatch.setattr(dashboard_brief_groups, "activity_groups", forbidden)
    monkeypatch.setattr(dashboard_open_groups, "_data", forbidden)


@pytest.mark.parametrize("section", ["activity", "open_items"])
def test_twenty_member_pages_match_native_without_regrouping(book, monkeypatch, section):
    engine = book[0]
    expenses(book, 23)
    expenses(book, 3, party="other-supplier", prefix="other")
    dashboard = Dashboard(engine)
    with raw_read():
        overview = dashboard.brief("2026-09")
        group = next(
            item
            for item in overview["data"]["collections"][section]["items"]
            if item["member_count"] == 23
        )
        first = dashboard.brief_group("2026-09", section=section, group_key=group["group_key"])
        tail = dashboard.brief_group(
            "2026-09",
            section=section,
            group_key=group["group_key"],
            cursor=first["data"]["collections"]["members"]["page"]["next_cursor"],
            expected_version=first["snapshot_version"],
        )
    Maintenance(engine).rebuild_dashboard_read_model("2026-09", request_id="groups-model")
    _forbid_native_grouping(monkeypatch)
    for expected, cursor in [
        (first, None),
        (tail, first["data"]["collections"]["members"]["page"]["next_cursor"]),
    ]:
        with source_body_work(monkeypatch) as work:
            actual = dashboard.brief_group(
                "2026-09",
                section=section,
                group_key=group["group_key"],
                cursor=cursor,
                expected_version=first["snapshot_version"],
            )
        assert actual == expected
        validate_response("dashboard_brief_group", actual)
        shown = {item["subject_id"] for item in actual["data"]["collections"]["members"]["items"]}
        with engine.store.connection(read_only=True) as connection:
            sources = {
                row["id"]: row["subject_id"]
                for row in connection.execute(
                    "SELECT id,subject_id FROM calculation",
                )
            }
        assert {sources[ident] for ident in work["outcomes"]} <= shown
        assert not work["lineages"]
    assert len(first["data"]["collections"]["members"]["items"]) == 20
    assert len(tail["data"]["collections"]["members"]["items"]) == 3


def test_producer_builds_complete_open_grouping_once_for_all_groups(book, monkeypatch):
    from ai_accounting.kernel import dashboard_open_groups

    expenses(book, 3)
    expenses(book, 2, party="other-supplier", prefix="other")
    original, calls = dashboard_open_groups._record_data, []

    def record(snap):
        calls.append(snap)
        return original(snap)

    monkeypatch.setattr(dashboard_open_groups, "_record_data", record)
    model = build_model(Dashboard(book[0]), "2026-09", block_size=2)
    assert len(calls) == 1
    blocks = [block for block in model.blocks if block.kind == "dashboard_brief_group"]
    assert blocks
    for block in blocks:
        validate_response("dashboard_brief_group", block.payload)
        section = block.payload["data"]["section"]
        field = "key" if section == "activity" else "id"
        items = block.payload["data"]["collections"]["members"]["items"]
        assert block.keys == tuple(
            (item[field], item["voucher_version_id"] or "") for item in items
        )
        assert set(block.sources) == {
            source for bindings in block.item_sources for source in bindings
        }
        assert set(block.voucher_version_ids) == {
            ident for versions in block.item_voucher_version_ids for ident in versions
        }


@pytest.mark.parametrize("locator", ["number", "version"])
def test_focused_voucher_uses_directory_without_complete_native_brief(book, monkeypatch, locator):
    from ai_accounting.kernel import dashboard_read_model

    engine = book[0]
    expenses(book, 5)
    dashboard = Dashboard(engine)
    with raw_read():
        voucher = dashboard.brief("2026-09", section="vouchers")["data"]["collections"]["vouchers"][
            "items"
        ][2]
        options = (
            {"voucher_number": int(voucher["number"])}
            if locator == "number"
            else {
                "voucher_version_id": voucher["voucher_version_id"],
            }
        )
        expected = dashboard.brief("2026-09", **options)
    Maintenance(engine).rebuild_dashboard_read_model("2026-09", request_id="focused-model")
    _forbid_native_grouping(monkeypatch)
    root_reads = []
    original_load = dashboard_read_model._load_root

    def load_root(*args, **kwargs):
        root_reads.append(True)
        return original_load(*args, **kwargs)

    monkeypatch.setattr(dashboard_read_model, "_load_root", load_root)
    with source_body_work(monkeypatch) as work:
        actual = dashboard.brief("2026-09", **options)
    assert comparable(actual) == comparable(expected)
    validate_response("dashboard_brief", actual)
    assert actual["data"]["focused_voucher"]["voucher_version_id"] == voucher["voucher_version_id"]
    assert len(set(work["outcomes"])) == 1
    assert not work["lineages"]
    assert len(root_reads) == 1  # Focus and overview share this request's root proof.
    assert comparable(dashboard.brief("2026-09", **options)) == comparable(expected)
    assert len(root_reads) == 2  # A new request independently authenticates it again.


@pytest.mark.parametrize("closed", [False, True])
def test_cross_month_open_group_keeps_exact_reviewed_voucher_and_frozen_amount(
    book, monkeypatch, closed
):
    engine, _, _, proof = book
    _, original, source = _review(book)
    if closed:
        close_month(inventories(engine, proof, "2026-09", {"transactions"}), proof, "2026-09")
    expenses(book, 1, period="2026-10", prefix="later")
    dashboard = Dashboard(engine)
    with raw_read():
        group = dashboard.brief("2026-10")["data"]["collections"]["open_items"]["items"][0]
        expected = dashboard.brief_group(
            "2026-10", section="open_items", group_key=group["group_key"]
        )
    Maintenance(engine).rebuild_dashboard_read_model("2026-10", request_id="historical-group-model")
    _forbid_native_grouping(monkeypatch)
    actual = dashboard.brief_group("2026-10", section="open_items", group_key=group["group_key"])
    assert actual == expected
    member = next(
        item
        for item in actual["data"]["collections"]["members"]["items"]
        if item["subject_id"] == "expense-000"
    )
    assert member["voucher_version_id"] == original["voucher_version_id"]
    assert member["source_amount_fen"] == member["outstanding_fen"] == 100
    damage(
        engine,
        "calculation",
        "UPDATE calculation SET outcome=json_set(outcome,'$.values.amount_fen',101) WHERE id=?",
        (source,),
    )
    with pytest.raises(KernelError) as failure:
        dashboard.brief_group("2026-10", section="open_items", group_key=group["group_key"])
    assert failure.value.code == "content_integrity_failed"


def test_group_cursor_and_exact_locator_fail_without_native_fallback(book, monkeypatch):
    expenses(book, 3)
    dashboard = Dashboard(book[0])
    Maintenance(book[0]).rebuild_dashboard_read_model("2026-09", request_id="group-identity-model")
    overview = dashboard.brief("2026-09")
    group = overview["data"]["collections"]["open_items"]["items"][0]
    first = dashboard.brief_group(
        "2026-09", section="open_items", group_key=group["group_key"], limit=1
    )
    _forbid_native_grouping(monkeypatch)
    with pytest.raises(KernelError) as failure:
        dashboard.brief_group(
            "2026-09",
            section="activity",
            group_key=group["group_key"],
            cursor=first["data"]["collections"]["members"]["page"]["next_cursor"],
        )
    assert failure.value.code == "dashboard_snapshot_changed"
    with pytest.raises(KernelError) as failure:
        dashboard.brief("2026-09", voucher_number=999)
    assert failure.value.code == "dashboard_voucher_not_found"


def test_closed_member_keeps_frozen_amount_when_current_correction_is_different(book, monkeypatch):
    engine, save, _, proof = book
    key, original, _ = _review(book)
    close_month(inventories(engine, proof, "2026-09", {"transactions"}), proof, "2026-09")
    save(
        "expense",
        "expense-000",
        {
            "period": "2026-09",
            "counterparty_id": "supplier",
            "amount_fen": 200,
            "expense_class": "administration",
            "creditor_kind": "supplier",
        },
        revision=2,
    )
    preview = engine.preview(["expense-000"], posting_period="2026-10")
    engine.confirm(
        ["expense-000"],
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        posting_period="2026-10",
        request_id="later-expense-correction",
    )
    dashboard = Dashboard(engine)
    with raw_read():
        expected = dashboard.brief_group("2026-09", section="open_items", group_key=key)
    Maintenance(engine).rebuild_dashboard_read_model(
        "2026-09", request_id="closed-correction-model"
    )
    _forbid_native_grouping(monkeypatch)
    actual = dashboard.brief_group("2026-09", section="open_items", group_key=key)
    assert actual == expected
    member = actual["data"]["collections"]["members"]["items"][0]
    assert member["voucher_version_id"] == original["voucher_version_id"]
    assert member["source_amount_fen"] == member["outstanding_fen"] == 100
    assert member["current_outstanding_fen"] == 200
    assert actual["data"]["collections"]["vouchers"]["items"][0]["amount_fen"] == 100


def test_social_personal_and_employer_members_keep_deduplicated_cross_month_vouchers(
    company, monkeypatch
):
    company.publish("january", "february")
    dashboard = Dashboard(company.engine)
    with raw_read():
        overview = dashboard.brief("2026-02")
        group = next(
            item
            for item in overview["data"]["collections"]["open_items"]["items"]
            if item["description"] == "社保"
        )
        expected = dashboard.brief_group(
            "2026-02", section="open_items", group_key=group["group_key"]
        )
    Maintenance(company.engine).rebuild_dashboard_read_model(
        "2026-02", request_id="contribution-group-model"
    )
    _forbid_native_grouping(monkeypatch)
    actual = dashboard.brief_group("2026-02", section="open_items", group_key=group["group_key"])
    assert actual == expected
    members = actual["data"]["collections"]["members"]["items"]
    vouchers = actual["data"]["collections"]["vouchers"]["items"]
    assert len(members) == 4
    assert len(vouchers) == 2
    assert {item["source_period"] for item in members} == {"2026-01", "2026-02"}
    assert {item["voucher_version_id"] for item in members} == {
        item["voucher_version_id"] for item in vouchers
    }


def test_native_member_continuations_reuse_sorted_order_without_repeating_group_work(
    book, monkeypatch
):
    from ai_accounting.kernel import dashboard_open_groups

    expenses(book, 5)
    with snapshot(book[0], "2026-09") as snap:
        group = dashboard_open_groups.open_group_page(snap)["collection"]["items"][0]
        sorted_member_counts = []

        def counted(values, *args, **kwargs):
            values = list(values)
            if values and all(
                hasattr(value, "get") and value.get("source_calculation_id") for value in values
            ):
                sorted_member_counts.append(len(values))
            return builtins.sorted(values, *args, **kwargs)

        monkeypatch.setattr(dashboard_open_groups, "sorted", counted, raising=False)
        first = dashboard_open_groups.open_group_members(snap, group["group_key"], limit=2)
        second = dashboard_open_groups.open_group_members(
            snap,
            group["group_key"],
            after=first["page"]["next_cursor"],
            limit=2,
        )
        third = dashboard_open_groups.open_group_members(
            snap,
            group["group_key"],
            after=second["page"]["next_cursor"],
            limit=2,
        )
        complete = dashboard_open_groups.open_group_members(snap, group["group_key"], limit=20)
        assert first["items"] + second["items"] + third["items"] == complete["items"]
        assert [item["subject_id"] for item in complete["items"]] == [
            f"expense-{index:03}" for index in range(5)
        ]
        assert sorted_member_counts == [5]


def test_opening_state_without_activity_has_no_formal_voucher_to_locate(opening_book, monkeypatch):
    engine, _, _, package, _ = opening_book
    package(
        [
            ("opening_bank", "bank-opening", {"bank_account_id": "bank", "balance_fen": 100}),
            (
                "opening_equity",
                "equity",
                {
                    "equity_kind": "paid_in_capital",
                    "balance_fen": 100,
                    "holder_or_basis_id": "owner",
                },
            ),
        ]
    )
    dashboard = Dashboard(engine)
    with raw_read():
        expected = dashboard.brief("2026-01")
        voucher_page = dashboard.brief("2026-01", section="vouchers")
        assert voucher_page["data"]["collections"]["vouchers"]["items"] == []
        with pytest.raises(KernelError) as missing:
            dashboard.brief("2026-01", voucher_number=1)
        assert missing.value.code == "dashboard_voucher_not_found"
    assert expected["data"]["activity_count"] == expected["data"]["voucher_count"] == 0
    assert expected["data"]["funds_overview"]["bank_fen"] == 100
    Maintenance(engine).rebuild_dashboard_read_model("2026-01", request_id="opening-focus-model")
    _forbid_native_grouping(monkeypatch)
    actual = dashboard.brief("2026-01")
    assert comparable(actual) == comparable(expected)
    with pytest.raises(KernelError) as missing:
        dashboard.brief("2026-01", voucher_number=1)
    assert missing.value.code == "dashboard_voucher_not_found"


def test_focused_mixed_payment_keeps_full_voucher_and_no_single_activity(book, monkeypatch):
    from test_dashboard_activity_classification import _allocation, _expense
    from test_dashboard_activity_components import _bank_batch

    engine, save, publish, _ = book
    _expense(save, publish, "supplier-cost", "supplier", 7000, "supplier")
    _expense(save, publish, "employee-cost", "alice", 10000, "employee")
    _bank_batch(
        book,
        "mixed",
        [
            _allocation("expense", "supplier-cost", 7000, recipient="supplier"),
            _allocation("expense", "employee-cost", 10000, recipient="alice"),
        ],
    )
    dashboard = Dashboard(engine)
    with raw_read():
        expected = dashboard.brief("2026-09", voucher_number=3)
    assert expected["data"]["focused_activity"] is None
    assert expected["data"]["focused_activity_group"] is None
    Maintenance(engine).rebuild_dashboard_read_model("2026-09", request_id="mixed-focus-model")
    _forbid_native_grouping(monkeypatch)
    actual = dashboard.brief("2026-09", voucher_number=3)
    assert comparable(actual) == comparable(expected)
    voucher = actual["data"]["focused_voucher"]
    assert voucher["subject_id"] == "mixed"
    assert voucher["business_amount_fen"] == voucher["amount_fen"] == 17000
    assert sum(line["debit_fen"] for line in voucher["lines"]) == 17000
    assert sum(line["credit_fen"] for line in voucher["lines"]) == 17000
