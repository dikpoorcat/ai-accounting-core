"""Print exact package-owned SQLite object changes without touching a database."""

import argparse
import json

from ai_accounting.kernel.catalog import catalog_sql
from ai_accounting.kernel.migration_contracts import diff_contracts
from ai_accounting.kernel.schema import schema_sql
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.service import default_registry
from ai_accounting.kernel.versions import contract, fingerprint


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("company", "catalog"))
    parser.add_argument("source", type=int)
    parser.add_argument("target", help="packaged version number or generated")
    args = parser.parse_args()
    contracts = production_bundle().contracts[args.kind]
    if args.source not in contracts:
        parser.error("source must be a packaged contract version")
    if args.target == "generated":
        script = schema_sql(default_registry()) if args.kind == "company" else catalog_sql()
        objects = contract(script)
        target = {"objects": objects, "sha256": fingerprint(objects).hex()}
    else:
        try:
            version = int(args.target)
            target = contracts[version]
        except (ValueError, KeyError):
            parser.error("target must be a packaged contract version or generated")
    print(
        json.dumps(
            {
                "target_sha256": target["sha256"],
                "changes": diff_contracts(contracts[args.source], target),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
