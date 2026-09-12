"""Bounded presentation of selected funds, statements and investment costs.

Version choice stays in the shared business selector. SQL below aggregates stored
effects and selects page keys; only returned rows need facts and display sources.
"""

from __future__ import annotations

from .contracts import KernelError
from .dashboard_reads import page_keys
from .types import YearMonth, canonical

FUND_TYPES = {"bank": "bank", "cash": "cash", "platform": "payment_platform"}
SECTIONS = {"accounts", "movements", "statements", "investment_products", "investment_events"}


def _sum(rows, field):
    values = [row[field] for row in rows]
    return None if None in values else sum(values)


def _sql_page(connection, source, parameters, *, after, limit, where="1=1", filters=()):
    total = connection.execute(f"SELECT count(*) FROM ({source})", parameters).fetchone()[0]
    filtered = f"SELECT * FROM ({source}) WHERE {where}"
    parameters = [*parameters, *filters]
    count = connection.execute(f"SELECT count(*) FROM ({filtered})", parameters).fetchone()[0]
    if (
        after is not None
        and not connection.execute(
            f"SELECT 1 FROM ({filtered}) WHERE page_key=?", [*parameters, after]
        ).fetchone()
    ):
        raise KernelError("dashboard_snapshot_changed", "分页位置已变化，请重新加载明细。")
    rows = connection.execute(
        f"SELECT * FROM ({filtered}) WHERE page_key>? ORDER BY page_key LIMIT ?",
        [*parameters, after or "", limit + 1],
    ).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    return rows, {
        "total_count": total,
        "filtered_count": count,
        "returned_count": len(rows),
        "has_more": more,
        "next_cursor": rows[-1]["page_key"] if more else None,
    }


