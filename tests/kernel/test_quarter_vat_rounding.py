"""Quarter VAT, its journal and owner totals share separately rounded form amounts."""

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal

import pytest
from test_banking import inventories
from test_business_domains import surtax_policy, vat_policy
from test_tax_credits import Company

from ai_accounting.kernel import close_review
from ai_accounting.kernel.business_queries import business_display_amount
from ai_accounting.kernel.close_storage import decode_close
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.domains import taxes
from ai_accounting.kernel.integrity import verify_integrity
from ai_accounting.kernel.response_contracts import validate_response
from ai_accounting.kernel.tax_filing_data import TaxFilingData
from ai_accounting.kernel.types import YearMonth

PERIOD = "2026-09"
ASSESSMENT = "quarter-vat"
NET_SALES = 33_433_651
ACCRUED_VAT = 334_336
PAYABLE_VAT = 334_337
SURTAX = 20_060
TOTAL_TAX = 354_397
TAX_AMOUNT_LABEL = "增值税及附加税费合计"


@pytest.fixture
def quarter_company(tmp_path):
    company = Company(tmp_path / "quarter-vat.sqlite", opening_period="2026-07")
    policy = vat_policy().model_copy(update={
        "threshold_fen": 30_000_000,
        "threshold_operator": "strictly_below",
    })
    company.save(taxes.VatPolicyFact, "vat", period="2026-07",
                 policy=policy.model_dump(mode="json"))
    company.save(taxes.SurtaxPolicyFact, "surtax", period="2026-07",
                 policy=surtax_policy().model_dump(mode="json"))
    # Two explicit transactions establish a quarter whose business accruals
    # differ by one fen from the separately rounded 3% and 2% form amounts.
    company.sale("july-sale", "2026-07", gross=33_767_937, eligible=True)
    company.sale("august-sale", "2026-08", gross=50, eligible=True)
    company.save(taxes.TaxAssessment, ASSESSMENT, period=PERIOD,
                 period_start="2026-07-01", period_end="2026-09-30",
                 vat_policy_id="vat", surtax_policy_id="surtax")
    return company


def confirm(company, request_id):
    preview = company.engine.preview([ASSESSMENT])
    result = company.engine.confirm(
        [ASSESSMENT], preview_digest=preview["digest"], epochs=preview["epochs"],
        request_id=request_id,
    )
    return preview, result


def assessment_result(publication):
    return next(item for item in publication["results"] if item["subject_id"] == ASSESSMENT)


def vat_account_balance(company):
    with company.engine.store.connection(read_only=True) as connection:
        return connection.execute(
            "SELECT coalesce(sum(credit-debit),0) FROM monthly_account "
            "WHERE account='222101' AND period<=?", (YearMonth(PERIOD).ordinal,),
        ).fetchone()[0]


def brief(company):
    result = Dashboard(company.engine).brief(PERIOD)
    validate_response("dashboard_brief", result)
    return result


def tax_category(response):
    return next(item for item in response["data"]["open_items"]["categories"]
                if item["key"] == "tax_payables")


def assert_tax_activity_amount(company, response, amount):
    groups = [item for item in response["data"]["collections"]["activity"]["items"]
              if item["kind"] == "tax_assessment"]
    assert len(groups) == 1
    group = groups[0]
    assert (group["amount_fen"], group["amount_label"]) == (amount, TAX_AMOUNT_LABEL)
    dashboard = Dashboard(company.engine)
    details = dashboard.brief_group(
        PERIOD, section="activity", group_key=group["group_key"],
        expected_version=response["snapshot_version"],
    )
    validate_response("dashboard_brief_group", details)
    members = details["data"]["collections"]["members"]["items"]
    assert len(members) == 1 and members[0]["subject_id"] == ASSESSMENT
    assert (members[0]["amount_fen"], members[0]["amount_label"]) == (amount, TAX_AMOUNT_LABEL)
    vouchers = details["data"]["collections"]["vouchers"]["items"]
    assert len(vouchers) == 1
    assert (vouchers[0]["business_amount_fen"], vouchers[0]["business_amount_label"]) == (
        amount, TAX_AMOUNT_LABEL,
    )
    progress = dashboard.business_status(PERIOD, ASSESSMENT,
                                         expected_version=response["snapshot_version"])
    validate_response("dashboard_business_status", progress)
    current = progress["data"]["current_business_result"]
    assert (current["amount_fen"], current["amount_label"]) == (amount, TAX_AMOUNT_LABEL)


