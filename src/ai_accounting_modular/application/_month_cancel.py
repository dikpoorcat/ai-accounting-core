"""Cancel one open month's inputs through their owners in one transaction."""

from ai_accounting_modular.contracts.public import ContractError, validate_contract
from ai_accounting_modular.reading.public import ReadingImpact
from ai_accounting_modular.records.public import RecordsChange, RecordsContext

from . import _documents, _execution, _notes, _source_discards
from ._submission import Submission

_WAITING = (
    "本月待重新提供资料，并从第1项流水与基础资料开始办理。"
    "仍被其他工作引用而保留的来源，不代表本月资料已重新收齐；"
    "不恢复已撤回的旧核算或沿用旧清单进度。"
)


def _scope(application, period, read):
    if period < read.start_period:
        raise ContractError("invalid_input", "所选月份早于公司接账月份。")
    closed = application.ledger.closed_through(read)
    if closed is not None and period <= closed:
        raise ContractError(
            "closed_period", "已关账月份不能整月清理；更正仍保留冻结结果及必要原件。"
        )
    return (
        read.repository("application").month_heads(period),
        application.records.period_sources(period, read),
    )


def _task_paths(application, sources, read):
    return (
        _source_discards._protected_paths(application, read)
        if any(source["kind"] == "file" for source in sources)
        else ()
    )


def _retained(application, sources, read):
    ids = tuple(source["source_id"] for source in sources)
    retained = []
    paths = _task_paths(application, sources, read)
    for offset in range(0, len(ids), 500):
        batch = ids[offset : offset + 500]
        retained.extend(
            application.records.source_discard_protection(
                batch,
                read,
                retained_source_ids=application.ledger.referenced_source_ids(batch, read),
                protected_paths=paths,
            )
        )
    return tuple(sorted(retained))


def _waiting_note(application, period, command_id, unit):
    scope = "period:" + period
    current = application.records.current_notes((scope,), unit)
    heads = _notes.basis_heads(application, (scope,), unit)
    plan = application.records.plan_change(
        RecordsChange(
            notes=(
                {
                    "scope": scope,
                    "expected_seq": current[0]["seq"] if current else 0,
                    "expected_basis_head": heads[scope],
                    "text": _WAITING,
                    "evidence_ids": [],
                },
            ),
        ),
        RecordsContext(unit, command_id, note_basis_heads=heads),
    )
    application.records.apply_current(plan, unit)


def _finish_cleanup(application, company, period):
    try:
        with application.storage.read_unit(company) as read:
            markers = application.records.pending_source_discard_markers(period, read)
        return _source_discards.cleanup(application, company, markers)
    except (ContractError, OSError) as error:
        code, message = _source_discards.cleanup_issue(error)
        return {"state": "pending", "issues": [{"code": code, "message": message}]}


def _finish_documents(application, company):
    try:
        with application.storage.read_unit(company) as read:
            status = application.records.document_status(read)
        if status["state"] != "ready":
            status = _documents.finish(application, company)
        return "ready" if status["state"] == "ready" else "pending"
    except (ContractError, OSError):
        # The accounting transaction has committed. The standard-document
        # targets remain pending and the normal finish command can resume them.
        return "pending"


def run(application, request):
    validate_contract("core.schema.json", "MonthCancelRequest", request)
    company, period = request["company_id"], request["period"]
    discarded = []
    with (
        application.storage.read_unit(company)
        if request["action"] == "inspect"
        else application.storage.write_unit(company)
    ) as unit:
        heads, sources = _scope(application, period, unit)
        result = {
            "company_id": company,
            "database_id": unit.database_id,
            "period": period,
            "head": unit.head,
            "business_ids": [head["business_id"] for head in heads],
            "source_ids": [source["source_id"] for source in sources],
            "discarded_source_ids": [],
            "retained_source_ids": [],
        }
        if request["action"] == "inspect":
            result.update(
                state="inspected",
                documents=application.records.document_status(unit)["state"],
                file_cleanup={"state": "not_started", "issues": []},
            )
            return validate_contract("core.schema.json", "MonthCancelResult", result)
        if request["expected_head"] != unit.head:
            same_cancel = not heads and application.records.note_written_by(
                "period:" + period, request["command_id"], unit
            )
            retained = _retained(application, sources, unit) if same_cancel else ()
            if not same_cancel or set(result["source_ids"]) - set(retained):
                raise ContractError(
                    "snapshot_changed",
                    "所选月份已有新变化，请重新核对，不能用旧撤销请求处理新业务或新资料。",
                )
            # A lost reply can resume cleanup, but must never clear new notes
            # or issues written after the original cancellation.
        else:
            if heads:
                _execution.execute(
                    application,
                    Submission(
                        company_id=company,
                        command_id=request["command_id"],
                        mutations=tuple(
                            {
                                "op": "withdraw",
                                "business_id": head["business_id"],
                                "expected_seq": head["seq"],
                            }
                            for head in heads
                        ),
                    ),
                    unit,
                )
            issue_periods = application.records.clear_period_issues(
                period, request["command_id"], unit
            )
            _waiting_note(application, period, request["command_id"], unit)
            paths = _task_paths(application, sources, unit)
            wanted, retained = result["source_ids"], []
            for offset in range(0, len(wanted), 500):
                batch = wanted[offset : offset + 500]
                prepared = application.records.discard_sources(
                    batch,
                    unit,
                    retained_source_ids=application.ledger.referenced_source_ids(batch, unit),
                    protected_paths=paths,
                    retain_protected=True,
                )
                discarded.extend(prepared.discarded_source_ids)
                retained.extend(prepared.retained_source_ids)
            application._notify_reading(
                ReadingImpact(note_scopes=("period:" + period,), issue_periods=issue_periods), unit
            )
            result["head"] = unit.next_head
        result.update(
            state="cancelled", discarded_source_ids=discarded, retained_source_ids=list(retained)
        )
    result["file_cleanup"] = _finish_cleanup(application, company, period)
    result["documents"] = _finish_documents(application, company)
    return validate_contract("core.schema.json", "MonthCancelResult", result)
