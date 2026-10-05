"""Check packaged scope assertions using saved real wire responses, without a book."""

import json
from pathlib import Path

import pytest

from scripts.verify_local_package import assert_owner_read

SAMPLES = json.loads(
    (Path(__file__).resolve().parents[2] / "frontend/tests/fixtures/dashboard-contracts.json")
    .read_text(encoding="utf-8")
)
OWNER_SAMPLES = [
    (name, sample) for name, sample in SAMPLES.items()
    if sample["command"].startswith("dashboard_")
]


def expected_scope(command, value):
    if command == "dashboard_context":
        return (value["current_company"] or {}).get("company_id"), None
    company = value["read_context"]["company_id"]
    if command == "dashboard_period_preparation":
        return company, value["period"]
    if command == "dashboard_quarterly_report":
        quarter = value["period"]
        return company, f"{quarter['year']:04d}-{3 * quarter['quarter'] - 2:02d}"
    return company, (value["selected_period"] or {}).get("key")


@pytest.mark.parametrize("name,sample", OWNER_SAMPLES, ids=[x[0] for x in OWNER_SAMPLES])
def test_packaged_read_scope_uses_each_actual_response_contract(name, sample):
    command, value = sample["command"], sample["response"]
    company, period = expected_scope(command, value)
    subject = value["data"]["identity"]["subject_id"] if (
        command == "dashboard_business_status"
    ) else None
    assert_owner_read(
        command, value, company_id=company, period=period, subject_id=subject, wire=True
    )
    if company is not None:
        with pytest.raises(AssertionError):
            assert_owner_read(command, value, company_id="another-company", wire=True)
    if period is not None:
        with pytest.raises(AssertionError):
            assert_owner_read(command, value, period="2099-12", wire=True)
