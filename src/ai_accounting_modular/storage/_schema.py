"""Exact packaged SQLite structure, independent of the source checkout."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from copy import deepcopy
from functools import cache
from importlib.resources import files

from ai_accounting_modular.contracts.public import ContractError

from ._types import ModuleSchema, SchemaObject

COMPANY_FORMAT = "ai-accounting-modular/1"
CATALOG_FORMAT = "ai-accounting-modular-catalog/1"
COMPANY_VERSION = 3


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


@cache
def packaged_schema(kind: str) -> dict:
    if kind not in {"company", "catalog"}:
        raise ValueError("Unknown storage kind")
    value = json.loads(files(__package__).joinpath("schema", f"{kind}.json").read_text("utf-8"))
    if (
        set(value) != {"kind", "version", "objects", "fingerprint"}
        or value["kind"] != kind
        or value["version"] != (COMPANY_VERSION if kind == "company" else 1)
        or digest(value["objects"]) != value["fingerprint"]
    ):
        raise RuntimeError("Packaged storage contract is damaged")
    return value


def company_structure_contract() -> dict:
    """Return the complete, detached contract for the current company baseline."""
    contract = packaged_schema("company")
    return {"format_family": COMPANY_FORMAT, **deepcopy(contract)}


@cache
def module_schema(owner: str) -> ModuleSchema:
    """Describe one owner's current SQL without a dependency on earlier contracts."""
    objects = tuple(
        SchemaObject(item["type"], item["name"], item["table"], item["sql"])
        for item in packaged_schema("company")["objects"]
        if item["owner"] == owner
    )
    if not objects:
        raise ValueError(f"Unknown storage owner: {owner}")
    raw = [dict(type=item.type, name=item.name, table=item.table, sql=item.sql) for item in objects]
    sql_fingerprint = digest(raw)
    return ModuleSchema(
        owner,
        1,
        objects,
        sql_fingerprint,
        digest(
            {
                "owner": owner,
                "version": 1,
                "sql_fingerprint": sql_fingerprint,
            }
        ),
    )


def install_structure(connection: sqlite3.Connection, kind: str) -> None:
    # Separate statements preserve the caller's transaction; executescript commits it.
    objects = packaged_schema(kind)["objects"]
    for object_type in ("table", "index", "trigger"):
        for item in objects:
            if item["type"] == object_type:
                connection.execute(item["sql"])


def verify_structure(connection: sqlite3.Connection, kind: str) -> None:
    expected = packaged_schema(kind)
    owners = {
        item["name"]: item["owner"] for item in expected["objects"] if item["type"] == "table"
    }
    actual = [
        {"owner": owners.get(table), "type": typ, "name": name, "table": table, "sql": sql}
        for typ, name, table, sql in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_schema "
            "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
        )
    ]
    actual.sort(key=lambda item: (item["owner"] or "", item["type"], item["name"]))
    if actual != expected["objects"]:
        raise ContractError("storage_corrupt", "数据库结构与本体系的结构合同不一致")
    table = "system_meta" if kind == "company" else "catalog_meta"
    row = connection.execute(f"SELECT * FROM {table} WHERE singleton=1").fetchone()
    family = COMPANY_FORMAT if kind == "company" else CATALOG_FORMAT
    if (
        row is None
        or row["format_family"] != family
        or row["schema_version"] != expected["version"]
        or row["structure_fingerprint"] != expected["fingerprint"]
    ):
        raise ContractError("storage_corrupt", "数据库格式或结构标识不一致")
    if kind == "company":
        expected_modules = []
        for owner in sorted({item["owner"] for item in expected["objects"]}):
            contract = module_schema(owner)
            expected_modules.append(
                (
                    owner,
                    contract.version,
                    contract.contract_digest,
                    contract.sql_fingerprint,
                )
            )
        actual_modules = [
            tuple(row)
            for row in connection.execute(
                "SELECT module_key,version,contract_digest,sql_fingerprint "
                "FROM module_schema ORDER BY module_key"
            )
        ]
        if actual_modules != expected_modules:
            raise ContractError("storage_corrupt", "模块结构记录与合同不一致")
