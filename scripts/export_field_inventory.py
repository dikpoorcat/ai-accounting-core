"""Inspect canonical field sources in memory; write Markdown only to an explicit path."""

from __future__ import annotations

import argparse
import dataclasses
import inspect
import sqlite3
from pathlib import Path
from typing import get_args, get_type_hints

from pydantic import BaseModel, TypeAdapter

from ai_accounting.kernel.catalog import catalog_sql
from ai_accounting.kernel.contracts import BalanceEffect, Calculation, Line, Outcome
from ai_accounting.kernel.display import BusinessProfile
from ai_accounting.kernel.entities import entity_profile_models
from ai_accounting.kernel.fund_accounts import IDENTITY_FIELDS, fund_profile_models
from ai_accounting.kernel.schema import schema_sql, sequence_model, table_name
from ai_accounting.kernel.service import default_registry
from ai_accounting.kernel.work_draft_contract import WorkDraft

if __package__:
    from .field_meanings import SUPPLEMENTS
else:
    from field_meanings import SUPPLEMENTS

ROOT = Path(__file__).resolve().parents[1]
UNDECLARED = "未声明精确字段语义"
WIRE_CONTRACTS = (
    "src/ai_accounting/kernel/command_schema.py",
    "src/ai_accounting/kernel/response_contracts.py",
    "src/ai_accounting/kernel/work_context_contract.py",
)


@dataclasses.dataclass(frozen=True)
class Source:
    basis: str
    writer: str
    consumer: str
    references: tuple[str, ...]


def source(basis, writer, consumer, *modules):
    return Source(
        basis,
        writer,
        consumer,
        tuple("src/ai_accounting/kernel/" + module + ".py" for module in modules),
    )


