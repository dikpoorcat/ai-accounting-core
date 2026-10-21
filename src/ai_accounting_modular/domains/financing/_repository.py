"""Only current owner facts; all formal balances remain with ledger."""

from ai_accounting_modular.contracts.public import (
    ContractError,
    canonical_json,
    loads_json,
    validate_contract,
)

from ._changes import object_roles, validate_fact
from ._cooperation_common import PREFIX

_TABLES = {"financing": "financing_fact", "investment": "investment_fact"}


def _table(family):
    if family not in _TABLES:
        raise ContractError("invalid_input", "未知融资投资事实家族。")
    return _TABLES[family]


def _main_object(fact):
    roles = object_roles(fact)
    return roles[0][1] if roles else None


def _effective_on(fact):
    if fact["kind"].startswith(PREFIX):
        return fact["data"].get("actual_on", fact.get("effective_on"))
    return fact.get("effective_on")


def _read(row, family, main_object):
    try:
        fact = loads_json(row["data_json"])
        validate_fact(fact, family)
        if (
            fact["business_id"] != row["business_id"]
            or fact["kind"] != row["schema_key"]
            or _effective_on(fact) != row["effective_on"]
            or main_object(fact) != row["main_object_id"]
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

    def _main_object(self, fact):
        if not fact["kind"].startswith(PREFIX):
            return _main_object(fact)
        if fact["kind"] == PREFIX + "contract":
            return fact["data"]["project_id"]
        row = self._session.fetchone(
            "SELECT data_json FROM investment_fact WHERE business_id=? AND schema_key=?",
            (fact["data"]["contract_id"], PREFIX + "contract"),
        )
        if row is None:
            raise ContractError("storage_corrupt", "合作当前事实缺少本模块维护的合同身份。")
        return loads_json(row["data_json"])["data"]["project_id"]

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
        return tuple(_read(row, family, self._main_object) for row in rows)

    def query(self, query):
        clauses = ["json_extract(data_json,'$.period')>=?", "json_extract(data_json,'$.period')<=?"]
        args = [query["period_from"], query["period_to"]]
        if "kind" in query:
            clauses.append("schema_key=?")
            args.append(query["kind"])
        if "object_id" in query:
            fields = (
                "owner_id",
                "lender_id",
                "fund_id",
                "counterparty_id",
                "funds_account_id",
                "project_id",
                "operator_id",
            )
            clauses.append(
                "(main_object_id=? OR "
                + " OR ".join(f"json_extract(data_json,'$.data.{field}')=?" for field in fields)
                + (
                    " OR EXISTS(SELECT 1 FROM investment_fact AS agreement "
                    "WHERE agreement.business_id="
                    "json_extract(investment_fact.data_json,'$.data.contract_id') "
                    "AND json_extract(agreement.data_json,'$.data.operator_id')=?)"
                    if query["family"] == "investment"
                    else ""
                )
                + ")"
            )
            args.extend([query["object_id"]] * (len(fields) + 1))
            if query["family"] == "investment":
                args.append(query["object_id"])
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
        return tuple(_read(row, query["family"], self._main_object) for row in rows)

    def save(self, fact, family):
        table = _table(family)
        main_object = self._main_object(fact)
        previous = (
            self._session.fetchone(
                "SELECT main_object_id FROM investment_fact WHERE business_id=?",
                (fact["business_id"],),
            )
            if family == "investment" and fact["kind"] == PREFIX + "contract"
            else None
        )
        self._session.execute(
            f"INSERT INTO {table}(business_id,schema_key,effective_on,main_object_id,data_json) "
            "VALUES(?,?,?,?,?) ON CONFLICT(business_id) DO UPDATE SET "
            "schema_key=excluded.schema_key,effective_on=excluded.effective_on,"
            "main_object_id=excluded.main_object_id,data_json=excluded.data_json "
            f"WHERE {table}.data_json<>excluded.data_json",
            (
                fact["business_id"],
                fact["kind"],
                _effective_on(fact),
                main_object,
                canonical_json(fact),
            ),
        )
        if previous is not None and previous["main_object_id"] != main_object:
            # The current contract owns its events' project index, not their frozen facts.
            self._session.execute(
                "UPDATE investment_fact SET main_object_id=? "
                "WHERE substr(schema_key,1,?)=? "
                "AND json_extract(data_json,'$.data.contract_id')=? "
                "AND main_object_id IS NOT ?",
                (main_object, len(PREFIX), PREFIX, fact["business_id"], main_object),
            )

    def remove(self, business_id, family):
        self._session.execute(f"DELETE FROM {_table(family)} WHERE business_id=?", (business_id,))
