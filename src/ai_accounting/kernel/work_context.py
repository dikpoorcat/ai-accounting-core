"""Bounded existing sources for one explicitly selected accounting work area.

This is discovery in one owned read snapshot, never a readiness or missing-fact
decision. Material receipt months remain distinct from recognition scopes.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Annotated, Literal

from pydantic import Field

from .contracts import KernelError
from .discovery import Discovery
from .entities import profiles_for_entities
from .entity_references import references_from_data, verify_hits
from .materials import PageLimit
from .query_reads import QueryReads
from .read_state import repair_revision
from .types import SubjectId, YearMonth, canonical, digest
from .worklist import AREA_LABELS, MATERIAL_SOURCE_KINDS, OTHER_ACCOUNTING_AREAS

WorkArea = Literal["bank", "payroll", "transactions", "tax", "assets", "financing"]
ContextIds = Annotated[list[SubjectId], Field(max_length=500)]

# These are the existing payroll preparation and tax-client export inputs.
# They remain management facts; discovery does not change their business lane.
MANAGEMENT_SOURCE_AREAS = {
    "payroll_plan_v2": "payroll",
    "payroll_plan_bounded": "payroll",
    "payroll_change_notice_v2": "payroll",
    "payroll_no_change_v2": "payroll",
    "tax_import_identity_v2": "payroll",
    "tax_import_details_v2": "payroll",
    "tax_import_mapping_v2": "payroll",
    "payroll_tax_declaration_actual": "payroll",
    "payroll_disbursement_basis": "payroll",
}
NON_MONTHLY_RECORDING_KINDS = {
    "tax_import_identity_v2",
    "payroll_tax_declaration_actual",
    "payroll_disbursement_basis",
}


class WorkContext:
    def __init__(self, engine, *, work_drafts=None):
        self.engine, self.store = engine, engine.store
        self.discovery = Discovery(engine)
        self.work_drafts = work_drafts

    @staticmethod
    def _ids(values, label):
        if values is None:
            return []
        if (
            not isinstance(values, (list, tuple))
            or len(values) > 500
            or any(not isinstance(value, str) or not value or len(value) > 200 for value in values)
        ):
            raise ValueError(label + " must contain at most 500 explicit identifiers")
        return sorted(set(values))

    @staticmethod
    def _cursor(cursor, scope):
        if cursor is None:
            return None
        if not isinstance(cursor, str) or len(cursor) > 2048:
            raise KernelError("work_context_cursor_invalid", "工作资料分页游标无效")
        try:
            value = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
            if set(value) != {"version", "scope", "period", "fact_id"}:
                raise ValueError("shape")
            if (
                type(value["version"]) is not int
                or value["version"] != 2 or value["scope"] != scope
            ):
                raise KernelError(
                    "work_context_cursor_stale", "公司、事项、筛选或版本已变化，请重新读取首页"
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
            raise KernelError("work_context_cursor_invalid", "工作资料分页游标无效") from exc

    @staticmethod
    def _seal_cursor(scope, row):
        return base64.urlsafe_b64encode(
            canonical(
                {"version": 2, "scope": scope, "period": row["period"], "fact_id": row["fact_id"]}
            ).encode()
        ).decode()

    def _headers_sql(self):
        replaced = self.discovery._superseded_subject("d")
        return (
            "SELECT d.fact_id,d.subject_id,d.revision,d.kind,d.period,"
            "EXISTS(SELECT 1 FROM discovery_fact_current live "
            "WHERE live.fact_id=d.fact_id) AND NOT " + replaced + " is_current,"
            "(EXISTS(SELECT 1 FROM discovery_fact_current live "
            "WHERE live.subject_id=d.subject_id AND live.fact_id<>d.fact_id) OR "
            + replaced
            + ") superseded,"
            "EXISTS(SELECT 1 FROM pending p WHERE p.subject_id=d.subject_id) pending,"
            "(SELECT c.id FROM calculation_current cc JOIN calculation c "
            "ON c.id=cc.calculation_id WHERE cc.subject_id=d.subject_id "
            "AND c.fact_id=d.fact_id) calculation_id FROM discovery_fact_history d "
        )

    def _candidate_sql(self, period, area, subjects, sources):
        kinds = sorted(
            kind
            for kind, model in self.store.registry.models.items()
            if (
                model.material_category
                or OTHER_ACCOUNTING_AREAS.get(kind)
                or MANAGEMENT_SOURCE_AREAS.get(kind)
            )
            == area
            and kind not in MATERIAL_SOURCE_KINDS
            and kind not in NON_MONTHLY_RECORDING_KINDS
        )
        # Candidate SQL reads scalar indexes only. Exact bodies are hydrated
        # after LIMIT, including source references added by this page.
        monthly = (
            "WITH monthly AS (SELECT d.fact_id FROM discovery_fact_current d "
            "WHERE d.period=? AND d.kind IN(SELECT value FROM json_each(?)) "
            "UNION SELECT d.fact_id FROM json_each(?) ids "
            "JOIN discovery_fact_current d ON d.subject_id=ids.value"
        )
        parameters = [period, canonical(kinds), canonical(subjects)]
        if area == "payroll":
            # Actual declarations and payment bases explicitly name the wage
            # tax period. Their recording month is not the requested wage month.
            monthly += (
                " UNION SELECT d.fact_id FROM fact_payroll_tax_declaration_actual s "
                "JOIN discovery_fact_current d ON d.fact_id=s.revision_id WHERE s.tax_period=?"
                " UNION SELECT d.fact_id FROM fact_payroll_disbursement_basis s "
                "JOIN discovery_fact_current d ON d.fact_id=s.revision_id WHERE s.tax_period=?"
            )
            parameters.extend((period, period))
        monthly += "), business AS (SELECT fact_id FROM monthly"
        if area == "payroll":
            # Current identity lookup has object scope, no invented validity
            # period. Scalar reference indexes select it before the shared page
            # LIMIT; no employee profiles or fact bodies are preloaded.
            monthly += (
                " UNION SELECT identity.fact_id FROM monthly m "
                "JOIN entity_reference_current employee ON employee.fact_id=m.fact_id "
                "AND employee.role='employee' JOIN entity_reference_current identity "
                "ON identity.entity_id=employee.entity_id AND identity.role='employee' "
                "AND identity.kind='tax_import_identity_v2' "
                "JOIN discovery_fact_current d ON d.fact_id=identity.fact_id"
            )
        sql = monthly + (
            "), "
            "scoped AS (SELECT fs.fact_id FROM fact_scope fs "
            "JOIN fact_current c ON c.fact_id=fs.fact_id "
            "WHERE fs.scope_key IN(?,?) AND fs.kind IN "
            "('material_period_allocation','material_resolution_v2','material_group_resolution')), "
            "source_ids(source_id) AS ("
            "SELECT value FROM json_each(?) UNION "
            "SELECT d.subject_id FROM discovery_fact_current d "
            "JOIN fact_material_source_v2 s ON s.revision_id=d.fact_id "
            "WHERE d.period=? AND s.category=? UNION "
            "SELECT d.subject_id FROM business b JOIN fact_evidence e ON e.fact_id=b.fact_id "
            "JOIN fact_scope fs ON fs.kind='material_source_v2' "
            "AND fs.scope_key='material-evidence:'||lower(hex(e.evidence_digest)) "
            "JOIN discovery_fact_current d ON d.fact_id=fs.fact_id UNION "
            "SELECT s.source_id FROM scoped x JOIN fact_material_period_allocation s "
            "ON s.revision_id=x.fact_id UNION "
            "SELECT s.source_id FROM scoped x JOIN fact_material_resolution_v2 s "
            "ON s.revision_id=x.fact_id UNION "
            "SELECT s.source_id FROM scoped x JOIN fact_material_group_resolution s "
            "ON s.revision_id=x.fact_id), "
            "sources AS (SELECT d.fact_id,d.subject_id FROM source_ids ids "
            "JOIN discovery_fact_current d ON d.subject_id=ids.source_id "
            "JOIN fact_material_source_v2 s ON s.revision_id=d.fact_id WHERE "
            "s.category=? OR d.subject_id IN(SELECT value FROM json_each(?))), "
            "candidates(fact_id) AS (SELECT fact_id FROM business "
            "UNION SELECT fact_id FROM sources "
            "UNION SELECT fs.fact_id FROM sources s JOIN fact_scope fs "
            "ON fs.scope_key='material-source:'||s.subject_id JOIN fact_current c "
            "ON c.fact_id=fs.fact_id WHERE fs.kind IN "
            "('material_period_allocation','material_resolution_v2','material_group_resolution')) "
        )
        return sql, parameters + [
            "material-period:" + str(YearMonth.from_ordinal(period)),
            "material-period-unknown",
            canonical(sources),
            period,
            area,
            area,
            canonical(sources),
        ]

    def _identity_matches(self, connection, records, *, fact_data, fact_digests):
        matches = {record["fact_id"]: [] for record in records}
        if records:
            verified = verify_hits(
                connection, records, registry=self.store.registry,
                fact_data=fact_data, fact_digests=fact_digests,
            )
            for fact_id, path, entity_id, role, *_ in verified:
                matches[fact_id].append(
                    {
                        "entity_id": entity_id,
                        "role": role,
                        "path": path,
                        "identity_match": "current",
                    }
                )
        return matches

    def _material_sources(self, connection, items):
        """Read exact originals referenced by this page, never every received file."""
        exact_ids = {item["fact_id"] for item in items if item["kind"] == "material_source_v2"}
        for item in items:
            if item["kind"] in MATERIAL_SOURCE_KINDS - {"material_source_v2"}:
                exact_ids.add(item["data"]["source_fact_id"])
        if not exact_ids:
            return []
        headers = list(
            connection.execute(
                self._headers_sql() + "JOIN json_each(?) ids ON d.fact_id=ids.value "
                "WHERE d.kind='material_source_v2' ORDER BY d.period DESC,d.fact_id DESC",
                (canonical(sorted(exact_ids)),),
            )
        )
        if len(headers) != len(exact_ids):
            raise KernelError("content_integrity_failed", "资料引用缺少原件登记来源")
        existing = {item["fact_id"]: item for item in items}
        missing = [row for row in headers if row["fact_id"] not in existing]
        existing.update(
            (item["fact_id"], item)
            for item in self.discovery._hydrate_fact_records(
                connection, missing, current_index=False
            )
        )
        evidence_ids = sorted(
            {existing[row["fact_id"]]["data"]["evidence_digest"] for row in headers}
        )
        metadata = {}
        for start in range(0, len(evidence_ids), 500):
            batch = evidence_ids[start : start + 500]
            metadata.update(
                (row["digest"], dict(row))
                for row in connection.execute(
                    "SELECT lower(hex(e.digest)) digest,e.name,e.media_type,"
                    "length(e.content) byte_size "
                    "FROM evidence e WHERE e.digest IN(" + ",".join("?" for _ in batch) + ")",
                    tuple(bytes.fromhex(value) for value in batch),
                )
            )
        result = []
        for row in headers:
            source = existing[row["fact_id"]]
            reference = source["data"]["evidence_digest"]
            if reference not in metadata:
                raise KernelError("content_integrity_failed", "资料登记缺少原件索引")
            related = [
                {"subject_id": item["subject_id"], "fact_id": item["fact_id"], "kind": item["kind"]}
                for item in items
                if item["kind"] in MATERIAL_SOURCE_KINDS - {"material_source_v2"}
                and item["data"]["source_fact_id"] == source["fact_id"]
            ]
            result.append(
                {
                    "source": dict(source),
                    "original": metadata[reference],
                    "received_period": source["period"],
                    "page_related_fact_refs": related,
                    "period_semantics": "接收月不是核算所属期；归期和处置以关联类型化事实为准",
                }
            )
        return result

    def query(
        self,
        *,
        period: str,
        work_area: WorkArea,
        subject_ids: ContextIds | None = None,
        source_ids: ContextIds | None = None,
        limit: PageLimit = 100,
        cursor: str | None = None,
        include_work_draft: bool = False,
    ):
        month = YearMonth(period)
        if work_area not in AREA_LABELS:
            raise ValueError("unknown work area")
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("work context page limit must be 1..500")
        if type(include_work_draft) is not bool:
            raise ValueError("include_work_draft must be a boolean")
        if include_work_draft and cursor is not None:
            raise KernelError(
                "work_context_draft_first_page_only", "工作稿只能随首页读取；续页请去掉工作稿选项"
            )
        subjects, sources = (
            self._ids(subject_ids, "subject_ids"),
            self._ids(source_ids, "source_ids"),
        )
        draft = None
        if include_work_draft:
            if self.work_drafts is None:
                raise KernelError("work_draft_unavailable", "当前读取未绑定工作稿存储")
            # The private file is authenticated separately, before the kernel
            # snapshot. Returning them together does not make the file part of
            # the SQLite transaction or turn draft notes into business state.
            draft = self.work_drafts.read(period=str(month), work_area=work_area)
        with QueryReads.snapshot(self.engine) as reads:
            connection = reads.connection
            context = self.discovery._company_context(connection)
            revision = repair_revision(connection)
            scope = digest(
                {
                    "contract": "work-context/2",
                    "company_id": self.store.company_id,
                    "database_id": self.store.database_id,
                    "period": str(month),
                    "work_area": work_area,
                    "subject_ids": subjects,
                    "source_ids": sources,
                    "epochs": context["epochs"],
                    "read_repair_revision": revision,
                }
            ).hex()
            position = self._cursor(cursor, scope)
            prefix, parameters = self._candidate_sql(month.ordinal, work_area, subjects, sources)
            predicate = "NOT " + self.discovery._superseded_subject("d")
            if position is not None:
                predicate += " AND (d.period<? OR(d.period=? AND d.fact_id<?))"
                parameters.extend((position[0], position[0], position[1]))
            records = list(
                connection.execute(
                    prefix
                    + self._headers_sql()
                    + "JOIN candidates x ON x.fact_id=d.fact_id WHERE "
                    + predicate
                    + " ORDER BY d.period DESC,d.fact_id DESC LIMIT ?",
                    (*parameters, limit + 1),
                )
            )
            selected = records[:limit]
            # Reuse only this snapshot's raw JSON and freshly computed digests.
            # Both discovery seals and object-reference indexes still compare
            # their own source metadata against these exact selected facts.
            fact_data = self.store.fact_data_many(
                connection, [record["fact_id"] for record in selected]
            )
            fact_digests = {fact_id: digest(data) for fact_id, data in fact_data.items()}
            items = self.discovery._hydrate_fact_records(
                connection,
                selected,
                current_index=True,
                identity_matches=self._identity_matches(
                    connection, selected, fact_data=fact_data, fact_digests=fact_digests,
                ),
                fact_data=fact_data,
                fact_digests=fact_digests,
            )
            material_sources = self._material_sources(connection, items)
            calculation_ids = sorted(
                {item["calculation_id"] for item in items if item["calculation_id"] is not None}
            )
            dependencies = {
                ident: {"fact_ids": [], "calculation_ids": []} for ident in calculation_ids
            }
            for row in connection.execute(
                "SELECT f.calculation_id,f.fact_id FROM json_each(?) ids "
                "JOIN dependency_fact f ON f.calculation_id=ids.value "
                "ORDER BY f.calculation_id,f.fact_id",
                (canonical(calculation_ids),),
            ):
                dependencies[row["calculation_id"]]["fact_ids"].append(row["fact_id"])
            for row in connection.execute(
                "SELECT c.calculation_id,c.upstream_id FROM json_each(?) ids "
                "JOIN dependency_calculation c ON c.calculation_id=ids.value "
                "ORDER BY c.calculation_id,c.upstream_id",
                (canonical(calculation_ids),),
            ):
                dependencies[row["calculation_id"]]["calculation_ids"].append(row["upstream_id"])
            entity_ids = {
                match["entity_id"] for item in items for match in item["identity_matches"]
            }
            object_profiles = profiles_for_entities(connection, entity_ids)
            entities = [
                {
                    "entity_id": ident,
                    "kind": profile["entity_kind"],
                    "account_type": profile["account_type"],
                    "profile": profile,
                }
                for ident, profile in object_profiles.items()
            ]
            # Exact dependency identities remain references. Their historical
            # bodies are retrieved only when the current task actually needs them.
            for item in items:
                item["calculation_dependencies"] = dependencies.get(item["calculation_id"])
                item["business_references"] = [
                    {"path": ref["path"], "role": ref["role"], "value": ref["entity_id"]}
                    for ref in references_from_data(
                        item["kind"], item["data"], registry=self.store.registry
                    )
                    if ref["reference_type"] == "business"
                ]
            has_more = len(records) > limit
            result = {
                "schema_version": 2,
                "company_id": self.store.company_id,
                "period": str(month),
                "work_area": work_area,
                "company_context": context,
                "read_repair_revision": revision,
                "sort": "period_desc_fact_id_desc",
                "items": items,
                "materials": material_sources,
                "entities": entities,
                "has_more": has_more,
                "next_cursor": self._seal_cursor(scope, selected[-1]) if has_more else None,
                "page_semantics": "这是有界已有资料页，不是完整性或可发布结论；"
                "本页没有不等于资料缺失；跨月及未知归期只展示已有作用域和明确来源，"
                "不推断发生月；摘要关联来源和解析映射可能在续页；"
                "关联历史正文按精确引用继续读取",
            }
            if include_work_draft:
                result["work_draft"] = draft
                result["page_semantics"] += "；工作稿文件与内核快照分别核验，不属于同一事务"
            return result
