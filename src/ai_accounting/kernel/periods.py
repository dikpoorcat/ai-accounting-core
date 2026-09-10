"""Materials, management metadata, period freezing and exports share kernel transactions."""

from __future__ import annotations

import json
import uuid

from .contracts import Calculation, Context, FactVersion, KernelError, NeedsInformation
from .types import YearMonth, canonical, digest

MATERIAL_CATEGORIES = ("transactions", "payroll", "bank", "tax", "assets", "financing")


class Periods:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

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
    def completeness(connection, month, registry):
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
            unprocessed = connection.execute(
                "SELECT count(*) FROM material_item i WHERE "
                "i.inventory_id=? AND NOT EXISTS(SELECT 1 "
                "FROM fact_evidence e JOIN fact_revision f ON "
                "f.id=e.fact_id WHERE "
                "e.evidence_digest=i.evidence_digest)",
                (row["id"],),
            ).fetchone()[0]
            if unprocessed:
                issues.append(
                    {
                        "field": f"materials.{category}",
                        "message": "已接收资料存在未确认的业务事实",
                        "unprocessed": unprocessed,
                    }
                )
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
            if row[0] in registry.evaluators and inventory and inventory["no_business"]:
                issues.append(
                    {
                        "field": f"materials.{model.material_category}",
                        "message": "无业务确认与已确认业务冲突",
                    }
                )
        return inventories, issues, unpublished

    def _manifest(self, connection, period: str, owner_confirmation: str):
        month = YearMonth(period).ordinal
        if not connection.execute(
            "SELECT 1 FROM evidence WHERE digest=?", (bytes.fromhex(owner_confirmation),)
        ).fetchone():
            raise NeedsInformation("owner_confirmation", "需要负责人不可变确认依据")
        previous_close = connection.execute(
            "SELECT period,digest FROM period_close ORDER BY period DESC LIMIT 1"
        ).fetchone()
        cutoff = previous_close["period"] if previous_close else -1
        if cutoff >= month:
            raise KernelError("already_closed", "月份已经关账")
        kinds = sorted(self.store.registry.evaluators)
        if kinds:
            earlier = connection.execute(
                "SELECT f.period FROM fact_revision f INDEXED BY fact_period "
                "CROSS JOIN fact_current c CROSS JOIN subject s "
                "WHERE f.period>? AND f.period<? AND c.fact_id=f.id AND s.id=f.subject_id "
                f"AND s.kind IN({','.join('?' for _ in kinds)}) ORDER BY f.period LIMIT 1",
                (cutoff, month, *kinds),
            ).fetchone()
            if earlier:
                raise KernelError(
                    "earlier_period_open",
                    "须先处理并关闭前面有业务的月份",
                    period=str(YearMonth.from_ordinal(earlier[0])),
                )
        inventories, issues, unpublished = self.completeness(connection, month, self.store.registry)
        readiness = {}
        for name, (required_reads, evaluate) in sorted(self.store.registry.readiness.items()):
            reads = tuple(required_reads(YearMonth(period)))
            context = Context({read: self.store.select(connection, read) for read in reads})
            issues.extend(evaluate(YearMonth(period), context))
            used = [item for read in context.used for item in context.selections[read]]
            readiness[name] = {
                "facts": sorted({item.id for item in used if isinstance(item, FactVersion)}),
                "calculations": sorted({item.id for item in used if isinstance(item, Calculation)}),
            }
        for row in unpublished:
            if row["kind"] in self.store.registry.evaluators:
                issues.append({"field": row["id"], "message": "业务事实尚未正式处理"})
        for checker in self.store.registry.snapshot_readiness.values():
            issues.extend(checker(self.store, connection, YearMonth(period)))
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
            issues.append(
                {
                    "field": "bank_reconciliation",
                    "bank_account_id": account,
                    "message": "银行账户尚未完成对账",
                }
            )
        pending = connection.execute(
            "SELECT p.subject_id FROM pending p CROSS JOIN "
            "fact_current c CROSS JOIN fact_revision f "
            "WHERE c.subject_id=p.subject_id AND f.id=c.fact_id AND f.period<=? LIMIT 1",
            (month,),
        ).fetchone()
        if pending:
            issues.append({"field": pending[0], "message": "当前或前期存在待更正事项"})
        if issues:
            raise KernelError("period_not_ready", "关账条件尚未满足", fact_issues=issues)
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
                "FROM monthly_account WHERE period<=? "
                "GROUP BY account ORDER BY account",
                (month,),
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
            "trial_balance": trial_balance,
            "report_classification": {
                "1": "assets",
                "2": "liabilities",
                "3": "equity",
                "4": "costs",
                "5": "income_and_expenses",
            },
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
            "digest": digest([manifest, epochs["accounting"], epochs["material"]]).hex(),
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
        preview = self.preview_close(period, owner_confirmation=owner_confirmation)
        if preview["digest"] != preview_digest:
            raise KernelError("preview_expired", "关账预览已变化")
        manifest = preview["manifest"]

        def operation(connection):
            connection.execute(
                "INSERT INTO period_close VALUES(?,?,?)",
                (YearMonth(period).ordinal, canonical(manifest), digest(manifest)),
            )
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
            request_id, hashed, epochs, ("accounting", "material"), "close", operation
        )

    def closed_report(self, period: str):
        with self.store.connection(read_only=True) as connection:
            row = connection.execute(
                "SELECT manifest FROM period_close WHERE period=?", (YearMonth(period).ordinal,)
            ).fetchone()
            if not row:
                raise KernelError("not_closed", "月份尚未关账")
            return json.loads(row[0])
