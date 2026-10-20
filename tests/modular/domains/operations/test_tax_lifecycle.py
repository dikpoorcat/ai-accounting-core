"""Operating rights and real money share one candidate and one atomic commit."""

from copy import deepcopy
from hashlib import sha256
from uuid import uuid4

import pytest

from ai_accounting_modular.application.public import create_application
from ai_accounting_modular.application.public import repository_factory as application_repository
from ai_accounting_modular.domains.operations.public import create_operations
from ai_accounting_modular.domains.operations.public import (
    repository_factory as operations_repository,
)
from ai_accounting_modular.domains.taxes.public import create_taxes
from ai_accounting_modular.domains.taxes.public import repository_factory as taxes_repository
from ai_accounting_modular.identity.public import create_identity
from ai_accounting_modular.identity.public import repository_factory as identity_repository
from ai_accounting_modular.ledger.public import create_ledger
from ai_accounting_modular.ledger.public import repository_factory as ledger_repository
from ai_accounting_modular.money.public import create_money
from ai_accounting_modular.money.public import repository_factory as money_repository
from ai_accounting_modular.records.public import create_records
from ai_accounting_modular.records.public import repository_factory as records_repository
from ai_accounting_modular.storage.public import CompanySpec, OwnerProfile, Storage, create_catalog


@pytest.fixture
def system(tmp_path):
    catalog = tmp_path / "catalog.sqlite3"
    create_catalog(catalog, OwnerProfile(str(uuid4()), "合成老板", "synthetic-password-hash"))
    storage = Storage(
        catalog,
        repositories=[
            application_repository(),
            identity_repository(),
            ledger_repository(),
            records_repository(),
            operations_repository(),
            money_repository(),
            taxes_repository(),
        ],
    )
    company = str(uuid4())
    storage.create_company(
        CompanySpec(
            company,
            str(uuid4()),
            "913100000000000002",
            "合成经营资金公司",
            "2026-01",
            tmp_path / "company",
        )
    )
    identity, ledger = create_identity(), create_ledger()
    money = create_money(ledger=ledger, identity=identity)
    app = create_application(
        storage=storage,
        identity=identity,
        ledger=ledger,
        records=create_records(root_for=lambda read: storage.company_metadata(read).company_root),
        domains=[create_operations(), create_taxes(), money],
        money=money,
    )
    ids = {key: str(uuid4()) for key in ("source", "party", "beneficiary", "bank")}
    text = "合成资料：真实经营往来、收付款及更正。"
    source = {
        "source_id": ids["source"],
        "kind": "owner_statement",
        "text": text,
        "sha256": sha256(text.encode()).hexdigest(),
        "archive_month": "2026-01",
    }
    objects = [
        {
            "object_id": ids[key],
            "kind": "organization",
            "name": "合成" + key,
            "expected_seq": 0,
            "data": {},
        }
        for key in ("party", "beneficiary")
    ]
    objects.append(
        {
            "object_id": ids["bank"],
            "kind": "bank_account",
            "name": "合成公司银行",
            "expected_seq": 0,
            "data": {
                "bank_name": "合成银行",
                "account_number": "synthetic",
                "currency": "CNY",
                "owner": "company",
                "opened_period": "2026-01",
                "zero_at_opening": True,
            },
        }
    )
    result = app.accounting_apply(command(company, objects=objects, sources=[source]))
    assert result["completion"] == "complete", result
    return app, company, ids


def command(company, *facts, mutations=None, **participants):
    return {
        "contract": "ai-accounting-modular/1",
        "company_id": company,
        "command_id": str(uuid4()),
        "mutations": mutations
        if mutations is not None
        else [{"op": "create", "expected_seq": 0, "fact": fact} for fact in facts],
        **participants,
    }


def fact(ids, kind, data, *, period="2026-01", actual=None):
    value = {
        "business_id": str(uuid4()),
        "kind": kind,
        "period": period,
        "evidence_ids": [ids["source"]],
        "data": data,
    }
    if actual is not None:
        value["effective_on"] = actual
    return value


def policy(ids):
    return fact(
        ids,
        "taxes.vat_policy",
        {
            "policy": {
                "version": "synthetic-1",
                "effective_from": "2026-01-01",
                "effective_to": "2026-12-31",
                "official_url": "https://example.gov.cn/synthetic-rule",
            },
            "nominal_rate": "0.03",
            "tax_base_rate": "0.01",
            "payable_rate": "0.01",
            "form_version": "small_taxpayer_2026",
            "threshold_fen": "0",
            "threshold_period": "month",
            "threshold_operator": "at_or_below",
            "exemption_scope": "eligible_non_excluded_sales",
            "special_invoice_may_waive_exemption": True,
            "special_invoice_may_waive_rate_reduction": True,
        },
    )


