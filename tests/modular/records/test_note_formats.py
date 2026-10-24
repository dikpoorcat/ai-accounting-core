"""One canonical note body survives retries, publication and snapshot verification."""

from uuid import uuid4

import pytest
from test_documents import _prepare, _publish, company_identity

from ai_accounting_modular.contracts.public import ContractError, content_digest, loads_json
from ai_accounting_modular.records._changes import note_current
from ai_accounting_modular.records._repository import RecordsRepository
from ai_accounting_modular.records.public import RecordsChange, RecordsContext

TEXT = "# 公司业务\n\n保留 *、_、1. 和 <公司>。\n第二行。"


def save(records_case, note):
    storage, (company, _), records = records_case
    with storage.write_unit(company.company_id) as unit:
        plan = records.plan_change(RecordsChange(notes=(note,)), RecordsContext(unit, str(uuid4())))
        records.apply_current(plan, unit)
        return plan


def current(records_case):
    storage, (company, _), records = records_case
    with storage.read_unit(company.company_id) as read:
        row = read.repository("records").notes(("company",))["company"]
        return records.current_notes(("company",), read)[0], row, read.head


@pytest.mark.parametrize("explicit_first", [False, True])
def test_plain_text_default_has_one_digest_and_lost_reply_retry(records_case, explicit_first):
    note = {"scope": "company", "text": TEXT, "expected_seq": 0, "evidence_ids": []}
    first = note | ({"text_format": "plain_text"} if explicit_first else {})
    assert save(records_case, first).changed_scopes == ("notes",)
    saved, row, head = current(records_case)
    assert saved == {
        "scope": "company",
        "seq": 1,
        "state": "current",
        "text": TEXT,
        "basis_head": None,
        "evidence_ids": [],
    }
    assert row["note_format"] == "plain_text"
    assert row["content_digest"] == content_digest(
        {
            "scope": "company",
            "state": "current",
            "text": TEXT,
            "basis_head": None,
            "evidence_ids": [],
        }
    )
    retry = note | ({} if explicit_first else {"text_format": "plain_text"})
    assert save(records_case, retry).changed_scopes == ()
    assert current(records_case) == (saved, row, head)
    assert save(records_case, retry | {"expected_seq": 1}).changed_scopes == ()
    assert current(records_case) == (saved, row, head)


def test_only_format_change_advances_note_and_return_to_plain_restores_original_digest(
    records_case,
):
    note = {"scope": "company", "text": TEXT, "expected_seq": 0, "evidence_ids": []}
    save(records_case, note)
    _, original, _ = current(records_case)
    markdown = note | {"expected_seq": 1, "text_format": "markdown"}
    assert save(records_case, markdown).changed_scopes == ("notes",)
    saved, row, _ = current(records_case)
    assert saved["seq"] == 2 and saved["text"] == TEXT
    assert saved["text_format"] == row["note_format"] == "markdown"
    assert row["content_digest"] != original["content_digest"]
    assert save(records_case, markdown).changed_scopes == ()
    save(records_case, note | {"expected_seq": 2, "text_format": "plain_text"})
    saved, row, _ = current(records_case)
    assert saved["seq"] == 3 and "text_format" not in saved
    assert row["content_digest"] == original["content_digest"]
    with pytest.raises(ContractError) as error:
        save(records_case, note | {"expected_seq": 1, "text": "过时正文"})
    assert error.value.code == "conflict"
    assert current(records_case)[0] == saved


@pytest.mark.parametrize(
    "scope,text_format",
    [
        ("period:2026-01", "markdown"),
        ("company", "html"),
        ("company", "unknown"),
    ],
)
def test_unsupported_format_is_input_error_without_any_saved_note(records_case, scope, text_format):
    note = {
        "scope": scope,
        "text": TEXT,
        "expected_seq": 0,
        "evidence_ids": [],
        "text_format": text_format,
    }
    if scope.startswith("period:"):
        note["expected_basis_head"] = "basis-1"
    with pytest.raises(ContractError) as error:
        save(records_case, note)
    assert error.value.code == "invalid_input"
    storage, (company, _), records = records_case
    with storage.read_unit(company.company_id) as read:
        assert read.head == 0 and records.current_notes(None, read) == ()


