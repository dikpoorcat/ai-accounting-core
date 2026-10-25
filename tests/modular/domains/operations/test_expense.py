"""Operating facts use real storage and ledger without another domain's private API."""

from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import (
    ContractError,
    EvaluatedSlot,
    ResourceDefinition,
    ResourceState,
    ResourceWindow,
    validate_contract,
)
from ai_accounting_modular.domains.operations.public import create_operations, repository_factory
from ai_accounting_modular.ledger.public import create_ledger
from ai_accounting_modular.ledger.public import repository_factory as ledger_factory
from ai_accounting_modular.money.public import create_money
from ai_accounting_modular.storage.public import (
    CompanySpec,
    OwnerProfile,
    RepositoryFactory,
    Storage,
    create_catalog,
    module_schema,
)

COMPANY = str(uuid4())
BUSINESS = str(uuid4())
PARTY = str(uuid4())
BENEFICIARY = str(uuid4())
BANK = str(uuid4())
SOURCE = str(uuid4())


class SeedRepository:
    def __init__(self, session):
        self.session = session

    def business(self, business_id=BUSINESS, kind="operations.expense", period="2026-01"):
        self.session.execute(
            "INSERT INTO business_head VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                business_id,
                "operations",
                kind,
                "[]",
                None,
                1,
                "active",
                period,
                period,
                "0" * 64,
                "create",
                0,
                str(uuid4()),
                "0" * 64,
                1,
            ),
        )

    def party(self, object_id=PARTY, kind="person"):
        self.session.execute(
            "INSERT INTO identity_object VALUES(?,?,?,?,?,?,?,?,?)",
            (
                object_id,
                kind,
                "合成供应商",
                object_id,
                1,
                "{}",
                "0" * 64,
                0,
                str(uuid4()),
            ),
        )

    def object(self, object_id):
        row = self.session.fetchone(
            "SELECT object_id,kind FROM identity_object WHERE object_id=?", (object_id,)
        )
        return dict(row) if row else None

    def source(self):
        self.session.execute(
            "INSERT INTO source VALUES(?,?,?,?,?,?,?,?,?)",
            (SOURCE, "owner_statement", None, None, None, None, None, "0" * 64, "合成事实依据"),
        )


class Context:
    def __init__(self, unit, *facts):
        self.read_context = unit
        self.facts = {f["business_id"]: f for f in facts}
        self.candidate = None

    def get_fact(self, business_id):
        return self.facts.get(business_id)

    @property
    def evaluation_period_to(self):
        return max(fact["period"] for fact in self.facts.values())

    def get_object(self, object_id):
        if self.read_context is not None:
            return self.read_context.repository("identity").object(object_id)
        kinds = {PARTY: "person", BENEFICIARY: "organization", BANK: "bank_account"}
        return {"object_id": object_id, "kind": kinds[object_id]} if object_id in kinds else None

    def resolve_sources(self, ids):
        return [{"source_id": source_id} for source_id in ids]

    def get_result(self, business_id, result_slot):
        ledger = create_ledger()
        results = ledger.get_results([business_id], self.read_context)
        found = next((x for x in results if x["reference"]["result_slot"] == result_slot), None)
        if found is None:
            return None
        return EvaluatedSlot(
            ledger.get_result_source(business_id, result_slot, self.read_context),
            found["target"],
            found["basis"],
        )

    def resource_definitions(self, refs, kind):
        if self.candidate is not None:
            return self.candidate.resource_definitions(refs, kind)
        return create_ledger().resource_definitions(refs, kind, self.read_context)

    def resource_states(self, refs, kind, cutoff, *, excluding_consumer=None):
        return self.candidate.resource_states(
            refs, kind, cutoff, excluding_consumer=excluding_consumer
        )

    def related_facts(self, producer_id, relation_kind, **scope):
        return tuple(self.facts.values())

    def actual_settled_fen(self, source, cutoff):
        return create_money().actual_settled_fen(source, cutoff, self)

    def resource_windows(
        self,
        refs,
        kind,
        cutoff,
        *,
        excluding_consumer=None,
        excluding_consumers=(),
        components=None,
    ):
        return self.candidate.resource_windows(
            refs,
            kind,
            cutoff,
            excluding_consumer=excluding_consumer,
            excluding_consumers=excluding_consumers,
            components=components,
        )


def fact(amount="100000", **extra):
    return {
        "business_id": BUSINESS,
        "kind": "operations.expense",
        "period": "2026-01",
        "evidence_ids": [SOURCE],
        "data": {
            "creditor_id": PARTY,
            "creditor_kind": "supplier",
            "amount_fen": amount,
            "expense_class": "administration",
            **extra,
        },
    }


@pytest.fixture
def store(tmp_path):
    catalog = tmp_path / "catalog.sqlite3"
    create_catalog(catalog, OwnerProfile(str(uuid4()), "合成老板", "synthetic-password-hash"))
    storage = Storage(
        catalog,
        repositories=[
            repository_factory(),
            ledger_factory(),
            RepositoryFactory(module_schema("application"), SeedRepository),
            RepositoryFactory(module_schema("records"), SeedRepository),
            RepositoryFactory(module_schema("identity"), SeedRepository),
        ],
    )
    storage.create_company(
        CompanySpec(
            COMPANY,
            str(uuid4()),
            "913100000000000001",
            "合成经营测试公司",
            "2026-01",
            tmp_path / "company",
        )
    )
    with storage.write_unit(COMPANY) as unit:
        unit.repository("application").business()
        unit.repository("identity").party()
        unit.repository("identity").party(BENEFICIARY, "organization")
        unit.repository("identity").party(BANK, "bank_account")
        unit.repository("records").source()
    return storage


