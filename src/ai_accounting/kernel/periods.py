"""Materials, management metadata, period freezing and exports share kernel transactions."""

from __future__ import annotations

import json
import uuid

from .contracts import Calculation, Context, FactVersion, KernelError, NeedsInformation
from .types import YearMonth, canonical, digest

MATERIAL_CATEGORIES = ("transactions", "payroll", "bank", "tax", "assets", "financing")
READINESS_WORK_AREAS = {
    "bank_accounts": "bank",
    "platform_movements": "transactions",
    "payroll_presence": "payroll",
}
ASSET_FINANCING_READINESS_FIELDS = {"loan_drawdown", "loan_interest"}
_CURRENT_CLOSE = object()


class Periods:
    def __init__(self, engine, *, authorize_close=None):
        self.engine, self.store = engine, engine.store
        self.authorize_close = authorize_close

    def management(
        self,
        subject_id: str,
        *,
        note: str | None,
        payment_period: str | None,
        payment_category: str | None,
        expected_revision: int,
        request_id: str,
    ):
        month = YearMonth(payment_period).ordinal if payment_period is not None else None
        data = [subject_id, note, month, payment_category, expected_revision]

        def operation(connection):
            current = connection.execute(
                "SELECT coalesce(max(revision),0) FROM management_revision WHERE subject_id=?",
                (subject_id,),
            ).fetchone()[0]
            if current != expected_revision:
                raise KernelError("management_conflict", "管理资料已变化")
            connection.execute(
                "INSERT INTO "
                "management_revision(subject_id,revision,note,payment_pe"
                "riod,payment_category) "
                "VALUES(?,?,?,?,?)",
                (subject_id, current + 1, note, month, payment_category),
            )
            return {"status": "saved", "revision": current + 1}

        return self.engine._write(
            request_id, digest(["management", data]), None, ("management",), "management", operation
        )

    def inventory(
        self,
        period: str,
        category: str,
        *,
        evidence: list[str],
        expected: int,
        no_business: bool,
        confirmation_evidence: str,
        request_id: str,
    ):
        month = YearMonth(period).ordinal
        if category not in MATERIAL_CATEGORIES or type(expected) is not int or expected < 0:
            raise ValueError("invalid material category or expected count")
        if type(no_business) is not bool or (no_business and (expected or evidence)):
            raise ValueError("no-business confirmation conflicts with received materials")
        if not no_business and expected == 0 and not evidence:
            raise NeedsInformation("no_business", "没有资料不能自动证明没有业务")
        items = sorted(set(evidence))
        payload = [period, category, items, expected, no_business, confirmation_evidence]

        def operation(connection):
            # An inventory revision cannot hide evidence that has already been received.
            previous = {
                r[0].hex()
                for r in connection.execute(
                    "SELECT i.evidence_digest FROM material_item i JOIN "
                    "material_revision m ON m.id=i.inventory_id "
                    "WHERE m.period=? AND m.category=?",
                    (month, category),
                )
            }
            if not previous <= set(items):
                raise KernelError("received_material_omitted", "不能从清单撤去已接收资料")
            row = connection.execute(
                "INSERT INTO "
                "material_revision(period,category,expected,received,no_business,evidence_digest) "
                "VALUES(?,?,?,?,?,?) RETURNING id",
                (
                    month,
                    category,
                    expected,
                    len(items),
                    int(no_business),
                    bytes.fromhex(confirmation_evidence),
                ),
            ).fetchone()
            connection.executemany(
                "INSERT INTO material_item VALUES(?,?)",
                [(row[0], bytes.fromhex(item)) for item in items],
            )
            return {"status": "saved", "inventory_id": row[0]}

        return self.engine._write(
            request_id, digest(["inventory", payload]), None, ("material",), "inventory", operation
        )

    @staticmethod
    def _has_business_activity(connection, month, kind, model):
        """Read compact, current publication summaries, never repeated fact rows."""
        count_field = model.activity_count_field
        if count_field is None and model.business_activity:
            return True  # The caller has already found this kind's current facts.
        condition = ""
        parameters = [month, kind]
        if count_field is not None:
            from .stored_json import verify_sql_outcomes

            verify_sql_outcomes(
                connection,
                (
                    row[0]
                    for row in connection.execute(
                        "SELECT c.id FROM fact_revision r INDEXED BY fact_period "
                        "CROSS JOIN fact_current f CROSS JOIN subject s "
                        "JOIN calculation_current a ON a.subject_id=s.id "
                        "JOIN calculation c ON c.id=a.calculation_id "
                        "WHERE r.period=? AND f.fact_id=r.id AND s.id=f.subject_id "
                        "AND s.kind=?",
                        (month, kind),
                    )
                ),
            )
            # Missing, malformed or nonzero counts never establish no activity.
            # The path is bound as data and is declared by the domain, not a caller.
            condition = (
                " OR json_type(c.outcome,?) IS NOT 'integer' OR json_extract(c.outcome,?)<>0"
            )
            parameters.extend(["$.values." + count_field] * 2)
        return (
            connection.execute(
                "SELECT 1 FROM fact_revision r INDEXED BY fact_period "
                "CROSS JOIN fact_current f CROSS JOIN subject s "
                "LEFT JOIN calculation_current a ON a.subject_id=s.id "
                "LEFT JOIN calculation c ON c.id=a.calculation_id "
                "WHERE r.period=? AND f.fact_id=r.id AND s.id=f.subject_id AND s.kind=? "
                "AND (c.fact_id IS NOT f.fact_id OR EXISTS(SELECT 1 FROM pending p "
                "WHERE p.subject_id=s.id)" + condition + ") LIMIT 1",
                parameters,
            ).fetchone()
            is not None
        )

    @staticmethod
    def completeness(connection, month, registry, *, material_coverage=None):
        inventories = {
            row["category"]: row
            for row in connection.execute(
                "SELECT m.* FROM material_revision m WHERE m.period=? AND m.id=(SELECT max(n.id) "
                "FROM material_revision n WHERE n.period=m.period AND n.category=m.category)",
                (month,),
            )
        }
        issues = []
        for category in MATERIAL_CATEGORIES:
            row = inventories.get(category)
            if row is None:
                issues.append(
                    {"field": f"materials.{category}", "message": "需要资料覆盖或无业务确认"}
                )
                continue
            if row["expected"] > row["received"]:
                issues.append({"field": f"materials.{category}", "message": "预期资料尚未全部收到"})
        from .materials import MaterialReadSummary, check_completeness

        if material_coverage is None:
            material_coverage = check_completeness(connection, month, registry)
        issues.extend(
            material_coverage.issues
            if isinstance(material_coverage, MaterialReadSummary)
            else material_coverage["issues"]
        )
        # Facts awaiting initial publication are just as incomplete as stale results.
        unpublished = connection.execute(
            "SELECT s.id,s.kind FROM subject s JOIN fact_current f "
            "ON f.subject_id=s.id JOIN fact_revision r "
            "ON r.id=f.fact_id LEFT JOIN calculation_current c ON c.subject_id=s.id "
            # Check publication first. A free-standing disposition predicate
            # can be pushed before the LEFT JOIN and scan every published
            # asset's history even though it cannot be an unpublished fact.
            "WHERE r.period=? AND CASE WHEN c.subject_id IS NOT NULL THEN 0 "
            "WHEN s.kind='asset_consumption' THEN NOT EXISTS("
            "SELECT 1 FROM disposition d WHERE d.subject_id=s.id "
            "AND d.cause_id=f.fact_id AND d.action='asset_derived_removed') ELSE 1 END",
            (month,),
        ).fetchall()
        for row in connection.execute(
            "SELECT DISTINCT s.kind FROM subject s JOIN fact_current c ON c.subject_id=s.id "
            "JOIN fact_revision f ON f.id=c.fact_id WHERE f.period=?",
            (month,),
        ):
            model = registry.models[row[0]]
            inventory = inventories.get(model.material_category)
            if (
                row[0] in registry.evaluators
                and inventory
                and inventory["no_business"]
                and Periods._has_business_activity(connection, month, row[0], model)
            ):
                issues.append(
                    {
                        "field": f"materials.{model.material_category}",
                        "message": "无业务确认与已确认业务冲突",
                    }
                )
        return inventories, issues, unpublished

    def _manifest(
        self,
        connection,
        period: str,
        owner_confirmation: str,
        *,
        previous_close=_CURRENT_CLOSE,
        _inspection_cache=None,
    ):
        from .display import Display

        month = YearMonth(period).ordinal
        if not connection.execute(
            "SELECT 1 FROM evidence WHERE digest=?", (bytes.fromhex(owner_confirmation),)
        ).fetchone():
            raise NeedsInformation("owner_confirmation", "需要负责人不可变确认依据")
        from .integrity import verify_close_integrity

        verify_close_integrity(self.engine, connection, month)
        checked = self.check_readiness(
            connection,
            period,
            previous_close,
            _inspection_cache=_inspection_cache,
            _reuse_closed_materials=True,
        )
        if checked["order_failure"]:
            failure = checked["order_failure"]
            raise KernelError(failure["code"], failure["message"], **failure["details"])
        if checked["issues"]:
            raise KernelError("period_not_ready", "关账条件尚未满足", fact_issues=checked["issues"])
        previous_close = checked["previous_close"]
        inventories = checked["materials"]["inventories"]
        material_coverage = checked["materials"]["coverage"]
        readiness = checked["close_requirements"]["readiness"]
        vouchers = [
            dict(r)
            for r in connection.execute(
                "SELECT v.id,v.voucher_id,v.calculation_id,v.total,s.number,v.reverses_id,"
                "r.subject_id FROM voucher_current c "
                "JOIN voucher_version v ON v.id=c.version_id JOIN voucher s ON s.id=v.voucher_id "
                "JOIN calculation r ON r.id=v.calculation_id "
                "WHERE v.period=? ORDER BY s.number",
                (month,),
            )
        ]
        from .asset_batches import frozen_members
        from .close_contract import CLOSE_FORMAT, CLOSE_FORMAT_VERSION, require_close_contract
        from .read_state import repair_revision

        adopted_results = []
        asset_adoptions = []
        for row in connection.execute(
            "SELECT c.*,p.id publication_id,p.posting_period FROM calculation_current h "
            "JOIN calculation c ON c.id=h.calculation_id "
            "JOIN calculation_publication p ON p.calculation_id=c.id "
            "WHERE p.posting_period=? ORDER BY c.subject_id",
            (month,),
        ):
            outcome = json.loads(row["outcome"])
            role = (
                "asset_batch_owner"
                if row["kind"] in {"asset_activation_batch", "asset_consumption_month"}
                else "opening_basis"
                if outcome["opening"]
                else "journal_basis"
                if outcome["lines"]
                else "state_only"
            )
            adopted_results.append(
                {
                    "publication_id": row["publication_id"],
                    "calculation_id": row["id"],
                    "result_digest": row["digest"].hex(),
                    "subject_id": row["subject_id"],
                    "fact_id": row["fact_id"],
                    "source_period": str(YearMonth.from_ordinal(row["period"])),
                    "posting_period": period,
                    "role": role,
                }
            )
            if role == "asset_batch_owner":
                frozen_members(connection, row["id"])
                asset_adoptions.append(
                    {
                        "owner_calculation_id": row["id"],
                        "membership_digest": outcome["values"]["membership_digest"],
                    }
                )
        by_subject = {item["subject_id"]: item for item in adopted_results}
        for voucher in vouchers:
            adopted = by_subject.get(voucher.pop("subject_id"))
            if adopted is None:
                raise KernelError(
                    "content_integrity_failed",
                    "凭证缺少本期直接采用的核算依据",
                    component="close",
                    reason="voucher_adoption_missing",
                )
            voucher["adopted_calculation_id"] = adopted["calculation_id"]
            voucher["result_digest"] = adopted["result_digest"]
        calculations = {item["calculation_id"] for item in adopted_results}
        calculations.update(row["calculation_id"] for row in vouchers)
        from .asset_card_adoption import build_asset_card_adoptions
        from .query_reads import QueryReads

        asset_card_adoptions = build_asset_card_adoptions(
            QueryReads(self.engine, connection),
            close_period=month,
            calculation_ids=calculations,
            voucher_calculation_ids={row["adopted_calculation_id"] for row in vouchers}
            | {row["calculation_id"] for row in vouchers},
        )
        trial_balance = [
            dict(r)
            for r in connection.execute(
                "SELECT account,sum(debit) debit,sum(credit) credit "
                "FROM (SELECT * FROM monthly_account WHERE period<=? UNION ALL "
                "SELECT * FROM opening_account WHERE period<=?) "
                "GROUP BY account ORDER BY account",
                (month, month),
            )
        ]
        manifest = {
            "format": CLOSE_FORMAT,
            "format_version": CLOSE_FORMAT_VERSION,
            "period": period,
            "company_id": self.store.company_id,
            "previous_close_period": str(YearMonth.from_ordinal(previous_close["period"]))
            if previous_close
            else None,
            "previous_close_digest": previous_close["digest"].hex() if previous_close else None,
            "database_id": self.store.database_id,
            "vouchers": vouchers,
            "adopted_results": adopted_results,
            "publication_sequence": connection.execute(
                "SELECT coalesce(max(sequence),0) FROM calculation_publication"
            ).fetchone()[0],
            "opening_calculation_id": next(
                (
                    item["calculation_id"]
                    for item in adopted_results
                    if item["role"] == "opening_basis"
                ),
                None,
            ),
            "read_version": {
                **self.store.epochs(connection),
                "read_repair_revision": repair_revision(connection),
            },
            "approval": None,
            "asset_batch_adoptions": asset_adoptions,
            "asset_card_adoptions": asset_card_adoptions,
            "inventories": {key: row["id"] for key, row in sorted(inventories.items())},
            "owner_confirmation": owner_confirmation,
            "readiness": readiness,
            "management_snapshot": Display.snapshot(
                connection, period, registry=self.store.registry
            ),
            "material_coverage": {
                key: value for key, value in material_coverage.items() if key != "issues"
            },
            "trial_balance": trial_balance,
            "report_classification": {
                "1": "assets",
                "2": "liabilities",
                "3": "equity",
                "4": "costs",
                "5": "income_and_expenses",
            },
        }

        from .close_review import build_owner_review

        # The owner summary is constructed immediately in this same transaction.
        # Reuse the full checks above; it must not repeat material parsing and
        # every readiness evaluator just to obtain the same follow-up counts.
        manifest["owner_review"] = build_owner_review(
            connection, self.engine, manifest, _checked_open=checked
        )

        return require_close_contract(manifest)

    def check_readiness(
        self,
        connection,
        period: str,
        previous_close=_CURRENT_CLOSE,
        *,
        _inspection_cache=None,
        _allow_frozen_materials=False,
        _reuse_closed_materials=False,
        _query_reads=None,
        _parallel_checks=None,
    ):
        """Collect the exact close checks without requiring owner authorization."""

        month = YearMonth(period).ordinal
        if previous_close is _CURRENT_CLOSE:
            previous_close = connection.execute(
                "SELECT period,digest FROM period_close ORDER BY period DESC LIMIT 1"
            ).fetchone()
        cutoff = previous_close["period"] if previous_close else -1
        if cutoff >= month:
            return {
                "period": period,
                "previous_close": previous_close,
                "order_failure": {
                    "code": "already_closed",
                    "message": "月份已经关账",
                    "details": {},
                },
                "materials": None,
                "accounting": None,
                "close_requirements": None,
                "issues": [],
            }
        kinds = sorted(
            kind
            for kind in self.store.registry.evaluators
            if self.store.registry.models[kind].lane != "management"
        )
        if kinds:
            earlier = connection.execute(
                "SELECT f.period FROM fact_revision f INDEXED BY fact_period "
                "CROSS JOIN fact_current c CROSS JOIN subject s "
                "WHERE f.period>? AND f.period<? AND c.fact_id=f.id AND s.id=f.subject_id "
                f"AND s.kind IN({','.join('?' for _ in kinds)}) ORDER BY f.period LIMIT 1",
                (cutoff, month, *kinds),
            ).fetchone()
            if earlier:
                return {
                    "period": period,
                    "previous_close": previous_close,
                    "order_failure": {
                        "code": "earlier_period_open",
                        "message": "须先处理并关闭前面有业务的月份",
                        "details": {"period": str(YearMonth.from_ordinal(earlier[0]))},
                    },
                    "materials": None,
                    "accounting": None,
                    "close_requirements": None,
                    "issues": [],
                }
        collected = self.collect_current_readiness(
            connection,
            period,
            closed_through=previous_close["period"] if previous_close else None,
            _inspection_cache=_inspection_cache,
            _allow_frozen_materials=_allow_frozen_materials,
            _reuse_closed_materials=_reuse_closed_materials,
            _query_reads=_query_reads,
            _parallel_checks=_parallel_checks,
        )
        return {
            "period": period,
            "previous_close": previous_close,
            "order_failure": None,
            "materials": collected["materials"],
            "accounting": collected["accounting"],
            "close_requirements": collected["close_requirements"],
            "issues": collected["issues"],
        }

    def collect_current_readiness(
        self,
        connection,
        period: str,
        *,
        closed_through=_CURRENT_CLOSE,
        _inspection_cache=None,
        _allow_frozen_materials=False,
        _reuse_closed_materials=False,
        _query_reads=None,
        _parallel_checks=None,
    ):
        """Collect current issues without interpreting a historical close boundary."""

        month = YearMonth(period).ordinal
        kinds = sorted(
            kind
            for kind in self.store.registry.evaluators
            if self.store.registry.models[kind].lane != "management"
        )
        from .materials import (
            _CompletenessInspectionCache,
            check_completeness,
            read_completeness_summary,
        )

        if _inspection_cache is None:
            _inspection_cache = _CompletenessInspectionCache(connection)
        if _parallel_checks is not None and (
            not _allow_frozen_materials
            or _query_reads is None
            or _query_reads.connection is not connection
            or not _query_reads._snapshot_active
        ):
            raise ValueError("parallel readiness belongs to an active summary snapshot")

        checker = read_completeness_summary if _allow_frozen_materials else check_completeness
        material_options = {
            "_inspection_cache": _inspection_cache,
            "_query_reads": _query_reads,
        }
        if _reuse_closed_materials and not _allow_frozen_materials:
            material_options["_allow_frozen_reuse"] = True
        material_coverage = _parallel_checks.material() if _parallel_checks is not None else (
            checker(
                connection,
                month,
                self.store.registry,
                **material_options,
            )
            if closed_through is _CURRENT_CLOSE
            else checker(
                connection,
                month,
                self.store.registry,
                closed_through=closed_through,
                **material_options,
            )
        )
        inventories, material_issues, unpublished = self.completeness(
            connection, month, self.store.registry, material_coverage=material_coverage
        )
        issues = list(material_issues)
        readiness = {}
        readiness_issues = []
        checks = [
            (name, tuple(required_reads(YearMonth(period))), evaluate)
            for name, (required_reads, evaluate) in sorted(self.store.registry.readiness.items())
        ]
        requested = sorted({read for _name, reads, _evaluate in checks for read in reads}, key=repr)
        if _query_reads is None:
            selected = self.store.select_many(connection, requested)
        else:
            if _query_reads.connection is not connection or _query_reads.store is not self.store:
                raise ValueError("period readiness reads belong to another snapshot")
            _query_reads.prime_select(requested)
            selected = {read: _query_reads.select(read) for read in requested}
        for name, reads, evaluate in checks:
            context = Context({read: selected[read] for read in reads})
            found = list(evaluate(YearMonth(period), context))
            for issue in found:
                if "work_area" not in issue:
                    area = READINESS_WORK_AREAS.get(name)
                    if name == "assets_and_financing":
                        area = (
                            "financing"
                            if issue.get("field") in ASSET_FINANCING_READINESS_FIELDS
                            else "assets"
                        )
                    if area is not None:
                        issue["work_area"] = area
            readiness_issues.extend(found)
            issues.extend(found)
            used = [item for _read, items in context.trace().selections for item in items]
            readiness[name] = {
                "facts": sorted({item.id for item in used if isinstance(item, FactVersion)}),
                "calculations": sorted({item.id for item in used if isinstance(item, Calculation)}),
            }
        accounting_issues = [
            issue for issue in readiness_issues if issue.get("domain") == "payroll"
        ]
        for row in unpublished:
            if row["kind"] in kinds:
                issue = {"field": row["id"], "message": "业务事实尚未正式处理"}
                accounting_issues.append(issue)
                issues.append(issue)
        snapshot_issues = []
        from .duplicates import DuplicateCandidates

        duplicate_issues = (
            _parallel_checks.duplicates()
            if _parallel_checks is not None
            else DuplicateCandidates(self.store).close_readiness(
                connection, period, _inspection_cache=_inspection_cache, _query_reads=_query_reads
            )
        )
        snapshot_issues.extend(duplicate_issues)
        issues.extend(duplicate_issues)
        for name, checker in self.store.registry.snapshot_readiness.items():
            found = list(
                _parallel_checks.report()
                if _parallel_checks is not None and name == "financial_reports"
                else checker(self.store, connection, YearMonth(period), reads=_query_reads)
            )
            snapshot_issues.extend(found)
            issues.extend(found)
        from .stored_json import verify_sql_outcomes

        current_result_ids = (
            row[0]
            for row in connection.execute(
                "SELECT c.id FROM calculation_current a JOIN calculation c "
                "ON c.id=a.calculation_id WHERE c.period=?",
                (month,),
            )
        )
        if _query_reads is not None:
            _query_reads.verify_sql_outcomes(current_result_ids)
        else:
            verify_sql_outcomes(connection, current_result_ids)
        bank_accounts = {
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT json_extract(b.value,'$.key') FROM calculation_current a "
                "JOIN calculation c ON c.id=a.calculation_id,json_each(c.outcome,'$.balances') b "
                "WHERE c.period=? AND json_extract(b.value,'$.category')='bank'",
                (month,),
            )
        }
        reconciled = {
            row[0]
            for row in connection.execute(
                "SELECT "
                "json_extract(c.outcome,'$.values.bank_account_id') "
                "FROM calculation_current a "
                "JOIN calculation c ON c.id=a.calculation_id WHERE "
                "c.period=? AND c.kind='bank_reconciliation'",
                (month,),
            )
        }
        for account in sorted(bank_accounts - reconciled):
            issue = {
                "field": "bank_reconciliation",
                "work_area": "bank",
                "bank_account_id": account,
                "message": "银行账户尚未完成对账",
            }
            snapshot_issues.append(issue)
            issues.append(issue)
        pending = (
            connection.execute(
                "SELECT p.subject_id FROM pending p CROSS JOIN "
                "fact_current c CROSS JOIN fact_revision f CROSS JOIN subject s "
                "WHERE c.subject_id=p.subject_id AND f.id=c.fact_id AND s.id=c.subject_id "
                f"AND s.kind IN({','.join('?' for _ in kinds)}) AND f.period<=? LIMIT 1",
                (*kinds, month),
            ).fetchone()
            if kinds
            else None
        )
        if pending:
            issue = {"field": pending[0], "message": "当前或前期存在待更正事项"}
            accounting_issues.append(issue)
            issues.append(issue)
        return {
            "period": period,
            "materials": {
                "status": "needs_information" if material_issues else "ready",
                "issues": material_issues,
                "inventories": inventories,
                "coverage": material_coverage,
            },
            "accounting": {
                "status": "needs_information" if accounting_issues else "ready",
                "issues": accounting_issues,
                "unpublished": unpublished,
                "pending_subject_id": pending[0] if pending else None,
            },
            "close_requirements": {
                "status": "needs_information" if readiness_issues or snapshot_issues else "ready",
                "issues": [*readiness_issues, *snapshot_issues],
                "readiness": readiness,
            },
            "issues": issues,
        }

    def preview_close(self, period: str, *, owner_confirmation: str):
        with self.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            epochs = self.store.epochs(connection)
            manifest = self._manifest(connection, period, owner_confirmation)
            connection.commit()
        return {
            "status": "preview",
            "epochs": epochs,
            "manifest": manifest,
            "digest": digest(
                [manifest, epochs["accounting"], epochs["material"], epochs["management"]]
            ).hex(),
        }

    def close(
        self,
        period: str,
        *,
        owner_confirmation: str,
        preview_digest: str,
        epochs: dict,
        request_id: str,
        backup_directory: str | None = None,
    ):
        hashed = digest(
            ["close", period, owner_confirmation, preview_digest, epochs, backup_directory]
        )
        cached = self.engine._cached(request_id, hashed)
        if cached:
            return cached

        def operation(connection):
            manifest = self._manifest(connection, period, owner_confirmation)
            current = self.store.epochs(connection)
            if (
                digest(
                    [manifest, current["accounting"], current["material"], current["management"]]
                ).hex()
                != preview_digest
            ):
                raise KernelError("preview_expired", "关账预览已变化")
            if self.authorize_close:
                manifest["approval"] = self.authorize_close(
                    connection, period, preview_digest, epochs
                )
            from .close_storage import write_close
            from .duplicate_freeze import persist_duplicate_freeze, prepare_duplicate_freeze
            from .material_watch import persist_material_watch, prepare_material_watch
            from .period_balance_freeze import persist_balance_freeze, prepare_balance_freeze
            from .report_classification_directory import (
                DERIVED_ROOT_NAME as REPORT_CLASSIFICATION_ROOT,
            )
            from .report_classification_directory import (
                persist_classification_directory,
                prepare_classification_directory,
            )
            from .report_flow import persist_report_flow, prepare_report_flow
            from .report_projection import prepare_report_projection
            from .report_semantics import persist_report_semantics, prepare_report_semantics
            from .settlement_freeze import (
                persist_freeze_projection,
                prepare_freeze_projection,
            )

            month = YearMonth(period).ordinal
            logical_close_digest = digest(manifest)
            prepared_settlement = prepare_freeze_projection(
                connection, month, logical_close_digest, manifest["publication_sequence"]
            )
            prepared_report = prepare_report_projection(
                self.engine, connection, month, manifest, logical_close_digest
            )
            prepared_semantics = prepare_report_semantics(
                self.engine, connection, month, prepared_report.rows, logical_close_digest
            )
            prepared_flow = prepare_report_flow(
                self.engine, connection, month, prepared_report, prepared_semantics
            )
            prepared_classifications = prepare_classification_directory(
                connection,
                month,
                logical_close_digest,
                manifest,
                prepared_flow.content,
                allow_absent_financial_reports=(
                    "financial_reports" not in self.store.registry.readiness
                    and "report_classification" not in self.store.registry.models
                ),
            )
            prepared_balances = prepare_balance_freeze(
                connection, month, logical_close_digest, manifest["publication_sequence"]
            )
            prepared_duplicates = prepare_duplicate_freeze(
                self.engine, connection, month, logical_close_digest
            )
            prepared_materials = prepare_material_watch(
                self.engine, connection, month, manifest, logical_close_digest
            )
            close_digest = write_close(
                connection,
                month,
                manifest,
                projection_roots={
                    "settlement": prepared_settlement.root_digest,
                    "report": prepared_report.root_digest,
                    "report_semantics": prepared_semantics.root_digest,
                    "report_flow": prepared_flow.root_digest,
                    REPORT_CLASSIFICATION_ROOT: prepared_classifications.root_digest,
                    "period_balance": prepared_balances.root_digest,
                    "duplicate": prepared_duplicates.root_digest,
                    "material_watch": prepared_materials.root_digest,
                },
            )
            persist_freeze_projection(connection, prepared_settlement)
            from .frozen_material import MATERIAL_COVERAGE_RULE_DIGEST

            connection.execute(
                "INSERT INTO material_close_rule VALUES(?,?)",
                (YearMonth(period).ordinal, MATERIAL_COVERAGE_RULE_DIGEST),
            )
            from .read_indexes import sync_close

            sync_close(connection, month)
            persist_duplicate_freeze(connection, prepared_duplicates)
            persist_material_watch(connection, prepared_materials)
            from .report_projection import persist_report_projection

            persist_report_projection(connection, prepared_report)
            persist_report_semantics(connection, prepared_semantics)
            persist_report_flow(connection, prepared_flow)
            persist_classification_directory(connection, prepared_classifications)
            persist_balance_freeze(connection, prepared_balances)
            job_id = None
            if backup_directory:
                job_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO jobs(id,kind,payload,status) VALUES(?,?,?,'pending')",
                    (
                        job_id,
                        "portable_backup",
                        canonical(
                            {
                                "directory": backup_directory,
                                "rollover": True,
                                "close_period": period,
                                "close_digest": close_digest.hex(),
                            }
                        ),
                    ),
                )
            return {
                "status": "closed",
                "period": period,
                "digest": close_digest.hex(),
                "backup_job": job_id,
            }

        return self.engine._write(
            request_id,
            hashed,
            epochs,
            ("accounting", "material"),
            "close",
            operation,
            checked_lanes=("accounting", "material", "management"),
        )

    def closed_report(self, period: str):
        try:
            month = YearMonth(period).ordinal
        except ValueError as exc:
            raise KernelError("invalid_command", "会计月份格式不正确") from exc
        with self.store.connection(read_only=True) as connection:
            row = connection.execute(
                "SELECT * FROM period_close WHERE period=?",
                (month,),
            ).fetchone()
            if not row:
                raise KernelError("frozen_snapshot_unavailable", "该月份没有精确的关账冻结记录")
            from .close_storage import decode_close

            return decode_close(connection, row)
