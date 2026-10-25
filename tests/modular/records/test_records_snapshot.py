"""Records prove the complete saved final-document set without repairing it."""

from uuid import uuid4

import pytest
from test_documents import _first_facts, _prepare, _publish, company_identity

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.records.public import RecordsChange, RecordsContext


def prepared(case):
    fact, obj = _first_facts(case)
    _publish(case, _prepare(case, [fact], [obj]))
    return fact, obj


def test_saved_documents_prove_complete_facts_without_republication(records_case):
    storage, (company, _), records = records_case
    fact, obj = prepared(records_case)
    before = (company.company_root / "standard/current.json").read_bytes()
    with storage.read_unit(company.company_id) as read:
        snapshot = records.verify_snapshot(company_identity(company), [fact], [obj], read)
        assert snapshot.document_set_id == records.document_status(read)["set_id"]
        assert (
            len(snapshot.files) == 6
        )  # manifest, README, objects/work, one source and one fact part
        assert read.head == 1
    assert (company.company_root / "standard/current.json").read_bytes() == before


@pytest.mark.parametrize("fault", ["fact", "object", "missing_fact", "identity", "frozen_source"])
def test_valid_manifest_is_not_proof_of_matching_database(records_case, fault):
    storage, (company, _), records = records_case
    fact, obj = prepared(records_case)
    identity, facts, objects, required = company_identity(company), [fact], [obj], ()
    if fault == "fact":
        facts = [fact | {"data": fact["data"] | {"amount_fen": "12000"}}]
    elif fault == "object":
        objects = [obj | {"name": "另一供应商"}]
    elif fault == "missing_fact":
        facts = []
    elif fault == "identity":
        identity = identity | {"legal_name": "另一公司"}
    else:
        required = (str(uuid4()),)
    with storage.read_unit(company.company_id) as read, pytest.raises(ContractError):
        records.verify_snapshot(identity, facts, objects, read, required_source_ids=required)


def test_inbox_originals_are_preserved_even_without_current_fact_reference(records_case):
    storage, (company, _), records = records_case
    fact, obj = prepared(records_case)
    path = company.company_root / "originals/inbox.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"unprocessed receipt")
    with storage.read_unit(company.company_id) as read:
        source = records.prepare_source(
            {"kind": "file", "relative_path": "originals/inbox.txt"}, read
        )
    with storage.write_unit(company.company_id) as unit:
        records.apply_current(
            records.plan_change(
                RecordsChange(sources=(source,)), RecordsContext(unit, str(uuid4()))
            ),
            unit,
        )
    _publish(records_case, _prepare(records_case, [], None))
    with storage.read_unit(company.company_id) as read:
        snapshot = records.verify_snapshot(company_identity(company), [fact], [obj], read)
        assert source["relative_path"] in {item.relative_path for item in snapshot.files}
        assert len(snapshot.files) == 7
    (company.company_root / source["relative_path"]).write_bytes(b"changed inbox")
    with storage.read_unit(company.company_id) as read, pytest.raises(ContractError):
        records.verify_snapshot(company_identity(company), [fact], [obj], read)


def test_stale_source_membership_cannot_hide_in_a_consistent_document_set(records_case):
    storage, (company, _), records = records_case
    fact, obj = prepared(records_case)
    with storage.write_unit(company.company_id) as unit:
        records.replace_source_uses("object", str(uuid4()), fact["evidence_ids"], unit)
    _publish(records_case, _prepare(records_case, [], None))
    with storage.read_unit(company.company_id) as read, pytest.raises(ContractError) as error:
        records.verify_snapshot(company_identity(company), [fact], [obj], read)
    assert error.value.code == "storage_corrupt"


def test_complete_internal_issue_scope_can_exceed_one_wire_submission(records_case):
    storage, (company, _), records = records_case
    issues = tuple(
        {
            "issue_id": str(uuid4()),
            "scope": "month:2026-01",
            "period": "2026-01",
            "evidence_ids": [],
            "missing_fields": ["/data/amount_fen"],
            "question": "请核实这笔费用的金额。",
            "assignee": "owner",
            "action": "clarify_reality",
        }
        for _ in range(501)
    )
    with storage.write_unit(company.company_id) as unit:
        records.apply_current(
            records.plan_change(
                RecordsChange(
                    unresolved=issues, unresolved_scope="month:2026-01", expected_scope_head=0
                ),
                RecordsContext(unit, str(uuid4())),
            ),
            unit,
        )
    with storage.read_unit(company.company_id) as read:
        assert len(records.current_issues("month:2026-01", read)["issues"]) == 501
    _publish(records_case, _prepare(records_case, [], []))
    with storage.read_unit(company.company_id) as read:
        records.verify_snapshot(company_identity(company), [], [], read)
