"""Public review finds current facts and joins only a selected financial scope."""

from copy import deepcopy
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError

from ._working_case import case as case
from .test_coordination import bank_account, empty_statement


def find(case, collection="obligations", **fields):
    return case.app.accounting_review(
        {
            "company_id": case.company,
            "action": "find",
            "cutoff_period": "2026-03",
            "collection": collection,
            "criteria": {"direction": "payable", "status": "all", "party_ids": [case.party]},
            **fields,
        }
    )


def inspect(case, business, collection="obligations", **fields):
    return case.app.accounting_review(
        {
            "company_id": case.company,
            "action": "inspect",
            "cutoff_period": "2026-03",
            "subject": {"type": "business", "business_id": business},
            "collection": collection,
            **fields,
        }
    )


def save(case, *facts, **fields):
    saved = case.app.accounting_apply(case.request(*facts, **fields))
    assert saved["completion"] == "complete", saved


def payment(case, account_id, *allocations, on="2026-02-10"):
    return {
        "business_id": str(uuid4()),
        "kind": "money.movement",
        "period": on[:7],
        "effective_on": on,
        "evidence_ids": [case.source_id],
        "data": {
            "type": "settlement",
            "funds_account_id": account_id,
            "direction": "outflow",
            "amount_fen": str(sum(int(amount) for _, amount in allocations)),
            "payment_method": "individual",
            "allocations": [
                {
                    "source": {"business_id": fact["business_id"], "slot": "primary"},
                    "amount_fen": amount,
                }
                for fact, amount in allocations
            ],
        },
    }


def review_subject(case, subject, collection="obligations", **fields):
    return case.app.accounting_review(
        {
            "company_id": case.company,
            "action": "inspect",
            "cutoff_period": "2026-03",
            "subject": subject,
            "collection": collection,
            **fields,
        }
    )


def movements(case, account_id, **fields):
    return find(
        case,
        "movements",
        criteria={"account_id": account_id, "period_from": "2026-01", "period_to": "2026-03"},
        **fields,
    )


def test_find_and_inspect_keep_period_balance_source_and_shared_basis(case):
    expense = case.seed()
    found = find(case)
    assert found["page"]["total_count"] == 1
    row = found["items"][0]
    assert row["relationship"] == "candidate"
    assert row["amounts"]["remaining_fen"] == "100000"
    assert row["origin"]["business_period"] == "2026-01"
    assert found["objects"][0]["name"] == "合成供应商"
    assert found["sources"][0]["source_id"] == case.source_id
    first = inspect(case, expense["business_id"])
    assert first["items"][0]["relationship"] == "direct"
    second = inspect(
        case, expense["business_id"], "settlements", expected_version=first["basis_version"]
    )
    assert second["items"] == []
    assert second["basis_version"] == first["basis_version"]


def test_full_candidate_scope_changes_off_page_but_unrelated_business_does_not(case):
    expense = case.seed()
    save(case, case.expense("200000"))
    first = find(case, limit=1)
    assert first["page"]["has_more"]
    save(case, case.expense("300000"))
    with pytest.raises(ContractError) as error:
        find(case, cursor=first["page"]["next_cursor"])
    assert error.value.code == "snapshot_changed"
    selected = inspect(case, expense["business_id"])
    save(case, case.expense("400000"))
    unchanged = inspect(case, expense["business_id"], expected_version=selected["basis_version"])
    assert unchanged["basis_version"] == selected["basis_version"]


