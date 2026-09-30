"""Released v1 read-only proof of immutable open-report contributions.

Rebuild only the saved contribution inputs.  Publication writes, ordinary
report selection, and repair remain in the current implementation.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass

from .content_v1 import _V1YearMonth
from .contracts import KernelError
from .history_encoding_v1 import canonical
from .query_relations_v1 import resolve_calculation_relations
from .report_party_v1 import RECLASS
from .report_semantics_v1 import compact_line_fields, immutable_line_fields, report_source_fact


@dataclass(frozen=True)
class OpenContribution:
    publication_id: str
    calculation_id: str
    content: str
    checksum: bytes


def _invalid(record_id, reason):
    raise KernelError(
        "content_integrity_failed",
        "历史报表行贡献与正式来源不一致",
        component="report_open_contribution",
        record_id=record_id,
        reason=reason,
    )


def _record(source, ident):
    row = source["calculations"].get(ident)
    if row is None:
        _invalid(ident, "calculation_missing")
    fact = source["facts"].get(row["fact_id"])
    if fact is None:
        _invalid(ident, "fact_missing")
    return {
        **row,
        "period": str(_V1YearMonth.from_ordinal(row["period"])),
        "result_digest": row["digest"].hex(),
        "fact_data": fact["data"],
        "outcome": row["decoded"],
        "decoded": row["decoded"],
    }


def _needs_ancestry(row, resolution):
    related = any(
        item.get("line_no") == row["line_no"]
        and item.get("role") not in {"funds", "tax_transfer", "reserve_expense"}
        for item in resolution["line_relations"]
    )
    return not related and row["account"] not in resolution["own_party_accounts"]


def _resolution_content(resolution):
    return {
        "issues": resolution["issues"],
        "line_relations": resolution["line_relations"],
        "own_party_accounts": sorted(resolution["own_party_accounts"]),
        "party_candidates_by_account": [
            [account, sorted(keys, key=canonical)]
            for account, keys in sorted(resolution["party_candidates_by_account"].items())
        ],
    }


def _publication_rows(connection, calculation_id):
    return list(
        connection.execute(
            "SELECT v.id version_id,v.calculation_id,v.reverses_id,l.line_no,l.account,"
            "l.debit,l.credit,l.cashflow FROM voucher_version v "
            "INDEXED BY voucher_calculation CROSS JOIN voucher_line l ON l.version_id=v.id "
            "WHERE v.calculation_id=? AND v.reverses_id IS NULL "
            "ORDER BY v.id,l.line_no",
            (calculation_id,),
        )
    )


def prepare_open_contribution(
    connection, publication, source, *, records=None, publication_rows=None
):
    """Rebuild one v1 body from independently authenticated source rows."""

    publication_id = publication["id"]
    calculation_id = publication["calculation_id"]
    if not calculation_id:
        _invalid(publication_id, "publication_without_calculation")
    records = {} if records is None else records

    def calculation(ident):
        if ident not in records:
            records[ident] = _record(source, ident)
        return records[ident]

    root = calculation(calculation_id)
    parent_ids = tuple(sorted(source["dependencies"][calculation_id]))
    loaded_sources = set()

    def load_direct(ident):
        loaded_sources.add(ident)
        return calculation(ident)

    relation = resolve_calculation_relations(
        root,
        load_calculation=load_direct,
        load_parents=lambda ident: source["dependencies"][ident],
        collect_ancestry=False,
    )
    if not loaded_sources.issubset(parent_ids):
        _invalid(publication_id, "direct_source_not_parent")
    used_ids = {calculation_id} | loaded_sources
    bindings = []
    for ident in sorted(used_ids):
        calculation_row = source["calculations"][ident]
        fact_id = calculation_row["fact_id"]
        bindings.append(
            [
                ident,
                calculation_row["digest"].hex(),
                fact_id,
                source["facts"][fact_id]["digest"].hex(),
            ]
        )
    facts = {}

    def source_fact(ident):
        if ident not in facts:
            facts[ident] = report_source_fact(calculation(ident))
        return facts[ident]

    projected = []
    usable = True
    rows = (
        _publication_rows(connection, calculation_id)
        if publication_rows is None else publication_rows
    )
    for row in rows:
        fields = immutable_line_fields(
            row,
            calculation_id,
            calculation=calculation,
            source_fact=source_fact,
            relations=lambda _ident: relation,
        )
        projected.append(
            [
                row["version_id"],
                row["line_no"],
                row["account"],
                row["debit"],
                row["credit"],
                row["cashflow"],
                compact_line_fields(fields),
            ]
        )
        if row["account"] in RECLASS and _needs_ancestry(row, relation):
            usable = False
    content = canonical(
        {
            "format": "ai-accounting-kernel/2/report-open-contribution/1",
            "publication_id": publication_id,
            "calculation_id": calculation_id,
            "result_digest": root["result_digest"],
            "fact_id": root["fact_id"],
            "parents": parent_ids,
            "source_bindings": bindings,
            "rows": projected,
            "resolution": _resolution_content(relation),
            "usable": usable,
        }
    )
    return OpenContribution(
        publication_id,
        calculation_id,
        content,
        hashlib.sha256(content.encode("utf-8")).digest(),
    )


def compare_open_contributions(engine, connection, *, check_bodies=True, verified_source=None):
    """Verify all v1 anchors and optionally all repairable bodies by rebuild."""

    if verified_source is None:
        from .integrity import _check_sources

        verified_source = _check_sources(engine, connection)
    records = {}
    vouchers = defaultdict(list)
    for voucher in verified_source["vouchers"].values():
        if voucher["reverses_id"] is None:
            vouchers[voucher["calculation_id"]].append(voucher)

    def owned_lines(calculation_id):
        # _check_sources has checked the saved order and complete line content.
        for voucher in sorted(vouchers[calculation_id], key=lambda item: item["id"]):
            for number, line in enumerate(voucher["lines"], start=1):
                yield {
                    "version_id": voucher["id"], "calculation_id": calculation_id,
                    "reverses_id": None, "line_no": number, **line,
                }
    expected = {
        row["id"]: prepare_open_contribution(
            connection, row, verified_source, records=records,
            publication_rows=owned_lines(row["calculation_id"]),
        )
        for row in connection.execute(
            "SELECT id,calculation_id FROM calculation_publication "
            "WHERE calculation_id IS NOT NULL ORDER BY sequence"
        )
    }
    anchors = {
        row["publication_id"]: row
        for row in connection.execute("SELECT * FROM report_open_contribution_anchor")
    }
    if set(anchors) != set(expected):
        _invalid("all", "anchor_coverage_mismatch")
    for ident, prepared in expected.items():
        row = anchors[ident]
        if (
            row["calculation_id"] != prepared.calculation_id
            or row["content_digest"] != prepared.checksum
        ):
            _invalid(ident, "anchor_rebuild_mismatch")
    if check_bodies:
        bodies = {
            row["publication_id"]: row
            for row in connection.execute("SELECT * FROM report_open_contribution")
        }
        if set(bodies) != set(expected):
            _invalid("all", "body_coverage_mismatch")
        for ident, prepared in expected.items():
            row = bodies[ident]
            if row["content"] != prepared.content or row["content_digest"] != prepared.checksum:
                _invalid(ident, "body_rebuild_mismatch")
    return expected
