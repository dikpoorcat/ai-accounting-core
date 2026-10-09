"""Named dashboard objects use one full-range pinyin order before paging."""

from entity_fixture import save_entity_display_profile
from test_banking import book as _bank_book
from test_banking import funding
from test_investments import book as _investment_book
from test_investments import subscription

from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_funds import FundsRead, funds

bank_book, investment_book = _bank_book, _investment_book


def named(engine, kind, ident, name):
    save_entity_display_profile(
        engine,
        {
            "kind": kind,
            "entity_id": ident,
            "display_name": name,
            "display_number": ident,
            "source": "synthetic explicit object names",
        },
        expected_revision=0,
        request_id=f"name:{kind}:{ident}",
    )


def paged_objects(read, period, section, expected_names):
    first = read(period, section=section, limit=2)
    collection = first["data"]["collections"][section]
    assert collection["page"]["total_count"] == 3
    assert collection["page"]["filtered_count"] == 3
    assert collection["page"]["returned_count"] == 2
    assert collection["page"]["has_more"] is True
    assert [item["name"] for item in collection["items"]] == expected_names[:2]

    second = read(
        period,
        section=section,
        limit=2,
        cursor=collection["page"]["next_cursor"],
        expected_version=first["snapshot_version"],
    )
    tail = second["data"]["collections"][section]
    assert tail["page"]["returned_count"] == 1
    assert tail["page"]["has_more"] is False
    assert tail["page"]["next_cursor"] is None
    items = collection["items"] + tail["items"]
    assert [item["name"] for item in items] == expected_names
    return first["data"], items


def test_fund_accounts_page_by_displayed_pinyin_name_before_internal_id(
    bank_book, monkeypatch,
):
    engine, save, publish, _ = bank_book
    for index, (ident, name) in enumerate(
        (("bank-a", "张三账户"), ("bank-b", "王五账户"), ("bank-c", "李四账户")),
        start=1,
    ):
        funding(save, publish, subject=f"capital-{ident}", bank=ident, amount=index * 100)
        named(engine, "fund_account", ident, name)

    dashboard = Dashboard(engine)
    data, accounts = paged_objects(
        dashboard.funds,
        "2026-09",
        "accounts",
        ["李四账户", "王五账户", "张三账户"],
    )
    assert [item["account_id"] for item in accounts] == ["bank-c", "bank-b", "bank-a"]
    assert data["total_fen"] == sum(item["closing_fen"] for item in accounts) == 600

    def unexpected_profile(*_args, **_kwargs):
        raise AssertionError("amount-only funds summaries must not read object names")

    monkeypatch.setattr(FundsRead, "profile", unexpected_profile)
    with dashboard._snapshot("2026-09") as snapshot:
        summary = funds(snapshot, summary_only=True)
    assert summary["total_fen"] == summary["bank_fen"] == 600


def test_money_fund_products_page_by_displayed_pinyin_name_before_internal_id(
    investment_book,
):
    engine, save, publish = investment_book
    for ident, name in (
        ("fund-a", "张三基金"), ("fund-b", "王五基金"), ("fund-c", "李四基金"),
    ):
        save("money_fund_subscription", f"buy-{ident}", subscription(fund_id=ident))
        publish(f"buy-{ident}")
        named(engine, "fund_product", ident, name)

    data, products = paged_objects(
        Dashboard(engine).funds,
        "2026-01",
        "investment_products",
        ["李四基金", "王五基金", "张三基金"],
    )
    assert [item["fund_id"] for item in products] == ["fund-c", "fund-b", "fund-a"]
    assert data["investments"]["closing_cost_fen"] == sum(
        item["closing_cost_fen"] for item in products
    ) == 30300


def test_assets_page_by_displayed_pinyin_name_before_asset_id_and_code(bank_book):
    engine, save, publish, _ = bank_book
    for ident, name in (
        ("asset-a", "张三设备"), ("asset-b", "王五设备"), ("asset-c", "李四设备"),
    ):
        save(
            "asset",
            f"purchase-{ident}",
            {
                "period": "2026-09",
                "asset_id": ident,
                "asset_type": "fixed",
                "supplier_id": "supplier",
                "acquisition_date": "2026-09-01",
                "cost_fen": 1000,
                "acquisition_basis": "direct_purchase",
            },
        )
        publish(f"purchase-{ident}")
        named(engine, "asset", ident, name)

    data, assets = paged_objects(
        Dashboard(engine).assets,
        "2026-09",
        "assets",
        ["李四设备", "王五设备", "张三设备"],
    )
    assert [item["asset_id"] for item in assets] == ["asset-c", "asset-b", "asset-a"]
    assert [item["code"] for item in assets] == ["asset-c", "asset-b", "asset-a"]
    assert data["registered_count"] == 3
    assert data["pending_fixed_cost_fen"] == sum(item["cost_fen"] for item in assets) == 3000
