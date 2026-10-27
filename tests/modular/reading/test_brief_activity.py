"""Brief indexes preserve real business parts while full vouchers stay page-local."""

import json
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from jsonschema import Draft202012Validator

from ai_accounting_modular.contracts.public import loads_json
from ai_accounting_modular.reading import _brief_activity, _brief_groups, _brief_vouchers, _monthly
from ai_accounting_modular.reading import _brief_activity_index as activity_index
from ai_accounting_modular.reading._brief_activity_parts import business_parties
from ai_accounting_modular.reading._brief_activity_sources import Contents
from ai_accounting_modular.reading._publication_guard import CaptureGuard
from tests.modular.application._working_case import case as case
from tests.modular.domains.assets.test_application import request as asset_request
from tests.modular.domains.assets.test_assets import case as asset_case
from tests.modular.domains.assets.test_reading import save
from tests.modular.domains.financing._cooperation_support import (
    PROJECT,
    agency_payment,
    contract,
    operator,
)
from tests.modular.domains.financing._cooperation_support import (
    expense as project_expense,
)
from tests.modular.domains.financing._cooperation_support import (
    funding as project_funding,
)
from tests.modular.domains.financing._support import (
    BANK,
    CASH,
    COUNTERPARTY,
    OWNER,
    fact,
    payment,
    six,
)
from tests.modular.domains.operations.test_tax_lifecycle import policy as vat_policy
from tests.modular.domains.operations.test_tax_lifecycle import sale as advance_sale
from tests.modular.domains.people.test_read_remuneration import (
    add as add_payroll,
)
from tests.modular.domains.people.test_read_remuneration import (
    business_case,
    declaration,
    no_contributions,
    replacement,
)
from tests.modular.money.test_payroll import inputs as payroll_inputs

from .test_funds import apply, initialize, selection


def capture(case, month="2026-01"):
    scope = selection(case, month)
    key, component = _monthly.key(scope), "brief_activity"
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        repo = unit.repository("reading")
        repo.register(key, component, unit.head, _monthly.version(component))
        repo.activity_mark(key, unit.head, full=True)
        repo._session.execute(
            "UPDATE read_scope_head SET wanted_head=?,state='preparing',revision=revision+1 "
            "WHERE scope_key=? AND component_key=?",
            (unit.head, key, component),
        )
    with case.kernel.storage.read_unit(case.company) as read:
        guard = CaptureGuard.capture(read.repository("reading").state(key, component), read.head)
        model = _brief_activity.prepare(case.kernel.reading, scope, read)
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        repo = unit.repository("reading")
        assert repo.publish(
            key,
            component,
            unit.head,
            _monthly.version(component),
            model,
            guard=guard,
            current_head=unit.head,
        )
    return model


def snapshot(case, read, month="2026-01"):
    scope = selection(case, month)
    return _monthly.ready_snapshot(case.kernel.reading, scope, ("brief_activity",), read)


def activity(case, month="2026-01", **query):
    with case.kernel.storage.read_unit(case.company) as read:
        return _brief_groups.activity_page(snapshot(case, read, month), read, query)


def group(case, key, month="2026-01", **query):
    with case.kernel.storage.read_unit(case.company) as read:
        return _brief_groups.build(
            case.kernel.reading, snapshot(case, read, month), read, {"group_key": key, **query}
        )["data"]["collections"]


def vouchers(case, month="2026-01", **query):
    with case.kernel.storage.read_unit(case.company) as read:
        return _brief_vouchers.page(case.kernel.reading, snapshot(case, read, month), read, query)


def validate(name, value):
    definitions = json.loads(
        (Path(__file__).parents[3] / "docs/design/contracts/dashboard-response-v2.json").read_text(
            encoding="utf8"
        )
    )["dashboard_brief"]["$defs"]
    Draft202012Validator({"$defs": definitions, "$ref": "#/$defs/" + name}).validate(value)


def mixed(case):
    initialize(case)
    *_, purchase, _ = six()
    purchase["data"]["purchase_price_fen"] = "300"
    expense = case.expense("700")
    expense["data"]["creditor_id"] = COUNTERPARTY
    paid = payment(purchase, 1000, party=COUNTERPARTY, slot="primary")
    paid["data"]["allocations"] = [
        {
            "source": {"business_id": purchase["business_id"], "slot": "primary"},
            "amount_fen": "300",
        },
        {"source": {"business_id": expense["business_id"], "slot": "primary"}, "amount_fen": "700"},
    ]
    return apply(case, purchase, expense, paid)


