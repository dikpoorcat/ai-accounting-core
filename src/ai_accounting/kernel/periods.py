"""Materials, management metadata, period freezing and exports share kernel transactions."""

from __future__ import annotations

import json
import uuid

from .contracts import Calculation, Context, FactVersion, KernelError, NeedsInformation
from .types import YearMonth, canonical, digest

MATERIAL_CATEGORIES = ("transactions", "payroll", "bank", "tax", "assets", "financing")
_CURRENT_CLOSE = object()


class Periods:
    def __init__(self, engine, *, authorize_close=None, authorize_close_range=None):
        self.engine, self.store = engine, engine.store
        self.authorize_close = authorize_close
        self.authorize_close_range = authorize_close_range

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
                "material_revision(period,category,expected,received,pro"
                "cessed,no_business,evidence_digest) "
                "VALUES(?,?,?,?,0,?,?) RETURNING id",
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
        from .materials import check_completeness

        if material_coverage is None:
            material_coverage = check_completeness(connection, month, registry)
        issues.extend(material_coverage["issues"])
        # Facts awaiting initial publication are just as incomplete as stale results.
        unpublished = connection.execute(
            "SELECT s.id,s.kind FROM subject s JOIN fact_current f "
            "ON f.subject_id=s.id JOIN fact_revision r "
            "ON r.id=f.fact_id LEFT JOIN calculation_current c ON c.subject_id=s.id "
            "WHERE r.period=? AND c.subject_id IS NULL",
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
        self, connection, period: str, owner_confirmation: str, *, previous_close=_CURRENT_CLOSE
    ):
        from .display import Display

        month = YearMonth(period).ordinal
        if not connection.execute(
            "SELECT 1 FROM evidence WHERE digest=?", (bytes.fromhex(owner_confirmation),)
        ).fetchone():
            raise NeedsInformation("owner_confirmation", "需要负责人不可变确认依据")
        checked = self.check_readiness(connection, period, previous_close)
        if checked["order_failure"]:
            failure = checked["order_failure"]
            raise KernelError(failure["code"], failure["message"], **failure["details"])
        if checked["issues"]:
            raise KernelError(
                "period_not_ready", "关账条件尚未满足", fact_issues=checked["issues"]
            )
        previous_close = checked["previous_close"]
        inventories = checked["materials"]["inventories"]
        material_coverage = checked["materials"]["coverage"]
        readiness = checked["close_requirements"]["readiness"]
        vouchers = [
            dict(r)
            for r in connection.execute(
                "SELECT v.id,v.voucher_id,v.calculation_id,v.total,s.number FROM voucher_current c "
                "JOIN voucher_version v ON v.id=c.version_id JOIN voucher s ON s.id=v.voucher_id "
                "WHERE v.period=? ORDER BY s.number",
                (month,),
            )
        ]
        lineage = """WITH RECURSIVE roots(id) AS (
          SELECT c.id FROM calculation c JOIN calculation_current a ON a.calculation_id=c.id
 WHERE c.period=?
          UNION SELECT v.calculation_id FROM voucher_version v JOIN voucher_current a ON
 a.version_id=v.id WHERE v.period=?
        ), lineage(id) AS (SELECT id FROM roots UNION
          SELECT d.upstream_id FROM dependency_calculation d JOIN lineage l ON
 l.id=d.calculation_id)
        """
        calculations = {
            r[0] for r in connection.execute(lineage + "SELECT id FROM lineage", (month, month))
        }
        facts = {
            r[0]
            for r in connection.execute(
                lineage + "SELECT DISTINCT d.fact_id FROM dependency_fact d "
                "JOIN lineage l ON l.id=d.calculation_id",
                (month, month),
            )
        }
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
        return {
            "period": period,
            "company_id": self.store.company_id,
            "previous_close_digest": previous_close["digest"].hex() if previous_close else None,
            "database_id": self.store.database_id,
            "vouchers": vouchers,
            "calculations": sorted(calculations),
            "facts": sorted(facts),
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

    def check_readiness(self, connection, period: str, previous_close=_CURRENT_CLOSE):
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
        collected = self.collect_current_readiness(connection, period)
        return {
            "period": period,
            "previous_close": previous_close,
            "order_failure": None,
            "materials": collected["materials"],
            "accounting": collected["accounting"],
            "close_requirements": collected["close_requirements"],
            "issues": collected["issues"],
        }

    def collect_current_readiness(self, connection, period: str):
        """Collect current issues without interpreting a historical close boundary."""

        month = YearMonth(period).ordinal
        kinds = sorted(
            kind
            for kind in self.store.registry.evaluators
            if self.store.registry.models[kind].lane != "management"
        )
        from .materials import check_completeness

        material_coverage = check_completeness(connection, month, self.store.registry)
        inventories, material_issues, unpublished = self.completeness(
            connection, month, self.store.registry, material_coverage=material_coverage
        )
        issues = list(material_issues)
        readiness = {}
        readiness_issues = []
        for name, (required_reads, evaluate) in sorted(self.store.registry.readiness.items()):
            reads = tuple(required_reads(YearMonth(period)))
            context = Context({read: self.store.select(connection, read) for read in reads})
            found = list(evaluate(YearMonth(period), context))
            readiness_issues.extend(found)
            issues.extend(found)
            used = [item for read in context.used for item in context.selections[read]]
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
        for checker in self.store.registry.snapshot_readiness.values():
            found = list(checker(self.store, connection, YearMonth(period)))
            snapshot_issues.extend(found)
            issues.extend(found)
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
                "status": "needs_information"
                if readiness_issues or snapshot_issues
                else "ready",
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
                manifest["password_confirmation"] = self.authorize_close(
                    connection, period, preview_digest, epochs
                )
            connection.execute(
                "INSERT INTO period_close VALUES(?,?,?)",
                (YearMonth(period).ordinal, canonical(manifest), digest(manifest)),
            )
            from .read_indexes import sync_close

            sync_close(connection, YearMonth(period).ordinal)
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
                                "close_digest": digest(manifest).hex(),
                            }
                        ),
                    ),
                )
            return {
                "status": "closed",
                "period": period,
                "digest": digest(manifest).hex(),
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
        with self.store.connection(read_only=True) as connection:
            row = connection.execute(
                "SELECT manifest FROM period_close WHERE period=?", (YearMonth(period).ordinal,)
            ).fetchone()
            if not row:
                raise KernelError("not_closed", "月份尚未关账")
            return json.loads(row[0])

    def _range_snapshot(self, connection, from_period, through_period, owner_confirmation):
        first, last = YearMonth(from_period), YearMonth(through_period)
        if first > last:
            raise KernelError("invalid_close_range", "关账起始月不能晚于截至月")
        epochs = self.store.epochs(connection)
        closed = list(connection.execute("SELECT period,digest FROM period_close ORDER BY period"))
        by_month = {row["period"]: row for row in closed}
        remaining, prefix = first.ordinal, []
        while remaining <= last.ordinal and remaining in by_month:
            row = by_month[remaining]
            prefix.append(
                {"period": str(YearMonth.from_ordinal(remaining)), "digest": row["digest"].hex()}
            )
            remaining += 1
        previous = closed[-1] if closed else None
        if remaining <= last.ordinal and previous is not None:
            if previous["period"] >= remaining:
                raise KernelError(
                    "closed_range_gap", "已有闭期不是连续前缀，不能补写或重开其前方月份"
                )
            if remaining != previous["period"] + 1:
                raise KernelError(
                    "close_range_not_contiguous",
                    "待关范围必须紧接当前最后闭期",
                    next_period=str(YearMonth.from_ordinal(previous["period"] + 1)),
                )
        anchor = (
            None
            if previous is None
            else {
                "period": str(YearMonth.from_ordinal(previous["period"])),
                "digest": previous["digest"].hex(),
            }
        )
        manifests = []
        for ordinal in range(remaining, last.ordinal + 1):
            month = str(YearMonth.from_ordinal(ordinal))
            try:
                manifest = self._manifest(
                    connection, month, owner_confirmation, previous_close=previous
                )
            except KernelError as error:
                error.details["closing_period"] = month
                for issue in error.details.get("fact_issues", ()):
                    issue.setdefault("closing_period", month)
                raise
            manifests.append(manifest)
            # Only the prior closed boundary is virtual. Facts, publications,
            # inventories and every readiness check use the unchanged read snapshot.
            previous = {"period": ordinal, "digest": digest(manifest)}
        result = {
            "status": "preview" if manifests else "already_closed",
            "company_id": self.store.company_id,
            "database_id": self.store.database_id,
            "from_period": str(first),
            "through_period": str(last),
            "owner_confirmation": owner_confirmation,
            "epochs": epochs,
            "closed_prefix": prefix,
            "previous_close": anchor,
            "manifests": manifests,
            "month_count": len(manifests),
        }
        result["digest"] = digest(
            {
                **result,
                "epochs": {key: epochs[key] for key in ("accounting", "material", "management")},
            }
        ).hex()
        return result

    def preview_close_range(
        self,
        from_period: str,
        through_period: str,
        *,
        owner_confirmation: str,
    ):
        """Preview all consecutive months without writing or rolling back any close."""
        with self.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            result = self._range_snapshot(
                connection, from_period, through_period, owner_confirmation
            )
            connection.commit()
        return result

    def close_range(
        self,
        from_period: str,
        through_period: str,
        *,
        owner_confirmation: str,
        preview_digest: str,
        epochs: dict,
        request_id: str,
        backup_directory: str | None = None,
    ):
        """Recheck and publish one company's complete range in one transaction."""
        first, last = str(YearMonth(from_period)), str(YearMonth(through_period))
        hashed = digest(
            [
                "close_range",
                first,
                last,
                owner_confirmation,
                preview_digest,
                epochs,
                backup_directory,
            ]
        )
        cached = self.engine._cached(request_id, hashed)
        if cached is not None:
            return cached

        def operation(connection):
            preview = self._range_snapshot(connection, first, last, owner_confirmation)
            if not preview["manifests"]:
                raise KernelError("already_closed", "请求范围已经全部关账，无需再次批准")
            if preview["digest"] != preview_digest:
                raise KernelError("preview_expired", "连续关账预览已变化，请重新核对整个范围")
            authorization = None
            if self.authorize_close_range:
                authorization = self.authorize_close_range(
                    connection, first, last, preview_digest, epochs
                )
            previous_digest = (
                preview["previous_close"]["digest"] if preview["previous_close"] else None
            )
            results = []
            for prepared in preview["manifests"]:
                manifest = {**prepared, "previous_close_digest": previous_digest}
                if authorization is not None:
                    manifest["password_confirmation"] = authorization
                manifest["close_range"] = {
                    "from_period": first,
                    "through_period": last,
                    "preview_digest": preview_digest,
                }
                hashed_manifest = digest(manifest)
                connection.execute(
                    "INSERT INTO period_close VALUES(?,?,?)",
                    (YearMonth(manifest["period"]).ordinal, canonical(manifest), hashed_manifest),
                )
                from .read_indexes import sync_close

                sync_close(connection, YearMonth(manifest["period"]).ordinal)
                result = {"period": manifest["period"], "digest": hashed_manifest.hex()}
                connection.execute(
                    "INSERT INTO audit(request_id,action,payload) VALUES(?,?,?)",
                    (
                        request_id,
                        "close_range_month",
                        canonical(
                            {
                                **result,
                                "from_period": first,
                                "through_period": last,
                                "preview_digest": preview_digest,
                                "actor": self.engine.audit_actor,
                            }
                        ),
                    ),
                )
                results.append(result)
                previous_digest = result["digest"]
                self.engine.fault("close_range_month:" + manifest["period"], connection)
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
                                "close_period": last,
                                "close_digest": previous_digest,
                            }
                        ),
                    ),
                )
            return {
                "status": "closed",
                "company_id": self.store.company_id,
                "database_id": self.store.database_id,
                "from_period": first,
                "through_period": last,
                "preview_digest": preview_digest,
                "closed_prefix": preview["closed_prefix"],
                "results": results,
                "backup_job": job_id,
            }

        return self.engine._write(
            request_id,
            hashed,
            epochs,
            ("accounting", "material"),
            "close_range",
            operation,
            checked_lanes=("accounting", "material", "management"),
        )
