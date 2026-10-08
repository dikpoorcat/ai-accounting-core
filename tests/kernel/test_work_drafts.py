"""Private working inputs retain omissions and never acquire business authority."""

import base64
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_accounting.kernel import work_drafts
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.permissions import (
    assert_private_directory,
    assert_private_file,
    create_private_file,
    ensure_private_directory,
)
from ai_accounting.kernel.runtime import private_file_lock
from ai_accounting.kernel.types import canonical
from ai_accounting.kernel.work_draft_contract import DRAFT_COMMANDS, WorkDraft
from ai_accounting.kernel.work_drafts import MAX_WORK_DRAFT_BYTES, WorkDraftStore


@pytest.fixture
def store(tmp_path):
    registry = SimpleNamespace(
        models={
            "loan": SimpleNamespace(registration_command=None),
            "asset_activation_batch": SimpleNamespace(
                registration_command="prepare_asset_activation_batch"
            ),
        }
    )
    return WorkDraftStore(
        tmp_path, "catalog", "company", "database", registry=registry, commands=DRAFT_COMMANDS
    )


def make_store(store, **identities):
    identity = store.identity | identities
    return WorkDraftStore(
        store.base.parent,
        identity["catalog_instance_id"],
        identity["company_id"],
        identity["database_id"],
        registry=store.registry,
        commands=store.commands,
        request_result=store.request_result,
    )


def candidate(**payload):
    return {
        "candidates": [
            {"id": "loan-1", "command": "save_fact", "payload": {"kind": "loan", **payload}}
        ]
    }


def saved_path(store, period="2026-10", area="financing"):
    return store.directory / f"{period}--{area}.json"


def save(store, draft, expected_revision=None, period="2026-10", area="financing"):
    return store.save(period, area, expected_revision, draft)


def assert_error(code, function, *args, **kwargs):
    with pytest.raises(KernelError) as caught:
        function(*args, **kwargs)
    assert caught.value.code == code
    return caught.value


def test_absent_reads_do_not_create_a_document(store):
    assert store.read("2026-10", "financing")["status"] == "absent"
    assert store.list()["items"] == []
    assert not store.base.exists()


def test_partial_working_input_answer_and_sources_roundtrip_after_restart(store):
    draft = candidate(data={"principal_fen": 9007199254740993}) | {
        "source_refs": [{"evidence_digest": "a" * 64, "location": "借款!B12"}],
        "questions": [
            {
                "id": "creditor",
                "question": "出借方性质？",
                "answer": "负责人本人",
                "source_refs": [{"source_id": "owner-answer", "location": "原回答"}],
            }
        ],
        "resume_note": "金额已经核对；业务月份未确认",
        "next_step": "核对出借方身份",
    }
    result = save(store, draft)
    assert "draft" not in result
    reopened = make_store(store).read("2026-10", "financing")
    assert reopened["draft"] == draft
    assert reopened["revision"] == result["revision"]
    assert reopened["warnings"] == []
    assert "period" not in reopened["draft"]["candidates"][0]["payload"]["data"]
    assert "pending_requests" not in reopened["draft"]
    assert_private_directory(store.base)
    assert_private_directory(store.directory)
    assert_private_file(saved_path(store))
    assert_private_file(saved_path(store).with_suffix(".lock"))


def test_model_input_does_not_fill_omitted_fields(store):
    save(store, WorkDraft.model_validate(candidate()))
    assert store.read("2026-10", "financing")["draft"] == candidate()


@pytest.mark.parametrize(
    "change",
    [{"company_id": "second"}, {"database_id": "second"}, {"catalog_instance_id": "second"}],
)
def test_identity_namespace_and_month_area_are_isolated(store, change):
    save(store, candidate())
    assert make_store(store, **change).read("2026-10", "financing")["status"] == "absent"
    assert make_store(store, **change).list()["items"] == []
    assert store.read("2026-09", "financing")["status"] == "absent"
    assert store.read("2026-10", "assets")["status"] == "absent"


