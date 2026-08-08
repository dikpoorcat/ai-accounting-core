from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from ai_accounting.coa import seed_organization
from ai_accounting.database import Base, make_engine, make_session_factory
from ai_accounting.models import Organization


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
    return seed_organization(session, name="测试服务公司")
