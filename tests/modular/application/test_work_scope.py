"""The saved work horizon is metadata, distinct from an invented or voucher-only month."""

from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.reading._context import last_period
from ai_accounting_modular.storage.public import RepositorySession
from tests.modular.domains.people.test_relationships import relationship

from ._working_case import case as case
from .conftest import source


def test_empty_scope_and_zero_formal_result_do_not_read_context_or_scan_accounting(
    case, monkeypatch
):
    with case.kernel.storage.read_unit(case.company) as read:
        empty = case.app.work_scope(read)
        assert empty.start_period == empty.working_period_to == "2026-01"
        assert not empty.has_formal_results and empty.closed_through is None
        assert last_period(case.kernel.reading, read) is None
    opening = case.seed(expense=False)
    assert case.get(opening["business_id"])["businesses"][0]["results"][0]["vouchers"] == []

    def forbidden(*args, **kwargs):
        raise AssertionError("A work horizon must not assemble context or scan formal months")

    monkeypatch.setattr(case.app, "context", forbidden)
    monkeypatch.setattr(case.kernel.ledger, "latest_period", forbidden)
    monkeypatch.setattr(case.kernel.records, "document_status", forbidden)
    statements, run = [], RepositorySession._run

    def observed(self, sql, *args, **kwargs):
        statements.append(sql)
        return run(self, sql, *args, **kwargs)

    monkeypatch.setattr(RepositorySession, "_run", observed)
    with case.kernel.storage.read_unit(case.company) as read:
        actual = case.app.work_scope(read)
        assert actual.has_formal_results
        assert actual.working_period_to == last_period(case.kernel.reading, read) == "2026-01"
        assert case.kernel.ledger.has_formal_results(read)
    assert not any("MAX(business_period)" in sql for sql in statements)
    assert not any("document_" in sql or "voucher" in sql for sql in statements)
    assert any("EXISTS" in sql and "adopted_result" in sql for sql in statements)


def test_future_relationship_preserves_work_scope_without_inventing_established_books(case):
    person, evidence = str(uuid4()), source("合成人员资料：一月起任职，三月整理确认。")
    fact = relationship(("regular", "2026-01-01"), person=person, period="2026-03")
    fact["evidence_ids"] = [evidence["source_id"]]
    result = case.app.accounting_apply(
        case.request(
            fact,
            sources=[evidence],
            objects=[
                {
                    "object_id": person,
                    "kind": "person",
                    "name": "合成人员",
                    "data": {},
                    "expected_seq": 0,
                }
            ],
        )
    )
    assert result["completion"] == "complete", result
    with case.kernel.storage.read_unit(case.company) as read:
        actual = case.app.work_scope(read)
        assert actual.working_period_to == "2026-03" and not actual.has_formal_results
        assert last_period(case.kernel.reading, read) is None
    case.seed(expense=False)
    with case.kernel.storage.read_unit(case.company) as read:
        assert case.app.work_scope(read).has_formal_results
        assert last_period(case.kernel.reading, read) == "2026-03"
    context = case.context(note_scopes=[], issue_scopes=[])
    assert context["company_identity"]["working_period_to"] == "2026-03"
    assert context["open_periods"] == ["2026-01", "2026-02", "2026-03"]


def test_future_issue_does_not_invent_a_later_work_horizon(case, monkeypatch):
    case.seed(expense=False)
    issue = {
        "issue_id": str(uuid4()),
        "scope": "company",
        "period": "2026-09",
        "question": "九月实际外部申报待办。",
        "missing_fields": [],
        "evidence_ids": [case.source_id],
        "action": "external_filing",
        "assignee": "owner",
    }
    result = case.app.accounting_apply(
        case.request(unresolved=[issue], unresolved_scope="company", expected_scope_head=0)
    )
    assert result["completion"] == "complete", result

    def forbidden(*args, **kwargs):
        raise AssertionError("Full context must reuse the saved working horizon")

    monkeypatch.setattr(case.kernel.ledger, "latest_period", forbidden)
    with case.kernel.storage.read_unit(case.company) as read:
        assert case.app.work_scope(read).working_period_to == "2026-01"
    assert (
        case.context(note_scopes=[], issue_scopes=[])["company_identity"]["working_period_to"]
        == "2026-01"
    )


