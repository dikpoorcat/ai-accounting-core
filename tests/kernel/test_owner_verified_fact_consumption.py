"""Owner tasks consume their authenticated wage facts once."""

import pytest
from stage9_metrics import measure_work
from test_integrity_content import damage
from test_payroll import payroll, profile
from test_payroll_corrections import company as _company_fixture

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_owner import owner_tasks
from ai_accounting.kernel.domains.payroll import PayrollProfile
from ai_accounting.kernel.integrity import verify_sources
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.types import YearMonth

payroll_company = _company_fixture


def original_owner_fixture(tmp_path, monkeypatch, *, mismatch=False):
    """Construct exact old facts, then read their unchanged migrated bodies."""
    import copy
    from dataclasses import replace

    import entity_fixture
    from test_adjust_stage9_samples import original_file
    from test_source24_history import wage

    from ai_accounting.kernel.backup import verify_file
    from ai_accounting.kernel.engine import Engine
    from ai_accounting.kernel.entity_profile_history import PreEventProfile
    from ai_accounting.kernel.offline_development_upgrade import upgrade_company
    from ai_accounting.kernel.schema_bundle import production_bundle
    from ai_accounting.kernel.source24_history import PayrollPlanPreRevision
    from ai_accounting.kernel.storage import Store
    from scripts.stage9_history_adjustment import adjust_connection

    path = tmp_path / "original-owner.sqlite"
    source = original_file(path, "company")
    registry = copy.copy(source.registry)
    # Construction-only permission for synthetic historical inputs. The reader
    # and both full verifications below keep their original strict bundle.
    registry.historical_only -= {"payroll", "payroll_plan_v2"}
    constructor = Engine(Store(path, replace(source, registry=registry),
                               "synthetic-company", "synthetic-database"))
    models = entity_fixture.entity_profile_models()
    with monkeypatch.context() as patch:
        patch.setattr(entity_fixture, "entity_profile_models",
                      lambda: models | {"person": PreEventProfile})
        entity_fixture.seed_entities(constructor, [("employee", "person", None)])
    proof = constructor.register_evidence(b"explicit original owner inputs", "text/plain", "proof",
                                         request_id="original-proof")["digest"]
    fact = wage()
    embedded = type(fact).model_validate(fact.model_dump() | {
        "accounting_gross_salary_fen": fact.accounting_gross_salary_fen + int(mismatch),
    })
    plan = PayrollPlanPreRevision(
        period=fact.period, employee_id=fact.employee_id, payroll=embedded,
        profile_revision={"subject_id": fact.profile_id, "revision": 1},
        contribution_policy_revision={"subject_id": fact.contribution_policy_id, "revision": 1},
        income_tax_policy_revision={"subject_id": fact.income_tax_policy_id, "revision": 1},
    )
    saved = {}
    for subject, value in (("original-wage", fact), ("original-plan", plan)):
        saved[subject] = constructor.save_fact(
            value.kind, subject, value.model_dump(mode="json"), evidence=(proof,),
            expected_revision=0, request_id="save-" + subject,
        )["fact_id"]
    assert verify_file(path, _bundle=source)["verification"]["status"] == "verified"
    with constructor.store.connection() as connection:
        assert adjust_connection(connection, kind="company")["status"] == "adjusted"
        assert upgrade_company(connection, production_bundle())["status"] == "upgraded"
    assert verify_file(path)["verification"]["status"] == "verified"
    engine = Engine(Store(path, production_bundle(), "synthetic-company", "synthetic-database"))
    return engine, saved


@pytest.mark.parametrize("mismatch", [False, True])
def test_original_owner_input_matches_the_complete_nested_wage_only(
    tmp_path, monkeypatch, mismatch,
):
    engine, _saved = original_owner_fixture(tmp_path, monkeypatch, mismatch=mismatch)
    with engine.store.connection(read_only=True) as connection:
        before = tuple(connection.iterdump())
    with Dashboard(engine)._snapshot("2026-01") as snapshot:
        tasks = owner_tasks(snapshot)
    assert [task["subject_id"] for task in tasks] == (["original-wage"] if mismatch else [])
    with engine.store.connection(read_only=True) as connection:
        assert tuple(connection.iterdump()) == before


def test_original_owner_plan_cannot_confirm_a_current_wage_body(tmp_path, monkeypatch):
    engine, _saved = original_owner_fixture(tmp_path, monkeypatch)
    with engine.store.connection(read_only=True) as connection:
        proof = bytes(connection.execute("SELECT digest FROM evidence LIMIT 1").fetchone()[0]).hex()
    current = payroll(accounting_gross_salary_fen=600_000)
    engine.save_fact(current.kind, "current-wage", current.model_dump(mode="json"),
                     evidence=(proof,), expected_revision=0, request_id="save-current-wage")
    with Dashboard(engine)._snapshot("2026-01") as snapshot:
        assert [task["subject_id"] for task in owner_tasks(snapshot)] == ["current-wage"]


