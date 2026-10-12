"""Evidenced tax-client input files do not require a published taxable wage."""

import pytest
import xlrd
from test_payroll_corrections import Company

from ai_accounting.kernel import tax_import
from ai_accounting.kernel.contracts import KernelError, NeedsInformation, Read
from ai_accounting.kernel.domains.payroll import (
    ContributionRuleFact,
    Payroll,
    PayrollContributionPolicy,
    PayrollProfile,
)
from ai_accounting.kernel.payroll_confirmation import FactRevisionReference, revision_reference
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.remuneration_tax_basis import RemunerationTaxBasis, tax_basis_scope
from ai_accounting.kernel.types import YearMonth


def contribution_rule(code, *, employee_rate="0.01", employer_rate="0.01"):
    return ContributionRuleFact(
        code=code,
        base_kind="social_insurance",
        employee_rate=employee_rate,
        employer_rate=employer_rate,
        minimum_base_fen=0,
        maximum_base_fen=10_000_000,
        rounding="half_up",
        enabled=True,
    )


def company(tmp_path, *, publish=True, confirm=True, rules=None):
    result = Company(tmp_path / "tax-input.sqlite")
    result.save(
        PayrollProfile(
            period="2026-01",
            employee_id="employee",
            effective_from="2026-01",
            effective_to=None,
            social_insurance_base_fen=1_000_000,
            housing_fund_base_fen=None,
            social_insurance_participating=True,
            housing_fund_participating=False,
            contribution_shortfall="reject",
        ),
        "profile",
    )
    result.save(
        PayrollContributionPolicy(
            period="2026-01",
            version="test-2026",
            jurisdiction="test",
            effective_from="2026-01-01",
            effective_to="2026-12-31",
            primary_source_url="https://www.mof.gov.cn/",
            rules=rules
            or (contribution_rule("pension", employee_rate="0.08", employer_rate="0.16"),),
        ),
        "contributions",
    )
    saved_wage = result.save(
        Payroll(
            period="2026-01",
            employee_id="employee",
            profile_revision=FactRevisionReference(subject_id="profile", revision=1),
            contribution_policy_revision=FactRevisionReference(
                subject_id="contributions", revision=1
            ),
            accounting_gross_salary_fen=1_000_000,
            expense_class="management",
            contribution_basis="policy_until_actual",
        ),
        "january",
    )
    if confirm:
        result.confirm_payroll("january")
    if publish:
        result.save(
            RemunerationTaxBasis(
                period="2026-01",
                source_kind="payroll",
                source_id="january",
                source_fact_id=saved_wage["fact_id"],
                tax_period="2026-01",
                tax_amount_fen=12_600,
                basis_type="final_statement",
                confirmed=True,
            ),
            "tax-basis",
        )
        result.publish("january")
    return result


def complete_details(instance, *, mapping=True, identity=True, include=True, **changes):
    with instance.engine.store.connection(read_only=True) as connection:
        wage = instance.engine.store.current_fact(connection, "january")
    instance.save(
        tax_import.TaxImportDetails(
            **{
                "period": "2026-01",
                "employee_id": "employee",
                "payroll_revision": revision_reference(wage),
                "include_in_filing": include,
                "tax_reported_salary_fen": 1_000_000,
                "tax_exempt_income_fen": 0,
                "cumulative_special_fen": {key: 0 for key in tax_import.SPECIAL_COLUMNS},
                "current_other_fen": {key: 0 for key in tax_import.OTHER_COLUMNS},
                "cumulative_personal_pension_fen": 0,
                "tax_relief_fen": 0,
                "treaty_relief_fen": 0,
                **changes,
            }
        ),
        "details",
    )
    if identity:
        instance.save(
            tax_import.TaxImportIdentity(
                period="2026-01",
                employee_id="employee",
                employee_code="00007",
                name="测试员工",
                document_type="居民身份证",
                document_number="001234567890123456",
            ),
            "identity",
        )
    if mapping:
        instance.save(
            tax_import.TaxImportMapping(
                period="2026-01",
                pension_code="pension",
                medical_code=None,
                unemployment_code=None,
            ),
            "mapping",
        )
    return tax_import.TaxImport(instance.engine)