def assert_shared_amounts(company):
    result = company.current("tax_assessment", ASSESSMENT)
    values = result.values
    assert values["net_sales_fen"] == NET_SALES
    assert values["accrued_vat_fen"] == ACCRUED_VAT
    assert values["rounding_adjustment_fen"] == 1
    assert values["payable_vat_fen"] == PAYABLE_VAT
    assert (values["urban_tax_fen"], values["education_tax_fen"],
            values["local_education_tax_fen"]) == (11_702, 5_015, 3_343)
    assert values["surtax_fen"] == SURTAX
    assert values["payable_vat_fen"] + values["surtax_fen"] == TOTAL_TAX
    fields = values["filing_fields"]
    assert fields["row_1_fen"] == NET_SALES
    assert fields["row_15_fen"] == 1_003_010
    assert fields["row_16_fen"] == 668_673
    assert fields["row_20_fen"] == fields["row_15_fen"] - fields["row_16_fen"] == PAYABLE_VAT
    filing = TaxFilingData(company.engine).query(2026, 3)
    validate_response("tax_filing_data", filing)
    current = {field["field_id"]: field for field in filing["fields"]
               if field["section"] == "vat_and_surtax" and field["period_basis"] == "current"}
    for field_id, amount in (("row_15_fen", 1_003_010), ("row_16_fen", 668_673),
                             ("row_20_fen", PAYABLE_VAT), ("payable_vat_fen", PAYABLE_VAT),
                             ("surtax_fen", SURTAX)):
        field = current["vat_and_surtax." + field_id]
        assert field["status"] == "ready" and field["value_fen"] == amount
    assert vat_account_balance(company) == PAYABLE_VAT
    assert {item["name"]: item["amount_fen"] for item in values["obligations"]} == {
        "vat": PAYABLE_VAT, "surtax": SURTAX,
    }
    assert company.balance(f"tax_assessment:{ASSESSMENT}:vat") == PAYABLE_VAT
    assert company.balance(f"tax_assessment:{ASSESSMENT}:surtax") == SURTAX
    response = brief(company)
    assert tax_category(response)["outstanding_fen"] == TOTAL_TAX
    displayed = {item["description"]: item["outstanding_fen"]
                 for item in response["data"]["collections"]["open_items"]["items"]
                 if item["category_key"] == "tax_payables"}
    assert displayed == {"增值税": PAYABLE_VAT, "附加税": SURTAX}
    assert_tax_activity_amount(company, response, TOTAL_TAX)
    return response


def adopt_previous_rounding(company, monkeypatch):
    calculator = taxes.calculate_vat_period

    def previous_calculator(**inputs):
        result = calculator(**inputs)
        payable = result.accrued_vat_fen - result.relief_fen
        policy = inputs["surtax_policy"]
        amounts = [taxes.round_fen(Decimal(payable) * rate / 100 * policy.payable_fraction)
                   for rate in (policy.urban_rate_percent, policy.education_rate_percent,
                                policy.local_education_rate_percent)]
        return result.model_copy(update={
            "payable_vat_fen": payable,
            "rounding_adjustment_fen": 0,
            "urban_tax_fen": amounts[0], "education_tax_fen": amounts[1],
            "local_education_tax_fen": amounts[2], "surtax_fen": sum(amounts),
            "filing_fields": {**result.filing_fields, "row_20_fen": payable},
        })

    with monkeypatch.context() as patch:
        patch.setattr(taxes, "calculate_vat_period", previous_calculator)
        _, publication = confirm(company, "previous-quarter-assessment")
    assert company.current("tax_assessment", ASSESSMENT).values["payable_vat_fen"] == ACCRUED_VAT
    assert vat_account_balance(company) == ACCRUED_VAT
    response = brief(company)
    assert tax_category(response)["outstanding_fen"] == TOTAL_TAX - 1
    return publication, response


def database_snapshot(company):
    with company.engine.store.connection(read_only=True) as connection:
        return list(connection.iterdump())


def test_quarter_rounding_reaches_journal_filing_obligations_and_dashboard(quarter_company):
    confirm(quarter_company, "initial-quarter-assessment")
    assert_shared_amounts(quarter_company)


@pytest.mark.parametrize("values,expected", [
    ({"payable_vat_fen": PAYABLE_VAT, "surtax_fen": SURTAX}, TOTAL_TAX),
    ({"payable_vat_fen": 0, "surtax_fen": 0}, 0),
    ({"payable_vat_fen": PAYABLE_VAT}, None),
    ({"surtax_fen": SURTAX}, None),
    ({"payable_vat_fen": None, "surtax_fen": SURTAX}, None),
    ({"payable_vat_fen": PAYABLE_VAT, "surtax_fen": True}, None),
    ({"payable_vat_fen": str(PAYABLE_VAT), "surtax_fen": SURTAX}, None),
])
def test_tax_confirmation_amount_requires_both_saved_integer_components(values, expected):
    assert business_display_amount({
        "kind": "tax_assessment", "fact": {"data": {"amount_fen": 999}},
        "outcome": {"values": values},
    }) == (expected, TAX_AMOUNT_LABEL)