# Reviewed lifecycle families, not a copy of table columns or business schemas.
SOURCES = {
    "fact": source(
        "精确类型化事实版本",
        "Store.write_fact／类型化登记",
        "注册计算器／来源核验",
        "storage",
        "integrity",
    ),
    "entity": source(
        "对象及管理档案修订", "Entities 登记／档案修改", "展示／对象核验", "entities", "display"
    ),
    "fund": source(
        "正式资金定义及明确身份更正链",
        "Entities 资金登记／身份更正",
        "资金展示／核算／身份核验",
        "fund_accounts",
        "entities",
    ),
    "business": source("业务补充档案修订", "Display.save_display_profile", "业务展示", "display"),
    "management": source(
        "明确登记的管理／清单修订",
        "所属类型化管理入口",
        "准备／展示／关账",
        "periods",
        "discovery",
        "exports",
    ),
    "calculation": source(
        "精确计算及采用历史",
        "Engine.confirm／更正／撤回",
        "核算／来源完整性核验",
        "engine",
        "integrity",
    ),
    "voucher": source(
        "采用计算生成的正式凭证", "正式发布事务", "报表／凭证核验", "engine", "integrity"
    ),
    "fund_detail": source(
        "精确正式行的资金明细及启用依据",
        "资金明细登记事务",
        "资金明细完整核验",
        "fund_line_details",
    ),
    "close": source(
        "不可变关账采用根及明细", "Periods.close", "历史／完整核验", "periods", "close_storage"
    ),
    "balance": source(
        "精确采用凭证／计算余额效果",
        "余额投影重建",
        "余额／报表读取",
        "projections",
        "period_balances",
    ),
    "settlement": source(
        "精确义务与清偿结果／冻结根", "清偿投影／冻结", "清偿读取及核验", "settlement_projection"
    ),
    "report": source(
        "采用凭证／计算／冻结报表依据",
        "报表投影或冻结事务",
        "报表读取及核验",
        "reports",
        "report_projection",
    ),
    "references": source(
        "不可变来源中实际存在的引用",
        "引用目录同步／重建",
        "引用定位与来源核验",
        "read_indexes",
        "entity_references",
    ),
    "discovery": source("实际事实版本及当前头", "查找索引重建", "业务查找", "discovery_indexes"),
    "model": source(
        "已验证当前／冻结来源快照", "读取模型生产", "实际页面命中及模型核验", "dashboard_read_model"
    ),
    "material_watch": source(
        "冻结清单与追加来源变化", "资料变更检测目录", "变更复核", "material_watch"
    ),
    "duplicate": source(
        "不可变重复复核依据", "重复审查／冻结事务", "重复采用核验", "duplicates", "duplicate_freeze"
    ),
    "asset_batch": source(
        "正式批量资产结果及成员", "资产批量确认", "批量采用与完整性核验", "asset_batches"
    ),
    "correction": source(
        "明确身份更正及精确旧来源", "身份更正事务", "更正链核验", "identity_corrections", "entities"
    ),
    "runtime": source(
        "持久请求／审计／任务／结构身份及运行版本",
        "所属原子运行事务",
        "幂等／恢复／结构及审计核验",
        "storage",
        "versions",
        "jobs",
    ),
    "change": source(
        "实际来源写入所追加的变化", "来源变化触发器", "变更检测及复核", "change_journal"
    ),
    "catalog": source(
        "公司目录及本地负责人安全生命周期",
        "目录／安全原子入口",
        "公司定位／负责人认证",
        "catalog",
        "security/schema",
    ),
    "draft": source("私有身份绑定工作稿", "save_work_draft", "读取／恢复工作稿", "work_drafts"),
}
TABLE_FAMILIES = {
    "fact": "subject evidence fact_revision fact_seal fact_evidence",
    "entity": "entity entity_profile_revision entity_resolution",
    "fund": "fund_account_definition fund_account_identity_revision",
    "business": "display_profile_revision",
    "management": (
        "company_note_revision management_revision material_revision material_item "
        "payee_revision period_commentary_revision"
    ),
    "calculation": (
        "calculation calculation_seal calculation_publication dependency_scope "
        "dependency_fact dependency_calculation disposition"
    ),
    "voucher": "voucher voucher_line voucher_version opening_account",
    "fund_detail": (
        "fund_line_detail opening_fund_line_detail fund_detail_activation fund_detail_opening"
    ),
    "close": (
        "period_close material_close_rule period_commentary_basis security_close_approval "
        "close_storage_block close_storage_directory close_storage_root close_storage_subroot"
    ),
    "balance": (
        "balance monthly_account monthly_cashflow period_balance period_balance_freeze_bucket "
        "period_balance_freeze_root period_balance_freeze_row period_balance_seal"
    ),
    "settlement": (
        "settlement_change settlement_freeze_block settlement_freeze_ref settlement_freeze_root "
        "settlement_projection_seal settlement_state_revision"
    ),
    "report": (
        "report_classification_directory report_classification_node report_line_source "
        "report_line_source_seal report_open_contribution report_open_contribution_anchor "
        "report_party_checkpoint report_party_checkpoint_seal report_party_delta "
        "report_party_month_seal report_period_flow report_semantic_line report_semantic_seal"
    ),
    "references": (
        "close_reference audit_reference job_reference read_index_source "
        "entity_reference_current entity_reference_recorded"
    ),
    "discovery": "discovery_fact_current discovery_fact_history",
    "model": "dashboard_read_model_block dashboard_read_model_commit dashboard_read_model_current",
    "material_watch": "material_watch_bucket material_watch_directory material_watch_root",
    "duplicate": (
        "business_duplicate_check duplicate_freeze_bucket duplicate_freeze_directory "
        "duplicate_freeze_root"
    ),
    "asset_batch": "asset_batch_member",
    "correction": "identity_correction identity_correction_item",
    "runtime": (
        "identity audit request jobs state schema_meta schema_history schema_draft_history "
        "pending fact_current fact_scope calculation_current calculation_scope voucher_current"
    ),
    "change": "source_change source_change_head",
}
CATALOG_TABLES = frozenset(
    "catalog_identity company company_operation company_setting schema_history schema_meta "
    "security_audit security_owner security_recovery security_session".split()
)


