"""Publication anchors bind reusable open report inputs to saved sources."""

import pytest
from test_integrity_content import damage
from test_reports import book as book  # noqa: F401
from test_reports import cit, scenario

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.maintenance import Maintenance
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.report_open_contribution import (
    compare_open_contributions,
    read_open_contributions,
    selected_open_line,
)
from ai_accounting.kernel.report_projection import _authoritative_rows, _party_delta
from ai_accounting.kernel.types import YearMonth


def test_open_party_and_full_report_reuse_same_saved_contributions(book, monkeypatch):
    engine = book[0]
    report = scenario(book)
    with QueryReads.snapshot(engine) as reads:
        rows = _authoritative_rows(reads.connection, "open", YearMonth("2026-03").ordinal)
        selected = {row[5] for row in rows}
        contributions = read_open_contributions(
            engine, reads.connection, selected, reads=reads
        )
        assert selected <= contributions.keys()

        def unexpected(_rows):
            raise AssertionError("the projected month must not resolve the same source again")

        monkeypatch.setattr(reads, "report_line_relations_many", unexpected)
        assert _party_delta(
            engine,
            reads.connection,
            YearMonth("2026-03").ordinal,
            tuple(rows),
            reads=reads,
        )[1]
        assert report._report(
            2026, 1, source="open", connection=reads.connection, reads=reads
        )["statements"]


def test_missing_body_falls_back_and_complete_check_repairs(book):
    engine = book[0]
    scenario(book)
    with engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM report_open_contribution")
        connection.commit()
    with QueryReads.snapshot(engine) as reads:
        result = read_open_contributions(
            engine,
            reads.connection,
            {
                row[0]
                for row in reads.connection.execute(
                    "SELECT calculation_id FROM calculation_publication "
                    "WHERE calculation_id IS NOT NULL"
                )
            },
            reads=reads,
        )
        assert result == {}
    with pytest.raises(KernelError):
        Maintenance(engine).verify_integrity()
    assert Maintenance(engine).rebuild_projections(request_id="restore-open-lines")["changed"]
    assert Maintenance(engine).verify_integrity()["status"] == "verified"


def test_tampered_saved_body_and_anchor_are_not_trusted(book):
    engine = book[0]
    scenario(book)
    with engine.store.connection(read_only=True) as connection:
        anchor = connection.execute(
            "SELECT publication_id FROM report_open_contribution_anchor "
            "WHERE calculation_id=(SELECT calculation_id FROM calculation_publication "
            "WHERE subject_id='payment' ORDER BY sequence DESC LIMIT 1)"
        ).fetchone()[0]
    damage(
        engine,
        "report_open_contribution",
        "UPDATE report_open_contribution SET content_digest=zeroblob(32) "
        "WHERE publication_id=?",
        (anchor,),
    )
    month = YearMonth("2026-03").ordinal
    with QueryReads.snapshot(engine) as reads:
        rows = tuple(_authoritative_rows(reads.connection, "open", month))
        with pytest.raises(KernelError, match="当前报表行贡献"):
            _party_delta(engine, reads.connection, month, rows, reads=reads)
    with pytest.raises(KernelError):
        Maintenance(engine).verify_integrity()
    Maintenance(engine).rebuild_projections(request_id="repair-body-damage")
    damage(
        engine,
        "report_open_contribution_anchor",
        "UPDATE report_open_contribution_anchor SET content_digest=zeroblob(32) "
        "WHERE publication_id=?",
        (anchor,),
    )
    with QueryReads.snapshot(engine) as reads:
        rows = tuple(_authoritative_rows(reads.connection, "open", month))
        with pytest.raises(KernelError):
            _party_delta(engine, reads.connection, month, rows, reads=reads)
    with pytest.raises(KernelError):
        Maintenance(engine).rebuild_projections(request_id="cannot-repair-anchor")


def test_exact_line_and_cashflow_must_match_authoritative_voucher(book):
    engine = book[0]
    scenario(book)
    period = YearMonth("2026-03").ordinal
    with QueryReads.snapshot(engine) as reads:
        row = next(
            row for row in _authoritative_rows(reads.connection, "open", period)
            if row[8] in {"1001", "1002", "1012"}
        )
        contribution = read_open_contributions(
            engine, reads.connection, {row[5]}, reads=reads
        )[row[5]]
        selected = {
            "version_id": row[2],
            "line_no": row[3],
            "account": row[8],
            "debit": row[9],
            "credit": row[10],
            "cashflow": row[11],
            "reverses_id": row[7],
        }
        assert selected_open_line(contribution, selected) is not None
        with pytest.raises(KernelError):
            selected_open_line(contribution, selected | {"cashflow": "changed"})


