"""Candidate identity work against real, published synthetic calculation rows."""

from dataclasses import asdict

import pytest
from test_banking import book as book
from test_banking import funding, opening
from test_integrity_content import damage

from ai_accounting.kernel import dashboard_model_sources as sources
from ai_accounting.kernel import integrity
from ai_accounting.kernel.content_history_context import month_type, read_type
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.integrity import _outcome
from ai_accounting.kernel.types import digest


@pytest.fixture
def published_sources(book):
    engine, save, publish, _ = book
    opening(save, publish)
    funding(save, publish, subject="first-funding", amount=17)
    funding(save, publish, subject="second-funding", amount=29)
    with engine.store.connection(read_only=True) as connection:
        rows = {
            row["subject_id"]: dict(row)
            for row in connection.execute("SELECT * FROM calculation")
        }
        assert set(rows) == {"opening-bank-a", "first-funding", "second-funding"}
        assert connection.execute("SELECT count(*) FROM calculation_publication").fetchone()[0] == 3
    return engine, rows


def identity_candidates(connection, row):
    """Independently reproduce the old asdict identity bytes, not the new helper."""
    ident = row["id"]
    dependencies = {
        item[0] for item in connection.execute(
            "SELECT upstream_id FROM dependency_calculation WHERE calculation_id=?", (ident,)
        )
    }
    facts = {
        item[0] for item in connection.execute(
            "SELECT fact_id FROM dependency_fact WHERE calculation_id=?", (ident,)
        )
    }
    reads = [
        read_type()(
            item["source"], item["kind"], item["scope_key"],
            None if item["before_period"] == 119988
            else month_type().from_ordinal(item["before_period"]),
        )
        for item in connection.execute(
            "SELECT * FROM dependency_scope WHERE calculation_id=?", (ident,)
        )
    ]
    versions = dependencies | (facts - {row["fact_id"]})
    payload = {
        "fact": row["fact_id"], "outcome": _outcome(row),
        "reads": [asdict(read) for read in sorted(reads, key=repr)],
        "program": row["program_version"],
    }
    return tuple(
        "c_" + digest({**payload, "versions": sorted(values)}).hex()
        for values in (versions, versions | {row["fact_id"]})
    )


def count_candidates(monkeypatch):
    observed = []
    original = sources.calculation_identity_digest

    def counted(**arguments):
        observed.append({
            "fact_id": arguments["fact_id"], "versions": tuple(arguments["versions"]),
        })
        return original(**arguments)

    monkeypatch.setattr(sources, "calculation_identity_digest", counted)
    return observed


def test_published_first_format_hashes_once_per_unique_source(published_sources, monkeypatch):
    engine, rows = published_sources
    selected = [rows[name] for name in ("first-funding", "second-funding")]
    observed = count_candidates(monkeypatch)
    with engine.store.connection(read_only=True) as connection:
        for row in selected:
            first, second = identity_candidates(connection, row)
            assert row["id"] == first != second
        ids = [selected[0]["id"], selected[1]["id"], selected[0]["id"]]
        result = sources.verify_model_sources(engine, connection, calculation_ids=ids)
    assert set(result) == set(ids)
    assert len(observed) == 2
    assert {item["fact_id"] for item in observed} == {row["fact_id"] for row in selected}


def test_real_self_read_uses_second_allowed_format(published_sources, monkeypatch):
    engine, rows = published_sources
    row = rows["opening-bank-a"]
    observed = count_candidates(monkeypatch)
    with engine.store.connection(read_only=True) as connection:
        first, second = identity_candidates(connection, row)
        assert row["id"] == second != first
        result = sources.verify_model_sources(engine, connection, calculation_ids=[row["id"]])
    assert set(result) == {row["id"]}
    assert len(observed) == 2
    assert row["fact_id"] not in observed[0]["versions"]
    assert set(observed[1]["versions"]) == set(observed[0]["versions"]) | {row["fact_id"]}


def test_identity_mismatch_rejects_after_both_candidates(published_sources, monkeypatch):
    engine, rows = published_sources
    row = rows["first-funding"]
    damage(
        engine, "calculation",
        "UPDATE calculation SET program_version=program_version||'-bad' WHERE id=?", (row["id"],),
    )
    observed = count_candidates(monkeypatch)
    with engine.store.connection(read_only=True) as connection:
        with pytest.raises(KernelError, match="看板命中来源") as failure:
            sources.verify_model_sources(engine, connection, calculation_ids=[row["id"]])
    assert failure.value.code == "content_integrity_failed"
    assert len(observed) == 2


