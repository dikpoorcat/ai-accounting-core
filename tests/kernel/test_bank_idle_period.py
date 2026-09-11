"""Published bank context can establish an idle month without hiding business."""

import itertools
import sqlite3
from types import SimpleNamespace
from typing import ClassVar, Literal

import pytest

from ai_accounting.kernel.contracts import Fact, KernelError, Outcome
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods
from ai_accounting.kernel.service import default_registry
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.types import YearMonth


class BusinessWithoutJournal(Fact):
    kind: ClassVar[str] = "test_business_without_journal"
    material_category: ClassVar[str] = "bank"


class CountedObservation(Fact):
    kind: ClassVar[str] = "test_counted_observation"
    material_category: ClassVar[str] = "bank"
    activity_count_field: ClassVar[str] = "count"
    mode: Literal["zero", "missing", "null", "boolean", "negative"]


def count_observation(version, context):
    values = {"zero": 0, "null": None, "boolean": False, "negative": -1}
    return Outcome(
        (), {} if version.fact.mode == "missing" else {"count": values[version.fact.mode]}
    )


@pytest.fixture
def book(tmp_path):
    registry = default_registry()
    registry.register(BusinessWithoutJournal, lambda version, context: Outcome((), {}))
    registry.register(CountedObservation, count_observation)
    engine = Engine(
        Store.create(tmp_path / "company.sqlite", registry, "company", "911100000000000001", "db")
    )
    proof = engine.register_evidence(
        b"Synthetic idle-account facts and owner confirmation",
        "text/plain",
        "fixture",
        request_id="evidence",
    )["digest"]
    counter = itertools.count()

    def save(kind, subject, data, revision=0):
        return engine.save_fact(
            kind,
            subject,
            data,
            evidence=(proof,),
            expected_revision=revision,
            request_id=f"save-{next(counter)}",
        )

    def publish(*subjects):
        preview = engine.preview(list(subjects))
        return engine.confirm(
            list(subjects),
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id=f"publish-{next(counter)}",
        )

    periods = Periods(engine)

    def inventory(month="2026-09"):
        for category in MATERIAL_CATEGORIES:
            periods.inventory(
                month,
                category,
                evidence=[],
                expected=0,
                no_business=True,
                confirmation_evidence=proof,
                request_id=f"inventory-{month}-{category}",
            )

    def close(month="2026-09"):
        preview = periods.preview_close(month, owner_confirmation=proof)
        return periods.close(
            month,
            owner_confirmation=proof,
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id="close-" + month,
        )

    return SimpleNamespace(
        engine=engine,
        save=save,
        publish=publish,
        periods=periods,
        proof=proof,
        inventory=inventory,
        close=close,
    )


def statement_data(month="2026-09", entries=()):
    return {
        "period": month,
        "bank_account_id": "bank",
        "opening_fen": 0,
        "closing_fen": sum(x["signed_fen"] for x in entries),
        "entries": list(entries),
    }


def empty_bank(book, *, publish_count=3, month="2026-09", opening=True):
    subjects = []
    if opening:
        book.save(
            "bank_opening",
            "opening",
            {"period": month, "bank_account_id": "bank", "opening_fen": 0, "basis": "new_account"},
        )
        subjects.append("opening")
    book.save("bank_statement", "statement-" + month, statement_data(month))
    book.save(
        "bank_reconciliation",
        "reconciliation-" + month,
        {
            "period": month,
            "statement_id": "statement-" + month,
            "bank_account_id": "bank",
            "matches": [],
        },
    )
    subjects.extend(("statement-" + month, "reconciliation-" + month))
    if publish_count:
        book.publish(*subjects[:publish_count])
    return subjects


def issues(book):
    with book.engine.store.connection(read_only=True) as connection:
        return Periods.completeness(
            connection,
            YearMonth("2026-09").ordinal,
            book.engine.store.registry,
            material_coverage={"issues": []},
        )[1]


def bank_conflict(items):
    return any(row["field"] == "materials.bank" and "无业务确认" in row["message"] for row in items)


def test_published_empty_bank_context_can_close_and_remains_frozen(book):
    subjects = empty_bank(book)
    book.inventory()
    assert not issues(book)
    book.close()
    frozen = book.periods.closed_report("2026-09")
    assert len(frozen["calculations"]) == len(subjects)
    assert frozen["vouchers"] == []
    assert frozen["facts"]
    assert not book.engine.overview("2026-09")["accounts"]
    empty_bank(book, month="2026-10", opening=False)
    book.inventory("2026-10")
    book.close("2026-10")
    assert book.periods.closed_report("2026-09") == frozen


