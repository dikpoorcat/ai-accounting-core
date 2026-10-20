"""Only the files-owned current task table is accessible here."""

from __future__ import annotations

from ai_accounting_modular.contracts.public import ContractError

_SUMMARY = (
    "task_id,kind,state,scope_key,scope_head,creator_request_id,attempts,capture_id,"
    "output_path,output_sha256,output_bytes,error_code,error_message"
)


class FilesRepository:
    def __init__(self, session):
        self._session = session

    def get(self, task_id, *, plan=False):
        columns = "*" if plan else _SUMMARY
        row = self._session.fetchone(f"SELECT {columns} FROM file_task WHERE task_id=?", (task_id,))
        return dict(row) if row is not None else None

    def scope(self, kind, key, *, plan=True):
        columns = "*" if plan else _SUMMARY
        row = self._session.fetchone(
            f"SELECT {columns} FROM file_task WHERE kind=? AND scope_key=?", (kind, key)
        )
        return dict(row) if row is not None else None

    def creator(self, request_id):
        return self._session.fetchone(
            f"SELECT {_SUMMARY} FROM file_task WHERE creator_request_id=? LIMIT 1", (request_id,)
        )

    def current(self):
        return tuple(
            dict(row)
            for row in self._session.fetchall("SELECT * FROM file_task ORDER BY kind,scope_key")
        )

    def executable(self):
        return tuple(
            row["task_id"]
            for row in self._session.fetchall(
                "SELECT task_id FROM file_task WHERE state IN ('pending','running') "
                "ORDER BY kind,scope_key"
            )
        )

    def insert(self, task_id, plan, scope_key, request_id, plan_json):
        # Different scope locks do not serialize the same creator key. This
        # check runs inside SQLite's company write transaction as well.
        if self.creator(request_id) is not None:
            raise ContractError("conflict", "当前创建请求标识已经用于另一文件任务。")
        self._session.execute(
            "INSERT INTO file_task(task_id,kind,state,scope_key,scope_head,creator_request_id,"
            "attempts,capture_id,plan_schema,plan_json) VALUES(?,?,'pending',?,?,?,0,?,?,?)",
            (
                task_id,
                plan["kind"],
                scope_key,
                plan["scope_head"],
                request_id,
                plan.get("capture_id"),
                "BackupPlan" if plan["kind"] == "backup" else "ReportExportPlan",
                plan_json,
            ),
        )

    def replace(self, row, *, state, attempts=None, output=None, error=None, expired=False):
        return self._session.execute(
            "UPDATE file_task SET state=?,attempts=?,output_path=?,output_sha256=?,"
            "output_bytes=?,error_code=?,error_message=?,"
            "plan_schema=CASE WHEN ? THEN NULL ELSE plan_schema END,"
            "plan_json=CASE WHEN ? THEN NULL ELSE plan_json END "
            "WHERE task_id=? AND scope_head=? AND state=? AND attempts=?",
            (
                state,
                row["attempts"] if attempts is None else attempts,
                *(output or (None, None, None)),
                *(error or (None, None)),
                expired,
                expired,
                row["task_id"],
                row["scope_head"],
                row["state"],
                row["attempts"],
            ),
        )

    def delete(self, row):
        return self._session.execute(
            "DELETE FROM file_task WHERE task_id=? AND scope_head=? AND state=? AND attempts=?",
            (row["task_id"], row["scope_head"], row["state"], row["attempts"]),
        )
