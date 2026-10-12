"""Complete filter counts, independent dimensions and bounded raw-hit reads."""

from types import SimpleNamespace

import pytest
from test_banking import book as book  # noqa: F401
from test_banking import entry, statement
from test_dashboard_funds_alignment import _publish_filter_funding
from test_dashboard_model_transactions import raw_read, source_body_work
from test_payroll_corrections import company as company  # noqa: F401

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard, EmployeeFilter
from ai_accounting.kernel.dashboard_model_filters import (
    decorate_keys,
    directory_matches,
    read_filtered_collection,
    read_filtered_data,
)
from ai_accounting.kernel.maintenance import Maintenance
from ai_accounting.kernel.response_contracts import validate_response


@pytest.fixture
def snap():
    return SimpleNamespace(
        store=SimpleNamespace(company_id="company", database_id="database"),
        period="2026-09",
        snapshot_version="snapshot",
        as_of="2026-10-11",
    )


class Model:
    def __init__(self, page, section, rows, *, variant="default", block_size=2):
        self.root = SimpleNamespace(blocks=[])
        self.blocks, self.loaded = {}, []
        for ordinal, start in enumerate(range(0, len(rows), block_size)):
            values = rows[start : start + block_size]
            key = f"{page}/{variant}/{section}/{ordinal}"
            keys = decorate_keys(page, section, [row["seek"] for row in values], values)
            self.root.blocks.append(
                SimpleNamespace(
                    key=key,
                    keys=keys,
                    total_count=len(rows),
                    filtered_count=len(rows),
                    item_count=len(values),
                )
            )
            self.blocks[key] = SimpleNamespace(
                payload={"data": {"collections": {section: {"items": values}}}},
                item_sources=tuple(
                    ((row["seek"], "calc-digest", "fact", "fact-digest"),) for row in values
                ),
                item_fact_sources=tuple(() for _ in values),
                item_voucher_version_ids=tuple(() for _ in values),
            )

    def read_page_block(self, key):
        self.loaded.append(key)
        return self.blocks[key]

    def collection_references(self, page, variant, section):
        return tuple(
            ref for ref in self.root.blocks if ref.key.startswith(f"{page}/{variant}/{section}/")
        )


def capture_hits(monkeypatch):
    from ai_accounting.kernel import dashboard_model_reads

    hits = []
    monkeypatch.setattr(
        dashboard_model_reads,
        "_verify_hits",
        lambda _snap, sources, facts, vouchers: hits.append(set(sources)),
    )
    return hits


def test_later_account_sparse_pages_keep_full_counts_and_hit_sources(snap, monkeypatch):
    rows = [
        {
            "seek": f"movement-{position}",
            "account_type": "bank",
            "account_id": account,
            "amount_fen": position + 100,
        }
        for position, account in enumerate(("first", "later", "first", "later", "first"))
    ]
    model = Model("funds", "movements", rows, variant="all")
    hits = capture_hits(monkeypatch)
    filters = {
        "movement_account_type": "bank",
        "movement_account_id": "later",
        "statement_account_id": "different-statement-account",
    }
    first = read_filtered_collection(model, snap, "funds", "movements", filters=filters, limit=1)
    assert first["items"] == [rows[1]]
    assert first["page"]["total_count"] == 5
    assert first["page"]["filtered_count"] == 2
    assert first["page"]["has_more"]
    assert model.loaded == ["funds/all/movements/0"]
    assert {source[0] for source in hits[0]} == {"movement-1"}
    model.loaded.clear()
    tail = read_filtered_collection(
        model,
        snap,
        "funds",
        "movements",
        filters=filters,
        limit=1,
        cursor=first["page"]["next_cursor"],
    )
    assert tail["items"] == [rows[3]]
    assert not tail["page"]["has_more"]
    assert model.loaded == ["funds/all/movements/1"]
    assert {source[0] for source in hits[1]} == {"movement-3"}
    with pytest.raises(KernelError, match="筛选"):
        read_filtered_collection(
            model,
            snap,
            "funds",
            "movements",
            limit=1,
            filters={**filters, "statement_account_id": None},
            cursor=first["page"]["next_cursor"],
        )


