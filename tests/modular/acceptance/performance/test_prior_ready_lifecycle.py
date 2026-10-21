"""Real 240-business months check the lifecycle boundary, never a timing budget."""

from contextlib import contextmanager

import pytest

from ai_accounting_modular.contracts.public import loads_json
from ai_accounting_modular.reading import _financial_projection, _models

from .._support import COMPANY, http
from . import lifecycle, portable_cases
from .dataset import build, count_businesses, open_dataset
from .ledger_variant import MODES
from .projections import parameters
from .workload import plan_month


@pytest.fixture(scope="module")
def prior_package(tmp_path_factory):
    root = tmp_path_factory.mktemp("prior-ready-lifecycle")
    source = root / "source"
    manifest = build(source, 2, probe_business_count=240, pending_last_month=True)
    backup = portable_cases.package(source, root / "prior.zip", "original")
    return backup, manifest


def declared_cost(planned):
    """Only the fixture's explicitly declared expense and expense-destination payroll."""
    total = 0
    for request in planned.requests:
        for mutation in request["mutations"]:
            fact = mutation["fact"]
            data = fact["data"]
            if fact["kind"] == "operations.expense":
                total += int(data["amount_fen"])
            elif fact["kind"] == "people.remuneration" and (
                data["type"] == "wage" or data["destination"]["type"] == "expense"
            ):
                total += int(data["gross_fen"])
    return total


def check_asset_policy(planned):
    facts = [mutation["fact"] for request in planned.requests for mutation in request["mutations"]]
    assets = {
        fact["data"]["asset"]["asset_id"]: fact["data"]["asset"]
        for fact in facts
        if fact["kind"] == "assets.acquisition"
    }
    cards = [
        item
        for fact in facts
        if fact["kind"] == "assets.activation"
        for item in fact["data"]["items"]
    ]
    assert len(assets) == len(cards) == 20
    assert sum(asset["asset_type"] == "fixed" for asset in assets.values()) == 10
    assert sum(asset["asset_type"] == "intangible" for asset in assets.values()) == 10
    assert all(asset["cost_fen"] == "10001" for asset in assets.values())
    assert {card["asset_id"] for card in cards} == assets.keys()
    for card in cards:
        assert card["useful_life_months"] == 60
        assert card["benefit_area"] == "administration"
        assert card["rounding"] == "floor_final_remainder"
        assert card["residual_fen"] == (
            "1" if assets[card["asset_id"]]["asset_type"] == "fixed" else "0"
        )


class SubmissionMoment:
    """Observe successful real commits; this does not replace lifecycle's timer."""

    def __init__(self):
        self.commits = 0
        self.final_requested = False
        self.final_committed = False

    def observe(self, write_unit):
        @contextmanager
        def observed(company):
            with write_unit(company) as unit:
                yield unit
            self.commits += 1
            if self.final_requested:
                self.final_committed = True

        return observed


def quarter_state(kernel):
    scope = _models.normalize(
        {
            "company_id": COMPANY,
            "period_from": "2026-01",
            "period_to": "2026-03",
            "view": "as_posted",
        }
    )
    with kernel.storage.read_unit(COMPANY) as read:
        repository = read.repository("reading")
        selected = kernel.ledger.financial_projection_request("snapshot", scope, read)
        target = _financial_projection.store(read)
        return (
            repository.state(_models.key(scope), "financial"),
            repository.payload(_models.key(scope), "financial"),
            target.get_blocks((selected.descriptor_key,)),
            target.header_json,
        )


