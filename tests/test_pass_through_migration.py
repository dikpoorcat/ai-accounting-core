"""The empty v3 baseline replaces the removed scene-specific migration chain."""

import pytest
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_financial_statements_postgres import _isolated_business_engine

from ai_accounting.coa import seed_organization
from ai_accounting.models import Account
from alembic import command


@pytest.mark.postgres
@pytest.mark.postgres_current
def test_v3_seeds_pass_through_class_and_refuses_removed_scene_revision():
    with _isolated_business_engine() as engine:
        with Session(engine) as session:
            org = seed_organization(
                session,
                name="Baseline account test",
                taxpayer_identification_number="91330106MA1234567T",
            )
            session.commit()
            accounts = list(
                session.execute(select(Account.id, Account.code, Account.business_class))
            )
            payable = session.scalar(
                select(Account).where(Account.system_role == "pass_through_payable")
            )
            assert payable.code == "224105" and payable.business_class == "pass_through_payable"
            config = Config("alembic.ini")
            config.attributes["database_url_override"] = engine.url.render_as_string(
                hide_password=False
            )
            with pytest.raises(Exception, match="Can't locate revision"):
                command.upgrade(config, "0006_pass_through")
            assert (
                list(session.execute(select(Account.id, Account.code, Account.business_class)))
                == accounts
            )
            assert org.name == "Baseline account test"
