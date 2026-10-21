"""One sparse financial contribution per exact formal Source, never a second journal."""

from __future__ import annotations

from copy import copy

from ai_accounting_modular.contracts.public import (
    MAX_FEN,
    MIN_FEN,
    ContractError,
    content_digest,
    validate_contract,
)

from ._cashflow import _ROWS, money_targets
from ._financial_cells import Issues
from ._financial_order import CHILD_KEYS
from ._financial_profit import _TARGET_VALUES, _contribution
from ._financial_projection_fold import Notice, merge_notices
from ._journal_amounts import journal_amount
from ._profit import pretax_values

COLUMN = "@column"
_PROFIT_LINES = frozenset(str(value) for value in range(1, 33))
_RESOURCE_FIELDS = {
    "obligation": (
        "public_slot",
        "party_id",
        "direction",
        "category",
        "amount_fen",
        "account_code",
        "due_on",
    ),
    "settlement": (
        "settlement_id",
        "source_business_id",
        "source_public_slot",
        "method",
        "amount_fen",
    ),
    "obligation_reclassification": (
        "relation_id",
        "source_business_id",
        "source_public_slot",
        "mode",
        "amount_fen",
    ),
}


def _resource(table, row):
    if set(row) != set(_RESOURCE_FIELDS[table]):
        raise ValueError
    if not _fen(row["amount_fen"]) or row["amount_fen"] < (0 if table == "obligation" else 1):
        raise ValueError
    if table == "obligation":
        if (
            type(row["public_slot"]) is not str
            or not row["public_slot"]
            or row["direction"] not in ("receivable", "payable")
            or type(row["category"]) is not str
            or not row["category"]
            or row["account_code"] not in ("2501", "224102", "224103")
            or (row["account_code"] != "2501" and row["party_id"] is not None)
        ):
            raise ValueError
        if row["party_id"] is not None:
            validate_contract("core.schema.json", "Id", row["party_id"])
        if row["due_on"] is not None:
            validate_contract("core.schema.json", "Date", row["due_on"])
    else:
        validate_contract("core.schema.json", "Id", row[CHILD_KEYS[table]])
        validate_contract("core.schema.json", "Id", row["source_business_id"])
        if type(row["source_public_slot"]) is not str or not row["source_public_slot"]:
            raise ValueError
        field, choices = (
            ("method", ("money", "offset", "agency"))
            if table == "settlement"
            else ("mode", ("transfer", "reclassify_excess"))
        )
        if row[field] not in choices:
            raise ValueError


class _CapturedIssues(Issues):
    def __init__(self):
        super().__init__()
        self.notices = ()

    def unknown(self, field, meaning, message, *, code="report_basis_missing"):
        self.notices = merge_notices(self.notices, (Notice(field, code, meaning, message),))
        return super().unknown(field, meaning, message, code=code)


def source_atom(source):
    return content_digest(source)


def _fen(value):
    return type(value) is int and MIN_FEN <= value <= MAX_FEN