class FundsRead:
    def __init__(self, snap):
        self.snap, self.connection = snap, snap.connection
        self.selected = snap.queries._selected_accounting(
            self.connection,
            None,
            snap.period,
            include_vouchers=False,
            kinds={
                "opening_package",
                "bank_opening",
                "opening_bank",
                "opening_cash",
                "bank_statement",
                "bank_reconciliation",
            },
        )["through_period"]
        self.states = snap.reads.metadata(
            item["calculation_id"] for item in self.selected["state_results"]
        )
        self.state_selections = {
            item["calculation_id"]: item for item in self.selected["state_results"]
        }
        self.opening_ids = [ident for ident, item in self.states.items() if item["opening"]]
        self.issues = self.selected["unestablished_state_selections"]
        self.account_rows = {}
        self.product_rows = {}
        self.profiles = {}
        self.event_queries = {}

    def events(self, *, current=False, accounts=None):
        from .read_indexes import verify_close_references

        accounts = {"1001", "1002", "1012", "1101"} if accounts is None else accounts
        cache_key = current, tuple(sorted(accounts))
        if cache_key in self.event_queries:
            query, parameters = self.event_queries[cache_key]
            return query, list(parameters)
        journal = self.snap.month_journal if current else self.snap.journal
        query, parameters = journal.select(accounts=accounts).sql()
        references = self.connection.execute(
            f"SELECT r.* FROM ({query}) j JOIN close_reference r ON r.reference_id=j.id "
            "AND r.reference_type='voucher' AND r.close_period=j.close_period",
            parameters,
        ).fetchall()
        verify_close_references(self.connection, references)
        source = (
            "WITH journal AS (" + query + "), events AS ("
            "SELECT j.period,j.id event_id,j.number,j.basis_calculation_id calculation_id,"
            "j.basis_kind kind,CASE WHEN j.reverses_id IS NULL THEN 1 ELSE -1 END sign,"
            "0 opening,c.outcome FROM journal j JOIN calculation c ON c.id=j.basis_calculation_id"
        )
        if not current:
            source += (
                " UNION ALL SELECT p.posting_period,c.id,0,c.id,c.kind,1,1,c.outcome "
                "FROM json_each(?) ids JOIN calculation c ON c.id=ids.value "
                "JOIN calculation_publication p ON p.calculation_id=c.id"
            )
            parameters.append(canonical(self.opening_ids))
        source += (
            "), effects AS (SELECT e.*,CAST(b.key AS INTEGER) effect_index,"
            "json_extract(b.value,'$.category') category,json_extract(b.value,'$.key') balance_key,"
            "json_extract(b.value,'$.amount') amount FROM events e,"
            "json_each(e.outcome,'$.balances') b WHERE json_extract(b.value,'$.category') "
            "IN ('bank','cash','platform','short_term_investment')) "
        )
        self.event_queries[cache_key] = source, tuple(parameters)
        return source, parameters

    def movements(self):
        source, parameters = self.events(current=True)
        source = source.rstrip() + (
            ", money AS (SELECT *,row_number() OVER(PARTITION BY event_id ORDER BY effect_index)-1 "
            "local_index FROM effects WHERE category IN ('bank','cash','platform') AND amount!=0),"
            "transfers AS (SELECT event_id,count(DISTINCT category||char(0)||balance_key)>1 "
            "AND sum(amount)=0 internal FROM money GROUP BY event_id) "
            "SELECT m.event_id,m.number,m.calculation_id,m.kind,m.sign,m.category,m.balance_key,"
            "m.sign*m.amount signed_amount,json_extract(m.outcome,'$.values.actual_date') "
            "actual_date,"
            "printf('%012d:%s:%06d',m.number,m.calculation_id,m.local_index) page_key,"
            "(m.kind IN ('funds_transfer','cash_bank_transfer','bank_platform_transfer') AND "
            "coalesce(json_extract(m.outcome,'$.values.accounting_treatment'),'')"
            "!='reserve_expense' "
            "AND t.internal) internal_transfer FROM money m JOIN transfers t USING(event_id)"
        )
        return source, parameters

    def base_account(self, category, ident):
        return self.account_rows.setdefault(
            (category, ident),
            {
                "account_id": ident,
                "type": FUND_TYPES[category],
                "opening_fen": 0,
                "inflow_fen": 0,
                "outflow_fen": 0,
                "net_change_fen": 0,
                "closing_fen": 0,
                "movement_count": 0,
                "last_activity_date": None,
                "negative_balance": False,
            },
        )

    def base_product(self, ident):
        return self.product_rows.setdefault(
            ident,
            {
                "fund_id": ident,
                "opening_cost_fen": 0,
                "subscription_cost_fen": 0,
                "redemption_cost_fen": 0,
                "closing_cost_fen": 0,
                "investment_income_fen": 0,
            },
        )

    def account_summary(self):
        source, parameters = self.events()
        for row in self.connection.execute(
            source
            + "SELECT category,balance_key,sum(CASE WHEN period<? OR opening THEN sign*amount "
            "ELSE 0 END) opening_fen FROM effects WHERE category IN ('bank','cash','platform') "
            "GROUP BY category,balance_key",
            [*parameters, self.snap.month],
        ):
            self.base_account(row["category"], row["balance_key"])["opening_fen"] = row[
                "opening_fen"
            ]
        source, parameters = self.movements()
        for row in self.connection.execute(
            "SELECT category,balance_key,sum(max(signed_amount,0)) inflow_fen,"
            "sum(max(-signed_amount,0)) outflow_fen,count(*) movement_count,"
            f"max(actual_date) last_activity_date FROM ({source}) GROUP BY category,balance_key",
            parameters,
        ):
            self.base_account(row["category"], row["balance_key"]).update(
                {
                    key: row[key]
                    for key in ("inflow_fen", "outflow_fen", "movement_count", "last_activity_date")
                }
            )
        # Scalar established starts and statements keep explicitly opened zero accounts.
        for row in self.connection.execute(
            "SELECT c.kind,json_extract(c.outcome,'$.values.bank_account_id') bank_id,"
            "json_extract(c.outcome,'$.values.cash_account_id') cash_id FROM json_each(?) ids "
            "JOIN calculation c ON c.id=ids.value",
            (canonical(sorted(self.states)),),
        ):
            if row["bank_id"] is not None:
                self.base_account("bank", row["bank_id"])
            if row["cash_id"] is not None:
                self.base_account("cash", row["cash_id"])
        for row in self.connection.execute(
            "SELECT json_extract(m.value,'$.kind') "
            "kind,json_extract(m.value,'$.values.bank_account_id') "
            "bank_id,json_extract(m.value,'$.values.cash_account_id') cash_id FROM json_each(?) "
            "ids "
            "JOIN calculation c ON c.id=ids.value,json_each(c.outcome,'$.values.members') m",
            (canonical(self.opening_ids),),
        ):
            if row["kind"] == "opening_bank":
                self.base_account("bank", row["bank_id"])
            elif row["kind"] == "opening_cash":
                self.base_account("cash", row["cash_id"])
        # An uncertain opening contributes identities, never invented zero amounts.
        unknown = [
            candidate["calculation_id"]
            for issue in self.issues
            for candidate in issue["candidates"]
            if candidate["kind"] == "opening_package"
        ]
        for row in self.connection.execute(
            "SELECT DISTINCT json_extract(b.value,'$.category') category,"
            "json_extract(b.value,'$.key') balance_key FROM json_each(?) ids JOIN calculation c "
            "ON c.id=ids.value,json_each(c.outcome,'$.balances') b WHERE "
            "json_extract(b.value,'$.category') IN ('bank','cash','platform')",
            (canonical(unknown),),
        ):
            self.base_account(row["category"], row["balance_key"])["opening_fen"] = None
        for index, (key, item) in enumerate(sorted(self.account_rows.items()), 1):
            item["fallback_code"] = f"账户 {index}"
            item["net_change_fen"] = item["inflow_fen"] - item["outflow_fen"]
            item["closing_fen"] = (
                item["opening_fen"] + item["net_change_fen"]
                if item["opening_fen"] is not None
                else None
            )
            item["negative_balance"] = item["closing_fen"] is not None and item["closing_fen"] < 0
            item["statement"] = {
                "inflow_fen": None,
                "outflow_fen": None,
                "transaction_count": 0,
                "matched_count": 0,
                "unmatched_count": 0,
                "needs_review_count": 0,
                "coverage_state": "missing" if key[0] == "bank" else "not_applicable",
                "last_activity_date": None,
            }
            item["reconciliation"] = {
                "state": "pending" if key[0] == "bank" else "not_applicable",
                "label": "本月尚未完成银行对账" if key[0] == "bank" else "不适用银行对账",
            }
        return dict(
            self.connection.execute(
                "SELECT coalesce(sum(CASE WHEN NOT internal_transfer THEN max(signed_amount,0) "
                "ELSE 0 END),0) "
                "inflow_fen,coalesce(sum(CASE WHEN NOT internal_transfer THEN "
                "max(-signed_amount,0) ELSE 0 END),0) "
                "outflow_fen,coalesce(sum(CASE WHEN internal_transfer THEN "
                "max(-signed_amount,0) ELSE 0 END),0) "
                f"internal_transfer_fen,count(*) movement_count FROM ({source})",
                parameters,
            ).fetchone()
        )

    def profile(self, kind, ident):
        key = kind, ident
        if key not in self.profiles:
            self.profiles[key] = self.snap.profile(kind, ident)
        return self.profiles[key]

    def account_display(self, category, ident):
        from .dashboard import _display_sources

        item = self.account_rows.get((category, ident))
        profile = self.profile("fund_account", ident)
        return {
            "code": profile.get("display_number")
            or (item["fallback_code"] if item else "未确认账户"),
            "name": profile.get("display_name") or "未提供账户名称",
            "active": profile.get("active"),
            "field_sources": _display_sources(
                profile, code="display_number", name="display_name", active="active"
            ),
        }

    def account_item(self, key):
        item = dict(self.account_rows[key])
        item.pop("fallback_code")
        display = self.account_display(*key)
        item.update(display)
        item["statement"] = item["statement"] | {
            "account_code": display["code"],
            "account_name": display["name"],
        }
        return item

    def movement_item(self, row):
        from .dashboard import _name

        calc = self.snap.calculation(row["calculation_id"])
        data = calc["fact"]["data"]
        account = self.account_display(row["category"], row["balance_key"])
        parties = {
            data[field]
            for field in ("counterparty_id", "owner_id", "lender_id", "payer_id", "recipient_id")
            if data.get(field) and data[field] != "payroll-group"
        }
        parties.update(
            item["party_id"]
            for item in self.snap.voucher_relations(calc, row["sign"])
            if item["party_id"]
        )
        if data.get("payment_method") == "bank_batch":
            parties.discard(data.get("counterparty_id"))
        parties.update(
            item["recipient_id"] for item in data.get("allocations", ()) if item.get("recipient_id")
        )
        short, label, sources = self.snap.business_summary(calc, row["sign"], include_sources=True)
        amount = row["signed_amount"]
        return {
            "id": row["page_key"],
            "date": data.get("actual_date"),
            "account_id": row["balance_key"],
            "account_code": account["code"],
            "account_name": account["name"],
            "account_type": FUND_TYPES[row["category"]],
            "direction": "inflow" if amount > 0 else "outflow",
            "amount_fen": abs(amount),
            "signed_amount_fen": amount,
            "reference": str(row["number"]),
            "calculation_id": row["calculation_id"],
            "type": _name(row["kind"]),
            "summary": label,
            "display_summary": label,
            "list_summary": short,
            "field_sources": sources
            | {
                "account_" + key: value
                for key, value in account["field_sources"].items()
                if key in {"code", "name"}
            },
            "party_sources": [
                {"party_id": ident, **self.snap.party_details(ident)} for ident in sorted(parties)
            ],
            "party": "、".join(dict.fromkeys(self.snap.party(ident) for ident in sorted(parties)))
            if parties
            else "公司账户内部划转"
            if row["internal_transfer"]
            else "未提供往来对象",
            "internal_transfer": bool(row["internal_transfer"]),
            "component_kinds": [row["kind"]],
        }

    def _closed_statement_parent(
        self, statement, reconciliation, result, statement_results, parents
    ):
        """A frozen bank reconciliation can prove its exact statement source only.

        The independent adoption comes from this response's shared selection.
        Persistent metadata/edges alone never establish that adoption.
        """
        selected = self.state_selections[result["id"]]
        if (
            selected["selection_source"] != "close_manifest"
            or selected["posting_period"] != self.snap.period
        ):
            return None, "unestablished", "对账结果在所选关账中的独立采用尚不能证明。"
        manifest = self.snap.closes.get(YearMonth(selected["posting_period"]).ordinal)
        members = set(manifest.get("calculations", ())) if manifest else set()
        sources = [item for item in parents if item["kind"] == "bank_statement"]
        if len(sources) != 1:
            return (
                None,
                "conflict" if sources else "unestablished",
                "对账采用的精确流水计算来源尚不能唯一确认。",
            )
        source = sources[0]
        if result["id"] not in members or source["id"] not in members:
            return None, "unestablished", "对账与流水来源未能在同一次关账采用记录中共同证明。"
        if (
            source["subject_id"] != statement["subject_id"]
            or source["fact_id"] != statement["revision_id"]
            or source["period"] != self.snap.period
            or statement["fact_period"] != self.snap.month
            or reconciliation["fact_period"] != self.snap.month
            or reconciliation["statement_id"] != source["subject_id"]
            or reconciliation["bank_account_id"] != statement["bank_account_id"]
            or result["period"] != self.snap.period
        ):
            return None, "conflict", "对账采用的流水来源与当前展示的版本、账户或期间不一致。"
        if statement_results and (
            len(statement_results) != 1 or statement_results[0]["id"] != source["id"]
        ):
            return None, "conflict", "独立采用的流水计算与对账采用的流水计算不一致。"
        return source, None, None

    def bank_summary(self):
        statement_ids = self.snap.fact_ids_of_kind("bank_statement", period=self.snap.period)
        reconciliation_ids = self.snap.fact_ids_of_kind(
            "bank_reconciliation", period=self.snap.period
        )
        statements = (
            [
                dict(row)
                for row in self.connection.execute(
                    "SELECT f.subject_id,f.revision,f.period fact_period,s.* "
                    "FROM json_each(?) ids JOIN "
                    "fact_bank_statement s "
                    "ON s.revision_id=ids.value JOIN fact_revision f ON f.id=s.revision_id "
                    "ORDER BY f.subject_id",
                    (canonical(statement_ids),),
                )
            ]
            if statement_ids
            else []
        )
        reconciliations = (
            [
                dict(row)
                for row in self.connection.execute(
                    "SELECT f.subject_id,f.revision,f.period fact_period,r.* "
                    "FROM json_each(?) ids JOIN "
                    "fact_bank_reconciliation r "
                    "ON r.revision_id=ids.value JOIN fact_revision f ON f.id=r.revision_id",
                    (canonical(reconciliation_ids),),
                )
            ]
            if reconciliation_ids
            else []
        )
        pending = (
            {
                row[0]
                for row in self.connection.execute(
                    "SELECT DISTINCT subject_id FROM pending WHERE subject_id IN (SELECT "
                    "f.subject_id "
                    "FROM json_each(?) ids JOIN fact_revision f ON f.id=ids.value)",
                    (canonical(statement_ids + reconciliation_ids),),
                )
            }
            if not self.snap.close
            else set()
        )
        states = {}
        for item in self.states.values():
            states.setdefault(item["subject_id"], []).append(item)
        statement_subjects = {item["subject_id"] for item in statements}
        month_results = {
            result["id"]
            for reconciliation in reconciliations
            if reconciliation["statement_id"] in statement_subjects
            for result in states.get(reconciliation["subject_id"], ())
            if result["period"] == self.snap.period
            and result["fact_id"] == reconciliation["revision_id"]
        }
        balanced = {
            row[0]
            for row in self.connection.execute(
                "SELECT c.id FROM json_each(?) ids JOIN calculation c ON c.id=ids.value "
                "WHERE json_extract(c.outcome,'$.values.balanced')=1",
                (canonical(sorted(month_results)),),
            )
        }
        parents_by_result = {}
        if self.snap.close and balanced:
            self.snap.reads.prime_parents(balanced)
            parent_ids = {ident: self.snap.reads.parents(ident) for ident in balanced}
            metadata = self.snap.reads.metadata(
                {parent for identifiers in parent_ids.values() for parent in identifiers}
            )
            parents_by_result = {
                ident: [metadata[parent] for parent in identifiers]
                for ident, identifiers in parent_ids.items()
            }
        provided, confirmed_accounts, headers = set(), set(), []
        self.bank_source_checks = {}
        for statement in statements:
            ident = statement["bank_account_id"]
            provided.add(ident)
            matches = [
                item for item in reconciliations if item["statement_id"] == statement["subject_id"]
            ]
            reconciliation = matches[0] if len(matches) == 1 else None
            results = states.get(reconciliation["subject_id"], []) if reconciliation else []
            result = results[0] if len(results) == 1 else None
            statement_results = states.get(statement["subject_id"], [])
            statement_result = statement_results[0] if len(statement_results) == 1 else None
            unique_statement = sum(item["bank_account_id"] == ident for item in statements) == 1
            unique_reconciliation = (
                sum(item["bank_account_id"] == ident for item in reconciliations) <= 1
            )
            confirmed = bool(
                statement_result
                and statement_result["fact_id"] == statement["revision_id"]
                and statement["subject_id"] not in pending
                and unique_statement
            )
            adopted_reconciliation = bool(
                result
                and result["fact_id"] == reconciliation["revision_id"]
                and result["period"] == self.snap.period
                and reconciliation["bank_account_id"] == ident
                and unique_reconciliation
                and reconciliation["subject_id"] not in pending
                and result["id"] in balanced
            )
            source = statement_result if confirmed else None
            source_error = source_error_state = None
            if self.snap.close and adopted_reconciliation:
                source, source_error_state, source_error = self._closed_statement_parent(
                    statement,
                    reconciliation,
                    result,
                    statement_results,
                    parents_by_result[result["id"]],
                )
                # A conflicting independently selected source cannot be bypassed
                # by falling back to the reconciliation's dependency.
                if source and unique_statement:
                    confirmed = True
            valid = bool(
                confirmed
                and adopted_reconciliation
                and (not self.snap.close or source is not None and source_error is None)
            )
            review = bool(
                not confirmed
                or not unique_reconciliation
                or len(matches) > 1
                or (reconciliation and not valid)
            )
            selection = self.state_selections[result["id"]] if result else None
            source_check = {
                "state": "confirmed" if confirmed else "unestablished",
                "message": "流水来源已确认。",
                "statement_confirmed": confirmed,
                "reconciliation_valid": valid,
                "statement_calculation_id": source["id"] if source else None,
                "selected_statement_calculation_ids": [item["id"] for item in statement_results],
                "statement_fact_id": statement["revision_id"],
                "reconciliation_calculation_id": result["id"] if result else None,
                "reconciliation_fact_id": reconciliation["revision_id"] if reconciliation else None,
                "selection_source": selection["selection_source"] if selection else None,
                "selection_proof": selection["selection_proof"] if selection else None,
                "proof_method": (
                    "frozen_reconciliation_direct_statement"
                    if self.snap.close and valid
                    else "independent_statement_selection"
                    if confirmed
                    else None
                ),
            }
            if (
                not unique_statement
                or not unique_reconciliation
                or len(matches) > 1
                or len(results) > 1
            ):
                source_check.update(
                    state="conflict", message="同一账户的流水或对账采用关系不唯一，需核对来源。"
                )
            elif source_error:
                source_check.update(state=source_error_state, message=source_error)
            elif not confirmed:
                source_check.update(
                    state="unestablished" if self.snap.close else "needs_review",
                    message="历史流水来源的采用尚不能证明，匹配状态待核对。"
                    if self.snap.close
                    else "当前流水资料尚待确认或复核，匹配状态待核对。",
                )
            elif valid:
                source_check["message"] = (
                    "所选关账对账已采用此精确流水来源，流水匹配已确认。"
                    if self.snap.close
                    else "当前流水来源与银行对账匹配已确认。"
                )
            elif reconciliation:
                source_check.update(
                    state="needs_review",
                    message="流水来源已确认；对应对账的采用或有效性尚需核对。",
                )
            else:
                source_check["message"] = "流水来源已确认，尚无本期已确认的银行对账匹配。"
            self.bank_source_checks[statement["revision_id"]] = source_check
            if confirmed:
                confirmed_accounts.add(ident)
            headers.append(
                {
                    "fact_id": statement["revision_id"],
                    "subject_id": statement["subject_id"],
                    "account_id": ident,
                    "reconciliation_id": reconciliation["revision_id"] if reconciliation else None,
                    "confirmed": confirmed,
                    "valid": valid,
                    "review": review,
                }
            )
            item = self.account_rows.get(("bank", ident))
            if item is not None:
                item["statement"]["coverage_state"] = "complete" if confirmed else "partial"
                item["reconciliation"] = {
                    "state": "complete" if valid else "attention",
                    "label": "已完成银行对账" if valid else "流水匹配状态待核对",
                    "source_check": source_check,
                    "version": reconciliation["revision"] if reconciliation else None,
                    "statement_closing_fen": statement["closing_fen"],
                    "book_closing_fen": item["closing_fen"],
                    "difference_fen": statement["closing_fen"] - item["closing_fen"]
                    if item["closing_fen"] is not None
                    else None,
                }
        self.bank_parameters = [canonical(headers)]
        self.bank_source = (
            "SELECT printf('%s:%012d',json_extract(h.value,'$.subject_id'),e.item_no) page_key,"
            "json_extract(h.value,'$.account_id') account_id,e.*,CASE WHEN "
            "json_extract(h.value,'$.valid') "
            "AND EXISTS(SELECT 1 FROM fact_bank_reconciliation_matches m WHERE m.revision_id="
            "json_extract(h.value,'$.reconciliation_id') AND m.reference=e.reference) THEN "
            "'matched' "
            "WHEN json_extract(h.value,'$.review') THEN 'needs_review' ELSE 'unmatched' END state "
            "FROM json_each(?) h JOIN fact_bank_statement_entries e ON "
            "e.revision_id=json_extract(h.value,'$.fact_id')"
        )
        if not headers:
            self.bank_parameters = []
            self.bank_source = (
                "SELECT NULL page_key,NULL account_id,NULL actual_date,NULL signed_fen,"
                "NULL description,NULL state WHERE 0"
            )
        sums = (
            "count(*) transaction_count,coalesce(sum(max(signed_fen,0)),0) inflow_fen,"
            "coalesce(sum(max(-signed_fen,0)),0) outflow_fen,coalesce(sum(state='matched'),0) "
            "matched_count,"
            "coalesce(sum(state='unmatched'),0) "
            "unmatched_count,coalesce(sum(state='needs_review'),0) needs_review_count"
        )
        totals = dict(
            self.connection.execute(
                f"SELECT {sums} FROM ({self.bank_source})", self.bank_parameters
            ).fetchone()
        )
        for row in self.connection.execute(
            f"SELECT account_id,{sums},max(actual_date) last_activity_date FROM "
            f"({self.bank_source}) GROUP BY account_id",
            self.bank_parameters,
        ):
            item = self.account_rows.get(("bank", row["account_id"]))
            if item is not None:
                item["statement"].update(
                    {key: row[key] for key in row.keys() if key != "account_id"}
                )
                item["reconciliation"].update(
                    {key: row[key] for key in ("unmatched_count", "needs_review_count")}
                )
        expected = {ident for category, ident in self.account_rows if category == "bank"} | provided
        coverage = (
            "not_applicable"
            if not expected
            else "missing"
            if not provided
            else "complete"
            if confirmed_accounts == expected
            else "partial"
        )
        if coverage in {"missing", "not_applicable"}:
            totals["inflow_fen"] = totals["outflow_fen"] = None
        unmatched = dict(
            self.connection.execute(
                "SELECT count(*) count,coalesce(sum(max(signed_fen,0)),0) inflow_fen,"
                f"coalesce(sum(max(-signed_fen,0)),0) outflow_fen FROM ({self.bank_source}) "
                f"WHERE state!='matched'",
                self.bank_parameters,
            ).fetchone()
        )
        return totals | {
            "rows": [],
            "unmatched_totals": unmatched,
            "coverage_state": coverage,
            "statement_count": len(statements),
            "expected_account_count": len(expected),
            "provided_account_count": len(provided),
            "missing_account_count": len(expected - provided),
        }

    def bank_item(self, row):
        from .dashboard import _display_sources

        account = self.account_display("bank", row["account_id"])
        profile = self.profile("fund_account", row["account_id"])
        amount = row["signed_fen"]
        return {
            "id": row["page_key"],
            "date": row["actual_date"],
            "account_id": row["account_id"],
            "account_code": account["code"],
            "account_name": account["name"],
            "field_sources": _display_sources(
                profile, account_code="display_number", account_name="display_name"
            ),
            "direction": "inflow" if amount > 0 else "outflow",
            "amount_fen": abs(amount),
            "signed_amount_fen": amount,
            "party": "未提供",
            "memo": row["description"] or "",
            "state": row["state"],
            "source_check": self.bank_source_checks[row["revision_id"]],
        }

    def investment_source(self, *, current=False):
        source, parameters = self.events(current=current, accounts=None if current else {"1101"})
        source = source.rstrip() + (
            ", investments AS (SELECT *,json_extract(outcome,'$.values.fund_id') fund_id FROM "
            "events "
            "WHERE kind IN ('money_fund_subscription','money_fund_redemption')), lots AS ("
            "SELECT e.event_id,'money-fund-cost:'||json_extract(m.value,'$.subject_id') "
            "balance_key,"
            "json_extract(m.value,'$.values.fund_id') fund_id FROM events e,"
            "json_each(e.outcome,'$.values.members') m "
            "WHERE e.opening AND json_extract(m.value,'$.kind')='opening_money_fund') "
        )
        return source, parameters

    def investment_events(self):
        source, parameters = self.investment_source(current=True)
        query = source + (
            "SELECT printf('%012d:%s:confirmation',number,calculation_id) "
            "page_key,number,sign,kind,fund_id,"
            "json_extract(outcome,'$.values.confirmation_date') "
            "actual_date,sign*json_extract(outcome,'$.values.cost_fen') cost_fen,"
            "sign*json_extract(outcome,'$.values.net_proceeds_fen') net_proceeds_fen,"
            "CASE WHEN kind='money_fund_redemption' THEN "
            "sign*coalesce(json_extract(outcome,'$.values.investment_income_fen'),0) END "
            "investment_income_fen,NULL settlement_fen,0 settlement FROM investments UNION ALL "
            "SELECT printf('%012d:%s:settlement:%06d',e.number,e.calculation_id,CAST(s.key AS "
            "INTEGER)),e.number,e.sign,c.kind,"
            "json_extract(c.outcome,'$.values.fund_id'),json_extract(e.outcome,'$.values.actual_date'),NULL,NULL,NULL,"
            "e.sign*json_extract(s.value,'$.amount_fen'),1 FROM events "
            "e,json_each(e.outcome,'$.values.settlements') s "
            "JOIN calculation c ON c.id=json_extract(s.value,'$.source_calculation') "
            "WHERE c.kind IN ('money_fund_subscription','money_fund_redemption') AND "
            "EXISTS(SELECT 1 FROM effects b "
            "WHERE b.event_id=e.event_id AND b.category IN ('bank','cash','platform'))"
        )
        return query, parameters

    def investment_summary(self):
        source, parameters = self.investment_source()
        for row in self.connection.execute(
            source
            + "SELECT coalesce(l.fund_id,json_extract(e.outcome,'$.values.fund_id')) fund_id,"
            "sum(e.sign*e.amount) closing_cost_fen,"
            "sum(CASE WHEN e.period<? OR e.opening THEN e.sign*e.amount ELSE 0 END) "
            "opening_cost_fen,"
            "sum(CASE WHEN e.period=? AND NOT e.opening AND e.kind='money_fund_subscription' "
            "THEN e.sign*e.amount ELSE 0 END) subscription_cost_fen,"
            "sum(CASE WHEN e.period=? AND NOT e.opening AND e.kind='money_fund_redemption' THEN "
            "-e.sign*e.amount ELSE 0 END) redemption_cost_fen "
            "FROM effects e LEFT JOIN lots l USING(event_id,balance_key) "
            "WHERE e.category='short_term_investment' "
            "GROUP BY coalesce(l.fund_id,json_extract(e.outcome,'$.values.fund_id'))",
            [*parameters, self.snap.month, self.snap.month, self.snap.month],
        ):
            if row["fund_id"] is None:
                raise KernelError("dashboard_investment_source_missing", "投资余额缺少正式成本来源")
            self.base_product(row["fund_id"]).update(dict(row))
        unknown = [
            candidate["calculation_id"]
            for issue in self.issues
            for candidate in issue["candidates"]
            if candidate["kind"] == "opening_package"
        ]
        for row in self.connection.execute(
            "SELECT DISTINCT json_extract(m.value,'$.values.fund_id') fund_id FROM json_each(?) "
            "ids JOIN calculation c "
            "ON c.id=ids.value,json_each(c.outcome,'$.values.members') m WHERE "
            "json_extract(m.value,'$.kind')='opening_money_fund'",
            (canonical(unknown),),
        ):
            self.base_product(row["fund_id"]).update(opening_cost_fen=None, closing_cost_fen=None)
        source, parameters = self.investment_events()
        for row in self.connection.execute(
            "SELECT fund_id,coalesce(sum(investment_income_fen),0) investment_income_fen "
            f"FROM ({source}) GROUP BY fund_id",
            parameters,
        ):
            self.base_product(row["fund_id"])["investment_income_fen"] = row[
                "investment_income_fen"
            ]
        totals = {
            key: _sum(self.product_rows.values(), key)
            for key in (
                "opening_cost_fen",
                "subscription_cost_fen",
                "redemption_cost_fen",
                "closing_cost_fen",
                "investment_income_fen",
            )
        }
        totals.update(
            dict(
                self.connection.execute(
                    "SELECT count(*) event_count,coalesce(sum(CASE WHEN "
                    "kind='money_fund_subscription' THEN settlement_fen ELSE 0 END),0) "
                    "actual_payments_fen,coalesce(sum(CASE WHEN kind='money_fund_redemption' "
                    "THEN settlement_fen ELSE 0 END),0) "
                    f"actual_receipts_fen FROM ({source})",
                    parameters,
                ).fetchone()
            )
        )
        return totals | {"products": [], "events": []}

    def product_display(self, ident):
        from .dashboard import _display_sources

        profile = self.profile("asset", ident)
        return {
            "name": profile.get("display_name") or "未提供基金名称",
            "field_sources": _display_sources(profile, name="display_name"),
        }

    def investment_item(self, row):
        purchase = row["kind"] == "money_fund_subscription"
        label = (
            ("申购实际付款" if purchase else "赎回实际到账")
            if row["settlement"]
            else ("申购确认" if purchase else "赎回确认")
        )
        return {
            "id": row["page_key"],
            "date": row["actual_date"],
            "period": self.snap.period,
            "fund_id": row["fund_id"],
            "name": self.product_display(row["fund_id"])["name"],
            "type": ("冲正：" if row["sign"] < 0 else "") + label,
            "reference": str(row["number"]),
            **{
                key: row[key]
                for key in (
                    "cost_fen",
                    "net_proceeds_fen",
                    "investment_income_fen",
                    "settlement_fen",
                )
            },
        }