def test_larger_continuation_keeps_order_counts_and_exact_hit_sources(snap, monkeypatch):
    rows = [
        {
            "seek": f"movement-{position:04d}",
            "account_type": "bank",
            "account_id": "wanted" if position % 2 == 0 else "unrelated",
            "amount_fen": position + 100,
        }
        for position in range(1102)
    ]
    expected = rows[::2]
    model = Model("funds", "movements", rows, variant="all", block_size=20)
    hits = capture_hits(monkeypatch)
    filters = {"movement_account_type": "bank", "movement_account_id": "wanted"}
    first = read_filtered_collection(model, snap, "funds", "movements", filters=filters)
    continuation = read_filtered_collection(
        model,
        snap,
        "funds",
        "movements",
        filters=filters,
        cursor=first["page"]["next_cursor"],
        limit=500,
    )
    tail = read_filtered_collection(
        model,
        snap,
        "funds",
        "movements",
        filters=filters,
        cursor=continuation["page"]["next_cursor"],
        limit=500,
    )
    assert [len(page["items"]) for page in (first, continuation, tail)] == [20, 500, 31]
    assert [item for page in (first, continuation, tail) for item in page["items"]] == expected
    for page, verified in zip((first, continuation, tail), hits, strict=True):
        assert page["page"]["total_count"] == len(rows)
        assert page["page"]["filtered_count"] == len(expected)
        assert page["page"]["returned_count"] == len(page["items"])
        assert {source[0] for source in verified} == {row["seek"] for row in page["items"]}
    assert not tail["page"]["has_more"]
    assert tail["page"]["next_cursor"] is None
    with pytest.raises(KernelError):
        read_filtered_collection(
            model,
            snap,
            "funds",
            "movements",
            filters={**filters, "movement_account_id": "unrelated"},
            cursor=first["page"]["next_cursor"],
            limit=500,
        )


def test_nonexistent_exact_selection_keeps_complete_summary_and_reads_no_raw(snap, monkeypatch):
    rows = [{"seek": "asset-a", "asset_type": "fixed", "status": "active", "cost_fen": 210}]
    model = Model("assets", "assets", rows)
    hits = capture_hits(monkeypatch)
    data = {
        "fixed_asset_cost_fen": 210,
        "registered_count": 1,
        "asset_id": None,
        "asset_filter": "all",
        "project_id": None,
        "collections": {"assets": {}},
    }
    result = read_filtered_data(
        model,
        snap,
        "assets",
        data,
        section="assets",
        filters={"asset_filter": "fixed", "asset_id": "missing", "project_id": None},
    )
    assert result["fixed_asset_cost_fen"] == 210
    assert result["registered_count"] == 1
    assert result["asset_id"] == "missing"
    assert result["collections"]["assets"]["page"] == {
        "total_count": 1,
        "filtered_count": 0,
        "returned_count": 0,
        "has_more": False,
        "next_cursor": None,
    }
    assert model.loaded == []
    assert hits == [set()]


def test_independent_asset_project_and_employee_labor_dimensions():
    assert directory_matches(
        "assets",
        "assets",
        ("a", "fixed", "active"),
        {"asset_filter": "all", "asset_id": None, "project_id": "other"},
    )
    assert directory_matches(
        "assets",
        "projects",
        ("calc", "project-a"),
        {"asset_filter": "pending", "asset_id": "other", "project_id": "project-a"},
    )
    assert directory_matches(
        "employees",
        "labor_sources",
        ("calc", "person-a"),
        {"employee_filter": "employment_departed", "employee_id": "person-a"},
    )
    assert directory_matches(
        "funds",
        "statements",
        ("row", "account-b"),
        {
            "movement_account_type": "cash",
            "movement_account_id": "other",
            "statement_account_id": "account-b",
        },
    )