def test_revision_conflicts_and_delete_recreate_prevent_old_version_reuse(store):
    first = save(store, candidate())
    error = assert_error("work_draft_revision_conflict", save, store, candidate())
    assert error.details["current_revision"] == first["revision"]
    second = save(store, {"resume_note": "second"}, first["revision"])
    assert second["revision"] != first["revision"]
    assert_error(
        "work_draft_revision_conflict", store.delete, "2026-10", "financing", first["revision"]
    )
    assert (
        store.delete("2026-10", "financing", second["revision"])["revision"] == second["revision"]
    )
    third = save(store, candidate())
    assert third["revision"] not in {first["revision"], second["revision"]}
    assert_error("work_draft_revision_conflict", save, store, candidate(), second["revision"])
    assert saved_path(store).with_suffix(".lock").exists()


def test_multiple_store_instances_serialize_competing_updates(store):
    first = save(store, candidate())

    def update(number):
        try:
            return save(make_store(store), {"resume_note": str(number)}, first["revision"])
        except KernelError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, (1, 2)))
    assert sum(isinstance(item, dict) for item in results) == 1
    assert "work_draft_revision_conflict" in results


def test_external_lock_is_busy_and_preserves_previous_draft(store):
    result = save(store, candidate())
    before = saved_path(store).read_bytes()
    with private_file_lock(saved_path(store).with_suffix(".lock")) as acquired:
        assert acquired
        assert_error("work_draft_busy", save, make_store(store), {}, result["revision"])
        assert_error("work_draft_busy", make_store(store).read, "2026-10", "financing")
    assert saved_path(store).read_bytes() == before


@pytest.mark.parametrize(
    "draft",
    [
        {"unknown": True},
        {"candidates": [{"id": "a", "command": "save_fact", "payload": []}]},
        candidate(kind=4),
        candidate(data={"amount_fen": float("nan")}),
        candidate(data={"amount_fen": float("inf")}),
        candidate(data={1: "bad"}),
        candidate(data={"bad": b"bytes"}),
        candidate(data={"bad": (1, 2)}),
        {"candidates": [{"id": "a", "command": "save_facts", "payload": {"facts": "bad"}}]},
        {"candidates": [{"id": "a", "command": "save_facts", "payload": {"facts": ["bad"]}}]},
        {"source_refs": [{"note": "no source"}]},
        {"result_refs": [{"note": "no result"}]},
    ],
)
def test_invalid_json_and_body_rejected_without_creating_draft(store, draft):
    assert_error("work_draft_invalid", save, store, draft)
    assert store.read("2026-10", "financing")["status"] == "absent"


@pytest.mark.parametrize(
    "key",
    [
        "approval_id",
        "approval_token",
        "password",
        "access_token",
        "content_base64",
        "parse_result",
        "registered_fact_snapshots",
        "session_token",
        "recovery_code",
        "recovery_codes",
        "api_key",
    ],
)
def test_credentials_tokens_original_bytes_and_snapshots_are_rejected(store, key):
    assert_error("work_draft_invalid", save, store, candidate(data={"nested": {key: "blocked"}}))


@pytest.mark.parametrize(
    "command", ["close", "create_company", "schema", "run_jobs", "arbitrary_entry"]
)
def test_only_business_working_targets_are_accepted(store, command):
    draft = {"candidates": [{"id": "a", "command": command, "payload": {}}]}
    assert_error("work_draft_target_invalid", save, store, draft)


def test_unknown_kind_generic_dedicated_route_and_cross_company_rejected(store):
    assert_error("work_draft_target_invalid", save, store, candidate(kind="unknown"))
    assert_error("work_draft_target_invalid", save, store, candidate(kind="asset_activation_batch"))
    assert_error("work_draft_scope_mismatch", save, store, candidate(company_id="second"))
    draft = {
        "candidates": [
            {
                "id": "assets",
                "command": "prepare_asset_activation_batch",
                "payload": {"kind": "asset_activation_batch"},
            }
        ]
    }
    save(store, draft)
    assert store.read("2026-10", "financing")["draft"] == draft