def funds(snap, *, sections=None, cursors=None, limit=100, filters=None, summary_only=False):
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("每页数量必须为 1 至 500")
    sections = set() if summary_only else SECTIONS if sections is None else set(sections)
    if sections - SECTIONS:
        raise ValueError("未知资金明细集合")
    cursors, filters = cursors or {}, filters or {}
    read = FundsRead(snap)
    movement_totals = read.account_summary()
    bank = read.bank_summary()
    investments = read.investment_summary()
    accounts = list(read.account_rows.values())
    totals = {key: _sum(accounts, key) for key in ("opening_fen", "net_change_fen")}
    data = (
        totals
        | movement_totals
        | {
            "total_fen": _sum(accounts, "closing_fen"),
            **{
                FUND_TYPES[category] + "_fen": _sum(
                    [item for item in accounts if item["type"] == FUND_TYPES[category]],
                    "closing_fen",
                )
                for category in FUND_TYPES
            },
            "account_count": len(accounts),
            **{
                FUND_TYPES[category] + "_account_count": sum(
                    item["type"] == FUND_TYPES[category] for item in accounts
                )
                for category in FUND_TYPES
            },
            "attention_account_count": sum(
                item["negative_balance"]
                or item["closing_fen"] is None
                or item["reconciliation"]["state"] in {"attention", "pending"}
                for item in accounts
            ),
            "accounts": [],
            "movements": [],
            "investments": investments,
            "bank_statement": bank,
            "collections": {},
            "fact_issues": read.issues,
        }
    )
    for section in sorted(sections):
        after = cursors.get(section)
        if section in {"accounts", "investment_products"}:
            mapping = (
                {
                    category + ":" + ident: (category, ident)
                    for category, ident in sorted(read.account_rows)
                }
                if section == "accounts"
                else {ident: ident for ident in sorted(read.product_rows)}
            )
            keys, page = page_keys(mapping, after, limit)
            items = (
                [read.account_item(mapping[key]) for key in keys]
                if section == "accounts"
                else [read.product_rows[key] | read.product_display(key) for key in keys]
            )
        else:
            where, filter_values = "1=1", []
            if section == "movements":
                source, parameters = read.movements()
                if filters.get("movement_account_type"):
                    category = next(
                        (
                            key
                            for key, value in FUND_TYPES.items()
                            if value == filters["movement_account_type"]
                        ),
                        "",
                    )
                    where += " AND category=? AND balance_key=?"
                    filter_values.extend((category, filters.get("movement_account_id")))
                present = read.movement_item
            elif section == "statements":
                source, parameters = read.bank_source, read.bank_parameters
                if filters.get("statement_account_id"):
                    where += " AND account_id=?"
                    filter_values.append(filters["statement_account_id"])
                present = read.bank_item
            else:
                source, parameters = read.investment_events()
                present = read.investment_item
            rows, page = _sql_page(
                snap.connection,
                source,
                parameters,
                after=after,
                limit=limit,
                where=where,
                filters=filter_values,
            )
            if section == "movements":
                snap.reads.prime_calculations(
                    {row["calculation_id"] for row in rows}, ancestors=True
                )
            items = [present(row) for row in rows]
        data["collections"][section] = {"items": items, "page": page}
        if section == "accounts":
            data["accounts"] = items
        elif section == "movements":
            data["movements"] = items
        elif section == "statements":
            bank["rows"] = items
        elif section == "investment_products":
            investments["products"] = items
        else:
            investments["events"] = items
    return data
