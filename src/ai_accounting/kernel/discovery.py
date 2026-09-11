"""Company-owned context and discoverable typed sources across local sessions."""

from __future__ import annotations

import hashlib
import uuid
from typing import Literal

from .contracts import KernelError, NeedsInformation
from .schema import table_name
from .types import YearMonth, digest

DISCOVERY_DDL = """
CREATE TABLE company_note_revision(id TEXT PRIMARY KEY,
 revision INTEGER NOT NULL UNIQUE CHECK(revision>0), text TEXT NOT NULL,
 digest BLOB NOT NULL CHECK(length(digest)=32),
 evidence_digest BLOB REFERENCES evidence(digest)) STRICT;
"""


class Discovery:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    @staticmethod
    def _note(connection):
        row = connection.execute(
            "SELECT revision,text,digest,evidence_digest FROM company_note_revision "
            "ORDER BY revision DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return {"revision": 0, "text": "", "digest": None, "evidence_digest": None}
        return {
            **dict(row),
            "digest": row["digest"].hex(),
            "evidence_digest": row["evidence_digest"].hex() if row["evidence_digest"] else None,
        }

    def company_context(self):
        with self.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            identity = dict(connection.execute("SELECT * FROM identity WHERE id=1").fetchone())
            return {
                "identity": identity,
                "company_note": self._note(connection),
                "epochs": self.store.epochs(connection),
                "source_kinds": sorted(self.store.registry.models),
                "note_semantics": "公司业务说明是管理背景，不能代替已确认事实或推断缺少的核算信息",
            }

    def update_company_note(
        self,
        text: str,
        *,
        expected_revision: int,
        request_id: str,
        evidence_digest: str | None = None,
    ):
        if not isinstance(text, str) or len(text) > 50000:
            raise ValueError("company note must be text of at most 50000 characters")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision must be a nonnegative integer")
        evidence = bytes.fromhex(evidence_digest) if evidence_digest is not None else None
        if evidence is not None and len(evidence) != 32:
            raise ValueError("evidence digest must be 32 bytes")
        content_digest = hashlib.sha256(text.encode("utf-8")).digest()

        def operation(connection):
            current = self._note(connection)
            if current["revision"] != expected_revision:
                raise KernelError(
                    "company_note_conflict",
                    "公司说明已由另一会话更新，请合并后重试",
                    current=current,
                )
            if (
                evidence is not None
                and connection.execute(
                    "SELECT 1 FROM evidence WHERE digest=?", (evidence,)
                ).fetchone()
                is None
            ):
                raise NeedsInformation("evidence_digest", "说明引用的依据尚未登记")
            connection.execute(
                "INSERT INTO company_note_revision VALUES(?,?,?,?,?)",
                (uuid.uuid4().hex, expected_revision + 1, text, content_digest, evidence),
            )
            return {"status": "saved", **self._note(connection)}

        return self.engine._write(
            request_id,
            digest(["company_note", text, expected_revision, evidence_digest]),
            None,
            ("management",),
            "company_note",
            operation,
        )

    def find_facts(
        self,
        *,
        kind: str | None = None,
        person_id: str | None = None,
        period_from: str | None = None,
        period_to: str | None = None,
        status: Literal["current", "pending", "published", "history", "deleted"] = "current",
        after_id: str | None = None,
        limit: int = 100,
    ):
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("source page limit must be 1..500")
        if kind is not None and kind not in self.store.registry.models:
            raise KernelError("unknown_fact_kind", "不存在该类型化来源")
        if status not in {"current", "pending", "published", "history", "deleted"}:
            raise ValueError("unknown source status")
        start = YearMonth(period_from).ordinal if period_from is not None else 0
        end = YearMonth(period_to).ordinal if period_to is not None else 119987
        if start > end:
            raise ValueError("period_from must not follow period_to")
        where = ["f.period BETWEEN ? AND ?"]
        parameters = [start, end]
        if kind is not None:
            where.append("s.kind=?")
            parameters.append(kind)
        if after_id is not None:
            where.append("f.id>?")
            parameters.append(after_id)
        if status == "deleted":
            where.append(
                "a.fact_id IS NULL AND NOT EXISTS(SELECT 1 FROM fact_current "
                "live WHERE live.subject_id=f.subject_id)"
            )
        elif status != "history":
            where.append("a.fact_id IS NOT NULL")
        if status == "pending":
            where.append("EXISTS(SELECT 1 FROM pending p WHERE p.subject_id=f.subject_id)")
        elif status == "published":
            where.append(
                "c.fact_id=f.id AND NOT EXISTS(SELECT 1 FROM pending p "
                "WHERE p.subject_id=f.subject_id)"
            )
        if person_id is not None:
            clauses = []
            for name, model in self.store.registry.models.items():
                if kind is not None and name != kind:
                    continue
                fields = [
                    field for field in ("employee_id", "person_id") if field in model.model_fields
                ]
                if fields:
                    clauses.append(
                        f"EXISTS(SELECT 1 FROM {table_name(name)} person WHERE "
                        "person.revision_id=f.id AND ("
                        + " OR ".join(f'person."{field}"=?' for field in fields)
                        + "))"
                    )
                    parameters.extend(person_id for _ in fields)
            where.append("(" + " OR ".join(clauses) + ")" if clauses else "0")
        sql = (
            """SELECT f.id,f.subject_id,f.revision,f.period,s.kind,
            a.fact_id IS NOT NULL is_current,c.id calculation_id,c.fact_id calculated_fact_id,
            EXISTS(SELECT 1 FROM pending p WHERE p.subject_id=f.subject_id) pending
            FROM fact_revision f JOIN subject s ON s.id=f.subject_id
            LEFT JOIN fact_current a ON a.fact_id=f.id
            LEFT JOIN calculation_current cc ON cc.subject_id=f.subject_id
            LEFT JOIN calculation c ON c.id=cc.calculation_id
            WHERE """
            + " AND ".join(where)
            + " ORDER BY f.id LIMIT ?"
        )
        with self.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            records = connection.execute(sql, (*parameters, limit + 1)).fetchall()
            result = []
            for record in records[:limit]:
                version = self.store.fact(connection, record["id"])
                result.append(
                    {
                        "fact_id": version.id,
                        "subject_id": version.subject_id,
                        "revision": version.revision,
                        "kind": version.fact.kind,
                        "period": str(version.fact.period),
                        "data": version.fact.model_dump(mode="json"),
                        "evidence": list(version.evidence),
                        "is_current": bool(record["is_current"]),
                        "pending": bool(record["pending"]),
                        "calculation_id": record["calculation_id"],
                        "published_current": bool(
                            record["calculated_fact_id"] == version.id and not record["pending"]
                        ),
                    }
                )
            return {
                "company_id": self.store.company_id,
                "items": result,
                "next_after_id": result[-1]["fact_id"] if len(records) > limit else None,
            }
