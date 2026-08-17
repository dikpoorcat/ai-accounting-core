"""Add versioned grouped half-up fixed-asset depreciation.

Revision ID: 0008_grouped_depreciation
Revises: 0007_refundable_deposit
Create Date: 2026-08-18
"""

# ruff: noqa: E501 -- exact PostgreSQL function fragments intentionally preserve layout.

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0008_grouped_depreciation"
down_revision = "0007_refundable_deposit"
branch_labels = None
depends_on = None


_LEGACY_POLICY = "floor_final_remainder_v1"
_GROUP_POLICY = "round_half_up_group_v1"

_DECLARATIONS_OLD = """        DECLARE base_monthly bigint;
        DECLARE depreciable bigint;
        DECLARE disposal_sequence integer;"""
_DECLARATIONS_NEW = """        DECLARE base_monthly bigint;
        DECLARE depreciable bigint;
        DECLARE group_depreciable bigint;
        DECLARE group_floor_total bigint;
        DECLARE group_extra_fen bigint;
        DECLARE group_member_count bigint;
        DECLARE member_remainder_rank bigint;
        DECLARE invalid_group_member boolean;
        DECLARE disposal_sequence integer;"""

_BASE_CALCULATION_OLD = """            depreciable := asset.cost_fen - activation.residual_value_fen;
            base_monthly := depreciable / activation.useful_life_months;

            FOR depreciation IN"""
_BASE_CALCULATION_NEW = """            depreciable := asset.cost_fen - activation.residual_value_fen;
            IF activation.depreciation_rounding_policy = 'floor_final_remainder_v1' THEN
                IF activation.depreciation_group_code IS NOT NULL THEN
                    RAISE EXCEPTION 'FIXED_ASSET_DEPRECIATION_GROUP_POLICY_INVALID';
                END IF;
                base_monthly := depreciable / activation.useful_life_months;
            ELSIF activation.depreciation_rounding_policy = 'round_half_up_group_v1' THEN
                SELECT EXISTS (
                    SELECT 1
                      FROM fixed_asset_activations AS member_activation
                      JOIN business_events AS member_event
                        ON member_event.id = member_activation.event_id
                       AND member_event.org_id = member_activation.org_id
                     WHERE member_activation.org_id = activation.org_id
                       AND member_event.status = 'posted'
                       AND (
                           (activation.depreciation_group_code IS NULL
                            AND member_activation.id = activation.id)
                           OR
                           (activation.depreciation_group_code IS NOT NULL
                            AND member_activation.depreciation_group_code
                                = activation.depreciation_group_code)
                       )
                       AND (
                           member_activation.depreciation_rounding_policy
                               <> activation.depreciation_rounding_policy
                           OR member_activation.in_service_date
                               <> activation.in_service_date
                           OR member_activation.useful_life_months
                               <> activation.useful_life_months
                           OR member_activation.benefit_area <> activation.benefit_area
                           OR member_activation.depreciation_method
                               <> activation.depreciation_method
                       )
                ) INTO invalid_group_member;
                IF invalid_group_member THEN
                    RAISE EXCEPTION 'FIXED_ASSET_DEPRECIATION_GROUP_POLICY_MISMATCH';
                END IF;
                IF activation.depreciation_group_code IS NOT NULL AND EXISTS (
                    SELECT 1
                      FROM fixed_asset_depreciations AS prior_depreciation
                      JOIN fixed_asset_activations AS prior_activation
                        ON prior_activation.id = prior_depreciation.activation_id
                       AND prior_activation.org_id = prior_depreciation.org_id
                      JOIN business_events AS prior_event
                        ON prior_event.id = prior_depreciation.event_id
                       AND prior_event.org_id = prior_depreciation.org_id
                     WHERE prior_activation.org_id = activation.org_id
                       AND prior_activation.depreciation_group_code
                           = activation.depreciation_group_code
                       AND prior_event.status IN ('posted', 'reversed')
                       AND prior_depreciation.created_at < activation.created_at
                ) THEN
                    RAISE EXCEPTION 'FIXED_ASSET_DEPRECIATION_GROUP_LOCKED';
                END IF;
                SELECT
                    sum(member_asset.cost_fen - member_activation.residual_value_fen),
                    sum(
                        (member_asset.cost_fen - member_activation.residual_value_fen)
                        / activation.useful_life_months
                    ),
                    count(*)
                  INTO group_depreciable, group_floor_total, group_member_count
                  FROM fixed_asset_activations AS member_activation
                  JOIN fixed_assets AS member_asset
                    ON member_asset.id = member_activation.asset_id
                   AND member_asset.org_id = member_activation.org_id
                  JOIN business_events AS member_event
                    ON member_event.id = member_activation.event_id
                   AND member_event.org_id = member_activation.org_id
                 WHERE member_activation.org_id = activation.org_id
                   AND member_event.status = 'posted'
                   AND (
                       (activation.depreciation_group_code IS NULL
                        AND member_activation.id = activation.id)
                       OR
                       (activation.depreciation_group_code IS NOT NULL
                        AND member_activation.depreciation_group_code
                            = activation.depreciation_group_code)
                   );
                IF group_member_count = 0 THEN
                    RAISE EXCEPTION 'FIXED_ASSET_DEPRECIATION_GROUP_INVALID';
                END IF;
                base_monthly := depreciable / activation.useful_life_months;
                group_extra_fen := round(
                    group_depreciable::numeric / activation.useful_life_months
                )::bigint - group_floor_total;
                SELECT ranked.remainder_rank INTO member_remainder_rank
                  FROM (
                      SELECT
                          member_asset.id,
                          row_number() OVER (
                              ORDER BY
                                  mod(
                                      member_asset.cost_fen
                                      - member_activation.residual_value_fen,
                                      activation.useful_life_months
                                  ) DESC,
                                  member_asset.asset_code,
                                  member_asset.id
                          ) AS remainder_rank
                        FROM fixed_asset_activations AS member_activation
                        JOIN fixed_assets AS member_asset
                          ON member_asset.id = member_activation.asset_id
                         AND member_asset.org_id = member_activation.org_id
                        JOIN business_events AS member_event
                          ON member_event.id = member_activation.event_id
                         AND member_event.org_id = member_activation.org_id
                       WHERE member_activation.org_id = activation.org_id
                         AND member_event.status = 'posted'
                         AND (
                             (activation.depreciation_group_code IS NULL
                              AND member_activation.id = activation.id)
                             OR
                             (activation.depreciation_group_code IS NOT NULL
                              AND member_activation.depreciation_group_code
                                  = activation.depreciation_group_code)
                         )
                  ) AS ranked
                 WHERE ranked.id = asset.id;
                IF group_extra_fen < 0 OR group_extra_fen > group_member_count
                   OR member_remainder_rank IS NULL THEN
                    RAISE EXCEPTION 'FIXED_ASSET_DEPRECIATION_GROUP_INVALID';
                END IF;
                IF member_remainder_rank <= group_extra_fen THEN
                    base_monthly := base_monthly + 1;
                END IF;
            ELSE
                RAISE EXCEPTION 'FIXED_ASSET_DEPRECIATION_ROUNDING_POLICY_INVALID';
            END IF;

            FOR depreciation IN"""

