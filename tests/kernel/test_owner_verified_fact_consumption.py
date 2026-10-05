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
        company.engine, "fact_payroll",
        "UPDATE fact_payroll SET accounting_gross_salary_fen=accounting_gross_salary_fen+1 "
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
