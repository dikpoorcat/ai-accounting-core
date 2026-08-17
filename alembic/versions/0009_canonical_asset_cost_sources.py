"""Add canonical asset cards with finite employee cost sources.

Revision ID: 0009_canonical_asset_sources
Revises: 0008_grouped_depreciation
Create Date: 2026-08-18
"""

# ruff: noqa: E501 -- exact PostgreSQL function fragments intentionally preserve layout.

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0009_canonical_asset_sources"
down_revision = "0008_grouped_depreciation"
branch_labels = None
depends_on = None


_ROUNDING_CHECK_0008 = (
    "depreciation_rounding_policy IN ('floor_final_remainder_v1','round_half_up_group_v1')"
)
_ROUNDING_CHECK_0009 = (
    "depreciation_rounding_policy IN "
    "('floor_final_remainder_v1','round_half_up_card_v1','round_half_up_group_v1')"
)
_SETTLEMENT_CHECK_OLD = "settlement_method IN ('bank','payable','employee_payable')"
_SETTLEMENT_CHECK_NEW = (
    "settlement_method IN ('bank','payable','employee_payable','allocated_employee_payables')"
)
_SETTLEMENT_DATES_OLD = (
    "(settlement_method = 'bank' AND payment_date IS NOT NULL AND due_date IS NULL "
    "AND reimbursing_employee_id IS NULL) OR "
    "(settlement_method = 'payable' AND payment_date IS NULL AND due_date IS NOT NULL "
    "AND reimbursing_employee_id IS NULL) OR "
    "(settlement_method = 'employee_payable' AND payment_date IS NULL "
    "AND due_date IS NOT NULL AND reimbursing_employee_id IS NOT NULL)"
)
_SETTLEMENT_DATES_NEW = (
    _SETTLEMENT_DATES_OLD + " OR (settlement_method = 'allocated_employee_payables' "
    "AND payment_date IS NULL AND due_date IS NULL "
    "AND reimbursing_employee_id IS NULL)"
)

_CARD_BRANCH_OLD = (
    """            ELSIF activation.depreciation_rounding_policy = 'round_half_up_group_v1' THEN"""
)
_CARD_BRANCH_NEW = """            ELSIF activation.depreciation_rounding_policy = 'round_half_up_card_v1' THEN
                IF activation.depreciation_group_code IS NOT NULL THEN
                    RAISE EXCEPTION 'FIXED_ASSET_DEPRECIATION_GROUP_POLICY_INVALID';
                END IF;
                base_monthly := round(
                    depreciable::numeric / activation.useful_life_months
                )::bigint;
            ELSIF activation.depreciation_rounding_policy = 'round_half_up_group_v1' THEN"""

_DECLARATIONS_OLD = """        DECLARE open_item_count bigint;
        DECLARE all_open_item_count bigint;
        DECLARE expected_gain bigint;"""
_DECLARATIONS_NEW = """        DECLARE open_item_count bigint;
        DECLARE all_open_item_count bigint;
        DECLARE cost_source_count bigint;
        DECLARE cost_source_total bigint;
        DECLARE invalid_cost_source_count bigint;
        DECLARE expected_gain bigint;"""

_EMPLOYEE_CREDIT_OLD = """                   OR finance_asset_role_amount(target_voucher.id, 'employee_payable', 'credit')
                      <> (CASE WHEN asset.settlement_method = 'employee_payable' THEN asset.cost_fen ELSE 0 END) THEN"""
_EMPLOYEE_CREDIT_NEW = """                   OR finance_asset_role_amount(target_voucher.id, 'employee_payable', 'credit')
                      <> (CASE WHEN asset.settlement_method IN (
                          'employee_payable','allocated_employee_payables'
                      ) THEN asset.cost_fen ELSE 0 END) THEN"""

