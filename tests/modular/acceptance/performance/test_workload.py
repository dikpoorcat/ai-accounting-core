"""Qualify the exact scale plan and a reduced real probe, without official timing."""

import pytest

from ai_accounting_modular.contracts.public import validate_contract

from .._support import COMPANY, ready
from .dataset import build, count_businesses, open_dataset
from .projections import PAGES, checked, collection, parameters
from .workload import BUSINESSES_PER_MONTH, CONTRACTORS, EMPLOYEES, PEOPLE, SCALES, plan_month


@pytest.mark.parametrize("index", (0, 1, 11, 47, 119))
def test_scale_plan_counts_real_businesses_and_respects_apply_contract(index):
    plan = plan_month(index)
    assert SCALES == (12, 48, 120) and EMPLOYEES == 50 and CONTRACTORS == 5 and PEOPLE == 55
    assert sum(plan.counts.values()) == BUSINESSES_PER_MONTH == 1000
    assert plan.counts["people.remuneration"] == 55
    assert plan.counts["assets.acquisition"] == 20
    assert plan.counts["investment.money_fund_purchase"] == 5
    assert plan.counts["money.reconciliation"] == len(plan.reconciliations) == 2
    assert len(plan.requests) == 4
    for request in plan.requests:
        validate_contract("core.schema.json", "ApplyRequest", request)


def test_real_mixed_probe_has_50_employees_5_contractors_and_complete_business_mix(tmp_path):
    root = tmp_path / "probe"
    manifest = build(root, 1, probe_business_count=240)
    assert manifest["official_scale"] is False
    with open_dataset(root, "aggregate_join") as (kernel, _):
        assert count_businesses(kernel, "2026-01") == 240
        observed = {}
        for page in PAGES:
            query = parameters(COMPANY, "2026-01", page)
            ready(kernel, page, query)
            observed[page] = checked(kernel, page, query)
        employee = observed["employees"]["data"]["collections"]
        assert employee["employees"]["page"]["total_count"] == 50
        assert employee["labor_workers"]["page"]["total_count"] == 5
        assert observed["assets"]["data"]["collections"]["assets"]["page"]["total_count"] == 20
        project_sources = observed["assets"]["data"]["collections"]["projects"]
        # This existing page lists cost sources, not one row per project: the
        # first project has an external cost and a separate capitalized labor.
        assert project_sources["page"]["total_count"] == 6
        assert len({item["project_id"] for item in project_sources["items"]}) == 5
        assert sum(item["kind"] == "people.remuneration" for item in project_sources["items"]) == 1
        funds = observed["funds"]
        bank_batches = [
            item["batch_payment"]
            for item in funds["data"]["collections"]["statements"]["items"]
            if item.get("batch_payment")
        ]
        assert sorted(len(item["items"]) for item in bank_batches) == [5, 5, 20, 20, 49]
        assert observed["employees"]["data"]["outstanding_remuneration_fen"] == "100000"
        assert funds["data"]["collections"]["accounts"]["page"]["total_count"] == 2
        assert funds["data"]["collections"]["investment_products"]["page"]["total_count"] == 1
        query = parameters(COMPANY, "2026-01", "employees") | {
            "section": "employees",
            "expected_version": observed["employees"]["snapshot_version"],
        }
        assert len(collection(kernel, "employees", query, "employees")["items"]) == 50
        assert kernel.reading.run_pending(COMPANY) == ()