def test_mixed_payment_is_two_real_parts_and_one_exact_voucher(case, monkeypatch):
    _, _, paid = mixed(case)
    calls = []
    original = case.kernel.ledger.get_vouchers

    def tracked(refs, read):
        calls.append(tuple(refs))
        return original(refs, read)

    monkeypatch.setattr(case.kernel.ledger, "get_vouchers", tracked)
    capture(case)
    assert calls == []
    groups = activity(case)
    assert groups["page"]["total_count"] == 4
    for item in groups["items"]:
        validate("BriefActivityDisplayGroup", item)
    paid_groups = [item for item in groups["items"] if item["group"] == "cash"]
    assert {(item["title"], item["amount_fen"]) for item in paid_groups} == {
        ("支付货币基金申购", "300"),
        ("支付管理费用", "700"),
    }
    pairs = [group(case, item["key"]) for item in paid_groups]
    assert len(calls) == 2 and all(len(call) == 1 for call in calls)
    tokens = set()
    for collections in pairs:
        member = collections["members"]["items"][0]
        actual = collections["vouchers"]["items"][0]
        validate("BriefActivityRow", member)
        validate("OwnerBriefVoucher", actual)
        assert member["subject_id"] == paid["business_id"]
        assert member["detail_scope_key"] == member["key"]
        assert member["voucher_version_id"] == actual["voucher_version_id"]
        assert actual["business_amount_fen"] == "1000" and len(actual["lines"]) == 3
        tokens.add(actual["voucher_version_id"])
    assert len(tokens) == 1
    with case.kernel.storage.read_unit(case.company) as read:
        saved = snapshot(case, read).components["brief_activity"]
        assert (saved["activity_count"], saved["voucher_count"], saved["group_count"]) == (4, 3, 4)
    independent = vouchers(case)
    assert independent["page"]["total_count"] == 3
    assert all(item["business_amount_fen"] is not None for item in independent["items"])


def test_closed_correction_uses_old_and_new_exact_type_and_date(case):
    initialize(case)
    expense = case.expense("100")
    (expense,) = apply(case, expense)
    checked = case.check()
    assert checked["ready"], checked
    assert case.close(checked)["state"] == "closed"
    with case.kernel.storage.read_unit(case.company) as read:
        before = _brief_activity.prepare(case.kernel.reading, selection(case), read)
    expense["data"].update(amount_fen="80", expense_class="sales")
    result = case.app.accounting_apply(
        case.request(
            correction_period="2026-02",
            mutations=[{"op": "replace", "expected_seq": 1, "fact": expense}],
        )
    )
    assert result["completion"] == "complete", json.dumps(result, ensure_ascii=False)
    with case.kernel.storage.read_unit(case.company) as read:
        after = _brief_activity.prepare(case.kernel.reading, selection(case), read)

        def visible(model):
            result = []
            for row in model.rows:
                if row[0] not in activity_index.PUBLIC:
                    continue
                value = loads_json(row[-1])["value"]
                result.append((row[:7], value["row"] if row[0] == "activity_members" else value))
            return result

        assert visible(after) == visible(before)
        # The historical display is unchanged, while its now obsolete subscription
        # to the current result's prose must leave the rebuildable private index.
        assert before.inputs.note_scopes == ("business:" + expense["business_id"],)
        assert after.inputs.note_scopes == ()
    capture(case, "2026-02")
    groups = activity(case, "2026-02")["items"]
    assert {(item["title"], item["amount_fen"]) for item in groups} == {
        ("冲正原业务 · 管理费用", "-100"),
        ("更正原业务 · 销售费用", "80"),
    }
    actual = vouchers(case, "2026-02")["items"]
    assert len(actual) == 2
    assert all(
        item["recognition"]["period"] == "2026-01" and item["date"] is None for item in actual
    )
    assert next(item for item in actual if item["state"] == "冲正")["reverses_version_id"]
    for item in actual:
        validate("OwnerBriefVoucher", item)


