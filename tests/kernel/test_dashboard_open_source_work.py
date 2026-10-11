"""Measure real loaded blocks/bytes while retaining complete current totals."""

import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest
from test_banking import book as book
from test_banking import close_month, inventories
from test_dashboard_open_groups import expenses, snapshot
from test_integrity_content import damage

from ai_accounting.kernel import dashboard_open_groups as groups
from ai_accounting.kernel import settlement_freeze as freeze
from ai_accounting.kernel import settlement_projection as projection
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard_sort import fact_identity_scalars
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.types import canonical


@pytest.mark.parametrize("closed", [False, True])
def test_current_exact_sources_keep_complete_cutoff_totals_and_reuse_actual_loads(
    book,
    closed,
    monkeypatch,
    record_property,
):
    engine, _, _, proof = book
    historic_amount = expenses(book, 40)
    if closed:
        assert (
            close_month(inventories(engine, proof, "2026-09", {"transactions"}), proof, "2026-09")[
                "status"
            ]
            == "closed"
        )
    expenses(book, 1, "2026-10", party="later-party", prefix="later")
    work = {
        "state_bytes": 0,
        "state_count": 0,
        "block_entries": 0,
        "block_count": 0,
        "header_batches": 0,
        "group_digests": 0,
    }
    original_sha, original_block, original_headers = (
        freeze._sha,
        freeze._decode_block,
        groups._source_headers,
    )
    original_group_hash = groups.hashlib.sha256

    def counted_group_hash(payload):
        if payload.startswith(b'["company","payable","supplier",'):
            work["group_digests"] += 1
        return original_group_hash(payload)

    def counted_sha(payload):
        if payload.startswith("{") and '"obligation_key"' in payload:
            work["state_count"] += 1
            work["state_bytes"] += len(payload.encode("utf-8"))
        return original_sha(payload)

    def counted_block(*args, **kwargs):
        result = original_block(*args, **kwargs)
        work["block_count"] += 1
        work["block_entries"] += len(result)
        return result

    def counted_headers(*args, **kwargs):
        work["header_batches"] += 1
        return original_headers(*args, **kwargs)

    monkeypatch.setattr(freeze, "_sha", counted_sha)
    monkeypatch.setattr(freeze, "_decode_block", counted_block)
    monkeypatch.setattr(groups, "_source_headers", counted_headers)
    monkeypatch.setattr(groups.hashlib, "sha256", counted_group_hash)
    with snapshot(engine, "2026-09") as snap:
        historical = projection.settlement_dashboard_open_sources(
            snap.connection,
            snap.period,
            reads=snap.reads,
        )
        after_historical = dict(work)
        current = projection.settlement_dashboard_open_sources(
            snap.connection,
            snap.period,
            current=True,
            page_keys={item["key"] for item in historical["obligations"][:20]},
            exact_sources=True,
            reads=snap.reads,
        )
        assert len(current["obligations"]) == 20
        assert current["categories"]["supplier_payables"] == {
            "count": 40,
            "amount": historic_amount,
        }
        assert current["complete"] and current["status"] == "established"
        if closed:
            # Count actual payload hash bytes and loaded block contents, not
            # wrapper invocations or a mocked SQL reader's reported sizes.
            assert work["state_bytes"] == after_historical["state_bytes"]
            assert work["state_count"] == after_historical["state_count"]
            assert work["block_entries"] == after_historical["block_entries"]
            assert work["block_count"] == after_historical["block_count"]
            assert snap.reads._frozen_settlement_scalars
        page = groups.open_group_page(snap)
        assert page["payable_fen"] == historic_amount
        assert page["current_outstanding"]["payable_fen"] == historic_amount
        # Compare the complete, unsliced source-reader path used before the
        # optimization with exact keys plus full category measures. Sources
        # outside the requested keys must keep contributing their full totals.
        full_current = projection.settlement_dashboard_open_sources(
            snap.connection, snap.period, current=True, reads=snap.reads,
        )
        data = snap._open_group_data
        identities = data["identities"]
        exact_display = groups._display_categories(current, identities)
        full_display = groups._display_categories(full_current, identities)
        assert exact_display["categories"] == full_display["categories"]
        statements = []
        snap.connection.set_trace_callback(statements.append)
        try:
            scalar_again = fact_identity_scalars(
                snap, snap._open_group_data["source_headers"].values(),
            )
        finally:
            snap.connection.set_trace_callback(None)
        assert scalar_again == snap._open_group_data["source_scalars"]
        assert statements == []
        next(iter(scalar_again.values()))["creditor_kind"] = "employee"
        assert all(
            item["creditor_kind"] == "supplier"
            for item in snap._open_group_data["source_scalars"].values()
        )
        before_members = dict(work)
        assert work["group_digests"] == 1
        member_page = groups.open_group_members(snap, page["collection"]["items"][0]["group_key"])
        assert member_page["page"]["total_count"] == 40
        assert len(member_page["items"]) == 20
        assert all(item["recognition"]["period"] == "2026-09"
                   for item in member_page["items"])
        assert len(snap.reads._report_snapshot_cache["dashboard_fact_sort_scalars"]) == 20
        assert work["header_batches"] == before_members["header_batches"]
        for key, value in work.items():
            record_property(key, value)
    with QueryReads.snapshot(engine) as reads:
        assert reads._frozen_settlement_scalars == {}
        assert reads._frozen_settlement_open_entries == {}


