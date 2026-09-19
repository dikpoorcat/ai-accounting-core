"""Print exact package-owned SQLite object changes without touching a database."""

import argparse
import json

from ai_accounting.kernel.migration_contracts import diff_contracts
from ai_accounting.kernel.schema_bundle import production_bundle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("company", "catalog"))
    parser.add_argument("source", type=int)
    parser.add_argument("target", type=int)
    args = parser.parse_args()
    contracts = production_bundle().contracts[args.kind]
    if args.source not in contracts or args.target not in contracts:
        parser.error("source and target must be packaged contract versions (draft is 0)")
    print(
        json.dumps(
            diff_contracts(contracts[args.source], contracts[args.target]),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
