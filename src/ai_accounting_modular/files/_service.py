"""Durable current-task coordination without accounting or report formulas."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from uuid import uuid4

from ai_accounting_modular.contracts.public import (
    ContractError,
    FilePlan,
    ReadContext,
    canonical_json,
    content_digest,
    validate_contract,
)

from . import _paths, _plans, _runtime
from ._locks import locked
from ._plans import copy_json as _copy
from ._types import (
    CaptureContext,
    ExpireTask,
    FileArtifact,
    FileCompletion,
    FileFailure,
    FileProducer,
    FileTaskContext,
    ProductionError,
    RunClaim,
    VerifiedArtifact,
    VerifiedDownload,
)


def _failure(value):
    if (
        not isinstance(value, (FileFailure, ExpireTask))
        or not isinstance(value.code, str)
        or not 1 <= len(value.code) <= 100
        or not isinstance(value.message, str)
        or not 1 <= len(value.message) <= 2000
        or (isinstance(value, FileFailure) and type(value.retryable) is not bool)
    ):
        raise ContractError("invalid_input", "文件失败须有有界代码、说明及明确重试含义。")
    return value.code, value.message


def _as_failure(exc):
    if isinstance(exc, ContractError):
        return FileFailure(exc.code[:100], exc.message[:2000], getattr(exc, "retryable", True))
    return FileFailure("file_generation_failed", "文件生成未完成，请接续当前任务。")


def _transient_download_error(error):
    while error is not None:
        if isinstance(error, OSError):
            return not isinstance(error, (FileNotFoundError, IsADirectoryError, NotADirectoryError))
        if isinstance(error, ContractError) and error.code in {
            "busy",
            "temporary_failure",
            "source_unavailable",
        }:
            return True
        error = error.__cause__
    return False


class Files:
    def __init__(self, storage, producers: Mapping[str, FileProducer]):
        if not isinstance(producers, Mapping) or set(producers) - {"backup", "report_export"}:
            raise ContractError("invalid_input", "仅能装配备份和报表两类文件生产者。")
        self._storage = storage
        self._producers = dict(producers)
        self._runtimes = {}
        self._recovered = set()
        self._closed = False
        self._workers = 0
        self._lifecycle = RLock()

    def _metadata(self, company_id):
        if self._closed:
            raise ContractError("invalid_operation", "文件任务服务已经关闭。")
        validate_contract("core.schema.json", "Id", company_id)
        with self._storage.read_unit(company_id) as read:
            metadata = self._storage.company_metadata(read)
        with self._lifecycle:
            if self._closed:
                raise ContractError("invalid_operation", "文件任务服务已经关闭。")
            if company_id not in self._runtimes:
                self._runtimes[company_id] = (
                    metadata.company_root,
                    _runtime.attach(metadata.company_root),
                )
            elif self._runtimes[company_id][0] != metadata.company_root:
                raise ContractError("conflict", "公司位置已变化，须重新装配文件任务服务。")
        return metadata

    def _live(self, metadata):
        return self._runtimes[metadata.company_id][1]

    def _producer(self, kind):
        producer = self._producers.get(kind)
        if producer is None:
            raise ContractError("unsupported", "该类文件生产者尚未装配。")
        return producer

    def _row(self, company_id, task_id, *, plan=False):
        validate_contract("core.schema.json", "Id", task_id)
        with self._storage.read_unit(company_id) as read:
            return read.repository("files").get(task_id, plan=plan)

    def _receipt(self, company_id, row):
        if row is None:
            raise ContractError("not_found", "当前公司没有这个文件任务。")
        receipt = {key: row[key] for key in ("task_id", "kind", "state", "scope_head", "attempts")}
        receipt["company_id"] = company_id
        if row["state"] == "succeeded":
            receipt.update(
                path=row["output_path"], sha256=row["output_sha256"], byte_size=row["output_bytes"]
            )
        if row["error_code"] is not None:
            receipt.update(error_code=row["error_code"], message=row["error_message"])
        try:
            return validate_contract("core.schema.json", "TaskReceipt", receipt)
        except ContractError as exc:
            raise ContractError("storage_corrupt", "文件任务回执不符合当前严格合同。") from exc

    def _context(self, metadata, task_id, capture_id=None):
        validate_contract("core.schema.json", "Id", task_id)
        if capture_id is not None:
            validate_contract("core.schema.json", "Id", capture_id)
        return FileTaskContext(
            metadata,
            task_id,
            _paths.controlled(metadata.company_root, "runtime/file-tasks", task_id),
            _paths.controlled(metadata.company_root, "runtime/tasks", capture_id)
            if capture_id is not None
            else None,
        )

    def _lock_path(self, metadata, row):
        root = _paths.controlled(
            metadata.company_root, "runtime/file-task-locks", directory=True, create=True
        )
        return root / content_digest({"kind": row["kind"], "scope": row["scope_key"]})

    def _describe(self, plan):
        return _plans.describe(self._producer(plan["kind"]), plan)

    def _plan(self, metadata, row):
        return _plans.load(metadata, row, self._producer(row["kind"]))

    def _acquire_lease(self, metadata, task_id, plan):
        live = self._live(metadata)
        with live.guard:
            if task_id in live.leases:
                return
        context = self._context(metadata, task_id, plan.get("capture_id"))
        lease = self._producer(plan["kind"]).recover_lease(_copy(plan), context)
        lease.__enter__()
        try:
            if plan["kind"] == "backup":
                for item in plan["inputs"]:
                    root = (
                        metadata.company_root
                        if item["source_root"] == "company"
                        else context.capture_root
                    )
                    path = _paths.controlled(root, item["relative_path"])
                    _paths.captured_bytes(path, item["sha256"], item["byte_size"])
            with live.guard:
                if task_id not in live.leases:
                    live.leases[task_id] = (plan.get("capture_id"), lease)
                    return
        except OSError as exc:
            raise ProductionError(
                "capture_missing",
                "已保存计划的捕获输入缺失，不能重新捕获冒充原计划。",
                retryable=False,
            ) from exc
        finally:
            with live.guard:
                retained = task_id in live.leases and live.leases[task_id][1] is lease
            if not retained:
                lease.__exit__(None, None, None)

    def _change(self, company_id, row, **changes):
        with self._storage.maintenance_unit(company_id) as uow:
            repository = uow.repository("files")
            if repository.replace(row, **changes) != 1:
                raise ContractError("task_changed", "任务头或执行代次已经变化。")
            current = repository.get(row["task_id"])
        return current

    def _expire(self, metadata, row, reason):
        return self._change(
            metadata.company_id,
            row,
            state="expired",
            attempts=0,
            error=_failure(reason),
            expired=True,
        )

    def _cleanup_locked(self, metadata, row):
        if row["state"] != "expired":
            raise ContractError("invalid_operation", "只能清理已退出的当前文件任务。")
        with locked(self._lock_path(metadata, row), 1, blocking=False) as acquired:
            if not acquired:
                return False
            live = self._live(metadata)
            live.release(row["task_id"])
            context = self._context(metadata, row["task_id"], row["capture_id"])
            try:
                self._producer(row["kind"]).cleanup(
                    context, keep_capture=live.capture_pinned(row["capture_id"])
                )
                _paths.remove_task_root(context.task_root)
            except (OSError, ContractError) as exc:
                error = _as_failure(exc)
                self._change(
                    metadata.company_id,
                    row,
                    state="expired",
                    attempts=min(3, row["attempts"] + 1),
                    error=_failure(error),
                    expired=True,
                )
                return False
            with self._storage.maintenance_unit(metadata.company_id) as uow:
                if uow.repository("files").delete(row) != 1:
                    raise ContractError("task_changed", "清理期间任务头已经变化。")
            return True

    def _cleanup_capture(self, metadata, row):
        """Keep the confirmed delivery, removing its capture and redundant work copy."""
        live = self._live(metadata)
        live.release(row["task_id"])
        try:
            context = self._context(metadata, row["task_id"], row["capture_id"])
            output = self._output_path(context, row["kind"], Path(row["output_path"]))
            self._producer("backup").cleanup(
                context,
                keep_capture=live.capture_pinned(row["capture_id"]),
            )
            if not output.is_relative_to(context.task_root):
                _paths.remove_task_root(context.task_root)
        except (OSError, ContractError):
            # Delivery remains successful; saved capture_id/task_id locate any
            # remaining temporaries for startup or explicit maintenance to retry.
            return False
        return True

    def _discard_unaccepted_capture_locked(self, metadata, capture_id):
        with self._storage.read_unit(metadata.company_id) as read:
            current = read.repository("files").scope("backup", "company", plan=False)
        live = self._live(metadata)
        if (current is not None and current["capture_id"] == capture_id) or live.capture_pinned(
            capture_id
        ):
            return False
        context = CaptureContext(
            metadata,
            capture_id,
            _paths.controlled(metadata.company_root, "runtime/tasks", capture_id),
        )
        try:
            self._producer("backup").discard_capture(context)
            if context.capture_root.exists() or context.capture_root.is_symlink():
                raise OSError("capture cleanup did not remove its temporary root")
        except Exception as exc:
            raise ContractError(
                "file_cleanup_pending", "未接纳的临时捕获尚未清理完，请用原捕获标识重试。"
            ) from exc
        return True

    def discard_unaccepted_capture(self, company_id: str, capture_id: str) -> bool:
        """Discard only an unreferenced capture; False means it must be retained."""
        validate_contract("core.schema.json", "Id", capture_id)
        metadata = self._metadata(company_id)
        selector = {"kind": "backup", "scope_key": "company"}
        with locked(self._lock_path(metadata, selector), 0):
            return self._discard_unaccepted_capture_locked(metadata, capture_id)

    def recover(self, company_id):
        """Restore every current input lease before attempting any obsolete cleanup."""
        metadata = self._metadata(company_id)
        with self._storage.read_unit(company_id) as read:
            rows = read.repository("files").current()
        for initial in rows:
            if not _plans.retains_inputs(initial):
                continue
            with locked(self._lock_path(metadata, initial), 0):
                row = self._row(company_id, initial["task_id"], plan=True)
                if row is None or not _plans.retains_inputs(row):
                    continue
                try:
                    self._acquire_lease(metadata, row["task_id"], self._plan(metadata, row))
                except (ContractError, OSError) as exc:
                    failure = _as_failure(exc)
                    self._change(company_id, row, state="failed", error=_failure(failure))
        self._recovered.add(company_id)
        # A second pass is deliberate: cleanup must not race an unrestored pin.
        for initial in rows:
            if initial["state"] != "expired" and not (
                initial["state"] == "succeeded" and initial["kind"] == "backup"
            ):
                continue
            with locked(self._lock_path(metadata, initial), 0):
                row = self._row(company_id, initial["task_id"], plan=True)
                if row is not None and row["state"] == "expired":
                    self._cleanup_locked(metadata, row)
                elif row is not None and row["state"] == "succeeded":
                    with locked(self._lock_path(metadata, row), 1, blocking=False) as acquired:
                        if acquired:
                            self._cleanup_capture(metadata, row)

    def _prepare(self, company_id):
        metadata = self._metadata(company_id)
        if company_id not in self._recovered:
            self.recover(company_id)
        return metadata

    def retained_input_plans(self, kind: str, read_context: ReadContext) -> tuple[FilePlan, ...]:
        """Strict accepted plans still protecting inputs, using only the caller's unit.

        No recovery, lease restoration, current-head check or filesystem work is
        performed. A failed task still protects its original inputs; succeeded
        and expired tasks no longer do. Plan input interpretation stays with its
        producer, including company-relative original paths in backup plans.
        """
        if self._closed:
            raise ContractError("invalid_operation", "文件任务服务已经关闭。")
        if type(kind) is not str or kind not in {"backup", "report_export"}:
            raise ContractError("invalid_input", "仅能查询备份或报表任务仍保留的输入计划。")
        metadata = self._storage.company_metadata(read_context)
        return tuple(
            self._plan(metadata, row)
            for row in read_context.repository("files").current(kind=kind)
            if _plans.retains_inputs(row)
        )

    def get(self, company_id, task_id, *, check_current=False):
        metadata = self._metadata(company_id)
        row = self._row(company_id, task_id, plan=check_current)
        if not check_current or row is None:
            return self._receipt(company_id, row)
        with locked(self._lock_path(metadata, row), 0):
            row = self._row(company_id, task_id, plan=True)
            if row is not None and row["state"] != "expired":
                plan = self._plan(metadata, row)
                context = self._context(metadata, task_id, row["capture_id"])
                if not _plans.is_current(self._producer(row["kind"]), plan, context):
                    row = self._expire(metadata, row, ExpireTask())
            return self._receipt(company_id, row)

    def ensure_current(self, plan, head, request_id):
        validate_contract("core.schema.json", "Digest", head)
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 500:
            raise ContractError("invalid_input", "文件请求标识必须是 1 至 500 字符的不透明字符串。")
        # Validate the outer wire before using it to select a company.
        plan = validate_contract("core.schema.json", "FilePlan", _copy(plan))
        metadata = self._prepare(plan["company_id"])
        plan = _plans.validate(metadata, plan)
        scope = self._describe(plan)
        if scope.head != head:
            raise ContractError("conflict", "请求头与文件计划不一致。")
        selector = {"kind": plan["kind"], "scope_key": scope.key}
        with locked(self._lock_path(metadata, selector), 0):
            with self._storage.read_unit(metadata.company_id) as read:
                repository = read.repository("files")
                creator = repository.creator(request_id)
                row = repository.scope(plan["kind"], scope.key)
            if creator is not None and (
                creator["kind"] != plan["kind"]
                or creator["scope_key"] != scope.key
                or creator["scope_head"] != head
            ):
                raise ContractError("conflict", "当前创建请求标识不能用于另一份文件计划。")
            if row is not None and row["scope_head"] == head:
                if row["state"] in {"pending", "running", "succeeded"} or (
                    row["state"] == "failed" and row["creator_request_id"] == request_id
                ):
                    existing = self._plan(metadata, row)
                    context = self._context(metadata, row["task_id"], row["capture_id"])
                    if not _plans.is_current(self._producer(row["kind"]), existing, context):
                        self._expire(metadata, row, ExpireTask())
                        raise ContractError("plan_expired", "原文件计划已失效，不能继续复用。")
                    if plan["kind"] == "backup":
                        self._discard_unaccepted_capture_locked(metadata, plan["capture_id"])
                    return self._receipt(metadata.company_id, row)
            task_id = str(uuid4())
            plan_json = canonical_json(plan)
            pending = {
                "task_id": task_id,
                "kind": plan["kind"],
                "state": "pending",
                "scope_head": head,
                "attempts": 0,
                "error_code": None,
            }
            live = self._live(metadata)
            handoff_from = None
            try:
                self._acquire_lease(metadata, task_id, plan)
                context = self._context(metadata, task_id, plan.get("capture_id"))
                if not _plans.is_current(self._producer(plan["kind"]), plan, context):
                    raise ContractError("plan_expired", "请求的文件计划已经失效。")
                shared_failed = (
                    row is not None
                    and row["state"] == "failed"
                    and row["scope_head"] == head
                    and row["capture_id"] is not None
                    and row["capture_id"] == plan.get("capture_id")
                )
                if shared_failed:
                    handoff_from = row["task_id"]
                    with locked(self._lock_path(metadata, row), 1, blocking=False) as acquired:
                        if not acquired:
                            raise ContractError("busy", "旧执行尚未退出，暂不能交接捕获输入。")
                        old_context = self._context(metadata, row["task_id"], row["capture_id"])
                        try:
                            self._producer(row["kind"]).cleanup(old_context, keep_capture=True)
                            _paths.remove_task_root(old_context.task_root)
                        except OSError as exc:
                            raise ContractError(
                                "file_cleanup_pending",
                                "旧任务输出尚未清理完，捕获输入和原失败任务已保留。",
                            ) from exc
                        guard = _plans.current_guard(self._producer(plan["kind"]), plan, context)
                        with self._storage.maintenance_unit(metadata.company_id) as uow:
                            repository = uow.repository("files")
                            if not guard():
                                raise ContractError(
                                    "plan_expired", "交接前业务依据已经变化，原失败任务已保留。"
                                )
                            if repository.delete(row) != 1:
                                raise ContractError("task_changed", "文件交接的原任务已经变化。")
                            repository.insert(task_id, plan, scope.key, request_id, plan_json)
                            receipt = self._receipt(metadata.company_id, pending)
                        live.release(row["task_id"])
                else:
                    if row is not None:
                        if row["state"] != "expired":
                            row = self._expire(metadata, row, ExpireTask())
                        if not self._cleanup_locked(metadata, row):
                            raise ContractError(
                                "file_cleanup_pending", "旧任务尚未清理完，请接续后重试。"
                            )
                    guard = _plans.current_guard(self._producer(plan["kind"]), plan, context)
                    with self._storage.maintenance_unit(metadata.company_id) as uow:
                        if not guard():
                            raise ContractError("plan_expired", "接纳文件任务前业务依据已经变化。")
                        uow.repository("files").insert(
                            task_id, plan, scope.key, request_id, plan_json
                        )
                        receipt = self._receipt(metadata.company_id, pending)
                return receipt
            except BaseException:
                # Commit may have succeeded before context exit or the caller
                # failed to receive its reply. Durable ownership then retains
                # the lease; uncertainty must not discard accepted inputs.
                try:
                    if self._row(metadata.company_id, task_id) is None:
                        live.release(task_id)
                    elif handoff_from is not None:
                        live.release(handoff_from)
                except BaseException:
                    pass
                raise

    def _output_path(self, context, kind, value):
        if not isinstance(value, Path):
            raise ContractError("unsafe_path", "生产者须返回明确的受控交付路径。")
        roots = [context.task_root]
        if kind == "backup":
            roots.append(context.company.backup_directory)
        for root in roots:
            try:
                parts = value.absolute().relative_to(root.resolve()).as_posix()
            except ValueError:
                continue
            return _paths.controlled(root, parts)
        raise ContractError("unsafe_path", "生产者交付位置越过任务或已配置备份目录。")

    def _verify(self, producer, plan, context, path):
        stream, digest, size = _paths.snapshot_file(path)
        try:
            producer.verify(_copy(plan), stream, context)
            stream.seek(0)
            return stream, digest, size
        except BaseException as exc:
            stream.close()
            if isinstance(exc, Exception) and not isinstance(exc, (ContractError, OSError)):
                raise ProductionError(
                    "file_invalid", "文件真实内容未通过生产者核验。", retryable=False
                ) from exc
            raise

    def update(self, task_id, expected_state, head, result, *, company_id):
        metadata = self._prepare(company_id)
        row = self._row(company_id, task_id, plan=True)
        if row is None:
            raise ContractError("not_found", "当前公司没有这个文件任务。")
        with locked(self._lock_path(metadata, row), 0):
            row = self._row(company_id, task_id, plan=True)
            if row is None or row["state"] != expected_state or row["scope_head"] != head:
                raise ContractError("task_changed", "任务状态或计划头已经变化。")
            if isinstance(result, ExpireTask):
                if row["state"] == "expired":
                    return self._receipt(company_id, row)
                return self._receipt(company_id, self._expire(metadata, row, result))
            if not isinstance(result, FileCompletion) or row["state"] != "running":
                raise ContractError("invalid_input", "只有领取中的文件任务可以确认执行结果。")
            claim = result.claim
            if (
                not isinstance(claim, RunClaim)
                or (claim.company_id, claim.task_id, claim.scope_head, claim.attempt)
                != (company_id, task_id, head, row["attempts"])
                or type(claim.attempt) is not int
            ):
                raise ContractError("task_changed", "旧执行代次不能确认当前文件任务。")
            if result.failure is not None:
                error = _failure(result.failure)
                state = "pending" if result.failure.retryable and row["attempts"] < 3 else "failed"
                return self._receipt(
                    company_id, self._change(company_id, row, state=state, error=error)
                )
            plan = self._plan(metadata, row)
            context = self._context(metadata, task_id, row["capture_id"])
            producer = self._producer(row["kind"])
            if not _plans.is_current(producer, plan, context):
                self._expire(metadata, row, ExpireTask())
                raise ContractError("plan_expired", "生成的文件计划已经失效，不能交付旧结果。")
            if not isinstance(result.artifact, FileArtifact):
                raise ContractError("invalid_input", "文件完成须使用受控候选文件。")
            candidate = _paths.controlled(context.task_root, result.artifact.relative_path)
            verified, digest, size = self._verify(producer, plan, context, candidate)
            try:
                artifact = VerifiedArtifact(
                    result.artifact.relative_path, verified, digest, size, claim
                )
                # No business transaction is held during validation/publication.
                path = self._output_path(
                    context, row["kind"], producer.publish(_copy(plan), artifact, context)
                )
                delivered, final_digest, final_size = _paths.snapshot_file(path)
                delivered.close()
                if (final_digest, final_size) != (digest, size):
                    raise ContractError("file_invalid", "发布文件与通过核验的候选字节不一致。")
            finally:
                verified.close()
            if not _plans.is_current(producer, plan, context):
                self._expire(metadata, row, ExpireTask())
                raise ContractError("plan_expired", "文件发布前计划已经失效。")
            # Scope locks order file requests, not business commits. Acquire
            # SQLite's writer lock before the final lightweight semantic guard,
            # so a business commit is unambiguously before or after success.
            guard = _plans.current_guard(producer, plan, context)
            with self._storage.maintenance_unit(company_id) as uow:
                repository = uow.repository("files")
                current_plan = guard()
                changes = (
                    {"state": "succeeded", "output": (str(path), digest, size)}
                    if current_plan
                    else {
                        "state": "expired",
                        "attempts": 0,
                        "error": _failure(ExpireTask()),
                        "expired": True,
                    }
                )
                if repository.replace(row, **changes) != 1:
                    raise ContractError("task_changed", "任务头或执行代次已经变化。")
                current = repository.get(task_id)
            if not current_plan:
                raise ContractError(
                    "plan_expired", "任务确认前业务依据已经变化，旧文件未成为有效交付。"
                )
            if current["kind"] == "backup":
                self._cleanup_capture(metadata, current)
            else:
                self._live(metadata).release(task_id)
            return self._receipt(company_id, current)

    def run(self, company_id, task_id):
        """Execute one automatic attempt; host schedules remaining pending attempts."""
        metadata = self._prepare(company_id)
        row = self._row(company_id, task_id, plan=True)
        if row is None:
            raise ContractError("not_found", "当前公司没有这个文件任务。")
        live = self._live(metadata)
        with locked(self._lock_path(metadata, row), 1, blocking=False) as acquired:
            if not acquired:
                return self.get(company_id, task_id)
            with self._lifecycle:
                if self._closed:
                    raise ContractError("invalid_operation", "文件任务服务已经关闭。")
                self._workers += 1
                with live.guard:
                    live.workers += 1
            try:
                with locked(self._lock_path(metadata, row), 0):
                    row = self._row(company_id, task_id, plan=True)
                    if row is None or row["state"] not in {"pending", "running"}:
                        return self._receipt(company_id, row)
                    if row["attempts"] == 3:
                        return self._receipt(
                            company_id,
                            self._change(
                                company_id,
                                row,
                                state="failed",
                                error=(
                                    "worker_interrupted",
                                    "第三次执行中断，需新的显式请求重新生成。",
                                ),
                            ),
                        )
                    plan = self._plan(metadata, row)
                    context = self._context(metadata, task_id, row["capture_id"])
                    if not _plans.is_current(self._producer(row["kind"]), plan, context):
                        return self._receipt(company_id, self._expire(metadata, row, ExpireTask()))
                    self._acquire_lease(metadata, task_id, plan)
                    row = self._change(
                        company_id, row, state="running", attempts=row["attempts"] + 1
                    )
                    claim = RunClaim(company_id, task_id, row["scope_head"], row["attempts"])
                try:
                    _paths.controlled(
                        metadata.company_root,
                        "runtime/file-tasks",
                        task_id,
                        "work",
                        directory=True,
                        create=True,
                    )
                    artifact = self._producer(row["kind"]).produce(_copy(plan), context)
                    completion = FileCompletion(claim, artifact=artifact)
                    return self.update(
                        task_id, "running", row["scope_head"], completion, company_id=company_id
                    )
                except Exception as exc:
                    if isinstance(exc, ContractError) and exc.code in {
                        "task_changed",
                        "plan_expired",
                        "not_found",
                    }:
                        return self.get(company_id, task_id)
                    failure = _as_failure(exc)
                    try:
                        return self.update(
                            task_id,
                            "running",
                            row["scope_head"],
                            FileCompletion(claim, failure=failure),
                            company_id=company_id,
                        )
                    except ContractError as changed:
                        if changed.code != "task_changed":
                            raise
                        return self.get(company_id, task_id)
            finally:
                with self._lifecycle:
                    self._workers -= 1
                    with live.guard:
                        live.workers -= 1

    def run_pending(self, company_id):
        self._prepare(company_id)
        with self._storage.read_unit(company_id) as read:
            ids = read.repository("files").executable()
        return tuple(self.run(company_id, task_id) for task_id in ids)

    def cleanup(self, company_id):
        """Try each obsolete cleanup once, after restoring all current input leases."""
        self.recover(company_id)

    @contextmanager
    def verified_download(self, company_id, task_id):
        metadata = self._prepare(company_id)
        row = self._row(company_id, task_id, plan=True)
        if row is None:
            raise ContractError("not_found", "当前公司没有这个文件任务。")
        stream = None
        try:
            with locked(self._lock_path(metadata, row), 0):
                row = self._row(company_id, task_id, plan=True)
                if row is None:
                    raise ContractError("not_found", "当前公司没有这个文件任务。")
                if row["state"] != "succeeded":
                    code = "plan_expired" if row["state"] == "expired" else "file_not_ready"
                    raise ContractError(code, "当前文件尚未成为可下载的有效交付。")
                plan = self._plan(metadata, row)
                producer = self._producer(row["kind"])
                context = self._context(metadata, task_id, row["capture_id"])
                if not _plans.is_current(producer, plan, context):
                    self._expire(metadata, row, ExpireTask())
                    raise ContractError("plan_expired", "文件计划已失效，不能下载旧结果。")
                path = self._output_path(context, row["kind"], Path(row["output_path"]))
                try:
                    stream, digest, size = _paths.snapshot_file(path)
                except (OSError, ContractError) as exc:
                    if _transient_download_error(exc):
                        raise ContractError(
                            "temporary_failure", "交付文件暂时无法读取，请稍后重试下载。"
                        ) from exc
                    self._change(
                        company_id,
                        row,
                        state="failed",
                        error=("file_invalid", "已生成文件缺失或内容核验失败，需显式重新生成。"),
                    )
                    raise ContractError("file_invalid", "交付文件缺失或内容核验失败。") from exc
                if (digest, size) != (row["output_sha256"], row["output_bytes"]):
                    self._change(
                        company_id,
                        row,
                        state="failed",
                        error=("file_invalid", "已生成文件字节与任务记录不一致，需显式重新生成。"),
                    )
                    raise ContractError("file_invalid", "交付文件字节与任务保存结果不一致。")
                if not _plans.is_current(producer, plan, context):
                    self._expire(metadata, row, ExpireTask())
                    raise ContractError("plan_expired", "文件计划已失效，不能下载旧结果。")
                name = self._describe(plan).file_name
            # The independent byte snapshot is the response snapshot. Network
            # sending holds neither a database transaction nor a scope lock.
            yield VerifiedDownload(stream, name, digest, size)
        finally:
            if stream is not None:
                stream.close()

    def close(self):
        with self._lifecycle:
            if self._closed:
                return
            if self._workers:
                raise ContractError("busy", "仍有文件生产者执行，请先结束后台执行再关闭。")
            for _, runtime in self._runtimes.values():
                _runtime.detach(runtime)
            self._runtimes.clear()
            self._closed = True
