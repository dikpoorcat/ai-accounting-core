"""The public snapshot constructor owns strict, immutable document bytes."""

import json
from dataclasses import FrozenInstanceError, fields, replace
from pathlib import Path

import pytest
from test_documents import _first_facts, _publish, company_identity

from ai_accounting_modular.contracts.public import ContractError, loads_json
from ai_accounting_modular.records.public import DocumentSnapshot, ShardTarget, open_standard_bundle


def _capture(records_case):
    storage, (company, _), records = records_case
    fact, obj = _first_facts(records_case)
    with storage.read_unit(company.company_id) as read:
        return records.capture_documents(company_identity(company), [fact], [obj], read)


def _arguments(snapshot):
    return {
        field.name: getattr(snapshot, field.name)
        for field in fields(DocumentSnapshot)
        if field.init
    }


def test_external_noncanonical_json_is_strictly_normalized_and_detached_without_io(
    records_case, monkeypatch
):
    _, (company, _), records = records_case
    snapshot = _capture(records_case)
    targets, scopes = list(snapshot.targets), list(snapshot.scopes)
    parts = [
        json.dumps(loads_json(raw), ensure_ascii=False, indent=2) for raw in snapshot.parts_json
    ]
    arguments = _arguments(snapshot) | {
        "targets": targets,
        "scopes": scopes,
        "parts_json": parts,
        "company_identity_json": json.dumps(loads_json(snapshot.company_identity_json), indent=2),
    }

    def no_filesystem(*args, **kwargs):
        pytest.fail("快照构造只能核验给定的值，不得读取磁盘")

    with monkeypatch.context() as patches:
        for method in ("open", "stat", "resolve"):
            patches.setattr(Path, method, no_filesystem)
        external = DocumentSnapshot(**arguments)
    targets.clear()
    scopes.clear()
    parts.clear()
    assert external == snapshot
    assert all(
        type(value) is tuple for value in (external.targets, external.scopes, external.parts_json)
    )
    assert not hasattr(external, "__dict__")
    assert not hasattr(external.targets[0], "__dict__")
    with pytest.raises(FrozenInstanceError):
        external.parts_json = ()
    with pytest.raises(FrozenInstanceError):
        external.targets[0].wanted_head = 99
    with pytest.raises(ContractError):
        ShardTarget("notes", 1, None, [], None)
    expected = records.prepare_documents(snapshot)
    actual = records.prepare_documents(external)
    assert actual.manifest == expected.manifest
    _publish(records_case, actual)
    bundle = open_standard_bundle(company.company_root / "standard/current.json")
    assert len(bundle.facts) == 1 and bundle.facts[0]["data"]["amount_fen"] == "10000"


@pytest.mark.parametrize("construct", ["direct", "replace"])
@pytest.mark.parametrize(
    "raw",
    [
        "{",
        '{"format":"ai-accounting-standard/1","key":"notes","notes":[],"notes":[]}',
        '{"format":"ai-accounting-standard/1","key":"notes","notes":NaN}',
        '{"format":"ai-accounting-standard/1","key":"notes",'
        '"notes":[{"scope":"company","text":"\\ud800","evidence_ids":[]}]}',
        '{"format":"wrong","key":"notes","notes":[]}',
        {"format": "ai-accounting-standard/1", "key": "notes", "notes": []},
    ],
)
def test_invalid_original_text_never_constructs_a_snapshot(records_case, construct, raw):
    snapshot = _capture(records_case)
    with pytest.raises(ContractError):
        if construct == "direct":
            DocumentSnapshot(**(_arguments(snapshot) | {"parts_json": [raw]}))
        else:
            replace(snapshot, parts_json=[raw])
    assert not (snapshot.company_root / "standard/current.json").exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"company_identity_json": "{}"},
        {"base_manifest_json": "[]"},
        {"parts_json": None},
        {"targets": ["not-a-target"]},
        {"scopes": [["notes"]]},
        {"head": True},
    ],
)
def test_snapshot_envelope_cannot_retain_invalid_or_mutable_fields(records_case, changes):
    snapshot = _capture(records_case)
    with pytest.raises(ContractError):
        replace(snapshot, **changes)


def test_subclass_cannot_bypass_construction_and_reach_document_writes(records_case):
    _, (company, _), records = records_case
    snapshot = _capture(records_case)

    class UncheckedSnapshot(DocumentSnapshot):
        def __post_init__(self):
            pass

    forged = UncheckedSnapshot(**(_arguments(snapshot) | {"parts_json": ("invalid",)}))
    with pytest.raises(ContractError) as error:
        records.prepare_documents(forged)
    assert error.value.code == "invalid_input"
    assert not (company.company_root / "standard/parts").exists()


def test_replacing_a_valid_snapshot_rechecks_then_preserves_exact_prepared_bytes(records_case):
    _, (company, _), records = records_case
    snapshot = _capture(records_case)
    copied = replace(snapshot, parts_json=tuple(reversed(snapshot.parts_json)))
    first, second = records.prepare_documents(snapshot), records.prepare_documents(copied)
    assert second.manifest == first.manifest
    for shard in first.manifest["shards"]:
        raw = (company.company_root / shard["path"]).read_bytes()
        assert raw == next(
            (part + "\n").encode("utf-8")
            for part in copied.parts_json
            if loads_json(part)["key"] == shard["key"]
        )
