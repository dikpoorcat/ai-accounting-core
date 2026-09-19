"""Print exact packaged SQLite object changes without touching any database."""

import argparse
import json

from ai_accounting.kernel.migration_contracts import diff_contracts
from ai_accounting.kernel.versions import known_contracts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("business", "catalog"))
    parser.add_argument("source", type=int)
    parser.add_argument("target", type=int)
    args = parser.parse_args()
    contracts = known_contracts(args.kind)
    if args.source not in contracts or args.target not in contracts:
        parser.error("source and target must be packaged standard contract versions")
    print(
        json.dumps(
            diff_contracts(contracts[args.source], contracts[args.target]),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
