"""Exercise real query text and contract indexes, counting work rather than time.

The memory fixtures contain tables and indexes only. Functional and corruption
checks use a complete temporary company; these fixtures do not replace integrity
verification of accounting data.
"""

import ast
import inspect
import re
import sqlite3
from types import SimpleNamespace

import pytest
from test_discovery import company as company_fixture

from ai_accounting.kernel import (
    business_queries,
    dashboard,
    dashboard_metadata,
    display,
    entity_references,
    report_projection,
    workflow,
)
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.discovery import Discovery, _selected_reference_rows
from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.types import canonical
from ai_accounting.kernel.work_context import WorkContext

company = company_fixture
PERIOD = 24310
SELECTED = list(range(20))


def _sql(module, unique):
    candidates = [
        node.value
        for node in ast.walk(ast.parse(inspect.getsource(module)))
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and unique in node.value
    ]
    assert len(candidates) == 1
    return candidates[0]


def _database():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    objects = production_bundle().current("company")["objects"]
    for kind in ("table", "index"):
        for item in objects:
            if item["type"] == kind:
                connection.execute(item["sql"])
    return connection


def _seed(connection, start, stop):
    for number in range(start, stop):
        s, f, c, v, e, t, u = (f"{prefix}{number:04d}" for prefix in "sfcvetu")
        for subject, fact, kind in (
            (s, f, "external_obligation"),
            ("tax-" + s, t, "tax_import_identity_v2"),
            ("supp-" + s, u, "payroll_tax_declaration_actual"),
            ("completion-" + s, "completion-" + f, "external_completion"),
        ):
            connection.execute("INSERT INTO subject(id,kind) VALUES(?,?)", (subject, kind))
            connection.execute(
                "INSERT INTO fact_revision VALUES(?,?,?,?,?)",
                (fact, subject, 1, PERIOD, b"d" * 32),
            )
            connection.execute("INSERT INTO fact_current VALUES(?,?)", (subject, fact))
        connection.execute(
            "INSERT INTO calculation VALUES(?,?,?,?,?,?,?,?)",
            (c, "completion-" + s, f, "asset_activation_batch", PERIOD, "{}", b"d" * 32, "test"),
        )
        connection.execute("INSERT INTO calculation_current VALUES(?,?)", ("completion-" + s, c))
        connection.execute("INSERT INTO voucher VALUES(?,?)", (v, number + 1))
        connection.execute(
            "INSERT INTO voucher_version VALUES(?,?,?,?,?,?)", (v, v, c, PERIOD, None, 1)
        )
        connection.execute(
            "INSERT INTO voucher_line VALUES(?,?,?,?,?,?)", (v, 1, "1001", 1, 0, None)
        )
        connection.execute(
            "INSERT INTO fact_scope VALUES(?,?,?)",
            (t, "tax_import_identity_v2", "tax-identity:" + e),
        )
        connection.execute(
            "INSERT INTO fact_tax_import_identity_v2 VALUES(?,?,?,?,?,?,?)",
            (t, PERIOD, e, "code", "name", "document", "number"),
        )
        connection.execute(
            "INSERT INTO fact_scope VALUES(?,?,?)",
            (u, "payroll_tax_declaration_actual", "scope-" + s),
        )
        connection.execute(
            "INSERT INTO discovery_fact_history VALUES(?,?,?,?,?)",
            (f, s, 1, "material_source_v2", PERIOD),
        )
        connection.execute(
            "INSERT INTO discovery_fact_current VALUES(?,?,?,?)",
            (s, f, "material_source_v2", PERIOD),
        )
        for table in ("entity_reference_current", "entity_reference_recorded"):
            # Every unrelated row shares the filter: the selected fact primary
            # key must lead even when an entity/role index is tempting.
            for path, entity, role in (("employee", "shared", "employee"), ("other", e, "other")):
                connection.execute(
                    f"INSERT INTO {table} VALUES(?,?,?,?,?,?,?)",
                    (f, path, entity, role, "external_obligation", PERIOD, b"d" * 32),
                )


def _work(connection, read):
    steps = 0

    def progress():
        nonlocal steps
        steps += 1
        return 0

    connection.set_progress_handler(progress, 1)
    try:
        result = read()
    finally:
        connection.set_progress_handler(None, 0)
    return result, steps


def _case(name):
    def ids(prefix):
        return canonical([f"{prefix}{number:04d}" for number in SELECTED])

    if name == "dashboard_accounts":
        return _sql(dashboard, "SELECT DISTINCT l.account FROM json_each"), (
            ids("v"), '["1001"]',
        ), 1
    if name == "display_asset_owners":
        return _sql(display, "WHERE c.kind IN ('asset_activation_batch'"), (ids("c"),), 20
    if name == "workflow_obligations":
        return _sql(workflow, "SELECT a.fact_id FROM json_each"), (ids("s"),), 20
    if name == "report_manifest":
        return _sql(report_projection, "c.id basis_calculation_id"), (ids("v"), PERIOD), 20
    if name in {"tax_candidates", "tax_reference_names"}:
        module = dashboard_metadata if name == "tax_candidates" else entity_references
        return _sql(module, "SELECT t.employee_id,f.id FROM json_each"), (ids("e"),), 20
    if name == "display_supplementary_scopes":
        return _sql(display, "SELECT DISTINCT f.id,f.subject_id,s.kind,f.revision,f.digest"), (
            canonical([f"scope-s{number:04d}" for number in SELECTED]),
        ), 20
    if name == "business_external_completions":
        return _sql(business_queries, "WHERE s.kind='external_completion'"), (
            canonical([f"completion-s{number:04d}" for number in SELECTED]),
        ), 20
    assert name == "work_material_headers"
    context = WorkContext(
        SimpleNamespace(store=SimpleNamespace(registry=production_bundle().registry))
    )
    sql = context._headers_sql(exact_ids=True)
    return sql + " WHERE d.kind='material_source_v2' ORDER BY d.fact_id", (ids("f"),), 20


