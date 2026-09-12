"""Frozen bank source proof is local, exact, and separate from state adoption."""

from pathlib import Path

import pytest
import test_banking as banking

import ai_accounting
from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard, _Snapshot
from ai_accounting.kernel.dashboard_funds import FundsRead
from ai_accounting.kernel.types import YearMonth

bank_book = banking.book
MONTH = "2026-09"


def closed_banks(book):
    engine, save, publish, proof = book
    statement_data = None
    for bank, amount in (("bank-a", 1000), ("bank-b", 500)):
        banking.opening(save, publish, bank)
        banking.funding(save, publish, subject=f"funding-{bank}", bank=bank, amount=amount)
        entries = (
            [banking.entry("first", amount=400), banking.entry("second", amount=600)]
            if bank == "bank-a"
            else [banking.entry("third", amount=500)]
        )
        _, data = banking.statement(save, publish, entries, subject=f"statement-{bank}", bank=bank)
        banking.reconciliation(
            save,
            publish,
            [banking.match(row["reference"], source=f"funding-{bank}") for row in entries],
            subject=f"reconciliation-{bank}",
            statement_id=f"statement-{bank}",
            bank=bank,
        )
        if bank == "bank-a":
            statement_data = data
    banking.close_month(
        banking.inventories(engine, proof, MONTH, {"bank", "transactions"}), proof, MONTH
    )
    return engine, save, statement_data


def test_frozen_reconciliation_proves_source_not_independent_adoption_and_pages(
    bank_book, monkeypatch
):
    candidate = Path(__file__).resolve().parents[2]
    assert Path(ai_accounting.__file__).resolve().is_relative_to(candidate / "src")
    engine, save, statement_data = closed_banks(bank_book)
    selection = BusinessQueries(engine).business_status(
        "statement-bank-a", MONTH, as_of="2026-10-01"
    )["selected_accounting"]["through_period"]
    assert selection["state_results"] == []
    unresolved = selection["unestablished_state_selections"][0]
    assert unresolved["candidates"][0]["trace_only"] is True

    # Observe only the bank projection, after the shared selector has run. An
    # unrelated historical result in through_period must not request its edges.
    with engine.store.connection(read_only=True) as connection:
        snap = _Snapshot(engine, connection, MONTH)
        read = FundsRead(snap)
        roots = {
            item["id"] for item in read.states.values() if item["kind"] == "bank_reconciliation"
        }
        expected_parents = {parent for ident in roots for parent in snap.reads.parents(ident)}
        unrelated = next(item for item in read.states.values() if item["id"] in roots) | {
            "id": "unrelated-history",
            "subject_id": "unrelated-history",
            "period": "2026-08",
        }
        read.states[unrelated["id"]] = unrelated
        prime_calls, metadata_calls = [], []
        original_prime, original_metadata = snap.reads.prime_parents, snap.reads.metadata

        def observe_prime(identifiers):
            identifiers = set(identifiers)
            prime_calls.append((identifiers, identifiers - snap.reads._parents.keys()))
            return original_prime(identifiers)

        def observe_metadata(identifiers, **kwargs):
            identifiers = set(identifiers)
            metadata_calls.append(identifiers)
            return original_metadata(identifiers, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(snap.reads, "prime_parents", observe_prime)
            patch.setattr(snap.reads, "metadata", observe_metadata)
            summary = read.bank_summary()
        assert summary["matched_count"] == 3 and summary["coverage_state"] == "complete"
        assert prime_calls[0][0] == roots
        assert all(identifiers <= roots for identifiers, _ in prime_calls)
        assert all(not missing for _, missing in prime_calls[1:])
        assert metadata_calls == [expected_parents]

    dashboard = Dashboard(engine)
    first = dashboard.funds(MONTH, limit=1)
    bank = first["data"]["bank_statement"]
    assert (bank["transaction_count"], bank["matched_count"], bank["needs_review_count"]) == (
        3,
        3,
        0,
    )
    assert bank["coverage_state"] == "complete" and bank["missing_account_count"] == 0
    assert len(bank["rows"]) == 1 and bank["page"]["has_more"]
    check = bank["rows"][0]["source_check"]
    assert check["statement_confirmed"] and check["reconciliation_valid"]
    assert check["selection_source"] == "close_manifest"
    assert check["selection_proof"] == {"basis": "manifest_lineage_root"}
    assert check["proof_method"] == "frozen_reconciliation_direct_statement"
    assert check["statement_calculation_id"] == unresolved["candidates"][0]["calculation_id"]
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT json_extract(outcome,'$.values.matched_count') FROM calculation WHERE id=?",
                (check["reconciliation_calculation_id"],),
            ).fetchone()[0]
            == 1
        )  # Two original statement rows legitimately share one funds source.
    next_page = dashboard.funds(
        MONTH,
        limit=1,
        after_statement=bank["page"]["next_cursor"],
        expected_version=first["snapshot_version"],
    )["data"]["bank_statement"]
    assert next_page["matched_count"] == 3
    assert next_page["rows"][0]["id"] != bank["rows"][0]["id"]
    assert next_page["rows"][0]["source_check"] == check

    # A later current fact/pending cannot replace the frozen statement or undo
    # its historical matches; it does invalidate an old pagination version.
    changed = statement_data | {
        "entries": [row | {"description": "later extraction"} for row in statement_data["entries"]]
    }
    save("bank_statement", "statement-bank-a", changed, revision=1)
    with pytest.raises(KernelError) as failure:
        dashboard.funds(MONTH, expected_version=first["snapshot_version"])
    assert failure.value.code == "dashboard_snapshot_changed"
    reloaded = dashboard.funds(MONTH)["data"]["bank_statement"]
    assert reloaded["matched_count"] == 3 and reloaded["coverage_state"] == "complete"
    assert all(row["memo"] != "later extraction" for row in reloaded["rows"])
    assert dashboard.brief(MONTH)["data"]["cash"]["missing_account_count"] == 0


