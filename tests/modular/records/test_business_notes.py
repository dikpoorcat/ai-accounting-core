"""Business descriptions retain reality, source links and a replaceable current body."""

from uuid import uuid4

import pytest
from test_documents import _first_facts, _prepare, _publish, company_identity
from test_standard_bundle import rewrite

from ai_accounting_modular.contracts.public import ContractError, content_digest
from ai_accounting_modular.records._changes import document_note, note_digest
from ai_accounting_modular.records._repository import RecordsRepository
from ai_accounting_modular.records.public import (
    RecordsChange,
    RecordsContext,
    copy_standard_bundle,
    open_standard_bundle,
)


def source(case, text):
    storage, (company, _), records = case
    with storage.read_unit(company.company_id) as read:
        return records.prepare_source({"kind": "owner_statement", "text": text}, read)


def save(case, note, *sources):
    storage, (company, _), records = case
    with storage.write_unit(company.company_id) as unit:
        plan = records.plan_change(
            RecordsChange(notes=(note,), sources=sources), RecordsContext(unit, str(uuid4()))
        )
        records.apply_current(plan, unit)
        return plan


def current(case, scope):
    storage, (company, _), records = case
    with storage.read_unit(company.company_id) as read:
        return records.current_notes((scope,), read), read.head


def note(scope, source_id, *, text="支付工厂租金，供本公司日常经营使用。", expected_seq=0):
    return {
        "scope": scope,
        "text": text,
        "evidence_ids": [source_id],
        "expected_seq": expected_seq,
    }


def test_save_replace_retry_and_conflict_use_stable_scope_and_existing_sequence(records_case):
    storage, (company, _), records = records_case
    first = source(records_case, "负责人确认工厂租金用途。")
    second = source(records_case, "负责人确认该租金实际用于仓库。")
    scope = "business:" + str(uuid4())
    original = note(scope, first["source_id"], text="经营用途" * 1000)
    assert save(records_case, original, first).changed_scopes == ("notes", "sources")
    assert save(records_case, original).changed_scopes == ()
    saved, head = current(records_case, scope)
    assert head == 1 and saved[0]["seq"] == 1 and saved[0]["basis_head"] is None
    replacement = note(scope, second["source_id"], text="仓库租金", expected_seq=1)
    save(records_case, replacement, second)
    assert save(records_case, replacement).changed_scopes == ()
    with storage.read_unit(company.company_id) as read:
        row = read.repository("records").notes((scope,))[scope]
        assert row["note_text"] == "仓库租金" and row["seq"] == 2
        assert read.repository("records").uses("note", scope) == (second["source_id"],)
        # Immutable original evidence remains, while the old current body is replaced.
        assert records.resolve_sources((first["source_id"],), read) == (first,)
    with pytest.raises(ContractError) as caught:
        save(records_case, original | {"expected_seq": 1})
    assert caught.value.code == "conflict"
    assert current(records_case, scope)[0][0]["text"] == "仓库租金"


def test_purpose_and_detail_share_full_replacement_sources_sequence_and_retry(records_case):
    storage, (company, _), records = records_case
    evidence = source(records_case, "负责人确认本项为仓库租金，并提供完整租赁说明。")
    scope = "business:" + str(uuid4())
    original = note(scope, evidence["source_id"]) | {"purpose": "仓库租金"}
    save(records_case, original, evidence)
    assert save(records_case, original).changed_scopes == ()
    saved = current(records_case, scope)[0][0]
    assert saved["purpose"] == "仓库租金" and saved["text"] == original["text"]

    short_only = original | {"text": "", "expected_seq": 1}
    save(records_case, short_only)
    assert save(records_case, short_only).changed_scopes == ()
    saved = current(records_case, scope)[0][0]
    assert saved["seq"] == 2 and saved["state"] == "current"
    assert saved["purpose"] == "仓库租金" and saved["text"] == ""
    with storage.read_unit(company.company_id) as read:
        assert read.repository("records").uses("note", scope) == (evidence["source_id"],)

    detailed_only = note(scope, evidence["source_id"], expected_seq=2)
    save(records_case, detailed_only)
    assert "purpose" not in current(records_case, scope)[0][0]
    assert save(records_case, detailed_only | {"purpose": None}).changed_scopes == ()
    with pytest.raises(ContractError) as caught:
        save(records_case, short_only)
    assert caught.value.code == "conflict"

    save(records_case, original | {"text": "", "expected_seq": 3})
    cleared = {"scope": scope, "text": "", "purpose": None, "evidence_ids": [], "expected_seq": 4}
    save(records_case, cleared)
    assert save(records_case, cleared).changed_scopes == ()
    saved = current(records_case, scope)[0][0]
    assert saved["seq"] == 5 and saved["state"] == "needs_refresh"
    assert saved["text"] is None and "purpose" not in saved
    with storage.read_unit(company.company_id) as read:
        assert read.repository("records").uses("note", scope) == ()


