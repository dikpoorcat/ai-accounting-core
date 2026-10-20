"""Only current owner facts; all formal balances remain with ledger."""

from ai_accounting_modular.contracts.public import (
    ContractError,
    canonical_json,
    loads_json,
    validate_contract,
)

from ._changes import object_roles, validate_fact

_TABLES = {"financing": "financing_fact", "investment": "investment_fact"}


def _table(family):
    if family not in _TABLES:
        raise ContractError("invalid_input", "未知融资投资事实家族。")
    return _TABLES[family]


def _main_object(fact):
    roles = object_roles(fact)
    return roles[0][1] if roles else None


def _read(row, family):
    try:
        fact = loads_json(row["data_json"])
        validate_fact(fact, family)
        if (
            fact["business_id"] != row["business_id"]
            or fact["kind"] != row["schema_key"]
            or fact.get("effective_on") != row["effective_on"]
            or _main_object(fact) != row["main_object_id"]
        ):
            raise ValueError("stored identity mismatch")
        return fact
    except (ValueError, ContractError) as error:
        raise ContractError(
            "storage_corrupt", "融资投资当前事实与保存的身份或格式不一致。"
        ) from error


class FinancingRepository:
    def __init__(self, session):
        self._session = session

    def get(self, family, ids):
        if len(ids) > 500 or len(ids) != len(set(ids)):
            raise ContractError("invalid_input", "当前事实查询至多500个不同业务身份。")
        for business_id in ids:
            validate_contract("core.schema.json", "Id", business_id)
        if not ids:
            return ()
        rows = self._session.fetchall(
            "SELECT business_id,schema_key,effective_on,main_object_id,data_json "
            f"FROM {_table(family)} "
            "WHERE business_id IN (SELECT value FROM json_each(?)) ORDER BY business_id",
            (canonical_json(list(ids)),),
        )
        return tuple(_read(row, family) for row in rows)

    def query(self, query):
        clauses = ["json_extract(data_json,'$.period')>=?", "json_extract(data_json,'$.period')<=?"]
        args = [query["period_from"], query["period_to"]]
        if "kind" in query:
            clauses.append("schema_key=?")
            args.append(query["kind"])
        if "object_id" in query:
            fields = ("owner_id", "lender_id", "fund_id", "counterparty_id", "funds_account_id")
            clauses.append(
                "("
                + " OR ".join(f"json_extract(data_json,'$.data.{field}')=?" for field in fields)
                + ")"
            )
            args.extend([query["object_id"]] * len(fields))
        if "cursor" in query:
            validate_contract("core.schema.json", "Id", query["cursor"])
            clauses.append("business_id>?")
            args.append(query["cursor"])
        args.append(query["limit"])
        rows = self._session.fetchall(
            "SELECT business_id,schema_key,effective_on,main_object_id,data_json FROM "
            + _table(query["family"])
            + " WHERE "
            + " AND ".join(clauses)
            + " ORDER BY business_id LIMIT ?",
            args,
        )
        return tuple(_read(row, query["family"]) for row in rows)

    def save(self, fact, family):
        table = _table(family)
        self._session.execute(
            f"INSERT INTO {table}(business_id,schema_key,effective_on,main_object_id,data_json) "
            "VALUES(?,?,?,?,?) ON CONFLICT(business_id) DO UPDATE SET "
            "schema_key=excluded.schema_key,effective_on=excluded.effective_on,"
            "main_object_id=excluded.main_object_id,data_json=excluded.data_json "
            f"WHERE {table}.data_json<>excluded.data_json",
            (
                fact["business_id"],
                fact["kind"],
                fact.get("effective_on"),
                _main_object(fact),
                canonical_json(fact),
            ),
        )

    def remove(self, business_id, family):
        self._session.execute(f"DELETE FROM {_table(family)} WHERE business_id=?", (business_id,))
