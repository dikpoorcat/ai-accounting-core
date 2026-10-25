"""Hydrate a bounded member page from exact frozen bodies and its selected cached amounts."""

from ai_accounting_modular.contracts.public import ContractError, content_digest

from . import _brief_open_rows as rows
from ._brief_activity_amounts import recognition
from ._brief_activity_sources import Contents, same_reference


def hydrate(reader, members, company_id, read, *, known_contents=()):
    """Expand only this page's exact frozen meanings; retain its already selected amounts."""
    cache = Contents(reader.ledger, read)
    cache.reuse(known_contents)
    cache.load(rows.reference(member) for member in members if "_locator" in member)
    opening = rows._opening(cache.saved.values())
    result = []
    for member in members:
        if "_locator" not in member:  # Explicit cooperation-position variant.
            result.append(member)
            continue
        locator = member["_locator"]
        content = cache.get(locator["reference"])
        if not same_reference(
            content.reference, locator["reference"]
        ) or content.source != rows.source(locator):
            raise ContractError("storage_corrupt", "展开成员的正式来源与已发布目录不一致。")
        definition = rows._resource(locator)
        meaning = rows._meaning(content, definition, opening)
        contribution = meaning.component if meaning.component in rows._CONTRIBUTIONS else None
        rec = recognition(content)
        result.append(
            dict(member)
            | {
                "date": rec["date"],
                "recognition": rec,
                "source_period": content.reference["business_period"],
                "voucher_version_id": None,
                "voucher_number": None,
                "description": member.get("_description") or meaning.description,
                "purpose": meaning.purpose,
                "contribution_group_key": content_digest(
                    [company_id, meaning.person_id, meaning.payroll_period]
                )
                if contribution
                else None,
                "contribution_component": contribution,
                "payroll_period": meaning.payroll_period,
                "_detail_scope": {
                    "reference": content.reference,
                    "source": content.source,
                    "business_period": content.reference["business_period"],
                    "posting_period": content.reference["posting_period"],
                    "kind": content.target["kind"],
                    "direction": definition.direction,
                    "obligations": [
                        {
                            "source": definition.reference,
                            "producer": content.source,
                            "person_id": meaning.person_id,
                            "recipient_id": meaning.recipient_id,
                            "payroll_period": meaning.payroll_period,
                            "component": meaning.component,
                        }
                    ],
                    "allocations": [],
                },
            }
        )
    return result, tuple(cache.saved.values())
