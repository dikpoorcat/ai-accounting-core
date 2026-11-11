"""Publish the explicitly captured description upgrade at existing company paths.

The durable journal protects the whole selected batch until --activate closes its
rollback window. Repeating an interrupted publication restores the entire old
batch and requires a newly captured plan. Company directories and catalogs stay
at their existing locations.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
from contextlib import closing, contextmanager
from pathlib import Path
from uuid import UUID, uuid4

from ai_accounting_modular.host.public import create_offline_application
from ai_accounting_modular.storage.public import (
    Storage,
    company_structure_contract,
    open_database_unit,
    verify_database,
)

FORMAT = "ai-accounting-description-publication/1"
SIDECARS = ("-wal", "-shm", "-journal")

_spec = importlib.util.spec_from_file_location(
    "description_upgrade_helper", Path(__file__).with_name("upgrade_business_descriptions.py")
)
handoff = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handoff)


def checked_path(value):
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("交接位置必须是明确的绝对路径。")
    for part in (path, *path.parents):
        if part.is_symlink() or part.is_junction():
            raise ValueError("交接位置不能经过符号链接或目录联接。")
    return path.resolve()


def identifier(value):
    if str(UUID(value)) != value:
        raise ValueError("交接身份必须使用完整的小写UUID。")
    return value


def checked_digest(value):
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError("交接摘要必须是完整的SHA-256。")
    return value


def normalized_plan(value):
    if value.get("format") != FORMAT or not value.get("companies"):
        raise ValueError("发布计划缺少本次交接标识或公司。")
    result = {
        "format": FORMAT,
        "batch_id": identifier(value["batch_id"]),
        "catalog_path": str(checked_path(value["catalog_path"])),
        "companies": [],
    }
    for item in value["companies"]:
        if type(item["database_head"]) is not int or item["database_head"] < 0:
            raise ValueError("原库业务头必须是明确的非负整数。")
        result["companies"].append(
            {
                "company_id": identifier(item["company_id"]),
                "company_root": str(checked_path(item["company_root"])),
                "database_id": identifier(item["database_id"]),
                "database_head": item["database_head"],
                "source_structure_fingerprint": checked_digest(
                    item["source_structure_fingerprint"]
                ),
                "source_sha256": checked_digest(item["source_sha256"]),
                "candidate_root": str(checked_path(item["candidate_root"])),
            }
        )
    for field in ("company_id", "database_id", "company_root"):
        values = [item[field] for item in result["companies"]]
        if len(values) != len(set(values)):
            raise ValueError("发布计划中有重复公司、数据库或资料根。")
    return result


def directory_sync(path):
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def save_journal(path, value):
    value["journal_digest"] = hashlib.sha256(
        handoff.canonical(
            {key: item for key, item in value.items() if key != "journal_digest"}
        ).encode()
    ).hexdigest()
    temporary = path.with_name(path.name + ".tmp-" + str(uuid4()))
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with temporary.open("xb") as stream:
            stream.write((handoff.canonical(value) + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_sync(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def load_journal(path, plan):
    journal = json.loads(path.read_bytes())
    digest = hashlib.sha256(
        handoff.canonical(
            {key: value for key, value in journal.items() if key != "journal_digest"}
        ).encode()
    ).hexdigest()
    if (
        journal.get("journal_digest") != digest
        or journal.get("format") != FORMAT
        or journal.get("plan") != plan
        or journal.get("state")
        not in {"prepared", "switching", "installed", "active", "rolled_back"}
        or len(journal["companies"]) != len(plan["companies"])
    ):
        raise RuntimeError("持久交接清单与本轮计划不一致或已损坏。")
    for item, expected in zip(journal["companies"], plan["companies"], strict=True):
        if any(item.get(key) != value for key, value in expected.items()):
            raise RuntimeError("持久交接清单的公司范围发生变化。")
        checked_digest(item["old_sha256"])
        checked_digest(item["new_sha256"])
        for field in ("old_rows", "new_rows"):
            if not item[field]:
                raise RuntimeError("持久交接清单缺少正式内容证明。")
            for value in item[field].values():
                checked_digest(value)
    return journal


@contextmanager
def service_window(catalog):
    """Use the resident's actual lifetime lock, including when its probe is stale."""
    if not catalog.is_file():
        raise ValueError("所选目录库不存在。")
    lock = checked_path(catalog.with_name(catalog.name + ".modular-lock"))
    with lock.open("a+b") as stream:
        if not lock.stat().st_size:
            stream.write(b"0")
            stream.flush()
            os.fsync(stream.fileno())
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError("所选目录的服务仍在运行，或另一轮交接正在进行。") from error
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def company_paths(item, batch):
    root = checked_path(item["company_root"])
    runtime = checked_path(root / "runtime")
    if runtime.parent != root:
        raise ValueError("运行目录必须直接位于指定公司资料根内。")
    return {
        "database": checked_path(runtime / "company.sqlite3"),
        "prepared": checked_path(runtime / f".description-upgrade-{batch}.new.sqlite3"),
        "rollback": checked_path(runtime / f".description-upgrade-{batch}.old.sqlite3"),
    }


