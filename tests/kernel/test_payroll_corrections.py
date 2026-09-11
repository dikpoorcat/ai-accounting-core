"""Real SQLite publication chains for payroll sources, settlement and closed periods."""

from __future__ import annotations

from collections import defaultdict

import pytest
from test_payroll import actual, contribution_policy, income_tax_policy, opening, payroll, profile

from ai_accounting.kernel.contracts import KernelError, NeedsInformation, Read
from ai_accounting.kernel.domains.payroll import PayrollOpeningState, PayrollProfile
from ai_accounting.kernel.domains.transactions import Allocation, Overpayment, Payment
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.materials import Materials
from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods
from ai_accounting.kernel.service import default_registry
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.types import YearMonth, canonical


class Company:
    def __init__(self, path):
        self.engine = Engine(
            Store.create(
                path,
                default_registry(),
                "payroll-company",
                "91310000123456789A",
                "payroll-db",
            )
        )
        self.sequence = 0
        self.materials = defaultdict(list)
        self.owner_confirmation = self.engine.register_evidence(
            "负责人确认本次已留存全部相关材料".encode(),
            "text/plain",
            "owner confirmation",
            request_id=self.request(),
        )["digest"]

    def request(self):
        self.sequence += 1
        return f"test-command-{self.sequence}"

    def save(self, fact, subject, revision=0):
        confirmed = canonical(
            {
                "subject": subject,
                "revision": revision + 1,
                "confirmed_facts": fact.model_dump(mode="json"),
            }
        )
        evidence = self.engine.register_evidence(
            confirmed.encode(),
            "application/json",
            f"{subject}-confirmed-facts",
            request_id=self.request(),
        )["digest"]
        result = self.engine.save_fact(
            fact.kind,
            subject,
            fact.model_dump(mode="json"),
            evidence=(evidence,),
            expected_revision=revision,
            request_id=self.request(),
        )
        self.materials[type(fact).material_category].append((str(fact.period), evidence))
        if type(fact).material_category is None:
            return result
        materials = Materials(self.engine)
        source = materials.receive(
            "test-source-" + evidence,
            {
                "period": str(fact.period),
                "evidence_digest": evidence,
                "category": type(fact).material_category,
                "purpose": "supporting",
                "supporting_purpose": "测试生成的类型化事实确认记录，不是原始业务表格",
                "specification": {
                    "format": "text",
                    "all_pages_reviewed": True,
                    "passages": [{"location": "confirmed-facts", "page": 1, "excerpt": confirmed}],
                },
            },
            evidence=(evidence,),
            expected_revision=0,
            request_id=self.request(),
        )
        materials.resolve(
            "test-resolution-" + evidence,
            {
                "period": str(fact.period),
                "source_id": source["subject_id"],
                "source_fact_id": source["fact_id"],
                "location": "confirmed-facts",
                "treatment": "supporting",
                "reason": "确认记录仅证明上述已保存的类型化事实，不代表额外业务行",
            },
            evidence=(evidence,),
            expected_revision=0,
            request_id=self.request(),
        )
        return result

    def publish(self, *subjects, correction_period=None):
        preview = self.engine.preview(list(subjects), correction_period=correction_period)
        result = self.engine.confirm(
            list(subjects),
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id=self.request(),
            correction_period=correction_period,
        )
        return preview, {item["subject_id"]: item for item in result["results"]}

    def current(self, subject, kind="payroll"):
        with self.engine.store.connection(read_only=True) as connection:
            return self.engine.store.select(connection, Read("calculation", kind, "@" + subject))[0]

    def pending(self):
        with self.engine.store.connection(read_only=True) as connection:
            return {row[0] for row in connection.execute("SELECT DISTINCT subject_id FROM pending")}

    def count(self, table):
        with self.engine.store.connection(read_only=True) as connection:
            return connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def close(self, period):
        periods = Periods(self.engine)
        for category in MATERIAL_CATEGORIES:
            # Retained earlier source documents still explain corrections in a later month.
            evidence = sorted({ev for month, ev in self.materials[category] if month <= period})
            periods.inventory(
                period,
                category,
                evidence=evidence,
                expected=len(evidence),
                no_business=not evidence,
                confirmation_evidence=self.owner_confirmation,
                request_id=self.request(),
            )
        preview = periods.preview_close(period, owner_confirmation=self.owner_confirmation)
        periods.close(
            period,
            owner_confirmation=self.owner_confirmation,
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id=self.request(),
        )
        return periods.closed_report(period)


