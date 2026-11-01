"""Sourced own-funds channels change management meaning, never the adopted ledger."""

from copy import deepcopy
from hashlib import sha256
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import (
    ContractError,
    FactDocumentPart,
    PageQuery,
    fact_source_ids,
    validate_contract,
)
from ai_accounting_modular.money.public import MovementQuery, counterparty_meaning
from ai_accounting_modular.records.public import create_records

from .test_application import get, payment, request, seed
from .test_application import system as system


def reserve(ids, *, channel=None):
    fact = payment(ids)
    fact["data"] = {
        "type": "managed_reserve",
        "funds_account_id": ids["bank"],
        "direction": "inflow",
        "amount_fen": "10000",
    }
    if channel is not None:
        fact["data"]["own_funds_channel"] = channel
    return fact


def channel_source():
    text = "合成资料：本公司自有平台备用金回到银行，平台资金按现有范围不管理余额。"
    source = {
        "source_id": str(uuid4()),
        "kind": "owner_statement",
        "text": text,
        "sha256": sha256(text.encode()).hexdigest(),
        "archive_month": "2026-01",
    }
    return source, {"name": "合成自有平台", "source": {"source_id": source["source_id"]}}


def movements(system, ids, *, page=None, view="current"):
    _, storage, company, money, _ = system
    with storage.read_unit(company) as read:
        scope = {
            "company_id": company,
            "period_from": "2026-01",
            "period_to": "2026-01",
            "view": view,
        }
        return money.query_movements(scope, MovementQuery(ids["bank"]), page or PageQuery(), read)


def test_known_missing_internal_and_own_funds_have_distinct_meanings():
    party = str(uuid4())
    _, channel = channel_source()
    assert counterparty_meaning(party_ids=(None, party, party)) == {
        "state": "known",
        "party_ids": [party],
    }
    assert counterparty_meaning() == {"state": "missing"}
    assert counterparty_meaning(internal_transfer=True) == {
        "state": "not_applicable",
        "reason": "internal_transfer",
    }
    assert counterparty_meaning(own_funds_channel=channel) == {
        "state": "not_applicable",
        "reason": "own_funds_channel",
        "own_funds_channel": channel,
    }
    with pytest.raises(ContractError) as error:
        counterparty_meaning(party_ids=(party,), own_funds_channel=channel)
    assert error.value.code == "storage_corrupt"


@pytest.mark.parametrize("frozen", [False, True])
def test_supplement_channel_preserves_exact_formal_result_and_protects_nested_source(
    system, frozen
):
    app, storage, company, money, ledger = system
    ids, _ = seed(system)
    original = reserve(ids)
    assert app.accounting_apply(request(company, original))["completion"] == "complete"
    if frozen:
        with storage.write_unit(company) as unit:
            unit.repository("ledger").freeze_heads("2026-01")
    before = get(app, company, ids["business"])["businesses"][0]["results"]
    old_page = movements(system, ids)
    assert old_page.items[0]["counterparty"] == {"state": "missing"}
    with storage.read_unit(company) as read:
        ref = ledger.get_result_refs((ids["business"],), read)[0]
        assert money.own_funds_channels((ref,), read) == {
            (ref["result_id"], ref["generation"]): None
        }
    source, channel = channel_source()
    revised = reserve(ids, channel=channel)
    response = app.accounting_apply(
        request(
            company,
            sources=[source],
            mutations=[{"op": "replace", "expected_seq": 1, "fact": revised}],
        )
    )
    assert response["completion"] == "complete", response
    assert response["accounting"] == "unchanged"
    assert get(app, company, ids["business"])["businesses"][0]["results"] == before
    page = movements(system, ids, view="as_posted" if frozen else "current")
    assert page.items[0]["movement"] == old_page.items[0]["movement"]
    assert page.items[0]["counterparty"] == {
        "state": "not_applicable",
        "reason": "own_funds_channel",
        "own_funds_channel": channel,
    }
    with pytest.raises(ContractError) as error:
        movements(system, ids, page=PageQuery(expected_version=old_page.version))
    assert error.value.code == "snapshot_changed"
    records = create_records(root_for=lambda read: storage.company_metadata(read).company_root)
    with storage.read_unit(company) as read:
        ref = ledger.get_result_refs((ids["business"],), read)[0]
        assert money.own_funds_channels((ref,), read) == {
            (ref["result_id"], ref["generation"]): channel
        }
        assert money.own_funds_channels(({**ref, "generation": ref["generation"] + 1},), read) == {}
        assert records.source_discard_protection(
            (source["source_id"],), read, retained_source_ids=(), protected_paths=()
        ) == (source["source_id"],)
        exported = money.export_current((ids["business"],), read)
    assert exported[0]["data"]["own_funds_channel"] == channel
    assert fact_source_ids(exported[0]) == tuple(sorted((ids["source"], source["source_id"])))
    shard = FactDocumentPart(
        {
            "format": "ai-accounting-standard/1",
            "key": "facts:money:2026-01",
            "facts": list(exported),
        }
    )
    assert shard.source_ids == tuple(sorted((ids["source"], source["source_id"])))


