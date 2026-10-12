"""Exercise the package verifier's current facts in a private synthetic service."""

import pytest
from pydantic import SecretStr

from ai_accounting.kernel.contracts import KernelError, Read
from ai_accounting.kernel.dashboard_async import run_pending
from ai_accounting.kernel.service import LocalService
from scripts.verify_local_package import verify_reserve_business


def test_package_reserve_inputs_keep_explicit_tax_and_actual_payment_split(tmp_path):
    service = LocalService(tmp_path / "private-service")
    password = SecretStr("Synthetic-reserve-verifier-only-2026")
    service.security.provision("synthetic-owner", password)
    token = service.security.login("synthetic-owner", password).session_token

    def call(command, payload):
        if command.startswith("dashboard_"):
            run_pending(service.engine(payload["company_id"]), limit=12)
        return service.dispatch(command, payload, session_token=token)

    def rejected(command, payload, *, code):
        with pytest.raises(KernelError) as error:
            call(command, payload)
        assert error.value.code == code

    class PreviewReached(Exception):
        pass

    def approve_close(company, period, preview):
        engine = service.engine(company["id"])
        with engine.store.connection(read_only=True) as connection:
            wage = engine.store.select(connection, Read("calculation", "payroll", "@reserve-wage"))[
                0
            ]
        assert wage.values["tax_fen"] == 0 and wage.values["net_fen"] == 420000
        assert wage.values["tax_basis"]["tax_amount_fen"] == 0
        assert preview["manifest"]["period"] == period
        raise PreviewReached

    try:
        with pytest.raises(PreviewReached):
            verify_reserve_business(call, rejected, approve_close, None, tmp_path)
    finally:
        service.close()