def model_key(model):
    return model.__module__ + "." + model.__qualname__


def nested_models(annotation):
    if inspect.isclass(annotation) and (
        issubclass(annotation, BaseModel) or dataclasses.is_dataclass(annotation)
    ):
        yield annotation
    else:
        for argument in get_args(annotation):
            yield from nested_models(argument)


def own_semantic(field):
    """Only this property's declaration, never its parent model or spelling."""
    metadata = field.get("x-accounting-fact") or {}
    return field.get("description") or metadata.get("meaning")


def model_properties(schema):
    """Resolve only the canonical root, including recursive models' local references."""
    root, seen = schema, set()
    while "$ref" in root:
        reference = root["$ref"]
        if not reference.startswith("#/") or reference in seen:
            raise ValueError("无法解析规范模型引用：" + reference)
        seen.add(reference)
        root = schema
        for part in reference[2:].split("/"):
            root = root[part.replace("~1", "/").replace("~0", "~")]
    return root.get("properties", {})


def source_for_table(database, name, typed_tables):
    if database == "catalog":
        return SOURCES["catalog"] if name in CATALOG_TABLES else None
    if database != "company":
        return None
    if name in typed_tables:
        return typed_tables[name]
    for family, tables in TABLE_FAMILIES.items():
        if name in tables.split():
            return SOURCES[family]
    return None


def fact_source(kind, model, registry):
    evaluator = registry.evaluators.get(kind)
    consumer = model_key(evaluator) if evaluator is not None else "注册事实的准备及来源核验"
    module = Path(inspect.getsourcefile(model)).resolve().relative_to(ROOT).as_posix()
    return Source(
        "精确事实版本：" + kind,
        model.registration_command or "save_fact／amend_fact／save_facts",
        consumer,
        (module, "src/ai_accounting/kernel/storage.py"),
    )


def registered_models(registry):
    """Canonical roots and actual nested classes, without traversing wire DTOs."""
    roots = [(model, fact_source(kind, model, registry)) for kind, model in registry.models.items()]
    roots += [(model, SOURCES["entity"]) for model in entity_profile_models().values()]
    roots += [(model, SOURCES["fund"]) for model in fund_profile_models().values()]
    roots += [(BusinessProfile, SOURCES["business"]), (WorkDraft, SOURCES["draft"])]
    roots += [
        (model, SOURCES["calculation"]) for model in (Outcome, Calculation, Line, BalanceEffect)
    ]
    return roots


