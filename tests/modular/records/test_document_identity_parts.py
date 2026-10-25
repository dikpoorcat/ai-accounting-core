"""Ordinary publication selects only the changed note/source bodies, never all history."""

from uuid import uuid4

import pytest
from test_documents import _first_facts, _prepare, _publish, company_identity
from test_standard_bundle import rewrite

from ai_accounting_modular.contracts.public import ContractError, loads_json
from ai_accounting_modular.records._repository import RecordsRepository
from ai_accounting_modular.records.public import RecordsChange, RecordsContext, open_standard_bundle


def _save(case, **values):
    storage, (company, _), records = case
    with storage.write_unit(company.company_id) as unit:
        records.apply_current(
            records.plan_change(RecordsChange(**values), RecordsContext(unit, str(uuid4()))), unit
        )


def _source(case, text):
    storage, (company, _), records = case
    with storage.read_unit(company.company_id) as read:
        return records.prepare_source({"kind": "owner_statement", "text": text}, read)


def _parts(case):
    company = case[1][0]
    return {
        item["key"]: item
        for item in loads_json((company.company_root / "standard/current.json").read_bytes())[
            "shards"
        ]
    }


@pytest.mark.parametrize("unrelated_count", [0, 40])
def test_one_description_edit_reads_and_writes_only_its_own_body(
    records_case, monkeypatch, unrelated_count
):
    storage, (company, _), records = records_case
    fact, obj = _first_facts(records_case)
    facts = [fact] + [fact | {"business_id": str(uuid4())} for _ in range(unrelated_count)]
    source = _source(records_case, "这些用途都有原始说明支持。")
    notes = tuple(
        {
            "scope": "business:" + item["business_id"],
            "text": "厂房租金",
            "evidence_ids": [source["source_id"]],
            "expected_seq": 0,
        }
        for item in facts
    )
    _save(records_case, sources=(source,), notes=notes)
    if unrelated_count:
        with storage.write_unit(company.company_id) as unit:
            for item in facts[1:]:
                records.replace_source_uses(
                    "business", item["business_id"], item["evidence_ids"], unit
                )
    _publish(records_case, _prepare(records_case, facts, [obj]))
    before = _parts(records_case)
    stamps = {
        key: (company.company_root / value["path"]).stat().st_mtime_ns
        for key, value in before.items()
    }
    changed_note = notes[0] | {"text": "仓库租金", "expected_seq": 1}
    _save(records_case, notes=(changed_note,))
    original_notes, original_sources = RecordsRepository.notes, RecordsRepository.sources
    reads = {"notes": [], "sources": []}

    def selected_notes(self, scopes=None):
        assert scopes is not None, "普通发布不能读取全部说明。"
        reads["notes"].append(tuple(scopes))
        return original_notes(self, scopes)

    def selected_sources(self, ids):
        reads["sources"].append(tuple(ids))
        return original_sources(self, ids)

    def forbid_all_sources(self):
        raise AssertionError("普通发布不能扫描全部来源引用。")

    with monkeypatch.context() as patch:
        patch.setattr(RecordsRepository, "notes", selected_notes)
        patch.setattr(RecordsRepository, "sources", selected_sources)
        patch.setattr(RecordsRepository, "used_source_ids", forbid_all_sources)
        prepared = _prepare(records_case, [], None)
        assert prepared.snapshot.scopes == ("notes:" + notes[0]["scope"],)
        assert len(prepared.snapshot.parts_json) == 1
        _publish(records_case, prepared)
    assert reads == {"notes": [(notes[0]["scope"],)], "sources": [(source["source_id"],)]}
    after = _parts(records_case)
    for key in before.keys() - {"notes:" + notes[0]["scope"]}:
        assert after[key] == before[key]
        assert (company.company_root / after[key]["path"]).stat().st_mtime_ns == stamps[key]
    with storage.read_unit(company.company_id) as read:
        records.verify_snapshot(company_identity(company), facts, [obj], read)