_READY_SHAPE_OLD = """                          AND ready.benefit_area
                              = target_event.facts::jsonb #>> '{ready_for_use,benefit_area}'
                          AND ready.accounting_rule_version"""
_READY_SHAPE_NEW = """                          AND ready.benefit_area
                              = target_event.facts::jsonb #>> '{ready_for_use,benefit_area}'
                          AND (
                              NOT (
                                  (target_event.facts::jsonb #> '{ready_for_use}')
                                      ? 'depreciation_rounding_policy'
                              )
                              OR ready.depreciation_rounding_policy
                                  = target_event.facts::jsonb #>>
                                      '{ready_for_use,depreciation_rounding_policy}'
                          )
                          AND (
                              NOT (
                                  (target_event.facts::jsonb #> '{ready_for_use}')
                                      ? 'depreciation_group_code'
                              )
                              OR COALESCE(ready.depreciation_group_code, '') = COALESCE(
                                  target_event.facts::jsonb #>>
                                      '{ready_for_use,depreciation_group_code}',
                                  ''
                              )
                          )
                          AND ready.accounting_rule_version"""

_ACTIVATION_SHAPE_OLD = """                   OR activation.accounting_rule_version <> 'small_enterprise_fixed_asset_straight_line_2013.1'
                   OR activation.accounting_rule_source_url <> 'https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852319878.pdf'
                   OR EXISTS (SELECT 1 FROM fixed_assets WHERE acquisition_event_id = target_event.id)"""
