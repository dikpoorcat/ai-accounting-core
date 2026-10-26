"""Capture shares strict fact shards while external snapshots stay untrusted."""

from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest
from test_documents import _first_facts, _prepare, _publish, company_identity

from ai_accounting_modular.contracts.public import ContractError, canonical_json, loads_json
from ai_accounting_modular.records.public import RecordsChange, RecordsContext, open_standard_bundle


def _bank_fact(source_id):
    from tests.modular.contracts.test_fact_sources import _facts, _locator_objects

    fact = deepcopy(next(value for value in _facts() if value["kind"] == "money.bank_statement"))
    fact["period"] = "2026-01"
    fact["evidence_ids"] = []
    for locator in _locator_objects(fact):
        locator["source_id"] = source_id
    return fact


def test_capture_detaches_nested_sources_and_preserves_other_shards_source_union(records_case):
    from tests.modular.contracts.test_fact_sources import _locator_objects

    storage, (company, _), records = records_case
    first, obj = _first_facts(records_case)
    _publish(records_case, _prepare(records_case, [first], [obj]))
    with storage.read_unit(company.company_id) as read:
        source = records.prepare_source({"kind": "owner_statement", "text": "银行原行确认"}, read)
    fact = _bank_fact(source["source_id"])
    expected = deepcopy(fact)
    with storage.write_unit(company.company_id) as unit:
        records.apply_current(
            records.plan_change(
                RecordsChange(sources=(source,)), RecordsContext(unit, str(uuid4()))
            ),
            unit,
        )
        records.replace_source_uses("business", fact["business_id"], (source["source_id"],), unit)
        records.mark_document_targets(("facts:money:2026-01",), unit)
    with storage.read_unit(company.company_id) as read:
        snapshot = records.capture_documents(company_identity(company), [fact], None, read)
    _locator_objects(fact)[0]["source_id"] = str(uuid4())
    fact["evidence_ids"].append(str(uuid4()))
    _publish(records_case, records.prepare_documents(snapshot))
    bundle = open_standard_bundle(company.company_root / "standard/current.json")
    assert {item["business_id"]: item for item in bundle.facts} == {
        first["business_id"]: first,
        expected["business_id"]: expected,
    }
    assert {item["source_id"] for item in bundle.sources} == {
        *first["evidence_ids"],
        source["source_id"],
    }


def test_capture_rejects_missing_nested_source_even_without_top_level_evidence(records_case):
    storage, (company, _), records = records_case
    missing = str(uuid4())
    fact = _bank_fact(missing)
    with storage.write_unit(company.company_id) as unit:
        records.mark_document_targets(("facts:money:2026-01",), unit)
    with storage.read_unit(company.company_id) as read, pytest.raises(ContractError) as error:
        records.capture_documents(company_identity(company), [fact], [], read)
    assert error.value.code == "invalid_input"
    assert [issue["code"] for issue in error.value.fact_issues] == ["source_not_found"]
    assert missing in error.value.fact_issues[0]["message"]
    assert not (company.company_root / "standard/current.json").exists()


@pytest.mark.parametrize("boundary", ["capture", "external_snapshot"])
@pytest.mark.parametrize(
    "damage", ["missing_amount", "bool_amount", "unknown_field", "duplicate_evidence"]
)
def test_strict_fact_validation_remains_at_capture_and_external_snapshot_prepare(
    records_case, boundary, damage
):
    storage, (company, _), records = records_case
    fact, obj = _first_facts(records_case)
    with storage.read_unit(company.company_id) as read:
        snapshot = records.capture_documents(company_identity(company), [fact], [obj], read)
    if damage == "missing_amount":
        del fact["data"]["amount_fen"]
    elif damage == "bool_amount":
        fact["data"]["amount_fen"] = True
    elif damage == "unknown_field":
        fact["data"]["unrecognized"] = "不能成为正式事实"
    else:
        fact["evidence_ids"] *= 2
    with pytest.raises(ContractError):
        if boundary == "capture":
            with storage.read_unit(company.company_id) as read:
                records.capture_documents(company_identity(company), [fact], [obj], read)
        else:
            parts = [loads_json(raw) for raw in snapshot.parts_json]
            next(part for part in parts if "facts" in part)["facts"] = [fact]
            external = replace(snapshot, parts_json=tuple(canonical_json(part) for part in parts))
            records.prepare_documents(external)
    assert not (company.company_root / "standard/current.json").exists()
    with storage.read_unit(company.company_id) as read:
        assert read.head == snapshot.head
        assert records.document_status(read)["state"] == "pending"


@pytest.mark.parametrize(
    "bad",
    [
        None,
        False,
        [],
        {},
        {"kind": None, "period": "2026-01"},
        {"kind": "operations.expense", "period": []},
    ],
)
def test_capture_reports_invalid_raw_fact_structure_as_a_contract_error(records_case, bad):
    storage, (company, _), records = records_case
    _first_facts(records_case)
    with storage.read_unit(company.company_id) as read, pytest.raises(ContractError) as error:
        records.capture_documents(company_identity(company), [bad], [], read)
    assert error.value.code == "invalid_input"


def test_capture_rejects_one_business_appearing_in_two_complete_month_shards(records_case):
    storage, (company, _), records = records_case
    first, obj = _first_facts(records_case)
    second = deepcopy(first)
    second["period"] = "2026-02"
    with storage.write_unit(company.company_id) as unit:
        records.mark_document_targets(("facts:operations:2026-02",), unit)
    with storage.read_unit(company.company_id) as read, pytest.raises(ContractError) as error:
        records.capture_documents(company_identity(company), [first, second], [obj], read)
    assert error.value.code == "invalid_input"
    assert "多个标准分片" in str(error.value)
