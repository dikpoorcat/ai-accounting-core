"""The same actual payee survives funds, bank breakdowns and exact business progress."""

from copy import deepcopy
from uuid import uuid4

from ai_accounting_modular.reading import _brief_activity
from tests.modular.application._working_case import case as case
from tests.modular.domains.financing._support import BANK, CASH, COUNTERPARTY, fact, objects

from .test_activity_parties import institutional_payments, member_scope
from .test_brief_activity import activity, capture, vouchers
from .test_business_status import progress
from .test_funds import apply, initialize, page
from .test_funds import capture as capture_funds
from .test_funds_integrity import original_row, pair


def test_funds_and_progress_keep_actual_payees_separate_from_employee_beneficiaries(case):
    wage, organization, person, payments = institutional_payments(case)
    original = case.get(wage["business_id"])
    with case.kernel.storage.read_unit(case.company) as read:
        formal_head = read.head
    capture(case)
    capture_funds(case)
    names = {organization: "合成征收机构", person: "合成社保结算收款人"}
    expected = {item["business_id"]: names[item["data"]["counterparty_id"]] for item in payments}
    actual = page(case)["data"]["collections"]["movements"]["items"]
    assert {item["subject_id"]: item["party"] for item in actual} == expected

    events = progress(case, wage["business_id"])["data"]["collections"]["settlement_events"][
        "items"
    ]
    assert {
        (item["purpose_label"], item["party"], item["signed_amount_fen"]) for item in events
    } == {
        ("个人社保", "合成征收机构", "100000"),
        ("公司社保", "合成征收机构", "150000"),
        ("公司社保", "合成社保结算收款人", "50000"),
        ("个人公积金", "合成征收机构", "50000"),
        ("公司公积金", "合成征收机构", "50000"),
        ("代扣个人所得税", "合成征收机构", "20000"),
    }
    for row in activity(case)["items"]:
        if row["group"] != "cash":
            continue
        member, _ = member_scope(case, row)
        data = progress(
            case,
            member["subject_id"],
            detail_scope_key=member["detail_scope_key"],
            voucher_version_id=member["voucher_version_id"],
        )["data"]
        profiles = data["display_profiles"]
        assert {item["entity_id"] for item in profiles["employees"]} == {wage["data"]["person_id"]}
        assert {item["values"]["display_name"] for item in profiles["counterparties"]} == {
            expected[member["subject_id"]]
        }
    assert case.get(wage["business_id"]) == original
    with case.kernel.storage.read_unit(case.company) as read:
        assert read.head == formal_head


def test_bank_breakdown_reuses_the_same_payee_but_preserves_original_counterparty_text(case):
    _, _, _, payments = institutional_payments(case)
    paid = payments[0]
    account_id = paid["data"]["funds_account_id"]
    with case.kernel.storage.read_unit(case.company) as read:
        (account,) = case.kernel.identity.get_batch([account_id], read)
    rows = [
        original_row(
            case, str(uuid4()), item["data"]["amount_fen"], item["effective_on"], "outflow"
        )
        | {"counterparty_text": "银行原文财政账户"}
        for item in payments
    ]
    apply(
        case,
        *pair(account_id, rows, payments),
        objects=[
            {
                "object_id": account_id,
                "kind": account["kind"],
                "name": account["name"],
                "expected_seq": account["seq"],
                "data": dict(account["data"])
                | {"opened_period": "2026-01", "zero_at_opening": True},
            }
        ],
    )
    capture_funds(case)
    bank_row = next(
        item
        for item in page(case)["data"]["collections"]["statements"]["items"]
        if item["id"] == rows[0]["row_id"]
    )
    assert bank_row["party"] == "银行原文财政账户"
    assert sorted(
        (item["party"], item["amount_fen"]) for item in bank_row["batch_payment"]["items"]
    ) == [
        ("合成征收机构", "100000"),
        ("合成征收机构", "150000"),
    ]


