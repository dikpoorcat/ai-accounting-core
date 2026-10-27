"""Convert an isolated company database copy from structure 3 to 4.

The source contract must be exported by its matching old runtime through
``company_structure_contract()``. This development tool does not move company
materials, connect a catalog, register a company or operate a running service.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
from contextlib import closing
from itertools import zip_longest
from pathlib import Path
from tempfile import TemporaryDirectory

from ai_accounting_modular.contracts.public import ContractError, content_digest
from ai_accounting_modular.storage.public import (
    company_structure_contract,
    module_schema,
    publish_company_directory,
    verify_database,
)

_SIDECARS = ("", "-wal", "-shm", "-journal")
_IGNORED_TABLES = {"system_meta", "module_schema"}


def _refuse(message):
    raise ContractError("conversion_refused", message)


def _contract(value):
    fields = {"format_family", "kind", "version", "objects", "fingerprint"}
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or value["kind"] != "company"
        or value["format_family"] != "ai-accounting-modular/1"
        or type(value["version"]) is not int
        or value["version"] != 3
        or not isinstance(value["objects"], list)
        or content_digest(value["objects"]) != value["fingerprint"]
    ):
        _refuse("必须提供旧版工具导出的完整结构 3 公司库合同。")
    objects, identities = value["objects"], set()
    for item in objects:
        if (
            not isinstance(item, dict)
            or set(item) != {"owner", "type", "name", "table", "sql"}
            or any(not isinstance(field, str) or not field for field in item.values())
            or item["type"] not in {"table", "index", "trigger"}
            or (item["type"], item["name"]) in identities
        ):
            _refuse("旧结构合同的对象记录无效或重复。")
        identities.add((item["type"], item["name"]))
    if objects != sorted(objects, key=lambda item: (item["owner"], item["type"], item["name"])):
        _refuse("旧结构合同的对象顺序与完整合同格式不一致。")
    return value


def _transition(old, current):
    if current["version"] != 4:
        _refuse("本工具只支持已核定的结构 3 到 4 副本转换。")
    before = {(item["type"], item["name"]): item for item in old["objects"]}
    after = {(item["type"], item["name"]): item for item in current["objects"]}
    index_key, meta_key = ("index", "read_scope_pending"), ("table", "system_meta")
    if after.keys() - before.keys() != {index_key} or before.keys() - after.keys():
        _refuse("结构差异超出新增待处理读取索引的范围。")
    for key, item in before.items():
        expected = dict(item)
        if key == meta_key:
            marker = "CHECK(schema_version=3)"
            if item["sql"].count(marker) != 1:
                _refuse("旧 system_meta 没有明确的结构 3 检查约束。")
            expected["sql"] = item["sql"].replace(marker, "CHECK(schema_version=4)")
        if expected != after[key]:
            _refuse("结构差异不只包含待处理读取索引及结构版本约束。")


def _module_rows(contract):
    result = []
    for owner in sorted({item["owner"] for item in contract["objects"]}):
        objects = [
            {key: item[key] for key in ("type", "name", "table", "sql")}
            for item in contract["objects"]
            if item["owner"] == owner
        ]
        sql_fingerprint = content_digest(objects)
        result.append(
            (
                owner,
                1,
                content_digest({"owner": owner, "version": 1, "sql_fingerprint": sql_fingerprint}),
                sql_fingerprint,
            )
        )
    return result


def _verify_source(connection, contract):
    owners = {
        item["name"]: item["owner"] for item in contract["objects"] if item["type"] == "table"
    }
    actual = [
        {"owner": owners.get(table), "type": kind, "name": name, "table": table, "sql": sql}
        for kind, name, table, sql in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_schema "
            "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
        )
    ]
    actual.sort(key=lambda item: (item["owner"] or "", item["type"], item["name"]))
    if actual != contract["objects"]:
        _refuse("源库实际结构与所提供的完整旧合同不一致。")
    metadata = connection.execute("SELECT * FROM system_meta WHERE singleton=1").fetchone()
    if (
        metadata is None
        or metadata["format_family"] != contract["format_family"]
        or metadata["schema_version"] != contract["version"]
        or metadata["structure_fingerprint"] != contract["fingerprint"]
    ):
        _refuse("源库的体系、类别、结构版本或完整指纹与旧合同不一致。")
    modules = list(
        connection.execute(
            "SELECT module_key,version,contract_digest,sql_fingerprint FROM module_schema "
            "ORDER BY module_key"
        )
    )
    if [tuple(row) for row in modules] != _module_rows(contract):
        _refuse("源库的模块结构记录与完整旧合同不一致。")
    if (
        connection.execute("PRAGMA journal_mode").fetchone()[0] != "wal"
        or connection.execute("PRAGMA quick_check").fetchone()[0] != "ok"
        or connection.execute("PRAGMA foreign_key_check").fetchone() is not None
    ):
        _refuse("源库完整性检查失败。")


def _file_state(path):
    if not os.path.lexists(path):
        return None
    if path.is_symlink() or path.is_junction() or not path.is_file():
        _refuse("源数据库及旁文件必须是实际文件，不能是链接或目录。")
    before = path.stat()
    with path.open("rb") as stream:
        fingerprint = hashlib.file_digest(stream, "sha256").hexdigest()
    after = path.stat()

    def identity(stat):
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns

    if identity(before) != identity(after):
        _refuse("源文件在复制期间发生变化，请提供稳定的离线副本。")
    return (*identity(after), fingerprint)


def _source_state(source):
    return {suffix: _file_state(Path(str(source) + suffix)) for suffix in _SIDECARS}


def _frozen_copy(source, target):
    before = _source_state(source)
    if before[""] is None:
        _refuse("源公司数据库不存在。")
    for suffix, state in before.items():
        if state is not None:
            shutil.copyfile(Path(str(source) + suffix), Path(str(target) + suffix))
    if _source_state(source) != before or any(
        _file_state(Path(str(target) + suffix))[-1] != state[-1]
        for suffix, state in before.items()
        if state is not None
    ):
        _refuse("源文件在复制期间发生变化，请提供稳定的离线副本。")


def _quoted(name):
    return '"' + name.replace('"', '""') + '"'


def _same_formal_rows(origin, destination, contract):
    result, missing = {}, object()
    for item in contract["objects"]:
        table = item["name"]
        if item["type"] != "table" or item["owner"] == "reading" or table in _IGNORED_TABLES:
            continue
        quoted = _quoted(table)
        columns = list(origin.execute("PRAGMA table_info(" + quoted + ")"))
        primary = sorted((column["pk"], column["name"]) for column in columns if column["pk"])
        order = (
            ",".join(_quoted(name) for _, name in primary)
            if primary
            else ",".join(str(index + 1) for index in range(len(columns)))
        )
        statement = "SELECT * FROM " + quoted + " ORDER BY " + order
        count = 0
        for left, right in zip_longest(
            origin.execute(statement), destination.execute(statement), fillvalue=missing
        ):
            if (
                left is missing
                or right is missing
                or any(type(a) is not type(b) or a != b for a, b in zip(left, right, strict=True))
            ):
                _refuse("正式表逐行核对失败：" + table)
            count += 1
        result[table] = count
    return result


def _convert(origin, destination, current):
    objects = {item["name"]: item for item in current["objects"]}
    original_meta = dict(origin.execute("SELECT * FROM system_meta").fetchone())
    destination.execute("BEGIN IMMEDIATE")
    try:
        destination.execute("DROP TABLE system_meta")
        destination.execute(objects["system_meta"]["sql"])
        destination.execute(
            "INSERT INTO system_meta VALUES(?,?,?,?,?,?)",
            tuple(original_meta[key] for key in ("singleton", "format_family", "database_id"))
            + (4, current["fingerprint"], original_meta["global_seq"]),
        )
        destination.execute(objects["read_scope_pending"]["sql"])
        destination.execute("DELETE FROM module_schema")
        destination.executemany(
            "INSERT INTO module_schema VALUES(?,?,?,?)",
            (
                (owner, schema.version, schema.contract_digest, schema.sql_fingerprint)
                for owner in sorted({item["owner"] for item in current["objects"]})
                for schema in (module_schema(owner),)
            ),
        )
        for item in current["objects"]:
            if item["type"] == "table" and item["owner"] == "reading":
                destination.execute("DELETE FROM " + _quoted(item["name"]))
        formal_tables = _same_formal_rows(origin, destination, current)
        expected_meta = dict(
            original_meta, schema_version=4, structure_fingerprint=current["fingerprint"]
        )
        if dict(destination.execute("SELECT * FROM system_meta").fetchone()) != expected_meta:
            _refuse("副本的数据库身份或正式序号发生了变化。")
        destination.commit()
        return formal_tables
    except BaseException:
        destination.rollback()
        raise


def convert_copy(*, source, target_directory, source_contract):
    """Publish only a validated new database directory; never connect the source to SQLite."""
    old, current = _contract(source_contract), company_structure_contract()
    _transition(old, current)
    source, target = Path(source), Path(target_directory)
    if not source.is_absolute() or not target.is_absolute():
        _refuse("源库和新目标目录必须使用明确的绝对路径。")
    source, target = source.absolute(), target.absolute()
    if os.path.lexists(target):
        raise ContractError("target_exists", "新目标目录必须不存在，不能覆盖已有内容。")
    if not target.parent.is_dir():
        _refuse("新目标目录的上级目录必须已经存在。")
    if target.resolve() in source.resolve().parents:
        _refuse("新目标目录不能包含源数据库。")
    with TemporaryDirectory(
        prefix=".reading-copy-", dir=target.parent, ignore_cleanup_errors=True
    ) as temporary:
        staging = Path(temporary).resolve()
        if not staging.is_relative_to(target.parent.resolve()):
            _refuse("暂存目录没有位于明确目标的上级目录内。")
        raw, output = staging / "source", staging / "output"
        raw.mkdir()
        output.mkdir()
        frozen, converted = raw / "company.sqlite3", output / "company.sqlite3"
        _frozen_copy(source, frozen)
        with (
            closing(sqlite3.connect(frozen, isolation_level=None)) as origin,
            closing(sqlite3.connect(converted, isolation_level=None)) as destination,
        ):
            origin.row_factory = destination.row_factory = sqlite3.Row
            origin.execute("PRAGMA query_only=ON")
            origin.execute("PRAGMA foreign_keys=ON")
            origin.execute("BEGIN")
            _verify_source(origin, old)
            origin.backup(destination)
            destination.execute("PRAGMA foreign_keys=ON")
            formal_tables = _convert(origin, destination, current)
            destination.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        verified = verify_database(converted)
        publish_company_directory(output, target)
    return {
        "database_path": str(target / "company.sqlite3"),
        "company_id": verified.company_id,
        "database_id": verified.database_id,
        "head": verified.head,
        "structure_version": current["version"],
        "structure_fingerprint": verified.structure_fingerprint,
        "formal_tables": formal_tables,
        "registered": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target-directory", type=Path, required=True)
    parser.add_argument("--source-contract", type=Path, required=True)
    args = parser.parse_args()
    try:
        contract = json.loads(args.source_contract.read_text(encoding="utf-8"))
        result = convert_copy(
            source=args.source, target_directory=args.target_directory, source_contract=contract
        )
    except (ContractError, OSError, ValueError, sqlite3.DatabaseError) as error:
        print(json.dumps({"status": "rejected", "message": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": "converted", **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
