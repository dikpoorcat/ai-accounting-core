"""A real ledger-owned SQL replacement, installed only while assembling synthetic hosts.

All writes, schema, calculation revisions and public services stay the original ones.
The alternate historical selection uses grouped maxima and joins instead of correlated
anti-existence predicates. Flow queries deliberately retain every actual adoption leg.
"""

from contextlib import contextmanager
from unittest.mock import patch

from ai_accounting_modular.contracts.public import ContractError, canonical_json
from ai_accounting_modular.ledger import public as ledger_public
from ai_accounting_modular.ledger._read_repository import _in
from ai_accounting_modular.ledger._read_selection import business_kind
from ai_accounting_modular.ledger._repository import (
    _FINANCIAL_FIELDS,
    _FINANCIAL_HEADERS,
    LedgerRepository,
)
from ai_accounting_modular.storage.public import RepositoryFactory

MODES = ("original", "aggregate_join")
_ORIGINAL_FACTORY = ledger_public.repository_factory


def _selected_table(cutoff, business_ids=()):
    if cutoff is None:
        return (
            "adopted_result r JOIN result_slot_current current_slot "
            "ON current_slot.head_result_id=r.result_id",
            (),
        )
    where, args = ["posting_period<=?"], [cutoff]
    _in("business_id", business_ids, where, args)
    latest = (
        "WITH candidates AS MATERIALIZED ("
        "SELECT business_id,result_slot,posting_period FROM adopted_result WHERE "
        + " AND ".join(where)
        + ") SELECT business_id,result_slot,MAX(posting_period) AS posting_period "
        "FROM candidates GROUP BY business_id,result_slot"
    )
    return (
        "adopted_result r JOIN (" + latest + ") selected "
        "ON selected.business_id=r.business_id AND selected.result_slot=r.result_slot "
        "AND selected.posting_period=r.posting_period",
        tuple(args),
    )


class AggregateJoinLedgerRepository(LedgerRepository):
    def heads_through(self, as_of, business_ids=()):
        table, args = _selected_table(as_of, business_ids)
        return [dict(row) for row in self._session.fetchall("SELECT r.* FROM " + table, args)]

    def financial_heads(self, cutoff, *, view, metadata_only=False):
        if view != "as_posted":
            return super().financial_heads(cutoff, view=view, metadata_only=metadata_only)
        table, args = _selected_table(cutoff)
        fields = _FINANCIAL_HEADERS if metadata_only else _FINANCIAL_FIELDS
        return [
            dict(row) for row in self._session.fetchall("SELECT " + fields + " FROM " + table, args)
        ]

    def read_result_scope(
        self,
        scope,
        kinds,
        selection,
        *,
        flow=False,
        business_ids=(),
        result_slots=(),
        source_refs=(),
    ):
        if flow and selection.view == "as_posted":
            return super().read_result_scope(
                scope,
                kinds,
                selection,
                flow=flow,
                business_ids=business_ids,
                result_slots=result_slots,
                source_refs=source_refs,
            )
        table, selected_args = _selected_table(selection.as_of, business_ids)
        clauses, args = ["r.business_period<=?"], [*selected_args, scope["period_to"]]
        if flow:
            clauses.append("r.business_period>=?")
            args.append(scope["period_from"])
        _in(business_kind(), kinds, clauses, args)
        _in("r.business_id", business_ids, clauses, args)
        _in("r.result_slot", result_slots, clauses, args)
        if source_refs:
            clauses.append(
                "r.result_id IN (SELECT b.result_id FROM json_each(?) wanted "
                "JOIN result_basis_ref b ON b.producer_business_id="
                "json_extract(wanted.value,'$.business_id') "
                "AND b.producer_result_slot=json_extract(wanted.value,'$.slot'))"
            )
            args.append(canonical_json(list(source_refs)))
        return table, " AND ".join(clauses), tuple(args)

    def result_references(self, business_ids, *, as_of=None):
        if as_of is None:
            return super().result_references(business_ids)
        table, args = _selected_table(as_of, business_ids)
        fields = (
            "r.business_id,r.result_slot,r.result_id,r.generation,r.business_period,"
            "r.posting_period,r.state,r.baseline_result_id,"
            "v.voucher_id,v.number AS voucher_number"
        )
        rows = self._session.fetchall(
            f"SELECT {fields} FROM {table} LEFT JOIN voucher v ON v.result_id=r.result_id "
            "AND v.role IN ('original','replacement') "
            "WHERE r.business_id IN (SELECT value FROM json_each(?)) "
            "ORDER BY r.business_id,r.result_slot,r.posting_period",
            (*args, canonical_json(list(business_ids))),
        )
        return [
            {key: value for key, value in dict(row).items() if value is not None} for row in rows
        ]


def repository_factory(mode="original"):
    if mode not in MODES:
        raise ContractError("invalid_input", "合成账务实现模式只能是original或aggregate_join。")
    original = _ORIGINAL_FACTORY()
    if mode == "original":
        return original
    return RepositoryFactory(
        original.schema, AggregateJoinLedgerRepository, original.maintenance_build
    )


@contextmanager
def install_ledger_implementation(mode="original"):
    """Create/reopen every kernel inside this context; no catalog or file is altered here."""
    selected = repository_factory(mode)
    with patch.object(ledger_public, "repository_factory", lambda: selected):
        yield selected
