"""Single-call accounting coordination with explicit, currently delivered owners."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence
from contextlib import ExitStack

from ai_accounting_modular.contracts.public import (
    CONTRACT,
    ApplyRequest,
    ApplyResult,
    BusinessGet,
    ContractError,
    FinancialSnapshot,
    JobsRequest,
    ReadContext,
    ReportRequest,
    SourcesCommandResult,
    SourcesRequest,
    TaskReceipt,
    UnitOfWork,
    canonical_json,
    schema_document,
    validate_contract,
)
from ai_accounting_modular.reading.public import ReadingImpact
from ai_accounting_modular.storage.public import RepositoryFactory, module_schema

from . import (
    _backup_capture,
    _documents,
    _execution,
    _month_cancel,
    _payment_exports,
    _period,
    _queries,
    _rebuild,
    _schema,
    _source_discards,
    _sources,
)
from ._backup_producer import create_backup_producer
from ._backup_scope import backup_scope_head
from ._backup_verify import BackupVerifier, VerifiedBackup, create_backup_verifier
from ._context import DomainRegistry
from ._repository import ApplicationRepository
from ._submission import ExecutionOutcome, Submission
from ._work_scope import WorkScope, work_scope

__all__ = [
    "Application",
    "BackupVerifier",
    "ExecutionOutcome",
    "VerifiedBackup",
    "WorkScope",
    "backup_scope_head",
    "create_application",
    "create_backup_producer",
    "create_backup_verifier",
    "repository_factory",
]

ReadingMarker = Callable[[ReadingImpact, UnitOfWork], Sequence[str]]


def repository_factory():
    return RepositoryFactory(module_schema("application"), ApplicationRepository)


class Application:
    def __init__(
        self,
        storage,
        identity,
        ledger,
        records,
        domains,
        money=None,
        mark_reading_targets: ReadingMarker | None = None,
        files=None,
        backup_verifier=None,
        repositories=(),
        payments=None,
    ):
        self.storage, self.identity, self.ledger, self.records = storage, identity, ledger, records
        self.registry = DomainRegistry(domains)
        self.money = money
        self._reading_targets = mark_reading_targets
        self.files = files
        self.backup_verifier = backup_verifier
        self.repositories = tuple(repositories)
        self.payments = payments

    def _notify_reading(self, impact, unit):
        return (
            bool(self._reading_targets(impact, unit))
            if impact and self._reading_targets is not None
            else False
        )

    def accounting_schema(self, request):
        return _schema.result(self.registry, request)

    def accounting_context(self, request):
        return self.context(request)

    def context(self, request, read_context=None):
        return _queries.context(self, request, read_context)

    def work_scope(self, read_context: ReadContext) -> WorkScope:
        return work_scope(self, read_context)

    def accounting_find(self, request):
        return _queries.find(self, request)

    def find_businesses(self, query, read_context):
        return _queries.find(self, query, read_context)

    def accounting_remuneration(self, request):
        validate_contract("core.schema.json", "RemunerationRequest", request)
        view = request.get("view", "current")
        owner = self.registry.owner("people.remuneration")
        with self.storage.read_unit(request["company_id"]) as read:
            return validate_contract(
                "core.schema.json",
                "RemunerationPositionResult",
                {
                    "company_id": read.company_id,
                    "database_id": read.database_id,
                    "period": request["period"],
                    "view": view,
                    "items": list(
                        owner.remuneration_positions(
                            request["business_ids"], request["period"], read, view=view
                        )
                    ),
                },
            )

    def get_businesses(self, query, read_context):
        return _queries.get(self, query, read_context)

    def accounting_period(self, request):
        return _period.run(self, request)

    def accounting_cancel_month(self, request):
        return _month_cancel.run(self, request)

    def capture_backup_snapshot(self, company_id, expected_database_id):
        return _backup_capture.capture(self, company_id, expected_database_id)

    def accounting_backup(self, request):
        return _backup_capture.start(self, request)

    def accounting_sources(self, request: SourcesRequest) -> SourcesCommandResult:
        return _sources.run(self, request)

    def finish_source_discards(self, company_id, *, after_marker_id=None, limit=500):
        return _source_discards.finish(
            self, company_id, after_marker_id=after_marker_id, limit=limit
        )

    def accounting_jobs(self, request: JobsRequest) -> TaskReceipt:
        validate_contract("core.schema.json", "JobsRequest", request)
        if self.files is None:
            raise ContractError("unsupported", "当前实例未装配文件任务。")
        return self.files.get(request["company_id"], request["task_id"])

    def accounting_report(self, request: ReportRequest) -> FinancialSnapshot:
        validate_contract("core.schema.json", "ReportRequest", request)
        year, quarter = request["year"], request["quarter"]
        last = f"{year:04d}-{quarter * 3:02d}"
        scope = {
            "company_id": request["company_id"],
            "period_from": f"{year:04d}-{quarter * 3 - 2:02d}",
            "period_to": last,
            "view": "as_posted",
            "as_of": last,
        }
        with self.storage.read_unit(request["company_id"]) as read:
            snapshot = self.ledger.financial_snapshot(scope, read)
        expected = request.get("expected_head")
        if expected is not None and expected != snapshot["selection"]["selection_digest"]:
            raise ContractError("snapshot_changed", "季度报表采用结果已变化，请重新读取。")
        return snapshot

    def rebuild_standard(self, bundle, unit, *, prepared_sources=None) -> ExecutionOutcome:
        return _rebuild.rebuild(self, bundle, unit, prepared_sources=prepared_sources)

    def accounting_isolated(self, request):
        from . import _isolated

        return _isolated.run(self, request)

    def accounting_objects(self, request):
        validate_contract("core.schema.json", "ObjectsRequest", request)
        with self.storage.read_unit(request["company_id"]) as read:
            if request["action"] == "find":
                return self.identity.find(request, read)
            objects = self.identity.get_batch(request["object_ids"], read)
            present = {obj["object_id"] for obj in objects}
            return validate_contract(
                "core.schema.json",
                "ObjectsResult",
                {
                    "company_id": read.company_id,
                    "head": read.head,
                    "objects": list(objects),
                    "missing_object_ids": [
                        key for key in request["object_ids"] if key not in present
                    ],
                },
            )

    def accounting_apply(self, request: ApplyRequest) -> ApplyResult | dict:
        try:
            validate_contract("core.schema.json", "ApplyRequest", request)
        except ContractError as error:
            return error.to_response()
        try:
            if len(canonical_json(request).encode("utf-8")) > 8 * 1024 * 1024:
                raise ContractError(
                    "batch_too_large", "本单规范内容超过8MiB，请按独立业务单元提交。"
                )
            outcome = self._apply(request)
        except ContractError as error:
            known = schema_document("core.schema.json")["$defs"]["ApplyResult"]["properties"][
                "error"
            ]["enum"]
            if error.code not in known:
                return error.to_response()
            result = self._base_response(request)
            result.update(
                error=error.code, message=error.message, fact_issues=list(error.fact_issues)
            )
            if error.diagnostic_id:
                result["diagnostic_id"] = error.diagnostic_id
            return validate_contract("core.schema.json", "ApplyResult", result)
        with self.storage.read_unit(request["company_id"]) as read:
            status = self.records.document_status(read)
        if status["state"] != "ready":
            status = _documents.finish(self, request["company_id"])
        outcome["documents"] = status["state"]
        outcome["completion"] = (
            "complete" if status["state"] == "ready" else "saved_documents_pending"
        )
        if status["set_id"]:
            outcome["document_set_id"] = status["set_id"]
        return validate_contract("core.schema.json", "ApplyResult", outcome)

    @staticmethod
    def _base_response(request):
        return {
            "contract": CONTRACT,
            "company_id": request["company_id"],
            "command_id": request["command_id"],
            "completion": "not_saved",
            "accounting": "unchanged",
            "documents": "unchanged",
            "reading": "unchanged",
            "businesses": [],
            "affected_periods": [],
            "fact_issues": [],
            "unresolved": [],
            "changes": "none",
            "participants": {"objects": [], "notes": [], "issue_scopes": []},
        }

    def _apply(self, request):
        submission = Submission.from_request(request)
        with ExitStack() as stack:
            prepared = None
            if submission.sources:
                with self.storage.read_unit(submission.company_id) as read:
                    prepared = stack.enter_context(
                        self.records.prepare_sources(submission.sources, read)
                    )
            active = stack.enter_context(self.storage.write_unit(submission.company_id))
            outcome = _execution.execute(
                self,
                submission,
                active,
                prepared_sources=prepared,
            )
            result = self._base_response(request)
            result.update(
                businesses=self._states(outcome.heads, active),
                affected_periods=list(outcome.affected_periods),
                participants={
                    "objects": [
                        {key: obj[key] for key in ("object_id", "seq", "content_digest")}
                        for obj in outcome.objects
                    ],
                    "notes": list(outcome.notes),
                    "issue_scopes": list(outcome.issue_scopes),
                },
                unresolved=list(outcome.unresolved),
                changes="committed" if outcome.changed else "none",
                accounting="saved" if outcome.accounting_changed else "unchanged",
                reading="preparing" if outcome.reading_changed else "unchanged",
            )
        return result

    def _states(self, heads, read, *, references=None):
        if references is None:
            ids = [head["business_id"] for head in heads]
            references = tuple(
                ref
                for offset in range(0, len(ids), 500)
                for ref in self.ledger.get_result_refs(ids[offset : offset + 500], read)
            )
        grouped = defaultdict(list)
        for reference in references:
            grouped[reference["business_id"]].append(reference)
        return [
            {
                "business_id": head["business_id"],
                "seq": head["seq"],
                "state": head["state"],
                "fact_digest": head["current_digest"],
                "results": grouped[head["business_id"]],
            }
            for head in heads
        ]

    def accounting_get(self, query: BusinessGet):
        return _queries.get(self, query)

    def accounting_payment_export(self, request):
        return _payment_exports.run(self, request)

    def accounting_documents(self, request):
        validate_contract("core.schema.json", "DocumentsRequest", request)
        if request["action"] == "finish":
            return _documents.finish(self, request["company_id"], verify_ready=True)
        with self.storage.read_unit(request["company_id"]) as read:
            return self.records.document_status(read)


def create_application(
    *,
    storage,
    identity,
    ledger,
    records,
    domains,
    money=None,
    mark_reading_targets: ReadingMarker | None = None,
    files=None,
    backup_verifier=None,
    repositories=(),
    payments=None,
):
    return Application(
        storage,
        identity,
        ledger,
        records,
        domains,
        money,
        mark_reading_targets,
        files,
        backup_verifier,
        repositories,
        payments,
    )
