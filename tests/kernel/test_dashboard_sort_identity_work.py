"""Exact ancestry JSON expansion preserves every identity and proof-only source."""

import sqlite3
from collections import Counter
from types import SimpleNamespace

from ai_accounting.kernel.dashboard_sort import business_sort_metadata
from ai_accounting.kernel.schema import table_name
from ai_accounting.kernel.types import canonical


class MeasuredCursor(sqlite3.Cursor):
    def fetchall(self):
        rows = super().fetchall()
        if self.connection.measuring:
            self.connection.set_progress_handler(None, 0)
            self.connection.measuring = False
            self.connection.identity_rows = [tuple(row) for row in rows]
        return rows


class MeasuredConnection(sqlite3.Connection):
    measuring = False
    legacy = False
    steps = 0
    text_children = False

    def execute(self, query, parameters=()):
        if query.startswith("WITH RECURSIVE keys AS MATERIALIZED"):
            self.identity_query = query, parameters
            if self.text_children or self.legacy:
                query = query.replace("jsonb_each(c.outcome,", "json_each(c.outcome,")
            if self.legacy:
                query = (
                    query.split(", ancestors(id) AS", 1)[0]
                    + " SELECT DISTINCT a.root,c.id ancestor_id,c.subject_id,c.kind,"
                    "json_extract(o.value,'$.name') component,"
                    "json_extract(o.value,'$.counterparty_id') party FROM ancestry a "
                    "JOIN calculation c ON c.id=a.id,"
                    "json_each(c.outcome,'$.values.obligations') o JOIN keys k "
                    "ON k.root=a.root AND k.obligation_key=json_extract(o.value,'$.key') "
                    "WHERE json_extract(o.value,'$.counterparty_id') IS NOT NULL "
                    "UNION SELECT DISTINCT a.root,a.id,NULL,NULL,NULL,NULL FROM ancestry a "
                    "JOIN calculation c ON c.id=a.id "
                    "WHERE c.kind NOT IN ('asset_activation','asset_consumption')"
                )
                query = (
                    query.replace(
                        "ids CROSS JOIN calculation c ON c.id=ids.value CROSS JOIN ",
                        "ids JOIN calculation c ON c.id=ids.value,",
                    )
                    .replace(
                        "CROSS JOIN calculation c ON c.id=a.id CROSS JOIN "
                        "json_each(c.outcome,'$.values.obligations') o CROSS JOIN keys k",
                        "JOIN calculation c ON c.id=a.id,"
                        "json_each(c.outcome,'$.values.obligations') o "
                        "JOIN keys k",
                    )
                    .replace(
                        "CROSS JOIN calculation c ON c.id=a.id ", "JOIN calculation c ON c.id=a.id "
                    )
                )
            self.steps = 0
            self.measuring = True

            def progress():
                self.steps += 1
                return 0

            self.set_progress_handler(progress, 1)
        return self.cursor(factory=MeasuredCursor).execute(query, parameters)


