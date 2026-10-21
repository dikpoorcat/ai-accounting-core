"""Decode persisted resources into their own contracts, without inventing a plan."""

from collections import defaultdict

from ai_accounting_modular.contracts.public import format_fen, loads_json, validate_contract

from ._output_digest import RESOURCE_TABLES
from ._planning import stored_source_reference
from ._report_details import report_contribution
from ._resource_view import resource_definition
from ._values import check_parts, fail, unique

_DEFINITIONS = {
    "obligation": "obligation",
    "cost_lot": "cost",
    "result_fund_movement": "funds",
}


def _selected(row, fields):
    return {key: row[key] for key in fields if row[key] is not None}


def _ref(row, prefix):
    return {"business_id": row[prefix + "_business_id"], "slot": row[prefix + "_public_slot"]}


def verify_resources(records, heads):
    seen = defaultdict(list)
    slots = defaultdict(list)
    outputs = set()
    for table in RESOURCE_TABLES:
        for row in records[table]:
            head = heads.get(row["result_id"])
            if head is None:
                fail("storage_corrupt", "正式资源缺少所属结果。")
            result_id, business_id = head["result_id"], head["business_id"]
            outputs.add(result_id)
            if head["target_state"] == "withdrawn":
                fail("storage_corrupt", "已撤去目标不能保留有效资源。")
            if table in _DEFINITIONS:
                kind = _DEFINITIONS[table]
                definition = resource_definition(kind, head, row, stored_source_reference(head))
                value = definition.value
                slots[result_id].append((kind, definition.reference["slot"]))
                seen[table, business_id, row["public_slot"], head["posting_period"]].append(
                    result_id
                )
                if kind == "obligation":
                    check_parts(value, obligation=True)
                    unique(value["allowed_settlements"], "正式义务的允许清偿方式不能重复。")
                elif kind == "funds":
                    check_parts(value)
                    if (
                        row["period"] != head["business_period"]
                        or value["actual_on"][:7] != row["period"]
                    ):
                        fail("storage_corrupt", "真实资金的实际日、核算月和所属结果不一致。")
            elif table in {"settlement", "cost_consumption"}:
                value = {
                    "source": _ref(row, "source"),
                    "consumer": {"business_id": business_id, "slot": row["consumer_public_slot"]},
                    "amount_fen": format_fen(row["amount_fen"]),
                    "period": row["period"],
                }
                if table == "settlement":
                    value.update(
                        _selected(
                            row,
                            (
                                "settlement_id",
                                "method",
                                "movement_id",
                                "agency_business_id",
                                "agency_public_slot",
                                "actual_on",
                            ),
                        )
                    )
                validate_contract(
                    "core.schema.json",
                    "Settlement" if table == "settlement" else "CostConsumption",
                    value,
                )
                if row["period"] != head["business_period"]:
                    fail("storage_corrupt", "正式清偿或成本消耗月份与所属结果不一致。")
            elif table == "obligation_reclassification":
                validate_contract(
                    "business-facts.schema.json",
                    "ObligationReclassification",
                    {
                        "relation_id": row["relation_id"],
                        "owner_business_id": business_id,
                        "source": _ref(row, "source"),
                        "target": _ref(row, "target"),
                        "amount_fen": format_fen(row["amount_fen"]),
                        "mode": row["mode"],
                    },
                )
            elif table == "result_tax_sale":
                if row["exemption_eligible"] not in (0, 1):
                    fail("storage_corrupt", "正式税额贡献的布尔值不合法。")
                value = _selected(
                    row,
                    (
                        "source_business_id",
                        "tax_period",
                        "nominal_rate",
                        "tax_base_rate",
                        "payable_rate",
                        "filing_bucket",
                        "invoice_kind",
                    ),
                )
                value.update(
                    net_sales_fen=format_fen(row["net_sales_fen"]),
                    vat_fen=format_fen(row["vat_fen"]),
                    exemption_eligible=bool(row["exemption_eligible"]),
                    ranges=loads_json(row["ranges_json"]),
                )
                validate_contract("business-facts.schema.json", "TaxSale", value)
                seen[table, result_id].append(row["item_no"])
            else:
                report_contribution(row)
    for key, values in seen.items():
        if key[0] == "result_tax_sale":
            if sorted(values) != list(range(1, len(values) + 1)):
                fail("storage_corrupt", "正式税额贡献序号不连续。")
        elif len(values) != 1:
            fail("storage_corrupt", "同一期间的公开资源槽不能重复。")
    return outputs, slots


def verify_selected_slots(slots, heads):
    """Already checked definitions must remain unique in each actual selected set."""
    seen = set()
    for head in heads:
        for kind, slot in slots.get(head["result_id"], ()):
            key = kind, head["business_id"], slot
            if key in seen:
                fail("storage_corrupt", "同一业务的公开资源槽不能重复。")
            seen.add(key)