def mapping_assessment(instance):
    with instance.engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        return tax_import.assess_tax_import_mapping(
            instance.engine.store,
            connection,
            YearMonth("2026-01"),
        )


def replace_contribution_policy(instance, rules):
    """Shared response fixture: replace the exact social policy, without tax rules."""
    with instance.engine.store.connection(read_only=True) as connection:
        policy = instance.engine.store.current_fact(connection, "contributions").fact
        wage = instance.engine.store.current_fact(connection, "january")
    instance.save(
        policy.model_copy(update={"rules": rules, "version": "four-components"}),
        "four-contributions",
    )
    revised = instance.save(
        wage.fact.model_copy(
            update={
                "contribution_policy_revision": FactRevisionReference(
                    subject_id="four-contributions", revision=1
                ),
            }
        ),
        "january",
        revision=wage.revision,
    )
    instance.confirm_payroll("january")
    with instance.engine.store.connection(read_only=True) as connection:
        sources = instance.engine.store.select(
            connection,
            Read(
                "fact",
                RemunerationTaxBasis.kind,
                tax_basis_scope(wage.fact.kind, wage.subject_id),
            ),
        )
    for source in sources:
        instance.save(
            source.fact.model_copy(update={"source_fact_id": revised["fact_id"]}),
            source.subject_id,
            revision=source.revision,
        )
    instance.publish("january")


def test_tax_file_is_ready_before_tax_basis_and_salary_publication(tmp_path):
    instance = company(tmp_path, publish=False)
    service = complete_details(instance)
    with pytest.raises(NeedsInformation) as exc:
        instance.engine.preview(["january"])
    assert exc.value.issues[0]["field"] == "remuneration_tax_basis"
    plan = service.preview("2026-01")
    assert plan["status"] == "ready", plan["fact_issues"]
    assert plan["rows_fen"][0][6] == 80_000 and len(plan["rows_fen"][0]) == 30
    assert plan["tax_import_mapping"]["calculation_ids"] == []
    assert instance.count("voucher") == 0
    queued = service.confirm(
        "2026-01",
        preview_digest=plan["digest"],
        output_directory=str(tmp_path / "out"),
        request_id=instance.request(),
    )
    job = tax_import.run_tax_import_jobs(instance.engine)[0]
    assert job["job_id"] == queued["job_id"] and job["status"] == "succeeded"
    book = xlrd.open_workbook(job["result"]["path"])
    assert book.sheet_names() == ["正常工资薪金收入", "填表说明"]
    sheet = book.sheet_by_index(0)
    assert sheet.row_values(0) == list(tax_import._HEADERS)
    assert sheet.cell_value(1, 0) == "00007" and sheet.cell_value(1, 3) == "001234567890123456"
    assert sheet.cell_value(1, 6) == 800 and instance.count("voucher") == 0


@pytest.mark.parametrize("missing", ["identity", "mapping", "details"])
def test_export_only_gaps_never_change_accounting(tmp_path, missing):
    instance = company(tmp_path)
    original = instance.current("january")
    service = (
        tax_import.TaxImport(instance.engine)
        if missing == "details"
        else complete_details(
            instance, identity=missing != "identity", mapping=missing != "mapping"
        )
    )
    plan = service.preview("2026-01")
    assert plan["status"] == "needs_information"
    assert all(item["semantics"] == "export_only" for item in plan["fact_issues"])
    assert instance.current("january") == original


def test_explicit_filing_period_can_reference_previous_accounting_month_before_tax(tmp_path):
    instance = company(tmp_path, publish=False)
    service = complete_details(instance, period="2026-02", mapping=False)
    instance.save(
        tax_import.TaxImportMapping(
            period="2026-02",
            pension_code="pension",
            medical_code=None,
            unemployment_code=None,
        ),
        "february-mapping",
    )
    plan = service.preview("2026-02")
    assert plan["status"] == "ready", plan["fact_issues"]
    assert plan["period"] == "2026-02" and plan["rows_fen"][0][6] == 80_000
    with instance.engine.store.connection(read_only=True) as connection:
        wage = instance.engine.store.current_fact(connection, "january")
    assert wage.id in plan["source_versions"]
    assert instance.count("voucher") == 0
    assert not service.preview("2026-01")["rows_fen"]