def validate_body(body, identity):
    """Check this owner's persisted sparse shape, not a second business contract."""
    try:
        if type(body) is not dict or set(body) != {
            "source",
            "business_period",
            "posting_period",
            "baseline_result_id",
            "journal",
            "has_report_details",
            "profit",
            "notices",
            "cash",
            "pretax",
            "resources",
        }:
            raise ValueError
        validate_contract("business-facts.schema.json", "ResultSourceRef", body["source"])
        if source_atom(body["source"]) != identity:
            raise ValueError
        for name in ("business_period", "posting_period"):
            validate_contract("core.schema.json", "Month", body[name])
        if body["baseline_result_id"] is not None:
            validate_contract("core.schema.json", "Id", body["baseline_result_id"])
        if (
            type(body["journal"]) is not list
            or type(body["has_report_details"]) is not bool
            or type(body["profit"]) is not dict
            or type(body["notices"]) is not list
            or type(body["cash"]) is not list
            or type(body["resources"]) is not dict
            or set(body["resources"]) != {"obligation", "settlement", "obligation_reclassification"}
        ):
            raise ValueError
        for table, rows in body["resources"].items():
            if type(rows) is not list:
                raise ValueError
            previous = None
            for row in rows:
                if (
                    type(row) is not dict
                    or type(row.get(CHILD_KEYS[table])) is not str
                    or not _fen(row.get("amount_fen"))
                    or row["amount_fen"] < 0
                    or (previous is not None and previous >= row[CHILD_KEYS[table]])
                ):
                    raise ValueError
                _resource(table, row)
                previous = row[CHILD_KEYS[table]]
        notices = tuple(Notice.from_record(raw) for raw in body["notices"])
        if len({note.field for note in notices}) != len(notices):
            raise ValueError
        previous = 0
        for row in body["journal"]:
            if (
                type(row) is not list
                or len(row) != 6
                or type(row[0]) is not int
                or row[0] <= previous
                or type(row[1]) is not str
                or not row[1]
                or any(value is not None and type(value) is not str for value in row[2:4])
                or any(not _fen(value) or value < 0 for value in row[4:])
            ):
                raise ValueError
            previous = row[0]
        for line, amount in body["profit"].items():
            if type(line) is not str or line not in _PROFIT_LINES:
                raise ValueError
            if type(amount) is list:
                if (
                    not amount
                    or any(
                        type(index) is not int or not 0 <= index < len(notices) for index in amount
                    )
                    or sorted(set(amount)) != amount
                ):
                    raise ValueError
            elif not _fen(amount) or amount == 0:
                raise ValueError
        for movement in body["cash"]:
            if movement is None:
                continue
            if type(movement) is not list:
                raise ValueError
            categories = set()
            for part in movement:
                if (
                    type(part) is not list
                    or len(part) != 2
                    or type(part[0]) is not str
                    or part[0] not in _ROWS
                    or part[0] in categories
                    or not _fen(part[1])
                ):
                    raise ValueError
                categories.add(part[0])
        if body["pretax"] is not None and (
            type(body["pretax"]) is not list
            or len(body["pretax"]) != 3
            or any(not _fen(value) for value in body["pretax"])
        ):
            raise ValueError
    except (ContractError, ValueError, KeyError, TypeError) as error:
        raise ContractError(
            "storage_corrupt", "财务派生正文的精确来源或稀疏金额格式损坏。"
        ) from error
    return body


def _body(selection, head, lines, details, funds, resources):
    local = copy(selection)
    local.issues = _CapturedIssues()
    profit = _contribution(local, head, lines, details, COLUMN)
    source = selection.source(head)
    notices = local.issues.notices
    note_fields = {note.field: index for index, note in enumerate(notices)}

    def amount(value):
        return (
            [note_fields[local.issues.values[index]["field"]] for index in value]
            if isinstance(value, tuple)
            else value
        )

    main = local.profit_rows(head, lines)
    return {
        "source": source,
        "business_period": head["business_period"],
        "posting_period": head["posting_period"],
        "baseline_result_id": head["baseline_result_id"],
        "journal": [
            [
                row["line_no"],
                row["account_code"],
                row.get("party_id"),
                row.get("object_id"),
                journal_amount(row["debit_fen"]),
                journal_amount(row["credit_fen"]),
            ]
            for row in lines
        ],
        "has_report_details": bool(details),
        "profit": {str(line): amount(value) for line, value in profit.items() if value != 0},
        "notices": [note.record() for note in notices],
        # Preserve movement and category order. None denotes actual missing meaning;
        # an empty category list can prove an internal transfer, rather than zero cash.
        "cash": [
            None if target is None else [[category, value] for category, value in target.items()]
            for target in money_targets(funds).values()
        ],
        "pretax": list(pretax_values(main))
        if any(value for line, value in main.items() if line != 31)
        else None,
        "resources": resources,
    }


def capture_bodies(selection, heads):
    """Batch the original owner inputs, then keep only their consumed financial meaning."""
    heads = tuple(heads)
    journals = selection.lines(heads)
    details = selection.report_details(heads)
    funds = selection.children("result_fund_movement", heads)
    records = selection.repo.financial_projection_resources([head["result_id"] for head in heads])
    resources = {head["result_id"]: {table: [] for table in records} for head in heads}
    for table, rows in records.items():
        for row in rows:
            resources[row["result_id"]][table].append(
                {key: row[key] for key in _RESOURCE_FIELDS[table]}
            )
        for grouped in resources.values():
            grouped[table].sort(key=lambda row: row[CHILD_KEYS[table]])
    selection.load_targets([head for head in heads if selection.kind(head) in _TARGET_VALUES])
    bodies = {}
    for head in heads:
        identity = head["result_id"]
        body = _body(
            selection,
            head,
            journals[identity],
            details[identity],
            funds[identity],
            resources[identity],
        )
        bodies[source_atom(body["source"])] = body
    return bodies
