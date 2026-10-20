from __future__ import annotations

from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import (
    CONTRACT,
    ContractError,
    loads_json,
    validate_contract,
)
from ai_accounting_modular.records.public import RecordsChange, RecordsContext


def test_notes_replace_current_text_invalidate_only_changed_period_and_preserve_seq(records_case):
    storage, (company, _), records = records_case
    command = str(uuid4())
    company_note = {
        "scope": "company",
        "expected_seq": 0,
        "text": "按项目提供服务",
        "evidence_ids": [],
    }
    month_note = {
        "scope": "period:2026-01",
        "expected_seq": 0,
        "text": "本月费用100元",
        "evidence_ids": [],
        "expected_basis_head": "basis-1",
    }
    change = RecordsChange(notes=(company_note, month_note))
    for _ in range(2):
        with storage.write_unit(company.company_id) as unit:
            plan = records.plan_change(
                change, RecordsContext(unit, command, {"period:2026-01": "basis-1"})
            )
            records.apply_current(plan, unit)
    with storage.write_unit(company.company_id) as unit:
        assert unit.head == 1
        changed = records.invalidate_notes({"period:2026-01": "basis-2"}, str(uuid4()), unit)
        assert changed == ("period:2026-01",)
    with storage.read_unit(company.company_id) as read:
        notes = records.current_notes(None, read)
        assert notes[0]["text"] == "按项目提供服务"
        assert notes[1] == {
            "scope": "period:2026-01",
            "seq": 2,
            "state": "needs_refresh",
            "text": None,
            "basis_head": "basis-2",
            "evidence_ids": [],
        }
    with storage.write_unit(company.company_id) as unit:
        replacement = month_note | {
            "expected_seq": 2,
            "expected_basis_head": "basis-2",
            "text": "本月最终费用120元",
        }
        plan = records.plan_change(
            RecordsChange(notes=(replacement,)),
            RecordsContext(unit, str(uuid4()), {"period:2026-01": "basis-2"}),
        )
        records.apply_current(plan, unit)
    with storage.read_unit(company.company_id) as read:
        assert records.current_notes(("period:2026-01",), read)[0]["seq"] == 3
    with pytest.raises(ContractError) as error:
        with storage.write_unit(company.company_id) as unit:
            records.plan_change(
                RecordsChange(notes=(month_note,)),
                RecordsContext(unit, str(uuid4()), {"period:2026-01": "basis-2"}),
            )
    assert error.value.code == "conflict"


def test_scope_replacement_clears_resolved_issues_without_work_history(records_case):
    storage, (company, _), records = records_case
    issue = {
        "issue_id": str(uuid4()),
        "scope": "month:2026-01",
        "period": "2026-01",
        "business_id": str(uuid4()),
        "evidence_ids": [],
        "missing_fields": ["/data/creditor_id"],
        "question": "费用应付给谁？",
        "assignee": "owner",
        "action": "clarify_reality",
    }
    change = RecordsChange(
        unresolved=(issue,), unresolved_scope="month:2026-01", expected_scope_head=0
    )
    for _ in range(2):
        with storage.write_unit(company.company_id) as unit:
            plan = records.plan_change(change, RecordsContext(unit, str(uuid4())))
            assert plan.issue_scope["head"] == 1
            records.apply_current(plan, unit)
    with storage.read_unit(company.company_id) as read:
        assert read.head == 1
        assert records.current_issues("month:2026-01", read)["issues"] == [issue]
    with storage.write_unit(company.company_id) as unit:
        clear = RecordsChange(
            unresolved=(), unresolved_scope="month:2026-01", expected_scope_head=1
        )
        records.apply_current(records.plan_change(clear, RecordsContext(unit, str(uuid4()))), unit)
    with storage.read_unit(company.company_id) as read:
        assert records.current_issues("month:2026-01", read) == {
            "scope": "month:2026-01",
            "head": 2,
            "issues": [],
        }
    with pytest.raises(ContractError) as error:
        with storage.write_unit(company.company_id) as unit:
            records.plan_change(change, RecordsContext(unit, str(uuid4())))
    assert error.value.code == "conflict"


def test_issue_identity_cannot_move_to_another_scope_implicitly(records_case):
    storage, (company, _), records = records_case
    issue = {
        "issue_id": str(uuid4()),
        "scope": "first",
        "evidence_ids": [],
        "missing_fields": [],
        "question": "AI继续核查",
        "assignee": "ai",
        "action": "professional_check",
    }
    with storage.write_unit(company.company_id) as unit:
        change = RecordsChange(unresolved=(issue,), unresolved_scope="first", expected_scope_head=0)
        records.apply_current(records.plan_change(change, RecordsContext(unit, str(uuid4()))), unit)
    with pytest.raises(ContractError) as error:
        with storage.write_unit(company.company_id) as unit:
            change = RecordsChange(
                unresolved=(issue | {"scope": "second"},),
                unresolved_scope="second",
                expected_scope_head=0,
            )
            records.plan_change(change, RecordsContext(unit, str(uuid4())))
    assert error.value.code == "conflict"


def test_legal_501_note_submission_is_saved_and_exported_completely(records_case):
    storage, (company, _), records = records_case
    notes = [
        {
            "scope": f"period:{2000 + index // 12:04d}-{1 + index % 12:02d}",
            "expected_seq": 0,
            "text": "该月已核对的经营说明",
            "evidence_ids": [],
            "expected_basis_head": f"basis:{index}",
        }
        for index in range(501)
    ]
    command_id = str(uuid4())
    validate_contract(
        "core.schema.json",
        "ApplyRequest",
        {
            "contract": CONTRACT,
            "company_id": company.company_id,
            "command_id": command_id,
            "mutations": [],
            "notes": notes,
        },
    )
    with storage.write_unit(company.company_id) as unit:
        context = RecordsContext(
            unit, command_id, {note["scope"]: note["expected_basis_head"] for note in notes}
        )
        records.apply_current(records.plan_change(RecordsChange(notes=tuple(notes)), context), unit)
    with storage.read_unit(company.company_id) as read:
        assert len(records.current_notes(None, read)) == 501
        snapshot = records.capture_documents(
            {
                "credit_code": company.credit_code,
                "legal_name": company.legal_name,
                "accounting_standard": "small_enterprise",
                "currency": "CNY",
                "start_period": company.start_period,
            },
            [],
            [],
            read,
        )
    exported = next(
        loads_json(raw)["notes"] for raw in snapshot.parts_json if loads_json(raw)["key"] == "notes"
    )
    assert len(exported) == 501
    assert [note["scope"] for note in exported] == [note["scope"] for note in notes]