def sale(ids, rule, tax_month="2026-03", *, advance=False):
    data = {
        "vat": {
            "policy_id": rule["business_id"],
            "invoice_kind": "ordinary",
            "exemption_eligible": False,
            "waive_exemption": False,
            "waive_rate_reduction": False,
        },
        "tax_point": {
            "type": "explicit",
            "tax_period": tax_month,
            "source": {"source_id": ids["source"]},
        },
    }
    if advance:
        data.update(
            side="customer",
            counterparty_id=ids["party"],
            amount_fen="10100",
            tax_status="due" if tax_month == "2026-01" else "not_due",
        )
    else:
        data.update(customer_id=ids["party"], gross_fen="10100")
    return fact(ids, "operations.advance" if advance else "operations.service_sale", data)


def get(app, company, current):
    value = app.accounting_get(
        {
            "company_id": company,
            "business_ids": [current["business_id"]],
            "include": ["facts", "results", "sources"],
        }
    )
    return value["businesses"][0]


def result(app, company, current, slot="main"):
    return next(
        row
        for row in get(app, company, current)["results"]
        if row["reference"]["result_slot"] == slot
    )


def reach(ids, month):
    return fact(
        ids,
        "operations.expense",
        {
            "creditor_id": ids["party"],
            "creditor_kind": "supplier",
            "amount_fen": "1",
            "expense_class": "service",
        },
        period=month,
    )


def test_future_vat_waits_for_working_month_then_releases_without_revenue_twice(system):
    app, company, ids = system
    rule = policy(ids)
    earned = sale(ids, rule)
    created = app.accounting_apply(command(company, earned, rule))
    assert created.get("completion") == "complete", created.get("message", created)
    main = result(app, company, earned)
    assert main["target"]["values"] == {
        "gross_fen": "10100",
        "revenue_fen": "10000",
        "vat_fen": "100",
        "current_tax_net_fen": "0",
        "current_tax_vat_fen": "0",
        "deferred_vat_fen": "100",
    }
    with app.storage.read_unit(company) as read:
        assert app.ledger.tax_sales("2026-01", "2026-03", read) == ()
    assert len(get(app, company, earned)["results"]) == 1
    later = app.accounting_apply(command(company, reach(ids, "2026-03")))
    assert later.get("completion") == "complete", later.get("message", later)
    release = result(app, company, earned, "tax:2026-03")
    assert release["target"]["values"] == {
        "gross_fen": "10100",
        "net_sales_fen": "10000",
        "vat_fen": "100",
    }
    assert [
        (line["account_code"], line["debit_fen"], line["credit_fen"])
        for line in release["vouchers"][0]["lines"]
    ] == [("222104", "100", "0"), ("222101", "0", "100")]
    assert result(app, company, earned)["reference"] == main["reference"]
    with app.storage.read_unit(company) as read:
        assert (
            app.ledger.pre_tax("2026-01", "2026-03", read).fields["operating_revenue"]["amount_fen"]
            == "10000"
        )


def test_future_tax_excludes_real_prior_return_and_later_return_adopts_release(system):
    app, company, ids = system
    rule = policy(ids)
    earned = sale(ids, rule)
    returned = fact(
        ids,
        "operations.sale_return",
        {
            "sale_id": earned["business_id"],
            "customer_id": ids["party"],
            "gross_fen": "2020",
            "credit_note_vat_fen": "20",
        },
        period="2026-02",
    )
    created = app.accounting_apply(command(company, returned, earned, rule))
    assert created.get("completion") == "complete", created.get("message", created)
    early = result(app, company, returned)
    assert early["target"]["values"]["tax_returns"] == [
        {"state": "not_due", "vat_fen": "20", "ranges": [{"from_fen": "0", "to_fen": "2020"}]}
    ]
    later = app.accounting_apply(command(company, reach(ids, "2026-03")))
    assert later.get("completion") == "complete", later.get("message", later)
    release = result(app, company, earned, "tax:2026-03")
    assert release["target"]["values"] == {
        "gross_fen": "8080",
        "net_sales_fen": "8000",
        "vat_fen": "80",
    }
    final = fact(
        ids,
        "operations.sale_return",
        {
            "sale_id": earned["business_id"],
            "customer_id": ids["party"],
            "gross_fen": "8080",
            "credit_note_vat_fen": "80",
        },
        period="2026-04",
    )
    saved = app.accounting_apply(command(company, final))
    assert saved.get("completion") == "complete", saved.get("message", saved)
    assert "tax:2026-03" in {
        source["result_slot"] for source in result(app, company, final)["basis"]["source_results"]
    }
    with app.storage.read_unit(company) as read:
        sales = app.ledger.tax_sales("2026-01", "2026-04", read)
    assert [
        (row.sale["tax_period"], row.sale["net_sales_fen"], row.sale["vat_fen"]) for row in sales
    ] == [("2026-03", "8000", "80"), ("2026-04", "-8000", "-80")]


