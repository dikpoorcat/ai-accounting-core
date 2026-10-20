"""Offline final archives are real files and never rely on a live source database."""

import hashlib
import os
import subprocess
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import (
    ContractError,
    canonical_json,
    content_digest,
    loads_json,
)
from ai_accounting_modular.records.public import (
    RecordsChange,
    RecordsContext,
    copy_standard_bundle,
    open_standard_bundle,
)


def company_identity(company):
    return {
        "credit_code": company.credit_code,
        "legal_name": company.legal_name,
        "accounting_standard": "small_enterprise",
        "currency": "CNY",
        "start_period": company.start_period,
    }


def link_directory(link, target):
    if os.name == "nt":
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True
        )
        assert completed.returncode == 0, completed.stderr
    else:
        link.symlink_to(target, target_is_directory=True)


def unlink_directory(link):
    if os.name == "nt":
        link.rmdir()
    else:
        link.unlink()


def publish(case, *, count=1, working="2026-03", file_size=16):
    storage, (company, _), records = case
    original = company.company_root / "originals/upload.bin"
    original.parent.mkdir(exist_ok=True)
    with original.open("wb") as stream:
        for _ in range(file_size // 16):
            stream.write(b"synthetic bytes!")
    with storage.read_unit(company.company_id) as read:
        source = records.prepare_source(
            {"kind": "file", "relative_path": "originals/upload.bin", "archive_month": "2026-01"},
            read,
        )
    party = str(uuid4())
    obj = {
        "object_id": party,
        "kind": "organization",
        "name": "合成交易方",
        "data": {},
        "evidence_ids": [source["source_id"]],
    }
    facts = [
        {
            "business_id": str(uuid4()),
            "kind": "operations.expense",
            "period": "2026-01",
            "evidence_ids": [source["source_id"]],
            "data": {
                "creditor_id": party,
                "creditor_kind": "supplier",
                "expense_class": "administration",
                "amount_fen": "0",
            },
        }
        for _ in range(count)
    ]
    issue = {
        "issue_id": str(uuid4()),
        "scope": "materials",
        "evidence_ids": [source["source_id"]],
        "missing_fields": ["/data/amount_fen"],
        "question": "补充另一项真实金额",
        "assignee": "ai",
        "action": "provide_material",
    }
    note = {
        "scope": "company",
        "expected_seq": 0,
        "text": "合成公司的当前说明",
        "evidence_ids": [source["source_id"]],
    }
    with storage.write_unit(company.company_id) as unit:
        records.apply_current(
            records.plan_change(
                RecordsChange(
                    sources=(source,),
                    notes=(note,),
                    unresolved=(issue,),
                    unresolved_scope="materials",
                    expected_scope_head=0,
                ),
                RecordsContext(unit, str(uuid4())),
            ),
            unit,
        )
        for fact in facts:
            records.replace_source_uses("business", fact["business_id"], fact["evidence_ids"], unit)
        records.mark_document_targets(("objects", "facts:operations:2026-01"), unit)
    identity = company_identity(company)
    if working is not None:
        identity["working_period_to"] = working
    with storage.read_unit(company.company_id) as read:
        snapshot = records.capture_documents(identity, facts, [obj], read)
    prepared = records.prepare_documents(snapshot)
    with storage.maintenance_unit(company.company_id) as unit:
        status = records.publish_documents(prepared, unit)
    return Path(status["manifest_path"]), source, facts, obj, issue


def rewrite(path, edit):
    """Re-sign deliberately false content, so tests exercise meaning beyond byte hashes."""
    root = path.parent.parent
    manifest = loads_json(path.read_bytes())
    parts = {
        item["key"]: loads_json((root / item["path"]).read_bytes()) for item in manifest["shards"]
    }
    edit(manifest, parts)
    shards = []
    for key, part in sorted(parts.items()):
        raw = (canonical_json(part) + "\n").encode()
        digest = hashlib.sha256(raw).hexdigest()
        relative = (
            "standard/parts/"
            + key.removeprefix("facts:").replace(":", "--")
            + "--"
            + digest
            + ".json"
        )
        (root / relative).write_bytes(raw)
        shards.append({"key": key, "path": relative, "sha256": digest, "byte_size": len(raw)})
    manifest["shards"] = shards
    manifest["set_id"] = content_digest(
        {key: value for key, value in manifest.items() if key != "set_id"}
    )
    path.write_bytes((canonical_json(manifest) + "\n").encode())
    # The optional human index is outside the machine manifest; omit it in handmade cases.
    (path.parent / "README.md").unlink(missing_ok=True)


@pytest.mark.parametrize("count", [0, 1, 501])
def test_real_generated_archive_is_offline_complete_and_copy_accepts_new_runtime(
    records_case, tmp_path, count
):
    path, source, facts, obj, issue = publish(records_case, count=count)
    storage, (company, _), _ = records_case
    manifest_raw = path.read_bytes()
    # No source catalog or source database is available to the offline implementation.
    storage.catalog_path.unlink()
    company.database_path.unlink()
    bundle = open_standard_bundle(path)
    assert bundle.manifest_digest == hashlib.sha256(manifest_raw).hexdigest()
    assert bundle.set_id == loads_json(manifest_raw)["set_id"]
    assert bundle.working_period_to == "2026-03"
    assert bundle.facts == tuple(sorted(facts, key=lambda value: value["business_id"]))
    assert bundle.objects == (obj,) and bundle.sources == (source,) and bundle.issues == (issue,)
    assert bundle.notes[0]["text"] == "合成公司的当前说明"
    altered = bundle.objects[0]
    altered["name"] = "不得改动内部捕获"
    assert bundle.objects[0]["name"] == obj["name"]
    target = tmp_path / "independent-target"
    (target / "runtime").mkdir(parents=True)
    (target / "runtime/company.sqlite3").write_bytes(b"owned by storage")
    (target / "standard").mkdir()
    (target / "originals").mkdir()
    copy_standard_bundle(bundle, target)
    restored = open_standard_bundle(target / "standard/current.json")
    assert restored.facts == bundle.facts and restored.manifest_digest == bundle.manifest_digest
    assert (target / "runtime/company.sqlite3").read_bytes() == b"owned by storage"
    assert path.read_bytes() == manifest_raw


def test_missing_working_month_defaults_to_start_and_final_facts_not_today(records_case):
    path, _, _, _, _ = publish(records_case, count=0, working=None)
    assert open_standard_bundle(path).working_period_to == "2026-01"


def test_current_month_move_contains_only_final_fact_in_its_new_shard(records_case):
    path, _, facts, _, _ = publish(records_case)
    storage, (company, _), records = records_case
    moved = deepcopy(facts[0])
    moved["period"] = "2026-02"
    with storage.write_unit(company.company_id) as unit:
        records.mark_document_targets(
            ("facts:operations:2026-01", "facts:operations:2026-02"), unit
        )
    with storage.read_unit(company.company_id) as read:
        snapshot = records.capture_documents(company_identity(company), [moved], None, read)
    prepared = records.prepare_documents(snapshot)
    with storage.maintenance_unit(company.company_id) as unit:
        records.publish_documents(prepared, unit)
    bundle = open_standard_bundle(path)
    assert bundle.facts == (moved,)
    assert bundle.working_period_to == "2026-02"


@pytest.mark.parametrize(
    "damage",
    [
        "digest",
        "size",
        "set_id",
        "missing_part",
        "missing_fixed",
        "wrong_fixed_type",
        "duplicate_business",
        "duplicate_across_months",
        "wrong_month",
        "duplicate_object",
        "duplicate_source",
        "duplicate_issue",
        "duplicate_note",
        "missing_source",
        "bad_statement",
        "working_month",
    ],
)
def test_handmade_corrupt_or_incomplete_archive_is_rejected(records_case, damage):
    path, _, _, _, _ = publish(records_case)
    if damage in {"digest", "size", "set_id", "missing_part"}:
        manifest = loads_json(path.read_bytes())
        shard = manifest["shards"][0]
        if damage == "missing_part":
            (path.parent.parent / shard["path"]).unlink()
        elif damage == "digest":
            raw = (path.parent.parent / shard["path"]).read_bytes()
            (path.parent.parent / shard["path"]).write_bytes(raw.replace(b"0", b"1", 1))
        elif damage == "size":
            shard["byte_size"] += 1
            manifest["set_id"] = content_digest(
                {k: v for k, v in manifest.items() if k != "set_id"}
            )
            path.write_bytes(canonical_json(manifest).encode())
        else:
            manifest["set_id"] = "0" * 64
            path.write_bytes(canonical_json(manifest).encode())
    else:

        def edit(manifest, parts):
            if damage == "missing_fixed":
                del parts["notes"]
            elif damage == "wrong_fixed_type":
                parts["notes"] = {"format": "ai-accounting-standard/1", "key": "notes", "facts": []}
            elif damage == "wrong_month":
                parts["facts:operations:2026-01"]["facts"][0]["period"] = "2026-02"
            elif damage == "working_month":
                manifest["company_identity"]["working_period_to"] = "2025-12"
            elif damage == "missing_source":
                parts["sources"]["sources"] = []
            elif damage == "bad_statement":
                source_id = parts["sources"]["sources"][0]["source_id"]
                parts["sources"]["sources"] = [
                    {
                        "source_id": source_id,
                        "kind": "owner_statement",
                        "text": "合成当前说明",
                        "sha256": "0" * 64,
                    }
                ]
            elif damage == "duplicate_across_months":
                fact = deepcopy(parts["facts:operations:2026-01"]["facts"][0])
                fact["period"] = "2026-02"
                parts["facts:operations:2026-02"] = {
                    "format": "ai-accounting-standard/1",
                    "key": "facts:operations:2026-02",
                    "facts": [fact],
                }
            else:
                key, field = {
                    "duplicate_business": ("facts:operations:2026-01", "facts"),
                    "duplicate_object": ("objects", "objects"),
                    "duplicate_source": ("sources", "sources"),
                    "duplicate_issue": ("work", "issues"),
                    "duplicate_note": ("notes", "notes"),
                }[damage]
                parts[key][field].append(deepcopy(parts[key][field][0]))

        rewrite(path, edit)
    with pytest.raises(ContractError):
        open_standard_bundle(path)


def test_nested_fact_source_locator_is_not_lost_when_not_repeated_as_evidence(records_case):
    path, _, _, _, _ = publish(records_case)
    absent = str(uuid4())

    def edit(_, parts):
        original = parts.pop("facts:operations:2026-01")["facts"][0]
        original.update(kind="investment.cooperation_contract")
        original["data"] = {
            "profile": "direct_joint_operation_cash_labor_v1",
            "project_id": str(uuid4()),
            "operator_id": str(uuid4()),
            "company_initial_commitment_fen": "100",
            "company_revenue_expense_share": "0.5",
            "contract_source": {"source_id": absent, "location": "合成合同定位"},
        }
        parts["facts:investment:2026-01"] = {
            "format": "ai-accounting-standard/1",
            "key": "facts:investment:2026-01",
            "facts": [original],
        }

    rewrite(path, edit)
    with pytest.raises(ContractError, match=absent):
        open_standard_bundle(path)


def test_original_streams_are_bounded_and_opened_one_at_a_time(records_case, tmp_path, monkeypatch):
    path, source, _, _, _ = publish(records_case, file_size=9 * 1024 * 1024)
    second = deepcopy(source)
    second["source_id"] = str(uuid4())
    second["relative_path"] = source["relative_path"].replace(
        source["source_id"], second["source_id"]
    )
    second_raw = b"second original"
    second.update(sha256=hashlib.sha256(second_raw).hexdigest(), byte_size=len(second_raw))
    (path.parent.parent / second["relative_path"]).write_bytes(second_raw)
    rewrite(path, lambda _, parts: parts["sources"]["sources"].append(second))
    original_open, active, maximum = Path.open, 0, 0

    class Bounded:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            return self

        def __exit__(self, *args):
            nonlocal active
            active -= 1
            self.stream.close()

        def read(self, size=-1):
            assert 0 < size <= 1024 * 1024
            return self.stream.read(size)

        def fileno(self):
            return self.stream.fileno()

    def guarded(file, mode="r", *args, **kwargs):
        stream = original_open(file, mode, *args, **kwargs)
        if mode == "rb" and file.name.endswith("--" + source["original_name"]):
            return Bounded(stream)
        return stream

    monkeypatch.setattr(Path, "open", guarded)
    bundle = open_standard_bundle(path)
    copy_standard_bundle(bundle, tmp_path / "streamed")
    assert maximum == 1 and active == 0


def test_copy_rechecks_changed_bytes_and_never_overwrites_existing_target(records_case, tmp_path):
    path, source, _, _, _ = publish(records_case)
    bundle = open_standard_bundle(path)
    existing = tmp_path / "existing"
    (existing / "standard").mkdir(parents=True)
    protected = existing / "standard/keep.txt"
    protected.write_bytes(b"existing user data")
    with pytest.raises(ContractError) as error:
        copy_standard_bundle(bundle, existing)
    assert error.value.code == "target_exists" and protected.read_bytes() == b"existing user data"
    actual = bundle.company_root / source["relative_path"]
    actual.write_bytes(b"changed content!")
    target = tmp_path / "changed"
    with pytest.raises(ContractError):
        copy_standard_bundle(bundle, target)
    assert not (target / "standard/current.json").exists()
    assert not list(target.rglob(".records-copy-*"))


def test_explicit_manifest_and_source_target_separation_are_required(records_case):
    path, _, _, _, _ = publish(records_case)
    with pytest.raises(ContractError):
        open_standard_bundle(path.parent)
    bundle = open_standard_bundle(path)
    with pytest.raises(ContractError):
        copy_standard_bundle(bundle, bundle.company_root / "nested")


@pytest.mark.parametrize("explicit", [False, True])
def test_known_partial_fact_sources_are_preserved_without_rewriting_explicit_evidence(
    records_case, explicit
):
    storage, (company, _), records = records_case
    with storage.read_unit(company.company_id) as read:
        source = records.prepare_source(
            {"kind": "owner_statement", "text": "合成合同已知部分"}, read
        )
    issue = {
        "issue_id": str(uuid4()),
        "scope": "contract-input",
        "evidence_ids": [source["source_id"]] if explicit else [],
        "missing_fields": ["/data/operator_id"],
        "question": "经营者是谁？",
        "assignee": "ai",
        "action": "clarify_reality",
        "known_fact": {
            "kind": "investment.cooperation_contract",
            "data": {
                "contract_source": {"source_id": source["source_id"], "location": "合同第一页"}
            },
        },
    }
    with storage.write_unit(company.company_id) as unit:
        records.apply_current(
            records.plan_change(
                RecordsChange(
                    sources=(source,),
                    unresolved=(issue,),
                    unresolved_scope=issue["scope"],
                    expected_scope_head=0,
                ),
                RecordsContext(unit, str(uuid4())),
            ),
            unit,
        )
    with storage.read_unit(company.company_id) as read:
        assert records.current_issues(issue["scope"], read)["issues"] == [issue]
        # Owner-level check: relation membership is independent of the explicit body list.
        assert read.repository("records").uses("issue", issue["issue_id"]) == (source["source_id"],)
        snapshot = records.capture_documents(company_identity(company), [], [], read)
    prepared = records.prepare_documents(snapshot)
    with storage.maintenance_unit(company.company_id) as unit:
        status = records.publish_documents(prepared, unit)
    path = Path(status["manifest_path"])
    bundle = open_standard_bundle(path)
    assert bundle.issues == (issue,) and bundle.sources == (source,)
    if not explicit:
        rewrite(path, lambda _, parts: parts["sources"].update(sources=[]))
        with pytest.raises(ContractError, match=source["source_id"]):
            open_standard_bundle(path)


def test_missing_known_partial_source_fails_atomically_at_current_issue_save(records_case):
    storage, (company, _), records = records_case
    issue = {
        "issue_id": str(uuid4()),
        "scope": "missing-input",
        "evidence_ids": [],
        "missing_fields": ["/data/operator_id"],
        "question": "经营者是谁？",
        "assignee": "ai",
        "action": "clarify_reality",
        "known_fact": {
            "kind": "investment.cooperation_contract",
            "data": {"contract_source": {"source_id": str(uuid4()), "location": "合同第一页"}},
        },
    }
    with pytest.raises(ContractError):
        with storage.write_unit(company.company_id) as unit:
            records.apply_current(
                records.plan_change(
                    RecordsChange(
                        unresolved=(issue,), unresolved_scope=issue["scope"], expected_scope_head=0
                    ),
                    RecordsContext(unit, str(uuid4())),
                ),
                unit,
            )
    with storage.read_unit(company.company_id) as read:
        assert read.head == 0
        assert records.current_issues(issue["scope"], read)["issues"] == []


def test_copy_revalidates_completed_target_and_preserves_unknown_changed_files(
    records_case, tmp_path, monkeypatch
):
    from ai_accounting_modular.records import _standard

    path, source, _, _, _ = publish(records_case)
    bundle = open_standard_bundle(path)
    original = _standard._copy_file
    target = tmp_path / "damaged-target"

    def damage_after_copy(bundle, target, item):
        copied = original(bundle, target, item)
        if item.relative_path == "standard/current.json":
            (target / source["relative_path"]).write_bytes(b"external change!")
        return copied

    monkeypatch.setattr(_standard, "_copy_file", damage_after_copy)
    with pytest.raises(ContractError):
        copy_standard_bundle(bundle, target)
    assert (target / source["relative_path"]).read_bytes() == b"external change!"
    assert not (target / "standard/current.json").exists()


@pytest.mark.parametrize("destination", [False, True])
def test_links_cannot_redirect_source_or_destination_materials(records_case, tmp_path, destination):
    path, _, _, _, _ = publish(records_case)
    bundle = open_standard_bundle(path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "protected.txt").write_bytes(b"external data")
    if destination:
        target = tmp_path / "linked-target"
        target.mkdir()
        link = target / "originals"
    else:
        source_parts = bundle.company_root / "standard/parts"
        source_parts.rename(bundle.company_root / "standard/original-parts")
        link = source_parts
    link_directory(link, outside)
    try:
        with pytest.raises(ContractError):
            if destination:
                copy_standard_bundle(bundle, target)
            else:
                open_standard_bundle(path)
        assert (outside / "protected.txt").read_bytes() == b"external data"
    finally:
        unlink_directory(link)


def test_failed_copy_cleanup_does_not_follow_a_replaced_parent_directory(
    records_case, tmp_path, monkeypatch
):
    from ai_accounting_modular.records import _standard

    path, source, _, _, _ = publish(records_case)
    bundle = open_standard_bundle(path)
    original = _standard._copy_file
    target, outside = tmp_path / "new-target", tmp_path / "relocated-originals"

    def replace_directory_after_copy(bundle, target, item):
        copied = original(bundle, target, item)
        if item.relative_path == "standard/current.json":
            (target / "originals").rename(outside)
            link_directory(target / "originals", outside)
        return copied

    monkeypatch.setattr(_standard, "_copy_file", replace_directory_after_copy)
    try:
        with pytest.raises(ContractError):
            copy_standard_bundle(bundle, target)
        moved = outside / source["relative_path"].removeprefix("originals/")
        assert moved.read_bytes() == b"synthetic bytes!"
    finally:
        if (target / "originals").is_symlink() or (target / "originals").is_junction():
            unlink_directory(target / "originals")


def test_legal_nested_source_collection_can_exceed_explicit_evidence_limit(records_case):
    storage, (company, _), records = records_case
    with storage.read_unit(company.company_id) as read:
        sources = tuple(
            records.prepare_source(
                {"kind": "owner_statement", "text": f"合成原行来源{index}"}, read
            )
            for index in range(501)
        )
    for start in range(0, len(sources), 500):
        with storage.write_unit(company.company_id) as unit:
            records.apply_current(
                records.plan_change(
                    RecordsChange(sources=sources[start : start + 500]),
                    RecordsContext(unit, str(uuid4())),
                ),
                unit,
            )
    issue = {
        "issue_id": str(uuid4()),
        "scope": "bank-input",
        "evidence_ids": [],
        "missing_fields": ["/data/funds_account_id"],
        "question": "银行账户待补",
        "assignee": "ai",
        "action": "provide_material",
        "known_fact": {
            "kind": "money.bank_statement",
            "data": {
                "rows": [
                    {"source": {"source_id": source["source_id"], "location": "该原件的合成行"}}
                    for source in sources
                ]
            },
        },
    }
    with storage.write_unit(company.company_id) as unit:
        records.apply_current(
            records.plan_change(
                RecordsChange(
                    unresolved=(issue,), unresolved_scope=issue["scope"], expected_scope_head=0
                ),
                RecordsContext(unit, str(uuid4())),
            ),
            unit,
        )
    with storage.read_unit(company.company_id) as read:
        assert records.current_issues(issue["scope"], read)["issues"] == [issue]
        assert len(read.repository("records").uses("issue", issue["issue_id"])) == 501
        snapshot = records.capture_documents(company_identity(company), [], [], read)
    prepared = records.prepare_documents(snapshot)
    with storage.maintenance_unit(company.company_id) as unit:
        status = records.publish_documents(prepared, unit)
    bundle = open_standard_bundle(status["manifest_path"])
    assert len(bundle.sources) == 501 and bundle.issues[0]["evidence_ids"] == []
