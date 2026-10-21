"""A database-bound analysis saves repeated work without weakening document proof."""

from copy import deepcopy
from uuid import uuid4

import pytest
from test_documents import company_identity as _identity
from test_records_snapshot import prepared
from test_standard_bundle import rewrite

from ai_accounting_modular.contracts.public import ContractError, analyze_fact
from ai_accounting_modular.records import _documents, _snapshot, _standard
from ai_accounting_modular.records.public import RecordsChange, RecordsContext, open_standard_bundle


def test_frozen_analysis_is_reused_without_republication_or_source_reanalysis(
    records_case, monkeypatch
):
    storage, (company, _), records = records_case
    fact, obj = prepared(records_case)
    analysis = analyze_fact(fact)
    fact["data"]["amount_fen"] = "99999"
    analysis.fact["data"]["amount_fen"] = "88888"
    analysis.source_paths.clear()

    def unnecessary(*args, **kwargs):
        pytest.fail("Already proven facts must not be reanalyzed or republished")

    monkeypatch.setattr(_snapshot, "analyze_fact", unnecessary)
    monkeypatch.setattr(_standard, "fact_source_ids", unnecessary)
    monkeypatch.setattr(_documents, "capture_documents", unnecessary)
    with storage.read_unit(company.company_id) as read:
        saved = records.verify_snapshot(_identity(company), [analysis], [obj], read)
    assert saved.document_set_id


def test_standalone_records_builds_its_own_proof_and_offline_bundle_remains_independent(
    records_case, monkeypatch
):
    storage, (company, _), records = records_case
    fact, obj = prepared(records_case)
    analyses, sources = [], []
    analyze, extract = _snapshot.analyze_fact, _standard.fact_source_ids

    def observed_analyze(value):
        analyses.append(deepcopy(value))
        return analyze(value)

    def observed_source(value):
        sources.append(deepcopy(value))
        return extract(value)

    monkeypatch.setattr(_snapshot, "analyze_fact", observed_analyze)
    monkeypatch.setattr(_standard, "fact_source_ids", observed_source)
    with storage.read_unit(company.company_id) as read:
        records.verify_snapshot(_identity(company), [fact], [obj], read)
    assert analyses == [fact] and sources == []
    bundle = open_standard_bundle(company.company_root / "standard/current.json")
    assert bundle.facts == (fact,) and sources == [fact]


@pytest.mark.parametrize("damage", ["same_amount_different_class", "missing", "duplicate", "empty"])
def test_self_consistent_document_damage_cannot_borrow_database_fact_proof(records_case, damage):
    storage, (company, _), records = records_case
    fact, obj = prepared(records_case)

    def edit(manifest, parts):
        if damage == "same_amount_different_class":
            parts["facts:operations:2026-01"]["facts"][0]["data"]["expense_class"] = "sales"
        elif damage == "missing":
            del parts["facts:operations:2026-01"]
        elif damage == "duplicate":
            parts["facts:operations:2026-01"]["facts"].append(deepcopy(fact))
        else:
            parts["facts:operations:2026-02"] = {
                "format": _documents.FORMAT,
                "key": "facts:operations:2026-02",
                "facts": [],
            }

    rewrite(company.company_root / "standard/current.json", edit)
    with storage.read_unit(company.company_id) as read, pytest.raises(ContractError):
        records.verify_snapshot(_identity(company), [analyze_fact(fact)], [obj], read)


def test_nested_locator_and_complete_source_membership_remain_part_of_the_proof(records_case):
    from tests.modular.contracts.test_fact_sources import _facts, _locator_objects

    storage, (company, _), records = records_case
    fact = deepcopy(next(value for value in _facts() if value["kind"] == "money.bank_statement"))
    fact["period"] = "2026-01"
    fact["evidence_ids"] = []
    with storage.read_unit(company.company_id) as read:
        source = records.prepare_source(
            {"kind": "owner_statement", "text": "嵌套来源合成确认"}, read
        )
    for locator in _locator_objects(fact):
        locator["source_id"] = source["source_id"]
    analysis = analyze_fact(fact)
    assert analysis.source_ids == (source["source_id"],)
    with storage.write_unit(company.company_id) as unit:
        records.apply_current(
            records.plan_change(
                RecordsChange(sources=(source,)), RecordsContext(unit, str(uuid4()))
            ),
            unit,
        )
        records.replace_source_uses("business", fact["business_id"], analysis.source_ids, unit)
        records.mark_document_targets(("objects", "facts:money:2026-01"), unit)
    with storage.read_unit(company.company_id) as read:
        capture = records.capture_documents(_identity(company), [fact], [], read)
    generated = records.prepare_documents(capture)
    with storage.maintenance_unit(company.company_id) as unit:
        records.publish_documents(generated, unit)
    with storage.read_unit(company.company_id) as read:
        records.verify_snapshot(_identity(company), [analysis], [], read)
    changed = deepcopy(fact)
    _locator_objects(changed)[0]["location"] = "同一来源的另一处记录"
    with storage.read_unit(company.company_id) as read, pytest.raises(ContractError):
        records.verify_snapshot(_identity(company), [analyze_fact(changed)], [], read)
    with storage.write_unit(company.company_id) as unit:
        records.replace_source_uses("business", fact["business_id"], (), unit)
    with storage.read_unit(company.company_id) as read, pytest.raises(ContractError):
        records.verify_snapshot(_identity(company), [analysis], [], read)


def test_unnormalized_valid_evidence_order_keeps_existing_document_semantics(records_case):
    storage, (company, _), records = records_case
    fact, obj = prepared(records_case)
    with storage.read_unit(company.company_id) as read:
        extra = records.prepare_source({"kind": "owner_statement", "text": "第二份合成确认"}, read)
    fact["evidence_ids"] = sorted([*fact["evidence_ids"], extra["source_id"]], reverse=True)
    analysis = analyze_fact(fact)
    with storage.write_unit(company.company_id) as unit:
        records.apply_current(
            records.plan_change(
                RecordsChange(sources=(extra,)), RecordsContext(unit, str(uuid4()))
            ),
            unit,
        )
        records.replace_source_uses("business", fact["business_id"], analysis.source_ids, unit)
        records.mark_document_targets(("facts:operations:2026-01",), unit)
    with storage.read_unit(company.company_id) as read:
        capture = records.capture_documents(_identity(company), [fact], None, read)
    generated = records.prepare_documents(capture)
    with storage.maintenance_unit(company.company_id) as unit:
        records.publish_documents(generated, unit)
    with storage.read_unit(company.company_id) as read:
        assert records.verify_snapshot(_identity(company), [analysis], [obj], read)
    assert fact["evidence_ids"] == sorted(fact["evidence_ids"], reverse=True)
