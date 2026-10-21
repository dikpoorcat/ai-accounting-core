"""Close a financial range once; archived amounts and mapper versions remain fixed."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from ai_accounting_modular.contracts.public import (
    CloseBasis,
    ContractError,
    FactIssue,
    FinancialSnapshot,
    YearMonth,
    content_digest,
    loads_json,
    validate_contract,
)

from . import _financial
from ._balances import unique_movements, validate_capacities
from ._planning import stored_source_reference
from ._profit import has_unmapped_line
from ._values import fail


@dataclass(frozen=True)
class LedgerPeriodCheck:
    period: str
    period_from: str
    closed_through: str | None
    state: Literal["open", "closed"]
    head: str
    blocking_issues: tuple[FactIssue, ...]
    report: FinancialSnapshot

    @property
    def ready(self):
        return not self.blocking_issues


@dataclass(frozen=True)
class PeriodClosure:
    period: str
    state: Literal["open", "closed"]
    closing_period: str | None = None
    period_from: str | None = None
    head: str | None = None


def _first(previous, start):
    if previous is None:
        return start
    return str(YearMonth.from_ordinal(YearMonth(previous).ordinal + 1))


def _scope(context, first, last=None):
    return {
        "company_id": context.company_id,
        "period_from": first,
        "period_to": last or first,
        "view": "as_posted",
    }


def _basis(repo, first, last, context):
    heads = repo.adoptions_between(first, last)
    refs = sorted(
        (stored_source_reference(head) for head in heads),
        key=lambda ref: (ref["business_id"], ref["result_slot"], ref["result_id"]),
    )
    basis: CloseBasis = {
        "format": "ai-accounting-close-basis/1",
        "company_id": context.company_id,
        "database_id": context.database_id,
        "period": last,
        "result_digest": content_digest(refs),
    }
    validate_contract("reporting.schema.json", "CloseBasis", basis)
    return basis, heads


def _head(basis, previous, report):
    return content_digest(
        {
            "basis": basis,
            "previous_closed_through": previous,
            "report_head": report["selection"]["selection_digest"],
            "mapping_version": report["mapping_version"],
        }
    )


def _header(repo, row, context, basis, report):
    if (
        row["basis_schema"] != "reporting.schema.json#/$defs/CloseBasis"
        or row["report_schema"] != "reporting.schema.json#/$defs/FinancialSnapshot"
    ):
        fail("storage_corrupt", "关账资料的明确合同标识不符。")
    try:
        validate_contract("core.schema.json", "Month", row["period"])
        validate_contract("reporting.schema.json", "CloseBasis", basis)
        for value in (
            row["result_head"],
            row["report_digest"],
            report["selection"]["selection_digest"],
        ):
            validate_contract("core.schema.json", "Digest", value)
    except ContractError as error:
        raise ContractError("storage_corrupt", "保存的关账依据或三表不符合正式合同。") from error
    period = row["period"]
    previous = repo.previous_close(period)
    first = _first(previous, context.start_period)
    if (
        basis["company_id"] != context.company_id
        or basis["database_id"] != context.database_id
        or basis["period"] != period
        or _head(basis, previous, report) != row["result_head"]
        or report["company_id"] != context.company_id
        or report["database_id"] != context.database_id
        or report["scope"] != {**_scope(context, period), "as_of": period}
        or report["mapping_version"] != _financial.MAPPING_V1
    ):
        fail("storage_corrupt", "保存的关账三表、冻结范围或摘要不一致。")
    return first


def _record(repo, row, context):
    try:
        basis, report = loads_json(row["basis_json"]), loads_json(row["report_json"])
        validate_contract("reporting.schema.json", "FinancialSnapshot", report)
    except ContractError as error:
        raise ContractError("storage_corrupt", "保存的关账三表不符合正式合同。") from error
    first = _header(repo, row, context, basis, report)
    if content_digest(report) != row["report_digest"] or any(
        check["status"] == "fail" for check in report["checks"]
    ):
        fail("storage_corrupt", "保存的关账三表或完整摘要不一致。")
    return first, report


def period_closure(period, context):
    validate_contract("core.schema.json", "Month", period)
    if period < context.start_period:
        fail("invalid_input", "不能将建账起点以前的月份作为账期范围。")
    repo = context.repository("ledger")
    row = repo.close_header(period)
    if row is None:
        return PeriodClosure(period, "open")
    try:
        basis = loads_json(row["basis_json"])
        metadata = loads_json(row["report_metadata_json"])
    except ContractError as error:
        raise ContractError("storage_corrupt", "保存的关账头格式不合法。") from error
    first = _header(repo, row, context, basis, metadata)
    return PeriodClosure(period, "closed", row["period"], first, row["result_head"])


def closed_report(scope, context):
    normalized = _financial.normalize(scope, context)
    if normalized["view"] != "as_posted":
        fail("invalid_input", "关账报告只接受历史采用口径。")
    repo = context.repository("ledger")
    last = normalized["period_to"]
    row = repo.close_record(last, covering=True)
    if row is None:
        fail("not_found", "该报表截止月尚未被关账覆盖。")
    _, saved = _record(repo, row, context)
    return _closed_result(scope, normalized, context, repo, row, saved)


def _closed_result(scope, normalized, context, repo, row, saved):
    last = normalized["period_to"]
    if normalized["period_from"] == last == row["period"]:
        result = saved
        result["head"] = context.head
        result["selection"]["closed_periods"] = repo.closed_periods(
            last[:4] + "-01", last, start_period=context.start_period
        )
    else:
        result = _financial.snapshot(normalized, context, mapping_version=saved["mapping_version"])
    if (
        scope.get("expected_head", result["selection"]["selection_digest"])
        != result["selection"]["selection_digest"]
    ):
        fail("snapshot_changed", "同一历史报告范围的正式采用已变化。")
    validate_contract("reporting.schema.json", "FinancialSnapshot", result)
    return result


def _issue(code, field, message):
    return {
        "code": code,
        "field": field,
        "meaning": "关账所需的正式核算依据",
        "message": message,
        "allowed_precision": ["fen", "source"],
        "reusable_sources": [],
        "blocking_scope": "period",
    }


def check_period(period, context):
    validate_contract("core.schema.json", "Month", period)
    if period < context.start_period:
        fail("invalid_input", "不能将建账起点以前的资料月份作为关账截止。")
    repo = context.repository("ledger")
    frontier = repo.closed_through()
    if frontier is not None and period <= frontier:
        row = repo.close_record(period, covering=True)
        first, saved = _record(repo, row, context)
        scope = _scope(context, period)
        return LedgerPeriodCheck(
            period,
            first,
            frontier,
            "closed",
            row["result_head"],
            (),
            _closed_result(scope, _financial.normalize(scope, context), context, repo, row, saved),
        )
    first = _first(frontier, context.start_period)
    basis, heads = _basis(repo, first, period, context)
    ids = [head["result_id"] for head in heads]
    repo.check_output_digests(ids, include_baselines=True)
    repo.check_sealed(ids)
    if any(has_unmapped_line(voucher["lines"]) for voucher in repo.vouchers(ids)):
        fail("storage_corrupt", "正式凭证存在未映射非零科目，不能冻结为正确账务。")
    owners = {head["result_id"]: head["business_id"] for head in heads}
    movements = repo.children("result_fund_movement", ids)
    businesses = {owners[row["result_id"]] for row in movements}
    for table in ("obligation", "cost_lot"):
        businesses.update(owners[row["result_id"]] for row in repo.children(table, ids))
    for table in ("settlement", "cost_consumption", "obligation_reclassification"):
        for row in repo.children(table, ids):
            businesses.update((owners[row["result_id"]], row["source_business_id"]))
            if table == "obligation_reclassification":
                businesses.add(row["target_business_id"])
    selected = repo.resource_view(as_of=period)
    if businesses:
        validate_capacities(selected, businesses)
    unique_movements(selected, {row["movement_id"] for row in movements})
    report = _financial.snapshot(_scope(context, period), context)
    issues = [
        _issue(
            "financial_reconciliation_failed",
            "/checks/" + check["code"],
            "正式三表的已知金额不能对平，须先查明并更正。",
        )
        for check in report["checks"]
        if check["status"] == "fail"
    ]
    if not report["selection"].get("opening_business_id"):
        issues.append(
            _issue(
                "opening_basis_missing", "/opening", "缺少正式期初，不能把未明确的账本起点当作零。"
            )
        )
    head = _head(basis, frontier, report)
    return LedgerPeriodCheck(period, first, frontier, "open", head, tuple(issues), report)


def close_period(period, expected_head, unit):
    validate_contract("core.schema.json", "Digest", expected_head)
    checked = check_period(period, unit)
    if checked.state == "closed":
        return checked
    if checked.head != expected_head:
        fail("conflict", "检查后的正式核算范围已变化，请重新检查关账范围。")
    if not checked.ready:
        raise ContractError(
            "needs_information",
            "正式核算尚未具备关账条件。",
            fact_issues=list(checked.blocking_issues),
        )
    completed = [False]

    def require_complete():
        if not completed[0]:
            fail("invalid_operation", "本次关账没有完整完成，不能提交部分冻结。")

    unit.check_before_commit(require_complete)
    repo = unit.repository("ledger")
    basis, _ = _basis(repo, checked.period_from, period, unit)
    repo.freeze_range(checked.period_from, period)
    repo.save_close(period, checked.head, basis, checked.report, datetime.now(UTC).isoformat())
    closed = check_period(period, unit)
    completed[0] = True
    return closed