def apply(storage, current, *, seq=0):
    operations, ledger = create_operations(), create_ledger()
    business_id = current["business_id"]
    with storage.write_unit(COMPANY) as unit:
        if not seq and business_id != BUSINESS:
            unit.repository("application").business(business_id, current["kind"], current["period"])
        context = Context(unit, current)
        description = operations.describe_change(
            [
                {
                    "business_id": business_id,
                    "expected_seq": seq,
                    "op": "replace" if seq else "create",
                    "fact": current,
                }
            ],
            context,
        )
        context.candidate = ledger.begin_candidate(
            description["businesses"], unit, publications=description["publications"]
        )
        plan = operations.evaluate_slot(business_id, "main", context)
        change = {**description, "retained_results": [], "plans": [plan]}
        context.candidate.stage(plan)
        context.candidate.complete()
        operations.validate_candidate(change, context)
        ledger.adopt([change], unit)
        operations.apply_current(change, unit)
    return plan


def other_fact(kind, data, *, effective_on=None):
    result = {
        "business_id": str(uuid4()),
        "kind": "operations." + kind,
        "period": "2026-01",
        "evidence_ids": [SOURCE],
        "data": data,
    }
    if effective_on is not None:
        result["effective_on"] = effective_on
    return result


def test_expense_uses_real_company_transaction_and_preserves_voucher_number(store):
    original = fact(report_details=[{"code": "management_research", "amount_fen": "100000"}])
    plan = apply(store, original)
    assert [(x["account_code"], x["debit_fen"], x["credit_fen"]) for x in plan["lines"]] == [
        ("5602", "100000", "0"),
        ("2202", "0", "100000"),
    ]
    assert plan["obligations"][0]["slot"] == "primary"
    assert plan["basis"]["source_locations"] == [{"source_id": SOURCE}]
    assert plan["report_contributions"] == [
        {
            "statement": "profit",
            "line": 17,
            "detail_code": "management_research",
            "amount_fen": "100000",
        }
    ]
    with store.read_unit(COMPANY) as unit:
        before = create_ledger().get_results([BUSINESS], unit)[0]
    apply(store, fact("80000"), seq=1)
    with store.read_unit(COMPANY) as unit:
        after = create_ledger().get_results([BUSINESS], unit)[0]
        assert after["reference"]["voucher_number"] == before["reference"]["voucher_number"]
        assert create_operations().export_current([BUSINESS], unit) == (fact("80000"),)
        assert "100000" not in str(after)
        assert after["target"]["values"]["report_detail_state"] == "unestablished"


@pytest.mark.parametrize(
    "details",
    [
        [{"code": "management_research", "amount_fen": "99999"}],
        [{"code": "finance_interest", "amount_fen": "100000"}],
        [
            {"code": "management_other", "amount_fen": "50000"},
            {"code": "management_other", "amount_fen": "50000"},
        ],
    ],
)
def test_invalid_report_details_cannot_replace_existing_fact(store, details):
    apply(store, fact())
    with pytest.raises(ContractError):
        apply(store, fact(report_details=details), seq=1)
    with store.read_unit(COMPANY) as unit:
        assert create_operations().export_current([BUSINESS], unit) == (fact(),)


def test_actual_date_not_invented_and_query_is_company_scoped(store):
    apply(store, fact())
    with store.read_unit(COMPANY) as unit:
        query = {
            "company_id": COMPANY,
            "family": "operations",
            "period_from": "2026-01",
            "period_to": "2026-01",
            "limit": 100,
        }
        assert create_operations().query_current(query, unit) == (fact(),)
        with pytest.raises(ContractError):
            create_operations().query_current({**query, "company_id": str(uuid4())}, unit)
        assert "effective_on" not in create_operations().export_current([BUSINESS], unit)[0]


def test_unregistered_object_is_an_input_reference_error_without_calculating():
    current = fact()
    current["data"]["creditor_id"] = str(uuid4())
    context = Context(None, current)
    with pytest.raises(ContractError) as failure:
        create_operations().describe_change(
            [{"business_id": BUSINESS, "expected_seq": 0, "op": "create", "fact": current}],
            context,
        )
    assert failure.value.code == "invalid_input"
    assert failure.value.fact_issues[0]["field"] == "/data/creditor_id"
    assert failure.value.fact_issues[0]["code"] == "reference_not_found"
    with pytest.raises(ContractError) as failure:
        create_operations().evaluate_slot(BUSINESS, "main", context)
    assert failure.value.code == "invalid_input"


def test_facts_do_not_commit_independently(store):
    operations = create_operations()
    with pytest.raises(RuntimeError):
        with store.write_unit(COMPANY) as unit:
            current = fact()
            description = operations.describe_change(
                [{"business_id": BUSINESS, "expected_seq": 0, "op": "create", "fact": current}],
                Context(unit, current),
            )
            operations.apply_current({**description, "retained_results": [], "plans": []}, unit)
            raise RuntimeError("合成下游失败")
    with store.read_unit(COMPANY) as unit:
        assert operations.export_current([BUSINESS], unit) == ()


def test_deposit_reimbursement_does_not_erase_deposit_receivable(store):
    current = other_fact(
        "deposit",
        {
            "type": "reimbursed",
            "counterparty_id": PARTY,
            "employee_id": PARTY,
            "amount_fen": "60000",
        },
    )
    plan = apply(store, current)
    assert [(x["slot"], x["direction"], x["amount_fen"]) for x in plan["obligations"]] == [
        ("deposit", "receivable", "60000"),
        ("reimbursement", "payable", "60000"),
    ]
    assert plan["fund_movements"] == []
    with store.read_unit(COMPANY) as unit:
        views = create_ledger().resolve(
            [{"business_id": current["business_id"], "slot": "deposit"}], "obligation", unit
        )
        assert views[0]["balance"]["remaining_fen"] == "60000"