def build_inventory(*, registry=None, roots=None, scripts=None, supplements=None):
    registry = registry if registry is not None else default_registry()
    roots = registered_models(registry) if roots is None else roots
    scripts = (
        [("company", schema_sql(registry)), ("catalog", catalog_sql())]
        if scripts is None
        else scripts
    )
    supplements = SUPPLEMENTS if supplements is None else supplements
    models, contexts = {}, {}

    def visit(model, policy):
        key = model_key(model)
        if policy in contexts.setdefault(key, []):
            return
        models[key] = model
        contexts[key].append(policy)
        annotations = (
            {name: field.annotation for name, field in model.model_fields.items()}
            if issubclass(model, BaseModel)
            else get_type_hints(model)
        )
        for annotation in annotations.values():
            for child in nested_models(annotation):
                visit(child, policy)

    for model, policy in roots:
        visit(model, policy)
    fields, known, missing, errors = [], set(), [], []
    for key, model in sorted(models.items()):
        schema = TypeAdapter(model).json_schema()
        declaration = Path(inspect.getsourcefile(model)).resolve()
        declaration = (
            declaration.relative_to(ROOT).as_posix()
            if declaration.is_relative_to(ROOT)
            else str(declaration)
        )
        for name, field in sorted(model_properties(schema).items()):
            exact = key + "." + name
            known.add(exact)
            meaning = own_semantic(field) or supplements.get(exact)
            if not meaning:
                missing.append(exact)
            policies = contexts[key]
            for kind, fund_model in fund_profile_models().items():
                if model is fund_model and name not in IDENTITY_FIELDS[kind]:
                    policies = [SOURCES["entity"]]
            fields.append(
                {
                    "key": exact,
                    "meaning": meaning or UNDECLARED,
                    "declaration": declaration,
                    "sources": policies,
                }
            )
    for exact in sorted(supplements.keys() - known):
        errors.append("失效语义补充：" + exact)
    typed_tables = {}
    for kind, model in registry.models.items():
        table = table_name(kind)
        policy = fact_source(kind, model, registry)
        typed_tables[table] = policy
        for name, field in model.model_fields.items():
            if sequence_model(field.annotation) is not None:
                typed_tables[table + "_" + name] = policy
    storage, tables = [], 0
    for database, ddl in scripts:
        with sqlite3.connect(":memory:") as connection:
            connection.executescript(ddl)
            for (name,) in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ):
                tables += 1
                policy = source_for_table(database, name, typed_tables)
                if policy is None:
                    errors.append("缺少来源归属：" + database + "." + name)
                for column in connection.execute(f'PRAGMA table_xinfo("{name}")'):
                    storage.append(
                        {
                            "key": database + "." + name + "." + column[1],
                            "type": column[2],
                            "source": policy,
                        }
                    )
    references = set(WIRE_CONTRACTS)
    for field in fields:
        for policy in field["sources"]:
            references.update(policy.references)
    for column in storage:
        if column["source"] is not None:
            references.update(column["source"].references)
    errors += [
        "失效源码引用：" + path for path in sorted(references) if not (ROOT / path).is_file()
    ]
    return {
        "fields": fields,
        "storage": storage,
        "missing_semantics": missing,
        "errors": errors,
        "counts": {
            "models": len(models),
            "tables": tables,
            "storage_columns": len(storage),
            "model_fields": len(fields),
            "missing_semantics": len(missing),
        },
    }


def markdown(inventory):
    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", " ")

    lines = [
        "# 按需字段来源检查",
        "",
        "来自实际模型和内存 DDL；没有读取业务数据库。",
        "未声明的字段语义保留为未声明，不按同名推断。",
        "",
        "接口合同直接参见：" + "、".join(f"`{path}`" for path in WIRE_CONTRACTS),
        "",
        "## 规范模型字段",
        "",
        "| 字段 | 精确语义 | 来源／登记／消费 | 声明 |",
        "| --- | --- | --- | --- |",
    ]
    for field in inventory["fields"]:
        policies = "；".join(f"{p.basis}／{p.writer}／{p.consumer}" for p in field["sources"])
        lines.append(
            "| "
            + " | ".join(
                map(cell, (field["key"], field["meaning"], policies, field["declaration"]))
            )
            + " |"
        )
    lines += [
        "",
        "## 实际持久字段",
        "",
        "| 字段 | 类型 | 来源／登记／消费 |",
        "| --- | --- | --- |",
    ]
    for column in inventory["storage"]:
        p = column["source"]
        policy = f"{p.basis}／{p.writer}／{p.consumer}" if p else "未声明来源"
        lines.append("| " + " | ".join(map(cell, (column["key"], column["type"], policy))) + " |")
    lines += ["", "## 检查结果", "", str(inventory["counts"]), ""]
    lines += ["- " + error for error in inventory["errors"]]
    lines += ["- 缺少精确语义：`" + key + "`" for key in inventory["missing_semantics"]]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="只读检查（默认）")
    mode.add_argument("--output", type=Path, help="向明确位置输出可读 Markdown")
    args = parser.parse_args(argv)
    if args.output is not None and args.output.suffix.lower() != ".md":
        parser.error("--output 必须明确指定 Markdown (.md) 文件")
    inventory = build_inventory()
    if args.output is not None:
        args.output.write_text(markdown(inventory), encoding="utf-8")
    print(inventory["counts"])
    for issue in inventory["errors"]:
        print(issue)
    for key in inventory["missing_semantics"]:
        print("缺少精确语义：" + key)
    return 1 if inventory["errors"] or inventory["missing_semantics"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