def test_saved_tax_amount_without_filing_fields_remains_displayable(quarter_company, monkeypatch):
    company = quarter_company
    evaluator = company.engine.store.registry.evaluators["tax_assessment"]

    def previous_fields(version, context):
        result = evaluator(version, context)
        return replace(result, values={key: value for key, value in result.values.items()
                                       if not key.startswith("filing_")})

    with monkeypatch.context() as patch:
        patch.setitem(company.engine.store.registry.evaluators, "tax_assessment", previous_fields)
        confirm(company, "assessment-without-filing-fields")
    before = database_snapshot(company)
    assert_tax_activity_amount(company, brief(company), TOTAL_TAX)
    filing = TaxFilingData(company.engine).query(2026, 3)
    current = [field for field in filing["fields"] if field["section"] == "vat_and_surtax"
               and field["period_basis"] == "current"]
    assert current and all(field["value_fen"] is None for field in current)
    assert any(issue["field"] == "filing_fields"
               for field in current for issue in field["fact_issues"])
    assert database_snapshot(company) == before


def test_open_quarter_recalculation_preserves_voucher_and_history(quarter_company, monkeypatch):
    company = quarter_company
    previous, previous_brief = adopt_previous_rounding(company, monkeypatch)
    old = assessment_result(previous)
    preview, publication = confirm(company, "correct-quarter-rounding")
    corrected = assessment_result(publication)
    assert assessment_result(preview)["impact"] == "accounting_changed"
    assert corrected["voucher_number"] == old["voucher_number"]
    assert corrected["calculation_id"] != old["calculation_id"]
    assert company.engine.trace(old["calculation_id"])["calculation"]["outcome"]["values"][
        "payable_vat_fen"
    ] == ACCRUED_VAT
    with company.engine.store.connection(read_only=True) as connection:
        history = connection.execute(
            "SELECT v.voucher_id,v.id FROM voucher_version v JOIN calculation c "
            "ON c.id=v.calculation_id WHERE c.subject_id=?", (ASSESSMENT,),
        ).fetchall()
        assert len(history) == 2 and len({row[0] for row in history}) == 1
        audits = connection.execute(
            "SELECT request_id,action FROM audit WHERE request_id IN (?,?) ORDER BY id",
            ("previous-quarter-assessment", "correct-quarter-rounding"),
        ).fetchall()
        assert [tuple(row) for row in audits] == [
            ("previous-quarter-assessment", "publish"), ("correct-quarter-rounding", "publish"),
        ]
    response = assert_shared_amounts(company)
    assert response["snapshot_version"] != previous_brief["snapshot_version"]
    with pytest.raises(KernelError) as failure:
        Dashboard(company.engine).brief(PERIOD, section="open_items",
                                       expected_version=previous_brief["snapshot_version"])
    assert failure.value.code == "dashboard_snapshot_changed"
    before = database_snapshot(company)
    assert company.engine.confirm(
        [ASSESSMENT], preview_digest=preview["digest"], epochs=preview["epochs"],
        request_id="correct-quarter-rounding",
    ) == publication
    assert database_snapshot(company) == before


@pytest.mark.parametrize("stage", ["begin", "lines", "calculation", "published", "commit"])
def test_failed_quarter_recalculation_is_atomic(quarter_company, monkeypatch, stage):
    company = quarter_company
    _, old_brief = adopt_previous_rounding(company, monkeypatch)
    preview = company.engine.preview([ASSESSMENT])
    before = database_snapshot(company)

    def fail(current, connection):
        if current == stage:
            raise RuntimeError("injected quarter correction failure")

    company.engine.fault = fail
    with pytest.raises(RuntimeError, match="injected quarter correction failure"):
        company.engine.confirm(
            [ASSESSMENT], preview_digest=preview["digest"], epochs=preview["epochs"],
            request_id="failed-quarter-correction",
        )
    assert database_snapshot(company) == before
    assert brief(company)["snapshot_version"] == old_brief["snapshot_version"]
    assert tax_category(brief(company))["outstanding_fen"] == TOTAL_TAX - 1
    company.engine.fault = lambda current, connection: None
    confirm(company, "retry-quarter-correction")
    assert_shared_amounts(company)


