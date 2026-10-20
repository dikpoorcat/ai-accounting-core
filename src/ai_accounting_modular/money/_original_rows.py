"""Ordered original rows, batched by owner and checked against their parent."""

from ai_accounting_modular.contracts.public import (
    ContractError,
    canonical_json,
    loads_json,
    validate_contract,
)

ROW_TABLES = {
    "money.bank_statement": "bank_statement_row",
    "money.platform_activity": "platform_activity_row",
}


def row_wire(row, kind, *, position=None):
    """A page checks its rows; only complete reconstruction checks the collection."""
    try:
        saved = loads_json(row["source_location"])
        source = saved["source"]
        if (
            set(saved) != {"row_id", "source", "position"}
            or type(saved["position"]) is not int
            or saved["position"] < 0
            or position is not None
            and saved["position"] != position
            or saved["row_id"] != row["row_id"]
            or source["source_id"] != row["source_id"]
            or row["direction"] not in {"in", "out"}
            or row["actual_on"][:7] != row["period"]
        ):
            raise ValueError("original row projection mismatch")
        value = {
            "row_id": row["row_id"],
            "source": source,
            "actual_on": row["actual_on"],
            "direction": "inflow" if row["direction"] == "in" else "outflow",
            "amount_fen": str(row["amount_fen"]),
        }
        for field in ("reference", "counterparty_text", "memo"):
            if row[field] is not None:
                value[field] = row[field]
        definition = "BankStatementRow" if kind == "money.bank_statement" else "PlatformOriginalRow"
        validate_contract("business-facts.schema.json", definition, value)
        return value
    except (ContractError, KeyError, TypeError, ValueError) as error:
        raise ContractError(
            "storage_corrupt", "原行的身份、顺序、资料定位或保存字段不一致。"
        ) from error


def load_rows(session, parents, kind):
    """One indexed collection read per row type, rather than one per parent."""
    result = {identity: [] for identity in parents}
    if not result:
        return result
    table = ROW_TABLES[kind]
    rows = session.fetchall(
        f"SELECT * FROM {table} WHERE business_id IN (SELECT value FROM json_each(?)) "
        "ORDER BY business_id,json_extract(source_location,'$.position'),row_id",
        (canonical_json(list(result)),),
    )
    for row in rows:
        parent = parents[row["business_id"]]
        account = parent["data"][
            "funds_account_id" if kind == "money.bank_statement" else "platform_account_id"
        ]
        if row["account_id"] != account or row["period"] != parent["period"]:
            raise ContractError("storage_corrupt", "原行账户或月份与所属完整清单不一致。")
        values = result[row["business_id"]]
        values.append(row_wire(row, kind, position=len(values)))
    return result
