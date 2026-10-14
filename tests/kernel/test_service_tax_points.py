"""Revenue, actual receipt, VAT point and advance fulfillment remain distinct facts."""

import json

import pytest
from entity_fixture import seed_fact_entities
from material_fixture import supporting_text
from test_business_domains import surtax_policy, vat_policy
from test_tax_credits import Company

from ai_accounting.kernel.contracts import KernelError, NeedsInformation
from ai_accounting.kernel.domains import taxes, transactions
from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods
from ai_accounting.kernel.tax_filing_data import TaxFilingData
from ai_accounting.kernel.tax_point_basis import TaxPointBasis


@pytest.fixture
def company(tmp_path, *, opening_period="2026-01"):
    result = Company(tmp_path / "service.sqlite", opening_period=opening_period)
    result.save(
        taxes.VatPolicyFact, "vat", period="2026-01", policy=vat_policy().model_dump(mode="json")
    )
    result.save(
        taxes.SurtaxPolicyFact,
        "surtax",
        period="2026-01",
        policy=surtax_policy().model_dump(mode="json"),
    )
    return result


@pytest.fixture
def march_company(tmp_path):
    return company.__wrapped__(tmp_path, opening_period="2026-03")


def sale(company, *, period="2026-03", tax_month="2026-04", **changes):
    company.save(
        transactions.ServiceSale,
        "sale",
        **(
            dict(
                period=period,
                customer_id="customer",
                gross_fen=149400,
                vat_policy_id="vat",
                exemption_eligible=False,
                tax_obligation_period=tax_month,
            )
            | changes
        ),
    )
    company.publish("sale")
    return company.current("service_sale", "sale")


def receipt(company, subject, amount, day, *, source_kind="service_sale", source="sale"):
    company.save(
        transactions.Payment,
        subject,
        period=day[:7],
        actual_date=day,
        direction="inflow",
        bank_account_id="bank",
        counterparty_id="customer",
        amount_fen=amount,
        allocations=[
            dict(source_kind=source_kind, source_id=source, obligation="primary", amount_fen=amount)
        ],
    )


def lines(company, kind, subject):
    calculation = company.current(kind, subject)
    with company.engine.store.connection(read_only=True) as connection:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT l.account,l.debit,l.credit FROM voucher_line l "
                "JOIN voucher_version v ON v.id=l.version_id WHERE v.calculation_id=?",
                (calculation.id,),
            )
        ]


def save_explicit_tax_fact(company, model, subject, **fields):
    """Register the scenario's exact source without generating a detailed basis."""
    fact = model.model_validate_json(json.dumps(fields))
    seed_fact_entities(company.engine, fact)
    original = company.engine.register_evidence(
        json.dumps(fields).encode(), "text/plain", "合成有源业务及明确税期",
        request_id=company.request(),
    )["digest"]
    result = company.engine.save_fact(
        fact.kind, subject, fact.model_dump(mode="json"), evidence=(original,),
        expected_revision=company.revisions.get(subject, 0), request_id=company.request(),
    )
    company.revisions[subject] = result["revision"]
    return result


def assert_no_tax_basis(company):
    with company.engine.store.connection(read_only=True) as connection:
        assert connection.execute(
            "SELECT count(*) FROM subject WHERE kind='tax_point_basis'"
        ).fetchone()[0] == 0


@pytest.mark.parametrize("tax_day", [None, "2026-04-02"])
def test_explicit_sourced_tax_month_publishes_and_settles_actual_april_receipt(company, tax_day):
    registered = save_explicit_tax_fact(
        company, transactions.ServiceSale, "explicit-sale", period="2026-03",
        customer_id="customer", gross_fen=149400, vat_policy_id="vat", exemption_eligible=False,
        tax_obligation_period="2026-04", tax_obligation_date=tax_day,
    )
    assert registered["status"] == "confirmed"
    company.publish("explicit-sale")
    revenue = company.current("service_sale", "explicit-sale")
    assert revenue.values["recognized_revenue_fen"] == 147921
    assert revenue.values["tax_obligation_period"] == "2026-04"
    assert revenue.values["tax_obligation_date"] == tax_day
    assert "tax_point_basis" not in revenue.values
    assert lines(company, "service_sale", "explicit-sale") == [
        ("1122", 149400, 0), ("5001", 0, 147921), ("222104", 0, 1479),
    ]
    receipt(company, "actual-april-receipt", 149400, "2026-04-02", source="explicit-sale")
    company.publish("actual-april-receipt")
    assert lines(company, "payment", "actual-april-receipt") == [
        ("1002", 149400, 0), ("1122", 0, 149400),
        ("222104", 1479, 0), ("222101", 0, 1479),
    ]
    assert company.balance("payment:service_sale:explicit-sale:primary") == 0
    assert company.balance("bank") == 149400
    assert_no_tax_basis(company)


def test_explicit_advance_tax_and_untaxed_advance_fulfillment_use_no_detailed_basis(company):
    save_explicit_tax_fact(
        company, transactions.Advance, "taxed-advance", period="2026-01",
        counterparty_id="customer", amount_fen=101000, side="customer",
        contractual_obligation_established=True, vat_due_on_advance=True,
        vat_policy_id="vat", exemption_eligible=False,
        tax_obligation_period="2026-01", tax_obligation_date="2026-01-05",
    )
    company.publish("taxed-advance")
    assert lines(company, "advance", "taxed-advance") == [
        ("1122", 101000, 0), ("2203", 0, 100000), ("222101", 0, 1000),
    ]
    save_explicit_tax_fact(
        company, transactions.Advance, "untaxed-advance", period="2026-01",
        counterparty_id="customer", amount_fen=101000, side="customer",
        contractual_obligation_established=True, vat_due_on_advance=False,
    )
    company.publish("untaxed-advance")
    save_explicit_tax_fact(
        company, transactions.AdvanceFulfillment, "explicit-fulfilled", period="2026-03",
        advance_id="untaxed-advance", fulfilled_gross_fen=101000,
        vat_policy_id="vat", exemption_eligible=False, tax_obligation_period="2026-03",
    )
    company.publish("explicit-fulfilled")
    assert lines(company, "advance_fulfillment", "explicit-fulfilled") == [
        ("2203", 101000, 0), ("5001", 0, 100000), ("222101", 0, 1000),
    ]
    assert_no_tax_basis(company)


