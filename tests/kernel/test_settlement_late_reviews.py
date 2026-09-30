"""Late reviews preserve frozen finance and reject sealed semantic forgeries."""

import json
from dataclasses import asdict

import pytest
from test_payroll import contribution_policy, income_tax_policy, opening, payroll, profile
from test_payroll_corrections import Company

from ai_accounting.kernel import engine as engine_module
from ai_accounting.kernel import publication, settlement_freeze
from ai_accounting.kernel.contracts import KernelError, Read
from ai_accounting.kernel.integrity import verify_integrity
from ai_accounting.kernel.types import YearMonth, canonical, digest


def prepared(tmp_path, *, zero=False):
    company = Company(tmp_path / "late-review.sqlite")
    for fact, name in (
        (profile(effective_to="2026-02", social_insurance_participating=not zero,
                 social_insurance_base_fen=None if zero else 1_000_000), "profile"),
        (contribution_policy(), "contributions"),
        (income_tax_policy(), "income-tax"), (opening(), "opening"),
        (payroll(accounting_gross_salary_fen=0, tax_reported_salary_fen=0)
         if zero else payroll(), "january"),
    ):
        company.save(fact, name)
    company.confirm_payroll("january")
    company.publish("january")
    company.close("2026-01")
    return company


def review(company, monkeypatch, number):
    # A new calculator build with identical accounting semantics generates a
    # distinct sealed result using the real preview/confirm path.
    with monkeypatch.context() as scope:
        scope.setattr(engine_module, "PROGRAM_VERSION", f"synthetic-late-review-{number}")
        _, result = company.publish("january")
    assert result["january"]["impact"] == "review_no_impact"
    return result["january"]["calculation_id"]


def frozen(company):
    with company.engine.store.connection(read_only=True) as connection:
        return [tuple(row) for row in connection.execute(
            "SELECT period,digest,manifest FROM period_close ORDER BY period"
        )]


def tail(company):
    with company.engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        root = settlement_freeze._read_root(connection, YearMonth("2026-01").ordinal)
        return settlement_freeze._tail_rows(connection, root, YearMonth("2026-02").ordinal)


@pytest.mark.parametrize("zero", [False, True])
def test_multiple_late_reviews_and_next_close_preserve_original_freeze(
    tmp_path, monkeypatch, zero
):
    company = prepared(tmp_path, zero=zero)
    before = frozen(company)
    ledger = company.engine.ledger("2026-01")
    if zero:
        assert ledger == []
    ids = [review(company, monkeypatch, number) for number in (1, 2)]
    assert len(set(ids)) == 2
    assert tail(company) == []
    assert frozen(company) == before
    assert company.engine.ledger("2026-01") == ledger
    with company.engine.store.connection(read_only=True) as connection:
        assert verify_integrity(company.engine, connection)["status"] == "verified"
    company.save(payroll(period="2026-02", accounting_gross_salary_fen=0 if zero else 1_000_000,
                         tax_reported_salary_fen=0 if zero else 1_000_000), "february")
    company.confirm_payroll("february")
    company.publish("february")
    company.close("2026-02")
    assert frozen(company)[0] == before[0]
    with company.engine.store.connection(read_only=True) as connection:
        assert verify_integrity(company.engine, connection)["status"] == "verified"


def corrupt(company, operation):
    """Isolated forensic fixture only: restore every original trigger verbatim."""
    with company.engine.store.connection() as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN IMMEDIATE")
        triggers = list(connection.execute(
            "SELECT name,sql FROM sqlite_schema WHERE type='trigger'"
        ))
        for row in triggers:
            connection.execute('DROP TRIGGER "' + row[0].replace('"', '""') + '"')
        operation(connection)
        for row in triggers:
            connection.execute(row[1])
        connection.commit()


def test_late_wrong_mode_is_rejected_by_tail_and_complete_verifier(tmp_path, monkeypatch):
    company = prepared(tmp_path)
    identifier = review(company, monkeypatch, 1)
    corrupt(company, lambda connection: connection.execute(
        "UPDATE calculation_publication SET mode='open_replace' WHERE calculation_id=?",
        (identifier,),
    ))
    with pytest.raises(KernelError) as rejected:
        tail(company)
    assert rejected.value.details["reason"] == "published_into_frozen_period"
    with company.engine.store.connection(read_only=True) as connection:
        with pytest.raises(KernelError):
            verify_integrity(company.engine, connection)


