"""Opening packages and every exact member are directly adopted at the first close."""

import json
from copy import deepcopy

import pytest
from test_opening_continuation import _close_without_current_business
from test_opening_continuation import book as _opening_book

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.integrity import _check_closes, _check_sources
from ai_accounting.kernel.types import canonical, digest

opening_book = _opening_book


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
        source = deepcopy(_check_sources(engine, connection))
        manifest = json.loads(connection.execute("SELECT manifest FROM period_close").fetchone()[0])
        yield engine, connection, source, manifest


def test_complete_package_and_details_are_explicit_direct_results(frozen_opening):
    engine, connection, source, manifest = frozen_opening
    package = next(
        item for item in source["calculations"].values() if item["kind"] == "opening_package"
    )
    assert manifest["opening_calculation_id"] == package["id"]
    assert len(manifest["adopted_results"]) == 3
    assert (
        len([item for item in manifest["adopted_results"] if item["role"] == "opening_basis"]) == 1
    )
    assert _check_closes(engine, connection, source) == (1, [])


@pytest.mark.parametrize(
    "problem",
    [
        "wrong_member_fact",
        "missing_member",
        "member_counts",
        "detail_values",
        "detail_balance",
        "detail_lines",
        "opening_lines",
        "wrong_package_dependency",
    ],
)
def test_incomplete_or_mismatched_frozen_package_is_error(frozen_opening, problem):
    engine, connection, source, _ = frozen_opening
    package = next(
        item for item in source["calculations"].values() if item["kind"] == "opening_package"
    )
    detail = next(
        item for item in source["calculations"].values() if item["kind"] == "opening_cash"
    )
    if problem == "wrong_member_fact":
        package["decoded"]["values"]["members"][0]["fact_id"] = "other-exact-version"
    elif problem == "missing_member":
        package["decoded"]["values"]["members"].pop()
    elif problem == "member_counts":
        source["facts"][package["fact_id"]]["data"]["counts"]["cash"] = 2
        package["decoded"]["values"]["counts"]["cash"] = 2
    elif problem == "detail_values":
        detail["decoded"]["values"]["opening_fen"] += 1
    elif problem == "detail_balance":
        detail["decoded"]["balances"].append({"key": "cash", "category": "cash", "amount": 1})
    elif problem == "detail_lines":
        detail["decoded"]["lines"].append({"account": "1001", "debit": 1, "credit": 0})
    elif problem == "opening_lines":
        package["decoded"]["opening_lines"][0]["debit"] += 1
    else:
        source["dependencies"][detail["id"]] = set()
    with pytest.raises(KernelError) as failure:
        _check_closes(engine, connection, source)
    assert failure.value.code == "content_integrity_failed"


@pytest.mark.parametrize("same_amount", [False, True])
def test_real_old_and_new_versions_require_exact_direct_adoption(opening_book, same_amount):
    from test_integrity_content import damage, verify

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
        old = dict(connection.execute("SELECT kind,id FROM calculation"))
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
    assert verify(engine)["status"] == "verified"
    with engine.store.connection(read_only=True) as connection:
        manifest = json.loads(connection.execute("SELECT manifest FROM period_close").fetchone()[0])
        assert manifest["opening_calculation_id"] != old["opening_package"]
        previous = connection.execute(
            "SELECT c.*,p.id publication_id FROM calculation c JOIN calculation_publication p "
            "ON p.calculation_id=c.id WHERE c.id=?",
            (old["opening_package"],),
        ).fetchone()
        replacement = next(
            item for item in manifest["adopted_results"] if item["role"] == "opening_basis"
        )
        replacement.update(
            calculation_id=previous["id"],
            publication_id=previous["publication_id"],
            fact_id=previous["fact_id"],
            result_digest=previous["digest"].hex(),
        )
        manifest["opening_calculation_id"] = previous["id"]
    damage(
        engine,
        "period_close",
        "UPDATE period_close SET manifest=?,digest=?",
        (canonical(manifest), digest(manifest)),
    )
    with pytest.raises(KernelError) as failure:
        verify(engine, include_indexes=False)
    assert failure.value.details["reason"] == "direct_adoption_set_mismatch"