@pytest.mark.parametrize("tax_month", ["2026-01", "2025-12"])
def test_explicit_advance_tax_month_cannot_be_ignored_by_a_no_tax_claim(company, tax_month):
    save_explicit_tax_fact(
        company, transactions.Advance, "contradictory-no-tax", period="2026-01",
        counterparty_id="customer", amount_fen=101000, side="customer",
        contractual_obligation_established=True, vat_due_on_advance=False,
        vat_policy_id="vat", exemption_eligible=False, tax_obligation_period=tax_month,
    )
    with company.engine.store.connection(read_only=True) as connection:
        epochs = company.engine.store.epochs(connection)
        calculations = connection.execute("SELECT count(*) FROM calculation").fetchone()[0]
    with pytest.raises(NeedsInformation) as failure:
        company.engine.preview(["contradictory-no-tax"])
    assert failure.value.issues[0]["field"] == "vat_due_on_advance"
    with company.engine.store.connection(read_only=True) as connection:
        assert company.engine.store.epochs(connection) == epochs
        assert connection.execute("SELECT count(*) FROM calculation").fetchone()[0] == calculations
    assert_no_tax_basis(company)


@pytest.mark.parametrize("model,fields", [
    (transactions.ServiceSale, {"customer_id": "customer", "gross_fen": 101000}),
    (transactions.Advance, {
        "counterparty_id": "customer", "amount_fen": 101000, "side": "customer",
    }),
    (transactions.AdvanceFulfillment, {"advance_id": "advance", "fulfilled_gross_fen": 101000}),
])
@pytest.mark.parametrize("month", [None, "2026-03"])
def test_direct_tax_day_requires_its_explicit_matching_month(company, model, fields, month):
    with pytest.raises(NeedsInformation if month is None else KernelError) as failure:
        save_explicit_tax_fact(
            company, model, "conflicting-date", period="2026-03",
            tax_obligation_period=month, tax_obligation_date="2026-04-02", **fields,
        )
    if month is None:
        assert failure.value.issues[0]["field"] == "tax_obligation_period"
        assert failure.value.issues[0]["allowed_precision"] == ["month"]
    else:
        assert failure.value.code == "tax_precision_conflict"
        assert failure.value.details["fact_issues"][0]["field"] == "tax_obligation_date"
    with company.engine.store.connection(read_only=True) as connection:
        assert connection.execute(
            "SELECT 1 FROM subject WHERE id='conflicting-date'"
        ).fetchone() is None


@pytest.mark.parametrize("basis_month,basis_day,field", [
    ("2026-05", "2026-05-02", "tax_obligation_period"),
    ("2026-04", "2026-04-03", "tax_obligation_date"),
    ("2026-04", None, "tax_obligation_date"),
])
def test_explicit_tax_point_cannot_silently_override_a_conflicting_detailed_source(
    company, basis_month, basis_day, field,
):
    from tax_point_fixture import contract_tax_basis

    save_explicit_tax_fact(
        company, transactions.ServiceSale, "different-sources", period="2026-03",
        customer_id="customer", gross_fen=149400, vat_policy_id="vat", exemption_eligible=False,
        tax_obligation_period="2026-04", tax_obligation_date="2026-04-02",
    )
    contract_tax_basis(
        company.engine, "service_sale", "different-sources",
        business_period="2026-03", contract_period=basis_month, contract_date=basis_day,
    )
    with company.engine.store.connection(read_only=True) as connection:
        epochs = company.engine.store.epochs(connection)
        count = connection.execute("SELECT count(*) FROM calculation").fetchone()[0]
    with pytest.raises(NeedsInformation) as failure:
        company.engine.preview(["different-sources"])
    assert failure.value.issues[0]["field"] == field
    assert failure.value.issues[0]["reusable_sources"]
    with company.engine.store.connection(read_only=True) as connection:
        assert company.engine.store.epochs(connection) == epochs
        assert connection.execute("SELECT count(*) FROM calculation").fetchone()[0] == count


@pytest.mark.parametrize("tax_day", [None, "2026-04-02"])
def test_explicit_tax_month_reuses_a_consistent_optional_detailed_source(company, tax_day):
    from tax_point_fixture import contract_tax_basis

    save_explicit_tax_fact(
        company, transactions.ServiceSale, "consistent-sources", period="2026-03",
        customer_id="customer", gross_fen=149400, vat_policy_id="vat", exemption_eligible=False,
        tax_obligation_period="2026-04", tax_obligation_date=tax_day,
    )
    contract_tax_basis(
        company.engine, "service_sale", "consistent-sources", business_period="2026-03",
        contract_period="2026-04", contract_date="2026-04-02",
    )
    company.publish("consistent-sources")
    result = company.current("service_sale", "consistent-sources")
    assert result.values["tax_obligation_period"] == "2026-04"
    assert result.values["tax_point_basis"]["intervals"][0]["actual_date"] == "2026-04-02"
    assert result.values["recognized_revenue_fen"] == 147921


def test_direct_tax_month_needs_actual_day_when_policy_changes_within_that_month(company):
    company.save(taxes.VatPolicyFact, "partial-direct-policy", period="2026-03",
                 policy=vat_policy().model_dump(mode="json") | {"effective_from": "2026-03-15"})
    save_explicit_tax_fact(
        company, transactions.ServiceSale, "direct-partial", period="2026-03",
        customer_id="customer", gross_fen=101000, vat_policy_id="partial-direct-policy",
        exemption_eligible=False, tax_obligation_period="2026-03",
    )
    with pytest.raises(NeedsInformation) as failure:
        company.engine.preview(["direct-partial"])
    assert failure.value.issues[0]["field"] == "tax_obligation_date"
    assert failure.value.issues[0]["allowed_precision"] == ["day"]
    assert failure.value.issues[0]["reusable_sources"] == ["direct-partial"]
    assert_no_tax_basis(company)


def adopted_advance_tax(company, path, *, with_day=True):
    from tax_point_fixture import contract_tax_basis

    month = "2026-04" if path == "later" else "2026-01"
    day = month + "-05" if with_day else None
    fields = dict(period="2026-01", counterparty_id="customer", amount_fen=101000,
                  side="customer", contractual_obligation_established=True,
                  vat_due_on_advance=path != "later", vat_policy_id="vat", exemption_eligible=False)
    if path == "direct":
        fields.update(tax_obligation_period=month, tax_obligation_date=day)
    else:
        contract_tax_basis(company.engine, "advance", "inherited-advance",
                           business_period="2026-01", contract_period=month, contract_date=day)
    save_explicit_tax_fact(company, transactions.Advance, "inherited-advance", **fields)
    company.publish("inherited-advance")
    source = company.current("advance", "inherited-advance")
    if path == "later":
        company.save(transactions.ServiceTaxPoint, "adopted-later-tax", period=month,
                     sale_id="inherited-advance", trigger="declared", declaration_confirmed=True)
        company.publish("adopted-later-tax")
        source = company.current("service_tax_point", "adopted-later-tax")
    return source, month, day


