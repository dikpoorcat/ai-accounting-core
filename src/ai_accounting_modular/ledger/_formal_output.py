"""Formal accounting state and its effective journal are separate comparisons."""

from __future__ import annotations

import json
from collections import defaultdict

from ai_accounting_modular.contracts.public import canonical_json, content_digest, parse_fen

from ._resource_rows import resource_rows


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
    """Find the full effective target through heads that needed no new journal.

    A reversal without a replacement means a genuinely empty target. It must
    stop inheritance just as an explicit withdrawal does.
    """
    while row is not None and row["target_state"] != "withdrawn":
        vouchers = repo.vouchers([row["result_id"]])
        voucher = next((v for v in vouchers if v["role"] in ("original", "replacement")), None)
        if voucher is not None:
            return voucher
        if any(v["role"] == "reversal" for v in vouchers) or row["baseline_result_id"] is None:
            break
        row = repo.result(row["baseline_result_id"])
    return None


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


def same_formal_output(repo, row, plan):
    if (
        row["target_digest"] != content_digest(plan["target"])
        or row["business_period"] != plan["accounting_period"]
        or _basis_values(json.loads(row["basis_json"])) != _basis_values(plan["basis"])
        or not same_journal(target_lines(repo, row), plan["lines"])
    ):
        return False
    for table, desired in resource_rows(row["result_id"], plan).items():
        if table == "result_source":
            continue
        existing = [list(child.values())[1:] for child in repo.children(table, [row["result_id"]])]
        expected = [list(child)[1:] for child in desired]
        if sorted(existing, key=canonical_json) != sorted(expected, key=canonical_json):
            return False
    return True