@pytest.mark.parametrize("owner", ["business", "object", "issue", "period_note", "business_note"])
def test_last_reference_removes_only_that_source_part(records_case, owner):
    storage, (company, _), records = records_case
    fact, obj = _first_facts(records_case)
    extra = _source(records_case, "只供这一项当前内容采用的资料。")
    _save(records_case, sources=(extra,))
    issue = {
        "issue_id": str(uuid4()),
        "scope": "materials",
        "evidence_ids": [extra["source_id"]],
        "missing_fields": [],
        "question": "继续核查事项。",
        "assignee": "ai",
        "action": "provide_material",
    }
    note_scope = "period:2026-01" if owner == "period_note" else "business:" + fact["business_id"]
    with storage.write_unit(company.company_id) as unit:
        if owner == "business":
            fact = fact | {"evidence_ids": sorted([*fact["evidence_ids"], extra["source_id"]])}
            records.replace_source_uses("business", fact["business_id"], fact["evidence_ids"], unit)
        elif owner == "object":
            obj = obj | {"evidence_ids": [extra["source_id"]]}
            records.replace_source_uses("object", obj["object_id"], obj["evidence_ids"], unit)
        else:
            change = (
                RecordsChange(
                    unresolved=(issue,), unresolved_scope="materials", expected_scope_head=0
                )
                if owner == "issue"
                else RecordsChange(
                    notes=(
                        {
                            "scope": note_scope,
                            "text": "已核实的现实用途",
                            "evidence_ids": [extra["source_id"]],
                            "expected_seq": 0,
                            **({"expected_basis_head": "basis"} if owner == "period_note" else {}),
                        },
                    )
                )
            )
            records.apply_current(
                records.plan_change(
                    change, RecordsContext(unit, str(uuid4()), {note_scope: "basis"})
                ),
                unit,
            )
    _publish(records_case, _prepare(records_case, [fact], [obj]))
    before = _parts(records_case)
    assert "sources:" + extra["source_id"] in before
    with storage.write_unit(company.company_id) as unit:
        if owner == "business":
            fact = fact | {
                "evidence_ids": [
                    value for value in fact["evidence_ids"] if value != extra["source_id"]
                ]
            }
            records.replace_source_uses("business", fact["business_id"], fact["evidence_ids"], unit)
            records.mark_document_targets(("facts:operations:2026-01",), unit)
        elif owner == "object":
            obj = {key: value for key, value in obj.items() if key != "evidence_ids"}
            records.replace_source_uses("object", obj["object_id"], (), unit)
            records.mark_document_targets(("objects",), unit)
        elif owner == "issue":
            records.apply_current(
                records.plan_change(
                    RecordsChange(
                        unresolved=(), unresolved_scope="materials", expected_scope_head=1
                    ),
                    RecordsContext(unit, str(uuid4())),
                ),
                unit,
            )
        elif owner == "period_note":
            records.invalidate_notes({note_scope: "new-basis"}, str(uuid4()), unit)
        else:
            records.clear_notes((note_scope,), str(uuid4()), unit)
    _publish(
        records_case,
        _prepare(
            records_case,
            [fact] if owner == "business" else [],
            [obj] if owner == "object" else None,
        ),
    )
    after = _parts(records_case)
    assert "sources:" + extra["source_id"] not in after
    for source_id in fact["evidence_ids"]:
        assert after["sources:" + source_id] == before["sources:" + source_id]
    with storage.read_unit(company.company_id) as read:
        assert records.resolve_sources((extra["source_id"],), read) == (extra,)
        records.verify_snapshot(company_identity(company), [fact], [obj], read)


def test_source_metadata_change_republishes_one_identity_and_unused_source_has_no_part(
    records_case,
):
    storage, (company, _), records = records_case
    fact, obj = _first_facts(records_case)
    _publish(records_case, _prepare(records_case, [fact], [obj]))
    before = _parts(records_case)
    with storage.read_unit(company.company_id) as read:
        original = records.resolve_sources(fact["evidence_ids"], read)[0]
    unused = _source(records_case, "尚未采用的新原件。")
    changed = original | {"archive_month": "2026-02"}
    _save(records_case, sources=(changed, unused))
    prepared = _prepare(records_case, [], None)
    assert {loads_json(raw)["key"] for raw in prepared.snapshot.parts_json} == {
        "sources:" + original["source_id"]
    }
    _publish(records_case, prepared)
    after = _parts(records_case)
    assert "sources:" + unused["source_id"] not in after
    for key in before.keys() - {"sources:" + original["source_id"]}:
        assert after[key] == before[key]
    assert open_standard_bundle(company.company_root / "standard/current.json").sources == (
        changed,
    )


