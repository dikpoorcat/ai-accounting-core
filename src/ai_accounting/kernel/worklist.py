"""Company work list from registered business sources and one selected month.

Month selection uses small indexed source queries.  Readiness is evaluated only
after a month has been chosen; an empty company has no inferred accounting month.
"""

from __future__ import annotations

import json

from .business_queries import BusinessQueries
from .contracts import KernelError
from .diagnostics import job_error_message, public_job_code
from .periods import MATERIAL_CATEGORIES
from .types import ActualDate, YearMonth
from .workflow import Workflow

AREA_LABELS = {
    "bank": "银行",
    "payroll": "工资",
    "transactions": "普通业务",
    "tax": "税务",
    "assets": "资产",
    "financing": "融资",
}
OTHER_ACCOUNTING_AREAS = {
    "opening_bank": "bank",
    "opening_cash": "transactions",
    "opening_obligation": "transactions",
    "opening_asset": "assets",
    "opening_loan": "financing",
    "opening_tax": "tax",
    "opening_payroll_payable": "payroll",
    "opening_payroll_state": "payroll",
    "opening_equity": "transactions",
    "opening_money_fund": "transactions",
    "opening_package": "transactions",
    "opening_identity_binding": "transactions",
    "opening_basis_correction": "transactions",
    "report_classification": "transactions",
    "report_income_tax_confirmation": "tax",
}
POLICY_ONLY_KINDS = {"report_profile", "continuation_report_profile"}
MATERIAL_SOURCE_KINDS = {
    "material_source_v2",
    "material_resolution_v2",
    "material_period_allocation",
    "material_group_resolution",
}