@pytest.mark.parametrize("path", ["direct", "basis", "later"])
@pytest.mark.parametrize("supplied", ["omitted", "explicit", "basis", "basis_month_only"])
def test_already_taxed_fulfillment_inherits_the_exact_adopted_tax_source(company, path, supplied):
    from tax_point_fixture import contract_tax_basis

    source, month, day = adopted_advance_tax(company, path)
    fields = dict(period="2026-05", advance_id="inherited-advance", fulfilled_gross_fen=50500)
    if supplied == "explicit":
        fields.update(tax_obligation_period=month, tax_obligation_date=day)
    elif supplied in {"basis", "basis_month_only"}:
        contract_tax_basis(
            company.engine, "advance_fulfillment", "inherited-fulfilled",
            business_period="2026-05", contract_period=month,
            contract_date=day if supplied == "basis" else None,
        )
    save_explicit_tax_fact(
        company, transactions.AdvanceFulfillment, "inherited-fulfilled", **fields,
    )
    company.publish("inherited-fulfilled")
    result = company.current("advance_fulfillment", "inherited-fulfilled")
    assert result.values["tax_source_calculation"] == source.id
    assert result.values["net_sales_fen"] == result.values["accrued_vat_fen"] == 0
    assert lines(company, "advance_fulfillment", "inherited-fulfilled") == [
        ("2203", 50000, 0), ("5001", 0, 50000),
    ]


@pytest.mark.parametrize("path", ["direct", "basis", "later"])
@pytest.mark.parametrize("supplied,field", [
    ("different_month", "tax_obligation_period"),
    ("different_day", "tax_obligation_date"),
    ("different_basis", "tax_point_basis"),
])
def test_already_taxed_fulfillment_rejects_contradictory_tax_sources_atomically(
    company, path, supplied, field,
):
    from tax_point_fixture import contract_tax_basis

    source, month, _ = adopted_advance_tax(company, path)
    fields = dict(period="2026-05", advance_id="inherited-advance", fulfilled_gross_fen=50500)
    if supplied == "different_month":
        fields["tax_obligation_period"] = "2026-05"
    elif supplied == "different_day":
        fields.update(tax_obligation_period=month, tax_obligation_date=month + "-06")
    else:
        contract_tax_basis(company.engine, "advance_fulfillment", "contradictory-fulfilled",
                           business_period="2026-05", contract_period="2026-05")
    save_explicit_tax_fact(
        company, transactions.AdvanceFulfillment, "contradictory-fulfilled", **fields,
    )
    with company.engine.store.connection(read_only=True) as connection:
        epochs = company.engine.store.epochs(connection)
        calculations = connection.execute("SELECT count(*) FROM calculation").fetchone()[0]
        vouchers = connection.execute("SELECT count(*) FROM voucher_version").fetchone()[0]
    with pytest.raises(NeedsInformation) as failure:
        company.engine.preview(["contradictory-fulfilled"])
    assert failure.value.issues[0]["field"] == field
    assert source.id in failure.value.issues[0]["reusable_sources"]
    with company.engine.store.connection(read_only=True) as connection:
        assert company.engine.store.epochs(connection) == epochs
        assert connection.execute("SELECT count(*) FROM calculation").fetchone()[0] == calculations
        assert connection.execute("SELECT count(*) FROM voucher_version").fetchone()[0] == vouchers


@pytest.mark.parametrize("supplied,field", [("explicit", "tax_obligation_date"),
                                          ("basis", "tax_point_basis")])
def test_already_taxed_fulfillment_does_not_invent_an_adopted_actual_day(company, supplied, field):
    from tax_point_fixture import contract_tax_basis

    source, month, _ = adopted_advance_tax(company, "direct", with_day=False)
    fields = dict(period="2026-05", advance_id="inherited-advance", fulfilled_gross_fen=50500)
    if supplied == "explicit":
        fields.update(tax_obligation_period=month, tax_obligation_date="2026-01-05")
    else:
        contract_tax_basis(company.engine, "advance_fulfillment", "invented-day",
                           business_period="2026-05", contract_period=month,
                           contract_date="2026-01-05")
    save_explicit_tax_fact(company, transactions.AdvanceFulfillment, "invented-day", **fields)
    with pytest.raises(NeedsInformation) as failure:
        company.engine.preview(["invented-day"])
    assert failure.value.issues[0]["field"] == field
    assert source.id in failure.value.issues[0]["reusable_sources"]
    assert "day" in failure.value.issues[0]["allowed_precision"]