_SETTLEMENT_SHAPE_OLD = """                SELECT COUNT(*) INTO open_item_count FROM open_items AS item
                 WHERE item.org_id = asset.org_id AND item.source_event_id = target_event.id
                   AND item.item_type = 'payable'
                   AND item.counterparty_id = CASE
                       WHEN asset.settlement_method = 'employee_payable'
                       THEN asset.reimbursing_employee_id ELSE asset.supplier_id END
                   AND item.original_amount_fen = asset.cost_fen
                   AND item.due_date = asset.due_date;
                SELECT COUNT(*) INTO all_open_item_count FROM open_items AS item
                 WHERE item.org_id = asset.org_id AND item.source_event_id = target_event.id;
                SELECT COUNT(*) INTO bank_direct_count FROM bank_transactions AS transaction
                 WHERE transaction.org_id = asset.org_id
                   AND transaction.matched_event_id = target_event.id;
                IF (asset.settlement_method = 'bank' AND (
                        (bank_count <> 0 AND (bank_inflow <> 0 OR bank_outflow <> -asset.cost_fen
                        OR bank_total <> -asset.cost_fen)) OR all_open_item_count <> 0
                        OR (target_event.status = 'posted' AND bank_direct_count <> bank_count)
                        OR (target_event.status = 'reversed' AND bank_direct_count <> 0)
                    )) OR (asset.settlement_method IN ('payable','employee_payable') AND (
                        bank_count <> 0 OR bank_direct_count <> 0
                        OR open_item_count <> 1 OR all_open_item_count <> 1
                    )) THEN
                    RAISE EXCEPTION 'FIXED_ASSET_ACQUISITION_SETTLEMENT_SHAPE_INVALID';
                END IF;"""
_SETTLEMENT_SHAPE_NEW = """                SELECT COUNT(*) INTO open_item_count FROM open_items AS item
                 WHERE item.org_id = asset.org_id AND item.source_event_id = target_event.id
                   AND item.item_type = 'payable'
                   AND item.counterparty_id = CASE
                       WHEN asset.settlement_method = 'employee_payable'
                       THEN asset.reimbursing_employee_id ELSE asset.supplier_id END
                   AND item.original_amount_fen = asset.cost_fen
                   AND item.due_date = asset.due_date;
                SELECT COUNT(*) INTO all_open_item_count FROM open_items AS item
                 WHERE item.org_id = asset.org_id AND item.source_event_id = target_event.id;
                SELECT
                    count(*), COALESCE(sum(source.amount_fen), 0),
                    count(*) FILTER (
                        WHERE source.asset_id <> asset.id
                           OR source.event_id <> target_event.id
                           OR item.id IS NULL
                           OR item.source_event_id <> target_event.id
                           OR item.item_type <> 'payable'
                           OR item.counterparty_id <> source.employee_id
                           OR item.original_amount_fen <> source.amount_fen
                           OR item.due_date <> source.due_date
                           OR employee.kind <> 'employee'
                    )
                  INTO cost_source_count, cost_source_total, invalid_cost_source_count
                  FROM fixed_asset_cost_sources AS source
                  LEFT JOIN open_items AS item
                    ON item.org_id = source.org_id AND item.id = source.open_item_id
                  LEFT JOIN counterparties AS employee
                    ON employee.org_id = source.org_id AND employee.id = source.employee_id
                 WHERE source.org_id = asset.org_id AND source.event_id = target_event.id;
                SELECT COUNT(*) INTO bank_direct_count FROM bank_transactions AS transaction
                 WHERE transaction.org_id = asset.org_id
                   AND transaction.matched_event_id = target_event.id;
                IF (asset.settlement_method = 'bank' AND (
                        cost_source_count <> 0
                        OR (bank_count <> 0 AND (bank_inflow <> 0 OR bank_outflow <> -asset.cost_fen
                        OR bank_total <> -asset.cost_fen)) OR all_open_item_count <> 0
                        OR (target_event.status = 'posted' AND bank_direct_count <> bank_count)
                        OR (target_event.status = 'reversed' AND bank_direct_count <> 0)
                    )) OR (asset.settlement_method IN ('payable','employee_payable') AND (
                        cost_source_count <> 0 OR bank_count <> 0 OR bank_direct_count <> 0
                        OR open_item_count <> 1 OR all_open_item_count <> 1
                    )) OR (asset.settlement_method = 'allocated_employee_payables' AND (
                        bank_count <> 0 OR bank_direct_count <> 0 OR cost_source_count = 0
                        OR cost_source_total <> asset.cost_fen
                        OR invalid_cost_source_count <> 0
                        OR all_open_item_count <> cost_source_count
                    )) THEN
                    RAISE EXCEPTION 'FIXED_ASSET_ACQUISITION_SETTLEMENT_SHAPE_INVALID';
                END IF;"""

_SETTLEMENT_SHAPE_OLD_0014 = _SETTLEMENT_SHAPE_OLD.replace(
    "(bank_count <> 0 AND (bank_inflow <> 0 OR bank_outflow <> -asset.cost_fen\n"
    "                        OR bank_total <> -asset.cost_fen)) OR all_open_item_count <> 0",
    "bank_count = 0 OR bank_inflow <> 0 OR bank_outflow <> -asset.cost_fen\n"
    "                        OR bank_total <> -asset.cost_fen OR all_open_item_count <> 0",
)
_SETTLEMENT_SHAPE_NEW_0014 = _SETTLEMENT_SHAPE_NEW.replace(
    "cost_source_count <> 0\n"
    "                        OR (bank_count <> 0 AND (bank_inflow <> 0 OR bank_outflow <> -asset.cost_fen\n"
    "                        OR bank_total <> -asset.cost_fen)) OR all_open_item_count <> 0",
    "cost_source_count <> 0\n"
    "                        OR bank_count = 0 OR bank_inflow <> 0\n"
    "                        OR bank_outflow <> -asset.cost_fen\n"
    "                        OR bank_total <> -asset.cost_fen OR all_open_item_count <> 0",
)


