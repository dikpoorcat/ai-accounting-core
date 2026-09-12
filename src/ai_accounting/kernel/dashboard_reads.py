"""SQL ranges and lazy display records for the existing dashboard projection.

The business selector owns version choice. These helpers only restrict a selected
relation and delay expensive presentation fields until a consumer needs them.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from itertools import islice

from .query_reads import selected_voucher_sql
from .types import YearMonth, canonical


def page_keys(keys, after=None, limit=100, *, total_count=None):
    """Choose stable presentation keys before expanding their facts or sources."""
    from .contracts import KernelError

    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("每页数量必须为 1 至 500")
    ordered = list(keys)
    start = 0
    if after is not None:
        try:
            start = ordered.index(after) + 1
        except ValueError as exc:
            raise KernelError(
                "dashboard_snapshot_changed", "分页位置已变化，请重新加载明细。"
            ) from exc
    picked = ordered[start : start + limit]
    more = start + len(picked) < len(ordered)
    return picked, {
        "total_count": len(ordered) if total_count is None else total_count,
        "filtered_count": len(ordered),
        "returned_count": len(picked),
        "has_more": more,
        "next_cursor": picked[-1] if more else None,
    }


def scalar_facts(snapshot, calculations):
    """Read normalized scalar columns without loading any growing child collection."""
    from .schema import table_name
    from .storage import decode_fields

    by_kind = {}
    for calc in calculations:
        by_kind.setdefault(calc["kind"], set()).add(calc["fact_id"])
    result = {}
    for kind, identifiers in by_kind.items():
        for row in snapshot.connection.execute(
            f"SELECT f.* FROM {table_name(kind)} f JOIN json_each(?) ids "
            "ON f.revision_id=ids.value",
            (canonical(sorted(identifiers)),),
        ):
            result[row["revision_id"]] = decode_fields(
                snapshot.store.registry.models[kind], dict(row)
            )
    return result


def metric_rows(journal, fields, *, cost_accounts=()):
    """Project only scalar facts and named metric values for whole-domain totals."""
    snapshot = journal.snapshot
    query, parameters = journal.sql()
    pairs = ",".join(f"'{field}',json_extract(c.outcome,'$.values.{field}')" for field in fields)
    rows = snapshot.connection.execute(
        f"SELECT j.*,json_object({pairs}) AS metric_values,coalesce((SELECT sum(l.debit-l.credit) "
        "FROM voucher_line l WHERE l.version_id=j.id AND l.account IN "
        f"(SELECT value FROM json_each(?))),0) AS metric_cost FROM ({query}) j "
        "JOIN calculation c ON c.id=j.basis_calculation_id ORDER BY j.period,j.number,j.id",
        [canonical(sorted(cost_accounts)), *parameters],
    ).fetchall()
    metadata = snapshot.reads.metadata({row["basis_calculation_id"] for row in rows})
    scalar = scalar_facts(snapshot, metadata.values())
    result = []
    for row in rows:
        record = dict(row)
        meta = metadata[row["basis_calculation_id"]]
        values = {
            key: value
            for key, value in json.loads(row["metric_values"]).items()
            if value is not None
        }
        record["basis"] = dict(snapshot.calculation(meta["id"])) | {
            "fact": {
                "id": meta["fact_id"],
                "revision": meta["fact_revision"],
                "data": scalar[meta["fact_id"]],
            },
            "outcome": {"values": values},
        }
        record["sign"] = -1 if record["reverses_id"] else 1
        record["calculation_id"] = meta["id"]
        result.append(record)
    return result


class CalculationView(dict):
    def __init__(self, snapshot, metadata):
        from .types import YearMonth

        super().__init__(metadata)
        self.snapshot = snapshot
        self["digest"] = bytes.fromhex(self["result_digest"])
        self["period"] = YearMonth(self["period"]).ordinal
        self["posting_period"] = YearMonth(self["posting_period"]).ordinal

    def __getitem__(self, key):
        if key == "fact" and key not in self:
            self[key] = self.snapshot.fact(super().__getitem__("fact_id"))
        if key == "outcome" and key not in self:
            self[key] = self.snapshot.reads.calculation(super().__getitem__("id"))["outcome"]
        return super().__getitem__(key)

    def get(self, key, default=None):
        return self[key] if key in self or key in {"fact", "outcome"} else default

    def __or__(self, other):
        self["fact"]
        self["outcome"]
        return dict(self) | other


class JournalRow(dict):
    def __init__(self, snapshot, metadata):
        super().__init__(metadata)
        self.snapshot = snapshot
        self["calculation_id"] = self["basis_calculation_id"]
        self["sign"] = -1 if self["reverses_id"] else 1

    def __getitem__(self, key):
        if key == "basis" and key not in self:
            self[key] = self.snapshot.calculation(super().__getitem__("basis_calculation_id"))
        if key == "lines" and key not in self:
            self[key] = self.snapshot.reads.voucher_lines((super().__getitem__("id"),))[
                super().__getitem__("id")
            ]
        return super().__getitem__(key)

    def get(self, key, default=None):
        return self[key] if key in self or key in {"basis", "lines"} else default


class Journal:
    def __init__(self, snapshot, *, month=None, kinds=None, accounts=None, subjects=None):
        self.snapshot, self.month = snapshot, month
        self.kinds, self.accounts, self.subjects = kinds, accounts, subjects

    def select(self, *, kinds=None, accounts=None, subjects=None):
        return Journal(
            self.snapshot,
            month=self.month,
            kinds=kinds if kinds is not None else self.kinds,
            accounts=accounts if accounts is not None else self.accounts,
            subjects=subjects if subjects is not None else self.subjects,
        )

    def sql(self):
        voucher_ids = None
        if self.accounts is not None:
            voucher_ids = {
                row[0]
                for row in self.snapshot.connection.execute(
                    "SELECT DISTINCT l.version_id FROM voucher_line l "
                    "INDEXED BY voucher_line_account "
                    "JOIN voucher_version v ON v.id=l.version_id WHERE l.account IN "
                    "(SELECT value FROM json_each(?)) AND v.period<=?"
                    + (" AND v.period=?" if self.month is not None else ""),
                    [canonical(sorted(self.accounts)), self.snapshot.month]
                    + ([self.month] if self.month is not None else []),
                )
            }
        source, parameters = selected_voucher_sql(
            self.snapshot.period,
            kinds=self.kinds,
            subject_ids=self.subjects,
            posting_period=str(YearMonth.from_ordinal(self.month))
            if self.month is not None
            else None,
            voucher_ids=voucher_ids,
        )
        query = f"SELECT j.* FROM ({source}) j WHERE 1=1"
        if self.month is not None:
            query += " AND j.period=?"
            parameters.append(self.month)
        if self.accounts is not None:
            query += (
                " AND EXISTS(SELECT 1 FROM voucher_line l WHERE l.version_id=j.id "
                "AND l.account IN (SELECT value FROM json_each(?)))"
            )
            parameters.append(canonical(sorted(self.accounts)))
        return query, parameters

    def __len__(self):
        query, parameters = self.sql()
        return self.snapshot.connection.execute(
            f"SELECT count(*) FROM ({query})", parameters
        ).fetchone()[0]

    def __iter__(self):
        query, parameters = self.sql()
        cursor = self.snapshot.connection.execute(query + " ORDER BY period,number,id", parameters)
        while rows := list(islice(cursor, 100)):
            yield from self.hydrate(rows)

    def hydrate(self, rows):
        from .read_indexes import verify_close_references

        self.snapshot.reads.metadata({row["basis_calculation_id"] for row in rows})
        # Lines are prefetched only for an actual returned batch, never summaries.
        self.snapshot.reads.voucher_lines({row["id"] for row in rows})
        frozen = [row["id"] for row in rows if row["close_period"] is not None]
        if frozen:
            references = self.snapshot.connection.execute(
                "SELECT r.* FROM json_each(?) ids JOIN close_reference r "
                "ON r.reference_id=ids.value WHERE r.reference_type='voucher' "
                "AND r.close_period<=?",
                (canonical(frozen), self.snapshot.month),
            ).fetchall()
            verify_close_references(self.snapshot.connection, references)
        return [JournalRow(self.snapshot, dict(row)) for row in rows]

    def page(self, after_number, limit, *, voucher_number=None, voucher_version_id=None):
        query, parameters = self.sql()
        total = self.snapshot.connection.execute(
            f"SELECT count(*) FROM ({query})", parameters
        ).fetchone()[0]
        query += (
            " AND j.id=?"
            if voucher_version_id is not None
            else " AND j.number=?"
            if voucher_number is not None
            else " AND j.number>?"
        )
        parameters.append(
            voucher_version_id
            if voucher_version_id is not None
            else voucher_number
            if voucher_number is not None
            else after_number
        )
        rows = self.snapshot.connection.execute(
            query + " ORDER BY j.number,j.id LIMIT ?", [*parameters, limit + 1]
        ).fetchall()
        more = len(rows) > limit
        records = self.hydrate(rows[:limit])
        return records, {
            "total_count": total,
            "filtered_count": total,
            "returned_count": len(records),
            "has_more": more,
            "next_cursor": records[-1]["number"] if more else None,
        }

    def totals(self):
        query, parameters = self.sql()
        return dict(
            self.snapshot.connection.execute(
                "SELECT count(*) AS line_count,coalesce(sum(l.debit),0) AS debit,"
                f"coalesce(sum(l.credit),0) AS credit FROM ({query}) j "
                "JOIN voucher_line l ON l.version_id=j.id",
                parameters,
            ).fetchone()
        )

    def kind_counts(self):
        query, parameters = self.sql()
        return [
            dict(row)
            for row in self.snapshot.connection.execute(
                "SELECT basis_kind AS kind,reverses_id IS NOT NULL AS reversal,count(*) AS count "
                f"FROM ({query}) GROUP BY basis_kind,reverses_id IS NOT NULL",
                parameters,
            )
        ]


class Calculations(Mapping):
    """Selected business heads, loaded by the exact domain or subject requested."""

    def __init__(self, snapshot):
        self.snapshot, self.cache = snapshot, {}
        self.unestablished_cache = {}

    def selected(self, *, kinds=None, subjects=None):
        key = (
            tuple(sorted(kinds)) if kinds is not None else None,
            tuple(sorted(subjects)) if subjects is not None else None,
        )
        if key not in self.cache:
            selected = self.snapshot.queries._selected_accounting(
                self.snapshot.connection,
                subjects,
                self.snapshot.period,
                kinds=kinds,
                include_lines=False,
            )
            self.unestablished_cache[key] = selected["through_period"][
                "unestablished_state_selections"
            ]
            events = selected["through_period"]["voucher_events"]
            states = selected["through_period"]["state_results"]
            identifiers = {item["calculation_id"] for item in [*events, *states]}
            metadata = self.snapshot.reads.metadata(identifiers)
            heads = {}
            for record in metadata.values():
                old = heads.get(record["subject_id"])
                if old is None or (record["posting_period"], record["fact_revision"]) > (
                    old["posting_period"],
                    old["fact_revision"],
                ):
                    heads[record["subject_id"]] = record
            self.cache[key] = {
                subject: self.snapshot.calculation(record["id"])
                for subject, record in heads.items()
            }
        return self.cache[key]

    def unestablished_entities(self, *, kinds, field):
        """Project exact candidate identity declarations without adopting a result."""
        self.selected(kinds=kinds)
        selections = self.unestablished_cache[tuple(sorted(kinds)), None]
        identifiers = {
            candidate["calculation_id"]
            for selection in selections
            for candidate in selection["candidates"]
        }
        metadata = self.snapshot.reads.metadata(identifiers, state=False)
        scalars = scalar_facts(self.snapshot, metadata.values())
        batch_ids = {
            record["fact_id"]
            for record in metadata.values()
            if record["kind"] == "reimbursed_asset_batch"
        }
        batch_entities = {ident: [] for ident in batch_ids}
        if batch_ids:
            for row in self.snapshot.connection.execute(
                "SELECT f.revision_id,f.asset_id,f.asset_type FROM json_each(?) ids "
                "JOIN fact_reimbursed_asset_batch_assets f ON f.revision_id=ids.value "
                "ORDER BY f.revision_id,f.item_no",
                (canonical(sorted(batch_ids)),),
            ):
                batch_entities[row["revision_id"]].append((row["asset_id"], row["asset_type"]))
        entities = {}
        for selection in selections:
            declarations = {}
            for candidate in selection["candidates"]:
                record = metadata[candidate["calculation_id"]]
                data = scalars[record["fact_id"]]
                identities = []
                if field in data and isinstance(data[field], str):
                    identities.append((data[field], data.get("asset_type")))
                elif field == "asset_id" and record["kind"] in {
                    "asset",
                    "opening_asset",
                    "reimbursed_asset",
                }:
                    # These typed card models define their subject as the asset
                    # identity; batch/lifecycle subjects are never substituted.
                    identities.append((record["subject_id"], data.get("asset_type")))
                elif field == "asset_id" and record["kind"] == "reimbursed_asset_batch":
                    identities.extend(batch_entities[record["fact_id"]])
                for identity, asset_type in identities:
                    declarations.setdefault(identity, set()).add(asset_type)
            for identity, types in declarations.items():
                entry = entities.setdefault(identity, {"candidate_selections": [], "types": set()})
                entry["candidate_selections"].append(selection)
                entry["types"].update(types)
        for entry in entities.values():
            entry["asset_type"] = next(iter(entry["types"])) if len(entry["types"]) == 1 else None
            entry.pop("types")
            candidates = {
                item["calculation_id"]: item
                for selection in entry["candidate_selections"]
                for item in selection["candidates"]
            }
            entry["trace_targets"] = [
                {
                    "calculation_id": ident,
                    "voucher_version_id": None,
                    "fact_id": item["fact_id"],
                    "result_digest": item["result_digest"],
                    "selection_status": "unestablished",
                }
                for ident, item in sorted(candidates.items())
            ]
        return entities

    def __getitem__(self, key):
        if key is None:
            raise KeyError(key)
        return self.selected(subjects={key})[key]

    def __iter__(self):
        return iter(self.selected())

    def __len__(self):
        return len(self.selected())

    def values(self):
        return self.selected().values()


class ClosedPeriods(Mapping):
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.periods = tuple(
            row[0]
            for row in snapshot.connection.execute(
                "SELECT period FROM period_close WHERE period<=? ORDER BY period", (snapshot.month,)
            )
        )
        self.cache = {}

    def __getitem__(self, period):
        if period not in self.periods:
            raise KeyError(period)
        if period not in self.cache:
            self.cache[period] = json.loads(
                self.snapshot.reads.close_rows(periods=(period,))[0]["manifest"]
            )
        return self.cache[period]

    def __iter__(self):
        return iter(self.periods)

    def __len__(self):
        return len(self.periods)


class FrozenFacts:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def __contains__(self, ident):
        return (
            self.snapshot.connection.execute(
                "SELECT 1 FROM close_reference r WHERE r.close_period<=? AND "
                "((r.reference_type='fact' AND r.reference_id=?) OR "
                "(r.reference_type='calculation' AND EXISTS(SELECT 1 FROM calculation c "
                "WHERE c.id=r.reference_id AND c.fact_id=?)) OR "
                "(r.reference_type='calculation' AND EXISTS(SELECT 1 FROM dependency_fact d "
                "WHERE d.calculation_id=r.reference_id AND d.fact_id=?))) LIMIT 1",
                (self.snapshot.month, ident, ident, ident),
            ).fetchone()
            is not None
        )


def account_totals(snapshot):
    """Exact frozen trial balance plus only the subsequent synchronous increments."""
    cutoff = snapshot.month
    row = snapshot.connection.execute(
        "SELECT period,json_extract(manifest,'$.trial_balance') AS trial_balance "
        "FROM period_close WHERE period<=? ORDER BY period DESC LIMIT 1",
        (cutoff,),
    ).fetchone()
    boundary = -1
    totals = {}
    if row is not None and row["trial_balance"] is not None:
        boundary = row["period"]
        for item in json.loads(row["trial_balance"]):
            totals[item["account"]] = item["debit"] - item["credit"]
    for item in snapshot.connection.execute(
        "SELECT account,sum(debit-credit) amount FROM ("
        "SELECT account,debit,credit FROM monthly_account WHERE period>? AND period<=? "
        "UNION ALL SELECT account,debit,credit FROM opening_account WHERE period>? AND period<=?) "
        "GROUP BY account",
        (boundary, cutoff, boundary, cutoff),
    ):
        totals[item["account"]] = totals.get(item["account"], 0) + item["amount"]
    return totals
