"""Future duplicate dependencies are read proofs, never early business adoption."""

import pytest
from stage9_book import MixedBook
from test_material_watch_future_links import _classify_expense, _expense, _link
from test_materials import Company, csv_spec

from ai_accounting.kernel import frozen_material, material_watch, materials
from ai_accounting.kernel.backup import create_portable, verify_portable
from ai_accounting.kernel.close_storage import decode_close
from ai_accounting.kernel.integrity import verify_integrity
from ai_accounting.kernel.types import YearMonth, canonical, digest


def _receive(book, name, period, raw):
    evidence = book.evidence(raw, name + ".csv")
    saved = book.materials.receive(
        name,
        dict(
            period=period,
            evidence_digest=evidence,
            category="transactions",
            purpose="business",
            specification=csv_spec(),
        ),
        evidence=(evidence, book.proof),
        expected_revision=0,
        request_id=book.request(name),
    )
    return saved, evidence


def _resolve(book, source, evidence, name, period, row, **fields):
    return book.materials.resolve(
        name,
        dict(
            period=period,
            source_id=source["subject_id"],
            source_fact_id=source["fact_id"],
            location=f"CSV!B{row}",
            **fields,
        ),
        evidence=(evidence, book.proof),
        expected_revision=0,
        request_id=book.request(name),
    )


def _inventory(book, period, extra):
    with book.engine.store.connection(read_only=True) as connection:
        ident = connection.execute(
            "SELECT id FROM material_revision WHERE period=? AND category='transactions' "
            "ORDER BY id DESC LIMIT 1",
            (YearMonth(period).ordinal,),
        ).fetchone()[0]
        existing = [
            row[0].hex()
            for row in connection.execute(
                "SELECT evidence_digest FROM material_item WHERE inventory_id=?",
                (ident,),
            )
        ]
    originals = sorted(set(existing + extra))
    book.periods.inventory(
        period,
        "transactions",
        evidence=originals,
        expected=len(originals),
        no_business=False,
        confirmation_evidence=book.proof,
        request_id=book.request("inventory"),
    )


def _closed_history(book):
    with book.engine.store.connection(read_only=True) as connection:
        return tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT p.period,p.digest,p.manifest,r.rule_digest,w.content,w.root_digest "
                "FROM period_close p JOIN material_close_rule r ON r.period=p.period "
                "JOIN material_watch_root w ON w.period=p.period ORDER BY p.period"
            )
        )


def test_duplicate_dependency_retains_transitive_competing_sources_and_all_allocations(tmp_path):
    company = Company(tmp_path)
    first = company.expense("future-first", 1000, "2026-07")
    second = company.expense("future-second", 1100, "2026-07")
    january = company.expense("january", 1000)
    primary, _ = company.source(
        b"item,amount,period\nprimary,5.00,2026-07\n",
        subject="primary",
        period="2026-07",
    )
    company.resolve(
        primary,
        "CSV!B2",
        [first | {"amount_fen": 500}],
        subject="primary-row",
        period="2026-07",
        recognition_period="2026-07",
    )
    neighbour, _ = company.source(
        b"item,amount,period\nneighbour,13.00,2026-07\n",
        subject="neighbour",
        period="2026-07",
    )
    company.resolve(
        neighbour,
        "CSV!B2",
        [first | {"amount_fen": 500}, second | {"amount_fen": 800}],
        subject="neighbour-row",
        period="2026-07",
        recognition_period="2026-07",
    )
    distant, _ = company.source(
        b"item,amount,period\ndistant,3.00,2026-07\n",
        subject="distant",
        period="2026-07",
    )
    company.resolve(
        distant,
        "CSV!B2",
        [second | {"amount_fen": 300}],
        subject="distant-row",
        period="2026-07",
        recognition_period="2026-07",
    )
    mixed, _ = company.source(
        b"item,amount,period\njanuary,10.00,2026-01\nfuture-copy,5.00,2026-07\n",
        subject="mixed",
    )
    company.resolve(mixed, "CSV!B2", [january], subject="mixed-january")
    company.resolve(
        mixed,
        "CSV!B3",
        subject="mixed-future",
        treatment="duplicate",
        recognition_period="2026-07",
        duplicate_source_id="primary",
        duplicate_location="CSV!B2",
        reason="Synthetic future original copy.",
    )
    checked = company.materials.check("2026-01")
    assert checked["status"] == checked["file_status"] == "complete", checked["issues"]
    assert {item["source_id"] for item in checked["file_summaries"]} == {
        "mixed",
        "primary",
        "neighbour",
        "distant",
    }
    assert len(checked["source_versions"]) == len(checked["allocation_versions"]) == 4
    assert len(checked["resolution_versions"]) == 5
    assert {item["source_id"] for item in checked["coverage"]} == {"mixed"}


