"""Filing consumers retain accounting scope and exact adopted sources."""

import json

import pytest
from pydantic import ValidationError
from test_local_service import service as service
from test_reports import book as book
from test_reports import scenario

from ai_accounting.kernel.reporting_fields import payroll_import_fields
from ai_accounting.kernel.reports import Reports
from ai_accounting.kernel.response_contracts import RESPONSE_ADAPTERS
from ai_accounting.kernel.tax_filing_data import TaxFilingData
from ai_accounting.kernel.types import digest


def cit_fields(result):
    return [field for field in result["fields"] if field["section"] == "cit"]


def test_pre_tax_fields_use_real_report_rows_without_income_tax_confirmation(book):
    report = scenario(book, tax=False)
    ordinary = report.report(2026, 1, source="open")
    assert any(issue["field"] == "report_income_tax_confirmation"
               for issue in ordinary["fact_issues"])
    result = TaxFilingData(book[0]).query(2026, 1)
    for field in cit_fields(result):
        assert field["status"] == "ready"
        line = {"cit.operating_revenue": 1, "cit.operating_cost": 2,
                "cit.profit_before_tax": 30}[field["field_id"]]
        assert field["value_fen"] == ordinary["statements"]["profit_statement"][str(line)][
            field["period_basis"] + "_fen"
        ]
        assert any(source["type"] == "report_basis_digest" for source in field["source_basis"])
        if line == 30:
            assert {"type": "tax_form_field", "value": "A200000.18"} in field["source_basis"]
    assert all(field["value_fen"] is None for field in result["fields"]
               if field["section"] == "vat_and_surtax")
    assert result["status"] == "needs_information"
    RESPONSE_ADAPTERS["tax_filing_data"].validate_python(result)


def test_pending_income_tax_is_independent_but_pending_expense_blocks_pre_tax(book):
    scenario(book, tax=False)
    engine, save, publish, close = book
    save("income_tax_assessment", "tax-pending", {
        "period": "2026-03", "year": 2026, "cumulative_assessed_fen": 1,
        "assessment_basis": "confirmed_provision",
    })
    assert all(field["status"] == "ready"
               for field in cit_fields(TaxFilingData(engine).query(2026, 1)))
    save("expense", "new-cost", {
        "period": "2026-03", "counterparty_id": "supplier", "amount_fen": 500,
        "expense_class": "administration", "creditor_kind": "supplier",
    })
    fields = cit_fields(TaxFilingData(engine).query(2026, 1))
    assert all(field["value_fen"] is None for field in fields)
    assert any(issue["field"] == "pending" for field in fields for issue in field["fact_issues"])


def test_one_snapshot_owns_both_report_and_tax_field_reads(book, monkeypatch):
    scenario(book, tax=False)
    connections = []
    original = Reports._report
    tax_read = TaxFilingData._tax_calculations

    def report(self, *args, **kwargs):
        reads = kwargs["reads"]
        assert reads._snapshot_active and reads.connection.in_transaction
        connections.append(reads.connection)
        return original(self, *args, **kwargs)

    def tax(self, reads, *args):
        assert connections == [reads.connection]
        assert reads._snapshot_active and reads.connection.in_transaction
        return tax_read(self, reads, *args)

    monkeypatch.setattr(Reports, "_report", report)
    monkeypatch.setattr(TaxFilingData, "_tax_calculations", tax)
    TaxFilingData(book[0]).query(2026, 1)


def test_wire_amount_schema_and_validation_keep_integer_fen(book):
    scenario(book, tax=False)
    result = TaxFilingData(book[0]).query(2026, 1)
    adapter = RESPONSE_ADAPTERS["tax_filing_data"]
    schema = adapter.json_schema()
    amount = schema["$defs"]["TaxFilingField"]["properties"]["value_fen"]
    assert any(item.get("type") == "integer" for item in amount["anyOf"])
    result["fields"][0]["value_fen"] = 0.0
    with pytest.raises(ValidationError):
        adapter.validate_python(result)


def test_field_and_response_digests_bind_values_and_official_mapping(book):
    scenario(book, tax=False)
    result = TaxFilingData(book[0]).query(2026, 1)
    assert result["source_basis_digest"] == digest({
        key: value for key, value in result.items() if key != "source_basis_digest"
    }).hex()
    for field in result["fields"]:
        assert field["source_basis_digest"] == digest({
            key: value for key, value in field.items() if key != "source_basis_digest"
        }).hex()


