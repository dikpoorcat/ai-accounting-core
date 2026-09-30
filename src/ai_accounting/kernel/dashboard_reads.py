"""SQL ranges and lazy display records for the existing dashboard projection.

The business selector owns version choice. These helpers only restrict a selected
relation and delay expensive presentation fields until a consumer needs them.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from itertools import islice

from .query_reads import selected_voucher_sql
from .types import YearMonth, canonical, checked


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


def verified_scalar_facts(snapshot, calculations):
    """Verify every candidate source before its scalar columns define a page scope."""
    from .integrity import verify_sources

    calculations = list(calculations)
    if not calculations:
        return {}
    verify_sources(
        snapshot.engine,
        snapshot.connection,
        fact_ids={calc["fact_id"] for calc in calculations},
    )
    return scalar_facts(snapshot, calculations)


def adopted_head_metadata(snapshot, kinds, *, posting_period=None, line_count_period=None):
    """Enumerate adopted calculation heads without loading result bodies.

    A closed publication is selected at that close's verified high-water mark;
    an open publication must still be the current head. The latest adopted
    posting for each subject is the same head chosen by ``Calculations``.
    Details load the matching close adoption proof when an employee is opened.
    When only one posting month's line counts are needed, other months return
    ``None`` (unknown), never zero. The default retains every line count.
    """
    selected_month = YearMonth(posting_period).ordinal if posting_period else None
    line_month = YearMonth(line_count_period).ordinal if line_count_period else None
    periods = [
        row[0]
        for row in snapshot.connection.execute(
            "SELECT period FROM period_close WHERE period<=?"
            + (" AND period=?" if selected_month is not None else "")
            + " ORDER BY period",
            (snapshot.month, selected_month)
            if selected_month is not None
            else (snapshot.month,),
        )
    ]
    closes = snapshot.reads.authoritative_close_rows(
        periods=periods, through_period=snapshot.month
    )
    from .contracts import KernelError

    limits = []
    for row in closes:
        highwater = snapshot.reads.close_header(row).root["small"]["publication_sequence"]
        if type(row["period"]) is not int or type(highwater) is not int:
            raise KernelError("content_integrity_failed", "已冻结的发布序号格式错误")
        limits.append([row["period"], highwater])
    # Parse the verified close limits once across all candidate publications.
    heads = [
        dict(row)
        for row in snapshot.connection.execute(
            "WITH limits(period,highwater) AS MATERIALIZED ("
            "SELECT CAST(json_extract(value,'$[0]') AS INTEGER),"
            "CAST(json_extract(value,'$[1]') AS INTEGER) FROM json_each(?)), "
            "eligible AS (SELECT p.subject_id,p.calculation_id,p.posting_period,p.sequence,"
            "c.fact_id,c.kind,c.period "
            "FROM calculation_publication p "
            "JOIN calculation c ON c.id=p.calculation_id "
            "LEFT JOIN limits l ON l.period=p.posting_period "
            "LEFT JOIN calculation_current cc ON cc.subject_id=p.subject_id "
            "WHERE p.posting_period<=? "
            + ("AND p.posting_period=? " if selected_month is not None else "")
            + "AND c.kind IN (SELECT value FROM json_each(?)) "
            "AND ((l.period IS NOT NULL AND p.sequence<=l.highwater "
            "AND NOT EXISTS(SELECT 1 FROM calculation_publication later "
            "WHERE later.subject_id=p.subject_id AND later.posting_period=p.posting_period "
            "AND later.sequence>p.sequence AND later.sequence<=l.highwater)) "
            "OR (l.period IS NULL AND cc.calculation_id=p.calculation_id))), "
            "heads AS (SELECT *,row_number() OVER(PARTITION BY subject_id "
            "ORDER BY posting_period DESC,sequence DESC) rn FROM eligible) "
            "SELECT subject_id,calculation_id id,posting_period,fact_id,kind,period "
            "FROM heads WHERE rn=1",
            (
                canonical(limits),
                snapshot.month,
                *((selected_month,) if selected_month is not None else ()),
                canonical(sorted(kinds)),
            ),
        )
    ]
    line_ids = {
        row["id"] for row in heads
        if line_month is None or row["posting_period"] == line_month
    }
    snapshot.reads.verify_sql_outcomes(line_ids)
    line_counts = {
        row["id"]: row["line_count"]
        for row in snapshot.connection.execute(
            "SELECT c.id,json_array_length(c.outcome,'$.lines') line_count "
            "FROM json_each(?) ids CROSS JOIN calculation c ON c.id=ids.value",
            (canonical(sorted(line_ids)),),
        )
    }
    for row in heads:
        row["line_count"] = line_counts.get(row["id"])
    return heads


def payroll_head_metadata(snapshot, kinds, *, line_count_period=None):
    return adopted_head_metadata(snapshot, kinds, line_count_period=line_count_period)


def payroll_head_identities(snapshot, heads):
    """Locate each adopted wage's employee through its exact source reference."""
    if not heads:
        return {}
    from .entity_references import verify_hits

    # Every head contributes to the employee list, including old no-line
    # publications absent from the month journal and heads later collapsed to
    # another representative employee. Authenticate the complete recorded role
    # set before any indexed employee identity influences that selection.
    verify_hits(
        snapshot.connection,
        [{"fact_id": ident} for ident in sorted({head["fact_id"] for head in heads})],
        identity_match="recorded" if snapshot.close else "current",
        registry=snapshot.store.registry,
    )
    table = "entity_reference_recorded" if snapshot.close else "entity_reference_current"
    by_fact = {head["fact_id"]: head for head in heads}
    found = {}
    for row in snapshot.connection.execute(
        "SELECT f.id fact_id,r.entity_id,r.kind,r.period,r.source_digest,f.digest "
        f"FROM json_each(?) ids JOIN fact_revision f ON f.id=ids.value "
        f"LEFT JOIN {table} r ON r.fact_id=f.id AND r.role='employee'",
        (canonical(sorted(by_fact)),),
    ):
        head = by_fact[row["fact_id"]]
        if (
            row["entity_id"] is None
            or row["fact_id"] in found and found[row["fact_id"]] != row["entity_id"]
            or row["kind"] != head["kind"]
            or row["period"] != head["period"]
            or row["source_digest"] != row["digest"]
        ):
            from .contracts import KernelError

            raise KernelError(
                "content_integrity_failed", "工资人员来源目录不匹配",
                component="employee", record_id=row["fact_id"],
            )
        found[row["fact_id"]] = row["entity_id"]
    if set(found) != set(by_fact):
        from .contracts import KernelError

        raise KernelError(
            "content_integrity_failed", "工资人员来源目录缺行", component="employee"
        )
    return found