def test_advance_fulfilled_before_future_tax_moves_deferred_not_a_second_sale(system):
    app, company, ids = system
    rule = policy(ids)
    advance = sale(ids, rule, advance=True)
    fulfilled = fact(
        ids,
        "operations.advance_fulfillment",
        {"advance_id": advance["business_id"], "gross_fen": "5050"},
        period="2026-02",
    )
    refunded = fact(
        ids,
        "operations.advance_refund",
        {"advance_id": advance["business_id"], "gross_fen": "2020"},
        period="2026-02",
    )
    created = app.accounting_apply(command(company, refunded, fulfilled, advance, rule))
    assert created.get("completion") == "complete", created.get("message", created)
    assert result(app, company, advance)["target"]["values"]["vat_fen"] == "0"
    assert result(app, company, refunded)["target"]["values"]["vat_reversed_fen"] == "0"
    assert result(app, company, fulfilled)["target"]["values"]["vat_fen"] == "50"
    later = app.accounting_apply(command(company, reach(ids, "2026-03")))
    assert later.get("completion") == "complete", later.get("message", later)
    release = result(app, company, advance, "tax:2026-03")
    assert release["target"]["values"]["vat_fen"] == "80"
    lines = release["vouchers"][0]["lines"]
    assert {(row["account_code"], row["debit_fen"], row["credit_fen"]) for row in lines} == {
        ("222104", "50", "0"),
        ("2203", "30", "0"),
        ("222101", "0", "80"),
    }


def point(ids, target, events):
    return fact(
        ids,
        "taxes.tax_point",
        {
            "target_business_id": target["business_id"],
            "transaction_mode": "ordinary_service",
            "service_state": "not_started",
            "contractual_entitlement": "not_set",
            "events": [
                {
                    "event_id": str(uuid4()),
                    "type": "invoice",
                    "month": month,
                    "source": {"source_id": ids["source"]},
                    "ranges": [{"from_fen": str(start), "to_fen": str(end)}],
                }
                for month, start, end in events
            ],
        },
    )


def test_detailed_multimonth_and_not_due_ranges_have_one_vat_owner(system):
    app, company, ids = system
    rule = policy(ids)
    earned = sale(ids, rule)
    resolved = point(ids, earned, [("2026-01", 0, 3030), ("2026-03", 3030, 7070)])
    earned["data"]["tax_point"] = {"type": "derived", "tax_point_id": resolved["business_id"]}
    created = app.accounting_apply(command(company, earned, resolved, rule))
    assert created.get("completion") == "complete", created.get("message", created)
    main = result(app, company, earned)
    assert main["target"]["values"]["current_tax_vat_fen"] == "30"
    assert main["target"]["values"]["deferred_vat_fen"] == "70"
    assert result(app, company, resolved)["target"]["values"]["segments"][-1]["state"] == "not_due"
    with app.storage.read_unit(company) as read:
        assert [
            (row.sale["tax_period"], row.sale["vat_fen"])
            for row in app.ledger.tax_sales("2026-01", "2026-03", read)
        ] == [("2026-01", "30")]
    later = app.accounting_apply(command(company, reach(ids, "2026-03")))
    assert later.get("completion") == "complete", later.get("message", later)
    assert result(app, company, earned, "tax:2026-03")["target"]["values"]["vat_fen"] == "40"
    assert not result(app, company, resolved)["vouchers"]


def test_multiple_partial_red_sources_require_and_honor_actual_components(system):
    app, company, ids = system
    rule = policy(ids)
    earned = sale(ids, rule)
    resolved = point(ids, earned, [("2026-01", 0, 5050), ("2026-02", 5050, 10100)])
    earned["data"]["tax_point"] = {"type": "derived", "tax_point_id": resolved["business_id"]}
    returned = fact(
        ids,
        "operations.sale_return",
        {
            "sale_id": earned["business_id"],
            "customer_id": ids["party"],
            "gross_fen": "4040",
            "credit_note_vat_fen": "40",
            "tax_ranges": [
                {"from_fen": "1010", "to_fen": "3030"},
                {"from_fen": "6060", "to_fen": "8080"},
            ],
        },
        period="2026-03",
    )
    rejected = app.accounting_apply(command(company, returned, earned, resolved, rule))
    assert rejected.get("error") == "needs_information", rejected
    returned["data"]["tax_returns"] = [
        {
            "state": "taxed",
            "original_tax_period": "2026-01",
            "vat_fen": "19",
            "ranges": [{"from_fen": "1010", "to_fen": "3030"}],
        },
        {
            "state": "taxed",
            "original_tax_period": "2026-02",
            "vat_fen": "21",
            "ranges": [{"from_fen": "6060", "to_fen": "8080"}],
        },
    ]
    saved = app.accounting_apply(command(company, returned, earned, resolved, rule))
    assert saved.get("completion") == "complete", saved.get("message", saved)
    view = result(app, company, returned)
    assert "original_tax_period" not in view["target"]["values"]
    assert [part["vat_fen"] for part in view["target"]["values"]["tax_returns"]] == ["19", "21"]
    with app.storage.read_unit(company) as read:
        negative = [row.sale for row in app.ledger.tax_sales("2026-03", "2026-03", read)]
    assert [row["vat_fen"] for row in negative] == ["-19", "-21"]
    assert all(row["source_business_id"] == returned["business_id"] for row in negative)


