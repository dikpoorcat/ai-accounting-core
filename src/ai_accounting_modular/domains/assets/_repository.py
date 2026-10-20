"""The asset owner stores one complete current fact and derived lookup columns."""

from ai_accounting_modular.contracts.public import (
    ContractError,
    canonical_json,
    loads_json,
    validate_contract,
)

from ._common import assets_in


def main_object(fact):
    cards = assets_in(fact)
    return cards[0]["asset_id"] if cards else fact["data"].get("project_id")


def _read(row):
    try:
        fact = loads_json(row["data_json"])
        validate_contract("business-facts.schema.json", "Fact", fact)
        if (
            not fact["kind"].startswith("assets.")
            or fact["business_id"] != row["business_id"]
            or fact["kind"] != row["schema_key"]
            or fact.get("effective_on") != row["effective_on"]
            or main_object(fact) != row["main_object_id"]
        ):
            raise ValueError("asset fact projection mismatch")
        return fact
    except (ContractError, ValueError) as error:
        raise ContractError("storage_corrupt", "资产事实正文与其身份或派生索引不一致。") from error


class AssetsRepository:
    def __init__(self, session):
        self._session = session

    def get(self, ids):
        if not ids:
            return ()
        rows = self._session.fetchall(
            "SELECT business_id,schema_key,effective_on,main_object_id,data_json FROM asset_fact "
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
            # Batches retain one body; search only the owner's selected range.
            clauses.append(
                "(main_object_id=? OR EXISTS (SELECT 1 FROM json_each(data_json,'$.data.items') "
                "WHERE json_extract(value,'$.asset_id')=?) OR EXISTS "
                "(SELECT 1 FROM json_each(data_json,'$.data.assets') "
                "WHERE json_extract(value,'$.asset_id')=?) OR "
                "json_extract(data_json,'$.data.project_id')=? OR "
                "json_extract(data_json,'$.data.supplier_id')=? OR "
                "json_extract(data_json,'$.data.buyer_id')=? OR EXISTS "
                "(SELECT 1 FROM json_each(data_json,'$.data.creditors') "
                "WHERE json_extract(value,'$.person_id')=?))"
            )
            args.extend([query["object_id"]] * 7)
        if "cursor" in query:
            clauses.append("business_id>?")
            args.append(query["cursor"])
        args.append(query["limit"])
        return tuple(
            _read(row)
            for row in self._session.fetchall(
                "SELECT business_id,schema_key,effective_on,main_object_id,data_json "
                "FROM asset_fact WHERE " + " AND ".join(clauses) + " ORDER BY business_id LIMIT ?",
                args,
            )
        )

    def save(self, fact):
        self._session.execute(
            "INSERT INTO asset_fact(business_id,schema_key,effective_on,main_object_id,data_json) "
            "VALUES(?,?,?,?,?) ON CONFLICT(business_id) DO UPDATE SET "
            "schema_key=excluded.schema_key,"
            "effective_on=excluded.effective_on,main_object_id=excluded.main_object_id,"
            "data_json=excluded.data_json WHERE asset_fact.data_json<>excluded.data_json",
            (
                fact["business_id"],
                fact["kind"],
                fact.get("effective_on"),
                main_object(fact),
                canonical_json(fact),
            ),
        )

    def remove(self, business_id):
        self._session.execute("DELETE FROM asset_fact WHERE business_id=?", (business_id,))