def _function_definition(signature: str) -> str:
    definition = op.get_bind().scalar(
        sa.text("SELECT pg_get_functiondef(CAST(:signature AS regprocedure))"),
        {"signature": signature},
    )
    if not isinstance(definition, str):
        raise RuntimeError(f"CANONICAL_ASSET_FUNCTION_NOT_FOUND:{signature}")
    return definition


def _replace_once(definition: str, old: str, new: str, *, signature: str) -> str:
    if new in definition:
        return definition
    if definition.count(old) != 1:
        raise RuntimeError(f"CANONICAL_ASSET_FUNCTION_VERSION_MISMATCH:{signature}")
    return definition.replace(old, new, 1)


def _rewrite_function(
    signature: str, replacements: tuple[tuple[str, str], ...], *, upgrade: bool
) -> None:
    definition = _function_definition(signature)
    selected = replacements
    if not upgrade:
        selected = tuple((new, old) for old, new in reversed(replacements))
    original = definition
    for old, new in selected:
        definition = _replace_once(definition, old, new, signature=signature)
    if definition != original:
        op.get_bind().exec_driver_sql(definition.replace("%", "%%"))


def _rewrite_postgresql_functions(*, upgrade: bool) -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    _rewrite_function(
        "finance_assert_fixed_asset(uuid)",
        ((_CARD_BRANCH_OLD, _CARD_BRANCH_NEW),),
        upgrade=upgrade,
    )
    for signature, settlement_replacement in (
        (
            "finance_assert_fixed_asset_event_shape(uuid)",
            (_SETTLEMENT_SHAPE_OLD, _SETTLEMENT_SHAPE_NEW),
        ),
        (
            "finance_assert_fixed_asset_event_shape_0014(uuid)",
            (_SETTLEMENT_SHAPE_OLD_0014, _SETTLEMENT_SHAPE_NEW_0014),
        ),
    ):
        _rewrite_function(
            signature,
            (
                (_DECLARATIONS_OLD, _DECLARATIONS_NEW),
                (_EMPLOYEE_CREDIT_OLD, _EMPLOYEE_CREDIT_NEW),
                settlement_replacement,
            ),
            upgrade=upgrade,
        )