@pytest.mark.parametrize("kind", ["wage", "plan"])
def test_original_owner_inputs_refuse_damaged_actual_body(tmp_path, monkeypatch, kind):
    engine, saved = original_owner_fixture(tmp_path, monkeypatch)
    table = "fact_payroll_pre_revision" if kind == "wage" else "fact_payroll_plan_v2_pre_revision"
    field = "accounting_gross_salary_fen" if kind == "wage" else "employee_id"
    expression = field + "+1" if kind == "wage" else "'wrong-person'"
    ident = saved["original-" + kind]
    damage(engine, table, f"UPDATE {table} SET {field}={expression} WHERE revision_id=?", (ident,))
    with Dashboard(engine)._snapshot("2026-01") as snapshot:
        with pytest.raises(KernelError) as error:
            owner_tasks(snapshot)
        assert error.value.code == "content_integrity_failed"
        assert ident not in snapshot.reads._fact_versions


def test_current_owner_input_keeps_exact_subject_and_revision(payroll_company):
    company = payroll_company
    saved = unconfirmed_wage(company)
    company.confirm_payroll("other-january")
    with Dashboard(company.engine)._snapshot("2026-01") as snapshot:
        assert owner_tasks(snapshot) == []
    with company.engine.store.connection(read_only=True) as connection:
        version = company.engine.store.fact(connection, saved["fact_id"])
    changed = type(version.fact).model_validate(version.fact.model_dump() | {
        "accounting_gross_salary_fen": version.fact.accounting_gross_salary_fen + 1,
    })
    company.save(changed, "other-january", revision=1)
    with Dashboard(company.engine)._snapshot("2026-01") as snapshot:
        assert [task["subject_id"] for task in owner_tasks(snapshot)] == ["other-january"]


def separate_authentication(reads, identifiers):
    """Control: the same strict proof followed by the original typed loader."""
    verify_sources(reads.engine, reads.connection, fact_ids=identifiers)


def unconfirmed_wage(company):
    company.save(
        PayrollProfile(**profile().model_dump() | {"employee_id": "other"}),
        "other-profile",
    )
    return company.save(
        payroll(employee_id="other", profile_id="other-profile"), "other-january"
    )


def test_owner_tasks_keep_exact_pending_wage_with_less_source_transfer(
    payroll_company, monkeypatch
):
    company = payroll_company
    unconfirmed_wage(company)

    def read():
        with Dashboard(company.engine)._snapshot("2026-01") as snapshot:
            return owner_tasks(snapshot)

    actual_work, actual = measure_work(company.engine, read)
    with monkeypatch.context() as patch:
        patch.setattr(QueryReads, "verify_fact_versions", separate_authentication)
        original_work, expected = measure_work(company.engine, read)
    assert actual == expected
    assert [(task["subject_id"], task["title"]) for task in actual] == [
        ("other-january", "确认本月工资")
    ]
    assert actual[0]["amount_fen"] is None and actual[0]["deadline"] is None
    assert actual_work["counters"]["returned_value_bytes"] < (
        original_work["counters"]["returned_value_bytes"]
    )


def test_owner_tasks_do_not_accept_a_model_valid_wage_with_wrong_source_digest(payroll_company):
    company = payroll_company
    saved = unconfirmed_wage(company)
    damage(
        company.engine, "fact_payroll_confirmed",
        "UPDATE fact_payroll_confirmed "
        "SET accounting_gross_salary_fen=accounting_gross_salary_fen+1 "
        "WHERE revision_id=?",
        (saved["fact_id"],),
    )
    with Dashboard(company.engine)._snapshot("2026-01") as snapshot:
        with pytest.raises(KernelError) as failure:
            owner_tasks(snapshot)
        assert failure.value.code == "content_integrity_failed"
        assert saved["fact_id"] not in snapshot.reads._fact_versions


@pytest.mark.parametrize("history_count", [12, 48])
def test_owner_kind_membership_keeps_month_scope_with_one_subject_seek(
    payroll_company, history_count
):
    """Real saved facts exercise the selective month join, not a mock proof."""
    company = payroll_company
    unconfirmed_wage(company)
    for index in range(16 + history_count):
        period = YearMonth("2026-01" if index < 16 else "2025-12")
        company.save(
            PayrollProfile(
                **profile().model_dump()
                | {"employee_id": f"unrelated-{index}", "period": period}
            ),
            f"unrelated-profile-{index}",
        )

    class OriginalMembership:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, statement, parameters=()):
            statement = statement.replace(
                "AND (s.kind IN(SELECT value FROM json_each(?))) IS TRUE",
                "AND s.kind IN(SELECT value FROM json_each(?))",
            )
            return self.connection.execute(statement, parameters)

        def __getattr__(self, name):
            return getattr(self.connection, name)

    def read(*, original=False):
        with Dashboard(company.engine)._snapshot("2026-01") as snapshot:
            if original:
                snapshot.connection = OriginalMembership(snapshot.connection)
            return owner_tasks(snapshot)

    actual_work, actual = measure_work(company.engine, read)
    original_work, expected = measure_work(company.engine, lambda: read(original=True))
    assert actual == expected
    assert [(task["subject_id"], task["title"]) for task in actual] == [
        ("other-january", "确认本月工资")
    ]

    def owner_query(work):
        return next(
            query for query in work["sql"]
            if query["statement"].startswith("SELECT f.id FROM fact_revision f INDEXED")
        )

    narrow, original = owner_query(actual_work), owner_query(original_work)
    assert narrow["returned_rows"] == original["returned_rows"]
    assert narrow["returned_value_bytes"] == original["returned_value_bytes"]
    assert narrow["sqlite_vm_steps"] < original["sqlite_vm_steps"]
