"""Exact labor-source rows join already computed amounts, obligations and cash uses."""

from collections import defaultdict

from ai_accounting_modular.contracts.public import format_fen, parse_fen

from ._employees_amounts import total

_TAX_LABELS = {
    "not_withheld_not_filed": "尚未记录扣缴及申报",
    "final_statement": "按最终报酬表确认扣税金额",
    "actual_withholding": "按实际扣税记录确认金额",
}


def _key(source):
    return source["result_id"], source["result_generation"], source["content_digest"]


def _period_amounts(events):
    paid, other = [], []
    for item in events:
        use = item.event.use
        if item.recovery:
            continue
        amount = parse_fen(use.value["amount_fen"])
        if use.component == "money":
            paid.append(amount if item.event.movement.value["direction"] == "outflow" else -amount)
        else:
            other.append(-amount if use.component == "reclassify_excess" else amount)
    return total(paid), total(other)


def _obligations(source, positions, events, row_id):
    rows = []
    kind = "labor_project_cost" if source.project_id else "labor"
    for item in positions:
        if item.recovery:
            continue
        position = item.position
        values = position.amounts
        name = "net" if item.component == "net_pay" else item.component
        reference = position.definition.reference
        paid, other = _period_amounts(events.get((reference["business_id"], reference["slot"]), ()))
        rows.append(
            {
                "key": f"{kind}:{row_id}:{name}",
                "name": name,
                "amount_fen": format_fen(values.amount_fen),
                "paid_fen": format_fen(values.money_settled_fen),
                "other_settled_fen": format_fen(
                    total(
                        (
                            values.agency_settled_fen,
                            values.offset_settled_fen,
                            values.transferred_out_fen,
                            -values.excess_reclassified_fen,
                        )
                    )
                ),
                "period_paid_fen": format_fen(paid),
                "period_other_settled_fen": format_fen(other),
                "remaining_fen": format_fen(values.remaining_fen),
            }
        )
    return sorted(rows, key=lambda row: (row["name"] != "net", row["name"], row["key"]))


def labor_sources(reader, activity, objects, current_relationships, read):
    month = activity.scope["period_to"]
    sources = [source for source in activity.sources if source.remuneration_type == "labor"]
    relationships = {
        (person, month): relation for person, relation in current_relationships.items()
    }
    missing = defaultdict(set)
    for source in sources:
        if (source.person_id, source.business_period) not in relationships:
            missing[source.business_period].add(source.person_id)
    for period, ids in sorted(missing.items()):
        ids = sorted(ids)
        for offset in range(0, len(ids), 500):
            relationships.update(
                ((relation.person_id, period), relation)
                for relation in reader.people.relationships(
                    ids[offset : offset + 500], period, read
                )
            )
    entries, positions, events = defaultdict(list), defaultdict(list), defaultdict(list)
    for entry in activity.entries:
        entries[_key(entry.source)].append(entry)
    for item in activity.positions:
        positions[_key(item.position.definition.source)].append(item)
    for item in activity.settlements:
        reference = item.event.use.value["source"]
        events[reference["business_id"], reference["slot"]].append(item)
    rows = []
    for source in sources:
        key = _key(source.source)
        flow = entries[key]
        amounts = {
            name: total(getattr(entry.amounts, field) for entry in flow)
            if flow
            else getattr(source.amounts, field)
            for name, field in (
                ("gross_fen", "gross_fen"),
                ("net_fen", "net_fen"),
                ("booked_tax_fen", "tax_fen"),
            )
        }
        row_id = source.reference["business_id"]
        if flow and all(entry.sign == -1 for entry in flow):
            row_id += f":{source.reference['result_id']}:{source.reference['generation']}"
        relation = relationships[source.person_id, source.business_period]
        rows.append(
            {
                "subject_id": source.reference["business_id"],
                "source_id": row_id,
                "cutoff_period": month,
                "period": source.business_period,
                "person_id": source.person_id,
                "name": objects[source.person_id]["name"],
                "capitalized": source.project_id is not None,
                "project_id": source.project_id,
                **{name: format_fen(value) for name, value in amounts.items()},
                "withholding_method": source.tax_state,
                "withholding_label": _TAX_LABELS[source.tax_state],
                "relationship_confirmed": relation.labor_member is True,
                "checking": bool(relation.issues),
                "obligations": _obligations(source, positions[key], events, row_id),
            }
        )
    return sorted(rows, key=lambda row: (row["period"], row["person_id"], row["source_id"]))