_ACTIVATION_SHAPE_NEW = """                   OR activation.accounting_rule_version <> 'small_enterprise_fixed_asset_straight_line_2013.1'
                   OR activation.accounting_rule_source_url <> 'https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852319878.pdf'
                   OR (
                       target_event.facts::jsonb ? 'depreciation_rounding_policy'
                       AND activation.depreciation_rounding_policy <>
                           target_event.facts::jsonb ->> 'depreciation_rounding_policy'
                   )
                   OR (
                       target_event.facts::jsonb ? 'depreciation_group_code'
                       AND COALESCE(activation.depreciation_group_code, '') <> COALESCE(
                           target_event.facts::jsonb ->> 'depreciation_group_code',
                           ''
                       )
                   )
                   OR EXISTS (SELECT 1 FROM fixed_assets WHERE acquisition_event_id = target_event.id)"""


def _function_definition(signature: str) -> str:
    definition = op.get_bind().scalar(
        sa.text("SELECT pg_get_functiondef(CAST(:signature AS regprocedure))"),
        {"signature": signature},
    )
    if not isinstance(definition, str):
        raise RuntimeError(f"GROUPED_DEPRECIATION_FUNCTION_NOT_FOUND:{signature}")
    return definition


def _replace_once(definition: str, old: str, new: str, *, signature: str) -> str:
    if new in definition:
        return definition
    if definition.count(old) != 1:
        raise RuntimeError(f"GROUPED_DEPRECIATION_FUNCTION_VERSION_MISMATCH:{signature}")
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
        (
            (_DECLARATIONS_OLD, _DECLARATIONS_NEW),
            (_BASE_CALCULATION_OLD, _BASE_CALCULATION_NEW),
        ),
        upgrade=upgrade,
    )
    for signature in (
        "finance_assert_fixed_asset_event_shape(uuid)",
        "finance_assert_fixed_asset_event_shape_0014(uuid)",
    ):
        _rewrite_function(
            signature,
            (
                (_READY_SHAPE_OLD, _READY_SHAPE_NEW),
                (_ACTIVATION_SHAPE_OLD, _ACTIVATION_SHAPE_NEW),
            ),
            upgrade=upgrade,
        )


def _assert_current_fixed_assets() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.get_bind().execute(
        sa.text(
            "SELECT finance_assert_fixed_asset_from_event(id) FROM business_events "
            "WHERE status IN ('posted','reversed') AND event_type LIKE 'fixed_asset_%'"
        )
    )


def upgrade() -> None:
    op.add_column(
        "fixed_asset_activations",
        sa.Column("depreciation_group_code", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "fixed_asset_activations",
        sa.Column(
            "depreciation_rounding_policy",
            sa.String(length=50),
            nullable=False,
            server_default=sa.text(f"'{_LEGACY_POLICY}'"),
        ),
    )
    with op.batch_alter_table("fixed_asset_activations") as batch:
        batch.create_check_constraint(
            "ck_asset_activation_rounding_policy",
            "depreciation_rounding_policy IN ('floor_final_remainder_v1','round_half_up_group_v1')",
        )
        batch.create_check_constraint(
            "ck_asset_activation_group_code",
            "depreciation_group_code IS NULL OR length(trim(depreciation_group_code)) > 0",
        )
        batch.alter_column(
            "depreciation_rounding_policy",
            existing_type=sa.String(length=50),
            nullable=False,
            server_default=sa.text(f"'{_GROUP_POLICY}'"),
        )
    op.create_index(
        "ix_fixed_asset_activation_org_group",
        "fixed_asset_activations",
        ["org_id", "depreciation_group_code"],
    )
    _rewrite_postgresql_functions(upgrade=True)
    _assert_current_fixed_assets()


def downgrade() -> None:
    unsafe = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM fixed_asset_activations "
            "WHERE depreciation_group_code IS NOT NULL "
            "OR depreciation_rounding_policy <> :legacy_policy)"
        ),
        {"legacy_policy": _LEGACY_POLICY},
    )
    if unsafe:
        raise RuntimeError("GROUPED_DEPRECIATION_DOWNGRADE_UNSAFE")
    _rewrite_postgresql_functions(upgrade=False)
    op.drop_index(
        "ix_fixed_asset_activation_org_group",
        table_name="fixed_asset_activations",
    )
    with op.batch_alter_table("fixed_asset_activations") as batch:
        batch.drop_constraint("ck_asset_activation_group_code", type_="check")
        batch.drop_constraint("ck_asset_activation_rounding_policy", type_="check")
        batch.drop_column("depreciation_rounding_policy")
        batch.drop_column("depreciation_group_code")
