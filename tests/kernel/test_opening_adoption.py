"""Domain proof over real typed immutable opening output, with detached bad sources."""

import json
from copy import deepcopy

import pytest
from test_opening_continuation import _close_without_current_business
from test_opening_continuation import book as _opening_book

from ai_accounting.kernel.opening_adoption import prove_opening_adoptions
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.types import YearMonth

opening_book = _opening_book


class DetachedReads:
    """Corrupt only the returned synthetic source under test, never the real store."""

    def __init__(self, reads, ids):
        self.reads = reads
        self.calcs = deepcopy(reads.calculations(ids))
        self.fact_overrides = {}

    def calculations(self, ids):
        return {ident: self.calcs[ident] for ident in ids}

    def facts(self, ids):
        return deepcopy(self.reads.facts(ids)) | self.fact_overrides

    def __getattr__(self, key):
        return getattr(self.reads, key)


@pytest.fixture
def frozen_opening(opening_book):
    engine, _, _, package, proof = opening_book
    package(
        [
            ("opening_cash", "cash", {"cash_account_id": "cash", "balance_fen": 100}),
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
    _close_without_current_business(engine, "2026-01", proof)
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        reads = QueryReads(engine, connection)
        close_period = YearMonth("2026-01").ordinal
        manifest = json.loads(reads.close_rows(periods=(close_period,))[0]["manifest"])
        ids = set(manifest["calculations"])
        metadata = reads.metadata(ids)
        upstream = {parent for ident in ids for parent in reads.parents(ident)}
        independent = {ident: {"basis": "manifest_lineage_root"} for ident in ids - upstream}
        detached = DetachedReads(reads, ids)
        yield (
            connection,
            detached,
            dict(
                close_period=close_period,
                manifest=manifest,
                metadata=metadata,
                independent_proofs=independent,
            ),
        )


def test_complete_package_dependency_is_proven_by_exact_opening_member(frozen_opening):
    connection, reads, args = frozen_opening
    result = prove_opening_adoptions(connection, reads, **args)
    assert len(result) == 1
    proof = next(iter(result.values()))
    assert proof["basis"] == "manifest_opening_member_adoption"
    assert proof["close_period"] == "2026-01"
    assert len(proof["anchors"]) == len(proof["member_fact_ids"]) == 2
    assert {item["selection_proof"]["basis"] for item in proof["anchors"]} == {
        "manifest_lineage_root"
    }


@pytest.mark.parametrize(
    "problem",
    [
        "no_anchor",
        "wrong_member_fact",
        "missing_member",
        "member_counts",
        "detail_values",
        "detail_balance",
        "detail_lines",
        "opening_lines",
        "trial_balance",
        "wrong_package_dependency",
    ],
)
def test_incomplete_or_mismatched_frozen_domain_contract_stays_unknown(frozen_opening, problem):
    connection, reads, args = frozen_opening
    package = next(item for item in reads.calcs.values() if item["kind"] == "opening_package")
    detail = next(item for item in reads.calcs.values() if item["kind"] == "opening_cash")
    if problem == "no_anchor":
        args["independent_proofs"] = {}
    elif problem == "wrong_member_fact":
        package["outcome"]["values"]["members"][0]["fact_id"] = "other-exact-version"
    elif problem == "missing_member":
        package["outcome"]["values"]["members"].pop()
    elif problem == "member_counts":
        package["fact_data"]["counts"]["cash"] = 2
        package["outcome"]["values"]["counts"]["cash"] = 2
    elif problem == "detail_values":
        detail["outcome"]["values"]["opening_fen"] += 1
    elif problem == "detail_balance":
        detail["outcome"]["balances"].append({"key": "cash", "category": "cash", "amount": 1})
    elif problem == "detail_lines":
        detail["outcome"]["lines"].append({"account": "1001", "debit": 1, "credit": 0})
    elif problem == "opening_lines":
        package["outcome"]["opening_lines"][0]["debit"] += 1
    elif problem == "trial_balance":
        args["manifest"]["trial_balance"][0]["debit"] += 1
    else:
        reads.parents = lambda ident: ()
    assert prove_opening_adoptions(connection, reads, **args) == {}


def test_unrelated_old_package_cannot_override_new_exact_member(frozen_opening):
    connection, reads, args = frozen_opening
    package = next(item for item in reads.calcs.values() if item["kind"] == "opening_package")
    # The old package's amount happens to agree with the close, but the adopted
    # member names a different exact fact version. Current/amount equality cannot fix it.
    adopted = next(item for item in reads.calcs.values() if item["kind"] == "opening_cash")
    adopted["fact_id"] = "newer-member-exact-fact"
    assert package["outcome"]["values"]["debit_fen"] == 100
    assert prove_opening_adoptions(connection, reads, **args) == {}


@pytest.mark.parametrize("same_amount", [False, True])
def test_real_old_and_new_versions_require_exact_member_adoption(opening_book, same_amount):
    engine, _, publish, package, proof = opening_book
    members = [
        ("opening_cash", "cash", {"cash_account_id": "old-cash", "balance_fen": 100}),
        (
            "opening_equity",
            "equity",
            {"equity_kind": "paid_in_capital", "balance_fen": 100, "holder_or_basis_id": "owner"},
        ),
    ]
    package(members)
    with engine.store.connection(read_only=True) as connection:
        old = {
            row["kind"]: row["id"] for row in connection.execute("SELECT id,kind FROM calculation")
        }
    amount = 100 if same_amount else 200
    for kind, subject, data in members:
        engine.amend_fact(
            kind,
            subject,
            {
                "period": "2026-01",
                "package_id": "opening",
                **data,
                "balance_fen": amount,
                **({"cash_account_id": "new-cash"} if kind == "opening_cash" else {}),
            },
            evidence=(proof,),
            expected_revision=1,
            recording_error_confirmed=True,
            request_id="correct-" + subject,
        )
    publish("opening", "cash", "equity")
    _close_without_current_business(engine, "2026-01", proof)
    with engine.store.connection(read_only=True) as connection:
        reads = QueryReads(engine, connection)
        ordinal = YearMonth("2026-01").ordinal
        manifest = json.loads(reads.close_rows(periods=(ordinal,))[0]["manifest"])
        current_ids = set(manifest["calculations"])
        # A historical package can enter the complete source set as a dependency.
        # Its actual old calculation/fact/dependency records remain untouched.
        manifest["calculations"] += list(old.values())
        metadata = reads.metadata(manifest["calculations"])
        upstream = {parent for ident in current_ids for parent in reads.parents(ident)}
        independent = {
            ident: {"basis": "manifest_lineage_root"} for ident in current_ids - upstream
        }
        args = dict(
            close_period=ordinal,
            manifest=manifest,
            metadata=metadata,
            independent_proofs=independent,
        )
        proven = prove_opening_adoptions(connection, reads, **args)
        assert len(proven) == 1 and old["opening_package"] not in proven
        # If incompatible old and new member versions are both asserted adopted,
        # neither package can claim a unique atomic boundary, even at equal totals.
        independent[old["opening_cash"]] = {"basis": "manifest_lineage_root"}
        assert prove_opening_adoptions(connection, reads, **args) == {}
