"""Keep new module owners and payment-file adapters inside declared directions."""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest

CHECK = runpy.run_path(
    str(Path(__file__).resolve().parents[3] / "tools/check_modular_boundaries.py")
)
PACKAGE = "ai_accounting_modular"
PAYMENT_INPUTS = (
    "contracts",
    "domains.people",
    "files",
    "identity",
    "ledger",
    "ledger.opening",
    "records",
    "storage",
)


@pytest.mark.parametrize("target", PAYMENT_INPUTS)
def test_payments_can_consume_its_declared_public_inputs(target):
    assert CHECK["violations"](
        f"from {PACKAGE}.{target}.public import value", f"{PACKAGE}.payments.public"
    ) == []


@pytest.mark.parametrize(
    "target",
    (
        "agent",
        "application",
        "dashboard",
        "domains.assets",
        "domains.financing",
        "domains.operations",
        "domains.taxes",
        "host",
        "ledger.reporting",
        "money",
        "reading",
    ),
)
def test_payments_rejects_dependencies_outside_its_responsibility(target):
    issues = CHECK["violations"](
        f"from {PACKAGE}.{target}.public import value", f"{PACKAGE}.payments.public"
    )
    assert any("模块依赖方向不符" in issue for issue in issues)


@pytest.mark.parametrize("owner", ("application", "host"))
def test_application_and_host_can_compose_payments(owner):
    assert CHECK["violations"](
        f"from {PACKAGE}.payments import public", f"{PACKAGE}.{owner}.public"
    ) == []


@pytest.mark.parametrize(
    "owner",
    (
        "agent",
        "contracts",
        "dashboard",
        "domains.assets",
        "domains.financing",
        "domains.operations",
        "domains.people",
        "domains.taxes",
        "files",
        "identity",
        "ledger",
        "ledger.opening",
        "ledger.reporting",
        "money",
        "reading",
        "records",
        "storage",
    ),
)
def test_other_modules_cannot_depend_on_payment_file_adapter(owner):
    issues = CHECK["violations"](
        f"from {PACKAGE}.payments.public import value", f"{PACKAGE}.{owner}.public"
    )
    assert any("模块依赖方向不符" in issue for issue in issues)


@pytest.mark.parametrize(
    "source",
    (
        "from ..application.public import value",
        "from ..reading import public",
        "from ..ledger._repository import value",
        "from ai_accounting_modular.files._types import value",
    ),
)
def test_relative_or_private_imports_cannot_bypass_payment_boundaries(source):
    assert CHECK["violations"](source, f"{PACKAGE}.payments.public")


def test_payment_internal_and_external_imports_remain_allowed():
    assert CHECK["violations"](
        "from ._plans import value\nimport sqlite3\nfrom openpyxl import Workbook",
        f"{PACKAGE}.payments.public",
    ) == []
    assert CHECK["violations"](
        "from ..contracts.public import value", f"{PACKAGE}.payments.public"
    ) == []


@pytest.mark.parametrize("owner", ("new_module", "domains.new_domain"))
def test_unregistered_owner_is_rejected_even_without_imports(owner):
    issues = CHECK["violations"]("", f"{PACKAGE}.{owner}.public")
    assert any("未声明模块依赖规则" in issue for issue in issues)
    assert not CHECK["permitted_dependency"](owner, "contracts")


@pytest.mark.parametrize("owner", ("application", "host", "domains.people", "reading"))
@pytest.mark.parametrize("target", ("new_module", "domains.new_domain"))
def test_unregistered_target_does_not_inherit_aggregate_or_domain_permissions(owner, target):
    assert not CHECK["permitted_dependency"](owner, target)
    assert CHECK["violations"](
        f"from {PACKAGE}.{target}.public import value", f"{PACKAGE}.{owner}.public"
    )


def test_tree_scan_detects_empty_unregistered_modules_without_rejecting_containers(tmp_path):
    (tmp_path / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "domains").mkdir()
    (tmp_path / "domains/__init__.py").write_text("", encoding="utf-8")
    assert CHECK["check_tree"](tmp_path) == []

    for relative in ("new_module", "domains/new_domain"):
        directory = tmp_path / relative
        directory.mkdir()
        (directory / "__init__.py").write_text("", encoding="utf-8")
    issues = CHECK["check_tree"](tmp_path)
    assert len(issues) == 2
    assert all("未声明模块依赖规则" in issue for issue in issues)