def test_full_check_rejects_missing_publication_anchor(book):
    engine = book[0]
    scenario(book)
    with engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        publication = connection.execute(
            "SELECT id FROM calculation_publication WHERE subject_id='cost'"
        ).fetchone()[0]
        connection.execute(
            "DELETE FROM report_open_contribution WHERE publication_id=?", (publication,)
        )
        connection.commit()
    damage(
        engine,
        "report_open_contribution_anchor",
        "DELETE FROM report_open_contribution_anchor WHERE publication_id=?",
        (publication,),
    )
    with engine.store.connection(read_only=True) as connection:
        with pytest.raises(KernelError):
            compare_open_contributions(engine, connection, check_bodies=False)
    with pytest.raises(KernelError):
        Maintenance(engine).rebuild_projections(request_id="missing-immutable-anchor")


def test_projected_line_still_checks_actual_saved_source_content(book):
    engine = book[0]
    scenario(book)
    month = YearMonth("2026-03").ordinal
    damage(
        engine,
        "calculation",
        "UPDATE calculation SET outcome=replace(outcome,'10000','10001') "
        "WHERE subject_id='payment'",
    )
    with QueryReads.snapshot(engine) as reads:
        rows = tuple(_authoritative_rows(reads.connection, "open", month))
        with pytest.raises(KernelError, match="本次读取的核算来源封签不一致"):
            _party_delta(engine, reads.connection, month, rows, reads=reads)


def test_no_impact_review_with_old_voucher_uses_exact_fallback(book, monkeypatch):
    import ai_accounting.kernel.report_open_contribution as contribution

    engine, save, publish, _ = book
    scenario(book, classification=False, tax=False)
    save(
        "expense",
        "cost",
        {
            "period": "2026-02",
            "counterparty_id": "supplier",
            "amount_fen": 10000,
            "expense_class": "administration",
            "creditor_kind": "supplier",
        },
        revision=1,
    )
    preview = engine.preview(["cost"])
    assert preview["results"][0]["impact"] == "review_no_impact"
    publish("cost")
    with QueryReads.snapshot(engine) as reads:
        reviewed_id = reads.connection.execute(
            "SELECT calculation_id FROM calculation_publication WHERE subject_id='cost' "
            "ORDER BY sequence DESC LIMIT 1"
        ).fetchone()[0]
        assert read_open_contributions(
            engine, reads.connection, {reviewed_id}, reads=reads
        )[reviewed_id]["rows"] == []
        month = YearMonth("2026-02").ordinal
        rows = tuple(_authoritative_rows(reads.connection, "open", month))
        assert any(row[5] == reviewed_id for row in rows)
        projected = _party_delta(engine, reads.connection, month, rows, reads=reads)
    with monkeypatch.context() as patch:
        patch.setattr(contribution, "read_open_contributions", lambda *a, **k: {})
        with QueryReads.snapshot(engine) as reads:
            rows = tuple(_authoritative_rows(reads.connection, "open", month))
            resolved = _party_delta(engine, reads.connection, month, rows, reads=reads)
    assert projected == resolved


def test_open_report_rejects_missing_no_impact_review_publication(book):
    engine, save, publish, _ = book
    scenario(book, classification=False, tax=False)
    save(
        "expense",
        "cost",
        {
            "period": "2026-02",
            "counterparty_id": "supplier",
            "amount_fen": 10000,
            "expense_class": "administration",
            "creditor_kind": "supplier",
        },
        revision=1,
    )
    assert engine.preview(["cost"])["results"][0]["impact"] == "review_no_impact"
    publish("cost")
    with engine.store.connection(read_only=True) as connection:
        reviewed_id = connection.execute(
            "SELECT calculation_id FROM calculation_current WHERE subject_id='cost'"
        ).fetchone()[0]
    damage(
        engine,
        "calculation_publication",
        "DELETE FROM calculation_publication WHERE calculation_id=?",
        (reviewed_id,),
        foreign_keys=False,
    )
    with QueryReads.snapshot(engine) as reads:
        for _ in range(2):
            with pytest.raises(KernelError, match="^正式发布缺少精确当前核算来源$") as failure:
                _authoritative_rows(reads.connection, "open", YearMonth("2026-02").ordinal)
            assert failure.value.code == "content_integrity_failed"
            assert not reads._verified_open_voucher_scopes
            assert not reads._verified_publication_ids
            assert not reads._verified_current_voucher_publications