@pytest.mark.parametrize("change", ["kind", "command", "route"])
def test_changed_build_keeps_readable_candidates_with_warnings(store, change):
    result = save(store, candidate())
    if change == "kind":
        store.registry.models.pop("loan")
    elif change == "command":
        store.commands = frozenset()
    else:
        store.registry.models["loan"].registration_command = "prepare_asset_activation_batch"
    reopened = store.read("2026-10", "financing")
    assert reopened["revision"] == result["revision"]
    assert reopened["draft"] == candidate()
    assert reopened["warnings"]
    assert_error("work_draft_target_invalid", save, store, candidate(), result["revision"])
    # Explicit deletion remains possible; a changed business target is not corruption.
    store.delete("2026-10", "financing", result["revision"])


@pytest.mark.parametrize(
    "damage,code",
    [
        ("json", "work_draft_corrupt"),
        ("duplicate", "work_draft_corrupt"),
        ("digest", "work_draft_corrupt"),
        ("nan", "work_draft_corrupt"),
        ("scope", "work_draft_corrupt"),
        ("revision", "work_draft_corrupt"),
        ("format", "work_draft_format_unsupported"),
        ("identity", "work_draft_identity_mismatch"),
    ],
)
def test_corrupt_or_unrecognized_files_are_never_rebuilt_overwritten_or_deleted(
    store, damage, code
):
    result = save(store, candidate())
    path = saved_path(store)
    envelope = json.loads(path.read_bytes())
    if damage == "json":
        raw = b"{"
    elif damage == "duplicate":
        raw = b'{"format":"duplicate",' + path.read_bytes()[1:]
    elif damage == "nan":
        raw = path.read_bytes().replace(b'"payload":{', b'"payload":{"bad":NaN,')
    else:
        if damage == "digest":
            envelope["draft_digest"] = "0" * 64
        elif damage == "scope":
            envelope["period"] = "2026-09"
        elif damage == "revision":
            envelope["revision"] = "not-a-revision"
        elif damage == "format":
            envelope["format"] = "ai-accounting-work-draft/999"
        else:
            envelope["identity"]["company_id"] = "other"
        raw = canonical(envelope).encode()
    path.write_bytes(raw)
    for method, args in (
        (store.read, ("2026-10", "financing")),
        (save, (store, {}, result["revision"])),
        (store.delete, ("2026-10", "financing", result["revision"])),
    ):
        assert_error(code, method, *args)
        assert path.read_bytes() == raw
    assert store.list()["items"] == [{"period": "2026-10", "work_area": "financing"}]


def test_listing_reads_filenames_only_and_cursors_bind_scope_names(store, monkeypatch):
    save(store, {}, period="2026-09")
    save(store, {}, area="assets")
    save(store, {})
    unrelated = create_private_file(store.directory / "unrelated.json")
    unrelated.write_bytes(b"not json")
    monkeypatch.setattr(Path, "read_bytes", lambda self: pytest.fail("list read draft body"))
    first = store.list(limit=1)
    assert first["items"] == [{"period": "2026-09", "work_area": "financing"}]
    second = store.list(limit=1, cursor=first["next_cursor"])
    assert second["items"] == [{"period": "2026-10", "work_area": "assets"}]
    assert store.list(period="2026-10", work_area="financing")["items"] == [
        {"period": "2026-10", "work_area": "financing"}
    ]
    assert_error(
        "work_draft_cursor_stale", store.list, period="2026-10", cursor=first["next_cursor"]
    )
    path = create_private_file(store.directory / "2026-11--bank.json")
    path.write_bytes(b"preserved corrupt body")
    assert_error("work_draft_cursor_stale", store.list, cursor=first["next_cursor"])


@pytest.mark.parametrize(
    "cursor",
    [
        "garbage",
        "@",
        base64.b64encode(b"[]").decode(),
        base64.b64encode(b'{"scope":"x","offset":true}').decode(),
    ],
)
def test_invalid_cursor_is_structured(store, cursor):
    assert_error("work_draft_cursor_invalid", store.list, cursor=cursor)


