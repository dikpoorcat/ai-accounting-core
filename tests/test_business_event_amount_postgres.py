"""Component amount consumption is guarded without a top-level amount envelope."""

import uuid

import pytest
from _postgres_helpers import catalog_owner_authority
from sqlalchemy.orm import Session
from test_business_components import (
    test_local_source_usage_survives_later_reference_and_rounding as assert_source_lifecycle,
)
from test_financial_statements_postgres import _isolated_business_engine

from ai_accounting.coa import seed_organization
from ai_accounting.models import Evidence

pytestmark = [pytest.mark.postgres, pytest.mark.postgres_current]


@pytest.fixture(scope="module")
def postgres_engine():
    with _isolated_business_engine() as engine:
        yield engine


@pytest.mark.parametrize("usage_kind", ["service_fulfillment", "customer_refund"])
def test_component_amount_usage_and_tax_rounding_survive_local_to_posted_sources(usage_kind):
    with _isolated_business_engine() as engine, Session(engine) as session:
        org = seed_organization(
            session,
            name="Component amount test",
            taxpayer_identification_number="91330106MA1234567T",
            accounting_period_control_enabled=False,
        )
        session.commit()
        with catalog_owner_authority(session, org) as authority:
            with authority.attributed_call(session, tool_name="finance_register_evidence"):
                proof = Evidence(
                    org_id=org.id,
                    original_name="source.txt",
                    storage_path="test/source.txt",
                    sha256=uuid.uuid4().hex * 2,
                    size_bytes=1,
                    media_type="text/plain",
                    source="test",
                )
                session.add(proof)
                session.flush()
            session.commit()
            with authority.attributed_call(session, tool_name="finance_record_event"):
                assert_source_lifecycle(session, org, proof, usage_kind)
            session.commit()