def test_funds_distinguish_external_missing_parties_from_company_internal_accounts(case):
    initialize(case)
    movements = [
        fact(
            "money.movement",
            {
                "type": "managed_reserve",
                "funds_account_id": BANK,
                "direction": direction,
                "amount_fen": "100",
            },
            actual="2026-01-16",
        )
        for direction in ("inflow", "outflow")
    ]
    transfer = fact(
        "money.transfer",
        {"type": "physical", "from_account_id": BANK, "to_account_id": CASH, "amount_fen": "50"},
        actual="2026-01-17",
    )
    owned = [
        fact(
            "money.movement",
            {
                "type": "managed_reserve",
                "funds_account_id": BANK,
                "direction": direction,
                "amount_fen": "120",
                "own_funds_channel": {
                    "name": "合成自有平台",
                    "source": {"source_id": case.source_id},
                },
            },
            actual="2026-01-16",
        )
        for direction in ("inflow", "outflow")
    ]
    external = deepcopy(movements[0])
    external["business_id"] = str(uuid4())
    external["data"]["counterparty_id"] = COUNTERPARTY
    original = original_row(case, str(uuid4()), "120", "2026-01-16", "inflow") | {
        "counterparty_text": "原行过渡户名称",
        "memo": "原行平台转回摘要",
    }
    statement, _ = pair(BANK, [original], [owned[0]])
    apply(case, *movements, transfer, *owned, external, statement)
    capture(case)
    capture_funds(case)
    rows = page(case)["data"]["collections"]["movements"]["items"]
    unresolved_ids = {item["business_id"] for item in movements}
    assert {
        (item["direction"], item["party"]) for item in rows if item["subject_id"] in unresolved_ids
    } == {("inflow", "付款方未提供"), ("outflow", "收款方未提供")}
    assert [item["party"] for item in rows if item["internal_transfer"]] == [
        "公司内部账户划转",
        "公司内部账户划转",
    ]
    owned_ids = {item["business_id"] for item in owned}
    assert {item["party"] for item in rows if item["subject_id"] in owned_ids} == {
        "合成自有平台（公司自有资金）"
    }
    assert {row["party"] for row in activity(case)["items"] if row["amount_fen"] == "120"} == {
        "合成自有平台（公司自有资金）"
    }
    bank_row = page(case)["data"]["collections"]["statements"]["items"][0]
    assert (bank_row["party"], bank_row["memo"]) == ("原行过渡户名称", "原行平台转回摘要")
    review = case.app.accounting_review(
        {
            "company_id": case.company,
            "action": "find",
            "collection": "movements",
            "cutoff_period": "2026-01",
            "criteria": {"account_id": BANK, "period_from": "2026-01", "period_to": "2026-01"},
            "limit": 100,
        }
    )
    meanings = {item["reference"]["business_id"]: item["counterparty"] for item in review["items"]}
    assert all(meanings[identity]["state"] == "missing" for identity in unresolved_ids)
    assert all(meanings[identity]["reason"] == "own_funds_channel" for identity in owned_ids)
    assert meanings[external["business_id"]] == {"state": "known", "party_ids": [COUNTERPARTY]}


