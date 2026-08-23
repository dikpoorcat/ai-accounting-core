"""Defer output VAT until the recorded tax-obligation date.

Revision ID: 0019_deferred_output_vat
Revises: 0018_payroll_participation
Create Date: 2026-08-23
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from alembic import op

revision = "0019_deferred_output_vat"
down_revision = "0018_payroll_participation"
branch_labels = None
depends_on = None

_ACCOUNT_CODE = "222104"
_ACCOUNT_ROLE = "deferred_output_vat"


def _assert_account_slot_available() -> None:
    conflict = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM accounts "
            "WHERE code = :code OR system_role = :role)"
        ),
        {"code": _ACCOUNT_CODE, "role": _ACCOUNT_ROLE},
    )
    if conflict:
        raise RuntimeError("DEFERRED_OUTPUT_VAT_ACCOUNT_CONFLICT")


def _create_transfer_table() -> None:
    op.create_table(
        "deferred_output_vat_transfers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("source_event_id", sa.Uuid(), nullable=False),
        sa.Column("source_open_item_id", sa.Uuid(), nullable=False),
        sa.Column("transfer_event_id", sa.Uuid(), nullable=False),
        sa.Column("amount_fen", sa.BigInteger(), nullable=False),
        sa.Column("tax_obligation_date", sa.Date(), nullable=False),
        sa.Column("accounting_rule_version", sa.String(length=50), nullable=False),
        sa.Column("accounting_rule_source_url", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint("amount_fen > 0", name="ck_deferred_vat_transfer_amount"),
        sa.CheckConstraint(
            "length(trim(accounting_rule_version)) > 0 "
            "AND length(trim(accounting_rule_source_url)) > 0",
            name="ck_deferred_vat_transfer_rule_text",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "source_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_deferred_vat_transfer_org_source_event",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "source_open_item_id"],
            ["open_items.org_id", "open_items.id"],
            name="fk_deferred_vat_transfer_org_open_item",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "transfer_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_deferred_vat_transfer_org_transfer_event",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "org_id",
            "source_event_id",
            "transfer_event_id",
            name="uq_deferred_vat_transfer_source_event",
        ),
        sa.UniqueConstraint("org_id", "id", name="uq_deferred_vat_transfer_org_id"),
    )
    op.create_index(
        "ix_deferred_output_vat_transfers_org_id",
        "deferred_output_vat_transfers",
        ["org_id"],
    )
    op.create_index(
        "ix_deferred_output_vat_transfers_source_event_id",
        "deferred_output_vat_transfers",
        ["source_event_id"],
    )
    op.create_index(
        "ix_deferred_output_vat_transfers_source_open_item_id",
        "deferred_output_vat_transfers",
        ["source_open_item_id"],
    )
    op.create_index(
        "ix_deferred_output_vat_transfers_transfer_event_id",
        "deferred_output_vat_transfers",
        ["transfer_event_id"],
    )


def _seed_accounts() -> None:
    organizations = op.get_bind().execute(sa.text("SELECT id FROM organizations")).all()
    if not organizations:
        return
    accounts = sa.table(
        "accounts",
        sa.column("id", sa.Uuid()),
        sa.column("org_id", sa.Uuid()),
        sa.column("code", sa.String()),
        sa.column("name", sa.String()),
        sa.column("category", sa.String()),
        sa.column("normal_side", sa.String()),
        sa.column("system_role", sa.String()),
        sa.column("active", sa.Boolean()),
        sa.column("requires_bank_reconciliation", sa.Boolean()),
    )
    op.bulk_insert(
        accounts,
        [
            {
                "id": uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"ai-accounting-core:{org_id}:deferred-output-vat",
                ),
                "org_id": org_id,
                "code": _ACCOUNT_CODE,
                "name": "应交税费—待转销项税额",
                "category": "liability",
                "normal_side": "credit",
                "system_role": _ACCOUNT_ROLE,
                "active": True,
                "requires_bank_reconciliation": False,
            }
            for (org_id,) in organizations
        ],
    )


def _create_postgresql_guards() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE FUNCTION finance_guard_deferred_output_vat_transfer_0019()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE source_event business_events%ROWTYPE;
        DECLARE transfer_event business_events%ROWTYPE;
        DECLARE source_item open_items%ROWTYPE;
        DECLARE source_vat bigint;
        BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN
                RAISE EXCEPTION 'deferred output VAT transfer links are immutable';
            END IF;
            SELECT * INTO source_event FROM business_events
             WHERE org_id = NEW.org_id AND id = NEW.source_event_id;
            SELECT * INTO transfer_event FROM business_events
             WHERE org_id = NEW.org_id AND id = NEW.transfer_event_id;
            SELECT * INTO source_item FROM open_items
             WHERE org_id = NEW.org_id AND id = NEW.source_open_item_id;
            source_vat := COALESCE(
                (source_event.facts::jsonb #>> '{derived,vat_fen}')::bigint, 0
            );
            IF source_event.status <> 'posted'
               OR source_event.event_type <> 'service_credit_sale'
               OR source_item.source_event_id <> source_event.id
               OR source_item.item_type <> 'receivable'
               OR transfer_event.status <> 'draft'
               OR transfer_event.event_type <> 'customer_receipt'
               OR source_event.tax_obligation_date <> NEW.tax_obligation_date
               OR transfer_event.payment_date <> NEW.tax_obligation_date
               OR transfer_event.posting_date <> NEW.tax_obligation_date
               OR source_event.tax_obligation_date <= source_event.posting_date
               OR source_event.facts::jsonb #>> '{derived,vat_recognition}' <> 'deferred'
               OR NEW.amount_fen <> source_vat THEN
                RAISE EXCEPTION 'DEFERRED_OUTPUT_VAT_TRANSFER_FACTS_INVALID';
            END IF;
            RETURN NEW;
        EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
            RAISE EXCEPTION 'DEFERRED_OUTPUT_VAT_TRANSFER_FACTS_INVALID';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION finance_assert_deferred_output_vat_event_0019(target_event_id uuid)
        RETURNS void LANGUAGE plpgsql AS $$
        DECLARE target_event business_events%ROWTYPE;
        DECLARE target_voucher_id uuid;
        DECLARE vat_fen bigint;
        DECLARE transfer_total bigint;
        DECLARE invalid_links bigint;
        BEGIN
            SELECT * INTO target_event FROM business_events WHERE id = target_event_id;
            IF NOT FOUND OR target_event.status NOT IN ('posted','reversed')
               OR target_event.event_type NOT IN ('service_credit_sale','customer_receipt') THEN
                RETURN;
            END IF;
            SELECT id INTO target_voucher_id FROM vouchers
             WHERE org_id = target_event.org_id AND event_id = target_event.id
               AND status IN ('posted','reversed');
            IF target_voucher_id IS NULL THEN
                RAISE EXCEPTION 'DEFERRED_OUTPUT_VAT_FINAL_VOUCHER_MISSING';
            END IF;
            IF target_event.event_type = 'service_credit_sale'
               AND target_event.tax_obligation_date > target_event.posting_date
               AND COALESCE(
                   (target_event.facts::jsonb #>> '{derived,vat_fen}')::bigint, 0
               ) > 0 THEN
                vat_fen := COALESCE(
                    (target_event.facts::jsonb #>> '{derived,vat_fen}')::bigint, 0
                );
                IF vat_fen <= 0
                   OR target_event.facts::jsonb #>> '{derived,vat_recognition}' <> 'deferred'
                   OR finance_asset_role_amount(
                       target_voucher_id, 'deferred_output_vat', 'credit'
                   ) <> vat_fen
                   OR finance_asset_role_amount(
                       target_voucher_id, 'deferred_output_vat', 'debit'
                   ) <> 0
                   OR finance_asset_role_amount(target_voucher_id, 'vat_payable', 'credit') <> 0
                   OR finance_asset_role_amount(target_voucher_id, 'vat_payable', 'debit') <> 0 THEN
                    RAISE EXCEPTION 'DEFERRED_OUTPUT_VAT_SOURCE_VOUCHER_INVALID';
                END IF;
            ELSIF target_event.event_type = 'customer_receipt' THEN
                SELECT COALESCE(sum(link.amount_fen), 0)::bigint,
                       count(*) FILTER (
                           WHERE source_event.event_type <> 'service_credit_sale'
                              OR source_event.tax_obligation_date <> link.tax_obligation_date
                              OR source_event.tax_obligation_date <= source_event.posting_date
                              OR source_event.facts::jsonb #>> '{derived,vat_recognition}'
                                 <> 'deferred'
                              OR COALESCE(
                                  (source_event.facts::jsonb #>> '{derived,vat_fen}')::bigint, 0
                                 ) <> link.amount_fen
                              OR source_item.source_event_id <> source_event.id
                              OR source_item.item_type <> 'receivable'
                              OR settlement.payment_event_id <> target_event.id
                              OR settlement.open_item_id <> source_item.id
                              OR settlement.reversed IS DISTINCT FROM
                                 (target_event.status = 'reversed')
                              OR target_event.payment_date <> link.tax_obligation_date
                              OR target_event.posting_date <> link.tax_obligation_date
                       )
                  INTO transfer_total, invalid_links
                  FROM deferred_output_vat_transfers AS link
                  JOIN business_events AS source_event
                    ON source_event.org_id = link.org_id
                   AND source_event.id = link.source_event_id
                  JOIN open_items AS source_item
                    ON source_item.org_id = link.org_id
                   AND source_item.id = link.source_open_item_id
                  LEFT JOIN settlements AS settlement
                    ON settlement.org_id = link.org_id
                   AND settlement.open_item_id = link.source_open_item_id
                   AND settlement.payment_event_id = link.transfer_event_id
                 WHERE link.org_id = target_event.org_id
                   AND link.transfer_event_id = target_event.id;
                IF invalid_links <> 0
                   OR finance_asset_role_amount(
                       target_voucher_id, 'deferred_output_vat', 'debit'
                   ) <> transfer_total
                   OR finance_asset_role_amount(
                       target_voucher_id, 'deferred_output_vat', 'credit'
                   ) <> 0
                   OR finance_asset_role_amount(
                       target_voucher_id, 'vat_payable', 'credit'
                   ) <> transfer_total
                   OR finance_asset_role_amount(target_voucher_id, 'vat_payable', 'debit') <> 0 THEN
                    RAISE EXCEPTION 'DEFERRED_OUTPUT_VAT_TRANSFER_VOUCHER_INVALID';
                END IF;
            END IF;
        EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
            RAISE EXCEPTION 'DEFERRED_OUTPUT_VAT_EVENT_FACTS_INVALID';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION finance_validate_deferred_output_vat_event_0019()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_TABLE_NAME = 'deferred_output_vat_transfers' THEN
                IF TG_OP IN ('UPDATE','DELETE') THEN
                    PERFORM finance_assert_deferred_output_vat_event_0019(OLD.source_event_id);
                    PERFORM finance_assert_deferred_output_vat_event_0019(OLD.transfer_event_id);
                END IF;
                IF TG_OP IN ('INSERT','UPDATE') THEN
                    PERFORM finance_assert_deferred_output_vat_event_0019(NEW.source_event_id);
                    PERFORM finance_assert_deferred_output_vat_event_0019(NEW.transfer_event_id);
                END IF;
            ELSIF TG_TABLE_NAME = 'business_events' THEN
                PERFORM finance_assert_deferred_output_vat_event_0019(COALESCE(NEW.id, OLD.id));
            ELSIF TG_TABLE_NAME = 'vouchers' THEN
                PERFORM finance_assert_deferred_output_vat_event_0019(
                    COALESCE(NEW.event_id, OLD.event_id)
                );
            ELSE
                PERFORM finance_assert_deferred_output_vat_event_0019(event_id)
                  FROM vouchers WHERE id = COALESCE(NEW.voucher_id, OLD.voucher_id);
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER deferred_output_vat_transfer_immutable_0019 "
        "BEFORE INSERT OR UPDATE OR DELETE ON deferred_output_vat_transfers "
        "FOR EACH ROW EXECUTE FUNCTION finance_guard_deferred_output_vat_transfer_0019()"
    )
    for table in (
        "deferred_output_vat_transfers",
        "business_events",
        "vouchers",
        "voucher_lines",
    ):
        op.execute(
            f"CREATE CONSTRAINT TRIGGER {table}_deferred_output_vat_invariant_0019 "
            f"AFTER INSERT OR UPDATE OR DELETE ON {table} "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
            "EXECUTE FUNCTION finance_validate_deferred_output_vat_event_0019()"
        )


def upgrade() -> None:
    _assert_account_slot_available()
    _create_transfer_table()
    _seed_accounts()
    _create_postgresql_guards()


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM deferred_output_vat_transfers)")):
        raise RuntimeError("DEFERRED_OUTPUT_VAT_DOWNGRADE_UNSAFE")
    if op.get_bind().dialect.name == "postgresql":
        for table in (
            "voucher_lines",
            "vouchers",
            "business_events",
            "deferred_output_vat_transfers",
        ):
            op.execute(
                f"DROP TRIGGER IF EXISTS {table}_deferred_output_vat_invariant_0019 ON {table}"
            )
        op.execute(
            "DROP TRIGGER IF EXISTS deferred_output_vat_transfer_immutable_0019 "
            "ON deferred_output_vat_transfers"
        )
        op.execute("DROP FUNCTION finance_validate_deferred_output_vat_event_0019()")
        op.execute("DROP FUNCTION finance_assert_deferred_output_vat_event_0019(uuid)")
        op.execute("DROP FUNCTION finance_guard_deferred_output_vat_transfer_0019()")
    op.drop_table("deferred_output_vat_transfers")
    op.execute(
        sa.text("DELETE FROM accounts WHERE code = :code AND system_role = :role").bindparams(
            code=_ACCOUNT_CODE, role=_ACCOUNT_ROLE
        )
    )
