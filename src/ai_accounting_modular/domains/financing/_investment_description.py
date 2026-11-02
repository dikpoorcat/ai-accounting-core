"""Declared holding membership and exact source dependencies for investments."""

from ai_accounting_modular.contracts.public import YearMonth


def _previous_position(fact, context, terms_id, period):
    terms = context.get_fact(terms_id)
    first = max(terms["period"], context.read_context.start_period)
    previous = str(YearMonth.from_ordinal(YearMonth(period).ordinal - 1))
    if previous < first:
        return None
    return _edge(fact, terms_id, slot="position:" + previous)


def _seed_dependency(fact, context, terms_id):
    seed = context.investment_seed(terms_id)
    if seed is None:
        return None
    return _edge(fact, terms_id, slot="position:" + seed.period, relation="reference")


def _edge(fact, producer, *, mode="result", kind="business", slot="main", relation="calculation"):
    result = {
        "consumer_business_id": fact["business_id"],
        "consumer_result_slot": "main",
        "producer_kind": kind,
        "producer_id": producer,
        "producer_public_slot": slot,
        "read_mode": mode,
        "relation_kind": relation,
    }
    if mode == "collection":
        result.update(period_from="0001-01", period_to=fact["period"])
    return result


def direct(fact):
    data = fact["data"]
    if fact["kind"] == "investment.unit_change_basis":
        return [
            _edge(fact, data["product_id"], mode="fact", kind="object", relation="identity"),
            *(
                _edge(fact, row["business_id"], slot=row["slot"], relation="reference")
                for row in data["unit_change_sources"]
            ),
        ]
    if fact["kind"] == "investment.terms":
        identities = [data["product_id"]]
        for entry in data.get("settlement_authorizations", ()):
            identities.extend((entry["principal_party_id"], entry["agent_id"]))
        return [
            _edge(fact, value, mode="fact", kind="object", relation="identity")
            for value in dict.fromkeys(identities)
        ]
    found = [
        _edge(
            fact,
            data["terms_id"],
            relation=("reference" if fact["kind"] == "investment.income" else "calculation"),
        )
    ]
    if "counterparty_id" in data:
        found.append(
            _edge(fact, data["counterparty_id"], mode="fact", kind="object", relation="identity")
        )
    for field in ("source", "income_source"):
        if field in data:
            found.append(
                _edge(
                    fact,
                    data[field]["business_id"],
                    relation="cost" if field == "source" else "settlement",
                )
            )
    for item in data.get("lot_allocations", ()):
        found.append(_edge(fact, item["source"]["business_id"], relation="cost"))
    for item in data.get("entitlements", ()):
        found.append(_edge(fact, item["source"]["business_id"], relation="cost"))
    recognized = data.get("maturity_income", {}).get("recognized_income_source")
    if recognized is not None:
        found.append(_edge(fact, recognized["business_id"]))
    return found


def dependencies(fact, context):
    found = direct(fact)
    if fact["kind"] == "investment.unit_change_basis":
        for row in fact["data"]["unit_change_sources"]:
            observer = _edge(fact, row["business_id"], mode="collection", relation="reference")
            observer["period_to"] = "9999-12"
            found.append(observer)
        return found
    if fact["kind"] == "investment.terms":
        first = max(fact["period"], context.read_context.start_period)
        found.extend(
            [
                {
                    **_edge(fact, fact["business_id"], mode="collection"),
                    "period_to": "9999-12",
                    "notify_effect": "description",
                },
                {
                    **_edge(fact, "work", mode="collection", kind="calendar"),
                    "period_from": first,
                    "period_to": "9999-12",
                    "notify_effect": "description",
                },
            ]
        )
        openings = context.investment_openings(fact["business_id"])
        for result_slot in slots(fact, context):
            if not result_slot.startswith("position:"):
                continue
            retained = context.investment_retained_dependencies(fact["business_id"], result_slot)
            if retained is not None:
                found.extend(retained)
                continue
            period = result_slot.removeprefix("position:")
            for producer in [
                fact,
                *openings,
                *context.investment_period_facts(fact["business_id"], period),
            ]:
                found.append(
                    {**_edge(fact, producer["business_id"]), "consumer_result_slot": result_slot}
                )
            previous = _previous_position(fact, context, fact["business_id"], period)
            if previous is not None:
                found.append({**previous, "consumer_result_slot": result_slot})
            seed = _seed_dependency(fact, context, fact["business_id"])
            if seed is not None:
                found.append({**seed, "consumer_result_slot": result_slot})
        return found
    if fact["kind"] == "investment.income":
        return found
    terms_id = fact["data"]["terms_id"]
    observer = _edge(fact, terms_id, mode="collection")
    found.append(observer)
    # Candidate replay reads earlier fact quantities. Only earlier result producers
    # can define an active monetary lot, so these edges cannot make sibling cycles.
    if fact["kind"] in {
        "investment.dispose",
        "investment.loss",
        "investment.capital_return",
        "investment.unit_change",
        "investment.debt_interest",
    }:
        for reference in context.investment_uses(terms_id, fact["business_id"]):
            found.append(_edge(fact, reference["business_id"]))
            use = _edge(
                fact, reference["business_id"], mode="use", slot=reference["slot"], relation="cost"
            )
            use.update(
                period_from=fact["period"], period_to=fact["period"], resource_components=["cost"]
            )
            found.append(use)
    for opening in context.investment_openings(terms_id):
        found.append(_edge(fact, opening["business_id"]))
    previous = _previous_position(fact, context, terms_id, fact["period"])
    if previous is not None:
        found.append(previous)
    seed = _seed_dependency(fact, context, terms_id)
    if seed is not None:
        found.append(seed)
    if "income_source" in fact["data"]:
        reference = fact["data"]["income_source"]
        found.append(
            _edge(
                fact,
                reference["business_id"],
                mode="use",
                slot=reference["slot"],
                relation="settlement",
            )
        )
        found[-1].update(
            period_from=fact["period"], period_to=fact["period"], resource_components=["offset"]
        )
    return found


def slots(fact, context=None):
    if fact["kind"] != "investment.terms":
        return ["main"]
    result = [
        "main",
        *(
            "authorization:" + entry["entry_key"]
            for entry in fact["data"].get("settlement_authorizations", ())
        ),
    ]
    if context is not None and (
        context.investment_facts(fact["business_id"])
        or context.investment_existing_positions(fact["business_id"])
        or any(
            member["data"].get("type") == "investment"
            and member["data"].get("terms_id") == fact["business_id"]
            for opening in context.investment_openings(fact["business_id"])
            for member in opening["data"]["members"]
        )
    ):
        first = YearMonth(max(fact["period"], context.read_context.start_period)).ordinal
        last = YearMonth(
            max(str(YearMonth.from_ordinal(first)), context.evaluation_period_to)
        ).ordinal
        result.extend(
            "position:" + str(YearMonth.from_ordinal(number)) for number in range(first, last + 1)
        )
    return result


def publications(fact):
    if fact["kind"] not in {
        "investment.income",
        "investment.dispose",
        "investment.loss",
        "investment.debt_interest",
    }:
        return []
    return [
        {
            "business_id": fact["business_id"],
            "result_slot": "main",
            "collection": "pre_tax",
            "period_from": fact["period"],
            "period_to": fact["period"],
        }
    ]