def verify_catalog(plan):
    locations = Storage(Path(plan["catalog_path"])).list_companies()
    actual = {
        item.company_id: (
            item.database_id,
            str(checked_path(item.company_root)),
            str(checked_path(item.database_path)),
        )
        for item in locations
    }
    expected = {
        item["company_id"]: (
            item["database_id"],
            item["company_root"],
            str(company_paths(item, plan["batch_id"])["database"]),
        )
        for item in plan["companies"]
    }
    if actual != expected:
        raise RuntimeError("目录中的有效公司集合或原库位置已变化，请重新捕获计划。")


def clear_sidecars(database):
    for suffix in ("-wal", "-journal"):
        path = checked_path(Path(str(database) + suffix))
        if path.exists() and path.stat().st_size:
            raise RuntimeError("数据库仍有未归入主文件的事务数据，保留现场。")
    for suffix in SIDECARS:
        checked_path(Path(str(database) + suffix)).unlink(missing_ok=True)


def checkpoint(database):
    if not database.is_file():
        raise FileNotFoundError(database)
    with closing(sqlite3.connect(database.as_uri() + "?mode=rw", uri=True)) as connection:
        if connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] != 0:
            raise RuntimeError("数据库仍有使用中的WAL连接，保留原库。")
    clear_sidecars(database)


def database_proof(database):
    """Read the actual complete structure and every authoritative row at one point."""
    owners = {
        item["name"]: item["owner"]
        for item in company_structure_contract()["objects"]
        if item["type"] == "table"
    }
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("数据库完整性检查未通过。")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("数据库存在失效的正式关联。")
        objects = [
            {"owner": owners[table], "type": kind, "name": name, "table": table, "sql": sql}
            for kind, name, table, sql in connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_schema "
                "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
            )
        ]
        objects.sort(key=lambda item: (item["owner"], item["type"], item["name"]))
        fingerprint = hashlib.sha256(handoff.canonical(objects).encode()).hexdigest()
        system = dict(connection.execute("SELECT * FROM system_meta WHERE singleton=1").fetchone())
        company = connection.execute("SELECT company_id FROM company_profile WHERE singleton=1")
        if (
            system["structure_fingerprint"] != fingerprint
            or system["format_family"] != company_structure_contract()["format_family"]
        ):
            raise RuntimeError("原库实际结构与保存的完整指纹不一致。")
        expected_modules = []
        for owner in sorted({item["owner"] for item in objects}):
            owned = [
                {field: item[field] for field in ("type", "name", "table", "sql")}
                for item in objects
                if item["owner"] == owner
            ]
            sql_fingerprint = hashlib.sha256(handoff.canonical(owned).encode()).hexdigest()
            contract_digest = hashlib.sha256(
                handoff.canonical(
                    {"owner": owner, "version": 1, "sql_fingerprint": sql_fingerprint}
                ).encode()
            ).hexdigest()
            expected_modules.append((owner, 1, contract_digest, sql_fingerprint))
        actual_modules = [
            tuple(row)
            for row in connection.execute(
                "SELECT module_key,version,contract_digest,sql_fingerprint "
                "FROM module_schema ORDER BY module_key"
            )
        ]
        if actual_modules != expected_modules:
            raise RuntimeError("原库模块结构记录与实际完整结构不一致。")
        columns = [row[1] for row in connection.execute("PRAGMA table_info(company_note)")]
        legacy = [column for column in columns if column != "purpose"]
        rows = handoff.table_digests(connection)
        legacy_rows = rows
        if "purpose" in columns:
            legacy_rows = dict(rows)
            legacy_rows["company_note"] = handoff.table_digest(
                connection, "company_note", columns=legacy
            )
        return {
            "company_id": company.fetchone()[0],
            "database_id": system["database_id"],
            "database_head": system["global_seq"],
            "schema_version": system["schema_version"],
            "fingerprint": fingerprint,
            "rows": rows,
            "legacy_rows": legacy_rows,
        }


