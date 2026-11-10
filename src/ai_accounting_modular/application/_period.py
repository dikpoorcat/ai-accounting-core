"""Coordinate exact closing scope, source-backed work, and deterministic ledger freezing."""

from dataclasses import dataclass
from uuid import uuid4

from ai_accounting_modular.contracts.public import ContractError, content_digest, validate_contract

from . import _closing_work, _documents, _execution, _reading
from ._submission import Submission
from ._work_scope import working_period


@dataclass(frozen=True)
class Scope:
    ledger: object
    actual_range: dict
    head: str
    unresolved: tuple
    blockers: tuple
    documents: str
    needs_horizon: bool
    work: _closing_work.WorkCheck | None


def _snapshot(application, period, basis, expected_material_version, read):
    ledger = application.ledger.check_period(period, read)
    documents = application.records.document_status(read)["state"]
    actual_range = {
        "period_from": ledger.period_from,
        "period_to": ledger.closing_period or period,
    }
    if ledger.state == "closed":
        head = ledger.work_basis["public_head"] if ledger.work_basis else ledger.head
        return Scope(ledger, actual_range, head, (), (), documents, False, None)
    if application.money is None:
        raise ContractError(
            "unsupported_business", "当前实例未装配完整资金核对，不能宣称已完成关账检查。"
        )
    money = application.money.check_period(ledger.period_from, period, read)
    issues, cursor = [], None
    while True:
        page = application.records.period_closing_issues(
            ledger.period_from, period, read, cursor=cursor
        )
        issues.extend(page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    blockers = list(money.blocking_issues)
    for index, item in enumerate(issues):
        for field_index, field in enumerate(item["missing_fields"]):
            blockers.append(
                _closing_work.issue(
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
            _closing_work.issue(
                "documents_pending",
                "/documents",
                "当前标准业务文档尚未归档完整，请接续文档后关账。",
            )
        )
    work = _closing_work.inspect(application, actual_range, basis, expected_material_version, read)
    working = working_period(read)
    inputs = (
        read.repository("application").horizon_inputs(working, period) if period > working else []
    )
    head = content_digest(
        {
            "company_id": read.company_id,
            "database_id": read.database_id,
            "actual_range": actual_range,
            "previous_closed_through": ledger.previous_closed_through,
            "ledger": ledger.head,
            "money": money.head,
            "issues": issues,
            "horizon_inputs": inputs,
            "basis": work.basis,
            "material_version": work.material_version,
            "note_versions": list(work.note_versions),
        }
    )
    return Scope(
        ledger,
        actual_range,
        head,
        tuple(issues),
        tuple(blockers),
        documents,
        period > working,
        work,
    )


def _prepare_horizon(application, period, unit):
    return _execution.execute(
        application,
        Submission(company_id=unit.company_id, command_id=str(uuid4())),
        unit,
        evaluation_period_to=period,
    )


def _frozen_request(before, request):
    """A retry adopts only the saved work basis, never today's mutable records."""
    saved = before.ledger.work_basis
    closing = request["action"] == "close"
    if saved is None:
        if closing or "basis" in request or "expected_material_version" in request:
            raise ContractError(
                "invalid_operation",
                "该历史关账未记录本次办理依据；可直接检查读取，不能补造后重复关闭。",
            )
        return
    matches = (
        (not closing or request["actual_range"] == saved["actual_range"])
        and (not closing or request["expected_head"] == saved["public_head"])
        and request.get("expected_material_version", saved["material_version"])
        == saved["material_version"]
        and (
            "basis" not in request
            or _closing_work.normalize(request["basis"]) == _closing_work.normalize(saved["basis"])
        )
    )
    if not matches:
        raise ContractError(
            "conflict", "该范围已经关账；重复请求须与原冻结范围、办理依据及版本一致。"
        )


def _wire(company, period, before, evaluated, *, preview=False):
    closed = evaluated.state == "closed"
    blockers = [] if closed else [*evaluated.blocking_issues, *before.blockers]
    work_issues = (
        []
        if closed
        else [
            *before.work.issues,
            *_closing_work.completion_issues(
                before.unresolved, before.actual_range, before.work.basis
            ),
        ]
    )
    material_version = (
        (evaluated.work_basis["material_version"] if evaluated.work_basis else None)
        if closed
        else before.work.material_version
    )
    return validate_contract(
        "core.schema.json",
        "PeriodCheck",
        {
            "company_id": company,
            "period": period,
            "actual_range": before.actual_range,
            "previous_closed_through": evaluated.previous_closed_through,
            "state": evaluated.state,
            "head": before.head,
            "accounting_ready": not blockers,
            "material_version": material_version,
            "work_issues": work_issues,
            "ready": not blockers and not work_issues,
            "blocking_issues": blockers,
            "unresolved": list(before.unresolved),
            "document_state": before.documents,
            "report_head": None if preview else evaluated.report["selection"]["selection_digest"],
        },
    )


def _checked(application, request, period):
    with application.storage.read_unit(request["company_id"]) as read:
        frontier = application.ledger.closed_through(read)
        if period <= working_period(read) or frontier is not None and period <= frontier:
            before = _snapshot(
                application,
                period,
                request.get("basis"),
                request.get("expected_material_version"),
                read,
            )
            if before.ledger.state == "closed":
                _frozen_request(before, request)
            return _wire(read.company_id, period, before, before.ledger)
    # Preview changes are rolled back; the public head keeps the exact pre-preview
    # formal state and horizon inputs which a subsequent close must still adopt.
    with application.storage.preview_unit(request["company_id"]) as unit:
        before = _snapshot(
            application,
            period,
            request.get("basis"),
            request.get("expected_material_version"),
            unit,
        )
        if before.ledger.state == "closed":
            _frozen_request(before, request)
        prepared, evaluated = None, before.ledger
        if before.needs_horizon:
            prepared = _prepare_horizon(application, period, unit)
            evaluated = application.ledger.check_period(period, unit)
        return _wire(
            unit.company_id,
            period,
            before,
            evaluated,
            preview=bool(prepared and prepared.accounting_changed),
        )


def _close(application, request, period):
    company = request["company_id"]
    with application.storage.write_unit(company) as unit:
        before = _snapshot(
            application, period, request["basis"], request["expected_material_version"], unit
        )
        if before.ledger.state == "closed":
            _frozen_request(before, request)
            return _wire(company, period, before, before.ledger)
        if before.actual_range != request["actual_range"]:
            raise ContractError("conflict", "实际关闭范围已改变，请核对起止月份后重新检查。")
        if before.head != request["expected_head"]:
            raise ContractError("conflict", "检查后的相关账务、资料或办理依据已变化，请重新检查。")
        evaluated = before.ledger
        if before.needs_horizon:
            _prepare_horizon(application, period, unit)
            evaluated = application.ledger.check_period(period, unit)
        checked = _wire(company, period, before, evaluated)
        if not checked["ready"]:
            invalid_scope = any(
                item["code"] == "early_close_scope_changed" for item in checked["work_issues"]
            )
            raise ContractError(
                "dependency_conflict" if invalid_scope else "needs_information",
                "采用的先关账依据与当前实际待办范围不一致，请修正引用。"
                if invalid_scope
                else "尚有本次关闭范围需要完成的账务或实际办理条件。",
                fact_issues=[*checked["blocking_issues"], *checked["work_issues"]],
            )
        work_basis = {
            "actual_range": before.actual_range,
            "basis": before.work.basis,
            "public_head": before.head,
            "material_version": before.work.material_version,
            "source_ids": list(before.work.source_ids),
        }
        closed = application.ledger.close_period(
            period, evaluated.head, unit, work_basis=work_basis
        )
        application.records.replace_source_uses(
            "period_close", period, work_basis["source_ids"], unit
        )
        application._notify_reading(_reading.closed(before.ledger.period_from, period), unit)
        return _wire(company, period, before, closed)


def run(application, request):
    validate_contract("core.schema.json", "PeriodRequest", request)
    if request["action"] == "check":
        return _checked(application, request, request["period"])
    if request["actual_range"]["period_from"] > request["actual_range"]["period_to"]:
        raise ContractError("invalid_input", "实际关闭范围的起始月不能晚于截止月。")
    outcome = _close(application, request, request["actual_range"]["period_to"])
    with application.storage.read_unit(request["company_id"]) as read:
        status = application.records.document_status(read)
    if status["state"] != "ready":
        status = _documents.finish(application, request["company_id"])
    outcome["document_state"] = status["state"]
    return validate_contract("core.schema.json", "PeriodCheck", outcome)