def test_second_closed_correction_uses_each_original_line_anchor(book, monkeypatch):
    import ai_accounting.kernel.report_open_contribution as contribution

    engine, save, publish, close = book
    scenario(book)
    for period in ("2026-01", "2026-02", "2026-03"):
        close(period)
    for amount, revision, posting_period in (
        (15000, 1, "2026-04"),
        (20000, 2, "2026-05"),
    ):
        save(
            "expense",
            "cost",
            {
                "period": "2026-02",
                "counterparty_id": "supplier",
                "amount_fen": amount,
                "expense_class": "administration",
                "creditor_kind": "supplier",
            },
            revision=revision,
        )
        publish("cost", posting_period=posting_period)
        if posting_period == "2026-04":
            with engine.store.connection(read_only=True) as connection:
                version = connection.execute(
                    "SELECT v.id FROM voucher_current a JOIN voucher_version v "
                    "ON v.id=a.version_id JOIN calculation c ON c.id=v.calculation_id "
                    "WHERE c.subject_id='cost' AND v.reverses_id IS NULL "
                    "ORDER BY v.period DESC LIMIT 1"
                ).fetchone()[0]
            save(
                "report_classification",
                "corrected-detail",
                {
                    "period": posting_period,
                    "voucher_version_id": version,
                    "profit_details": [
                        {
                            "line_no": 1,
                            "detail_code": "management_entertainment",
                            "amount_fen": amount,
                        }
                    ],
                },
            )
            cit(save, publish, "2026-06")
            close(posting_period)

    month = YearMonth("2026-05").ordinal
    with QueryReads.snapshot(engine) as reads:
        rows = tuple(_authoritative_rows(reads.connection, "open", month))
        assert {row[7] is not None for row in rows} == {False, True}
        contributions = read_open_contributions(
            engine, reads.connection, {row[5] for row in rows}, reads=reads
        )
        for row in rows:
            selected = {
                "version_id": row[2],
                "line_no": row[3],
                "account": row[8],
                "debit": row[9],
                "credit": row[10],
                "cashflow": row[11],
                "reverses_id": row[7],
            }
            assert selected_open_line(contributions[row[5]], selected) is not None
        projected = _party_delta(engine, reads.connection, month, rows, reads=reads)
    with monkeypatch.context() as patch:
        patch.setattr(contribution, "read_open_contributions", lambda *a, **k: {})
        with QueryReads.snapshot(engine) as reads:
            rows = tuple(_authoritative_rows(reads.connection, "open", month))
            resolved = _party_delta(engine, reads.connection, month, rows, reads=reads)
    assert projected == resolved


def test_classification_fact_body_proof_is_scoped_to_successful_snapshot(book, monkeypatch):
    import ai_accounting.kernel.integrity as integrity

    engine = book[0]
    scenario(book)
    with engine.store.connection(read_only=True) as connection:
        classification_id = connection.execute(
            "SELECT f.id FROM fact_revision f JOIN subject s ON s.id=f.subject_id "
            "WHERE s.kind='report_classification'"
        ).fetchone()[0]
    from ai_accounting.kernel.reports import _verify_report_fact_sources

    original = integrity.verify_sources
    calls = []

    def verify(*args, **kwargs):
        calls.append(set(kwargs["fact_ids"]))
        if len(calls) == 1:
            raise KernelError("content_integrity_failed", "synthetic failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(integrity, "verify_sources", verify)
    with QueryReads.snapshot(engine) as reads:
        with pytest.raises(KernelError, match="synthetic failure"):
            _verify_report_fact_sources(
                reads.connection, reads, {classification_id}
            )
        _verify_report_fact_sources(reads.connection, reads, {classification_id})
        _verify_report_fact_sources(reads.connection, reads, {classification_id})
    with QueryReads.snapshot(engine) as reads:
        _verify_report_fact_sources(reads.connection, reads, {classification_id})
    assert calls == [{classification_id}] * 3