@pytest.mark.parametrize("tax_origin", ["completion", "contract", "invoice"])
def test_commission_source_point_drives_revenue_late_receipt_assessment_and_filing(
    company, tax_origin
):
    engine = company.engine
    deferred = tax_origin == "contract"
    review_month = "2026-04" if deferred else "2026-03"
    proof = engine.register_evidence(
        json.dumps(
            {
                "business": "commission-sale",
                "gross_fen": 149400,
                "service_completed": "2026-03-31",
                "contract_status": "no_written_contract"
                if tax_origin == "completion"
                else "payment_date_set",
                "contract_payment_date": None if tax_origin == "completion" else "2026-04-10",
                "invoice_date": "2026-03-05" if tax_origin == "invoice" else None,
                "review": "all earlier invoices, payment rights and receipts checked through "
                + review_month,
            }
        ).encode(),
        "text/plain",
        "合成佣金服务税点原件",
        request_id=company.request(),
    )["digest"]
    supporting_text(engine, proof, period=review_month)
    with engine.store.connection(read_only=True) as connection:
        original = engine.store.current_fact(connection, "synthetic-support:" + proof)
    source = {
        "kind": original.fact.kind,
        "subject_id": original.subject_id,
        "fact_id": original.id,
        "evidence_digest": proof,
        "location": "synthetic-confirmation",
    }
    fields = {
        "period": review_month,
        "business_kind": "service_sale",
        "business_id": "commission-sale",
        "service_pattern": "single",
        "earlier_candidates_reviewed": True,
        "reviewed_through_period": review_month,
        "review_source": source,
        "contract_source": source,
        "written_contract_status": "no_written_contract"
        if tax_origin == "completion"
        else "payment_date_set",
        "performance_completion_period": "2026-03",
        "performance_completion_date": "2026-03-31",
        "performance_source": source,
    }
    if tax_origin != "completion":
        fields |= {"contract_payment_period": "2026-04", "contract_payment_date": "2026-04-10"}
    if tax_origin == "invoice":
        fields["candidates"] = [
            {
                "trigger": "invoice",
                "period": "2026-03",
                "actual_date": "2026-03-05",
                "gross_start_fen": 0,
                "gross_end_fen": 149400,
                "source": source,
            }
        ]
    basis = TaxPointBasis.model_validate_json(json.dumps(fields))
    engine.save_fact(
        basis.kind,
        "commission-tax-basis",
        basis.model_dump(mode="json"),
        evidence=[proof],
        expected_revision=0,
        request_id=company.request(),
    )
    company.save(
        transactions.ServiceSale,
        "commission-sale",
        period="2026-03",
        customer_id="customer",
        gross_fen=149400,
        fulfillment_date="2026-03-31",
        vat_policy_id="vat",
        exemption_eligible=False,
    )
    company.publish("commission-sale")
    revenue = company.current("service_sale", "commission-sale")
    assert revenue.values["recognized_revenue_fen"] == 147921
    assert revenue.values["accrued_vat_fen"] == (0 if deferred else 1479)
    assert revenue.values["tax_point_basis"]["intervals"][0]["trigger"] == (
        "invoice" if tax_origin == "invoice" else "contract"
    )

    def assess_quarter(quarter):
        start, end = ("2026-01-01", "2026-03-31") if quarter == 1 else ("2026-04-01", "2026-06-30")
        subject = f"commission-assessment-q{quarter}"
        company.save(
            taxes.TaxAssessment,
            subject,
            period=end[:7],
            period_start=start,
            period_end=end,
            vat_policy_id="vat",
            surtax_policy_id="surtax",
        )
        company.publish(subject)
        return company.current("tax_assessment", subject)

    first_assessment = assess_quarter(1)
    assert first_assessment.values["payable_vat_fen"] == (0 if deferred else 1480)
    if deferred:
        # This is the already evidenced contractual right, independently of the later cash.
        company.save(
            transactions.ServiceTaxPoint,
            "commission-april-point",
            period="2026-04",
            sale_id="commission-sale",
            trigger="declared",
            declaration_confirmed=True,
        )
        company.publish("commission-april-point")
    receipt(company, "commission-late-receipt", 149400, "2026-05-15", source="commission-sale")
    company.publish("commission-late-receipt")
    assert not company.current("payment", "commission-late-receipt").values.get("tax_sales")
    assert company.balance("payment:service_sale:commission-sale:primary") == 0
    second_assessment = assess_quarter(2)
    assert second_assessment.values["payable_vat_fen"] == (1480 if deferred else 0)
    for quarter, assessment in ((1, first_assessment), (2, second_assessment)):
        filing = TaxFilingData(engine).query(2026, quarter)
        vat_fields = {
            f["field_id"]: f
            for f in filing["fields"]
            if f["section"] == "vat_and_surtax" and f["period_basis"] == "current"
        }
        field = vat_fields["vat_and_surtax.payable_vat_fen"]
        assert field["status"] == "ready"
        assert field["value_fen"] == assessment.values["payable_vat_fen"]
        assert {"type": "calculation_id", "value": assessment.id} in field["source_basis"]


def attach_receipt_sources(company, *subjects):
    """Append a fresh review that explicitly adopts exact fictional receipts."""

    with company.engine.store.connection(read_only=True) as connection:
        basis = company.engine.store.current_fact(connection, "tax-basis:sale")
        receipts = [company.engine.store.current_fact(connection, s) for s in subjects]
    data = basis.fact.model_dump(mode="json")
    original = {
        "review": "all earlier invoices, contractual rights and exact receipts checked",
        "receipts": [
            {"fact_id": r.id, "date": str(r.fact.actual_date), "gross_fen": r.fact.amount_fen}
            for r in receipts
        ],
    }
    proof = company.engine.register_evidence(
        json.dumps(original).encode(),
        "text/plain",
        "合成完整税点复核",
        request_id=company.request(),
    )["digest"]
    supporting_text(company.engine, proof, period=data["period"])
    with company.engine.store.connection(read_only=True) as connection:
        review = company.engine.store.current_fact(connection, "synthetic-support:" + proof)
    data["review_source"] = {
        "kind": review.fact.kind,
        "subject_id": review.subject_id,
        "fact_id": review.id,
        "evidence_digest": proof,
        "location": "synthetic-confirmation",
    }
    total, candidates = 0, []
    for r in sorted(receipts, key=lambda r: (r.fact.actual_date, r.subject_id)):
        candidates.append(
            {
                "trigger": "receipt",
                "period": str(r.fact.period),
                "actual_date": str(r.fact.actual_date),
                "gross_start_fen": total,
                "gross_end_fen": total + r.fact.amount_fen,
                "receipt_stage": "during_or_after_transaction",
                "source": {
                    "kind": r.fact.kind,
                    "subject_id": r.subject_id,
                    "fact_id": r.id,
                    "evidence_digest": r.evidence[0],
                    "location": "synthetic actual receipt",
                },
            }
        )
        total += r.fact.amount_fen
    data["candidates"] = candidates
    company.engine.save_fact(
        "tax_point_basis",
        basis.subject_id,
        data,
        evidence=tuple(sorted({*basis.evidence, proof, *(r.evidence[0] for r in receipts)})),
        expected_revision=basis.revision,
        request_id=company.request(),
    )


def test_partial_receipts_before_contract_due_transfer_only_received_ranges_without_cycle(company):
    sale(company, tax_month="2026-06")
    receipt(company, "first", 40000, "2026-04-02")
    receipt(company, "second", 109400, "2026-05-10")
    attach_receipt_sources(company, "first", "second")
    company.publish("sale", "first", "second")
    assert lines(company, "payment", "first") == [
        ("1002", 40000, 0),
        ("1122", 0, 40000),
        ("222104", 396, 0),
        ("222101", 0, 396),
    ]
    assert lines(company, "payment", "second") == [
        ("1002", 109400, 0),
        ("1122", 0, 109400),
        ("222104", 1083, 0),
        ("222101", 0, 1083),
    ]
    assert company.current("payment", "first").values["tax_sales"][0]["net_sales_fen"] == 39604
    assert company.current("payment", "second").values["tax_sales"][0]["net_sales_fen"] == 108317


def test_revenue_month_needs_no_invented_fulfillment_day(company):
    result = sale(company)
    assert result.values["recognized_revenue_fen"] == 147921
    assert result.values["net_sales_fen"] == result.values["accrued_vat_fen"] == 0
    assert result.values["tax_obligation_date"] is None
    assert lines(company, "service_sale", "sale") == [
        ("1122", 149400, 0),
        ("5001", 0, 147921),
        ("222104", 0, 1479),
    ]


