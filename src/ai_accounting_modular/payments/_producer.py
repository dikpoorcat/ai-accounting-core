"""Durable Files worker adapter; plan validity remains the payment module's responsibility."""

import os
import tempfile
from contextlib import nullcontext

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.files.public import (
    FileArtifact,
    FileScope,
    ProductionError,
    VerifiedArtifact,
)

from . import _plans, _workbook

ARTIFACT = "work/payment-export.xlsx"


def _path(context):
    path = context.task_root / ARTIFACT
    for parent in (*reversed(path.parents), path):
        if parent.is_symlink() or parent.is_junction():
            raise ProductionError("unsafe_path", "代发表任务位置不能经过链接。", retryable=False)
    return path


class PaymentExportProducer:
    def __init__(self, payments):
        self.payments = payments

    def describe(self, plan):
        _plans.validate(plan)
        suffix = "选定业务" if plan["scope"]["selection"] == "selected" else "月度候选"
        return FileScope(
            _plans.scope_key(plan["scope"]),
            plan["scope_head"],
            f"{plan['scope']['period']}{suffix}银行代发表.xlsx",
        )

    def recover_lease(self, plan, context):
        return nullcontext()

    def is_current(self, plan, context):
        if context.company.database_id != plan["database_id"]:
            return False
        with self.payments.storage.read_unit(plan["company_id"]) as read:
            return self.payments.is_current(plan, read)

    def _template(self, plan):
        with self.payments.storage.read_unit(plan["company_id"]) as read:
            with self.payments.records.lease_source(plan["template"]["source_id"], read) as lease:
                if lease.source["sha256"] != plan["template"]["sha256"]:
                    raise ProductionError(
                        "plan_expired", "银行模板已变化，请重新生成代发表。", retryable=False
                    )
                return lease.read()

    def produce(self, plan, context):
        self.describe(plan)
        raw = _workbook.render(self._template(plan), plan["template"]["layout"], plan["rows"])
        path = _path(context)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=".payment-", dir=path.parent, delete=False
            ) as output:
                temporary = output.name
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            _path(context)
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                os.unlink(temporary)
        return FileArtifact(ARTIFACT)

    def verify(self, plan, stream, context):
        self.describe(plan)
        try:
            _workbook.verify(
                stream.read(), self._template(plan), plan["template"]["layout"], plan["rows"]
            )
        except ContractError as error:
            raise ProductionError(error.code, error.message, retryable=False) from error

    def publish(self, plan, artifact, context):
        if not isinstance(artifact, VerifiedArtifact):
            raise ProductionError(
                "file_invalid", "代发表发布须使用已核验的独立字节。", retryable=False
            )
        artifact.require_scope(context, plan["scope_head"])
        if artifact.relative_path != ARTIFACT:
            raise ProductionError(
                "payment_export_invalid", "代发表候选位置不正确。", retryable=False
            )
        return _path(context)

    def cleanup(self, context, *, keep_capture):
        pass  # Files owns the task directory; the registered bank template is not temporary.

    def discard_capture(self, context):
        raise ProductionError("invalid_operation", "代发表任务没有临时捕获。", retryable=False)


def create_payment_export_producer(payments):
    return PaymentExportProducer(payments)