def test_one_original_bank_batch_keeps_two_people_in_one_purpose_scope(case):
    initialize(case)
    persons = [str(uuid4()), str(uuid4())]
    labor = [
        fact(
            "people.remuneration",
            {
                "type": "labor",
                "person_id": person,
                "gross_fen": value,
                "destination": {"type": "expense", "area": "administration"},
                "withholding_state": "not_withheld_not_filed",
            },
        )
        for person, value in zip(persons, ("300", "700"), strict=True)
    ]
    batch = payment(labor[0], 1000, slot="net_pay")
    batch["data"].update(
        payment_method="bank_batch",
        allocations=[
            {
                "source": {"business_id": item["business_id"], "slot": "net_pay"},
                "amount_fen": value,
                "recipient_id": person,
            }
            for item, person, value in zip(labor, persons, ("300", "700"), strict=True)
        ],
    )
    batch["data"].pop("counterparty_id", None)
    apply(
        case,
        *labor,
        batch,
        objects=[
            {
                "object_id": person,
                "kind": "person",
                "name": "合成劳务" + str(index),
                "expected_seq": 0,
                "data": {},
            }
            for index, person in enumerate(persons)
        ],
    )
    capture(case)
    cash = [item for item in activity(case)["items"] if item["group"] == "cash"]
    assert len(cash) == 1 and cash[0]["is_batch"]
    assert cash[0]["member_count"] == cash[0]["voucher_count"] == 1
    assert (cash[0]["title"], cash[0]["amount_fen"]) == ("支付劳务报酬", "1000")
    member = group(case, cash[0]["key"])["members"]["items"][0]
    with case.kernel.storage.read_unit(case.company) as read:
        scope = _brief_groups.member_scope(snapshot(case, read), read, member["key"])["scope"]
    assert {part["person_id"] for part in scope["allocations"]} == set(persons)
    assert {part["amount_fen"] for part in scope["allocations"]} == {"300", "700"}
    with case.kernel.storage.read_unit(case.company) as read:
        before = snapshot(case, read).version
    old_pair_version = group(case, cash[0]["key"])["vouchers"]["page"]["collection_version"]
    apply(
        case,
        objects=[
            {
                "object_id": persons[0],
                "kind": "person",
                "name": "改名后的合成劳务",
                "expected_seq": 1,
                "data": {},
            }
        ],
    )
    capture(case)
    with case.kernel.storage.read_unit(case.company) as read:
        assert snapshot(case, read).version != before
    updated = group(case, cash[0]["key"])
    assert updated["vouchers"]["page"]["collection_version"] != old_pair_version
    assert any(
        line["party"] == "改名后的合成劳务"
        for item in updated["vouchers"]["items"]
        for line in item["lines"]
    )


def test_social_components_stay_separate_and_wage_is_gross_not_debit_total(case):
    case.seed(expense=False)
    call, gross, wage = payroll_inputs(case.company, pay=False)
    result = case.app.accounting_apply(call)
    assert result["completion"] == "complete", result
    paid = fact(
        "money.movement",
        {
            "type": "settlement",
            "funds_account_id": gross["data"]["funds_account_id"],
            "direction": "outflow",
            "amount_fen": "300000",
            "payment_method": "individual",
            "allocations": [
                {"source": {"business_id": wage["business_id"], "slot": slot}, "amount_fen": amount}
                for slot, amount in (("employee_social", "100000"), ("employer_social", "200000"))
            ],
        },
        actual="2026-01-20",
    )
    apply(case, paid)
    capture(case)
    cash = [item for item in activity(case)["items"] if item["group"] == "cash"]
    assert {(item["title"], item["amount_fen"]) for item in cash} == {
        ("支付个人承担社保费", "100000"),
        ("支付单位承担社保费", "200000"),
    }
    for item in cash:
        member = group(case, item["key"])["members"]["items"][0]
        with case.kernel.storage.read_unit(case.company) as read:
            scope = _brief_groups.member_scope(snapshot(case, read), read, member["key"])["scope"]
        assert len(scope["allocations"]) == 1
        actual = scope["allocations"][0]
        assert actual["person_id"] == wage["data"]["person_id"]
        assert actual["payroll_period"] == "2026-01"
        assert actual["component"] == (
            "employee_social" if "个人" in item["title"] else "employer_social"
        )
    wage_voucher = next(
        item for item in vouchers(case)["items"] if item["kind"] == "people.remuneration"
    )
    assert wage_voucher["business_amount_fen"] == "1000000"
    assert wage_voucher["amount_fen"] == "1250000"
    validate("OwnerBriefVoucher", wage_voucher)


