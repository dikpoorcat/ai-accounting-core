"""Protect independently developed modules from internal-code and private-table coupling."""

from __future__ import annotations

import runpy
from pathlib import Path

CHECK = runpy.run_path(
    str(Path(__file__).resolve().parents[3] / "tools/check_modular_boundaries.py")
)


def test_implemented_modules_obey_declared_boundaries():
    assert CHECK["check_tree"]() == []


def test_relative_imports_cannot_hide_internal_dependencies():
    scan = CHECK["violations"]
    assert (
        scan("from ..contracts.public import ReadContext", "ai_accounting_modular.ledger.public")
        == []
    )
    assert scan("from ..ledger import public", "ai_accounting_modular.reading.public") == []
    assert scan(
        "from ..ledger._repository import LedgerRepository", "ai_accounting_modular.reading.public"
    )
    assert scan("from ...host.public import run", "ai_accounting_modular.domains.people.public")
    assert scan(
        "from ai_accounting.kernel.service import Service",
        "ai_accounting_modular.application.public",
    )
    assert scan(
        "from ai_accounting_modular.domains import people",
        "ai_accounting_modular.application.public",
    )
    assert scan("import ai_accounting_modular", "ai_accounting_modular.application.public")


def test_private_tables_and_repositories_remain_with_their_owner():
    scan = CHECK["violations"]
    assert scan('ctx.repository("ledger")', "ai_accounting_modular.reading.public")
    assert scan(
        'query = "SELECT business_id FROM op_fact"',
        "ai_accounting_modular.reading.public",
        tables={"op_fact": "domains.operations"},
    )
    assert not scan(
        'query = "SELECT result_id FROM adopted_result"',
        "ai_accounting_modular.ledger.public",
        tables={"adopted_result": "ledger"},
    )
