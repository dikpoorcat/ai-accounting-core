"""Owned decoded owner bodies do not replace complete member validation."""

import copy
import json
from unittest.mock import patch

import pytest
from stage9_metrics import measure_work
from test_asset_batch_reads import prepare_batch_assets
from test_asset_batches import month
from test_integrity_content import damage
from test_payroll_corrections import Company

from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.types import YearMonth


@pytest.fixture
def open_owners(tmp_path):
    company = Company(tmp_path / "open-owner-outcomes.sqlite")
    prepare_batch_assets(company)
    engine = company.engine
    with engine.store.connection(read_only=True) as connection:
        evidence = engine.store.current_fact(connection, "computer").evidence[0]
    month(engine, evidence, "2026-03", "open-consumption-march")
    with engine.store.connection(read_only=True) as connection:
        owners = {row["id"]: dict(row) for row in connection.execute(
            "SELECT * FROM calculation WHERE kind IN "
            "('asset_activation_batch','asset_consumption_month')",
        )}
    assert len(owners) == 2
    return engine, owners


def read_members(engine, owners, *, reuse):
    with QueryReads.snapshot(engine) as reads:
        decoded = {}
        reads.metadata(owners, _decoded_outcomes=decoded)
        members = reads.asset_members_many(owners, _decoded_owners=decoded if reuse else None)
        assert not reads._verified_source_contents
        return members


def test_complete_members_have_equal_value_with_two_fewer_actual_owner_bodies(open_owners):
    engine, owners = open_owners
    before_work, before = measure_work(engine, lambda: read_members(engine, owners, reuse=False))
    after_work, after = measure_work(engine, lambda: read_members(engine, owners, reuse=True))
    assert after == before
    old, new = before_work["counters"], after_work["counters"]
    expected_bytes = sum(len(owner["outcome"].encode("utf-8")) for owner in owners.values())
    for counter in ("calculation_result_rows_loaded", "calculation_result_json_decodes"):
        assert old[counter] - new[counter] == len(owners)
    assert old["calculation_result_bytes_loaded"] - new[
        "calculation_result_bytes_loaded"
    ] == expected_bytes
    assert new["returned_value_bytes"] < old["returned_value_bytes"]
    print(json.dumps({"owners": sorted(owners), "body_bytes": expected_bytes,
                      "complete": old, "reused": new}))


@pytest.mark.parametrize("closed_history", [False, True], ids=["all-open", "closed-open-tail"])
def test_public_assets_equal_with_less_actual_owner_body_work(tmp_path, closed_history):
    company = Company(tmp_path / "public-owner-outcomes.sqlite")
    prepare_batch_assets(company)
    engine = company.engine
    with engine.store.connection(read_only=True) as connection:
        evidence = engine.store.current_fact(connection, "computer").evidence[0]
    if closed_history:
        company.close("2026-02")
    month(engine, evidence, "2026-03", "public-consumption-march")
    period = "2026-03"
    if closed_history:
        company.close("2026-03")
        period = "2026-04"
        month(engine, evidence, period, "public-consumption-april")

    # Determine this fixture's open consumer from its named business subject,
    # independently of the selector/cache whose body reuse is being measured.
    subject = "asset-consumption-month:" + period
    with engine.store.connection(read_only=True) as connection:
        owner = dict(connection.execute(
            "SELECT * FROM calculation WHERE subject_id=?", (subject,),
        ).fetchone())
        consumed = {owner["id"]: owner}
        assert owner["kind"] == "asset_consumption_month"
        assert owner["period"] == YearMonth(period).ordinal
        assert {row[0] for row in connection.execute(
            "SELECT calculation_id FROM calculation_current WHERE subject_id=?", (subject,),
        )} == consumed.keys()
        assert {(row[0], row[1]) for row in connection.execute(
            "SELECT calculation_id,posting_period FROM calculation_publication "
            "WHERE subject_id=?", (subject,),
        )} == {(owner["id"], YearMonth(period).ordinal)}
        assert {row[0] for row in connection.execute(
            "SELECT v.calculation_id FROM voucher_version v JOIN voucher_current h "
            "ON h.version_id=v.id WHERE v.calculation_id=?", (owner["id"],),
        )} == consumed.keys()
        owners = {row[0] for row in connection.execute(
            "SELECT id FROM calculation WHERE subject_id IN "
            "('activation-batch','asset-consumption-month:2026-03',"
            "'asset-consumption-month:2026-04')",
        )}
        assert len(owners) == (3 if closed_history else 2)
        assert {row[0] for row in connection.execute("SELECT period FROM period_close")} == (
            {YearMonth("2026-02").ordinal, YearMonth("2026-03").ordinal}
            if closed_history else set()
        )

    original = QueryReads.asset_members_many
    seen = {"complete": set(), "reused": set()}

    def selected_owner_bodies(mode):
        def read(reads, owner_ids, *, _decoded_owners=None):
            ids = tuple(owner_ids)
            missing = set(ids) - reads._asset_members.keys()
            targets = missing & consumed.keys()
            if targets:
                assert targets <= (_decoded_owners or {}).keys()
                seen[mode].update(targets)
            # Both variants keep activation reuse and all member/source guards.
            # The control re-reads only the independently named open consumer.
            retained = {
                ident: _decoded_owners[ident] for ident in ids
                if ident in (_decoded_owners or {})
                and (mode == "reused" or ident not in consumed)
            } if _decoded_owners is not None else None
            return original(reads, ids, _decoded_owners=retained)
        return read

    with patch.object(QueryReads, "asset_members_many", selected_owner_bodies("complete")):
        before_work, before = measure_work(
            engine, lambda: Dashboard(engine).assets(period, preparation="deferred"),
        )
    with patch.object(QueryReads, "asset_members_many", selected_owner_bodies("reused")):
        after_work, after = measure_work(
            engine, lambda: Dashboard(engine).assets(period, preparation="deferred"),
        )
    assert after == before
    assert seen == {"complete": consumed.keys(), "reused": consumed.keys()}
    assert after["data"]["collections"]["assets"]["page"]["total_count"] == 2
    assert after["data"]["active_count"] == 2
    old, new = before_work["counters"], after_work["counters"]
    # Default card balances consume the complete open consumption batch.
    # Reusing its already decoded body saves input, never member validation.
    expected_bytes = sum(len(owner["outcome"].encode("utf-8")) for owner in consumed.values())
    for counter in ("calculation_result_rows_loaded", "calculation_result_json_decodes"):
        assert old[counter] - new[counter] == len(consumed)
    assert old["calculation_result_bytes_loaded"] - new[
        "calculation_result_bytes_loaded"
    ] == expected_bytes
    assert old["returned_value_bytes"] - new["returned_value_bytes"] == expected_bytes
    assert new["typed_fact_json_decodes"] == old["typed_fact_json_decodes"]
    assert new["returned_rows"] == old["returned_rows"]
    print(json.dumps({"period": period, "closed_history": closed_history,
                      "consumption_owners": sorted(consumed), "body_bytes": expected_bytes,
                      "complete_assets": old, "reused_assets": new}))