def test_missing_report_profile_and_closed_months_never_yield_a_filing_zero(book):
    fields = cit_fields(TaxFilingData(book[0]).query(2026, 1, source="closed"))
    assert all(field["status"] == "needs_information" and field["value_fen"] is None
               for field in fields)


def test_filing_income_keeps_its_explicit_meaning_instead_of_accounting_gross():
    from types import SimpleNamespace

    detail = SimpleNamespace(
        tax_reported_salary_fen=800_000, tax_exempt_income_fen=100_000,
        cumulative_special_fen={"education": 50_000}, current_other_fen={"health": 2000},
        cumulative_personal_pension_fen=1000, tax_relief_fen=100, treaty_relief_fen=10,
    )
    shared = payroll_import_fields(detail, [80_000, 0, 0, 0])
    assert shared["tax_reported_salary_fen"] == 800_000
    assert shared["tax_exempt_income_fen"] == 100_000
    assert shared["employee_contributions_fen"] == (80_000, 0, 0, 0)
    assert shared["cumulative_special_fen"] == {"education": 50_000}
    assert shared["current_other_fen"] == {"health": 2000}
    assert shared["tax_relief_fen"] == 100
    assert shared["treaty_relief_fen"] == 10


def vat_company(tmp_path, months=3, *, historical=False, monthly=False):
    from calendar import monthrange
    from dataclasses import replace

    from test_business_domains import surtax_policy, vat_policy
    from test_tax_credits import Company

    from ai_accounting.kernel.domains import taxes

    company = Company(tmp_path / "filing.sqlite")
    evaluator = company.engine.store.registry.evaluators["tax_assessment"]
    if historical:
        def previous_result(version, context):
            result = evaluator(version, context)
            return replace(result, values={
                key: value for key, value in result.values.items()
                if not key.startswith("filing_")
            })

        company.engine.store.registry.evaluators["tax_assessment"] = previous_result
    company.save(taxes.VatPolicyFact, "vat", period="2026-01",
                 policy=vat_policy().model_dump(mode="json"))
    company.save(taxes.SurtaxPolicyFact, "surtax", period="2026-01",
                 policy=surtax_policy().model_dump(mode="json"))
    for month in (range(1, months + 1) if monthly else range(3, months + 1, 3)):
        period = f"2026-{month:02d}"
        company.save(taxes.TaxAssessment, f"tax-{month}", period=period,
                     period_start=f"2026-{month if monthly else month - 2:02d}-01",
                     period_end=f"{period}-{monthrange(2026, month)[1]}",
                     vat_policy_id="vat", surtax_policy_id="surtax")
        company.publish(f"tax-{month}")
    company.engine.store.registry.evaluators["tax_assessment"] = evaluator
    return company


def vat_fields(result, dimension="current"):
    return {field["field_id"]: field for field in result["fields"]
            if field["section"] == "vat_and_surtax" and field["period_basis"] == dimension}


def test_exact_adopted_empty_quarter_proves_zero_and_accepts_january_coverage(tmp_path):
    company = vat_company(tmp_path)
    result = TaxFilingData(company.engine).query(2026, 1)
    fields = vat_fields(result)
    assert all(fields[name]["value_fen"] == 0 and fields[name]["status"] == "ready"
               for name in ("vat_and_surtax.net_sales_fen", "vat_and_surtax.payable_vat_fen",
                            "vat_and_surtax.row_15_fen", "vat_and_surtax.row_20_fen"))
    assert all(fields[f"vat_and_surtax.row_{row}_fen"]["value_fen"] is None
               for row in (21, 22, 23, 24, 25))
    assert any(mapping["effective_from"] == "2026-02-01"
               for mapping in result["mapping_versions"])
    sources = fields["vat_and_surtax.payable_vat_fen"]["source_basis"]
    assert len([item for item in sources if item["type"] == "calculation_id"]) == 1
    assert any(item["type"] == "vat_policy_fact_id" for item in sources)
    assert any(item["type"] == "surtax_policy_fact_id" for item in sources)
    RESPONSE_ADAPTERS["tax_filing_data"].validate_python(result)


