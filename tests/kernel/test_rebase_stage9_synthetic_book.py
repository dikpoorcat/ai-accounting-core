"""Synthetic fixture rebasing fails closed and preserves business row bytes."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from ai_accounting.kernel import schema, versions
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.storage import Store

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(_SCRIPTS))
_SPEC = importlib.util.spec_from_file_location(
    "rebase_stage9_synthetic_book", _SCRIPTS / "rebase_stage9_synthetic_book.py"
)
assert _SPEC and _SPEC.loader
tool = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(tool)


@pytest.mark.parametrize("damage", ["missing", "bytes", "count", "digest", "escape"])
def test_historical_snapshot_keeps_listed_bytes_paths_count_and_digest(tmp_path, damage):
    from test_prepare_stage9_main120 import _manifest

    tree = tmp_path / "historical"
    sha, _ = _manifest(tree, {
        "src/old.py": b"sealed implementation", "frontend/dist/old.js": b"sealed build",
    })
    tool._require_snapshot(tree, sha, historical=True)
    with pytest.raises(ValueError, match="missing code"):
        tool._require_snapshot(tree, sha)
    path = tree / "source-manifest.json"
    if damage == "missing":
        (tree / "src/old.py").unlink()
    elif damage == "bytes":
        (tree / "src/old.py").write_bytes(b"different bytes")
    else:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if damage == "count":
            manifest["file_count"] += 1
        elif damage == "digest":
            manifest["sha256"] = "0" * 64
        else:
            from snapshot_stage9_source import _digest

            outside = tmp_path / "outside.py"
            outside.write_bytes(b"must not read as sealed source")
            manifest["files"] = {"../outside.py": tool._sha(outside)}
            manifest["file_count"] = 1
            manifest["sha256"] = sha = _digest(manifest["files"])
        path.write_text(json.dumps(manifest), encoding="utf-8")
    # The completion check must fail even though the historical layout passed.
    with pytest.raises(ValueError, match="changed|escaped"):
        tool._require_snapshot(tree, sha, historical=True)


def test_new_snapshot_still_requires_complete_current_layout(tmp_path, monkeypatch):
    from snapshot_stage9_source import _inventory
    from test_prepare_stage9_main120 import _manifest

    tree = tmp_path / "new"
    sha, files = _manifest(tree, {"src/new.py": b"current"})
    monkeypatch.setattr(sys.modules["snapshot_stage9_source"], "_inventory", lambda _: files)
    tool._require_snapshot(tree, sha)
    monkeypatch.setattr(sys.modules["snapshot_stage9_source"], "_inventory", _inventory)
    with pytest.raises(ValueError, match="missing code"):
        tool._require_snapshot(tree, sha)


def _pair(tmp_path, monkeypatch, *, pre_cover=False):
    current = production_bundle()
    new_sql = schema.schema_sql(current.registry)
    new_objects = versions.contract(new_sql)
    new = {(obj["type"], obj["name"]): obj["sql"] for obj in new_objects}
    removed = tool.ADDED | (tool.COVER_INDEXES.keys() if pre_cover else set())
    old = {key: sql for key, sql in new.items() if key not in removed}
    for name, field in (
        ("fact_pass_through", "beneficiary_id"),
        ("fact_payment", "counterparty_id"),
    ):
        old[("table", name)] = old[("table", name)].replace(
            f'"{field}" TEXT,', f'"{field}" TEXT NOT NULL,'
        )
    branch = (
        " WHEN 'managed_reserve_internal_movement' THEN EXISTS(SELECT 1 FROM "
        "fact_managed_reserve_internal_movement t WHERE t.revision_id=f.id)"
    )
    old[("trigger", "fact_seal_shape")] = old[("trigger", "fact_seal_shape")].replace(branch, "")
    order = {"table": 0, "view": 1, "index": 2, "trigger": 3}
    old_sql = (
        ";\n".join(old[key] for key in sorted(old, key=lambda key: (order[key[0]], key[1]))) + ";"
    )
    old_sql += "INSERT INTO state VALUES(1,0,0,0,1,0); INSERT INTO source_change_head VALUES(1,0);"
    old_objects = versions.contract(old_sql)
    old_contract = {
        **current.current("company"),
        "objects": old_objects,
        "sha256": versions.fingerprint(old_objects).hex(),
    }
    old_bundle = replace(
        current,
        development_contracts={
            **current.development_contracts,
            "company": {
                **current.development_contracts.get("company", {}),
                old_contract["sha256"]: old_contract,
            },
        },
        contracts={
            **current.contracts,
            "company": {current.current_versions["company"]: old_contract},
        },
    )
    with monkeypatch.context() as patch:
        patch.setattr(schema, "schema_sql", lambda registry: old_sql)
        source_store = Store.create(
            tmp_path / "source.sqlite", old_bundle, "company", "91310000123456789S", "database"
        )
    target_store = Store.create(
        tmp_path / "target.sqlite", current, "company", "91310000123456789S", "database"
    )
    source = sqlite3.connect(source_store.path)
    target = sqlite3.connect(target_store.path)
    source.execute(
        "INSERT INTO evidence(digest,content,media_type,name) VALUES(?,?,?,?)",
        (b"d" * 32, b"raw\x00bytes\xff", "text/plain", "原样"),
    )
    source.commit()
    return source, target, tool._objects(source), tool._objects(target)


@pytest.mark.parametrize("pre_cover", [False, True])
def test_copy_retains_rowid_bytes_identity_and_generated_metadata(tmp_path, monkeypatch, pre_cover):
    source, target, old, new = _pair(tmp_path, monkeypatch, pre_cover=pre_cover)
    try:
        source_path = tmp_path / "source.sqlite"
        source.close()
        source_sha = tool._sha(source_path)
        source = sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)
        metadata = {
            name: tool._table_digest(target, name, tool._columns(target, name))
            for name in tool.GENERATED
        }
        copied = tool._copy_rows(source, target, old, new, pre_cover=pre_cover)
        assert copied["evidence"]["rows"] == 1
        assert list(source.execute("SELECT rowid,* FROM evidence")) == list(
            target.execute("SELECT rowid,* FROM evidence")
        )
        assert metadata == {
            name: tool._table_digest(target, name, tool._columns(target, name))
            for name in tool.GENERATED
        }
        assert all(
            target.execute(f"SELECT count(*) FROM {name}").fetchone()[0] == 0
            for name in tool.NEW_TABLES
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            target.execute("UPDATE evidence SET name='changed'")
        assert tool._objects(target) == new
        assert tool._sha(source_path) == source_sha
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            source.execute("DELETE FROM state")
    finally:
        source.close()
        target.close()


def test_unexpected_sql_or_new_seed_rows_fail_before_copy(tmp_path, monkeypatch):
    source, target, old, new = _pair(tmp_path, monkeypatch)
    try:
        target.execute("CREATE INDEX unexpected ON evidence(name)")
        with pytest.raises(ValueError, match="Actual saved SQL"):
            tool._copy_rows(source, target, old, new)
        target.execute("DROP INDEX unexpected")
        target.execute(
            "INSERT INTO schema_draft_history VALUES(1,?,?,?,?)",
            (b"a" * 32, b"b" * 32, b"c" * 32, "now"),
        )
        target.commit()
        with pytest.raises(ValueError, match="unexpected generated or seeded"):
            tool._copy_rows(source, target, old, new)
        assert target.execute("SELECT count(*) FROM evidence").fetchone()[0] == 0
    finally:
        source.close()
        target.close()


def test_interrupted_copy_rolls_back_rows_and_restores_guards(tmp_path, monkeypatch):
    source, target, old, new = _pair(tmp_path, monkeypatch)
    try:
        before = tool._digests(target)

        def fail(phase, **values):
            if values.get("table") == "evidence":
                raise RuntimeError("interrupted fixture copy")

        with pytest.raises(RuntimeError, match="interrupted"):
            tool._copy_rows(source, target, old, new, progress=fail)
        assert tool._digests(target) == before
        assert tool._objects(target) == new
        assert target.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        source.close()
        target.close()


def test_foreign_key_failure_rolls_back_every_copied_table(tmp_path, monkeypatch):
    source, target, old, new = _pair(tmp_path, monkeypatch)
    try:
        source.execute("PRAGMA foreign_keys=OFF")
        triggers = [(name, sql) for (kind, name), sql in old.items() if kind == "trigger"]
        for name, _ in triggers:
            source.execute(f'DROP TRIGGER "{name}"')
        source.execute("INSERT INTO fact_evidence VALUES(?,?)", ("missing-fact", b"d" * 32))
        for _, sql in triggers:
            source.execute(sql)
        source.commit()
        before = tool._digests(target)
        with pytest.raises(ValueError, match="foreign key"):
            tool._copy_rows(source, target, old, new)
        assert tool._digests(target) == before
        assert tool._objects(target) == new
        assert target.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        source.close()
        target.close()


def test_resume_requires_fresh_raw_attestation_and_rejects_changed_bytes(tmp_path, monkeypatch):
    source, target, old, new = _pair(tmp_path, monkeypatch)
    try:
        tool._copy_rows(source, target, old, new)
        copied, before_source, before_target = tool._attest_copy(source, target, old, new)
        assert copied["evidence"] == before_source["evidence"] == before_target["evidence"]
        triggers = [(name, sql) for (kind, name), sql in new.items() if kind == "trigger"]
        for name, _ in triggers:
            target.execute(f'DROP TRIGGER "{name}"')
        target.execute("UPDATE evidence SET content=?", (b"changed",))
        for _, sql in triggers:
            target.execute(sql)
        target.commit()
        with pytest.raises(ValueError, match="retain exact source rowid/bytes"):
            tool._attest_copy(source, target, old, new)
        assert tool._objects(target) == new
    finally:
        source.close()
        target.close()


@pytest.mark.parametrize("case", list(tool.CASES))
def test_paths_never_accept_arbitrary_real_or_existing_target(tmp_path, case):
    temporary = tmp_path / ".tmp"
    temporary.mkdir()
    definition = tool.CASES[case]
    args = argparse.Namespace(
        source_root=temporary / case,
        target_root=temporary / tool._target_name(definition),
        source=temporary / definition["source_tree"],
        target_source=temporary / tool.NEW_TREE,
        source_report=temporary / definition["report"],
    )
    for path in (args.source_root, args.source, args.target_source):
        path.mkdir()
    args.source_report.write_text("{}")
    assert tool._paths(args, tmp_path)[1] == args.target_root
    args.target_root.mkdir()
    with pytest.raises(ValueError, match="already exists"):
        tool._paths(args, tmp_path)
    args.source_root = tmp_path / "data" / case
    with pytest.raises(ValueError, match="workspace .tmp"):
        tool._paths(args, tmp_path)


def test_resume_never_accepts_an_arbitrary_or_incomplete_staging_name(tmp_path):
    temporary = tmp_path / ".tmp"
    temporary.mkdir()
    case = next(iter(tool.CASES))
    definition = tool.CASES[case]
    args = argparse.Namespace(
        source_root=temporary / case,
        target_root=temporary / tool._target_name(definition),
        source=temporary / definition["source_tree"],
        target_source=temporary / tool.NEW_TREE,
        source_report=temporary / definition["report"],
        resume_staging=temporary / "stage9-arbitrary.staging",
    )
    args.resume_staging.mkdir()
    with pytest.raises(ValueError, match="explicitly approved"):
        tool._paths(args, tmp_path)
    args.resume_staging = None
    args.attest_copy_only = True
    with pytest.raises(ValueError, match="requires explicit resume"):
        tool._paths(args, tmp_path)


def test_pre_cover_delta_requires_exact_two_index_statements(tmp_path, monkeypatch):
    source, target, old, new = _pair(tmp_path, monkeypatch, pre_cover=True)
    try:
        tool._require_delta(old, new, pre_cover=True)
        with pytest.raises(ValueError, match="inventory"):
            tool._require_delta(old, new)
        changed = dict(new)
        changed[("index", "subject_id_kind_cover")] += " "
        with pytest.raises(ValueError, match="index SQL"):
            tool._require_delta(old, changed, pre_cover=True)
    finally:
        source.close()
        target.close()


def _guard_fixture(tmp_path, kind, status):
    root, source_tree = tmp_path / "synthetic", tmp_path / "fixed-source"
    root.mkdir()
    company_id, taxpayer_id, name, database_id, filename = tool.COMPANIES[kind]
    company = {
        "id": company_id,
        "taxpayer_id": taxpayer_id,
        "name": name,
        "path": str(root / taxpayer_id / "company.sqlite"),
        "database_id": database_id,
    }
    checkpoint = {
        "company": company,
        "month_stats": [{"period": "2016-01", "business_count": 1000, "closed": False}],
        "snapshots": {
            "2016-01": {"closed": False, "owner_confirmation": "owner", "preview_digest": "digest"}
        },
        "epochs": [1, 0, 0, 0, 1, 0],
        "businesses": 1000,
    }
    if kind == "main":
        checkpoint.update(employees_count=50, business_subjects=["synthetic"] * 1000)
    else:
        checkpoint.update(objects_count=50, business_count=1000)
    report = {
        "company": company,
        "root": str(root),
        "source": str(source_tree),
        "status": status,
        "distribution": ("mixed_cumulative" if kind == "main" else "independent_local_pairs"),
        "requested_months": 1,
        "monthly_business_count": 1000,
        "employee_count": 50 if kind == "main" else 0,
        "registered_object_count": 50,
        "business_count": 1000,
        "months": checkpoint["month_stats"],
        "snapshots": checkpoint["snapshots"],
        "integrity": None,
        "construction_integrity": {"final_full_verification_required": True},
    }
    if status == "complete":
        report["integrity"] = {
            "status": "verified",
            "limitations": [],
            "coverage": {name: "verified" for name in tool.COVERAGE},
        }
    checkpoint_path, report_path = root / filename, tmp_path / "report.json"
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    report_path.write_text(json.dumps(report), encoding="utf-8")
    case = {
        "kind": kind,
        "months": 1,
        "status": status,
        "report_sha256": tool._sha(report_path),
        "checkpoint_sha256": tool._sha(checkpoint_path),
    }
    return root, source_tree, report_path, checkpoint_path, case, report


@pytest.mark.parametrize("kind", ["main", "independent"])
@pytest.mark.parametrize("status", ["complete", "built_not_verified"])
def test_source_guard_binds_original_status_identity_and_exact_bytes(tmp_path, kind, status):
    root, tree, path, checkpoint_path, case, report = _guard_fixture(tmp_path, kind, status)
    assert tool._source_guard(root, path, tree, case)[4] == "2016-01"
    original_sha = tool._sha(path)
    report["status"] = "complete" if status != "complete" else "built_not_verified"
    path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError, match="bytes changed"):
        tool._source_guard(root, path, tree, case)
    case["report_sha256"] = tool._sha(path)
    with pytest.raises(ValueError, match="identity, source, status or dimensions"):
        tool._source_guard(root, path, tree, case)
    assert tool._sha(path) != original_sha
    assert tool._sha(checkpoint_path) == case["checkpoint_sha256"]


@pytest.mark.parametrize("status", ["complete", "built_not_verified"])
def test_publish_gate_needs_fresh_full_proof_for_all_raw_counts(status):
    counts = dict(facts=1, calculations=2, vouchers=3, closes=4, evidence=5)
    report = {"status": status, "integrity": {"counts": counts} if status == "complete" else None}
    integrity = {
        "status": "verified",
        "limitations": [],
        "counts": counts,
        "coverage": {name: "verified" for name in tool.COVERAGE},
    }
    tool._require_content_proof({"integrity": integrity}, counts, report)
    for bad in (
        {"status": "built_not_verified"},
        {"limitations": ["partial history"]},
        {"coverage": {"sources": "verified"}},
        {"counts": {**counts, "closes": 0}},
    ):
        with pytest.raises(ValueError, match="all frozen content"):
            tool._require_content_proof({"integrity": {**integrity, **bad}}, counts, report)
    assert report["status"] == status
    if status == "complete":
        with pytest.raises(ValueError, match="Source full-proof counts"):
            tool._require_content_proof(
                {"integrity": integrity},
                counts,
                {"status": status, "integrity": {"counts": {**counts, "closes": 0}}},
            )
