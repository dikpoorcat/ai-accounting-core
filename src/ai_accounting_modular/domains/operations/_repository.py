"""The operating domain's single, owner-scoped fact repository."""

from ai_accounting_modular.contracts.public import (
    ContractError,
    canonical_json,
    loads_json,
    validate_contract,
)


def _read_fact(row):
    try:
        fact = loads_json(row["data_json"])
        validate_contract("business-facts.schema.json", "Fact", fact)
        if (
            fact["business_id"] != row["business_id"]
            or fact["kind"] != row["schema_key"]
            or fact.get("effective_on") != row["effective_on"]
            or not fact["kind"].startswith("operations.")
        ):
            raise ValueError("stored fact identity mismatch")
        return fact
    except (ContractError, ValueError) as error:
        raise ContractError("storage_corrupt", "经营当前事实与其身份或格式不一致。") from error


class OperationsRepository:
    def __init__(self, session):
        self._session = session

    def get(self, ids):
        if not ids:
            return ()
        rows = self._session.fetchall(
            "SELECT business_id,schema_key,effective_on,data_json FROM op_fact "
            "WHERE business_id IN "
            "(SELECT value FROM json_each(?)) ORDER BY business_id",
            (canonical_json(list(ids)),),
        )
        return tuple(_read_fact(row) for row in rows)

    def query(self, query):
        clauses = ["json_extract(data_json,'$.period')>=?", "json_extract(data_json,'$.period')<=?"]
        args = [query["period_from"], query["period_to"]]
        if "kind" in query:
            clauses.append("schema_key=?")
            args.append(query["kind"])
        if "object_id" in query:
            clauses.append("main_object_id=?")
            args.append(query["object_id"])
        if "cursor" in query:
            clauses.append("business_id>?")
            args.append(query["cursor"])
        args.append(query["limit"])
        rows = self._session.fetchall(
            "SELECT business_id,schema_key,effective_on,data_json FROM op_fact WHERE "
            + " AND ".join(clauses)
            + " ORDER BY business_id LIMIT ?",
            args,
        )
        return tuple(_read_fact(row) for row in rows)

    def save(self, fact, main_object_id):
        self._session.execute(
            "INSERT INTO op_fact(business_id,schema_key,effective_on,main_object_id,data_json) "
            "VALUES(?,?,?,?,?) ON CONFLICT(business_id) DO UPDATE SET "
            "schema_key=excluded.schema_key,effective_on=excluded.effective_on,"
            "main_object_id=excluded.main_object_id,data_json=excluded.data_json "
            "WHERE schema_key IS NOT excluded.schema_key "
            "OR effective_on IS NOT excluded.effective_on "
            "OR main_object_id IS NOT excluded.main_object_id "
            "OR data_json IS NOT excluded.data_json",
            (
                fact["business_id"],
                fact["kind"],
                fact.get("effective_on"),
                main_object_id,
                canonical_json(fact),
            ),
        )

    def remove(self, business_id):
        self._session.execute("DELETE FROM op_fact WHERE business_id=?", (business_id,))