def test_missing_or_pending_tax_periods_are_not_zero(tmp_path):
    from ai_accounting.kernel.domains import transactions

    company = vat_company(tmp_path, months=2)
    fields = vat_fields(TaxFilingData(company.engine).query(2026, 1))
    assert all(field["value_fen"] is None for field in fields.values())
    company.save(transactions.ServiceSale, "new-february-sale", period="2026-02",
                 customer_id="customer", gross_fen=202_000, fulfillment_date="2026-02-10",
                 tax_obligation_period="2026-02", vat_policy_id="vat", exemption_eligible=False)
    fields = vat_fields(TaxFilingData(company.engine).query(2026, 1))
    assert all(field["status"] == "needs_information" for field in fields.values())
    assert any(issue.get("subject_id") == "new-february-sale" for field in fields.values()
               for issue in field["fact_issues"])


def test_later_recorded_tax_revision_is_selected_by_real_tax_scope(tmp_path):
    from ai_accounting.kernel.domains import taxes

    company = vat_company(tmp_path)
    company.save(taxes.TaxAssessment, "tax-3", period="2026-04",
                 period_start="2026-01-01", period_end="2026-03-31",
                 vat_policy_id="vat", surtax_policy_id="surtax")
    fields = vat_fields(TaxFilingData(company.engine).query(2026, 1))
    assert all(field["value_fen"] is None for field in fields.values())
    assert any(issue.get("subject_id") == "tax-3" for field in fields.values()
               for issue in field["fact_issues"])
    company.publish("tax-3")
    adopted = vat_fields(TaxFilingData(company.engine).query(2026, 1))[
        "vat_and_surtax.row_20_fen"
    ]
    assert adopted["status"] == "ready" and adopted["value_fen"] == 0
    assert {"type": "posting_period", "value": "2026-04"} in adopted["source_basis"]
    closed = vat_fields(TaxFilingData(company.engine).query(2026, 1, source="closed"))
    assert closed["vat_and_surtax.row_20_fen"]["value_fen"] is None


@pytest.mark.parametrize("period,expected_ready", [("2026-03", False), ("2026-04", True)])
def test_pending_business_blocks_only_its_proven_tax_scope(tmp_path, period, expected_ready):
    from ai_accounting.kernel.domains import transactions

    company = vat_company(tmp_path)
    company.save(transactions.ServiceSale, "new-sale", period=period,
                 customer_id="customer", gross_fen=202_000, fulfillment_date=f"{period}-10",
                 tax_obligation_period=period, vat_policy_id="vat", exemption_eligible=False)
    fields = vat_fields(TaxFilingData(company.engine).query(2026, 1))
    field = fields["vat_and_surtax.row_20_fen"]
    assert (field["status"] == "ready") is expected_ready
    assert (field["value_fen"] == 0) is expected_ready


@pytest.mark.parametrize("tax_period,expected_ready", [("2026-03", False), ("2026-04", True)])
def test_confirmed_tax_period_without_contract_basis_limits_pending_filing_scope(
    tmp_path, tax_period, expected_ready
):
    from entity_fixture import seed_fact_entities

    from ai_accounting.kernel.domains.transactions import ServiceSale

    company = vat_company(tmp_path)
    fact = ServiceSale.model_validate_json(json.dumps({
        "period": "2026-03", "customer_id": "customer", "gross_fen": 202_000,
        "vat_policy_id": "vat", "exemption_eligible": False,
        "tax_obligation_period": tax_period, "tax_obligation_date": f"{tax_period}-02",
    }))
    seed_fact_entities(company.engine, fact)
    # Use the real registration entry, not the fixture that creates a contract basis.
    company.engine.save_fact(
        fact.kind, "source-confirmed-income", fact.model_dump(mode="json"),
        evidence=(company.filing, company.confirmation), expected_revision=0,
        request_id=company.request(),
    )
    with company.engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM fact_tax_point_basis").fetchone()[0] == 0
    result = TaxFilingData(company.engine).query(2026, 1)
    field = vat_fields(result)["vat_and_surtax.row_20_fen"]
    assert (field["status"] == "ready") is expected_ready
    assert field["value_fen"] == (0 if expected_ready else None)
    if not expected_ready:
        assert any(issue.get("subject_id") == "source-confirmed-income"
                   for issue in field["fact_issues"])


def test_current_and_cumulative_fields_use_their_own_complete_tax_periods(tmp_path):
    company = vat_company(tmp_path, months=6)
    company.sale("april-income", "2026-04", gross=202_000)
    company.publish("tax-6")
    company.sale("january-income", "2026-01", gross=101_000)
    company.publish("tax-3")
    result = TaxFilingData(company.engine).query(2026, 2)
    current, cumulative = vat_fields(result), vat_fields(result, "year_to_date")
    assert current["vat_and_surtax.net_sales_fen"]["value_fen"] == 200_000
    assert cumulative["vat_and_surtax.net_sales_fen"]["value_fen"] == 300_000
    assert current["vat_and_surtax.row_15_fen"]["value_fen"] == 6000
    assert current["vat_and_surtax.row_16_fen"]["value_fen"] == 4000
    assert current["vat_and_surtax.row_20_fen"]["value_fen"] == 2000