def test_partial_and_combined_payments_keep_allocation_separate_from_whole_cash(case):
    original = case.seed()
    other = case.expense("90000")
    bank = bank_account(str(uuid4()))
    first = payment(case, bank["object_id"], (original, "30000"))
    combined = payment(case, bank["object_id"], (original, "20000"), (other, "40000"))
    separate = payment(case, bank["object_id"], (other, "10000"), on="2026-03-05")
    save(case, other, first, combined, separate, objects=[bank])

    balance = inspect(case, original["business_id"])
    version = balance["basis_version"]
    assert balance["items"][0]["amounts"]["amount_fen"] == "100000"
    assert balance["items"][0]["amounts"]["money_settled_fen"] == "50000"
    assert balance["items"][0]["amounts"]["remaining_fen"] == "50000"
    events = inspect(case, original["business_id"], "settlements", expected_version=version)
    assert sorted(item["use"]["value"]["amount_fen"] for item in events["items"]) == [
        "20000",
        "30000",
    ]
    funds = inspect(case, original["business_id"], "movements", expected_version=version)
    assert {item["reference"]["business_id"] for item in funds["items"]} == {
        first["business_id"],
        combined["business_id"],
    }
    mixed = next(
        item
        for item in funds["items"]
        if item["reference"]["business_id"] == combined["business_id"]
    )
    assert mixed["movement"]["amount_fen"] == "60000"
    assert {row["source"]["business_id"]: row["amount_fen"] for row in mixed["allocations"]} == {
        original["business_id"]: "20000",
        other["business_id"]: "40000",
    }

    direct_cash = review_subject(
        case, {"type": "movement", "movement_id": mixed["movement"]["movement_id"]}
    )
    by_business = {item["reference"]["business_id"]: item for item in direct_cash["items"]}
    assert by_business[other["business_id"]]["amounts"]["remaining_fen"] == "40000"
    direct_events = review_subject(
        case,
        {"type": "movement", "movement_id": mixed["movement"]["movement_id"]},
        "settlements",
        expected_version=direct_cash["basis_version"],
    )
    assert {item["use"]["source"]["business_id"] for item in direct_events["items"]} == {
        combined["business_id"]
    }


def test_settled_obligation_remains_findable_and_links_its_real_payment(case):
    original = case.seed()
    bank = bank_account(str(uuid4()))
    paid = payment(case, bank["object_id"], (original, "100000"))
    save(case, paid, objects=[bank])
    settled = find(
        case, criteria={"direction": "payable", "status": "settled", "party_ids": [case.party]}
    )
    assert settled["page"]["total_count"] == 1
    assert settled["items"][0]["amounts"]["remaining_fen"] == "0"
    unsettled = find(
        case, criteria={"direction": "payable", "status": "unsettled", "party_ids": [case.party]}
    )
    assert unsettled["items"] == []
    events = inspect(case, original["business_id"], "settlements")
    assert len(events["items"]) == 1
    assert events["items"][0]["movement"]["reference"]["business_id"] == paid["business_id"]
    assert events["items"][0]["use"]["value"]["amount_fen"] == "100000"


def test_unmatched_original_row_exposes_registered_candidate_without_inventing_link(case):
    original = case.seed()
    bank = bank_account(str(uuid4()))
    paid = payment(case, bank["object_id"], (original, "30000"), on="2026-01-10")
    statement, reconciliation = empty_statement(bank["object_id"])
    row_id = str(uuid4())
    statement["evidence_ids"] = [case.source_id]
    statement["data"].update(
        closing_fen="-30000",
        rows=[
            {
                "row_id": row_id,
                "actual_on": "2026-01-10",
                "direction": "outflow",
                "amount_fen": "30000",
                "counterparty_text": "合成供应商",
                "memo": "支付维修款",
                "source": {"source_id": case.source_id, "location": "合成流水第1行"},
            }
        ],
    )
    save(case, paid, statement, objects=[bank])
    subject = {
        "type": "statement_row",
        "statement_business_id": statement["business_id"],
        "row_id": row_id,
    }
    related = {
        "movements": {
            "account_id": bank["object_id"],
            "period_from": "2026-01",
            "period_to": "2026-01",
        }
    }
    rows = review_subject(case, subject, "statement_rows", related_scope=related)
    assert rows["items"][0]["match_group_id"] is None
    assert rows["items"][0]["memo"] == "支付维修款"
    empty = review_subject(
        case, subject, "links", related_scope=related, expected_version=rows["basis_version"]
    )
    assert empty["items"] == []
    candidates = review_subject(
        case, subject, "movements", related_scope=related, expected_version=rows["basis_version"]
    )
    assert len(candidates["items"]) == 1
    assert candidates["items"][0]["relationship"] == "candidate"
    assert candidates["items"][0]["reference"]["business_id"] == paid["business_id"]
    occurrence = candidates["items"][0]["movement"]["movement_id"]
    reconciliation["data"]["matches"] = [
        {"statement_row_ids": [row_id], "fund_movement_ids": [occurrence]}
    ]
    save(case, reconciliation)
    with pytest.raises(ContractError) as failed:
        review_subject(
            case, subject, "links", related_scope=related, expected_version=rows["basis_version"]
        )
    assert failed.value.code == "snapshot_changed"
    links = review_subject(case, subject, "links", related_scope=related)
    assert links["items"][0]["relation"] == "reconciliation"
    assert links["items"][0]["movement_ids"] == [occurrence]


