"""Search snapshots and exact history are checked against real owner results."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError

from ._working_case import case as case


def search(case, **extra):
    return case.app.accounting_find(
        {
            "company_id": case.company,
            "family": "operations",
            "period_from": "2026-01",
            "period_to": "2026-01",
            "limit": 2,
            **extra,
        }
    )


def test_find_returns_complete_stable_pages_and_ignores_unrelated_future_business(case):
    first = case.seed()
    other = [case.expense("20000"), case.expense("30000")]
    assert case.app.accounting_apply(case.request(*other))["completion"] == "complete"
    page = search(case)
    assert page["page"]["total_count"] == page["page"]["filtered_count"] == 3
    assert page["page"]["has_more"]
    later = case.expense("40000", "2026-03")
    assert case.app.accounting_apply(case.request(later))["completion"] == "complete"
    second = search(case, cursor=page["page"]["next_cursor"], status="active", limit=1)
    assert second["page"]["collection_version"] == page["page"]["collection_version"]
    assert not second["page"]["has_more"]
    ids = [item["business_id"] for item in page["items"] + second["items"]]
    assert len(ids) == len(set(ids)) == 3
    assert set(ids) == {first["business_id"], *(fact["business_id"] for fact in other)}
    changed = deepcopy(first)
    changed["data"]["amount_fen"] = "90000"
    request = case.request(changed)
    request["mutations"][0].update(op="replace", expected_seq=1)
    assert case.app.accounting_apply(request)["completion"] == "complete"
    with pytest.raises(ContractError) as error:
        search(case, cursor=page["page"]["next_cursor"])
    assert error.value.code == "snapshot_changed"


def test_find_tombstone_preserves_month_but_never_pretends_to_keep_old_object_relation(case):
    fact = case.seed()
    assert search(case, object_id=case.party)["items"][0]["business_id"] == fact["business_id"]
    request = case.request()
    request["mutations"] = [
        {"op": "withdraw", "expected_seq": 1, "business_id": fact["business_id"]}
    ]
    assert case.app.accounting_apply(request)["completion"] == "complete"
    row = search(case, status="withdrawn")["items"][0]
    assert row["period"] == "2026-01" and row["state"] == "withdrawn" and row["seq"] == 2
    assert "fact" not in case.get(fact["business_id"])["businesses"][0]
    assert search(case, object_id=case.party, status="all")["items"] == []
    with pytest.raises(ContractError) as error:
        search(case, object_id=case.party, status="withdrawn")
    assert error.value.code == "invalid_input"


@pytest.mark.parametrize(
    "extra",
    [
        {"cursor": "not-valid!!"},
        {"kind": "assets.acquisition"},
        {"period_from": "2026-02", "period_to": "2026-01"},
    ],
)
def test_find_rejects_invalid_or_mismatched_scope_explicitly(case, extra):
    case.seed()
    with pytest.raises(ContractError) as error:
        search(case, **extra)
    assert error.value.code == "invalid_input"


def test_open_replaced_generation_is_conflict_and_exact_current_has_no_fake_historical_fact(case):
    fact = case.seed()
    old = case.get(fact["business_id"])["businesses"][0]["results"][0]["reference"]
    changed = deepcopy(fact)
    changed["data"]["amount_fen"] = "90000"
    request = case.request(changed)
    request["mutations"][0].update(op="replace", expected_seq=1)
    assert case.app.accounting_apply(request)["completion"] == "complete"
    with pytest.raises(ContractError) as error:
        case.get(fact["business_id"], result_id=old["result_id"], generation=old["generation"])
    assert error.value.code == "conflict"
    now = case.get(fact["business_id"])["businesses"][0]["results"][0]["reference"]
    exact = case.get(fact["business_id"], result_id=now["result_id"], generation=now["generation"])[
        "businesses"
    ][0]
    assert "fact" not in exact
    assert exact["results"][0]["target"]["values"]["amount_fen"] == "90000"
    assert [item["source_id"] for item in exact["sources"]] == [case.source_id]
    with pytest.raises(ContractError) as wrong:
        case.get(str(uuid4()), result_id=now["result_id"], generation=now["generation"])
    assert wrong.value.code == "invalid_input"


def test_exact_frozen_result_keeps_original_sources_after_current_fact_is_corrected(case):
    fact = case.seed()
    original = case.get(fact["business_id"])["businesses"][0]["results"][0]["reference"]
    checked = case.check()
    assert checked["ready"], checked
    assert case.close(checked)["state"] == "closed"
    changed = deepcopy(fact)
    changed["data"]["amount_fen"] = "80000"
    changed["evidence_ids"] = []
    request = case.request(changed, correction_period="2026-02")
    request["mutations"][0].update(op="replace", expected_seq=1)
    assert case.app.accounting_apply(request)["completion"] == "complete"
    exact = case.get(
        fact["business_id"], result_id=original["result_id"], generation=original["generation"]
    )["businesses"][0]
    assert "fact" not in exact
    assert exact["results"][0]["target"]["values"]["amount_fen"] == "100000"
    assert [item["source_id"] for item in exact["sources"]] == [case.source_id]
    current = case.get(fact["business_id"])["businesses"][0]
    assert current["fact"] == changed and current["sources"] == []


def test_object_queries_delegate_to_identity_and_report_missing_ids(case):
    case.seed()
    missing = str(uuid4())
    result = case.app.accounting_objects(
        {"company_id": case.company, "action": "get", "object_ids": [case.party, missing]}
    )
    assert result["missing_object_ids"] == [missing]
    assert result["objects"][0]["object_id"] == case.party
    page = case.app.accounting_objects(
        {"company_id": case.company, "action": "find", "kind": "organization", "limit": 1}
    )
    assert page["page"]["filtered_count"] == 1


def test_fact_reads_and_apply_states_do_not_load_result_or_voucher_bodies(case, monkeypatch):
    fact = case.seed()
    expected = case.get(fact["business_id"])["businesses"][0]

    def forbidden(*args, **kwargs):
        raise AssertionError("读取事实与当前状态不应加载核算正文或凭证明细。")

    monkeypatch.setattr(case.app.ledger, "get_results", forbidden)
    actual = case.app.accounting_get(
        {
            "company_id": case.company,
            "business_ids": [fact["business_id"]],
            "include": ["facts"],
        }
    )["businesses"][0]
    assert actual == {"state": expected["state"], "fact": fact, "results": []}
    changed = deepcopy(fact)
    changed["data"]["amount_fen"] = "70000"
    request = case.request(changed)
    request["mutations"][0].update(op="replace", expected_seq=1)
    outcome = case.app.accounting_apply(request)
    assert outcome["completion"] == "complete", outcome
    assert outcome["businesses"][0]["seq"] == 2


def test_shared_read_ports_keep_one_snapshot_without_opening_another_connection(case, monkeypatch):
    fact = case.seed()
    query = {
        "company_id": case.company,
        "business_ids": [fact["business_id"]],
        "include": ["facts", "results", "sources"],
    }
    find = {
        "company_id": case.company,
        "family": "operations",
        "period_from": "2026-01",
        "period_to": "2026-01",
        "limit": 1,
    }
    with case.kernel.storage.read_unit(case.company) as read:
        before = case.app.get_businesses(query, read)

        def forbidden(*args, **kwargs):
            raise AssertionError("公开同快照读取不能另开连接。")

        with monkeypatch.context() as patches:
            patches.setattr(case.kernel.storage, "read_unit", forbidden)
            assert case.app.get_businesses(query, read) == before
            assert case.app.find_businesses(find, read)["items"][0]["seq"] == 1
            old_basis = case.app.context({"company_id": case.company, "issue_scopes": []}, read)[
                "note_basis_heads"
            ]
        changed = deepcopy(fact)
        changed["data"]["amount_fen"] = "70000"
        request = case.request(changed)
        request["mutations"][0].update(op="replace", expected_seq=1)
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert (
                pool.submit(case.app.accounting_apply, request).result(timeout=20)["completion"]
                == "complete"
            )
        assert case.app.get_businesses(query, read) == before
        assert (
            case.app.context({"company_id": case.company, "issue_scopes": []}, read)[
                "note_basis_heads"
            ]
            == old_basis
        )
        with pytest.raises(ContractError) as error:
            case.app.get_businesses({**query, "company_id": str(uuid4())}, read)
        assert error.value.code == "invalid_input"
    assert case.get(fact["business_id"])["businesses"][0]["state"]["seq"] == 2