@pytest.mark.parametrize("quarter", [1, 2])
def test_overlapping_filing_ranges_share_reads_only_in_one_snapshot(
    tmp_path, monkeypatch, quarter
):
    from collections import Counter
    from contextlib import contextmanager

    from tax_point_fixture import contract_tax_basis

    import ai_accounting.kernel.tax_point_basis as tax_points
    from ai_accounting.kernel.domains.transactions import ServiceSale
    from ai_accounting.kernel.query_reads import QueryReads

    company = vat_company(tmp_path, months=quarter * 3)
    company.sale("january-income", "2026-01", gross=101_000)
    company.publish("tax-3")
    if quarter == 2:
        company.sale("april-income", "2026-04", gross=202_000)
        company.publish("tax-6")
    subjects = [f"pending-january-{index}" for index in range(3)]
    for subject in subjects:
        company.save(ServiceSale, subject, period="2026-01", customer_id="customer",
                     gross_fen=202_000, fulfillment_date="2026-01-10",
                     tax_obligation_period="2026-01", vat_policy_id="vat",
                     exemption_eligible=False)
        contract_tax_basis(company.engine, "service_sale", subject,
                           business_period="2026-01", contract_period="2026-01",
                           reviewed_through=f"2026-{quarter * 3:02d}")
    work = Counter()
    snapshot, resolve = QueryReads.snapshot, tax_points.resolve_tax_point

    def record(sql):
        if "SELECT c.fact_id,c.subject_id FROM fact_current c " in sql:
            work["pending_source_scans"] += 1
        if sql.startswith("SELECT f.fact_id id,c.subject_id,a.calculation_id"):
            work["tax_candidate_batches"] += 1
        if sql.startswith("SELECT 1 FROM pending WHERE subject_id="):
            work["per_candidate_pending_reads"] += 1
        if sql.startswith("SELECT DISTINCT fact_id FROM dependency_fact "):
            work["reference_fact_reads"] += 1

    @contextmanager
    def measured_snapshot(engine):
        with snapshot(engine) as reads:
            reads.connection.set_trace_callback(record)
            try:
                yield reads
            finally:
                reads.connection.set_trace_callback(None)

    def measured_resolution(*args):
        work["tax_point_resolutions"] += 1
        return resolve(*args)

    monkeypatch.setattr(QueryReads, "snapshot", measured_snapshot)
    monkeypatch.setattr(tax_points, "resolve_tax_point", measured_resolution)
    query = TaxFilingData(company.engine)
    result = query.query(2026, quarter)
    current = vat_fields(result)["vat_and_surtax.row_20_fen"]
    cumulative = vat_fields(result, "year_to_date")["vat_and_surtax.row_20_fen"]
    assert cumulative["status"] == "needs_information" and cumulative["value_fen"] is None
    assert {item["subject_id"] for item in cumulative["fact_issues"]
            if item["field"] == "vat_source"} == set(subjects)
    if quarter == 1:
        assert current["value_fen"] is None and current["status"] == "needs_information"
        assert current["source_basis"] == cumulative["source_basis"]
    else:
        assert current["value_fen"] == 2000 and current["status"] == "ready"
        assert {item["value"] for item in current["source_basis"]
                if item["type"] == "calculation_id"} < {
                    item["value"] for item in cumulative["source_basis"]
                    if item["type"] == "calculation_id"
                }
    assert work == {
        "pending_source_scans": 1, "tax_candidate_batches": 1,
        "tax_point_resolutions": len(subjects), "reference_fact_reads": quarter,
    }
    first_work = dict(work)
    assert query.query(2026, quarter) == result
    assert work == {name: count * 2 for name, count in first_work.items()}

    # Later formal adoption must be read afresh by the same query object.
    company.publish(*subjects)
    company.publish("tax-3")
    adopted = query.query(2026, quarter)
    cumulative = vat_fields(adopted, "year_to_date")["vat_and_surtax.row_20_fen"]
    assert cumulative["status"] == "ready"
    assert cumulative["value_fen"] == (7000 if quarter == 1 else 9000)
    assert cumulative["source_basis_digest"] != vat_fields(
        result, "year_to_date"
    )["vat_and_surtax.row_20_fen"]["source_basis_digest"]