def test_null_purpose_preserves_existing_digest_sequence_and_document_bytes(records_case):
    storage, (company, _), records = records_case
    evidence = source(records_case, "已有完整业务说明的真实依据。")
    scope = "business:" + str(uuid4())
    original = note(scope, evidence["source_id"])
    save(records_case, original, evidence)
    saved, head = current(records_case, scope)
    expected = {
        key: saved[0][key] for key in ("scope", "state", "text", "basis_head", "evidence_ids")
    }
    digest = content_digest(expected)
    assert note_digest(saved[0]) == note_digest(saved[0] | {"purpose": None}) == digest
    assert document_note(saved[0]) == document_note(saved[0] | {"purpose": None})
    assert save(records_case, original | {"purpose": None}).changed_scopes == ()
    assert current(records_case, scope) == (saved, head)
    with storage.read_unit(company.company_id) as read:
        row = read.repository("records").notes((scope,))[scope]
        assert row["purpose"] is None and row["content_digest"] == digest
    save(records_case, original | {"purpose": "仓库租金", "expected_seq": 1})
    saved = current(records_case, scope)[0][0]
    assert saved["seq"] == 2 and note_digest(saved) != digest


def test_missing_description_source_rejects_entire_change_and_stale_plan_is_atomic(records_case):
    storage, (company, _), records = records_case
    evidence = source(records_case, "合成合法资料")
    scope = "business:" + str(uuid4())
    with pytest.raises(ContractError) as caught:
        save(records_case, note(scope, str(uuid4())), evidence)
    assert caught.value.code == "invalid_input"
    assert [issue["code"] for issue in caught.value.fact_issues] == ["source_not_found"]
    with storage.read_unit(company.company_id) as read:
        assert read.head == 0 and records.current_notes(None, read) == ()
        assert records.resolve_sources((evidence["source_id"],), read) == ()
    save(records_case, note(scope, evidence["source_id"]), evidence)
    with storage.read_unit(company.company_id) as read:
        stale = records.plan_change(
            RecordsChange(notes=(note(scope, evidence["source_id"], expected_seq=1),)),
            RecordsContext(read, str(uuid4())),
        )
    replacement = note(scope, evidence["source_id"], expected_seq=1, text="另一项已核实的用途")
    save(records_case, replacement)
    with pytest.raises(ContractError) as caught:
        with storage.write_unit(company.company_id) as unit:
            records.apply_current(stale, unit)
    assert caught.value.code == "conflict"
    assert current(records_case, scope)[0][0]["text"] == replacement["text"]


def test_empty_change_clears_body_and_sources_with_sequence_and_retry(records_case):
    storage, (company, _), records = records_case
    evidence = source(records_case, "此项经营事实的原始确认")
    scope = "business:" + str(uuid4())
    save(records_case, note(scope, evidence["source_id"]), evidence)
    clear = {"scope": scope, "text": "", "evidence_ids": [], "expected_seq": 1}
    assert save(records_case, clear).changed_scopes == ("notes", "sources")
    assert save(records_case, clear).changed_scopes == ()
    cleared, head = current(records_case, scope)
    assert cleared == (
        {
            "scope": scope,
            "seq": 2,
            "state": "needs_refresh",
            "text": None,
            "basis_head": None,
            "evidence_ids": [],
        },
    )
    with storage.read_unit(company.company_id) as read:
        assert read.repository("records").uses("note", scope) == ()
        assert records.current_issues(scope, read) == {"scope": scope, "head": 0, "issues": []}
    save(records_case, note(scope, evidence["source_id"], expected_seq=2))
    assert current(records_case, scope)[0][0]["seq"] == 3
    assert current(records_case, scope)[1] == head + 1


def test_internal_clear_protects_company_notes_and_preserves_owner_work(records_case):
    storage, (company, _), records = records_case
    evidence = source(records_case, "公司、月份及业务共同使用的现实资料")
    scope = "business:" + str(uuid4())
    owner_issue = {
        "issue_id": str(uuid4()),
        "scope": scope,
        "required_before_close": None,
        "question": "确认另一项真实事项。",
        "evidence_ids": [evidence["source_id"]],
        "missing_fields": [],
        "assignee": "owner",
        "action": "clarify_reality",
    }
    with storage.write_unit(company.company_id) as unit:
        records.apply_current(
            records.plan_change(
                RecordsChange(
                    sources=(evidence,),
                    notes=(
                        note(scope, evidence["source_id"]),
                        note("company", evidence["source_id"]),
                        note("period:2026-01", evidence["source_id"])
                        | {"expected_basis_head": "month-basis"},
                    ),
                    unresolved=(owner_issue,),
                    unresolved_scope=scope,
                    expected_scope_head=0,
                ),
                RecordsContext(unit, str(uuid4()), {"period:2026-01": "month-basis"}),
            ),
            unit,
        )
    with storage.write_unit(company.company_id) as unit:
        with pytest.raises(ContractError):
            records.clear_notes((scope, "company"), str(uuid4()), unit)
    assert current(records_case, scope)[0][0]["seq"] == 1
    with storage.write_unit(company.company_id) as unit:
        assert records.clear_notes((scope,), str(uuid4()), unit) == (scope,)
    with storage.write_unit(company.company_id) as unit:
        assert records.clear_notes((scope,), str(uuid4()), unit) == ()
        assert records.clear_notes(("business:" + str(uuid4()),), str(uuid4()), unit) == ()
    with storage.read_unit(company.company_id) as read:
        notes = {value["scope"]: value for value in records.current_notes(None, read)}
        assert notes[scope]["seq"] == 2 and notes[scope]["text"] is None
        assert notes["company"]["seq"] == notes["period:2026-01"]["seq"] == 1
        assert records.current_issues(scope, read)["issues"] == [owner_issue]
        assert read.repository("records").uses("note", scope) == ()