def test_complete_utf8_file_8_mib_boundary_preserves_previous_on_overflow(store):
    assert MAX_WORK_DRAFT_BYTES == 8 * 1024 * 1024
    first = save(store, {"resume_note": ""})
    overhead = saved_path(store).stat().st_size
    available = MAX_WORK_DRAFT_BYTES - overhead
    note = "中" * (available // 3) + "a" * (available % 3)
    second = save(store, {"resume_note": note}, first["revision"])
    assert saved_path(store).stat().st_size == MAX_WORK_DRAFT_BYTES
    before = saved_path(store).read_bytes()
    assert_error(
        "work_draft_too_large", save, store, {"resume_note": note + "a"}, second["revision"]
    )
    assert saved_path(store).read_bytes() == before


def test_failed_replace_keeps_previous_and_removes_only_own_temp(store, monkeypatch):
    first = save(store, candidate())
    before = saved_path(store).read_bytes()
    unrelated = create_private_file(store.directory / "unrelated.tmp")
    monkeypatch.setattr(
        work_drafts.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("disk failure"))
    )
    assert_error("work_draft_save_failed", save, store, {"resume_note": "new"}, first["revision"])
    assert saved_path(store).read_bytes() == before
    assert not list(store.directory.glob(".write-*.tmp"))
    assert unrelated.exists()


@pytest.mark.parametrize(
    "failed_call,expected", [(1, "work_draft_save_failed"), (2, "work_draft_result_unconfirmed")]
)
def test_fsync_failure_distinguishes_before_and_after_replace(
    store, monkeypatch, failed_call, expected
):
    first = save(store, candidate())
    before = saved_path(store).read_bytes()
    original = work_drafts.os.fsync
    calls = []

    def failing(descriptor):
        calls.append(descriptor)
        if len(calls) == failed_call:
            raise OSError("sync failure")
        return original(descriptor)

    monkeypatch.setattr(work_drafts.os, "fsync", failing)
    assert_error(expected, save, store, {"resume_note": "new"}, first["revision"])
    after = saved_path(store).read_bytes()
    assert (after == before) == (failed_call == 1)
    if failed_call == 2:
        assert store.read("2026-10", "financing")["revision"] != first["revision"]


def test_delete_sync_failure_is_unconfirmed_and_re_read_absent(store, monkeypatch):
    first = save(store, candidate())
    monkeypatch.setattr(store, "_sync_directory", lambda: (_ for _ in ()).throw(OSError("failure")))
    assert_error(
        "work_draft_result_unconfirmed", store.delete, "2026-10", "financing", first["revision"]
    )
    assert store.read("2026-10", "financing")["status"] == "absent"


def test_private_file_failure_preserves_bytes(store, monkeypatch):
    first = save(store, candidate())
    before = saved_path(store).read_bytes()
    original = work_drafts.assert_private_file

    def rejected(path):
        if Path(path) == saved_path(store):
            raise PermissionError("private path unavailable")
        return original(path)

    monkeypatch.setattr(work_drafts, "assert_private_file", rejected)
    assert_error("work_draft_unavailable", store.read, "2026-10", "financing")
    assert_error("work_draft_save_failed", save, store, {}, first["revision"])
    assert_error(
        "work_draft_delete_failed", store.delete, "2026-10", "financing", first["revision"]
    )
    assert saved_path(store).read_bytes() == before


def test_oversized_private_file_is_corrupt_not_a_miss(store):
    ensure_private_directory(store.base)
    ensure_private_directory(store.directory)
    path = create_private_file(saved_path(store))
    with path.open("wb") as handle:
        handle.truncate(MAX_WORK_DRAFT_BYTES + 1)
    assert_error("work_draft_corrupt", store.read, "2026-10", "financing")
    assert path.stat().st_size == MAX_WORK_DRAFT_BYTES + 1


@pytest.mark.parametrize("scope", ["namespace", "document"])
def test_dangling_links_are_never_treated_as_absent_or_replaced(store, scope):
    # A synthetic dangling symlink exercises exists()==False on both POSIX and
    # Windows when Developer Mode or link privileges are enabled.
    ensure_private_directory(store.base)
    if scope == "document":
        ensure_private_directory(store.directory)
        linked = saved_path(store)
    else:
        linked = store.directory
    try:
        linked.symlink_to(
            store.base.parent / "not-present", target_is_directory=scope == "namespace"
        )
    except OSError:
        pytest.skip("this Windows account cannot create a synthetic symbolic link")
    assert linked.is_symlink() and not linked.exists()
    assert_error("work_draft_unavailable", store.read, "2026-10", "financing")
    assert_error("work_draft_save_failed", save, store, candidate())
    assert_error("work_draft_delete_failed", store.delete, "2026-10", "financing", "old")
    if scope == "namespace":
        assert_error("work_draft_unavailable", store.list)
    assert linked.is_symlink()