@pytest.mark.parametrize(
    "subject, expected_candidates", (("first-funding", 1), ("opening-bank-a", 2)),
)
def test_real_outcome_is_encoded_once_for_both_identity_formats(
    published_sources, monkeypatch, subject, expected_candidates,
):
    engine, rows = published_sources
    calls = []
    original = integrity.canonical

    def counted(value):
        if isinstance(value, dict) and {"values", "lines", "balances"} <= value.keys():
            calls.append(1)
        return original(value)

    monkeypatch.setattr(integrity, "canonical", counted)
    candidates = count_candidates(monkeypatch)
    with engine.store.connection(read_only=True) as connection:
        result = sources.verify_model_sources(
            engine, connection, calculation_ids=[rows[subject]["id"]] * 2,
        )
    assert set(result) == {rows[subject]["id"]}
    assert calls == [1]
    assert len(candidates) == expected_candidates


@pytest.mark.parametrize("kind", ("outcome", "fact", "missing"))
def test_duplicate_source_selection_does_not_hide_invalid_member(
    published_sources, monkeypatch, kind,
):
    engine, rows = published_sources
    good, bad = rows["first-funding"], rows["second-funding"]
    ids = [good["id"], good["id"], bad["id"], bad["id"]]
    if kind == "outcome":
        damage(
            engine, "calculation",
            "UPDATE calculation SET outcome=json_set(outcome,'$.values.amount',30) WHERE id=?",
            (bad["id"],),
        )
    elif kind == "fact":
        damage(
            engine, "fact_funding", "UPDATE fact_funding SET amount_fen=30 WHERE revision_id=?",
            (bad["fact_id"],),
        )
    else:
        ids.extend(["c_" + "0" * 64] * 2)
    count_candidates(monkeypatch)
    normalized = {}
    with engine.store.connection(read_only=True) as connection:
        with pytest.raises(KernelError) as failure:
            sources.verify_model_sources(
                engine, connection, calculation_ids=ids, normalized_lines=normalized,
            )
    assert failure.value.code == "content_integrity_failed"
    assert normalized == {}


def test_model_physical_comparison_reuses_only_its_local_normalized_sources(
    published_sources, monkeypatch,
):
    engine, rows = published_sources
    selected = {rows[name]["id"] for name in ("first-funding", "second-funding")}
    calls = []
    original = integrity._lines

    def counted(lines, component, ident, *, opening=False):
        calls.append((component, ident, opening))
        return original(lines, component, ident, opening=opening)

    monkeypatch.setattr(integrity, "_lines", counted)
    for request in range(2):
        with engine.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            normalized = {}
            outcomes = sources.verify_model_sources(
                engine, connection, calculation_ids=[*selected, *selected],
                normalized_lines=normalized,
            )
            versions = {row[0] for row in connection.execute(
                "SELECT id FROM voucher_version WHERE calculation_id IN (?,?)", tuple(selected),
            )}
            physical = sources.verify_model_voucher_lines(
                connection, versions, outcomes, normalized_lines=normalized,
            )
            assert set(physical) == versions and len(versions) == 2
            assert set(normalized) == selected
            assert {item["total"] for item in physical.values()} == {17, 29}
        for ident in selected:
            assert calls.count(("calculation", ident, False)) == request + 1
            assert calls.count(("calculation", ident, True)) == request + 1
        for ident in versions:
            assert calls.count(("voucher", ident, False)) == request + 1


def test_full_sources_reuse_normalized_lines_without_changing_returned_rows(
    published_sources, monkeypatch,
):
    engine, _ = published_sources
    calls = []
    original = integrity._lines

    def counted(lines, component, ident, *, opening=False):
        calls.append((component, ident, opening))
        return original(lines, component, ident, opening=opening)

    monkeypatch.setattr(integrity, "_lines", counted)
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        result = integrity._check_sources(engine, connection)
    assert len(result["calculations"]) == 3 and len(result["vouchers"]) == 2
    for ident, row in result["calculations"].items():
        assert calls.count(("calculation", ident, False)) == 1
        assert calls.count(("calculation", ident, True)) == 1
        assert "normalized_lines" not in row
    for ident in result["vouchers"]:
        assert calls.count(("voucher", ident, False)) == 1


def test_failed_current_adoption_never_publishes_normalized_lines(published_sources):
    engine, rows = published_sources
    row = rows["first-funding"]
    damage(engine, "calculation_current", "DELETE FROM calculation_current WHERE calculation_id=?",
           (row["id"],))
    normalized = {}
    with engine.store.connection(read_only=True) as connection:
        with pytest.raises(KernelError) as failure:
            sources.verify_model_sources(
                engine, connection, calculation_ids=[row["id"]], normalized_lines=normalized,
            )
    assert failure.value.code == "content_integrity_failed"
    assert normalized == {}