def test_real_consumption_has_its_own_basis_without_a_fictitious_input(case):
    case.seed(expense=False)
    save(case.app, asset_request(case.company, asset_case("B20"), asset_case("B21")))
    # Advancing genuine work creates the normal monthly consumption slots.
    apply(case, case.expense("1", period="2026-02"))
    capture(case, "2026-02")
    actual = [
        item for item in vouchers(case, "2026-02")["items"] if item["kind"] == "assets.consumption"
    ]
    assert len(actual) == 1
    assert actual[0]["type"] == "资产折旧与摊销"
    assert actual[0]["business_amount_fen"] == "3333"
    assert actual[0]["recognition"]["period"] == "2026-02"
    assert len(actual[0]["asset_members"]) == 1
    validate("OwnerBriefVoucher", actual[0])


def test_voucher_display_is_bound_and_historical_dependency_uses_only_referenced_assets(
    case, monkeypatch
):
    case.seed(expense=False)
    acquired, activated = asset_case("B20"), asset_case("B21")
    registration = asset_request(case.company, acquired, activated)
    asset = next(item for item in registration["objects"] if item["kind"] == "asset")
    save(case.app, registration)
    assert case.close(case.check())["state"] == "closed"
    apply(case, case.expense("1", period="2026-03"))
    ref = next(
        result["reference"]
        for result in case.get(acquired["business_id"])["businesses"][0]["results"]
        if result["target"]["result_slot"] == "main"
    )
    capture(case)
    original = vouchers(case)["items"][0]
    with case.kernel.storage.read_unit(case.company) as read:
        selected = snapshot(case, read)
        old_version = selected.version
        descriptors = _monthly.collection_items(
            selected, "brief_activity", "brief_vouchers", (original["voucher_version_id"],), read
        )
        before = _brief_vouchers.display_head(case.kernel.reading, (ref,), read)
    unrelated = str(uuid4())
    apply(
        case,
        objects=[
            {
                "object_id": unrelated,
                "kind": "asset",
                "name": "无关资产",
                "expected_seq": 0,
                "data": {"asset_type": "fixed", "category": "其他设备"},
            }
        ],
    )
    with case.kernel.storage.read_unit(case.company) as read:
        assert _brief_vouchers.display_head(case.kernel.reading, (ref,), read) == before
    revised = deepcopy(asset)
    revised.update(name="资产新名称", expected_seq=1)
    revised["data"]["code"] = "设备-002"
    apply(case, objects=[revised])
    with case.kernel.storage.read_unit(case.company) as read:
        assert _brief_vouchers.display_head(case.kernel.reading, (ref,), read) != before
        with monkeypatch.context() as context:

            def unexpected(*args):
                raise AssertionError("voucher hydration reread identity")

            context.setattr(
                case.kernel.identity,
                "get_batch",
                unexpected,
            )
            assert _brief_vouchers.for_descriptors(case.kernel.reading, descriptors, read) == [
                original
            ]
    capture(case)
    updated = vouchers(case)["items"][0]
    assert updated["asset"]["name"] == "资产新名称" and updated["asset"]["code"] == "设备-002"
    with case.kernel.storage.read_unit(case.company) as read:
        assert snapshot(case, read).version != old_version
    assert updated["business_amount_fen"] == original["business_amount_fen"]


def test_evidence_only_formal_adoption_is_quiet_but_equal_reclassification_is_visible(case):
    case.seed(expense=False)
    wage = business_case("B17Z")
    add_payroll(case, no_contributions(), wage)
    assert case.close(case.check())["state"] == "closed"
    capture(case)
    first = activity(case)
    declared, reference = declaration(wage, "0")
    wage["data"]["tax_basis"]["pooled_source"] = reference
    command = replacement(case.company, wage)
    command["correction_period"] = "2026-02"
    command["mutations"].append({"op": "create", "expected_seq": 0, "fact": declared})
    result = case.app.accounting_apply(command)
    assert result["completion"] == "complete", result
    capture(case, "2026-02")
    assert activity(case, "2026-02")["page"]["total_count"] == 0
    assert vouchers(case, "2026-02")["items"] == []
    capture(case)
    assert activity(case) == first
    wage["data"]["expense_area"] = "sales"
    command = replacement(case.company, wage)
    command["correction_period"] = "2026-02"
    command["mutations"][0]["expected_seq"] = 2
    result = case.app.accounting_apply(command)
    assert result["completion"] == "complete", result
    capture(case, "2026-02")
    changed = activity(case, "2026-02")["items"]
    assert sorted(item["amount_fen"] for item in changed) == ["-100000", "100000"]
    assert all(item["group"] == "correction" for item in changed)


