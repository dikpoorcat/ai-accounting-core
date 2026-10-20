from __future__ import annotations

import hashlib
from copy import deepcopy
from pathlib import Path

import pytest

from ai_accounting_modular.contracts.public import (
    ContractError,
    content_digest,
    loads_json,
    validate_contract,
)

EXAMPLES = Path(__file__).resolve().parents[3] / "docs/redesign/examples"


def business_view(count):
    examples = loads_json((EXAMPLES / "core-acceptance.json").read_bytes())
    fact = next(
        deepcopy(mutation["fact"])
        for request in examples["requests"].values()
        for mutation in request["mutations"]
        if mutation.get("fact", {}).get("kind") == "operations.expense"
    )
    sources = []
    for index in range(count):
        text = f"Synthetic business evidence {index}"
        sources.append(
            {
                "source_id": f"40000000-0000-4000-8000-{index:012x}",
                "kind": "owner_statement",
                "text": text,
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }
        )
    fact["evidence_ids"] = [source["source_id"] for source in sources]
    return {
        "state": {
            "business_id": fact["business_id"],
            "seq": 1,
            "state": "active",
            "fact_digest": content_digest(fact),
            "results": [],
        },
        "fact": fact,
        "results": [],
        "sources": sources,
    }


@pytest.mark.parametrize("count", [501, 1000])
def test_complete_business_sources_fit_the_legal_fact_capacity(count):
    view = business_view(count)
    validate_contract("business-facts.schema.json", "Fact", view["fact"])
    validate_contract("core.schema.json", "BusinessView", view)
    response = {
        "company_id": "10000000-0000-4000-8000-000000000001",
        "database_id": "20000000-0000-4000-8000-000000000001",
        "head": 1,
        "businesses": [view],
        "missing_business_ids": [],
    }
    validated = validate_contract("core.schema.json", "BusinessGetResult", response)
    assert validated["businesses"][0]["sources"] == view["sources"]
    assert len(validated["businesses"][0]["sources"]) == count


def test_fact_and_complete_business_output_both_reject_1001_sources():
    view = business_view(1001)
    with pytest.raises(ContractError):
        validate_contract("business-facts.schema.json", "Fact", view["fact"])
    # Isolate the output limit: an invalid fact must not mask a permissive
    # BusinessView.sources definition.
    del view["fact"]
    with pytest.raises(ContractError, match="/sources"):
        validate_contract("core.schema.json", "BusinessView", view)


def test_source_batches_and_paged_output_keep_their_500_item_limit():
    sources = business_view(501)["sources"]
    response = {
        "company_id": "10000000-0000-4000-8000-000000000001",
        "sources": sources[:500],
        "missing_source_ids": [],
    }
    validate_contract("core.schema.json", "SourcesResult", response)
    response["sources"] = sources
    with pytest.raises(ContractError):
        validate_contract("core.schema.json", "SourcesResult", response)
    request = {
        "contract": "ai-accounting-modular/1",
        "company_id": response["company_id"],
        "command_id": "30000000-0000-4000-8000-000000000001",
        "mutations": [],
        "sources": sources[:500],
    }
    validate_contract("core.schema.json", "ApplyRequest", request)
    request["sources"] = sources
    with pytest.raises(ContractError):
        validate_contract("core.schema.json", "ApplyRequest", request)
