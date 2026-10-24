"""Protect independently developed modules from internal-code and private-table coupling."""

from __future__ import annotations

import runpy
from pathlib import Path

CHECK = runpy.run_path(
    str(Path(__file__).resolve().parents[3] / "tools/check_modular_boundaries.py")
)


def test_implemented_modules_obey_declared_boundaries():
    assert CHECK["check_tree"]() == []


def test_quoted_current_ddl_tables_still_have_their_declared_owners():
    owners = CHECK["table_owners"]()
    assert owners["system_meta"] == "storage"
    assert owners["file_task"] == "files"
    assert CHECK["violations"](
        'query = "SELECT task_id FROM file_task"',
        "ai_accounting_modular.application.public",
        tables=owners,
    )


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


def test_declared_opening_and_reporting_owners_have_real_boundaries():
    scan = CHECK["violations"]
    assert not scan(
        'ctx.repository("ledger.opening")', "ai_accounting_modular.ledger.opening._repository"
    )
    assert scan('ctx.repository("ledger")', "ai_accounting_modular.ledger.opening.public")
    assert not scan(
        "from ai_accounting_modular.ledger.opening.public import create_opening",
        "ai_accounting_modular.application.public",
    )
    assert scan(
        "from ai_accounting_modular.ledger.opening._repository import OpeningRepository",
        "ai_accounting_modular.ledger.public",
    )
    assert scan(
        "from ai_accounting_modular.application.public import create_application",
        "ai_accounting_modular.ledger.reporting.public",
    )
    assert scan(
        'query = "SELECT * FROM opening_member"',
        "ai_accounting_modular.ledger.public",
        tables={"opening_member": "ledger.opening"},
    )
