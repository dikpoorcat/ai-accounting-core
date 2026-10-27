"""Generate the packaged SQLite contracts from the reviewed design DDL."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "src/ai_accounting_modular/storage/schema"


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def generate(kind: str) -> str:
    sql = (ROOT / "docs/design/schema" / f"{kind}.sql").read_text("utf-8")
    table_owners: dict[str, str] = {}
    owner = "storage" if kind == "catalog" else None
    for line in sql.splitlines():
        match = re.match(r"-- owner: (\S+)", line)
        if match:
            owner = match[1]
        match = re.match(r'CREATE TABLE "?(\w+)"?', line)
        if match:
            if owner is None:
                raise ValueError(f"Missing owner: {match[1]}")
            table_owners[match[1]] = owner
    with sqlite3.connect(":memory:") as connection:
        connection.executescript(sql)
        objects = [
            {
                "owner": table_owners[table],
                "type": typ,
                "name": name,
                "table": table,
                "sql": definition,
            }
            for typ, name, table, definition in connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_schema "
                "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type,name"
            )
        ]
    objects.sort(key=lambda item: (item["owner"], item["type"], item["name"]))
    result = {"kind": kind, "version": 4 if kind == "company" else 1, "objects": objects}
    result["fingerprint"] = hashlib.sha256(canonical(objects).encode("utf-8")).hexdigest()
    return json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    for kind in ("catalog", "company"):
        path = OUTPUT / f"{kind}.json"
        expected = generate(kind)
        if args.check:
            if not path.is_file() or path.read_text("utf-8") != expected:
                raise SystemExit(f"Packaged storage contract is stale: {path}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(expected, encoding="utf-8", newline="\n")
    print("Packaged storage contracts are current.")


if __name__ == "__main__":
    main()