def _ordinary_join(sql):
    return re.sub(r" INDEXED BY \w+", "", sql.replace("CROSS JOIN", "JOIN"))


CASES = (
    "dashboard_accounts", "display_asset_owners", "workflow_obligations", "report_manifest",
    "tax_candidates", "tax_reference_names", "display_supplementary_scopes",
    "business_external_completions", "work_material_headers",
)


@pytest.mark.parametrize("name", CASES)
def test_selected_read_work_is_independent_of_unrelated_history(name):
    connection = _database()
    try:
        _seed(connection, 0, 100)
        sql, parameters, count = _case(name)
        def read():
            return [tuple(row) for row in connection.execute(sql, parameters)]

        baseline, before = _work(connection, read)
        assert len(baseline) == count
        prefix = {
            "display_asset_owners": "c", "workflow_obligations": "f", "report_manifest": "v",
            "tax_candidates": "e", "tax_reference_names": "e",
            "display_supplementary_scopes": "u", "business_external_completions": "c",
            "work_material_headers": "f",
        }.get(name)
        expected = {f"{prefix}{number:04d}" for number in SELECTED} if prefix else {"1001"}
        assert {row[0] for row in baseline} == expected
        plan = [row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + sql, parameters)]
        assert plan[0].startswith("SCAN ") and "VIRTUAL TABLE" in plan[0], plan
        # This is a result equivalence oracle with the optimizer free to choose
        # the old ordinary JOIN direction over exactly the same fixture.
        old, _ = _work(connection, lambda: [
            tuple(row) for row in connection.execute(_ordinary_join(sql), parameters)
        ])
        assert sorted(old) == sorted(baseline)
        _seed(connection, 100, 1000)
        result, after = _work(connection, read)
        assert result == baseline  # Includes all declared ordering contracts.
        assert after <= before + 50, (name, before, after, plan)
    finally:
        connection.close()


@pytest.mark.parametrize("table", ["entity_reference_current", "entity_reference_recorded"])
@pytest.mark.parametrize("filters", [{"entity_id": "shared"}, {"role": "employee"}, {
    "entity_id": "shared", "role": "employee",
}])
def test_selected_entity_reference_work_uses_fact_primary_key(table, filters):
    connection = _database()
    try:
        _seed(connection, 0, 100)
        selected = [f"f{number:04d}" for number in SELECTED]
        def read():
            return [
                tuple(row)
                for row in _selected_reference_rows(connection, table, selected, **filters)
            ]
        result, before = _work(connection, read)
        assert len(result) == 20
        assert [row[0] for row in result] == selected
        assert all(row[2:4] == ("shared", "employee") for row in result)
        traced = []
        connection.set_trace_callback(traced.append)
        read()
        connection.set_trace_callback(None)
        plan = [row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + traced[0])]
        assert plan[0].startswith("SCAN ids VIRTUAL TABLE"), plan
        assert any(f"sqlite_autoindex_{table}_1 (fact_id=?)" in item for item in plan), plan
        _seed(connection, 100, 1000)
        grown, after = _work(connection, read)
        assert grown == result
        assert after <= before + 50, (before, after, plan)
    finally:
        connection.close()


@pytest.mark.parametrize("identity_match", ["current", "recorded"])
@pytest.mark.parametrize("status", ["current", "history"])
@pytest.mark.parametrize("mode", ["entity", "role", "both"])
def test_find_facts_rejects_damaged_selected_entity_reference(
    company, identity_match, status, mode
):
    proof = company.register_evidence(
        b"selected source", "text/plain", "proof", request_id="proof"
    )["digest"]
    vendor = Entities(company).register_entity(
        "organization", {"display_name": "Vendor"}, source="test", request_id="vendor"
    )["entity_id"]
    company.save_fact(
        "expense", "cost", {"period": "2026-01", "counterparty_id": vendor,
        "amount_fen": 100, "expense_class": "administration", "creditor_kind": "supplier"},
        evidence=(proof,), expected_revision=0, request_id="cost",
    )
    table = "entity_reference_" + identity_match
    with company.store.connection(read_only=True) as connection:
        match = connection.execute(f"SELECT * FROM {table}").fetchone()
    filters = {"identity_match": identity_match, "status": status}
    if mode in {"entity", "both"}:
        filters["entity_id"] = match["entity_id"]
    if mode in {"role", "both"}:
        filters["role"] = match["role"]
    discovery = Discovery(company)
    assert len(discovery.find_facts(**filters)["items"]) == 1
    with company.store.connection() as connection:
        connection.execute(f"UPDATE {table} SET source_digest=zeroblob(32)")
    with pytest.raises(KernelError) as error:
        discovery.find_facts(**filters)
    assert error.value.code == "entity_reference_corrupt"
