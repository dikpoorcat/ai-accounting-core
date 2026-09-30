"""A closed month keeps complete cross-month dispositions without adopting future results."""

import json

import pytest
from stage9_book import MixedBook

from ai_accounting.kernel import material_watch
from ai_accounting.kernel.backup import create_portable, verify_portable
from ai_accounting.kernel.close_storage import decode_close
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.integrity import verify_integrity
from ai_accounting.kernel.material_watch import require_material_watch
from ai_accounting.kernel.types import YearMonth


def _link(book, subject, period, amount):
    return {
        "subject_id": subject,
        "fact_kind": "expense",
        "fact_id": book.facts[subject],
        "calculation_id": book.results[subject],
        "amount_field": "fact.amount_fen",
        "amount_fen": amount,
        "recognition_period": period,
    }


def _expense(book, subject, period, amount):
    book.save(
        [
            book.item(
                "expense",
                subject,
                {
                    "period": period,
                    "counterparty_id": book.supplier,
                    "amount_fen": amount,
                    "expense_class": "administration",
                    "creditor_kind": "supplier",
                },
            )
        ]
    )
    book.publish([subject])


def _classify_expense(book, subject, period):
    with book.engine.store.connection(read_only=True) as connection:
        rows = connection.execute(
            "SELECT v.id,l.line_no,l.account,l.debit+l.credit FROM voucher_current vc "
            "JOIN voucher_version v ON v.id=vc.version_id "
            "JOIN voucher_line l ON l.version_id=v.id "
            "WHERE v.calculation_id=? AND l.account IN ('5601','5602')",
            (book.results[subject],),
        ).fetchall()
    assert rows
    book.save(
        [
            book.item(
                "report_classification",
                "report-class-" + version,
                {
                    "period": period,
                    "voucher_version_id": version,
                    "profit_details": [
                        {
                            "line_no": line_no,
                            "detail_code": (
                                "sales_other" if account == "5601" else "management_other"
                            ),
                            "amount_fen": amount,
                        }
                    ],
                },
            )
            for version, line_no, account, amount in rows
        ]
    )


def _replace_february_expense(book, subject):
    kind, old = book.inputs[subject]
    changed = {**old, "expense_class": "sales"}
    saved = book.engine.amend_fact(
        kind,
        subject,
        changed,
        evidence=(book.proof,),
        expected_revision=book.revisions[subject],
        recording_error_confirmed=True,
        request_id=book.request("feb-open-replace"),
    )
    assert saved["status"] == "confirmed"
    book.inputs[subject] = (kind, changed)
    book.facts[subject] = saved["fact_id"]
    book.revisions[subject] += 1
    book.publish([subject])


def _add_cross_month_rows(book, *, grouped):
    raw = (
        b"item,amount\njanuary,10.00\nfebruary,20.00\n"
        if grouped
        else b"item,amount,period\njanuary,10.00,2016-01\nfebruary,20.00,2016-02\n"
    )
    evidence = book.evidence(raw, "jan-feb-original.csv")
    columns = [{"column": "A", "role": "context"}, {"column": "B", "role": "amount"}]
    if not grouped:
        columns.append({"column": "C", "role": "recognition_period"})
    source = book.materials.receive(
        "jan-feb-original",
        {
            "period": "2016-01",
            "evidence_digest": evidence,
            "category": "transactions",
            "purpose": "business",
            "specification": {"format": "csv", "columns": columns},
        },
        evidence=(evidence, book.proof),
        expected_revision=0,
        request_id=book.request("cross-month-source"),
    )
    if grouped:
        _expense(book, "cross-feb-expense", "2016-02", 1000)
        _expense(book, "cross-mar-expense", "2016-03", 2000)
    else:
        _expense(book, "cross-jan-expense", "2016-01", 1000)
        _expense(book, "cross-feb-expense", "2016-02", 2000)
        _classify_expense(book, "cross-jan-expense", "2016-01")
    if grouped:
        book.materials.resolve_group(
            "cross-joint-group",
            _group_data(book, source),
            evidence=(evidence, book.proof),
            expected_revision=0,
            request_id=book.request("cross-joint-group"),
        )
    else:
        for row, subject, period, amount in (
            (2, "cross-jan-expense", "2016-01", 1000),
            (3, "cross-feb-expense", "2016-02", 2000),
        ):
            book.materials.resolve(
                f"cross-row-{row}",
                _row_data(book, source, row, subject, period, amount),
                evidence=(evidence, book.proof),
                expected_revision=0,
                request_id=book.request("cross-row-resolution"),
            )
    january = YearMonth("2016-01").ordinal
    with book.engine.store.connection(read_only=True) as connection:
        inventory_id = connection.execute(
            "SELECT id FROM material_revision WHERE period=? AND category='transactions' "
            "ORDER BY id DESC LIMIT 1",
            (january,),
        ).fetchone()[0]
        originals = [
            item[0].hex()
            for item in connection.execute(
                "SELECT evidence_digest FROM material_item WHERE inventory_id=?",
                (inventory_id,),
            )
        ]
    book.periods.inventory(
        "2016-01",
        "transactions",
        evidence=[*originals, evidence],
        expected=len(originals) + 1,
        no_business=False,
        confirmation_evidence=book.proof,
        request_id=book.request("cross-month-inventory"),
    )
    return source, evidence


