from __future__ import annotations

import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, inspect, pool
from sqlalchemy.engine import make_url

from ai_accounting import models  # noqa: F401
from ai_accounting.config import DEVELOPMENT_DATABASE_URL, get_settings
from ai_accounting.database import Base
from alembic import context

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
database_url_override = config.attributes.get("database_url_override")
configured_database_url = config.get_main_option("sqlalchemy.url")
environment_database_url = os.getenv("DATABASE_URL")
if database_url_override:
    migration_database_url = database_url_override
elif environment_database_url:
    migration_database_url = get_settings().migration_database_url(
        environment_database_url
    )
elif configured_database_url != DEVELOPMENT_DATABASE_URL:
    # Programmatic callers (especially isolated migration and invariant tests)
    # deliberately replace alembic.ini's checked-in development placeholder.
    migration_database_url = configured_database_url
else:
    migration_database_url = get_settings().migration_database_url(
        configured_database_url
    )
config.set_main_option(
    "sqlalchemy.url",
    migration_database_url,
)
target_metadata = Base.metadata

_POSTGRESQL_ONLY_CHECK_CONSTRAINTS = {
    "ck_company_registry_database_name",
    "ck_owner_account_password_hash_shape",
    "ck_owner_account_login_ascii",
    "ck_owner_recovery_code_lowerhex",
    "ck_owner_session_secret_lowerhex",
    "ck_execution_attribution_executor_name_ascii",
    "ck_execution_attribution_executor_version_ascii",
    "ck_execution_attribution_tool_name_ascii",
    "ck_evidence_sha256_lower_hex",
    "ck_intangible_asset_acquisition_month",
    "ck_tax_period_hash_lower_hex",
    "ck_zero_tax_confirmation_request_hash_lower_hex",
    "ck_zero_tax_confirmation_hash_lower_hex",
    "ck_borrowing_accrual_hash_lower_hex",
    "ck_intangible_amortization_period_month_start",
    "ck_intangible_amortization_posting_month",
    "ck_intangible_amortization_hash_lower_hex",
    "ck_intangible_retirement_month_end",
    "ck_fixed_asset_depreciation_posting_month",
    "ck_fixed_asset_depreciation_hash_lower_hex",
    "ck_fixed_asset_depreciation_period_month_start",
    "ck_fixed_asset_depreciation_batch_hash_lower_hex",
    "ck_fixed_asset_depreciation_batch_period_month_start",
    "ck_financial_statement_classification_hash_lower_hex",
    "ck_enterprise_income_tax_confirmation_hash_lower_hex",
    "ck_period_close_commentary_context_hash_lower_hex",
}

_CATALOG_TABLES = {
    "accounting_period_close_backups",
    "catalog_metadata",
    "close_backup_location_versions",
    "company_registry",
    "company_lifecycle_actions",
}
_IDENTITY_TABLES = {
    "identity_audit_events",
    "owner_accounts",
    "owner_recovery_codes",
    "owner_sessions",
}


def _include_object_for_dialect(dialect_name: str, *, identity_split: bool = False):
    def include_object(_object, name, type_, _reflected, _compare_to):
        table_name = (
            name
            if type_ == "table"
            else getattr(getattr(_object, "table", None), "name", None)
        )
        if table_name in _CATALOG_TABLES:
            return False
        if identity_split and table_name in _IDENTITY_TABLES:
            return False
        if identity_split and type_ == "foreign_key_constraint":
            if any(
                element.target_fullname.split(".", 1)[0] in _IDENTITY_TABLES
                for element in getattr(_object, "elements", ())
            ):
                return False
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
            make_url(config.get_main_option("sqlalchemy.url")).get_backend_name(),
            identity_split=bool(config.attributes.get("identity_split_verified")),
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
        inspector = inspect(connection)
        identity_split = inspector.has_table(
            "organization_database_metadata"
        ) and not inspector.has_table("owner_accounts")
        # Inspector reads autobegin a SQLAlchemy transaction. End that read
        # transaction so Alembic owns and commits the migration transaction.
        connection.rollback()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_object=_include_object_for_dialect(
                connection.dialect.name,
                identity_split=identity_split,
            ),
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