def test_obligation_arrays_expand_once_instead_of_once_per_balance_key(
    monkeypatch, record_property
):
    from ai_accounting.kernel import report_open_contribution

    proof_batches = []

    def proof(reads, identifiers, *, _sql_outcomes):
        assert _sql_outcomes is True
        proof_batches.append(frozenset(identifiers))
        return frozenset()

    monkeypatch.setattr(report_open_contribution, "verify_published_source_bindings", proof)
    with sqlite3.connect(":memory:", factory=MeasuredConnection) as connection:
        connection.row_factory = sqlite3.Row
        connection.executescript(
            "CREATE TABLE calculation(id TEXT PRIMARY KEY,subject_id TEXT,fact_id TEXT,"
            "kind TEXT,period INTEGER,outcome TEXT);"
            "CREATE TABLE dependency_calculation(calculation_id TEXT,upstream_id TEXT,"
            "PRIMARY KEY(calculation_id,upstream_id));"
        )
        model = SimpleNamespace(model_fields=dict.fromkeys(("actual_date", "counterparty_id")))
        connection.execute(
            f"CREATE TABLE {table_name('funding')}(revision_id TEXT PRIMARY KEY,period INTEGER,"
            "actual_date TEXT,counterparty_id TEXT)"
        )
        connection.execute(
            "INSERT INTO calculation VALUES(?,?,?,?,?,?)",
            (
                "root",
                "root-subject",
                "root-fact",
                "funding",
                1,
                canonical({"balances": [{"key": f"key-{index}"} for index in range(128)]}),
            ),
        )
        connection.execute(
            "INSERT INTO calculation VALUES(?,?,?,?,?,?)",
            (
                "source",
                "source-subject",
                "source-fact",
                "expense",
                1,
                canonical(
                    {
                        "values": {
                            "obligations": [
                                {
                                    "key": f"key-{index}",
                                    "name": "primary",
                                    "counterparty_id": "party",
                                }
                                for index in range(512)
                            ]
                        }
                    }
                ),
            ),
        )
        connection.execute(
            "INSERT INTO calculation VALUES(?,?,?,?,?,?)",
            (
                "proof-only",
                "proof-subject",
                "proof-fact",
                "expense",
                1,
                canonical({"values": {"obligations": []}}),
            ),
        )
        connection.execute("INSERT INTO dependency_calculation VALUES('root','source')")
        connection.execute("INSERT INTO dependency_calculation VALUES('source','proof-only')")
        connection.execute(
            f"INSERT INTO {table_name('funding')} VALUES('root-fact',1,'2026-09-01',NULL)"
        )
        snapshot = SimpleNamespace(
            connection=connection,
            store=SimpleNamespace(registry=SimpleNamespace(models={"funding": model})),
            metadata=SimpleNamespace(prime_profiles=lambda *_: None),
            party=lambda ident: ident,
            reads=SimpleNamespace(verify_sql_outcomes=lambda *_: None),
            profiles={
                name: {"party": {"display_name": "party"}} for name in ("employee", "counterparty")
            },
            current_profiles={
                name: {"party": {"display_name": "party"}} for name in ("employee", "counterparty")
            },
        )
        connection.legacy = True
        legacy = business_sort_metadata(snapshot, {"root"})
        legacy_steps, legacy_rows = connection.steps, connection.identity_rows
        connection.legacy = False
        connection.text_children = True
        text = business_sort_metadata(snapshot, {"root"})
        text_steps, text_rows = connection.steps, connection.identity_rows
        connection.text_children = False
        optimized = business_sort_metadata(snapshot, {"root"})
        assert (
            optimized
            == legacy
            == text
            == {
                "root": {"date": "2026-09-01", "party": "party", "identities": ("party",)},
            }
        )
        assert connection.identity_rows == legacy_rows
        assert connection.identity_rows == text_rows
        assert proof_batches == [frozenset({"root", "source", "proof-only"})] * 3
        assert connection.steps < legacy_steps // 5
        assert connection.steps <= text_steps
        binary_steps = connection.steps
        query, parameters = connection.identity_query
        child_reads = []
        for text_children in (True, False):
            observed = Counter()

            def observe(value, observed=observed):
                observed[type(value).__name__] += 1
                observed[type(value).__name__ + "_bytes"] += len(
                    value if isinstance(value, bytes) else value.encode("utf-8")
                )
                return value

            connection.create_function("observe_child", 1, observe)
            instrumented = query.replace(
                "json_extract(o.value,", "json_extract(observe_child(o.value),"
            ).replace("json_extract(b.value,", "json_extract(observe_child(b.value),")
            if text_children:
                instrumented = instrumented.replace("jsonb_each(c.outcome,", "json_each(c.outcome,")
            actual = [tuple(row) for row in sqlite3.Connection.execute(
                connection, instrumented, parameters,
            )]
            assert actual == text_rows
            child_reads.append(observed)
        text_reads, binary_reads = child_reads
        assert text_reads["str"] == binary_reads["bytes"] > 0
        assert binary_reads["str"] == 0
        assert binary_reads["bytes_bytes"] < text_reads["str_bytes"]
        record_property("legacy_identity_sql_vm_steps", legacy_steps)
        record_property("text_child_identity_sql_vm_steps", text_steps)
        record_property("optimized_identity_sql_vm_steps", binary_steps)
        record_property("identity_sql_returned_rows", len(connection.identity_rows))
        record_property("text_child_inputs", text_reads["str"])
        record_property("binary_child_inputs", binary_reads["bytes"])
        record_property("text_child_utf8_bytes", text_reads["str_bytes"])
        record_property("binary_child_bytes", binary_reads["bytes_bytes"])