def _row_data(book, source, row, subject, period, amount):
    return {
        "period": "2016-01",
        "source_id": "jan-feb-original",
        "source_fact_id": source["fact_id"],
        "location": f"CSV!B{row}",
        "treatment": "recognize" if row == 2 else "other_period",
        "recognition_period": period,
        "links": [_link(book, subject, period, amount)],
    }


def _group_data(book, source):
    return {
        "period": "2016-01",
        "source_id": "jan-feb-original",
        "source_fact_id": source["fact_id"],
        "members": [
            {"location": "CSV!B2", "amount_fen": 1000},
            {"location": "CSV!B3", "amount_fen": 2000},
        ],
        "group_amount_fen": 3000,
        "links": [
            _link(book, "cross-feb-expense", "2016-02", 1000),
            _link(book, "cross-mar-expense", "2016-03", 2000),
        ],
        "joint_basis_confirmed": True,
        "basis_evidence_digest": book.proof,
        "basis_location": "合成跨月共同核准",
        "reason": "两原行共同形成二月与三月费用；原件没有逐行期间或单行到结果的对应。",
    }


def _frozen(company):
    with company.engine.store.connection(read_only=True) as connection:
        january = YearMonth("2016-01").ordinal
        row = connection.execute(
            "SELECT digest,manifest FROM period_close WHERE period=?", (january,)
        ).fetchone()
        assert row is not None
        return bytes(row["digest"]), row["manifest"]


