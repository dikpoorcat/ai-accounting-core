"""The people domain owns one complete current fact per business identity."""

from ai_accounting_modular.contracts.public import (
    ContractError,
    canonical_json,
    loads_json,
    validate_contract,
)

from ._relationships import ordered_bounds


def _read_fact(row):
    try:
        fact = loads_json(row["data_json"])
        validate_contract("business-facts.schema.json", "Fact", fact)
        if (
            fact["business_id"] != row["business_id"]
            or fact["kind"] != row["schema_key"]
            or fact.get("effective_on") != row["effective_on"]
            or fact["data"].get("person_id", fact["data"].get("employee_id"))
            != row["main_object_id"]
            or not fact["kind"].startswith("people.")
        ):
            raise ValueError("stored fact identity mismatch")
        if fact["kind"] == "people.relationship":
            ordered_bounds(fact)
        return fact
    except (ContractError, ValueError) as error:
        raise ContractError("storage_corrupt", "薪酬当前事实与其身份或格式不一致。") from error


class PeopleRepository:
    def __init__(self, session):
        self._session = session

    def get(self, ids):
        if not ids:
            return ()
        rows = self._session.fetchall(
            "SELECT business_id,schema_key,effective_on,main_object_id,data_json FROM people_fact "
            "WHERE business_id IN (SELECT value FROM json_each(?)) ORDER BY business_id",
            (canonical_json(list(ids)),),
        )
        return tuple(_read_fact(row) for row in rows)

    def query(self, query):
        clauses = ["json_extract(data_json,'$.period')>=?", "json_extract(data_json,'$.period')<=?"]
        args = [query["period_from"], query["period_to"]]
        for name, column in (("kind", "schema_key"), ("object_id", "main_object_id")):
            if name in query:
                clauses.append(column + "=?")
                args.append(query[name])
        if "cursor" in query:
            clauses.append("business_id>?")
            args.append(query["cursor"])
        args.append(query["limit"])
        rows = self._session.fetchall(
            "SELECT business_id,schema_key,effective_on,main_object_id,data_json "
            "FROM people_fact WHERE " + " AND ".join(clauses) + " ORDER BY business_id LIMIT ?",
            args,
        )
        return tuple(_read_fact(row) for row in rows)

    def relationships(self, person_ids):
        if not person_ids:
            return {}
        rows = self._session.fetchall(
            "SELECT business_id,schema_key,effective_on,main_object_id,data_json FROM people_fact "
            "WHERE main_object_id IN (SELECT value FROM json_each(?)) "
            "AND schema_key='people.relationship' ORDER BY business_id",
            (canonical_json(list(person_ids)),),
        )
        facts = {}
        for row in rows:
            fact = _read_fact(row)
            person_id = fact["data"]["person_id"]
            if person_id in facts:
                raise ContractError("storage_corrupt", "同一人员出现多份当前关系时间线。")
            facts[person_id] = fact
        return facts

    def save(self, fact):
        data = fact["data"]
        self._session.execute(
            "INSERT INTO people_fact(business_id,schema_key,effective_on,main_object_id,data_json) "
            "VALUES(?,?,?,?,?) ON CONFLICT(business_id) DO UPDATE SET "
            "schema_key=excluded.schema_key,effective_on=excluded.effective_on,"
            "main_object_id=excluded.main_object_id,data_json=excluded.data_json "
            "WHERE people_fact.data_json<>excluded.data_json",
            (
                fact["business_id"],
                fact["kind"],
                fact.get("effective_on"),
                data.get("person_id", data.get("employee_id")),
                canonical_json(fact),
            ),
        )

    def remove(self, business_id):
        self._session.execute("DELETE FROM people_fact WHERE business_id=?", (business_id,))
