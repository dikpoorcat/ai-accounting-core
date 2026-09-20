"""A narrow immutable proof for the atomic opening-package domain.

The close directly declares its opening result. This module validates the
stored package and members without inferring adoption from downstream anchors
or running an evaluator.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from .domains.opening import CATEGORIES, MODELS

_DETAIL_KINDS = {model.kind for model in MODELS}


def _totals(lines):
    totals = defaultdict(lambda: [0, 0])
    if not isinstance(lines, list):
        return None
    for line in lines:
        if not isinstance(line, dict) or not isinstance(line.get("account"), str):
            return None
        debit, credit = line.get("debit"), line.get("credit")
        if type(debit) is not int or type(credit) is not int or min(debit, credit) < 0:
            return None
        totals[line["account"]][0] += debit
        totals[line["account"]][1] += credit
    return {key: value for key, value in totals.items() if value != [0, 0]}


def _detail_shape(outcome):
    return (
        isinstance(outcome, dict)
        and set(outcome)
        == {"lines", "values", "balances", "explanation", "opening_lines", "opening"}
        and all(outcome[key] == [] for key in ("lines", "balances", "explanation", "opening_lines"))
        and outcome["opening"] is False
        and isinstance(outcome["values"], dict)
    )


def _package_contract(package, facts, dependency_facts):
    """Check the stored package's identities and additive output, never reprice it."""
    data, outcome = package["fact_data"], package["outcome"]
    values = outcome.get("values", {})
    if (
        package["subject_id"] != data.get("package_id")
        or data.get("completeness_confirmed") is not True
        or outcome.get("opening") is not True
        or outcome.get("lines") != []
        or outcome.get("explanation") != []
        or values.get("bookkeeping_start") != package["period"]
        or data.get("period") != package["period"]
        or not isinstance(values.get("members"), list)
    ):
        return None
    declared = {(item["kind"], item["subject_id"]) for item in data["members"]}
    members = values["members"]
    if any(
        not isinstance(item, dict)
        or set(item) != {"subject_id", "fact_id", "kind", "values", "opening_lines", "balances"}
        for item in members
    ):
        return None
    identities = {(item["kind"], item["subject_id"]) for item in members}
    if (
        declared != identities
        or len(declared) != len(data["members"])
        or len(identities) != len(members)
        or len({item["subject_id"] for item in members}) != len(members)
        or any(item["kind"] not in _DETAIL_KINDS for item in members)
    ):
        return None
    expected_ids = {item["fact_id"] for item in members}
    exact_details = {ident for ident in dependency_facts if facts[ident]["kind"] in _DETAIL_KINDS}
    if expected_ids != exact_details or len(expected_ids) != len(members):
        return None
    counts = Counter({category: 0 for category in CATEGORIES.values()})
    lines, balances = [], []
    for member in members:
        fact = facts[member["fact_id"]]
        if (
            fact["kind"] != member["kind"]
            or fact["subject_id"] != member["subject_id"]
            or fact["period"] != package["period"]
            or fact["data"].get("package_id") != package["subject_id"]
            or not fact["evidence"]
            or not isinstance(member["opening_lines"], list)
            or not isinstance(member["balances"], list)
            or not isinstance(member["values"], dict)
            or any(member["values"].get(key) != value for key, value in fact["data"].items())
        ):
            return None
        counts[CATEGORIES[fact["kind"]]] += 1
        lines.extend(member["opening_lines"])
        balances.extend(member["balances"])
    # Counts come from the exact immutable facts, not two mutually agreeing labels.
    if dict(counts) != data.get("counts") or dict(counts) != values.get("counts"):
        return None
    if (
        lines != outcome.get("opening_lines")
        or balances != outcome.get("balances")
        or any(line.get("cashflow") for line in lines)
    ):
        return None
    totals = _totals(lines)
    if totals is None:
        return None
    debit, credit = (
        sum(item[0] for item in totals.values()),
        sum(item[1] for item in totals.values()),
    )
    if debit != credit or debit != values.get("debit_fen") or credit != values.get("credit_fen"):
        return None
    return {item["subject_id"]: item for item in members}, totals
