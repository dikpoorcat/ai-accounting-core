"""The single complete current continuation body, with checked projections."""

from ai_accounting_modular.contracts.public import (
    ContractError,
    canonical_json,
    loads_json,
    validate_contract,
)

from ._facts import KIND, validate_fact


def _read(row):
    try:
        fact = loads_json(row["data_json"])
        validate_fact(fact)
        if (
            fact["business_id"] != row["business_id"]
            or row["schema_key"] != KIND
            or row["effective_on"] != fact.get("effective_on")
            or row["main_object_id"] is not None
        ):
            raise ValueError("continuation projection")
        return fact
    except (ContractError, KeyError, TypeError, ValueError) as error:
        raise ContractError(
            "storage_corrupt", "报表接续当前正文、身份或检索投影不一致。"
        ) from error


class ReportingRepository:
    def __init__(self, session):
        self._session = session

    def get(self, ids):
        if len(ids) > 500 or len(ids) != len(set(ids)):
            raise ContractError("invalid_input", "接续查询至多500个不同业务身份。")
        for identity in ids:
            validate_contract("core.schema.json", "Id", identity)
        if not ids:
            return ()
        return tuple(
            _read(row)
            for row in self._session.fetchall(
                "SELECT business_id,schema_key,effective_on,main_object_id,data_json "
                "FROM report_basis_fact "
                "WHERE business_id IN (SELECT value FROM json_each(?)) ORDER BY business_id",
                (canonical_json(list(ids)),),
            )
        )

    def query(self, query):
        if query.get("kind", KIND) != KIND or "object_id" in query:
            return ()
        clauses = ["json_extract(data_json,'$.period')>=?", "json_extract(data_json,'$.period')<=?"]
        args = [query["period_from"], query["period_to"]]
        if "cursor" in query:
            validate_contract("core.schema.json", "Id", query["cursor"])
            clauses.append("business_id>?")
            args.append(query["cursor"])
        args.append(query["limit"])
        return tuple(
            _read(row)
            for row in self._session.fetchall(
                "SELECT business_id,schema_key,effective_on,main_object_id,data_json "
                "FROM report_basis_fact WHERE "
                + " AND ".join(clauses)
                + " ORDER BY business_id LIMIT ?",
                args,
            )
        )

    def replace_current(self, desired):
        for business_id, fact in desired.items():
            if fact is None:
                self._session.execute(
                    "DELETE FROM report_basis_fact WHERE business_id=?", (business_id,)
                )
            else:
                validate_fact(fact)
                self._session.execute(
                    "INSERT INTO report_basis_fact"
                    "(business_id,schema_key,effective_on,main_object_id,data_json) "
                    "VALUES(?,?,?,NULL,?) ON CONFLICT(business_id) DO UPDATE SET "
                    "schema_key=excluded.schema_key,effective_on=excluded.effective_on,"
                    "main_object_id=NULL,data_json=excluded.data_json "
                    "WHERE schema_key IS NOT excluded.schema_key "
                    "OR effective_on IS NOT excluded.effective_on "
                    "OR main_object_id IS NOT NULL OR data_json IS NOT excluded.data_json",
                    (business_id, KIND, fact.get("effective_on"), canonical_json(fact)),
                )
