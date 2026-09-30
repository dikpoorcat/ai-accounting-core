"""Reproducible mixed books made through production registration/publication APIs.

This is deliberately a test fixture, not an import or reconstruction facility.
Policies and owner confirmations describe synthetic transactions only.
"""

from __future__ import annotations

import calendar
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from stage9_checkpoint import publish_checkpoint

from ai_accounting.kernel.catalog import Catalog
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.materials import Materials
from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.types import YearMonth


def month_at(index):
    return str(YearMonth.from_ordinal(YearMonth("2016-01").ordinal + index))


def synthetic_temporary():
    workspace = os.environ.get("STAGE9_WORKSPACE_ROOT")
    selected = Path(workspace).resolve() if workspace else Path(__file__).resolve().parents[2]
    return selected / ".tmp"


class MixedBook:
    _CHECKPOINT_FIELDS = (
        "company",
        "employees_count",
        "businesses",
        "distribution",
        "sequence",
        "facts",
        "results",
        "inputs",
        "revisions",
        "business_subjects",
        "snapshots",
        "month_stats",
        "bank_balance",
        "carried_settlement",
        "proof",
        "owner",
        "supplier",
        "customer",
        "lender",
        "bank",
        "cash",
        "employees",
    )

    def __init__(self, root, *, employees=50, businesses=1000, distribution="mixed_cumulative"):
        if root.exists():
            raise ValueError("A benchmark must create a fresh synthetic root")
        if employees < 1 or businesses < employees * 2 + 24:
            raise ValueError("The mixed sample needs wages, payments and independent business")
        if distribution != "mixed_cumulative":
            raise ValueError("MixedBook provides only the mixed cumulative distribution")
        self.root = root
        self.catalog = Catalog(root, production_bundle())
        self.company = self.catalog.create_company("91310000123456789S", "阶段九合成规模企业")
        self.engine = Engine(self.catalog.bind(self.company["id"]))
        self.entities = Entities(self.engine)
        self.materials = Materials(self.engine)
        self.periods = Periods(self.engine)
        self.employees_count, self.businesses = employees, businesses
        self.distribution = distribution
        self.sequence = 0
        self.facts, self.results, self.inputs, self.revisions = {}, {}, {}, {}
        self.business_subjects, self.snapshots, self.month_stats = [], {}, []
        self.deferred_close_checks = {}
        self.bank_balance = 0
        self.carried_settlement = None
        self.proof = self.evidence(
            "合成测试负责人确认：全部数值仅用于规模验证。", "规模测试说明.txt"
        )
        self.owner = self.entity("person", "合成出资人")
        self.supplier = self.entity("organization", "合成供应商")
        self.customer = self.entity("organization", "合成客户")
        self.lender = self.entity("organization", "合成持牌贷款人")
        self.bank = self.entity("fund_account", "合成公司银行", account_type="bank")
        self.cash = self.entity("fund_account", "合成现金账户", account_type="cash")
        self.employees = [self.entity("person", f"合成员工{i + 1:03}") for i in range(employees)]
        self._setup()

    def checkpoint(self):
        """Test-only construction state, written after a complete synthetic month."""
        payload = {key: getattr(self, key) for key in self._CHECKPOINT_FIELDS}
        payload["deferred_close_checks"] = self.deferred_close_checks
        with self.engine.store.connection(read_only=True) as connection:
            payload["epochs"] = list(connection.execute("SELECT * FROM state").fetchone())
        temporary = self.root / "stage9-builder.json.new"
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        publish_checkpoint(temporary, self.root / "stage9-builder.json")

    @classmethod
    def resume(cls, root):
        """Resume this fixture only; never repairs a partly written month or schema."""
        temporary = synthetic_temporary()
        root = root.resolve()
        if root.parent != temporary.resolve() or not root.name.startswith("stage9-"):
            raise ValueError("only an explicit Stage 9 synthetic root can be resumed")
        state = json.loads((root / "stage9-builder.json").read_text(encoding="utf-8"))
        book = cls.__new__(cls)
        book.root = root
        book.catalog = Catalog(root, production_bundle())
        companies = book.catalog.companies()
        matching = [company for company in companies if company["id"] == state["company"]["id"]]
        if len(matching) != 1 or matching[0] != state["company"]:
            raise ValueError("synthetic checkpoint company changed")
        for key in cls._CHECKPOINT_FIELDS:
            setattr(book, key, state[key])
        book.deferred_close_checks = state.get("deferred_close_checks", {})
        book.engine = Engine(book.catalog.bind(book.company["id"]))
        with book.engine.store.connection(read_only=True) as connection:
            if list(connection.execute("SELECT * FROM state").fetchone()) != state["epochs"]:
                raise ValueError("synthetic book changed after its completed-month checkpoint")
        book.entities = Entities(book.engine)
        book.materials = Materials(book.engine)
        book.periods = Periods(book.engine)
        return book

    @contextmanager
    def defer_historical_verification_for_construction(self):
        """Build fixture data, never measure or certify the production close gate.

        Readiness, manifest construction, digest comparison and atomic writes
        still execute. The complete unchanged production verifier must inspect
        the finished book in a later process before any browser measurement.
        """
        temporary = synthetic_temporary()
        root = self.root.resolve()
        if (
            root.parent != temporary.resolve()
            or not root.name.startswith("stage9-")
            or self.catalog.companies() != [self.company]
            or (self.company["name"], self.company["taxpayer_id"])
            not in {
                ("阶段九合成规模企业", "91310000123456789S"),
                ("阶段九合成独立业务企业", "91310000123456789I"),
            }
            or self.engine.store.path.resolve().parent.parent != root
        ):
            raise ValueError("only the isolated Stage 9 fixture may defer construction checks")

        def deferred(engine, connection, period):
            if engine is not self.engine or not connection.in_transaction:
                raise ValueError("construction check belongs to another engine or transaction")
            database = connection.execute("PRAGMA database_list").fetchone()[2]
            if Path(database).resolve() != self.engine.store.path.resolve():
                raise ValueError("construction check belongs to another database")
            key = str(YearMonth.from_ordinal(period))
            self.deferred_close_checks[key] = self.deferred_close_checks.get(key, 0) + 1

        with patch("ai_accounting.kernel.integrity.verify_close_integrity", deferred):
            yield

    def close_last_month(self):
        period = self.month_stats[-1]["period"]
        snapshot = self.snapshots[period]
        if snapshot["closed"]:
            return
        proof = snapshot["owner_confirmation"]
        preview = self.periods.preview_close(period, owner_confirmation=proof)
        self.periods.close(
            period,
            owner_confirmation=proof,
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id=self.request("close-extended-sample"),
        )
        snapshot.update(closed=True, preview_digest=preview["digest"])
        self.month_stats[-1]["closed"] = True
        self.checkpoint()

    def request(self, label="fixture"):
        self.sequence += 1
        return f"stage9-{label}-{self.sequence}"

    def evidence(self, content, name):
        return self.engine.register_evidence(
            content.encode("utf-8") if isinstance(content, str) else content,
            "text/plain" if name.endswith(".txt") else "text/csv",
            name,
            request_id=self.request("evidence"),
        )["digest"]

    def entity(self, kind, name, **options):
        return self.entities.register_entity(
            kind,
            {"display_name": name},
            source="明确的合成规模样本",
            request_id=self.request("entity"),
            **options,
        )["entity_id"]

    def item(self, kind, subject, data, *, evidence=None, revision=0, business=False):
        self.inputs[subject] = (kind, data)
        if business:
            self.business_subjects.append(subject)
        return {
            "kind": kind,
            "subject_id": subject,
            "data": data,
            "evidence": tuple(evidence or (self.proof,)),
            "expected_revision": revision,
        }

    def save(self, items):
        for start in range(0, len(items), 5000):
            result = self.engine.save_facts(
                items[start : start + 5000], request_id=self.request("facts")
            )
            for row in result["results"]:
                if row["status"] != "confirmed":
                    raise AssertionError(row)
                self.facts[row["subject_id"]] = row["fact_id"]
                self.revisions[row["subject_id"]] = 1

    def revise_expense(self, subject, amount, *, posting_period=None, publish=True):
        kind, old = self.inputs[subject]
        if kind != "expense":
            raise AssertionError(subject)
        updated = {**old, "amount_fen": amount}
        result = self.engine.amend_fact(
            kind,
            subject,
            updated,
            evidence=(self.proof,),
            expected_revision=self.revisions[subject],
            recording_error_confirmed=True,
            request_id=self.request("amend"),
        )
        if result["status"] != "confirmed":
            raise AssertionError(result)
        self.facts[subject] = result["fact_id"]
        self.revisions[subject] += 1
        self.inputs[subject] = (kind, updated)
        if publish:
            self.publish([subject], posting_period=posting_period)
        return (subject, amount, "fact.amount_fen")

    def correct_closed_interest(self, prior_period, posting_period):
        subject = f"interest-{prior_period}"
        kind, old = self.inputs[subject]
        if kind != "loan_interest":
            raise AssertionError(subject)
        updated = {**old, "period_end_exclusive": prior_period + "-19"}
        result = self.engine.amend_fact(
            kind,
            subject,
            updated,
            evidence=(self.proof,),
            expected_revision=self.revisions[subject],
            recording_error_confirmed=True,
            request_id=self.request("closed-amend"),
        )
        if result["status"] != "confirmed":
            raise AssertionError(result)
        self.facts[subject] = result["fact_id"]
        self.revisions[subject] += 1
        self.inputs[subject] = (kind, updated)
        self.publish([subject], posting_period=posting_period)

    def publish(self, subjects, *, posting_period=None):
        if not subjects:
            return
        preview = self.engine.preview(subjects, posting_period=posting_period)
        result = self.engine.confirm(
            subjects,
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            posting_period=posting_period,
            request_id=self.request("publish"),
        )
        self.results.update({row["subject_id"]: row["calculation_id"] for row in result["results"]})

    def _setup(self):
        from test_payroll import contribution_policy, income_tax_policy, profile

        policy = contribution_policy().model_dump(mode="json")
        policy.update(
            period=month_at(0),
            effective_from="2016-01-01",
            effective_to="2035-12-31",
            version="synthetic-contribution-v1",
        )
        tax = income_tax_policy().model_dump(mode="json")
        tax.update(
            period=month_at(0),
            effective_from="2016-01-01",
            effective_to="2035-12-31",
            version="synthetic-income-tax-v1",
        )
        vat = {
            "version": "synthetic-vat-v1",
            "source_url": "https://www.chinatax.gov.cn/",
            "effective_from": "2016-01-01",
            "effective_to": "2035-12-31",
            "rate_percent": "1",
            "threshold_fen": 1000000,
            "threshold_operator": "at_or_below",
        }
        surtax = {
            "version": "synthetic-surtax-v1",
            "source_url": "https://www.chinatax.gov.cn/",
            "effective_from": "2016-01-01",
            "effective_to": "2035-12-31",
            "urban_rate_percent": "7",
            "education_rate_percent": "3",
            "local_education_rate_percent": "2",
            "payable_fraction": "0.5",
        }
        items = [
            self.item(
                "report_profile",
                "report-profile",
                {
                    "period": month_at(0),
                    "company_name": "阶段九合成规模企业",
                    "accounting_standard": "small_enterprise",
                    "bookkeeping_start": month_at(0),
                    "newly_established_zero_opening_confirmed": True,
                },
            ),
            self.item("payroll_contribution_policy", "contributions", policy),
            self.item("payroll_income_tax_policy", "income-tax", tax),
            self.item("vat_policy", "vat", {"period": month_at(0), "policy": vat}),
            self.item("surtax_policy", "surtax", {"period": month_at(0), "policy": surtax}),
        ]
        for i, employee in enumerate(self.employees):
            data = profile(
                employee_id=employee,
                period=month_at(0),
                effective_from=month_at(0),
                withholding_start_date="2016-01-01",
            ).model_dump(mode="json")
            items.append(self.item("payroll_profile", f"profile-{i}", data))
        items.append(
            self.item(
                "bank_opening",
                "bank-start",
                {
                    "period": month_at(0),
                    "bank_account_id": self.bank,
                    "opening_fen": 0,
                    "basis": "new_account",
                },
            )
        )
        self.save(items)
        self.publish(["bank-start"])

    def report_basis(self, period):
        """Explicit synthetic classifications, never inferred real company facts."""
        from collections import defaultdict

        with self.engine.store.connection(read_only=True) as connection:
            rows = connection.execute(
                "SELECT v.id,l.line_no,l.debit+l.credit FROM voucher_current vc "
                "JOIN voucher_version v ON v.id=vc.version_id "
                "JOIN voucher_line l ON l.version_id=v.id "
                "WHERE v.period=? AND v.reverses_id IS NULL AND l.account='5602' "
                "ORDER BY v.id,l.line_no",
                (YearMonth(period).ordinal,),
            ).fetchall()
            quarter_end = f"{period[:4]}-{((int(period[5:]) - 1) // 3 + 1) * 3:02}"
            tax_subject = "report-income-tax-" + quarter_end
            tax_exists = (
                connection.execute(
                    "SELECT 1 FROM fact_current WHERE subject_id=?",
                    (tax_subject,),
                ).fetchone()
                is not None
            )
        grouped = defaultdict(list)
        for version, line, amount in rows:
            grouped[version].append(
                {
                    "line_no": line,
                    "detail_code": "management_other",
                    "amount_fen": amount,
                }
            )
        items = [
            self.item(
                "report_classification",
                "report-class-" + version,
                {
                    "period": period,
                    "voucher_version_id": version,
                    "profit_details": lines,
                },
            )
            for version, lines in grouped.items()
        ]
        if not tax_exists:
            items.append(
                self.item(
                    "report_income_tax_confirmation",
                    tax_subject,
                    {
                        "period": quarter_end,
                        "treatment": "zero",
                        "cumulative_assessed_fen": 0,
                        "explanation": (
                            "合成测试设定：本季度企业所得税为零；负责人明确确认，非从报表亏损推定。"
                        ),
                    },
                )
            )
        self.save(items)

    def source_rows(self, period, category, rows):
        """Register real CSV rows and return precise locations used by dispositions."""
        raw = "subject,amount,period\n" + "".join(
            f"{subject},{'-' if amount < 0 else ''}"
            f"{abs(amount) // 100}.{abs(amount) % 100:02},{period}\n"
            for subject, amount, _ in rows
        )
        proof = self.evidence(raw, f"{period}-{category}-合成明细.csv")
        source_id = f"source-{period}-{category}"
        source = self.materials.receive(
            source_id,
            {
                "period": period,
                "category": category,
                "evidence_digest": proof,
                "purpose": "business",
                "specification": {
                    "format": "csv",
                    "columns": [
                        {"column": "A", "role": "context"},
                        {"column": "B", "role": "amount"},
                        {"column": "C", "role": "recognition_period"},
                    ],
                },
            },
            evidence=(proof, self.proof),
            expected_revision=0,
            request_id=self.request("source"),
        )
        return proof, source

    def resolve_rows(self, period, rows, source):
        items = []
        for i, (subject, amount, field) in enumerate(rows):
            items.append(
                self.item(
                    "material_resolution_v2",
                    f"resolution-{source['subject_id']}-{i}",
                    {
                        "period": period,
                        "source_id": source["subject_id"],
                        "source_fact_id": source["fact_id"],
                        "location": f"CSV!B{i + 2}",
                        "treatment": "recognize",
                        "recognition_period": period,
                        "links": [
                            {
                                "subject_id": subject,
                                "fact_kind": self.inputs[subject][0],
                                "fact_id": self.facts[subject],
                                "calculation_id": self.results[subject],
                                "amount_field": field,
                                "amount_fen": amount,
                                "recognition_period": period,
                            }
                        ],
                    },
                )
            )
        return items

    def add_month(self, index, *, close=True):
        from test_banking import activate_asset, consume_assets, entry, match
        from test_payroll import opening, payroll

        period = month_at(index)
        started = time.perf_counter()
        before = len(self.business_subjects)
        proof = self.evidence(
            f"合成负责人确认 {period} 全月工资、业务和资料完整性。", f"{period}-负责人确认.txt"
        )
        self.proof = proof
        acquisitions, payables, bank_rows, bank_matches, materials = [], [], [], [], {}
        carried_in, self.carried_settlement = self.carried_settlement, None
        closed_corrections = 0
        if index and self.snapshots[month_at(index - 1)]["closed"]:
            prior = month_at(index - 1)
            self.correct_closed_interest(prior, period)
            followup = f"interest-followup-{prior}"
            self.save(
                [
                    self.item(
                        "loan_interest",
                        followup,
                        {
                            "period": prior,
                            "agreement_id": f"agreement-{prior}",
                            "drawdown_id": f"draw-{prior}",
                            "period_start": prior + "-19",
                            "period_end_exclusive": prior + "-20",
                        },
                        business=True,
                    )
                ]
            )
            self.publish([followup], posting_period=period)
            closed_corrections = 1
        sale_count = min(40, max(2, self.businesses // 25))
        fixed = (
            self.employees_count * 2
            + 8
            + 3
            + 1
            + sale_count * 2
            + bool(carried_in)
            + closed_corrections
        )
        expense_count, odd = divmod(self.businesses - fixed, 2)
        if expense_count < 1:
            raise ValueError("increase the business count for this mixed fixture")
        if period.endswith("-01"):
            self.save(
                [
                    self.item(
                        "payroll_opening_state",
                        f"tax-opening-{period}-{i}",
                        opening(employee_id=employee, period=period).model_dump(mode="json"),
                    )
                    for i, employee in enumerate(self.employees)
                ]
            )
        wages = []
        for i, employee in enumerate(self.employees):
            subject = f"wage-{period}-{i}"
            data = payroll(
                period=period,
                employee_id=employee,
                profile_id=f"profile-{i}",
                accounting_gross_salary_fen=600000 + i * 100,
                tax_reported_salary_fen=600000 + i * 100,
            ).model_dump(mode="json")
            wages.append(self.item("payroll", subject, data, business=True))
            wages.append(
                self.item(
                    "payroll_plan_v2",
                    f"wage-plan-{period}-{i}",
                    {
                        "period": period,
                        "employee_id": employee,
                        "payroll": data,
                        "profile_revision": {"subject_id": f"profile-{i}", "revision": 1},
                        "contribution_policy_revision": {
                            "subject_id": "contributions",
                            "revision": 1,
                        },
                        "income_tax_policy_revision": {"subject_id": "income-tax", "revision": 1},
                        "change_notice_revisions": [],
                    },
                )
            )
        self.save(wages)
        acquisitions.extend(item["subject_id"] for item in wages if item["kind"] == "payroll")
        wage_rows = [
            (
                subject,
                self.inputs[subject][1]["accounting_gross_salary_fen"],
                "fact.accounting_gross_salary_fen",
            )
            for subject in acquisitions
        ]

        base = []
        expense_rows, sales_rows = [], []
        for i in range(expense_count):
            subject, amount = f"expense-{period}-{i}", 10000 + i
            base.append(
                self.item(
                    "expense",
                    subject,
                    {
                        "period": period,
                        "counterparty_id": self.supplier,
                        "amount_fen": amount,
                        "expense_class": "administration",
                        "creditor_kind": "supplier",
                    },
                    business=True,
                )
            )
            expense_rows.append((subject, amount, "fact.amount_fen"))
            payment_amount = amount - 100 if i == 0 else amount
            payables.append(
                (subject, "expense", payment_amount, self.supplier, "primary", "outflow")
            )
            if i == 0:
                self.carried_settlement = (
                    subject,
                    "expense",
                    100,
                    self.supplier,
                    "primary",
                    "outflow",
                )
        for i in range(sale_count):
            subject, amount = f"sale-{period}-{i}", 50500 + i * 101
            base.append(
                self.item(
                    "service_sale",
                    subject,
                    {
                        "period": period,
                        "customer_id": self.customer,
                        "gross_fen": amount,
                        "vat_policy_id": "vat",
                        "exemption_eligible": False,
                        "tax_obligation_period": period,
                    },
                    business=True,
                )
            )
            sales_rows.append((subject, amount, "fact.gross_fen"))
            payables.append((subject, "service_sale", amount, self.customer, "primary", "inflow"))
        agreement, draw = f"agreement-{period}", f"draw-{period}"
        base.append(
            self.item(
                "loan_agreement",
                agreement,
                {
                    "period": period,
                    "lender_id": self.lender,
                    "lender_is_licensed": True,
                    "currency": "CNY",
                    "annual_rate_percent": "3.65",
                    "day_count_basis": "actual_365",
                    "maturity_date": month_at(index + 1) + "-01",
                    "loan_term": "short_term",
                },
            )
        )
        base.append(
            self.item(
                "loan_drawdown",
                draw,
                {
                    "period": period,
                    "agreement_id": agreement,
                    "principal_fen": 1000000,
                    "actual_date": period + "-01",
                    "bank_account_id": self.bank,
                },
                business=True,
            )
        )
        payables.append((draw, "loan_drawdown", 1000000, self.lender, "principal", "outflow"))
        bank_rows.append(entry(draw, period + "-01", 1000000))
        bank_matches.append(match(draw, "loan_drawdown", draw))
        asset_id = self.entity("asset", f"合成设备-{period}")
        asset_subject = f"acquisition-{period}"
        base.append(
            self.item(
                "asset",
                asset_subject,
                {
                    "period": period,
                    "asset_id": asset_id,
                    "asset_type": "fixed",
                    "supplier_id": self.supplier,
                    "acquisition_date": period + "-02",
                    "cost_fen": 120000,
                    "acquisition_basis": "direct_purchase",
                },
                business=True,
            )
        )
        reserve_rows = []
        for i in range(8 + odd):
            kind = "managed_reserve_refund" if i % 2 else "managed_reserve_expense"
            subject, amount = f"reserve-{period}-{i}", 500 + i
            channel = "bank" if i < 4 else "cash"
            base.append(
                self.item(
                    kind,
                    subject,
                    {
                        "period": period,
                        "actual_date": period + "-10",
                        f"{channel}_account_id": getattr(self, channel),
                        "amount_fen": amount,
                        "counterparty_id": self.supplier,
                    },
                    business=True,
                )
            )
            reserve_rows.append((subject, amount if i % 2 else -amount, "fact.amount_fen"))
            if channel == "bank":
                bank_rows.append(entry(subject, period + "-10", amount if i % 2 else -amount))
                bank_matches.append(match(subject, kind, subject))
        self.save(base)
        acquisitions.extend(item["subject_id"] for item in base if item["kind"] != "loan_agreement")
        self.publish(acquisitions)
        amendments = 0
        amended_subjects = []
        for i in range(1, expense_count, 20):
            subject = f"expense-{period}-{i}"
            expense_rows[i] = self.revise_expense(
                subject, self.inputs[subject][1]["amount_fen"] + 1_000_000, publish=False
            )
            amended_subjects.append(subject)
            amendments += 1
        self.publish(amended_subjects)
        if carried_in:
            payables.append(carried_in)

        with self.engine.store.connection(read_only=True) as connection:
            for i, employee in enumerate(self.employees):
                subject = f"wage-{period}-{i}"
                values = json.loads(
                    connection.execute(
                        "SELECT outcome FROM calculation WHERE id=?", (self.results[subject],)
                    ).fetchone()[0]
                )["values"]
                payables.append((subject, "payroll", values["net_fen"], employee, "net", "outflow"))
        payments = []
        for i, (source, kind, amount, party, obligation, direction) in enumerate(payables):
            subject = f"payment-{period}-{i}"
            day = period + ("-20" if kind == "loan_drawdown" else "-15")
            payments.append(
                self.item(
                    "payment",
                    subject,
                    {
                        "period": period,
                        "actual_date": day,
                        "direction": direction,
                        "bank_account_id": self.bank,
                        "counterparty_id": party,
                        "amount_fen": amount,
                        "allocations": [
                            {
                                "source_kind": kind,
                                "source_id": source,
                                "obligation": obligation,
                                "amount_fen": amount,
                            }
                        ],
                    },
                    business=True,
                )
            )
            bank_rows.append(entry(subject, day, amount if direction == "inflow" else -amount))
            bank_matches.append(match(subject, "payment", subject))
        self.save(payments)
        self.publish([row["subject_id"] for row in payments])
        interest = f"interest-{period}"
        self.save(
            [
                self.item(
                    "loan_interest",
                    interest,
                    {
                        "period": period,
                        "agreement_id": agreement,
                        "drawdown_id": draw,
                        "period_start": period + "-01",
                        "period_end_exclusive": period + "-20",
                    },
                    business=True,
                )
            ]
        )
        self.publish([interest])
        activate_asset(
            self.engine,
            proof,
            f"activation-{period}",
            {
                "period": period,
                "asset_id": asset_id,
                "in_use_date": period + "-02",
                "useful_life_months": 120,
                "residual_fen": 0,
                "benefit_area": "administration",
                "rounding_policy": "floor_final_remainder",
            },
        )
        consume_assets(self.engine, proof, period)
        tax_subject = f"tax-{period}"
        last_day = calendar.monthrange(int(period[:4]), int(period[5:]))[1]
        self.save(
            [
                self.item(
                    "tax_assessment",
                    tax_subject,
                    {
                        "period": period,
                        "period_start": period + "-01",
                        "period_end": f"{period}-{last_day}",
                        "vat_policy_id": "vat",
                        "surtax_policy_id": "surtax",
                    },
                )
            ]
        )
        self.publish([tax_subject])
        bank_rows.sort(key=lambda row: (row["actual_date"], row["reference"]))
        statement, reconciliation = f"statement-{period}", f"reconcile-{period}"
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

        with self.engine.store.connection(read_only=True) as connection:
            tax_values = json.loads(
                connection.execute(
                    "SELECT outcome FROM calculation WHERE id=?", (self.results[tax_subject],)
                ).fetchone()[0]
            )["values"]
        rows_by_category = {
            "payroll": wage_rows,
            "transactions": [*expense_rows, *sales_rows, *reserve_rows],
            "bank": [
                (item["subject_id"], item["data"]["amount_fen"], "fact.amount_fen")
                for item in payments
            ],
            "assets": [(asset_subject, 120000, "fact.cost_fen")],
            "financing": [(draw, 1000000, "fact.principal_fen")],
            "tax": [(tax_subject, tax_values["payable_vat_fen"], "result.payable_vat_fen")],
        }
        resolution_items = []
        for category, rows in rows_by_category.items():
            ev, source = self.source_rows(period, category, rows)
            resolution_items.extend(self.resolve_rows(period, rows, source))
            materials[category] = [ev]
        self.save(resolution_items)
        # A separate, explicitly supportive owner note covers confirmations and
        # policy/assessment evidence; it is not a replacement for the CSV rows.
        from material_fixture import supporting_text

        supporting_text(self.engine, proof, period=period)
        materials.setdefault("transactions", []).append(proof)
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
            "employees": len(self.employees),
            "amendments": amendments,
            "closed_corrections": closed_corrections,
            "cross_month_settlements": int(bool(carried_in)),
            "seconds": time.perf_counter() - started,
            "closed": close,
        }
        self.month_stats.append(stats)
        self.checkpoint()
        return stats

    def describe(self):
        with self.engine.store.connection(read_only=True) as connection:
            counts = {
                table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in (
                    "subject",
                    "fact_revision",
                    "calculation",
                    "calculation_publication",
                    "voucher_version",
                    "period_close",
                    "evidence",
                    "entity",
                )
            }
            kinds = dict(connection.execute("SELECT kind,count(*) FROM subject GROUP BY kind"))
        return {
            "company": self.company,
            "root": str(self.root),
            "employee_count": self.employees_count,
            "monthly_business_count": self.businesses,
            "business_count": len(self.business_subjects),
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
            "kinds": kinds,
        }