def test_effective_voucher_helper_preserves_ref_mapping_without_current_body_substitution(
    case, monkeypatch
):
    case.seed(expense=False)
    wage = business_case("B17Z")
    add_payroll(case, no_contributions(), wage)
    open_reference = case.get(wage["business_id"])["businesses"][0]["results"][0]["reference"]
    assert case.close(case.check())["state"] == "closed"
    old = case.get(wage["business_id"])["businesses"][0]["results"][0]["reference"]
    with case.kernel.storage.read_unit(case.company) as read:
        cache = Contents(case.kernel.ledger, read)
        cache.load((open_reference, old))
        assert cache.get(old).reference["state"] == "frozen"
    declared, reference = declaration(wage, "0")
    wage["data"]["tax_basis"]["pooled_source"] = reference
    command = replacement(case.company, wage)
    command["correction_period"] = "2026-02"
    command["mutations"].append({"op": "create", "expected_seq": 0, "fact": declared})
    result = case.app.accounting_apply(command)
    assert result["completion"] == "complete", result
    current = case.get(wage["business_id"])["businesses"][0]["results"][0]["reference"]
    assert current["result_id"] != old["result_id"]
    called = []
    original = case.kernel.ledger.get_adopted_contents

    def tracked(refs, read):
        called.extend(ref["result_id"] for ref in refs)
        return original(refs, read)

    monkeypatch.setattr(case.kernel.ledger, "get_adopted_contents", tracked)
    with case.kernel.storage.read_unit(case.company) as read:
        descriptors = _brief_vouchers.descriptors_for_references(
            case.kernel.reading, (open_reference, old, current, current), read
        )
        assert len(descriptors) == 4 and all(item == descriptors[0] for item in descriptors)
        assert descriptors[0]["locator"]["result_id"] == old["result_id"]
        assert called == [old["result_id"]]
        actual = _brief_vouchers.for_descriptors(case.kernel.reading, descriptors[:1], read)
    assert actual[0]["recognition"]["period"] == "2026-01"
    assert actual[0]["business_amount_fen"] == "100000"
    validate("OwnerBriefVoucher", actual[0])


def test_true_cooperation_events_without_vouchers_do_not_turn_project_money_into_company_cash(case):
    initialize(case)
    agreement = contract()
    agreement["data"]["contract_source"]["source_id"] = case.source_id
    agreement["data"]["company_revenue_expense_share"] = "0.1"
    source = project_expense(agreement, project="1", company="0")
    paid = agency_payment(agreement, source, project="1", company="0")
    transfer = operator(agreement, 20, into=True)
    supplied = project_funding(agreement)
    apply(
        case,
        agreement,
        supplied,
        transfer,
        source,
        paid,
        objects=[
            {
                "object_id": PROJECT,
                "kind": "investment_product",
                "name": "合成合作项目",
                "expected_seq": 0,
                "data": {"category": "cooperation"},
            }
        ],
    )
    capture(case)
    groups = activity(case)["items"]
    assert not any(
        item["kind"] in {"investment.cooperation_contract", "investment.cooperation_position"}
        for item in groups
    )
    moved = next(
        item for item in groups if item["kind"] == "investment.cooperation_operator_transfer"
    )
    assert moved["amount_fen"] is None and moved["amount_label"] == "无单一公司金额"
    assert moved["voucher_count"] == 0
    members = group(case, moved["key"])
    assert members["vouchers"]["items"] == []
    member = members["members"]["items"][0]
    assert member["voucher_number"] == 0 and member["voucher_version_id"] == ""
    validate("BriefActivityRow", member)
    agency = next(item for item in groups if item["kind"] == "investment.cooperation_payment")
    assert agency["amount_fen"] == "0" and agency["voucher_count"] == 0
    actual = vouchers(case)["items"]
    assert len(actual) == 1
    assert actual[0]["kind"] == "investment.cooperation_funding"
    assert actual[0]["date"] == "2026-01-03" and actual[0]["business_amount_fen"] == "100"


