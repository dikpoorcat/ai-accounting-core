"""Rebuildable reads use public owners within one company snapshot."""

from ai_accounting_modular.contracts.public import ContractError, validate_contract
from ai_accounting_modular.storage.public import RepositoryFactory, module_schema

from . import _context, _materials, _models, _month_queries, _monthly, _report_exports
from ._report_producer import create_report_producer
from ._repository import ReadingRepository
from ._types import (
    BusinessReader,
    ModelDelivery,
    MonthScope,
    PeriodRange,
    ReadingImpact,
    ReportDownload,
    SourceDownload,
)

__all__ = [
    "BusinessReader",
    "ModelDelivery",
    "MonthScope",
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
    def __init__(
        self,
        *,
        storage,
        ledger,
        records,
        businesses: BusinessReader,
        files=None,
        identity=None,
        money=None,
        investment=None,
        assets=None,
    ):
        self.storage, self.ledger = storage, ledger
        self.records, self.businesses = records, businesses
        self.files = files
        self.identity, self.money, self.investment = identity, money, investment
        self.assets = assets

    def page(self, page_key, scope, query):
        if page_key == "assets":
            from ._assets import build

            query = _month_queries.assets(query, scope)
            return _monthly.deliver(
                self,
                scope,
                (_monthly.ASSETS,),
                lambda snapshot, read: build(snapshot, read, query, identity=self.identity),
            )
        if page_key == "funds":
            from ._funds import build

            query = _month_queries.funds(query, scope)
            return _monthly.deliver(
                self,
                scope,
                (_monthly.FUNDS,),
                lambda snapshot, read: build(snapshot, read, query),
            )
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

    def retry(self, page_key, scope, query=None):
        """An explicit retry only restarts failed projections; accounting is untouched."""
        query = {} if query is None else query
        if page_key == "quarterly_report":
            if query:
                raise ContractError("invalid_input", "季度重试不接受额外筛选。")
            normalized = _models.normalize(scope)
            scope_key, components = _models.key(normalized), (_models.FINANCIAL,)
            company_id = scope["company_id"]
        else:
            if not isinstance(scope, MonthScope) or scope.period is None:
                raise ContractError("invalid_input", "月页重试须明确公司和月份。")
            if page_key == "funds":
                _month_queries.funds(query, scope)
                components = (_monthly.FUNDS,)
            elif page_key == "assets":
                _month_queries.assets(query, scope)
                components = (_monthly.ASSETS,)
            else:
                raise ContractError("invalid_input", "此读取页面尚未接入。")
            normalized = _monthly.normalize(
                {
                    "company_id": scope.company_id,
                    "period_from": scope.period,
                    "period_to": scope.period,
                    "view": "as_posted",
                }
            )
            return _monthly.retry(self, normalized, components)
        with self.storage.maintenance_unit(company_id) as unit:
            repository = unit.repository("reading")
            for component in components:
                repository.retry(scope_key, component)


def create_reading(
    *,
    storage,
    ledger,
    records,
    businesses,
    files=None,
    identity=None,
    money=None,
    investment=None,
    assets=None,
):
    return Reading(
        storage=storage,
        ledger=ledger,
        records=records,
        businesses=businesses,
        files=files,
        identity=identity,
        money=money,
        investment=investment,
        assets=assets,
    )
