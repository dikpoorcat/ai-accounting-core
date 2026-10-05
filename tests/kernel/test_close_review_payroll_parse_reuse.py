"""A verified payroll result is decoded once per owner-review consumer."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest
from schema_fixture import TEST_FAMILY, full_contract, write_contract
from test_integrity_content import damage
from test_payroll_preparation import company as payroll_company

from ai_accounting.kernel import (
    close_review,
    close_review_integrity_v1,
    content_v1,
    service,
    stored_json,
)
from ai_accounting.kernel.catalog import catalog_sql
from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.schema import schema_sql
from ai_accounting.kernel.schema_bundle import APPLICATION_ID, load_bundle
from ai_accounting.kernel.storage import Store


@pytest.fixture
def released_payroll(tmp_path, monkeypatch):
    registry = service.default_registry()
    directory = tmp_path / "schema_contracts"
    write_contract(
        directory,
        full_contract(schema_sql(registry), kind="company", status="released", version=1),
    )
    write_contract(
        directory,
        full_contract(catalog_sql(), kind="catalog", status="released", version=1),
    )
    (directory / "content-v1.json").write_text(
        json.dumps(content_v1.content_contract(registry), ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr(content_v1, "__file__", str(tmp_path / "content_v1.py"))
    content_v1.v1_registry.cache_clear()
    bundle = load_bundle(
        registry,
        directory,
        family=TEST_FAMILY,
        application_id=APPLICATION_ID,
        status="released",
        current_versions={"company": 1, "catalog": 1},
        company_verifiers={1: content_v1.verify_v1_company},
    )
    monkeypatch.setattr("test_payroll_corrections.production_bundle", lambda: bundle)
    try:
        company = payroll_company(tmp_path)
        historical = Engine(
            Store(
                company.engine.store.path,
                replace(bundle, registry=content_v1.v1_registry()),
                company.engine.store.company_id,
                company.engine.store.database_id,
            )
        )
        yield company, historical
    finally:
        content_v1.v1_registry.cache_clear()


def _payroll_source(engine):
    with engine.store.connection(read_only=True) as connection:
        row = connection.execute(
            "SELECT id,outcome,digest FROM calculation WHERE kind='payroll' LIMIT 1"
        ).fetchone()
    return row["id"], json.loads(row["outcome"]), row["digest"]


def test_payroll_confirmation_decoded_once_without_changing_sources(
    released_payroll, monkeypatch
):
    instance, historical = released_payroll
    engine = instance.engine
    ident, outcome, stored_digest = _payroll_source(engine)
    expected_ids = close_review._confirmation_fact_ids(
        outcome["values"]["payroll_confirmation"]
    )
    expected_gross = instance.current("january").values["gross_fen"]

    current_calls = []
    original_current = stored_json.load_outcome

    def current_decode(raw):
        current_calls.append(raw)
        return original_current(raw)

    monkeypatch.setattr(stored_json, "load_outcome", current_decode)
    with QueryReads.snapshot(engine) as reads:
        basis = close_review.business_adopted_basis(
            reads.connection, engine, (ident,), reads=reads
        )
        assert len(current_calls) == 1
        current_calls.clear()
        cards = close_review._payroll_cards(reads.connection, [ident])
        assert current_calls == []  # verify_outcome_bytes already decoded the same bytes.

        v1_calls = []
        original_v1 = close_review_integrity_v1.load_outcome

        def v1_decode(raw):
            v1_calls.append(raw)
            return original_v1(raw)

        monkeypatch.setattr(close_review_integrity_v1, "load_outcome", v1_decode)
        v1_basis = close_review_integrity_v1.business_adopted_basis(
            reads.connection, historical, (ident,)
        )
        assert len(v1_calls) == 1
        v1_calls.clear()
        v1_cards = close_review_integrity_v1._payroll_cards(reads.connection, [ident])
        assert len(v1_calls) == 1

    for selected in (basis, v1_basis):
        [confirmation] = selected["payroll_confirmations"]
        assert confirmation["calculation_reference"]["id"] == ident
        assert confirmation["calculation_reference"]["digest"] == stored_digest.hex()
        assert [ref["id"] for ref in confirmation["confirmation_references"]] == expected_ids
    for selected in (cards, v1_cards):
        [card] = selected
        assert card["key"] == ident
        assert card["references"][0]["digest"] == stored_digest.hex()
        assert [ref["id"] for ref in card["references"][2:]] == expected_ids
    assert basis == v1_basis
    assert cards == v1_cards
    assert instance.current("january").values["gross_fen"] == expected_gross
    detail = Dashboard(engine).business_status("2026-01", "january", as_of="2026-02-01")
    result = detail["data"]["current_business_result"]
    assert result["amount_fen"] == expected_gross
    assert "payroll_confirmation" not in result
    core = BusinessQueries(engine).business_status("january", "2026-01", as_of="2026-02-01")
    published = core["current_business_result"]
    assert published["calculation"]["id"] == ident
    assert published["payroll_confirmation"]["confirmation_fact_id"] in expected_ids


def test_payroll_confirmation_duplicate_saved_key_still_rejected(released_payroll):
    instance, historical = released_payroll
    engine = instance.engine
    ident, outcome, _digest = _payroll_source(engine)
    # SQLite JSON1 would select the first values member; both versioned readers
    # must reject the second member even if the row digest is rewritten with it.
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
            lambda: close_review_integrity_v1.business_adopted_basis(
                reads.connection, historical, (ident,)
            ),
            lambda: close_review_integrity_v1._payroll_cards(reads.connection, [ident]),
        ):
            with pytest.raises(KernelError) as error:
                consumer()
            assert error.value.code == "content_integrity_failed"