def actual_advance_refund(ids, original, vat, *, month="2026-02"):
    return fact(
        ids,
        "operations.advance_refund",
        {
            "advance_id": original["business_id"],
            "gross_fen": "2020",
            "tax_returns": [
                {
                    "state": "taxed",
                    "original_tax_period": "2026-01",
                    "vat_fen": str(vat),
                    "ranges": [{"from_fen": "0", "to_fen": "2020"}],
                }
            ],
        },
        period=month,
    )


def test_actual_advance_red_vat_is_kept_and_final_fulfillment_absorbs_remainder(system):
    app, company, ids = system
    rule = policy(ids)
    advance = sale(ids, rule, "2026-01", advance=True)
    returned = actual_advance_refund(ids, advance, 19)
    fulfilled = fact(
        ids,
        "operations.advance_fulfillment",
        {"advance_id": advance["business_id"], "gross_fen": "8080"},
        period="2026-03",
    )
    saved = app.accounting_apply(command(company, fulfilled, returned, advance, rule))
    assert saved.get("completion") == "complete", saved.get("message", saved)
    assert result(app, company, returned)["target"]["values"]["vat_reversed_fen"] == "19"
    view = result(app, company, fulfilled)
    assert view["target"]["values"]["vat_fen"] == "81"
    adopted = [item for item in view["basis"]["adopted_quantities"] if item["type"] == "tax_return"]
    assert adopted == [
        {
            "type": "tax_return",
            "business_id": returned["business_id"],
            "period": "2026-02",
            "tax_returns": returned["data"]["tax_returns"],
        }
    ]
    returned["data"]["tax_returns"][0]["vat_fen"] = "20"
    replaced = app.accounting_apply(
        command(company, mutations=[{"op": "replace", "expected_seq": 1, "fact": returned}])
    )
    assert replaced.get("completion") == "complete", replaced.get("message", replaced)
    assert result(app, company, fulfilled)["target"]["values"]["vat_fen"] == "80"
    withdrawn = app.accounting_apply(
        command(
            company,
            mutations=[
                {"op": "withdraw", "expected_seq": 2, "business_id": returned["business_id"]}
            ],
        )
    )
    assert withdrawn.get("completion") == "complete", withdrawn.get("message", withdrawn)
    assert result(app, company, fulfilled)["target"]["values"]["vat_fen"] == "80"
    assert not [
        item
        for item in result(app, company, fulfilled)["basis"]["adopted_quantities"]
        if item["type"] == "tax_return"
    ]


def test_later_red_fact_cannot_rewrite_earlier_economic_month(system):
    app, company, ids = system
    rule = policy(ids)
    advance = sale(ids, rule, "2026-01", advance=True)
    fulfilled = fact(
        ids,
        "operations.advance_fulfillment",
        {"advance_id": advance["business_id"], "gross_fen": "8080"},
        period="2026-02",
    )
    created = app.accounting_apply(command(company, fulfilled, advance, rule))
    assert created.get("completion") == "complete", created.get("message", created)
    before = result(app, company, fulfilled)
    # The remaining original interval is [8080,10100); an actual 19 VAT would
    # leave one fen of original VAT without any remaining gross capacity.
    returned = actual_advance_refund(ids, advance, 19, month="2026-03")
    returned["data"]["tax_returns"][0]["ranges"] = [{"from_fen": "8080", "to_fen": "10100"}]
    rejected = app.accounting_apply(command(company, returned))
    assert rejected.get("completion") == "not_saved", rejected
    assert result(app, company, fulfilled) == before