def test_acceptance_transfers_existing_debt_without_second_cost_or_fictional_date(store):
    apply(store, fact())
    current = other_fact(
        "reimbursement",
        {
            "type": "acceptance",
            "payer_id": PARTY,
            "payer_kind": "owner",
            "sources": [
                {
                    "source": {"business_id": BUSINESS, "slot": "primary"},
                    "amount_fen": "60000",
                    "recipient_id": PARTY,
                }
            ],
        },
    )
    plan = apply(store, current)
    assert [(x["account_code"], x["debit_fen"], x["credit_fen"]) for x in plan["lines"]] == [
        ("2202", "60000", "0"),
        ("2241", "0", "60000"),
    ]
    assert plan["fund_movements"] == []
    assert "effective_on" not in plan["basis"]
    assert plan["basis"]["source_results"][0]["business_id"] == BUSINESS
    with store.read_unit(COMPANY) as unit:
        old = create_ledger().resolve(
            [{"business_id": BUSINESS, "slot": "primary"}], "obligation", unit
        )[0]
        assert old["balance"]["remaining_fen"] == "40000"
    too_much = other_fact(
        "reimbursement",
        {
            "type": "acceptance",
            "payer_id": PARTY,
            "payer_kind": "owner",
            "sources": [
                {
                    "source": {"business_id": BUSINESS, "slot": "primary"},
                    "amount_fen": "40001",
                    "recipient_id": PARTY,
                }
            ],
        },
    )
    with pytest.raises(ContractError):
        apply(store, too_much)
    with store.read_unit(COMPANY) as unit:
        assert create_operations().export_current([too_much["business_id"]], unit) == ()


def test_real_personal_payment_requires_actual_day_and_original_recipient(store):
    apply(store, fact())
    current = other_fact(
        "reimbursement",
        {
            "type": "personal_payment",
            "payer_id": PARTY,
            "payer_kind": "owner",
            "amount_fen": "100000",
            "allocations": [
                {
                    "source": {"business_id": BUSINESS, "slot": "primary"},
                    "amount_fen": "100000",
                    "recipient_id": PARTY,
                }
            ],
        },
    )
    with pytest.raises(ContractError) as missing:
        apply(store, current)
    assert missing.value.code == "needs_information"
    assert missing.value.fact_issues[0]["field"] == "/effective_on"
    current["effective_on"] = "2026-01-06"
    plan = apply(store, current)
    assert plan["target"]["values"]["amount_fen"] == "100000"
    assert plan["basis"]["effective_on"] == "2026-01-06"


@pytest.mark.parametrize("side,purpose", [("supplier", "fixed_asset"), ("customer", None)])
def test_contract_advance_has_two_resources_without_inventing_payment(store, side, purpose):
    data = {"side": side, "counterparty_id": PARTY, "amount_fen": "20000"}
    if purpose:
        data["purpose"] = purpose
    else:
        data["tax_status"] = "not_due"
    plan = apply(store, other_fact("advance", data))
    assert {x["slot"] for x in plan["obligations"]} == {"primary", "advance"}
    assert plan["fund_movements"] == []
    assert plan["target"]["values"] == {
        "side": side,
        "gross_fen": "20000",
        "advance_balance_fen": "20000",
        "net_fen": "20000",
        "vat_fen": "0",
    }


def vat_context(current, *, effective_from="2026-01-01", effective_to="2026-12-31", **parameters):
    policy_id = current["data"]["vat"]["policy_id"]
    policy = {
        "business_id": policy_id,
        "kind": "taxes.vat_policy",
        "period": "2026-01",
        "evidence_ids": [SOURCE],
        "data": {
            "policy": {
                "version": "synthetic-vat-1",
                "official_url": "https://example.gov.cn/synthetic-vat-rule",
                "effective_from": effective_from,
                "effective_to": effective_to,
            },
            "nominal_rate": "0.03",
            "tax_base_rate": "0.03",
            "payable_rate": "0.03",
            "form_version": "small_taxpayer_2026",
            "threshold_fen": "0",
            "threshold_period": "month",
            "threshold_operator": "at_or_below",
            "exemption_scope": "eligible_non_excluded_sales",
            "special_invoice_may_waive_exemption": True,
            "special_invoice_may_waive_rate_reduction": True,
        },
    }
    policy["data"].update(parameters)
    validate_contract("business-facts.schema.json", "Fact", policy)
    values = {
        "policy_version": "synthetic-vat-1",
        "effective_from": effective_from,
        "effective_to": effective_to,
        "official_url": policy["data"]["policy"]["official_url"],
    }
    adopted = EvaluatedSlot(
        {
            "business_id": policy_id,
            "result_id": str(uuid4()),
            "result_slot": "main",
            "result_generation": 1,
            "content_digest": "0" * 64,
        },
        {"kind": "taxes.vat_policy", "result_slot": "main", "values": values},
        {
            "kind": "taxes.vat_policy",
            "result_slot": "main",
            "period": "2026-01",
            "input": policy["data"],
            "source_results": [],
            "adopted_quantities": [],
            "policies": [],
            "source_locations": [],
        },
    )

    class VatContext(Context):
        def get_result(self, business_id, result_slot):
            return adopted if business_id == policy_id else None

    return VatContext(None, current, policy)


def sale_fact(tax_period="2026-01"):
    return other_fact(
        "service_sale",
        {
            "customer_id": PARTY,
            "gross_fen": "103000",
            "vat": {
                "policy_id": str(uuid4()),
                "exemption_eligible": False,
                "invoice_kind": "ordinary",
                "waive_exemption": False,
                "waive_rate_reduction": True,
            },
            "tax_point": {
                "type": "explicit",
                "tax_period": tax_period,
                "source": {"source_id": SOURCE, "location": "合成税点说明"},
            },
        },
    )


def test_zero_expense_has_no_unknown_report_detail_or_remaining_cost_line():
    current = fact("0")
    plan = create_operations().evaluate_slot(BUSINESS, "main", Context(None, current))
    assert plan["target"]["values"]["report_detail_state"] == "known"
    assert plan["report_contributions"] == []
    assert plan["lines"] == []
    assert plan["obligations"][0]["amount_fen"] == "0"


def test_service_sale_splits_exact_tax_and_does_not_create_money():
    current = sale_fact()
    context = vat_context(current)
    plan = create_operations().evaluate_slot(current["business_id"], "main", context)
    assert plan["target"]["values"] == {
        "gross_fen": "103000",
        "revenue_fen": "100000",
        "vat_fen": "3000",
        "current_tax_net_fen": "100000",
        "current_tax_vat_fen": "3000",
        "deferred_vat_fen": "0",
    }
    assert [(x["account_code"], x["credit_fen"]) for x in plan["lines"]] == [
        ("1122", "0"),
        ("5001", "100000"),
        ("222101", "3000"),
    ]
    assert plan["tax_sales"][0]["net_sales_fen"] == "100000"
    assert plan["fund_movements"] == []
    assert "source" not in plan["basis"]["input"]["tax_point"]
    assert {"source_id": SOURCE, "location": "合成税点说明"} in plan["basis"]["source_locations"]