def test_unresolved_original_request_survives_candidate_edits_and_keeps_its_payload(store):
    pending = {
        "command": "save_fact",
        "request_id": "original-request",
        "payload": {
            "company_id": "company",
            "request_id": "original-request",
            "kind": "loan",
            "data": {"principal_fen": 7},
        },
    }
    first = save(store, candidate() | {"pending_requests": [pending]})
    assert_error(
        "work_draft_pending_request_required",
        save,
        store,
        {"resume_note": "edited"},
        first["revision"],
    )
    changed = pending | {"payload": pending["payload"] | {"data": {"principal_fen": 8}}}
    assert_error(
        "work_draft_request_conflict",
        save,
        store,
        {"pending_requests": [changed]},
        first["revision"],
    )
    second = save(
        store, {"pending_requests": [pending], "resume_note": "edited"}, first["revision"]
    )
    resolved = {"result_refs": [{"request_id": "original-request", "fact_id": "registered"}]}
    assert_error("work_draft_pending_request_required", save, store, resolved, second["revision"])
    store.request_result = lambda ident: {
        "status": "unknown",
        "company_id": "company",
        "database_id": "database",
        "submitted_request_id": ident,
    }
    assert_error("work_draft_pending_request_required", save, store, resolved, second["revision"])
    store.request_result = lambda ident: {
        "status": "committed",
        "company_id": "company",
        "database_id": "database",
        "submitted_request_id": ident,
    }
    save(
        store,
        resolved,
        second["revision"],
    )
    assert store.read("2026-10", "financing")["draft"] == {
        "result_refs": [{"request_id": "original-request", "fact_id": "registered"}]
    }


def test_pending_request_requires_original_key_and_company_in_complete_payload(store):
    pending = {
        "command": "save_fact",
        "request_id": "original-request",
        "payload": {"kind": "loan"},
    }
    assert_error("work_draft_request_invalid", save, store, {"pending_requests": [pending]})


@pytest.mark.parametrize("changed_value", [True, 1.0])
def test_pending_original_json_number_type_cannot_be_rewritten(store, changed_value):
    pending = {
        "command": "save_fact",
        "request_id": "original-request",
        "payload": {
            "company_id": "company",
            "request_id": "original-request",
            "kind": "loan",
            "data": {"principal_fen": 1},
        },
    }
    first = save(store, {"pending_requests": [pending]})
    before = saved_path(store).read_bytes()
    changed = pending | {
        "payload": pending["payload"] | {"data": {"principal_fen": changed_value}}
    }
    assert_error(
        "work_draft_request_conflict",
        save,
        store,
        {"pending_requests": [changed]},
        first["revision"],
    )
    assert saved_path(store).read_bytes() == before


def test_readonly_preparation_cannot_become_pending_formal_request(store):
    draft = {
        "pending_requests": [
            {
                "command": "prepare_payroll",
                "request_id": "prepare",
                "payload": {"company_id": "company", "request_id": "prepare"},
            }
        ]
    }
    assert_error("work_draft_request_invalid", save, store, draft)


def test_lone_surrogate_is_rejected_before_replacing_old_draft(store):
    first = save(store, candidate())
    before = saved_path(store).read_bytes()
    assert_error("work_draft_invalid", save, store, {"resume_note": "\ud800"}, first["revision"])
    assert saved_path(store).read_bytes() == before


def test_parse_cache_activity_does_not_touch_drafts(store):
    from ai_accounting.kernel.inspection_cache import InspectionCache

    first = save(store, candidate())
    path = saved_path(store)
    before = path.read_bytes()
    cache = InspectionCache(store.base.parent, build_id="updated", max_bytes=0)
    cache.inspect(
        company_id="company",
        database_id="database",
        evidence_digest=__import__("hashlib").sha256(b"a").hexdigest(),
        specification={"format": "text"},
        raw=b"a",
        parse=lambda: {"items": [], "issues": [], "coverage": [], "control_totals": []},
    )
    assert path.read_bytes() == before
    assert make_store(store).read("2026-10", "financing")["revision"] == first["revision"]