@pytest.mark.parametrize("change", ["payment", "replace", "withdraw", "new_candidate"])
def test_candidate_page_rejects_change_to_an_unread_relevant_item(case, change):
    first, second = case.seed(), case.expense("200000")
    save(case, second)
    page = find(case, limit=1)
    assert page["page"]["has_more"]
    shown = page["items"][0]["reference"]["business_id"]
    off_page = second if shown == first["business_id"] else first
    if change == "payment":
        bank = bank_account(str(uuid4()))
        save(case, payment(case, bank["object_id"], (off_page, "10000")), objects=[bank])
    elif change == "new_candidate":
        save(case, case.expense("300000"))
    else:
        mutation = {"op": change, "expected_seq": 1}
        if change == "replace":
            replacement = deepcopy(off_page)
            replacement["data"]["amount_fen"] = "250000"
            mutation["fact"] = replacement
        else:
            mutation["business_id"] = off_page["business_id"]
        save(case, mutations=[mutation])
    with pytest.raises(ContractError) as failed:
        find(case, limit=1, cursor=page["page"]["next_cursor"])
    assert failed.value.code == "snapshot_changed"


@pytest.mark.parametrize("change", ["add", "replace", "withdraw"])
def test_shared_basis_rejects_related_payment_change_across_collections(case, change):
    original = case.seed()
    bank = bank_account(str(uuid4()))
    paid = payment(case, bank["object_id"], (original, "30000"))
    save(case, paid, objects=[bank])
    balance = inspect(case, original["business_id"])
    if change == "add":
        save(case, payment(case, bank["object_id"], (original, "10000"), on="2026-03-01"))
    elif change == "replace":
        revised = deepcopy(paid)
        revised["data"]["amount_fen"] = "20000"
        revised["data"]["allocations"][0]["amount_fen"] = "20000"
        save(case, mutations=[{"op": "replace", "expected_seq": 1, "fact": revised}])
    else:
        save(
            case,
            mutations=[{"op": "withdraw", "expected_seq": 1, "business_id": paid["business_id"]}],
        )
    for collection in ("settlements", "movements"):
        with pytest.raises(ContractError) as failed:
            inspect(
                case, original["business_id"], collection, expected_version=balance["basis_version"]
            )
        assert failed.value.code == "snapshot_changed"


def test_unrelated_account_and_obligation_keep_selected_basis_and_funds_cursor(case):
    original, other = case.seed(), case.expense("90000")
    bank, other_bank = bank_account(str(uuid4())), bank_account(str(uuid4()))
    first = payment(case, bank["object_id"], (original, "30000"))
    second = payment(case, bank["object_id"], (original, "20000"), on="2026-03-05")
    save(case, other, first, second, objects=[bank, other_bank])
    selected = inspect(case, original["business_id"])
    page = movements(case, bank["object_id"], limit=1)
    assert page["page"]["has_more"]
    save(case, payment(case, other_bank["object_id"], (other, "10000"), on="2026-03-06"))
    resumed = movements(case, bank["object_id"], limit=1, cursor=page["page"]["next_cursor"])
    assert len(resumed["items"]) == 1
    assert resumed["basis_version"] == page["basis_version"]
    unchanged = inspect(
        case, original["business_id"], "settlements", expected_version=selected["basis_version"]
    )
    assert unchanged["basis_version"] == selected["basis_version"]