@pytest.mark.parametrize(
    "waive,gross,net,tax,base",
    [(False, "101000", "100000", "1000", "0.01"), (True, "103000", "100000", "3000", "0.03")],
)
def test_reduced_service_rate_and_explicit_waiver_keep_gross_net_tax_consistent(
    waive, gross, net, tax, base
):
    current = sale_fact()
    current["data"]["gross_fen"] = gross
    current["data"]["vat"].update(waive_rate_reduction=waive, exemption_eligible=True)
    context = vat_context(current, tax_base_rate="0.01", payable_rate="0.01")
    plan = create_operations().evaluate_slot(current["business_id"], "main", context)
    assert plan["target"]["values"]["revenue_fen"] == net
    assert plan["target"]["values"]["vat_fen"] == tax
    sale = plan["tax_sales"][0]
    assert (sale["net_sales_fen"], sale["vat_fen"]) == (net, tax)
    assert (sale["nominal_rate"], sale["tax_base_rate"], sale["payable_rate"]) == (
        "0.03",
        base,
        base,
    )
    assert sum(int(row["credit_fen"]) for row in plan["lines"]) == int(gross)
    assert all(row["account_code"] != "630101" for row in plan["lines"])


def test_future_tax_point_is_frozen_without_entering_current_tax_sales():
    current = sale_fact("2026-02")
    plan = create_operations().evaluate_slot(current["business_id"], "main", vat_context(current))
    assert plan["tax_sales"] == []
    assert plan["target"]["values"]["deferred_vat_fen"] == "3000"
    adopted = next(
        item["sale"] for item in plan["basis"]["adopted_quantities"] if item["type"] == "tax_sale"
    )
    assert adopted["tax_period"] == "2026-02" and adopted["vat_fen"] == "3000"


def test_same_month_tax_point_requires_enough_real_policy_precision():
    current = sale_fact()
    with pytest.raises(ContractError) as missing:
        create_operations().evaluate_slot(
            current["business_id"], "main", vat_context(current, effective_from="2026-01-10")
        )
    assert missing.value.code == "needs_information"
    assert missing.value.fact_issues[0]["field"] == "/data/tax_point/tax_on"


def test_pass_through_is_not_income_and_refund_cannot_exceed_actual_receipts(store):
    current = other_fact(
        "pass_through", {"payer_id": PARTY, "beneficiary_id": BENEFICIARY, "amount_fen": "100000"}
    )
    plan = apply(store, current)
    assert {x["account_code"] for x in plan["lines"]} == {"122105", "224105"}
    assert {x["slot"] for x in plan["obligations"]} == {"collection", "remittance"}
    assert plan["fund_movements"] == []
    returned = other_fact(
        "pass_through_return", {"pass_through_id": current["business_id"], "amount_fen": "1"}
    )
    with pytest.raises(ContractError) as invalid:
        apply(store, returned)
    assert invalid.value.code == "dependency_conflict"
    assert "operations.pass_through_return" in create_operations().supported_kinds


def test_management_date_changes_fact_without_changing_adopted_expense_basis(store):
    first = fact()
    first["effective_on"] = "2026-01-06"
    a = apply(store, first)
    second = fact()
    second["effective_on"] = "2026-01-20"
    b = apply(store, second, seq=1)
    assert "effective_on" not in a["basis"]
    assert a["basis"] == b["basis"]
    with store.read_unit(COMPANY) as unit:
        assert (
            create_operations().export_current([BUSINESS], unit)[0]["effective_on"] == "2026-01-20"
        )


@pytest.mark.parametrize("creditor_kind", ["supplier", "employee", "individual"])
def test_company_bank_account_cannot_be_a_creditor(store, creditor_kind):
    current = fact(creditor_id=BANK, creditor_kind=creditor_kind)
    with pytest.raises(ContractError) as rejected:
        apply(store, current)
    assert rejected.value.code == "invalid_input"
    assert rejected.value.fact_issues[0]["field"] == "/data/creditor_id"
    assert rejected.value.fact_issues[0]["code"] == "incompatible_object_kind"
    with store.read_unit(COMPANY) as unit:
        assert create_operations().export_current([BUSINESS], unit) == ()
        assert create_ledger().get_results([BUSINESS], unit) == ()


@pytest.mark.parametrize("creditor_kind", ["employee", "individual"])
def test_person_creditor_roles_cannot_use_organization(store, creditor_kind):
    with pytest.raises(ContractError) as rejected:
        apply(store, fact(creditor_id=BENEFICIARY, creditor_kind=creditor_kind))
    assert rejected.value.code == "invalid_input"


@pytest.mark.parametrize("creditor_id", [PARTY, BENEFICIARY])
def test_supplier_can_be_person_or_organization(store, creditor_id):
    plan = apply(store, fact(creditor_id=creditor_id))
    assert plan["obligations"][0]["party_id"] == creditor_id


@pytest.mark.parametrize("creditor_kind,account", [("employee", "224101"), ("individual", "2241")])
def test_person_creditor_roles_keep_supported_payable_accounts(store, creditor_kind, account):
    plan = apply(store, fact(creditor_kind=creditor_kind))
    assert plan["obligations"][0]["account_code"] == account


