"""Exact group duplicate selectors retain complete frozen material dependencies."""

import json

import pytest
from stage9_book import MixedBook
from test_material_watch_future_links import _classify_expense, _expense, _frozen, _link

from ai_accounting.kernel import material_watch
from ai_accounting.kernel.backup import create_portable, verify_portable
from ai_accounting.kernel.close_storage import decode_close
from ai_accounting.kernel.integrity import verify_integrity
from ai_accounting.kernel.types import YearMonth


@pytest.mark.parametrize("change", ["group", "source"])
def test_group_duplicate_freeze_and_current_followup(tmp_path, change):
    book = MixedBook(tmp_path / "group-duplicate", employees=1, businesses=26)
    book.add_month(0, close=False)
    sources, evidence = {}, []
    specifications = {}
    for name, amount in (("original-pool", "30.00"), ("duplicate-copy", "20.00")):
        proof = book.evidence(
            f"item,amount,period\nconfirmed,{amount},2016-01\n".encode(), name + ".csv"
        )
        specification = {
            "format": "csv",
            "columns": [
                {"column": "A", "role": "context"},
                {"column": "B", "role": "amount"},
                {"column": "C", "role": "recognition_period"},
            ],
        }
        specifications[name] = specification
        sources[name] = book.materials.receive(
            name,
            dict(
                period="2016-01",
                evidence_digest=proof,
                category="transactions",
                purpose="business",
                specification=specification,
            ),
            evidence=(proof, book.proof),
            expected_revision=0,
            request_id=book.request(name),
        )
        evidence.append(proof)
    for name, amount in (("pool-first", 1000), ("pool-second", 2000)):
        _expense(book, name, "2016-01", amount)
        _classify_expense(book, name, "2016-01")
    links = [
        _link(book, name, "2016-01", amount)
        for name, amount in (("pool-first", 1000), ("pool-second", 2000))
    ]
    group_data = dict(
        period="2016-01",
        source_id="original-pool",
        source_fact_id=sources["original-pool"]["fact_id"],
        members=[dict(location="CSV!B2", amount_fen=3000)],
        group_amount_fen=3000,
        links=links,
        joint_basis_confirmed=True,
        basis_evidence_digest=book.proof,
        basis_location="L1",
        reason="Confirmed complete synthetic pool.",
    )
    group = book.materials.resolve_group(
        "confirmed-group",
        group_data,
        evidence=(evidence[0], book.proof),
        expected_revision=0,
        request_id=book.request("group"),
    )
    selector = book.materials.resolve(
        "confirmed-copy",
        dict(
            period="2016-01",
            source_id="duplicate-copy",
            source_fact_id=sources["duplicate-copy"]["fact_id"],
            location="CSV!B2",
            treatment="duplicate",
            recognition_period="2016-01",
            links=[links[1]],
            duplicate_source_id="original-pool",
            duplicate_location="CSV!B2",
            reason="The copy documents the second complete pool allocation.",
        ),
        evidence=(evidence[1], book.proof),
        expected_revision=0,
        request_id=book.request("selector"),
    )
    january = YearMonth("2016-01").ordinal
    with book.engine.store.connection(read_only=True) as connection:
        inventory = connection.execute(
            "SELECT id FROM material_revision WHERE period=? AND category='transactions' "
            "ORDER BY id DESC LIMIT 1",
            (january,),
        ).fetchone()[0]
        originals = [
            row[0].hex()
            for row in connection.execute(
                "SELECT evidence_digest FROM material_item WHERE inventory_id=?",
                (inventory,),
            )
        ]
    book.periods.inventory(
        "2016-01",
        "transactions",
        evidence=[*originals, *evidence],
        expected=len(originals) + 2,
        no_business=False,
        confirmation_evidence=book.proof,
        request_id=book.request("inventory"),
    )
    with book.engine.store.connection(read_only=True) as connection:
        readiness = book.periods.check_readiness(connection, "2016-01")
        assert not readiness["issues"], readiness["issues"]
    book.close_last_month()
    frozen = _frozen(book)
    with book.engine.store.connection(read_only=True) as connection:
        row = connection.execute("SELECT * FROM period_close WHERE period=?", (january,)).fetchone()
        manifest = decode_close(connection, row)
        coverage = manifest["material_coverage"]
        assert group["fact_id"] in coverage["group_versions"]
        assert selector["fact_id"] in coverage["resolution_versions"]
        stored = connection.execute(
            "SELECT subject_id,amount_fen FROM fact_material_resolution_v2_links "
            "WHERE revision_id=?",
            (selector["fact_id"],),
        ).fetchall()
        assert [tuple(item) for item in stored] == [("pool-second", 2000)]
        heads = material_watch.heads_at(
            connection, material_watch._stored_root(connection, january)["highwater"]
        )
        _, reverse = material_watch._directory(connection, january, manifest, historic_heads=heads)
        assert "original-pool" in reverse["business:pool-first"]
        assert "original-pool" in reverse["business:duplicate-copy"]
        assert "duplicate-copy" in reverse["business:original-pool"]
        assert verify_integrity(book.engine, connection)["status"] == "verified"
    archive = create_portable(
        book.engine.store.path, tmp_path / "portable", _bundle=book.engine.store.bundle
    )
    assert (
        verify_portable(archive["path"], _bundle=book.engine.store.bundle)["latest_closed_period"]
        == "2016-01"
    )
    if change == "group":
        book.materials.resolve_group(
            "confirmed-group",
            group_data | {"reason": "New recorded owner review."},
            evidence=(evidence[0], book.proof),
            expected_revision=1,
            request_id=book.request("group-revision"),
        )
    else:
        specification = json.loads(json.dumps(specifications["original-pool"]))
        specification["columns"][1]["funds_direction"] = "outflow"
        book.materials.receive(
            "original-pool",
            dict(
                period="2016-01",
                evidence_digest=evidence[0],
                category="transactions",
                purpose="business",
                specification=specification,
            ),
            evidence=(evidence[0], book.proof),
            expected_revision=1,
            request_id=book.request("source-revision"),
        )
    checked = book.materials.check("2016-02")
    if change == "group":
        # An audit-only reason revision does not invalidate identical adoption.
        assert not checked["issues"], checked["issues"]
    else:
        assert {"material_duplicate_basis", "material_item_unresolved"} <= {
            issue["code"] for issue in checked["issues"]
        }, checked["issues"]
    assert _frozen(book) == frozen