def metric_rows(journal, fields, *, cost_accounts=()):
    """Project only scalar facts and named metric values for whole-domain totals."""
    snapshot = journal.snapshot
    query, parameters = journal.sql()
    snapshot.reads.verify_sql_outcomes(
        row[0]
        for row in snapshot.connection.execute(
            "SELECT DISTINCT basis_calculation_id FROM (" + query + ")", parameters
        )
    )
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
        self["posting_period"] = (
            YearMonth(self["posting_period"]).ordinal
            if self["posting_period"] is not None
            else None
        )

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


def _account_voucher_ids(connection, accounts, cutoff, *, posting_period=None):
    """Locate account candidates within the requested posting scope.

    A single month starts with its period index; an all-history request keeps
    the account index. This only narrows candidates, before the existing
    publication/adoption selector and content checks.
    """
    if posting_period is not None:
        query = (
            "SELECT v.id FROM voucher_version v INDEXED BY voucher_period "
            "WHERE v.period=? AND v.period<=? AND EXISTS(SELECT 1 FROM voucher_line l "
            "WHERE l.version_id=v.id AND l.account IN (SELECT value FROM json_each(?)))"
        )
        parameters = (posting_period, cutoff, canonical(sorted(accounts)))
    else:
        query = (
            "SELECT DISTINCT l.version_id FROM voucher_line l INDEXED BY voucher_line_account "
            "JOIN voucher_version v ON v.id=l.version_id WHERE l.account IN "
            "(SELECT value FROM json_each(?)) AND v.period<=?"
        )
        parameters = (canonical(sorted(accounts)), cutoff)
    return {row[0] for row in connection.execute(query, parameters)}


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

    def _cache(self):
        reads = self.snapshot.reads
        return reads._report_snapshot_cache if reads._snapshot_active else None

    def _key(self, operation):
        return (
            operation,
            self.snapshot.month,
            self.month,
            *(
                None if value is None else frozenset((value,) if isinstance(value, str) else value)
                for value in (self.kinds, self.accounts, self.subjects)
            ),
        )

    def sql(self):
        cache, key = self._cache(), self._key("journal_sql")
        if cache is not None and key in cache:
            query, parameters = cache[key]
            return query, list(parameters)
        query, parameters = self._sql()
        if cache is not None:
            cache[key] = query, tuple(parameters)
        return query, parameters

    def _sql(self):
        voucher_ids = None
        if self.accounts is not None:
            voucher_ids = _account_voucher_ids(
                self.snapshot.connection, self.accounts, self.snapshot.month,
                posting_period=self.month,
            )
        no_close_references = False
        if self.month is not None:
            cache, key = self._cache(), ("open_voucher_period", self.snapshot.month, self.month)
            if cache is not None and key in cache:
                no_close_references = cache[key]
            else:
                connection = self.snapshot.connection
                no_close_references = (
                    connection.execute(
                        "SELECT 1 FROM period_close WHERE period=?", (self.month,)
                    ).fetchone() is None
                    and connection.execute(
                        "SELECT 1 FROM voucher_version v INDEXED BY voucher_period "
                        "CROSS JOIN close_reference r INDEXED BY close_reference_lookup "
                        "ON r.reference_type='voucher' AND r.reference_id=v.id "
                        "AND r.close_period<=? WHERE v.period=? LIMIT 1",
                        (self.snapshot.month, self.month),
                    ).fetchone() is None
                )
                if cache is not None:
                    cache[key] = no_close_references
        source, parameters = selected_voucher_sql(
            self.snapshot.period,
            kinds=self.kinds,
            subject_ids=self.subjects,
            posting_period=str(YearMonth.from_ordinal(self.month))
            if self.month is not None
            else None,
            voucher_ids=voucher_ids,
            no_close_references=no_close_references,
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
        cache, key = self._cache(), self._key("journal_count")
        if cache is not None and key in cache:
            return cache[key]
        query, parameters = self.sql()
        result = self.snapshot.connection.execute(
            f"SELECT count(*) FROM ({query})", parameters
        ).fetchone()[0]
        if cache is not None:
            cache[key] = result
        return result

    def prime_summary(self):
        """Read whole-month counts and line totals through one exact selection.

        Pages that only need a total still use the narrower ``__len__`` query.
        A caller needing all three summaries opts in before asking for a page.
        """
        cache, key = self._cache(), self._key("journal_summary")
        if cache is not None and key in cache:
            return cache[key]
        query, parameters = self.sql()
        # The selector has one row per voucher version. DISTINCT counts that
        # row once after the line join, including a voucher with no lines.
        groups = tuple(
            (
                row["kind"],
                row["reversal"],
                row["count"],
                row["line_count"],
                row["debit"],
                row["credit"],
            )
            for row in self.snapshot.connection.execute(
                "SELECT j.basis_kind kind,j.reverses_id IS NOT NULL reversal,"
                "count(DISTINCT j.id) count,count(l.version_id) line_count,"
                "coalesce(sum(l.debit),0) debit,coalesce(sum(l.credit),0) credit "
                f"FROM ({query}) j LEFT JOIN voucher_line l ON l.version_id=j.id "
                "GROUP BY j.basis_kind,j.reverses_id IS NOT NULL",
                parameters,
            )
        )
        summary = {
            "count": sum(row[2] for row in groups),
            "groups": groups,
            "totals": {
                "line_count": sum(row[3] for row in groups),
                "debit": checked(sum(row[4] for row in groups)),
                "credit": checked(sum(row[5] for row in groups)),
            },
        }
        if cache is not None:
            cache[key] = summary
            cache[self._key("journal_count")] = summary["count"]
        return summary

    def __iter__(self):
        query, parameters = self.sql()
        cursor = self.snapshot.connection.execute(query + " ORDER BY period,number,id", parameters)
        while rows := list(islice(cursor, 100)):
            yield from self.hydrate(rows)

    def hydrate(self, rows):
        from .read_indexes import selected_voucher_references

        self.snapshot.reads.metadata({row["basis_calculation_id"] for row in rows})
        # Lines are prefetched only for an actual returned batch, never summaries.
        self.snapshot.reads.voucher_lines({row["id"] for row in rows})
        frozen = [row["id"] for row in rows if row["close_period"] is not None]
        if frozen:
            references = selected_voucher_references(
                self.snapshot.connection, frozen, through_period=self.snapshot.month
            )
            self.snapshot.reads.verify_close_references(references)
        return [JournalRow(self.snapshot, dict(row)) for row in rows]

    def page(self, after_number, limit, *, voucher_number=None, voucher_version_id=None):
        query, parameters = self.sql()
        total = len(self)
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
        cache, key = self._cache(), self._key("journal_summary")
        if cache is not None and key in cache:
            return dict(cache[key]["totals"])
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
        cache, key = self._cache(), self._key("journal_summary")
        if cache is not None and key in cache:
            return [
                {"kind": kind, "reversal": reversal, "count": count}
                for kind, reversal, count, *_ in cache[key]["groups"]
            ]
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

    def selected(self, *, kinds=None, subjects=None, posting_period=None):
        key = (
            tuple(sorted(kinds)) if kinds is not None else None,
            tuple(sorted(subjects)) if subjects is not None else None,
            posting_period,
        )
        if key not in self.cache:
            selected = self.snapshot.queries._selected_accounting(
                self.snapshot.connection,
                subjects,
                self.snapshot.period,
                kinds=kinds,
                include_lines=False,
                posting_period=posting_period,
            )
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
            member_events = self.snapshot.asset_member_events(kinds=kinds, subjects=subjects)
            member_metadata = self.snapshot.reads.metadata(
                item["calculation_id"] for item in member_events
            )
            member_heads = {}
            for event in member_events:
                if event["direction"] < 0:
                    # A reversal removes exactly its adopted member, not a later
                    # replacement already selected through another batch.
                    old = member_heads.get(event["subject_id"])
                    if old and old["calculation_id"] == event["calculation_id"]:
                        member_heads.pop(event["subject_id"])
                    continue
                member_heads[event["subject_id"]] = event
            for subject, event in member_heads.items():
                record = member_metadata[event["calculation_id"]]
                old = heads.get(subject)
                if old is None or event["adoption_period"] >= old["posting_period"]:
                    heads[subject] = record
            views = {
                subject: self.snapshot.calculation(record["id"])
                for subject, record in heads.items()
            }
            self.cache[key] = views
        return self.cache[key]

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


class FrozenCloseView:
    """A verified close handle; it does not imply all frozen bodies were decoded."""

    def __init__(self, reads, row):
        self.reads, self.row = reads, row
        self.header = reads.close_header(row)

    def section(self, name):
        return self.reads.close_section(self.row, name)


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
            reads = self.snapshot.reads
            row = reads.authoritative_close_rows(periods=(period,))[0]
            self.cache[period] = FrozenCloseView(reads, row)
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


def posted_account_totals(connection, cutoff, *, source="open", reads=None):
    """Shared account totals from a frozen baseline and stored posting increments."""
    row = connection.execute(
        "SELECT period,manifest,digest FROM period_close "
        "WHERE period<=? ORDER BY period DESC LIMIT 1",
        (cutoff,),
    ).fetchone()
    boundary = -1
    totals = {}
    if row is not None:
        boundary = row["period"]
        if reads is not None:
            close_row = reads.authoritative_close_rows(periods=[boundary])[0]
            trial_balance = reads.close_section(close_row, "trial_balance")
        else:
            from .close_storage import read_section, verified_header

            trial_balance = read_section(
                connection, verified_header(connection, row), "trial_balance"
            )
        for item in trial_balance:
            totals[item["account"]] = item["debit"] - item["credit"]
    if source == "open":
        for item in connection.execute(
            "SELECT account,sum(debit-credit) amount FROM ("
            "SELECT account,debit,credit FROM monthly_account WHERE period>? AND period<=? "
            "UNION ALL SELECT account,debit,credit FROM opening_account "
            "WHERE period>? AND period<=?) GROUP BY account",
            (boundary, cutoff, boundary, cutoff),
        ):
            totals[item["account"]] = totals.get(item["account"], 0) + item["amount"]
    return totals


def account_totals(snapshot):
    return posted_account_totals(snapshot.connection, snapshot.month, reads=snapshot.reads)