class Worklist:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    def _candidate_months(self, connection):
        """Read existing month markers without running any period checker."""
        kinds = sorted(
            kind
            for kind, model in self.store.registry.models.items()
            if model.lane != "management" and kind not in POLICY_ONLY_KINDS
        )
        facts = (
            connection.execute(
                "SELECT DISTINCT r.period FROM fact_revision r INDEXED BY fact_period "
                "JOIN fact_current c ON c.fact_id=r.id JOIN subject s ON s.id=r.subject_id "
                "WHERE s.kind IN (SELECT value FROM json_each(?)) ORDER BY r.period",
                (json.dumps(kinds),),
            ).fetchall()
            if kinds
            else ()
        )
        candidates = {row[0] for row in facts}
        for statement in (
            "SELECT DISTINCT period FROM material_revision",
            "SELECT DISTINCT posting_period FROM calculation_publication",
            "SELECT DISTINCT r.start_period FROM job_reference r JOIN jobs j ON j.id=r.job_id "
            "WHERE r.start_period IS NOT NULL AND j.status IN ('pending','running','failed')",
            "SELECT DISTINCT f.end_period FROM fact_external_obligation f "
            "JOIN fact_current c ON c.fact_id=f.revision_id",
        ):
            if (
                "fact_external_obligation" in statement
                and "external_obligation" not in self.store.registry.models
            ):
                continue
            candidates.update(row[0] for row in connection.execute(statement))
        return sorted(candidates)

    @staticmethod
    def _order_obligations(items):
        def rank(item):
            actual = item["actual_completion_status"]
            review = item["basis_review_status"]
            if actual == "due":
                priority = 0
            elif actual == "completed" and review in {
                "difference_identified",
                "outdated",
                "not_reviewed",
                "unestablished",
            }:
                priority = 1
            elif actual == "pending" and item["due_date"] is not None:
                priority = 2
            elif actual == "pending":
                priority = 3
            else:
                priority = 4
            return priority, item["due_date"] or "9999-12-31", item["id"]

        return sorted(items, key=rank)

    def _select_month(self, connection, requested, as_of, inspection_cache):
        if requested is not None:
            return str(YearMonth(requested)), "explicit"
        latest_close = connection.execute("SELECT max(period) FROM period_close").fetchone()[0]
        cutoff = ActualDate(as_of).period.ordinal
        candidates = [month for month in self._candidate_months(connection) if month <= cutoff]
        open_candidates = [
            month for month in candidates if latest_close is None or month > latest_close
        ]
        if (
            latest_close is not None
            and latest_close < cutoff
            and (not open_candidates or open_candidates[0] > latest_close + 1)
            and self._has_closed_carry(connection, latest_close, inspection_cache)
        ):
            return str(YearMonth.from_ordinal(latest_close + 1)), "closed_issue_carry"
        if open_candidates:
            return str(YearMonth.from_ordinal(open_candidates[0])), "earliest_open_source"
        if latest_close is not None and latest_close <= cutoff:
            return str(YearMonth.from_ordinal(latest_close)), "latest_processed_background"
        if candidates:
            return str(YearMonth.from_ordinal(candidates[-1])), "latest_processed_background"
        return None, "empty"

    def _has_closed_carry(self, connection, latest_close, inspection_cache):
        from .materials import check_completeness_many

        kinds = sorted(
            kind
            for kind, model in self.store.registry.models.items()
            if model.lane != "management" and kind not in POLICY_ONLY_KINDS
        )
        pending = (
            (
                connection.execute(
                    "SELECT 1 FROM pending p JOIN fact_current c ON c.subject_id=p.subject_id "
                    "JOIN fact_revision r ON r.id=c.fact_id "
                    "JOIN subject s ON s.id=p.subject_id "
                    "WHERE r.period<=? AND s.kind IN (SELECT value FROM json_each(?)) LIMIT 1",
                    (latest_close, json.dumps(kinds)),
                ).fetchone()
                is not None
            )
            if kinds
            else False
        )
        if pending:
            return True
        first_open = latest_close + 1
        checked = check_completeness_many(
            connection,
            (first_open,),
            self.store.registry,
            closed_through=latest_close,
            _inspection_cache=inspection_cache,
        )[first_open]
        return bool(checked["issues"])

    @staticmethod
    def _material_area(connection, kind, fact_id):
        if kind == "material_source_v2":
            row = connection.execute(
                "SELECT category FROM fact_material_source_v2 WHERE revision_id=?", (fact_id,)
            ).fetchone()
        else:
            row = connection.execute(
                f"SELECT s.category FROM fact_{kind} r "
                "JOIN fact_material_source_v2 s ON s.revision_id=r.source_fact_id "
                "WHERE r.revision_id=?",
                (fact_id,),
            ).fetchone()
        return row[0] if row else None

    def _sources(self, connection, month):
        facts = []
        for row in connection.execute(
            "SELECT s.id subject_id,s.kind,r.id fact_id,"
            "CASE WHEN calc.fact_id=r.id THEN calc.id ELSE NULL END calculation_id,"
            "EXISTS(SELECT 1 FROM pending p WHERE p.subject_id=s.id) pending "
            "FROM fact_revision r INDEXED BY fact_period "
            "JOIN fact_current f ON f.fact_id=r.id JOIN subject s ON s.id=r.subject_id "
            "LEFT JOIN calculation_current c ON c.subject_id=s.id "
            "LEFT JOIN calculation calc ON calc.id=c.calculation_id WHERE r.period=?",
            (month,),
        ):
            model = self.store.registry.models[row["kind"]]
            area = model.material_category or OTHER_ACCOUNTING_AREAS.get(row["kind"])
            if row["kind"] in MATERIAL_SOURCE_KINDS:
                area = self._material_area(connection, row["kind"], row["fact_id"])
            if area not in MATERIAL_CATEGORIES or model.lane == "management":
                continue
            facts.append(
                {
                    "subject_id": row["subject_id"],
                    "kind": row["kind"],
                    "fact_id": row["fact_id"],
                    "calculation_id": row["calculation_id"],
                    "pending": bool(row["pending"]),
                    "work_area": area,
                    "lane": model.lane,
                }
            )
        inventory_rows = connection.execute(
            "SELECT m.category,m.id,m.expected,m.received,m.no_business FROM material_revision m "
            "WHERE m.period=? AND m.id=(SELECT max(n.id) FROM material_revision n "
            "WHERE n.period=m.period AND n.category=m.category)",
            (month,),
        )
        inventories = {row["category"]: dict(row) for row in inventory_rows}
        return facts, inventories

    @staticmethod
    def _issue_area(issue):
        area = issue.get("work_area")
        if area in AREA_LABELS:
            return area
        field = issue.get("field", "")
        if field.startswith("materials."):
            candidate = field.split(".", 1)[1]
            return candidate if candidate in AREA_LABELS else None
        if issue.get("domain") == "payroll":
            return "payroll"
        if field == "bank_reconciliation":
            return "bank"
        return None

    def _areas(self, facts, inventories, readiness):
        current = readiness["current_followups"] if readiness else None
        material_issues = current["materials"]["issues"] if current else []
        accounting_issues = current["accounting"]["issues"] if current else []
        close_issues = current["close_requirements"]["issues"] if current else []
        by_area = {area: [] for area in AREA_LABELS}
        for fact in facts:
            by_area[fact["work_area"]].append(fact)
        areas = []
        for area, label in AREA_LABELS.items():
            sources = by_area[area]
            accounting_sources = [item for item in sources if item["lane"] == "accounting"]
            material_sources = [item for item in sources if item["lane"] == "material"]
            inventory = inventories.get(area)
            area_material_issues = [i for i in material_issues if self._issue_area(i) == area]
            area_accounting_issues = [
                i
                for i in accounting_issues
                if self._issue_area(i) == area
                or i.get("field") in {f["subject_id"] for f in sources}
            ]
            area_close_issues = [i for i in close_issues if self._issue_area(i) == area]
            areas.append(
                {
                    "id": area,
                    "label": label,
                    "materials": {
                        "status": "needs_information"
                        if area_material_issues
                        else "ready"
                        if inventory
                        else "unestablished",
                        "inventory": inventory,
                        "source_count": len(material_sources),
                        "issues": area_material_issues,
                    },
                    "accounting": {
                        "status": "needs_information"
                        if area_accounting_issues
                        or area_close_issues
                        or any(
                            f["pending"] or f["calculation_id"] is None for f in accounting_sources
                        )
                        else "ready"
                        if accounting_sources
                        else "unestablished",
                        "fact_count": len(accounting_sources),
                        "calculation_count": sum(
                            f["calculation_id"] is not None for f in accounting_sources
                        ),
                        "pending_count": sum(f["pending"] for f in accounting_sources),
                        "issues": area_accounting_issues,
                    },
                    "close_issues": area_close_issues,
                    "sources": sources,
                }
            )
        return areas

    @staticmethod
    def _company_jobs(connection):
        from .read_indexes import verify_sources

        def object_record(raw, field, *, required=False):
            if raw is None:
                if required:
                    raise KernelError("content_integrity_failed", f"{field} 缺少任务记录")
                return None
            try:
                value = json.loads(raw)
            except (TypeError, ValueError, UnicodeDecodeError, RecursionError):
                raise KernelError("content_integrity_failed", f"{field} 内容格式无效") from None
            if not isinstance(value, dict):
                raise KernelError("content_integrity_failed", f"{field} 必须是对象")
            return value

        rows = list(
            connection.execute(
                "SELECT * FROM jobs "
                "WHERE status IN ('pending','running','failed') OR "
                "(kind='portable_backup' AND status='succeeded' AND rowid=("
                "SELECT max(rowid) FROM jobs WHERE kind='portable_backup' AND status='succeeded')) "
                "ORDER BY rowid"
            )
        )
        indexed = [
            row for row in rows if row["kind"] in {"payment_export", "tax_import", "report_export"}
        ]
        if indexed:
            verify_sources(connection, "job", indexed)
        reference_periods = {}
        if indexed:
            for ref in connection.execute(
                "SELECT r.job_id,min(r.start_period) period FROM job_reference r "
                "JOIN json_each(?) ids ON ids.value=r.job_id "
                "WHERE r.start_period IS NOT NULL GROUP BY r.job_id",
                (json.dumps([row["id"] for row in indexed]),),
            ):
                reference_periods[ref["job_id"]] = str(YearMonth.from_ordinal(ref["period"]))
        items = []
        for row in rows:
            error_code = public_job_code(row["error_code"]) if row["status"] == "failed" else None
            result = object_record(row["result"], "job.result")
            payload = object_record(row["payload"], "job.payload", required=True)
            plan = payload.get("plan")
            if plan is not None and not isinstance(plan, dict):
                raise KernelError("content_integrity_failed", "job.payload.plan 必须是对象")
            period = reference_periods.get(row["id"])
            if isinstance(plan, dict) and isinstance(plan.get("period"), (str, dict)):
                period = plan["period"]
            if row["kind"] == "portable_backup":
                close_period = payload.get("close_period")
                if close_period is not None:
                    try:
                        period = str(YearMonth(close_period))
                    except (TypeError, ValueError):
                        raise KernelError(
                            "content_integrity_failed", "job.payload.close_period 无效"
                        ) from None
            result_issue = None
            if row["status"] == "succeeded" and (
                not isinstance(result, dict)
                or not isinstance(result.get("path"), str)
                or not isinstance(result.get("sha256"), str)
            ):
                result_issue = {"field": "job.result", "message": "成功备份缺少已验证文件结果"}
            items.append(
                {
                    "job_id": row["id"],
                    "kind": row["kind"],
                    "status": row["status"],
                    "attempts": row["attempts"],
                    "error_code": error_code,
                    "error_message": job_error_message(error_code) if error_code else None,
                    "association": "period_scope" if period is not None else "company",
                    "period": period,
                    "references": [],
                    "result_issue": result_issue,
                    "contract_issues": [],
                    "current_file_availability": "not_checked",
                    "verified_when_succeeded": (
                        row["status"] == "succeeded" and result_issue is None
                    ),
                }
            )
        return items

    @staticmethod
    def _latest_file_jobs(connection, queries):
        items = []
        for row in connection.execute(
            "SELECT j.id,j.kind,j.attempts,min(r.start_period) period FROM jobs j "
            "LEFT JOIN job_reference r ON r.job_id=j.id "
            "WHERE j.status='succeeded' AND j.kind IN "
            "('payment_export','tax_import','report_export') "
            "AND j.rowid=(SELECT max(x.rowid) FROM jobs x "
            "WHERE x.kind=j.kind AND x.status='succeeded') GROUP BY j.id"
        ):
            if row["period"] is None:
                items.append(
                    {
                        "job_id": row["id"],
                        "kind": row["kind"],
                        "status": "succeeded",
                        "attempts": row["attempts"],
                        "error_code": None,
                        "error_message": None,
                        "association": "company",
                        "period": None,
                        "references": [],
                        "verified_when_succeeded": False,
                        "result_issue": {
                            "field": "job_reference",
                            "message": "成功文件缺少可核验的期间来源",
                        },
                        "contract_issues": [],
                        "current_file_availability": "not_checked",
                    }
                )
                continue
            period = str(YearMonth.from_ordinal(row["period"]))
            matches = queries._file_jobs(
                connection, None, period, job_ids=[row["id"]], include_result=False
            )
            if not matches:
                raise KernelError(
                    "content_integrity_failed", "成功文件任务缺少可核验的精确来源", job_id=row["id"]
                )
            items.extend(matches)
        return items

    @staticmethod
    def _normalize_jobs(items):
        for item in items:
            if item["status"] == "failed":
                code = public_job_code(item.get("error_code"))
                item["error_code"] = code
                item["error_message"] = job_error_message(code)
            else:
                item["error_code"] = None
                item["error_message"] = None
            item.pop("last_error", None)
            item.setdefault("result_issue", None)
            item.setdefault("contract_issues", [])
            item.setdefault("current_file_availability", "not_checked")
        return items

    def query(self, connection, *, as_of: str, period: str | None = None):
        from .materials import _CompletenessInspectionCache

        day = str(ActualDate(as_of))
        inspection_cache = _CompletenessInspectionCache(connection)
        selected, selection = self._select_month(connection, period, day, inspection_cache)
        queries = BusinessQueries(self.engine)
        if selected is None:
            obligations = Workflow(self.engine)._external_obligations(
                connection, str(ActualDate(day).period), day, reads=queries._reads(connection)
            )
            obligations = self._order_obligations(obligations)
            jobs = self._company_jobs(connection)
            seen_jobs = {item["job_id"] for item in jobs}
            jobs.extend(
                item
                for item in self._latest_file_jobs(connection, queries)
                if item["job_id"] not in seen_jobs
            )
            return {
                "schema_version": 1,
                "company_id": self.store.company_id,
                "database_id": self.store.database_id,
                "as_of": day,
                "as_of_semantics": "current_knowledge",
                "period": None,
                "period_selection": selection,
                "sections": {
                    "materials_and_accounting": self._areas([], {}, None),
                    "close": None,
                    "external": {"obligations": obligations, "settlements": None},
                    "files": {
                        "jobs": self._normalize_jobs(jobs),
                        "tax_import_mapping": None,
                    },
                },
                "fact_issues": [],
            }
        month = YearMonth(selected).ordinal
        readiness = queries._period_readiness(
            connection, selected, as_of=day, _inspection_cache=inspection_cache
        )
        facts, inventories = self._sources(connection, month)
        areas = self._areas(facts, inventories, readiness)
        # Real external work remains company-wide after an accounting month closes.
        external = Workflow(self.engine)._external_obligations(
            connection, selected, day, reads=queries._reads(connection)
        )
        external = self._order_obligations(external)
        file_jobs = queries._file_jobs(connection, None, selected, include_result=False)
        file_jobs = [
            item
            for item in file_jobs
            if item.get("status") in {"pending", "running", "failed"}
            or item.get("status") == "succeeded"
        ]
        seen_jobs = {item["job_id"] for item in file_jobs}
        for item in self._latest_file_jobs(connection, queries):
            if item["job_id"] not in seen_jobs:
                file_jobs.append(item)
                seen_jobs.add(item["job_id"])
        file_jobs.extend(
            item for item in self._company_jobs(connection) if item["job_id"] not in seen_jobs
        )
        self._normalize_jobs(file_jobs)
        followups = readiness["current_followups"]
        order_issues = [
            issue
            for issue in followups["external"]["fact_issues"]
            if issue.get("code") in {"already_closed", "earlier_period_open"}
        ]
        raw_close_issues = (
            followups["materials"]["issues"]
            + followups["accounting"]["issues"]
            + followups["close_requirements"]["issues"]
            + order_issues
        )
        close_issues = list(
            {
                json.dumps(issue, sort_keys=True, ensure_ascii=False): issue
                for issue in raw_close_issues
            }.values()
        )
        return {
            "schema_version": 1,
            "company_id": self.store.company_id,
            "database_id": self.store.database_id,
            "as_of": day,
            "as_of_semantics": "current_knowledge",
            "period": selected,
            "period_selection": selection,
            "sections": {
                "materials_and_accounting": areas,
                "close": {
                    "status": "closed"
                    if readiness["closure"]["state"] != "open"
                    else "needs_information"
                    if close_issues
                    else "ready",
                    "issues": close_issues,
                    "closure": readiness["closure"],
                },
                "external": {
                    "obligations": external,
                    "settlements": followups["settlements"],
                },
                "files": {
                    "jobs": file_jobs,
                    "tax_import_mapping": followups["tax_import_mapping"],
                },
            },
            "fact_issues": readiness["current_followups"]["external"]["fact_issues"],
        }
