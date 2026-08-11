"""Add deterministic natural-month accounting-period control and close snapshots.

Revision ID: 0012_accounting_period_close
Revises: 0011_intangible_borrowings
Create Date: 2026-08-11
"""

# ruff: noqa: E501

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa

from alembic import op

revision = "0012_accounting_period_close"
down_revision = "0011_intangible_borrowings"
branch_labels = None
depends_on = None


def _event_amount(facts: dict[str, Any]) -> int:
    amounts = facts.get("amounts")
    if not isinstance(amounts, dict):
        raise RuntimeError("BUSINESS_EVENT_DEPENDENCY_PRECHECK_FAILED")
    raw = amounts.get("gross_amount_fen", amounts.get("amount_fen"))
    if type(raw) is not int or raw <= 0:
        raise RuntimeError("BUSINESS_EVENT_DEPENDENCY_PRECHECK_FAILED")
    return raw


def _parent_amount(event_type: str, facts: dict[str, Any]) -> int:
    if event_type == "customer_receipt":
        derived = facts.get("derived")
        raw = derived.get("advance_fen") if isinstance(derived, dict) else None
        if type(raw) is not int or raw <= 0:
            raise RuntimeError("BUSINESS_EVENT_DEPENDENCY_PRECHECK_FAILED")
        return raw
    return _event_amount(facts)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise RuntimeError("BUSINESS_EVENT_DEPENDENCY_PRECHECK_FAILED") from exc
    if not isinstance(value, dict):
        raise RuntimeError("BUSINESS_EVENT_DEPENDENCY_PRECHECK_FAILED")
    return value


def _preflight() -> list[dict[str, Any]]:
    """Reject legacy facts before 0012 creates its first object."""

    bind = op.get_bind()
    if bind.execute(sa.text("SELECT 1 FROM accounting_periods LIMIT 1")).scalar() is not None:
        raise RuntimeError("ACCOUNTING_PERIOD_LEGACY_PERIOD_PRECHECK_FAILED")

    rows = (
        bind.execute(
            sa.text(
                """
            SELECT id, org_id, event_type, status, facts
              FROM business_events
             WHERE event_type IN ('service_fulfillment','customer_refund')
               AND status IN ('posted','reversed')
             ORDER BY org_id, id
            """
            )
        )
        .mappings()
        .all()
    )
    if not rows:
        return []
    all_events = {
        str(uuid.UUID(str(row["id"]))): dict(row, facts=_json_object(row["facts"]))
        for row in bind.execute(
            sa.text("SELECT id, org_id, event_type, status, facts FROM business_events")
        ).mappings()
    }
    result: list[dict[str, Any]] = []
    active_usage: dict[str, int] = {}
    for child in rows:
        facts = _json_object(child["facts"])
        details = facts.get("details")
        if not isinstance(details, dict):
            raise RuntimeError("BUSINESS_EVENT_DEPENDENCY_PRECHECK_FAILED")
        try:
            parent_key = str(uuid.UUID(str(details.get("original_event_id"))))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("BUSINESS_EVENT_DEPENDENCY_PRECHECK_FAILED") from exc
        parent = all_events.get(parent_key)
        if parent is None or uuid.UUID(str(parent["org_id"])) != uuid.UUID(str(child["org_id"])):
            raise RuntimeError("BUSINESS_EVENT_DEPENDENCY_PRECHECK_FAILED")
        if child["event_type"] == "service_fulfillment":
            kind = "advance_fulfillment"
            allowed = {"customer_advance", "customer_receipt"}
        elif details.get("refund_kind") == "advance":
            kind = "advance_refund"
            allowed = {"customer_advance", "customer_receipt"}
        elif details.get("refund_kind") == "sale_return":
            kind = "sale_return"
            allowed = {"service_cash_sale"}
        else:
            raise RuntimeError("BUSINESS_EVENT_DEPENDENCY_PRECHECK_FAILED")
        if parent["event_type"] not in allowed:
            raise RuntimeError("BUSINESS_EVENT_DEPENDENCY_PRECHECK_FAILED")
        if parent["status"] not in {"posted", "reversed"}:
            raise RuntimeError("BUSINESS_EVENT_DEPENDENCY_PRECHECK_FAILED")
        amount = _event_amount(facts)
        if child["status"] == "posted":
            if parent["status"] != "posted":
                raise RuntimeError("BUSINESS_EVENT_DEPENDENCY_PRECHECK_FAILED")
            active_usage[parent_key] = active_usage.get(parent_key, 0) + amount
        result.append(
            {
                "id": uuid.uuid4(),
                "org_id": uuid.UUID(str(child["org_id"])),
                "parent_event_id": uuid.UUID(str(parent["id"])),
                "child_event_id": uuid.UUID(str(child["id"])),
                "dependency_kind": kind,
                "amount_fen": amount,
            }
        )
    for parent_key, used in active_usage.items():
        parent = all_events[parent_key]
        if used > _parent_amount(parent["event_type"], parent["facts"]):
            raise RuntimeError("BUSINESS_EVENT_DEPENDENCY_PRECHECK_FAILED")
    return result