@pytest.mark.parametrize("fault", ["summary", "dependency", "member_body", "owner_seal"])
def test_reused_owner_keeps_member_guards_and_failure_atomicity(open_owners, fault):
    engine, owners = open_owners
    with engine.store.connection(read_only=True) as connection:
        member = dict(connection.execute(
            "SELECT m.* FROM asset_batch_member m "
            "JOIN calculation c ON c.id=m.owner_calculation_id "
            "WHERE c.kind='asset_consumption_month' ORDER BY m.position LIMIT 1",
        ).fetchone())
    if fault == "summary":
        damage(engine, "asset_batch_member", "UPDATE asset_batch_member SET summary='{}' "
               "WHERE owner_calculation_id=? AND position=?",
               (member["owner_calculation_id"], member["position"]))
    elif fault == "dependency":
        damage(engine, "dependency_calculation", "DELETE FROM dependency_calculation "
               "WHERE calculation_id=? AND upstream_id=?",
               (member["owner_calculation_id"], member["member_calculation_id"]))
    elif fault == "owner_seal":
        damage(engine, "calculation_seal", "DELETE FROM calculation_seal WHERE calculation_id=?",
               (member["owner_calculation_id"],), foreign_keys=False)
    else:
        damage(engine, "calculation", "UPDATE calculation SET outcome=json_set(outcome, "
               "'$.values.asset_id','changed-member') WHERE id=?",
               (member["member_calculation_id"],))
    with QueryReads.snapshot(engine) as reads:
        decoded = {}
        reads.metadata(owners, _decoded_outcomes=decoded)
        assert decoded.keys() == owners.keys()
        for _ in range(2):
            with pytest.raises(KernelError):
                reads.asset_members_many(owners, _decoded_owners=decoded)
            assert not reads._asset_members
    with pytest.raises(KernelError):
        Dashboard(engine).assets("2026-03", preparation="deferred")


def test_owner_digest_and_unowned_or_new_snapshot_decoded_proof_boundaries(open_owners):
    engine, owners = open_owners
    with QueryReads.snapshot(engine) as reads:
        decoded = {}
        reads.metadata(owners, _decoded_outcomes=decoded)
        changed = copy.deepcopy(decoded)
        ident = next(iter(changed))
        changed[ident]["values"]["member_count"] += 1
        with pytest.raises(KernelError) as failure:
            reads.asset_members_many(owners, _decoded_owners=changed)
        assert failure.value.code == "asset_batch_digest"
        assert not reads._asset_members
    assert not reads._snapshot_active
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        unowned = QueryReads(engine, connection)
        assert unowned.asset_members_many(owners, _decoded_owners=changed) == (
            read_members(engine, owners, reuse=False)
        )
    with QueryReads.snapshot(engine) as reads:
        assert not reads._verified_sql_outcomes
        assert reads.asset_members_many(owners, _decoded_owners=changed) == (
            read_members(engine, owners, reuse=False)
        )
    with QueryReads.snapshot(engine) as reads, historical_content(1):
        reads._verified_sql_outcomes.update(owners)
        assert reads.asset_members_many(owners, _decoded_owners=changed) == (
            read_members(engine, owners, reuse=False)
        )
