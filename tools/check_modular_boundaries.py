"""Check the module boundaries already specified in docs/redesign/02-architecture.md."""

from __future__ import annotations

import argparse
import ast
import re
import sys
from importlib.util import resolve_name
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "ai_accounting_modular"
SOURCE = ROOT / "src" / PACKAGE
KNOWN_OWNERS = frozenset(
    {
        "agent",
        "application",
        "contracts",
        "dashboard",
        "domains.assets",
        "domains.financing",
        "domains.operations",
        "domains.people",
        "domains.taxes",
        "files",
        "host",
        "identity",
        "ledger",
        "ledger.opening",
        "ledger.reporting",
        "money",
        "payments",
        "reading",
        "records",
        "storage",
    }
)
_SQL_TABLE = re.compile(r'\b(?:FROM|JOIN|UPDATE|INTO)\s+["\[]?([a-z_][a-z_0-9]*)', re.IGNORECASE)


def owner_of(module: str) -> str | None:
    parts = module.split(".")
    if parts[0] != PACKAGE or len(parts) < 2:
        return None
    if parts[1] == "domains":
        return ".".join(parts[1:3]) if len(parts) > 2 else None
    if parts[1] == "ledger" and len(parts) > 2 and parts[2] in {"opening", "reporting"}:
        return ".".join(parts[1:3])
    return parts[1]


def table_owners() -> dict[str, str]:
    result = {}
    for filename in ("company.sql", "catalog.sql"):
        owner = "storage"
        source = (ROOT / "docs/redesign/schema" / filename).read_text(encoding="utf-8")
        for line in source.splitlines():
            marker = re.match(r"-- owner:\s*(\S+)", line)
            table = re.match(r'CREATE TABLE\s+"?(\w+)"?', line)
            if marker:
                owner = marker[1]
            if table:
                result[table[1]] = owner
    return result


def imports(node: ast.Import | ast.ImportFrom, module: str, is_package: bool):
    if isinstance(node, ast.Import):
        yield from (alias.name for alias in node.names)
        return
    if node.level:
        package = module if is_package else module.rpartition(".")[0]
        target = resolve_name("." * node.level + (node.module or ""), package)
    else:
        target = node.module or ""
    # `from package.owner import public` names a public submodule. A named
    # function imported from package.owner.public retains that same public path.
    owner = owner_of(target)
    owner_path = f"{PACKAGE}.{owner}" if owner else PACKAGE
    if target in {owner_path, PACKAGE + ".domains"}:
        yield from (target + "." + alias.name for alias in node.names)
    else:
        yield target


def permitted_dependency(owner: str, target: str) -> bool:
    if owner not in KNOWN_OWNERS or target not in KNOWN_OWNERS:
        return False
    if owner == "host":
        return True
    if target == "host":
        return False
    if owner == "application":
        return True
    if target == "payments":
        return False
    if owner == "contracts":
        return False
    if owner == "storage":
        return target == "contracts"
    if owner in {"files", "identity"}:
        return target in {"contracts", "storage"}
    if owner == "dashboard":
        return target in {"contracts", "reading"}
    if owner == "agent":
        return target in {"contracts", "application"}
    if owner == "payments":
        return target in {
            "contracts",
            "domains.people",
            "files",
            "identity",
            "ledger",
            "ledger.opening",
            "records",
            "storage",
        }
    if owner == "reading":
        return target not in {"application", "agent", "dashboard"}
    if owner == "records":
        return target not in {"application", "agent", "reading", "dashboard"}
    if owner.startswith(("domains.", "ledger.")) or owner in {"ledger", "money"}:
        return target not in {"application", "agent", "reading", "dashboard"}
    return False


def violations(source: str, module: str, *, is_package=False, tables=None) -> list[str]:
    owner = owner_of(module)
    if owner is None:
        return []
    if owner not in KNOWN_OWNERS:
        return [f"1: 未声明模块依赖规则：{owner}"]
    tree = ast.parse(source)
    issues = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            for target in imports(node, module, is_package):
                if target == "ai_accounting" or target.startswith("ai_accounting."):
                    issues.append(f"{node.lineno}: 新模块不能依赖旧总体系：{target}")
                    continue
                if target in {PACKAGE, PACKAGE + ".domains"}:
                    issues.append(f"{node.lineno}: 须直接导入所属模块的公开入口：{target}")
                    continue
                other = owner_of(target)
                if other is None or other == owner:
                    continue
                public = f"{PACKAGE}.{other}.public"
                if target != public:
                    issues.append(f"{node.lineno}: 跨模块须使用公开入口：{target}")
                if not permitted_dependency(owner, other):
                    issues.append(f"{node.lineno}: 模块依赖方向不符：{owner} → {other}")
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "repository"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and node.args[0].value != owner
        ):
            issues.append(f"{node.lineno}: 模块不能取得其他 owner 的私有仓储")
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and tables:
            for table in _SQL_TABLE.findall(node.value):
                other = tables.get(table)
                if other is not None and other != owner:
                    issues.append(f"{node.lineno}: SQL 不能读取或修改其他模块私表：{table}")
    return issues


def check_tree(source_root: Path = SOURCE) -> list[str]:
    issues = []
    tables = table_owners()
    for path in sorted(source_root.rglob("*.py")):
        relative = path.relative_to(source_root)
        parts = list(relative.with_suffix("").parts)
        is_package = parts[-1] == "__init__"
        if is_package:
            parts.pop()
        module = ".".join([PACKAGE, *parts])
        issues.extend(
            f"{relative.as_posix()}:{message}"
            for message in violations(
                path.read_text(encoding="utf-8"), module, is_package=is_package, tables=tables
            )
        )
    return issues


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    issues = check_tree()
    if issues:
        print("\n".join(issues))
        return 1
    print("模块公开入口、依赖方向及私有仓储检查通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
