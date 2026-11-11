"""Convert an explicitly selected old company into a separate verified target.

The JSON plan is an array of complete taxes.investment_basis facts. Its evidence
must support the supplied applicability ranges. This command never registers a
company in the live catalog, overwrites an input, or starts a service.
"""

import argparse
from pathlib import Path

from ai_accounting_modular.contracts.public import ContractError, canonical_json, loads_json
from ai_accounting_modular.host.public import create_offline_application


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-kind", choices=("company", "backup", "standard"), required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--basis-facts", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = create_offline_application().convert_investment_basis(
            args.source,
            args.target,
            loads_json(args.basis_facts.read_bytes()),
            source_kind=args.source_kind,
        )
    except (ContractError, OSError) as error:
        if isinstance(error, ContractError):
            print(canonical_json(error.to_response()))
        else:
            print(canonical_json({"error": "source_unavailable", "message": str(error)}))
        return 1
    print(canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
