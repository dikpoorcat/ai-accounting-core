"""Copy one explicitly approved synthetic Stage 9 book to the owner candidate.

This is a fixture constructor, never a production migration or business replay.
The old/new isolated released/1 candidates are pinned; frozen business bytes
are copied verbatim. A full registered verifier and real open preview precede
publication. Failed staging directories and their diagnostics are retained.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
import traceback
from contextlib import closing
from pathlib import Path

from reseed_stage9_book import (
    _INITIAL_NONEMPTY,
    _INITIAL_ROWS,
    _columns,
    _objects,
    _Progress,
    _quoted,
    _read_json,
    _rows,
    _sha,
    _table_digest,
    _tables,
    _write_json_new,
)

NEW_TREE = "stage9-build-source-owner-r43-release"
NEW_SNAPSHOT = "bc626237e789b9a5d8ad34ea55eb963a300bee856fd0efe3de021a3e4016ea2a"
OLD_COMPANY = "923584f720781cb28a369dd5b269537f64a4551034d306e9447bc2935d263861"
PRE_COVER_COMPANY = "c16b169268f62d31e58d74596af1bc80658488364830ffd615497cac220f5ec7"
NEW_COMPANY = "c9f9f7051bca67f1241ee5c89676fb9476bc819dc8f92c1a0c0bd1e940f459cb"
NEW_CONTENT = "ac6d042b8478d87add37376fab5e5c9b4ae99b6ccab89c1e2f52678b779fe5f8"
CATALOG = "b484d315272bb64de3856b458f04d2d4881da05db7552250864260916c69a4ee"
COMPANIES = {
    "main": (
        "c73e4f4d144340c98ade475b8dd8c8b9",
        "91310000123456789S",
        "阶段九合成规模企业",
        "6cc5f6cbe6cd402ebdfd072195398780",
        "stage9-builder.json",
    ),
    "independent": (
        "680c88a23ffc484d905583ba2962713a",
        "91310000123456789I",
        "阶段九合成独立业务企业",
        "9da703f8a5d04f9b802ba2cc5bb00de3",
        "stage9-independent-builder.json",
    ),
}


def _case(
    kind,
    months,
    source_tree,
    source_snapshot,
    source_content,
    report,
    report_sha256,
    checkpoint_sha256,
    *,
    status="complete",
    pre_cover=False,
):
    return {
        "kind": kind,
        "months": months,
        "source_tree": source_tree,
        "source_snapshot": source_snapshot,
        "source_content": source_content,
        "report": report,
        "report_sha256": report_sha256,
        "checkpoint_sha256": checkpoint_sha256,
        "status": status,
        "pre_cover": pre_cover,
        "company_contract": PRE_COVER_COMPANY if pre_cover else OLD_COMPANY,
        "target_stem": f"stage9-owner-{kind}{months}",
    }


# Each entry was inspected against its actual saved SQL, catalog, checkpoint,
# identity and original report. Construction-only entries remain unverified
# sources; only a fresh target full content proof can certify their copied rows.
CASES = {
    "stage9-reseed-main-12-r36": _case(
        "main",
        12,
        "stage9-build-source-r37-release",
        "eee15731556fd7087541bf867eef166aa1006c59d5b3ad65e86a0313d40cb996",
        "5ff0648ea00702c2d745c68850c3ca924421221b637e6da49a415212f7885597",
        "stage9-index-main12-verification-r37.json",
        "bbe6214f347505f4d90b4d9dcc6f9f3e9e4e4f49ed4f7f963a40d27c35b2ed97",
        "d45fcf4b044b3b92cc3c1769d26ea1c6dc23e6556b3da23da1ed2e14768e9de4",
    ),
    "stage9-reseed-main-48-r36.staging": _case(
        "main",
        48,
        "stage9-build-source-r38-release",
        "f1929db15dd92e0dfa6317569277f6e374a38f0c8eeafce1fcccaa44e29f8a60",
        "5ff0648ea00702c2d745c68850c3ca924421221b637e6da49a415212f7885597",
        "stage9-main48-verification-r38.json",
        "8f4f46bd0ad7b61b4d287356f920b7fb509106053b17f03d91e57a8491a66024",
        "c4e98c422b16b783c96f1d720634b7bc7ea3130748b726da81fc95788d1a218e",
    ),
    "stage9-release-main-120": _case(
        "main",
        120,
        "stage9-build-source-r22-release",
        "760930585879146be8288928e848a3b895df4f5ce03b25755d019b9fa41d81c9",
        "ccea891721436bc8445c7dd6da256e5dd06816814875da7b0cc6717c8519e052",
        "stage9-release-main-120-build-r22h.json",
        "a33b6998b50bd5c3b435b0e4f9ea4907f1328a2cc17814085b56566a675bd7f1",
        "19b61ef1ced63997e7892adb2d6ed2ef00bd3a96440ca60b679c79a3f8b6b588",
        status="built_not_verified",
        pre_cover=True,
    ),
    "stage9-release-independent-12": _case(
        "independent",
        12,
        "stage9-build-source-r15-release",
        "ebb49b1b221f492cef9098594a9d23c98447f42e1ce7d0d6031534e51ba23218",
        "ceba2091a600f2549a12bec8a81310581745d2ca65bc2469a81d224649e898cd",
        "stage9-release-independent-12-verified-r15.json",
        "bdb86835ba8d6a0d1817af01815e6d040472786cba08461190b60e12583a5e59",
        "3b89e8c4f289239ae1daf32b2bf87e045e8f79cf4f25b181ae8176cdf7844b33",
        pre_cover=True,
    ),
    "stage9-release-independent-48": _case(
        "independent",
        48,
        "stage9-build-source-r11-release",
        "3b71ba2031f2dcf302c09c19875a9d37c30af33db8088f7b1d67bff63fbbee6c",
        "6add91e6be5fd7ebb3b7bae7301a25623a72c3c39f43018b83838d805c90fec3",
        "stage9-release-independent-48-build-r11d.json",
        "4b81d46c8fe2748bca219076ce8ada2a6fb0e763b4e3ca26ec91320e8e06de8d",
        "4ed4ec38fc9172edf7932102f723afd8bdf46385f507483bb1c394b60f84fbc6",
        status="built_not_verified",
        pre_cover=True,
    ),
    "stage9-release-independent-120": _case(
        "independent",
        120,
        "stage9-build-source-r30-release",
        "bbb18c584ee827224daade05fbb138d688b4249d35b850d3d02171dcff84da2b",
        "9d888ee8557722ee13a3b3010c59865309d302b482a26513e6cf512af6c74c27",
        "stage9-release-independent-120-verified-r30.json",
        "1318f7993c1de621c01ae24eef452b0ea4ab07c796f8f002ef83bf5053923986",
        "a9dae1bd17426433f7889a844e59cb647d935de67b51bd03a26b4abc213859a0",
        pre_cover=True,
    ),
}
COVER_INDEXES = {
    ("index", "subject_id_kind_cover"): "CREATE INDEX subject_id_kind_cover ON subject(id,kind)",
    (
        "index",
        "fact_id_subject_cover",
    ): "CREATE INDEX fact_id_subject_cover ON fact_revision(id,subject_id)",
}
COVERAGE = frozenset({"sources", "historical_adoption", "projections", "read_indexes"})
NEW_TABLES = frozenset({"fact_managed_reserve_internal_movement", "schema_draft_history"})
ADDED = {("table", name) for name in NEW_TABLES} | {
    ("trigger", name)
    for name in (
        "immutable_fact_managed_reserve_internal_movement_DELETE",
        "immutable_fact_managed_reserve_internal_movement_UPDATE",
        "sealed_fact_managed_reserve_internal_movement_insert",
        "owner_fact_managed_reserve_internal_movement",
        "immutable_schema_draft_history_update",
        "immutable_schema_draft_history_delete",
    )
}
CHANGED = {
    ("table", "fact_pass_through"),
    ("table", "fact_payment"),
    ("trigger", "fact_seal_shape"),
}
# Identity is generated with the exact old identity. Metadata attests the new
# fixture structure; its fresh history is deliberately not an upgrade history.
GENERATED = frozenset({"identity", "schema_meta", "schema_history"})
RESUMABLE_STAGING = frozenset({"stage9-owner-main12-r42.staging"})


def _map(contract):
    return {(item["type"], item["name"]): item["sql"] for item in contract["objects"]}


def _require_snapshot(tree, expected=NEW_SNAPSHOT, *, historical=False):
    from snapshot_stage9_source import _digest, _inventory

    manifest = _read_json(tree / "source-manifest.json")
    files = manifest.get("files")
    if (
        manifest.get("status") != "complete"
        or manifest.get("target") != str(tree)
        or manifest.get("sha256") != expected
        or not isinstance(files, dict)
        or not files
        or manifest.get("file_count") != len(files)
        or _digest(files) != expected
    ):
        raise ValueError("Fixed candidate snapshot content changed")
    # A sealed old source can predate today's frontend/source layout. Check
    # every registered byte and path, without imposing new mandatory files.
    for name, sha in files.items():
        if not isinstance(name, str):
            raise ValueError("Manifest path must be text")
        path = tree / name
        if (
            Path(name).is_absolute()
            or ".." in Path(name).parts
            or path.is_symlink()
            or not path.resolve().is_relative_to(tree)
            or not path.is_file()
            or _sha(path) != sha
        ):
            raise ValueError("Manifest file escaped, disappeared or changed")
    if not historical and _inventory(tree) != files:
        raise ValueError("Fixed candidate snapshot content changed")


def _target_name(case):
    revision = NEW_TREE.removeprefix("stage9-build-source-owner-").removesuffix("-release")
    return f"{case['target_stem']}-{revision}"


def _source_guard(root, report_path, source_tree, case):
    company_id, taxpayer_id, name, database_id, checkpoint_name = COMPANIES[case["kind"]]
    checkpoint_path = root / checkpoint_name
    if (_sha(report_path), _sha(checkpoint_path)) != (
        case["report_sha256"],
        case["checkpoint_sha256"],
    ):
        raise ValueError("Pinned original report or checkpoint bytes changed")
    checkpoint, report = _read_json(checkpoint_path), _read_json(report_path)
    company = {
        "id": company_id,
        "taxpayer_id": taxpayer_id,
        "name": name,
        "path": str(root / taxpayer_id / "company.sqlite"),
        "database_id": database_id,
    }
    months, snapshots = checkpoint.get("month_stats"), checkpoint.get("snapshots")
    distribution = "mixed_cumulative" if case["kind"] == "main" else "independent_local_pairs"
    if (
        checkpoint.get("company") != company
        or report.get("company") != company
        or report.get("root") != str(root)
        or report.get("source") != str(source_tree)
        or report.get("status") != case["status"]
        or report.get("distribution") != distribution
        or report.get("requested_months") != case["months"]
        or report.get("monthly_business_count") != 1000
        or report.get("employee_count") != (50 if case["kind"] == "main" else 0)
        or report.get("business_count") != case["months"] * 1000
        or not isinstance(months, list)
        or len(months) != case["months"]
        or not isinstance(snapshots, dict)
        or len(snapshots) != case["months"]
        or report.get("months") != months
        or report.get("snapshots") != snapshots
        or not isinstance(checkpoint.get("epochs"), list)
        or len(checkpoint["epochs"]) != 6
        or any(type(value) is not int for value in checkpoint["epochs"])
    ):
        raise ValueError("Pinned synthetic identity, source, status or dimensions changed")
    if case["kind"] == "main":
        if (
            checkpoint.get("employees_count") != 50
            or checkpoint.get("businesses") != 1000
            or len(checkpoint.get("business_subjects", [])) != case["months"] * 1000
        ):
            raise ValueError("Main construction dimensions changed")
    elif (
        checkpoint.get("objects_count") != 50
        or checkpoint.get("businesses") != 1000
        or report.get("registered_object_count") != 50
        or checkpoint.get("business_count") != case["months"] * 1000
    ):
        raise ValueError("Independent construction dimensions changed")
    for index, month in enumerate(months):
        period = f"{2016 + index // 12:04d}-{index % 12 + 1:02d}"
        closed = index < case["months"] - 1
        snapshot = snapshots.get(period, {})
        if (
            month.get("period") != period
            or month.get("business_count") != 1000
            or month.get("closed") is not closed
            or snapshot.get("closed") is not closed
            or not isinstance(snapshot.get("owner_confirmation"), str)
            or not snapshot["owner_confirmation"]
            or not isinstance(snapshot.get("preview_digest"), str)
            or not snapshot["preview_digest"]
        ):
            raise ValueError("Completed month, frozen checkpoint or open final month changed")
    if case["status"] == "complete":
        integrity = report.get("integrity", {})
        if (
            integrity.get("status") != "verified"
            or integrity.get("limitations")
            or integrity.get("coverage") != {name: "verified" for name in COVERAGE}
        ):
            raise ValueError("Complete source lacks its original full content proof")
    elif (
        case["status"] != "built_not_verified"
        or report.get("integrity") is not None
        or report.get("construction_integrity", {}).get("final_full_verification_required")
        is not True
    ):
        raise ValueError("Construction-only source must retain its unverified status")
    return case["kind"], checkpoint_name, checkpoint, report, months[-1]["period"]


def _expected_counts(digests):
    return {
        label: digests[table]["rows"]
        for label, table in (
            ("facts", "fact_revision"),
            ("calculations", "calculation"),
            ("vouchers", "voucher_version"),
            ("closes", "period_close"),
            ("evidence", "evidence"),
        )
    }


def _require_content_proof(proof, expected_counts, report):
    integrity = proof.get("integrity", {})
    if (
        integrity.get("status") != "verified"
        or integrity.get("limitations") != []
        or integrity.get("coverage") != {name: "verified" for name in COVERAGE}
        or integrity.get("counts") != expected_counts
    ):
        raise ValueError("Current full verifier did not attest all frozen content")
    if report["status"] == "complete" and report["integrity"].get("counts") != expected_counts:
        raise ValueError("Source full-proof counts differ from the preserved raw rows")


def _require_delta(old, new, *, pre_cover=False):
    added = ADDED | (COVER_INDEXES.keys() if pre_cover else set())
    if new.keys() - old.keys() != added or old.keys() - new.keys():
        raise ValueError("Unapproved synthetic DDL inventory difference")
    if pre_cover and any(new[key] != sql for key, sql in COVER_INDEXES.items()):
        raise ValueError("Approved covering index SQL changed")
    changed = {key for key in old if old[key] != new[key]}
    if changed != CHANGED:
        raise ValueError("Unapproved synthetic DDL SQL difference")
    for name, field in (
        ("fact_pass_through", "beneficiary_id"),
        ("fact_payment", "counterparty_id"),
    ):
        key = ("table", name)
        token = f'"{field}" TEXT NOT NULL'
        if old[key].count(token) != 1 or old[key].replace(token, f'"{field}" TEXT') != new[key]:
            raise ValueError("Nullable field difference has unexpected SQL")
    branch = (
        " WHEN 'managed_reserve_internal_movement' THEN EXISTS(SELECT 1 FROM "
        "fact_managed_reserve_internal_movement t WHERE t.revision_id=f.id)"
    )
    seal = new[("trigger", "fact_seal_shape")]
    if seal.count(branch) != 1 or seal.replace(branch, "") != old[("trigger", "fact_seal_shape")]:
        raise ValueError("Fact seal branch differs beyond the new empty fact type")


def _require_actual(source, target, old, new, *, pre_cover=False, draft_fixture=False):
    if _objects(source) != old or _objects(target) != new:
        raise ValueError("Actual saved SQL differs from its exact pinned package contract")
    if draft_fixture:
        # Only the explicit synthetic constructor selects this branch. It
        # installs a fresh draft, never changes the source's released format.
        if pre_cover or old != new:
            raise ValueError("Draft fixture requires identical complete SQL contracts")
        for connection, status, version in ((source, "released", 1), (target, "draft", 0)):
            if (
                [tuple(row) for row in connection.execute("SELECT id,family,kind,status "
                                                        "FROM schema_meta")]
                != [(1, "ai-accounting-kernel/2", "company", status)]
                or connection.execute("PRAGMA user_version").fetchone()[0] != version
                or [tuple(row) for row in connection.execute(
                    "SELECT version,hex(fingerprint) FROM schema_history"
                )] != [(version, NEW_COMPANY.upper())]
                or connection.execute("SELECT 1 FROM schema_draft_history LIMIT 1").fetchone()
            ):
                raise ValueError("Draft fixture format or empty ancestry changed")
    else:
        _require_delta(old, new, pre_cover=pre_cover)


def _digests(connection):
    return {
        name: dict(
            zip(
                ("rows", "sha256"),
                _table_digest(connection, name, _columns(connection, name)),
                strict=True,
            )
        )
        for name in _tables(connection)
    }


def _attest_copy(source, target, old, new, *, pre_cover=False, draft_fixture=False):
    """Independently prove an explicitly selected committed raw copy."""
    _require_actual(source, target, old, new, pre_cover=pre_cover, draft_fixture=draft_fixture)
    before_source, before_target = _digests(source), _digests(target)
    added_tables = set() if draft_fixture else NEW_TABLES
    if set(before_target) - set(before_source) != added_tables or set(before_source) - set(
        before_target
    ):
        raise ValueError("Staged copy has an unexpected table inventory")
    for name in before_source:
        if _columns(source, name) != _columns(target, name):
            raise ValueError("Staged copy changed insertable fields")
        if (
            name not in {"schema_meta", "schema_history"}
            and before_source[name] != before_target[name]
        ):
            raise ValueError(f"Staged copy does not retain exact source rowid/bytes: {name}")
    if any(before_target[name]["rows"] != 0 for name in added_tables):
        raise ValueError("New staged tables must remain empty")
    if target.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("Staged copy violates a foreign key")
    if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise ValueError("Staged copy fails SQLite integrity")
    copied = {name: values for name, values in before_source.items() if name not in GENERATED}
    return copied, before_source, before_target


def _copy_rows(source, target, old, new, *, progress=None, pre_cover=False, draft_fixture=False):
    _require_actual(source, target, old, new, pre_cover=pre_cover, draft_fixture=draft_fixture)
    tables = _tables(source)
    added_tables = set() if draft_fixture else NEW_TABLES
    if set(_tables(target)) - set(tables) != added_tables or set(tables) - set(_tables(target)):
        raise ValueError("Unexpected table inventory")
    columns = {name: _columns(source, name) for name in tables}
    if any(columns[name] != _columns(target, name) for name in tables):
        raise ValueError("Insertable field layout changed; business conversion is forbidden")
    if target.in_transaction:
        raise ValueError("Copy must own its target transaction")
    nonempty = {
        name
        for name in _tables(target)
        if target.execute(f"SELECT 1 FROM {_quoted(name)} LIMIT 1").fetchone()
    }
    if (
        nonempty != _INITIAL_NONEMPTY
        or any(
            [tuple(row) for row in _rows(target, name, columns[name])] != [expected]
            for name, expected in _INITIAL_ROWS.items()
        )
        or any(
            target.execute(f"SELECT count(*) FROM {_quoted(name)}").fetchone()[0] != 1
            for name in GENERATED
        )
    ):
        raise ValueError("Fresh target has unexpected generated or seeded rows")
    before_generated = {name: _table_digest(target, name, columns[name]) for name in GENERATED}
    if [tuple(row) for row in _rows(source, "identity", columns["identity"])] != [
        tuple(row) for row in _rows(target, "identity", columns["identity"])
    ]:
        raise ValueError("Synthetic identity changed")
    triggers = [(name, sql) for (kind, name), sql in new.items() if kind == "trigger"]
    target.execute("PRAGMA foreign_keys=OFF")
    if target.execute("PRAGMA foreign_keys").fetchone()[0] != 0:
        raise ValueError("Unable to isolate target foreign keys")
    try:
        target.execute("BEGIN IMMEDIATE")
        for name, _ in triggers:
            target.execute(f"DROP TRIGGER {_quoted(name)}")
        copied = {}
        for table in tables:
            fields = columns[table]
            expected = _table_digest(source, table, fields)
            if table in GENERATED:
                continue
            if table in _INITIAL_ROWS:
                saved = [tuple(row) for row in _rows(source, table, fields)]
                if len(saved) != 1 or tuple(saved[0][:2]) != (1, 1):
                    raise ValueError("Source state/head identity changed")
                assignments = ",".join(f"{_quoted(name)}=?" for name in fields[1:])
                target.execute(
                    f"UPDATE {_quoted(table)} SET {assignments} WHERE id=1", saved[0][2:]
                )
            else:
                names = ",".join(("rowid", *(_quoted(name) for name in fields)))
                marks = ",".join("?" for _ in range(len(fields) + 1))
                cursor = _rows(source, table, fields)
                while batch := cursor.fetchmany(256):
                    target.executemany(
                        f"INSERT INTO {_quoted(table)}({names}) VALUES({marks})", batch
                    )
            actual = _table_digest(target, table, fields)
            if expected != actual:
                raise ValueError(f"Raw rowid or saved bytes changed: {table}")
            copied[table] = {"rows": actual[0], "sha256": actual[1]}
            if progress:
                progress("table_copied", table=table, **copied[table])
        for _, sql in triggers:
            target.execute(sql)
        _require_actual(source, target, old, new, pre_cover=pre_cover, draft_fixture=draft_fixture)
        if any(
            target.execute(f"SELECT 1 FROM {_quoted(name)} LIMIT 1").fetchone()
            for name in added_tables
        ):
            raise ValueError("New fixture tables must remain empty")
        if any(
            _table_digest(target, name, columns[name]) != digest
            for name, digest in before_generated.items()
        ):
            raise ValueError("Generated target metadata changed during raw copy")
        if target.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("Copied fixture violates a foreign key")
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Copied fixture fails SQLite integrity")
        target.commit()
        return copied
    except BaseException:
        target.rollback()
        raise
    finally:
        target.execute("PRAGMA foreign_keys=ON")
        if target.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            target.close()
            raise ValueError("Foreign key guards were not restored")


def _checkpoint_new_target(target, *, staging, target_root):
    """Finish only a newly copied, unpublished Stage 9 staging database.

    This is a construction step, never a connection policy or a source repair.
    The caller has committed the copy and checked its exact target schema; raw
    rowid/byte attestation must follow before any catalog registration/publication.
    """
    from ai_accounting.kernel.permissions import reject_reparse_path

    staging, target_root = Path(staging), Path(target_root)
    if (
        not staging.is_absolute()
        or staging.parent.name != ".tmp"
        or target_root.parent != staging.parent
        or not target_root.name.startswith("stage9-")
        or staging.name != target_root.name + ".staging"
        or not staging.is_dir()
        or target_root.exists()
    ):
        raise ValueError("Checkpoint requires a new unpublished Stage 9 staging target")
    reject_reparse_path(staging)
    reject_reparse_path(target_root)
    databases = [tuple(row) for row in target.execute("PRAGMA database_list")]
    main = [row for row in databases if row[1] == "main"]
    if len(main) != 1 or any(row[1] not in {"main", "temp"} for row in databases):
        raise ValueError("Checkpoint requires only the owned main target database")
    path = Path(main[0][2])
    reject_reparse_path(path)
    if (
        path.name != "company.sqlite"
        or path.parent.parent != staging
        or not path.is_file()
        or path.resolve().parent.parent != staging.resolve()
    ):
        raise ValueError("Checkpoint database is outside the owned new target")
    if target.in_transaction:
        raise ValueError("Checkpoint requires a committed copy with no active transaction")
    if (
        target.execute("PRAGMA query_only").fetchone()[0] != 0
        or target.execute("PRAGMA foreign_keys").fetchone()[0] != 1
        or target.execute("PRAGMA journal_mode").fetchone()[0] != "wal"
    ):
        raise ValueError("Checkpoint target must be writable WAL with enabled foreign keys")
    result = tuple(target.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
    if result != (0, 0, 0):
        raise ValueError(f"New target checkpoint did not finish without busy frames: {result}")
    if target.in_transaction or target.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise ValueError("Checkpoint changed target transaction or foreign key guards")
    return {"mode": "new_unpublished_synthetic_target", "result": list(result)}


def _paths(args, workspace):
    paths = tuple(
        path.resolve()
        for path in (
            args.source_root,
            args.target_root,
            args.source,
            args.target_source,
            args.source_report,
        )
    )
    old_root, target_root, old_tree, new_tree, report = paths
    if any(path.parent != (workspace / ".tmp").resolve() for path in paths):
        raise ValueError("Only immediate named workspace .tmp children are accepted")
    if old_root.name not in CASES:
        raise ValueError("Source is not an approved completed synthetic case")
    case = CASES[old_root.name]
    if (target_root.name, report.name, old_tree.name, new_tree.name) != (
        _target_name(case),
        case["report"],
        case["source_tree"],
        NEW_TREE,
    ):
        raise ValueError("Source report, candidate trees or target do not match the exact case")
    staging = target_root.with_name(target_root.name + ".staging")
    resume = getattr(args, "resume_staging", None)
    if getattr(args, "attest_copy_only", False) and resume is None:
        raise ValueError("Copy-only attestation requires explicit resume staging")
    if resume is not None:
        resume = resume.resolve(strict=True)
        if (
            resume.parent != (workspace / ".tmp").resolve()
            or resume.name not in RESUMABLE_STAGING
            or old_root.name != "stage9-reseed-main-12-r36"
        ):
            raise ValueError("Resume needs one explicitly approved synthetic staging directory")
        if resume != staging and staging.exists():
            raise ValueError("A different target staging already exists")
        staging = resume
    if target_root.exists() or (staging.exists() and resume is None):
        raise ValueError("Destination or staging already exists; never overwrite")
    for path in (old_root, old_tree, new_tree, report):
        path.resolve(strict=True)
    return (*paths, staging)


def rebase(args):
    from stage9_source import configure_source, require_source_module, workspace_root

    workspace = workspace_root(Path(__file__).resolve().parents[1], args.workspace)
    old_root, target_root, old_tree, new_tree, report_path, staging = _paths(args, workspace)
    case = CASES[old_root.name]
    _, checkpoint_name, checkpoint, report, period = _source_guard(
        old_root, report_path, old_tree, case
    )
    company = checkpoint["company"]
    _require_snapshot(old_tree, case["source_snapshot"], historical=True)
    _require_snapshot(new_tree, NEW_SNAPSHOT)
    contract_path = Path("src/ai_accounting/kernel/schema_contracts/content-v1.json")
    if (
        _read_json(old_tree / contract_path)["sha256"],
        _read_json(new_tree / contract_path)["sha256"],
    ) != (case["source_content"], NEW_CONTENT):
        raise ValueError("Pinned source or current content contract changed")
    configure_source(new_tree, workspace)
    from ai_accounting.kernel import schema_bundle

    require_source_module(schema_bundle, new_tree, "src/ai_accounting/kernel/schema_bundle.py")
    from stage9_verified_open_preview import verify_book_open_preview

    from ai_accounting.kernel.catalog import Catalog
    from ai_accounting.kernel.engine import Engine
    from ai_accounting.kernel.storage import Store
    from ai_accounting.kernel.types import YearMonth
    from ai_accounting.kernel.versions import verify_schema

    bundle = schema_bundle.production_bundle()
    old_bundle = schema_bundle.load_bundle(
        bundle.registry,
        old_tree / "src/ai_accounting/kernel/schema_contracts",
        family=schema_bundle.FAMILY,
        application_id=schema_bundle.APPLICATION_ID,
        status="released",
        current_versions={"company": 1, "catalog": 1},
    )
    if bundle.status != "released" or bundle.current_versions != {"company": 1, "catalog": 1}:
        raise ValueError("Target must be the pinned isolated released/1 candidate")
    if (
        old_bundle.current("company")["sha256"],
        bundle.current("company")["sha256"],
        old_bundle.current("catalog")["sha256"],
        bundle.current("catalog")["sha256"],
    ) != (case["company_contract"], NEW_COMPANY, CATALOG, CATALOG):
        raise ValueError("Candidate package contract fingerprint changed")
    old, new = _map(old_bundle.current("company")), _map(bundle.current("company"))
    _require_delta(old, new, pre_cover=case["pre_cover"])
    source_db = old_root / company["taxpayer_id"] / "company.sqlite"
    with closing(
        sqlite3.connect((old_root / "catalog.sqlite").as_uri() + "?mode=ro", uri=True)
    ) as source_catalog:
        source_catalog.row_factory = sqlite3.Row
        source_catalog.execute("BEGIN")
        verify_schema(source_catalog, bundle=old_bundle, kind="catalog")
        catalog_companies = [dict(row) for row in source_catalog.execute("SELECT * FROM company")]
        if (
            catalog_companies != [company]
            or source_catalog.execute("SELECT 1 FROM company_setting LIMIT 1").fetchone()
        ):
            raise ValueError("Source catalog company or external settings changed")
        omitted_security = {
            name: source_catalog.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
            for name in ("security_owner", "security_session", "security_audit")
        }
    with closing(sqlite3.connect(source_db.as_uri() + "?mode=ro", uri=True)) as source:
        source.row_factory = sqlite3.Row
        source.execute("BEGIN")
        verify_schema(source, bundle=old_bundle)
        if _objects(source) != old:
            raise ValueError("Source actual SQL differs from its pinned contract")
        if [tuple(row) for row in source.execute("SELECT * FROM state")] != [
            tuple(checkpoint["epochs"])
        ]:
            raise ValueError("Source state changed since completed checkpoint")
        for name in ("jobs", "security_close_approval"):
            if source.execute(f"SELECT 1 FROM {name} LIMIT 1").fetchone():
                raise ValueError("Source contains external delivery/security references")
        closed = {
            str(YearMonth.from_ordinal(row[0])): row[1]
            for row in source.execute(
                "SELECT period,json_extract(manifest,'$.preview_digest') FROM period_close"
            )
        }
        if closed != {
            month: value["preview_digest"]
            for month, value in checkpoint["snapshots"].items()
            if value["closed"] is True
        }:
            raise ValueError("Frozen closes differ from completed checkpoint")
        provenance = {
            "mode": "synthetic_raw_rows_owner_candidate_not_migration",
            "source_root": str(old_root),
            "source_tree": str(old_tree),
            "source_report": str(report_path),
            "source_report_sha256": _sha(report_path),
            "source_checkpoint_sha256": _sha(old_root / checkpoint_name),
            "source_status": case["status"],
            "source_snapshot_sha256": case["source_snapshot"],
            "source_content_contract": case["source_content"],
            "target_content_contract": NEW_CONTENT,
            "old_company_contract": case["company_contract"],
            "new_company_contract": NEW_COMPANY,
            "target_snapshot_sha256": NEW_SNAPSHOT,
            "executor": {
                "path": str(Path(__file__).resolve()),
                "sha256": _sha(Path(__file__).resolve()),
            },
            "catalog_contract": CATALOG,
            "added_objects": [
                list(key)
                for key in sorted(ADDED | (COVER_INDEXES.keys() if case["pre_cover"] else set()))
            ],
            "changed_objects": [list(key) for key in sorted(CHANGED)],
            "generated_target_tables": sorted(GENERATED),
            "omitted_source_catalog_security_rows": omitted_security,
            "existing_business_format_conversion": False,
        }
        if args.dry_run:
            return {"status": "exact_difference_verified", **provenance}
        resume = getattr(args, "resume_staging", None) is not None
        if not resume:
            staging.mkdir()
        progress = _Progress(staging / "stage9-rebase-progress.jsonl")
        progress("resume_requested" if resume else "staging_created")
        try:
            catalog = Catalog(staging, bundle)
            staged_db = staging / company["taxpayer_id"] / "company.sqlite"
            staged_company = {**company, "path": str(staged_db)}
            if resume:
                if any(
                    (staging / name).exists()
                    for name in ("stage9-rebase-finished.json", "stage9-rebase-report.json")
                ):
                    raise ValueError("Published/finished content cannot be resumed")
                with catalog.connection(read_only=True) as connection:
                    if any(
                        connection.execute(f"SELECT 1 FROM {name} LIMIT 1").fetchone()
                        for name in (
                            "company",
                            "company_setting",
                            "security_owner",
                            "security_session",
                            "security_audit",
                        )
                    ):
                        raise ValueError(
                            "Resume staging already has registered company or security rows"
                        )
                if _read_json(staging / checkpoint_name) != {
                    **checkpoint,
                    "company": staged_company,
                }:
                    raise ValueError("Resume checkpoint differs from exact source construction")
                store = Store(
                    staged_db,
                    bundle,
                    company["id"],
                    company["database_id"],
                    taxpayer_id=company["taxpayer_id"],
                )
                with store.connection(read_only=True) as target:
                    copied, source_before, target_before = _attest_copy(
                        source, target, old, new, pre_cover=case["pre_cover"]
                    )
            else:
                staged_db.parent.mkdir()
                store = Store.create(
                    staged_db, bundle, company["id"], company["taxpayer_id"], company["database_id"]
                )
                with store.connection() as target:
                    copied = _copy_rows(
                        source, target, old, new, progress=progress, pre_cover=case["pre_cover"]
                    )
                    verify_schema(target, bundle=bundle)
                    provenance["target_checkpoint"] = _checkpoint_new_target(
                        target, staging=staging, target_root=target_root,
                    )
                    copied, source_before, target_before = _attest_copy(
                        source, target, old, new, pre_cover=case["pre_cover"],
                    )
                _write_json_new(
                    staging / checkpoint_name, {**checkpoint, "company": staged_company}
                )
            copy_proof_path = staging / f"stage9-rebase-copy-proof-{time.time_ns()}.json"
            _write_json_new(
                copy_proof_path,
                {
                    "status": "raw_copy_verified_content_verification_pending",
                    "resumed": resume,
                    "provenance": provenance,
                    "all_source_table_digests": source_before,
                    "all_target_table_digests": target_before,
                },
            )
            progress("committed_raw_copy_attested", resumed=resume)
            if getattr(args, "attest_copy_only", False):
                if not resume:
                    raise ValueError("Copy-only attestation requires explicit resume staging")
                return {
                    "status": "raw_copy_verified_content_verification_pending",
                    "proof": str(copy_proof_path),
                    "published": False,
                }
            progress("full_registered_verification_started")
            proof = verify_book_open_preview(
                Engine(store),
                checkpoint_path=staging / checkpoint_name,
                company=staged_company,
                snapshots=checkpoint["snapshots"],
                period=period,
                source=new_tree,
            )
            _require_content_proof(proof, _expected_counts(source_before), report)
            with store.connection(read_only=True) as target:
                if _digests(target) != target_before or _objects(target) != new:
                    raise ValueError("Registered verifier/preview changed saved fixture content")
            if _digests(source) != source_before:
                raise ValueError("Source content changed within held read snapshot")
            _source_guard(old_root, report_path, old_tree, case)
            _require_snapshot(old_tree, case["source_snapshot"], historical=True)
            _require_snapshot(new_tree, NEW_SNAPSHOT)
            final_company = {
                **company,
                "path": str(target_root / company["taxpayer_id"] / "company.sqlite"),
            }
            # No company is registered or bound until full verification finishes.
            with catalog.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO company(id,taxpayer_id,name,path,database_id) VALUES(?,?,?,?,?)",
                    tuple(
                        staged_company[field]
                        for field in ("id", "taxpayer_id", "name", "path", "database_id")
                    ),
                )
                connection.commit()
            catalog.bind(company["id"])
            with catalog.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE company SET path=? WHERE id=?", (final_company["path"], company["id"])
                )
                connection.commit()
            (staging / checkpoint_name).write_text(
                json.dumps({**checkpoint, "company": final_company}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            final_report = {
                **report,
                "status": "complete",
                "source": str(new_tree),
                "root": str(target_root),
                "company": final_company,
                "measurements": {},
                **proof,
                "rebase_provenance": {
                    **provenance,
                    "all_copied_company_rows": copied,
                    "all_source_table_digests": source_before,
                    "all_target_table_digests": target_before,
                    "source_snapshot_unchanged": True,
                    "verifier_and_preview_unchanged": True,
                },
            }
            _write_json_new(staging / "stage9-rebase-report.json", final_report)
            _write_json_new(
                staging / "stage9-rebase-finished.json",
                {
                    "root": str(target_root),
                    "source": str(new_tree),
                    "company": final_company,
                    "report_sha256": _sha(staging / "stage9-rebase-report.json"),
                },
            )
        except BaseException as error:
            _write_json_new(
                staging / f"stage9-rebase-failed-{time.time_ns()}.json",
                {
                    "status": "failed_not_published",
                    "error": str(error),
                    "traceback": traceback.format_exc(),
                    "provenance": provenance,
                },
            )
            progress("failed_not_published", error=str(error))
            raise
    try:
        os.rename(staging, target_root)
    except BaseException as error:
        _write_json_new(
            staging / f"stage9-rebase-publish-failed-{time.time_ns()}.json",
            {
                "status": "failed_not_published",
                "error": str(error),
                "target_root": str(target_root),
                "traceback": traceback.format_exc(),
            },
        )
        raise
    _Progress(target_root / "stage9-rebase-progress.jsonl")("published")
    return {"status": "complete", "report": str(target_root / "stage9-rebase-report.json")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path)
    for name in ("source-root", "target-root", "source", "target-source", "source-report"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--resume-staging",
        type=Path,
        help="Explicitly reuse the approved r42 staging only after a fresh complete raw-copy proof",
    )
    parser.add_argument(
        "--attest-copy-only",
        action="store_true",
        help="Prove an explicit staged raw copy and stop before registered content verification",
    )
    args = parser.parse_args()
    print(json.dumps(rebase(args), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
