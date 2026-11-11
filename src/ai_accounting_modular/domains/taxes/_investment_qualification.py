"""Explain economic events only as far as the current tax calculation requires."""

from copy import deepcopy
from dataclasses import dataclass

from . import _investment_basis as basis
from ._common import month_end
from ._investment_pools import affects_disposal, compatible, stored_identity
from ._investment_tax_math import is_financial_transfer


@dataclass(frozen=True)
class TaxEventView:
    event: dict
    source: dict
    period: str
    effective_on: str | None
    event_index: int


def _explain(view, fact, result):
    event = deepcopy(view.event)
    event["tax_facts"] = deepcopy(result.target["values"]["tax_facts"])
    if "financial_instrument_reference" in result.target["values"]:
        event["financial_instrument_reference"] = deepcopy(
            result.target["values"]["financial_instrument_reference"]
        )
    event["_tax_basis_id"] = fact["business_id"]
    event["_economic_business_id"] = view.source["business_id"]
    return TaxEventView(event, view.source, view.period, view.effective_on, view.event_index)


def _necessary(event, purpose):
    kind, right = event["event_kind"], event.get("right_type")
    character = event.get("income_character", event.get("maturity_income", {}).get("character"))
    if purpose == "income_tax":
        return (
            character in {"dividend", "fund_distribution", "interest"}
            or kind == "debt_interest"
            or kind == "disposal"
            and right == "direct_bond"
        )
    if kind == "disposal":
        return right != "partnership_interest" and not (
            event.get("exit_mode") == "maturity" and right == "direct_bond"
        )
    return (
        kind == "debt_interest"
        or kind == "income"
        and character in {"interest", "market_distribution", "fund_distribution"}
    )


def _identity(data, terms):
    value = {
        "instrument_type": data["tax_facts"]["instrument_type"],
        "right_type": terms["data"]["right_type"],
        "quantity_unit": terms["data"]["quantity_unit"],
    }
    if "financial_instrument_reference" in data:
        value["instrument_reference"] = data["financial_instrument_reference"]["reference"]
    return value


def economic_rights(consumer, context, views):
    """Recover omitted historical rights from the named accounting terms, never tax labels."""
    normalized = []
    for view in views:
        if "terms_id" in view.event and (
            "right_type" not in view.event or "quantity_unit" not in view.event
        ):
            terms = context.get_fact(view.event["terms_id"])
            if terms is None or terms["kind"] != "investment.terms":
                basis.unsupported(
                    consumer, "历史投资事件缺少真实会计权利，且没有可核对的原投资条款。"
                )
            event = deepcopy(view.event)
            for key in ("right_type", "quantity_unit"):
                event.setdefault(key, terms["data"][key])
            view = TaxEventView(
                event, view.source, view.period, view.effective_on, view.event_index
            )
        normalized.append(view)
    return tuple(normalized)


def _check_pool_transitions(consumer, views, terms, relevant, method):
    """Distinct known identities under one contract need a supported continuity rule."""
    identities = {}
    for view in views:
        event = view.event
        actions = relevant.get(event["product_id"], ())
        if "tax_facts" not in event or not any(
            affects_disposal(view, disposal, method) for _, _, _, disposal in actions
        ):
            continue
        identity = _identity(event, terms[event["terms_id"]])
        prior = identities.setdefault(event["terms_id"], [])
        if identity in prior:
            continue
        if any(not compatible(previous, identity) for previous in prior) and any(
            compatible(identity, target) or any(compatible(old, target) for old in prior)
            for _, _, target, _ in actions
        ):
            basis.unsupported(
                consumer,
                "同一条款各实际事件采用的金融商品身份不同且影响本期转让均价；"
                "当前税池连续算法不能覆盖已知依据、按条款合并或猜测拆池。",
            )
        prior.append(identity)


