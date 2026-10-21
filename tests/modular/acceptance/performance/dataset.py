"""Build new synthetic catalogs through public Application; never seed owner tables."""

import json
import os
import sys
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from argon2 import PasswordHasher

from ai_accounting_modular.host.public import create_kernel
from ai_accounting_modular.storage.public import CompanySpec, OwnerProfile, create_catalog

from .._support import COMPANY, DATABASE, FAMILIES, defer_documents
from .ledger_variant import install_ledger_implementation
from .workload import (
    BUSINESSES_PER_MONTH,
    CONTRACTORS,
    EMPLOYEES,
    INITIAL_BANK,
    PEOPLE,
    SCALES,
    START,
    identity,
    month,
    plan_month,
)
from .workload_reads import reconciliation_request

FORMAT = "modular-c14-synthetic/1"
PASSWORD = "synthetic-performance-password"
NAME = "合成模块性能验收有限公司"


def count_businesses(kernel, period):
    actual, cursor = [], None
    for family in FAMILIES:
        while True:
            query = {
                "company_id": COMPANY,
                "family": family,
                "period_from": period,
                "period_to": period,
                "status": "all",
                "limit": 500,
            }
            if cursor:
                query["cursor"] = cursor
            page = kernel.application.accounting_find(query)
            actual.extend(page["items"])
            cursor = page["page"]["next_cursor"]
            if not page["page"]["has_more"]:
                assert cursor is None
                break
    assert len(actual) == len({item["business_id"] for item in actual})
    assert all(item["state"] == "active" for item in actual)
    return len(actual)


def _save_manifest(root, manifest):
    pending = root / "synthetic.pending.json"
    pending.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(pending, root / "synthetic.json")


def build(
    root,
    months,
    mode="original",
    *,
    probe_business_count=None,
    pending_last_month=False,
    _checkpoint=None,
):
    """Only a nonexisting explicit directory is a valid target. No cleanup or overwrite."""
    if months not in SCALES and probe_business_count is None:
        raise ValueError("Official scales are exactly 12, 48 or 120 months")
    if type(months) is not int or months < 1:
        raise ValueError("A synthetic dataset needs a positive whole number of months")
    count = BUSINESSES_PER_MONTH if probe_business_count is None else probe_business_count
    if type(count) is not int or count < 1:
        raise ValueError("A synthetic month needs a positive whole number of businesses")
    intended_months = months
    if pending_last_month:
        months -= 1
        if months < 1:
            raise ValueError("A lifecycle prefix needs at least one established month")
    root = Path(root).resolve()
    root.mkdir(parents=False, exist_ok=False)
    catalog = root / "catalog.sqlite3"
    create_catalog(
        catalog, OwnerProfile(identity("owner"), "合成性能负责人", PasswordHasher().hash(PASSWORD))
    )
    manifest = {
        "format": FORMAT,
        "budget_source": [
            "docs/redesign/06-dashboard-adapter.md §10",
            "examples/core-acceptance.json C14",
        ],
        "official_scale": False,
        "months": 0,
        "people": PEOPLE,
        "employees": EMPLOYEES,
        "contractors": CONTRACTORS,
        "businesses_per_month": count,
        "company_id": COMPANY,
        "database_id": DATABASE,
        "mode_built": mode,
        "period_from": START,
        "period_to": month(months - 1),
        "months_data": [],
    }
    if pending_last_month:
        manifest.update(intended_scale_months=intended_months, pending_period=month(months))
    with install_ledger_implementation(mode):
        kernel = create_kernel(catalog)
        try:
            kernel.storage.create_company(
                CompanySpec(
                    COMPANY,
                    DATABASE,
                    "913100000000000001",
                    NAME,
                    START,
                    root / "company",
                    backup_directory=root / "backups",
                )
            )
            balance = INITIAL_BANK
            for index in range(months):
                planned = plan_month(index, business_count=count, opening_bank=balance)
                # Every batch still commits real facts/results. The final two
                # bank reconciliations finish the complete month's documents once.
                with defer_documents(kernel):
                    for request in planned.requests:
                        saved = kernel.application.accounting_apply(request)
                        assert (
                            saved["completion"] == "saved_documents_pending"
                            and saved["documents"] == "pending"
                        ), saved
                saved = kernel.application.accounting_apply(reconciliation_request(kernel, planned))
                assert saved["completion"] == "complete" and saved["documents"] == "ready", saved
                assert count_businesses(kernel, planned.period) == count
                balance = planned.ending_bank_fen
                manifest["months_data"].append(
                    {
                        "period": planned.period,
                        "counts": planned.counts,
                        "apply_batches": len(planned.requests) + 1,
                        "expense_id": planned.expense_id,
                    }
                )
                manifest.update(
                    months=index + 1,
                    period_to=planned.period,
                    ending_bank_fen=balance,
                    official_scale=probe_business_count is None
                    and not pending_last_month
                    and index + 1 == months,
                )
                _save_manifest(root, manifest)
                print(
                    f"合成样本 {index + 1}/{months}：{planned.period}，"
                    f"{count} 项业务及标准文档已完成。",
                    file=sys.stderr,
                    flush=True,
                )
                if _checkpoint is not None:
                    _checkpoint(root, deepcopy(manifest))
        finally:
            kernel.close()
    return manifest


def describe(root):
    root = Path(root).resolve()
    manifest = json.loads((root / "synthetic.json").read_text(encoding="utf-8"))
    assert manifest["format"] == FORMAT and manifest["company_id"] == COMPANY
    assert manifest["database_id"] == DATABASE
    return manifest


@contextmanager
def fixed_clock(manifest):
    selected = datetime.fromisoformat(manifest["period_to"] + "-28T10:00:00+08:00")

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return selected.astimezone(tz) if tz else selected.replace(tzinfo=None)

    with ExitStack() as stack:
        for name in (
            "ai_accounting_modular.ledger._closing.datetime",
            "ai_accounting_modular.reading._brief.datetime",
            "ai_accounting_modular.reading._business_status.datetime",
        ):
            stack.enter_context(patch(name, Clock))
        yield Clock


@contextmanager
def open_dataset(root, mode, *, clock_period=None):
    root = Path(root).resolve()
    manifest = describe(root)
    clock_manifest = manifest if clock_period is None else manifest | {"period_to": clock_period}
    with install_ledger_implementation(mode), fixed_clock(clock_manifest) as clock:
        kernel = create_kernel(root / "catalog.sqlite3")
        try:
            kernel.dashboard.now = clock.now
            with kernel.storage.read_unit(COMPANY) as read:
                metadata = kernel.storage.company_metadata(read)
                assert metadata.database_id == DATABASE
                assert metadata.company_root.resolve() == root / "company"
                assert metadata.legal_name == NAME
            yield kernel, manifest
        finally:
            kernel.close()