def test_new_snapshot_rejects_damaged_state_after_successful_scalar_cache(book):
    engine, _, _, proof = book
    expenses(book, 2)
    assert (
        close_month(inventories(engine, proof, "2026-09", {"transactions"}), proof, "2026-09")[
            "status"
        ]
        == "closed"
    )
    with snapshot(engine, "2026-09") as snap:
        assert groups.open_group_page(snap)["payable_count"] == 2
        assert snap.reads._frozen_settlement_scalars
    damage(
        engine,
        "settlement_state_revision",
        "UPDATE settlement_state_revision SET payload=json_set(payload,'$.paid',1)",
    )
    with snapshot(engine, "2026-09") as snap:
        with pytest.raises(KernelError, match="冻结清偿依据不匹配"):
            groups.open_group_page(snap)


def test_equal_scope_sources_reuse_after_current_tail_check_and_keep_results_independent(
    book, monkeypatch,
):
    engine, _, _, proof = book
    amount = expenses(book, 2)
    assert close_month(inventories(engine, proof, "2026-09", {"transactions"}), proof, "2026-09")[
        "status"
    ] == "closed"
    work = {"views": 0, "tails": 0}
    original_view, original_tail = projection._obligation_view, freeze._tail_rows

    def view(*a, **k):
        work["views"] += 1
        return original_view(*a, **k)

    def tail(*a, **k):
        work["tails"] += 1
        return original_tail(*a, **k)

    monkeypatch.setattr(projection, "_obligation_view", view)
    monkeypatch.setattr(freeze, "_tail_rows", tail)
    with snapshot(engine, "2026-09") as snap:
        historical = projection.settlement_dashboard_open_sources(
            snap.connection, snap.period, reads=snap.reads, reuse_scope_sources=True,
        )
        keys = {item["key"] for item in historical["obligations"]}
        before = dict(work)
        historical["categories"]["supplier_payables"]["amount"] = 1
        historical["obligations"][0]["remaining_fen"] = 1
        historical["obligations"][0]["source_business"]["kind"] = "changed"
        current = projection.settlement_dashboard_open_sources(
            snap.connection, snap.period, current=True, exact_sources=True,
            page_keys=keys, reads=snap.reads, reuse_scope_sources=True,
        )
        assert work["tails"] > before["tails"]
        assert work["views"] == before["views"]
        assert current["categories"]["supplier_payables"]["amount"] == amount
        assert sum(item["remaining_fen"] for item in current["obligations"]) == amount
        assert all(item["source_business"]["kind"] == "expense" for item in current["obligations"])
        current["obligations"][0]["remaining_fen"] = 2
        again = projection.settlement_dashboard_open_sources(
            snap.connection, snap.period, current=True, exact_sources=True,
            page_keys=keys, reads=snap.reads, reuse_scope_sources=True,
        )
        assert sum(item["remaining_fen"] for item in again["obligations"]) == amount
        # Subsets and ordinary public reads retain their independent path.
        subset = projection.settlement_dashboard_open_sources(
            snap.connection, snap.period, current=True, exact_sources=True,
            page_keys={next(iter(keys))}, reads=snap.reads, reuse_scope_sources=True,
        )
        assert len(subset["obligations"]) == 1 and work["views"] > before["views"]


def test_exact_display_category_moves_preserve_unknown_complete_measures():
    original = {
        "categories": {"other_payables": {"count": 2, "amount": None}},
        "_category_measures": {"other_payables": {"count": 2, "sum": 50, "unknown_count": 1}},
        "obligations": [
            {"key": "unknown", "category_key": "other_payables", "remaining_fen": None}
        ],
        "complete": False,
    }
    displayed = groups._display_categories(original, {"unknown": {"category_key": "tax_payables"}})
    assert displayed["categories"] == {
        "other_payables": {"count": 1, "amount": 50},
        "tax_payables": {"count": 1, "amount": None},
    }
    assert displayed["complete"] is False
    assert "_category_measures" not in displayed
    assert original["_category_measures"]["other_payables"]["unknown_count"] == 1