def qualify(consumer, context, plan, views, *, previous=(), period=None, purpose="vat"):
    """A purchase uses a current pool proof, not an invented historic tax status."""
    views = economic_rights(consumer, context, views)
    terms_ids = {view.event["terms_id"] for view in views}
    terms_ids.update(
        lot["terms_id"] for row in previous for lot in row.get("eit_lots", ()) if "terms_id" in lot
    )
    terms_ids.update(
        row["unclassified_terms_id"] for row in previous if "unclassified_terms_id" in row
    )
    found = basis.candidates(context, terms_ids)
    terms = {identity: context.get_fact(identity) for identity in terms_ids}
    adopted, matches, relevant = {}, {}, {}
    method = consumer["data"].get("method")

    def select(terms_id, start, end):
        # Matching has the event/range's identity. Only formal-body loading is
        # shared by basis ID; a terms ID never stands for one month's tax facts.
        key = terms_id, start, end
        if key not in matches:
            chosen = basis.selected(found[terms_id], start, end)
            proof = None
            if chosen is not None:
                identity = chosen["business_id"]
                if identity not in adopted:
                    adopted[identity] = basis.adopt(chosen, context, plan)
                result = adopted[identity]
                proof = chosen, result, _identity(result.target["values"], terms[terms_id])
            matches[key] = proof
        return matches[key]

    explained = {}
    for index, view in enumerate(views):
        if not _necessary(view.event, purpose):
            continue
        terms_id = view.event["terms_id"]
        start, end = basis.event_range(view)
        proof = select(terms_id, start, end)
        if proof is None:
            basis.required(consumer, terms_id, found[terms_id], start, end, view=view)
        explained[index] = _explain(view, proof[0], proof[1])
        if purpose == "vat" and is_financial_transfer(explained[index].event):
            relevant.setdefault(view.event["product_id"], []).append((start, end, proof[2], view))

    def pool_proof(terms_id, product_id, event_range, *, prior_identity=None, view=None):
        # An existing qualification for this event wins. A current transfer may
        # explain an unclassified old buy price, but cannot overwrite known facts.
        exact = select(terms_id, *event_range)
        if exact is not None:
            return exact
        known = {
            "right_type": terms[terms_id]["data"]["right_type"],
            "quantity_unit": terms[terms_id]["data"]["quantity_unit"],
        }
        if prior_identity:
            known.update(prior_identity)
        needed = [
            (start, end, identity)
            for start, end, identity, disposal in relevant.get(product_id, ())
            if compatible(known, identity) and affects_disposal(view, disposal, method)
        ]
        if (
            needed
            and view is not None
            and any(
                row["data"]["applicable_from"] <= event_range[1]
                and row["data"].get("applicable_to", "9999-12-31") >= event_range[0]
                for row in found[terms_id]
            )
        ):
            basis.required(consumer, terms_id, found[terms_id], *event_range, view=view)
        proof = None
        for start, end, identity in needed:
            current = select(terms_id, start, end)
            if current is None:
                if not (prior_identity and all(key in prior_identity for key in identity)):
                    basis.required(consumer, terms_id, found[terms_id], start, end)
                continue
            if proof is not None and not compatible(proof[2], current[2]):
                basis.unsupported(
                    consumer, "本次范围涉及不同金融商品身份，不能把原持仓按比例拆池。"
                )
            proof = current
        return proof

    current = []
    for index, view in enumerate(views):
        if index in explained:
            current.append(explained[index])
            continue
        if purpose != "vat":
            current.append(view)
            continue
        proof = pool_proof(
            view.event["terms_id"], view.event["product_id"], basis.event_range(view), view=view
        )
        current.append(_explain(view, proof[0], proof[1]) if proof is not None else view)

    if relevant:
        _check_pool_transitions(consumer, current, terms, relevant, method)

    positions = []
    for old in previous:
        row = deepcopy(old)
        members = {lot["terms_id"] for lot in row.get("eit_lots", ()) if "terms_id" in lot}
        if "unclassified_terms_id" in row:
            members.add(row["unclassified_terms_id"])
        declared = stored_identity(row)
        complete = len(members) > 0 and all("terms_id" in lot for lot in row.get("eit_lots", ()))
        for terms_id in sorted(members):
            proof = pool_proof(
                terms_id,
                row["product_id"],
                (period + "-01", month_end(period)),
                prior_identity=declared if "unclassified_terms_id" not in row else None,
            )
            if proof is not None:
                if not compatible(declared, proof[2]):
                    if any(
                        compatible(declared, identity) or compatible(proof[2], identity)
                        for _, _, identity, _ in relevant.get(row["product_id"], ())
                    ):
                        basis.unsupported(
                            consumer,
                            "本期税务依据与原池真实金融商品身份不同，不能猜测拆分或沿用旧池。",
                        )
                    complete = False
                    continue
                declared = {**declared, **proof[2]}
            else:
                complete = False
        if complete:
            row.pop("unclassified_terms_id", None)
            row["vat_pool_identity"] = declared
        elif "unclassified_terms_id" in row:
            terms_id = row["unclassified_terms_id"]
            row["_unclassified_identity"] = {
                key: terms[terms_id]["data"][key] for key in ("right_type", "quantity_unit")
            }
        positions.append(row)
    return tuple(current), positions
