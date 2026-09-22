from dataclasses import replace

import pytest
from pydantic import TypeAdapter, ValidationError

from ai_accounting.kernel.domains.payroll import ContributionRuleFact, PayrollContributionPolicy
from ai_accounting.kernel.domains.taxes import VatPolicy
from ai_accounting.kernel.workflow import FilingCalendarPolicy
from ai_accounting.payroll import CalculationValidationError, CumulativeIncomeTaxPolicy
from ai_accounting.policy_sources import OfficialPolicySourceURL


@pytest.mark.parametrize(
    "value",
    (
        "http://www.mof.gov.cn/policy",
        "https://example.com/policy",
        "https://gov.cn.evil.example/policy",
        "https://user:secret@www.gov.cn/policy",
        "https://www.gov.cn\\@evil.example/policy",
        "https://.gov.cn/policy",
        "https://www..gov.cn/policy",
        "https://evil..gov.cn/policy",
        "https://bad_label.gov.cn/policy",
        "https://www.gov.cn/policy\n",
        "\u200bhttps://www.gov.cn/policy",
        "https://www.gov.cn/\u00a0policy",
        "https://www.gov.cn/policy%",
        "https://www.gov.cn/policy%2",
        "https://www.gov.cn/policy%GG",
        *("https://www.gov.cn/policy/" + character for character in '<>"{}|^`'),
    ),
)
def test_official_policy_source_rejects_nonofficial_or_ambiguous_urls(value):
    with pytest.raises(ValidationError):
        TypeAdapter(OfficialPolicySourceURL).validate_python(value)


def test_official_policy_source_accepts_root_and_subdomain_without_rewriting():
    adapter = TypeAdapter(OfficialPolicySourceURL)
    for value in (
        "https://gov.cn",
        "https://www.mof.gov.cn/policy?edition=1#article",
        "https://www.gov.cn/政策/依据",
        "https://www.gov.cn/%E6%94%BF%E7%AD%96",
    ):
        assert adapter.validate_python(value) == value


def test_pure_payroll_policy_uses_the_shared_official_source_rule():
    policy = CumulativeIncomeTaxPolicy.china_resident_wage_withholding()
    with pytest.raises(CalculationValidationError) as error:
        replace(policy, primary_source_url="https://gov.cn.evil.example/policy")
    assert error.value.code == "INVALID_SOURCE_URL"


def test_tax_policy_uses_the_shared_official_source_rule():
    with pytest.raises(ValidationError):
        VatPolicy(
            version="synthetic",
            source_url="https://user:secret@www.gov.cn/policy",
            effective_from="2026-01-01",
            effective_to=None,
            rate_percent="1",
            threshold_fen=0,
            threshold_operator="strictly_below",
        )


def test_kernel_payroll_policy_uses_the_shared_official_source_rule():
    with pytest.raises(ValidationError):
        PayrollContributionPolicy(
            period="2026-01",
            version="synthetic",
            jurisdiction="synthetic",
            effective_from="2026-01-01",
            effective_to=None,
            primary_source_url="https://www.gov.cn.evil.example/policy",
            rules=(
                ContributionRuleFact(
                    code="pension",
                    base_kind="social_insurance",
                    employee_rate="0.08",
                    employer_rate="0.16",
                    minimum_base_fen=0,
                    maximum_base_fen=10_000_000,
                    rounding="half_up",
                    enabled=True,
                ),
            ),
        )


def test_filing_calendar_uses_the_shared_official_source_rule():
    with pytest.raises(ValidationError):
        FilingCalendarPolicy(
            period="2026-01",
            version="synthetic",
            effective_from="2026-01",
            effective_to="2026-12",
            primary_source_url="https://user@www.gov.cn/policy",
            rules=(),
        )
