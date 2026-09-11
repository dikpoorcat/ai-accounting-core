"""Public JSON tax policies preserve exact decimals and fail batches atomically."""

from copy import deepcopy
from decimal import Decimal
from typing import ClassVar

import pytest

from ai_accounting.kernel.command_schema import command_models, validate_command
from ai_accounting.kernel.contracts import Fact, KernelError, Registry
from ai_accounting.kernel.domains.taxes import (
    SurtaxPolicyFact,
    UsedAssetVatPolicyFact,
    VatPolicyFact,
)
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.types import digest


class OrdinarySource(Fact):
    kind: ClassVar[str] = "ordinary_source"
    description: str


POLICIES = {
    "vat_policy": {
        "rate_percent": "1.00",
        "threshold_fen": 30_000_000,
        "threshold_operator": "strictly_below",
    },
    "surtax_policy": {
        "urban_rate_percent": "7.00",
        "education_rate_percent": "3",
        "local_education_rate_percent": "2",
        "payable_fraction": "0.50",
    },
    "used_asset_vat_policy": {
        "tax_base_rate_percent": "3.00",
        "payable_rate_percent": "2.00",
    },
}


@pytest.fixture(scope="module")
def registry_and_commands():
    registry = Registry()
    for model in (OrdinarySource, VatPolicyFact, SurtaxPolicyFact, UsedAssetVatPolicyFact):
        registry.register(model)
    return registry, command_models(registry)


@pytest.fixture
def company(tmp_path, registry_and_commands):
    registry, models = registry_and_commands
    engine = Engine(Store.create(tmp_path / "company.sqlite", registry, "company", "tax", "db"))
    evidence = engine.register_evidence(
        b"Synthetic explicitly selected policy", "text/plain", "policy", request_id="evidence"
    )["digest"]
    return engine, models, evidence


def policy_record(kind, evidence):
    return {
        "kind": kind,
        "subject_id": kind,
        "data": {
            "period": "2026-01",
            "policy": {
                "version": "synthetic-policy-v1",
                "effective_from": "2026-01-01",
                "effective_to": "2027-12-31",
                "source_url": "https://fgk.chinatax.gov.cn/synthetic-policy",
                **POLICIES[kind],
            },
        },
        "evidence": [evidence],
        "expected_revision": 0,
    }


def test_public_save_variants_preserve_policy_wire_and_stored_fact_hash(company):
    engine, models, evidence = company
    records = [policy_record(kind, evidence) for kind in POLICIES]
    for record in records:
        single = validate_command(
            models,
            "save_fact",
            {"company_id": "company", "request_id": "single", **record},
        )
        assert single["data"] == record["data"]
    wire = validate_command(
        models,
        "save_facts",
        {"company_id": "company", "request_id": "batch", "facts": records},
    )
    assert wire["facts"] == records
    result = engine.save_facts(wire["facts"], request_id=wire["request_id"])
    assert len(result["results"]) == 3
    with engine.store.connection(read_only=True) as connection:
        for record in records:
            current = engine.store.current_fact(connection, record["subject_id"])
            # Legacy policy facts already serialize Decimal as the exact input
            # strings, including trailing zeroes. Strict wire acceptance must
            # neither rewrite that canonical representation nor change its hash.
            assert current.fact.model_dump(mode="json") == record["data"]
            persisted = connection.execute(
                "SELECT digest FROM fact_revision WHERE id=?", (current.id,)
            ).fetchone()[0]
            assert persisted == digest(record["data"])
            assert all(
                isinstance(getattr(current.fact.policy, field), Decimal)
                for field in POLICIES[record["kind"]]
                if field.endswith("percent") or field == "payable_fraction"
            )


@pytest.mark.parametrize(
    "kind,field",
    [
        ("vat_policy", "rate_percent"),
        ("surtax_policy", "urban_rate_percent"),
        ("surtax_policy", "payable_fraction"),
        ("used_asset_vat_policy", "payable_rate_percent"),
    ],
)
@pytest.mark.parametrize("invalid", [0.5, True, "NaN", "Infinity", "-Infinity", "-0.01", "101"])
def test_invalid_rates_reject_public_json_and_leave_no_partial_batch(company, kind, field, invalid):
    engine, models, evidence = company
    first = {
        "kind": "ordinary_source",
        "subject_id": "first-valid-source",
        "data": {"period": "2026-01", "description": "Must not be partially saved"},
        "evidence": [evidence],
        "expected_revision": 0,
    }
    bad = deepcopy(policy_record(kind, evidence))
    bad["data"]["policy"][field] = invalid
    payload = {"company_id": "company", "request_id": "invalid-batch", "facts": [first, bad]}
    with pytest.raises(KernelError):
        validate_command(models, "save_facts", payload)
    # Internal callers retain the same domain validation and atomic failure,
    # even if they did not come through the public command adapter.
    with pytest.raises(KernelError):
        engine.save_facts(payload["facts"], request_id="internal-invalid-batch")
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM fact_revision").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM subject").fetchone()[0] == 0


def test_surtax_fraction_upper_bound_is_one_at_public_boundary(registry_and_commands):
    _, models = registry_and_commands
    record = policy_record("surtax_policy", "0" * 64)
    record["data"]["policy"]["payable_fraction"] = "1.01"
    with pytest.raises(KernelError):
        validate_command(
            models, "save_fact", {"company_id": "company", "request_id": "bad", **record}
        )