def test_mixed_taxed_and_untaxed_advance_uses_new_point_only_for_unresolved_range(system):
    app, company, ids = system
    rule = policy(ids)
    advance = sale(ids, rule, "2026-01", advance=True)
    tax_point = point(ids, advance, [("2026-01", 0, 5050)])
    advance["data"]["tax_point"] = {"type": "derived", "tax_point_id": tax_point["business_id"]}
    fulfilled = fact(
        ids,
        "operations.advance_fulfillment",
        {
            "advance_id": advance["business_id"],
            "gross_fen": "10100",
            "tax_point": {
                "type": "explicit",
                "tax_period": "2026-03",
                "source": {"source_id": ids["source"]},
            },
        },
        period="2026-02",
    )
    saved = app.accounting_apply(command(company, fulfilled, advance, tax_point, rule))
    assert saved.get("completion") == "complete", saved.get("message", saved)
    assert result(app, company, fulfilled)["target"]["values"] == {
        "gross_fen": "10100",
        "revenue_fen": "10000",
        "vat_fen": "100",
        "advance_consumed_fen": "10100",
    }
    later = app.accounting_apply(command(company, reach(ids, "2026-03")))
    assert later.get("completion") == "complete", later.get("message", later)
    release = result(app, company, fulfilled, "tax:2026-03")
    assert release["target"]["values"] == {
        "gross_fen": "5050",
        "net_sales_fen": "5000",
        "vat_fen": "50",
    }
    with app.storage.read_unit(company) as read:
        sales = app.ledger.tax_sales("2026-01", "2026-03", read)
    assert [(row.sale["tax_period"], row.sale["vat_fen"]) for row in sales] == [
        ("2026-01", "50"),
        ("2026-03", "50"),
    ]


def test_untaxed_advance_independent_fulfillment_can_have_a_future_tax_month(system):
    app, company, ids = system
    rule = policy(ids)
    advance = sale(ids, rule, advance=True)
    del advance["data"]["tax_point"]
    fulfilled = fact(
        ids,
        "operations.advance_fulfillment",
        {
            "advance_id": advance["business_id"],
            "gross_fen": "10100",
            "tax_point": {
                "type": "explicit",
                "tax_period": "2026-03",
                "source": {"source_id": ids["source"]},
            },
        },
        period="2026-02",
    )
    saved = app.accounting_apply(command(company, fulfilled, advance, rule))
    assert saved.get("completion") == "complete", saved.get("message", saved)
    with app.storage.read_unit(company) as read:
        assert not app.ledger.tax_sales("2026-01", "2026-02", read)
    later = app.accounting_apply(command(company, reach(ids, "2026-03")))
    assert later.get("completion") == "complete", later.get("message", later)
    assert result(app, company, fulfilled, "tax:2026-03")["target"]["values"]["vat_fen"] == "100"


def test_future_advance_without_vat_is_valid_until_its_true_tax_month_requires_it(system):
    app, company, ids = system
    rule = policy(ids)
    advance = sale(ids, rule, advance=True)
    vat = advance["data"].pop("vat")
    saved = app.accounting_apply(command(company, advance))
    assert saved.get("completion") == "complete", saved.get("message", saved)
    attempted = app.accounting_apply(command(company, reach(ids, "2026-03")))
    assert attempted.get("error") == "needs_information", attempted
    assert attempted["fact_issues"][0]["field"] == "/data/vat"
    assert len(get(app, company, advance)["results"]) == 1
    advance["data"]["vat"] = vat
    fixed = app.accounting_apply(
        command(
            company,
            mutations=[
                {"op": "replace", "expected_seq": 1, "fact": advance},
                {"op": "create", "expected_seq": 0, "fact": rule},
                {"op": "create", "expected_seq": 0, "fact": reach(ids, "2026-03")},
            ],
        )
    )
    assert fixed.get("completion") == "complete", fixed.get("message", fixed)
    assert result(app, company, advance, "tax:2026-03")["target"]["values"]["vat_fen"] == "100"


def test_vat_assessment_waits_for_the_future_tax_publisher_in_the_same_submission(system):
    app, company, ids = system
    rule = policy(ids)
    earned = sale(ids, rule)
    extra = fact(
        ids,
        "taxes.surtax_policy",
        {
            "policy": {
                "version": "synthetic-surtax-1",
                "effective_from": "2026-01-01",
                "effective_to": "2026-12-31",
                "official_url": "https://example.gov.cn/synthetic-surtax",
            },
            "urban_rate": "0",
            "education_rate": "0",
            "local_education_rate": "0",
            "payable_fraction": "1",
        },
    )
    assessed = fact(
        ids,
        "taxes.assessment",
        {
            "type": "vat",
            "start_month": "2026-03",
            "end_month": "2026-03",
            "vat_policy_id": rule["business_id"],
            "surtax_policy_id": extra["business_id"],
            "filing_profile": {
                "taxpayer_type": "small_scale",
                "form_scope": "nonpilot",
                "source": {"source_id": ids["source"]},
            },
        },
        period="2026-03",
    )
    saved = app.accounting_apply(command(company, assessed, earned, extra, rule))
    assert saved.get("completion") == "complete", saved.get("message", saved)
    assert result(app, company, assessed)["target"]["values"]["payable_vat_fen"] == "100"
    references = result(app, company, assessed)["basis"]["source_results"]
    assert any(
        source["business_id"] == earned["business_id"] and source["result_slot"] == "tax:2026-03"
        for source in references
    )


