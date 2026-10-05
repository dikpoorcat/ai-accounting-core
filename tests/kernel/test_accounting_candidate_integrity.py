"""Requested current and frozen identities survive damaged candidate directories."""

import copy

import pytest
from stage9_metrics import measure_work
from test_asset_batches import activate, month
from test_asset_batches import asset_engine as asset_engine_fixture
from test_close_accounting_filter import _close_empty_months
from test_engine import engine as engine_fixture
from test_engine import evidence, publish, save
from test_integrity_content import damage

from ai_accounting.kernel import close_storage
from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.report_projection import _party_balance_nets
from ai_accounting.kernel.reports import _has_open_publication
from ai_accounting.kernel.types import YearMonth

engine = engine_fixture
asset_engine = asset_engine_fixture
JANUARY = YearMonth("2026-01").ordinal


@pytest.mark.parametrize("no_lines", [False, True])
@pytest.mark.parametrize("warm", [False, True])
def test_frozen_missing_publication_cannot_hide_month_or_subject(engine, no_lines, warm):
    engine.save_fact("test_charge", "selected", {
        "period": "2026-01", "amount": 100, "suppress_posting": no_lines,
    }, evidence=(evidence(engine),), expected_revision=0, request_id="selected")
    publish(engine, ["selected"])
    _close_empty_months(engine, ["2026-01", "2026-02"])
    damage(engine, "calculation_publication",
           "DELETE FROM calculation_publication WHERE subject_id='selected'",
           foreign_keys=False)
    with QueryReads.snapshot(engine) as reads:
        rows = reads.authoritative_close_rows(periods=[JANUARY, JANUARY + 1])
        if warm:
            reads.close_accounting_many(
                rows, subjects={"absent"},
                subjects_by_period={row["period"]: set() for row in rows},
            )
        before = (dict(reads._close_accounting_slices), dict(reads._verified_close_storage_parts),
                  dict(reads._close_accounting_positions), dict(reads._metadata))
        with pytest.raises(KernelError) as failure:
            BusinessQueries(engine, reads=reads)._selected_accounting(
                reads.connection, {"selected", "absent"}, "2026-02", include_vouchers=False,
            )
        assert failure.value.code == "content_integrity_failed"
        assert before == (reads._close_accounting_slices, reads._verified_close_storage_parts,
                          reads._close_accounting_positions, reads._metadata)


def test_empty_candidates_use_frozen_filter_without_loading_unrelated_buckets(engine, monkeypatch):
    save(engine, subject="closed")
    publish(engine, ["closed"])
    _close_empty_months(engine, ["2026-01", "2026-02", "2026-03"])

    def read():
        with QueryReads.snapshot(engine) as reads:
            rows = reads.authoritative_close_rows(periods=[JANUARY, JANUARY + 1, JANUARY + 2])
            result = reads.close_accounting_many(
                rows, subjects={"never-adopted"},
                subjects_by_period={row["period"]: set() for row in rows},
            )
            assert all(part.subjects == {"never-adopted"} for part in result)
            assert all(part.adopted_results == part.vouchers == () for part in result)
            return result

    narrow_work, expected = measure_work(engine, read)
    with monkeypatch.context() as positive:
        # Force only false positives; a Bloom positive never establishes
        # membership or authorizes a missing leaf/body.
        positive.setattr(close_storage, "_may_contain_with_positions", lambda *_args: True)
        false_positive_work, actual = measure_work(engine, read)
    assert expected == actual
    assert narrow_work["counters"]["calculation_result_rows_loaded"] == 0
    assert false_positive_work["counters"]["calculation_result_rows_loaded"] == 0
    assert (narrow_work["counters"]["returned_value_bytes"]
            <= false_positive_work["counters"]["returned_value_bytes"])


@pytest.mark.parametrize(
    "corruption", ["publication", "calculation", "kind", "current", "redirect", "fact_period"]
)
@pytest.mark.parametrize("no_lines", [False, True])
def test_current_heads_are_not_filtered_before_identity_failure(engine, corruption, no_lines):
    engine.save_fact("test_charge", "selected", {
        "period": "2026-01", "amount": 100, "suppress_posting": no_lines,
    }, evidence=(evidence(engine),), expected_revision=0, request_id="selected")
    publish(engine, ["selected"])
    with engine.store.connection(read_only=True) as connection:
        row = connection.execute(
            "SELECT c.* FROM calculation_current a JOIN calculation c ON c.id=a.calculation_id "
            "WHERE a.subject_id='selected'",
        ).fetchone()
    if corruption == "publication":
        damage(engine, "calculation_publication",
               "DELETE FROM calculation_publication WHERE calculation_id=?", (row["id"],),
               foreign_keys=False)
    elif corruption == "calculation":
        damage(engine, "calculation", "DELETE FROM calculation WHERE id=?", (row["id"],),
               foreign_keys=False)
    elif corruption == "kind":
        damage(engine, "calculation", "UPDATE calculation SET kind='test_source' WHERE id=?",
               (row["id"],))
    elif corruption == "current":
        damage(engine, "calculation_current",
               "DELETE FROM calculation_current WHERE subject_id='selected'")
    elif corruption == "redirect":
        save(engine, subject="other", period="2026-02", request="other")
        publish(engine, ["other"], request="other-publication")
        with engine.store.connection(read_only=True) as connection:
            other_id = connection.execute(
                "SELECT calculation_id FROM calculation_current WHERE subject_id='other'",
            ).fetchone()[0]
        damage(engine, "calculation_current",
               "DELETE FROM calculation_current WHERE subject_id='other'")
        damage(engine, "calculation_current", "UPDATE calculation_current SET calculation_id=? "
               "WHERE subject_id='selected'", (other_id,), foreign_keys=False)
    else:
        damage(engine, "fact_revision", "UPDATE fact_revision SET period=period+1 WHERE id=?",
               (row["fact_id"],))
    with QueryReads.snapshot(engine) as reads:
        queries = BusinessQueries(engine, reads=reads)
        before = copy.deepcopy(reads._metadata)
        for operation in (
            lambda: queries._selected_accounting(reads.connection, {"selected"}, "2026-01"),
            lambda: queries._current_publication(reads.connection, "selected"),
        ):
            with pytest.raises(KernelError) as failure:
                operation()
            assert failure.value.code == "content_integrity_failed"
        assert reads._metadata == before


