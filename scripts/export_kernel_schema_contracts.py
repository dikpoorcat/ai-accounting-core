"""Generate or verify the package's exact current draft SQLite contracts."""

import argparse
import json
from pathlib import Path

from ai_accounting.kernel.catalog import catalog_sql
from ai_accounting.kernel.schema import schema_sql
from ai_accounting.kernel.schema_bundle import APPLICATION_ID, FAMILY
from ai_accounting.kernel.service import default_registry
from ai_accounting.kernel.versions import contract, fingerprint


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="replace only the package draft JSONs")
    mode.add_argument("--check", action="store_true", help="verify without writing (the default)")
    args = parser.parse_args()
    directory = Path(__file__).resolve().parents[1] / "src/ai_accounting/kernel/schema_contracts"
    changed = []
    for kind, script in (("company", schema_sql(default_registry())), ("catalog", catalog_sql())):
        items = contract(script)
        data = {
            "family": FAMILY,
            "kind": kind,
            "status": "draft",
            "version": 0,
            "application_id": APPLICATION_ID,
            "objects": items,
            "sha256": fingerprint(items).hex(),
        }
        output = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        path = directory / kind / "draft.json"
        if not path.exists() or path.read_text("utf-8") != output:
            changed.append(kind)
            if args.write:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(output, "utf-8")
    if changed and not args.write:
        parser.exit(1, "Draft contracts differ: " + ", ".join(changed) + "\n")
    print("Draft contracts " + ("written" if args.write else "verified"))


if __name__ == "__main__":
    main()