@pytest.mark.parametrize("grouped", (False, True))
def test_future_open_replace_preserves_january_close_and_full_watch(tmp_path, grouped):
    book = MixedBook(tmp_path / "future-links", employees=1, businesses=26)
    book.add_month(0, close=False)
    source, evidence = _add_cross_month_rows(book, grouped=grouped)
    assert book.materials.check("2016-01")["status"] == "complete"
    assert book.materials.check("2016-02")["status"] == "complete"
    _replace_february_expense(book, "cross-feb-expense")
    assert book.materials.check("2016-01")["status"] == "complete"
    assert "material_result_stale" in {
        issue["code"] for issue in book.materials.check("2016-02")["issues"]
    }
    with book.engine.store.connection(read_only=True) as connection:
        checked = book.periods.check_readiness(connection, "2016-01")
        assert not checked["issues"], checked["issues"]
    book.close_last_month()
    frozen = _frozen(book)
    with book.engine.store.connection(read_only=True) as connection:
        row = connection.execute(
            "SELECT * FROM period_close WHERE period=?",
            (YearMonth("2016-01").ordinal,),
        ).fetchone()
        manifest = decode_close(connection, row)
    assert source["fact_id"] in manifest["material_coverage"]["fact_ids"]
    assert all(
        item["subject_id"] != "cross-feb-expense" for item in manifest["adopted_results"]
    )
    with book.engine.store.connection(read_only=True) as connection:
        january = YearMonth("2016-01").ordinal
        heads = material_watch.heads_at(
            connection, material_watch._stored_root(connection, january)["highwater"]
        )
        version_label = "group_versions" if grouped else "resolution_versions"
        link_table = (
            "fact_material_group_resolution_links"
            if grouped
            else "fact_material_resolution_v2_links"
        )
        frozen_future_links = [
            result[0]
            for result in connection.execute(
                "SELECT l.calculation_id FROM json_each(?) ids "
                f"CROSS JOIN {link_table} l ON l.revision_id=ids.value "
                "WHERE l.subject_id='cross-feb-expense'",
                (json.dumps(manifest["material_coverage"][version_label]),),
            )
        ]
        assert len(frozen_future_links) == 1
        assert frozen_future_links[0] != heads["calculation"]["cross-feb-expense"]
        _, reverse = material_watch._directory(
            connection, january, manifest, historic_heads=heads
        )
        assert "jan-feb-original" in reverse["business:cross-feb-expense"]
        if not grouped:
            incorrect = {
                "fact": dict(heads["fact"]),
                "calculation": dict(heads["calculation"]),
            }
            incorrect["calculation"]["cross-jan-expense"] = "wrong-adopted-calculation"
            with pytest.raises(KernelError) as rejected:
                material_watch._directory(
                    connection, january, manifest, historic_heads=incorrect
                )
            assert rejected.value.code == "content_integrity_failed"
    assert book.materials.check("2016-01")["status"] == "complete"
    assert "material_result_stale" in {
        issue["code"] for issue in book.materials.check("2016-02")["issues"]
    }
    with book.engine.store.connection(read_only=True) as connection:
        require_material_watch(book.engine, connection)
        assert verify_integrity(book.engine, connection)["status"] == "verified"
    archive = create_portable(
        book.engine.store.path,
        tmp_path / "portable",
        _bundle=book.engine.store.bundle,
    )
    assert (
        verify_portable(archive["path"], _bundle=book.engine.store.bundle)[
            "latest_closed_period"
        ]
        == "2016-01"
    )
    assert _frozen(book) == frozen
    with book.engine.store.connection(read_only=True) as connection:
        checked = book.periods.check_readiness(connection, "2016-02")
        assert any(
            issue.get("code") == "material_result_stale"
            for issue in checked["issues"]
        ), checked["issues"]
    with pytest.raises(KernelError):
        book.periods.preview_close("2016-02", owner_confirmation=book.proof)
    if grouped:
        book.materials.resolve_group(
            "cross-joint-group",
            _group_data(book, source),
            evidence=(evidence, book.proof),
            expected_revision=1,
            request_id=book.request("rebind-february-group"),
        )
    else:
        book.materials.resolve(
            "cross-row-3",
            _row_data(book, source, 3, "cross-feb-expense", "2016-02", 2000),
            evidence=(evidence, book.proof),
            expected_revision=1,
            request_id=book.request("rebind-february-row"),
        )
    assert book.materials.check("2016-02")["status"] == "complete"
    _classify_expense(book, "cross-feb-expense", "2016-02")
    book.add_month(1, close=False)
    with book.engine.store.connection(read_only=True) as connection:
        checked = book.periods.check_readiness(connection, "2016-02")
        assert not checked["issues"], checked["issues"]
    book.close_last_month()
    assert _frozen(book) == frozen
    with book.engine.store.connection(read_only=True) as connection:
        assert verify_integrity(book.engine, connection)["status"] == "verified"
        if grouped:
            february = YearMonth("2016-02").ordinal
            row = connection.execute(
                "SELECT * FROM period_close WHERE period=?", (february,)
            ).fetchone()
            february_manifest = decode_close(connection, row)
            assert february_manifest["material_coverage"]["group_versions"]
            heads = material_watch.heads_at(
                connection,
                material_watch._stored_root(connection, february)["highwater"],
            )
            incorrect = {
                "fact": dict(heads["fact"]),
                "calculation": dict(heads["calculation"]),
            }
            incorrect["calculation"]["cross-feb-expense"] = "wrong-adopted-calculation"
            with pytest.raises(KernelError) as rejected:
                material_watch._directory(
                    connection,
                    february,
                    february_manifest,
                    historic_heads=incorrect,
                )
            assert rejected.value.code == "content_integrity_failed"