def test_derived_month_precision_requests_the_actual_event_day_from_its_source(system):
    app, company, ids = system
    rule = policy(ids)
    rule["data"]["policy"]["effective_from"] = "2026-03-10"
    earned = sale(ids, rule)
    derived = point(ids, earned, [("2026-03", 0, 10100)])
    earned["data"]["tax_point"] = {"type": "derived", "tax_point_id": derived["business_id"]}
    attempted = app.accounting_apply(command(company, earned, derived, rule))
    assert attempted["completion"] == "not_saved" and attempted["error"] == "needs_information", (
        attempted
    )
    problem = attempted["fact_issues"][0]
    assert (problem["business_id"], problem["field"]) == (
        derived["business_id"],
        "/data/events/0/on",
    )
    assert (
        app.accounting_get(
            {
                "company_id": company,
                "business_ids": [earned["business_id"], derived["business_id"]],
                "include": ["facts", "results"],
            }
        )["businesses"]
        == []
    )
    derived["data"]["events"][0]["on"] = "2026-03-20"
    accepted = app.accounting_apply(command(company, earned, derived, rule))
    assert accepted["completion"] == "complete", accepted


@pytest.mark.parametrize("changed", ["old_month", "old_day", "old_state", "new_month"])
def test_mixed_fulfillment_validates_inherited_segments_even_outside_its_new_tax_scope(
    system, changed
):
    app, company, ids = system
    rule = policy(ids)
    original = sale(ids, rule, "2026-01", advance=True)
    first = point(ids, original, [("2026-01", 0, 5050)])
    first["data"]["events"][0]["on"] = "2026-01-10"
    original["data"]["tax_point"] = {"type": "derived", "tax_point_id": first["business_id"]}
    fulfilled = fact(
        ids,
        "operations.advance_fulfillment",
        {"advance_id": original["business_id"], "gross_fen": "10100"},
        period="2026-02",
    )
    stated = point(ids, fulfilled, [("2026-01", 0, 5050), ("2026-03", 5050, 10100)])
    stated["data"]["events"][0]["on"] = "2026-01-10"
    if changed == "old_month":
        stated["data"]["events"][0].update(month="2026-02", on="2026-02-10")
    elif changed == "old_day":
        stated["data"]["events"][0]["on"] = "2026-01-11"
    elif changed == "old_state":
        del stated["data"]["events"][0]
    else:
        stated["data"]["events"][1]["month"] = "2026-01"
    fulfilled["data"]["tax_point"] = {"type": "derived", "tax_point_id": stated["business_id"]}
    refused = app.accounting_apply(command(company, fulfilled, stated, original, first, rule))
    assert refused["completion"] == "not_saved", refused
    assert refused["error"] == ("needs_information" if changed == "new_month" else "invalid_input")
    assert (
        app.accounting_get(
            {
                "company_id": company,
                "business_ids": [original["business_id"], fulfilled["business_id"]],
                "include": ["facts", "results"],
            }
        )["businesses"]
        == []
    )


def test_fully_inherited_fulfillment_accepts_true_restatement_but_rejects_false_explicit_month(
    system,
):
    app, company, ids = system
    rule = policy(ids)
    original = sale(ids, rule, "2026-01", advance=True)
    fulfilled = fact(
        ids,
        "operations.advance_fulfillment",
        {
            "advance_id": original["business_id"],
            "gross_fen": "10100",
            "tax_point": {
                "type": "explicit",
                "tax_period": "2026-02",
                "source": {"source_id": ids["source"]},
            },
        },
        period="2026-02",
    )
    refused = app.accounting_apply(command(company, fulfilled, original, rule))
    assert refused["error"] == "invalid_input" and refused["completion"] == "not_saved", refused
    fulfilled["data"]["tax_point"]["tax_period"] = "2026-01"
    accepted = app.accounting_apply(command(company, fulfilled, original, rule))
    assert accepted["completion"] == "complete", accepted
    current = result(app, company, fulfilled)
    assert current["target"]["values"]["vat_fen"] == "100"
    with app.storage.read_unit(company) as read:
        assert [
            (row.sale["source_business_id"], row.sale["tax_period"])
            for row in app.ledger.tax_sales("2026-01", "2026-02", read)
        ] == [(original["business_id"], "2026-01")]


