import json
import uuid

import pytest
from _postgres_helpers import authenticated_business_database
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_essential_postgres import _record_pass_through

from ai_accounting.business_metadata import UpdateBusinessMetadataRequest, update_business_metadata
from ai_accounting.models import BusinessEvent, BusinessMetadataVersion, VoucherLine
from alembic import command


@pytest.mark.postgres
def test_payment_scope_forward_upgrade_preserves_ledger_and_metadata_guards():
    with authenticated_business_database(
        "mybank_scope", revision="0007_mybank_payment_sources"
    ) as (
        engine,
        org_id,
        evidence_id,
        authority,
    ):
        with Session(engine) as session:
            with authority.attributed_call(session, tool_name="finance_record_event"):
                posted = _record_pass_through(
                    session, org_id, evidence_id, key="payout-source", amount=10000
                )
            assert posted.status == "posted"
            session.commit()
            original_facts = session.get(BusinessEvent, posted.event_id).facts
            original_lines = list(
                session.execute(
                    select(VoucherLine.id, VoucherLine.debit_fen, VoucherLine.credit_fen)
                )
            )
            request = UpdateBusinessMetadataRequest(
                org_id=org_id,
                source={"event_key": "payout-source", "component_key": "collection"},
                metadata={
                    "purpose": "回扣报销1",
                    "payment_period": "2026-08",
                    "payment_category": "reimbursement",
                },
                expected_version=1,
                idempotency_key="payment-scope-v2",
            )
            with pytest.raises(DBAPIError, match="BUSINESS_METADATA_SOURCE_OR_PAYLOAD_INVALID"):
                with authority.attributed_call(
                    session, tool_name="finance_update_business_metadata"
                ):
                    update_business_metadata(session, request)
            session.rollback()
            config = Config("alembic.ini")
            config.attributes["database_url_override"] = engine.url.render_as_string(
                hide_password=False
            )
            command.upgrade(config, "0008_mybank_payment_metadata")
            with authority.attributed_call(session, tool_name="finance_update_business_metadata"):
                result = update_business_metadata(session, request)
            assert result["status"] == "updated", result
            session.commit()
            assert session.get(BusinessEvent, posted.event_id).facts == original_facts
            assert (
                list(
                    session.execute(
                        select(VoucherLine.id, VoucherLine.debit_fen, VoucherLine.credit_fen)
                    )
                )
                == original_lines
            )
            version = session.scalar(
                select(BusinessMetadataVersion).where(BusinessMetadataVersion.version == 2)
            )
            version_id = version.id
            for payload, error in [
                ({"payment_period": "2026-13"}, "PAYMENT_PERIOD_INVALID"),
                ({"payment_category": "salary"}, "PAYMENT_CATEGORY_INVALID"),
                ({"amount_fen": 1}, "SOURCE_OR_PAYLOAD_INVALID"),
            ]:
                with pytest.raises(DBAPIError, match=error):
                    with authority.attributed_call(
                        session, tool_name="finance_update_business_metadata"
                    ):
                        session.execute(
                            text("""
INSERT INTO business_metadata_versions
(id,org_id,event_id,component_key,version,metadata_values,idempotency_key,request_hash,execution_attribution_id,created_at)
SELECT :id,org_id,event_id,component_key,version+1,CAST(:payload AS json),:key,request_hash,
       current_setting('finance.execution_attribution_id')::uuid,CURRENT_TIMESTAMP
FROM business_metadata_versions WHERE id=:source
"""),
                            {
                                "id": uuid.uuid4(),
                                "source": version_id,
                                "key": str(uuid.uuid4()),
                                "payload": json.dumps(payload),
                            },
                        )
                session.rollback()
