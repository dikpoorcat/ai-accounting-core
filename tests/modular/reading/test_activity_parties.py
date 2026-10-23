"""Actual payees, employee beneficiaries and batch members keep their distinct roles."""

from copy import deepcopy
from uuid import uuid4

from ai_accounting_modular.reading import _brief_groups, _business_status_selection
from tests.modular.application._working_case import case as case
from tests.modular.domains.assets.test_application import request as asset_request
from tests.modular.domains.assets.test_assets import bulk_fixed_assets
from tests.modular.domains.financing._support import BANK, CASH, fact, payment
from tests.modular.money.test_settlement_parties import payroll

from .test_brief_activity import activity, capture, group, snapshot
from .test_funds import apply, initialize, selection


def institutional_payments(case, *, explicit_recipients=True):
    case.seed(expense=False)
    call, wage, organization, person, payments = payroll(case.company)
    for item in payments:
        if explicit_recipients:
            for allocation in item["data"]["allocations"]:
                allocation["recipient_id"] = item["data"]["counterparty_id"]
        call["mutations"].append({"op": "create", "expected_seq": 0, "fact": item})
    saved = case.app.accounting_apply(call)
    assert saved["completion"] == "complete", saved
    return wage, organization, person, payments


def member_scope(case, row):
    member = group(case, row["key"])["members"]["items"][0]
    with case.kernel.storage.read_unit(case.company) as read:
        scope = _brief_groups.member_scope(snapshot(case, read), read, member["key"])["scope"]
    return member, scope


def test_social_housing_and_tax_payees_are_actual_recipients_without_rewriting_employee_dues(case):
    wage, organization, person, payments = institutional_payments(case)
    original = case.get(wage["business_id"])
    capture(case)
    rows = [row for row in activity(case)["items"] if row["group"] == "cash"]
    assert {(row["title"], row["party"], row["amount_fen"]) for row in rows} == {
        ("支付个人承担社保费", "合成征收机构", "100000"),
        ("支付单位承担社保费", "合成征收机构", "150000"),
        ("支付单位承担社保费", "合成社保结算收款人", "50000"),
        ("支付个人承担住房公积金", "合成征收机构", "50000"),
        ("支付单位承担住房公积金", "合成征收机构", "50000"),
        ("支付代扣个人所得税", "合成征收机构", "20000"),
    }
    payees = {item["business_id"]: item["data"]["counterparty_id"] for item in payments}
    for row in rows:
        member, scope = member_scope(case, row)
        assert {item["person_id"] for item in scope["allocations"]} == {wage["data"]["person_id"]}
        assert {item["recipient_id"] for item in scope["allocations"]} == {
            payees[member["subject_id"]]
        }
        with case.kernel.storage.read_unit(case.company) as read:
            selected = _business_status_selection.select(
                case.kernel.reading,
                selection(case),
                {
                    "subject_id": member["subject_id"],
                    "detail_scope_key": member["detail_scope_key"],
                    "voucher_version_id": member["voucher_version_id"],
                },
                read,
            )
            assert selected.range["allocations"] == scope["allocations"]
    assert case.get(wage["business_id"]) == original
    with case.kernel.storage.read_unit(case.company) as read:
        definitions = case.kernel.ledger.resource_definitions(
            [
                {"business_id": wage["business_id"], "slot": slot}
                for slot in (
                    "employee_social",
                    "employer_social",
                    "employee_housing",
                    "employer_housing",
                    "tax",
                )
            ],
            "obligation",
            read,
        )
    assert all("party_id" not in item.value for item in definitions)


def test_actual_payee_identity_change_updates_activity_without_changing_beneficiary(case):
    wage, organization, _, _ = institutional_payments(case)
    capture(case)
    with case.kernel.storage.read_unit(case.company) as read:
        before = snapshot(case, read).version
    changed = case.app.accounting_apply(
        case.request(
            objects=[
                {
                    "object_id": organization,
                    "kind": "organization",
                    "name": "更名征收机构",
                    "expected_seq": 1,
                    "data": {},
                }
            ]
        )
    )
    assert changed["completion"] == "complete", changed
    assert case.kernel.reading.run_pending(case.company)
    with case.kernel.storage.read_unit(case.company) as read:
        assert snapshot(case, read).version != before
    rows = [row for row in activity(case)["items"] if row["group"] == "cash"]
    assert sum(row["party"] == "更名征收机构" for row in rows) == 5
    assert all(row["party"] != "合成征收机构" for row in rows)
    for row in rows:
        _, scope = member_scope(case, row)
        assert {item["person_id"] for item in scope["allocations"]} == {wage["data"]["person_id"]}