def test_closed_current_channel_updates_only_its_activity_and_preserves_vouchers(case, monkeypatch):
    case.seed(expense=False)
    first, other = [
        fact(
            "money.movement",
            {
                "type": "managed_reserve",
                "funds_account_id": CASH,
                "direction": "inflow",
                "amount_fen": amount,
            },
            actual="2026-01-16",
        )
        for amount in ("100", "200")
    ]
    first, other = apply(
        case, first, other, objects=[item for item in objects() if item["object_id"] == CASH]
    )
    assert case.close(case.check())["state"] == "closed"
    capture(case)
    capture_funds(case)
    original_rows = activity(case)["items"]
    original_subjects = {
        row["key"]: member_scope(case, row)[0]["subject_id"] for row in original_rows
    }
    # Both names initially match. Choose the first actual row, rather than let
    # random formal-source digests decide whether this case exercises a move.
    first_key, other_key = (row["key"] for row in original_rows)
    facts = {item["business_id"]: item for item in (first, other)}
    first = facts[original_subjects[first_key]]
    other = facts[original_subjects[other_key]]
    original_amount = first["data"]["amount_fen"]
    original_by_key = {row["key"]: row for row in original_rows}
    original_identities = {
        row["key"]: (original_subjects[row["key"]], row["amount_fen"]) for row in original_rows
    }
    original_funds = {
        row["subject_id"]: row for row in page(case)["data"]["collections"]["movements"]["items"]
    }
    original_vouchers = vouchers(case)["items"]

    def formal_references():
        return {
            (result["reference"]["business_id"], result["reference"]["result_slot"]): result[
                "reference"
            ]
            for item in case.get(first["business_id"], other["business_id"])["businesses"]
            for result in item["results"]
        }

    references = formal_references()
    original = case.kernel.ledger.get_adopted_contents
    requested = []

    def exact(refs, read):
        requested.extend(ref["business_id"] for ref in refs)
        return original(refs, read)

    for seq, name in enumerate(("公司备用钱包", "公司备用平台", None), 1):
        if name is None:
            first["data"].pop("own_funds_channel")
        else:
            first["data"]["own_funds_channel"] = {
                "name": name,
                "source": {"source_id": case.source_id},
            }
        saved = case.app.accounting_apply(
            case.request(mutations=[{"op": "replace", "expected_seq": seq, "fact": first}])
        )
        assert saved["completion"] == "complete", saved
        assert saved["accounting"] == "unchanged"
        requested.clear()
        with monkeypatch.context() as patch:
            patch.setattr(case.kernel.ledger, "get_adopted_contents", exact)
            assert case.kernel.reading.run_pending(case.company)
        assert set(requested) <= {first["business_id"]}
        expected = name + "（公司自有资金）" if name else "付款方未提供"
        current_rows = activity(case)["items"]
        current_by_key = {row["key"]: row for row in current_rows}
        assert current_by_key[first_key] == original_by_key[first_key] | {"party": expected}
        assert current_by_key[other_key] == original_by_key[other_key]
        assert {
            row["key"]: (member_scope(case, row)[0]["subject_id"], row["amount_fen"])
            for row in current_rows
        } == original_identities
        # Same date: fukuanfang precedes gongsi. Removing the channel restores
        # the original tie order; the business grouping identity never changes.
        assert [row["key"] for row in current_rows] == (
            [other_key, first_key] if name else [first_key, other_key]
        )
        assert [row["party"] for row in current_rows] == ["付款方未提供", expected]
        assert vouchers(case)["items"] == original_vouchers
        assert formal_references() == references
        funds = page(case)["data"]["collections"]["movements"]["items"]
        current_funds = next(row for row in funds if row["subject_id"] == first["business_id"])
        assert current_funds["party"] == expected
        assert (
            next(row for row in funds if row["subject_id"] == other["business_id"])
            == (original_funds[other["business_id"]])
        )
        # A fresh rebuild must agree with the incremental publication.
        capture(case)
        capture_funds(case)
        assert activity(case)["items"] == current_rows
        assert page(case)["data"]["collections"]["movements"]["items"] == funds

    first["data"].update(
        amount_fen="120",
        own_funds_channel={"name": "公司新备用渠道", "source": {"source_id": case.source_id}},
    )
    saved = case.app.accounting_apply(
        case.request(
            mutations=[{"op": "replace", "expected_seq": 4, "fact": first}],
            correction_period="2026-02",
        )
    )
    assert saved["completion"] == "complete", saved
    capture(case)
    capture(case, "2026-02")
    previous = next(row for row in activity(case)["items"] if row["key"] == first_key)
    assert previous["party"] == "付款方未提供"
    corrections = {row["amount_fen"]: row["party"] for row in activity(case, "2026-02")["items"]}
    assert corrections == {
        "-" + original_amount: "付款方未提供",
        "120": "公司新备用渠道（公司自有资金）",
    }
    periods = []
    original_capture = _brief_activity.capture

    def changed_period(reader, scope, read, **kwargs):
        periods.append(scope["period_to"])
        return original_capture(reader, scope, read, **kwargs)

    first["data"]["own_funds_channel"]["name"] = "公司当前备用渠道"
    saved = case.app.accounting_apply(
        case.request(mutations=[{"op": "replace", "expected_seq": 5, "fact": first}])
    )
    assert saved["completion"] == "complete", saved
    with monkeypatch.context() as patch:
        patch.setattr(_brief_activity, "capture", changed_period)
        assert case.kernel.reading.run_pending(case.company)
    assert periods == ["2026-02"]
    corrections = {row["amount_fen"]: row["party"] for row in activity(case, "2026-02")["items"]}
    assert corrections == {
        "-" + original_amount: "付款方未提供",
        "120": "公司当前备用渠道（公司自有资金）",
    }
