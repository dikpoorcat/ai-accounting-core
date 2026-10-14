"""Actual owner defaults and exact frozen voucher bases retain native behavior."""

import json

import pytest
from close_storage_fixture import stored_manifest
from dashboard_model_fixture import produce
from test_banking import book as book  # noqa: F401
from test_banking import consume_assets
from test_dashboard_model_transactions import raw_read, source_body_work
from test_integrity_content import damage
from test_payroll_corrections import company as company  # noqa: F401
from test_reimbursement_assets import activate_assets, activation
from test_reimbursement_assets import consume_assets as revise_consumption

from ai_accounting.kernel import engine as engine_module
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_model_runtime import stable_payload
from ai_accounting.kernel.maintenance import Maintenance
from ai_accounting.kernel.response_contracts import validate_response


def work_counts(work):
    return {name: len(values) for name, values in work.items()}


def test_assets_active_and_all_profiles_keep_complete_native_pages(book, monkeypatch):
    engine, save, publish, proof = book
    identities = [f"asset-{index:02}" for index in range(24)]
    for ident in identities:
        save(
            "asset",
            ident,
            {
                "period": "2026-09",
                "asset_id": ident,
                "asset_type": "fixed",
                "acquisition_date": "2026-09-01",
                "supplier_id": "supplier",
                "cost_fen": 120000,
                "acquisition_basis": "direct_purchase",
            },
        )
    publish(*identities)
    activate_assets(
        engine,
        "activate-assets",
        "2026-09",
        [
            (
                "activate-" + ident,
                activation(
                    period="2026-09",
                    asset_id=ident,
                    in_use_date="2026-09-02",
                ),
            )
            for ident in identities[:-1]
        ],
        (proof,),
    )
    consume_assets(engine, proof, "2026-10")
    save(
        "asset_disposal",
        "dispose",
        {
            "period": "2026-10",
            "asset_id": identities[0],
            "disposal_date": "2026-10-31",
            "disposal_kind": "scrap",
            "gross_proceeds_fen": 0,
        },
    )
    revise_consumption(
        engine, "2026-10", (proof,), expected_revision=1, request_id="consume-disposal"
    )
    Maintenance(engine).rebuild_dashboard_read_model("2026-10", request_id="asset-model")
    produce(engine, "2026-10")
    dashboard = Dashboard(engine)
    from ai_accounting.kernel import dashboard_model_reads

    consumed = []
    original_verify = dashboard_model_reads._verify_hits

    def verify(*args, **kwargs):
        consumed.append({source[0] for source in args[1]})
        return original_verify(*args, **kwargs)

    monkeypatch.setattr(dashboard_model_reads, "_verify_hits", verify)
    complete = {}
    for selected in ("active", "all"):
        options = {"asset_filter": selected}
        with raw_read():
            expected = dashboard.assets("2026-10", **options)
        consumed.clear()
        with source_body_work(monkeypatch) as work:
            actual = dashboard.assets("2026-10", **options)
        # Current nondefault asset profiles remain independently checked native reads.
        assert stable_payload(actual) == stable_payload(expected)
        validate_response("dashboard_assets", actual)
        print(json.dumps({"filter": selected, "source_work": work_counts(work)}))
        first = actual["data"]["collections"]["assets"]
        assert len(first["items"]) == 20
        assert first["page"]["has_more"]
        ids = {item["asset_id"] for item in first["items"]}
        continuation = {
            **options,
            "section": "assets",
            "cursor": first["page"]["next_cursor"],
            "expected_version": actual["snapshot_version"],
        }
        with raw_read():
            native_first = dashboard.assets("2026-10", **options)
            expected_tail = dashboard.assets(
                "2026-10",
                **(
                    continuation
                    | {
                        "cursor": native_first["data"]["collections"]["assets"]["page"][
                            "next_cursor"
                        ],
                        "expected_version": native_first["snapshot_version"],
                    }
                ),
            )
        consumed.clear()
        tail = dashboard.assets("2026-10", **continuation)
        assert stable_payload(tail) == stable_payload(expected_tail)
        validate_response("dashboard_assets", tail)
        ids.update(item["asset_id"] for item in tail["data"]["collections"]["assets"]["items"])
        assert not tail["data"]["collections"]["assets"]["page"]["has_more"]
        complete[selected] = ids
    assert complete["all"] == set(identities)
    assert complete["active"] == set(identities[1:-1])
    for options, ident, subjects in (
        ({"asset_filter": "pending"}, identities[-1], {identities[-1]}),
        (
            {"asset_id": identities[0]},
            identities[0],
            {identities[0], "activate-" + identities[0], "dispose"},
        ),
    ):
        with engine.store.connection(read_only=True) as connection:
            expected_sources = {
                row["id"]: row["fact_id"]
                for row in connection.execute(
                    "SELECT id,fact_id FROM calculation WHERE subject_id IN "
                    "(SELECT value FROM json_each(?)) "
                    "AND kind IN ('asset','asset_activation','asset_disposal')",
                    (json.dumps(sorted(subjects)),),
                )
            }
        with raw_read():
            expected = dashboard.assets("2026-10", **options)
        consumed.clear()
        with source_body_work(monkeypatch) as work:
            actual = dashboard.assets("2026-10", **options)
        assert stable_payload(actual) == stable_payload(expected)
        validate_response("dashboard_assets", actual)
        assert set().union(*consumed) <= expected_sources.keys()
        collection = actual["data"]["collections"]["assets"]
        assert [item["asset_id"] for item in collection["items"]] == [ident]
        assert collection["page"]["total_count"] == len(identities)
        assert collection["page"]["filtered_count"] == 1
        assert not collection["page"]["has_more"]
        print(json.dumps({"filter": options, "source_work": work_counts(work)}))