def test_month_explicit_plain_text_keeps_existing_retry_and_invalidation(records_case):
    storage, (company, _), records = records_case
    note = {
        "scope": "period:2026-01",
        "text": TEXT,
        "expected_seq": 0,
        "expected_basis_head": "basis-1",
        "evidence_ids": [],
    }
    for supplied in (note | {"text_format": "plain_text"}, note):
        with storage.write_unit(company.company_id) as unit:
            records.apply_current(
                records.plan_change(
                    RecordsChange(notes=(supplied,)),
                    RecordsContext(unit, str(uuid4()), {note["scope"]: "basis-1"}),
                ),
                unit,
            )
    with storage.write_unit(company.company_id) as unit:
        assert unit.head == 1
        assert records.invalidate_notes({note["scope"]: "basis-2"}, str(uuid4()), unit) == (
            note["scope"],
        )
    with storage.read_unit(company.company_id) as read:
        assert records.current_notes((note["scope"],), read) == (
            {
                "scope": note["scope"],
                "text": None,
                "seq": 2,
                "state": "needs_refresh",
                "basis_head": "basis-2",
                "evidence_ids": [],
            },
        )
        assert (
            read.repository("records").notes((note["scope"],))[note["scope"]]["note_format"]
            == "plain_text"
        )


@pytest.mark.parametrize("text_format", ["plain_text", "markdown"])
def test_standard_note_and_snapshot_use_the_same_canonical_format(records_case, text_format):
    save(
        records_case,
        {
            "scope": "company",
            "text": TEXT,
            "expected_seq": 0,
            "evidence_ids": [],
            "text_format": text_format,
        },
    )
    _publish(records_case, _prepare(records_case, [], []))
    storage, (company, _), records = records_case
    with storage.read_unit(company.company_id) as read:
        snapshot = records.verify_snapshot(company_identity(company), [], [], read)
        assert snapshot.document_set_id == records.document_status(read)["set_id"]
        with records.lease_document_set(read) as manifest:
            shard = next(shard for shard in manifest["shards"] if shard["key"] == "notes")
            raw = (company.company_root / shard["path"]).read_bytes()
        notes = loads_json(raw)["notes"]
    assert notes == [
        {
            "scope": "company",
            "text": TEXT,
            "evidence_ids": [],
            **({"text_format": "markdown"} if text_format == "markdown" else {}),
        }
    ]
    before = (company.company_root / "standard/current.json").read_bytes()
    assert (
        save(
            records_case,
            {
                "scope": "company",
                "text": TEXT,
                "expected_seq": 1,
                "evidence_ids": [],
                **({"text_format": "markdown"} if text_format == "markdown" else {}),
            },
        ).changed_scopes
        == ()
    )
    assert (company.company_root / "standard/current.json").read_bytes() == before


def test_format_change_reuses_source_links_and_source_target_but_real_source_change_updates_them(
    records_case, monkeypatch
):
    storage, (company, _), records = records_case
    with storage.read_unit(company.company_id) as read:
        sources = tuple(
            records.prepare_source({"kind": "owner_statement", "text": text}, read)
            for text in ("公司原安排。", "公司当前已确认的新安排。")
        )
    with storage.write_unit(company.company_id) as unit:
        records.apply_current(
            records.plan_change(RecordsChange(sources=sources), RecordsContext(unit, str(uuid4()))),
            unit,
        )
    note = {
        "scope": "company",
        "text": TEXT,
        "expected_seq": 0,
        "evidence_ids": [sources[0]["source_id"]],
    }
    assert save(records_case, note).changed_scopes == ("notes", "sources")
    _publish(records_case, _prepare(records_case, [], []))
    with storage.read_unit(company.company_id) as read:
        targets_before = {
            target["shard_key"]: target for target in read.repository("records").targets()
        }

    def forbid_rewriting_uses(*args, **kwargs):
        raise AssertionError("只改说明格式不应删写任何来源关系。")

    with monkeypatch.context() as patch:
        patch.setattr(RecordsRepository, "replace_uses", forbid_rewriting_uses)
        assert save(
            records_case,
            note
            | {
                "expected_seq": 1,
                "text_format": "markdown",
            },
        ).changed_scopes == ("notes",)
    with storage.read_unit(company.company_id) as read:
        targets_after = {
            target["shard_key"]: target for target in read.repository("records").targets()
        }
        assert records.document_status(read)["pending_scopes"] == ["notes"]
        assert targets_after["sources"] == targets_before["sources"]
    _publish(records_case, _prepare(records_case, [], None))
    changed = note | {"expected_seq": 2, "evidence_ids": [sources[1]["source_id"]]}
    assert save(records_case, changed).changed_scopes == ("notes", "sources")
    with storage.read_unit(company.company_id) as read:
        assert (
            records.current_notes(("company",), read)[0]["evidence_ids"] == changed["evidence_ids"]
        )
    _publish(records_case, _prepare(records_case, [], None))
    with storage.read_unit(company.company_id) as read:
        records.verify_snapshot(company_identity(company), [], [], read)


def test_stored_unknown_format_is_corruption_even_when_text_is_valid():
    row = {
        "scope_key": "company",
        "seq": 1,
        "state": "current",
        "note_text": TEXT,
        "basis_head": None,
        "note_format": "html",
        "content_digest": "irrelevant",
    }
    with pytest.raises(ContractError) as error:
        note_current(row, None, [])
    assert error.value.code == "storage_corrupt"
