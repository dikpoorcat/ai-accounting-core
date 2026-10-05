"""Package smoke validates actual owner business results and request scope."""

import copy
import json

import pytest
from pydantic import SecretStr
from test_brief_parallel_integration import PERIOD, _fund

from ai_accounting.kernel.service import LocalService
from scripts.verify_local_package import verify_owner_brief


@pytest.fixture
def resident_service(tmp_path):
    app = LocalService(tmp_path / "synthetic-package-owner", enable_read_pool=True)
    password = SecretStr("Synthetic-package-owner-123")
    app.security.provision("owner", password)
    token = app.security.login("owner", password).session_token
    company = app.catalog.create_company("91310000123456789A", "合成包公司")
    _fund(app.engine(company["id"]), "package-funding", 123456)
    try:
        yield app, token, company
    finally:
        app.close()


def test_package_owner_smoke_validates_contract_scope_and_amount(resident_service, tmp_path):
    app, token, company = resident_service
    def call(command, payload):
        return app.dispatch(command, payload, session_token=token)
    output = tmp_path / "owner-brief.json"
    response = verify_owner_brief(app, call, company["id"], PERIOD, output)
    proof = json.loads(output.read_text(encoding="utf-8"))
    assert response["data"]["activity_count"] == 1
    assert response["data"]["funds_overview"]["bank_fen"] == 123456
    assert proof["activity_count"] == 1
    assert proof["funds_overview"]["bank_fen"] == "123456"
    assert proof["company_id"] == company["id"] and proof["period"] == PERIOD
    assert proof["response_contract_validated"] and proof["single_snapshot_result_equal"]


@pytest.mark.parametrize("fault", ["company", "month", "amount"])
def test_package_owner_smoke_rejects_wrong_scope_or_business_amount(
    resident_service, tmp_path, fault
):
    app, token, company = resident_service
    def call(command, payload):
        result = copy.deepcopy(app.dispatch(command, payload, session_token=token))
        if fault == "company":
            result["read_context"]["company_id"] = "another-company"
        elif fault == "month":
            result["selected_period"]["key"] = "2026-10"
        else:
            result["data"]["funds_overview"]["bank_fen"] += 1
        return result
    output = tmp_path / "invalid-must-not-pass.json"
    with pytest.raises(AssertionError):
        verify_owner_brief(app, call, company["id"], PERIOD, output)
    assert not output.exists()