@pytest.mark.parametrize(
    "kind,data,field",
    [
        (
            "advance",
            {
                "side": "supplier",
                "counterparty_id": BANK,
                "amount_fen": "20000",
                "purpose": "operating",
            },
            "/data/counterparty_id",
        ),
        (
            "deposit",
            {
                "type": "reimbursed",
                "counterparty_id": PARTY,
                "employee_id": BANK,
                "amount_fen": "20000",
            },
            "/data/employee_id",
        ),
        (
            "pass_through",
            {"payer_id": BANK, "beneficiary_id": BENEFICIARY, "amount_fen": "20000"},
            "/data/payer_id",
        ),
        (
            "pass_through",
            {"payer_id": PARTY, "beneficiary_id": BANK, "amount_fen": "20000"},
            "/data/beneficiary_id",
        ),
        (
            "reimbursement",
            {
                "type": "acceptance",
                "payer_id": BANK,
                "payer_kind": "owner",
                "sources": [
                    {
                        "source": {"business_id": BUSINESS, "slot": "primary"},
                        "amount_fen": "20000",
                        "recipient_id": PARTY,
                    }
                ],
            },
            "/data/payer_id",
        ),
        (
            "reimbursement",
            {
                "type": "acceptance",
                "payer_id": PARTY,
                "payer_kind": "employee",
                "sources": [
                    {
                        "source": {"business_id": BUSINESS, "slot": "primary"},
                        "amount_fen": "20000",
                        "recipient_id": BANK,
                    }
                ],
            },
            "/data/sources/0/recipient_id",
        ),
    ],
)
def test_shared_role_checks_cover_all_non_tax_operating_paths(store, kind, data, field):
    current = other_fact(kind, data)
    with store.read_unit(COMPANY) as unit:
        context = Context(unit, current)
        with pytest.raises(ContractError) as rejected:
            create_operations().describe_change(
                [{"op": "create", "expected_seq": 0, "fact": current}], context
            )
        assert rejected.value.code == "invalid_input"
        assert rejected.value.fact_issues[0]["field"] == field
        with pytest.raises(ContractError) as rejected:
            create_operations().evaluate_slot(current["business_id"], "main", context)
        assert rejected.value.code == "invalid_input"
        assert rejected.value.fact_issues[0]["field"] == field


def test_service_customer_must_be_a_party_not_company_account(store):
    current = sale_fact()
    current["data"]["customer_id"] = BANK
    with pytest.raises(ContractError) as rejected:
        apply(store, current)
    assert rejected.value.fact_issues[0]["field"] == "/data/customer_id"


def test_same_party_in_multiple_roles_does_not_duplicate_identity_dependency(store):
    current = other_fact(
        "pass_through", {"payer_id": PARTY, "beneficiary_id": PARTY, "amount_fen": "20000"}
    )
    with store.read_unit(COMPANY) as unit:
        description = create_operations().describe_change(
            [{"op": "create", "expected_seq": 0, "fact": current}], Context(unit, current)
        )
        assert len(description["dependencies"]) == 1
        assert description["issues"] == []
    apply(store, current)


@pytest.mark.parametrize("field", ["waive_exemption", "waive_rate_reduction"])
def test_special_invoice_cannot_override_policy_waiver_switch(store, field):
    current = sale_fact()
    current["data"]["vat"].update(
        invoice_kind="special", waive_exemption=False, waive_rate_reduction=False
    )
    current["data"]["vat"][field] = True
    with pytest.raises(ContractError) as rejected:
        with store.write_unit(COMPANY) as unit:
            context = vat_context(current, **{"special_invoice_may_" + field: False})
            context.read_context = unit
            create_operations().evaluate_slot(current["business_id"], "main", context)
    assert rejected.value.code == "unsupported_policy"
    with store.read_unit(COMPANY) as unit:
        assert create_ledger().get_results([current["business_id"]], unit) == ()


def test_policy_waiver_switches_do_not_block_permitted_special_or_ordinary_sale():
    current = sale_fact()
    current["data"]["vat"].update(
        invoice_kind="special", waive_exemption=True, waive_rate_reduction=True
    )
    plan = create_operations().evaluate_slot(current["business_id"], "main", vat_context(current))
    assert plan["target"]["values"]["vat_fen"] == "3000"
    current["data"]["vat"]["invoice_kind"] = "ordinary"
    plan = create_operations().evaluate_slot(
        current["business_id"],
        "main",
        vat_context(
            current,
            special_invoice_may_waive_exemption=False,
            special_invoice_may_waive_rate_reduction=False,
        ),
    )
    assert plan["target"]["values"]["vat_fen"] == "3000"


def test_future_tax_point_missing_policy_cannot_leave_partial_fact_or_voucher(store):
    current = sale_fact("2026-02")
    with pytest.raises(ContractError) as rejected:
        apply(store, current)
    assert rejected.value.code == "invalid_input"
    assert rejected.value.fact_issues[0]["code"] == "reference_not_found"
    assert rejected.value.fact_issues[0]["field"] == "/data/vat/policy_id"
    with store.read_unit(COMPANY) as unit:
        assert create_operations().export_current([current["business_id"]], unit) == ()
        assert create_ledger().get_results([current["business_id"]], unit) == ()


@pytest.mark.parametrize(
    "kind,data",
    [
        (
            "overpayment",
            {
                "source": {"business_id": BUSINESS, "slot": "primary"},
                "counterparty_id": PARTY,
                "amount_fen": "20000",
            },
        ),
        ("pass_through_return", {"pass_through_id": BUSINESS, "amount_fen": "20000"}),
    ],
)
def test_real_money_balance_paths_require_valid_original_definitions(store, kind, data):
    current = other_fact(kind, data)
    validate_contract("business-facts.schema.json", "Fact", current)
    operations = create_operations()
    assert current["kind"] in operations.supported_kinds
    with store.read_unit(COMPANY) as unit:
        context = Context(unit, current)
        description = operations.describe_change(
            [{"op": "create", "expected_seq": 0, "fact": current}], context
        )
        assert description["businesses"][0]["complete_result_slots"] == ["main"]
        with pytest.raises(ContractError) as evaluation_error:
            operations.evaluate_slot(current["business_id"], "main", context)
        assert evaluation_error.value.to_response()["code"] == "dependency_conflict"
    with pytest.raises(ContractError) as submit_error:
        apply(store, current)
    assert submit_error.value.to_response()["code"] == "dependency_conflict"
    with store.read_unit(COMPANY) as unit:
        assert operations.export_current([current["business_id"]], unit) == ()
        assert create_ledger().get_results([current["business_id"]], unit) == ()


