"""Seal actual saved outputs without changing typed business-result identity."""

from ai_accounting_modular.contracts.public import (
    ContractError,
    canonical_json,
    content_digest,
    validate_contract,
)

from ._values import fail

RESOURCE_TABLES = (
    "obligation",
    "settlement",
    "obligation_reclassification",
    "cost_lot",
    "cost_consumption",
    "result_fund_movement",
    "result_tax_sale",
    "result_report_contribution",
)
_TABLES = ("voucher", "voucher_line", *RESOURCE_TABLES)


def _digest(tables):
    return content_digest(
        {
            "format": "ai-accounting-formal-output/1",
            "tables": {table: sorted(rows, key=canonical_json) for table, rows in tables.items()},
        }
    )


EMPTY_OUTPUT_DIGEST = _digest({table: [] for table in _TABLES})


def output_digests(records, result_ids):
    grouped = {result_id: {table: [] for table in _TABLES} for result_id in result_ids}
    voucher_owners = {}
    for table in ("voucher", *RESOURCE_TABLES):
        for row in records[table]:
            result_id = row["result_id"]
            if result_id in grouped:
                grouped[result_id][table].append(row)
                if table == "voucher":
                    voucher_owners[row["voucher_id"]] = result_id
    for row in records["voucher_line"]:
        result_id = voucher_owners.get(row["voucher_id"])
        if result_id in grouped:
            grouped[result_id]["voucher_line"].append(row)
    return {result_id: _digest(tables) for result_id, tables in grouped.items()}


def verify_output_digests(records, heads):
    actual = output_digests(records, heads)
    for result_id, row in heads.items():
        try:
            validate_contract("core.schema.json", "Digest", row["formal_output_digest"])
        except ContractError as error:
            raise ContractError("storage_corrupt", "正式输出摘要不符合保存合同。") from error
        if row["formal_output_digest"] != actual[result_id]:
            fail("storage_corrupt", "正式输出不完整或与采用时保存的凭证、资源及分类摘要不一致。")
