"""Build, verify and publish one explicit offline target without catalog access."""

import shutil
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path
from uuid import uuid4

from ai_accounting_modular.contracts.public import ContractError, validate_contract
from ai_accounting_modular.records.public import copy_standard_bundle, open_standard_bundle
from ai_accounting_modular.storage.public import (
    CompanySpec,
    company_metadata,
    create_company_database,
    open_database_unit,
    verify_database,
)

from . import _backup_verify, _documents
from ._backup_scope import DATABASE_PATH
from ._target_paths import beneath, checked, copy_file, file_digest


def _target(request):
    value = Path(request["new_target_directory"])
    if not value.is_absolute():
        raise ContractError("invalid_path", "新目标目录须明确为绝对路径。")
    path = checked(value).resolve()
    if path.exists():
        raise ContractError("target_exists", "新目标已经存在，即使为空也不能覆盖。")
    return path


def _apart(source, target):
    source, target = source.resolve(), target.resolve()
    if source.is_relative_to(target) or target.is_relative_to(source):
        raise ContractError("invalid_path", "新目标与源资料不能重叠。")


@contextmanager
def _stage(target, check_only):
    if check_only:
        path = Path(tempfile.mkdtemp(prefix=".accounting-check-"))
    else:
        checked(target.parent).mkdir(parents=True, exist_ok=True)
        path = Path(tempfile.mkdtemp(prefix=".accounting-build-", dir=target.parent))
    identity = path.stat()
    parent = path.parent.resolve()
    try:
        yield path
    finally:
        if path.exists() or path.is_symlink():
            checked(path)
            current = path.stat()
            if path.resolve().parent != parent or (current.st_dev, current.st_ino) != (
                identity.st_dev,
                identity.st_ino,
            ):
                raise ContractError("target_changed", "暂存目录已被替换，保留现场供核验。")
            # Only this invocation's still-identical temporary root is removed.
            shutil.rmtree(path)


def _unit(application, root, mode="read"):
    return open_database_unit(
        beneath(root, DATABASE_PATH),
        repositories=application.repositories,
        company_root=root,
        mode=mode,
    )


def _publish_documents(application, root):
    with _unit(application, root, "maintenance") as unit:
        application.records.ensure_document_targets(unit)
    with _unit(application, root) as read:
        snapshot = _documents.capture(application, read)
    prepared = application.records.prepare_documents(snapshot)
    with _unit(application, root, "maintenance") as unit:
        status = application.records.publish_documents(prepared, unit)
    if status["state"] != "ready":
        raise ContractError("documents_pending", "新账的标准业务文档尚未完整归档。")


def _rebuild(application, bundle, root):
    identity = bundle.company_identity
    copy_standard_bundle(bundle, root)
    create_company_database(
        CompanySpec(
            str(uuid4()),
            str(uuid4()),
            identity["credit_code"],
            identity["legal_name"],
            identity["start_period"],
            root,
        )
    )
    with ExitStack() as stack:
        with _unit(application, root) as read:
            sources = stack.enter_context(application.records.prepare_sources(bundle.sources, read))
        with _unit(application, root, "write") as unit:
            application.rebuild_standard(bundle, unit, prepared_sources=sources)
    _publish_documents(application, root)


def _restore(application, extracted, root):
    manifest = extracted.manifest
    for item in manifest["files"]:
        copy_file(
            beneath(extracted.company_root, item["path"]),
            beneath(root, item["path"]),
            item["sha256"],
            item["byte_size"],
        )
    for item in manifest["files"]:
        if file_digest(beneath(root, item["path"])) != (item["sha256"], item["byte_size"]):
            raise ContractError("target_changed", "恢复目标的实际字节与备份完整清单不一致。")
    from ai_accounting_modular.files.public import repository_factory as files_repository
    from ai_accounting_modular.files.public import reset_for_restore as reset_files
    from ai_accounting_modular.reading.public import repository_factory as reading_repository
    from ai_accounting_modular.reading.public import reset_for_restore as reset_reading

    with open_database_unit(
        beneath(root, DATABASE_PATH),
        repositories=(files_repository(), reading_repository()),
        company_root=root,
        mode="maintenance",
    ) as unit:
        reset_files(unit)
        reset_reading(unit)
    return application.backup_verifier.verify_extracted(root, manifest)


