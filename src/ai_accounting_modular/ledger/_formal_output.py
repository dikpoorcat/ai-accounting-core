"""Formal accounting state and its effective journal are separate comparisons."""

from __future__ import annotations

import json
from collections import defaultdict

from ai_accounting_modular.contracts.public import canonical_json, content_digest, parse_fen

from ._resource_rows import resource_rows
from ._values import fail


def _source_relation(reference):
    return reference["business_id"], reference["result_slot"]


def _basis_values(basis):
    """Keep accounting values; a replaced source locator is not a new value.

    Domains put only actually used accounting input in Basis. Optional dates
    needed for accounting therefore participate; management-only dates stay in
    the current Fact rather than being copied into frozen accounting evidence.
    """
    values = {key: value for key, value in basis.items() if key != "source_locations"}
    if "source_results" in values:
        values["source_results"] = sorted(
            {_source_relation(ref) for ref in values["source_results"]}
        )
    if "baseline_ref" in values:
        values["baseline_ref"] = _source_relation(values["baseline_ref"])
    for key in ("adopted_quantities", "adopted_cards", "policies"):
        if key in values:
            values[key] = sorted(values[key], key=canonical_json)
    return values


def target_voucher(repo, row):
    return target_vouchers(repo, [row])[row["result_id"]] if row is not None else None


def target_vouchers(repo, heads, *, headers_only=False):
    """Select effective journals or headers with the same inheritance, never add deltas.

    Formal-only heads inherit their baseline. Withdrawal or a reversal without
    replacement ends inheritance, including an explicitly empty corrected target.
    """
    load_heads = repo.read_heads if headers_only else repo.results
    load_vouchers = repo.read_voucher_headers if headers_only else repo.vouchers
    rows = {head["result_id"]: head for head in heads}
    pending = {key: key for key in rows}
    seen, result = set(), {}
    while pending:
        ids = set(pending.values())
        missing = ids - rows.keys()
        if missing:
            rows.update((row["result_id"], row) for row in load_heads(missing))
        grouped = defaultdict(list)
        for voucher in load_vouchers(ids):
            grouped[voucher["result_id"]].append(voucher)
        following = {}
        for head_id, result_id in pending.items():
            if (head_id, result_id) in seen or result_id not in rows:
                fail("storage_corrupt", "完整正式目标的凭证基线不存在或形成循环。")
            seen.add((head_id, result_id))
            row, vouchers = rows[result_id], grouped[result_id]
            own = next((v for v in vouchers if v["role"] in ("original", "replacement")), None)
            if row["target_state"] == "withdrawn":
                result[head_id] = None
            elif own is not None:
                result[head_id] = own
            elif (
                any(v["role"] == "reversal" for v in vouchers) or row["baseline_result_id"] is None
            ):
                result[head_id] = None
            else:
                following[head_id] = row["baseline_result_id"]
        pending = following
    return result


def target_lines(repo, row):
    voucher = target_voucher(repo, row)
    return voucher["lines"] if voucher else []


def _line_totals(lines):
    grouped = defaultdict(lambda: [0, 0])
    for line in lines:
        key = line["account_code"], line.get("party_id"), line.get("object_id")
        grouped[key][0] += parse_fen(line["debit_fen"])
        grouped[key][1] += parse_fen(line["credit_fen"])
    return sorted(([list(key), value] for key, value in grouped.items()), key=canonical_json)


def same_journal(existing_lines, planned_lines):
    return _line_totals(existing_lines) == _line_totals(planned_lines)


def _formal_children(table, rows, *, frozen):
    if table != "result_report_contribution" or not frozen:
        return rows
    values = []
    for row in rows:
        value = list(row)
        if value[-1] is not None:
            issue = json.loads(value[-1])
            issue.pop("reusable_sources", None)
            value[-1] = canonical_json(issue)
        values.append(value)
    return values


def same_formal_output(repo, row, plan):
    if (
        row["target_digest"] != content_digest(plan["target"])
        or row["business_period"] != plan["accounting_period"]
        or _basis_values(json.loads(row["basis_json"])) != _basis_values(plan["basis"])
        or not same_journal(target_lines(repo, row), plan["lines"])
    ):
        return False
    for table, desired in resource_rows(row["result_id"], plan).items():
        if table in {"result_source", "result_basis_ref"}:
            continue
        existing = [list(child.values())[1:] for child in repo.children(table, [row["result_id"]])]
        expected = [list(child)[1:] for child in desired]
        existing = _formal_children(table, existing, frozen=row["state"] == "frozen")
        expected = _formal_children(table, expected, frozen=row["state"] == "frozen")
        if sorted(existing, key=canonical_json) != sorted(expected, key=canonical_json):
            return False
    return True