@pytest.mark.parametrize("selected,expected", [
    ("all", {"active", "ended", "converted", "unknown"}),
    ("employment_active", {"active", "converted"}),
    ("in_period", {"active", "converted"}),
    ("ended", {"ended"}),
    ("employment_departed", {"ended", "converted"}),
    ("unknown", {"unknown"}),
    ("employment_unknown", {"unknown"}),
    ("employment_unpaid_leave", set()),
])
def test_labor_person_directory_matches_month_membership_and_reads_only_hits(
    snap, monkeypatch, selected, expected,
):
    rows = [
        {"seek": key, "person_id": key, "in_period": member, "employment_state": state,
         "period_states": states}
        for key, member, state, states in (
            ("active", True, "labor", ["labor"]),
            ("converted", True, "regular", ["labor", "regular"]),
            ("ended", False, "labor_ended", ["labor_ended"]),
            ("unknown", None, "unknown", ["unknown"]),
        )
    ]
    model = Model("employees", "labor_workers", rows, variant="all")
    hits = capture_hits(monkeypatch)
    result = read_filtered_collection(
        model, snap, "employees", "labor_workers",
        filters={"employee_filter": selected, "employee_id": None},
    )
    assert {item["person_id"] for item in result["items"]} == expected
    assert result["page"]["total_count"] == 4
    assert result["page"]["filtered_count"] == len(expected)
    assert {source[0] for source in hits[0]} == expected
    assert len(model.loaded) <= len(expected)


def test_labor_person_directory_rejects_a_different_displayed_identity():
    with pytest.raises(KernelError) as failure:
        decorate_keys("employees", "labor_workers", ["expected"], [{
            "person_id": "different", "in_period": True,
            "employment_state": "labor", "period_states": ["labor"],
        }])
    assert failure.value.code == "content_integrity_failed"


@pytest.mark.parametrize(
    ("selected", "expected"),
    [
        ("all", {"active", "pending", "disposed", "retired"}),
        ("active", {"active"}),
        ("pending", {"pending"}),
        ("exited", {"disposed", "retired"}),
        ("fixed", {"active", "pending", "disposed"}),
        ("intangible", {"retired"}),
    ],
)
def test_asset_filters_cover_full_lifecycle(selected, expected):
    entries = [
        ("active", "fixed", "active"),
        ("pending", "fixed", "pending_activation"),
        ("disposed", "fixed", "disposed"),
        ("retired", "intangible", "retired"),
    ]
    assert {
        entry[0]
        for entry in entries
        if directory_matches(
            "assets",
            "assets",
            entry,
            {"asset_filter": selected, "asset_id": None, "project_id": None},
        )
    } == expected


@pytest.mark.parametrize(
    ("selected", "expected"),
    [
        ("all", {"regular", "leave", "departed", "unknown", "future"}),
        ("in_period", {"regular", "leave"}),
        ("unknown", {"unknown"}),
        ("ended", {"departed"}),
        ("employment_active", {"regular"}),
        ("employment_unpaid_leave", {"leave"}),
        ("employment_departed", {"departed"}),
        ("employment_unknown", {"unknown"}),
    ],
)
def test_monthly_state_filters_never_use_later_hire_or_current_state(selected, expected):
    entries = [
        ("regular", "in_period", "true", "established", "regular"),
        ("leave", "in_period", "true", "established", "unpaid_leave"),
        ("departed", "ended", "false", "established", "departed"),
        ("unknown", "unknown", "unknown", "established", "unknown"),
        ("future", "not_started", "false", "established", "-"),
    ]
    states = {
        "regular": "regular",
        "leave": "unpaid_leave",
        "departed": "departed",
        "unknown": "unknown",
        "future": "regular",
    }
    filters = {"employee_filter": selected, "employee_id": None}
    assert {
        entry[0]
        for entry in entries
        if directory_matches(
            "employees",
            "employees",
            entry,
            filters,
            employee_states=states,
        )
    } == expected


def test_corrupt_predicate_and_partial_scope_are_rejected(snap, monkeypatch):
    capture_hits(monkeypatch)
    rows = [{"seek": "asset-a", "asset_type": "fixed", "status": "active"}]
    model = Model("assets", "assets", rows)
    filters = {"asset_filter": "all", "asset_id": None, "project_id": None}
    # A root/body projection mismatch cannot return an incorrectly selected row.
    model.blocks["assets/default/assets/0"].payload["data"]["collections"]["assets"]["items"][0] = {
        **rows[0],
        "status": "disposed",
    }
    with pytest.raises(KernelError) as error:
        read_filtered_collection(model, snap, "assets", "assets", filters=filters)
    assert error.value.code == "content_integrity_failed"
    model.root.blocks[0].filtered_count = 0
    with pytest.raises(KernelError):
        read_filtered_collection(model, snap, "assets", "assets", filters=filters)