def test_shared_pending_tax_scope_preserves_unknown_until_source_is_established(tmp_path):
    from tax_point_fixture import contract_tax_basis

    from ai_accounting.kernel.domains.transactions import ServiceSale

    company = vat_company(tmp_path, months=6)
    company.save(ServiceSale, "unresolved-july", period="2026-07", customer_id="customer",
                 gross_fen=202_000, fulfillment_date="2026-07-10", vat_policy_id="vat",
                 exemption_eligible=False)
    query = TaxFilingData(company.engine)
    unresolved = query.query(2026, 2)
    for dimension in ("current", "year_to_date"):
        field = vat_fields(unresolved, dimension)["vat_and_surtax.row_20_fen"]
        assert field["status"] == "needs_information" and field["value_fen"] is None
        assert any(item.get("subject_id") == "unresolved-july"
                   for item in field["fact_issues"])
    contract_tax_basis(company.engine, "service_sale", "unresolved-july",
                       business_period="2026-07", contract_period="2026-07")
    established = query.query(2026, 2)
    for dimension in ("current", "year_to_date"):
        field = vat_fields(established, dimension)["vat_and_surtax.row_20_fen"]
        assert field["status"] == "ready" and field["value_fen"] == 0


def test_historical_adopted_tax_results_are_not_recalculated_to_fill_missing_fields(
    tmp_path, monkeypatch
):
    company = vat_company(tmp_path, historical=True)

    def forbidden(*args):
        raise AssertionError("read-only filing data must never call a tax calculator")

    monkeypatch.setitem(company.engine.store.registry.evaluators, "tax_assessment", forbidden)
    result = TaxFilingData(company.engine).query(2026, 1)
    fields = vat_fields(result)
    assert all(field["value_fen"] is None for field in fields.values())
    assert any(issue["field"] == "filing_fields" for field in fields.values()
               for issue in field["fact_issues"])
    assert not any("vat" in mapping["version"] for mapping in result["mapping_versions"])


def test_equal_rounding_and_exemption_income_use_exact_line_meaning():
    from types import SimpleNamespace

    from ai_accounting.kernel.reports import _profit_rows

    values = {"vat_exemption_profit_lines": [{"line_no": 2, "amount_fen": 100}]}
    base = {"period": 1, "account": "6301", "amount": -100, "kind": "tax_assessment",
            "fact": SimpleNamespace(), "values": values, "classification": None,
            "reverses_id": None, "version_id": "voucher"}
    problems = []
    result = _profit_rows([base | {"line_no": 2}, base | {"line_no": 4}], 1, 1, problems)
    assert result[22] == 200
    assert result[23] == 100
    assert result[30] == 200
    assert not problems
    reversal = base | {"line_no": 2, "amount": 100, "reverses_id": "original"}
    assert _profit_rows([reversal], 1, 1, [])[23] == -100


@pytest.mark.parametrize("lines", [None, [], [{"line_no": 2, "amount_fen": 100}]])
def test_compact_tax_line_preserves_historical_absence_and_current_empty_components(lines):
    from types import SimpleNamespace

    from ai_accounting.kernel.report_semantics import _decode, compact_line_fields

    values = {} if lines is None else {"vat_exemption_profit_lines": lines}
    compact = compact_line_fields({
        "source_calculation_id": "calculation", "source_fact_id": "fact",
        "source_result_digest": "a" * 64, "kind": "tax_assessment", "values": values,
        "fact": SimpleNamespace(), "cash_source": SimpleNamespace(), "relation_issues": [],
    })
    from ai_accounting.kernel.types import canonical

    restored = _decode(canonical(compact))
    assert restored["values"] == values


def test_current_financial_field_cannot_be_taken_as_cumulative_cit_filing(book):
    scenario(book, tax=False)
    result = TaxFilingData(book[0]).query(2026, 1)
    current = [field for field in result["fields"] if field["section"] == "financial_statement"]
    assert len(current) == 3
    assert all(field["period_basis"] == "current" for field in current)
    assert not any(source["type"] == "tax_form_field" for field in current
                   for source in field["source_basis"])
    assert all(field["period_basis"] == "year_to_date" for field in cit_fields(result))