@pytest.mark.parametrize("refund_day", ["2026-03-10", "2026-03-25", None])
def test_unpriced_future_tax_release_requires_proof_that_same_month_refund_precedes_tax(
    system, refund_day
):
    app, company, ids = system
    original = sale(ids, policy(ids), "2026-03", advance=True)
    original["data"]["tax_point"]["tax_on"] = "2026-03-20"
    del original["data"]["vat"]
    refunded = fact(
        ids,
        "operations.advance_refund",
        {"advance_id": original["business_id"], "gross_fen": "10100"},
        period="2026-03",
        actual=refund_day,
    )
    posted = app.accounting_apply(command(company, refunded, original))
    if refund_day != "2026-03-10":
        assert posted["completion"] == "not_saved" and posted["error"] == "needs_information", (
            posted
        )
        assert posted["fact_issues"][0]["field"] == "/data/vat"
        return
    assert posted["completion"] == "complete", posted
    assert result(app, company, refunded)["basis"]["effective_on"] == refund_day
    released = result(app, company, original, "tax:2026-03")
    assert released["target"]["values"]["gross_fen"] == "0" and released["vouchers"] == []
    assert {source["business_id"] for source in released["basis"]["source_results"]} == {
        original["business_id"],
        refunded["business_id"],
    }


def test_a_zero_unpriced_release_reopens_its_real_requirement_if_refund_is_reduced_or_withdrawn(
    system,
):
    app, company, ids = system
    rule = policy(ids)
    original = sale(ids, rule, "2026-03", advance=True)
    vat = original["data"].pop("vat")
    refunded = fact(
        ids,
        "operations.advance_refund",
        {"advance_id": original["business_id"], "gross_fen": "10100"},
        period="2026-02",
    )
    initial = app.accounting_apply(command(company, refunded, original, reach(ids, "2026-03")))
    assert initial["completion"] == "complete", initial
    baseline = get(app, company, original)
    refund_seq = get(app, company, refunded)["state"]["seq"]
    reduced = deepcopy(refunded)
    reduced["data"]["gross_fen"] = "5050"
    for mutation in (
        {"op": "replace", "expected_seq": refund_seq, "fact": reduced},
        {"op": "withdraw", "expected_seq": refund_seq, "business_id": refunded["business_id"]},
    ):
        denied = app.accounting_apply(command(company, mutations=[mutation]))
        assert denied["completion"] == "not_saved" and denied["error"] == "needs_information", (
            denied
        )
        assert get(app, company, original) == baseline
    original["data"]["vat"] = vat
    restored = app.accounting_apply(
        command(
            company,
            mutations=[
                {"op": "create", "expected_seq": 0, "fact": rule},
                {"op": "replace", "expected_seq": baseline["state"]["seq"], "fact": original},
                {"op": "replace", "expected_seq": refund_seq, "fact": reduced},
            ],
        )
    )
    assert restored["completion"] == "complete", restored
    assert result(app, company, original, "tax:2026-03")["target"]["values"]["vat_fen"] == "50"


def test_inherited_tax_coordinates_survive_a_prior_refund_and_only_new_scope_is_released(system):
    app, company, ids = system
    rule = policy(ids)
    original = sale(ids, rule, "2026-01", advance=True)
    first = point(ids, original, [("2026-01", 0, 5050)])
    original["data"]["tax_point"] = {"type": "derived", "tax_point_id": first["business_id"]}
    refunded = actual_advance_refund(ids, original, 20)
    fulfilled = fact(
        ids,
        "operations.advance_fulfillment",
        {"advance_id": original["business_id"], "gross_fen": "8080"},
        period="2026-03",
    )
    own_point = point(ids, fulfilled, [("2026-01", 0, 3030), ("2026-04", 3030, 8080)])
    fulfilled["data"]["tax_point"] = {"type": "derived", "tax_point_id": own_point["business_id"]}
    saved = app.accounting_apply(
        command(company, own_point, fulfilled, refunded, first, original, rule)
    )
    assert saved["completion"] == "complete", saved
    current = result(app, company, fulfilled)
    assert current["target"]["values"]["vat_fen"] == "80"
    own_resolution = next(
        item["resolution"]
        for item in current["basis"]["adopted_quantities"]
        if item["type"] == "tax_point"
        and item["resolution"]["target_business_id"] == fulfilled["business_id"]
    )
    assert [(part["tax_period"], part["ranges"]) for part in own_resolution["segments"]] == [
        ("2026-01", [{"from_fen": "0", "to_fen": "3030"}]),
        ("2026-04", [{"from_fen": "3030", "to_fen": "8080"}]),
    ]
    reached = app.accounting_apply(command(company, reach(ids, "2026-04")))
    assert reached["completion"] == "complete", reached
    assert result(app, company, fulfilled, "tax:2026-04")["target"]["values"] == {
        "gross_fen": "5050",
        "net_sales_fen": "5000",
        "vat_fen": "50",
    }
    with app.storage.read_unit(company) as read:
        sales = app.ledger.tax_sales("2026-01", "2026-04", read)
    assert sorted((item.sale["tax_period"], item.sale["vat_fen"]) for item in sales) == [
        ("2026-01", "50"),
        ("2026-02", "-20"),
        ("2026-04", "50"),
    ]


