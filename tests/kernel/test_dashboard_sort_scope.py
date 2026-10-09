"""Sorting locators must not traverse ancestry that cannot add displayed objects."""

import sqlite3
from types import SimpleNamespace

import pytest

from ai_accounting.kernel.dashboard_sort import business_sort_metadata
from ai_accounting.kernel.schema import table_name
from ai_accounting.kernel.types import YearMonth, canonical


@pytest.mark.parametrize("scenario", ["individual_payment", "no_balances", "mixed"])
@pytest.mark.parametrize("funds", [False, True])
def test_irrelevant_sort_ancestry_does_not_increase_work(scenario, funds, record_property, monkeypatch):
    from ai_accounting.kernel import report_open_contribution

    monkeypatch.setattr(report_open_contribution, "verify_published_source_bindings",
                        lambda reads, identifiers: frozenset())
    with sqlite3.connect(":memory:") as connection:
        connection.row_factory = sqlite3.Row
        connection.executescript(
            "CREATE TABLE calculation(id TEXT PRIMARY KEY,subject_id TEXT,fact_id TEXT,"
            "kind TEXT,period INTEGER,outcome TEXT);"
            "CREATE TABLE dependency_calculation(calculation_id TEXT,upstream_id TEXT,"
            "PRIMARY KEY(calculation_id,upstream_id));"
        )
        fields = ("actual_date", "counterparty_id", "payment_method")
        models = {}
        for kind in ("payment", "funding"):
            models[kind] = SimpleNamespace(model_fields=dict.fromkeys(fields))
            connection.execute(
                f"CREATE TABLE {table_name(kind)}(revision_id TEXT PRIMARY KEY,period INTEGER,"
                "actual_date TEXT,counterparty_id TEXT,payment_method TEXT)"
            )
        period = YearMonth("2026-09").ordinal

        def calculation(ident, kind, outcome):
            connection.execute(
                "INSERT INTO calculation VALUES(?,?,?,?,?,?)",
                (ident, "subject-" + ident, "fact-" + ident, kind, period, canonical(outcome)),
            )

        kind = "funding" if scenario == "no_balances" else "payment"
        calculation("root", kind, {"balances": [] if scenario == "no_balances" else [
            {"key": "old-obligation", "amount": -100}
        ]})
        connection.execute(
            f"INSERT INTO {table_name(kind)} VALUES(?,?,?,?,?)",
            ("fact-root", period, "2026-09-01", "actual-party", "individual"),
        )
        identifiers = ["root"]
        expected = {"root": {"date": "2026-09-01", "party": "actual-party",
                             "identities": ("actual-party",)}}
        if scenario == "mixed":
            calculation("required", "payment", {"balances": [{"key": "required-obligation"}]})
            calculation("required-source", "expense", {"values": {"obligations": [
                {"key": "required-obligation", "name": "primary", "counterparty_id": "source-party"}
            ]}})
            connection.execute("INSERT INTO dependency_calculation VALUES(?,?)",
                               ("required", "required-source"))
            connection.execute(
                f"INSERT INTO {table_name('payment')} VALUES(?,?,?,?,?)",
                ("fact-required", period, "2026-09-02", None, "bank_batch"),
            )
            identifiers.append("required")
            expected["required"] = {"date": "2026-09-02", "party": "source-party",
                                    "identities": ("source-party",)}
        snapshot = SimpleNamespace(
            connection=connection, store=SimpleNamespace(registry=SimpleNamespace(models=models)),
            metadata=SimpleNamespace(prime_profiles=lambda *_: None), party=lambda ident: ident,
            reads=SimpleNamespace(verify_sql_outcomes=lambda *_: None),
            profiles={kind: {ident: {"display_name": ident} for ident in ("actual-party", "source-party")}
                      for kind in ("employee", "counterparty")},
            current_profiles={kind: {ident: {"display_name": ident} for ident in ("actual-party", "source-party")}
                              for kind in ("employee", "counterparty")},
        )
        previous, baseline = 0, None
        for depth in (10, 100, 400):
            for index in range(previous, depth):
                ident = f"parent-{index}"
                calculation(ident, "expense", {"values": {"obligations": [
                    {"key": "old-obligation", "name": "primary", "counterparty_id": "old-party"}
                ]}})
                connection.execute(
                    "INSERT INTO dependency_calculation VALUES(?,?)",
                    ("root" if index == 0 else f"parent-{index - 1}", ident),
                )
            steps = 0

            def progress():
                nonlocal steps
                steps += 1
                return 0

            connection.set_progress_handler(progress, 1)
            try:
                result = business_sort_metadata(snapshot, identifiers, funds=funds)
            finally:
                connection.set_progress_handler(None, 0)
            assert result == expected
            record_property(f"ancestors_{depth}_sqlite_vm_steps", steps)
            if baseline is None:
                baseline = steps
            else:
                assert steps <= baseline + 100
            previous = depth