@pytest.mark.parametrize("timing", ["before", "after", "both"])
def test_reviewed_voucher_binds_frozen_basis_and_preserves_later_review(
    company,
    monkeypatch,
    timing,
):
    company.publish("january")
    engine = company.engine

    def review(label):
        with monkeypatch.context() as scope:
            scope.setattr(engine_module, "PROGRAM_VERSION", "synthetic-coverage-" + label)
            _, published = company.publish("january")
        assert published["january"]["impact"] == "review_no_impact"
        return published["january"]["calculation_id"]

    if timing in {"before", "both"}:
        review("before")
    company.close("2026-01")
    if timing in {"after", "both"}:
        review("after")
    Maintenance(engine).rebuild_dashboard_read_model("2026-01", request_id="review-model")
    produce(engine, "2026-01")
    with engine.store.connection(read_only=True) as connection:
        leaf = stored_manifest(connection, "2026-01")["vouchers"][0]
    if timing in {"before", "both"}:
        assert leaf["calculation_id"] != leaf["adopted_calculation_id"]
    dashboard = Dashboard(engine)
    for options in ({}, {"section": "vouchers", "limit": 1}):
        with raw_read():
            expected = dashboard.brief("2026-01", **options)
        with source_body_work(monkeypatch) as work:
            actual = dashboard.brief("2026-01", **options)
        assert stable_payload(actual) == stable_payload(expected)
        validate_response("dashboard_brief", actual)
        if options.get("section") != "vouchers":
            before_overview = stable_payload(actual)
        else:
            shown = actual["data"]["collections"]["vouchers"]["items"][0]
            assert shown["voucher_version_id"] == leaf["id"]
        print(
            json.dumps(
                {
                    "review_timing": timing,
                    "section": options.get("section"),
                    "source_work": work_counts(work),
                }
            )
        )
    damage(
        engine,
        "calculation",
        "UPDATE calculation SET outcome=json_set(outcome,'$.values.gross_fen',7) WHERE id=?",
        (leaf["adopted_calculation_id"],),
    )
    assert stable_payload(dashboard.brief("2026-01")) == before_overview
    with pytest.raises(KernelError) as error:
        dashboard.brief("2026-01", section="vouchers", limit=1)
    assert error.value.code == "content_integrity_failed"
    with pytest.raises(KernelError) as error:
        Maintenance(engine).verify_integrity()
    assert error.value.code == "content_integrity_failed"
