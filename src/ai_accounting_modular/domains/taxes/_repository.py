"""Only the tax domain reads or writes tax_fact bodies."""

from ai_accounting_modular.contracts.public import (
    ContractError,
    canonical_json,
    loads_json,
    validate_contract,
)


def _read(row):
    try:
        fact = loads_json(row["data_json"])
        validate_contract("business-facts.schema.json", "Fact", fact)
        if (
            fact["business_id"] != row["business_id"]
            or fact["kind"] != row["schema_key"]
            or fact.get("effective_on") != row["effective_on"]
            or row["main_object_id"] is not None
            or not fact["kind"].startswith("taxes.")
        ):
            raise ValueError("stored tax identity mismatch")
        return fact
    except (ContractError, ValueError) as error:
        raise ContractError("storage_corrupt", "税务当前事实与其身份或格式不一致。") from error


class TaxesRepository:
    def __init__(self, session):
        self._session = session

    def get(self, ids):
        if not ids:
            return ()
        rows = self._session.fetchall(
            "SELECT business_id,schema_key,effective_on,main_object_id,data_json FROM tax_fact "
            "WHERE business_id IN (SELECT value FROM json_each(?)) ORDER BY business_id",
            (canonical_json(list(ids)),),
        )
        return tuple(_read(row) for row in rows)

    def query(self, query):
        clauses = ["json_extract(data_json,'$.period')>=?", "json_extract(data_json,'$.period')<=?"]
        args = [query["period_from"], query["period_to"]]
        if "kind" in query:
            clauses.append("schema_key=?")
            args.append(query["kind"])
        if "object_id" in query:
            return ()
        if "cursor" in query:
            clauses.append("business_id>?")
            args.append(query["cursor"])
        args.append(query["limit"])
        rows = self._session.fetchall(
            "SELECT business_id,schema_key,effective_on,main_object_id,data_json "
            "FROM tax_fact WHERE " + " AND ".join(clauses) + " ORDER BY business_id LIMIT ?",
            args,
        )
        return tuple(_read(row) for row in rows)

    def save(self, fact):
        self._session.execute(
            "INSERT INTO tax_fact(business_id,schema_key,effective_on,main_object_id,data_json) "
            "VALUES(?,?,?,NULL,?) ON CONFLICT(business_id) DO UPDATE SET "
            "schema_key=excluded.schema_key,effective_on=excluded.effective_on,"
            "data_json=excluded.data_json "
            "WHERE tax_fact.data_json<>excluded.data_json",
            (fact["business_id"], fact["kind"], fact.get("effective_on"), canonical_json(fact)),
        )

    def remove(self, business_id):
        self._session.execute("DELETE FROM tax_fact WHERE business_id=?", (business_id,))
