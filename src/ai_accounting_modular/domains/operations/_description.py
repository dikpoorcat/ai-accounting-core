"""Describe this owner's explicit typed changes before their edges are registered."""


class DescriptionContext:
    def __init__(self, context, changes):
        self._context = context
        self._facts, self._shadowed = {}, set()
        for change in changes:
            business_id = change.get("business_id", change.get("fact", {}).get("business_id"))
            self._shadowed.add(business_id)
            if change["op"] not in {"withdraw", "handoff_out"}:
                fact = change.get("fact") or context.get_fact(business_id)
                if fact is not None:
                    self._facts[business_id] = fact

    def __getattr__(self, name):
        return getattr(self._context, name)

    def get_fact(self, business_id):
        return (
            self._facts.get(business_id)
            if business_id in self._shadowed
            else self._context.get_fact(business_id)
        )

    def related_facts(
        self,
        producer_id,
        relation_kind,
        *,
        producer_kind="business",
        period_from=None,
        period_to=None,
    ):
        found = {
            fact["business_id"]: fact
            for fact in self._context.related_facts(
                producer_id,
                relation_kind,
                producer_kind=producer_kind,
                period_from=period_from,
                period_to=period_to,
            )
            if fact["business_id"] not in self._shadowed
        }
        if producer_kind == "business":
            fields = {
                "operations.expense_recovery": "expense_id",
                "operations.sale_return": "sale_id",
                "operations.advance_fulfillment": "advance_id",
                "operations.advance_refund": "advance_id",
            }
            for fact in self._facts.values():
                field = fields.get(fact["kind"])
                if relation_kind == "calculation" and field and fact["data"][field] == producer_id:
                    found[fact["business_id"]] = fact
        return tuple(
            fact
            for _, fact in sorted(found.items())
            if (period_from is None or fact["period"] >= period_from)
            and (period_to is None or fact["period"] <= period_to)
        )