def test_unconfirmed_wage_cannot_export_complete_raw_input(tmp_path):
    instance = company(tmp_path, publish=False, confirm=False)
    plan = complete_details(instance).preview("2026-01")
    assert plan["status"] == "needs_information" and not plan["rows_fen"]
    assert plan["tax_import_mapping"]["status"] == "pending_confirmation"


def test_cumulative_deductions_are_original_inputs_without_tax_state(tmp_path):
    instance = company(tmp_path, publish=False)
    special = {key: 1_000_000 for key in tax_import.SPECIAL_COLUMNS}
    plan = complete_details(
        instance,
        cumulative_special_fen=special,
        cumulative_personal_pension_fen=250_000,
        tax_relief_fen=17_000,
    ).preview("2026-01")
    assert plan["status"] == "ready"
    assert plan["rows_fen"][0][10:16] == [1_000_000] * 6
    assert plan["rows_fen"][0][16] == 250_000 and plan["rows_fen"][0][27] == 17_000


def test_no_change_input_file_freezes_prior_adoption_without_copying_tax(tmp_path):
    from ai_accounting.kernel.payroll_preparation import PayrollNoChange, PayrollPreparation

    instance = company(tmp_path)
    complete_details(instance)
    service = PayrollPreparation(instance.engine)
    reuse = service.reuse_basis("2026-02")
    instance.save(
        PayrollNoChange(
            period="2026-02",
            employees=tuple(reuse["employees"]),
            employee_roster_unchanged=True,
            salary_and_deductions_unchanged=True,
        ),
        "no-change",
    )
    prepared = service.prepare("2026-02")
    saved = service.confirm(
        "2026-02", preview_digest=prepared["digest"], request_id=instance.request()
    )
    subject = saved["results"][0]["subject_id"]
    with instance.engine.store.connection(read_only=True) as connection:
        wage = instance.engine.store.current_fact(connection, subject)
        detail = instance.engine.store.current_fact(connection, "details").fact
    instance.save(
        detail.model_copy(
            update={"period": YearMonth("2026-02"), "payroll_revision": revision_reference(wage)}
        ),
        "february-details",
    )
    instance.save(
        tax_import.TaxImportMapping(
            period="2026-02",
            pension_code="pension",
            medical_code=None,
            unemployment_code=None,
        ),
        "february-mapping",
    )
    with pytest.raises(NeedsInformation):
        instance.engine.preview([subject])
    plan = tax_import.TaxImport(instance.engine).preview("2026-02")
    assert plan["status"] == "ready", plan["fact_issues"]
    assert instance.current("january").id in plan["source_versions"]


def test_explicit_exclusion_never_creates_empty_return(tmp_path):
    instance = company(tmp_path, publish=False)
    plan = complete_details(instance, include=False, mapping=False, identity=False).preview(
        "2026-01"
    )
    assert plan["tax_import_mapping"]["status"] == "not_applicable"
    assert plan["row_count"] == 0 and plan["status"] == "needs_information"
    assert plan["excluded_sources"][0]["reason"] == "explicitly_excluded_from_filing"


def test_four_nonzero_components_are_file_capability_error(tmp_path):
    rules = tuple(
        contribution_rule(code, employee_rate="0.01", employer_rate="0.01")
        for code in ("pension", "medical", "unemployment", "injury")
    )
    instance = company(tmp_path, rules=rules)
    service = complete_details(instance)
    plan = service.preview("2026-01")
    assert plan["status"] == "unsupported"
    issue = plan["tax_import_mapping"]["issues"][0]
    assert issue["code"] == "tax_import_format_unsupported" and issue["amount_fen"] == 40_000
    assert instance.current("january").values["employee_contributions_fen"] == 40_000
    with pytest.raises(KernelError) as exc:
        service.confirm(
            "2026-01",
            preview_digest=plan["digest"],
            output_directory=str(tmp_path / "out"),
            request_id=instance.request(),
        )
    assert exc.value.code == "tax_import_format_unsupported"


