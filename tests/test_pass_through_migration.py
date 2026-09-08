import pytest
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session
from test_business_event_amount_postgres import IMAGE
from testcontainers.community.postgres import PostgresContainer

from ai_accounting.coa import seed_organization
from ai_accounting.models import Account
from alembic import command


@pytest.mark.postgres
@pytest.mark.postgres_current
def test_populated_company_upgrade_seeds_only_pass_through_account():
    with PostgresContainer(IMAGE, driver="psycopg") as postgres:
        url = postgres.get_connection_url(driver="psycopg")
        config = Config("alembic.ini")
        config.attributes["database_url_override"] = url
        command.upgrade(config, "head")
        engine = create_engine(url)
        try:
            with Session(engine) as session:
                org = seed_organization(
                    session,
                    name="Existing company",
                    taxpayer_identification_number="91330106MA1234567T",
                )
                company_id = org.id
                session.commit()
            command.downgrade(config, "0005_event_amount_null")
            with engine.connect() as connection:
                before = connection.execute(
                    text("SELECT id,code,name FROM accounts ORDER BY code")
                ).all()
                org_before = connection.execute(text("SELECT * FROM organizations")).all()
            command.upgrade(config, "head")
            command.check(config)
            with engine.connect() as connection:
                assert (
                    connection.execute(
                        text("SELECT id,code,name FROM accounts WHERE code<>'224105' ORDER BY code")
                    ).all()
                    == before
                )
                assert connection.execute(text("SELECT * FROM organizations")).all() == org_before
                assert (
                    connection.scalar(
                        select(Account.code).where(
                            Account.org_id == company_id,
                            Account.system_role == "pass_through_payable",
                        )
                    )
                    == "224105"
                )
        finally:
            engine.dispose()
