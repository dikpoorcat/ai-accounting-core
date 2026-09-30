"""The package verifier must prove a real spawned brief, not serial fallback."""

import json

import pytest
from pydantic import SecretStr
from test_brief_parallel_integration import PERIOD, _fund

from ai_accounting.kernel.service import LocalService
from scripts.verify_local_package import verify_parallel_brief


@pytest.fixture
def parallel_service(tmp_path):
    app = LocalService(
        tmp_path / "synthetic-package-parallel",
        enable_read_pool=True,
        enable_parallel_brief=True,
    )
    password = SecretStr("Synthetic-package-parallel-owner-123")
    app.security.provision("owner", password)
    token = app.security.login("owner", password).session_token
    company = app.catalog.create_company("91310000123456789A", "合成包并行公司")
    _fund(app.engine(company["id"]), "package-funding", 123456)
    try:
        yield app, token, company
    finally:
        app.close()


def test_package_parallel_smoke_records_real_workers_and_rejects_serial_fallback(
    parallel_service, tmp_path
):
    app, token, company = parallel_service

    def call(command, payload):
        return app.dispatch(command, payload, session_token=token)

    output = tmp_path / "parallel-brief.json"
    response = verify_parallel_brief(app, call, company["id"], PERIOD, output)
    proof = json.loads(output.read_text(encoding="utf-8"))
    assert response["data"]["voucher_count"] == 1
    assert proof["parallel_finish"] is True
    assert proof["company_id"] == company["id"]
    assert len(set(proof["pool_worker_pids"])) == 3
    assert set(proof["worker_pids"].values()) <= set(proof["pool_worker_pids"])

    assert app.brief_parallel._gate.acquire(blocking=False)
    try:
        fallback_output = tmp_path / "fallback-must-not-pass.json"
        with pytest.raises(AssertionError, match="silently fell back"):
            verify_parallel_brief(app, call, company["id"], PERIOD, fallback_output)
        assert not fallback_output.exists()
    finally:
        app.brief_parallel._gate.release()