def test_personal_reimbursement_rejects_wrong_actual_month_and_future_original_debt(store):
    source = other_fact("expense", fact()["data"])
    source["period"] = "2026-02"
    apply(store, source)
    current = other_fact(
        "reimbursement",
        {
            "type": "personal_payment",
            "payer_id": PARTY,
            "payer_kind": "owner",
            "amount_fen": "20000",
            "allocations": [
                {
                    "source": {"business_id": source["business_id"], "slot": "primary"},
                    "amount_fen": "20000",
                    "recipient_id": PARTY,
                }
            ],
        },
        effective_on="2026-02-06",
    )
    with pytest.raises(ContractError) as wrong_actual:
        apply(store, current)
    assert wrong_actual.value.code == "invalid_input"
    current["effective_on"] = "2026-01-06"
    with pytest.raises(ContractError) as future_source:
        apply(store, current)
    assert future_source.value.code == "dependency_conflict"
    with store.read_unit(COMPANY) as unit:
        assert create_operations().export_current([current["business_id"]], unit) == ()


def test_not_due_optional_vat_is_validated_as_future_information_without_adoption():
    policy_fields = sale_fact()["data"]["vat"]
    current = other_fact(
        "advance",
        {
            "side": "customer",
            "counterparty_id": PARTY,
            "amount_fen": "103000",
            "tax_status": "not_due",
            "vat": policy_fields,
        },
    )
    context = vat_context(current, effective_from="2026-06-10")
    description = create_operations().describe_change(
        [{"op": "create", "expected_seq": 0, "fact": current}], context
    )
    policy_edge = next(
        edge
        for edge in description["dependencies"]
        if edge["producer_id"] == policy_fields["policy_id"]
    )
    assert policy_edge["read_mode"] == "fact" and policy_edge["relation_kind"] == "policy"
    plan = create_operations().evaluate_slot(current["business_id"], "main", context)
    assert plan["target"]["values"]["vat_fen"] == "0"
    assert plan["basis"]["input"] == {
        "side": "customer",
        "counterparty_id": PARTY,
        "amount_fen": "103000",
        "tax_status": "not_due",
    }
    assert plan["basis"]["policies"] == [] and plan["basis"]["source_results"] == []
    assert "vat" in current["data"]
    assert "tax_point" not in current["data"]


@pytest.mark.parametrize("kind", ["service_sale", "advance"])
def test_already_known_tax_day_outside_policy_is_not_asked_again(kind):
    current = sale_fact()
    current["data"]["tax_point"]["tax_on"] = "2026-01-05"
    if kind == "advance":
        data = current["data"]
        current["kind"] = "operations.advance"
        current["data"] = {
            "side": "customer",
            "counterparty_id": PARTY,
            "amount_fen": data["gross_fen"],
            "tax_status": "due",
            "vat": data["vat"],
            "tax_point": data["tax_point"],
        }
    with pytest.raises(ContractError) as rejected:
        create_operations().evaluate_slot(
            current["business_id"], "main", vat_context(current, effective_from="2026-01-10")
        )
    assert rejected.value.code == "unsupported_policy"
    assert rejected.value.fact_issues[0]["field"] == "/data/vat/policy_id"
    assert rejected.value.fact_issues[0]["code"] == "policy_not_effective"
    current["data"]["tax_point"]["tax_on"] = "2026-01-15"
    plan = create_operations().evaluate_slot(
        current["business_id"], "main", vat_context(current, effective_from="2026-01-10")
    )
    assert plan["target"]["values"]["vat_fen"] == "3000"


def test_complete_report_details_sum_overflow_is_structured():
    current = fact(
        str(2**63 - 1),
        report_details=[
            {"code": "management_startup", "amount_fen": str(2**63 - 1)},
            {"code": "management_other", "amount_fen": "1"},
        ],
    )
    validate_contract("business-facts.schema.json", "Fact", current)
    with pytest.raises(ContractError) as rejected:
        create_operations().evaluate_slot(BUSINESS, "main", Context(None, current))
    assert rejected.value.code == "invalid_input"


class LifecycleContext(Context):
    """A strict original result plus its actual public resource definitions."""

    def __init__(self, original, plan, *facts, policy_context=None):
        super().__init__(None, original, *facts)
        self.original, self.plan, self.policy_context = original, plan, policy_context
        self.source = {
            "business_id": original["business_id"],
            "result_id": str(uuid4()),
            "result_slot": "main",
            "result_generation": 1,
            "content_digest": "0" * 64,
        }

    def get_fact(self, business_id):
        return super().get_fact(business_id) or (
            self.policy_context.get_fact(business_id) if self.policy_context else None
        )

    def get_result(self, business_id, result_slot):
        if business_id == self.original["business_id"]:
            return EvaluatedSlot(self.source, self.plan["target"], self.plan["basis"])
        return (
            self.policy_context.get_result(business_id, result_slot)
            if self.policy_context
            else None
        )

    def resource_definitions(self, refs, kind):
        return tuple(
            ResourceDefinition(kind, ref, self.source, value)
            for ref in refs
            for value in self.plan["obligations"]
            if ref == {"business_id": value["business_id"], "slot": value["slot"]}
        )

    def resource_windows(
        self,
        refs,
        kind,
        cutoff,
        *,
        excluding_consumer=None,
        excluding_consumers=(),
        components=None,
    ):
        return tuple(
            ResourceWindow(
                ResourceState(
                    definition,
                    money_settled_fen=(
                        paid := self.actual_settled_fen(definition.reference, cutoff)
                    ),
                    remaining_fen=int(definition.value["amount_fen"]) - paid,
                    components=components,
                )
            )
            for definition in self.resource_definitions(refs, kind)
        )


def customer_advance(*, taxed=True, total="103", known_vat=True):
    sale = sale_fact()
    tax_context = vat_context(sale)
    value = other_fact(
        "advance",
        {
            "side": "customer",
            "counterparty_id": PARTY,
            "amount_fen": total,
            "tax_status": "due" if taxed else "not_due",
        },
    )
    if known_vat:
        value["data"]["vat"] = sale["data"]["vat"]
    if taxed:
        value["data"]["tax_point"] = sale["data"]["tax_point"]
    tax_context.facts[value["business_id"]] = value
    plan = create_operations().evaluate_slot(value["business_id"], "main", tax_context)
    return value, plan, tax_context


