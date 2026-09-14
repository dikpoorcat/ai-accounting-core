"""Independent checks of posting ownership and the frozen asset adoption boundary."""

import json
import sqlite3
from contextlib import closing

import pytest

from ai_accounting.kernel.asset_batches import AssetBatches, frozen_members
from ai_accounting.kernel.contracts import FactVersion, KernelError
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.runtime import connect
from ai_accounting.kernel.service import default_registry
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.types import canonical, digest


@pytest.fixture
def batch_book(tmp_path):
    engine = Engine(
        Store.create(
            tmp_path / "batch.sqlite", default_registry(), "company", "taxpayer", "database"
        )
    )
    proof = engine.register_evidence(
        b"synthetic asset confirmation", "text/plain", "source", request_id="evidence"
    )["digest"]
    members = []
    for asset, amount in (("card-a", 1200), ("card-b", 2400)):
        engine.save_fact(
            "asset",
            asset,
            {
                "period": "2026-01",
                "asset_type": "fixed",
                "supplier_id": "supplier",
                "acquisition_date": "2026-01-02",
                "cost_fen": amount,
                "acquisition_basis": "direct_purchase",
            },
            evidence=(proof,),
            expected_revision=0,
            request_id="save-" + asset,
        )
        preview = engine.preview([asset])
        engine.confirm(
            [asset],
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id="publish-" + asset,
        )
        members.append(
            {
                "subject_id": "activate-" + asset,
                "expected_revision": 0,
                "data": {
                    "period": "2026-01",
                    "asset_id": asset,
                    "in_use_date": "2026-01-04",
                    "useful_life_months": 12,
                    "residual_fen": 0,
                    "benefit_area": "administration",
                    "rounding_policy": "floor_final_remainder",
                },
            }
        )
    batches = AssetBatches(engine)
    preview = batches.prepare_activation_batch(
        "activation-batch",
        "2026-01",
        members,
        evidence=(proof,),
        expected_revision=0,
    )
    batches.confirm_activation_batch(
        "activation-batch",
        "2026-01",
        members,
        evidence=(proof,),
        expected_revision=0,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="activate",
    )
    return engine, batches, proof


def current_owner(connection, kind):
    return connection.execute(
        "SELECT c.* FROM calculation_current a JOIN calculation c ON c.id=a.calculation_id "
        "WHERE c.kind=?",
        (kind,),
    ).fetchone()


def test_asset_owner_alone_posts_and_projection_rebuild_preserves_card_balances(batch_book):
    engine, batches, proof = batch_book
    preview = batches.prepare_consumption_month("2026-02", evidence=(proof,), expected_revision=0)
    result = batches.confirm_consumption_month(
        "2026-02",
        evidence=(proof,),
        expected_revision=0,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="consume",
    )
    assert (
        batches.confirm_consumption_month(
            "2026-02",
            evidence=(proof,),
            expected_revision=0,
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id="consume",
        )
        == result
    )
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM calculation_publication p JOIN calculation c "
                "ON c.id=p.calculation_id WHERE c.kind IN('asset_activation','asset_consumption')"
            ).fetchone()[0]
            == 0
        )
        for kind in ("asset_activation_batch", "asset_consumption_month"):
            owner = current_owner(connection, kind)
            assert len(frozen_members(connection, owner["id"])) == 2
            assert len(json.loads(owner["outcome"])["lines"]) == 4
        before = [tuple(row) for row in connection.execute("SELECT * FROM balance ORDER BY 1,2")]
        asset_balances = dict(
            connection.execute("SELECT balance_key,amount FROM balance WHERE category='asset'")
        )
        assert asset_balances == {"asset:card-a:carrying": 1100, "asset:card-b:carrying": 2200}
    assert len(engine.ledger("2026-02")) == 1
    engine.rebuild_projections(request_id="rebuild")
    with engine.store.connection(read_only=True) as connection:
        assert [
            tuple(row) for row in connection.execute("SELECT * FROM balance ORDER BY 1,2")
        ] == before


@pytest.mark.parametrize(
    "damage",
    [
        "digest",
        "order",
        "gap",
        "overlap",
        "out_of_bounds",
        "member_count",
        "unsealed_member",
        "unsealed_owner",
        "other_owner_subject",
    ],
)
def test_corrupt_frozen_adoption_never_reads_as_valid(batch_book, damage):
    engine, _, _ = batch_book
    with closing(connect(engine.store.path)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        owner = current_owner(connection, "asset_activation_batch")
        owner_id = owner["id"]
        assert len(frozen_members(connection, owner_id)) == 2
        # Deliberately damage only this temporary fixture. The helper must not
        # mistake self-consistent JSON or an existing calculation for adoption.
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='trigger' "
            "AND tbl_name IN('calculation','calculation_seal','asset_batch_member')"
        ).fetchall():
            connection.execute('DROP TRIGGER "' + row[0] + '"')
        with pytest.raises((KernelError, sqlite3.IntegrityError)):
            outcome = json.loads(owner["outcome"])
            if damage == "other_owner_subject":
                original = engine.store.fact(connection, owner["fact_id"])
                alternate = FactVersion(
                    "other-owner-fact",
                    "other-owner",
                    1,
                    original.fact,
                    original.evidence,
                )
                engine.store.write_fact(
                    connection,
                    alternate,
                    digest(alternate.fact.model_dump(mode="json")),
                )
                owner_id = "other-owner-calculation"
                connection.execute(
                    "INSERT INTO calculation "
                    "SELECT ?,?,?,kind,period,outcome,digest,program_version "
                    "FROM calculation WHERE id=?",
                    (owner_id, alternate.subject_id, alternate.id, owner["id"]),
                )
                connection.execute(
                    "INSERT INTO asset_batch_member SELECT ?,position,asset_id,member_subject_id,"
                    "member_fact_id,member_calculation_id,result_digest,summary,"
                    "line_start,line_count "
                    "FROM asset_batch_member WHERE owner_calculation_id=?",
                    (owner_id, owner["id"]),
                )
                connection.execute(
                    "INSERT INTO dependency_calculation SELECT ?,upstream_id "
                    "FROM dependency_calculation WHERE calculation_id=?",
                    (owner_id, owner["id"]),
                )
                connection.execute("INSERT INTO calculation_seal VALUES(?)", (owner_id,))
            elif damage.startswith("unsealed_"):
                target = (
                    owner_id
                    if damage == "unsealed_owner"
                    else outcome["values"]["members"][0]["member_calculation_id"]
                )
                connection.execute("DELETE FROM calculation_seal WHERE calculation_id=?", (target,))
            else:
                if damage == "digest":
                    outcome["values"]["membership_digest"] = "00" * 32
                elif damage == "member_count":
                    outcome["values"]["member_count"] = 3
                elif damage == "order":
                    outcome["values"]["members"].reverse()
                else:
                    start = {"gap": 4, "overlap": 1, "out_of_bounds": 99}[damage]
                    connection.execute(
                        "UPDATE asset_batch_member SET line_start=? "
                        "WHERE owner_calculation_id=? AND position=2",
                        (start, owner_id),
                    )
                    outcome["values"]["members"][1]["line_start"] = start
                    outcome["values"]["membership_digest"] = digest(
                        outcome["values"]["members"]
                    ).hex()
                connection.execute(
                    "UPDATE calculation SET outcome=?,digest=? WHERE id=?",
                    (canonical(outcome), digest(outcome), owner_id),
                )
            frozen_members(connection, owner_id)
        connection.rollback()
