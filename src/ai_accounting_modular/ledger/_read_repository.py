"""Narrow directory SQL; payloads are read only after selecting page identities."""

from ai_accounting_modular.contracts.public import canonical_json

from ._read_selection import business_kind, selected

HEAD_FIELDS = ",".join(
    "r." + key
    for key in (
        "result_id",
        "business_id",
        "result_slot",
        "generation",
        "business_period",
        "posting_period",
        "state",
        "target_state",
        "baseline_result_id",
        "calculation_key",
        "calculation_revision",
        "basis_digest",
        "target_digest",
    )
)


def _in(column, values, clauses, args):
    if values:
        clauses.append(f"{column} IN (SELECT value FROM json_each(?))")
        args.append(canonical_json(list(values)))


def _resource_keys(business, slot, business_ids, references):
    clauses, args = [], []
    _in(business, business_ids, clauses, args)
    if references:
        clauses.append(
            f"({business},{slot}) IN (SELECT json_extract(value,'$.business_id'),"
            "json_extract(value,'$.slot') FROM json_each(?))"
        )
        args.append(canonical_json(list(references)))
    return "(" + " OR ".join(clauses) + ")" if clauses else "1", tuple(args)


def _balance_keys(keys):
    return canonical_json(
        [{"account_code": key.account_code, "object_id": key.object_id} for key in keys]
    )


_BALANCE_MATCH = (
    "l.account_code=json_extract(wanted.value,'$.account_code') "
    "AND (json_extract(wanted.value,'$.object_id') IS NULL "
    "OR l.object_id=json_extract(wanted.value,'$.object_id'))"
)