def test_full_contractual_entitlement_then_partial_receipt_transfers_vat_once(company):
    sale(company, tax_obligation_date="2026-04-02")
    receipt(company, "first", 40000, "2026-04-02")
    company.publish("first")
    assert lines(company, "payment", "first") == [
        ("1002", 40000, 0),
        ("1122", 0, 40000),
        ("222104", 1479, 0),
        ("222101", 0, 1479),
    ]
    receipt(company, "second", 109400, "2026-05-10")
    company.publish("second")
    assert lines(company, "payment", "second") == [("1002", 109400, 0), ("1122", 0, 109400)]
    company.save(
        transactions.ServiceTaxPoint,
        "receipt-proof",
        period="2026-04",
        sale_id="sale",
        trigger="receipt",
        payment_id="first",
    )
    company.publish("receipt-proof")
    assert lines(company, "service_tax_point", "receipt-proof") == []
    company.assess("march", "2026-03")
    company.save(
        taxes.TaxAssessment,
        "april",
        period="2026-04",
        period_start="2026-04-01",
        period_end="2026-04-30",
        vat_policy_id="vat",
        surtax_policy_id="surtax",
    )
    company.publish("march", "april")
    assert company.current("tax_assessment", "march").values["payable_vat_fen"] == 0
    assert company.current("tax_assessment", "april").values["payable_vat_fen"] == 1480
    assert company.current("tax_assessment", "april").values["accrued_vat_fen"] == 1479
    assert company.current("tax_assessment", "april").values["net_sales_fen"] == 147921


@pytest.mark.parametrize("day", ["2026-03-31", "2026-04-01", "2026-05-02"])
def test_actual_receipt_cannot_infer_a_different_declared_tax_point(company, day):
    sale(company, tax_obligation_date="2026-04-02")
    receipt(company, "wrong", 40000, day)
    with pytest.raises(NeedsInformation) as error:
        company.engine.preview(["wrong"])
    assert error.value.issues[0]["field"] in {"tax_point_basis", "tax_point_basis.candidates"}
    assert company.balance("bank") == 0


def test_declared_tax_point_then_receipt_does_not_repeat_vat(company):
    sale(company)
    company.save(
        transactions.ServiceTaxPoint,
        "tax-point",
        period="2026-04",
        sale_id="sale",
        trigger="declared",
        declaration_confirmed=True,
    )
    company.publish("tax-point")
    assert lines(company, "service_tax_point", "tax-point") == [
        ("222104", 1479, 0),
        ("222101", 0, 1479),
    ]
    receipt(company, "paid", 149400, "2026-05-10")
    company.publish("paid")
    assert len(lines(company, "payment", "paid")) == 2
    company.save(
        transactions.ServiceTaxPoint,
        "duplicate",
        period="2026-04",
        sale_id="sale",
        trigger="declared",
        declaration_confirmed=True,
    )
    with pytest.raises(KernelError, match="重复|一次"):
        company.engine.preview(["duplicate"])


def test_same_month_tax_point_posts_directly_and_payment_does_not_repeat(company):
    sale(company, tax_month="2026-03")
    assert lines(company, "service_sale", "sale")[-1] == ("222101", 0, 1479)
    receipt(company, "paid", 149400, "2026-03-12")
    company.publish("paid")
    assert len(lines(company, "payment", "paid")) == 2


def test_missing_tax_month_and_partial_month_policy_need_actual_facts(company):
    company.save(
        transactions.ServiceSale,
        "missing",
        period="2026-03",
        customer_id="customer",
        gross_fen=101000,
        vat_policy_id="vat",
        exemption_eligible=False,
    )
    with pytest.raises(NeedsInformation) as error:
        company.engine.preview(["missing"])
    assert error.value.issues[0]["field"] == "tax_obligation_period"
    company.save(
        taxes.VatPolicyFact,
        "partial-policy",
        period="2026-03",
        policy=vat_policy().model_dump(mode="json") | {"effective_from": "2026-03-15"},
    )
    company.save(
        transactions.ServiceSale,
        "partial",
        period="2026-03",
        customer_id="customer",
        gross_fen=101000,
        vat_policy_id="partial-policy",
        exemption_eligible=False,
        tax_obligation_period="2026-03",
    )
    with pytest.raises(NeedsInformation) as error:
        company.engine.preview(["partial"])
    assert error.value.issues[0]["field"] == "tax_point_basis.contract_payment_date"
    assert error.value.issues[0]["allowed_precision"] == ["day"]
    assert error.value.issues[0]["reusable_sources"]


def test_taxed_advance_partial_fulfillment_uses_cumulative_net_without_vat_twice(company):
    company.save(
        taxes.VatPolicyFact,
        "vat-three",
        period="2026-01",
        policy=vat_policy().model_dump(mode="json") | {"rate_percent": "3"},
    )
    company.save(
        transactions.Advance,
        "advance",
        period="2026-01",
        counterparty_id="customer",
        amount_fen=100,
        side="customer",
        contractual_obligation_established=True,
        vat_due_on_advance=True,
        vat_policy_id="vat-three",
        exemption_eligible=False,
        tax_obligation_period="2026-01",
    )
    company.publish("advance")
    receipt(company, "prepaid", 100, "2026-01-05", source_kind="advance", source="advance")
    company.publish("prepaid")
    assert lines(company, "advance", "advance") == [
        ("1122", 100, 0),
        ("2203", 0, 97),
        ("222101", 0, 3),
    ]
    for subject, period in (("first", "2026-02"), ("second", "2026-03")):
        company.save(
            transactions.AdvanceFulfillment,
            subject,
            period=period,
            advance_id="advance",
            fulfilled_gross_fen=50,
        )
        company.publish(subject)
    assert lines(company, "advance_fulfillment", "first") == [("2203", 49, 0), ("5001", 0, 49)]
    assert lines(company, "advance_fulfillment", "second") == [("2203", 48, 0), ("5001", 0, 48)]
    assert company.balance("advance:advance:advance") == 0
    company.save(
        transactions.AdvanceFulfillment,
        "excess",
        period="2026-05",
        advance_id="advance",
        fulfilled_gross_fen=1,
    )
    with pytest.raises(KernelError, match="不能超过"):
        company.engine.preview(["excess"])


