"""Profit detail fixes preserve each close's originally adopted row rules."""

import json

import pytest
from test_reports import book as book  # noqa: F401
from test_reports import close_quarter, scenario

from ai_accounting.kernel import report_flow
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.types import YearMonth, canonical


def _close_with_late_fee(book, monkeypatch, rules_version):
    engine, save, publish, _ = book
    report = scenario(book)
    save(
        "expense",
        "tax-late-fee",
        {
            "period": "2026-02",
            "counterparty_id": "supplier",
            "amount_fen": 467,
            "expense_class": "tax_late_fee",
            "creditor_kind": "supplier",
        },
    )
    publish("tax-late-fee")
    original = report_flow._content

    def adopted_rules(*args, **kwargs):
        return original(*args, **(kwargs | {"profit_rules_version": rules_version}))

    with monkeypatch.context() as previous:
        previous.setattr(report_flow, "_content", adopted_rules)
        close_quarter(book)
    return engine, report


@pytest.mark.parametrize("rules_version", [1, 2])
@pytest.mark.parametrize("damage", ["missing", "wrong_version"])
def test_profit_rule_history_repairs_exact_original_root_from_sources(
    book, monkeypatch, rules_version, damage
):
    engine, _ = _close_with_late_fee(book, monkeypatch, rules_version)
    month = YearMonth("2026-02").ordinal
    with engine.store.connection(read_only=True) as connection:
        original = tuple(connection.execute(
            "SELECT content,close_digest,root_digest FROM report_period_flow "
            "WHERE posting_period=?", (month,),
        ).fetchone())
        close_roots = list(connection.execute(
            "SELECT period,storage_digest FROM close_storage_root ORDER BY period"
        ))
        assert not report_flow.require_report_flow(engine, connection)["changed"]
    body = json.loads(original[0])
    assert body.get("profit_rules_version", 1) == rules_version
    assert ("profit_rules_version" in body) is (rules_version == 2)
    assert body["profit"]["24"] == 467
    assert body["profit"]["29"] == (467 if rules_version == 2 else 0)
    with engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if damage == "missing":
            connection.execute("DELETE FROM report_period_flow WHERE posting_period=?", (month,))
        else:
            body["profit_rules_version"] = 3 - rules_version
            connection.execute(
                "UPDATE report_period_flow SET content=? WHERE posting_period=?",
                (canonical(body), month),
            )
        assert report_flow.compare_report_flow(engine, connection)["changed"]
        assert report_flow.repair_report_flow(engine, connection)["changed"]
        assert tuple(connection.execute(
            "SELECT content,close_digest,root_digest FROM report_period_flow "
            "WHERE posting_period=?", (month,),
        ).fetchone()) == original
        assert list(connection.execute(
            "SELECT period,storage_digest FROM close_storage_root ORDER BY period"
        )) == close_roots
        connection.commit()
    with QueryReads.snapshot(engine) as reads:
        assert report_flow.read_report_profit_rules_version(
            reads.connection, month, reads=reads
        ) == rules_version
        assert report_flow.read_report_flow(reads.connection, month, reads=reads)[
            "profit"
        ]["29"] == (467 if rules_version == 2 else 0)


@pytest.mark.parametrize("rules_version", [1, 2])
def test_root_authenticated_profit_version_survives_classification_fallback(
    book, monkeypatch, rules_version
):
    engine, report = _close_with_late_fee(book, monkeypatch, rules_version)
    month = YearMonth("2026-02").ordinal
    selected = report.report(2026, 1)
    monkeypatch.setattr(report_flow, "_flow_classification_headers_match", lambda *_: False)
    with QueryReads.snapshot(engine) as reads:
        assert report_flow.read_report_flow(reads.connection, month, reads=reads) is None
        assert report_flow.read_report_profit_rules_version(
            reads.connection, month, reads=reads
        ) == rules_version
    fallback = report.report(2026, 1)
    assert fallback["statements"] == selected["statements"]
    assert fallback["statements"]["profit_statement"]["29"]["current_fen"] == (
        467 if rules_version == 2 else 0
    )


def test_unknown_root_authenticated_profit_version_is_rejected(book, monkeypatch):
    engine, _, _, close = book
    scenario(book)
    original = report_flow._content

    def unsupported_rules(*args, **kwargs):
        body = json.loads(original(*args, **kwargs))
        body["profit_rules_version"] = 99
        return canonical(body)

    with monkeypatch.context() as future:
        future.setattr(report_flow, "_content", unsupported_rules)
        close("2026-01")
    month = YearMonth("2026-01").ordinal
    with QueryReads.snapshot(engine) as reads:
        for reader in (
            report_flow.read_report_flow,
            report_flow.read_report_profit_rules_version,
        ):
            with pytest.raises(KernelError, match="利润规则版本无效"):
                reader(reads.connection, month, reads=reads)