@pytest.fixture
def company(tmp_path):
    result = Company(tmp_path / "payroll.sqlite")
    for fact, subject in (
        (profile(effective_to="2026-02"), "profile"),
        (contribution_policy(), "contributions"),
        (income_tax_policy(), "income-tax"),
        (opening(), "opening"),
        (payroll(), "january"),
        (payroll(period="2026-02"), "february"),
    ):
        result.save(fact, subject)
    return result


def payment(amount=907_400):
    return Payment(
        period="2026-02",
        actual_date="2026-02-10",
        direction="outflow",
        bank_account_id="bank",
        counterparty_id="employee",
        amount_fen=amount,
        allocations=(
            Allocation(
                source_kind="payroll",
                source_id="january",
                obligation="net",
                amount_fen=amount,
            ),
        ),
    )


def test_sqlite_actual_backfill_recomputes_its_employee_chain_and_preserves_numbers(company):
    company.save(
        PayrollProfile(**profile().model_dump() | {"employee_id": "other"}), "other-profile"
    )
    company.save(
        PayrollOpeningState(**opening().model_dump() | {"employee_id": "other"}), "other-opening"
    )
    company.save(payroll(employee_id="other", profile_id="other-profile"), "other-january")
    company.save(
        payroll(period="2026-02", employee_id="other", profile_id="other-profile"), "other-february"
    )
    _, first = company.publish("january", "february", "other-january", "other-february")
    before = {sid: company.current(sid) for sid in first}
    saved = company.save(actual(), "actual")
    assert set(saved["pending"]) == company.pending() == {"january", "february"}
    preview, amended = company.publish("actual")
    assert set(preview["subjects"]) == {"january", "february"}
    assert company.pending() == set()
    for subject in ("january", "february"):
        assert amended[subject]["voucher_number"] == first[subject]["voucher_number"]
        assert company.current(subject).id != before[subject].id
    assert company.current("january").values["net_fen"] == 888_000
    assert company.current("february").values["tax_state"]["cumulative_withheld_tax_fen"] == 24_600
    for subject in ("other-january", "other-february"):
        assert company.current(subject) == before[subject]
    old = company.engine.trace(before["january"].id)
    assert old["calculation"]["outcome"]["values"]["net_fen"] == 907_400
    assert all(item["kind"] != "payroll_contribution_actual" for item in old["facts"])
    assert any(
        item["id"] == saved["fact_id"]
        for item in company.engine.trace(company.current("january").id)["facts"]
    )


def test_sqlite_payment_stays_immutable_and_unexplained_overpayment_blocks_publication(company):
    company.save(payment(), "payment")
    _, initial = company.publish("january", "february", "payment")
    paid_calculation = company.current("payment", "payment")
    with company.engine.store.connection(read_only=True) as connection:
        paid_fact = company.engine.store.current_fact(connection, "payment")
    company.save(actual(), "actual")
    counts = {
        name: company.count(name) for name in ("calculation", "voucher_version", "voucher_line")
    }
    with pytest.raises(NeedsInformation) as error:
        company.engine.preview(["actual"])
    assert error.value.response()["fact_issues"][0]["field"] == "overpayment"
    assert company.current("payment", "payment") == paid_calculation
    assert {name: company.count(name) for name in counts} == counts
    assert company.pending() == {"january", "february", "payment"}

    company.save(
        Overpayment(
            period="2026-02",
            source_kind="payroll",
            source_id="january",
            obligation_name="net",
            counterparty_id="employee",
            amount_fen=19_400,
            recovery_right_confirmed=True,
        ),
        "recovery",
    )
    _, corrected = company.publish("actual", "recovery")
    assert corrected["payment"]["voucher_number"] == initial["payment"]["voucher_number"]
    assert company.current("payment", "payment").values["amount_fen"] == 907_400
    with company.engine.store.connection(read_only=True) as connection:
        assert company.engine.store.current_fact(connection, "payment") == paid_fact
        outstanding = connection.execute(
            "SELECT coalesce((SELECT amount FROM balance "
            "WHERE category='payable' AND balance_key='payroll:january:net'),0)"
        ).fetchone()[0]
        bank_total = connection.execute(
            "SELECT credit FROM monthly_account WHERE period=? AND account='1002'",
            (YearMonth("2026-02").ordinal,),
        ).fetchone()[0]
    assert outstanding == 0
    assert bank_total == 907_400
    assert company.current("recovery", "overpayment").values["overpayment_fen"] == 19_400
    assert company.pending() == set()
    with pytest.raises(KernelError) as immutable:
        company.save(payment(888_000), "payment", revision=1)
    assert immutable.value.code == "immutable_fact"


