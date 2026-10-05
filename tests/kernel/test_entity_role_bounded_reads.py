"""Exact role reads retain source proofs while unrelated role histories grow."""

import json

import pytest
from entity_fixture import seed_entities
from stage9_metrics import measure_work
from test_employee_head_adoption_reads import heads
from test_identity_corrections import confirm, expense
from test_identity_corrections import identity_engine as identity_engine
from test_integrity_content import damage
from test_payroll import payroll, profile
from test_payroll_corrections import Company
from test_settlement_late_reviews import prepared

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_reads import payroll_head_identities
from ai_accounting.kernel.entity_references import current_role_matches, verify_hits
from ai_accounting.kernel.types import canonical


def save_profile(company, employee, subject, revision=0):
    fact = profile(employee_id=employee, social_insurance_base_fen=100_000 + revision)
    return company.engine.save_fact(
        fact.kind, subject, fact.model_dump(mode="json"),
        evidence=(company.owner_confirmation,), expected_revision=revision,
        request_id=company.request(),
    )["fact_id"]


def test_role_lookup_work_ignores_unrelated_objects_and_historical_revisions(tmp_path):
    company = Company(tmp_path / "role-scope.sqlite")
    engine = company.engine
    seed_entities(engine, [(f"selected-{i}", "person", None) for i in range(24)])
    requested = {
        save_profile(company, f"selected-{i}", f"selected-{i}"): f"selected-{i}"
        for i in range(24)
    }

    def read():
        with engine.store.connection(read_only=True) as connection:
            return current_role_matches(
                connection, requested, "employee", registry=engine.store.registry
            )

    before, first = measure_work(engine, read)
    seed_entities(engine, [(f"unrelated-{i}", "person", None) for i in range(100)])
    for index in range(100):
        for revision in range(3):
            save_profile(company, f"unrelated-{index}", f"unrelated-{index}", revision)
    after, later = measure_work(engine, read)
    assert first == later == requested
    statement = next(row["statement"] for row in after["sql"]
                     if row["statement"].startswith("SELECT * FROM entity_reference_current"))
    with engine.store.connection(read_only=True) as connection:
        plan = [row[3] for row in connection.execute(
            "EXPLAIN QUERY PLAN " + statement, (canonical(sorted(requested)),)
        )]
    assert any("SEARCH entity_reference_current" in item and "fact_id=?" in item for item in plan)
    assert not any("entity_current_role" in item for item in plan)
    for counter in ("returned_rows", "returned_value_bytes", "stdlib_json_loads",
                    "typed_fact_json_decodes", "raw_fact_json_decodes"):
        assert after["counters"].get(counter, 0) == before["counters"].get(counter, 0)
    assert before["counters"].get("sqlite_vm_steps", 0) > 0
    assert (
        after["counters"].get("sqlite_vm_steps", 0)
        <= before["counters"]["sqlite_vm_steps"] + 100
    )
    print(json.dumps({"role_plan": plan, "before": before["counters"],
                      "after": after["counters"]}, ensure_ascii=False))


@pytest.mark.parametrize("corruption", ["role", "source_digest", "source_body", "omitted"])
def test_role_lookup_still_proves_complete_source_and_all_roles(tmp_path, corruption):
    company = Company(tmp_path / f"role-damage-{corruption}.sqlite")
    engine = company.engine
    seed_entities(engine, [("employee-a", "person", None)])
    fact_id = save_profile(company, "employee-a", "profile")
    if corruption == "source_body":
        damage(engine, "fact_payroll_profile",
               "UPDATE fact_payroll_profile SET social_insurance_base_fen=999 WHERE revision_id=?",
               (fact_id,))
    elif corruption == "omitted":
        damage(engine, "entity_reference_current",
               "DELETE FROM entity_reference_current WHERE fact_id=?", (fact_id,))
    else:
        value = "supplier" if corruption == "role" else bytes(32)
        damage(engine, "entity_reference_current",
               f"UPDATE entity_reference_current SET {corruption}=? WHERE fact_id=?",
               (value, fact_id))
    with engine.store.connection(read_only=True) as connection:
        with pytest.raises(KernelError) as failure:
            current_role_matches(connection, [fact_id], "employee", registry=engine.store.registry)
    assert failure.value.code == (
        "content_integrity_failed" if corruption == "source_body" else "entity_reference_corrupt"
    )


def test_role_lookup_preserves_recorded_and_current_exact_correction(identity_engine):
    engine, evidence, first, second = identity_engine
    data = expense(engine, evidence, first)
    with engine.store.connection(read_only=True) as connection:
        fact_id = engine.store.current_fact(connection, "expense").id
        roles = [row[0] for row in connection.execute(
            "SELECT role FROM entity_reference_recorded WHERE fact_id=?", (fact_id,)
        )]
    assert roles
    confirm(engine, dict(
        changes=[dict(subject_id="expense", expected_revision=1, action="reassign",
                      data={**data, "counterparty_id": second})],
        evidence=[evidence], reason="explicit synthetic identity correction",
    ))
    with engine.store.connection(read_only=True) as connection:
        verify_hits(connection, [{"fact_id": fact_id}], identity_match="recorded",
                    registry=engine.store.registry)
        for role in roles:
            assert current_role_matches(connection, [fact_id], role,
                                        registry=engine.store.registry) == {fact_id: second}
        assert {row[0] for row in connection.execute(
            "SELECT entity_id FROM entity_reference_recorded WHERE fact_id=?", (fact_id,)
        )} == {first}
    damage(engine, "entity_reference_current",
           "UPDATE entity_reference_current SET entity_id=? WHERE fact_id=?", (first, fact_id))
    with engine.store.connection(read_only=True) as connection:
        with pytest.raises(KernelError, match="对象引用目录"):
            current_role_matches(connection, [fact_id], roles[0], registry=engine.store.registry)


@pytest.mark.parametrize("period,table", [
    ("2026-01", "entity_reference_recorded"),
    ("2026-02", "entity_reference_current"),
])
def test_payroll_head_role_lookup_is_bounded_in_frozen_and_open_periods(tmp_path, period, table):
    company = prepared(tmp_path)
    engine = company.engine
    if period == "2026-02":
        company.save(payroll(period="2026-02"), "february-unpublished")

    def read():
        with Dashboard(engine)._snapshot(period) as snap:
            return payroll_head_identities(snap, heads(snap))

    before, first = measure_work(engine, read)
    seed_entities(engine, [(f"other-{i}", "person", None) for i in range(40)])
    for index in range(40):
        for revision in range(3):
            save_profile(company, f"other-{index}", f"other-{index}", revision)
    after, later = measure_work(engine, read)
    assert first == later
    sql = next(row["statement"] for row in after["sql"]
               if row["statement"].startswith(f"SELECT * FROM {table}"))
    with engine.store.connection(read_only=True) as connection:
        plan = [row[3] for row in connection.execute(
            "EXPLAIN QUERY PLAN " + sql, (canonical(sorted(first)),)
        )]
    assert any(f"sqlite_autoindex_{table}_1 (fact_id=?)" in row for row in plan)
    for key in ("returned_rows", "returned_value_bytes", "stdlib_json_loads",
                "typed_fact_json_decodes", "raw_fact_json_decodes"):
        assert before["counters"].get(key, 0) == after["counters"].get(key, 0)
    assert (
        after["counters"].get("sqlite_vm_steps", 0)
        <= before["counters"].get("sqlite_vm_steps", 0) + 100
    )
    print(json.dumps({"period": period, "role_plan": plan,
                      "before": before["counters"], "after": after["counters"]},
                     ensure_ascii=False))