def advance_use(original, kind, gross, *, month="2026-02"):
    current = other_fact(kind, {"advance_id": original["business_id"], "gross_fen": str(gross)})
    current["period"] = month
    return current


def test_taxed_advance_fulfillment_and_refund_split_once_and_exhaust_original_values():
    original, original_plan, policy = customer_advance()
    first = advance_use(original, "advance_fulfillment", 17)
    second = advance_use(original, "advance_refund", 31)
    third = advance_use(original, "advance_fulfillment", 55, month="2026-03")
    context = LifecycleContext(original, original_plan, third, first, second, policy_context=policy)
    plans = [
        create_operations().evaluate_slot(item["business_id"], "main", context)
        for item in (first, second, third)
    ]
    assert (
        sum(
            int(
                plan["target"]["values"].get(
                    "revenue_fen", plan["target"]["values"].get("net_reversed_fen")
                )
            )
            for plan in plans
        )
        == 100
    )
    assert (
        sum(
            int(
                plan["target"]["values"].get(
                    "vat_fen", plan["target"]["values"].get("vat_reversed_fen")
                )
            )
            for plan in plans
        )
        == 3
    )
    assert plans[0]["tax_sales"] == plans[2]["tax_sales"] == []
    negative = plans[1]["tax_sales"][0]
    assert (
        negative["tax_period"] == "2026-02"
        and negative["source_business_id"] == second["business_id"]
    )
    assert int(negative["vat_fen"]) <= 0
    for plan in plans:
        assert sum(int(row["debit_fen"]) - int(row["credit_fen"]) for row in plan["lines"]) == 0
        adopted = next(
            item for item in plan["basis"]["adopted_quantities"] if item["type"] == "tax_sale"
        )
        assert adopted["sale"] == original_plan["tax_sales"][0]


def test_untaxed_fulfillment_reuses_known_vat_but_requires_real_current_tax_point():
    original, original_plan, policy = customer_advance(taxed=False)
    first = advance_use(original, "advance_fulfillment", 17)
    returned = advance_use(original, "advance_refund", 31)
    last = advance_use(original, "advance_fulfillment", 55, month="2026-03")
    for current in (first, last):
        current["data"]["tax_point"] = {
            "type": "explicit",
            "tax_period": current["period"],
            "source": {"source_id": SOURCE},
        }
    context = LifecycleContext(
        original, original_plan, returned, last, first, policy_context=policy
    )
    plans = [
        create_operations().evaluate_slot(item["business_id"], "main", context)
        for item in (first, returned, last)
    ]
    assert plans[1]["tax_sales"] == []
    assert sum(int(plan["target"]["values"].get("revenue_fen", "0")) for plan in plans) == 70
    assert sum(int(plan["target"]["values"].get("vat_fen", "0")) for plan in plans) == 2
    assert plans[0]["basis"]["input"]["vat"] == original["data"]["vat"]
    assert "source" not in plans[0]["basis"]["input"]["tax_point"]
    del first["data"]["tax_point"]
    with pytest.raises(ContractError) as failed:
        create_operations().evaluate_slot(first["business_id"], "main", context)
    assert failed.value.code == "needs_information"
    assert [item["field"] for item in failed.value.fact_issues] == ["/data/tax_point"]


def test_advance_cumulative_capacity_and_inherited_optional_facts_are_checked():
    original, original_plan, policy = customer_advance()
    first = advance_use(original, "advance_fulfillment", 80)
    second = advance_use(original, "advance_refund", 24)
    context = LifecycleContext(original, original_plan, first, second, policy_context=policy)
    with pytest.raises(ContractError) as failed:
        create_operations().evaluate_slot(first["business_id"], "main", context)
    assert failed.value.code == "dependency_conflict"
    context.facts.pop(second["business_id"])
    first["data"]["tax_point"] = {
        "type": "explicit",
        "tax_period": "2026-02",
        "source": {"source_id": SOURCE},
    }
    with pytest.raises(ContractError) as failed:
        create_operations().evaluate_slot(first["business_id"], "main", context)
    assert failed.value.code == "invalid_input"


def test_sale_return_adopts_original_tax_and_checks_complete_group():
    original = sale_fact()
    policy = vat_context(original)
    original_plan = create_operations().evaluate_slot(original["business_id"], "main", policy)
    first = other_fact(
        "sale_return",
        {
            "sale_id": original["business_id"],
            "customer_id": PARTY,
            "gross_fen": "33000",
            "credit_note_vat_fen": "1000",
        },
    )
    second = other_fact(
        "sale_return",
        {
            "sale_id": original["business_id"],
            "customer_id": PARTY,
            "gross_fen": "70000",
            "credit_note_vat_fen": "2000",
        },
    )
    first["period"], second["period"] = "2026-02", "2026-03"
    context = LifecycleContext(original, original_plan, second, first, policy_context=policy)
    for current in (first, second):
        plan = create_operations().evaluate_slot(current["business_id"], "main", context)
        assert plan["tax_sales"][0]["tax_period"] == current["period"]
        assert plan["tax_sales"][0]["source_business_id"] == current["business_id"]
        assert plan["tax_sales"][0]["nominal_rate"] == "0.03"
        assert (
            next(
                item["sale"]
                for item in plan["basis"]["adopted_quantities"]
                if item["type"] == "tax_sale"
            )
            == original_plan["tax_sales"][0]
        )
    second["data"]["credit_note_vat_fen"] = "1999"
    with pytest.raises(ContractError) as failed:
        create_operations().evaluate_slot(first["business_id"], "main", context)
    assert failed.value.code == "dependency_conflict"


