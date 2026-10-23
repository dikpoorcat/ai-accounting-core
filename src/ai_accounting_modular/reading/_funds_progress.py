"""Funds detail availability from exact allocations and bounded public owner reads."""

from ai_accounting_modular.contracts.public import ContractError, QuerySelection

from ._business_labels import business_fund_accounts
from ._context import last_period


def capture(reader, scope, selected_sources, read):
    """Batch whole-business obligations and current withdrawal state, never detail cards."""
    ids = sorted(selected_sources)
    found = {business: False for business in ids}
    cutoff = max(last_period(reader, read) or scope["period_to"], scope["period_to"])
    for start in range(0, len(ids), 500):
        batch = ids[start : start + 500]
        # The public BusinessGet protocol requires an include entry. Facts is
        # the narrow option; no result or source bodies are requested or used.
        states = reader.businesses.get_businesses(
            {"company_id": read.company_id, "business_ids": batch, "include": ["facts"]}, read,
        )
        if (states["company_id"], states["database_id"]) != (read.company_id, read.database_id):
            raise ContractError("storage_corrupt", "资金业务身份不属于本次公司读取范围。")
        returned = {item["state"]["business_id"]: item["state"] for item in states["businesses"]}
        if returned.keys() != set(batch):
            raise ContractError("storage_corrupt", "资金明细的正式业务身份不完整。")
        for business, state in returned.items():
            found[business] = state["state"] == "withdrawn"
        historical = reader.ledger.get_obligation_positions(
            scope["period_to"], read, business_ids=batch,
            selection=QuerySelection("as_posted", scope["period_to"]),
        )
        for item in historical:
            business = item.definition.reference["business_id"]
            if item.definition.source == selected_sources.get(business):
                found[business] = True
        if cutoff > scope["period_to"]:
            current = reader.ledger.get_obligation_positions(
                cutoff, read, business_ids=batch, selection=QuerySelection("current"),
            )
            for item in current:
                # Whole-business progress can show a later new obligation even
                # when this historical result had none. The detail is not strict.
                business = item.definition.reference["business_id"]
                if business in found:
                    found[business] = True
    return found


def available(value, view, content, presence):
    if not value["subject_id"]:
        return False
    if presence[value["subject_id"]] or view["allocations"]:
        return True
    visible = (value["party"], value["list_summary"] or value["type"])
    description = value["display_summary"]
    if description and description not in visible:
        return True
    return bool(business_fund_accounts(content) - {value["account_id"]})