def test_exact_display_categories_equal_full_sources_with_outside_and_settled_keys():
    # HEAD builds identities only from historically open keys. A currently
    # reopened/new source outside those keys keeps the source classification;
    # its payroll/labor tax display matter is not applied by the full path.
    historical_keys = {"historical-tax", "historical-settled"}
    rows = [
        {"key": "historical-tax", "category_key": "payroll_payables", "remaining_fen": 7},
        {"key": "historical-settled", "category_key": "supplier_payables", "remaining_fen": 0},
        {"key": "outside-tax", "category_key": "payroll_payables", "remaining_fen": 11},
        {"key": "outside-labor-tax", "category_key": "labor_payables", "remaining_fen": 13},
        {"key": "outside-opening", "category_key": "other_payables", "remaining_fen": None},
    ]
    identities = {
        "historical-tax": {"category_key": "tax_payables"},
        "historical-settled": {"category_key": "employee_payables"},
    }
    common = {"complete": False, "status": "partially_established"}
    full = groups._display_categories(common | {"obligations": rows}, identities)
    exact = groups._display_categories(common | {
        "obligations": [row for row in rows if row["key"] in historical_keys],
        "_category_measures": {
            "payroll_payables": {"count": 2, "sum": 18, "unknown_count": 0},
            "labor_payables": {"count": 1, "sum": 13, "unknown_count": 0},
            "other_payables": {"count": 1, "sum": 0, "unknown_count": 1},
        },
    }, identities)
    assert exact["categories"] == full["categories"] == {
        "tax_payables": {"count": 1, "amount": 7},
        "payroll_payables": {"count": 1, "amount": 11},
        "labor_payables": {"count": 1, "amount": 13},
        "other_payables": {"count": 1, "amount": None},
    }
    assert exact["complete"] == full["complete"] is False


@pytest.mark.parametrize("exceptional", [None, "object", "array", "large_integer"])
def test_packed_state_scalars_equal_individual_json1_types_without_decoding_events(
    monkeypatch,
    exceptional,
):
    with sqlite3.connect(":memory:") as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE settlement_state_revision(digest BLOB PRIMARY KEY,"
            "obligation_key TEXT,payload TEXT)"
        )
        state = freeze._empty_state("key")
        state.update(first_source_period=1, source_event_count=1, bad_paid=True)
        state["events"] = [{"unconsumed": index} for index in range(200)]
        if exceptional == "object":
            state["counterparty_id"] = {"z": 1, "a": [True, None]}
        elif exceptional == "array":
            state["counterparty_id"] = [True, None, "name"]
        elif exceptional == "large_integer":
            state["source_amount"] = 1 << 70
        payload = canonical(state)
        checksum = hashlib.sha256(payload.encode()).hexdigest()
        connection.execute(
            "INSERT INTO settlement_state_revision VALUES(?,?,?)",
            (bytes.fromhex(checksum), "key", payload),
        )
        decoded_arrays = []
        original_loads = json.loads
        original_parse = freeze.from_json
        original_native = freeze._STATE_SCALAR_VALUES

        def scalar_only_loads(raw, **kwargs):
            assert raw.startswith("["), "growing state histories must not be decoded"
            result = original_parse(raw, **kwargs)
            decoded_arrays.append(raw)
            return result

        def scalar_only_native(raw):
            assert raw.startswith("["), "growing state histories must not be decoded"
            result = original_native.validate_json(raw)
            decoded_arrays.append(raw)
            return result

        with monkeypatch.context() as patch:
            patch.setattr(freeze, "from_json", scalar_only_loads)
            patch.setattr(freeze, "_STATE_SCALAR_VALUES",
                          SimpleNamespace(validate_json=scalar_only_native))
            result = freeze._dashboard_state_scalars(
                connection,
                SimpleNamespace(base_period=1, overrides={}),
                {"key": [checksum, 1]},
            )["key"]
        expected = dict(
            connection.execute(
                "SELECT "
                + ",".join(f"json_extract(payload,'$.{field}') {field}" for field in result)
                + " FROM settlement_state_revision",
            ).fetchone()
        )
        assert result == expected
        assert {key: type(value) for key, value in result.items()} == {
            key: type(value) for key, value in expected.items()
        }
        assert len(decoded_arrays) == 1
        assert len(original_loads(decoded_arrays[0])) == 18
