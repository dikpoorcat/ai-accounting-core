"""Explicit offline proofs for the one supported pre-separation investment shape.

These helpers never relax the normal Fact or ApplyRequest validator. Original
bodies are validated before a detached projection is used to validate a document
envelope. Frozen accounting results keep their own normal retained contracts.
"""

from copy import deepcopy
from hashlib import sha256

from ._fact_analysis import FactAnalysis
from ._fact_sources import _validated_paths, partial_fact_source_ids
from ._validation import canonical_json, validate_contract

_RIGHTS = {
    "securities_fund": "fund_units",
    "contractual_fund": "fund_units",
    "financial_contract": "financial_contract",
    "company_equity": "company_equity",
    "partnership_interest": "partnership_interest",
    "direct_bond": "direct_bond",
}


def is_legacy_investment(fact):
    return (
        isinstance(fact, dict)
        and fact.get("kind") == "investment.terms"
        and isinstance(fact.get("data"), dict)
        and bool(
            {"legal_form", "tax_facts", "financial_instrument_reference"} & fact["data"].keys()
        )
    )


def converted_investment_fact(fact, *, partial=False):
    """Change only the accounting representation, after proving its old contract."""
    definition = (
        "HistoricalPartialFactInvestmentTerms" if partial else "HistoricalFactInvestmentTerms"
    )
    validate_contract("business-facts.schema.json", definition, fact)
    result = deepcopy(fact)
    data = result["data"]
    if "legal_form" in data:
        data["right_type"] = _RIGHTS[data.pop("legal_form")]
    data.pop("tax_facts", None)
    data.pop("financial_instrument_reference", None)
    validate_contract("business-facts.schema.json", "PartialFact" if partial else "Fact", result)
    return result


class _LegacyInvestmentAnalysis(FactAnalysis):
    def __init__(self, fact):
        definition = "HistoricalFactInvestmentTerms"
        validate_contract("business-facts.schema.json", definition, fact)
        body = canonical_json(fact)
        paths = _validated_paths(fact, definition)
        object.__setattr__(self, "_body", body)
        object.__setattr__(self, "digest", sha256(body.encode("utf-8")).hexdigest())
        object.__setattr__(self, "source_ids", tuple(paths))
        object.__setattr__(self, "_paths", tuple(paths.items()))


def analyze_conversion_fact(fact):
    return _LegacyInvestmentAnalysis(fact) if is_legacy_investment(fact) else FactAnalysis(fact)


def conversion_issue_source_ids(issue):
    result = set(issue["evidence_ids"])
    known = issue.get("known_fact")
    if known is not None:
        if is_legacy_investment(known):
            definition = "HistoricalPartialFactInvestmentTerms"
            validate_contract("business-facts.schema.json", definition, known)
            result.update(_validated_paths(known, definition))
        else:
            result.update(partial_fact_source_ids(known))
    return tuple(sorted(result))


def validate_conversion_envelope(definition, value):
    """Validate unchanged non-investment fields and strictly proven old facts."""
    projected = deepcopy(value)
    if not isinstance(projected, dict):
        return validate_contract("core.schema.json", definition, value)
    if definition == "StandardPart":
        facts = projected.get("facts", ())
        for index, fact in enumerate(facts if isinstance(facts, list) else ()):
            if is_legacy_investment(fact):
                projected["facts"][index] = converted_investment_fact(fact)
        issues = projected.get("issues", ())
    elif definition == "IssueScope":
        issues = projected.get("issues", ())
    elif definition == "CurrentIssue":
        issues = (projected,)
    else:
        raise ValueError("Unsupported offline envelope")
    for issue in issues if isinstance(issues, (list, tuple)) else ():
        if not isinstance(issue, dict):
            continue
        known = issue.get("known_fact")
        if known is not None and is_legacy_investment(known):
            issue["known_fact"] = converted_investment_fact(known, partial=True)
    validate_contract("core.schema.json", definition, projected)
    return value