def test_untaxed_advance_requires_explicit_fulfillment_tax_point(company):
    from tax_point_fixture import contract_tax_basis

    contract_tax_basis(
        company.engine, "advance", "advance", business_period="2026-01", contract_period="2026-03"
    )
    company.save(
        transactions.Advance,
        "advance",
        period="2026-01",
        counterparty_id="customer",
        amount_fen=101000,
        side="customer",
        contractual_obligation_established=True,
        vat_due_on_advance=False,
    )
    company.publish("advance")
    company.save(
        transactions.AdvanceFulfillment,
        "fulfilled",
        period="2026-03",
        advance_id="advance",
        fulfilled_gross_fen=101000,
    )
    with pytest.raises(NeedsInformation) as error:
        company.engine.preview(["fulfilled"])
    assert error.value.issues[0]["field"] == "vat_policy_id"
    company.save(
        transactions.AdvanceFulfillment,
        "fulfilled",
        period="2026-03",
        advance_id="advance",
        fulfilled_gross_fen=101000,
        vat_policy_id="vat",
        exemption_eligible=False,
        tax_obligation_period="2026-03",
    )
    company.publish("fulfilled")
    assert lines(company, "advance_fulfillment", "fulfilled") == [
        ("2203", 101000, 0),
        ("5001", 0, 100000),
        ("222101", 0, 1000),
    ]


@pytest.mark.parametrize("prior_refund", [0, 40000])
def test_phased_prepaid_service_independent_tax_point_then_partial_fulfillment_and_refund(
    company, prior_refund
):
    from tax_point_fixture import contract_tax_basis

    contract_tax_basis(
        company.engine, "advance", "advance", business_period="2026-01", contract_period="2026-06"
    )
    company.save(
        transactions.Advance,
        "advance",
        period="2026-01",
        counterparty_id="customer",
        amount_fen=149400,
        side="customer",
        contractual_obligation_established=True,
        vat_due_on_advance=False,
        vat_policy_id="vat",
        exemption_eligible=False,
    )
    company.publish("advance")
    receipt(company, "prepaid", 149400, "2026-01-10", source_kind="advance", source="advance")
    company.publish("prepaid")
    if prior_refund:
        company.save(
            transactions.AdvanceRefund,
            "prior-refund",
            period="2026-02",
            advance_id="advance",
            refunded_gross_fen=prior_refund,
            refund_right_confirmed=True,
        )
        company.publish("prior-refund")
    with company.engine.store.connection(read_only=True) as connection:
        basis = company.engine.store.current_fact(connection, "tax-basis:advance")
        paid = company.engine.store.current_fact(connection, "prepaid")
    data = basis.fact.model_dump(mode="json")
    document = {
        "service_pattern": "phased",
        "first_actual_service_period": "2026-04",
        "receipt_fact_id": paid.id,
        "received_gross_fen": 149400,
        "earlier_invoice_and_payment_rights_reviewed": True,
    }
    proof = company.engine.register_evidence(
        json.dumps(document).encode(),
        "text/plain",
        "合成分次服务首次开始依据",
        request_id=company.request(),
    )["digest"]
    supporting_text(company.engine, proof, period="2026-06")
    with company.engine.store.connection(read_only=True) as connection:
        original = company.engine.store.current_fact(connection, "synthetic-support:" + proof)
    ref = {
        "kind": original.fact.kind,
        "subject_id": original.subject_id,
        "fact_id": original.id,
        "evidence_digest": proof,
        "location": "synthetic-confirmation",
    }
    data.update(
        service_pattern="phased",
        first_service_started=True,
        first_service_period="2026-04",
        first_service_source=ref,
        review_source=ref,
        candidates=[
            {
                "trigger": "receipt",
                "period": "2026-01",
                "actual_date": "2026-01-10",
                "gross_start_fen": 0,
                "gross_end_fen": 149400,
                "receipt_stage": "before_transaction",
                "source": {
                    "kind": paid.fact.kind,
                    "subject_id": paid.subject_id,
                    "fact_id": paid.id,
                    "evidence_digest": paid.evidence[0],
                    "location": "synthetic actual prepaid receipt",
                },
            }
        ],
    )
    company.engine.save_fact(
        "tax_point_basis",
        basis.subject_id,
        data,
        evidence=tuple(sorted({*basis.evidence, proof, paid.evidence[0]})),
        expected_revision=basis.revision,
        request_id=company.request(),
    )
    company.save(
        transactions.ServiceTaxPoint,
        "advance-point",
        period="2026-04",
        sale_id="advance",
        trigger="declared",
        declaration_confirmed=True,
    )
    company.save(
        transactions.AdvanceFulfillment,
        "first-performance",
        period="2026-05",
        advance_id="advance",
        fulfilled_gross_fen=40000,
    )
    company.publish("advance", "advance-point", "first-performance")
    vat_due = 1479 if prior_refund == 0 else 1083
    assert lines(company, "service_tax_point", "advance-point") == [
        ("2203", vat_due, 0),
        ("222101", 0, vat_due),
    ]
    assert lines(company, "advance_fulfillment", "first-performance") == [
        ("2203", 39604, 0),
        ("5001", 0, 39604),
    ]
    assert company.current("service_tax_point", "advance-point").values["net_sales_fen"] == (
        147921 if prior_refund == 0 else 108317
    )
    assert company.current("advance_fulfillment", "first-performance").values["net_sales_fen"] == 0
    company.save(
        taxes.TaxAssessment,
        "q2",
        period="2026-06",
        period_start="2026-04-01",
        period_end="2026-06-30",
        vat_policy_id="vat",
        surtax_policy_id="surtax",
    )
    company.publish("q2")
    tax_source = company.current("service_tax_point", "advance-point").id
    assert (
        tax_source in company.current("tax_assessment", "q2").values["vat_source_calculation_ids"]
    )
    company.save(
        transactions.AdvanceRefund,
        "refund-rest",
        period="2026-07",
        advance_id="advance",
        refunded_gross_fen=109400 - prior_refund,
        refund_right_confirmed=True,
    )
    company.publish("refund-rest")
    refunded = company.current("advance_refund", "refund-rest").values
    assert (
        refunded["returned_net_fen"],
        refunded["credit_note_vat_fen"],
        refunded["original_sale_period"],
    ) == (108317 if prior_refund == 0 else 68713, 1083 if prior_refund == 0 else 687, "2026-04")
    assert refunded["tax_source_calculation"] == tax_source
    assert company.balance("advance:advance:advance") == 0
    company.save(
        taxes.TaxCreditConfirmation,
        "phased-credit",
        period="2026-08",
        original_assessment_id="q2",
        original_period_start="2026-04-01",
        original_period_end="2026-06-30",
        returns=[{"return_id": "refund-rest", "original_advance_id": "advance"}],
        authority_confirmed=True,
        original_filing_reference="synthetic-q2-filing",
        confirmation_reference="synthetic-phased-refund-credit",
        original_filing_evidence=company.filing,
        confirmation_evidence=company.confirmation,
        confirmation_date="2026-08-05",
        disposition="refund_or_offset",
        vat_credit_fen=1083 if prior_refund == 0 else 687,
        surtax_credit_fen=0,
        exemption_reversal_fen=0,
    )
    company.publish("phased-credit")
    assert company.current("tax_credit_confirmation", "phased-credit").values["vat_credit_fen"] == (
        1083 if prior_refund == 0 else 687
    )
    company.save(
        transactions.ServiceTaxPoint,
        "duplicate-point",
        period="2026-04",
        sale_id="advance",
        trigger="declared",
        declaration_confirmed=True,
    )
    with pytest.raises(KernelError, match="重复"):
        company.engine.preview(["duplicate-point"])


