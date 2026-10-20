"""Independent cross-owner accounting oracles, with actual application transactions."""

from copy import deepcopy
from json import loads
from uuid import uuid4

from ai_accounting_modular.application.public import create_application
from ai_accounting_modular.application.public import repository_factory as application_repository
from ai_accounting_modular.domains.assets.public import create_assets
from ai_accounting_modular.domains.assets.public import repository_factory as assets_repository
from ai_accounting_modular.domains.financing.public import create_financing, create_investment
from ai_accounting_modular.domains.financing.public import (
    repository_factory as financing_repository,
)
from ai_accounting_modular.domains.operations.public import create_operations
from ai_accounting_modular.domains.operations.public import (
    repository_factory as operations_repository,
)
from ai_accounting_modular.domains.taxes.public import create_taxes
from ai_accounting_modular.domains.taxes.public import repository_factory as taxes_repository
from ai_accounting_modular.identity.public import create_identity
from ai_accounting_modular.identity.public import repository_factory as identity_repository
from ai_accounting_modular.ledger.opening.public import create_opening
from ai_accounting_modular.ledger.opening.public import repository_factory as opening_repository
from ai_accounting_modular.ledger.public import create_ledger
from ai_accounting_modular.ledger.public import repository_factory as ledger_repository
from ai_accounting_modular.ledger.reporting.public import create_reporting
from ai_accounting_modular.ledger.reporting.public import repository_factory as reporting_repository
from ai_accounting_modular.money.public import create_money
from ai_accounting_modular.money.public import repository_factory as money_repository
from ai_accounting_modular.records.public import create_records
from ai_accounting_modular.records.public import repository_factory as records_repository
from ai_accounting_modular.storage.public import CompanySpec, OwnerProfile, Storage, create_catalog
from tests.modular.ledger.opening._support import (
    BANK,
    FIXED,
    PARTY,
    fact,
    get,
    loan_opening,
    member,
    opening,
    replace,
    request,
    result,
)
from tests.modular.ledger.reporting._support import prior_rows


def _assemble(tmp_path, start="2026-07"):
    catalog = tmp_path / "catalog.sqlite3"
    create_catalog(catalog, OwnerProfile(str(uuid4()), "合成负责人", "synthetic-hash"))
    storage = Storage(
        catalog,
        repositories=[
            application_repository(),
            identity_repository(),
            ledger_repository(),
            opening_repository(),
            reporting_repository(),
            records_repository(),
            assets_repository(),
            financing_repository(),
            operations_repository(),
            taxes_repository(),
            money_repository(),
        ],
    )
    company = str(uuid4())
    storage.create_company(
        CompanySpec(
            company,
            str(uuid4()),
            "913100000000000001",
            "合成跨模块公司",
            start,
            tmp_path / "company",
        )
    )
    identity, ledger, assets = create_identity(), create_ledger(), create_assets()
    money = create_money(identity=identity, ledger=ledger)
    app = create_application(
        storage=storage,
        identity=identity,
        ledger=ledger,
        records=create_records(root_for=lambda read: storage.company_metadata(read).company_root),
        domains=[
            create_opening(asset_accounting=assets),
            create_reporting(),
            assets,
            create_financing(),
            create_investment(),
            create_operations(),
            create_taxes(),
            money,
        ],
        money=money,
    )
    return app, company


def _opening_with_asset(*, start="2026-07", in_use="2026-05", life=3, cost=10001, used=1):
    depreciable = cost - 1
    accumulated = depreciable // life * used
    return opening(
        member({"type": "bank", "funds_account_id": BANK, "balance_fen": "50000"}),
        member(
            {
                "type": "asset",
                "asset_id": FIXED,
                "asset_type": "fixed",
                "cost_fen": str(cost),
                "accumulated_fen": str(accumulated),
                "completed_months": used,
                "in_use_period": in_use,
                "useful_life_months": life,
                "residual_fen": "1",
                "benefit_area": "administration",
                "rounding": "floor_final_remainder",
            }
        ),
        member(
            {
                "type": "equity",
                "equity_kind": "retained_earnings",
                "holder_or_basis_id": PARTY,
                "balance_fen": str(50000 + cost - accumulated),
            }
        ),
        period=start,
    )


