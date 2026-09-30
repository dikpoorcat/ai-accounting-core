"""Add a second, independent synthetic company to a Stage 9 browser book.

Only an existing, complete Stage 9 primary root under .tmp is accepted. The
company is registered and populated through the production kernel; the main
book checkpoint and database are left intact.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__:
    from .stage9_source import (
        configure_source,
        require_source_module,
        synthetic_path,
        workspace_root,
    )
    from .stage9_verified_open_preview import verify_book_open_preview
else:
    from stage9_source import (
        configure_source,
        require_source_module,
        synthetic_path,
        workspace_root,
    )
    from stage9_verified_open_preview import verify_book_open_preview


def complete_switch_report(book, integrity, integrity_contract, root, primary_company_id, source):
    return {
        **book.describe(),
        "root": str(root),
        "source": str(source),
        "requested_months": 1,
        "status": "complete",
        "integrity": integrity,
        "integrity_contract": integrity_contract,
        "primary_company_id": primary_company_id,
    }


def registered_integrity(engine, *, expected_epochs=None):
    """Use the installed company's historical content contract, not current rules."""
    from ai_accounting.kernel.versions import database_format

    bundle = engine.store.bundle
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        if expected_epochs is not None and list(
            connection.execute("SELECT * FROM state").fetchone()
        ) != expected_epochs:
            raise ValueError("Switch company changed after its completed-month checkpoint")
        installed = database_format(connection, bundle=bundle)
        integrity = bundle.company_verifiers[installed["version"]](connection, bundle)
    return installed, integrity


