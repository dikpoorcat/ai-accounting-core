"""离线检查本设计包。只读设计文件；SQLite 检查只用 :memory:。

这不是产品实现、业务验收或性能测试。算账预期仍须在未来真实模块集成中验证。
运行：.\\.tmp-kernel-venv\\Scripts\\python.exe docs/redesign/verify_design.py
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
from urllib.parse import unquote

from jsonschema import Draft202012Validator, FormatChecker, ValidationError, validators
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

ROOT = Path(__file__).resolve().parent
sys.stdout.reconfigure(encoding="utf-8")
COUNTS: dict[str, int] = {}


def load(path: Path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"重复 JSON 键：{path}: {key}")
            result[key] = value
        return result
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)


def integer_bound(validator, bound, instance, schema):
    if isinstance(instance, str) and re.fullmatch(r"0|-?[1-9][0-9]*", instance):
        value = int(instance)
        minimum = schema.get("x-integer-minimum")
        maximum = schema.get("x-integer-maximum")
        if minimum is not None and value < int(minimum) or maximum is not None and value > int(maximum):
            yield ValidationError("整数分超出合同的 int64 范围")


Validator = validators.extend(Draft202012Validator, {"x-integer-minimum": integer_bound})
SCHEMAS = {p.name: load(p) for p in sorted((ROOT / "contracts").glob("*.json"))}
registry = Registry()
for name, schema in SCHEMAS.items():
    if name == "dashboard-response-v2.json":
        for namespace, child in schema.items():
            registry = registry.with_resource("https://ai-accounting.local/design/dashboard/" + namespace, Resource.from_contents(child, default_specification=DRAFT202012))
        continue
    resource = Resource.from_contents(schema, default_specification=DRAFT202012)
    uris = [(ROOT / "contracts" / name).as_uri(), "https://ai-accounting.local/design/" + name]
    if "$id" in schema:
        uris.append(schema["$id"])
    for uri in set(uris):
        registry = registry.with_resource(uri, resource)


def walk(value):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def validator(name, definition):
    uri = "https://ai-accounting.local/design/" + name + "#/$defs/" + definition
    return Validator({"$ref": uri}, registry=registry, format_checker=FormatChecker())


def changed_example(value, replacements, removals):
    changed = deepcopy(value)
    for pointer, replace, replacement in [(key, True, value) for key, value in replacements.items()] + [(key, False, None) for key in removals]:
        assert pointer.startswith("/"), ("非 JSON pointer", pointer)
        keys = [key.replace("~1", "/").replace("~0", "~") for key in pointer[1:].split("/")]
        parent = changed
        for key in keys[:-1]:
            parent = parent[int(key)] if isinstance(parent, list) else parent[key]
        last = int(keys[-1]) if isinstance(parent, list) else keys[-1]
        if replace:
            parent[last] = replacement
        else:
            del parent[last]
    return changed


def check_contracts():
    count = 0
    for name, schema in SCHEMAS.items():
        documents = [(name, schema, schema.get("$id", (ROOT / "contracts" / name).as_uri()))]
        if name == "dashboard-response-v2.json":
            documents = [(key, child, "https://ai-accounting.local/design/dashboard/" + key) for key, child in schema.items()]
        for label, child, base in documents:
            validators.validator_for(child).check_schema(child)
            resolver = registry.resolver(base)
            for node in walk(child):
                if isinstance(node, dict) and "$ref" in node:
                    try:
                        resolver.lookup(node["$ref"])
                    except Exception:
                        raise AssertionError(("合同引用无目标", name, label, node["$ref"])) from None
                    count += 1
    COUNTS["schema_files"] = len(SCHEMAS)
    COUNTS["resolved_refs"] = count
    fen = validator("core.schema.json", "Fen")
    for value in ["0", "-1", str(-(2**63)), str(2**63 - 1)]:
        fen.validate(value)
    for value in [0, True, "01", "-0", "1.00", str(2**63), str(-(2**63)-1)]:
        assert not fen.is_valid(value), ("金额反例未拒绝", value)
    date = validator("core.schema.json", "Date")
    assert not date.is_valid("2026-02-30")
    manifest = ROOT / "contracts/dashboard-response-v2.json"
    # Git 在 Windows 检出时可转换为 CRLF；冻结合同内容，忽略行尾表示差异。
    frozen_bytes = manifest.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(frozen_bytes).hexdigest() == "ade6c0319c137511110464762a7a4ac0702c5a7e9ec017f7a79d89380009db2d", "冻结看板合同被修改"


def check_examples():
    fact = validator("business-facts.schema.json", "Fact")
    apply = validator("core.schema.json", "ApplyRequest")
    valid_count = invalid_count = 0
    known_blocks = {
        "core-acceptance.json": {"format", "scope", "amount_unit", "runtime_contract", "company", "ids", "sources", "objects", "requests", "cases", "clock"},
        "business-facts-cases.json": {"format", "purpose", "execution", "sources", "object_ids", "valid_cases", "negative_cases", "investment_lifecycle"},
        "reporting-cases.json": {"format", "synthetic_only", "instructions", "cases", "contract_examples"},
        "review-materials-cases.json": {"format", "synthetic_only", "examples", "negative_cases", "integration_checks"},
        "company-note-cases.json": {"format", "status", "description", "schema", "valid", "invalid", "checks"},
        "file-cases.json": {"format", "synthetic_only", "scope", "schema_map", "contract_examples", "cases", "negative_cases"},
    }
    def validate_examples(name, definition, values):
        nonlocal valid_count
        for value in values:
            validator(name, definition).validate(value)
            valid_count += 1
    for path in sorted((ROOT / "examples").glob("*.json")):
        data = load(path)
        assert path.name in known_blocks, ("未声明的样例文件；须明确逐块校验规则", path.name)
        assert set(data) <= known_blocks[path.name], ("未分类的样例块", path.name, set(data) - known_blocks[path.name])
        # 金额/日期按严格 Schema 检查，案例自己的核算预期不是已通过的产品计算。
        if path.name == "core-acceptance.json":
            validate_examples("core.schema.json", "Object", data["objects"])
            validate_examples("core.schema.json", "Source", data["sources"])
            for name, request in data["requests"].items():
                try:
                    apply.validate(request)
                except ValidationError as error:
                    raise AssertionError((path.name, name, error.message)) from error
                valid_count += 1
        elif path.name == "business-facts-cases.json":
            validate_examples("core.schema.json", "Source", data["sources"])
            for case in data["valid_cases"]:
                fact.validate(case["when"])
                valid_count += 1
            for case in data["negative_cases"]:
                if case["then"]["schema"] == "invalid":
                    assert not fact.is_valid(case["when"]), case["case_id"]
                    invalid_count += 1
                else:
                    fact.validate(case["when"])
            lifecycle = data["investment_lifecycle"]
            lifecycle_blocks = {"format", "scope", "runtime_contract", "amount_unit", "ids", "synthetic_source", "synthetic_objects", "cases", "negative_cases", "required_integration_checks", "company", "bootstrap_facts", "request_construction", "plan_examples"}
            assert set(lifecycle) == lifecycle_blocks, ("未分类的投资生命周期样例块", set(lifecycle) ^ lifecycle_blocks)
            validate_examples("core.schema.json", "Object", lifecycle["synthetic_objects"])
            validate_examples("core.schema.json", "Source", [lifecycle["synthetic_source"]])
            plans = lifecycle["plan_examples"]
            plan_names = {"company_funding", "agent_collection", "february_position_before_operator_topup"}
            assert set(plans) == plan_names | {"scope", "checks"}, ("未分类的合作计划样例", set(plans))
            validate_examples("business-facts.schema.json", "PostingPlan", [plans[key] for key in sorted(plan_names)])
            by_id = {case["id"]: case for case in lifecycle["cases"]}
            for f in lifecycle["bootstrap_facts"]:
                fact.validate(f)
                valid_count += 1
            for case in lifecycle["cases"]:
                if "given_public_operations_result" in case:
                    validate_examples("business-facts.schema.json", "ResultTarget", [case["given_public_operations_result"]])
                    validate_examples("business-facts.schema.json", "Fact", [case["given_vat_policy_fact"]])
                    assert "steps" not in case, (case["id"], "公开结果夹具与实际步骤须明确区分")
                else:
                    assert "steps" in case and case["steps"], (case["id"], "没有声明可校验的事实或公开结果夹具")
                for step in case.get("steps", []):
                    if "fact" in step:
                        fact.validate(step["fact"])
                        if step["fact"]["kind"] == "investment.cooperation_contract":
                            contract = step["fact"]
                            assert contract["data"]["contract_source"]["source_id"] in contract["evidence_ids"], (case["id"], "合同来源不属于已提交证据")
                        valid_count += 1
                    else:
                        assert "check_only" in step and "expect_position" in step, (case["id"], "无事实步骤必须明确只核对什么")
            for case in lifecycle["negative_cases"]:
                if "base" not in case:
                    assert case["validation_layer"] == "business" and "change" in case, (case["id"], "Schema 反例必须有实际可构造输入")
                    continue  # 纯业务语义反例留给真实模块验收，不能冒充 Schema 测试。
                match = re.fullmatch(r"(CO\d+)\.steps\[(\d+)\]\.fact", case["base"])
                assert match, (case["id"], case["base"])
                changed = changed_example(by_id[match[1]]["steps"][int(match[2])]["fact"], case.get("set", {}), case.get("remove", []))
                if case.get("validation_layer") == "schema" or case["expect"].startswith("schema_invalid"):
                    assert not fact.is_valid(changed), case["id"]
                    invalid_count += 1
                else:
                    assert case["validation_layer"] == "business", case["id"]
                    fact.validate(changed)
        elif path.name == "reporting-cases.json":
            for case in data["cases"]:
                if "expected_snapshot" in case:
                    validator("reporting.schema.json", "FinancialSnapshot").validate(case["expected_snapshot"])
                    valid_count += 1
            for example in data["contract_examples"]:
                assert set(example) == {"schema_ref", "value"}, ("未分类的报表合同样例", set(example))
                definition = example["schema_ref"].removeprefix("reporting.schema.json#/$defs/")
                assert definition in {"FinancialSnapshot", "CashflowContribution"}, example["schema_ref"]
                validate_examples("reporting.schema.json", definition, [example["value"]])
        elif path.name == "review-materials-cases.json":
            mapping = {"current_query": "ReviewMaterialsQuery", "frozen_query": "ReviewMaterialsQuery", "response": "ReviewMaterialsResponse", "content_query": "SourceContentQuery", "statement_content": "SourceTextContent", "file_receipt": "SourceFileReceipt"}
            assert set(data["examples"]) == set(mapping), ("未分类的来源查阅样例", set(data["examples"]))
            for key, definition in mapping.items():
                validate_examples("review-materials.schema.json", definition, [data["examples"][key]])
            for case in data["negative_cases"]:
                assert case["definition"] in mapping.values(), case["definition"]
                assert not validator("review-materials.schema.json", case["definition"]).is_valid(case["value"]), case["id"]
                invalid_count += 1
            examples = data["examples"]
            statement = examples["statement_content"]
            receipt = examples["file_receipt"]
            file_bytes = b"synthetic-file-v1\n"  # 样例中明确给出的原件字节，不是产品核算。
            assert receipt["sha256"] == hashlib.sha256(file_bytes).hexdigest()
            assert receipt["byte_size"] == len(file_bytes)
            by_source = {item["source_id"]: item for item in examples["response"]["sources"]}
            for value, size in [(statement, len(statement["text"].encode("utf-8"))), (receipt, receipt["byte_size"])]:
                item = by_source[value["source_id"]]
                assert item["sha256"] == value["sha256"] and item["byte_size"] == size
        elif path.name == "company-note-cases.json":
            assert data["status"] == "planned", "设计样例不能冒充产品验收结果"
            assert data["schema"] == "company-note.schema.json"
            for case in data["valid"]:
                validate_examples(case.get("schema", data["schema"]), case["definition"], [case["value"]])
            for case in data["invalid"]:
                assert not validator(case.get("schema", data["schema"]), case["definition"]).is_valid(case["value"]), case["name"]
                invalid_count += 1
            identifiers = [case["id"] for case in data["checks"]]
            assert len(set(identifiers)) == len(identifiers)
            assert all(case["given"] and case["action"] and case["expect"] for case in data["checks"])
            COUNTS["company_note_planned_checks"] = len(identifiers)
        elif path.name == "file-cases.json":
            mapping = {"backup_plan": "FilePlan", "report_export_plan": "FilePlan", "restore_check_request": "IsolatedTargetRequest", "restore_run_request": "IsolatedTargetRequest", "rebuild_run_request": "IsolatedTargetRequest", "restore_checked_result": "IsolatedTargetResult", "restore_created_result": "IsolatedTargetResult", "rebuild_created_result": "IsolatedTargetResult", "missing_source_failed_result": "IsolatedTargetResult"}
            assert data["schema_map"] == mapping, ("未分类的文件样例映射", data["schema_map"])
            assert set(data["contract_examples"]) == set(mapping), ("未分类的文件合同样例", set(data["contract_examples"]))
            for key, definition in mapping.items():
                validate_examples("core.schema.json", definition, [data["contract_examples"][key]])
            for case in data["negative_cases"]:
                assert case["definition"] == mapping[case["base_example"]], case["id"]
                changed = changed_example(data["contract_examples"][case["base_example"]], case.get("patch", {}), case.get("remove", []))
                if case["expect"] == "schema_rejected":
                    assert not validator("core.schema.json", case["definition"]).is_valid(changed), case["id"]
                    invalid_count += 1
                else:
                    assert case["expect"] == "semantic_rejected", case["id"]
                    validator("core.schema.json", case["definition"]).validate(changed)
                    # 路径/包重复等语义反例留给未来真实模块，不计作已经拒绝。
        for node in walk(data):
            if isinstance(node, dict) and node.get("kind") == "owner_statement" and "sha256" in node and "text" in node:
                assert hashlib.sha256(node["text"].encode("utf-8")).hexdigest() == node["sha256"], path.name
    COUNTS["valid_schema_examples"] = valid_count
    COUNTS["rejected_schema_examples"] = invalid_count


def check_sql():
    db = sqlite3.connect(":memory:")
    db.executescript((ROOT / "schema/company.sql").read_text(encoding="utf-8"))
    COUNTS["company_tables"] = db.execute("SELECT count(*) FROM sqlite_schema WHERE type='table'").fetchone()[0]
    COUNTS["company_triggers"] = db.execute("SELECT count(*) FROM sqlite_schema WHERE type='trigger'").fetchone()[0]
    digest = "0" * 64
    uid = lambda n: f"00000000-0000-4000-8000-{n:012d}"
    def insert(table, **fields):
        keys = ",".join(fields)
        db.execute(f"INSERT INTO {table}({keys}) VALUES ({','.join('?' for _ in fields)})", tuple(fields.values()))
    def rejected(statement, params=(), reason="frozen_result"):
        db.execute("SAVEPOINT expected_rejection")
        try:
            db.execute(statement, params)
        except sqlite3.IntegrityError as error:
            assert str(error) == reason, ("DDL 拒绝原因错误，不能用外键/唯一键碰巧代替保护", statement, reason, str(error))
        else:
            raise AssertionError("DDL 应拒绝但已接受：" + statement)
        finally:
            db.execute("ROLLBACK TO expected_rejection")
            db.execute("RELEASE expected_rejection")
    insert("company_profile", singleton=1, company_id=uid(30), credit_code="000000000000000001", legal_name="合成公司", accounting_standard="small_enterprise", currency="CNY", start_period="2026-01")
    insert("identity_object", object_id=uid(1), kind="organization", name="合成方", sort_key="a", seq=1, data_json="{}", content_digest=digest, latest_expected_seq=0, latest_command_id=uid(2))
    insert("business_head", business_id=uid(3), family="operations", kind="operations.expense", result_slots_json='["main"]', seq=1, state="active", period="2026-01", archive_month="2026-01", current_digest=digest, latest_op="create", latest_expected_seq=0, latest_command_id=uid(2), latest_content_digest=digest, search_revision=1)
    result = dict(result_id=uid(4), business_id=uid(3), result_slot="main", generation=1, business_period="2026-01", posting_period="2026-01", state="open", target_state="active", adopted_fact_seq=1, calculation_key="operations.expense", calculation_revision=1, basis_schema="guard_fixture", basis_json="{}", target_schema="guard_fixture", target_json="{}", basis_digest=digest, target_digest=digest, formal_output_digest=digest)
    # 以下是 DDL 保护测试的最小占位 JSON，不宣称是业务 Schema 合法 PostingPlan。
    insert("adopted_result", **result)
    insert("voucher", voucher_id=uid(5), result_id=uid(4), role="original", period="2026-01", number=1, summary="合成保护检查")
    insert("voucher_line", voucher_id=uid(5), line_no=1, account_code="5602", debit_fen=100, credit_fen=0, summary="借")
    rejected("UPDATE voucher SET sealed=1 WHERE voucher_id=?", (uid(5),), "unbalanced_voucher")
    insert("voucher_line", voucher_id=uid(5), line_no=2, account_code="2202", debit_fen=0, credit_fen=100, summary="贷")
    db.execute("UPDATE voucher SET sealed=1 WHERE voucher_id=?", (uid(5),))
    rejected("UPDATE voucher_line SET debit_fen=101 WHERE voucher_id=? AND line_no=1", (uid(5),), "sealed_voucher")
    insert("source", source_id=uid(7), kind="owner_statement", sha256=digest, statement_text="合成")
    insert("money_occurrence", occurrence_id=uid(8), business_id=uid(3), public_slot="funds")
    children = {
        "result_source": dict(result_id=uid(4), source_id=uid(7), location="第1行"),
        "result_basis_ref": dict(result_id=uid(4), item_no=1, producer_business_id=uid(3), producer_result_slot="main", producer_result_id=uid(4), producer_generation=1, producer_content_digest=digest),
        "obligation": dict(result_id=uid(4), public_slot="primary", party_id=uid(1), direction="payable", category="trade_payable", amount_fen=100, account_code="2202", allowed_settlements_json='["money"]', business_period="2026-01"),
        "settlement": dict(result_id=uid(4), settlement_id=uid(9), source_business_id=uid(3), source_public_slot="primary", consumer_public_slot="payment", period="2026-01", amount_fen=10, method="money", movement_id=uid(8)),
        "obligation_reclassification": dict(result_id=uid(4), relation_id=uid(10), mode="transfer", source_business_id=uid(3), source_public_slot="primary", target_business_id=uid(3), target_public_slot="recovery", amount_fen=10),
        "cost_lot": dict(result_id=uid(4), public_slot="cost", object_id=uid(1), category="project", amount_fen=100, business_period="2026-01"),
        "cost_consumption": dict(result_id=uid(4), consumer_public_slot="expense", source_business_id=uid(3), source_public_slot="cost", period="2026-01", amount_fen=10),
        "result_fund_movement": dict(result_id=uid(4), movement_id=uid(8), public_slot="funds", account_id=uid(1), actual_on="2026-01-01", period="2026-01", direction="out", amount_fen=10),
        "result_tax_sale": dict(result_id=uid(4), item_no=1, source_business_id=uid(3), tax_period="2026-01", net_sales_fen=100, vat_fen=0, nominal_rate="0.03", tax_base_rate="0.03", payable_rate="0.01", exemption_eligible=1, filing_bucket="standard", invoice_kind="ordinary", ranges_json="[]"),
        "result_report_contribution": dict(result_id=uid(4), statement="profit", line=12, detail_code="management_other", amount_fen=100),
    }
    for table, fields in children.items():
        insert(table, **fields)
    db.execute("UPDATE adopted_result SET state='frozen' WHERE result_id=?", (uid(4),))
    rejected("UPDATE adopted_result SET generation=2 WHERE result_id=?", (uid(4),))
    rejected("DELETE FROM adopted_result WHERE result_id=?", (uid(4),))
    for table, fields in children.items():
        rejected(f"INSERT INTO {table}({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})", tuple(fields.values()))
        update_column = next(key for key in fields if key != "result_id")
        rejected(f"UPDATE {table} SET {update_column}={update_column} WHERE result_id=?", (uid(4),))
        rejected(f"DELETE FROM {table} WHERE result_id=?", (uid(4),))
    rejected("INSERT INTO voucher(voucher_id,result_id,role,period,number,summary) VALUES (?,?,?, ?,?,?)", (uid(11), uid(4), "replacement", "2026-01", 2, "冻结插入"))
    rejected("UPDATE voucher SET summary='冻结修改' WHERE voucher_id=?", (uid(5),))
    rejected("DELETE FROM voucher WHERE voucher_id=?", (uid(5),))
    rejected("INSERT INTO voucher_line(voucher_id,line_no,account_code,debit_fen,credit_fen,summary) VALUES (?,3,'5602',1,0,'冻结插入')", (uid(5),), "sealed_voucher")
    rejected("DELETE FROM voucher_line WHERE voucher_id=? AND line_no=1", (uid(5),), "sealed_voucher")
    insert("period_close", period="2026-01", result_head=digest, frozen_at="2026-02-01T00:00:00Z", basis_schema="guard_fixture", basis_json="{}", report_schema="guard_fixture", report_json="{}", report_digest=digest)
    rejected("DELETE FROM period_close WHERE period='2026-01'", reason="closed_period")
    rejected("UPDATE period_close SET result_head='x' WHERE period='2026-01'", reason="closed_period")
    result.update(result_id=uid(6))
    rejected("INSERT INTO adopted_result(" + ",".join(result) + ") VALUES (" + ",".join("?" for _ in result) + ")", tuple(result.values()), "closed_period")
    db.execute("SAVEPOINT covered_month_guard")
    insert("period_close", period="2026-03", result_head=digest, frozen_at="2026-04-01T00:00:00Z", basis_schema="guard_fixture", basis_json="{}", report_schema="guard_fixture", report_json="{}", report_digest=digest)
    covered = dict(result, business_period="2026-02", posting_period="2026-02")
    rejected("INSERT INTO adopted_result(" + ",".join(covered) + ") VALUES (" + ",".join("?" for _ in covered) + ")", tuple(covered.values()), "closed_period")
    insert("adopted_result", **dict(result, business_period="2026-04", posting_period="2026-04"))
    rejected("UPDATE adopted_result SET posting_period='2026-02' WHERE result_id=?", (uid(6),), "closed_period")
    db.execute("ROLLBACK TO covered_month_guard")
    db.execute("RELEASE covered_month_guard")
    rejected("UPDATE source SET statement_text='另一内容' WHERE source_id=?", (uid(7),), "immutable_source_content")
    rejected("UPDATE source SET sha256=? WHERE source_id=?", ("1" * 64, uid(7)), "immutable_source_content")
    rejected("UPDATE money_occurrence SET public_slot='other' WHERE occurrence_id=?", (uid(8),), "immutable_money_identity")
    # 整单出错必须由调用者回滚：ABORT 只撤销失败语句，不替它证明前面的写入已撤销。
    def rows():
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name")]
        return {table: db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() for table in tables}
    before = rows()
    db.execute("SAVEPOINT atomic_unit")
    try:
        new_result = dict(result, result_id=uid(20), business_period="2026-02", posting_period="2026-02")
        insert("adopted_result", **new_result)
        insert("voucher", voucher_id=uid(21), result_id=uid(20), role="original", period="2026-02", number=1, summary="必须回滚")
        insert("voucher_line", voucher_id=uid(21), line_no=1, account_code="5602", debit_fen=999, credit_fen=0, summary="必须回滚")
        insert("obligation", **dict(children["obligation"], result_id=uid(20), amount_fen=999))
        db.execute("UPDATE voucher SET sealed=1 WHERE voucher_id=?", (uid(21),))
    except sqlite3.IntegrityError as error:
        assert str(error) == "unbalanced_voucher", str(error)
        db.execute("ROLLBACK TO atomic_unit")
    else:
        raise AssertionError("不平衡整单未拒绝")
    finally:
        db.execute("RELEASE atomic_unit")
    assert rows() == before, "整单回滚残留新行或改变原金额"
    COUNTS["frozen_child_tables_checked"] = len(children)
    assert not db.execute("PRAGMA foreign_key_check").fetchall()
    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    db.close()
    catalog = sqlite3.connect(":memory:")
    catalog.executescript((ROOT / "schema/catalog.sql").read_text(encoding="utf-8"))
    COUNTS["catalog_tables"] = catalog.execute("SELECT count(*) FROM sqlite_schema WHERE type='table'").fetchone()[0]
    catalog.close()


def check_links():
    count = 0
    for path in ROOT.rglob("*.md"):
        for target in re.findall(r"\]\(([^)]+)\)", path.read_text(encoding="utf-8")):
            target = target.strip("<>")
            if "://" in target or target.startswith("#"):
                continue
            location = unquote(target.split("#", 1)[0])
            if not location:
                continue
            assert (path.parent / location).exists(), (path.name, location)
            count += 1
    COUNTS["local_links"] = count


def check_company_note_structure_plan():
    """Check proposed DDL only in memory; this is not a runtime migration test."""
    baseline = (ROOT / "schema/company.sql").read_text(encoding="utf-8")
    proposed = (ROOT / "schema/company-note-format.sql").read_text(encoding="utf-8")
    columns = "scope_key,note_text,basis_head,state,seq,latest_command_id,latest_expected_seq,content_digest"
    for rollback in (False, True):
        db = sqlite3.connect(":memory:")
        try:
            db.executescript(baseline)
            db.executemany(
                f"INSERT INTO company_note({columns}) VALUES(?,?,?,?,?,?,?,?)",
                [(scope, "原文 * _ <公司>\n第二行", None, "current", 1, "00000000-0000-4000-8000-000000000401", 0, "0" * 64)
                 for scope in ("company", "period:2026-08")],
            )
            db.commit()
            before = db.execute(f"SELECT {columns} FROM company_note ORDER BY scope_key").fetchall()
            db.execute("SAVEPOINT note_format_plan")
            statement = ""
            for line in proposed.splitlines(True):
                statement += line
                if sqlite3.complete_statement(statement):
                    db.execute(statement)
                    statement = ""
            assert not statement.strip(), "结构接续包含不完整SQL"
            if rollback:
                try:
                    db.execute("UPDATE company_note SET note_format='html'")
                except sqlite3.IntegrityError:
                    db.execute("ROLLBACK TO note_format_plan")
                else:
                    raise AssertionError("格式约束未拒绝HTML")
                assert "note_format" not in {row[1] for row in db.execute("PRAGMA table_info(company_note)")}
            else:
                assert db.execute("SELECT DISTINCT note_format FROM company_note").fetchall() == [("plain_text",)]
                db.execute("UPDATE company_note SET note_format='markdown' WHERE scope_key='company'")
                try:
                    db.execute("UPDATE company_note SET note_format='markdown' WHERE scope_key LIKE 'period:%'")
                except sqlite3.IntegrityError:
                    pass
                else:
                    raise AssertionError("月份说明不应接受Markdown")
            db.execute("RELEASE note_format_plan")
            assert db.execute(f"SELECT {columns} FROM company_note ORDER BY scope_key").fetchall() == before
        finally:
            db.close()
    COUNTS["company_note_memory_ddl"] = "default, scope constraint and rollback checked"


def main():
    check_contracts()
    check_examples()
    check_sql()
    check_company_note_structure_plan()
    check_links()
    print(json.dumps(COUNTS, ensure_ascii=False, indent=2))
    print("设计静态检查通过。未运行产品业务、真实集成、浏览器或性能验收。")


if __name__ == "__main__":
    try:
        main()
    except ValidationError as error:
        sys.exit(f"合同样例失败：{list(error.absolute_path)}: {error.message[:1000]}")
