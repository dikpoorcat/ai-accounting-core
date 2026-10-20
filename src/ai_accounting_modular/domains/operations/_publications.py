"""Declare which formal periods operating result slots can contribute to."""


def publications(fact, slots):
    collections = []
    if fact["kind"] in {
        "operations.expense",
        "operations.service_sale",
        "operations.other_income",
        "operations.expense_recovery",
        "operations.sale_return",
        "operations.advance_fulfillment",
    }:
        collections.append("pre_tax")
    if fact["kind"] in {
        "operations.service_sale",
        "operations.sale_return",
        "operations.advance_fulfillment",
        "operations.advance_refund",
    } or (
        fact["kind"] == "operations.advance"
        and fact["data"]["side"] == "customer"
        and fact["data"]["tax_status"] == "due"
    ):
        collections.append("tax_sales")
    result = [
        {
            "business_id": fact["business_id"],
            "result_slot": "main",
            "collection": collection,
            "period_from": fact["period"],
            "period_to": fact["period"],
        }
        for collection in collections
    ]
    result.extend(
        {
            "business_id": fact["business_id"],
            "result_slot": slot,
            "collection": "tax_sales",
            "period_from": slot[4:],
            "period_to": slot[4:],
        }
        for slot in slots
        if slot.startswith("tax:")
    )
    return result