def test_channel_source_is_required_and_external_party_is_mutually_exclusive(system):
    app, _, company, _, _ = system
    ids, _ = seed(system)
    _, channel = channel_source()
    missing_source = reserve(ids, channel=channel)
    response = app.accounting_apply(request(company, missing_source))
    assert response["completion"] == "not_saved", response
    assert response["error"] == "invalid_input"
    conflicting = reserve(ids, channel=channel)
    conflicting["data"]["counterparty_id"] = ids["party"]
    with pytest.raises(ContractError):
        validate_contract("business-facts.schema.json", "Fact", conflicting)
    for value in ({"name": "合成平台"}, {"name": " ", "source": channel["source"]}):
        with pytest.raises(ContractError):
            validate_contract("business-facts.schema.json", "Fact", reserve(ids, channel=value))


def test_channel_edits_keep_result_but_accounting_replacement_does_not_relabel_old_result(
    system,
):
    app, storage, company, money, ledger = system
    ids, _ = seed(system)
    source, channel = channel_source()
    fact = reserve(ids, channel=channel)
    assert (
        app.accounting_apply(request(company, fact, sources=[source]))["completion"] == "complete"
    )
    with storage.read_unit(company) as read:
        original_ref = ledger.get_result_refs((ids["business"],), read)[0]
    before = movements(system, ids)
    changed = deepcopy(fact)
    changed["data"]["own_funds_channel"]["name"] = "合成自有备用金渠道"
    for seq, next_fact in ((1, changed), (2, reserve(ids))):
        result = app.accounting_apply(
            request(company, mutations=[{"op": "replace", "expected_seq": seq, "fact": next_fact}])
        )
        assert result["completion"] == "complete", result
        assert result["accounting"] == "unchanged"
        current = movements(system, ids)
        assert current.version != before.version
        before = current
    assert before.items[0]["counterparty"] == {"state": "missing"}
    changed["data"]["amount_fen"] = "20000"
    response = app.accounting_apply(
        request(company, mutations=[{"op": "replace", "expected_seq": 3, "fact": changed}])
    )
    assert response["completion"] == "complete", response
    with storage.read_unit(company) as read:
        assert money.own_funds_channels((original_ref,), read) == {}
        current_ref = ledger.get_result_refs((ids["business"],), read)[0]
        assert money.own_funds_channels((current_ref,), read) == {
            (current_ref["result_id"], current_ref["generation"]): changed["data"][
                "own_funds_channel"
            ]
        }


def test_channel_query_only_reads_selected_businesses_and_unrelated_changes_keep_version(
    system, monkeypatch
):
    app, storage, company, money, ledger = system
    ids, _ = seed(system)
    source, channel = channel_source()
    fact = reserve(ids, channel=channel)
    other = reserve({**ids, "business": str(uuid4()), "bank": ids["cash"]}, channel=channel)
    response = app.accounting_apply(request(company, fact, other, sources=[source]))
    assert response["completion"] == "complete", response
    before = movements(system, ids)
    other["data"]["own_funds_channel"] = {**channel, "name": "另一自有渠道"}
    response = app.accounting_apply(
        request(company, mutations=[{"op": "replace", "expected_seq": 1, "fact": other}])
    )
    assert response["completion"] == "complete", response
    assert movements(system, ids, page=PageQuery(expected_version=before.version)) == before
    with storage.read_unit(company) as read:
        reference = ledger.get_result_refs((ids["business"],), read)[0]
        repository = read.repository("money")
        projected = repository.own_funds_channels
        selected = []

        def narrow(ids_to_read):
            selected.extend(ids_to_read)
            return projected(ids_to_read)

        def no_whole_fact(*_):
            raise AssertionError("channel lookup must not load full business facts")

        monkeypatch.setattr(repository, "get", no_whole_fact)
        monkeypatch.setattr(repository, "own_funds_channels", narrow)
        assert money.own_funds_channels((reference,), read) == {
            (reference["result_id"], reference["generation"]): channel
        }
        assert selected == [ids["business"]]


@pytest.mark.parametrize("damage", ["conflicting_party", "missing_source", "wrong_kind"])
def test_damaged_channel_projection_is_rejected(system, damage):
    app, storage, company, money, ledger = system
    ids, _ = seed(system)
    source, channel = channel_source()
    response = app.accounting_apply(
        request(company, reserve(ids, channel=channel), sources=[source])
    )
    assert response["completion"] == "complete", response
    with storage.write_unit(company) as unit:
        session = unit.repository("money")._session
        paths = {
            "conflicting_party": ("$.data.counterparty_id", ids["party"]),
            "missing_source": ("$.data.own_funds_channel.source", None),
            "wrong_kind": ("$.kind", "money.bank_statement"),
        }
        path, value = paths[damage]
        session.execute(
            "UPDATE money_fact SET data_json=json_set(data_json,?,?) WHERE business_id=?",
            (path, value, ids["business"]),
        )
    with storage.read_unit(company) as read:
        reference = ledger.get_result_refs((ids["business"],), read)[0]
        with pytest.raises(ContractError) as error:
            money.own_funds_channels((reference,), read)
        assert error.value.code == "storage_corrupt"