def test_sqlite_closed_source_correction_keeps_history_and_reuses_open_compensation(company):
    company.publish("january", "february")
    original = company.current("january")
    frozen = company.close("2026-01")
    original_ledger = company.engine.ledger("2026-01")
    first_source = company.save(actual(), "actual")
    assert set(first_source["pending"]) == {"january", "february"}
    assert Periods(company.engine).closed_report("2026-01") == frozen
    before_count = company.count("calculation")
    with pytest.raises(KernelError) as missing_period:
        company.publish("actual")
    assert missing_period.value.code == "closed_correction_required"
    assert company.count("calculation") == before_count
    assert company.current("january") == original

    _, first_correction = company.publish("actual", correction_period="2026-03")
    assert company.engine.ledger("2026-01") == original_ledger
    march = company.engine.ledger("2026-03")
    assert len(march) == 2
    reversal = next(item for item in march if item["reverses_id"])
    replacement = next(item for item in march if item["reverses_id"] is None)
    assert reversal["reverses_id"] == original_ledger[0]["id"]
    assert replacement["number"] == first_correction["january"]["voucher_number"]
    assert company.current("january").values["rule_versions"] == original.values["rule_versions"]
    assert company.pending() == set()

    company.save(actual(employee=110_000, employer=220_000), "actual", revision=1)
    _, second_correction = company.publish("actual")
    assert second_correction["january"]["voucher_number"] == replacement["number"]
    assert company.engine.ledger("2026-01") == original_ledger
    second_march = company.engine.ledger("2026-03")
    assert len(second_march) == 2
    assert next(x for x in second_march if x["reverses_id"]) == reversal
    assert company.current("january").values["net_fen"] == 878_300
    assert company.pending() == set()
    assert Periods(company.engine).closed_report("2026-01") == frozen

    projections = {
        period: company.engine.overview(period)["accounts"]
        for period in ("2026-01", "2026-02", "2026-03")
    }
    company.engine.rebuild_projections(request_id=company.request())
    assert {
        period: company.engine.overview(period)["accounts"] for period in projections
    } == projections


def test_sqlite_correction_after_compensation_is_closed_targets_latest_frozen_result(company):
    company.publish("january", "february")
    january_frozen = company.close("2026-01")
    company.save(actual(), "actual")
    company.publish("actual", correction_period="2026-03")
    company.save(actual(employee=110_000, employer=220_000), "actual", revision=1)
    company.publish("actual")
    company.close("2026-02")
    march_frozen = company.close("2026-03")
    march_ledger = company.engine.ledger("2026-03")
    latest_replacement = next(row for row in march_ledger if row["reverses_id"] is None)

    company.save(actual(employee=120_000, employer=240_000), "actual", revision=2)
    company.publish("actual", correction_period="2026-04")
    april = company.engine.ledger("2026-04")
    assert len(april) == 4  # The current January replacement and February are both now closed.
    assert any(row["reverses_id"] == latest_replacement["id"] for row in april)
    assert company.engine.ledger("2026-03") == march_ledger
    assert Periods(company.engine).closed_report("2026-01") == january_frozen
    assert Periods(company.engine).closed_report("2026-03") == march_frozen
    assert company.pending() == set()
