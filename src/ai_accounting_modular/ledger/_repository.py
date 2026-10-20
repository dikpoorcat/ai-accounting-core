"""The only SQL access to ledger-owned tables; the host supplies its session."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from ai_accounting_modular.contracts.public import canonical_json as canonical
from ai_accounting_modular.contracts.public import content_digest as digest
from ai_accounting_modular.contracts.public import format_fen, parse_fen

from ._resource_rows import resource_rows
from ._values import fail

_CHILD_TABLES = (
    "result_source",
    "obligation",
    "settlement",
    "obligation_reclassification",
    "cost_lot",
    "cost_consumption",
    "result_fund_movement",
    "result_tax_sale",
    "result_report_contribution",
)


class LedgerRepository:
    def latest_period(self):
        return self._session.fetchone("SELECT MAX(business_period) FROM adopted_result")[0]

    def __init__(self, session: Any):
        self._session = session
        self._prepared = {}
        self._bound_plans = {}
        self.resource_revision = 0
        self._resource_views = {}

    def resource_view(self, *, as_of=None):
        from ._resource_view import ResourceRepository

        if as_of not in self._resource_views:
            self._resource_views[as_of] = ResourceRepository(self, as_of=as_of)
        return self._resource_views[as_of]

    def _resources_changed(self):
        self.resource_revision += 1
        self._resource_views.clear()

    def prepared(self, business_id, result_slot):
        return self._prepared.get((business_id, result_slot))

    def remember_prepared(self, prepared):
        self._prepared[prepared.business_id, prepared.result_slot] = prepared

    def consume_prepared(self, business_id, result_slot):
        self._prepared.pop((business_id, result_slot), None)
        self._bound_plans.pop((business_id, result_slot), None)

    def bound_plan(self, business_id, result_slot):
        return self._bound_plans.get((business_id, result_slot))

    def remember_bound_plan(self, bound):
        self._bound_plans[bound.identity.business_id, bound.identity.result_slot] = bound

    def current_heads(self, business_ids=()):
        sql = (
            "SELECT r.* FROM adopted_result r JOIN result_slot_current c "
            "ON c.head_result_id=r.result_id"
        )
        args = ()
        if business_ids:
            sql += " WHERE r.business_id IN (SELECT value FROM json_each(?))"
            args = (canonical(list(business_ids)),)
        return [dict(row) for row in self._session.fetchall(sql, args)]

    def heads_through(self, as_of, business_ids=()):
        sql = (
            "SELECT r.* FROM adopted_result r WHERE r.posting_period<=? AND NOT EXISTS("
            "SELECT 1 FROM adopted_result newer WHERE newer.business_id=r.business_id "
            "AND newer.result_slot=r.result_slot AND newer.posting_period<=? "
            "AND newer.posting_period>r.posting_period)"
        )
        args = [as_of, as_of]
        if business_ids:
            sql += " AND r.business_id IN (SELECT value FROM json_each(?))"
            args.append(canonical(list(business_ids)))
        return [dict(row) for row in self._session.fetchall(sql, tuple(args))]

    def result(self, result_id):
        row = self._session.fetchone("SELECT * FROM adopted_result WHERE result_id=?", (result_id,))
        return dict(row) if row else None

    def formal_heads_through(self, as_of, business_ids=()):
        sql = "SELECT * FROM adopted_result WHERE 1=1"
        args = []
        if as_of is not None:
            sql += " AND posting_period<=?"
            args.append(as_of)
        if business_ids:
            sql += " AND business_id IN (SELECT value FROM json_each(?))"
            args.append(canonical(list(business_ids)))
        return [dict(row) for row in self._session.fetchall(sql, tuple(args))]

    def is_closed(self, period):
        return (
            self._session.fetchone("SELECT 1 FROM period_close WHERE period=?", (period,))
            is not None
        )

    def vouchers(self, result_ids):
        rows = self._session.fetchall(
            "SELECT * FROM voucher WHERE result_id IN (SELECT value FROM json_each(?)) "
            "ORDER BY period,number,voucher_id",
            (canonical(list(result_ids)),),
        )
        ids = [r["voucher_id"] for r in rows]
        lines = self._session.fetchall(
            "SELECT * FROM voucher_line WHERE voucher_id IN (SELECT value FROM json_each(?)) "
            "ORDER BY voucher_id,line_no",
            (canonical(ids),),
        )
        by_id = {key: [] for key in ids}
        for row in lines:
            line = {k: row[k] for k in ("line_no", "account_code", "summary")}
            line.update(
                debit_fen=format_fen(row["debit_fen"]), credit_fen=format_fen(row["credit_fen"])
            )
            for key in ("party_id", "object_id"):
                if row[key] is not None:
                    line[key] = row[key]
            by_id[row["voucher_id"]].append(line)
        return [dict(row, lines=by_id[row["voucher_id"]]) for row in rows]

    def children(self, table, result_ids):
        if table not in _CHILD_TABLES:
            raise ValueError("not a ledger child table")
        return [
            dict(row)
            for row in self._session.fetchall(
                f"SELECT * FROM {table} WHERE result_id IN (SELECT value FROM json_each(?))",
                (canonical(list(result_ids)),),
            )
        ]

    def save_head(self, plan, fact_seq, *, result_id, generation, posting_period, baseline_id):
        self._resources_changed()
        values = (
            result_id,
            plan["business_id"],
            plan["result_slot"],
            generation,
            plan["accounting_period"],
            posting_period,
            "open",
            "withdrawn" if plan["target"]["kind"] == "system.withdrawal" else "active",
            baseline_id,
            fact_seq,
            plan["calculation_key"],
            plan["calculation_revision"],
            "business-facts.schema.json#/$defs/AdoptedBasis",
            canonical(plan["basis"]),
            "business-facts.schema.json#/$defs/ResultTarget",
            canonical(plan["target"]),
            digest(plan["basis"]),
            digest(plan["target"]),
        )
        self._session.execute(
            "INSERT INTO adopted_result VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(result_id) DO UPDATE SET generation=excluded.generation,"
            "business_period=excluded.business_period,posting_period=excluded.posting_period,"
            "target_state=excluded.target_state,adopted_fact_seq=excluded.adopted_fact_seq,"
            "calculation_key=excluded.calculation_key,calculation_revision=excluded.calculation_revision,"
            "basis_schema=excluded.basis_schema,basis_json=excluded.basis_json,"
            "target_schema=excluded.target_schema,target_json=excluded.target_json,"
            "basis_digest=excluded.basis_digest,target_digest=excluded.target_digest",
            values,
        )
        self._session.execute(
            "INSERT INTO result_slot_current VALUES(?,?,?,?) "
            "ON CONFLICT(business_id,result_slot) DO UPDATE SET "
            "head_result_id=excluded.head_result_id,generation=excluded.generation",
            (plan["business_id"], plan["result_slot"], result_id, generation),
        )

    def sync_open_fact_seq(self, result_id, fact_seq):
        self._session.execute(
            "UPDATE adopted_result SET adopted_fact_seq=? WHERE result_id=? "
            "AND state='open' AND adopted_fact_seq<>?",
            (fact_seq, result_id, fact_seq),
        )

    def clear_children(self, result_id):
        self._resources_changed()
        for table in _CHILD_TABLES:
            self._session.execute(f"DELETE FROM {table} WHERE result_id=?", (result_id,))

    def remove_open(self, row):
        if row["state"] != "open":
            fail("dependency_conflict", "冻结结果不能直接退出，须提交发现期撤去目标。")
        self.clear_children(row["result_id"])
        for voucher in self.vouchers([row["result_id"]]):
            self._session.execute(
                "UPDATE voucher SET sealed=0 WHERE voucher_id=?", (voucher["voucher_id"],)
            )
            self._session.execute(
                "DELETE FROM voucher WHERE voucher_id=?", (voucher["voucher_id"],)
            )
        self._session.execute(
            "DELETE FROM result_slot_current WHERE head_result_id=?", (row["result_id"],)
        )
        self._session.execute("DELETE FROM adopted_result WHERE result_id=?", (row["result_id"],))

    def write_vouchers(self, result_id, period, specifications):
        existing = {v["role"]: v for v in self.vouchers([result_id])}
        wanted = {role for role, _, _ in specifications}
        for role, voucher in existing.items():
            if role not in wanted:
                self._session.execute(
                    "UPDATE voucher SET sealed=0 WHERE voucher_id=?", (voucher["voucher_id"],)
                )
                self._session.execute(
                    "DELETE FROM voucher WHERE voucher_id=?", (voucher["voucher_id"],)
                )
        for role, lines, reverses in specifications:
            old = existing.get(role)
            voucher_id = old["voucher_id"] if old else str(uuid4())
            number = old["number"] if old and old["period"] == period else self._reserve(period)
            summary = lines[0]["summary"]
            if old:
                self._session.execute(
                    "UPDATE voucher SET sealed=0 WHERE voucher_id=?", (voucher_id,)
                )
                self._session.execute("DELETE FROM voucher_line WHERE voucher_id=?", (voucher_id,))
                self._session.execute(
                    "UPDATE voucher SET period=?,number=?,summary=?,reverses_voucher_id=? "
                    "WHERE voucher_id=?",
                    (period, number, summary, reverses, voucher_id),
                )
            else:
                self._session.execute(
                    "INSERT INTO voucher VALUES(?,?,?,?,?,?,?,0)",
                    (voucher_id, result_id, role, period, number, summary, reverses),
                )
            self._session.executemany(
                "INSERT INTO voucher_line VALUES(?,?,?,?,?,?,?,?)",
                [
                    (
                        voucher_id,
                        line["line_no"],
                        line["account_code"],
                        parse_fen(line["debit_fen"]),
                        parse_fen(line["credit_fen"]),
                        line.get("party_id"),
                        line.get("object_id"),
                        line["summary"],
                    )
                    for line in lines
                ],
            )
            self._session.execute("UPDATE voucher SET sealed=1 WHERE voucher_id=?", (voucher_id,))

    def _reserve(self, period):
        row = self._session.fetchone(
            "SELECT next_number FROM voucher_sequence WHERE period=?", (period,)
        )
        number = row[0] if row else 1
        self._session.execute(
            "INSERT INTO voucher_sequence VALUES(?,?) ON CONFLICT(period) "
            "DO UPDATE SET next_number=excluded.next_number",
            (period, number + 1),
        )
        return number

    def write_resources(self, result_id, plan):
        self._resources_changed()
        for table, rows in resource_rows(result_id, plan).items():
            if rows:
                placeholders = ",".join("?" for _ in rows[0])
                self._session.executemany(f"INSERT INTO {table} VALUES({placeholders})", rows)

    def check_sealed(self, result_ids):
        bad = self._session.fetchone(
            "SELECT v.voucher_id FROM voucher v WHERE v.result_id IN "
            "(SELECT value FROM json_each(?)) AND (v.sealed<>1 OR NOT EXISTS "
            "(SELECT 1 FROM voucher_line l WHERE l.voucher_id=v.voucher_id) OR "
            "(SELECT COALESCE(SUM(debit_fen),0)-COALESCE(SUM(credit_fen),0) "
            "FROM voucher_line l WHERE l.voucher_id=v.voucher_id)<>0) LIMIT 1",
            (canonical(list(result_ids)),),
        )
        if bad:
            fail("unbalanced_voucher", "事务不能提交未封存或不平衡的凭证。")

    def incoming(self, table, business_ids, *, through=None):
        if table not in ("settlement", "cost_consumption", "obligation_reclassification"):
            raise ValueError("not a resource relation")
        if through is None:
            sql = (
                f"SELECT x.* FROM {table} x JOIN result_slot_current c "
                "ON c.head_result_id=x.result_id WHERE x.source_business_id IN "
                "(SELECT value FROM json_each(?))"
            )
            args = (canonical(list(business_ids)),)
        else:
            sql = (
                f"SELECT x.* FROM {table} x JOIN adopted_result r ON r.result_id=x.result_id "
                "WHERE x.source_business_id IN (SELECT value FROM json_each(?)) "
                "AND r.posting_period<=? AND NOT EXISTS(SELECT 1 FROM adopted_result newer "
                "WHERE newer.business_id=r.business_id AND newer.result_slot=r.result_slot "
                "AND newer.posting_period<=? AND newer.posting_period>r.posting_period)"
            )
            args = (canonical(list(business_ids)), through, through)
        return [dict(row) for row in self._session.fetchall(sql, args)]

    def linked_sources(self, business_ids):
        heads = self.current_heads(business_ids)
        ids = [r["result_id"] for r in heads]
        result = set(business_ids)
        for table in ("settlement", "cost_consumption", "obligation_reclassification"):
            for row in self.children(table, ids):
                result.add(row["source_business_id"])
                if table == "obligation_reclassification":
                    result.add(row["target_business_id"])
        return result

    def movements(self, movement_ids):
        return [
            dict(row)
            for row in self._session.fetchall(
                "SELECT m.* FROM result_fund_movement m JOIN result_slot_current c "
                "ON c.head_result_id=m.result_id WHERE m.movement_id IN "
                "(SELECT value FROM json_each(?))",
                (canonical(list(movement_ids)),),
            )
        ]

    def movement_scope(self, account_id, period_from, period_to, *, as_of=None):
        selection = (
            "JOIN result_slot_current c ON c.head_result_id=m.result_id " if as_of is None else ""
        )
        sql = (
            "SELECT m.*,r.business_id,r.result_slot,r.business_period,r.generation,"
            "r.baseline_result_id,r.calculation_key,r.calculation_revision,"
            "r.basis_digest,r.target_digest FROM result_fund_movement m "
            "JOIN adopted_result r ON r.result_id=m.result_id "
            + selection
            + "WHERE m.account_id=? AND m.period>=? AND m.period<=?"
        )
        args = [account_id, period_from, period_to]
        if as_of is not None:
            sql += (
                " AND r.posting_period<=? AND NOT EXISTS(SELECT 1 FROM adopted_result newer "
                "WHERE newer.business_id=r.business_id AND newer.result_slot=r.result_slot "
                "AND newer.posting_period<=? AND newer.posting_period>r.posting_period)"
            )
            args += [as_of, as_of]
        return [dict(row) for row in self._session.fetchall(sql, tuple(args))]

    def settlements_by_movements(self, movement_ids):
        return [
            dict(row)
            for row in self._session.fetchall(
                "SELECT s.* FROM settlement s JOIN result_slot_current c "
                "ON c.head_result_id=s.result_id WHERE s.movement_id IN "
                "(SELECT value FROM json_each(?))",
                (canonical(list(movement_ids)),),
            )
        ]

    def transfers_to(self, business_ids):
        return [
            dict(row)
            for row in self._session.fetchall(
                "SELECT x.* FROM obligation_reclassification x JOIN result_slot_current c "
                "ON c.head_result_id=x.result_id WHERE x.target_business_id IN "
                "(SELECT value FROM json_each(?))",
                (canonical(list(business_ids)),),
            )
        ]

    def freeze_heads(self, period):
        self._resources_changed()
        self._session.execute(
            "UPDATE adopted_result SET state='frozen' WHERE posting_period=? AND state='open'",
            (period,),
        )