def test_a_full_pre_tax_refund_cancels_every_unpriced_future_segment_without_a_rate(system):
    app, company, ids = system
    original = sale(ids, policy(ids), "2026-03", advance=True)
    del original["data"]["vat"]
    own_point = point(ids, original, [("2026-03", 0, 5050), ("2026-04", 5050, 10100)])
    original["data"]["tax_point"] = {"type": "derived", "tax_point_id": own_point["business_id"]}
    refunded = fact(
        ids,
        "operations.advance_refund",
        {"advance_id": original["business_id"], "gross_fen": "10100"},
        period="2026-02",
    )
    saved = app.accounting_apply(
        command(company, reach(ids, "2026-04"), refunded, original, own_point)
    )
    assert saved["completion"] == "complete", saved
    for slot in ("tax:2026-03", "tax:2026-04"):
        release = result(app, company, original, slot)
        assert release["vouchers"] == []
        assert release["target"]["values"]["vat_fen"] == "0"
        assert refunded["business_id"] in {
            source["business_id"] for source in release["basis"]["source_results"]
        }
    with app.storage.read_unit(company) as read:
        assert app.ledger.tax_sales("2026-01", "2026-04", read) == ()


def test_refund_day_is_not_adopted_for_a_different_untaxed_segment(system):
    app, company, ids = system
    original = sale(ids, policy(ids), "2026-03", advance=True)
    del original["data"]["vat"]
    own_point = point(ids, original, [("2026-03", 0, 5050), ("2026-04", 5050, 10100)])
    own_point["data"]["events"][0]["on"] = "2026-03-20"
    original["data"]["tax_point"] = {"type": "derived", "tax_point_id": own_point["business_id"]}
    refunds = []
    for start, end, month, actual in (
        (0, 5050, "2026-02", None),
        (5050, 10100, "2026-03", "2026-03-10"),
    ):
        refunds.append(
            fact(
                ids,
                "operations.advance_refund",
                {
                    "advance_id": original["business_id"],
                    "gross_fen": "5050",
                    "tax_returns": [
                        {
                            "state": "not_due",
                            "ranges": [{"from_fen": str(start), "to_fen": str(end)}],
                            "vat_fen": "0",
                        }
                    ],
                },
                period=month,
                actual=actual,
            )
        )
    saved = app.accounting_apply(
        command(company, reach(ids, "2026-04"), *refunds, original, own_point)
    )
    assert saved["completion"] == "complete", saved
    assert "effective_on" not in result(app, company, refunds[1])["basis"]


@pytest.mark.parametrize("advance", [False, True])
@pytest.mark.parametrize("source_month", ["2026-01", "2026-03"])
@pytest.mark.parametrize("actual", ["2026-03-10", "2026-03-25", None])
def test_same_tax_month_uses_known_days_without_asking_for_immaterial_unknown_order(
    system, advance, source_month, actual
):
    app, company, ids = system
    rule = policy(ids)
    original = sale(ids, rule, "2026-03", advance=advance)
    original["period"] = source_month
    original["data"]["tax_point"]["tax_on"] = "2026-03-20"
    if advance:
        original["data"]["tax_status"] = "due" if source_month == "2026-03" else "not_due"
        returned = fact(
            ids,
            "operations.advance_refund",
            {"advance_id": original["business_id"], "gross_fen": "2020"},
            period="2026-03",
            actual=actual,
        )
    else:
        returned = fact(
            ids,
            "operations.sale_return",
            {
                "sale_id": original["business_id"],
                "customer_id": ids["party"],
                "gross_fen": "2020",
                "credit_note_vat_fen": "20",
            },
            period="2026-03",
            actual=actual,
        )
    saved = app.accounting_apply(command(company, returned, original, rule))
    assert saved.get("completion") == "complete", saved.get("message", saved)
    returned_result = result(app, company, returned)
    part = returned_result["basis"]["input"]["tax_returns"][0]
    before = actual == "2026-03-10"
    assert part["state"] == ("not_due" if before else "taxed")
    if actual:
        assert returned_result["basis"]["effective_on"] == actual
    else:
        assert "effective_on" not in returned_result["basis"]
    with app.storage.read_unit(company) as read:
        sales = app.ledger.tax_sales("2026-03", "2026-03", read)
    assert sum(int(row.sale["vat_fen"]) for row in sales) == 80
    assert len(sales) == (1 if before else 2)
    if before and source_month == "2026-03":
        adopted = [
            item
            for item in result(app, company, original)["basis"]["adopted_quantities"]
            if item["type"] == "tax_return"
        ]
        assert len(adopted) == 1 and adopted[0]["effective_on"] == actual
