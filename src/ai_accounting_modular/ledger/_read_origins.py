"""Finite frozen origin fields or complete contents; neither substitutes current facts."""

from copy import deepcopy
from dataclasses import dataclass
from sqlite3 import OperationalError

from ai_accounting_modular.contracts.public import (
    ContractError,
    loads_json,
    validate_contract_items,
)

from . import _read_contents
from ._read_origin_headers import checked_items, head_signature, source_headers
from ._types import AdoptedOriginProjection
from ._values import fail


@dataclass(frozen=True)
class _Selection:
    definition: str
    fields: tuple[str, ...]


_SELECTIONS = {
    "operations.expense": _Selection(
        "OperationsExpense", ("creditor_id", "creditor_kind", "expense_class", "cooperation_id")
    ),
    "operations.service_sale": _Selection(
        "OperationsServiceSale", ("customer_id", "cooperation_id")
    ),
    "operations.sale_return": _Selection("OperationsSaleReturn", ("sale_id", "customer_id")),
    "operations.expense_recovery": _Selection(
        "OperationsExpenseRecovery", ("expense_id", "counterparty_id")
    ),
    "operations.pass_through": _Selection("OperationsPassThrough", ("payer_id", "beneficiary_id")),
}


def _rows(identities, read):
    try:
        rows = read.repository("ledger").read_origin_rows(
            identities,
            tuple((kind, selection.fields) for kind, selection in _SELECTIONS.items()),
        )
    except OperationalError as error:
        if "malformed JSON" in str(error):
            raise ContractError("storage_corrupt", "正式来源的JSON正文损坏。") from error
        raise
    return {row["result_id"]: row for row in rows}


def _fragment(row, selection, checked):
    # The immutable projected JSON bytes and the exact schema plan are the key.
    # Every returned fragment gets fresh mutable dictionaries and quantity arrays.
    raw = row["projection_json"]
    if not isinstance(raw, str):
        fail("storage_corrupt", "正式来源缺少所选字段。")
    identity = selection, raw.encode("utf-8")
    if identity not in checked:
        try:
            tokens = loads_json(raw)
            if not isinstance(tokens, list) or len(tokens) != len(selection.fields) + 3:
                raise ValueError
            values = [None if token is None else loads_json(token) for token in tokens]
            target = {} if tokens[0] is None else {"kind": values[0]}
            basis = {} if tokens[1] is None else {"kind": values[1]}
            if tokens[2] is not None:
                basis["adopted_quantities"] = values[2]
            data = {
                field: value
                for field, token, value in zip(
                    selection.fields, tokens[3:], values[3:], strict=True
                )
                if token is not None
            }
            checked_items(
                "business-facts.schema.json",
                "Target" + selection.definition,
                [target],
                fields=("kind",),
            )
            checked_items(
                "business-facts.schema.json",
                "Basis" + selection.definition,
                [basis],
                fields=("kind", "adopted_quantities"),
            )
            checked_items(
                "business-facts.schema.json",
                "Accounting" + selection.definition,
                [data],
                fields=selection.fields,
            )
        except (ContractError, ValueError, TypeError) as error:
            if isinstance(error, ContractError) and error.code == "invalid_contract":
                raise
            raise ContractError("storage_corrupt", "正式来源的所选字段与合同不一致。") from error
        checked[identity] = (
            target["kind"],
            basis["kind"],
            tuple(data.items()),
            basis["adopted_quantities"],
        )
    kind, basis_kind, fields, quantities = checked[identity]
    return {"kind": kind}, {
        "kind": basis_kind,
        "input": dict(fields),
        "adopted_quantities": deepcopy(quantities),
    }


def _project(sources, headers, rows, read):
    result, fallback, checked = {}, [], {}
    for entry in sources:
        identity = entry.reference["result_id"]
        row = rows.get(identity)
        if row is None:
            fail("not_found", "指定正式来源不存在。")
        if head_signature(row) != head_signature(headers[identity]):
            fail("storage_corrupt", "同一次读取的正式来源身份发生变化。")
        selection = _SELECTIONS.get(row["business_kind"])
        if selection is None:
            fallback.append(entry)
        else:
            target, basis = _fragment(row, selection, checked)
            result[identity] = AdoptedOriginProjection(entry.reference, entry.source, target, basis)
    # Complex frozen relationships retain the full existing Schema and canonical-hash checks.
    for start in range(0, len(fallback), 500):
        requested = fallback[start : start + 500]
        contents = _read_contents.get_adopted_contents(
            [entry.reference for entry in requested], read
        )
        for entry, content in zip(requested, contents, strict=True):
            if content.source != entry.source:
                fail("storage_corrupt", "完整来源与所定位的正式采用身份不一致。")
            result[content.reference["result_id"]] = content
    return result


def capture(sources, headers, read):
    if not sources:
        return {}
    return _project(
        sources, headers, _rows([entry.reference["result_id"] for entry in sources], read), read
    )


def get_origins(locators, read):
    if not isinstance(locators, list | tuple):
        fail("invalid_input", "正式来源须使用精确结果定位列表。")
    validate_contract_items("core.schema.json", "ResultRef", list(locators))
    identities = [ref["result_id"] for ref in locators]
    if len(set(identities)) != len(identities):
        fail("invalid_input", "一次不能重复读取同一正式结果。")
    if not locators:
        return ()
    # The same selector fetch supplies metadata and projected fields. No second
    # header query or caller-supplied validation proof is accepted.
    mapped = _rows(identities, read)
    for ref in locators:
        _read_contents._locate(ref, mapped.get(ref["result_id"]))
    sources, headers = source_headers(mapped.values())
    projected = _project(sources, headers, mapped, read)
    return tuple(projected[identity] for identity in identities)