def test_internal_empty_clear_does_not_read_notes_or_advance_version(records_case, monkeypatch):
    storage, (company, _), records = records_case

    def forbid_note_read(*args, **kwargs):
        raise AssertionError("空清除集合不应查询任何说明。")

    with monkeypatch.context() as patch:
        patch.setattr(RecordsRepository, "notes", forbid_note_read)
        with storage.write_unit(company.company_id) as unit:
            assert records.clear_notes((), str(uuid4()), unit) == ()
    with storage.read_unit(company.company_id) as read:
        assert read.head == 0 and records.current_notes(None, read) == ()


@pytest.mark.parametrize("detail", ["", "支付工厂租金，供本公司日常经营使用。"])
def test_business_description_survives_fact_changes_copy_and_rejects_orphan_archive(
    records_case, tmp_path, detail
):
    storage, (company, _), records = records_case
    fact, obj = _first_facts(records_case)
    evidence = source(records_case, "公司确认这项费用用于仓库经营。")
    scope = "business:" + fact["business_id"]
    save(
        records_case,
        note(scope, evidence["source_id"], text=detail) | {"purpose": "仓库租金"},
        evidence,
    )
    _publish(records_case, _prepare(records_case, [fact], [obj]))
    new_fact_source = source(records_case, "金额与核算所属期修改后的新原始凭据。")
    changed = fact | {
        "period": "2026-02",
        "data": fact["data"] | {"amount_fen": "20000"},
        "evidence_ids": [new_fact_source["source_id"]],
    }
    with storage.write_unit(company.company_id) as unit:
        assert records.invalidate_notes({scope: "changed-fact"}, str(uuid4()), unit) == ()
        records.apply_current(
            records.plan_change(
                RecordsChange(sources=(new_fact_source,)), RecordsContext(unit, str(uuid4()))
            ),
            unit,
        )
        records.replace_source_uses("business", fact["business_id"], changed["evidence_ids"], unit)
        records.mark_document_targets(
            ("facts:operations:2026-01", "facts:operations:2026-02"), unit
        )
    _publish(records_case, _prepare(records_case, [changed], None))
    with storage.read_unit(company.company_id) as read:
        records.verify_snapshot(company_identity(company), [changed], [obj], read)
    path = company.company_root / "standard/current.json"
    bundle = open_standard_bundle(path)
    assert bundle.notes == (
        {
            "scope": scope,
            "purpose": "仓库租金",
            "text": detail,
            "evidence_ids": [evidence["source_id"]],
        },
    )
    copy_standard_bundle(bundle, tmp_path / "independent")
    copied = open_standard_bundle(tmp_path / "independent/standard/current.json")
    assert copied.notes == bundle.notes and copied.sources == bundle.sources
    # A correctly re-signed archive must still prove the description's business exists.
    rewrite(path, lambda _, parts: parts.pop("facts:operations:2026-02"))
    with pytest.raises(ContractError) as caught:
        open_standard_bundle(path)
    assert caught.value.code == "documents_corrupt"


def test_cleared_description_is_not_exported_and_unused_source_is_not_lost(records_case):
    storage, (company, _), records = records_case
    fact, obj = _first_facts(records_case)
    evidence = source(records_case, "只供该用途说明引用的原始确认")
    scope = "business:" + fact["business_id"]
    save(records_case, note(scope, evidence["source_id"]), evidence)
    _publish(records_case, _prepare(records_case, [fact], [obj]))
    with storage.write_unit(company.company_id) as unit:
        records.clear_notes((scope,), str(uuid4()), unit)
    _publish(records_case, _prepare(records_case, [], None))
    bundle = open_standard_bundle(company.company_root / "standard/current.json")
    assert bundle.notes == () and evidence not in bundle.sources
    with storage.read_unit(company.company_id) as read:
        assert records.resolve_sources((evidence["source_id"],), read) == (evidence,)
        records.verify_snapshot(company_identity(company), [fact], [obj], read)