def _continuation(initial, *, year_profit=30000, quarter_profit=10000):
    year, quarter = "year_to_date_fen", "quarter_to_date_fen"
    balance = {(line, "beginning_fen"): 50000 for line in (1, 15, 30, 51, 52, 53)}
    profit = {(line, year): year_profit for line in (1, 21, 30, 32)}
    profit.update({(line, quarter): quarter_profit for line in (1, 21, 30, 32)})
    cash = {(line, column): 50000 for line in (21, 22) for column in (year, quarter)}
    return fact(
        "reports.continuation",
        {
            "accounting_standard": "small_enterprise",
            "bookkeeping_start": initial["period"],
            "opening_package_id": initial["business_id"],
            "year_beginning_balance": prior_rows(53, ("beginning_fen",), balance),
            "prior_profit": prior_rows(32, (quarter, year), profit),
            "prior_cash": prior_rows(22, (quarter, year), cash),
        },
        period=initial["period"],
    )


def _slots(app, company, item):
    return {
        row["reference"]["result_slot"]: row
        for row in get(app, company, item["business_id"])["businesses"][0]["results"]
    }


def _pretax(app, company, start, end):
    with app.storage.read_unit(company) as read:
        return app.ledger.pre_tax(start, end, read)


def test_opening_asset_spans_year_end_and_disposal_uses_only_the_new_year_depreciation(tmp_path):
    app, company = _assemble(tmp_path, "2026-12")
    initial = _opening_with_asset(start="2026-12", in_use="2026-11", life=3, cost=12001, used=0)
    prior = _continuation(initial)
    sold = fact(
        "assets.disposal",
        {"type": "scrap", "asset_id": FIXED},
        period="2027-01",
        actual="2027-01-31",
    )
    saved = app.accounting_apply(request(company, sold, prior, initial))
    assert saved["completion"] == "complete", saved
    slots = _slots(app, company, initial)
    assert set(slots) == {"main", "depreciation:2026-12", "depreciation:2027-01"}
    assert slots["main"]["vouchers"] == []
    assert slots["depreciation:2026-12"]["target"]["values"]["total_amount_fen"] == "4000"
    assert slots["depreciation:2027-01"]["target"]["values"]["total_amount_fen"] == "4000"
    assert result(app, company, sold)["target"]["values"]["gain_loss_fen"] == "-4001"
    assert _pretax(app, company, "2026-01", "2026-12").fields["profit_before_tax"] == {
        "state": "known",
        "amount_fen": "26000",
    }
    assert _pretax(app, company, "2027-01", "2027-12").fields["profit_before_tax"] == {
        "state": "known",
        "amount_fen": "-8001",
    }
    with app.storage.read_unit(company) as read:
        assert app.ledger.fund_movements(BANK, "2026-01", "2027-12", read) == ()
        assert (
            create_assets().query_current(
                {
                    "company_id": company,
                    "family": "assets",
                    "kind": "assets.activation",
                    "period_from": "2026-01",
                    "period_to": "2027-12",
                    "limit": 100,
                },
                read,
            )
            == ()
        )


