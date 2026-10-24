"""Exact packaged SQLite structure, independent of the source checkout."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from copy import deepcopy
from functools import cache, lru_cache
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
        value["kind"] != kind
        or value["version"] != (COMPANY_VERSION if kind == "company" else 1)
        or digest(value["objects"]) != value["fingerprint"]
    ):
        raise RuntimeError("Packaged storage contract is damaged")
    return value


def company_structure_contract(*, version: int | None = None) -> dict:
    """Return an exact current or explicitly selected archived contract."""
    contract = company_schema(version)
    return {"format_family": COMPANY_FORMAT, **deepcopy(contract)}


def company_schema(version: int | None = None) -> dict:
    if version is None or type(version) is int and version == COMPANY_VERSION:
        return packaged_schema("company")
    if type(version) is int and version in {1, 2}:
        return previous_company_schema(version)
    raise ContractError("unsupported_structure", "不支持这个公司数据库结构版本")


@lru_cache(maxsize=None, typed=True)
def module_schema(owner: str, *, structure_version=None) -> ModuleSchema:
    return module_contract(company_schema(structure_version), owner)


def module_contract(contract: dict, owner: str) -> ModuleSchema:
    objects = tuple(
        SchemaObject(item["type"], item["name"], item["table"], item["sql"])
        for item in contract["objects"]
        if item["owner"] == owner
    )
    if not objects:
        raise ValueError(f"Unknown storage owner: {owner}")
    raw = [dict(type=item.type, name=item.name, table=item.table, sql=item.sql) for item in objects]
    sql_fingerprint = digest(raw)
    version, parent = 1, None
    if contract["version"] > 1:
        previous = module_contract(company_schema(contract["version"] - 1), owner)
        if previous.sql_fingerprint == sql_fingerprint:
            return previous
        version, parent = previous.version + 1, previous.contract_digest
    return ModuleSchema(
        owner,
        version,
        objects,
        sql_fingerprint,
        digest(
            {
                "owner": owner,
                "version": version,
                "sql_fingerprint": sql_fingerprint,
                "parent_digest": parent,
            }
        ),
        parent,
    )


@lru_cache(maxsize=None, typed=True)
def previous_company_schema(version: int) -> dict:
    if type(version) is not int or version not in {1, 2}:
        raise ContractError("unsupported_structure", "没有这个封存公司结构合同")
    value = json.loads(
        files(__package__)
        .joinpath("schema", "previous", f"company-v{version}.json")
        .read_text("utf-8")
    )
    if (
        value["kind"] != "company"
        or value["version"] != version
        or digest(value["objects"]) != value["fingerprint"]
    ):
        raise RuntimeError("Archived company structure contract is damaged")
    if company_schema(version + 1)["parent_fingerprint"] != value["fingerprint"]:
        raise RuntimeError("Company structure parent does not match")
    return value


def install_structure(connection: sqlite3.Connection, kind: str) -> None:
    # Separate statements preserve the caller's transaction; executescript commits it.
    objects = packaged_schema(kind)["objects"]
    for object_type in ("table", "index", "trigger"):
        for item in objects:
            if item["type"] == object_type:
                connection.execute(item["sql"])


def verify_structure(connection: sqlite3.Connection, kind: str, *, expected=None) -> None:
    expected = packaged_schema(kind) if expected is None else expected
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
        if kind == "company" and expected["version"] == COMPANY_VERSION:
            for version in (2, 1):
                previous = previous_company_schema(version)
                if actual == previous["objects"]:
                    verify_structure(connection, kind, expected=previous)
                    raise ContractError(
                        "structure_upgrade_required", "公司数据库需要先完成明确的结构升级"
                    )
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
            contract = (
                module_schema(owner)
                if expected is packaged_schema("company")
                else module_contract(expected, owner)
            )
            expected_modules.append(
                (
                    owner,
                    contract.version,
                    contract.contract_digest,
                    contract.parent_digest,
                    contract.sql_fingerprint,
                )
            )
        actual_modules = [
            tuple(row)
            for row in connection.execute(
                "SELECT module_key,version,contract_digest,parent_digest,sql_fingerprint "
                "FROM module_schema ORDER BY module_key"
            )
        ]
        if actual_modules != expected_modules:
            raise ContractError("storage_corrupt", "模块结构记录与合同不一致")
