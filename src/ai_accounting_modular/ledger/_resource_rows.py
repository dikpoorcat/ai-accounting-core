"""One mapping for formal child persistence and accounting-state comparisons."""

from __future__ import annotations

from ai_accounting_modular.contracts.public import canonical_json, parse_fen


def _json(value, key):
    if key not in value:
        return None
    content = value[key]
    if key == "cashflow_parts":
        content = sorted(
            content, key=lambda part: (part["category"], part.get("source_business_id", ""))
        )
    return canonical_json(content)


def resource_rows(result_id, plan):
    return {
        "result_source": [
            (result_id, source, location)
            for source, location in sorted(
                {
                    (s["source_id"], s.get("location", ""))
                    for s in plan["basis"].get("source_locations", [])
                }
            )
        ],
        "obligation": [
            (
                result_id,
                o["slot"],
                o.get("party_id"),
                o["direction"],
                o["category"],
                parse_fen(o["amount_fen"]),
                o["account_code"],
                _json(o, "cashflow_parts"),
                canonical_json(sorted(set(o["allowed_settlements"]))),
                o["business_period"],
                o.get("due_on"),
            )
            for o in plan["obligations"]
        ],
        "settlement": [
            (
                result_id,
                s["settlement_id"],
                s["source"]["business_id"],
                s["source"]["slot"],
                s["consumer"]["slot"],
                s["period"],
                parse_fen(s["amount_fen"]),
                s["method"],
                s.get("movement_id"),
                s.get("agency_business_id"),
                s.get("agency_public_slot"),
                s.get("actual_on"),
            )
            for s in plan["settlements"]
        ],
        "obligation_reclassification": [
            (
                result_id,
                r["relation_id"],
                r["mode"],
                r["source"]["business_id"],
                r["source"]["slot"],
                r["target"]["business_id"],
                r["target"]["slot"],
                parse_fen(r["amount_fen"]),
            )
            for r in plan["obligation_reclassifications"]
        ],
        "cost_lot": [
            (
                result_id,
                c["slot"],
                c["object_id"],
                c["category"],
                parse_fen(c["amount_fen"]),
                c["business_period"],
            )
            for c in plan["cost_lots"]
        ],
        "cost_consumption": [
            (
                result_id,
                c["consumer"]["slot"],
                c["source"]["business_id"],
                c["source"]["slot"],
                c["period"],
                parse_fen(c["amount_fen"]),
            )
            for c in plan["cost_consumptions"]
        ],
        "result_fund_movement": [
            (
                result_id,
                m["movement_id"],
                m["public_slot"],
                m["funds_account_id"],
                m["actual_on"],
                plan["accounting_period"],
                {"inflow": "in", "outflow": "out"}[m["direction"]],
                parse_fen(m["amount_fen"]),
                m.get("transfer_group_id"),
                m.get("party_id"),
                _json(m, "cashflow_parts"),
                None,
            )
            for m in plan["fund_movements"]
        ],
        "result_tax_sale": [
            (
                result_id,
                no,
                t["source_business_id"],
                t["tax_period"],
                parse_fen(t["net_sales_fen"]),
                parse_fen(t["vat_fen"]),
                t["nominal_rate"],
                t["tax_base_rate"],
                t["payable_rate"],
                int(t["exemption_eligible"]),
                t["filing_bucket"],
                t["invoice_kind"],
                canonical_json(t["ranges"]),
            )
            for no, t in enumerate(plan["tax_sales"], 1)
        ],
        "result_report_contribution": [
            (
                result_id,
                r["statement"],
                r["line"],
                r["detail_code"],
                parse_fen(r["amount_fen"]),
            )
            for r in plan["report_contributions"]
        ],
    }
