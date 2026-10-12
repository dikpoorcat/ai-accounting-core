"""Fund account pages use the earliest formally adopted bookkeeping month."""

import pytest
import test_banking as banking
from test_opening_continuation import _close_without_current_business

from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.entities import Entities

bank_book = banking.book


def _use_read_path(monkeypatch, raw):
    if raw:
        from ai_accounting.kernel import dashboard_model_reads

        monkeypatch.setattr(dashboard_model_reads, "read_page", lambda *args, **kwargs: None)


def _name(engine, ident, value):
    from entity_fixture import save_entity_display_profile

    save_entity_display_profile(
        engine,
        {
            "kind": "fund_account",
            "entity_id": ident,
            "display_name": value,
            "source": "synthetic explicit account name",
        },
        expected_revision=0,
        request_id="account-name:" + ident,
    )


def _base_opening(engine, proof, period="2026-01"):
    from response_samples import publish_empty_opening_package

    publish_empty_opening_package(engine, proof, period, "base-opening:" + period)


@pytest.mark.parametrize("raw", [False, True], ids=["default-read", "raw"])
def test_earliest_month_precedes_name_and_drives_pagination_and_first_account(
    bank_book, monkeypatch, raw,
):
    engine, save, publish, proof = bank_book
    # A zero opening-package member is still a formally adopted account start.
    from response_samples import adopt_zero_opening_package

    adopt_zero_opening_package(save, publish, "cash-a", "2026-01", "zero-cash-opening")
    # A nonzero February transaction also outranks a later account whose name
    # sorts first by pinyin, while the zero January start survives freezing.
    banking.funding(
        save, publish, subject="early-bank-z", bank="bank-z", day="2026-02-05", amount=300
    )
    _close_without_current_business(engine, "2026-01", proof)
    banking.funding(
        save, publish, subject="later-bank-b", bank="bank-b", day="2026-08-03", amount=500
    )
    banking.funding(
        save, publish, subject="later-bank-c", bank="bank-c", day="2026-08-04", amount=700
    )
    _name(engine, "cash-a", "王五账户")
    _name(engine, "bank-z", "张三账户")
    _name(engine, "bank-b", "阿尔法账户")
    _name(engine, "bank-c", "李四账户")
    _use_read_path(monkeypatch, raw)
    dashboard = Dashboard(engine)

    first = dashboard.funds("2026-08", limit=2)
    collection = first["data"]["collections"]["accounts"]
    assert [item["account_id"] for item in collection["items"]] == ["cash-a", "bank-z"]
    assert collection["page"]["has_more"] is True
    tail = dashboard.funds(
        "2026-08", section="accounts", cursor=collection["page"]["next_cursor"],
        expected_version=first["snapshot_version"], limit=2,
    )["data"]["collections"]["accounts"]
    assert [item["account_id"] for item in tail["items"]] == ["bank-b", "bank-c"]
    all_ids = [item["account_id"] for item in collection["items"] + tail["items"]]
    assert all_ids.index("bank-z") < all_ids.index("bank-b")

    selected = dashboard.funds(
        "2026-08", movement_account_selection="first", limit=2,
    )["data"]
    assert selected["selected_movement_account"] == {"type": "cash", "account_id": "cash-a"}
    assert selected["collections"]["accounts"]["items"][0]["account_id"] == "cash-a"
    assert selected["collections"]["movements"]["items"] == []


@pytest.mark.parametrize("raw", [False, True], ids=["default-read", "raw"])
def test_same_start_month_uses_pinyin_then_stable_identity(bank_book, monkeypatch, raw):
    engine, save, publish, proof = bank_book
    _base_opening(engine, proof)
    banking.funding(save, publish, subject="same-month-a", bank="bank-a", day="2026-09-02")
    banking.funding(save, publish, subject="same-month-z", bank="bank-z", day="2026-09-03")
    banking.funding(save, publish, subject="same-month-b", bank="bank-b", day="2026-09-04")
    _name(engine, "bank-a", "李四账户")
    _name(engine, "bank-z", "李四账户")
    _name(engine, "bank-b", "张三账户")
    _use_read_path(monkeypatch, raw)

    accounts = Dashboard(engine).funds("2026-09")["data"]["collections"]["accounts"]["items"]
    assert [item["account_id"] for item in accounts] == ["bank-a", "bank-z", "bank-b"]


@pytest.mark.parametrize("raw", [False, True], ids=["default-read", "raw"])
def test_idle_zero_bank_start_precedes_later_funding(bank_book, monkeypatch, raw):
    engine, save, publish, proof = bank_book
    _base_opening(engine, proof)
    banking.opening(save, publish, bank="bank-a", month="2026-02")
    banking.funding(save, publish, bank="bank-b", day="2026-08-03")
    _name(engine, "bank-a", "张三账户")
    _name(engine, "bank-b", "李四账户")
    _use_read_path(monkeypatch, raw)

    data = Dashboard(engine).funds("2026-08", movement_account_selection="first")["data"]
    accounts = data["collections"]["accounts"]["items"]
    assert [item["account_id"] for item in accounts] == ["bank-a", "bank-b"]
    assert accounts[0]["closing_fen"] == accounts[0]["movement_count"] == 0
    assert data["selected_movement_account"] == {"type": "bank", "account_id": "bank-a"}
    assert data["collections"]["movements"]["items"] == []


@pytest.mark.parametrize("raw", [False, True], ids=["default-read", "raw"])
def test_unpublished_draft_does_not_set_the_start_month(bank_book, monkeypatch, raw):
    engine, save, publish, proof = bank_book
    _base_opening(engine, proof)
    banking.funding(save, publish, subject="published-later", bank="bank-b", day="2026-08-03")
    banking.opening(save, lambda *_: None, bank="draft-bank", month="2026-01", amount=0)
    _name(engine, "bank-b", "晚些开始的账户")
    _use_read_path(monkeypatch, raw)

    data = Dashboard(engine).funds("2026-08")["data"]
    assert [item["account_id"] for item in data["collections"]["accounts"]["items"]] == ["bank-b"]


def test_registered_but_unbooked_account_is_not_listed(bank_book):
    engine, _, _, proof = bank_book
    _base_opening(engine, proof, "2026-09")
    registered = Entities(engine).register_entity(
        "fund_account",
        {
            "display_name": "仅登记账户",
            "bank_name": "合成银行",
            "bank_account_number": "000012345678",
            "account_holder_name": "合成企业",
        },
        account_type="bank",
        source="synthetic explicit registration evidence",
        evidence_digest=proof,
        request_id="register-unbooked-account",
    )

    data = Dashboard(engine).funds("2026-09")["data"]
    assert data["account_count"] == 0
    assert data["collections"]["accounts"]["items"] == []
    assert registered["status"] == "registered"