def test_future_current_body_is_outside_older_cutoff(engine):
    save(engine, subject="future", period="2026-02")
    publish(engine, ["future"])
    damage(engine, "calculation",
           "UPDATE calculation SET outcome=json_set(outcome,'$.values.amount',2) "
           "WHERE subject_id='future'")
    with QueryReads.snapshot(engine) as reads:
        selected = BusinessQueries(engine, reads=reads)._selected_accounting(
            reads.connection, {"future"}, "2026-01",
        )["through_period"]
        assert selected["voucher_events"] == selected["state_results"] == []
        assert reads._metadata == reads._verified_source_contents == {}


def test_report_no_publication_shortcut_cannot_hide_actual_current_voucher(engine):
    save(engine)
    publish(engine)
    damage(engine, "calculation_publication", "DELETE FROM calculation_publication",
           foreign_keys=False)
    with QueryReads.snapshot(engine) as reads:
        assert _has_open_publication(reads.connection, JANUARY)
        with pytest.raises(KernelError) as failure:
            _party_balance_nets(engine, reads.connection, JANUARY, source="open", reads=reads)
        assert failure.value.code == "content_integrity_failed"


def test_report_missing_preyear_publication_keeps_detailed_history_fallback(engine):
    save(engine, period="2025-12")
    publish(engine)
    damage(engine, "calculation_publication", "DELETE FROM calculation_publication",
           foreign_keys=False)
    with QueryReads.snapshot(engine) as reads:
        assert _party_balance_nets(
            engine, reads.connection, JANUARY, source="open", reads=reads,
        ) is None


def test_current_member_owner_batch_is_exact_and_unrelated_growth_keeps_body_scope(
    asset_engine, monkeypatch,
):
    engine, evidence = asset_engine
    activate(engine, evidence)
    subjects = {"activate-fixed-a", "activate-intangible-b"}
    owner_batches = []
    original = QueryReads.asset_members_many

    def capture(self, identifiers, *, _decoded_owners=None):
        identifiers = set(identifiers)
        owner_batches.append(identifiers)
        return original(self, identifiers, _decoded_owners=_decoded_owners)

    monkeypatch.setattr(QueryReads, "asset_members_many", capture)

    def selected():
        with QueryReads.snapshot(engine) as reads:
            result = BusinessQueries(engine, reads=reads)._current_accounting_heads(
                reads.connection, subjects, cutoff=JANUARY, include_members=True,
            )
            assert {row["subject_id"] for row in result} == subjects
            assert len(owner_batches[-1]) == 1
            return tuple(sorted(row["id"] for row in result))

    before_work, expected = measure_work(engine, selected)
    assert len(owner_batches) == 1
    for period in ("2026-01", "2026-02"):
        month(engine, evidence, period, "consume-" + period)
    after_work, actual = measure_work(engine, selected)
    assert actual == expected and len(owner_batches) == 2
    for counter in (
        "returned_rows", "returned_value_bytes", "calculation_result_rows_loaded",
        "calculation_result_bytes_loaded", "calculation_result_json_decodes", "sql_calls",
    ):
        assert after_work["counters"][counter] == before_work["counters"][counter]
    assert (after_work["counters"]["sqlite_vm_steps"]
            <= before_work["counters"]["sqlite_vm_steps"] + 200)


def test_current_member_missing_own_adoption_fails_before_batch_cache(asset_engine):
    engine, evidence = asset_engine
    activate(engine, evidence)
    damage(engine, "asset_batch_member",
           "DELETE FROM asset_batch_member WHERE member_subject_id='activate-intangible-b'",
           foreign_keys=False)
    with QueryReads.snapshot(engine) as reads:
        with pytest.raises(KernelError) as failure:
            BusinessQueries(engine, reads=reads)._current_accounting_heads(
                reads.connection, {"activate-fixed-a", "activate-intangible-b"},
                include_members=True,
            )
        assert failure.value.code == "content_integrity_failed"
        assert reads._asset_members == reads._metadata == {}