def test_incomplete_or_conflicting_bank_proof_does_not_spread_to_other_accounts(bank_book):
    engine, _, _ = closed_banks(bank_book)
    # These are malformed/incomplete read-contract inputs, not edits to the
    # immutable fixture DB. Each single missing proof must keep bank B matched.
    cases = (
        "missing_edge",
        "two_statement_parents",
        "different_close_members",
        "unadopted_reconciliation",
        "different_fact",
        "different_period",
        "independently_selected_conflict",
        "independent_source_missing_edge",
        "not_balanced",
        "duplicate_statement",
        "duplicate_reconciliation",
        "account_mismatch",
    )
    for case in cases:
        with engine.store.connection(read_only=True) as connection:
            snap = _Snapshot(engine, connection, MONTH)
            read = FundsRead(snap)
            by_subject = {item["subject_id"]: item for item in read.states.values()}
            rec = by_subject["reconciliation-bank-a"]
            parents = snap.reads.metadata(snap.reads.parents(rec["id"]))
            source = next(item for item in parents.values() if item["kind"] == "bank_statement")
            rec_b = by_subject["reconciliation-bank-b"]
            source_b = next(
                item
                for item in snap.reads.metadata(snap.reads.parents(rec_b["id"])).values()
                if item["kind"] == "bank_statement"
            )
            if case in {
                "not_balanced",
                "duplicate_statement",
                "duplicate_reconciliation",
                "account_mismatch",
            }:

                class ContractConnection:
                    def execute(
                        self, sql, parameters, *, case=case, rec=rec, connection=connection
                    ):
                        rows = connection.execute(sql, parameters)
                        if case == "not_balanced" and "$.values.balanced" in sql:
                            return [row for row in rows if row[0] != rec["id"]]
                        statement_query = "JOIN fact_bank_statement s " in sql
                        reconciliation_query = "JOIN fact_bank_reconciliation r " in sql
                        if (
                            case == "duplicate_statement"
                            and statement_query
                            or case in {"duplicate_reconciliation", "account_mismatch"}
                            and reconciliation_query
                        ):
                            values = [dict(row) for row in rows]
                            index = next(
                                i
                                for i, row in enumerate(values)
                                if row["bank_account_id"] == "bank-a"
                            )
                            if case == "account_mismatch":
                                values[index] = values[index] | {
                                    "bank_account_id": "different-bank"
                                }
                            else:
                                values.append(values[index] | {"subject_id": "second-same-account"})
                            return values
                        return rows

                read.connection = ContractConnection()
            if case in {"missing_edge", "independent_source_missing_edge"}:
                snap.reads._parents[rec["id"]] = tuple(
                    ident for ident in snap.reads.parents(rec["id"]) if ident != source["id"]
                )
            elif case == "two_statement_parents":
                snap.reads._parents[rec["id"]] += (source_b["id"],)
            elif case == "different_close_members":
                month = YearMonth(MONTH).ordinal
                snap.closes.cache[month] = snap.close | {
                    "calculations": [
                        ident for ident in snap.close["calculations"] if ident != source["id"]
                    ]
                }
            elif case == "unadopted_reconciliation":
                del read.states[rec["id"]]
                del read.state_selections[rec["id"]]
            elif case == "different_fact":
                snap.reads._metadata[source["id"]] = source | {"fact_id": source_b["fact_id"]}
            elif case == "different_period":
                snap.reads._metadata[source["id"]] = source | {"period": "2026-08"}
            if case in {"independently_selected_conflict", "independent_source_missing_edge"}:
                ident = (
                    source_b["id"] if case == "independently_selected_conflict" else source["id"]
                )
                read.states[ident] = source | {"id": ident}
                read.state_selections[ident] = BusinessQueries._state_metadata(
                    read.states[ident], "close_manifest", {"basis": "manifest_lineage_root"}
                )
            summary = read.bank_summary()
            assert summary["matched_count"] == 1, case
            assert summary["needs_review_count"] == (4 if case == "duplicate_statement" else 2), (
                case
            )
            assert summary["unmatched_count"] == 0, case
            assert summary["missing_account_count"] == 0, case
            if case not in {"independently_selected_conflict", "independent_source_missing_edge"}:
                assert summary["coverage_state"] == "partial", case
            else:
                # Independent source coverage can be known even though the
                # reconciliation's exact source cannot establish matching.
                assert summary["coverage_state"] == "complete", case
            check = read.bank_source_checks[source["fact_id"]]
            assert not check["reconciliation_valid"], case
            assert check["state"] in {"unestablished", "conflict"}, case
