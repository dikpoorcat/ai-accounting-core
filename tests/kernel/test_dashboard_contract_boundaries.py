"""Every actual dashboard response rejects malformed envelopes at the read boundary."""

import copy

import pytest
from response_samples import native_samples

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.response_contracts import validate_response


@pytest.fixture(scope="module")
def live_responses(tmp_path_factory):
    return native_samples(tmp_path_factory.mktemp("dashboard-boundaries"))


@pytest.mark.parametrize(
    "change", ["extra", "missing_version", "wrong_version", "float_version", "bool_version"]
)
def test_all_current_backend_samples_reject_invalid_envelopes(live_responses, change):
    commands = set()
    for sample in live_responses.values():
        original = sample["response"]
        if "schema_version" not in original:
            continue
        command = sample["command"]
        value = copy.deepcopy(original)
        if change == "extra":
            value["private-original-document-name"] = "private-response-content"
        elif change == "missing_version":
            del value["schema_version"]
        elif change == "wrong_version":
            value["schema_version"] += 10
        elif change == "float_version":
            value["schema_version"] = float(value["schema_version"])
        else:
            value["schema_version"] = True
        with pytest.raises(KernelError) as failure:
            validate_response(command, value)
        assert failure.value.code == "response_contract_mismatch", command
        assert "private-original-document-name" not in str(failure.value.response())
        assert "private-response-content" not in str(failure.value.response())
        assert "fact_issues" not in failure.value.response()
        commands.add(command)
    assert commands >= {
        "dashboard_context",
        "dashboard_brief",
        "dashboard_funds",
        "dashboard_employees",
        "dashboard_assets",
        "dashboard_business_status",
        "dashboard_period_preparation",
        "dashboard_quarterly_report",
    }
