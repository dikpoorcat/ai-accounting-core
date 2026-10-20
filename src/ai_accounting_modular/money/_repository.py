"""Money-owned current facts, normalized source rows and stable occurrences."""

from copy import deepcopy

from ai_accounting_modular.contracts.public import (
    ContractError,
    canonical_json,
    loads_json,
    parse_fen,
    restore_fact_collection,
    split_fact_collection,
    validate_contract,
)

from ._original_rows import ROW_TABLES as _ROW_TABLES
from ._original_rows import load_rows


class MoneyRepository:
    def __init__(self, session):
        self._session = session

    def _facts(self, rows):
        try:
            projected = []
            for row in rows:
                fact = loads_json(row["data_json"])
                kind = fact["kind"]
                expected_schema = kind + ("/storage_projection/1" if kind in _ROW_TABLES else "")
                if (
                    fact["business_id"] != row["business_id"]
                    or type(kind) is not str
                    or not kind.startswith("money.")
                    or row["schema_key"] != expected_schema
                    or fact.get("effective_on") != row["effective_on"]
                    or kind in _ROW_TABLES
                    and row["main_object_id"] != self._main_object(fact)
                ):
                    raise ValueError("stored money fact identity mismatch")
                projected.append(fact)
            collections = {}
            for kind in _ROW_TABLES:
                parents = {f["business_id"]: f for f in projected if f["kind"] == kind}
                collections.update(load_rows(self._session, parents, kind))
            result = []
            for fact in projected:
                kind = fact["kind"]
                if kind in _ROW_TABLES:
                    fact = restore_fact_collection(fact, "rows", collections[fact["business_id"]])
                else:
                    validate_contract("business-facts.schema.json", "Fact", fact)
                result.append(fact)
            return tuple(result)
        except (ContractError, KeyError, TypeError, ValueError) as error:
            raise ContractError("storage_corrupt", "资金当前事实或原行结构不完整。") from error

    def get(self, ids):
        if not ids:
            return ()
        return self._facts(
            self._session.fetchall(
                "SELECT business_id,schema_key,effective_on,main_object_id,data_json "
                "FROM money_fact "
                "WHERE business_id IN (SELECT value FROM json_each(?)) ORDER BY business_id",
                (canonical_json(list(ids)),),
            )
        )

    def query(self, query):
        clauses = ["json_extract(data_json,'$.period')>=?", "json_extract(data_json,'$.period')<=?"]
        args = [query["period_from"], query["period_to"]]
        if "kind" in query:
            clauses.append("json_extract(data_json,'$.kind')=?")
            args.append(query["kind"])
        if "object_id" in query:
            clauses.append("main_object_id=?")
            args.append(query["object_id"])
        if "cursor" in query:
            clauses.append("business_id>?")
            args.append(query["cursor"])
        args.append(query["limit"])
        return self._facts(
            self._session.fetchall(
                "SELECT business_id,schema_key,effective_on,main_object_id,data_json "
                "FROM money_fact WHERE " + " AND ".join(clauses) + " ORDER BY business_id LIMIT ?",
                args,
            )
        )

    @staticmethod
    def _main_object(fact, main_object_id=None):
        data = fact["data"]
        return main_object_id or next(
            (
                data[field]
                for field in ("funds_account_id", "platform_account_id", "from_account_id")
                if field in data
            ),
            None,
        )

    def replace_current(self, desired, main_object_ids):
        """Validate the final set, release changing original rows, then save it."""
        existing_rows = self._session.fetchall(
            "SELECT business_id,schema_key,effective_on,main_object_id,data_json FROM money_fact "
            "WHERE business_id IN (SELECT value FROM json_each(?))",
            (canonical_json(list(desired)),),
        )
        existing = {fact["business_id"]: fact for fact in self._facts(existing_rows)}
        accounts = {row["business_id"]: row["main_object_id"] for row in existing_rows}
        changed = {}
        for business_id, fact in desired.items():
            before = existing.get(business_id)
            if fact is None:
                if before is not None:
                    changed[business_id] = None
            elif canonical_json(before) != canonical_json(fact) or accounts.get(
                business_id
            ) != self._main_object(fact, main_object_ids.get(business_id)):
                changed[business_id] = fact
        releases = {table: set() for table in _ROW_TABLES.values()}
        inserts = {table: [] for table in _ROW_TABLES.values()}
        for business_id, fact in changed.items():
            before = existing.get(business_id)
            for candidate in (before, fact):
                if candidate is not None and candidate["kind"] in _ROW_TABLES:
                    releases[_ROW_TABLES[candidate["kind"]]].add(business_id)
            if fact is not None and fact["kind"] in _ROW_TABLES:
                inserts[_ROW_TABLES[fact["kind"]]].extend(
                    (fact, index, row) for index, row in enumerate(fact["data"]["rows"])
                )
        for table in releases:
            self._check_row_ownership(table, inserts[table], releases[table])
        # No row is inserted while an obsolete owner in this same unit still
        # occupies its identity. Any later SQL failure rolls back the whole unit.
        for table, business_ids in releases.items():
            if business_ids:
                self._session.execute(
                    f"DELETE FROM {table} WHERE business_id IN (SELECT value FROM json_each(?))",
                    (canonical_json(sorted(business_ids)),),
                )
        for business_id, fact in changed.items():
            if fact is None:
                self._session.execute(
                    "DELETE FROM money_reconciliation WHERE business_id=?", (business_id,)
                )
                self._session.execute("DELETE FROM money_fact WHERE business_id=?", (business_id,))
            else:
                self._save_fact(fact, main_object_ids.get(business_id))

    @staticmethod
    def _row_conflict(fact, index, row):
        raise ContractError(
            "dependency_conflict",
            "原始行身份在最终清单中重复，或仍归属于本单未释放的其他清单。",
            fact_issues=[
                {
                    "code": "original_row_conflict",
                    "business_id": fact["business_id"],
                    "field": f"/data/rows/{index}/row_id",
                    "meaning": "原始资料行的稳定身份",
                    "message": "同一原行只能归属于一份有效清单，请明确其最终归属。",
                    "allowed_precision": ["identity"],
                    "reusable_sources": [row["source"]],
                    "blocking_scope": "business",
                }
            ],
        )

    def _check_row_ownership(self, table, inserts, releases):
        by_id = {}
        for fact, index, row in inserts:
            row_id = row["row_id"]
            if row_id in by_id:
                self._row_conflict(fact, index, row)
            by_id[row_id] = fact, index, row
        if not by_id:
            return
        conflicts = self._session.fetchall(
            f"SELECT row_id FROM {table} WHERE row_id IN (SELECT value FROM json_each(?)) "
            "AND business_id NOT IN (SELECT value FROM json_each(?)) ORDER BY row_id",
            (canonical_json(list(by_id)), canonical_json(sorted(releases))),
        )
        if conflicts:
            self._row_conflict(*by_id[conflicts[0]["row_id"]])

    def _save_fact(self, fact, main_object_id=None):
        kind = fact["kind"]
        account = self._main_object(fact, main_object_id)
        schema = kind
        if kind in _ROW_TABLES:
            body, rows = split_fact_collection(fact, "rows")
            schema += "/storage_projection/1"
            self._insert_rows(fact, _ROW_TABLES[kind], rows, account)
        else:
            body = deepcopy(fact)
        self._session.execute(
            "INSERT INTO money_fact(business_id,schema_key,effective_on,main_object_id,data_json) "
            "VALUES(?,?,?,?,?) ON CONFLICT(business_id) DO UPDATE SET "
            "schema_key=excluded.schema_key,effective_on=excluded.effective_on,"
            "main_object_id=excluded.main_object_id,data_json=excluded.data_json",
            (fact["business_id"], schema, fact.get("effective_on"), account, canonical_json(body)),
        )

    def _insert_rows(self, fact, table, rows, account):
        for position, row in enumerate(rows):
            # An absent original location stays absent in the exported source. The
            # stable row identity only disambiguates storage of such source rows.
            location = canonical_json(
                {"row_id": row["row_id"], "source": row["source"], "position": position}
            )
            self._session.execute(
                f"INSERT INTO {table}(row_id,business_id,account_id,source_id,source_location,"
                "reference,actual_on,period,direction,amount_fen,memo,counterparty_text) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["row_id"],
                    fact["business_id"],
                    account,
                    row["source"]["source_id"],
                    location,
                    row.get("reference"),
                    row["actual_on"],
                    fact["period"],
                    "in" if row["direction"] == "inflow" else "out",
                    parse_fen(row["amount_fen"]),
                    row.get("memo"),
                    row.get("counterparty_text"),
                ),
            )

    def register_occurrence(self, business_id, slot, movement_id):
        self._session.execute(
            "INSERT INTO money_occurrence(occurrence_id,business_id,public_slot) VALUES(?,?,?)",
            (movement_id, business_id, slot),
        )

    def occurrences(self, ids=None, business_ids=None):
        clauses, args = [], []
        for field, values in (("occurrence_id", ids), ("business_id", business_ids)):
            if values is not None:
                if not values:
                    return ()
                clauses.append(field + " IN (SELECT value FROM json_each(?))")
                args.append(canonical_json(list(values)))
        return tuple(
            dict(row)
            for row in self._session.fetchall(
                "SELECT occurrence_id,business_id,public_slot FROM money_occurrence"
                + (" WHERE " + " AND ".join(clauses) if clauses else "")
                + " ORDER BY business_id,public_slot",
                args,
            )
        )

    def groups(self, business_id):
        rows = self._session.fetchall(
            "SELECT * FROM money_reconciliation WHERE business_id=? ORDER BY group_id",
            (business_id,),
        )
        members_by_group = {}
        for member in self._session.fetchall(
            "SELECT m.group_id,m.member_kind,m.member_id FROM money_reconciliation_member m "
            "JOIN money_reconciliation g ON g.group_id=m.group_id WHERE g.business_id=? "
            "ORDER BY m.group_id,m.member_kind,m.member_id",
            (business_id,),
        ):
            members_by_group.setdefault(member["group_id"], {}).setdefault(
                member["member_kind"], []
            ).append(member["member_id"])
        values = []
        for row in rows:
            members = members_by_group.get(row["group_id"], {})
            values.append(
                {
                    "group_id": row["group_id"],
                    "account_id": row["account_id"],
                    "direction": row["direction"],
                    "amount_fen": row["amount_fen"],
                    "members": members,
                }
            )
        return values

    def replace_groups(self, desired):
        changed = {
            business_id: groups
            for business_id, groups in desired.items()
            if canonical_json(sorted(groups, key=lambda group: group["group_id"]))
            != canonical_json(self.groups(business_id))
        }
        for business_id in changed:
            self._session.execute(
                "DELETE FROM money_reconciliation WHERE business_id=?", (business_id,)
            )
        for business_id, groups in changed.items():
            self._insert_groups(business_id, groups)

    def _insert_groups(self, business_id, groups):
        for group in groups:
            self._session.execute(
                "INSERT INTO money_reconciliation("
                "group_id,business_id,account_id,direction,amount_fen) "
                "VALUES(?,?,?,?,?)",
                (
                    group["group_id"],
                    business_id,
                    group["account_id"],
                    group["direction"],
                    group["amount_fen"],
                ),
            )
            for kind, ids in group["members"].items():
                for member_id in ids:
                    self._session.execute(
                        "INSERT INTO money_reconciliation_member("
                        "group_id,member_kind,member_id) VALUES(?,?,?)",
                        (group["group_id"], kind, member_id),
                    )
