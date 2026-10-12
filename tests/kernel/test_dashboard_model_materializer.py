"""Complete typed read content, ordered paging and independent rebuilds."""

from contextlib import contextmanager

import pytest
from entity_fixture import save_entity_display_profile
from test_banking import book as book
from test_banking import entry, funding, match, opening, reconciliation, statement
from test_dashboard_funds_alignment import _publish_filter_funding
from test_dashboard_open_groups import expenses
from test_payroll_corrections import company as company_fixture

from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_model_materializer import build_model
from ai_accounting.kernel.response_contracts import validate_response

company = company_fixture


def _block(model, page, section="overview", ordinal=0):
    return next(
        block for block in model.blocks if block.key == f"{page}/default/{section}/{ordinal}"
    )


def test_default_content_matches_existing_native_pages_and_uses_one_snapshot(book, monkeypatch):
    engine = book[0]
    _publish_filter_funding(engine, ["bank-a", "bank-b", "bank-a"])
    dashboard = Dashboard(engine)
    ordinary = {
        "brief": dashboard.brief("2026-09"),
        "funds": dashboard.funds("2026-09", movement_account_selection="first"),
        "employees": dashboard.employees("2026-09"),
        "assets": dashboard.assets("2026-09"),
    }
    calls = []
    original = dashboard._snapshot

    @contextmanager
    def counted(period):
        with original(period) as snapshot:
            calls.append(snapshot)
            yield snapshot

    monkeypatch.setattr(dashboard, "_snapshot", counted)
    model = build_model(dashboard, "2026-09", block_size=2)
    assert len(calls) == 1
    assert model.period == "2026-09"
    assert model.unmaterialized == ()
    for page, expected in ordinary.items():
        actual = _block(model, page).payload
        validate_response("dashboard_" + page, actual)
        assert actual["snapshot_version"] == expected["snapshot_version"]
        assert actual["selected_period"] == expected["selected_period"]
        for collection in expected["data"]["collections"].values():
            collection["page"]["next_cursor"] = None
        for collection in actual["data"]["collections"].values():
            collection["page"]["next_cursor"] = None
        if page == "brief":
            expected["data"].pop("generated_at")
            actual["data"].pop("generated_at")
        assert actual["data"] == expected["data"]


def test_complete_collections_retain_original_order_and_exact_voucher_lines(book):
    engine = book[0]
    _publish_filter_funding(engine, ["bank-a", "bank-b", "bank-a", "bank-b", "bank-a"])
    dashboard = Dashboard(engine)
    model = build_model(dashboard, "2026-09", block_size=2)
    for index in model.collections:
        prefix = f"{index.page}/{index.variant}/{index.section}/"
        blocks = [block for block in model.blocks if block.key.startswith(prefix)]
        assert tuple(key[0] for block in blocks for key in block.keys) == index.keys
        assert len(index.keys) == index.filtered_count
        assert len(set(index.keys)) == len(index.keys)
        assert all(block.total_count == index.total_count for block in blocks)
        assert all(len(block.keys) <= 2 for block in blocks)
        for block in blocks:
            validate_response(block.kind, block.payload)
            collection = block.payload["data"]["collections"][index.section]
            assert collection["page"]["returned_count"] == len(block.keys)
            assert collection["page"]["has_more"] == (block != blocks[-1])
            assert len(block.item_sources) == len(block.keys)
            assert len(block.item_voucher_version_ids) == len(block.keys)
            assert set(block.sources) == {
                source for sources in block.item_sources for source in sources
            }
    voucher_blocks = [
        block for block in model.blocks if block.key.startswith("brief/default/vouchers/")
    ]
    assert len(voucher_blocks) == 3
    returned = [
        item
        for block in voucher_blocks
        for item in block.payload["data"]["collections"]["vouchers"]["items"]
    ]
    assert [item["number"] for item in returned] == ["1", "2", "3", "4", "5"]
    for block in voucher_blocks:
        assert block.sources
        assert len(block.voucher_version_ids) == len(block.keys)
        for item in block.payload["data"]["collections"]["vouchers"]["items"]:
            assert item["amount_fen"] == sum(line["debit_fen"] for line in item["lines"])
            assert item["amount_fen"] == sum(line["credit_fen"] for line in item["lines"])
    for section in ("activity", "open_items"):
        assert all(
            not block.sources
            for block in model.blocks
            if block.key.startswith(f"brief/default/{section}/")
        )


def test_build_after_source_change_recomputes_complete_summary_and_snapshot(book):
    engine = book[0]
    _publish_filter_funding(engine, ["bank-a"])
    dashboard = Dashboard(engine)
    before = build_model(dashboard, "2026-09", block_size=1)
    funding(book[1], book[2], subject="second", amount=1, bank="bank-b")
    after = build_model(dashboard, "2026-09", block_size=1)
    assert before.snapshot_version != after.snapshot_version
    assert _block(before, "brief").payload["data"]["voucher_count"] == 1
    assert _block(after, "brief").payload["data"]["voucher_count"] == 2
    assert _block(before, "funds").payload["data"]["total_fen"] == 1
    assert _block(after, "funds").payload["data"]["total_fen"] == 2


