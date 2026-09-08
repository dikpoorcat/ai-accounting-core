"""Supplier advances and traceable intangible project costs.

Revision ID: 0002_purchase_projects
Revises: 0001_business_baseline_v3
"""

import uuid
from pathlib import Path

import sqlalchemy as sa

from alembic import op

revision = "0002_purchase_projects"
down_revision = "0001_business_baseline_v3"
branch_labels = None
depends_on = None


def _replace_function(name, before, after):
    bind = op.get_bind()
    definition = bind.scalar(
        sa.text("SELECT pg_get_functiondef(CAST(:name AS regprocedure))"),
        {"name": f"public.{name}(uuid)"},
    )
    if definition is None or definition.count(before) != 1:
        raise RuntimeError(f"PURCHASE_MIGRATION_UNEXPECTED_FUNCTION:{name}")
    with bind.connection.driver_connection.cursor() as cursor:
        cursor.execute(definition.replace(before, after))


def upgrade():
    bind = op.get_bind()
    with op.batch_alter_table("intangible_assets") as batch:
        batch.drop_constraint("ck_intangible_asset_settlement_method", type_="check")
        batch.drop_constraint("ck_intangible_asset_settlement_dates", type_="check")
        batch.create_check_constraint(
            "ck_intangible_asset_settlement_method",
            "settlement_method IN ('bank','payable','project_cost')",
        )
        batch.create_check_constraint(
            "ck_intangible_asset_settlement_dates",
            "(settlement_method = 'bank' AND payment_date IS NOT NULL AND due_date IS NULL) OR "
            "(settlement_method = 'payable' AND payment_date IS NULL AND due_date IS NOT NULL) OR "
            "(settlement_method = 'project_cost' AND payment_date IS NULL AND due_date IS NULL)",
        )
    accounts = sa.Table("accounts", sa.MetaData(), autoload_with=bind)
    for org_id in bind.scalars(sa.text("SELECT id FROM organizations")):
        for code, name, role in (
            ("1123", "预付账款", "prepayments"),
            ("189901", "其他非流动资产—外购无形资产项目成本", "intangible_project_cost"),
            ("4301", "研发支出—资本化支出", "development_expenditure"),
        ):
            existing = (
                bind.execute(
                    sa.select(accounts).where(
                        accounts.c.org_id == org_id,
                        sa.or_(accounts.c.code == code, accounts.c.system_role == role),
                    )
                )
                .mappings()
                .all()
            )
            if existing:
                if (
                    len(existing) != 1
                    or existing[0]["system_role"] != role
                    or existing[0]["business_class"] != role
                ):
                    raise RuntimeError(f"PURCHASE_ACCOUNT_CODE_CONFLICT:{code}")
                continue
            bind.execute(
                accounts.insert().values(
                    id=uuid.uuid4() if bind.dialect.name == "postgresql" else uuid.uuid4().hex,
                    org_id=org_id,
                    code=code,
                    name=name,
                    category="asset",
                    normal_side="debit",
                    system_role=role,
                    business_class=role,
                    active=True,
                    requires_bank_reconciliation=False,
                )
            )
    if bind.dialect.name == "postgresql":
        sql_path = Path(__file__).resolve().parents[1] / "sql" / "0002_purchase_projects.sql"
        with bind.connection.driver_connection.cursor() as cursor:
            cursor.execute(sql_path.read_text(encoding="utf-8"))
        _replace_function(
            "finance_assert_component_semantics",
            "    WHEN 'expense' THEN",
            "    WHEN 'supplier_advance','supplier_advance_application',\n"
            "         'supplier_advance_refund',\n"
            "         'project_cost','project_cost_expense' THEN\n"
            "        PERFORM finance_assert_purchase_component(c.id); RETURN;\n"
            "    WHEN 'expense' THEN",
        )
        _replace_function(
            "finance_assert_component_semantics",
            "allowed_classes := ARRAY['intangible_asset_cost','accounts_payable'];",
            "allowed_classes := ARRAY['intangible_asset_cost','accounts_payable',"
            "'intangible_project_cost','development_expenditure'];\n"
            "        IF c.facts->>'settlement_method'='project_cost' THEN\n"
            "            PERFORM finance_assert_purchase_component(c.id);\n"
            "        ELSIF EXISTS (SELECT 1 FROM voucher_lines l\n"
            "            JOIN accounts a ON a.id=l.account_id\n"
            "            WHERE l.component_id=c.id AND coalesce(a.business_class,a.system_role)\n"
            "                IN ('intangible_project_cost','development_expenditure')) THEN\n"
            "            RAISE EXCEPTION 'PROJECT_COST_SOURCES_REQUIRED'; END IF;",
        )
        source_comment = (
            "        -- Source accounts are normalized on every obligation, "
            "including detail accounts."
        )
        _replace_function(
            "finance_assert_component_semantics",
            source_comment,
            source_comment + "\n"
            "        IF c.kind='receivable_settlement' AND EXISTS(\n"
            "            SELECT 1 FROM settlements s JOIN open_items i ON i.id=s.open_item_id\n"
            "            JOIN accounts a ON a.id=i.account_id WHERE s.payment_component_id=c.id\n"
            "              AND coalesce(a.business_class,a.system_role)='prepayments') THEN\n"
            "            RAISE EXCEPTION 'SUPPLIER_ADVANCE_REQUIRES_TYPED_APPLICATION_OR_REFUND';\n"
            "        END IF;",
        )


def downgrade():
    raise RuntimeError("PURCHASE_PROJECTS_FORWARD_ONLY")