def test_last_reference_withdrawal_then_discard_keeps_one_precise_document_target(records_case):
    storage, (company, _), records = records_case
    fact, obj = _first_facts(records_case)
    extra = _source(records_case, "只支撑将被撤去的业务说明。")
    scope = "business:" + fact["business_id"]
    _save(
        records_case,
        sources=(extra,),
        notes=(
            {
                "scope": scope,
                "text": "确认的用途",
                "evidence_ids": [extra["source_id"]],
                "expected_seq": 0,
            },
        ),
    )
    _publish(records_case, _prepare(records_case, [fact], [obj]))
    with storage.write_unit(company.company_id) as unit:
        records.clear_notes((scope,), str(uuid4()), unit)
        before = records.pending_document_scopes(unit)
        discarded = records.discard_sources(
            (extra["source_id"],), unit, retained_source_ids=(), protected_paths=()
        )
        assert discarded.discarded_source_ids == (extra["source_id"],)
        assert records.pending_document_scopes(unit) == before
    _publish(records_case, _prepare(records_case, [], None))
    assert "sources:" + extra["source_id"] not in _parts(records_case)
    with storage.read_unit(company.company_id) as read:
        records.verify_snapshot(company_identity(company), [fact], [obj], read)


def test_unused_source_import_and_metadata_edit_do_not_republish_standard_documents(
    records_case,
):
    storage, (company, _), records = records_case
    fact, obj = _first_facts(records_case)
    _publish(records_case, _prepare(records_case, [fact], [obj]))
    path = company.company_root / "standard/current.json"
    before, stamp = path.read_bytes(), path.stat().st_mtime_ns
    with storage.read_unit(company.company_id) as read:
        targets = records.document_target_heads(read)
    source = _source(records_case, "未采用的资料只保存在收件区。")
    _save(records_case, sources=(source,))
    _save(records_case, sources=(source | {"archive_month": "2026-02"},))
    assert path.read_bytes() == before and path.stat().st_mtime_ns == stamp
    with storage.read_unit(company.company_id) as read:
        assert records.document_status(read)["state"] == "ready"
        assert records.document_target_heads(read) == targets
        assert (
            records.resolve_sources((source["source_id"],), read)[0]["archive_month"] == "2026-02"
        )
        records.verify_snapshot(company_identity(company), [fact], [obj], read)


def test_old_aggregate_input_is_readable_but_explicit_repair_has_only_one_writer(records_case):
    storage, (company, _), records = records_case
    fact, obj = _first_facts(records_case)
    note = {
        "scope": "company",
        "text": "已确认公司现实情况。",
        "evidence_ids": fact["evidence_ids"],
        "expected_seq": 0,
    }
    _save(records_case, notes=(note,))
    _publish(records_case, _prepare(records_case, [fact], [obj]))
    path = company.company_root / "standard/current.json"

    def aggregate(_, parts):
        for prefix, field in (("notes", "notes"), ("sources", "sources")):
            selected = [key for key in parts if key.startswith(prefix + ":")]
            values = [item for key in selected for item in parts.pop(key)[field]]
            parts[prefix] = {"format": "ai-accounting-standard/1", "key": prefix, field: values}

    rewrite(path, aggregate)
    old = open_standard_bundle(path)
    assert old.facts == (fact,) and old.notes[0]["text"] == note["text"]
    # Re-signing a manifest cannot turn a partial publication into format adoption.
    _save(records_case, sources=(_source(records_case, "尚未采用的另一份确认资料。"),))
    with pytest.raises(ContractError):
        _prepare(records_case, [], None)
    with storage.maintenance_unit(company.company_id) as unit:
        records.request_document_repair(unit)
    _publish(records_case, _prepare(records_case, [fact], [obj]))
    current = open_standard_bundle(path)
    assert (
        current.facts == old.facts and current.notes == old.notes and current.sources == old.sources
    )
    assert not {"notes", "sources"} & _parts(records_case).keys()
    with storage.read_unit(company.company_id) as read:
        records.verify_snapshot(company_identity(company), [fact], [obj], read)


@pytest.mark.parametrize("field,prefix", [("notes", "notes:"), ("sources", "sources:")])
def test_scoped_part_cannot_lie_about_its_contents_even_with_new_hash(records_case, field, prefix):
    from test_standard_bundle import publish

    path, _, _, _, _ = publish(records_case)

    def mismatched(_, parts):
        old_key = next(key for key in parts if key.startswith(prefix))
        key = "notes:period:2026-02" if field == "notes" else "sources:" + str(uuid4())
        part = parts.pop(old_key)
        part["key"] = key
        parts[key] = part

    rewrite(path, mismatched)
    with pytest.raises(ContractError) as caught:
        open_standard_bundle(path)
    assert caught.value.code == "documents_corrupt"