def test_same_counterparty_two_funds_are_two_matters_even_in_one_payment(case):
    initialize(case)
    *_, first, _ = six()
    first["data"]["purchase_price_fen"] = "300"
    second = deepcopy(first)
    second["business_id"] = str(uuid4())
    second["data"].update(fund_id=str(uuid4()), purchase_price_fen="700")
    paid = payment(first, 1000, slot="primary", party=COUNTERPARTY)
    paid["data"]["allocations"] = [
        {"source": {"business_id": source["business_id"], "slot": "primary"}, "amount_fen": value}
        for source, value in ((first, "300"), (second, "700"))
    ]
    apply(
        case,
        first,
        second,
        paid,
        objects=[
            {
                "object_id": second["data"]["fund_id"],
                "kind": "investment_product",
                "name": "另一合成基金",
                "expected_seq": 0,
                "data": {"category": "money_fund"},
            }
        ],
    )
    capture(case)
    actual = [item for item in activity(case)["items"] if item["group"] == "cash"]
    assert len(actual) == 2
    assert {item["amount_fen"] for item in actual} == {"300", "700"}
    matters = []
    for item in actual:
        member = group(case, item["key"])["members"]["items"][0]
        with case.kernel.storage.read_unit(case.company) as read:
            scope = _brief_groups.member_scope(snapshot(case, read), read, member["key"])["scope"]
        assert len(scope["allocations"]) == 1
        matters.append(scope["matter"])
    assert {item["fund_id"] for item in matters} == {
        first["data"]["fund_id"],
        second["data"]["fund_id"],
    }


def test_licensed_interest_and_real_fund_events_use_their_public_typed_fields(case):
    initialize(case)
    transfer = fact(
        "money.transfer",
        {"type": "physical", "from_account_id": BANK, "to_account_id": CASH, "amount_fen": "11"},
        actual="2026-01-18",
    )
    reserve = fact(
        "money.movement",
        {
            "type": "managed_reserve",
            "funds_account_id": BANK,
            "direction": "outflow",
            "amount_fen": "19",
        },
        actual="2026-01-19",
    )
    apply(case, *six(), transfer, reserve)
    capture(case)
    actual = activity(case)["items"]
    interest = next(item for item in actual if item["kind"] == "financing.interest_period")
    assert interest["amount_fen"] == "3100" and interest["title"] == "借款利息"
    assert not any(item["kind"] == "financing.loan_contract" for item in actual)
    cash = [item for item in actual if item["group"] == "cash"]
    assert {(item["title"], item["amount_fen"]) for item in cash} == {
        ("公司内部划转", "11"),
        ("支付代管备用金费用", "19"),
    }
    for item in vouchers(case)["items"]:
        validate("OwnerBriefVoucher", item)


def test_overpayment_reuses_its_exact_adopted_party_when_input_does_not_repeat_it(case):
    initialize(case)
    expense = case.expense("100")
    paid = payment(expense, 100, slot="primary", party=case.party)
    expense, _ = apply(case, expense, paid)
    expense["data"]["amount_fen"] = "80"
    claim = fact(
        "operations.overpayment",
        {"source": {"business_id": expense["business_id"], "slot": "primary"}, "amount_fen": "20"},
    )
    claim["evidence_ids"] = [case.source_id]
    result = case.app.accounting_apply(
        case.request(
            mutations=[
                {"op": "replace", "expected_seq": 1, "fact": expense},
                {"op": "create", "expected_seq": 0, "fact": claim},
            ]
        )
    )
    assert result["completion"] == "complete", result
    capture(case)
    actual = next(
        item for item in activity(case)["items"] if item["kind"] == "operations.overpayment"
    )
    assert actual["title"] == "多付款追收" and actual["amount_fen"] == "20"
    assert actual["party"] == "合成供应商"
    assert next(
        item for item in vouchers(case)["items"] if item["kind"] == "operations.overpayment"
    )["lines"]


def test_project_cost_binds_its_real_supplier_before_full_voucher_expansion(case, monkeypatch):
    initialize(case)
    charged = asset_case("B18")
    charged["data"]["supplier_id"] = COUNTERPARTY
    apply(
        case,
        charged,
        objects=[
            {
                "object_id": charged["data"]["project_id"],
                "kind": "project",
                "name": "合成研发项目",
                "expected_seq": 0,
                "data": {},
            }
        ],
    )
    capture(case)
    row = next(item for item in activity(case)["items"] if item["kind"] == "assets.project_cost")
    with case.kernel.storage.read_unit(case.company) as read:
        expected = case.kernel.identity.get_batch([COUNTERPARTY], read)[0]["name"]
    assert row["party"] == expected

    def no_identity(*args):
        raise AssertionError("hot voucher expansion must use its bound formal supplier display")

    monkeypatch.setattr(case.kernel.identity, "get_batch", no_identity)
    actual = vouchers(case)["items"][0]
    assert actual["business_amount_fen"] == "100000"
    assert [
        (party["id"], party["name"]) for line in actual["lines"] for party in line["parties"]
    ] == [(COUNTERPARTY, expected)]
    validate("OwnerBriefVoucher", actual)