def test_expense_detail_recovery_uses_monotone_cumulative_shares():
    original = fact(
        "7",
        report_details=[
            {"code": "management_startup", "amount_fen": "3"},
            {"code": "management_research", "amount_fen": "4"},
        ],
    )
    original_plan = create_operations().evaluate_slot(BUSINESS, "main", Context(None, original))
    returns = [
        other_fact(
            "expense_recovery",
            {"expense_id": BUSINESS, "counterparty_id": PARTY, "amount_fen": str(value)},
        )
        for value in (1, 2, 4)
    ]
    context = LifecycleContext(original, original_plan, *reversed(returns))
    totals = {}
    for current in returns:
        plan = create_operations().evaluate_slot(current["business_id"], "main", context)
        for row in plan["report_contributions"]:
            assert int(row["amount_fen"]) < 0
            totals[row["detail_code"]] = totals.get(row["detail_code"], 0) + int(row["amount_fen"])
    assert totals == {"management_startup": -3, "management_research": -4}


def test_taxed_advance_accepts_same_tax_point_with_original_evidence():
    original, original_plan, policy = customer_advance()
    current = advance_use(original, "advance_fulfillment", 103)
    current["data"]["tax_point"] = original["data"]["tax_point"].copy()
    current["data"]["vat"] = original["data"]["vat"].copy()
    context = LifecycleContext(original, original_plan, current, policy_context=policy)
    plan = create_operations().evaluate_slot(current["business_id"], "main", context)
    assert plan["target"]["values"]["revenue_fen"] == "100"
    assert plan["tax_sales"] == []


def test_explicit_return_range_reserves_space_before_assigning_implicit_ranges():
    original = sale_fact()
    policy = vat_context(original)
    original_plan = create_operations().evaluate_slot(original["business_id"], "main", policy)
    explicit = other_fact(
        "sale_return",
        {
            "sale_id": original["business_id"],
            "customer_id": PARTY,
            "gross_fen": "70000",
            "credit_note_vat_fen": "2000",
            "tax_ranges": [{"from_fen": "33000", "to_fen": "103000"}],
        },
    )
    implicit = other_fact(
        "sale_return",
        {
            "sale_id": original["business_id"],
            "customer_id": PARTY,
            "gross_fen": "33000",
            "credit_note_vat_fen": "1000",
        },
    )
    explicit["period"], implicit["period"] = "2026-02", "2026-03"
    context = LifecycleContext(original, original_plan, implicit, explicit, policy_context=policy)
    plan = create_operations().evaluate_slot(implicit["business_id"], "main", context)
    assert plan["tax_sales"][0]["ranges"] == [{"from_fen": "0", "to_fen": "33000"}]
    implicit["data"]["tax_ranges"] = [{"from_fen": "30000", "to_fen": "63000"}]
    with pytest.raises(ContractError) as failed:
        create_operations().evaluate_slot(explicit["business_id"], "main", context)
    assert failed.value.code == "dependency_conflict"


@pytest.mark.parametrize("known", [True, False])
def test_overpayment_recovery_inherits_original_cashflow_and_keeps_unknown(known):
    original = fact("5")
    original_plan = create_operations().evaluate_slot(BUSINESS, "main", Context(None, original))
    source = original_plan["obligations"][0]
    if known:
        source["cashflow_weights"] = [
            {"category": "employee_payments", "weight": "2"},
            {"category": "tax_payments", "weight": "3"},
        ]
    else:
        source.pop("cashflow_weights")
    current = other_fact(
        "overpayment",
        {
            "source": {"business_id": BUSINESS, "slot": "primary"},
            "counterparty_id": PARTY,
            "amount_fen": "2",
        },
    )
    paid = {
        "business_id": str(uuid4()),
        "kind": "money.movement",
        "period": "2026-01",
        "evidence_ids": [SOURCE],
        "effective_on": "2026-01-15",
        "data": {
            "type": "settlement",
            "funds_account_id": BANK,
            "direction": "outflow",
            "amount_fen": "7",
            "payment_method": "individual",
            "counterparty_id": PARTY,
            "allocations": [{"source": current["data"]["source"], "amount_fen": "7"}],
        },
    }
    validate_contract("business-facts.schema.json", "Fact", paid)
    context = LifecycleContext(original, original_plan, current, paid)
    plan = create_operations().evaluate_slot(current["business_id"], "main", context)
    if known:
        assert plan["obligations"][0]["cashflow_weights"] == [
            {"category": "employee_payments", "weight": "1"},
            {"category": "tax_payments", "weight": "1"},
        ]
    else:
        assert "cashflow_weights" not in plan["obligations"][0]
    span = next(
        item for item in plan["basis"]["adopted_quantities"] if item["type"] == "cumulative_range"
    )
    assert (span["from_fen"], span["to_fen"]) == ("5", "7")


@pytest.mark.parametrize("reverse_ids", [False, True])
def test_real_advance_red_vat_and_installments_conserve_frozen_tax_source(reverse_ids):
    original, _, policy = customer_advance(total="10100")
    original["data"]["vat"]["waive_rate_reduction"] = False
    rule = policy.get_fact(original["data"]["vat"]["policy_id"])
    rule["data"].update(tax_base_rate="0.01", payable_rate="0.01")
    original_plan = create_operations().evaluate_slot(original["business_id"], "main", policy)
    returned = advance_use(original, "advance_refund", 2020)
    returned["data"]["tax_returns"] = [
        {
            "state": "taxed",
            "original_tax_period": "2026-01",
            "ranges": [{"from_fen": "0", "to_fen": "2020"}],
            "vat_fen": "19",
        }
    ]
    first = advance_use(original, "advance_fulfillment", 2020, month="2026-03")
    last = advance_use(original, "advance_fulfillment", 6060, month="2026-04")
    ids = sorted(item["business_id"] for item in (first, last, returned))
    if reverse_ids:
        ids.reverse()
    for item, identifier in zip((first, last, returned), ids, strict=True):
        item["business_id"] = identifier
    context = LifecycleContext(
        original, original_plan, last, returned, first, policy_context=policy
    )
    plans = [
        create_operations().evaluate_slot(item["business_id"], "main", context)
        for item in (returned, first, last)
    ]
    assert plans[0]["target"]["values"]["vat_reversed_fen"] == "19"
    assert [plan["target"]["values"]["vat_fen"] for plan in plans[1:]] == ["20", "61"]
    assert (
        sum(
            int(
                plan["target"]["values"].get(
                    "vat_fen", plan["target"]["values"].get("vat_reversed_fen")
                )
            )
            for plan in plans
        )
        == 100
    )
