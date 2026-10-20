"""Rebuildable reads use public owners within one company snapshot."""

from ai_accounting_modular.contracts.public import ContractError, validate_contract
from ai_accounting_modular.storage.public import RepositoryFactory, module_schema

from . import _context, _materials, _models, _report_exports
from ._report_producer import create_report_producer
from ._repository import ReadingRepository
from ._types import (
    BusinessReader,
    ModelDelivery,
    PeriodRange,
    ReadingImpact,
    ReportDownload,
    SourceDownload,
)

__all__ = [
    "BusinessReader",
    "ModelDelivery",
    "PeriodRange",
    "ReadingImpact",
    "SourceDownload",
    "ReportDownload",
    "Reading",
    "create_reading",
    "create_report_producer",
    "repository_factory",
    "reset_for_restore",
]


def repository_factory():
    return RepositoryFactory(module_schema("reading"), ReadingRepository, ReadingRepository)


def reset_for_restore(uow) -> None:
    """Discard projections tied to the previous location; source accounting is unchanged."""
    uow.repository("reading").reset()


class Reading:
    def __init__(self, *, storage, ledger, records, businesses: BusinessReader, files=None):
        self.storage, self.ledger = storage, ledger
        self.records, self.businesses = records, businesses
        self.files = files

    def page(self, page_key, scope, query):
        if query:
            raise ContractError("invalid_input", "当前读取入口不接受额外筛选。")
        if page_key == "context":
            return _context.context(self, scope)
        if page_key == "quarterly_report":
            return _models.page(self, scope)
        raise ContractError("unsupported_business", "此看板页面尚未接入新读取模型。")

    def detail(self, detail_key, scope, query):
        if detail_key != "review_materials":
            raise ContractError("unsupported_business", "此只读详情尚未接入。")
        validate_contract("core.schema.json", "ContextScope", scope)
        if scope.get("company_id") != query.get("company_id"):
            raise ContractError("invalid_input", "资料查阅的公司范围不一致。")
        with self.storage.read_unit(scope["company_id"]) as read:
            return _materials.page(self, query, read)

    def source_content(self, query):
        return _materials.content(self, query)

    def export_report(self, scope, head, request_id, *, epochs):
        return _report_exports.export(self, scope, head, request_id, epochs=epochs)

    def report_task(self, company_id, job_id):
        return _report_exports.task(self, company_id, job_id)

    def report_download(self, company_id, job_id):
        return _report_exports.download(self, company_id, job_id)

    def mark_targets(self, impact: ReadingImpact, uow):
        return _models.mark_targets(impact, uow)

    def run_pending(self, company_id, *, limit=20):
        return _models.run_pending(self, company_id, limit)

    def retry(self, scope):
        normalized = _models.normalize(scope)
        with self.storage.maintenance_unit(scope["company_id"]) as unit:
            unit.repository("reading").retry(_models.key(normalized), _models.FINANCIAL)


def create_reading(*, storage, ledger, records, businesses, files=None):
    return Reading(
        storage=storage, ledger=ledger, records=records, businesses=businesses, files=files
    )