def test_individual_mixed_known_and_unknown_sources_share_only_the_proven_single_party(case):
    case.seed(expense=False)
    call, wage, organization, _, payments = payroll(case.company)
    saved = case.app.accounting_apply(call)
    assert saved["completion"] == "complete", saved
    expense = case.expense("100")
    expense["data"]["creditor_id"] = organization
    charged = case.app.accounting_apply(case.request(expense))
    assert charged["completion"] == "complete", charged
    paid = deepcopy(payments[0])
    paid["data"].pop("counterparty_id")
    paid["data"]["amount_fen"] = "100100"
    paid["data"]["allocations"] = [
        {
            "source": {"business_id": wage["business_id"], "slot": "employee_social"},
            "amount_fen": "100000",
        },
        {"source": {"business_id": expense["business_id"], "slot": "primary"}, "amount_fen": "100"},
    ]
    saved = case.app.accounting_apply(case.request(paid))
    assert saved["completion"] == "complete", saved
    capture(case)
    rows = [row for row in activity(case)["items"] if row["group"] == "cash"]
    assert {(row["title"], row["party"], row["amount_fen"]) for row in rows} == {
        ("支付个人承担社保费", "合成征收机构", "100000"),
        ("支付管理费用", "合成征收机构", "100"),
    }


def test_bank_batch_keeps_each_actual_member_and_never_claims_one_global_payee(case):
    initialize(case)
    persons = [str(uuid4()), str(uuid4())]
    labor = [
        fact(
            "people.remuneration",
            {
                "type": "labor",
                "person_id": person,
                "gross_fen": amount,
                "destination": {"type": "expense", "area": "administration"},
                "withholding_state": "not_withheld_not_filed",
            },
        )
        for person, amount in zip(persons, ("300", "700"), strict=True)
    ]
    paid = payment(labor[0], 1000, slot="net_pay")
    paid["data"].update(
        payment_method="bank_batch",
        allocations=[
            {
                "source": {"business_id": item["business_id"], "slot": "net_pay"},
                "amount_fen": amount,
                "recipient_id": person,
            }
            for item, amount, person in zip(labor, ("300", "700"), persons, strict=True)
        ],
    )
    paid["data"].pop("counterparty_id", None)
    apply(
        case,
        *labor,
        paid,
        objects=[
            {
                "object_id": person,
                "kind": "person",
                "name": f"批次职工{index}",
                "expected_seq": 0,
                "data": {},
            }
            for index, person in enumerate(persons)
        ],
    )
    capture(case)
    (row,) = [row for row in activity(case)["items"] if row["group"] == "cash"]
    assert row["party"] == "银行批量付款" and row["is_batch"]
    _, scope = member_scope(case, row)
    assert {
        (item["person_id"], item["recipient_id"], item["amount_fen"])
        for item in scope["allocations"]
    } == {
        (persons[0], persons[0], "300"),
        (persons[1], persons[1], "700"),
    }


def test_external_missing_parties_and_internal_transfer_are_distinct_normal_displays(case):
    initialize(case)
    external = [
        fact(
            "money.movement",
            {
                "type": "managed_reserve",
                "funds_account_id": BANK,
                "direction": direction,
                "amount_fen": amount,
            },
            actual="2026-01-19",
        )
        for direction, amount in (("outflow", "19"), ("inflow", "23"))
    ]
    transfer = fact(
        "money.transfer",
        {"type": "physical", "from_account_id": BANK, "to_account_id": CASH, "amount_fen": "11"},
        actual="2026-01-19",
    )
    apply(case, *external, transfer)
    capture(case)
    rows = activity(case)["items"]
    assert {(row["party"], row["amount_fen"]) for row in rows} == {
        ("收款方未提供", "19"),
        ("付款方未提供", "23"),
        ("公司内部账户划转", "11"),
    }


def test_noncash_asset_activation_has_no_external_counterparty_to_request(case):
    case.seed(expense=False)
    acquired, enabled = bulk_fixed_assets(1)
    saved = case.app.accounting_apply(asset_request(case.company, acquired, enabled))
    assert saved["completion"] == "complete", saved
    capture(case)
    actual = next(row for row in activity(case)["items"] if row["kind"] == "assets.activation")
    assert actual["party"] == "—"
    assert actual["group"] == "assets" and actual["amount_label"] == "启用资产成本"


def test_offset_displays_the_common_creditor_without_calling_it_a_cash_recipient(case):
    expense = case.seed()
    advance = fact(
        "operations.advance",
        {
            "side": "supplier",
            "counterparty_id": case.party,
            "amount_fen": "10000",
            "purpose": "operating",
        },
    )
    offset = fact(
        "money.transfer",
        {
            "type": "offset",
            "offset_kind": "advance_application",
            "first": {
                "source": {"business_id": advance["business_id"], "slot": "advance"},
                "amount_fen": "10000",
            },
            "second": {
                "source": {"business_id": expense["business_id"], "slot": "primary"},
                "amount_fen": "10000",
            },
        },
    )
    apply(case, advance, offset)
    capture(case)
    actual = next(row for row in activity(case)["items"] if row["kind"] == "money.transfer")
    assert actual["party"] == "合成供应商"
    assert actual["title"] == "往来抵销" and actual["amount_label"] == "抵销金额"
    _, scope = member_scope(case, actual)
    assert scope["direction"] is None
    assert {item["recipient_id"] for item in scope["allocations"]} == {case.party}
