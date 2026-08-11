from __future__ import annotations

import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import make_url

from ai_accounting import models  # noqa: F401
from ai_accounting.config import get_settings
from ai_accounting.database import Base
from alembic import context

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
config.set_main_option(
    "sqlalchemy.url",
    get_settings().migration_database_url(
        os.getenv("DATABASE_URL", config.get_main_option("sqlalchemy.url"))
    ),
)
target_metadata = Base.metadata

_POSTGRESQL_ONLY_CHECK_CONSTRAINTS = {
    "ck_owner_account_password_hash_shape",
    "ck_execution_attribution_executor_name_ascii",
    "ck_execution_attribution_executor_version_ascii",
    "ck_execution_attribution_tool_name_ascii",
}


def _include_object_for_dialect(dialect_name: str):
    def include_object(_object, name, type_, _reflected, _compare_to):
        return not (
            dialect_name != "postgresql"
            and type_ == "check_constraint"
            and name in _POSTGRESQL_ONLY_CHECK_CONSTRAINTS
        )

    return include_object


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=_include_object_for_dialect(
            make_url(config.get_main_option("sqlalchemy.url")).get_backend_name()
        ),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_object=_include_object_for_dialect(connection.dialect.name),
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