class ReadRepository:
    def read_balance_heads(self, keys, cutoff, *, view):
        predicate, args = selected("r", cutoff if view == "as_posted" else None)
        # Search every historical journal: a later correction can introduce an
        # account into an earlier business month, or inherit a frozen journal.
        sql = (
            "WITH candidates AS (SELECT DISTINCT old.business_id,old.result_slot "
            "FROM json_each(?) wanted JOIN voucher_line l ON " + _BALANCE_MATCH + " "
            "JOIN voucher v ON v.voucher_id=l.voucher_id "
            "JOIN adopted_result old ON old.result_id=v.result_id) "
            f"SELECT {HEAD_FIELDS} FROM candidates c JOIN adopted_result r "
            "ON r.business_id=c.business_id AND r.result_slot=c.result_slot "
            f"WHERE {predicate} AND {business_kind()} "
            "NOT IN ('opening.package','reports.continuation')"
        )
        parameters = [_balance_keys(keys), *args]
        if view == "current":
            sql += " AND r.business_period<=?"
            parameters.append(cutoff)
        return [dict(row) for row in self._session.fetchall(sql, tuple(parameters))]

    def read_balance_lines(self, keys, voucher_ids):
        sql = (
            "SELECT DISTINCT l.* FROM json_each(?) wanted JOIN voucher_line l ON "
            + _BALANCE_MATCH
            + " WHERE l.voucher_id IN (SELECT value FROM json_each(?)) "
            "ORDER BY l.voucher_id,l.line_no"
        )
        return [
            dict(row)
            for row in self._session.fetchall(
                sql, (_balance_keys(keys), canonical_json(sorted(voucher_ids)))
            )
        ]

    def read_first_recorded(self, keys, *, as_of=None):
        where = " WHERE (l.debit_fen<>0 OR l.credit_fen<>0)"
        args = [_balance_keys(keys)]
        if as_of is not None:
            where += " AND v.period<=?"
            args.append(as_of)
        sql = (
            "WITH ranked AS (SELECT CAST(wanted.key AS INTEGER) query_index,v.result_id,"
            "v.period AS first_period,ROW_NUMBER() OVER(PARTITION BY wanted.key "
            "ORDER BY v.period,v.number,v.voucher_id) position "
            "FROM json_each(?) wanted JOIN voucher_line l ON " + _BALANCE_MATCH + " "
            "JOIN voucher v ON v.voucher_id=l.voucher_id" + where + ") "
            f"SELECT m.query_index,m.first_period,{HEAD_FIELDS} FROM ranked m "
            "JOIN adopted_result r ON r.result_id=m.result_id WHERE m.position=1"
        )
        return [dict(row) for row in self._session.fetchall(sql, tuple(args))]

    def read_heads(self, result_ids):
        return [
            dict(row)
            for row in self._session.fetchall(
                f"SELECT {HEAD_FIELDS},{business_kind()} AS business_kind "
                "FROM adopted_result r WHERE r.result_id IN "
                "(SELECT value FROM json_each(?))",
                (canonical_json(list(result_ids)),),
            )
        ]

    def read_event_scope(self, query, selection):
        parts, parameters = [], []
        for table, event_id, period, component in (
            ("settlement", "x.settlement_id", "x.period", "x.method"),
            ("obligation_reclassification", "x.relation_id", "r.business_period", "x.mode"),
        ):
            predicate, selected_args = selected("r", selection.as_of)
            clauses = [f"{period}>=?", f"{period}<=?", predicate]
            args = [query.period_from, query.period_to, *selected_args]
            _in("r.business_id", query.consumer_business_ids, clauses, args)
            _in(component, query.components, clauses, args)
            if query.source_refs:
                clauses.append(
                    "(x.source_business_id,x.source_public_slot) IN "
                    "(SELECT json_extract(value,'$.business_id'),json_extract(value,'$.slot') "
                    "FROM json_each(?))"
                )
                args.append(canonical_json(list(query.source_refs)))
            if query.source_business_kinds:
                producer_selected, producer_args = selected("producer", selection.as_of)
                clauses.append(
                    "EXISTS(SELECT 1 FROM obligation definition "
                    "JOIN adopted_result producer ON producer.result_id=definition.result_id "
                    "WHERE producer.business_id=x.source_business_id "
                    "AND definition.public_slot=x.source_public_slot "
                    f"AND {producer_selected} AND {business_kind('producer')} "
                    "IN (SELECT value FROM json_each(?)))"
                )
                args.extend((*producer_args, canonical_json(list(query.source_business_kinds))))
            if query.movement_ids:
                if table == "settlement":
                    _in("x.movement_id", query.movement_ids, clauses, args)
                else:
                    clauses.append("0")
            day = "json_extract(r.basis_json,'$.effective_on')"
            joins = ""
            if table == "settlement":
                joins = (
                    "LEFT JOIN result_fund_movement m ON m.result_id=x.result_id "
                    "AND m.movement_id=x.movement_id "
                )
                day = (
                    "CASE WHEN x.method='money' THEN m.actual_on "
                    f"ELSE COALESCE(x.actual_on,{day}) END"
                )
            metadata = ",".join(
                "r." + name
                for name in (
                    "business_id",
                    "result_slot",
                    "generation",
                    "posting_period",
                    "state",
                    "target_state",
                    "baseline_result_id",
                    "calculation_key",
                    "calculation_revision",
                    "basis_digest",
                    "target_digest",
                )
            )
            sql = (
                f"SELECT r.result_id,{metadata},'{table}' AS use_table,"
                f"{period} AS event_period,{event_id} AS event_id,"
                f"{day} AS event_day FROM {table} x JOIN adopted_result r "
                "ON r.result_id=x.result_id " + joins + "WHERE " + " AND ".join(clauses)
            )
            parts.append(sql)
            parameters.extend(args)
        return " UNION ALL ".join(parts), tuple(parameters)

    def read_event_identities(self, sql, args):
        return [
            dict(row)
            for row in self._session.fetchall(
                "SELECT * FROM (" + sql + ") ORDER BY event_period,event_day IS NULL,"
                "COALESCE(event_day,''),use_table,event_id,result_id",
                args,
            )
        ]

    def read_event_page(self, sql, args, *, after=None, limit=20):
        where = ""
        if after is not None:
            where = (
                " WHERE (event_period,event_day IS NULL,COALESCE(event_day,''),"
                "use_table,event_id,result_id)>(?,?,?,?,?,?)"
            )
            args = (*args, *after)
        return [
            dict(row)
            for row in self._session.fetchall(
                "SELECT * FROM (" + sql + ")" + where + " ORDER BY event_period,event_day IS NULL,"
                "COALESCE(event_day,''),use_table,event_id,result_id LIMIT ?",
                (*args, limit),
            )
        ]

    def read_event_payloads(self, headers):
        result = {}
        for table, event_id in (
            ("settlement", "settlement_id"),
            ("obligation_reclassification", "relation_id"),
        ):
            wanted = [
                {"result_id": row["result_id"], "event_id": row["event_id"]}
                for row in headers
                if row["use_table"] == table
            ]
            if not wanted:
                continue
            rows = self._session.fetchall(
                f"SELECT x.* FROM json_each(?) wanted JOIN {table} x "
                "ON x.result_id=json_extract(wanted.value,'$.result_id') "
                f"AND x.{event_id}=json_extract(wanted.value,'$.event_id')",
                (canonical_json(wanted),),
            )
            result.update(((table, row["result_id"], row[event_id]), dict(row)) for row in rows)
        return result

    def read_position_rows(self, kind, cutoff, selection, *, business_ids=(), references=()):
        return self._position_rows(
            kind, cutoff, selection, business_ids=business_ids, references=references
        )

    def read_obligation_directory_rows(
        self, cutoff, selection, *, business_ids=(), references=(), categories=(), direction=None
    ):
        return self._position_rows(
            "obligation",
            cutoff,
            selection,
            business_ids=business_ids,
            references=references,
            directory=True,
            categories=categories,
            direction=direction,
        )

    def has_adoptions_after(self, period):
        return bool(
            self._session.fetchone(
                "SELECT EXISTS(SELECT 1 FROM adopted_result WHERE posting_period>?) AS found",
                (period,),
            )["found"]
        )

    def read_origin_rows(self, result_ids, selections):
        # Plans contain only owner-declared names, never caller JSON paths or SQL.
        def token(owner, path):
            return f"substr(r.{owner}_json -> '{path}',1)"

        cases = []
        for kind, fields in selections:
            tokens = [
                token("target", "$.kind"),
                token("basis", "$.kind"),
                token("basis", "$.adopted_quantities"),
            ]
            tokens.extend(token("basis", "$.input." + field) for field in fields)
            cases.append(f"WHEN '{kind}' THEN json_array(" + ",".join(tokens) + ")")
        fields = HEAD_FIELDS.replace("r.target_state,", "")
        sql = (
            f"SELECT {fields},json_extract(r.target_json,'$.kind') AS business_kind,"
            "CASE json_extract(r.target_json,'$.kind') "
            + " ".join(cases)
            + " END AS projection_json FROM adopted_result r "
            "WHERE r.result_id IN (SELECT value FROM json_each(?))"
        )
        return [
            dict(row) for row in self._session.fetchall(sql, (canonical_json(list(result_ids)),))
        ]

    def _position_rows(
        self,
        kind,
        cutoff,
        selection,
        *,
        business_ids=(),
        references=(),
        directory=False,
        categories=(),
        direction=None,
    ):
        table = "obligation" if kind == "obligation" else "cost_lot"
        predicate, source_args = selected("r", selection.as_of)
        use_predicate, use_args = selected("u", selection.as_of)
        source_keys, source_key_args = _resource_keys(
            "r.business_id", "d.public_slot", business_ids, references
        )
        candidate, candidate_args = "", ()
        if directory and (categories or direction is not None):
            clauses, candidate_values = [], []
            _in("d.category", categories, clauses, candidate_values)
            if direction is not None:
                clauses.append("d.direction=?")
                candidate_values.append(direction)
            candidate = (
                "candidates AS MATERIALIZED (SELECT r.business_id,d.public_slot "
                "FROM obligation d JOIN adopted_result r ON r.result_id=d.result_id "
                f"WHERE r.business_period<=? AND {predicate} AND {source_keys} AND "
                + " AND ".join(clauses)
                + "),"
            )
            candidate_args = (cutoff, *source_args, *source_key_args, *candidate_values)
            source_keys = (
                "(r.business_id,d.public_slot) IN (SELECT business_id,public_slot FROM candidates)"
            )
            source_key_args = ()
        definition_fields = (
            "d.result_id,d.public_slot,d.party_id,d.direction,d.category,d.amount_fen,d.business_period"
            if directory
            else "d.*"
        )
        if kind == "obligation":
            settlement_keys, settlement_args = _resource_keys(
                "s.source_business_id", "s.source_public_slot", business_ids, references
            )
            relation_keys, relation_args = _resource_keys(
                "x.source_business_id", "x.source_public_slot", business_ids, references
            )
            if candidate:
                settlement_keys = (
                    "(s.source_business_id,s.source_public_slot) "
                    "IN (SELECT business_id,public_slot FROM candidates)"
                )
                relation_keys = (
                    "(x.source_business_id,x.source_public_slot) "
                    "IN (SELECT business_id,public_slot FROM candidates)"
                )
                settlement_args = relation_args = ()
            sql = (
                "WITH used AS (SELECT s.source_business_id,s.source_public_slot,"
                "SUM(CASE WHEN s.method='money' THEN s.amount_fen ELSE 0 END) money,"
                "SUM(CASE WHEN s.method='agency' THEN s.amount_fen ELSE 0 END) agency,"
                "SUM(CASE WHEN s.method='offset' THEN s.amount_fen ELSE 0 END) offset "
                "FROM settlement s JOIN adopted_result u ON u.result_id=s.result_id "
                f"WHERE u.business_period<=? AND {use_predicate} AND {settlement_keys} "
                "GROUP BY s.source_business_id,s.source_public_slot),"
                "reclassified AS (SELECT x.source_business_id,x.source_public_slot,"
                "SUM(CASE WHEN x.mode='transfer' THEN x.amount_fen ELSE 0 END) transferred,"
                "SUM(CASE WHEN x.mode='reclassify_excess' THEN x.amount_fen ELSE 0 END) excess "
                "FROM obligation_reclassification x "
                "JOIN adopted_result u ON u.result_id=x.result_id "
                f"WHERE u.business_period<=? AND {use_predicate} AND {relation_keys} "
                "GROUP BY x.source_business_id,x.source_public_slot) "
                f"SELECT {definition_fields},COALESCE(s.money,0) money,COALESCE(s.agency,0) agency,"
                "COALESCE(s.offset,0) offset,COALESCE(x.transferred,0) transferred,"
                "COALESCE(x.excess,0) excess,0 consumed,"
            )
            joins = (
                "LEFT JOIN used s ON s.source_business_id=r.business_id "
                "AND s.source_public_slot=d.public_slot "
                "LEFT JOIN reclassified x ON x.source_business_id=r.business_id "
                "AND x.source_public_slot=d.public_slot "
            )
            args = (cutoff, *use_args, *settlement_args, cutoff, *use_args, *relation_args)
        else:
            consumption_keys, consumption_args = _resource_keys(
                "c.source_business_id", "c.source_public_slot", business_ids, references
            )
            sql = (
                "WITH used AS (SELECT c.source_business_id,c.source_public_slot,"
                "SUM(c.amount_fen) consumed FROM cost_consumption c "
                "JOIN adopted_result u ON u.result_id=c.result_id "
                f"WHERE u.business_period<=? AND {use_predicate} AND {consumption_keys} "
                "GROUP BY c.source_business_id,c.source_public_slot) "
                "SELECT d.*,0 money,0 agency,0 offset,0 transferred,0 excess,"
                "COALESCE(s.consumed,0) consumed,"
            )
            joins = (
                "LEFT JOIN used s ON s.source_business_id=r.business_id "
                "AND s.source_public_slot=d.public_slot "
            )
            args = (cutoff, *use_args, *consumption_args)
        if candidate:
            sql = "WITH " + candidate + sql.removeprefix("WITH ")
            args = (*candidate_args, *args)
        head = ",".join(
            "r." + key
            for key in (
                "business_id",
                "result_slot",
                "generation",
                "posting_period",
                "state",
                "target_state",
                "baseline_result_id",
                "calculation_key",
                "calculation_revision",
                "basis_digest",
                "target_digest",
            )
            if not directory or key != "target_state"
        )
        sql += head + ",r.business_period AS owner_business_period "
        sql += f"FROM {table} d JOIN adopted_result r ON r.result_id=d.result_id " + joins
        sql += f"WHERE r.business_period<=? AND {predicate} AND {source_keys} "
        if not directory:
            sql += "ORDER BY r.business_id,d.public_slot"
        return [
            dict(row)
            for row in self._session.fetchall(sql, (*args, cutoff, *source_args, *source_key_args))
        ]

    def read_result_scope(
        self, scope, kinds, selection, *, flow=False, business_ids=(), result_slots=()
    ):
        first, last = scope["period_from"], scope["period_to"]
        clauses, args = [], []
        if flow and selection.view == "as_posted":
            clauses.extend(("r.posting_period>=?", "r.posting_period<=?"))
            args.extend((first, min(last, selection.as_of)))
        else:
            predicate, selected_args = selected("r", selection.as_of)
            clauses.extend((predicate, "r.business_period<=?"))
            args.extend((*selected_args, last))
            if flow:
                clauses.append("r.business_period>=?")
                args.append(first)
        _in(business_kind(), kinds, clauses, args)
        _in("r.business_id", business_ids, clauses, args)
        _in("r.result_slot", result_slots, clauses, args)
        return "adopted_result r", " AND ".join(clauses), tuple(args)

    def read_identity_rows(self, table, where, args):
        return [
            dict(row)
            for row in self._session.fetchall(
                f"SELECT {HEAD_FIELDS} FROM {table} WHERE {where} "
                "ORDER BY r.business_id,r.result_slot,r.posting_period,r.result_id",
                args,
            )
        ]

    def read_result_page(self, table, where, args, *, after=None, limit=20, flow=False):
        fields = (
            ("r.posting_period", "r.business_id", "r.result_slot", "r.result_id")
            if flow
            else ("r.business_id", "r.result_slot", "r.result_id")
        )
        if after is not None:
            where += " AND (" + ",".join(fields) + ")>(" + ",".join("?" for _ in fields) + ")"
            args = (*args, *after)
        return [
            dict(row)
            for row in self._session.fetchall(
                f"SELECT {HEAD_FIELDS},{business_kind()} AS business_kind FROM {table} "
                f"WHERE {where} ORDER BY " + ",".join(fields) + " LIMIT ?",
                (*args, limit),
            )
        ]

    def read_voucher_scope(self, scope, kinds, selected_view, business_ids=(), roles=()):
        clauses = ["v.period>=?", "v.period<=?"]
        args = [scope["period_from"], scope["period_to"]]
        if selected_view.as_of is not None:
            clauses.append("v.period<=?")
            args.append(selected_view.as_of)
        _in("r.business_id", business_ids, clauses, args)
        _in(business_kind(), kinds, clauses, args)
        _in("v.role", roles, clauses, args)
        return (
            "voucher v JOIN adopted_result r ON r.result_id=v.result_id",
            " AND ".join(clauses),
            tuple(args),
        )

    def read_voucher_identities(self, table, where, args):
        return [
            dict(row)
            for row in self._session.fetchall(
                f"SELECT v.*,r.generation,r.basis_digest,r.target_digest FROM {table} "
                f"WHERE {where} ORDER BY v.period,v.number,v.voucher_id",
                args,
            )
        ]

    def read_voucher_page(self, table, where, args, *, after=None, limit=20):
        if after is not None:
            where += " AND (v.period,v.number,v.voucher_id)>(?,?,?)"
            args = (*args, *after)
        return [
            dict(row)
            for row in self._session.fetchall(
                f"SELECT v.*,{HEAD_FIELDS} FROM {table} WHERE {where} "
                "ORDER BY v.period,v.number,v.voucher_id LIMIT ?",
                (*args, limit),
            )
        ]

    def read_voucher_headers(self, result_ids):
        return [
            dict(row)
            for row in self._session.fetchall(
                "SELECT * FROM voucher WHERE result_id IN (SELECT value FROM json_each(?))",
                (canonical_json(list(result_ids)),),
            )
        ]

    def read_vouchers(self, voucher_ids):
        return [
            dict(row)
            for row in self._session.fetchall(
                "SELECT * FROM voucher WHERE voucher_id IN (SELECT value FROM json_each(?))",
                (canonical_json(list(voucher_ids)),),
            )
        ]

    def read_voucher_lines(self, voucher_ids):
        return [
            dict(row)
            for row in self._session.fetchall(
                "SELECT * FROM voucher_line WHERE voucher_id IN (SELECT value FROM json_each(?)) "
                "ORDER BY voucher_id,line_no",
                (canonical_json(list(voucher_ids)),),
            )
        ]