@pytest.mark.parametrize("mode", MODES)
def test_prior_quarter_waits_for_final_real_commit_before_month_publication(
    prior_package, tmp_path, monkeypatch, mode
):
    backup, manifest = prior_package
    baseline = portable_cases.prepare_baseline(
        backup, tmp_path / "history", manifest, mode, prior_ready=True
    )
    sample = tmp_path / "sample"
    sample.mkdir()
    portable_cases.clone_baseline(baseline, sample, manifest, mode)
    prior = plan_month(0, business_count=240)
    planned = plan_month(1, business_count=240, opening_bank=manifest["ending_bank_fen"])
    assert manifest["period_to"] == prior.period == "2026-01"
    assert manifest["pending_period"] == planned.period == "2026-02"
    assert sum(len(value["mutations"]) for value in planned.requests) == 238
    assert declared_cost(prior) == 5_113_000 and declared_cost(planned) == 5_219_000
    check_asset_policy(prior)
    check_asset_policy(planned)
    # The declared 60-month policies charge 166 fen per eligible asset:
    # January's ten intangibles; February's prior twenty and new ten intangibles.
    prior_cost, current_cost = declared_cost(prior) + 1_660, declared_cost(planned) + 4_980
    expected_profit = "-10338640"
    assert str(-prior_cost - current_cost) == expected_profit

    with open_dataset(sample, mode, clock_period=planned.period) as (kernel, _):
        pointer = sample / "company/standard/current.json"
        original_pointer = pointer.read_bytes()
        original = quarter_state(kernel)
        assert original[0]["state"] == "ready" and original[2][0] is not None
        assert loads_json(original[1]["payload_json"])["snapshot"]["profit_statement"]["32"][
            "current"
        ]["amount_fen"] == str(-prior_cost)
        moment, events = SubmissionMoment(), []
        monkeypatch.setattr(kernel.storage, "write_unit", moment.observe(kernel.storage.write_unit))

        def observe(owner, method):
            actual = getattr(owner, method)

            def checked(*args, **kwargs):
                events.append((method, moment.final_committed))
                assert moment.final_committed, f"{method} ran before the final real commit"
                return actual(*args, **kwargs)

            monkeypatch.setattr(owner, method, checked)

        for owner, method in (
            (kernel.ledger, "prepare_financial_projection"),
            (kernel.ledger, "read_financial_projection"),
            (kernel.reading, "run_pending"),
            (kernel.records, "prepare_documents"),
            (kernel.records, "publish_documents"),
        ):
            observe(owner, method)

        reconcile = lifecycle.reconciliation_request
        boundaries = []

        def after_prefix(actual_kernel, actual_planned):
            assert actual_kernel is kernel and actual_planned is planned
            assert moment.commits == len(planned.requests) and not moment.final_committed
            assert count_businesses(kernel, planned.period) == 238
            assert events == [] and pointer.read_bytes() == original_pointer
            current = quarter_state(kernel)
            assert current[0]["state"] == "preparing"
            assert current[0]["wanted_head"] > current[0]["ready_head"]
            assert current[1:] == original[1:]
            with kernel.storage.read_unit(COMPANY) as read:
                target = _financial_projection.store(read)
                assert target.state()["state"] == "preparing" and target.work()["pending"]
                assert kernel.records.document_status(read)["state"] == "pending"
                scopes = [
                    loads_json(value["scope_key"]) for value in read.repository("reading").scopes()
                ]
                assert all(
                    value.get("page") != "monthly" or value["scope"]["period_to"] != planned.period
                    for value in scopes
                )
            response = http(
                kernel, "quarterly-report", parameters(COMPANY, prior.period, "quarterly-report")
            )
            assert response["http_status"] == 200 and response["body"]["state"] == "preparing"
            assert "response" not in response["body"]
            final = reconcile(kernel, planned)
            assert events == [] and len(final["mutations"]) == 2
            boundaries.append(True)
            moment.final_requested = True
            return final

        monkeypatch.setattr(lifecycle, "reconciliation_request", after_prefix)
        result = lifecycle.whole_month(kernel, planned)
        assert boundaries == [True] and moment.final_committed
        assert moment.commits == len(planned.requests) + 1
        assert events and all(committed for _, committed in events)
        assert {name for name, _ in events} == {
            "prepare_financial_projection",
            "read_financial_projection",
            "run_pending",
            "prepare_documents",
            "publish_documents",
        }
        assert result["saved"]["completion"] == "complete"
        assert result["evidence"]["archived_business_count"] == 240
        assert result["evidence"]["all_current_month_facts_verified"] is True
        assert (
            result["pages"]["quarterly-report"]["summary"]["current_net_profit_fen"]
            == expected_profit
        )
        assert pointer.read_bytes() != original_pointer
        current = quarter_state(kernel)
        assert (
            current[0]["state"] == "ready" and current[0]["ready_head"] == current[0]["wanted_head"]
        )
        assert (
            current[1]["payload_json"] != original[1]["payload_json"] and current[2] != original[2]
        )
    assert portable_cases._snapshot(baseline.company_root) == baseline.files
