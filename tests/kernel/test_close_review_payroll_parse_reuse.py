"""A verified payroll result is decoded once per owner-review consumer."""

from __future__ import annotations

import hashlib
import json

import pytest
from test_integrity_content import damage
from test_payroll_preparation import company as payroll_company

from ai_accounting.kernel import close_review, stored_json
from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.query_reads import QueryReads


@pytest.fixture
def confirmed_payroll(tmp_path):
    return payroll_company(tmp_path)


def _payroll_source(engine):
    with engine.store.connection(read_only=True) as connection:
        row = connection.execute(
            "SELECT id,outcome,digest FROM calculation WHERE kind='payroll' LIMIT 1"
        ).fetchone()
    return row["id"], json.loads(row["outcome"]), row["digest"]


def test_payroll_confirmation_decoded_once_without_changing_sources(confirmed_payroll, monkeypatch):
    instance = confirmed_payroll
    engine = instance.engine
    ident, outcome, stored_digest = _payroll_source(engine)
    expected_ids = close_review._confirmation_fact_ids(outcome["values"]["payroll_confirmation"])
    expected_gross = instance.current("january").values["gross_fen"]

    current_calls = []
    original_current = stored_json.load_outcome

    def current_decode(raw):
        current_calls.append(raw)
        return original_current(raw)

    monkeypatch.setattr(stored_json, "load_outcome", current_decode)
    with QueryReads.snapshot(engine) as reads:
        basis = close_review.business_adopted_basis(reads.connection, engine, (ident,), reads=reads)
        assert len(current_calls) == 1
        current_calls.clear()
        cards = close_review._payroll_cards(reads.connection, [ident])
        assert current_calls == []  # verify_outcome_bytes already decoded the same bytes.

    for selected in (basis,):
        [confirmation] = selected["payroll_confirmations"]
        assert confirmation["calculation_reference"]["id"] == ident
        assert confirmation["calculation_reference"]["digest"] == stored_digest.hex()
        assert [ref["id"] for ref in confirmation["confirmation_references"]] == expected_ids
    for selected in (cards,):
        [card] = selected
        assert card["key"] == ident
        assert card["references"][0]["digest"] == stored_digest.hex()
        assert [ref["id"] for ref in card["references"][2:]] == expected_ids
    assert instance.current("january").values["gross_fen"] == expected_gross
    detail = Dashboard(engine).business_status("2026-01", "january", as_of="2026-02-01")
    result = detail["data"]["current_business_result"]
    assert result["amount_fen"] == expected_gross
    assert "payroll_confirmation" not in result
    core = BusinessQueries(engine).business_status("january", "2026-01", as_of="2026-02-01")
    published = core["current_business_result"]
    assert published["calculation"]["id"] == ident
    assert published["payroll_confirmation"]["confirmation_fact_id"] in expected_ids


def test_payroll_confirmation_duplicate_saved_key_still_rejected(confirmed_payroll):
    instance = confirmed_payroll
    engine = instance.engine
    ident, outcome, _digest = _payroll_source(engine)
    # SQLite JSON1 would select the first values member; current readers must
    # reject the second member even if the row digest is rewritten with it.
    damaged = '{"values":{},' + json.dumps(outcome, ensure_ascii=False)[1:]
    damage(
        engine,
        "calculation",
        "UPDATE calculation SET outcome=?,digest=? WHERE id=?",
        (damaged, hashlib.sha256(damaged.encode()).digest(), ident),
    )
    with QueryReads.snapshot(engine) as reads:
        for consumer in (
            lambda: close_review.business_adopted_basis(
                reads.connection, engine, (ident,), reads=reads
            ),
            lambda: close_review._payroll_cards(reads.connection, [ident]),
        ):
            with pytest.raises(KernelError) as error:
                consumer()
            assert error.value.code == "content_integrity_failed"