def test_zero_components_do_not_consume_columns(tmp_path):
    instance = company(
        tmp_path,
        rules=(
            contribution_rule("pension"),
            *(contribution_rule(code, employee_rate="0") for code in ("a", "b", "c")),
        ),
    )
    complete_details(instance)
    assert mapping_assessment(instance)["status"] == "ready"


def test_mapping_reuses_own_snapshot_and_rejects_other_snapshot(tmp_path):
    instance = company(tmp_path, publish=False)
    complete_details(instance)
    with QueryReads.snapshot(instance.engine) as reads:
        assert (
            tax_import.assess_tax_import_mapping(
                instance.engine.store,
                reads.connection,
                YearMonth("2026-01"),
                reads=reads,
            )["status"]
            == "ready"
        )
        with QueryReads.snapshot(instance.engine) as other:
            with pytest.raises(ValueError, match="another active snapshot"):
                tax_import.assess_tax_import_mapping(
                    instance.engine.store,
                    other.connection,
                    YearMonth("2026-01"),
                    reads=reads,
                )


def test_changed_wage_invalidates_details_and_preview(tmp_path):
    instance = company(tmp_path, publish=False)
    service = complete_details(instance)
    plan = service.preview("2026-01")
    with instance.engine.store.connection(read_only=True) as connection:
        wage = instance.engine.store.current_fact(connection, "january").fact
    instance.save(
        wage.model_copy(update={"accounting_gross_salary_fen": 1_100_000}), "january", revision=1
    )
    instance.confirm_payroll("january")
    assert service.preview("2026-01")["status"] == "needs_information"
    with pytest.raises(KernelError) as exc:
        service.confirm(
            "2026-01",
            preview_digest=plan["digest"],
            output_directory=str(tmp_path / "out"),
            request_id=instance.request(),
        )
    assert exc.value.code == "preview_expired"


def test_frozen_export_retries_file_and_request_idempotently(tmp_path):
    instance = company(tmp_path, publish=False)
    service = complete_details(instance)
    plan, key = service.preview("2026-01"), instance.request()
    queued = service.confirm(
        "2026-01",
        preview_digest=plan["digest"],
        output_directory=str(tmp_path / "out"),
        request_id=key,
    )

    def crash(stage, job):
        if stage == "files_published":
            raise RuntimeError("simulated crash after durable files")

    assert tax_import.run_tax_import_jobs(instance.engine, fault=crash)[0]["status"] == "failed"
    assert tax_import.run_tax_import_jobs(instance.engine)[0]["status"] == "succeeded"
    assert (
        service.confirm(
            "2026-01",
            preview_digest=plan["digest"],
            output_directory=str(tmp_path / "out"),
            request_id=key,
        )
        == queued
    )


def test_retry_rejects_corrupt_file_with_rewritten_manifest_checksum(tmp_path):
    import hashlib
    import json
    from decimal import Decimal

    instance = company(tmp_path, publish=False)
    service, target = complete_details(instance), tmp_path / "out"
    plan = service.preview("2026-01")
    service.confirm(
        "2026-01",
        preview_digest=plan["digest"],
        output_directory=str(target),
        request_id=instance.request(),
    )

    def crash(stage, job):
        if stage == "files_published":
            raise RuntimeError("simulated crash")

    tax_import.run_tax_import_jobs(instance.engine, fault=crash)
    changed = [
        [
            value if col in {0, 1, 2, 3, 29} else Decimal(value) / 100
            for col, value in enumerate(row)
        ]
        for row in plan["rows_fen"]
    ]
    changed[0][4] += 1
    raw = tax_import.build_payroll_tax_import_xls(changed)
    (target / "正常工资薪金收入.xls").write_bytes(raw)
    path = target / "个税导入核对.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["sha256"] = hashlib.sha256(raw).hexdigest()
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert tax_import.run_tax_import_jobs(instance.engine)[0]["status"] == "failed"