def test_return_before_tax_point_reduces_deferred_source_then_receipt_transfers_remainder(company):
    sale(company, period="2026-01", tax_month="2026-03", gross_fen=202000)
    company.save(
        transactions.SaleReturn,
        "returned",
        period="2026-02",
        sale_id="sale",
        returned_gross_fen=101000,
        credit_note_vat_fen=1000,
        customer_id="customer",
    )
    company.publish("returned")
    assert lines(company, "sale_return", "returned")[-1] == ("222104", 1000, 0)
    receipt(company, "paid", 101000, "2026-03-10")
    company.publish("paid")
    assert lines(company, "payment", "paid")[-2:] == [("222104", 1000, 0), ("222101", 0, 1000)]


@pytest.mark.parametrize("path", ["declared", "bank", "cash", "platform"])
def test_full_deferred_tax_point_preserves_confirmed_red_note_allocation(company, path):
    from ai_accounting.kernel.domains.cash import CashPayment
    from ai_accounting.kernel.domains.platforms import PlatformPayment

    sale(company, gross_fen=100)
    company.save(transactions.SaleReturn, "red-note", period="2026-03", sale_id="sale",
                 returned_gross_fen=49, credit_note_vat_fen=1, customer_id="customer")
    company.publish("red-note")
    if path == "declared":
        company.save(transactions.ServiceTaxPoint, "tax-rest", period="2026-04",
                     sale_id="sale", trigger="declared", declaration_confirmed=True)
        company.publish("tax-rest")
        values = company.current("service_tax_point", "tax-rest").values
    else:
        model, account = {
            "bank": (transactions.Payment, {"bank_account_id": "bank"}),
            "cash": (CashPayment, {"cash_account_id": "cash"}),
            "platform": (PlatformPayment, {"platform_account_id": "platform"}),
        }[path]
        if path == "platform":
            from entity_fixture import seed_registration_entities

            row_data = dict(period="2026-04", actual_date="2026-04-10", direction="inflow",
                            platform_account_id="platform", amount_fen=51,
                            source_location="synthetic platform row 1")
            proof = company.engine.register_evidence(
                json.dumps(row_data).encode(), "text/plain", "合成平台收款原行",
                request_id=company.request(),
            )["digest"]
            supporting_text(company.engine, proof, period="2026-04")
            row_data["source_evidence_digest"] = proof
            seed_registration_entities(company.engine, "platform_movement", row_data)
            company.engine.save_fact(
                "platform_movement", "platform-row", row_data, evidence=(proof,),
                expected_revision=0, request_id=company.request(),
            )
            company.publish("platform-row")
            account["movement_ids"] = ["platform-row"]
        company.save(model, "tax-rest", period="2026-04", actual_date="2026-04-10",
                     direction="inflow", counterparty_id="customer", amount_fen=51,
                     allocations=[dict(source_kind="service_sale", source_id="sale",
                                       obligation="primary", amount_fen=51)], **account)
        company.publish("tax-rest")
        values = company.current(model.kind, "tax-rest").values["tax_sales"][0]
    assert (values["net_sales_fen"], values["accrued_vat_fen"]) == (51, 0)
    with company.engine.store.connection(read_only=True) as connection:
        assert connection.execute(
            "SELECT coalesce(sum(credit-debit),0) FROM monthly_account WHERE account='222104'",
        ).fetchone()[0] == 0


def test_partial_tax_point_after_return_requires_the_actual_return_scope(company):
    sale(company, gross_fen=100, tax_month="2026-06")
    company.save(transactions.SaleReturn, "red-note", period="2026-03", sale_id="sale",
                 returned_gross_fen=49, credit_note_vat_fen=1, customer_id="customer")
    company.publish("red-note")
    receipt(company, "partial-rest", 25, "2026-04-10")
    attach_receipt_sources(company, "partial-rest")
    with company.engine.store.connection(read_only=True) as connection:
        epochs = company.engine.store.epochs(connection)
        calculation_count = connection.execute("SELECT count(*) FROM calculation").fetchone()[0]
    with pytest.raises(NeedsInformation) as failure:
        company.engine.preview(["sale", "partial-rest"])
    assert any(issue["field"] == "return_tax_scope" for issue in failure.value.issues)
    with company.engine.store.connection(read_only=True) as connection:
        assert company.engine.store.epochs(connection) == epochs
        assert connection.execute(
            "SELECT count(*) FROM calculation"
        ).fetchone()[0] == calculation_count


def test_complete_pre_tax_return_records_zero_at_its_declared_tax_point(company):
    sale(company, gross_fen=100)
    company.save(transactions.SaleReturn, "full-red-note", period="2026-03", sale_id="sale",
                 returned_gross_fen=100, credit_note_vat_fen=1, customer_id="customer")
    company.publish("full-red-note")
    company.save(transactions.ServiceTaxPoint, "zero-tax-point", period="2026-04",
                 sale_id="sale", trigger="declared", declaration_confirmed=True)
    company.publish("zero-tax-point")
    values = company.current("service_tax_point", "zero-tax-point").values
    assert (values["net_sales_fen"], values["accrued_vat_fen"]) == (0, 0)
    assert lines(company, "service_tax_point", "zero-tax-point") == []


