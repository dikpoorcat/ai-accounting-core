"""A money owner must reject damaged source sets even without application heads."""

import pytest

from ai_accounting_modular.contracts.public import ContractError

from .test_period_and_row_changes import row_system


@pytest.mark.parametrize("kind", ["bank", "platform"])
@pytest.mark.parametrize(
    "corruption",
    ["missing", "amount", "memo", "source_location", "position", "account", "missing_digest"],
)
def test_owner_export_rejects_incomplete_or_changed_normalized_original_rows(
    tmp_path, kind, corruption
):
    system, ids, _, first, second = row_system(tmp_path, kind)
    _, storage, company, money, _ = system
    with storage.read_unit(company) as read:
        assert money.export_current([first["business_id"], second["business_id"]], read) == tuple(
            sorted((first, second), key=lambda fact: fact["business_id"])
        )
    table = "bank_statement_row" if kind == "bank" else "platform_activity_row"
    with storage.write_unit(company) as unit:
        session = unit.repository("money")._session
        row_id = first["data"]["rows"][0]["row_id"]
        if corruption == "missing":
            session.execute(f"DELETE FROM {table} WHERE row_id=?", (row_id,))
        elif corruption == "amount":
            session.execute(f"UPDATE {table} SET amount_fen=101 WHERE row_id=?", (row_id,))
        elif corruption == "memo":
            session.execute(f"UPDATE {table} SET memo='注入的损坏文字' WHERE row_id=?", (row_id,))
        elif corruption == "source_location":
            session.execute(
                f"UPDATE {table} SET source_location="
                "json_set(source_location,'$.source.location','changed') WHERE row_id=?",
                (row_id,),
            )
        elif corruption == "position":
            session.execute(
                f"UPDATE {table} SET source_location="
                "json_set(source_location,'$.position',1) WHERE row_id=?",
                (row_id,),
            )
        elif corruption == "account":
            wrong_account = ids["cash"] if kind == "bank" else ids["bank"]
            session.execute(
                f"UPDATE {table} SET account_id=? WHERE row_id=?", (wrong_account, row_id)
            )
        else:
            session.execute(
                "UPDATE money_fact SET data_json=json_remove(data_json,'$.collection_digest') "
                "WHERE business_id=?",
                (first["business_id"],),
            )
    with storage.read_unit(company) as read, pytest.raises(ContractError) as error:
        money.export_current([first["business_id"]], read)
    assert error.value.code == "storage_corrupt"
    # A different complete collection is still independently readable.
    with storage.read_unit(company) as read:
        assert money.export_current([second["business_id"]], read) == (second,)