def test_group_summary_certifies_all_members_without_claiming_raw_member_hits(book):
    total = expenses(book, 5)
    total += expenses(book, 2, party="other-supplier", prefix="other")
    dashboard = Dashboard(book[0])
    model = build_model(dashboard, "2026-09", block_size=1)
    data = _block(model, "brief").payload["data"]
    assert data["open_items"]["payable_fen"] == total
    assert data["open_items"]["payable_count"] == 7
    groups = [_block(model, "brief", "open_items", ordinal) for ordinal in range(2)]
    items = [block.payload["data"]["collections"]["open_items"]["items"][0] for block in groups]
    assert sum(item["member_count"] for item in items) == 7
    assert sum(item["outstanding_fen"] for item in items) == total
    assert all(block.sources == () and block.item_sources == ((),) for block in groups)
    for item in items:
        members = dashboard.brief_group(
            "2026-09", section="open_items", group_key=item["group_key"], limit=20
        )
        assert len(members["data"]["collections"]["members"]["items"]) == item["member_count"]


def test_statement_hits_bind_actual_extraction_and_exact_reconciliation_dependencies(book):
    engine, save, publish, _ = book
    opening(save, publish)
    funding(save, publish)
    statement(save, publish, [entry()])
    reconciliation(save, publish, [match()])
    model = build_model(Dashboard(engine), "2026-09", block_size=1)
    block = _block(model, "funds", "statements")
    with engine.store.connection(read_only=True) as connection:
        origins = {
            row["subject_id"]: row["id"]
            for row in connection.execute(
                "SELECT c.id,c.subject_id FROM calculation_current cc "
                "JOIN calculation c ON c.id=cc.calculation_id",
            )
        }
        fact = connection.execute(
            "SELECT f.id,f.digest FROM fact_current fc JOIN fact_revision f ON f.id=fc.fact_id "
            "WHERE fc.subject_id='statement'",
        ).fetchone()
    assert {source[0] for source in block.sources} == {
        origins["funding"],
        origins["reconciliation"],
    }
    assert block.fact_sources == ((fact["id"], fact["digest"].hex()),)
    assert block.item_fact_sources == (block.fact_sources,)


def test_employee_history_stays_certified_aggregate_outside_displayed_month(company):
    company.publish("january", "february")
    save_entity_display_profile(
        company.engine,
        {
            "kind": "employee",
            "entity_id": "employee",
            "workforce_event": {"action": "hire", "effective_date": "2026-01-01"},
            "source": "Synthetic employment confirmation",
        },
        expected_revision=0,
        request_id=company.request(),
    )
    model = build_model(Dashboard(company.engine), "2026-02", block_size=1)
    with company.engine.store.connection(read_only=True) as connection:
        sources = dict(
            connection.execute(
                "SELECT subject_id,id FROM calculation WHERE subject_id IN ('january','february')",
            )
        )
    block = _block(model, "employees", "employees")
    assert block.keys
    assert sources["january"] not in {source[0] for source in block.sources}
    assert sources["february"] in {source[0] for source in block.sources}


def test_pending_statement_uses_current_fact_without_claiming_old_matched_body(book):
    engine, save, publish, _ = book
    opening(save, publish)
    funding(save, publish)
    statement(save, publish, [entry()])
    reconciliation(save, publish, [match()])
    save(
        "bank_statement",
        "statement",
        {
            "period": "2026-09",
            "bank_account_id": "bank-a",
            "opening_fen": 0,
            "closing_fen": 2000,
            "entries": [entry(amount=2000)],
        },
        revision=1,
    )
    model = build_model(Dashboard(engine), "2026-09", block_size=1)
    block = _block(model, "funds", "statements")
    item = block.payload["data"]["collections"]["statements"]["items"][0]
    assert item["amount_fen"] == 2000
    assert item["party"] == "未提供"
    assert block.sources == ()
    with engine.store.connection(read_only=True) as connection:
        fact = connection.execute(
            "SELECT f.id,f.digest FROM fact_current fc JOIN fact_revision f ON f.id=fc.fact_id "
            "WHERE fc.subject_id='statement'",
        ).fetchone()
    assert block.fact_sources == ((fact["id"], fact["digest"].hex()),)


@pytest.mark.parametrize("block_size", (0, 501, True, 1.0))
def test_invalid_block_size_is_rejected_before_reading(book, block_size):
    with pytest.raises(ValueError):
        build_model(Dashboard(book[0]), "2026-09", block_size=block_size)