def expected_identity(proof, item, version):
    fields = ("company_id", "database_id", "database_head")
    if any(proof[field] != item[field] for field in fields) or proof["schema_version"] != version:
        raise RuntimeError("公司身份、业务头或结构版本已变化，请重新捕获交接资料。")
    fingerprint = (
        item["source_structure_fingerprint"]
        if version == 6
        else company_structure_contract()["fingerprint"]
    )
    if proof["fingerprint"] != fingerprint:
        raise RuntimeError("数据库的完整结构不属于本轮新旧基线。")


def verify_current(application, database, root):
    verify_database(database)
    with open_database_unit(
        database, repositories=application.repositories, company_root=root
    ) as read:
        application.backup_verifier.verify_snapshot(read)
    checkpoint(database)


def candidate_proof(application, item):
    root = checked_path(item["candidate_root"])
    receipt = json.loads((root / "handoff.json").read_bytes())
    fields = ("company_id", "database_id", "database_head", "source_sha256")
    if receipt.get("state") != "verified" or any(receipt[field] != item[field] for field in fields):
        raise RuntimeError("结构7候选的原包摘要或公司身份不属于本次计划。")
    if checked_path(receipt["target"]) != root:
        raise RuntimeError("候选交接回执指向其他位置。")
    database = checked_path(root / "runtime/company.sqlite3")
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        if connection.execute(
            "SELECT 1 FROM company_note WHERE purpose IS NOT NULL LIMIT 1"
        ).fetchone():
            raise RuntimeError("结构接续候选已包含简短事项写入，请保留并重新整理候选。")
    backup = checked_path(receipt["backup"])
    if not backup.is_relative_to(root / "verified-backup"):
        raise ValueError("候选ZIP必须位于其已验证备份目录。")
    with backup.open("rb") as stream:
        manifest = application.backup_verifier(stream)
    if any(manifest[field] != item[field] for field in fields[:3]):
        raise RuntimeError("候选ZIP与计划身份不一致。")
    clear_sidecars(database)
    descriptor = next(value for value in manifest["files"] if value["kind"] == "database")
    if (handoff.digest_file(database), database.stat().st_size) != (
        descriptor["sha256"],
        descriptor["byte_size"],
    ):
        raise RuntimeError("候选数据库与已验证ZIP不是相同字节。")
    proof = database_proof(database)
    expected_identity(proof, item, 7)
    if proof["fingerprint"] != receipt["structure_fingerprint"]:
        raise RuntimeError("候选实际结构与交接回执不一致。")
    return database, proof


def prepare(application, plan):
    entries = []
    for item in plan["companies"]:
        paths = company_paths(item, plan["batch_id"])
        if paths["prepared"].exists() or paths["rollback"].exists():
            raise FileExistsError("本轮临时文件或回退文件已存在，保留原文件。")
        checkpoint(paths["database"])
        source = database_proof(paths["database"])
        expected_identity(source, item, 6)
        candidate, following = candidate_proof(application, item)
        if source["rows"] != following["legacy_rows"]:
            raise RuntimeError("原库正式内容已变化，请重新捕获交接资料。")
        entries.append(
            {
                **item,
                "old_sha256": handoff.digest_file(paths["database"]),
                "new_sha256": handoff.digest_file(candidate),
                "old_rows": source["rows"],
                "new_rows": following["rows"],
            }
        )
    return {"format": FORMAT, "plan": plan, "state": "prepared", "companies": entries}


def prepare_copies(application, journal):
    for item in journal["companies"]:
        paths = company_paths(item, journal["plan"]["batch_id"])
        candidate = checked_path(Path(item["candidate_root"]) / "runtime/company.sqlite3")
        with candidate.open("rb") as source, paths["prepared"].open("xb") as target:
            shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(target.fileno())
        if handoff.digest_file(paths["prepared"]) != item["new_sha256"]:
            raise RuntimeError("原位置的候选副本与已验证候选字节不一致。")
        verify_current(application, paths["prepared"], Path(item["company_root"]))


def recovery_proof(item, batch):
    paths = company_paths(item, batch)
    rollback = paths["rollback"]
    if rollback.exists():
        clear_sidecars(rollback)
        if handoff.digest_file(rollback) != item["old_sha256"]:
            raise RuntimeError("原库回退文件已变化，保留所有现有数据库。")
    database = paths["database"]
    if not database.exists():
        if not rollback.exists():
            raise RuntimeError("原位置和回退位置都缺少数据库，保留交接清单。")
        return paths
    checkpoint(database)
    proof = database_proof(database)
    version = proof["schema_version"]
    if version not in {6, 7}:
        raise RuntimeError("当前位置不是本轮新旧数据库，禁止旧快照回退。")
    expected_identity(proof, item, version)
    expected_rows = item["new_rows"] if version == 7 else item["old_rows"]
    if proof["rows"] != expected_rows or version == 7 and not rollback.exists():
        raise RuntimeError("已有新的正式写入或原库回退文件缺失，禁止旧快照回退。")
    if (
        version == 6
        and not rollback.exists()
        and (handoff.digest_file(database) != item["old_sha256"])
    ):
        raise RuntimeError("未切换公司的原库字节已变化，保留全部数据库。")
    return paths