@pytest.mark.parametrize("grouped", (False, True))
def test_future_duplicate_chain_closes_complete_dependency_proof_without_future_adoption(
    tmp_path, monkeypatch, grouped
):
    book = MixedBook(tmp_path / "future-duplicate-chain", employees=1, businesses=26)
    book.add_month(0, close=False)
    old_rule = digest({"contract": "ai-accounting-kernel/2/material-coverage", "version": 3})
    with monkeypatch.context() as old:
        old.setattr(frozen_material, "MATERIAL_COVERAGE_RULE_DIGEST", old_rule)
        book.close_last_month()
    january_history = _closed_history(book)
    assert january_history[0][3] == old_rule
    with book.engine.store.connection(read_only=True) as connection:
        assert (
            frozen_material.verified_frozen_materials(
                connection,
                YearMonth("2016-02").ordinal,
                YearMonth("2016-01").ordinal,
                book.engine.store.registry,
            )
            is None
        )

    book.add_month(1, close=False)
    _expense(book, "dependency-feb-expense", "2016-02", 1000)
    _classify_expense(book, "dependency-feb-expense", "2016-02")
    _expense(book, "dependency-jul-expense", "2016-07", 3000)
    primary, primary_evidence = _receive(
        book,
        "future-primary",
        "2016-07",
        b"item,amount,period\nprimary,30.00,2016-07\n",
    )
    future_link = _link(book, "dependency-jul-expense", "2016-07", 3000)
    if grouped:
        disposition = book.materials.resolve_group(
            "future-primary-group",
            dict(
                period="2016-07",
                source_id="future-primary",
                source_fact_id=primary["fact_id"],
                members=[dict(location="CSV!B2", amount_fen=3000)],
                group_amount_fen=3000,
                links=[future_link],
                joint_basis_confirmed=True,
                basis_evidence_digest=book.proof,
                basis_location="L1",
                reason="Synthetic complete future pool.",
            ),
            evidence=(primary_evidence, book.proof),
            expected_revision=0,
            request_id=book.request("primary-group"),
        )
    else:
        disposition = _resolve(
            book,
            primary,
            primary_evidence,
            "future-primary-row",
            "2016-07",
            2,
            treatment="recognize",
            recognition_period="2016-07",
            links=[future_link],
        )
    middle, middle_evidence = _receive(
        book,
        "future-copy",
        "2016-07",
        b"item,amount,period\ncopy,30.00,2016-07\n",
    )
    middle_disposition = _resolve(
        book,
        middle,
        middle_evidence,
        "future-copy-row",
        "2016-07",
        2,
        treatment="duplicate",
        recognition_period="2016-07",
        links=[],
        duplicate_source_id="future-primary",
        duplicate_location="CSV!B2",
        reason="Synthetic exact future duplicate.",
    )
    mixed, mixed_evidence = _receive(
        book,
        "mixed-original",
        "2016-02",
        b"item,amount,period\nfebruary,10.00,2016-02\njuly-copy,30.00,2016-07\n",
    )
    _resolve(
        book,
        mixed,
        mixed_evidence,
        "mixed-feb-row",
        "2016-02",
        2,
        treatment="recognize",
        recognition_period="2016-02",
        links=[_link(book, "dependency-feb-expense", "2016-02", 1000)],
    )
    _resolve(
        book,
        mixed,
        mixed_evidence,
        "mixed-jul-row",
        "2016-02",
        3,
        treatment="duplicate",
        recognition_period="2016-07",
        links=[],
        duplicate_source_id="future-copy",
        duplicate_location="CSV!B2",
        reason="Synthetic exact future duplicate through another copy.",
    )
    _inventory(book, "2016-02", [mixed_evidence])
    checked = book.materials.check("2016-02")
    assert checked["status"] == "complete", checked["issues"]
    assert {"mixed-original", "future-copy", "future-primary"} <= {
        row["source_id"] for row in checked["file_summaries"]
    }
    assert {primary["fact_id"], middle["fact_id"]} <= set(checked["source_versions"])
    assert middle_disposition["fact_id"] in checked["resolution_versions"]
    assert disposition["fact_id"] in checked["group_versions" if grouped else "resolution_versions"]
    with book.engine.store.connection(read_only=True) as connection:
        for source in (primary, middle):
            allocation_id = connection.execute(
                "SELECT a.revision_id FROM fact_material_period_allocation a "
                "JOIN fact_current c ON c.fact_id=a.revision_id WHERE a.source_id=?",
                (source["subject_id"],),
            ).fetchone()[0]
            assert allocation_id in checked["allocation_versions"]
    book.close_last_month()
    history = _closed_history(book)
    assert history[:1] == january_history
    february = YearMonth("2016-02").ordinal
    with book.engine.store.connection(read_only=True) as connection:
        row = connection.execute(
            "SELECT * FROM period_close WHERE period=?", (february,)
        ).fetchone()
        manifest = decode_close(connection, row)
        proof = manifest["material_coverage"]
        assert set(proof["source_versions"]) == {
            item["source_fact_id"] for item in proof["file_summaries"]
        }
        assert all(
            item["subject_id"] != "dependency-jul-expense" for item in manifest["adopted_results"]
        )
        _, reverse = material_watch._directory(connection, february, manifest)
        assert {"mixed-original", "future-copy", "future-primary"} <= set(
            reverse["business:dependency-jul-expense"]
        )
        assert verify_integrity(book.engine, connection)["status"] == "verified"
    archive = create_portable(
        book.engine.store.path,
        tmp_path / "portable",
        _bundle=book.engine.store.bundle,
    )
    assert (
        verify_portable(archive["path"], _bundle=book.engine.store.bundle)["latest_closed_period"]
        == "2016-02"
    )

    # A read dependency in a close does not make its future business immutable.
    kind, original = book.inputs["dependency-jul-expense"]
    saved = book.engine.amend_fact(
        kind,
        "dependency-jul-expense",
        original | {"expense_class": "sales"},
        evidence=(book.proof,),
        expected_revision=book.revisions["dependency-jul-expense"],
        recording_error_confirmed=True,
        request_id=book.request("future-open-replace"),
    )
    assert saved["status"] == "confirmed"
    book.facts["dependency-jul-expense"] = saved["fact_id"]
    book.publish(["dependency-jul-expense"])
    assert "material_result_stale" in {
        issue["code"] for issue in book.materials.check("2016-07")["issues"]
    }
    assert book.materials.check("2016-03")["status"] == "complete"
    with book.engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        assert {"mixed-original", "future-copy", "future-primary"} <= set(
            material_watch.changed_material_sources(connection, february)[1]
        )
        full = materials.check_completeness(
            connection,
            YearMonth("2016-07").ordinal,
            book.engine.store.registry,
        )
        reused = materials.check_completeness(
            connection,
            YearMonth("2016-07").ordinal,
            book.engine.store.registry,
            _allow_frozen_reuse=True,
        )
        assert canonical(full) == canonical(reused)
        assert verify_integrity(book.engine, connection)["status"] == "verified"
    assert _closed_history(book) == history