def test_frozen_withdrawal_retains_formal_presence_and_actual_discovery_horizon(case):
    opening = case.seed(expense=False)
    assert case.close(case.check())["state"] == "closed"
    result = case.app.accounting_apply(
        case.request(
            correction_period="2026-03",
            explanation="已核实原建账事项登记错误，撤去并保留真实冻结依据。",
            mutations=[
                {"op": "withdraw", "business_id": opening["business_id"], "expected_seq": 1}
            ],
        )
    )
    assert result["completion"] == "complete", result
    with case.kernel.storage.read_unit(case.company) as read:
        actual = case.app.work_scope(read)
        assert actual.has_formal_results and actual.closed_through == "2026-01"
        assert actual.working_period_to == last_period(case.kernel.reading, read) == "2026-03"
        assert case.kernel.ledger.latest_period(read) == "2026-01"
        assert case.app.backup_verifier.verify_snapshot(read).document_set_id


def test_current_work_metadata_does_not_change_historical_accounting_selection(case):
    fact = case.seed()
    assert case.close(case.check())["state"] == "closed"
    fact["data"]["amount_fen"] = "90000"
    result = case.app.accounting_apply(
        case.request(
            correction_period="2026-03",
            mutations=[{"op": "replace", "fact": fact, "expected_seq": 1}],
        )
    )
    assert result["completion"] == "complete", result
    ledger, ids = case.kernel.ledger, (fact["business_id"],)
    with case.kernel.storage.read_unit(case.company, view="as_posted", as_of="2026-01") as read:
        before = ledger.get_obligation_positions("2026-01", read, business_ids=ids)
        work = case.app.work_scope(read)
        assert work.has_formal_results and work.working_period_to == "2026-03"
        assert work.closed_through == "2026-01"
        assert read.view == "as_posted" and read.as_of == "2026-01"
        after = ledger.get_obligation_positions("2026-01", read, business_ids=ids)
        assert before == after and after[0].amounts.amount_fen == 100000
    with case.kernel.storage.read_unit(case.company) as read:
        assert case.app.work_scope(read) == work
        assert (
            ledger.get_obligation_positions("2026-03", read, business_ids=ids)[0].amounts.amount_fen
            == 90000
        )


@pytest.mark.parametrize("actual", ["fact", "adoption", "closing"])
def test_snapshot_checks_work_horizon_before_accepting_documents(case, monkeypatch, actual):
    case.seed(expense=False)
    if actual == "fact":
        person = str(uuid4())
        fact = relationship(("regular", "2026-01-01"), person=person, period="2026-03")
        fact["evidence_ids"] = [case.source_id]
        result = case.app.accounting_apply(
            case.request(
                fact,
                objects=[
                    {
                        "object_id": person,
                        "kind": "person",
                        "name": "无凭证事实人员",
                        "data": {},
                        "expected_seq": 0,
                    }
                ],
            )
        )
    elif actual == "adoption":
        fact = case.expense()
        assert case.app.accounting_apply(case.request(fact))["completion"] == "complete"
        assert case.close(case.check())["state"] == "closed"
        fact["data"]["amount_fen"] = "90000"
        result = case.app.accounting_apply(
            case.request(
                correction_period="2026-03",
                mutations=[{"op": "replace", "fact": fact, "expected_seq": 1}],
            )
        )
    else:
        result = case.close(case.check("2026-03"))
        assert result["state"] == "closed"
    if actual != "closing":
        assert result["completion"] == "complete", result
    with case.kernel.storage.write_unit(case.company) as unit:
        unit.repository("application")._session.execute(
            "UPDATE application_state SET working_period_to='2026-01' WHERE singleton=1"
        )

    def forbidden(*args, **kwargs):
        raise AssertionError("A contradictory saved horizon must fail before document acceptance")

    monkeypatch.setattr(case.kernel.records, "verify_snapshot", forbidden)
    with case.kernel.storage.read_unit(case.company) as read:
        with pytest.raises(ContractError) as failure:
            case.app.backup_verifier.verify_snapshot(read)
    assert failure.value.code == "storage_corrupt"
    assert "工作" in failure.value.message