def test_transfer_reports_non_cash_use_and_actual_target_reference(case):
    original = case.seed()
    payer = {
        "object_id": str(uuid4()),
        "kind": "person",
        "name": "合成垫付人",
        "expected_seq": 0,
        "data": {},
    }
    transferred = {
        "business_id": str(uuid4()),
        "kind": "operations.reimbursement",
        "period": "2026-02",
        "evidence_ids": [case.source_id],
        "data": {
            "type": "acceptance",
            "payer_id": payer["object_id"],
            "payer_kind": "owner",
            "sources": [
                {
                    "source": {"business_id": original["business_id"], "slot": "primary"},
                    "amount_fen": "30000",
                    "recipient_id": case.party,
                }
            ],
        },
    }
    save(case, transferred, objects=[payer])
    balance = inspect(case, original["business_id"])
    assert balance["items"][0]["amounts"]["transferred_out_fen"] == "30000"
    assert balance["items"][0]["amounts"]["remaining_fen"] == "70000"
    events = inspect(
        case, original["business_id"], "settlements", expected_version=balance["basis_version"]
    )
    assert len(events["items"]) == 1
    event = events["items"][0]
    assert event["use"]["kind"] == "reclassification" and event["movement"] is None
    assert event["use"]["value"]["mode"] == "transfer"
    assert event["use"]["value"]["target"] == {
        "business_id": transferred["business_id"],
        "slot": "reimbursement",
    }
    assert event["origin"]["business_id"] == transferred["business_id"]
    assert events["sources"][0]["source_id"] == case.source_id


def test_page_metadata_only_reads_the_current_page_origins(case, monkeypatch):
    case.seed()
    save(case, case.expense("200000"), case.expense("300000"))
    seen = []
    original = case.kernel.ledger.get_review_sources
    heads = []

    def record(references, read):
        seen.append({reference["business_id"] for reference in references})
        repo = read.repository("ledger")
        read_heads = repo.read_heads

        def record_heads(result_ids):
            heads.append(tuple(result_ids))
            return read_heads(result_ids)

        monkeypatch.setattr(repo, "read_heads", record_heads)
        monkeypatch.setattr(repo, "results", lambda *a: pytest.fail("核查元数据加载了业务正文"))
        return original(references, read)

    monkeypatch.setattr(case.kernel.ledger, "get_review_sources", record)
    monkeypatch.setattr(
        case.kernel.ledger,
        "get_adopted_sources",
        lambda *a: pytest.fail("核查元数据重复读取了正式来源"),
    )
    first = find(case, limit=1)
    assert seen == [{first["items"][0]["reference"]["business_id"]}]
    assert len(heads) == 1
    seen.clear()
    heads.clear()
    second = find(case, limit=1, cursor=first["page"]["next_cursor"])
    assert seen == [{second["items"][0]["reference"]["business_id"]}]
    assert len(heads) == 1
    assert first["items"][0]["reference"] != second["items"][0]["reference"]
    assert first["page"]["total_count"] == second["page"]["total_count"] == 3


@pytest.mark.parametrize("kind", ["business", "obligation", "movement", "statement_row"])
def test_missing_subject_is_an_input_error_without_claiming_missing_reality(case, kind):
    original = case.seed()
    if kind == "business":
        subject = {"type": kind, "business_id": str(uuid4())}
    elif kind == "obligation":
        subject = {"type": kind, "business_id": original["business_id"], "slot": "not-present"}
    elif kind == "movement":
        subject = {"type": kind, "movement_id": str(uuid4())}
    else:
        bank = bank_account(str(uuid4()))
        statement, _ = empty_statement(bank["object_id"])
        save(case, statement, objects=[bank])
        subject = {
            "type": kind,
            "statement_business_id": statement["business_id"],
            "row_id": str(uuid4()),
        }
    with case.kernel.storage.read_unit(case.company) as read:
        head = read.head
    with pytest.raises(ContractError) as failed:
        review_subject(case, subject)
    assert failed.value.code == "invalid_input"
    with case.kernel.storage.read_unit(case.company) as read:
        assert read.head == head


def test_subject_withdrawal_is_a_changed_basis_on_resume(case):
    original = case.seed()
    before = inspect(case, original["business_id"])
    save(
        case,
        mutations=[{"op": "withdraw", "expected_seq": 1, "business_id": original["business_id"]}],
    )
    with pytest.raises(ContractError) as failed:
        inspect(
            case,
            original["business_id"],
            expected_version=before["basis_version"],
        )
    assert failed.value.code == "snapshot_changed"
    with pytest.raises(ContractError) as missing:
        inspect(case, original["business_id"])
    assert missing.value.code == "invalid_input"


