"""The same typed use mapping serves candidate and adopted resource reads."""

from ai_accounting_modular.contracts.public import ResourceUse, format_fen


def resource_use(row, table, business_id, source):
    ref = {"business_id": row["source_business_id"], "slot": row["source_public_slot"]}
    if table == "settlement":
        value = {
            "settlement_id": row["settlement_id"],
            "source": ref,
            "consumer": {"business_id": business_id, "slot": row["consumer_public_slot"]},
            "period": row["period"],
            "amount_fen": format_fen(row["amount_fen"]),
            "method": row["method"],
        }
        for key in ("movement_id", "agency_business_id", "agency_public_slot", "actual_on"):
            if row[key] is not None:
                value[key] = row[key]
        kind = "settlement"
    elif table == "obligation_reclassification":
        value = {
            "relation_id": row["relation_id"],
            "owner_business_id": business_id,
            "source": ref,
            "target": {"business_id": row["target_business_id"], "slot": row["target_public_slot"]},
            "amount_fen": format_fen(row["amount_fen"]),
            "mode": row["mode"],
        }
        kind = "reclassification"
    else:
        value = {
            "source": ref,
            "consumer": {"business_id": business_id, "slot": row["consumer_public_slot"]},
            "period": row["period"],
            "amount_fen": format_fen(row["amount_fen"]),
        }
        kind = "cost"
    return ResourceUse(kind, source, value)
