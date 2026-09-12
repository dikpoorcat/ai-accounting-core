"""A narrow immutable proof for the atomic opening-package domain.

A dependency is not a publication root. This proof instead requires an already
proven opening detail, its exact frozen package contract, and the first close's
complete gross trial balance. It never runs an evaluator or reads current heads.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from .domains.opening import CATEGORIES, MODELS
from .types import YearMonth, canonical

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
        or set(item) != {"subject_id", "fact_id", "kind", "values", "opening_lines"}
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
    lines = []
    for member in members:
        fact = facts[member["fact_id"]]
        if (
            fact["kind"] != member["kind"]
            or fact["subject_id"] != member["subject_id"]
            or fact["period"] != package["period"]
            or fact["data"].get("package_id") != package["subject_id"]
            or not fact["evidence"]
            or not isinstance(member["opening_lines"], list)
            or not isinstance(member["values"], dict)
            or any(member["values"].get(key) != value for key, value in fact["data"].items())
        ):
            return None
        counts[CATEGORIES[fact["kind"]]] += 1
        lines.extend(member["opening_lines"])
    # Counts come from the exact immutable facts, not two mutually agreeing labels.
    if dict(counts) != data.get("counts") or dict(counts) != values.get("counts"):
        return None
    if lines != outcome.get("opening_lines") or any(line.get("cashflow") for line in lines):
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


def prove_opening_adoptions(
    connection, reads, *, close_period, manifest, metadata, independent_proofs
):
    """Return every package proven within the complete manifest; callers choose uniquely."""
    members = set(manifest.get("calculations", ()))
    month = str(YearMonth.from_ordinal(close_period))
    package_ids = {
        ident
        for ident in members
        if metadata[ident]["kind"] == "opening_package"
        and metadata[ident]["period"] == month
        and metadata[ident]["posting_period"] == month
    }
    anchor_ids = {
        ident
        for ident in members & independent_proofs.keys()
        if metadata[ident]["kind"] in _DETAIL_KINDS
    }
    if not package_ids or not anchor_ids:
        return {}
    if manifest.get("period") != month or "trial_balance" not in manifest:
        return {}
    if connection.execute(
        "SELECT 1 FROM period_close WHERE period<? LIMIT 1", (close_period,)
    ).fetchone():
        return {}
    if connection.execute(
        "SELECT 1 FROM voucher_version WHERE period<? LIMIT 1", (close_period,)
    ).fetchone():
        return {}
    packages_and_anchors = reads.calculations(package_ids | anchor_ids)
    dependency_facts = {ident: set() for ident in package_ids}
    for row in connection.execute(
        "SELECT d.calculation_id,d.fact_id FROM json_each(?) ids JOIN dependency_fact d "
        "ON d.calculation_id=ids.value",
        (canonical(sorted(package_ids)),),
    ):
        dependency_facts[row["calculation_id"]].add(row["fact_id"])
    facts = reads.facts({ident for ids in dependency_facts.values() for ident in ids})
    references = manifest.get("vouchers", [])
    if not isinstance(references, list) or any(
        not isinstance(item, dict) or not item.get("id") for item in references
    ):
        return {}
    voucher_ids = [item["id"] for item in references]
    if len(set(voucher_ids)) != len(voucher_ids):
        return {}
    vouchers = reads.vouchers(voucher_ids)
    if any(
        vouchers[item["id"]]["period"] != close_period
        or vouchers[item["id"]]["calculation_id"] != item.get("calculation_id")
        or item.get("calculation_id") not in members
        for item in references
    ):
        return {}
    frozen_totals = _totals(manifest["trial_balance"])
    if frozen_totals is None:
        return {}
    # Gross debit/credit from exactly this manifest, with reversal lines as stored.
    voucher_totals = {
        row["account"]: [row["debit"], row["credit"]]
        for row in connection.execute(
            "SELECT l.account,sum(l.debit) debit,sum(l.credit) credit FROM json_each(?) ids "
            "JOIN voucher_line l ON l.version_id=ids.value GROUP BY l.account",
            (canonical(voucher_ids),),
        )
    }
    close = connection.execute(
        "SELECT digest FROM period_close WHERE period=?", (close_period,)
    ).fetchone()
    if close is None:
        return {}
    proven = {}
    for ident in sorted(package_ids):
        package = packages_and_anchors[ident]
        contract = _package_contract(package, facts, dependency_facts[ident])
        if contract is None:
            continue
        declarations, opening_totals = contract
        compatible, anchors = True, []
        for anchor_id in sorted(anchor_ids):
            detail = packages_and_anchors[anchor_id]
            member = declarations.get(detail["subject_id"])
            if member is None:
                continue
            matches = (
                detail["kind"] == member["kind"]
                and detail["fact_id"] == member["fact_id"]
                and detail["period"] == month
                and detail["posting_period"] == month
                and detail["fact_data"].get("package_id") == package["subject_id"]
                and _detail_shape(detail["outcome"])
                and detail["outcome"]["values"] == member["values"]
                and set(reads.parents(anchor_id)) == {ident}
            )
            if not matches:
                compatible = False
                break
            anchors.append(
                {
                    "calculation_id": anchor_id,
                    "fact_id": detail["fact_id"],
                    "result_digest": detail["result_digest"],
                    "selection_proof": independent_proofs[anchor_id],
                }
            )
        if not compatible or not anchors:
            continue
        totals = {key: list(value) for key, value in voucher_totals.items()}
        for account, amounts in opening_totals.items():
            total = totals.setdefault(account, [0, 0])
            total[0] += amounts[0]
            total[1] += amounts[1]
        totals = {key: value for key, value in totals.items() if value != [0, 0]}
        if totals != frozen_totals:
            continue
        proven[ident] = {
            "basis": "manifest_opening_member_adoption",
            "contract_version": 1,
            "close_period": month,
            "close_digest": close["digest"].hex(),
            "package": {
                "calculation_id": ident,
                "fact_id": package["fact_id"],
                "result_digest": package["result_digest"],
            },
            "anchors": anchors,
            "member_fact_ids": sorted(item["fact_id"] for item in declarations.values()),
            "trial_balance_basis": "exact_first_close_vouchers_plus_package_opening_lines",
        }
    return proven