def test_frozen_review_amount_semantics_follow_saved_contract(quarter_company, monkeypatch):
    company = quarter_company
    confirm(company, "quarter-before-old-presentation-close")
    current_builder = close_review.build_owner_review

    def previous_presentation(*args, **kwargs):
        kwargs.setdefault("_presentation_contract", close_review.PRESENTATION_CONTRACT_V1)
        return current_builder(*args, **kwargs)

    def close(period, active):
        periods = inventories(company.engine, company.confirmation, period, active)
        preview = periods.preview_close(period, owner_confirmation=company.confirmation)
        result = periods.close(
            period, owner_confirmation=company.confirmation,
            preview_digest=preview["digest"], epochs=preview["epochs"],
            request_id="close-review-" + period,
        )
        assert result["status"] == "closed"
        return preview["manifest"]["owner_review"]

    with monkeypatch.context() as previous:
        previous.setattr(close_review, "build_owner_review", previous_presentation)
        close("2026-07", {"transactions"})
        close("2026-08", {"transactions"})
        old_review = close(PERIOD, {"tax"})
    old_tax = next(item for item in old_review["business_summary"]
                   if item["kind"] == "tax_assessment")
    assert old_review["presentation_contract"] == close_review.PRESENTATION_CONTRACT_V1
    assert (old_tax["business_amount_fen"], old_tax["amount_label"]) == (None, "业务确认金额")
    before = database_snapshot(company)
    with company.engine.store.connection(read_only=True) as connection:
        assert verify_integrity(company.engine, connection)["status"] == "verified"
    assert database_snapshot(company) == before
    assert_tax_activity_amount(company, brief(company), TOTAL_TAX)

    with company.engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        row = connection.execute("SELECT * FROM period_close WHERE period=?",
                                 (YearMonth(PERIOD).ordinal,)).fetchone()
        manifest = decode_close(connection, row)
        for invalid in (None, "ai-accounting-kernel/2/close-review/99"):
            altered = deepcopy(manifest)
            if invalid is None:
                altered["owner_review"].pop("presentation_contract")
            else:
                altered["owner_review"]["presentation_contract"] = invalid
            with pytest.raises(KernelError) as failure:
                close_review.verify_owner_review_integrity(connection, company.engine, altered)
            assert failure.value.details["reason"] == "invalid_owner_review_contract"
        altered = deepcopy(manifest)
        altered["owner_review"]["presentation_contract"] = close_review.PRESENTATION_CONTRACT
        with pytest.raises(KernelError) as failure:
            close_review.verify_owner_review_integrity(connection, company.engine, altered)
        assert failure.value.details["reason"] == "owner_review_semantic_mismatch"
        altered = deepcopy(manifest)
        changed = next(item for item in altered["owner_review"]["business_summary"]
                       if item["kind"] == "tax_assessment")
        changed["business_amount_fen"] = TOTAL_TAX
        changed["amount_label"] = TAX_AMOUNT_LABEL
        with pytest.raises(KernelError) as failure:
            close_review.verify_owner_review_integrity(connection, company.engine, altered)
        assert failure.value.details["reason"] == "owner_review_semantic_mismatch"
        frozen_rows = [tuple(item) for item in connection.execute("SELECT * FROM period_close")]

    company.sale("october-sale", "2026-10", gross=101_000, eligible=False)
    company.save(taxes.TaxAssessment, "october-tax", period="2026-10",
                 period_start="2026-10-01", period_end="2026-10-31",
                 vat_policy_id="vat", surtax_policy_id="surtax")
    company.publish("october-tax")
    new_review = close("2026-10", {"transactions", "tax"})
    new_tax = next(item for item in new_review["business_summary"]
                   if item["kind"] == "tax_assessment")
    assert new_review["presentation_contract"] == close_review.PRESENTATION_CONTRACT
    assert (new_tax["business_amount_fen"], new_tax["amount_label"]) == (1_060, TAX_AMOUNT_LABEL)
    before = database_snapshot(company)
    with company.engine.store.connection(read_only=True) as connection:
        assert verify_integrity(company.engine, connection)["status"] == "verified"
        assert [tuple(item) for item in connection.execute(
            "SELECT * FROM period_close WHERE period<=?", (YearMonth(PERIOD).ordinal,),
        )] == frozen_rows
    assert database_snapshot(company) == before


@pytest.mark.parametrize("contract", [
    close_review.PRESENTATION_CONTRACT_V1, close_review.PRESENTATION_CONTRACT,
])
def test_review_cursor_is_bound_to_its_saved_presentation_contract(contract):
    cursor = close_review._cursor("close-binding", "vouchers", 0, 1,
                                  presentation_contract=contract)
    assert close_review._decode_cursor(cursor, "close-binding", "vouchers",
                                       presentation_contract=contract) == (0, 1)
    other = (close_review.PRESENTATION_CONTRACT
             if contract == close_review.PRESENTATION_CONTRACT_V1
             else close_review.PRESENTATION_CONTRACT_V1)
    with pytest.raises(KernelError) as failure:
        close_review._decode_cursor(cursor, "close-binding", "vouchers",
                                    presentation_contract=other)
    assert failure.value.code == "dashboard_snapshot_changed"