@pytest.mark.parametrize("publish_count", [0, 1, 2])
def test_each_unpublished_bank_context_still_blocks_close(book, publish_count):
    subjects = empty_bank(book, publish_count=publish_count)
    book.inventory()
    with pytest.raises(KernelError) as failure:
        book.periods.preview_close("2026-09", owner_confirmation=book.proof)
    assert failure.value.code == "period_not_ready"
    assert any(
        item["field"] == subjects[publish_count] and "尚未正式处理" in item["message"]
        for item in failure.value.details["fact_issues"]
    )


def test_current_empty_fact_cannot_use_a_stale_published_summary(book):
    empty_bank(book)
    book.inventory()
    book.save("bank_statement", "statement-2026-09", statement_data(), 1)
    assert bank_conflict(issues(book))
    with pytest.raises(KernelError, match="关账条件"):
        book.periods.preview_close("2026-09", owner_confirmation=book.proof)
    book.publish("statement-2026-09", "reconciliation-2026-09")
    assert not issues(book)
    book.close()


@pytest.mark.parametrize("amounts", [(1000,), (1000, -1000)])
def test_nonempty_statement_is_activity_even_when_net_change_is_zero(book, amounts):
    entries = [
        {"reference": str(i), "actual_date": "2026-09-01", "signed_fen": value}
        for i, value in enumerate(amounts)
    ]
    book.save("bank_statement", "statement", statement_data(entries=entries))
    book.publish("statement")
    book.inventory()
    assert bank_conflict(issues(book))


@pytest.mark.parametrize("publish_funds", [False, True])
def test_later_real_funds_invalidate_idle_bank_confirmation(book, publish_funds):
    empty_bank(book)
    book.inventory()
    book.save(
        "funding",
        "receipt",
        {
            "period": "2026-09",
            "owner_id": "owner",
            "amount_fen": 1000,
            "funding_kind": "capital",
            "actual_date": "2026-09-15",
            "bank_account_id": "bank",
        },
    )
    if publish_funds:
        book.save(
            "bank_statement",
            "statement-2026-09",
            statement_data(
                entries=[
                    {
                        "reference": "receipt",
                        "actual_date": "2026-09-15",
                        "signed_fen": 1000,
                    }
                ]
            ),
            1,
        )
        book.save(
            "bank_reconciliation",
            "reconciliation-2026-09",
            {
                "period": "2026-09",
                "statement_id": "statement-2026-09",
                "bank_account_id": "bank",
                "matches": [
                    {"reference": "receipt", "source_kind": "funding", "source_id": "receipt"}
                ],
            },
            1,
        )
        book.publish("receipt", "statement-2026-09", "reconciliation-2026-09")
    assert bank_conflict(issues(book))
    with pytest.raises(KernelError, match="关账条件"):
        book.periods.preview_close("2026-09", owner_confirmation=book.proof)


def test_no_journal_does_not_mean_no_business(book):
    book.save(BusinessWithoutJournal.kind, "business", {"period": "2026-09"})
    book.publish("business")
    book.inventory()
    assert bank_conflict(issues(book))


@pytest.mark.parametrize("mode", ["missing", "null", "boolean", "negative"])
def test_only_exact_published_integer_zero_proves_no_activity(book, mode):
    book.save(CountedObservation.kind, "observation", {"period": "2026-09", "mode": mode})
    book.publish("observation")
    book.inventory()
    assert bank_conflict(issues(book))


def test_activity_query_does_not_read_statement_child_rows(book):
    entries = [
        {"reference": str(i), "actual_date": "2026-09-01", "signed_fen": 1 if i % 2 else -1}
        for i in range(2000)
    ]
    book.save("bank_statement", "large-statement", statement_data(entries=entries))
    book.publish("large-statement")
    book.inventory()
    child_reads = []

    def authorizer(action, table, column, database, origin):
        if action == sqlite3.SQLITE_READ and table == "fact_bank_statement_entries":
            child_reads.append((table, column))
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    with book.engine.store.connection(read_only=True) as connection:
        connection.set_authorizer(authorizer)
        _, found, _ = Periods.completeness(
            connection,
            YearMonth("2026-09").ordinal,
            book.engine.store.registry,
            material_coverage={"issues": []},
        )
    assert bank_conflict(found)
    assert child_reads == []