def _create_postgresql_triggers() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE TRIGGER fixed_asset_cost_source_row_lock
        BEFORE INSERT OR UPDATE OR DELETE ON fixed_asset_cost_sources
        FOR EACH ROW EXECUTE FUNCTION finance_lock_fixed_asset_row()
        """
    )
    op.execute(
        """
        CREATE TRIGGER immutable_final_fixed_asset_cost_source
        BEFORE INSERT OR UPDATE OR DELETE ON fixed_asset_cost_sources
        FOR EACH ROW EXECUTE FUNCTION finance_block_final_fixed_asset_fact_mutation()
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER fixed_asset_cost_source_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON fixed_asset_cost_sources
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_fixed_asset_fact()
        """
    )


def upgrade() -> None:
    with op.batch_alter_table("fixed_assets") as batch:
        batch.drop_constraint("ck_fixed_asset_settlement_method", type_="check")
        batch.drop_constraint("ck_fixed_asset_settlement_dates", type_="check")
        batch.alter_column(
            "settlement_method",
            existing_type=sa.String(length=20),
            type_=sa.String(length=40),
            existing_nullable=False,
        )
        batch.create_check_constraint("ck_fixed_asset_settlement_method", _SETTLEMENT_CHECK_NEW)
        batch.create_check_constraint("ck_fixed_asset_settlement_dates", _SETTLEMENT_DATES_NEW)
    with op.batch_alter_table("fixed_asset_activations") as batch:
        batch.drop_constraint("ck_asset_activation_rounding_policy", type_="check")
        batch.create_check_constraint("ck_asset_activation_rounding_policy", _ROUNDING_CHECK_0009)
        batch.alter_column(
            "depreciation_rounding_policy",
            existing_type=sa.String(length=50),
            nullable=False,
            server_default=sa.text("'round_half_up_card_v1'"),
        )
    op.create_table(
        "fixed_asset_cost_sources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("open_item_id", sa.Uuid(), nullable=False),
        sa.Column("source_key", sa.String(length=200), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("amount_fen", sa.BigInteger(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("amount_fen > 0", name="ck_fixed_asset_cost_source_amount"),
        sa.CheckConstraint("length(trim(source_key)) > 0", name="ck_fixed_asset_cost_source_key"),
        sa.CheckConstraint(
            "length(trim(description)) > 0",
            name="ck_fixed_asset_cost_source_description",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "asset_id"],
            ["fixed_assets.org_id", "fixed_assets.id"],
            name="fk_fixed_asset_cost_source_org_asset",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_fixed_asset_cost_source_org_event",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "employee_id"],
            ["counterparties.org_id", "counterparties.id"],
            name="fk_fixed_asset_cost_source_org_employee",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "open_item_id"],
            ["open_items.org_id", "open_items.id"],
            name="fk_fixed_asset_cost_source_org_open_item",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "org_id", "event_id", "source_key", name="uq_fixed_asset_cost_source_event_key"
        ),
        sa.UniqueConstraint("org_id", "id", name="uq_fixed_asset_cost_source_org_id"),
        sa.UniqueConstraint("open_item_id", name="uq_fixed_asset_cost_source_open_item"),
    )
    op.create_index(
        "ix_fixed_asset_cost_sources_org_id",
        "fixed_asset_cost_sources",
        ["org_id"],
    )
    op.create_index(
        "ix_fixed_asset_cost_sources_asset_id",
        "fixed_asset_cost_sources",
        ["asset_id"],
    )
    op.create_index(
        "ix_fixed_asset_cost_sources_event_id",
        "fixed_asset_cost_sources",
        ["event_id"],
    )
    _rewrite_postgresql_functions(upgrade=True)
    _create_postgresql_triggers()
    if op.get_bind().dialect.name == "postgresql":
        op.get_bind().execute(
            sa.text(
                "SELECT finance_assert_fixed_asset_from_event(id) FROM business_events "
                "WHERE status IN ('posted','reversed') AND event_type LIKE 'fixed_asset_%'"
            )
        )


def downgrade() -> None:
    unsafe = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM fixed_asset_cost_sources) OR EXISTS ("
            "SELECT 1 FROM fixed_assets WHERE settlement_method = 'allocated_employee_payables'"
            ") OR EXISTS (SELECT 1 FROM fixed_asset_activations "
            "WHERE depreciation_rounding_policy = 'round_half_up_card_v1')"
        )
    )
    if unsafe:
        raise RuntimeError("CANONICAL_ASSET_SOURCES_DOWNGRADE_UNSAFE")
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER fixed_asset_cost_source_invariant_deferred ON fixed_asset_cost_sources"
        )
        op.execute(
            "DROP TRIGGER immutable_final_fixed_asset_cost_source ON fixed_asset_cost_sources"
        )
        op.execute("DROP TRIGGER fixed_asset_cost_source_row_lock ON fixed_asset_cost_sources")
    _rewrite_postgresql_functions(upgrade=False)
    op.drop_index("ix_fixed_asset_cost_sources_event_id", table_name="fixed_asset_cost_sources")
    op.drop_index("ix_fixed_asset_cost_sources_asset_id", table_name="fixed_asset_cost_sources")
    op.drop_index("ix_fixed_asset_cost_sources_org_id", table_name="fixed_asset_cost_sources")
    op.drop_table("fixed_asset_cost_sources")
    with op.batch_alter_table("fixed_asset_activations") as batch:
        batch.drop_constraint("ck_asset_activation_rounding_policy", type_="check")
        batch.create_check_constraint("ck_asset_activation_rounding_policy", _ROUNDING_CHECK_0008)
        batch.alter_column(
            "depreciation_rounding_policy",
            existing_type=sa.String(length=50),
            nullable=False,
            server_default=sa.text("'round_half_up_group_v1'"),
        )
    with op.batch_alter_table("fixed_assets") as batch:
        batch.drop_constraint("ck_fixed_asset_settlement_dates", type_="check")
        batch.drop_constraint("ck_fixed_asset_settlement_method", type_="check")
        batch.create_check_constraint("ck_fixed_asset_settlement_method", _SETTLEMENT_CHECK_OLD)
        batch.create_check_constraint("ck_fixed_asset_settlement_dates", _SETTLEMENT_DATES_OLD)
        batch.alter_column(
            "settlement_method",
            existing_type=sa.String(length=40),
            type_=sa.String(length=20),
            existing_nullable=False,
        )