def test_rounding_income_retains_meaning_after_real_close_and_projection(book):
    from entity_fixture import seed_entities
    from tax_point_fixture import sourced_tax_data
    from test_business_domains import surtax_policy, vat_policy
    from test_reports import close_quarter

    report = scenario(book)
    engine, save, publish, close = book
    seed_entities(engine, (("customer", "organization", None),))
    save("vat_policy", "vat", {"period": "2026-01",
                               "policy": vat_policy().model_dump(mode="json")})
    save("surtax_policy", "surtax", {"period": "2026-01",
                                     "policy": surtax_policy().model_dump(mode="json")})
    for index in (1, 2):
        subject = f"tiny-income-{index}"
        save("service_sale", subject, sourced_tax_data(engine, "service_sale", subject, {
            "period": "2026-01", "customer_id": "customer", "gross_fen": 51,
            "fulfillment_date": "2026-01-10", "tax_obligation_period": "2026-01",
            "vat_policy_id": "vat", "exemption_eligible": False,
        }))
        publish(subject)
    save("tax_assessment", "quarter-tax", {
        "period": "2026-03", "period_start": "2026-01-01", "period_end": "2026-03-31",
        "vat_policy_id": "vat", "surtax_policy_id": "surtax",
    })
    publish("quarter-tax")
    before = report.report(2026, 1, source="open")
    assert before["status"] == "ready"
    assert before["statements"]["profit_statement"]["22"]["year_to_date_fen"] == 1
    assert before["statements"]["profit_statement"]["23"]["year_to_date_fen"] == 0
    close_quarter(book)
    after = report.report(2026, 1, source="closed")
    assert after["statements"] == before["statements"]
    closed = TaxFilingData(engine).query(2026, 1, source="closed")
    assert vat_fields(closed)["vat_and_surtax.payable_vat_fen"]["value_fen"] == 1
    assert all(field["status"] == "ready" for field in cit_fields(closed))


def test_monthly_threshold_results_cannot_be_merged_into_a_quarterly_return(tmp_path):
    company = vat_company(tmp_path, monthly=True)
    for month in (1, 2, 3):
        company.sale(f"income-{month}", f"2026-{month:02d}", gross=505_000, eligible=True)
        assert company.current("tax_assessment", f"tax-{month}").values["payable_vat_fen"] == 0
    result = TaxFilingData(company.engine).query(2026, 1)
    assert all(field["value_fen"] is None for field in vat_fields(result).values())
    assert any(issue["field"] == "tax_period_scope" for field in vat_fields(result).values()
               for issue in field["fact_issues"])


def test_public_command_schema_protocol_and_native_response_bind_one_company(service):
    from ai_accounting.kernel.contracts import KernelError

    app, company_id = service
    payload = {"company_id": company_id, "year": 2026, "quarter": 1, "source": "open"}
    result = app.dispatch("tax_filing_data", payload)
    assert result["company_id"] == company_id
    assert result["status"] == "needs_information"
    RESPONSE_ADAPTERS["tax_filing_data"].validate_python(result)
    discovered = app.dispatch("schema", {"view": "selected", "commands": ["tax_filing_data"]})
    schema = discovered["command_schemas"]["tax_filing_data"]
    assert schema["properties"]["year"]["type"] == "integer"
    assert schema["properties"]["source"]["enum"] == ["open", "closed"]
    assert "tax_reporting" in str(discovered["agent_operating_rules"])
    with pytest.raises(KernelError) as error:
        app.dispatch("tax_filing_data", payload | {"year": "2026"})
    assert error.value.code == "invalid_command"


def test_field_definitions_distinguish_accounting_tax_and_settlement_meanings(tmp_path):
    company = vat_company(tmp_path)
    fields = vat_fields(TaxFilingData(company.engine).query(2026, 1))
    sales = fields["vat_and_surtax.row_1_fen"]
    assert sales["definition"]["amount_basis"] == "tax_exclusive"
    assert sales["definition"]["form_field"] == "small_scale_vat.1"
    assert sales["definition"]["business_scope"]
    assert "Decimal" in sales["definition"]["rounding_rule"]
    urban = fields["vat_and_surtax.urban_tax_fen"]
    assert urban["definition"]["amount_basis"] == "tax_amount"
    assert urban["definition"]["form_field"] is None
    assert urban["status"] == "ready"
    settlement = fields["vat_and_surtax.row_23_fen"]
    assert settlement["status"] == "needs_information" and settlement["value_fen"] is None
    assert "应补退" in settlement["definition"]["meaning"]
    assert any(issue["amount_field"] == "row_23_fen" for issue in settlement["fact_issues"])