def test_multiple_formal_party_roles_bind_deposit_cooperation_and_refund_vouchers(
    case, monkeypatch
):
    initialize(case)
    agreement = contract()
    agreement["data"]["contract_source"]["source_id"] = case.source_id
    charged = project_expense(agreement)
    agency = agency_payment(agreement, charged)
    supplied = project_funding(agreement)
    deposit = fact(
        "operations.deposit",
        {
            "type": "reimbursed",
            "counterparty_id": COUNTERPARTY,
            "employee_id": OWNER,
            "amount_fen": "50",
        },
    )
    reimbursed = payment(deposit, 50, slot="reimbursement", party=OWNER)
    passed = fact(
        "operations.pass_through",
        {"payer_id": COUNTERPARTY, "beneficiary_id": OWNER, "amount_fen": "7"},
    )
    received = payment(passed, 7, slot="collection", party=COUNTERPARTY, direction="inflow")
    returned = fact(
        "operations.pass_through_return",
        {"pass_through_id": passed["business_id"], "amount_fen": "3"},
    )
    apply(
        case,
        agreement,
        supplied,
        charged,
        agency,
        deposit,
        reimbursed,
        passed,
        received,
        returned,
        objects=[
            {
                "object_id": PROJECT,
                "kind": "investment_product",
                "name": "合成合作项目",
                "expected_seq": 0,
                "data": {"category": "cooperation"},
            }
        ],
    )
    capture(case)
    with case.kernel.storage.read_unit(case.company) as read:
        position_ref = next(
            ref
            for ref in case.kernel.ledger.get_result_refs([agreement["business_id"]], read)
            if ref["result_slot"] == "position:2026-01"
        )
        cached = Contents(case.kernel.ledger, read)
        cached.load([position_ref])
        body = cached.get(position_ref)
        assert len(body.basis["source_results"]) > 1
        loads, original = [], case.kernel.ledger.get_adopted_contents

        def requested(refs, read):
            loads.extend(ref["business_id"] for ref in refs)
            return original(refs, read)

        with monkeypatch.context() as observe:
            observe.setattr(case.kernel.ledger, "get_adopted_contents", requested)
            assert business_parties(body, cached) == (OWNER,)
            assert business_parties(body, cached) == (OWNER,)
        assert loads == [agreement["business_id"]]
    returned_row = next(
        item for item in activity(case)["items"] if item["kind"] == "operations.pass_through_return"
    )
    assert returned_row["party"]
    reimbursed_row = next(
        item
        for item in activity(case)["items"]
        if item["kind"] == "money.movement" and item["amount_fen"] == "50"
    )
    with case.kernel.storage.read_unit(case.company) as read:
        selected = group(case, reimbursed_row["key"])["members"]["items"][0]
        scope = _brief_groups.member_scope(snapshot(case, read), read, selected["key"])["scope"]
    assert scope["allocations"][0]["recipient_id"] == OWNER

    def no_identity(*args):
        raise AssertionError("all formal voucher party roles must be bound before hot expansion")

    monkeypatch.setattr(case.kernel.identity, "get_batch", no_identity)
    actual = {item["kind"]: item for item in vouchers(case)["items"]}
    for kind in (
        "operations.deposit",
        "investment.cooperation_payment",
        "operations.pass_through_return",
    ):
        value = actual[kind]
        assert {party["id"] for line in value["lines"] for party in line["parties"]} == {
            COUNTERPARTY,
            OWNER,
        }
        assert all(line["party_state"] == "known" for line in value["lines"] if line["parties"])
        validate("OwnerBriefVoucher", value)


def test_reimbursed_asset_batch_allocations_keep_each_actual_creditor(case):
    initialize(case)
    acquired = asset_case("B20R")
    result = case.app.accounting_apply(asset_request(case.company, acquired))
    assert result["completion"] == "complete", result
    payments = [
        payment(
            acquired,
            int(item["amount_fen"]),
            slot="reimbursement:" + item["person_id"],
            party=item["person_id"],
        )
        for item in acquired["data"]["creditors"]
    ]
    apply(case, *payments)
    capture(case)
    groups = [item for item in activity(case)["items"] if item["kind"] == "money.movement"]
    assert len(groups) == 2
    recipients = set()
    for row in groups:
        member = group(case, row["key"])["members"]["items"][0]
        with case.kernel.storage.read_unit(case.company) as read:
            scope = _brief_groups.member_scope(snapshot(case, read), read, member["key"])["scope"]
        recipients.add(scope["allocations"][0]["recipient_id"])
    assert recipients == {item["person_id"] for item in acquired["data"]["creditors"]}
    assert len(vouchers(case)["items"]) == 3