def verify_existing_switch(root, primary, source, *, creation_report=None):
    """Recheck a previously built switch fixture without opening a writable catalog."""
    from benchmark_stage9_browser import validate_book_report
    from stage9_book import MixedBook

    from ai_accounting.kernel.catalog import Catalog
    from ai_accounting.kernel.engine import Engine
    from ai_accounting.kernel.schema_bundle import production_bundle

    checkpoint_root = root / "stage9-switch-checkpoint"
    if checkpoint_root.resolve().parent != root:
        raise ValueError("Switch checkpoint must remain inside the synthetic root")
    state = json.loads((checkpoint_root / "stage9-builder.json").read_text(encoding="utf-8"))
    if any(key not in state for key in (*MixedBook._CHECKPOINT_FIELDS, "epochs")):
        raise ValueError("Switch checkpoint is incomplete")
    if not isinstance(state["epochs"], list):
        raise ValueError("Switch checkpoint epochs are invalid")

    # Catalog.__init__ may resume pending operations. This mode only opens
    # validated read-only connections and must never mutate the sample.
    catalog = Catalog.__new__(Catalog)
    catalog.root = root
    catalog.bundle = production_bundle()
    catalog.path = root / "catalog.sqlite"
    companies = catalog.companies()
    switch = state["company"]
    if (
        len(companies) != 2
        or primary["company"] not in companies
        or switch not in companies
        or primary["company"]["id"] == switch["id"]
        or (switch["taxpayer_id"], switch["name"])
        != ("91310000123456789T", "阶段九合成切换企业")
    ):
        raise ValueError("Primary or switch company differs from the synthetic checkpoint")
    if creation_report is not None:
        validate_book_report(
            creation_report,
            company_name="阶段九合成切换企业",
            require_current_preview=False,
        )
        if (
            creation_report.get("company") != switch
            or Path(creation_report.get("root", "")).resolve() != root
            or creation_report.get("primary_company_id") != primary["company"]["id"]
        ):
            raise ValueError("Switch creation report differs from the checkpoint")

    book = MixedBook.__new__(MixedBook)
    book.root = checkpoint_root
    book.catalog = catalog
    for key in MixedBook._CHECKPOINT_FIELDS:
        setattr(book, key, state[key])
    book.deferred_close_checks = state.get("deferred_close_checks", {})
    book.engine = Engine(catalog.bind(switch["id"]))
    shape = book.describe()
    validate_book_report(
        {**shape, "requested_months": 1},
        company_name="阶段九合成切换企业",
        require_verified=False,
    )
    if book.employees_count != 2 or book.businesses != 40:
        raise ValueError("Switch fixture dimensions changed")
    verified = verify_book_open_preview(
        book.engine,
        checkpoint_path=checkpoint_root / "stage9-builder.json",
        company=switch,
        snapshots=shape["snapshots"],
        period=shape["months"][-1]["period"],
        source=source,
    )
    report = complete_switch_report(
        book,
        verified["integrity"],
        verified["integrity_contract"],
        root,
        primary["company"]["id"],
        source,
    )
    report.update(
        verified_open_preview=verified["verified_open_preview"],
        integrity_ms=verified["integrity_ms"],
    )
    if creation_report is not None:
        report["creation_source"] = creation_report.get("source")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--book-report", type=Path, required=True)
    parser.add_argument("--read-existing", action="store_true")
    parser.add_argument("--switch-report", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    workspace = workspace_root(Path(__file__).resolve().parents[1], args.workspace)
    if Path(sys.prefix).resolve() != workspace / ".tmp-kernel-venv":
        raise ValueError("Use workspace virtual environment")
    synthetic_path(args.book_report, workspace)
    synthetic_path(args.output, workspace)
    if args.switch_report is not None:
        synthetic_path(args.switch_report, workspace)
        if not args.read_existing:
            parser.error("--switch-report requires --read-existing")
    source = configure_source(args.source, workspace)
    import benchmark_stage9_browser
    import stage9_book

    import ai_accounting

    require_source_module(ai_accounting, source, "src/ai_accounting/__init__.py")
    require_source_module(stage9_book, source, "tests/kernel/stage9_book.py")
    require_source_module(benchmark_stage9_browser, source, "scripts/benchmark_stage9_browser.py")

    from benchmark_stage9_browser import validate_book_report
    from stage9_book import MixedBook

    from ai_accounting.kernel.catalog import Catalog
    from ai_accounting.kernel.engine import Engine
    from ai_accounting.kernel.entities import Entities
    from ai_accounting.kernel.materials import Materials
    from ai_accounting.kernel.periods import Periods
    from ai_accounting.kernel.schema_bundle import production_bundle

    primary = json.loads(args.book_report.read_text(encoding="utf-8"))
    validate_book_report(primary, company_name="阶段九合成规模企业")
    if Path(primary.get("source", "")).resolve() != source:
        raise ValueError("Switch company must use the primary book's fixed source")
    if primary.get("distribution") != "mixed_cumulative":
        raise ValueError("Switch company requires the verified mixed primary book")
    root = Path(primary["root"]).resolve()
    synthetic_path(root, workspace)
    if args.output.exists():
        raise ValueError("Preserve an existing switch-company report")
    if args.read_existing:
        creation_report = (
            json.loads(args.switch_report.read_text(encoding="utf-8"))
            if args.switch_report is not None else None
        )
        report = verify_existing_switch(
            root, primary, source, creation_report=creation_report
        )
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps({"status": "complete", "output": str(args.output)}), flush=True)
        return
    bundle = production_bundle()
    catalog = Catalog(root, bundle)
    companies = catalog.companies()
    if (
        len(companies) != 1
        or companies[0]["id"] != primary["company"]["id"]
        or companies[0]["database_id"] != primary["company"]["database_id"]
    ):
        raise ValueError("Primary company does not match the completed synthetic report")
    checkpoint_root = root / "stage9-switch-checkpoint"
    checkpoint_root.mkdir(exist_ok=False)

    book = MixedBook.__new__(MixedBook)
    book.root = checkpoint_root
    book.catalog = catalog
    book.company = catalog.create_company("91310000123456789T", "阶段九合成切换企业")
    book.engine = Engine(catalog.bind(book.company["id"]))
    book.entities = Entities(book.engine)
    book.materials = Materials(book.engine)
    book.periods = Periods(book.engine)
    book.employees_count, book.businesses, book.distribution = 2, 40, "mixed_cumulative"
    book.sequence = 0
    book.facts, book.results, book.inputs, book.revisions = {}, {}, {}, {}
    book.business_subjects, book.snapshots, book.month_stats = [], {}, []
    book.deferred_close_checks = {}
    book.bank_balance, book.carried_settlement = 0, None
    book.proof = book.evidence(
        "合成切换企业负责人确认：全部数值仅用于浏览器切换验证。", "切换样本说明.txt"
    )
    book.owner = book.entity("person", "合成出资人")
    book.supplier = book.entity("organization", "合成供应商")
    book.customer = book.entity("organization", "合成客户")
    book.lender = book.entity("organization", "合成持牌贷款人")
    book.bank = book.entity("fund_account", "合成公司银行", account_type="bank")
    book.cash = book.entity("fund_account", "合成现金账户", account_type="cash")
    book.employees = [
        book.entity("person", f"合成员工{i + 1:03}") for i in range(book.employees_count)
    ]
    book._setup()
    stats = book.add_month(0, close=False)
    verified = verify_book_open_preview(
        book.engine,
        checkpoint_path=checkpoint_root / "stage9-builder.json",
        company=book.company,
        snapshots=book.snapshots,
        period=stats["period"],
        source=source,
    )
    if stats["business_count"] != 40:
        raise AssertionError("Second company did not reach a verified 40-business state")
    report = complete_switch_report(
        book,
        verified["integrity"],
        verified["integrity_contract"],
        root,
        primary["company"]["id"],
        source,
    )
    report.update(
        verified_open_preview=verified["verified_open_preview"],
        integrity_ms=verified["integrity_ms"],
    )
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": "complete",
                "company_id": book.company["id"],
                "business_count": stats["business_count"],
                "output": str(args.output),
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