def test_replacing_the_whole_opening_source_rebinds_asset_and_explicit_continuation_atomically(
    tmp_path,
):
    app, company = _assemble(tmp_path)
    initial = _opening_with_asset()
    prior = _continuation(initial, quarter_profit=0)
    disposed = fact("assets.disposal", {"type": "scrap", "asset_id": FIXED}, actual="2026-07-31")
    saved = app.accounting_apply(request(company, prior, disposed, initial))
    assert saved["completion"] == "complete", saved
    before = result(app, company, disposed)
    successor = deepcopy(initial)
    successor["business_id"] = str(uuid4())
    prior["data"]["opening_package_id"] = successor["business_id"]
    submitted = replace(company, prior)
    submitted["mutations"].extend(
        [
            {"op": "withdraw", "expected_seq": 1, "business_id": initial["business_id"]},
            {"op": "create", "expected_seq": 0, "fact": successor},
        ]
    )
    saved = app.accounting_apply(submitted)
    assert saved["completion"] == "complete", saved
    after = result(app, company, disposed)
    assert after["target"] == before["target"]
    assert after["reference"]["voucher_number"] == before["reference"]["voucher_number"]
    assert {source["business_id"] for source in after["basis"]["source_results"]} == {
        successor["business_id"]
    }
    withdrawn = get(app, company, initial["business_id"])["businesses"][0]
    assert withdrawn["results"] == [] and "fact" not in withdrawn
    with app.storage.read_unit(company) as read:
        assert create_opening().export_current([initial["business_id"]], read) == ()
        assert create_opening().export_current([successor["business_id"]], read) == (successor,)
        assert create_reporting().export_current([prior["business_id"]], read) == (prior,)
        root = app.storage.company_metadata(read).company_root
        with app.records.lease_document_set(read) as manifest:
            final_facts = {
                row["business_id"]: row
                for shard in manifest["shards"]
                if shard["key"].startswith("facts:")
                for row in loads((root / shard["path"]).read_bytes())["facts"]
            }
        assert initial["business_id"] not in final_facts
        assert final_facts[successor["business_id"]] == successor
        assert final_facts[prior["business_id"]] == prior


def test_changed_card_recalculates_depreciation_and_preserves_explicit_prior_profit(tmp_path):
    app, company = _assemble(tmp_path)
    initial = _opening_with_asset(life=5, cost=10001)
    prior = _continuation(initial, quarter_profit=0)
    later = fact(
        "operations.expense",
        {
            "creditor_id": PARTY,
            "creditor_kind": "supplier",
            "amount_fen": "1000",
            "expense_class": "administration",
        },
        period="2026-09",
    )
    saved = app.accounting_apply(request(company, later, initial, prior))
    assert saved["completion"] == "complete", saved
    assert (
        _pretax(app, company, "2026-01", "2026-09").fields["profit_before_tax"]["amount_fen"]
        == "23000"
    )
    prior_result = result(app, company, prior)
    asset = initial["data"]["members"][1]["data"]
    asset.update(cost_fen="15001", accumulated_fen="3000")
    initial["data"]["members"][-1]["data"]["balance_fen"] = "62001"
    saved = app.accounting_apply(replace(company, initial))
    assert saved["completion"] == "complete", saved
    assert (
        _pretax(app, company, "2026-01", "2026-09").fields["profit_before_tax"]["amount_fen"]
        == "20000"
    )
    assert result(app, company, prior)["basis"]["input"] == prior_result["basis"]["input"]
    assert {
        slot["target"]["values"]["total_amount_fen"]
        for key, slot in _slots(app, company, initial).items()
        if key != "main"
    } == {"3000"}


def test_historical_contract_can_feed_current_opening_but_historical_expense_cannot_create_books(
    tmp_path,
):
    app, company = _assemble(tmp_path)
    contract, initial, _ = loan_opening(contract_period="2025-12")
    old_expense = fact(
        "operations.expense",
        {
            "creditor_id": PARTY,
            "creditor_kind": "supplier",
            "amount_fen": "100",
            "expense_class": "administration",
        },
        period="2026-06",
    )
    refused = app.accounting_apply(request(company, contract, old_expense, initial))
    assert refused.get("completion") == "not_saved", refused
    with app.storage.read_unit(company) as read:
        assert read.head == 0
    saved = app.accounting_apply(request(company, contract, initial))
    assert saved["completion"] == "complete", saved
    assert result(app, company, contract)["vouchers"] == []
    assert result(app, company, initial)["vouchers"] == []
