"""Protect independently developed modules from internal-code and private-table coupling."""

from __future__ import annotations

import runpy
from inspect import signature
from pathlib import Path
from typing import get_type_hints

from ai_accounting_modular.contracts.public import EvaluationContext, Fact
from ai_accounting_modular.domains.taxes.public import tax_point_periods

CHECK = runpy.run_path(
    str(Path(__file__).resolve().parents[3] / "tools/check_modular_boundaries.py")
)


def test_implemented_modules_obey_declared_boundaries():
    assert CHECK["check_tree"]() == []


def test_tax_period_query_is_a_documented_public_fact_port():
    port = tax_point_periods.__name__
    public = "ai_accounting_modular.domains.taxes.public"
    assert tax_point_periods.__module__ == public
    call = signature(tax_point_periods)
    assert tuple(call.parameters) == ("fact", "context")
    assert get_type_hints(tax_point_periods) == {
        "fact": Fact,
        "context": EvaluationContext,
        "return": tuple[str, ...],
    }

    documents = Path(__file__).resolve().parents[3] / "docs/design"
    responsibilities = next(
        row
        for row in (documents / "02-architecture.md").read_text(encoding="utf-8").splitlines()
        if row.startswith("| `domains/operations` |")
    )
    assert f"`domains/taxes.public.{port}`" in responsibilities
    contract = next(
        row
        for row in (documents / "03-contracts.md").read_text(encoding="utf-8").splitlines()
        if row.startswith("| domains.taxes.public |")
    )
    documented_call = f"{port}({', '.join(call.parameters)}) -> {call.return_annotation}"
    assert f"`{documented_call}`" in contract

    consumer = "ai_accounting_modular.domains.operations._tax_lifecycle"
    scan = CHECK["violations"]
    assert scan(f"from {public} import {port}", consumer) == []
    assert scan(
        "from ai_accounting_modular.domains.taxes._receipt_basis import receipts", consumer
    )


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