def test_late_review_does_not_hide_broken_current_projection_seal(tmp_path, monkeypatch):
    company = prepared(tmp_path)
    review(company, monkeypatch, 1)
    corrupt(company, lambda connection: connection.execute(
        "DELETE FROM settlement_projection_seal WHERE posting_period=?",
        (YearMonth("2026-01").ordinal,),
    ))
    with pytest.raises(KernelError):
        tail(company)
    with company.engine.store.connection(read_only=True) as connection:
        with pytest.raises(KernelError):
            verify_integrity(company.engine, connection)


def forge_obligation_identity(connection, identifier):
    old = connection.execute("SELECT * FROM calculation WHERE id=?", (identifier,)).fetchone()
    outcome = json.loads(old["outcome"])
    outcome["values"]["obligations"][0]["name"] = "forged-financial-obligation"
    # Keep balanced journal lines and balance effects completely unchanged.
    reads = [dict(row) for row in connection.execute(
        "SELECT source,kind,scope_key,before_period FROM dependency_scope "
        "WHERE calculation_id=?", (identifier,),
    )]
    selected_reads = [Read(row["source"], row["kind"], row["scope_key"],
                           YearMonth.from_ordinal(row["before_period"])
                           if row["before_period"] != 119988 else None) for row in reads]
    versions = {row[0] for row in connection.execute(
        "SELECT fact_id FROM dependency_fact WHERE calculation_id=? AND fact_id<>? "
        "UNION SELECT upstream_id FROM dependency_calculation WHERE calculation_id=?",
        (identifier, old["fact_id"], identifier),
    )}
    new_id = "c_" + digest(dict(
        fact=old["fact_id"], outcome=outcome,
        reads=[asdict(item) for item in sorted(selected_reads, key=repr)],
        versions=sorted(versions), program=old["program_version"],
    )).hex()
    tables = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )]
    for table in tables:
        for foreign in connection.execute(f'PRAGMA foreign_key_list("{table}")'):
            if foreign[2] in {"calculation", "calculation_seal"}:
                connection.execute(
                    f'UPDATE "{table}" SET "{foreign[3]}"=? WHERE "{foreign[3]}"=?',
                    (new_id, identifier),
                )
    # calculation.id also references calculation_seal.calculation_id: the
    # complete FK rewrite above changes both primary IDs and all their users.
    assert connection.execute(
        "SELECT count(*) FROM calculation WHERE id=?", (new_id,),
    ).fetchone()[0] == 1
    connection.execute("UPDATE calculation SET outcome=?,digest=? WHERE id=?",
                       (canonical(outcome), digest(outcome), new_id))
    event = dict(connection.execute(
        "SELECT * FROM calculation_publication WHERE calculation_id=?", (new_id,),
    ).fetchone())
    new_event_id = "p_" + digest({
        field: event[field] for field in publication.CONTENT_FIELDS
    }).hex()
    for table in tables:
        for foreign in connection.execute(f'PRAGMA foreign_key_list("{table}")'):
            if foreign[2] == "calculation_publication":
                connection.execute(
                    f'UPDATE "{table}" SET "{foreign[3]}"=? WHERE "{foreign[3]}"=?',
                    (new_event_id, event["id"]),
                )
    connection.execute("UPDATE calculation_publication SET id=? WHERE id=?",
                       (new_event_id, event["id"]))


def test_sealed_review_with_only_changed_obligation_identity_is_rejected(tmp_path, monkeypatch):
    company = prepared(tmp_path)
    identifier = review(company, monkeypatch, 1)
    corrupt(company, lambda connection: forge_obligation_identity(connection, identifier))
    with pytest.raises(KernelError) as rejected:
        tail(company)
    assert rejected.value.details["reason"] == "late_review_accounting_mismatch"
    with company.engine.store.connection(read_only=True) as connection:
        with pytest.raises(KernelError):
            verify_integrity(company.engine, connection)
