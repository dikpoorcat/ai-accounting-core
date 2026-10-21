"""One scope guard coordinates deterministic horizon work and the ledger's close."""

from dataclasses import dataclass
from uuid import uuid4

from ai_accounting_modular.contracts.public import (
    ContractError,
    content_digest,
    validate_contract,
)

from . import _documents, _execution, _reading
from ._submission import Submission
from ._work_scope import working_period


@dataclass(frozen=True)
class Scope:
    ledger: object
    head: str
    unresolved: tuple
    blockers: tuple
    documents: str
    needs_horizon: bool


def _issue(code, field, message, *, sources=(), business_id=None):
    result = {
        "code": code,
        "field": field,
        "meaning": "完成本期账务所必需的真实资料及完整核对",
        "message": message,
        "allowed_precision": ["source", "fen", "month"],
        "reusable_sources": [{"source_id": source} for source in sources],
        "blocking_scope": "period",
    }
    if business_id is not None:
        result["business_id"] = business_id
    return result


def _snapshot(application, period, read):
    ledger = application.ledger.check_period(period, read)
    documents = application.records.document_status(read)["state"]
    if ledger.state == "closed":
        return Scope(ledger, ledger.head, (), (), documents, False)
    if application.money is None:
        raise ContractError(
            "unsupported_business", "当前实例未装配完整资金核对，不能宣称已完成关账检查。"
        )
    money = application.money.check_period(ledger.period_from, period, read)
    issues, cursor = [], None
    while True:
        page = application.records.period_issues(ledger.period_from, period, read, cursor=cursor)
        issues.extend(page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    blockers = list(money.blocking_issues)
    for index, item in enumerate(issues):
        if item["missing_fields"] and item["action"] not in {"external_payment", "external_filing"}:
            for field_index, field in enumerate(item["missing_fields"]):
                blockers.append(
                    _issue(
                        "unresolved_accounting_fact",
                        field
                        if item.get("business_id")
                        else f"/unresolved/{index}/missing_fields/{field_index}",
                        item["question"],
                        sources=item["evidence_ids"],
                        business_id=item.get("business_id"),
                    )
                )
    if documents != "ready":
        blockers.append(
            _issue(
                "documents_pending",
                "/documents",
                "当前标准业务文档尚未归档完整，请接续文档后关账。",
            )
        )
    working = working_period(read)
    inputs = (
        read.repository("application").horizon_inputs(working, period) if period > working else []
    )
    head = content_digest(
        {
            "company_id": read.company_id,
            "database_id": read.database_id,
            "period": period,
            "ledger": ledger.head,
            "money": money.head,
            "issues": issues,
            "horizon_inputs": inputs,
        }
    )
    return Scope(ledger, head, tuple(issues), tuple(blockers), documents, period > working)


def _prepare_horizon(application, period, unit):
    return _execution.execute(
        application,
        Submission(company_id=unit.company_id, command_id=str(uuid4())),
        unit,
        evaluation_period_to=period,
    )


def _wire(company, period, before, evaluated, *, preview=False):
    blockers = [*evaluated.blocking_issues, *before.blockers]
    return validate_contract(
        "core.schema.json",
        "PeriodCheck",
        {
            "company_id": company,
            "period": period,
            "state": evaluated.state,
            "head": before.head,
            "ready": not blockers,
            "blocking_issues": blockers,
            "unresolved": list(before.unresolved),
            "document_state": before.documents,
            "report_head": None if preview else evaluated.report["selection"]["selection_digest"],
        },
    )


def run(application, request):
    validate_contract("core.schema.json", "PeriodRequest", request)
    company, period = request["company_id"], request["period"]
    if request["action"] == "check":
        with application.storage.read_unit(company) as read:
            before = _snapshot(application, period, read)
            if not before.needs_horizon:
                return _wire(company, period, before, before.ledger)
        # This uses the same owners and complete commit checks, but every write
        # is rolled back by storage. No document/file publication occurs here.
        with application.storage.preview_unit(company) as unit:
            before = _snapshot(application, period, unit)
            prepared = _prepare_horizon(application, period, unit) if before.needs_horizon else None
            evaluated = application.ledger.check_period(period, unit)
            return _wire(
                company,
                period,
                before,
                evaluated,
                preview=bool(prepared and prepared.accounting_changed),
            )
    with application.storage.write_unit(company) as unit:
        before = _snapshot(application, period, unit)
        if before.ledger.state == "closed":
            outcome = _wire(company, period, before, before.ledger)
        else:
            if before.head != request["expected_head"]:
                raise ContractError(
                    "conflict", "检查后的相关账务或待办已变化，请重新检查关账范围。"
                )
            if before.needs_horizon:
                _prepare_horizon(application, period, unit)
            evaluated = application.ledger.check_period(period, unit)
            checked = _wire(company, period, before, evaluated)
            if not checked["ready"]:
                raise ContractError(
                    "needs_information",
                    "尚有必须补齐的本期账务资料或核对。",
                    fact_issues=checked["blocking_issues"],
                )
            closed = application.ledger.close_period(period, evaluated.head, unit)
            application._notify_reading(_reading.closed(before.ledger.period_from, period), unit)
            outcome = _wire(
                company,
                period,
                Scope(closed, closed.head, before.unresolved, (), before.documents, False),
                closed,
            )
    with application.storage.read_unit(company) as read:
        status = application.records.document_status(read)
    if status["state"] != "ready":
        status = _documents.finish(application, company)
    outcome["document_state"] = status["state"]
    return validate_contract("core.schema.json", "PeriodCheck", outcome)
