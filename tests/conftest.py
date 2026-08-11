from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pytest
from sqlalchemy.orm import Session

from ai_accounting.coa import seed_organization
from ai_accounting.database import Base, make_engine, make_session_factory
from ai_accounting.models import Organization


@pytest.fixture(autouse=True)
def deterministic_business_date(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep fixed-date unit tests stable; boundary tests override this clock."""

    monkeypatch.setattr("ai_accounting.ledger.china_current_date", lambda: date.max)
    monkeypatch.setattr(
        "ai_accounting.accounting_period_service.china_current_date", lambda: date.max
    )


@pytest.fixture
def session() -> Iterator[Session]:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        with session.begin():
            yield session
    engine.dispose()


@pytest.fixture
def organization(session: Session) -> Organization:
    organization = seed_organization(
        session,
        name="测试服务公司",
        accounting_period_control_enabled=False,
    )
    # Existing module tests intentionally exercise the compatibility path.
    # New accounting-period tests create a fresh organization separately and
    # assert that the product default remains enabled.
    return organization