def _create_schema(dependencies: list[dict[str, Any]]) -> None:
    bind = op.get_bind()
    with op.batch_alter_table("organizations") as batch_op:
        batch_op.add_column(
            sa.Column(
                "accounting_period_control_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.add_column(
            sa.Column("accounting_period_control_start_date", sa.Date(), nullable=True)
        )
        batch_op.create_check_constraint(
            "ck_org_accounting_period_control",
            "accounting_period_control_enabled IS TRUE OR "
            "accounting_period_control_start_date IS NULL",
        )
    # Existing organizations are the compatibility cohort.  New rows default
    # to fail-closed period control after the migration completes.
    if bind.dialect.name == "postgresql":
        op.alter_column(
            "organizations",
            "accounting_period_control_enabled",
            server_default=sa.true(),
        )
    else:
        with op.batch_alter_table("organizations") as batch_op:
            batch_op.alter_column("accounting_period_control_enabled", server_default=sa.true())

    op.create_table(
        "accounting_period_actions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("action_type", sa.String(length=30), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=True),
        sa.Column("request_payload_hash", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("input_facts", sa.JSON(), nullable=False),
        sa.Column("missing_information", sa.JSON(), nullable=False),
        sa.Column("errors", sa.JSON(), nullable=False),
        sa.Column("confirmed_by", sa.String(length=100), nullable=True),
        sa.Column("confirmation_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "action_type IN ('period_generation','period_close')",
            name="ck_accounting_period_action_type",
        ),
        sa.CheckConstraint(
            "status IN ('posted','needs_information','rejected')",
            name="ck_accounting_period_action_status",
        ),
        sa.CheckConstraint(
            "request_payload_hash IS NULL OR length(request_payload_hash) = 64",
            name="ck_accounting_period_action_hash_length",
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "id", name="uq_accounting_period_action_org_id"),
        sa.UniqueConstraint(
            "org_id", "idempotency_key", name="uq_accounting_period_action_idempotency"
        ),
    )
    op.create_index("ix_accounting_period_actions_org_id", "accounting_period_actions", ["org_id"])
    op.create_table(
        "accounting_period_calendars",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("calendar_year", sa.Integer(), nullable=False),
        sa.Column("rule_version", sa.String(length=80), nullable=False),
        sa.Column("rule_effective_from", sa.Date(), nullable=False),
        sa.Column("source_urls", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "calendar_year BETWEEN 1 AND 9999", name="ck_accounting_period_calendar_year"
        ),
        sa.CheckConstraint(
            "length(trim(rule_version)) > 0", name="ck_accounting_period_calendar_rule"
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "id", name="uq_accounting_period_calendar_org_id"),
        sa.UniqueConstraint(
            "org_id", "calendar_year", name="uq_accounting_period_calendar_org_year"
        ),
    )
    op.create_index(
        "ix_accounting_period_calendars_org_id", "accounting_period_calendars", ["org_id"]
    )

    with op.batch_alter_table("accounting_periods") as batch_op:
        batch_op.add_column(sa.Column("calendar_id", sa.Uuid(), nullable=False))
        batch_op.add_column(sa.Column("generation_action_id", sa.Uuid(), nullable=False))
        batch_op.add_column(sa.Column("calendar_year", sa.Integer(), nullable=False))
        batch_op.add_column(sa.Column("calendar_month", sa.Integer(), nullable=False))
        batch_op.create_foreign_key(
            "fk_accounting_period_org_calendar",
            "accounting_period_calendars",
            ["org_id", "calendar_id"],
            ["org_id", "id"],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            "fk_accounting_period_org_generation_action",
            "accounting_period_actions",
            ["org_id", "generation_action_id"],
            ["org_id", "id"],
            ondelete="RESTRICT",
        )
        batch_op.create_unique_constraint("uq_accounting_period_org_id", ["org_id", "id"])
        batch_op.create_unique_constraint(
            "uq_accounting_period_org_month", ["org_id", "calendar_year", "calendar_month"]
        )
        batch_op.create_unique_constraint(
            "uq_accounting_period_generation_action", ["generation_action_id"]
        )
        batch_op.create_check_constraint("ck_period_year", "calendar_year BETWEEN 1 AND 9999")
        batch_op.create_check_constraint("ck_period_month", "calendar_month BETWEEN 1 AND 12")
        batch_op.create_check_constraint(
            "ck_period_natural_month",
            "calendar_year = CAST(substr(CAST(start_date AS VARCHAR), 1, 4) AS INTEGER) "
            "AND calendar_month = CAST(substr(CAST(start_date AS VARCHAR), 6, 2) AS INTEGER) "
            "AND substr(CAST(start_date AS VARCHAR), 9, 2) = '01' "
            "AND calendar_year = CAST(substr(CAST(end_date AS VARCHAR), 1, 4) AS INTEGER) "
            "AND calendar_month = CAST(substr(CAST(end_date AS VARCHAR), 6, 2) AS INTEGER) "
            "AND CAST(substr(CAST(end_date AS VARCHAR), 9, 2) AS INTEGER) BETWEEN 28 AND 31",
        )
    op.create_index("ix_accounting_periods_calendar_id", "accounting_periods", ["calendar_id"])

    op.create_table(
        "accounting_period_closes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("period_id", sa.Uuid(), nullable=False),
        sa.Column("action_id", sa.Uuid(), nullable=False),
        sa.Column("calculation", sa.JSON(), nullable=False),
        sa.Column("calculation_payload", sa.Text(), nullable=False),
        sa.Column("calculation_hash", sa.String(length=64), nullable=False),
        sa.Column("rule_version", sa.String(length=80), nullable=False),
        sa.Column("rule_effective_from", sa.Date(), nullable=False),
        sa.Column("source_urls", sa.JSON(), nullable=False),
        sa.Column("previous_close_hash", sa.String(length=64), nullable=True),
        sa.Column("checker_version", sa.String(length=80), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("voucher_count", sa.Integer(), nullable=False),
        sa.Column("line_count", sa.Integer(), nullable=False),
        sa.Column("total_debit_fen", sa.BigInteger(), nullable=False),
        sa.Column("total_credit_fen", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("length(calculation_payload) > 0", name="ck_period_close_payload"),
        sa.CheckConstraint("length(calculation_hash) = 64", name="ck_period_close_hash_length"),
        sa.CheckConstraint(
            "previous_close_hash IS NULL OR length(previous_close_hash) = 64",
            name="ck_period_close_previous_hash_length",
        ),
        sa.CheckConstraint("voucher_count >= 0 AND line_count >= 0", name="ck_period_close_counts"),
        sa.CheckConstraint(
            "total_debit_fen >= 0 AND total_debit_fen = total_credit_fen",
            name="ck_period_close_totals",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "period_id"],
            ["accounting_periods.org_id", "accounting_periods.id"],
            name="fk_accounting_period_close_org_period",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "action_id"],
            ["accounting_period_actions.org_id", "accounting_period_actions.id"],
            name="fk_accounting_period_close_org_action",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "id", name="uq_accounting_period_close_org_id"),
        sa.UniqueConstraint("period_id", name="uq_accounting_period_close_period"),
        sa.UniqueConstraint("action_id", name="uq_accounting_period_close_action"),
    )
    op.create_index("ix_accounting_period_closes_org_id", "accounting_period_closes", ["org_id"])
    with op.batch_alter_table("accounting_periods") as batch_op:
        batch_op.add_column(sa.Column("close_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            "fk_accounting_period_org_close",
            "accounting_period_closes",
            ["org_id", "close_id"],
            ["org_id", "id"],
            ondelete="RESTRICT",
        )
        batch_op.create_unique_constraint("uq_accounting_period_close_id", ["close_id"])
        batch_op.create_check_constraint(
            "ck_period_close_state",
            "(status = 'open' AND closed_at IS NULL AND close_id IS NULL) OR "
            "(status = 'closed' AND closed_at IS NOT NULL AND close_id IS NOT NULL)",
        )

    op.create_table(
        "accounting_period_close_sources",
        sa.Column("close_id", sa.Uuid(), nullable=False),
        sa.Column("voucher_id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("voucher_number", sa.String(length=50), nullable=False),
        sa.Column("posting_date", sa.Date(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("event_type", sa.String(length=60), nullable=False),
        sa.Column("event_status_at_close", sa.String(length=30), nullable=False),
        sa.Column("request_payload_hash_at_close", sa.String(length=64), nullable=True),
        sa.Column("debit_fen", sa.BigInteger(), nullable=False),
        sa.Column("credit_fen", sa.BigInteger(), nullable=False),
        sa.Column("line_snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "event_status_at_close IN ('posted','reversed')",
            name="ck_period_close_source_event_status",
        ),
        sa.CheckConstraint(
            "debit_fen > 0 AND debit_fen = credit_fen",
            name="ck_period_close_source_balanced",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "close_id"],
            ["accounting_period_closes.org_id", "accounting_period_closes.id"],
            name="fk_period_close_source_org_close",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "voucher_id"],
            ["vouchers.org_id", "vouchers.id"],
            name="fk_period_close_source_org_voucher",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_period_close_source_org_event",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("close_id", "voucher_id"),
    )
    op.create_index(
        "ix_accounting_period_close_sources_org_id",
        "accounting_period_close_sources",
        ["org_id"],
    )
    op.create_table(
        "accounting_period_action_evidence",
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("action_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["org_id", "action_id"],
            ["accounting_period_actions.org_id", "accounting_period_actions.id"],
            name="fk_period_action_evidence_org_action",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "evidence_id"],
            ["evidence.org_id", "evidence.id"],
            name="fk_period_action_evidence_org_evidence",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("org_id", "action_id", "evidence_id"),
    )

    op.create_table(
        "business_event_dependencies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("parent_event_id", sa.Uuid(), nullable=False),
        sa.Column("child_event_id", sa.Uuid(), nullable=False),
        sa.Column("dependency_kind", sa.String(length=30), nullable=False),
        sa.Column("amount_fen", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "dependency_kind IN ('advance_fulfillment','advance_refund','sale_return')",
            name="ck_business_event_dependency_kind",
        ),
        sa.CheckConstraint(
            "parent_event_id <> child_event_id", name="ck_event_dependency_distinct"
        ),
        sa.CheckConstraint("amount_fen > 0", name="ck_event_dependency_amount"),
        sa.ForeignKeyConstraint(
            ["org_id", "parent_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_business_event_dependency_org_parent",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "child_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_business_event_dependency_org_child",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "id", name="uq_business_event_dependency_org_id"),
        sa.UniqueConstraint("child_event_id", name="uq_business_event_dependency_child"),
    )
    op.create_index(
        "ix_business_event_dependencies_org_id", "business_event_dependencies", ["org_id"]
    )
    op.create_index(
        "ix_business_event_dependencies_parent_event_id",
        "business_event_dependencies",
        ["parent_event_id"],
    )
    op.create_table(
        "accounting_period_dependency_migration_actions",
        sa.Column("dependency_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["dependency_id"],
            ["business_event_dependencies.id"],
            name="fk_period_dependency_migration_action_dependency",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("dependency_id"),
    )
    if dependencies:
        table = sa.table(
            "business_event_dependencies",
            sa.column("id", sa.Uuid()),
            sa.column("org_id", sa.Uuid()),
            sa.column("parent_event_id", sa.Uuid()),
            sa.column("child_event_id", sa.Uuid()),
            sa.column("dependency_kind", sa.String()),
            sa.column("amount_fen", sa.BigInteger()),
            sa.column("created_at", sa.DateTime(timezone=True)),
        )
        bind.execute(
            table.insert(),
            [dict(row, created_at=datetime.now(UTC)) for row in dependencies],
        )
        actions = sa.table(
            "accounting_period_dependency_migration_actions",
            sa.column("dependency_id", sa.Uuid()),
            sa.column("created_at", sa.DateTime(timezone=True)),
        )
        bind.execute(
            actions.insert(),
            [{"dependency_id": row["id"], "created_at": datetime.now(UTC)} for row in dependencies],
        )

    with op.batch_alter_table("tax_periods") as batch_op:
        batch_op.add_column(sa.Column("adjustment_posting_date", sa.Date(), nullable=True))
    op.execute(
        """
        UPDATE tax_periods AS period
           SET adjustment_posting_date = (
               SELECT event.posting_date FROM business_events AS event
                WHERE event.id = period.adjustment_event_id
                  AND event.org_id = period.org_id
           )
        """
    )
    with op.batch_alter_table("tax_periods") as batch_op:
        batch_op.alter_column("adjustment_posting_date", nullable=False)


def upgrade() -> None:
    dependencies = _preflight()
    _create_schema(dependencies)
    _install_postgresql_guards()


def _install_postgresql_guards() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        r"""
        ALTER TABLE accounting_periods
          ADD CONSTRAINT ex_accounting_period_no_overlap
          EXCLUDE USING gist (
              org_id WITH =,
              daterange(start_date, end_date, '[]') WITH &&
          ) DEFERRABLE INITIALLY DEFERRED;

        CREATE OR REPLACE FUNCTION finance_lock_accounting_month(
            target_org_id uuid, target_posting_date date
        ) RETURNS void AS $$
        BEGIN
            IF target_org_id IS NULL OR target_posting_date IS NULL THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_NOT_GENERATED';
            END IF;
            PERFORM pg_advisory_xact_lock(hashtextextended(
                'accounting_period:' || target_org_id::text || ':' ||
                date_trunc('month', target_posting_date)::date::text,
                0
            ));
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_lock_accounting_period_generation_org(
            target_org_id uuid
        ) RETURNS void AS $$
        BEGIN
            IF target_org_id IS NULL THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_NOT_GENERATED';
            END IF;
            PERFORM pg_advisory_xact_lock(hashtextextended(
                'accounting-period-generation-org:' || target_org_id::text, 0
            ));
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_assert_accounting_write_period(
            target_org_id uuid, target_posting_date date
        ) RETURNS void AS $$
        DECLARE target_org organizations%ROWTYPE;
        DECLARE target_period accounting_periods%ROWTYPE;
        BEGIN
            IF target_org_id IS NULL OR target_posting_date IS NULL THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_NOT_GENERATED';
            END IF;
            IF target_posting_date >
               (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Shanghai')::date THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_FUTURE_POSTING_NOT_ALLOWED';
            END IF;
            PERFORM pg_advisory_xact_lock(hashtextextended(
                'tax-period-org:' || target_org_id::text, 0
            ));
            PERFORM finance_lock_accounting_month(target_org_id, target_posting_date);
            SELECT * INTO target_org FROM organizations
             WHERE id = target_org_id FOR KEY SHARE;
            IF NOT FOUND THEN RETURN; END IF;
            IF target_org.accounting_period_control_enabled IS FALSE THEN
                IF EXISTS (
                    SELECT 1 FROM accounting_periods AS period
                     WHERE period.org_id = target_org_id AND period.status = 'closed'
                       AND target_posting_date BETWEEN period.start_date AND period.end_date
                ) THEN
                    RAISE EXCEPTION 'ACCOUNTING_PERIOD_CLOSED';
                END IF;
                RETURN;
            END IF;
            IF target_org.accounting_period_control_start_date IS NULL
               OR target_posting_date < target_org.accounting_period_control_start_date THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_NOT_GENERATED';
            END IF;
            SELECT * INTO target_period FROM accounting_periods AS period
             WHERE period.org_id = target_org_id
               AND target_posting_date BETWEEN period.start_date AND period.end_date;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_NOT_GENERATED';
            ELSIF target_period.status = 'closed' THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_CLOSED';
            END IF;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_guard_final_voucher_accounting_period()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.status NOT IN ('posted','reversed') THEN RETURN NEW; END IF;
            IF TG_OP = 'UPDATE'
               AND OLD.status = NEW.status
               AND OLD.org_id = NEW.org_id
               AND OLD.posting_date = NEW.posting_date THEN
                RETURN NEW;
            END IF;
            PERFORM finance_assert_accounting_write_period(NEW.org_id, NEW.posting_date);
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS final_voucher_closed_period_guard ON vouchers;
        DROP FUNCTION IF EXISTS finance_block_final_voucher_in_closed_period();
        CREATE TRIGGER final_voucher_accounting_period_guard
        BEFORE INSERT OR UPDATE ON vouchers
        FOR EACH ROW EXECUTE FUNCTION finance_guard_final_voucher_accounting_period();

        CREATE OR REPLACE FUNCTION finance_validate_draft_business_event_period()
        RETURNS trigger AS $$
        DECLARE current_event business_events%ROWTYPE;
        BEGIN
            SELECT * INTO current_event FROM business_events WHERE id = NEW.id;
            IF FOUND AND current_event.status = 'draft' THEN
                PERFORM finance_assert_accounting_write_period(
                    current_event.org_id, current_event.posting_date
                );
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_draft_voucher_period()
        RETURNS trigger AS $$
        DECLARE current_voucher vouchers%ROWTYPE;
        BEGIN
            SELECT * INTO current_voucher FROM vouchers WHERE id = NEW.id;
            IF FOUND AND current_voucher.status = 'draft' THEN
                PERFORM finance_assert_accounting_write_period(
                    current_voucher.org_id, current_voucher.posting_date
                );
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_unfinished_payroll_period()
        RETURNS trigger AS $$
        DECLARE current_batch payroll_batches%ROWTYPE;
        BEGIN
            SELECT * INTO current_batch FROM payroll_batches WHERE id = NEW.id;
            IF FOUND AND current_batch.status IN ('draft','calculated') THEN
                PERFORM finance_assert_accounting_write_period(
                    current_batch.org_id, current_batch.posting_date
                );
            ELSIF TG_OP = 'UPDATE'
               AND OLD.status IN ('draft','calculated')
               AND NEW.status = 'superseded' THEN
                PERFORM finance_assert_accounting_write_period(OLD.org_id, OLD.posting_date);
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE CONSTRAINT TRIGGER draft_business_event_period_invariant_deferred
        AFTER INSERT OR UPDATE ON business_events
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_draft_business_event_period();
        CREATE CONSTRAINT TRIGGER draft_voucher_period_invariant_deferred
        AFTER INSERT OR UPDATE ON vouchers
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_draft_voucher_period();
        CREATE CONSTRAINT TRIGGER unfinished_payroll_period_invariant_deferred
        AFTER INSERT OR UPDATE ON payroll_batches
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_unfinished_payroll_period();

        CREATE OR REPLACE FUNCTION finance_guard_accounting_period_org_mutation()
        RETURNS trigger AS $$
        BEGIN
            IF OLD.accounting_period_control_enabled IS TRUE
               AND NEW.accounting_period_control_enabled IS FALSE THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_CONTROL_ALREADY_ENABLED';
            END IF;
            IF OLD.accounting_period_control_start_date IS NOT NULL
               AND NEW.accounting_period_control_start_date IS DISTINCT FROM
                   OLD.accounting_period_control_start_date THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_guard_accounting_period_mutation()
        RETURNS trigger AS $$
        DECLARE previous_end date;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
            ELSIF TG_OP = 'INSERT' THEN
                PERFORM finance_lock_accounting_period_generation_org(NEW.org_id);
                PERFORM finance_lock_accounting_month(NEW.org_id, NEW.start_date);
                IF NEW.start_date > date_trunc(
                    'month', (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Shanghai')::date
                )::date THEN
                    RAISE EXCEPTION 'ACCOUNTING_PERIOD_FUTURE_GENERATION_NOT_ALLOWED';
                END IF;
                SELECT max(end_date) INTO previous_end FROM accounting_periods
                 WHERE org_id = NEW.org_id;
                IF previous_end IS NULL THEN
                    IF EXISTS (
                        SELECT 1 FROM business_events
                         WHERE org_id = NEW.org_id AND status IN ('posted','reversed')
                    ) OR EXISTS (
                        SELECT 1 FROM vouchers
                         WHERE org_id = NEW.org_id AND status IN ('posted','reversed')
                    ) THEN
                        RAISE EXCEPTION 'ACCOUNTING_PERIOD_LEGACY_DATA_REQUIRES_MIGRATION';
                    END IF;
                ELSIF NEW.start_date <> previous_end + 1 THEN
                    RAISE EXCEPTION 'ACCOUNTING_PERIOD_GENERATION_OUT_OF_SEQUENCE';
                END IF;
                RETURN NEW;
            END IF;
            PERFORM finance_lock_accounting_month(OLD.org_id, OLD.start_date);
            IF OLD.status = 'open' AND NEW.status = 'closed'
               AND (to_jsonb(OLD) - 'status' - 'closed_at' - 'close_id') =
                   (to_jsonb(NEW) - 'status' - 'closed_at' - 'close_id')
               AND NEW.closed_at IS NOT NULL AND NEW.close_id IS NOT NULL THEN
                RETURN NEW;
            END IF;
            IF OLD.status = 'closed' AND NEW.status = 'open' THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_REOPEN_NOT_SUPPORTED';
            END IF;
            RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_block_accounting_period_immutable()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN RETURN NEW; END IF;
            RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_guard_accounting_period_close_insert()
        RETURNS trigger AS $$
        DECLARE target_period accounting_periods%ROWTYPE;
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;
            SELECT * INTO target_period FROM accounting_periods
             WHERE id = NEW.period_id AND org_id = NEW.org_id;
            IF NOT FOUND THEN RETURN NEW; END IF;
            PERFORM finance_lock_accounting_month(NEW.org_id, target_period.start_date);
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_guard_accounting_period_close_source_insert()
        RETURNS trigger AS $$
        DECLARE current_status varchar;
        BEGIN
            SELECT event.status INTO current_status
              FROM vouchers AS voucher
              JOIN business_events AS event
                ON event.org_id = voucher.org_id AND event.id = voucher.event_id
             WHERE voucher.org_id = NEW.org_id AND voucher.id = NEW.voucher_id
               AND event.id = NEW.event_id;
            IF current_status IS NULL OR current_status <> NEW.event_status_at_close THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER accounting_period_org_immutable
        BEFORE UPDATE ON organizations
        FOR EACH ROW EXECUTE FUNCTION finance_guard_accounting_period_org_mutation();
        CREATE TRIGGER accounting_period_single_direction
        BEFORE INSERT OR UPDATE OR DELETE ON accounting_periods
        FOR EACH ROW EXECUTE FUNCTION finance_guard_accounting_period_mutation();
        CREATE TRIGGER accounting_period_calendar_immutable
        BEFORE UPDATE OR DELETE ON accounting_period_calendars
        FOR EACH ROW EXECUTE FUNCTION finance_block_accounting_period_immutable();
        CREATE TRIGGER accounting_period_action_immutable
        BEFORE UPDATE OR DELETE ON accounting_period_actions
        FOR EACH ROW EXECUTE FUNCTION finance_block_accounting_period_immutable();
        CREATE TRIGGER accounting_period_close_immutable
        BEFORE INSERT OR UPDATE OR DELETE ON accounting_period_closes
        FOR EACH ROW EXECUTE FUNCTION finance_guard_accounting_period_close_insert();
        CREATE TRIGGER accounting_period_close_source_immutable
        BEFORE UPDATE OR DELETE ON accounting_period_close_sources
        FOR EACH ROW EXECUTE FUNCTION finance_block_accounting_period_immutable();
        CREATE TRIGGER accounting_period_close_source_insert_guard
        BEFORE INSERT ON accounting_period_close_sources
        FOR EACH ROW EXECUTE FUNCTION finance_guard_accounting_period_close_source_insert();
        CREATE TRIGGER accounting_period_action_evidence_immutable
        BEFORE UPDATE OR DELETE ON accounting_period_action_evidence
        FOR EACH ROW EXECUTE FUNCTION finance_block_accounting_period_immutable();
        CREATE TRIGGER accounting_period_dependency_migration_action_immutable
        BEFORE INSERT OR UPDATE OR DELETE ON accounting_period_dependency_migration_actions
        FOR EACH ROW EXECUTE FUNCTION finance_block_accounting_period_immutable();
        """
    )
    _install_postgresql_period_assertions()
    _install_postgresql_dependency_assertions()
    _install_postgresql_tax_posting_date_assertions()


def _install_postgresql_period_assertions() -> None:
    op.execute(
        r"""
        CREATE OR REPLACE FUNCTION finance_assert_accounting_period_action(
            target_action_id uuid
        ) RETURNS void AS $$
        DECLARE target_action accounting_period_actions%ROWTYPE;
        DECLARE linked_count bigint;
        DECLARE command_name text;
        DECLARE expected_request_hash text;
        DECLARE invalid_evidence boolean;
        BEGIN
            SELECT * INTO target_action FROM accounting_period_actions
             WHERE id = target_action_id;
            IF NOT FOUND THEN RETURN; END IF;
            IF jsonb_typeof(target_action.input_facts::jsonb) <> 'object'
               OR jsonb_typeof(target_action.missing_information::jsonb) <> 'array'
               OR jsonb_typeof(target_action.errors::jsonb) <> 'array' THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;
            IF target_action.status = 'posted' THEN
                command_name := CASE target_action.action_type
                    WHEN 'period_generation' THEN 'finance_generate_accounting_period'
                    ELSE 'finance_confirm_accounting_period_close'
                END;
                expected_request_hash := encode(digest(convert_to(
                    finance_canonical_jsonb(jsonb_build_object(
                        'command', command_name,
                        'request', target_action.input_facts::jsonb
                    )), 'UTF8'
                ), 'sha256'), 'hex');
                IF target_action.idempotency_key IS NULL
                   OR length(trim(target_action.idempotency_key)) = 0
                   OR target_action.request_payload_hash !~ '^[0-9a-f]{64}$'
                   OR target_action.confirmed_by IS NULL
                   OR length(trim(target_action.confirmed_by)) = 0
                   OR length(target_action.confirmed_by) > 100
                   OR target_action.confirmation_note IS NULL
                   OR length(trim(target_action.confirmation_note)) = 0
                   OR length(target_action.confirmation_note) > 2000
                   OR target_action.input_facts::jsonb = '{}'::jsonb
                   OR target_action.request_payload_hash <> expected_request_hash
                   OR target_action.input_facts::jsonb ->> 'org_id' <>
                      target_action.org_id::text
                   OR target_action.input_facts::jsonb ->> 'idempotency_key' <>
                      target_action.idempotency_key
                   OR target_action.input_facts::jsonb ->> 'confirmed_by' <>
                      target_action.confirmed_by
                   OR target_action.input_facts::jsonb ->> 'confirmation_note' <>
                      target_action.confirmation_note
                   OR target_action.missing_information::jsonb <> '[]'::jsonb
                   OR target_action.errors::jsonb <> '[]'::jsonb
                   OR jsonb_array_length(
                        target_action.input_facts::jsonb -> 'evidence_references'
                      ) <> (SELECT count(DISTINCT value)
                              FROM jsonb_array_elements_text(
                                  target_action.input_facts::jsonb
                                  -> 'evidence_references'
                              ) AS value)
                   OR NOT EXISTS (
                       SELECT 1 FROM accounting_period_action_evidence AS evidence
                        WHERE evidence.org_id = target_action.org_id
                          AND evidence.action_id = target_action.id
                   ) THEN
                    RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
                END IF;
                SELECT EXISTS (
                    (SELECT value::uuid
                       FROM jsonb_array_elements_text(
                           target_action.input_facts::jsonb -> 'evidence_references'
                       ) AS value
                     EXCEPT
                     SELECT evidence.evidence_id
                       FROM accounting_period_action_evidence AS evidence
                      WHERE evidence.org_id = target_action.org_id
                        AND evidence.action_id = target_action.id)
                    UNION ALL
                    (SELECT evidence.evidence_id
                       FROM accounting_period_action_evidence AS evidence
                      WHERE evidence.org_id = target_action.org_id
                        AND evidence.action_id = target_action.id
                     EXCEPT
                     SELECT value::uuid
                       FROM jsonb_array_elements_text(
                           target_action.input_facts::jsonb -> 'evidence_references'
                       ) AS value)
                ) INTO invalid_evidence;
                IF jsonb_typeof(
                    target_action.input_facts::jsonb -> 'evidence_references'
                   ) <> 'array' OR invalid_evidence THEN
                    RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
                END IF;
                IF target_action.action_type = 'period_generation' THEN
                    IF (SELECT array_agg(key ORDER BY key)
                          FROM jsonb_object_keys(target_action.input_facts::jsonb) AS key)
                       <> ARRAY['confirmation_note','confirmed_by','evidence_references',
                                'idempotency_key','org_id','period_month'] THEN
                        RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
                    END IF;
                    SELECT count(*) INTO linked_count FROM accounting_periods
                     WHERE org_id = target_action.org_id
                       AND generation_action_id = target_action.id;
                ELSE
                    IF (SELECT array_agg(key ORDER BY key)
                          FROM jsonb_object_keys(target_action.input_facts::jsonb) AS key)
                       <> ARRAY['calculation_hash','closing_date','confirmation_note',
                                'confirmed_by','evidence_references','idempotency_key',
                                'org_id','period_id','review_facts']
                       OR (SELECT array_agg(key ORDER BY key)
                             FROM jsonb_object_keys(
                                 target_action.input_facts::jsonb -> 'review_facts'
                             ) AS key)
                          <> ARRAY['asset_and_borrowing_schedules_reviewed',
                                   'bank_reconciliation_reviewed','open_items_reviewed',
                                   'payroll_and_statutory_items_reviewed','tax_items_reviewed',
                                   'voucher_completeness_reviewed'] THEN
                        RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
                    END IF;
                    SELECT count(*) INTO linked_count FROM accounting_period_closes
                     WHERE org_id = target_action.org_id AND action_id = target_action.id;
                END IF;
                IF linked_count <> 1 THEN
                    RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
                END IF;
                IF target_action.action_type = 'period_close' AND (
                    target_action.input_facts::jsonb #>>
                        '{review_facts,voucher_completeness_reviewed}' <> 'true'
                    OR target_action.input_facts::jsonb #>>
                        '{review_facts,bank_reconciliation_reviewed}' <> 'true'
                    OR target_action.input_facts::jsonb #>>
                        '{review_facts,open_items_reviewed}' <> 'true'
                    OR target_action.input_facts::jsonb #>>
                        '{review_facts,payroll_and_statutory_items_reviewed}' <> 'true'
                    OR target_action.input_facts::jsonb #>>
                        '{review_facts,tax_items_reviewed}' <> 'true'
                    OR target_action.input_facts::jsonb #>>
                        '{review_facts,asset_and_borrowing_schedules_reviewed}' <> 'true'
                ) THEN
                    RAISE EXCEPTION 'ACCOUNTING_PERIOD_REVIEW_INCOMPLETE';
                END IF;
            ELSE
                IF target_action.request_payload_hash IS NULL
                   OR target_action.request_payload_hash !~ '^[0-9a-f]{64}$'
                   OR target_action.input_facts::jsonb <> '{}'::jsonb
                   OR target_action.confirmed_by IS NOT NULL
                   OR target_action.confirmation_note IS NOT NULL
                   OR EXISTS (
                       SELECT 1 FROM accounting_period_action_evidence AS evidence
                        WHERE evidence.org_id = target_action.org_id
                          AND evidence.action_id = target_action.id
                   ) OR EXISTS (
                       SELECT 1 FROM jsonb_array_elements(
                           target_action.missing_information::jsonb
                       ) AS item WHERE jsonb_typeof(item) <> 'string'
                          OR item #>> '{}' NOT IN (
                              'idempotency_key','confirmed_by','confirmation_note',
                              'evidence_references','calculation_hash',
                              'review_facts.voucher_completeness_reviewed',
                              'review_facts.bank_reconciliation_reviewed',
                              'review_facts.open_items_reviewed',
                              'review_facts.payroll_and_statutory_items_reviewed',
                              'review_facts.tax_items_reviewed',
                              'review_facts.asset_and_borrowing_schedules_reviewed'
                          )
                   ) OR EXISTS (
                       SELECT 1 FROM jsonb_array_elements(
                           target_action.errors::jsonb
                       ) AS item
                        WHERE jsonb_typeof(item) <> 'object'
                           OR (SELECT array_agg(key ORDER BY key)
                                 FROM jsonb_object_keys(item) AS key)
                              <> ARRAY['code','field_paths']
                           OR jsonb_typeof(item -> 'code') <> 'string'
                           OR item ->> 'code' !~ '^ACCOUNTING_PERIOD_[A-Z0-9_]+$'
                           OR jsonb_typeof(item -> 'field_paths') <> 'array'
                           OR EXISTS (
                               SELECT 1 FROM jsonb_array_elements(item -> 'field_paths') AS path
                                WHERE jsonb_typeof(path) <> 'string'
                                   OR path #>> '{}' NOT IN (
                                      'idempotency_key','confirmed_by','confirmation_note',
                                      'evidence_references','calculation_hash',
                                      'review_facts.voucher_completeness_reviewed',
                                      'review_facts.bank_reconciliation_reviewed',
                                      'review_facts.open_items_reviewed',
                                      'review_facts.payroll_and_statutory_items_reviewed',
                                      'review_facts.tax_items_reviewed',
                                      'review_facts.asset_and_borrowing_schedules_reviewed'
                                   )
                           )
                   ) OR (target_action.missing_information::jsonb = '[]'::jsonb
                         AND target_action.errors::jsonb = '[]'::jsonb)
                   OR EXISTS (
                       SELECT 1 FROM accounting_periods
                        WHERE generation_action_id = target_action.id
                   ) OR EXISTS (
                       SELECT 1 FROM accounting_period_closes
                        WHERE action_id = target_action.id
                   ) THEN
                    RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
                END IF;
            END IF;
        EXCEPTION WHEN invalid_text_representation THEN
            RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_assert_accounting_period_org(
            target_org_id uuid
        ) RETURNS void AS $$
        DECLARE target_org organizations%ROWTYPE;
        DECLARE first_start date;
        DECLARE last_end date;
        DECLARE period_count bigint;
        DECLARE expected_count bigint;
        DECLARE invalid_period boolean;
        BEGIN
            SELECT * INTO target_org FROM organizations WHERE id = target_org_id;
            IF NOT FOUND THEN RETURN; END IF;
            SELECT min(start_date), max(end_date), count(*)
              INTO first_start, last_end, period_count
              FROM accounting_periods WHERE org_id = target_org_id;
            IF target_org.accounting_period_control_enabled IS FALSE THEN
                IF target_org.accounting_period_control_start_date IS NOT NULL
                   OR period_count <> 0
                   OR EXISTS (SELECT 1 FROM accounting_period_calendars
                               WHERE org_id = target_org_id) THEN
                    RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
                END IF;
                RETURN;
            END IF;
            IF period_count = 0 THEN
                IF target_org.accounting_period_control_start_date IS NOT NULL
                   OR EXISTS (SELECT 1 FROM accounting_period_calendars
                               WHERE org_id = target_org_id) THEN
                    RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
                END IF;
                RETURN;
            END IF;
            IF target_org.accounting_period_control_start_date IS DISTINCT FROM first_start THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;
            expected_count := (
                extract(year FROM age(last_end + 1, first_start))::integer * 12
                + extract(month FROM age(last_end + 1, first_start))::integer
            );
            IF period_count <> expected_count THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_GENERATION_OUT_OF_SEQUENCE';
            END IF;
            SELECT EXISTS (
                SELECT 1
                  FROM accounting_periods AS period
                  LEFT JOIN accounting_period_calendars AS calendar
                    ON calendar.org_id = period.org_id AND calendar.id = period.calendar_id
                  LEFT JOIN accounting_period_actions AS action
                    ON action.org_id = period.org_id
                   AND action.id = period.generation_action_id
                 WHERE period.org_id = target_org_id
                   AND (
                       period.start_date <> make_date(
                           period.calendar_year, period.calendar_month, 1
                       )
                       OR period.end_date <> (
                           make_date(period.calendar_year, period.calendar_month, 1)
                           + interval '1 month - 1 day'
                       )::date
                       OR calendar.id IS NULL
                       OR calendar.calendar_year <> period.calendar_year
                       OR action.id IS NULL
                       OR action.action_type <> 'period_generation'
                       OR action.status <> 'posted'
                       OR action.input_facts::jsonb ->> 'period_month' <>
                          to_char(period.start_date, 'YYYY-MM')
                   )
            ) INTO invalid_period;
            IF invalid_period THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;
            IF EXISTS (
                SELECT 1 FROM accounting_period_calendars AS calendar
                 WHERE calendar.org_id = target_org_id
                   AND (
                       calendar.rule_version <> 'cn_accounting_period_close_2026.1'
                       OR calendar.rule_effective_from <> DATE '2026-08-11'
                       OR calendar.source_urls::jsonb <> jsonb_build_array(
                            'https://kjs.mof.gov.cn/zt/kjfxcgc/kjfqw/202408/t20240814_3941788.htm',
                            'https://xzfg.moj.gov.cn/front/law/detail?LawID=722',
                            'https://www.mof.gov.cn/gp/xxgkml/tfs/201903/t20190318_3195239.htm',
                            'https://kjs.mof.gov.cn/zhengcefabu/202408/P020240805628932632907.pdf',
                            'https://kjs.mof.gov.cn/zhengcefabu/202408/P020240805635126967297.pdf'
                       )
                       OR NOT EXISTS (
                           SELECT 1 FROM accounting_periods AS period
                            WHERE period.org_id = calendar.org_id
                              AND period.calendar_id = calendar.id
                       )
                   )
            ) THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_assert_accounting_period_close(
            target_close_id uuid
        ) RETURNS void AS $$
        DECLARE target_close accounting_period_closes%ROWTYPE;
        DECLARE target_period accounting_periods%ROWTYPE;
        DECLARE target_action accounting_period_actions%ROWTYPE;
        DECLARE expected_previous_hash varchar;
        DECLARE expected_sources jsonb;
        DECLARE expected_account_totals jsonb;
        DECLARE expected_voucher_count bigint;
        DECLARE expected_line_count bigint;
        DECLARE expected_debit numeric;
        DECLARE expected_credit numeric;
        DECLARE invalid_source boolean;
        DECLARE draft_voucher_count bigint;
        DECLARE draft_event_count bigint;
        DECLARE fixed_missing bigint;
        DECLARE intangible_missing bigint;
        DECLARE borrowing_missing bigint;
        DECLARE unfinished_payroll bigint;
        DECLARE open_item_count bigint;
        DECLARE unmatched_bank_count bigint;
        DECLARE tax_item_count bigint;
        DECLARE expected_system_checks jsonb;
        DECLARE expected_module_checks jsonb;
        DECLARE expected_review_counts jsonb;
        DECLARE expected_warnings jsonb;
        BEGIN
            SELECT * INTO target_close FROM accounting_period_closes
             WHERE id = target_close_id;
            IF NOT FOUND THEN RETURN; END IF;
            SELECT * INTO target_period FROM accounting_periods
             WHERE id = target_close.period_id AND org_id = target_close.org_id;
            SELECT * INTO target_action FROM accounting_period_actions
             WHERE id = target_close.action_id AND org_id = target_close.org_id;
            IF target_period.id IS NULL OR target_action.id IS NULL
               OR target_period.status <> 'closed'
               OR target_period.close_id <> target_close.id
               OR target_period.closed_at IS DISTINCT FROM target_close.confirmed_at
               OR target_action.action_type <> 'period_close'
               OR target_action.status <> 'posted'
               OR target_action.input_facts::jsonb ->> 'period_id' <>
                  target_period.id::text
               OR target_action.input_facts::jsonb ->> 'closing_date' <>
                  target_period.end_date::text
               OR target_action.input_facts::jsonb ->> 'calculation_hash' <>
                  target_close.calculation_hash
               OR target_close.rule_version <> 'cn_accounting_period_close_2026.1'
               OR target_close.rule_effective_from <> DATE '2026-08-11'
               OR target_close.checker_version <>
                  'accounting_period_close_checker_2026.1'
               OR target_close.source_urls::jsonb <> jsonb_build_array(
                    'https://kjs.mof.gov.cn/zt/kjfxcgc/kjfqw/202408/t20240814_3941788.htm',
                    'https://xzfg.moj.gov.cn/front/law/detail?LawID=722',
                    'https://www.mof.gov.cn/gp/xxgkml/tfs/201903/t20190318_3195239.htm',
                    'https://kjs.mof.gov.cn/zhengcefabu/202408/P020240805628932632907.pdf',
                    'https://kjs.mof.gov.cn/zhengcefabu/202408/P020240805635126967297.pdf'
               ) THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;
            SELECT close.calculation_hash INTO expected_previous_hash
              FROM accounting_periods AS period
              JOIN accounting_period_closes AS close
                ON close.org_id = period.org_id AND close.id = period.close_id
             WHERE period.org_id = target_period.org_id
               AND period.end_date < target_period.start_date
             ORDER BY period.end_date DESC LIMIT 1;
            IF target_close.previous_close_hash IS DISTINCT FROM expected_previous_hash THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;
            IF EXISTS (
                SELECT 1 FROM accounting_periods AS earlier
                 WHERE earlier.org_id = target_period.org_id
                   AND earlier.start_date < target_period.start_date
                   AND earlier.status <> 'closed'
            ) THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_CLOSE_OUT_OF_SEQUENCE';
            END IF;
            IF target_period.end_date >
               (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Shanghai')::date THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_FUTURE_CLOSE_NOT_ALLOWED';
            END IF;

            SELECT count(*) INTO draft_voucher_count FROM vouchers
             WHERE org_id = target_period.org_id AND status = 'draft'
               AND posting_date BETWEEN target_period.start_date AND target_period.end_date;
            SELECT count(*) INTO draft_event_count FROM business_events
             WHERE org_id = target_period.org_id AND status = 'draft'
               AND posting_date BETWEEN target_period.start_date AND target_period.end_date;
            IF draft_voucher_count <> 0 OR draft_event_count <> 0 OR EXISTS (
                SELECT 1 FROM business_events AS event
                 WHERE event.org_id = target_period.org_id
                   AND event.status IN ('posted','reversed')
                   AND event.posting_date BETWEEN
                       target_period.start_date AND target_period.end_date
                   AND (SELECT count(*) FROM vouchers AS voucher
                         WHERE voucher.org_id = event.org_id
                           AND voucher.event_id = event.id
                           AND voucher.status IN ('posted','reversed')) <> 1
            ) THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_CLOSE_BLOCKED';
            END IF;

            PERFORM finance_assert_fixed_asset(asset.id)
              FROM fixed_assets AS asset
             WHERE asset.org_id = target_period.org_id;
            PERFORM finance_assert_intangible_asset(asset.id)
              FROM intangible_assets AS asset
             WHERE asset.org_id = target_period.org_id;

            SELECT count(*) INTO fixed_missing
              FROM fixed_asset_activations AS activation
              JOIN business_events AS activation_event
                ON activation_event.org_id = activation.org_id
               AND activation_event.id = activation.event_id
             WHERE activation.org_id = target_period.org_id
               AND activation_event.status = 'posted'
               AND activation.in_service_date <= target_period.end_date
               AND (date_trunc('month', activation.in_service_date)::date
                    + interval '1 month')::date <= target_period.start_date
               AND (EXISTS (
                   SELECT 1
                     FROM generate_series(
                         (date_trunc('month', activation.in_service_date)::date
                          + interval '1 month')::date,
                         LEAST(
                             target_period.start_date,
                             (date_trunc('month', activation.in_service_date)::date
                              + activation.useful_life_months * interval '1 month')::date,
                             COALESCE((
                                 SELECT min(date_trunc('month', disposal.disposal_date)::date)
                                   FROM fixed_asset_disposals AS disposal
                                   JOIN business_events AS disposal_event
                                     ON disposal_event.org_id = disposal.org_id
                                    AND disposal_event.id = disposal.event_id
                                  WHERE disposal.org_id = activation.org_id
                                    AND disposal.activation_id = activation.id
                                    AND disposal.disposal_date <= target_period.end_date
                                    AND disposal_event.status = 'posted'
                             ), target_period.start_date)
                         ),
                         interval '1 month'
                     ) WITH ORDINALITY AS expected(period_start, sequence_no)
                    WHERE (SELECT count(*)
                             FROM fixed_asset_depreciations AS depreciation
                             JOIN business_events AS depreciation_event
                               ON depreciation_event.org_id = depreciation.org_id
                              AND depreciation_event.id = depreciation.event_id
                            WHERE depreciation.org_id = activation.org_id
                              AND depreciation.activation_id = activation.id
                              AND depreciation.period_start = expected.period_start::date
                              AND depreciation.sequence_no = expected.sequence_no
                              AND depreciation_event.status = 'posted') <> 1
               ) OR EXISTS (
                   SELECT 1
                     FROM fixed_asset_depreciations AS depreciation
                     JOIN business_events AS depreciation_event
                       ON depreciation_event.org_id = depreciation.org_id
                      AND depreciation_event.id = depreciation.event_id
                    WHERE depreciation.org_id = activation.org_id
                      AND depreciation.activation_id = activation.id
                      AND depreciation.period_start <= target_period.end_date
                      AND depreciation_event.status = 'posted'
                      AND NOT EXISTS (
                          SELECT 1
                            FROM generate_series(
                                (date_trunc('month', activation.in_service_date)::date
                                 + interval '1 month')::date,
                                LEAST(
                                    target_period.start_date,
                                    (date_trunc('month', activation.in_service_date)::date
                                     + activation.useful_life_months * interval '1 month')::date,
                                    COALESCE((
                                        SELECT min(date_trunc('month', disposal.disposal_date)::date)
                                          FROM fixed_asset_disposals AS disposal
                                          JOIN business_events AS disposal_event
                                            ON disposal_event.org_id = disposal.org_id
                                           AND disposal_event.id = disposal.event_id
                                         WHERE disposal.org_id = activation.org_id
                                           AND disposal.activation_id = activation.id
                                           AND disposal.disposal_date <= target_period.end_date
                                           AND disposal_event.status = 'posted'
                                    ), target_period.start_date)
                                ), interval '1 month'
                            ) WITH ORDINALITY AS expected(period_start, sequence_no)
                           WHERE expected.period_start::date = depreciation.period_start
                             AND expected.sequence_no = depreciation.sequence_no
                      )
               ));

            SELECT count(*) INTO intangible_missing
              FROM intangible_assets AS asset
              JOIN business_events AS acquisition_event
                ON acquisition_event.org_id = asset.org_id
               AND acquisition_event.id = asset.acquisition_event_id
             WHERE asset.org_id = target_period.org_id
               AND asset.is_available_for_use IS TRUE
               AND asset.available_for_use_date <= target_period.end_date
               AND acquisition_event.status = 'posted'
               AND date_trunc('month', asset.available_for_use_date)::date
                   <= target_period.start_date
               AND (EXISTS (
                   SELECT 1
                     FROM generate_series(
                         date_trunc('month', asset.available_for_use_date)::date,
                         LEAST(
                             target_period.start_date,
                             (date_trunc('month', asset.available_for_use_date)::date
                              + (asset.useful_life_months - 1) * interval '1 month')::date,
                             COALESCE((
                                 SELECT min(date_trunc('month', retirement.retirement_date)::date)
                                   FROM intangible_asset_retirements AS retirement
                                   JOIN business_events AS retirement_event
                                     ON retirement_event.org_id = retirement.org_id
                                    AND retirement_event.id = retirement.event_id
                                  WHERE retirement.org_id = asset.org_id
                                    AND retirement.asset_id = asset.id
                                    AND retirement.retirement_date <= target_period.end_date
                                    AND retirement_event.status = 'posted'
                             ), target_period.start_date)
                         ),
                         interval '1 month'
                     ) WITH ORDINALITY AS expected(period_start, sequence_no)
                    WHERE (SELECT count(*)
                             FROM intangible_asset_amortizations AS amortization
                             JOIN business_events AS amortization_event
                               ON amortization_event.org_id = amortization.org_id
                              AND amortization_event.id = amortization.event_id
                            WHERE amortization.org_id = asset.org_id
                              AND amortization.asset_id = asset.id
                              AND amortization.period_start = expected.period_start::date
                              AND amortization.sequence_no = expected.sequence_no
                              AND amortization_event.status = 'posted') <> 1
               ) OR EXISTS (
                   SELECT 1
                     FROM intangible_asset_amortizations AS amortization
                     JOIN business_events AS amortization_event
                       ON amortization_event.org_id = amortization.org_id
                      AND amortization_event.id = amortization.event_id
                    WHERE amortization.org_id = asset.org_id
                      AND amortization.asset_id = asset.id
                      AND amortization.period_start <= target_period.end_date
                      AND amortization_event.status = 'posted'
                      AND NOT EXISTS (
                          SELECT 1
                            FROM generate_series(
                                date_trunc('month', asset.available_for_use_date)::date,
                                LEAST(
                                    target_period.start_date,
                                    (date_trunc('month', asset.available_for_use_date)::date
                                     + (asset.useful_life_months - 1) * interval '1 month')::date,
                                    COALESCE((
                                        SELECT min(date_trunc('month', retirement.retirement_date)::date)
                                          FROM intangible_asset_retirements AS retirement
                                          JOIN business_events AS retirement_event
                                            ON retirement_event.org_id = retirement.org_id
                                           AND retirement_event.id = retirement.event_id
                                         WHERE retirement.org_id = asset.org_id
                                           AND retirement.asset_id = asset.id
                                           AND retirement.retirement_date <= target_period.end_date
                                           AND retirement_event.status = 'posted'
                                    ), target_period.start_date)
                                ), interval '1 month'
                            ) WITH ORDINALITY AS expected(period_start, sequence_no)
                           WHERE expected.period_start::date = amortization.period_start
                             AND expected.sequence_no = amortization.sequence_no
                      )
               ));

            SELECT count(*) INTO borrowing_missing
              FROM borrowings AS borrowing
              JOIN business_events AS draw_event
                ON draw_event.org_id = borrowing.org_id
               AND draw_event.id = borrowing.drawdown_event_id
             WHERE borrowing.org_id = target_period.org_id
               AND borrowing.drawdown_date <= target_period.end_date
               AND draw_event.status = 'posted'
               AND EXISTS (
                   SELECT 1
                     FROM jsonb_array_elements_text(
                         borrowing.interest_due_dates::jsonb
                     ) WITH ORDINALITY AS due(value, sequence_no)
                    WHERE due.value::date <= target_period.end_date
                      AND (SELECT count(*)
                             FROM borrowing_interest_accruals AS accrual
                             JOIN business_events AS accrual_event
                               ON accrual_event.org_id = accrual.org_id
                              AND accrual_event.id = accrual.event_id
                            WHERE accrual.org_id = borrowing.org_id
                              AND accrual.borrowing_id = borrowing.id
                              AND accrual.period_end = due.value::date
                              AND accrual.sequence_no = due.sequence_no
                              AND accrual_event.status = 'posted') <> 1
               );

            SELECT count(*) INTO unfinished_payroll FROM payroll_batches
             WHERE org_id = target_period.org_id
               AND payroll_period = to_char(target_period.start_date, 'YYYY-MM')
               AND status NOT IN ('posted','reversed','superseded');
            IF fixed_missing <> 0 OR intangible_missing <> 0
               OR borrowing_missing <> 0 OR unfinished_payroll <> 0 THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_CLOSE_BLOCKED';
            END IF;

            SELECT count(*) INTO open_item_count FROM open_items
             WHERE org_id = target_period.org_id AND status IN ('open','partial');
            SELECT count(*) INTO unmatched_bank_count FROM bank_transactions
             WHERE org_id = target_period.org_id
               AND booking_date <= target_period.end_date AND matched_event_id IS NULL;
            SELECT count(*) INTO tax_item_count FROM business_events
             WHERE org_id = target_period.org_id AND status = 'posted'
               AND tax_obligation_date BETWEEN
                   target_period.start_date AND target_period.end_date;
            expected_system_checks := jsonb_build_array(
                jsonb_build_object('code','ACCOUNTING_PERIOD_CLOSE_SEQUENCE',
                                   'passed',true,'count',0),
                jsonb_build_object('code','ACCOUNTING_PERIOD_NO_DRAFT_EVENTS',
                                   'passed',true,'count',0),
                jsonb_build_object('code','ACCOUNTING_PERIOD_NO_DRAFT_VOUCHERS',
                                   'passed',true,'count',0),
                jsonb_build_object('code','ACCOUNTING_PERIOD_OPEN',
                                   'passed',true,'count',0),
                jsonb_build_object('code','ACCOUNTING_PERIOD_VOUCHER_INTEGRITY',
                                   'passed',true,'count',0)
            );
            expected_module_checks := jsonb_build_object(
                'borrowings', jsonb_build_object(
                    'code','ACCOUNTING_PERIOD_BORROWING_INTEREST_PENDING',
                    'count',borrowing_missing,'blocking',false),
                'fixed_assets', jsonb_build_object(
                    'code','ACCOUNTING_PERIOD_FIXED_ASSET_DEPRECIATION_PENDING',
                    'count',fixed_missing,'blocking',false),
                'intangible_assets', jsonb_build_object(
                    'code','ACCOUNTING_PERIOD_INTANGIBLE_AMORTIZATION_PENDING',
                    'count',intangible_missing,'blocking',false),
                'payroll', jsonb_build_object(
                    'code','ACCOUNTING_PERIOD_PAYROLL_PENDING',
                    'count',unfinished_payroll,'blocking',false)
            );
            expected_review_counts := jsonb_build_object(
                'open_items',open_item_count,
                'tax_items_to_review',tax_item_count,
                'unmatched_bank_transactions',unmatched_bank_count
            );
            expected_warnings := jsonb_build_array(
                jsonb_build_object('code','ACCOUNTING_PERIOD_OPEN_ITEMS_REVIEW',
                                   'count',open_item_count),
                jsonb_build_object('code','ACCOUNTING_PERIOD_TAX_REVIEW',
                                   'count',tax_item_count),
                jsonb_build_object('code','ACCOUNTING_PERIOD_UNMATCHED_BANK_REVIEW',
                                   'count',unmatched_bank_count)
            );

            SELECT COALESCE(jsonb_agg(
                jsonb_build_object(
                    'id', source.voucher_id,
                    'voucher_number', source.voucher_number,
                    'posting_date', source.posting_date::text,
                    'description', source.description,
                    'event_id', source.event_id,
                    'event_type', source.event_type,
                    'event_status_at_close', source.event_status_at_close,
                    'request_payload_hash_at_close', source.request_payload_hash_at_close,
                    'debit_fen', source.debit_fen,
                    'credit_fen', source.credit_fen,
                    'line_snapshot', source.line_snapshot::jsonb
                ) ORDER BY source.posting_date, source.voucher_id
            ), '[]'::jsonb) INTO expected_sources
              FROM accounting_period_close_sources AS source
             WHERE source.org_id = target_close.org_id
               AND source.close_id = target_close.id;

            SELECT COALESCE(jsonb_agg(
                jsonb_build_object(
                    'id', totals.account_id,
                    'account_code', totals.account_code,
                    'debit_fen', totals.debit_fen,
                    'credit_fen', totals.credit_fen,
                    'net_fen', totals.debit_fen - totals.credit_fen
                ) ORDER BY totals.account_code, totals.account_id
            ), '[]'::jsonb) INTO expected_account_totals
              FROM (
                SELECT account.id AS account_id, account.code AS account_code,
                       sum(line.debit_fen)::bigint AS debit_fen,
                       sum(line.credit_fen)::bigint AS credit_fen
                  FROM vouchers AS voucher
                  JOIN voucher_lines AS line
                    ON line.org_id = voucher.org_id AND line.voucher_id = voucher.id
                  JOIN accounts AS account
                    ON account.org_id = line.org_id AND account.id = line.account_id
                 WHERE voucher.org_id = target_period.org_id
                   AND voucher.status IN ('posted','reversed')
                   AND voucher.posting_date BETWEEN
                       target_period.start_date AND target_period.end_date
                 GROUP BY account.id, account.code
              ) AS totals;

            SELECT count(DISTINCT voucher.id), count(line.id),
                   COALESCE(sum(line.debit_fen), 0), COALESCE(sum(line.credit_fen), 0)
              INTO expected_voucher_count, expected_line_count,
                   expected_debit, expected_credit
              FROM vouchers AS voucher
              LEFT JOIN voucher_lines AS line
                ON line.org_id = voucher.org_id AND line.voucher_id = voucher.id
             WHERE voucher.org_id = target_period.org_id
               AND voucher.status IN ('posted','reversed')
               AND voucher.posting_date BETWEEN
                   target_period.start_date AND target_period.end_date;
            IF target_close.voucher_count <> expected_voucher_count
               OR target_close.line_count <> expected_line_count
               OR target_close.total_debit_fen <> expected_debit
               OR target_close.total_credit_fen <> expected_credit
               OR target_close.total_debit_fen <> target_close.total_credit_fen
               OR (SELECT count(*) FROM accounting_period_close_sources
                    WHERE org_id = target_close.org_id AND close_id = target_close.id)
                  <> expected_voucher_count THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;

            SELECT EXISTS (
                SELECT 1
                  FROM accounting_period_close_sources AS source
                  JOIN vouchers AS voucher
                    ON voucher.org_id = source.org_id AND voucher.id = source.voucher_id
                  JOIN business_events AS event
                    ON event.org_id = voucher.org_id AND event.id = voucher.event_id
                 WHERE source.org_id = target_close.org_id
                   AND source.close_id = target_close.id
                   AND (
                       source.event_id <> voucher.event_id
                       OR source.voucher_number <> voucher.voucher_number
                       OR source.posting_date <> voucher.posting_date
                       OR source.description <> voucher.description
                       OR source.event_type <> event.event_type
                       OR source.request_payload_hash_at_close IS DISTINCT FROM
                          event.request_payload_hash
                       OR source.debit_fen <> (
                           SELECT sum(line.debit_fen) FROM voucher_lines AS line
                            WHERE line.org_id = voucher.org_id
                              AND line.voucher_id = voucher.id
                       )
                       OR source.credit_fen <> (
                           SELECT sum(line.credit_fen) FROM voucher_lines AS line
                            WHERE line.org_id = voucher.org_id
                              AND line.voucher_id = voucher.id
                       )
                       OR source.line_snapshot::jsonb <> (
                           SELECT COALESCE(jsonb_agg(jsonb_build_object(
                               'id', line.id,
                               'line_number', line.line_number,
                               'account_id', line.account_id,
                               'account_code', account.code,
                               'counterparty_id', line.counterparty_id,
                               'debit_fen', line.debit_fen,
                               'credit_fen', line.credit_fen,
                               'memo', line.memo
                           ) ORDER BY line.line_number, line.id), '[]'::jsonb)
                             FROM voucher_lines AS line
                             JOIN accounts AS account
                               ON account.org_id = line.org_id
                              AND account.id = line.account_id
                            WHERE line.org_id = voucher.org_id
                              AND line.voucher_id = voucher.id
                       )
                   )
            ) OR EXISTS (
                SELECT 1 FROM vouchers AS voucher
                 WHERE voucher.org_id = target_period.org_id
                   AND voucher.status IN ('posted','reversed')
                   AND voucher.posting_date BETWEEN
                       target_period.start_date AND target_period.end_date
                   AND NOT EXISTS (
                       SELECT 1 FROM accounting_period_close_sources AS source
                        WHERE source.org_id = voucher.org_id
                          AND source.close_id = target_close.id
                          AND source.voucher_id = voucher.id
                   )
            ) INTO invalid_source;
            IF invalid_source THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;

            IF NOT finance_text_is_canonical_jsonb(target_close.calculation_payload)
               OR target_close.calculation_payload::jsonb <>
                  target_close.calculation::jsonb
               OR encode(digest(convert_to(target_close.calculation_payload, 'UTF8'), 'sha256'), 'hex')
                  <> target_close.calculation_hash
               OR target_close.calculation::jsonb ->> 'rule_version' <>
                  target_close.rule_version
               OR target_close.calculation::jsonb ->> 'rule_effective_from' <>
                  target_close.rule_effective_from::text
               OR target_close.calculation::jsonb -> 'source_urls' <>
                  target_close.source_urls::jsonb
               OR target_close.calculation::jsonb ->> 'checker_version' <>
                  target_close.checker_version
               OR target_close.calculation::jsonb ->> 'organization_id' <>
                  target_close.org_id::text
               OR target_close.calculation::jsonb ->> 'period_id' <>
                  target_period.id::text
               OR (target_close.calculation::jsonb ->> 'calendar_year')::integer <>
                  target_period.calendar_year
               OR (target_close.calculation::jsonb ->> 'calendar_month')::integer <>
                  target_period.calendar_month
               OR target_close.calculation::jsonb ->> 'start_date' <>
                  target_period.start_date::text
               OR target_close.calculation::jsonb ->> 'end_date' <>
                  target_period.end_date::text
               OR target_close.calculation::jsonb ->> 'closing_date' <>
                  target_period.end_date::text
               OR target_close.calculation::jsonb ->> 'previous_close_hash'
                  IS DISTINCT FROM target_close.previous_close_hash
               OR target_close.calculation::jsonb -> 'voucher_sources' <>
                  expected_sources
               OR target_close.calculation::jsonb -> 'account_totals' <>
                  expected_account_totals
               OR target_close.calculation::jsonb -> 'system_checks' <>
                  expected_system_checks
               OR target_close.calculation::jsonb -> 'module_checks' <>
                  expected_module_checks
               OR target_close.calculation::jsonb -> 'review_counts' <>
                  expected_review_counts
               OR target_close.calculation::jsonb -> 'warnings' <>
                  expected_warnings THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;
        EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
            RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_assert_accounting_period(
            target_period_id uuid
        ) RETURNS void AS $$
        DECLARE target_period accounting_periods%ROWTYPE;
        BEGIN
            SELECT * INTO target_period FROM accounting_periods
             WHERE id = target_period_id;
            IF NOT FOUND THEN RETURN; END IF;
            PERFORM finance_assert_accounting_period_org(target_period.org_id);
            PERFORM finance_assert_accounting_period_action(target_period.generation_action_id);
            IF target_period.status = 'open' THEN
                IF target_period.close_id IS NOT NULL OR target_period.closed_at IS NOT NULL THEN
                    RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
                END IF;
            ELSE
                PERFORM finance_assert_accounting_period_close(target_period.close_id);
            END IF;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_accounting_period()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN
                PERFORM finance_assert_accounting_period(OLD.id);
            END IF;
            IF TG_OP IN ('INSERT','UPDATE') THEN
                PERFORM finance_assert_accounting_period(NEW.id);
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_accounting_period_action()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN
                PERFORM finance_assert_accounting_period_action(OLD.id);
            END IF;
            IF TG_OP IN ('INSERT','UPDATE') THEN
                PERFORM finance_assert_accounting_period_action(NEW.id);
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_accounting_period_evidence()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN
                PERFORM finance_assert_accounting_period_action(OLD.action_id);
            END IF;
            IF TG_OP IN ('INSERT','UPDATE') THEN
                PERFORM finance_assert_accounting_period_action(NEW.action_id);
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_accounting_period_close()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN
                PERFORM finance_assert_accounting_period_close(OLD.id);
            END IF;
            IF TG_OP IN ('INSERT','UPDATE') THEN
                PERFORM finance_assert_accounting_period_close(NEW.id);
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_accounting_period_close_source()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN
                PERFORM finance_assert_accounting_period_close(OLD.close_id);
            END IF;
            IF TG_OP IN ('INSERT','UPDATE') THEN
                PERFORM finance_assert_accounting_period_close(NEW.close_id);
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_accounting_period_org()
        RETURNS trigger AS $$
        BEGIN
            PERFORM finance_assert_accounting_period_org(NEW.id);
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_accounting_period_calendar()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN
                PERFORM finance_assert_accounting_period_org(OLD.org_id);
            END IF;
            IF TG_OP IN ('INSERT','UPDATE') THEN
                PERFORM finance_assert_accounting_period_org(NEW.org_id);
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE CONSTRAINT TRIGGER accounting_period_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON accounting_periods
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_accounting_period();
        CREATE CONSTRAINT TRIGGER accounting_period_action_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON accounting_period_actions
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_accounting_period_action();
        CREATE CONSTRAINT TRIGGER accounting_period_evidence_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON accounting_period_action_evidence
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_accounting_period_evidence();
        CREATE CONSTRAINT TRIGGER accounting_period_close_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON accounting_period_closes
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_accounting_period_close();
        CREATE CONSTRAINT TRIGGER accounting_period_close_source_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON accounting_period_close_sources
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_accounting_period_close_source();
        CREATE CONSTRAINT TRIGGER accounting_period_org_invariant_deferred
        AFTER INSERT OR UPDATE ON organizations
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_accounting_period_org();
        CREATE CONSTRAINT TRIGGER accounting_period_calendar_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON accounting_period_calendars
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_accounting_period_calendar();
        """
    )


def _install_postgresql_dependency_assertions() -> None:
    op.execute(
        r"""
        CREATE OR REPLACE FUNCTION finance_business_event_amount(target_facts jsonb)
        RETURNS bigint AS $$
        DECLARE raw jsonb;
        DECLARE numeric_value numeric;
        BEGIN
            raw := COALESCE(
                target_facts #> '{amounts,gross_amount_fen}',
                target_facts #> '{amounts,amount_fen}'
            );
            IF jsonb_typeof(raw) <> 'number' THEN
                RAISE EXCEPTION 'BUSINESS_EVENT_DEPENDENCY_INVALID';
            END IF;
            numeric_value := (raw #>> '{}')::numeric;
            IF numeric_value <= 0 OR numeric_value <> trunc(numeric_value)
               OR numeric_value > 9223372036854775807 THEN
                RAISE EXCEPTION 'BUSINESS_EVENT_DEPENDENCY_INVALID';
            END IF;
            RETURN numeric_value::bigint;
        EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
            RAISE EXCEPTION 'BUSINESS_EVENT_DEPENDENCY_INVALID';
        END;
        $$ LANGUAGE plpgsql IMMUTABLE STRICT;

        CREATE OR REPLACE FUNCTION finance_business_event_parent_amount(
            target_event business_events
        ) RETURNS bigint AS $$
        DECLARE raw jsonb;
        DECLARE numeric_value numeric;
        BEGIN
            IF target_event.event_type <> 'customer_receipt' THEN
                RETURN finance_business_event_amount(target_event.facts::jsonb);
            END IF;
            raw := target_event.facts::jsonb #> '{derived,advance_fen}';
            IF jsonb_typeof(raw) <> 'number' THEN
                RAISE EXCEPTION 'BUSINESS_EVENT_DEPENDENCY_INVALID';
            END IF;
            numeric_value := (raw #>> '{}')::numeric;
            IF numeric_value <= 0 OR numeric_value <> trunc(numeric_value)
               OR numeric_value > 9223372036854775807 THEN
                RAISE EXCEPTION 'BUSINESS_EVENT_DEPENDENCY_INVALID';
            END IF;
            RETURN numeric_value::bigint;
        EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
            RAISE EXCEPTION 'BUSINESS_EVENT_DEPENDENCY_INVALID';
        END;
        $$ LANGUAGE plpgsql IMMUTABLE STRICT;

        CREATE OR REPLACE FUNCTION finance_assert_business_event_dependency(
            target_dependency_id uuid
        ) RETURNS void AS $$
        DECLARE dependency business_event_dependencies%ROWTYPE;
        DECLARE parent business_events%ROWTYPE;
        DECLARE child business_events%ROWTYPE;
        DECLARE active_usage numeric;
        BEGIN
            SELECT * INTO dependency FROM business_event_dependencies
             WHERE id = target_dependency_id;
            IF NOT FOUND THEN RETURN; END IF;
            SELECT * INTO parent FROM business_events
             WHERE id = dependency.parent_event_id AND org_id = dependency.org_id;
            SELECT * INTO child FROM business_events
             WHERE id = dependency.child_event_id AND org_id = dependency.org_id;
            IF parent.id IS NULL OR child.id IS NULL
               OR parent.status NOT IN ('posted','reversed')
               OR child.status NOT IN ('posted','reversed')
               OR child.facts::jsonb #>> '{details,original_event_id}' <>
                  parent.id::text
               OR dependency.amount_fen <> finance_business_event_amount(child.facts::jsonb)
               OR NOT (
                   (dependency.dependency_kind = 'advance_fulfillment'
                    AND child.event_type = 'service_fulfillment'
                    AND parent.event_type IN ('customer_advance','customer_receipt'))
                   OR (dependency.dependency_kind = 'advance_refund'
                    AND child.event_type = 'customer_refund'
                    AND child.facts::jsonb #>> '{details,refund_kind}' = 'advance'
                    AND parent.event_type IN ('customer_advance','customer_receipt'))
                   OR (dependency.dependency_kind = 'sale_return'
                    AND child.event_type = 'customer_refund'
                    AND child.facts::jsonb #>> '{details,refund_kind}' = 'sale_return'
                    AND parent.event_type = 'service_cash_sale')
               ) THEN
                RAISE EXCEPTION 'BUSINESS_EVENT_DEPENDENCY_INVALID';
            END IF;
            IF child.status = 'posted' AND parent.status <> 'posted' THEN
                RAISE EXCEPTION 'REVERSE_DEPENDENT_EVENTS_FIRST';
            END IF;
            SELECT COALESCE(sum(candidate.amount_fen), 0) INTO active_usage
              FROM business_event_dependencies AS candidate
              JOIN business_events AS candidate_child
                ON candidate_child.org_id = candidate.org_id
               AND candidate_child.id = candidate.child_event_id
             WHERE candidate.org_id = dependency.org_id
               AND candidate.parent_event_id = dependency.parent_event_id
               AND candidate_child.status = 'posted';
            IF active_usage > finance_business_event_parent_amount(parent) THEN
                RAISE EXCEPTION 'BUSINESS_EVENT_DEPENDENCY_INVALID';
            END IF;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_lock_business_event_dependency_parent()
        RETURNS trigger AS $$
        DECLARE parent_status varchar;
        BEGIN
            PERFORM pg_advisory_xact_lock(hashtextextended(
                'business-event-dependency-parent:' || NEW.org_id::text || ':' ||
                NEW.parent_event_id::text,
                0
            ));
            SELECT status INTO parent_status FROM business_events
             WHERE org_id = NEW.org_id AND id = NEW.parent_event_id
             FOR UPDATE;
            IF parent_status IS NULL OR parent_status NOT IN ('posted','reversed') THEN
                RAISE EXCEPTION 'BUSINESS_EVENT_DEPENDENCY_INVALID';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_assert_business_event_dependency_from_event(
            target_event_id uuid
        ) RETURNS void AS $$
        DECLARE target_event business_events%ROWTYPE;
        DECLARE dependency_id uuid;
        DECLARE dependency_count bigint;
        BEGIN
            SELECT * INTO target_event FROM business_events WHERE id = target_event_id;
            IF NOT FOUND THEN RETURN; END IF;
            IF target_event.status IN ('posted','reversed')
               AND target_event.event_type IN ('service_fulfillment','customer_refund') THEN
                SELECT count(*) INTO dependency_count FROM business_event_dependencies
                 WHERE org_id = target_event.org_id AND child_event_id = target_event.id;
                IF dependency_count <> 1 THEN
                    RAISE EXCEPTION 'BUSINESS_EVENT_DEPENDENCY_INVALID';
                END IF;
            END IF;
            FOR dependency_id IN
                SELECT id FROM business_event_dependencies
                 WHERE org_id = target_event.org_id
                   AND (parent_event_id = target_event.id OR child_event_id = target_event.id)
                 ORDER BY id
            LOOP
                PERFORM finance_assert_business_event_dependency(dependency_id);
            END LOOP;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_guard_business_event_dependency_parent_reversal()
        RETURNS trigger AS $$
        BEGIN
            IF OLD.status = 'posted' AND NEW.status = 'reversed'
               AND EXISTS (
                   SELECT 1
                     FROM business_event_dependencies AS dependency
                     JOIN business_events AS child
                       ON child.org_id = dependency.org_id
                      AND child.id = dependency.child_event_id
                    WHERE dependency.org_id = OLD.org_id
                      AND dependency.parent_event_id = OLD.id
                      AND child.status = 'posted'
               ) THEN
                RAISE EXCEPTION 'REVERSE_DEPENDENT_EVENTS_FIRST';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_block_business_event_dependency_mutation()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN RETURN NEW; END IF;
            RAISE EXCEPTION 'BUSINESS_EVENT_DEPENDENCY_INVALID';
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_business_event_dependency()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN
                PERFORM finance_assert_business_event_dependency(OLD.id);
            END IF;
            IF TG_OP IN ('INSERT','UPDATE') THEN
                PERFORM finance_assert_business_event_dependency(NEW.id);
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_business_event_dependency_event()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN
                PERFORM finance_assert_business_event_dependency_from_event(OLD.id);
            END IF;
            IF TG_OP IN ('INSERT','UPDATE') THEN
                PERFORM finance_assert_business_event_dependency_from_event(NEW.id);
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER business_event_dependency_parent_reversal_guard
        BEFORE UPDATE ON business_events
        FOR EACH ROW EXECUTE FUNCTION finance_guard_business_event_dependency_parent_reversal();
        CREATE TRIGGER business_event_dependency_parent_insert_lock
        BEFORE INSERT ON business_event_dependencies
        FOR EACH ROW EXECUTE FUNCTION finance_lock_business_event_dependency_parent();
        CREATE TRIGGER business_event_dependency_immutable
        BEFORE UPDATE OR DELETE ON business_event_dependencies
        FOR EACH ROW EXECUTE FUNCTION finance_block_business_event_dependency_mutation();
        CREATE CONSTRAINT TRIGGER business_event_dependency_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON business_event_dependencies
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_business_event_dependency();
        CREATE CONSTRAINT TRIGGER business_event_dependency_event_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON business_events
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_business_event_dependency_event();
        """
    )


def _install_postgresql_tax_posting_date_assertions() -> None:
    """Version the 0010 validator and extend only its deterministic date leaves."""

    bind = op.get_bind()
    definition = bind.execute(
        sa.text("SELECT pg_get_functiondef('finance_assert_tax_period(uuid)'::regprocedure)")
    ).scalar_one()
    signature = "public.finance_assert_tax_period(target_period_id uuid)"
    if signature not in definition:
        raise RuntimeError("ACCOUNTING_PERIOD_TAX_ASSERTION_PRECHECK_FAILED")
    legacy_definition = definition.replace(
        signature,
        "public.finance_assert_tax_period_0011(target_period_id uuid)",
        1,
    )
    bind.execute(sa.text(legacy_definition))
    current_definition = definition.replace(
        signature,
        "public.finance_assert_tax_period_0012(target_period_id uuid)",
        1,
    )
    replacements = (
        (
            "'end_date', period.end_date::text\n                ),",
            "'end_date', period.end_date::text,\n"
            "                    'adjustment_posting_date', "
            "period.adjustment_posting_date::text\n                ),",
        ),
        (
            "'taxable_event_count', taxable_event_count\n                ),",
            "'taxable_event_count', taxable_event_count,\n"
            "                    'adjustment_posting_date', "
            "period.adjustment_posting_date::text\n                ),",
        ),
        (
            "'end_date', period.end_date::text,\n                'filing_cycle'",
            "'end_date', period.end_date::text,\n"
            "                'adjustment_posting_date', "
            "period.adjustment_posting_date::text,\n                'filing_cycle'",
        ),
        (
            "OR adjustment.posting_date <> period.end_date",
            "OR period.adjustment_posting_date < period.end_date\n"
            "               OR adjustment.posting_date <> period.adjustment_posting_date",
        ),
        (
            "OR target_voucher.posting_date <> period.end_date",
            "OR target_voucher.posting_date <> period.adjustment_posting_date",
        ),
    )
    for old, new in replacements:
        if old not in current_definition:
            raise RuntimeError("ACCOUNTING_PERIOD_TAX_ASSERTION_PRECHECK_FAILED")
        current_definition = current_definition.replace(old, new, 1)
    bind.execute(sa.text(current_definition))
    op.execute(
        r"""
        CREATE OR REPLACE FUNCTION finance_assert_tax_period(target_period_id uuid)
        RETURNS void AS $$
        DECLARE target_period tax_periods%ROWTYPE;
        BEGIN
            SELECT * INTO target_period FROM tax_periods WHERE id = target_period_id;
            IF NOT FOUND THEN RETURN; END IF;
            IF target_period.calculation::jsonb ? 'adjustment_posting_date' THEN
                PERFORM finance_assert_tax_period_0012(target_period_id);
            ELSE
                IF target_period.adjustment_posting_date <> target_period.end_date THEN
                    RAISE EXCEPTION 'TAX_PERIOD_SNAPSHOT_IMMUTABLE';
                END IF;
                PERFORM finance_assert_tax_period_0011(target_period_id);
            END IF;
        END;
        $$ LANGUAGE plpgsql;
        """
    )


def _assert_downgrade_safe() -> None:
    bind = op.get_bind()
    for table_name in (
        "accounting_periods",
        "accounting_period_actions",
        "accounting_period_closes",
    ):
        if bind.execute(sa.text(f"SELECT 1 FROM {table_name} LIMIT 1")).scalar() is not None:
            raise RuntimeError("ACCOUNTING_PERIOD_DOWNGRADE_UNSAFE: period-control history exists")
    if (
        bind.execute(
            sa.text(
                """
                SELECT 1 FROM business_event_dependencies AS dependency
                 LEFT JOIN accounting_period_dependency_migration_actions AS owned
                   ON owned.dependency_id = dependency.id
                 WHERE owned.dependency_id IS NULL
                 LIMIT 1
                """
            )
        ).scalar()
        is not None
    ):
        raise RuntimeError("ACCOUNTING_PERIOD_DOWNGRADE_UNSAFE: canonical event dependencies exist")
    if (
        bind.execute(
            sa.text(
                """
                SELECT 1
                  FROM tax_periods AS period
                  LEFT JOIN business_events AS event
                    ON event.org_id = period.org_id
                   AND event.id = period.adjustment_event_id
                  LEFT JOIN vouchers AS voucher
                    ON voucher.org_id = event.org_id AND voucher.event_id = event.id
                 WHERE period.adjustment_posting_date <> period.end_date
                    OR event.posting_date <> period.end_date
                    OR voucher.posting_date <> period.end_date
                 LIMIT 1
                """
            )
        ).scalar()
        is not None
    ):
        raise RuntimeError(
            "ACCOUNTING_PERIOD_DOWNGRADE_UNSAFE: later tax adjustment posting exists"
        )
    if (
        bind.execute(
            sa.text(
                """
                SELECT 1 FROM organizations AS organization
                 WHERE organization.accounting_period_control_start_date IS NOT NULL
                   AND NOT EXISTS (
                       SELECT 1 FROM accounting_period_actions AS action
                        WHERE action.org_id = organization.id
                   )
                 LIMIT 1
                """
            )
        ).scalar()
        is not None
    ):
        raise RuntimeError(
            "ACCOUNTING_PERIOD_DOWNGRADE_UNSAFE: untracked period-control configuration exists"
        )


def downgrade() -> None:
    _assert_downgrade_safe()
    _remove_postgresql_guards()
    with op.batch_alter_table("tax_periods") as batch_op:
        batch_op.drop_column("adjustment_posting_date")
    op.drop_table("accounting_period_dependency_migration_actions")
    op.drop_index(
        "ix_business_event_dependencies_parent_event_id",
        table_name="business_event_dependencies",
    )
    op.drop_index("ix_business_event_dependencies_org_id", table_name="business_event_dependencies")
    op.drop_table("business_event_dependencies")
    op.drop_table("accounting_period_action_evidence")
    op.drop_index(
        "ix_accounting_period_close_sources_org_id",
        table_name="accounting_period_close_sources",
    )
    op.drop_table("accounting_period_close_sources")
    with op.batch_alter_table("accounting_periods") as batch_op:
        batch_op.drop_constraint("ck_period_close_state", type_="check")
        batch_op.drop_constraint("uq_accounting_period_close_id", type_="unique")
        batch_op.drop_constraint("fk_accounting_period_org_close", type_="foreignkey")
        batch_op.drop_column("close_id")
    op.drop_index("ix_accounting_period_closes_org_id", table_name="accounting_period_closes")
    op.drop_table("accounting_period_closes")
    op.drop_index("ix_accounting_periods_calendar_id", table_name="accounting_periods")
    with op.batch_alter_table("accounting_periods") as batch_op:
        batch_op.drop_constraint("ck_period_natural_month", type_="check")
        batch_op.drop_constraint("ck_period_month", type_="check")
        batch_op.drop_constraint("ck_period_year", type_="check")
        batch_op.drop_constraint("uq_accounting_period_generation_action", type_="unique")
        batch_op.drop_constraint("uq_accounting_period_org_month", type_="unique")
        batch_op.drop_constraint("uq_accounting_period_org_id", type_="unique")
        batch_op.drop_constraint("fk_accounting_period_org_generation_action", type_="foreignkey")
        batch_op.drop_constraint("fk_accounting_period_org_calendar", type_="foreignkey")
        batch_op.drop_column("calendar_month")
        batch_op.drop_column("calendar_year")
        batch_op.drop_column("generation_action_id")
        batch_op.drop_column("calendar_id")
    op.drop_index("ix_accounting_period_calendars_org_id", table_name="accounting_period_calendars")
    op.drop_table("accounting_period_calendars")
    op.drop_index("ix_accounting_period_actions_org_id", table_name="accounting_period_actions")
    op.drop_table("accounting_period_actions")
    with op.batch_alter_table("organizations") as batch_op:
        batch_op.drop_constraint("ck_org_accounting_period_control", type_="check")
        batch_op.drop_column("accounting_period_control_start_date")
        batch_op.drop_column("accounting_period_control_enabled")


def _remove_postgresql_guards() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        r"""
        DROP TRIGGER IF EXISTS business_event_dependency_event_invariant_deferred
          ON business_events;
        DROP TRIGGER IF EXISTS business_event_dependency_invariant_deferred
          ON business_event_dependencies;
        DROP TRIGGER IF EXISTS business_event_dependency_immutable
          ON business_event_dependencies;
        DROP TRIGGER IF EXISTS business_event_dependency_parent_insert_lock
          ON business_event_dependencies;
        DROP TRIGGER IF EXISTS business_event_dependency_parent_reversal_guard
          ON business_events;
        DROP TRIGGER IF EXISTS unfinished_payroll_period_invariant_deferred
          ON payroll_batches;
        DROP TRIGGER IF EXISTS draft_voucher_period_invariant_deferred ON vouchers;
        DROP TRIGGER IF EXISTS draft_business_event_period_invariant_deferred
          ON business_events;
        DROP TRIGGER IF EXISTS accounting_period_org_invariant_deferred ON organizations;
        DROP TRIGGER IF EXISTS accounting_period_calendar_invariant_deferred
          ON accounting_period_calendars;
        DROP TRIGGER IF EXISTS accounting_period_close_source_invariant_deferred
          ON accounting_period_close_sources;
        DROP TRIGGER IF EXISTS accounting_period_close_invariant_deferred
          ON accounting_period_closes;
        DROP TRIGGER IF EXISTS accounting_period_evidence_invariant_deferred
          ON accounting_period_action_evidence;
        DROP TRIGGER IF EXISTS accounting_period_action_invariant_deferred
          ON accounting_period_actions;
        DROP TRIGGER IF EXISTS accounting_period_invariant_deferred ON accounting_periods;
        DROP TRIGGER IF EXISTS accounting_period_dependency_migration_action_immutable
          ON accounting_period_dependency_migration_actions;
        DROP TRIGGER IF EXISTS accounting_period_action_evidence_immutable
          ON accounting_period_action_evidence;
        DROP TRIGGER IF EXISTS accounting_period_close_source_immutable
          ON accounting_period_close_sources;
        DROP TRIGGER IF EXISTS accounting_period_close_source_insert_guard
          ON accounting_period_close_sources;
        DROP TRIGGER IF EXISTS accounting_period_close_immutable ON accounting_period_closes;
        DROP TRIGGER IF EXISTS accounting_period_action_immutable ON accounting_period_actions;
        DROP TRIGGER IF EXISTS accounting_period_calendar_immutable
          ON accounting_period_calendars;
        DROP TRIGGER IF EXISTS accounting_period_single_direction ON accounting_periods;
        DROP TRIGGER IF EXISTS accounting_period_org_immutable ON organizations;
        DROP TRIGGER IF EXISTS final_voucher_accounting_period_guard ON vouchers;

        DROP FUNCTION IF EXISTS finance_validate_business_event_dependency_event();
        DROP FUNCTION IF EXISTS finance_validate_business_event_dependency();
        DROP FUNCTION IF EXISTS finance_block_business_event_dependency_mutation();
        DROP FUNCTION IF EXISTS finance_lock_business_event_dependency_parent();
        DROP FUNCTION IF EXISTS finance_guard_business_event_dependency_parent_reversal();
        DROP FUNCTION IF EXISTS finance_assert_business_event_dependency_from_event(uuid);
        DROP FUNCTION IF EXISTS finance_assert_business_event_dependency(uuid);
        DROP FUNCTION IF EXISTS finance_business_event_parent_amount(business_events);
        DROP FUNCTION IF EXISTS finance_business_event_amount(jsonb);
        DROP FUNCTION IF EXISTS finance_validate_accounting_period_org();
        DROP FUNCTION IF EXISTS finance_validate_accounting_period_calendar();
        DROP FUNCTION IF EXISTS finance_validate_accounting_period_close_source();
        DROP FUNCTION IF EXISTS finance_validate_accounting_period_close();
        DROP FUNCTION IF EXISTS finance_validate_accounting_period_evidence();
        DROP FUNCTION IF EXISTS finance_validate_accounting_period_action();
        DROP FUNCTION IF EXISTS finance_validate_accounting_period();
        DROP FUNCTION IF EXISTS finance_assert_accounting_period(uuid);
        DROP FUNCTION IF EXISTS finance_assert_accounting_period_close(uuid);
        DROP FUNCTION IF EXISTS finance_assert_accounting_period_org(uuid);
        DROP FUNCTION IF EXISTS finance_assert_accounting_period_action(uuid);
        DROP FUNCTION IF EXISTS finance_guard_accounting_period_close_insert();
        DROP FUNCTION IF EXISTS finance_guard_accounting_period_close_source_insert();
        DROP FUNCTION IF EXISTS finance_block_accounting_period_immutable();
        DROP FUNCTION IF EXISTS finance_guard_accounting_period_mutation();
        DROP FUNCTION IF EXISTS finance_guard_accounting_period_org_mutation();
        DROP FUNCTION IF EXISTS finance_guard_final_voucher_accounting_period();
        DROP FUNCTION IF EXISTS finance_validate_unfinished_payroll_period();
        DROP FUNCTION IF EXISTS finance_validate_draft_voucher_period();
        DROP FUNCTION IF EXISTS finance_validate_draft_business_event_period();
        DROP FUNCTION IF EXISTS finance_assert_accounting_write_period(uuid, date);
        DROP FUNCTION IF EXISTS finance_lock_accounting_period_generation_org(uuid);
        DROP FUNCTION IF EXISTS finance_lock_accounting_month(uuid, date);

        ALTER TABLE accounting_periods
          DROP CONSTRAINT IF EXISTS ex_accounting_period_no_overlap;
        DROP FUNCTION IF EXISTS finance_assert_tax_period(uuid);
        DROP FUNCTION IF EXISTS finance_assert_tax_period_0012(uuid);
        ALTER FUNCTION finance_assert_tax_period_0011(uuid)
          RENAME TO finance_assert_tax_period;

        CREATE OR REPLACE FUNCTION finance_block_final_voucher_in_closed_period()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.status IN ('posted', 'reversed')
               AND (TG_OP = 'INSERT' OR OLD.status IS DISTINCT FROM NEW.status)
               AND EXISTS (
                    SELECT 1 FROM accounting_periods AS period
                     WHERE period.org_id = NEW.org_id AND period.status = 'closed'
                       AND NEW.posting_date BETWEEN period.start_date AND period.end_date
               ) THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_CLOSED';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER final_voucher_closed_period_guard
        BEFORE INSERT OR UPDATE ON vouchers
        FOR EACH ROW EXECUTE FUNCTION finance_block_final_voucher_in_closed_period();
        """
    )