def test_native_later_account_statement_selection_and_zero_hit_sources(book, monkeypatch):
    engine, save, publish, _ = book
    _publish_filter_funding(engine, ["bank-a", "bank-b", "bank-a"])
    statement(save, publish, [entry("statement-b", amount=7)], bank="bank-b")
    Maintenance(engine).rebuild_dashboard_read_model("2026-09", request_id="filter-model")
    dashboard = Dashboard(engine)
    for options in (
        {"movement_account_type": "bank", "movement_account_id": "bank-b"},
        {"statement_account_id": "bank-b"},
        {"movement_account_selection": "first", "statement_account_id": "bank-b"},
        {"movement_account_type": "bank", "movement_account_id": "missing", "section": "movements"},
        {"statement_account_id": "missing", "section": "statements"},
    ):
        with raw_read():
            expected = dashboard.funds("2026-09", **options)
        with source_body_work(monkeypatch) as work:
            actual = dashboard.funds("2026-09", **options)
        assert actual == expected
        validate_response("dashboard_funds", actual)
        assert actual["data"]["total_fen"] == 3
        if "missing" in options.values():
            assert not work["outcomes"]
            assert not work["facts"]
        if options.get("movement_account_id") == "bank-b":
            assert actual["data"]["collections"]["movements"]["page"]["filtered_count"] == 1


def test_native_asset_type_lifecycle_and_exact_id_keep_complete_money(book, monkeypatch):
    engine, save, publish, _ = book
    for ident, kind, amount in (("fixed-a", "fixed", 121), ("intangible-a", "intangible", 240)):
        save(
            "asset",
            ident,
            {
                "period": "2026-09",
                "asset_id": ident,
                "asset_type": kind,
                "acquisition_date": "2026-09-01",
                "supplier_id": "supplier",
                "cost_fen": amount,
                "acquisition_basis": "direct_purchase",
            },
        )
    publish("fixed-a", "intangible-a")
    Maintenance(engine).rebuild_dashboard_read_model("2026-09", request_id="filter-model")
    dashboard = Dashboard(engine)
    for options in (
        {"asset_filter": "fixed"},
        {"asset_filter": "intangible"},
        {"asset_filter": "pending"},
        {"asset_filter": "exited"},
        {"asset_filter": "all", "asset_id": "intangible-a"},
        {"asset_filter": "fixed", "asset_id": "intangible-a", "section": "assets"},
        {"project_id": "missing", "section": "projects"},
    ):
        with raw_read():
            expected = dashboard.assets("2026-09", **options)
        with source_body_work(monkeypatch) as work:
            actual = dashboard.assets("2026-09", **options)
        assert actual == expected
        validate_response("dashboard_assets", actual)
        assert actual["data"]["registered_count"] == 2
        if options.get("section"):
            assert not work["outcomes"]
            assert not work["facts"]


def test_native_all_employee_filters_and_exact_id_keep_complete_money(company, monkeypatch):
    from typing import get_args

    company.publish("january")
    Maintenance(company.engine).rebuild_dashboard_read_model("2026-01", request_id="filter-model")
    dashboard = Dashboard(company.engine)
    for selected in get_args(EmployeeFilter):
        for ident in (None, "employee", "missing"):
            options = {"employee_filter": selected, "employee_id": ident, "section": "employees"}
            with raw_read():
                expected = dashboard.employees("2026-01", **options)
            with source_body_work(monkeypatch) as work:
                actual = dashboard.employees("2026-01", **options)
            assert actual == expected
            validate_response("dashboard_employees", actual)
            assert actual["data"]["employees"]["registered_count"] == 1
            if ident == "missing":
                assert not work["outcomes"]
                assert not work["facts"]
