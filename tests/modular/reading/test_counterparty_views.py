"""The same actual payee survives funds, bank breakdowns and exact business progress."""

from uuid import uuid4

from tests.modular.application._working_case import case as case
from tests.modular.domains.financing._support import BANK, CASH, fact

from .test_activity_parties import institutional_payments, member_scope
from .test_brief_activity import activity, capture
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
    apply(case, *movements, transfer)
    capture_funds(case)
    rows = page(case)["data"]["collections"]["movements"]["items"]
    assert {
        (item["direction"], item["party"]) for item in rows if not item["internal_transfer"]
    } == {("inflow", "付款方未提供"), ("outflow", "收款方未提供")}
    assert [item["party"] for item in rows if item["internal_transfer"]] == [
        "公司内部账户划转",
        "公司内部账户划转",
    ]