@pytest.mark.parametrize("path", ["declared", "receipt"])
def test_later_return_after_full_remaining_tax_point_uses_that_adopted_tax_month(company, path):
    sale(company, gross_fen=100)
    company.save(transactions.SaleReturn, "before-tax-return", period="2026-03", sale_id="sale",
                 returned_gross_fen=49, credit_note_vat_fen=1, customer_id="customer")
    company.publish("before-tax-return")
    before = company.current("sale_return", "before-tax-return")
    if path == "declared":
        company.save(transactions.ServiceTaxPoint, "tax-rest", period="2026-04",
                     sale_id="sale", trigger="declared", declaration_confirmed=True)
    else:
        receipt(company, "tax-rest", 51, "2026-04-10")
    company.publish("tax-rest")
    company.save(transactions.SaleReturn, "after-tax-return", period="2026-05", sale_id="sale",
                 returned_gross_fen=51, credit_note_vat_fen=0, customer_id="customer")
    company.publish("after-tax-return")
    values = company.current("sale_return", "after-tax-return").values
    assert (values["original_sale_period"], values["net_sales_fen"],
            values["accrued_vat_fen"]) == ("2026-04", -51, 0)
    assert company.current("sale_return", "before-tax-return").id == before.id


def test_later_return_does_not_reopen_earlier_closed_return(march_company):
    company = march_company
    sale(company, tax_month="2026-03", gross_fen=202000)
    company.save(
        transactions.SaleReturn,
        "first-return",
        period="2026-03",
        sale_id="sale",
        returned_gross_fen=50500,
        credit_note_vat_fen=500,
        customer_id="customer",
    )
    company.publish("first-return")
    company.assess("march", "2026-03")
    company.publish("march")
    periods = Periods(company.engine)
    for category in MATERIAL_CATEGORIES:
        evidence = (
            [company.filing, company.confirmation] if category in {"transactions", "tax"} else []
        )
        periods.inventory(
            "2026-03",
            category,
            evidence=evidence,
            expected=len(evidence),
            no_business=not evidence,
            confirmation_evidence=company.confirmation,
            request_id=company.request(),
        )
    preview = periods.preview_close("2026-03", owner_confirmation=company.confirmation)
    periods.close(
        "2026-03",
        owner_confirmation=company.confirmation,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id=company.request(),
    )
    closed = periods.closed_report("2026-03")
    first = company.current("sale_return", "first-return")
    company.save(
        transactions.SaleReturn,
        "later-return",
        period="2026-04",
        sale_id="sale",
        returned_gross_fen=50500,
        credit_note_vat_fen=500,
        customer_id="customer",
    )
    company.publish("later-return")
    assert company.current("sale_return", "first-return").id == first.id
    assert periods.closed_report("2026-03") == closed


def test_taxed_advance_fulfillment_then_refund_releases_exact_net_and_tax(company):
    company.save(
        taxes.VatPolicyFact,
        "vat-three",
        period="2026-01",
        policy=vat_policy().model_dump(mode="json") | {"rate_percent": "3"},
    )
    company.save(
        transactions.Advance,
        "advance",
        period="2026-01",
        counterparty_id="customer",
        amount_fen=100,
        side="customer",
        contractual_obligation_established=True,
        vat_due_on_advance=True,
        vat_policy_id="vat-three",
        exemption_eligible=False,
        tax_obligation_period="2026-01",
    )
    company.publish("advance")
    receipt(company, "prepaid", 100, "2026-01-05", source_kind="advance", source="advance")
    company.publish("prepaid")
    company.save(
        transactions.AdvanceFulfillment,
        "fulfilled",
        period="2026-02",
        advance_id="advance",
        fulfilled_gross_fen=50,
    )
    company.publish("fulfilled")
    company.save(
        transactions.AdvanceRefund,
        "refund",
        period="2026-03",
        advance_id="advance",
        refunded_gross_fen=50,
        refund_right_confirmed=True,
    )
    company.publish("refund")
    assert lines(company, "advance_refund", "refund") == [
        ("2203", 0, 50),
        ("2203", 48, 0),
        ("222101", 2, 0),
    ]
    assert company.balance("advance:advance:advance") == 0
    company.save(
        transactions.Payment,
        "paid-back",
        period="2026-03",
        actual_date="2026-03-10",
        direction="outflow",
        bank_account_id="bank",
        counterparty_id="customer",
        amount_fen=50,
        allocations=[
            dict(
                source_kind="advance_refund",
                source_id="refund",
                obligation="primary",
                amount_fen=50,
            )
        ],
    )
    company.publish("paid-back")
    assert company.balance("advance_refund:refund:primary") == 0
    assert company.balance("bank") == 50
    company.save(
        transactions.AdvanceRefund,
        "duplicate",
        period="2026-05",
        advance_id="advance",
        refunded_gross_fen=1,
        refund_right_confirmed=True,
    )
    with pytest.raises(KernelError, match="不能超过"):
        company.engine.preview(["duplicate"])


def test_same_period_full_taxed_advance_refund_nets_tax_source_to_zero(company):
    company.save(
        transactions.Advance,
        "advance",
        period="2026-01",
        counterparty_id="customer",
        amount_fen=101000,
        side="customer",
        contractual_obligation_established=True,
        vat_due_on_advance=True,
        vat_policy_id="vat",
        exemption_eligible=False,
        tax_obligation_period="2026-01",
    )
    company.save(
        transactions.AdvanceRefund,
        "refund",
        period="2026-01",
        advance_id="advance",
        refunded_gross_fen=101000,
        refund_right_confirmed=True,
    )
    company.publish("advance", "refund")
    company.assess("january", "2026-01")
    company.publish("january")
    result = company.current("tax_assessment", "january")
    assert result.values["net_sales_fen"] == result.values["payable_vat_fen"] == 0


def test_cross_period_advance_refund_uses_explicit_filed_tax_credit(company):
    company.save(
        transactions.Advance,
        "advance",
        period="2026-01",
        counterparty_id="customer",
        amount_fen=202000,
        side="customer",
        contractual_obligation_established=True,
        vat_due_on_advance=True,
        vat_policy_id="vat",
        exemption_eligible=False,
        tax_obligation_period="2026-01",
    )
    company.publish("advance")
    company.assess("january", "2026-01")
    company.publish("january")
    company.save(
        transactions.AdvanceRefund,
        "returned",
        period="2026-02",
        advance_id="advance",
        refunded_gross_fen=101000,
        refund_right_confirmed=True,
    )
    company.publish("returned")
    company.save(
        taxes.TaxCreditConfirmation,
        "credit",
        **company.credit_fields(
            returns=[
                dict(
                    return_id="returned",
                    original_advance_id="advance",
                )
            ]
        ),
    )
    company.assess("february", "2026-02", ["credit"])
    company.publish("credit", "february")
    assert company.balance("tax_assessment:february:vat_credit_credit") == 1000
    assert company.balance("tax_assessment:february:surtax_credit_credit") == 60