def test_cursor_cannot_cross_collections_but_common_basis_can(case):
    original, other = case.seed(), case.expense("90000")
    bank = bank_account(str(uuid4()))
    paid = payment(case, bank["object_id"], (original, "30000"), (other, "20000"))
    save(case, other, paid, objects=[bank])
    funds = movements(case, bank["object_id"])
    subject = {"type": "movement", "movement_id": funds["items"][0]["movement"]["movement_id"]}
    first = review_subject(case, subject, limit=1)
    assert first["page"]["has_more"]
    with pytest.raises(ContractError) as failed:
        review_subject(case, subject, "settlements", cursor=first["page"]["next_cursor"])
    assert failed.value.code == "invalid_input"
    settlements = review_subject(
        case, subject, "settlements", expected_version=first["basis_version"]
    )
    assert settlements["page"]["total_count"] == 2
    assert settlements["basis_version"] == first["basis_version"]
    with pytest.raises(ContractError) as failed:
        review_subject(case, subject, cursor="this-is-not-a-review-cursor")
    assert failed.value.code == "invalid_input"


def test_explicit_candidates_share_basis_without_becoming_confirmed_relations(case):
    original, other = case.seed(), case.expense("100000")
    save(case, other)
    references = [
        {"business_id": original["business_id"], "slot": "primary"},
        {"business_id": other["business_id"], "slot": "primary"},
    ]
    related = {"obligations": {"references": references}}
    first = inspect(case, original["business_id"], related_scope=related)
    assert {row["reference"]["business_id"]: row["relationship"] for row in first["items"]} == {
        original["business_id"]: "direct",
        other["business_id"]: "candidate",
    }
    reordered = {"obligations": {"references": list(reversed(references))}}
    resumed = inspect(
        case,
        original["business_id"],
        "settlements",
        related_scope=reordered,
        expected_version=first["basis_version"],
    )
    assert resumed["items"] == [] and resumed["basis_version"] == first["basis_version"]
    bank = bank_account(str(uuid4()))
    save(case, payment(case, bank["object_id"], (other, "10000")), objects=[bank])
    with pytest.raises(ContractError) as failed:
        inspect(
            case,
            original["business_id"],
            "movements",
            related_scope=related,
            expected_version=first["basis_version"],
        )
    assert failed.value.code == "snapshot_changed"


def test_new_candidate_invalidates_only_the_inspect_scope_that_requested_discovery(case):
    original = case.seed()
    related = {
        "obligations": {
            "criteria": {"direction": "payable", "status": "all", "party_ids": [case.party]}
        }
    }
    direct = inspect(case, original["business_id"])
    discover = inspect(case, original["business_id"], related_scope=related)
    save(case, case.expense("100000"))
    with pytest.raises(ContractError) as failed:
        inspect(
            case,
            original["business_id"],
            "settlements",
            related_scope=related,
            expected_version=discover["basis_version"],
        )
    assert failed.value.code == "snapshot_changed"
    resumed = inspect(case, original["business_id"], expected_version=direct["basis_version"])
    assert resumed["basis_version"] == direct["basis_version"]


def test_candidate_other_payment_does_not_become_direct_via_combined_payment(case):
    original, other = case.seed(), case.expense("90000")
    bank = bank_account(str(uuid4()))
    combined = payment(case, bank["object_id"], (original, "20000"), (other, "40000"))
    separate = payment(case, bank["object_id"], (other, "10000"), on="2026-03-05")
    save(case, other, combined, separate, objects=[bank])
    related = {
        "obligations": {"references": [{"business_id": other["business_id"], "slot": "primary"}]}
    }
    events = inspect(case, original["business_id"], "settlements", related_scope=related)
    unrelated = [
        item
        for item in events["items"]
        if item["use"]["source"]["business_id"] == separate["business_id"]
    ]
    assert len(unrelated) == 1
    assert unrelated[0]["relationship"] == "candidate"
    funds = inspect(
        case,
        original["business_id"],
        "movements",
        related_scope=related,
        expected_version=events["basis_version"],
    )
    assert {item["reference"]["business_id"]: item["relationship"] for item in funds["items"]} == {
        combined["business_id"]: "direct",
        separate["business_id"]: "candidate",
    }
