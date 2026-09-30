"""Released v1 party attribution used only to verify frozen report roots."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Hashable, Mapping
from typing import Any

RECLASS = frozenset({
    "1122", "1123", "1221", "122101", "122105", "2202", "2203", "2241",
    "224101", "224102", "224103", "224104", "224105",
})


def _issue(field: str, message: str, **details) -> dict:
    return {"field": field, "message": message, "semantics": "accounting", **details}

def report_party_splits(
    row: Mapping[str, Any], resolution: Mapping[str, Any], *, explicit_party_id: str | None = None
) -> dict:
    """Resolve one reclassifiable row, accepting an exact report classification."""

    amount = row.get("amount_fen", row.get("amount"))
    reverse = -1 if row.get("reverses_id") else 1
    original = amount * reverse if type(amount) is int else None
    relations = [
        relation
        for relation in resolution.get("line_relations", ())
        if relation.get("line_no") == row.get("line_no")
        and relation.get("role") not in {"funds", "tax_transfer", "reserve_expense"}
    ]
    inferred = None
    if (
        original is not None
        and relations
        and all(
            relation.get("state") == "resolved" and relation.get("party_key") is not None
            for relation in relations
        )
        and sum(relation["amount_fen"] for relation in relations) == original
    ):
        parts: dict[Hashable, int] = defaultdict(int)
        for relation in relations:
            parts[relation["party_key"]] += relation["amount_fen"] * reverse
        inferred = tuple((party, part) for party, part in parts.items() if part)
    choices = {party for party, _ in inferred} if inferred is not None else set()
    if (
        inferred is None
        and not relations
        and row.get("account") not in resolution.get("own_party_accounts", ())
    ):
        choices = set(resolution.get("party_candidates_by_account", {}).get(row.get("account"), ()))
    explicit_key = ("party", explicit_party_id) if explicit_party_id else None
    issues = []
    if explicit_key is not None and choices and (len(choices) != 1 or explicit_key not in choices):
        issues.append(
            _issue(
                "counterparty_id",
                "分类与已确认交易方不一致",
                voucher_version_id=row.get("reverses_id") or row.get("version_id"),
                line_no=row.get("line_no"),
            )
        )
    if inferred is not None:
        party_key = inferred[0][0] if len(inferred) == 1 else None
        return {
            "state": "resolved",
            "party_key": explicit_key or party_key,
            "party_id": explicit_party_id
            or (party_key[1] if party_key and party_key[0] == "party" else None),
            "splits": inferred,
            "relations": relations,
            "issues": issues,
        }
    if explicit_key is not None:
        return {
            "state": "resolved",
            "party_key": explicit_key,
            "party_id": explicit_party_id,
            "splits": ((explicit_key, amount),),
            "relations": relations,
            "issues": issues,
        }
    if len(choices) == 1 and type(amount) is int:
        party_key = next(iter(choices))
        return {
            "state": "resolved",
            "party_key": party_key,
            "party_id": party_key[1] if party_key[0] == "party" else None,
            "splits": ((party_key, amount),),
            "relations": relations,
            "issues": issues,
        }
    issues.append(
        _issue(
            "report_classification.counterparty_id",
            "往来明细无法唯一归属交易方",
            voucher_version_id=row.get("reverses_id") or row.get("version_id"),
            line_no=row.get("line_no"),
        )
    )
    return {
        "state": "unresolved",
        "party_key": None,
        "party_id": None,
        "splits": None,
        "relations": relations,
        "issues": issues,
    }