def restore_batch(path, journal):
    if journal["state"] == "active":
        raise RuntimeError("本轮已启用，禁止再用旧快照回退。")
    batch = journal["plan"]["batch_id"]
    # Verify every company before restoring any company.
    paths = [recovery_proof(item, batch) for item in journal["companies"]]
    for item, locations in zip(journal["companies"], paths, strict=True):
        if locations["rollback"].exists():
            clear_sidecars(locations["database"])
            os.replace(locations["rollback"], locations["database"])
            directory_sync(locations["database"].parent)
        if handoff.digest_file(locations["database"]) != item["old_sha256"]:
            raise RuntimeError("恢复后的原库字节未通过核验，保留交接清单。")
        if locations["prepared"].exists() and (
            handoff.digest_file(locations["prepared"]) == item["new_sha256"]
        ):
            clear_sidecars(locations["prepared"])
            locations["prepared"].unlink()
    journal["state"] = "rolled_back"
    save_journal(path, journal)
    return {"state": "rolled_back", "journal": str(path), "message": "原库已整批恢复，请重新捕获。"}


def activate(application, path, journal):
    if journal["state"] == "active":
        return {"state": "active", "journal": str(path)}
    if journal["state"] != "installed":
        raise RuntimeError("整批数据库尚未安装并验证，不能启用。")
    for item in journal["companies"]:
        paths = company_paths(item, journal["plan"]["batch_id"])
        verify_installed(application, item, paths["database"])
    journal["state"] = "active"
    save_journal(path, journal)
    return {"state": "active", "journal": str(path)}


def verify_installed(application, item, database):
    checkpoint(database)
    proof = database_proof(database)
    expected_identity(proof, item, 7)
    if proof["rows"] != item["new_rows"]:
        raise RuntimeError("安装后已有新的正式写入，请保全现状并重新核验。")
    verify_current(application, database, Path(item["company_root"]))


def publish(plan, journal_path, *, activate_only=False):
    plan = normalized_plan(plan)
    path = checked_path(journal_path)
    application = create_offline_application()
    with service_window(Path(plan["catalog_path"])):
        verify_catalog(plan)
        if path.exists():
            journal = load_journal(path, plan)
            if activate_only:
                return activate(application, path, journal)
            if journal["state"] == "rolled_back":
                raise RuntimeError("本轮已经恢复旧库，请使用重新捕获的新计划。")
            return restore_batch(path, journal)
        if activate_only:
            raise FileNotFoundError("本轮持久交接清单尚未建立。")
        journal = prepare(application, plan)
        save_journal(path, journal)
        try:
            prepare_copies(application, journal)
            journal["state"] = "switching"
            save_journal(path, journal)
            for item in journal["companies"]:
                paths = company_paths(item, plan["batch_id"])
                checkpoint(paths["database"])
                if handoff.digest_file(paths["database"]) != item["old_sha256"]:
                    raise RuntimeError("原库在发布前发生变化，请重新捕获。")
                if paths["rollback"].exists():
                    raise FileExistsError("回退文件已经存在，不能覆盖。")
                os.rename(paths["database"], paths["rollback"])
                directory_sync(paths["database"].parent)
                os.replace(paths["prepared"], paths["database"])
                directory_sync(paths["database"].parent)
            for item in journal["companies"]:
                paths = company_paths(item, plan["batch_id"])
                verify_installed(application, item, paths["database"])
            journal["state"] = "installed"
            save_journal(path, journal)
        except Exception:
            restore_batch(path, journal)
            raise
    return {"state": "installed", "journal": str(path)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--activate", action="store_true")
    args = parser.parse_args()
    try:
        result = publish(
            json.loads(checked_path(args.plan).read_bytes()),
            args.journal,
            activate_only=args.activate,
        )
    except Exception as error:
        print(handoff.canonical({"state": "failed", "message": str(error)}))
        return 1
    print(handoff.canonical(result))
    return 0 if result["state"] in {"installed", "active"} else 1


if __name__ == "__main__":
    sys.exit(main())