def test_derived_tax_release_voucher_uses_its_exact_main_party_without_a_new_sale_activity(case):
    initialize(case)
    ids = {"party": case.party, "source": case.source_id}
    rule = vat_policy(ids)
    advance = advance_sale(ids, rule, advance=True)
    apply(case, rule, advance)
    apply(case, case.expense("1", "2026-03"))
    capture(case, "2026-03")
    events = activity(case, "2026-03")["items"]
    assert [row["kind"] for row in events] == ["operations.expense"]
    released = next(
        item
        for item in vouchers(case, "2026-03")["items"]
        if item["kind"] == "operations.tax_release"
    )
    assert released["list_summary"] == "确认本月已到税点税额"
    assert released["business_amount_state"] == "known"
    assert released["business_amount_label"] == "转入应交增值税金额"
    assert released["business_amount_fen"] == "100"
    assert released["business_amount_fen"] != advance["data"]["amount_fen"]
    assert {party["id"] for line in released["lines"] for party in line["parties"]} == {case.party}
    validate("OwnerBriefVoucher", released)


def test_zero_company_real_project_amount_correction_is_visible_without_fictitious_vouchers(case):
    initialize(case)
    agreement = contract()
    agreement["data"]["contract_source"]["source_id"] = case.source_id
    agreement["data"]["company_revenue_expense_share"] = "0.1"
    charged = project_expense(agreement, project="1", company="0")
    apply(
        case,
        agreement,
        charged,
        objects=[
            {
                "object_id": PROJECT,
                "kind": "investment_product",
                "name": "合成零公司份额项目",
                "expected_seq": 0,
                "data": {"category": "cooperation"},
            }
        ],
    )
    assert case.close(case.check())["state"] == "closed"
    charged["data"]["project_total_fen"] = "2"
    charged["evidence_ids"] = [case.source_id]
    saved = case.app.accounting_apply(
        case.request(
            mutations=[{"op": "replace", "expected_seq": 1, "fact": charged}],
            correction_period="2026-02",
        )
    )
    assert saved["completion"] == "complete", saved
    capture(case, "2026-02")
    changed = activity(case, "2026-02")["items"]
    assert len(changed) == 2 and {item["kind"] for item in changed} == {"operations.expense"}
    assert {item["amount_fen"] for item in changed} == {"0"}
    assert all(item["voucher_count"] == 0 for item in changed)
    assert vouchers(case, "2026-02")["items"] == []


def test_501_group_members_and_independent_vouchers_have_separate_bounded_cursors(
    case, monkeypatch
):
    initialize(case)
    expenses = [case.expense("1") for _ in range(501)]
    apply(case, *expenses[:500])
    apply(case, expenses[-1])
    calls = []
    original = case.kernel.ledger.get_vouchers

    def tracked(refs, read):
        calls.append(len(refs))
        return original(refs, read)

    monkeypatch.setattr(case.kernel.ledger, "get_vouchers", tracked)
    capture(case)
    assert calls == []
    groups = activity(case)
    assert groups["page"]["total_count"] == 1
    assert groups["items"][0]["member_count"] == groups["items"][0]["voucher_count"] == 501
    key = groups["items"][0]["key"]

    def unexpected(*args, **kwargs):
        raise AssertionError("hot brief reread complete formal business bodies")

    monkeypatch.setattr(case.kernel.ledger, "query_adopted_values", unexpected)
    monkeypatch.setattr(case.kernel.ledger, "get_adopted_contents", unexpected)
    first = group(case, key)
    last = group(case, key, limit=500, cursor=first["members"]["page"]["next_cursor"])
    assert (len(first["members"]["items"]), len(last["members"]["items"])) == (20, 481)
    for collections in (first, last):
        assert [item["voucher_version_id"] for item in collections["members"]["items"]] == [
            item["voucher_version_id"] for item in collections["vouchers"]["items"]
        ]
    own = vouchers(case)
    tail = vouchers(case, limit=500, cursor=own["page"]["next_cursor"])
    assert own["page"]["total_count"] == tail["page"]["total_count"] == 501
    assert (len(own["items"]), len(tail["items"])) == (20, 481)
    assert calls == [20, 481, 20, 481]