def _message_parts(code, text, label):
    if len(text) <= 2000:
        return [{"code": code, "state": "fail", "message": text}]
    result, offset = [], 0
    while offset < len(text):
        prefix = f"{label}第{len(result) + 1}段："
        length = 2000 - len(prefix)
        result.append(
            {"code": code, "state": "fail", "message": prefix + text[offset : offset + length]}
        )
        offset += length
    return result


def _failure(result, error):
    if not isinstance(error, ContractError):
        result.update(
            state="failed", error="source_unavailable", message="离线资料读取或目标交付未完成。"
        )
        return
    result.update(
        state="failed",
        error=error.code,
        message=error.message
        if len(error.message) <= 2000
        else "离线处理未完成，完整错误正文见检查项。",
    )
    if len(error.message) > 2000:
        result["checks"].extend(_message_parts("error_message", error.message, "错误正文"))
    for issue in error.fact_issues:
        context = ("业务：" + issue["business_id"] + "\n") if "business_id" in issue else ""
        context += "字段：" + issue["field"] + "\n" + issue["message"]
        result["checks"].extend(_message_parts(issue["code"], context, "缺项正文"))


def run(application, request):
    validate_contract("core.schema.json", "IsolatedTargetRequest", request)
    result = {
        "action": request["action"],
        "state": "failed",
        "new_target_directory": request["new_target_directory"],
        "checks": [],
    }
    try:
        target = _target(request)
        source = checked(request["source_path"])
        if not Path(request["source_path"]).is_absolute():
            raise ContractError("invalid_path", "源资料须明确为绝对路径。")
        if application.backup_verifier is None:
            raise ContractError("unsupported", "当前实例未装配离线完整性核验。")
        check_only = request["mode"] == "check"
        if request["action"] == "rebuild":
            bundle = open_standard_bundle(source)
            _apart(bundle.company_root, target)
            result["source_digest"] = bundle.manifest_digest
            with _stage(target, check_only) as stage:
                _rebuild(application, bundle, stage)
                _finish(application, stage, target, check_only, result)
        else:
            _apart(source, target)
            opener = (
                application.backup_verifier.open
                if check_only
                else application.backup_verifier.extract
            )
            with source.open("rb") as stream:
                with opener(stream) as extracted:
                    result["source_digest"] = extracted.sha256
                    if check_only:
                        _publish(extracted.company_root, target, True, result)
                    else:
                        _backup_verify.verify_structure_contract(extracted.company_root)
                        with _stage(target, False) as stage:
                            metadata = _restore(application, extracted, stage)
                            _publish(stage, target, False, result, metadata)
    except (ContractError, OSError) as error:
        _failure(result, error)
    return validate_contract("core.schema.json", "IsolatedTargetResult", result)


def _finish(application, stage, target, check_only, result):
    verify_database(beneath(stage, DATABASE_PATH))
    with _unit(application, stage) as read:
        application.backup_verifier.verify_snapshot(read)
        metadata = company_metadata(read)
    _publish(stage, target, check_only, result, metadata)


def _publish(stage, target, check_only, result, metadata=None):
    result["checks"].append(
        {"code": "complete_target", "state": "pass", "message": "完整资料及正式账务核验通过。"}
    )
    if check_only:
        result["state"] = "checked"
        return
    from ai_accounting_modular.storage.public import publish_company_directory

    publish_company_directory(stage, target)
    result.update(state="created", company_id=metadata.company_id, database_id=metadata.database_id)
