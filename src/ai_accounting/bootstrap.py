from __future__ import annotations

import argparse
from decimal import Decimal

from sqlalchemy import select

from .coa import seed_organization
from .database import SessionLocal
from .models import Organization


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize one private-pilot organization")
    parser.add_argument("--name", required=True, help="Organization name")
    parser.add_argument("--filing-cycle", choices=["monthly", "quarterly"], default="quarterly")
    parser.add_argument("--jurisdiction", default="CN")
    parser.add_argument("--urban-maintenance-rate", default="0.07")
    args = parser.parse_args()

    with SessionLocal.begin() as session:
        existing = session.scalar(select(Organization).limit(1))
        if existing is not None:
            parser.error(f"phase 1 supports one organization; existing org_id is {existing.id}")
        organization = seed_organization(
            session,
            name=args.name,
            filing_cycle=args.filing_cycle,
            jurisdiction=args.jurisdiction,
            urban_maintenance_rate=Decimal(args.urban_maintenance_rate),
        )
        print(str(organization.id))


if __name__ == "__main__":
    main()
