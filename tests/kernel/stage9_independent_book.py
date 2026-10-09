"""Independent-business contrast book for Stage 9 scale experiments.

Each month adds exact new expense/payment business facts in local pairs. The
50 suppliers are registered entities, not label-only placeholders. All facts,
publications, bank rows, material rows, monthly inventories and closes use the
production kernel. This contrast sample does not replace the mixed payroll
book used for owner-page acceptance.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import sys
import time
from contextlib import ExitStack
from pathlib import Path

import stage9_book
from stage9_book import MixedBook, month_at, synthetic_temporary
from stage9_checkpoint import publish_checkpoint

import ai_accounting
from ai_accounting.kernel.catalog import Catalog
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.materials import Materials
from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods
from ai_accounting.kernel.schema_bundle import production_bundle


class IndependentBook(MixedBook):
    CHECKPOINT = "stage9-independent-builder.json"

    def __init__(self, root: Path, *, objects: int = 50, businesses: int = 1000):
        root = root.resolve()
        if root.parent != synthetic_temporary().resolve() or not root.name.startswith("stage9-"):
            raise ValueError("Only a named synthetic root under .tmp may be created")
        if root.exists():
            raise ValueError("Independent benchmark requires a new synthetic root")
        if objects < 1 or businesses < 4 or businesses % 2:
            raise ValueError(
                "Use registered objects and an even business count for "
                "funding/reserve plus expense/payment"
            )
        self.root = root
        self.catalog = Catalog(root, production_bundle())
        self.company = self.catalog.create_company("91310000123456789I", "阶段九合成独立业务企业")
        self.engine = Engine(self.catalog.bind(self.company["id"]))
        self.entities = Entities(self.engine)
        self.materials = Materials(self.engine)
        self.periods = Periods(self.engine)
        self.objects_count, self.businesses = objects, businesses
        self.distribution = "independent_local_pairs"
        self.sequence = 0
        self.facts, self.results, self.inputs, self.revisions = {}, {}, {}, {}
        self.business_subjects, self.snapshots, self.month_stats = [], {}, []
        self.deferred_close_checks = {}
        self.bank_balance = 0
        self.proof = self.evidence(
            "合成独立业务规模样本，所有金额与对象均为测试资料。", "独立业务说明.txt"
        )
        self.owner = self.entity("person", "合成负责人")
        self.suppliers = [
            self.entity("organization", f"合成供应商{i + 1:03}") for i in range(objects)
        ]
        self.bank = self.entity("fund_account", "合成经营银行账户", account_type="bank")
        self.save(
            [
                self.item(
                    "report_profile",
                    "report-profile",
                    {
                        "period": month_at(0),
                        "company_name": "阶段九合成独立业务企业",
                        "accounting_standard": "small_enterprise",
                        "bookkeeping_start": month_at(0),
                        "newly_established_zero_opening_confirmed": True,
                    },
                ),
                self.item(
                    "bank_opening",
                    "bank-start",
                    {
                        "period": month_at(0),
                        "bank_account_id": self.bank,
                        "opening_fen": self.bank_balance,
                        "basis": "new_account",
                    },
                ),
            ]
        )
        self.publish(["bank-start"])

    def checkpoint(self):
        with self.engine.store.connection(read_only=True) as connection:
            epochs = list(connection.execute("SELECT * FROM state").fetchone())
        state = {
            "company": self.company,
            "objects_count": self.objects_count,
            "businesses": self.businesses,
            "distribution": self.distribution,
            "sequence": self.sequence,
            "business_count": len(self.business_subjects),
            "snapshots": self.snapshots,
            "month_stats": self.month_stats,
            "bank_balance": self.bank_balance,
            "proof": self.proof,
            "owner": self.owner,
            "suppliers": self.suppliers,
            "bank": self.bank,
            "epochs": epochs,
            "deferred_close_checks": self.deferred_close_checks,
        }
        temporary = self.root / (self.CHECKPOINT + ".new")
        temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        publish_checkpoint(temporary, self.root / self.CHECKPOINT)

    @classmethod
    def resume(cls, root: Path):
        root = root.resolve()
        if root.parent != synthetic_temporary().resolve() or not root.name.startswith("stage9-"):
            raise ValueError("Only a named synthetic root under .tmp may resume")
        state = json.loads((root / cls.CHECKPOINT).read_text(encoding="utf-8"))
        book = cls.__new__(cls)
        book.root = root
        book.catalog = Catalog(root, production_bundle())
        if book.catalog.companies() != [state["company"]]:
            raise ValueError("Independent company identity changed")
        if (
            state["company"].get("name"),
            state["company"].get("taxpayer_id"),
        ) != ("阶段九合成独立业务企业", "91310000123456789I"):
            raise ValueError("Only the fixed synthetic independent company may resume")
        for name in (
            "company",
            "objects_count",
            "businesses",
            "distribution",
            "sequence",
            "snapshots",
            "month_stats",
            "bank_balance",
            "proof",
            "owner",
            "suppliers",
            "bank",
        ):
            setattr(book, name, state[name])
        book.business_subjects = [None] * state["business_count"]
        book.deferred_close_checks = state.get("deferred_close_checks", {})
        book.facts, book.results, book.inputs, book.revisions = {}, {}, {}, {}
        book.engine = Engine(book.catalog.bind(book.company["id"]))
        with book.engine.store.connection(read_only=True) as connection:
            if list(connection.execute("SELECT * FROM state").fetchone()) != state["epochs"]:
                raise ValueError("Independent book changed after checkpoint")
        book.entities = Entities(book.engine)
        book.materials = Materials(book.engine)
        book.periods = Periods(book.engine)
        return book

    def close_last_month(self):
        if not self.month_stats or self.month_stats[-1]["closed"]:
            return
        period = self.month_stats[-1]["period"]
        proof = self.snapshots[period]["owner_confirmation"]
        preview = self.periods.preview_close(period, owner_confirmation=proof)
        self.periods.close(
            period,
            owner_confirmation=proof,
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id=self.request("close-last"),
        )
        self.snapshots[period].update(closed=True, preview_digest=preview["digest"])
        self.month_stats[-1]["closed"] = True
        self.checkpoint()

    def add_month(self, index: int, *, close: bool = True):
        from material_fixture import supporting_text
        from test_banking import entry, match

        if index != len(self.month_stats):
            raise ValueError("Build independent months in order")
        period = month_at(index)
        started = time.perf_counter()
        before = len(self.business_subjects)
        self.facts.clear()
        self.results.clear()
        self.inputs.clear()
        self.revisions.clear()
        proof = self.evidence(
            f"合成负责人确认 {period} 的逐项业务与资料完整。", f"{period}-确认.txt"
        )
        self.proof = proof
        expense_count = (self.businesses - 2) // 2
        expenses, expense_rows = [], []
        for i in range(expense_count):
            subject, amount = f"expense-{period}-{i}", 10_000 + i % 1000
            expenses.append(
                self.item(
                    "expense",
                    subject,
                    {
                        "period": period,
                        "counterparty_id": self.suppliers[i % self.objects_count],
                        "amount_fen": amount,
                        "expense_class": "administration",
                        "creditor_kind": "supplier",
                    },
                    business=True,
                )
            )
            expense_rows.append((subject, amount, "fact.amount_fen"))
        self.save(expenses)
        self.publish([item["subject_id"] for item in expenses])

        reserve_amount = 1_000
        funding_amount = sum(item["data"]["amount_fen"] for item in expenses) + reserve_amount
        funding = f"funding-{period}"
        self.save(
            [
                self.item(
                    "funding",
                    funding,
                    {
                        "period": period,
                        "owner_id": self.owner,
                        "amount_fen": funding_amount,
                        "funding_kind": "capital",
                        "actual_date": period + "-01",
                        "bank_account_id": self.bank,
                    },
                    business=True,
                )
            ]
        )
        self.publish([funding])
        payments = []
        bank_rows = [entry(funding, period + "-01", funding_amount)]
        bank_matches = [match(funding, "funding", funding)]
        for i, expense in enumerate(expenses):
            amount = expense["data"]["amount_fen"]
            subject = f"payment-{period}-{i}"
            day = period + "-15"
            payments.append(
                self.item(
                    "payment",
                    subject,
                    {
                        "period": period,
                        "actual_date": day,
                        "direction": "outflow",
                        "bank_account_id": self.bank,
                        "counterparty_id": self.suppliers[i % self.objects_count],
                        "amount_fen": amount,
                        "allocations": [
                            {
                                "source_kind": "expense",
                                "source_id": expense["subject_id"],
                                "obligation": "primary",
                                "amount_fen": amount,
                            }
                        ],
                    },
                    business=True,
                )
            )
            bank_rows.append(entry(subject, day, -amount))
            bank_matches.append(match(subject, "payment", subject))
        self.save(payments)
        self.publish([item["subject_id"] for item in payments])
        reserve = f"reserve-{period}"
        self.save(
            [
                self.item(
                    "managed_reserve_expense",
                    reserve,
                    {
                        "period": period,
                        "actual_date": period + "-20",
                        "bank_account_id": self.bank,
                        "amount_fen": reserve_amount,
                        "counterparty_id": self.suppliers[0],
                    },
                    business=True,
                )
            ]
        )
        self.publish([reserve])
        bank_rows.append(entry(reserve, period + "-20", -reserve_amount))
        bank_matches.append(match(reserve, "managed_reserve_expense", reserve))
        bank_rows.sort(key=lambda row: (row["actual_date"], row["reference"]))
        statement = f"statement-{period}"
        self.save(
            [
                self.item(
                    "bank_statement",
                    statement,
                    {
                        "period": period,
                        "bank_account_id": self.bank,
                        "opening_fen": self.bank_balance,
                        "closing_fen": self.bank_balance
                        + sum(row["signed_fen"] for row in bank_rows),
                        "entries": bank_rows,
                    },
                )
            ]
        )
        self.publish([statement])
        self.bank_balance += sum(row["signed_fen"] for row in bank_rows)
        reconciliation = f"reconcile-{period}"
        self.save(
            [
                self.item(
                    "bank_reconciliation",
                    reconciliation,
                    {
                        "period": period,
                        "bank_account_id": self.bank,
                        "statement_id": statement,
                        "matches": bank_matches,
                    },
                )
            ]
        )
        self.publish([reconciliation])

        materials = {}
        resolution_items = []
        for category, rows in {
            "transactions": [
                *expense_rows,
                (funding, funding_amount, "fact.amount_fen"),
                (reserve, -reserve_amount, "fact.amount_fen"),
            ],
            "bank": [
                (item["subject_id"], item["data"]["amount_fen"], "fact.amount_fen")
                for item in payments
            ],
        }.items():
            evidence, source = self.source_rows(period, category, rows)
            resolution_items.extend(self.resolve_rows(period, rows, source))
            materials[category] = [evidence]
        self.save(resolution_items)
        supporting_text(self.engine, proof, period=period)
        materials["transactions"].append(proof)
        for category in MATERIAL_CATEGORIES:
            evidence = materials.get(category, [])
            self.periods.inventory(
                period,
                category,
                evidence=evidence,
                expected=len(evidence),
                no_business=not evidence,
                confirmation_evidence=proof,
                request_id=self.request("inventory"),
            )
        actual = len(self.business_subjects) - before
        if actual != self.businesses:
            raise AssertionError((period, actual, self.businesses))
        self.report_basis(period)
        preview = self.periods.preview_close(period, owner_confirmation=proof)
        if close:
            self.periods.close(
                period,
                owner_confirmation=proof,
                preview_digest=preview["digest"],
                epochs=preview["epochs"],
                request_id=self.request("close"),
            )
        self.snapshots[period] = {
            "owner_confirmation": proof,
            "closed": close,
            "preview_digest": preview["digest"],
        }
        stats = {
            "period": period,
            "business_count": actual,
            "registered_objects": self.objects_count,
            "independent_pairs": expense_count,
            "funding_count": 1,
            "reserve_count": 1,
            "ending_bank_fen": self.bank_balance,
            "closed": close,
            "construction_seconds": time.perf_counter() - started,
        }
        self.month_stats.append(stats)
        self.checkpoint()
        return stats

    def describe(self, *, requested_months: int):
        with self.engine.store.connection(read_only=True) as connection:
            counts = {
                table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in (
                    "entity",
                    "subject",
                    "fact_revision",
                    "calculation",
                    "voucher_version",
                    "period_close",
                    "evidence",
                )
            }
        return {
            "status": "creating",
            "root": str(self.root),
            "company": self.company,
            "employee_count": 0,
            "registered_object_count": self.objects_count,
            "monthly_business_count": self.businesses,
            "business_count": len(self.business_subjects),
            "requested_months": requested_months,
            "distribution": self.distribution,
            "construction_integrity": {
                "mode": (
                    "deferred_historical_verification"
                    if self.deferred_close_checks else "production_per_close_checks"
                ),
                "deferred_checks_by_period": dict(self.deferred_close_checks),
                "final_full_verification_required": True,
            },
            "snapshots": self.snapshots,
            "months": self.month_stats,
            "counts": counts,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, help="workspace containing the synthetic .tmp")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--months", type=int, default=12)
    parser.add_argument("--objects", type=int, default=50)
    parser.add_argument("--businesses", type=int, default=1000)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--defer-historical-verification", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--stop-file", type=Path, help="stop safely at a completed-month boundary")
    args = parser.parse_args()
    if args.verify_only and (not args.resume or args.build_only):
        parser.error("--verify-only requires --resume and excludes --build-only")
    if args.build_only != args.defer_historical_verification:
        parser.error("independent construction requires both --build-only and "
                     "--defer-historical-verification")
    if args.months < 1:
        raise ValueError("At least one requested month is required")
    repository = Path(__file__).resolve().parents[2]
    if Path(stage9_book.__file__).resolve() != repository / "tests/kernel/stage9_book.py":
        raise ValueError("Independent fixture imported stage9_book from another source")
    if Path(ai_accounting.__file__).resolve() != repository / "src/ai_accounting/__init__.py":
        raise ValueError("Independent fixture imported implementation from another source")
    if args.workspace is not None:
        os.environ["STAGE9_WORKSPACE_ROOT"] = str(args.workspace.resolve())
    workspace = Path(os.environ.get("STAGE9_WORKSPACE_ROOT", repository)).resolve()
    if Path(sys.prefix).resolve() != workspace / ".tmp-kernel-venv":
        raise ValueError("Use repository virtual environment")
    sys.path.insert(0, str(repository))
    root = args.root.resolve()
    if root.parent != synthetic_temporary().resolve() or not root.name.startswith("stage9-"):
        raise ValueError("Use a new, named Stage 9 synthetic root under repository .tmp")
    output = args.output.resolve()
    if output.parent != synthetic_temporary().resolve() or not output.name.startswith("stage9-"):
        raise ValueError("Use a named Stage 9 synthetic report under workspace .tmp")
    if args.stop_file is not None:
        stop_file = args.stop_file.resolve()
        if stop_file.parent != synthetic_temporary().resolve() or not stop_file.name.startswith(
            "stage9-"
        ):
            raise ValueError("Use a named Stage 9 stop file under workspace .tmp")
    if args.output.exists():
        raise ValueError("Preserve previous report")
    from ai_accounting.kernel.daemon import _prepare_static_runtime

    # Use the same static-model initialization as the resident service before
    # creating or resuming any company book held by this fixture process.
    _prepare_static_runtime()
    book = (
        IndependentBook.resume(root)
        if args.resume
        else IndependentBook(root, objects=args.objects, businesses=args.businesses)
    )
    if book.objects_count != args.objects or book.businesses != args.businesses:
        raise ValueError("Sample dimensions changed")
    if len(book.month_stats) > args.months:
        raise ValueError("Existing sample exceeds requested months")
    connections = ExitStack()
    connections.enter_context(
        book.engine.store.connection(read_only=True)
        if args.verify_only
        else book.construction_wal_keeper()
    )
    atexit.register(connections.close)
    if args.defer_historical_verification:
        connections.enter_context(book.defer_historical_verification_for_construction())
    if args.resume and len(book.month_stats) < args.months:
        if args.verify_only:
            raise ValueError("Cannot verify an incomplete independent sample")
        book.close_last_month()

    def describe(current_book):
        return {
            **current_book.describe(requested_months=args.months),
            "source": str(repository.resolve()),
            "workspace": str(workspace),
        }

    report = describe(book)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for index in range(len(book.month_stats), args.months):
        if args.stop_file is not None and args.stop_file.exists():
            report["status"] = "construction_stopped"
            args.output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            connections.close()
            return
        try:
            stats = book.add_month(index, close=index != args.months - 1)
        except Exception as exc:
            report = {
                **describe(book),
                "status": "creation_failed",
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }
            args.output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            raise
        report = describe(book)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(stats), flush=True)
    if args.build_only:
        report = {**describe(book), "status": "built_not_verified"}
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        connections.close()
        return
    from scripts.benchmark_stage9_browser import validate_book_report
    from scripts.stage9_verified_open_preview import verify_book_open_preview

    report = {**describe(book), "status": "verifying"}
    validate_book_report(
        report, company_name="阶段九合成独立业务企业", require_verified=False
    )
    months = book.month_stats
    complete_shape = (
        len(months) == args.months
        and len(book.snapshots) == args.months
        and all(
            month["period"] == month_at(i)
            and month["business_count"] == args.businesses
            and book.snapshots[month["period"]]["closed"] == month["closed"]
            and book.snapshots[month["period"]]["owner_confirmation"]
            and book.snapshots[month["period"]]["preview_digest"]
            for i, month in enumerate(months)
        )
        and all(month["closed"] for month in months[:-1])
        and not months[-1]["closed"]
        and len(book.business_subjects) == args.months * args.businesses
    )
    engine = book.engine
    del book
    try:
        verified = verify_book_open_preview(
            engine,
            checkpoint_path=root / IndependentBook.CHECKPOINT,
            company=report["company"],
            snapshots=report["snapshots"],
            period=months[-1]["period"],
            source=repository,
        )
    except Exception as exc:
        report.update(
            status="verification_failed",
            error={"type": type(exc).__name__, "message": str(exc)},
        )
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        raise
    report.update(
        **verified,
        status="complete"
        if complete_shape and verified["integrity"]["status"] == "verified"
        else "verification_failed",
    )
    if report["status"] == "complete":
        try:
            validate_book_report(report, company_name="阶段九合成独立业务企业")
        except ValueError as exc:
            report["status"] = "verification_failed"
            report["error"] = {"type": type(exc).__name__, "message": str(exc)}
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"status": report["status"], "output": str(args.output)}), flush=True)
    if report["status"] != "complete":
        raise SystemExit(1)
    connections.close()


if __name__ == "__main__":
    main()
