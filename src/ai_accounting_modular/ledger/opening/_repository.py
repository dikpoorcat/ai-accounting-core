"""Normalized opening membership is the sole persisted member body."""

from ai_accounting_modular.contracts.public import (
    ContractError,
    canonical_json,
    loads_json,
    restore_fact_collection,
    split_fact_collection,
    validate_contract,
)

from ._facts import member_object, validate_fact

_SCHEMA = "opening.package/storage_projection/1"


class OpeningRepository:
    def __init__(self, session):
        self._session = session

    def _facts(self, rows):
        if not rows:
            return ()
        by_business = {row["business_id"]: [] for row in rows}
        members = self._session.fetchall(
            "SELECT member_id,business_id,kind,object_id,data_json FROM opening_member "
            "WHERE business_id IN (SELECT value FROM json_each(?)) "
            "ORDER BY business_id,json_extract(data_json,'$.position'),member_id",
            (canonical_json(list(by_business)),),
        )
        try:
            for row in members:
                saved = loads_json(row["data_json"])
                if set(saved) != {"position", "member"} or type(saved["position"]) is not int:
                    raise ValueError("member storage projection")
                member = saved["member"]
                validate_contract("business-facts.schema.json", "OpeningMember", member)
                if (
                    member["member_id"] != row["member_id"]
                    or member["data"]["type"] != row["kind"]
                    or member_object(member) != row["object_id"]
                    or saved["position"] != len(by_business[row["business_id"]])
                ):
                    raise ValueError("member identity mismatch")
                by_business[row["business_id"]].append(member)
            facts = []
            for row in rows:
                fact = loads_json(row["data_json"])
                if (
                    row["schema_key"] != _SCHEMA
                    or row["main_object_id"] is not None
                    or row["business_id"] != fact["business_id"]
                    or row["effective_on"] != fact.get("effective_on")
                ):
                    raise ValueError("parent storage projection")
                fact = restore_fact_collection(fact, "members", by_business[row["business_id"]])
                validate_fact(fact)
                facts.append(fact)
            return tuple(facts)
        except (KeyError, TypeError, ValueError, ContractError) as error:
            raise ContractError(
                "storage_corrupt", "期初事实与独立成员的身份或格式不完整。"
            ) from error

    def get(self, ids):
        if len(ids) > 500 or len(ids) != len(set(ids)):
            raise ContractError("invalid_input", "期初查询至多500个不同业务身份。")
        for business_id in ids:
            validate_contract("core.schema.json", "Id", business_id)
        if not ids:
            return ()
        rows = self._session.fetchall(
            "SELECT business_id,schema_key,effective_on,main_object_id,data_json FROM opening_fact "
            "WHERE business_id IN (SELECT value FROM json_each(?)) ORDER BY business_id",
            (canonical_json(list(ids)),),
        )
        return self._facts(rows)

    def query(self, query):
        clauses = ["json_extract(data_json,'$.period')>=?", "json_extract(data_json,'$.period')<=?"]
        args = [query["period_from"], query["period_to"]]
        if query.get("kind", "opening.package") != "opening.package":
            return ()
        if "object_id" in query:
            clauses.append(
                "EXISTS(SELECT 1 FROM opening_member m "
                "WHERE m.business_id=opening_fact.business_id "
                "AND (m.object_id=? OR json_extract(m.data_json,'$.member.data.recipient_id')=? "
                "OR json_extract(m.data_json,'$.member.data.counterparty_id')=? "
                "OR json_extract(m.data_json,'$.member.data.fund_id')=?))"
            )
            args.extend([query["object_id"]] * 4)
        if "cursor" in query:
            validate_contract("core.schema.json", "Id", query["cursor"])
            clauses.append("business_id>?")
            args.append(query["cursor"])
        args.append(query["limit"])
        return self._facts(
            self._session.fetchall(
                "SELECT business_id,schema_key,effective_on,main_object_id,data_json "
                "FROM opening_fact WHERE "
                + " AND ".join(clauses)
                + " ORDER BY business_id LIMIT ?",
                args,
            )
        )

    def replace_current(self, desired):
        before = {fact["business_id"]: fact for fact in self.get(tuple(desired))}
        changed = {
            business_id: fact
            for business_id, fact in desired.items()
            if canonical_json(before.get(business_id)) != canonical_json(fact)
        }
        if not changed:
            return
        # Release all obsolete members before inserting the final desired set.
        # A failure is rolled back with the parent's head and formal resources.
        self._session.execute(
            "DELETE FROM opening_member WHERE business_id IN (SELECT value FROM json_each(?))",
            (canonical_json(list(changed)),),
        )
        for business_id, fact in changed.items():
            if fact is None:
                self._session.execute(
                    "DELETE FROM opening_fact WHERE business_id=?", (business_id,)
                )
                continue
            validate_fact(fact)
            body, members = split_fact_collection(fact, "members")
            self._session.execute(
                "INSERT INTO opening_fact"
                "(business_id,schema_key,effective_on,main_object_id,data_json) "
                "VALUES(?,?,?,NULL,?) ON CONFLICT(business_id) DO UPDATE SET "
                "schema_key=excluded.schema_key,effective_on=excluded.effective_on,"
                "main_object_id=NULL,data_json=excluded.data_json",
                (business_id, _SCHEMA, fact.get("effective_on"), canonical_json(body)),
            )
            for position, member in enumerate(members):
                self._session.execute(
                    "INSERT INTO opening_member(member_id,business_id,kind,object_id,data_json) "
                    "VALUES(?,?,?,?,?)",
                    (
                        member["member_id"],
                        business_id,
                        member["data"]["type"],
                        member_object(member),
                        canonical_json({"position": position, "member": member}),
                    ),
                )
