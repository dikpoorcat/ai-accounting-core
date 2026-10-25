"""Produce and fully verify the retained workbook without reading business bodies."""

import io
import os
import tempfile
import zipfile
from contextlib import nullcontext
from xml.etree import ElementTree

from ai_accounting_modular.files.public import (
    FileArtifact,
    FileScope,
    ProductionError,
    VerifiedArtifact,
)
from ai_accounting_modular.storage.public import company_metadata

from . import _report_plans, _report_workbook

_ARTIFACT = "work/report.xlsx"


def _path(context):
    path = context.task_root / _ARTIFACT
    for parent in (*reversed(path.parents), path):
        if parent.is_symlink() or parent.is_junction():
            raise ProductionError("unsafe_path", "报表任务位置不能经过链接。", retryable=False)
    return path


class ReportProducer:
    def __init__(self, storage):
        self.storage = storage

    def describe(self, plan):
        scope = _report_plans.validate(plan)
        return FileScope(
            f"quarter:{scope['period_from']}:{plan['variant']}",
            plan["scope_head"],
            _report_plans.file_name(scope, plan["organization"]),
        )

    def recover_lease(self, plan, context):
        return nullcontext()

    def is_current(self, plan, context):
        with self.storage.read_unit(plan["company_id"]) as read:
            return (context.company.company_id, context.company.database_id) == (
                read.company_id,
                read.database_id,
            ) and _report_plans.is_current(plan, read, company_metadata(read))

    def produce(self, plan, context):
        self.describe(plan)
        raw = _report_workbook.render(plan, context.task_id)
        path = _path(context)
        path.parent.mkdir(parents=True, exist_ok=True)
        _path(context)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=".report-", dir=path.parent, delete=False
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
                _path(context)
                os.unlink(temporary)
        return FileArtifact(_ARTIFACT)

    def verify(self, plan, stream, context):
        self.describe(plan)
        expected = _report_workbook.render(plan, context.task_id)
        try:
            with (
                zipfile.ZipFile(stream) as actual,
                zipfile.ZipFile(io.BytesIO(expected)) as reference,
            ):
                names = actual.namelist()
                if names != reference.namelist() or any(
                    item.orig_filename != item.filename or item.flag_bits & 1
                    for item in actual.infolist()
                ):
                    raise ValueError("workbook_members")
                for name in names:
                    if actual.getinfo(name).file_size != reference.getinfo(name).file_size:
                        raise ValueError("workbook_member_size")
                    raw = actual.read(name)
                    if name.endswith((".xml", ".rels")):
                        ElementTree.fromstring(raw)
                    if raw != reference.read(name):
                        raise ValueError("workbook_content")
        except (
            zipfile.BadZipFile,
            NotImplementedError,
            EOFError,
            ValueError,
            ElementTree.ParseError,
        ) as error:
            raise ProductionError(
                "report_invalid", "报表完整内容与该公司、任务及正式计划不一致。", retryable=False
            ) from error

    def publish(self, plan, artifact, context):
        if not isinstance(artifact, VerifiedArtifact):
            raise ProductionError(
                "file_invalid", "报表发布须使用已核验的独立字节。", retryable=False
            )
        artifact.require_scope(context, plan["scope_head"])
        if artifact.relative_path != _ARTIFACT:
            raise ProductionError("report_invalid", "报表候选位置不正确。", retryable=False)
        return _path(context)

    def cleanup(self, context, *, keep_capture):
        pass  # Files owns the delivery task directory; report plans have no capture files.

    def discard_capture(self, context):
        raise ProductionError("invalid_operation", "报表任务没有临时捕获。", retryable=False)


def create_report_producer(storage):
    return ReportProducer(storage)
