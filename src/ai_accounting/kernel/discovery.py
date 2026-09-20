"""Company-owned context and discoverable typed sources across local sessions."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import uuid
from typing import Literal

from .contracts import KernelError, NeedsInformation
from .publication import verify_record as verify_publication_record
from .read_state import repair_revision
from .types import YearMonth, canonical, digest
from .versions import database_format

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
            identity = dict(
                connection.execute(
                    "SELECT company_id,taxpayer_id,database_id FROM identity WHERE id=1"
                ).fetchone()
            )
            return {
                "identity": identity,
                "database_format": database_format(connection, bundle=self.store.bundle),
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

    def _fact_cursor_scope(self, connection, filters):
        return digest(
            {
                "contract": "find-facts/2",
                "company_id": self.store.company_id,
                "database_id": self.store.database_id,
                "filters": filters,
                "epochs": self.store.epochs(connection),
                "read_repair_revision": repair_revision(connection),
            }
        ).hex()

    @staticmethod
    def _fact_cursor(cursor, scope):
        if cursor is None:
            return None
        if not isinstance(cursor, str) or len(cursor) > 2048:
            raise KernelError("fact_cursor_invalid", "事实分页游标无效")
        try:
            value = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
            if set(value) != {"version", "scope", "period", "fact_id"}:
                raise ValueError("shape")
            if value["version"] != 2 or value["scope"] != scope:
                raise KernelError(
                    "fact_cursor_stale", "公司、筛选条件或事实版本已变化，请重新读取首页"
                )
            if (
                type(value["period"]) is not int
                or not 0 <= value["period"] <= 119987
                or not isinstance(value["fact_id"], str)
                or not value["fact_id"]
            ):
                raise ValueError("position")
            return value["period"], value["fact_id"]
        except KernelError:
            raise
        except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error) as exc:
            raise KernelError("fact_cursor_invalid", "事实分页游标无效") from exc

    @staticmethod
    def _seal_fact_cursor(scope, record):
        return base64.urlsafe_b64encode(
            canonical(
                {
                    "version": 2,
                    "scope": scope,
                    "period": record["period"],
                    "fact_id": record["fact_id"],
                }
            ).encode()
        ).decode()

    @staticmethod
    def _adoptions(connection, fact_ids):
        if not fact_ids:
            return {}
        rows = connection.execute(
            "WITH ids AS (SELECT value fact_id FROM json_each(?)), adopted AS ("
            "SELECT c.fact_id discovered_fact_id,NULL member_calculation_id,"
            "CASE WHEN cc.calculation_id=c.id THEN 1 ELSE 0 END current,p.* "
            "FROM ids JOIN calculation c ON c.fact_id=ids.fact_id "
            "JOIN calculation_publication p ON p.calculation_id=c.id "
            "LEFT JOIN calculation_current cc ON cc.calculation_id=c.id UNION ALL "
            "SELECT m.member_fact_id discovered_fact_id,m.member_calculation_id,"
            "CASE WHEN mc.calculation_id=m.member_calculation_id "
            "AND oc.calculation_id=m.owner_calculation_id THEN 1 ELSE 0 END current,p.* "
            "FROM ids JOIN asset_batch_member m ON m.member_fact_id=ids.fact_id "
            "JOIN calculation_publication p ON p.calculation_id=m.owner_calculation_id "
            "LEFT JOIN calculation_current mc ON mc.calculation_id=m.member_calculation_id "
            "LEFT JOIN calculation_current oc ON oc.calculation_id=m.owner_calculation_id) "
            "SELECT * FROM adopted ORDER BY discovered_fact_id,sequence DESC",
            (canonical(sorted(fact_ids)),),
        )
        result = {}
        for raw in rows:
            row = dict(raw)
            verify_publication_record(row)
            fact_id = row["discovered_fact_id"]
            if fact_id in result:
                continue
            member = row["member_calculation_id"]
            result[fact_id] = {
                "basis": "asset_batch_member" if member else "direct_publication",
                "publication_id": row["id"],
                "calculation_id": member or row["calculation_id"],
                "posting_period": str(YearMonth.from_ordinal(row["posting_period"])),
                "mode": row["mode"],
                "owner_calculation_id": row["calculation_id"] if member else None,
                "current": bool(row["current"]),
            }
        return result

    def _superseded_subject(self, alias):
        """SQL predicate for a source whose latest explicit identity action replaced it."""
        predicates = [
            "EXISTS(SELECT 1 FROM identity_correction_item i "
            f"WHERE i.subject_id={alias}.subject_id AND i.action='supersede' "
            "AND i.rowid=(SELECT max(j.rowid) FROM identity_correction_item j "
            "WHERE j.subject_id=i.subject_id))"
        ]
        if "opening_identity_binding" in self.store.registry.models:
            predicates.append(
                "EXISTS(SELECT 1 FROM calculation_current bh "
                "JOIN calculation bc ON bc.id=bh.calculation_id "
                "JOIN fact_opening_identity_binding binding ON binding.revision_id=bc.fact_id "
                f"WHERE bh.subject_id='opening-identity:'||{alias}.subject_id "
                "AND binding.operation='supersede')"
            )
        return "(" + " OR ".join(predicates) + ")"

    @staticmethod
    def _verify_fact_hits(connection, records, data, *, current_index):
        """Verify only hydrated hits against their immutable source and seal."""
        identifiers = [row["fact_id"] for row in records]
        if not identifiers:
            return
        sources = {
            row["id"]: row
            for row in connection.execute(
                "SELECT f.*,s.kind,EXISTS(SELECT 1 FROM fact_seal z "
                "WHERE z.fact_id=f.id) sealed,EXISTS(SELECT 1 FROM fact_current c "
                "WHERE c.subject_id=f.subject_id AND c.fact_id=f.id) stored_current "
                "FROM json_each(?) ids JOIN fact_revision f ON f.id=ids.value "
                "JOIN subject s ON s.id=f.subject_id",
                (canonical(sorted(identifiers)),),
            )
        }
        for record in records:
            fact_id = record["fact_id"]
            source = sources.get(fact_id)
            reason = None
            if source is None:
                reason = "missing_source_fact"
            elif not source["sealed"]:
                reason = "fact_seal_missing"
            elif (
                source["subject_id"],
                source["revision"],
                source["kind"],
                source["period"],
            ) != (
                record["subject_id"],
                record["revision"],
                record["kind"],
                record["period"],
            ):
                reason = "discovery_source_mismatch"
            elif current_index and not source["stored_current"]:
                reason = "discovery_current_mismatch"
            elif digest(data[fact_id]) != source["digest"]:
                reason = "fact_digest_mismatch"
            if reason is not None:
                raise KernelError(
                    "content_integrity_failed",
                    "事实发现命中项与不可变来源不一致",
                    component="fact_discovery",
                    record_id=fact_id,
                    reason=reason,
                )

    def find_facts(
        self,
        *,
        kind: str | None = None,
        entity_id: str | None = None,
        role: str | None = None,
        identity_match: Literal["current", "recorded"] = "current",
        period_from: str | None = None,
        period_to: str | None = None,
        status: Literal[
            "current", "pending", "published", "superseded", "history", "deleted"
        ] = "current",
        cursor: str | None = None,
        limit: int = 100,
    ):
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("source page limit must be 1..500")
        if kind is not None and kind not in self.store.registry.models:
            raise KernelError("unknown_fact_kind", "不存在该类型化来源")
        if status not in {
            "current",
            "pending",
            "published",
            "superseded",
            "history",
            "deleted",
        }:
            raise ValueError("unknown source status")
        if identity_match not in {"current", "recorded"}:
            raise ValueError("unknown identity match")
        start = YearMonth(period_from).ordinal if period_from is not None else None
        end = YearMonth(period_to).ordinal if period_to is not None else None
        if start is not None and end is not None and start > end:
            raise ValueError("period_from must not follow period_to")
        with self.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            from .entity_references import validate_filter

            validate_filter(connection, entity_id, role, identity_match)
            filters = {
                "kind": kind,
                "entity_id": entity_id,
                "role": role,
                "identity_match": identity_match,
                "period_from": period_from,
                "period_to": period_to,
                "status": status,
            }
            scope = self._fact_cursor_scope(connection, filters)
            position = self._fact_cursor(cursor, scope)
            history = status in {"history", "superseded", "deleted"}
            table = "discovery_fact_history" if history else "discovery_fact_current"
            where, parameters = [], []
            reference_table = None
            if entity_id is not None or role is not None:
                reference_table = (
                    "entity_reference_current"
                    if identity_match == "current"
                    else "entity_reference_recorded"
                )
                if entity_id is not None:
                    where.append("r.entity_id=?")
                    parameters.append(entity_id)
                if role is not None:
                    where.append("r.role=?")
                    parameters.append(role)
                duplicate = ["duplicate.fact_id=r.fact_id", "duplicate.path<r.path"]
                if entity_id is not None:
                    duplicate.append("duplicate.entity_id=?")
                    parameters.append(entity_id)
                if role is not None:
                    duplicate.append("duplicate.role=?")
                    parameters.append(role)
                where.append(
                    "NOT EXISTS(SELECT 1 FROM "
                    + reference_table
                    + " duplicate WHERE "
                    + " AND ".join(duplicate)
                    + ")"
                )
            filter_alias = "r" if reference_table else "d"
            superseded_subject = self._superseded_subject("d")
            if kind is not None:
                where.append(filter_alias + ".kind=?")
                parameters.append(kind)
            if start is not None:
                where.append(filter_alias + ".period>=?")
                parameters.append(start)
            if end is not None:
                where.append(filter_alias + ".period<=?")
                parameters.append(end)
            if position is not None:
                where.append(
                    "("
                    + filter_alias
                    + ".period<? OR("
                    + filter_alias
                    + ".period=? AND "
                    + filter_alias
                    + ".fact_id<?))"
                )
                parameters.extend((position[0], position[0], position[1]))
            if status == "pending":
                where.append("NOT " + superseded_subject)
                where.append("EXISTS(SELECT 1 FROM pending p WHERE p.subject_id=d.subject_id)")
            elif status == "published":
                where.extend(
                    (
                        "NOT " + superseded_subject,
                        "NOT EXISTS(SELECT 1 FROM pending p WHERE p.subject_id=d.subject_id)",
                        "EXISTS(SELECT 1 FROM calculation_current cc "
                        "JOIN calculation c ON c.id=cc.calculation_id AND c.fact_id=d.fact_id "
                        "WHERE cc.subject_id=d.subject_id AND ("
                        "EXISTS(SELECT 1 FROM calculation_publication p "
                        "WHERE p.calculation_id=c.id) OR EXISTS(SELECT 1 "
                        "FROM asset_batch_member m JOIN calculation_current oc "
                        "ON oc.calculation_id=m.owner_calculation_id "
                        "JOIN calculation_publication p "
                        "ON p.calculation_id=m.owner_calculation_id "
                        "WHERE m.member_calculation_id=c.id)))",
                    )
                )
            elif status == "superseded":
                where.append(
                    "(EXISTS(SELECT 1 FROM discovery_fact_current live "
                    "WHERE live.subject_id=d.subject_id AND live.fact_id<>d.fact_id) OR "
                    + superseded_subject
                    + ")"
                )
            elif status == "deleted":
                where.append(
                    "NOT EXISTS(SELECT 1 FROM discovery_fact_current live "
                    "WHERE live.subject_id=d.subject_id) AND NOT " + superseded_subject
                )
            elif status == "current":
                where.append("NOT " + superseded_subject)
            if reference_table:
                prefix = "entity_current" if identity_match == "current" else "entity_recorded"
                if entity_id is not None and role is not None and kind is not None:
                    reference_index = prefix + "_entity_role_kind"
                elif entity_id is not None and role is not None:
                    reference_index = prefix + "_entity_role"
                else:
                    reference_index = prefix + ("_lookup" if entity_id is not None else "_role")
                source = (
                    reference_table
                    + " AS r INDEXED BY "
                    + reference_index
                    + " JOIN "
                    + table
                    + " d ON d.fact_id=r.fact_id"
                )
            else:
                source = table + " d"
            revision = "d.revision"
            if not history:
                source += " JOIN discovery_fact_history historical ON historical.fact_id=d.fact_id"
                revision = "historical.revision"
            sql = (
                "SELECT " + "d.fact_id,d.subject_id," + revision + " revision,d.kind,d.period,"
                "EXISTS(SELECT 1 FROM discovery_fact_current live "
                "WHERE live.fact_id=d.fact_id) AND NOT " + superseded_subject + " is_current,"
                "(EXISTS(SELECT 1 FROM discovery_fact_current live "
                "WHERE live.subject_id=d.subject_id AND live.fact_id<>d.fact_id) OR "
                + superseded_subject
                + ") superseded,"
                "EXISTS(SELECT 1 FROM pending p WHERE p.subject_id=d.subject_id) pending,"
                "(SELECT c.id FROM calculation_current cc JOIN calculation c "
                "ON c.id=cc.calculation_id WHERE cc.subject_id=d.subject_id "
                "AND c.fact_id=d.fact_id) calculation_id FROM "
                + source
                + (" WHERE " + " AND ".join(where) if where else "")
                + " ORDER BY "
                + filter_alias
                + ".period DESC,"
                + filter_alias
                + ".fact_id DESC LIMIT ?"
            )
            records = connection.execute(sql, (*parameters, limit + 1)).fetchall()
            selected = records[:limit]
            fact_ids = [record["fact_id"] for record in selected]
            identity_matches = {fact_id: [] for fact_id in fact_ids}
            if reference_table:
                from .entity_references import verify_hits

                reference_where = []
                reference_parameters = [canonical(sorted(fact_ids))]
                if entity_id is not None:
                    reference_where.append("r.entity_id=?")
                    reference_parameters.append(entity_id)
                if role is not None:
                    reference_where.append("r.role=?")
                    reference_parameters.append(role)
                reference_rows = list(
                    connection.execute(
                        "SELECT r.* FROM json_each(?) ids JOIN "
                        + reference_table
                        + " r ON r.fact_id=ids.value"
                        + (" WHERE " + " AND ".join(reference_where) if reference_where else "")
                        + " ORDER BY r.fact_id,r.path",
                        tuple(reference_parameters),
                    )
                )
                verify_hits(
                    connection,
                    reference_rows,
                    identity_match=identity_match,
                    registry=self.store.registry,
                )
                for match in reference_rows:
                    identity_matches[match["fact_id"]].append(
                        {
                            "entity_id": match["entity_id"],
                            "role": match["role"],
                            "path": match["path"],
                            "identity_match": identity_match,
                        }
                    )
            data = self.store.fact_data_many(connection, fact_ids)
            self._verify_fact_hits(connection, selected, data, current_index=not history)
            evidence = {fact_id: [] for fact_id in fact_ids}
            for row in connection.execute(
                "SELECT e.fact_id,e.evidence_digest FROM json_each(?) ids "
                "JOIN fact_evidence e ON e.fact_id=ids.value "
                "ORDER BY e.fact_id,e.evidence_digest",
                (canonical(sorted(fact_ids)),),
            ):
                evidence[row["fact_id"]].append(row["evidence_digest"].hex())
            adoptions = self._adoptions(connection, fact_ids)
            result = []
            for record in selected:
                fact_id = record["fact_id"]
                result.append(
                    {
                        "fact_id": fact_id,
                        "subject_id": record["subject_id"],
                        "revision": record["revision"],
                        "kind": record["kind"],
                        "period": str(YearMonth.from_ordinal(record["period"])),
                        "data": data[fact_id],
                        "evidence": evidence[fact_id],
                        "is_current": bool(record["is_current"]),
                        "superseded": bool(record["superseded"]),
                        "pending": bool(record["pending"]),
                        "calculation_id": record["calculation_id"],
                        "adoption": adoptions.get(fact_id),
                        "identity_matches": identity_matches[fact_id],
                    }
                )
            return {
                "schema_version": 2,
                "company_id": self.store.company_id,
                "sort": "period_desc_fact_id_desc",
                "items": result,
                "next_cursor": (
                    self._seal_fact_cursor(scope, selected[-1]) if len(records) > limit else None
                ),
            }
