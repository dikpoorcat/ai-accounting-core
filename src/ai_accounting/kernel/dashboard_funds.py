"""Bounded presentation of selected funds, statements and investment costs.

Version choice stays in the shared business selector. SQL below aggregates stored
effects and selects page keys; only returned rows need facts and display sources.
"""

from __future__ import annotations

import json

from .contracts import KernelError
from .dashboard_reads import page_keys
from .domains.money import FUNDS_ACCOUNT_TYPE_BY_BALANCE_CATEGORY
from .text_sort import pinyin_key
from .types import YearMonth, canonical, checked

FUND_TYPES = FUNDS_ACCOUNT_TYPE_BY_BALANCE_CATEGORY
SECTIONS = {"accounts", "movements", "statements", "investment_products", "investment_events"}

# Each actual fact drives its own complete role lookup. The role-only index
# would scan all matching roles for each candidate; keep every path for this
# fact so malformed or duplicate bank roles still trigger the full fallback.
_BANK_IDENTITY_SCALARS_SQL = (
    "SELECT c.id,c.period,CASE c.kind WHEN 'bank_statement' THEN b.bank_account_id "
    "WHEN 'bank_opening' THEN o.bank_account_id ELSE x.bank_account_id END bank_id,"
    "CASE c.kind WHEN 'bank_statement' THEN b.period "
    "WHEN 'bank_opening' THEN o.period ELSE x.period END scalar_period,"
    "r.path,r.role,r.entity_id,r.kind,r.period role_period,"
    "r.source_digest role_digest,f.digest fact_digest "
    "FROM json_each(?) ids JOIN calculation c ON c.id=ids.value "
    "LEFT JOIN fact_revision f ON f.id=c.fact_id "
    "LEFT JOIN fact_bank_statement b ON b.revision_id=c.fact_id "
    "LEFT JOIN fact_bank_reconciliation x ON x.revision_id=c.fact_id "
    "LEFT JOIN fact_bank_opening o ON o.revision_id=c.fact_id "
    "LEFT JOIN entity_reference_recorded r "
    "INDEXED BY sqlite_autoindex_entity_reference_recorded_1 ON r.fact_id=c.fact_id "
    "AND r.role='bank_account'"
)


def _sum(rows, field):
    values = [row[field] for row in rows]
    return None if None in values else sum(values)


def _finish_sql_page(found, *, after, limit):
    total, count = found[0]["total_count"], found[0]["filtered_count"]
    if after is not None and not found[0]["cursor_present"]:
        raise KernelError("dashboard_snapshot_changed", "分页位置已变化，请重新加载明细。")
    rows = [row for row in found if row["page_key"] is not None]
    more = len(rows) > limit
    rows = rows[:limit]
    return rows, {
        "total_count": total,
        "filtered_count": count,
        "returned_count": len(rows),
        "has_more": more,
        "next_cursor": rows[-1]["page_key"] if more else None,
    }


def _sql_summary_page(
    connection,
    source,
    parameters,
    summary_sql,
    summary_columns,
    *,
    after,
    limit,
    where="1=1",
    filters=(),
    order_rows=None,
):
    """Return a full source summary and bounded page from one selected event set."""
    summary_json = "json_group_array(json_array(" + ",".join(summary_columns) + "))"
    order_values = []
    if order_rows is not None:
        candidates = connection.execute(
            f"WITH source_rows AS MATERIALIZED ({source}), "
            f"summary_rows AS MATERIALIZED ({summary_sql}) "
            f"SELECT page_key,calculation_id,internal_transfer FROM source_rows WHERE {where}",
            [*parameters, *filters],
        ).fetchall()
        ordered = order_rows(candidates)
        picked, _ = page_keys(ordered, after, limit + 1 if limit < 500 else 500)
        # page_keys limits public sizes to 500; the final page still needs one
        # extra scalar candidate to establish continuation at the maximum size.
        if limit == 500:
            start = ordered.index(after) + 1 if after is not None else 0
            picked = ordered[start:start + limit + 1]
        order_values = [canonical(picked)]
        page_relation = (
            "page_rows AS (SELECT f.*,CAST(ids.key AS INTEGER) sort_position "
            "FROM json_each(?) ids JOIN filtered_rows f ON f.page_key=ids.value) "
        )
        page_order = "page_rows.sort_position"
    else:
        page_relation = (
            "page_rows AS (SELECT * FROM filtered_rows WHERE page_key>? "
            "ORDER BY page_key LIMIT ?) "
        )
        order_values = [after or "", limit + 1]
        page_order = "page_rows.page_key"
    query = (
        f"WITH source_rows AS MATERIALIZED ({source}), "
        f"summary_rows AS MATERIALIZED ({summary_sql}), "
        f"filtered_rows AS MATERIALIZED (SELECT * FROM source_rows WHERE {where}), "
        + page_relation +
        "SELECT page_rows.*, counts.total_count, counts.filtered_count, "
        f"counts.cursor_present,CASE WHEN row_number() OVER (ORDER BY {page_order})=1 "
        "THEN counts.summary_json END summary_json FROM (SELECT "
        "(SELECT count(*) FROM source_rows) total_count, "
        "(SELECT count(*) FROM filtered_rows) filtered_count, "
        "(SELECT count(*) FROM filtered_rows WHERE page_key=?) cursor_present, "
        f"(SELECT {summary_json} FROM summary_rows) summary_json) counts "
        f"LEFT JOIN page_rows ON 1=1 ORDER BY {page_order}"
    )
    found = connection.execute(
        query, [*parameters, *filters, *order_values, after]
    ).fetchall()
    summary = json.loads(found[0]["summary_json"])
    if any(row["summary_json"] is not None for row in found[1:]):
        raise ValueError("funds summary returned more than once")
    rows, page = _finish_sql_page(found, after=after, limit=limit)
    return [dict(zip(summary_columns, values, strict=True)) for values in summary], rows, page


def _bank_identity_witness_heads(snap):
    """Bound unused frozen bank states only after positive account coverage.

    Actual headers, scalar roles and registration locate every candidate; they
    are not body/adoption proofs. The ordinary selector still authenticates the
    chosen exact witnesses, all open/month states and independent openings.
    Missing roles never establish absence: an incomplete guard uses full scope.
    """
    from . import close_storage, publication
    from .content_history_context import close_reader, publication_reader
    from .dashboard_reads import _adopted_heads_sql, verified_adopted_head_identities
    from .storage import _active_fact_reads

    reads, connection = snap.reads, snap.connection
    if (not reads._snapshot_active or not connection.in_transaction
            or _active_fact_reads.get() is not reads
            or close_reader() is not close_storage or publication_reader() is not publication
            or getattr(snap.store.registry, "content_version", None) == 1):
        return None
    # Corrections can reassign frozen identities or opening bindings. Until
    # their exact bank scope is independently established, keep full selection.
    if connection.execute("SELECT 1 FROM identity_correction LIMIT 1").fetchone():
        return None
    # Genuine monetary openings and their uncertainty retain full selection.
    # A bank start is different: it has no balance effect, but its own exact
    # independently adopted source must still be consumed by the funds page.
    if connection.execute(
        "SELECT 1 FROM subject WHERE kind IN "
        "('opening_package','opening_bank','opening_cash') LIMIT 1"
    ).fetchone():
        return None
    kinds = {"bank_opening", "bank_statement", "bank_reconciliation"}
    # A redirected current pointer can exclude its true terminal from the
    # locator. The existing independent guard checks every open bank terminal,
    # including those whose registered account already has a frozen witness.
    verified_adopted_head_identities(snap, [], kinds=kinds)
    query, parameters = _adopted_heads_sql(snap, kinds)
    # Keep every posting tranche, including damaged frozen/current locators.
    # A latest-subject rank would erase a former account after a correction.
    if connection.execute(
        query + "SELECT 1 FROM eligible WHERE mode='withdrawn' LIMIT 1", parameters
    ).fetchone():
        return None
    heads = {}
    for row in connection.execute(
        query + "SELECT subject_id,calculation_id id,posting_period,fact_id,kind,period,"
        "current_missing FROM heads", parameters,
    ):
        head = dict(row)
        if head["current_missing"]:
            raise KernelError("content_integrity_failed", "银行采用来源缺少正式发布")
        previous = heads.get(head["id"])
        if previous is not None and previous != head:
            return None
        heads[head["id"]] = head
    if not heads:
        return None
    posting_by_subject = {}
    for head in heads.values():
        posting_by_subject.setdefault(head["subject_id"], set()).add(head["posting_period"])
    if any(len(periods) > 1 for periods in posting_by_subject.values()):
        # Continuous closed corrections need all precise adopted tranches.
        return None
    headers = reads.calculation_identity_headers(heads)
    reads.verify_publication_records(headers.values())
    for ident, head in heads.items():
        header = headers[ident]
        if (not header["cs"] or not header["fs"] or (
                header["source_subject"], header["source_fact_id"],
                header["source_kind"], header["source_period"], header["posting_period"]
            ) != (head["subject_id"], head["fact_id"], head["kind"], head["period"],
                  head["posting_period"])):
            raise KernelError("content_integrity_failed", "银行采用来源的精确身份不一致")
    registered = {row[0] for row in connection.execute(
        "SELECT id FROM entity WHERE kind='fund_account' AND account_type='bank'"
    )}
    identities = {}
    for row in connection.execute(
        _BANK_IDENTITY_SCALARS_SQL,
        (canonical(sorted(heads)),),
    ):
        if (row["id"] in identities or row["path"] != "bank_account_id"
                or row["bank_id"] not in registered or row["entity_id"] != row["bank_id"]
                or row["scalar_period"] != row["period"]
                or row["role_period"] != row["period"]
                or row["role_digest"] != row["fact_digest"]
                or row["kind"] != heads[row["id"]]["kind"]):
            return None
        identities[row["id"]] = row["bank_id"]
    if identities.keys() != heads.keys() or set(identities.values()) != registered:
        # Registered drafts are not formal accounts. Requiring positive cover
        # makes them a full-scope fallback, never a fabricated page account.
        return None
    closed = {row[0] for row in connection.execute(
        "SELECT period FROM period_close WHERE period<=?", (snap.month,)
    )}
    selected = {
        head["id"] for head in heads.values()
        if head["posting_period"] not in closed or head["posting_period"] == snap.month
        or head["period"] == snap.month or head["kind"] == "bank_opening"
    }
    witnessed = set()
    for head in sorted(heads.values(), key=lambda item: (
        item["posting_period"], item["kind"] != "bank_statement", item["id"]
    )):
        bank = identities[head["id"]]
        if bank not in witnessed:
            selected.add(head["id"])
            witnessed.add(bank)
    return [head for ident, head in heads.items() if ident in selected]


def _bank_identity_states(snap, *, outcomes):
    """Prove positive witnesses at their exact adopted posting nodes.

    These precise consumers prove presence, not absence from every later close.
    The ordinary selector checks full accounting nodes and result identities;
    bank starts/open/current-month consumers retain their complete source path.
    """
    heads = _bank_identity_witness_heads(snap)
    if heads is None:
        return None
    by_period = {}
    for head in heads:
        by_period.setdefault(head["posting_period"], []).append(head)
    states = []
    for period, group in sorted(by_period.items()):
        selected = snap.queries._selected_accounting(
            snap.connection, {head["subject_id"] for head in group}, snap.period,
            kinds={head["kind"] for head in group}, include_vouchers=False,
            posting_period=str(YearMonth.from_ordinal(period)),
            **({"_owner_outcomes": outcomes} if outcomes is not None else {}),
        )["through_period"]
        if selected["unestablished_state_selections"]:
            return None
        actual = {state["calculation_id"]: state for state in selected["state_results"]}
        if len(actual) != len(selected["state_results"]) or actual.keys() != {
            head["id"] for head in group
        } or any(
            (actual[head["id"]]["fact_id"], actual[head["id"]]["kind"],
             actual[head["id"]]["calculation_period"], actual[head["id"]]["posting_period"])
            != (head["fact_id"], head["kind"], str(YearMonth.from_ordinal(head["period"])),
                str(YearMonth.from_ordinal(head["posting_period"])))
            for head in group
        ):
            raise KernelError("content_integrity_failed", "银行账户见证缺少精确独立采用")
        states.extend(actual.values())
    states.sort(key=lambda state: (state["posting_period"], state["calculation_id"]))
    return {"status": "established" if states else "not_established",
            "voucher_events": [], "state_results": states, "unestablished_state_selections": []}


class FundsRead:
    def __init__(self, snap, *, amounts_only=False):
        self.snap, self.connection = snap, snap.connection
        from . import close_storage
        from .content_history_context import close_reader
        from .storage import _active_fact_reads

        # The brief consumes money, not a complete zero-account directory.
        # Bank starts/statements/reconciliations have no accounting effect;
        # their state identities are still needed by the full funds page.
        # Corrections retain that full route until their exact identity scope
        # can be separated from monetary opening uncertainty independently.
        self.amounts_only = (
            amounts_only and snap.reads._snapshot_active
            and self.connection.in_transaction
            and _active_fact_reads.get() is snap.reads
            and close_reader() is close_storage
            and getattr(snap.store.registry, "content_version", None) != 1
            and self.connection.execute("SELECT 1 FROM identity_correction LIMIT 1").fetchone()
            is None
        )

        # Carry exact selected state bodies locally; scope candidates never
        # become content proofs. Incomplete guards retain the full selector.
        state_outcomes = (
            {} if snap.reads._snapshot_active and self.connection.in_transaction
            and close_reader() is close_storage
            and getattr(snap.store.registry, "content_version", None) != 1 else None
        )
        self.selected = (
            None if self.amounts_only else _bank_identity_states(snap, outcomes=state_outcomes)
        )
        if self.selected is None:
            self.selected = snap.queries._selected_accounting(
                self.connection, None, snap.period, include_vouchers=False,
                kinds={"opening_package", "opening_bank", "opening_cash"} | (
                    set() if self.amounts_only
                    else {"bank_opening", "bank_statement", "bank_reconciliation"}
                ),
                **({"_owner_outcomes": state_outcomes} if state_outcomes is not None else {}),
            )["through_period"]
        self._state_outcomes = state_outcomes
        self.states = snap.reads.metadata(
            item["calculation_id"] for item in self.selected["state_results"]
        )
        # Internal adoption metadata still proves frozen statement sources;
        # removing owner-facing proof payload must not remove this check.
        self.state_selections = {
            item["calculation_id"]: item for item in self.selected["state_results"]
        }
        self.opening_ids = [ident for ident, item in self.states.items() if item["opening"]]
        self.issues = list(self.selected["unestablished_state_selections"])
        self.account_rows = {}
        self.omitted_account_rows = {}
        self.product_rows = {}
        self.profiles = {}
        self.event_queries = {}
        self._money_event_rows = None
        self.bank_match_calculations = {}
        self.investment_registered = None
        self.shared_pages = {}

    def _verify_event_sources(self, identifiers, frozen_periods):
        """Prove consumed bodies and bind frozen bases to their independent adoption."""
        identifiers = set(identifiers)
        if not identifiers:
            return
        reads = self.snap.reads
        metadata = reads.metadata(identifiers, state=False)
        if frozen_periods:
            closes = reads.authoritative_close_rows(
                periods=set(frozen_periods.values()), through_period=self.snap.month
            )
            selections = reads.close_accounting_many(
                closes, subjects={metadata[ident]["subject_id"] for ident in frozen_periods}
            )
            adopted = {
                (close["period"], item["calculation_id"]): item
                for close, selection in zip(closes, selections, strict=True)
                for item in selection.adopted_results
            }
            owners = {
                (close["period"], item["calculation_id"])
                for close, selection in zip(closes, selections, strict=True)
                for item in selection.vouchers
            }
            saved_owners = set()
            for ident, period in frozen_periods.items():
                item, calc = adopted.get((period, ident)), metadata[ident]
                if item is None:
                    # A pre-close no-impact review adopts a newer result while
                    # retaining the voucher's original owner. Its exact owner
                    # ID is anchored by the frozen voucher, and its own saved
                    # input digest proves the consumed old body independently.
                    if (period, ident) not in owners:
                        raise KernelError("content_integrity_failed", "冻结资金来源缺少独立采用")
                    saved_owners.add(ident)
                    continue
                if any(
                    item[adopted_field] != calc[field]
                    for adopted_field, field in (
                        ("publication_id", "publication_id"),
                        ("subject_id", "subject_id"),
                        ("fact_id", "fact_id"),
                        ("source_period", "period"),
                        ("posting_period", "posting_period"),
                        ("result_digest", "result_digest"),
                    )
                ):
                    raise KernelError("content_integrity_failed", "冻结资金来源与独立采用不一致")
            reads.verify_saved_input_identity(saved_owners)
        reads.verify_selected_content(identifiers)

    def _frozen_state_periods(self, identifiers):
        return {
            ident: YearMonth(
                self.state_selections[ident]["selection_proof"]["close_period"]
            ).ordinal
            for ident in identifiers
            if self.state_selections[ident]["selection_source"] == "close_manifest"
        }

    def _verified_current_event_rows(self, *, current, accounts, subjects):
        """Filter already authenticated month headers by their actual posted lines."""
        from . import close_storage, publication
        from .content_history_context import close_reader, publication_reader
        from .storage import _active_fact_reads

        reads = self.snap.reads
        if (
            not current
            or subjects is not None
            or self.snap.close is not None
            or not reads._snapshot_active
            or not self.connection.in_transaction
            or reads.connection is not self.connection
            or self.snap.queries.reads is not reads
            or _active_fact_reads.get() is not reads
            or close_reader() is not close_storage
            or publication_reader() is not publication
            or getattr(self.snap.store.registry, "content_version", None) == 1
        ):
            return None
        rows = self.snap.month_journal.verified_rows()
        if rows is None:
            return None
        lines = reads.voucher_lines({row["id"] for row in rows})
        return [
            row for row in rows
            if any(line["account"] in accounts for line in lines[row["id"]])
        ]

    def events(self, *, current=False, accounts=None, subjects=None):
        accounts = {"1001", "1002", "1012", "1101"} if accounts is None else accounts
        cache_key = (
            current,
            tuple(sorted(accounts)),
            None if subjects is None else tuple(sorted(subjects)),
        )
        if cache_key in self.event_queries:
            query, parameters = self.event_queries[cache_key]
            return query, list(parameters)
        journal = self.snap.month_journal if current else self.snap.journal
        verified = self._verified_current_event_rows(
            current=current, accounts=accounts, subjects=subjects,
        )
        if verified is not None:
            # The full month proof already selected these exact headers and
            # authenticated their actual posted lines. Keep a SQL consumer's
            # scope identical, including an empty account selection, without
            # repeating the account-to-voucher locator.
            query, parameters = journal.sql()
            query += " AND j.id IN (SELECT value FROM json_each(?))"
            parameters.append(canonical(sorted(row["id"] for row in verified)))
        else:
            query, parameters = journal.select(accounts=accounts, subjects=subjects).sql()
        sources = (
            verified if verified is not None else self.connection.execute(
                "SELECT DISTINCT id,basis_calculation_id,close_period,reverses_id FROM ("
                + query + ")", parameters,
            ).fetchall()
        )
        self.snap.reads.verify_selected_voucher_adoptions(sources, through_period=self.snap.month)
        identifiers = {row["basis_calculation_id"] for row in sources}
        frozen = {
            row["basis_calculation_id"]: row["close_period"]
            for row in sources
            if row["close_period"] is not None and row["reverses_id"] is None
        }
        originals = self.snap.reads.vouchers(
            row["reverses_id"] for row in sources if row["reverses_id"] is not None
        )
        frozen.update(
            (row["basis_calculation_id"], originals[row["reverses_id"]]["period"])
            for row in sources
            if row["reverses_id"] is not None
        )
        if not current:
            identifiers.update(self.opening_ids)
            frozen.update(self._frozen_state_periods(self.opening_ids))
        self._verify_event_sources(identifiers, frozen)
        self.snap.reads.verify_sql_outcomes(identifiers)
        # Share this physical selection only after all event proofs succeed.
        # It is neither a caller-supplied witness nor another body cache.
        if (current and subjects is None
                and accounts == {"1001", "1002", "1012", "1101"}):
            self._money_event_rows = verified
        source = (
            "WITH journal AS MATERIALIZED (" + query + "), events AS MATERIALIZED ("
            "SELECT j.period,j.id event_id,j.number,j.basis_calculation_id calculation_id,"
            "j.basis_kind kind,CASE WHEN j.reverses_id IS NULL THEN 1 ELSE -1 END sign,"
            "0 opening,c.outcome FROM journal j JOIN calculation c ON c.id=j.basis_calculation_id"
        )
        if not current:
            source += (
                " UNION ALL SELECT p.posting_period,c.id,0,c.id,c.kind,1,1,c.outcome "
                "FROM json_each(?) ids JOIN calculation c ON c.id=ids.value "
                "JOIN calculation_publication p ON p.calculation_id=c.id"
            )
            parameters.append(canonical(self.opening_ids))
        source += (
            "), effects AS (SELECT e.*,CAST(b.key AS INTEGER) effect_index,"
            "json_extract(b.value,'$.category') category,json_extract(b.value,'$.key') balance_key,"
            "json_extract(b.value,'$.amount') amount FROM events e,"
            "json_each(e.outcome,'$.balances') b WHERE json_extract(b.value,'$.category') "
            "IN ('bank','cash','platform','short_term_investment')) "
        )
        self.event_queries[cache_key] = source, tuple(parameters)
        return source, parameters

    def _verified_money_effects(self):
        """Carry only current money fields from exact, already proved bodies.

        Headers establish the posted scope; they cannot stand in for a decoded
        body. Do not load bodies to make this path eligible. Frozen months and
        unusual saved shapes retain their existing SQL consumption/diagnostics.
        """
        from . import close_storage, publication
        from .content_history_context import close_reader, publication_reader
        from .storage import _active_fact_reads

        if (self.snap.close is not None or _active_fact_reads.get() is not self.snap.reads
                or close_reader() is not close_storage or publication_reader() is not publication
                or getattr(self.snap.store.registry, "content_version", None) == 1):
            return None
        headers = self._money_event_rows
        if headers is None:
            return None
        contents = self.snap.reads._verified_source_contents
        effects = []
        for header in headers:
            ident = header["basis_calculation_id"]
            if ident not in contents:
                return None
            outcome = contents[ident]
            balances, values = outcome.get("balances"), outcome.get("values")
            if type(balances) is not list or type(values) is not dict:
                return None
            actual_date = values.get("actual_date")
            if actual_date is not None and type(actual_date) is not str:
                return None
            for index, balance in enumerate(balances):
                if (type(balance) is not dict
                        or type(balance.get("category")) is not str
                        or type(balance.get("key")) is not str
                        or type(balance.get("amount")) is not int
                        or not -(1 << 63) <= balance["amount"] < (1 << 63)):
                    return None
                if balance["category"] in {"bank", "cash", "platform"} and balance["amount"]:
                    effects.append([
                        header["period"], header["id"], header["number"], ident,
                        header["basis_kind"], -1 if header["reverses_id"] is not None else 1,
                        index, balance["category"], balance["key"], balance["amount"], actual_date,
                    ])
        return effects

    def movements(self):
        # Keep selection, adoption, saved body and reference checks in events,
        # including the original frozen source of a current-period reversal.
        source, parameters = self.events(current=True)
        effects = self._verified_money_effects()
        actual_date = "json_extract(m.outcome,'$.values.actual_date')"
        if effects is not None:
            columns = (
                "period", "event_id", "number", "calculation_id", "kind", "sign",
                "effect_index", "category", "balance_key", "amount", "actual_date",
            )
            source = "WITH effects AS (SELECT " + ",".join(
                f"json_extract(value,'$[{index}]') {column}"
                for index, column in enumerate(columns)
            ) + " FROM json_each(?)) "
            parameters = [canonical(effects)]
            actual_date = "m.actual_date"
        source = source.rstrip() + (
            ", money AS (SELECT *,row_number() OVER(PARTITION BY event_id ORDER BY effect_index)-1 "
            "local_index FROM effects WHERE category IN ('bank','cash','platform') AND amount!=0),"
            "transfers AS (SELECT event_id,count(DISTINCT category||char(0)||balance_key)>1 "
            "AND sum(amount)=0 internal FROM money GROUP BY event_id) "
            "SELECT m.event_id,m.number,m.calculation_id,m.kind,m.sign,m.category,m.balance_key,"
            "m.sign*m.amount signed_amount," + actual_date + " "
            "actual_date,"
            "printf('%012d:%s:%06d',m.number,m.calculation_id,m.local_index) page_key,"
            "(m.kind IN ('funds_transfer','cash_bank_transfer','bank_platform_transfer') "
            "AND t.internal) internal_transfer FROM money m JOIN transfers t USING(event_id)"
        )
        return source, parameters

    def movement_order(self, rows):
        from .dashboard_sort import business_sort_metadata, date_object_key

        rows = list(rows)
        metadata = business_sort_metadata(
            self.snap, {row["calculation_id"] for row in rows}, funds=True,
        )
        return [row["page_key"] for row in sorted(rows, key=lambda row: date_object_key(
            metadata[row["calculation_id"]], None, row["page_key"],
            month_confirmation=False, internal=bool(row["internal_transfer"]),
            missing_party="未提供往来对象",
        ))]

    def _verified_money_summary(self):
        """Consume proved effects without building the unused movement page.

        Only ordinary, bounded integer sums use this consumer. Possible SQLite
        intermediate overflow retains the original SQL and its error semantics.
        This is neither a source proof nor another result cache.
        """
        effects = self._verified_money_effects()
        if effects is None:
            return None
        magnitude = 0
        events = {}
        for row in effects:
            magnitude += abs(row[9])
            if magnitude >= 1 << 63:
                return None
            try:
                for value in (row[7], row[8], row[10]):
                    if value is not None:
                        value.encode("utf-8")
            except UnicodeEncodeError:
                return None
            # Only transfer kinds can be internal. Ordinary receipts/payments
            # do not need an unused per-event account set for the summary.
            if row[4] in {"funds_transfer", "cash_bank_transfer", "bank_platform_transfer"}:
                event = events.get(row[1])
                if event is None:
                    event = events[row[1]] = {"accounts": set(), "amount": 0}
                event["accounts"].add(row[7] + "\0" + row[8])
                event["amount"] += row[9]
        summaries = {}
        for row in effects:
            _, event_id, _, _, kind, sign, _, category, key, amount, actual_date = row
            entry = summaries.get((category, key))
            if entry is None:
                entry = summaries[category, key] = {
                    "category": category,
                    "balance_key": key,
                    "inflow_fen": 0,
                    "outflow_fen": 0,
                    "external_inflow_fen": 0,
                    "external_outflow_fen": 0,
                    "internal_outflow_fen": 0,
                }
                if not self.amounts_only:
                    entry.update(movement_count=0, last_activity_date=None)
            inflow, outflow = sign * max(amount, 0), sign * max(-amount, 0)
            event = events.get(event_id)
            internal = (kind in {"funds_transfer", "cash_bank_transfer", "bank_platform_transfer"}
                        and len(event["accounts"]) > 1 and event["amount"] == 0)
            entry["inflow_fen"] += inflow
            entry["outflow_fen"] += outflow
            if not self.amounts_only:
                entry["movement_count"] += 1
                if actual_date is not None:
                    previous = entry["last_activity_date"]
                    if previous is None or actual_date > previous:
                        entry["last_activity_date"] = actual_date
            if internal:
                entry["internal_outflow_fen"] += outflow
            else:
                entry["external_inflow_fen"] += inflow
                entry["external_outflow_fen"] += outflow
        return [summaries[key] for key in sorted(summaries)]

    def base_account(self, category, ident):
        return self.account_rows.setdefault(
            (category, ident),
            {
                "account_id": ident,
                "type": FUND_TYPES[category],
                "opening_fen": 0,
                "inflow_fen": 0,
                "outflow_fen": 0,
                "net_change_fen": 0,
                "closing_fen": 0,
                **({"attribution_adjustment_fen": 0, "movement_count": 0,
                    "last_activity_date": None, "negative_balance": False}
                   if not self.amounts_only else {}),
            },
        )

    def base_product(self, ident):
        return self.product_rows.setdefault(
            ident,
            {
                "fund_id": ident,
                "opening_cost_fen": 0,
                "subscription_cost_fen": 0,
                "redemption_cost_fen": 0,
                "closing_cost_fen": 0,
                "investment_income_fen": 0,
            },
        )

    def _can_share_first_page(self):
        """Only current owned snapshots can select first before the summary is consumed."""
        from . import close_storage, publication
        from .content_history_context import close_reader, publication_reader
        from .storage import _active_fact_reads

        return (
            not self.amounts_only and self.snap.reads._snapshot_active
            and self.connection.in_transaction and _active_fact_reads.get() is self.snap.reads
            and close_reader() is close_storage and publication_reader() is publication
            and getattr(self.snap.store.registry, "content_version", None) != 1
            # Retired opening identities depend on final movement amounts/counts.
            # Keep their established summary-then-selection order in its entirety.
            and self.connection.execute("SELECT 1 FROM identity_correction LIMIT 1").fetchone()
            is None
        )

    def _prepare_account_identities(self):
        """Prove every state and unknown opening identity, including zero-activity accounts."""
        # Scalar established starts and statements keep explicitly opened zero accounts.
        # These no-voucher identities are consumed by the whole account list,
        # even when their result has no opening accounting effect.
        frozen_states = self._frozen_state_periods(self.states)
        from . import close_storage
        from .content_history_context import close_reader

        historical_bank = (
            {
                ident: period
                for ident, period in frozen_states.items()
                if self.states[ident]["kind"] in {"bank_statement", "bank_reconciliation"}
                and YearMonth(self.states[ident]["period"]).ordinal < self.snap.month
            }
            if close_reader() is close_storage
            else {}
        )
        historical_accounts = (
            self.snap.reads.frozen_bank_account_identities(
                historical_bank, through_period=self.snap.month,
                _decoded_outcomes=self._state_outcomes,
            )
            if historical_bank
            else {}
        )
        for account in historical_accounts.values():
            self.base_account("bank", account)
        complete_states = self.states.keys() - historical_bank.keys()
        self._verify_event_sources(
            complete_states,
            {ident: period for ident, period in frozen_states.items() if ident in complete_states},
        )
        # Zero-amount state values have no period-balance digest anchor. Prove
        # their precise saved input identity before consuming account/match data.
        self.snap.reads.verify_saved_input_identity(complete_states - frozen_states.keys())
        for row in self.connection.execute(
            "SELECT c.kind,json_extract(c.outcome,'$.values.bank_account_id') bank_id,"
            "json_extract(c.outcome,'$.values.cash_account_id') cash_id FROM json_each(?) ids "
            "JOIN calculation c ON c.id=ids.value",
            (canonical(sorted(complete_states)),),
        ):
            if row["bank_id"] is not None:
                self.base_account("bank", row["bank_id"])
            if row["cash_id"] is not None:
                self.base_account("cash", row["cash_id"])
        for row in self.connection.execute(
            "SELECT json_extract(m.value,'$.kind') "
            "kind,json_extract(m.value,'$.values.bank_account_id') "
            "bank_id,json_extract(m.value,'$.values.cash_account_id') cash_id FROM json_each(?) "
            "ids "
            "JOIN calculation c ON c.id=ids.value,json_each(c.outcome,'$.values.members') m",
            (canonical(self.opening_ids),),
        ):
            if row["kind"] == "opening_bank":
                self.base_account("bank", row["bank_id"])
            elif row["kind"] == "opening_cash":
                self.base_account("cash", row["cash_id"])
        # An uncertain opening contributes identities, never invented zero amounts.
        unknown = [
            candidate["calculation_id"]
            for issue in self.issues
            for candidate in issue["candidates"]
            if candidate["kind"] == "opening_package"
        ]
        self.snap.reads.verify_saved_input_identity(unknown)
        self.snap.reads.verify_sql_outcomes(unknown)
        for row in self.connection.execute(
            "SELECT DISTINCT json_extract(b.value,'$.category') category,"
            "json_extract(b.value,'$.key') balance_key FROM json_each(?) ids JOIN calculation c "
            "ON c.id=ids.value,json_each(c.outcome,'$.balances') b WHERE "
            "json_extract(b.value,'$.category') IN ('bank','cash','platform')",
            (canonical(unknown),),
        ):
            self.base_account(row["category"], row["balance_key"])["opening_fen"] = None

    def account_summary(self, *, page_request=None, first_page_request=None):
        from .period_balances import balance_movements, balance_totals

        categories = ("bank", "cash", "platform")
        projected_closing = {}
        for row in balance_totals(
            self.connection, self.snap.month, categories, reads=self.snap.reads
        ):
            if row["category"] in categories:
                self.base_account(row["category"], row["key"])
                projected_closing[row["category"], row["key"]] = row["amount"]
        current_activity = {}
        for row in balance_movements(
            self.connection, self.snap.month, categories, reads=self.snap.reads
        ):
            if row["category"] in categories:
                current_activity[row["category"], row["key"]] = row["amount"]
        for key, amount in projected_closing.items():
            self.base_account(*key)["opening_fen"] = checked(amount - current_activity.get(key, 0))
        summary_columns = (
            "category",
            "balance_key",
            "inflow_fen",
            "outflow_fen",
            *(() if self.amounts_only else ("movement_count", "last_activity_date")),
            "external_inflow_fen",
            "external_outflow_fen",
            "internal_outflow_fen",
        )
        summary_select = (
            "SELECT category,balance_key,sum(sign*max(sign*signed_amount,0)) inflow_fen,"
            "sum(sign*max(-sign*signed_amount,0)) outflow_fen,"
            + ("" if self.amounts_only else
               "count(*) movement_count,max(actual_date) last_activity_date,")
            + "sum(CASE WHEN NOT internal_transfer THEN sign*max(sign*signed_amount,0) "
            "ELSE 0 END) external_inflow_fen,"
            "sum(CASE WHEN NOT internal_transfer THEN sign*max(-sign*signed_amount,0) "
            "ELSE 0 END) external_outflow_fen,"
            "sum(CASE WHEN internal_transfer THEN sign*max(-sign*signed_amount,0) "
            "ELSE 0 END) internal_outflow_fen"
        )
        grouping = " GROUP BY category,balance_key"
        identities_prepared = False
        if first_page_request is not None and self._can_share_first_page():
            self._prepare_account_identities()
            identities_prepared = True
            keys = list(self.account_rows)
            supported = all(type(category) is str and type(key) is str for category, key in keys)
            if supported:
                try:
                    for category, key in keys:
                        category.encode("utf-8")
                        key.encode("utf-8")
                except UnicodeEncodeError:
                    supported = False
            if supported:
                # summary_rows covers every actual movement account; prepared
                # identities contribute accounts with no movements. Filtering
                # happens after whole-event transfers and the complete summary.
                page_request = dict(first_page_request) | {
                    "where": "(category,balance_key)=(SELECT category,balance_key FROM ("
                    "SELECT category,balance_key FROM summary_rows UNION "
                    "SELECT json_extract(value,'$[0]') category,"
                    "json_extract(value,'$[1]') balance_key FROM json_each(?)) "
                    "ORDER BY category,balance_key LIMIT 1)",
                    "filters": (canonical(sorted(keys)),),
                }
        if page_request is None:
            self.events(current=True)
            summaries = self._verified_money_summary()
            if summaries is None:
                source, parameters = self.movements()
                summaries = self.connection.execute(
                    summary_select + f" FROM ({source})" + grouping, parameters
                )
        else:
            source, parameters = self.movements()
            summaries, rows, page = _sql_summary_page(
                self.connection,
                source,
                parameters,
                summary_select + " FROM source_rows" + grouping,
                summary_columns,
                **page_request,
                order_rows=self.movement_order,
            )
            self.shared_pages["movements"] = rows, page
        movement_totals = {
            "inflow_fen": 0,
            "outflow_fen": 0,
            "internal_transfer_fen": 0,
            **({} if self.amounts_only else {"movement_count": 0}),
        }
        for row in summaries:
            self.base_account(row["category"], row["balance_key"]).update(
                {
                    key: row[key]
                    for key in (
                        ("inflow_fen", "outflow_fen") if self.amounts_only else
                        ("inflow_fen", "outflow_fen", "movement_count", "last_activity_date")
                    )
                }
            )
            movement_totals["inflow_fen"] += row["external_inflow_fen"]
            movement_totals["outflow_fen"] += row["external_outflow_fen"]
            movement_totals["internal_transfer_fen"] += row["internal_outflow_fen"]
            if not self.amounts_only:
                movement_totals["movement_count"] += row["movement_count"]
        if not identities_prepared:
            self._prepare_account_identities()
        for index, (key, item) in enumerate(sorted(self.account_rows.items()), 1):
            item["net_change_fen"] = item["inflow_fen"] - item["outflow_fen"]
            item["closing_fen"] = (
                projected_closing.get(key, 0) if item["opening_fen"] is not None else None
            )
            if self.amounts_only:
                continue
            item["fallback_code"] = f"账户 {index}"
            item["attribution_adjustment_fen"] = (
                checked(item["closing_fen"] - item["opening_fen"] - item["net_change_fen"])
                if item["closing_fen"] is not None and item["opening_fen"] is not None
                else None
            )
            item["negative_balance"] = item["closing_fen"] is not None and item["closing_fen"] < 0
            item["statement"] = {
                "inflow_fen": None,
                "outflow_fen": None,
                "transaction_count": 0,
                "matched_count": 0,
                "unmatched_count": 0,
                "needs_review_count": 0,
                "coverage_state": "missing" if key[0] == "bank" else "not_applicable",
                "last_activity_date": None,
            }
            item["reconciliation"] = {
                "state": "pending" if key[0] == "bank" else "not_applicable",
                "label": "本月尚未完成银行对账" if key[0] == "bank" else "不适用银行对账",
            }
        if not self.amounts_only:
            self._omit_retired_opening_accounts()
        return movement_totals

    def _omit_retired_opening_accounts(self):
        """Hide an old identity whose only current-period effect is reassignment."""
        if self.snap.close is not None:
            return
        from .identity_corrections import current_opening_bindings

        fields = {
            "opening_bank": ("bank", "bank_account_id"),
            "opening_cash": ("cash", "cash_account_id"),
        }
        bindings = current_opening_bindings(self.connection)
        for binding in bindings.values():
            mapped = fields.get(binding["source_kind"])
            if mapped is None:
                continue
            category, field = mapped
            source = self.snap.store.fact(self.connection, binding["source_fact_id"])
            original_id = getattr(source.fact, field)
            if original_id == binding["basis_data"].get(field):
                continue
            witness = self.connection.execute(
                "SELECT r.fact_id FROM entity_reference_current r JOIN fact_current c "
                "ON c.fact_id=r.fact_id WHERE r.entity_id=? "
                "ORDER BY r.period DESC,r.fact_id DESC LIMIT 1",
                (original_id,),
            ).fetchone()
            if witness:
                from .entity_references import verify_hits

                verify_hits(self.connection, [witness], registry=self.snap.store.registry)
                continue
            item = self.account_rows.get((category, original_id))
            if item is None:
                continue
            if (
                item["opening_fen"] is not None
                and item["closing_fen"] == 0
                and item["inflow_fen"] == 0
                and item["outflow_fen"] == 0
                and item["net_change_fen"] == 0
                and item["movement_count"] == 0
                and item["attribution_adjustment_fen"] == -item["opening_fen"]
            ):
                self.omitted_account_rows[category, original_id] = item
                del self.account_rows[category, original_id]

    def profile(self, kind, ident):
        key = kind, ident
        if key not in self.profiles:
            self.profiles[key] = self.snap.profile(kind, ident)
        return self.profiles[key]

    def account_display(self, category, ident):
        item = self.account_rows.get((category, ident))
        profile = self.profile("fund_account", ident)
        return {
            "code": profile.get("display_number")
            or (item["fallback_code"] if item else "未确认账户"),
            "name": profile.get("display_name") or "未提供账户名称",
            "active": profile.get("active"),
        }

    def account_item(self, key):
        item = dict(self.account_rows[key])
        item.pop("fallback_code")
        display = self.account_display(*key)
        item.update(display)
        item["statement"] = item["statement"] | {
            "account_code": display["code"],
            "account_name": display["name"],
        }
        for field in ("matched_count", "unmatched_count", "needs_review_count"):
            item["statement"].pop(field)
        item["reconciliation"] = {
            key: value
            for key, value in item["reconciliation"].items()
            if key in {"state", "label", "difference_fen"}
        }
        return item

    def money_parties(self, calc, sign, *, internal_transfer=False):
        """Present the exact parties attached to one typed funds calculation."""
        data = calc["fact"]["data"]
        parties = {
            data[field]
            for field in ("counterparty_id", "owner_id", "lender_id", "payer_id", "recipient_id")
            if data.get(field) and data[field] != "payroll-group"
        }
        parties.update(
            item["party_id"] for item in self.snap.voucher_relations(calc, sign) if item["party_id"]
        )
        if data.get("payment_method") == "bank_batch":
            parties.discard(data.get("counterparty_id"))
        parties.update(
            item["recipient_id"] for item in data.get("allocations", ()) if item.get("recipient_id")
        )
        party = (
            "、".join(sorted((self.snap.party(ident) for ident in parties), key=pinyin_key))
            if parties
            else "公司账户内部划转"
            if internal_transfer
            else "未提供往来对象"
        )
        return party

    def movement_item(self, row):
        from .dashboard import _name

        calc = self.snap.calculation(row["calculation_id"])
        data = calc["fact"]["data"]
        account = self.account_display(row["category"], row["balance_key"])
        party = self.money_parties(
            calc, row["sign"], internal_transfer=bool(row["internal_transfer"])
        )
        short, label = self.snap.business_summary(calc, row["sign"])
        amount = row["signed_amount"]
        return {
            "id": row["page_key"],
            "subject_id": calc["subject_id"],
            "date": data.get("actual_date"),
            "account_id": row["balance_key"],
            "account_code": account["code"],
            "account_name": account["name"],
            "account_type": FUND_TYPES[row["category"]],
            "direction": "inflow" if amount * row["sign"] > 0 else "outflow",
            "correction": row["sign"] < 0,
            "amount_fen": abs(amount),
            "signed_amount_fen": amount,
            "type": _name(row["kind"]),
            "display_summary": label,
            "list_summary": short,
            "party": party,
            "internal_transfer": bool(row["internal_transfer"]),
        }

    def prepare_calculation_items(self, identifiers):
        """Load only page calculations, relations and display records in batches."""
        identifiers = set(identifiers)
        if not identifiers:
            return
        self.snap.reads.prime_calculations(identifiers, ancestors=True)
        resolutions = self.snap.reads.relations_many(identifiers)
        presentation_ids = set(identifiers)
        for ident, resolution in resolutions.items():
            balance_keys = {
                item["key"]
                for item in self.snap.reads.calculation(ident)["outcome"].get("balances", ())
            }
            presentation_ids.update(
                item["source_calculation_id"]
                for item in resolution["obligations"]
                if item.get("key") in balance_keys and item.get("source_calculation_id")
            )
        # voucher_relations presents the same source calculations and typed facts
        # for every page row.  Prime those exact records once instead of issuing
        # one metadata and one fact SELECT per visible movement.
        metadata = self.snap.reads.metadata(presentation_ids)
        self.snap.reads.fact_versions({item["fact_id"] for item in metadata.values()})
        subjects = {self.snap.calculation(ident)["subject_id"] for ident in identifiers}
        self.snap.metadata.prime_profiles("business", subjects)
        self.snap.management.prime(subjects)

    def _closed_statement_parent(
        self, statement, reconciliation, result, statement_results, parents
    ):
        """A frozen bank reconciliation can prove its exact statement source only.

        The independent adoption comes from this response's shared selection.
        Persistent metadata/edges alone never establish that adoption.
        """
        selected = self.state_selections[result["id"]]
        if (
            selected["selection_source"] != "close_manifest"
            or selected["posting_period"] != self.snap.period
        ):
            return None, "unestablished", "对账结果在所选关账中的独立采用尚不能证明。"
        sources = [item for item in parents if item["kind"] == "bank_statement"]
        if len(sources) != 1:
            return (
                None,
                "conflict" if sources else "unestablished",
                "对账采用的精确流水计算来源尚不能唯一确认。",
            )
        source = sources[0]
        if (
            source["subject_id"] != statement["subject_id"]
            or source["fact_id"] != statement["revision_id"]
            or source["period"] != self.snap.period
            or statement["fact_period"] != self.snap.month
            or reconciliation["fact_period"] != self.snap.month
            or reconciliation["statement_id"] != source["subject_id"]
            or reconciliation["bank_account_id"] != statement["bank_account_id"]
            or result["period"] != self.snap.period
        ):
            return None, "conflict", "对账采用的流水来源与当前展示的版本、账户或期间不一致。"
        if statement_results and (
            len(statement_results) != 1 or statement_results[0]["id"] != source["id"]
        ):
            return None, "conflict", "独立采用的流水计算与对账采用的流水计算不一致。"
        return source, None, None

    def _closed_bank_issue_sources(self, result, parents):
        """Prove statement/opening display roles from one frozen reconciliation.

        This is page-local source proof.  It deliberately does not add either
        dependency to the shared independently selected state results.
        """
        selected = self.state_selections.get(result["id"])
        if (
            selected is None
            or selected["selection_source"] != "close_manifest"
            or selected["posting_period"] != result["period"]
        ):
            return set()
        month = YearMonth(selected["posting_period"])
        reconciliation = self.snap.reads.connection.execute(
            "SELECT c.fact_id,f.subject_id,f.period fact_period,r.statement_id,"
            "r.bank_account_id,json_extract(c.outcome,'$.values.statement_id') "
            "result_statement_id,json_extract(c.outcome,'$.values.bank_account_id') "
            "result_bank_account_id,json_extract(c.outcome,'$.values.opening_fen') "
            "result_opening_fen,json_extract(c.outcome,'$.values.balanced') balanced "
            "FROM calculation c JOIN fact_revision f ON f.id=c.fact_id "
            "JOIN fact_bank_reconciliation r ON r.revision_id=c.fact_id WHERE c.id=?",
            (result["id"],),
        ).fetchone()
        if (
            reconciliation is None
            or reconciliation["fact_id"] != result["fact_id"]
            or reconciliation["subject_id"] != result["subject_id"]
            or reconciliation["fact_period"] != month.ordinal
            or reconciliation["result_statement_id"] != reconciliation["statement_id"]
            or reconciliation["result_bank_account_id"] != reconciliation["bank_account_id"]
            or reconciliation["balanced"] != 1
        ):
            return set()
        statement_parents = [item for item in parents if item["kind"] == "bank_statement"]
        if len(statement_parents) != 1:
            return set()
        statement = statement_parents[0]
        statement_fact = self.snap.reads.connection.execute(
            "SELECT c.fact_id,f.subject_id,f.period fact_period,s.bank_account_id "
            "FROM calculation c JOIN fact_revision f ON f.id=c.fact_id "
            "JOIN fact_bank_statement s ON s.revision_id=c.fact_id WHERE c.id=?",
            (statement["id"],),
        ).fetchone()
        selected_statements = [
            item
            for item in self.states.values()
            if item["kind"] == "bank_statement" and item["subject_id"] == statement["subject_id"]
        ]
        if (
            statement_fact is None
            or statement["posting_period"] != result["period"]
            or statement["period"] != result["period"]
            or statement_fact["fact_id"] != statement["fact_id"]
            or statement_fact["subject_id"] != statement["subject_id"]
            or statement_fact["fact_period"] != month.ordinal
            or statement_fact["subject_id"] != reconciliation["statement_id"]
            or statement_fact["bank_account_id"] != reconciliation["bank_account_id"]
            or selected_statements
            and (len(selected_statements) != 1 or selected_statements[0]["id"] != statement["id"])
        ):
            return set()
        proven = {statement["id"]}
        opening_parents = [item for item in parents if item["kind"] == "bank_opening"]
        if len(opening_parents) != 1:
            return proven
        opening = opening_parents[0]
        opening_fact = self.snap.reads.connection.execute(
            "SELECT c.fact_id,f.subject_id,f.period fact_period,o.bank_account_id,o.opening_fen,"
            "json_extract(c.outcome,'$.values.opening_fen') result_opening_fen "
            "FROM calculation c JOIN fact_revision f ON f.id=c.fact_id "
            "JOIN fact_bank_opening o ON o.revision_id=c.fact_id WHERE c.id=?",
            (opening["id"],),
        ).fetchone()
        selected_openings = [
            item
            for item in self.states.values()
            if item["kind"] == "bank_opening" and item["subject_id"] == opening["subject_id"]
        ]
        if (
            opening_fact is not None
            and opening["posting_period"] == result["period"]
            and opening["period"] == result["period"]
            and opening_fact["fact_id"] == opening["fact_id"]
            and opening_fact["subject_id"] == opening["subject_id"]
            and opening_fact["fact_period"] == month.ordinal
            and opening_fact["bank_account_id"] == reconciliation["bank_account_id"]
            and opening_fact["opening_fen"] == opening_fact["result_opening_fen"]
            and opening_fact["opening_fen"] == reconciliation["result_opening_fen"]
            and (
                not selected_openings
                or len(selected_openings) == 1
                and selected_openings[0]["id"] == opening["id"]
            )
        ):
            proven.add(opening["id"])
        return proven

    def bank_summary(self, *, page_request=None):
        statement_ids = self.snap.fact_ids_of_kind("bank_statement", period=self.snap.period)
        reconciliation_ids = self.snap.fact_ids_of_kind(
            "bank_reconciliation", period=self.snap.period
        )
        statements = (
            [
                dict(row)
                for row in self.connection.execute(
                    "SELECT f.subject_id,f.revision,f.period fact_period,s.* "
                    "FROM json_each(?) ids JOIN "
                    "fact_bank_statement s "
                    "ON s.revision_id=ids.value JOIN fact_revision f ON f.id=s.revision_id "
                    "ORDER BY f.subject_id",
                    (canonical(statement_ids),),
                )
            ]
            if statement_ids
            else []
        )
        reconciliations = (
            [
                dict(row)
                for row in self.connection.execute(
                    "SELECT f.subject_id,f.revision,f.period fact_period,r.* "
                    "FROM json_each(?) ids JOIN "
                    "fact_bank_reconciliation r "
                    "ON r.revision_id=ids.value JOIN fact_revision f ON f.id=r.revision_id",
                    (canonical(reconciliation_ids),),
                )
            ]
            if reconciliation_ids
            else []
        )
        pending = (
            {
                row[0]
                for row in self.connection.execute(
                    "SELECT DISTINCT subject_id FROM pending WHERE subject_id IN (SELECT "
                    "f.subject_id "
                    "FROM json_each(?) ids JOIN fact_revision f ON f.id=ids.value)",
                    (canonical(statement_ids + reconciliation_ids),),
                )
            }
            if not self.snap.close
            else set()
        )
        states = {}
        for item in self.states.values():
            states.setdefault(item["subject_id"], []).append(item)
        statement_subjects = {item["subject_id"] for item in statements}
        month_results = {
            result["id"]
            for reconciliation in reconciliations
            if reconciliation["statement_id"] in statement_subjects
            for result in states.get(reconciliation["subject_id"], ())
            if result["period"] == self.snap.period
            and result["fact_id"] == reconciliation["revision_id"]
        }
        balanced = {
            row[0]
            for row in self.connection.execute(
                "SELECT c.id FROM json_each(?) ids JOIN calculation c ON c.id=ids.value "
                "WHERE json_extract(c.outcome,'$.values.balanced')=1",
                (canonical(sorted(month_results)),),
            )
        }
        parents_by_result = {}
        if self.snap.close and balanced:
            self.snap.reads.prime_parents(balanced)
            parent_ids = {ident: self.snap.reads.parents(ident) for ident in balanced}
            metadata = self.snap.reads.metadata(
                {parent for identifiers in parent_ids.values() for parent in identifiers}
            )
            parents_by_result = {
                ident: [metadata[parent] for parent in identifiers]
                for ident, identifiers in parent_ids.items()
            }
        provided, confirmed_accounts, headers = set(), set(), []
        for statement in statements:
            ident = statement["bank_account_id"]
            provided.add(ident)
            matches = [
                item for item in reconciliations if item["statement_id"] == statement["subject_id"]
            ]
            reconciliation = matches[0] if len(matches) == 1 else None
            results = states.get(reconciliation["subject_id"], []) if reconciliation else []
            result = results[0] if len(results) == 1 else None
            statement_results = states.get(statement["subject_id"], [])
            statement_result = statement_results[0] if len(statement_results) == 1 else None
            unique_statement = sum(item["bank_account_id"] == ident for item in statements) == 1
            unique_reconciliation = (
                sum(item["bank_account_id"] == ident for item in reconciliations) <= 1
            )
            confirmed = bool(
                statement_result
                and statement_result["fact_id"] == statement["revision_id"]
                and statement["subject_id"] not in pending
                and unique_statement
            )
            adopted_reconciliation = bool(
                result
                and result["fact_id"] == reconciliation["revision_id"]
                and result["period"] == self.snap.period
                and reconciliation["bank_account_id"] == ident
                and unique_reconciliation
                and reconciliation["subject_id"] not in pending
                and result["id"] in balanced
            )
            source = statement_result if confirmed else None
            source_error = source_error_state = None
            if self.snap.close and adopted_reconciliation:
                source, source_error_state, source_error = self._closed_statement_parent(
                    statement,
                    reconciliation,
                    result,
                    statement_results,
                    parents_by_result[result["id"]],
                )
                # A conflicting independently selected source cannot be bypassed
                # by falling back to the reconciliation's dependency.
                if source and unique_statement:
                    confirmed = True
            valid = bool(
                confirmed
                and adopted_reconciliation
                and (not self.snap.close or source is not None and source_error is None)
            )
            if self.snap.close and valid:
                self._closed_bank_issue_sources(result, parents_by_result[result["id"]])
            review = bool(
                not confirmed
                or not unique_reconciliation
                or len(matches) > 1
                or (reconciliation and not valid)
            )
            if confirmed:
                confirmed_accounts.add(ident)
            headers.append(
                {
                    "fact_id": statement["revision_id"],
                    "subject_id": statement["subject_id"],
                    "account_id": ident,
                    "reconciliation_id": reconciliation["revision_id"] if reconciliation else None,
                    "reconciliation_calculation_id": result["id"] if result else None,
                    "confirmed": confirmed,
                    "valid": valid,
                    "review": review,
                }
            )
            item = self.account_rows.get(("bank", ident))
            if item is not None:
                item["statement"]["coverage_state"] = "complete" if confirmed else "partial"
                item["reconciliation"] = {
                    "state": "complete" if valid else "attention",
                    "label": "本月银行流水已核对" if valid else "AI 会计核对中",
                    "difference_fen": statement["closing_fen"] - item["closing_fen"]
                    if item["closing_fen"] is not None
                    else None,
                }
        self.bank_parameters = [
            canonical(headers),
            canonical(
                sorted({item["reconciliation_id"] for item in headers if item["reconciliation_id"]})
            ),
        ]
        bank_columns = (
            "SELECT printf('%s:%012d',json_extract(h.value,'$.subject_id'),e.item_no) page_key,"
            "json_extract(h.value,'$.account_id') account_id,"
            "json_extract(h.value,'$.reconciliation_calculation_id') "
            "reconciliation_calculation_id,"
            "CASE WHEN m.match_count=1 THEN m.source_kind END source_kind,"
            "CASE WHEN m.match_count=1 THEN m.source_id END source_id,"
            "m.matched_sources,"
        )
        bank_detail_columns = (
            "CASE WHEN m.match_count>1 THEN 1 ELSE count(*) OVER (PARTITION BY "
            "json_extract(h.value,'$.reconciliation_calculation_id'),m.source_kind,m.source_id) "
            "END source_row_count,"
            "CASE WHEN m.match_count>1 THEN abs(e.signed_fen) ELSE "
            "sum(abs(e.signed_fen)) OVER (PARTITION BY "
            "json_extract(h.value,'$.reconciliation_calculation_id'),m.source_kind,m.source_id) "
            "END source_rows_total_fen,"
        )
        bank_rows = (
            "e.*,CASE WHEN "
            "json_extract(h.value,'$.valid') "
            "AND m.match_count>0 THEN "
            "'matched' "
            "WHEN json_extract(h.value,'$.review') THEN 'needs_review' ELSE 'unmatched' END state "
            "FROM json_each(?) h JOIN fact_bank_statement_entries e ON "
            "e.revision_id=json_extract(h.value,'$.fact_id') LEFT JOIN "
            "(SELECT r.revision_id,r.reference,count(*) match_count,"
            "min(r.source_kind) source_kind,min(r.source_id) source_id,"
            "json_group_array(json_array(r.source_kind,r.source_id)) matched_sources "
            "FROM json_each(?) ids JOIN fact_bank_reconciliation_matches r "
            "ON r.revision_id=ids.value GROUP BY r.revision_id,r.reference) m "
            "ON m.revision_id="
            "json_extract(h.value,'$.reconciliation_id') AND m.reference=e.reference"
        )
        self.bank_source = bank_columns + bank_detail_columns + bank_rows
        # Only displayed bank rows need source batch counts and totals. The
        # complete account summary keeps the same match/state source, without
        # sorting all entries for two otherwise unused detail windows.
        bank_summary_source = bank_columns + bank_rows
        if not headers:
            self.bank_parameters = []
            self.bank_source = (
                "SELECT NULL page_key,NULL account_id,NULL actual_date,NULL signed_fen,"
                "NULL description,NULL reconciliation_calculation_id,NULL source_kind,"
                "NULL source_id,NULL matched_sources,NULL source_row_count,"
                "NULL source_rows_total_fen,"
                "NULL reference,NULL state WHERE 0"
            )
            bank_summary_source = self.bank_source
        sums = (
            "count(*) transaction_count,coalesce(sum(max(signed_fen,0)),0) inflow_fen,"
            "coalesce(sum(max(-signed_fen,0)),0) outflow_fen,coalesce(sum(state='matched'),0) "
            "matched_count,"
            "coalesce(sum(state='unmatched'),0) "
            "unmatched_count,coalesce(sum(state='needs_review'),0) needs_review_count"
        )
        totals = {
            key: 0
            for key in (
                "transaction_count",
                "inflow_fen",
                "outflow_fen",
                "matched_count",
                "unmatched_count",
                "needs_review_count",
            )
        }
        unmatched = {key: 0 for key in ("count", "inflow_fen", "outflow_fen")}
        summary_columns = (
            "account_id",
            "transaction_count",
            "inflow_fen",
            "outflow_fen",
            "matched_count",
            "unmatched_count",
            "needs_review_count",
            "last_activity_date",
            "unmatched_total_count",
            "unmatched_inflow_fen",
            "unmatched_outflow_fen",
        )
        summary_select = (
            f"SELECT account_id,{sums},max(actual_date) last_activity_date,"
            "coalesce(sum(state!='matched'),0) unmatched_total_count,"
            "coalesce(sum(CASE WHEN state!='matched' THEN max(signed_fen,0) "
            "ELSE 0 END),0) unmatched_inflow_fen,"
            "coalesce(sum(CASE WHEN state!='matched' THEN max(-signed_fen,0) "
            "ELSE 0 END),0) unmatched_outflow_fen"
        )
        if page_request is None:
            summaries = self.connection.execute(
                summary_select + f" FROM ({bank_summary_source}) GROUP BY account_id",
                self.bank_parameters,
            )
        else:
            summaries, rows, page = _sql_summary_page(
                self.connection,
                self.bank_source,
                self.bank_parameters,
                summary_select + " FROM source_rows GROUP BY account_id",
                summary_columns,
                **page_request,
            )
            self.shared_pages["statements"] = rows, page
        for row in summaries:
            for key in totals:
                totals[key] += row[key]
            unmatched["count"] += row["unmatched_total_count"]
            unmatched["inflow_fen"] += row["unmatched_inflow_fen"]
            unmatched["outflow_fen"] += row["unmatched_outflow_fen"]
            item = self.account_rows.get(("bank", row["account_id"]))
            if item is not None:
                item["statement"].update({key: row[key] for key in (*totals, "last_activity_date")})
                item["reconciliation"].update(
                    {key: row[key] for key in ("unmatched_count", "needs_review_count")}
                )
        expected = {ident for category, ident in self.account_rows if category == "bank"} | provided
        coverage = (
            "not_applicable"
            if not expected
            else "missing"
            if not provided
            else "complete"
            if confirmed_accounts == expected
            else "partial"
        )
        if coverage in {"missing", "not_applicable"}:
            totals["inflow_fen"] = totals["outflow_fen"] = None
        return totals | {
            "unmatched_totals": unmatched,
            "coverage_state": coverage,
            "statement_count": len(statements),
            "expected_account_count": len(expected),
            "provided_account_count": len(provided),
            "missing_account_count": len(expected - provided),
        }

    def prepare_bank_items(self, rows):
        """Bind page rows to the exact funds calculations adopted by reconciliation."""
        expected = {}
        requests = []
        for row in rows:
            if row["state"] != "matched" or not row["reconciliation_calculation_id"]:
                continue
            sources = sorted(
                {tuple(source) for source in json.loads(row["matched_sources"] or "[]")}
            )
            if not sources:
                continue
            expected[row["page_key"]] = sources
            requests.extend(
                {
                    "page_key": row["page_key"],
                    "root": row["reconciliation_calculation_id"],
                    "source_kind": kind,
                    "source_id": subject,
                }
                for kind, subject in sources
            )
        if not requests:
            return
        matches = self.connection.execute(
            "SELECT json_extract(r.value,'$.page_key') page_key,"
            "json_extract(r.value,'$.source_kind') source_kind,"
            "json_extract(r.value,'$.source_id') source_id,min(c.id) calculation_id,"
            "count(*) candidate_count FROM json_each(?) r "
            "JOIN calculation c INDEXED BY calculation_subject ON "
            "c.subject_id=json_extract(r.value,'$.source_id') AND "
            "c.kind=json_extract(r.value,'$.source_kind') "
            "JOIN fact_revision f ON f.id=c.fact_id AND f.subject_id=c.subject_id "
            "JOIN dependency_calculation d INDEXED BY dependency_upstream ON "
            "d.upstream_id=c.id AND d.calculation_id=json_extract(r.value,'$.root') "
            "GROUP BY page_key,source_kind,source_id",
            (canonical(requests),),
        ).fetchall()
        candidates = {
            (row["page_key"], row["source_kind"], row["source_id"]): row["calculation_id"]
            for row in matches
            if row["candidate_count"] == 1
        }
        selected = {
            page_key: tuple(candidates[(page_key, *source)] for source in sources)
            for page_key, sources in expected.items()
            if all((page_key, *source) in candidates for source in sources)
        }
        self.prepare_calculation_items(
            calculation_id for identifiers in selected.values() for calculation_id in identifiers
        )
        self.bank_match_calculations.update(
            {
                page_key: tuple(
                    self.snap.calculation(calculation_id) for calculation_id in identifiers
                )
                for page_key, identifiers in selected.items()
            }
        )

    @staticmethod
    def calculation_is_internal_transfer(calc):
        return calc["kind"] in {"funds_transfer", "cash_bank_transfer", "bank_platform_transfer"}

    def bank_batch_presentation(self, calc, row):
        """Describe a batch without assigning whole-batch recipients to one bank row."""
        data = calc["fact"]["data"]
        allocations = data.get("allocations", ())
        if data.get("payment_method") != "bank_batch" or not allocations:
            return None, None

        items, seen_parties = [], set()
        for allocation in allocations:
            recipient_id = allocation.get("recipient_id")
            details = self.snap.party_details(recipient_id)
            name = details["name"] if details.get("source") else "收款人名称未提供"
            items.append({"party": name, "amount_fen": allocation["amount_fen"]})
            if recipient_id:
                seen_parties.add(recipient_id)

        reserve_expense_fen = data.get("reserve_expense_fen")
        if reserve_expense_fen:
            items.append({"party": "备用金支出", "amount_fen": reserve_expense_fen})

        short, _ = self.snap.business_summary(calc, 1)
        title = {
            "支付工资奖金": "工资批量代发",
            "支付社保": "社保批量支付",
            "支付公积金": "公积金批量支付",
        }.get(short, "批量付款")
        recipient_count = len(seen_parties)
        count_label = (
            f"{recipient_count} 人"
            if title == "工资批量代发" and recipient_count == len(allocations)
            else f"{len(allocations)} 项"
        )
        bank_row_count = row["source_row_count"] or 1
        return (
            f"{title} · {count_label}",
            {
                "bank_row_count": bank_row_count,
                "total_fen": row["source_rows_total_fen"] or data["amount_fen"],
                "items": items,
            },
        )

    def bank_item(self, row):
        account = self.account_display("bank", row["account_id"])
        amount = row["signed_fen"]
        calculations = self.bank_match_calculations.get(row["page_key"], ())
        calc = calculations[0] if len(calculations) == 1 else None
        batch_party, batch = self.bank_batch_presentation(calc, row) if calc else (None, None)
        if len(calculations) > 1:
            parts = []
            for source in calculations:
                data = source["fact"]["data"]
                source_amount = data[
                    "principal_fen" if source["kind"] == "loan_drawdown" else "amount_fen"
                ]
                short, _ = self.snap.business_summary(source, 1)
                source_party = self.money_parties(
                    source, 1, internal_transfer=self.calculation_is_internal_transfer(source)
                )
                label = (
                    short
                    if source_party in {"未提供往来对象", "公司账户内部划转"}
                    else f"{short} · {source_party}"
                )
                parts.append({"party": label, "amount_fen": source_amount})
            if sum(part["amount_fen"] for part in parts) != abs(amount):
                raise KernelError("dashboard_bank_match_difference", "银行原行的组合来源金额不一致")
            party = f"组合{'收款' if amount > 0 else '付款'} · {len(parts)} 项"
            batch = {"bank_row_count": 1, "total_fen": abs(amount), "items": parts}
        elif batch:
            party = batch_party
        elif calc:
            party = self.money_parties(
                calc,
                1,
                internal_transfer=self.calculation_is_internal_transfer(calc),
            )
        else:
            party = "未提供"
        item = {
            "id": row["page_key"],
            "date": row["actual_date"],
            "account_id": row["account_id"],
            "account_code": account["code"],
            "account_name": account["name"],
            "direction": "inflow" if amount > 0 else "outflow",
            "amount_fen": abs(amount),
            "signed_amount_fen": amount,
            "party": party,
            "memo": row["description"] or "",
        }
        if batch:
            item["batch_payment"] = batch
        return item

    def has_investment_sources(self):
        if self.investment_registered is None:
            # All actual investment settlements name an investment source. An
            # indexed absence check avoids decoding unrelated payment outcomes.
            self.investment_registered = (
                self.connection.execute(
                    "SELECT 1 FROM subject WHERE kind IN "
                    "('money_fund_subscription','money_fund_redemption','opening_money_fund') "
                    "LIMIT 1"
                ).fetchone()
                is not None
            )
        return self.investment_registered

    def investment_source(self, *, current=False):
        self.has_investment_sources()
        source, parameters = self.events(
            current=current,
            accounts=None if current else {"1101"},
            subjects=None if self.investment_registered else set(),
        )
        source = source.rstrip() + (
            ", investments AS (SELECT *,json_extract(outcome,'$.values.fund_id') fund_id FROM "
            "events "
            "WHERE kind IN ('money_fund_subscription','money_fund_redemption')), lots AS ("
            "SELECT e.event_id,'money-fund-cost:'||json_extract(m.value,'$.subject_id') "
            "balance_key,"
            "json_extract(m.value,'$.values.fund_id') fund_id FROM events e,"
            "json_each(e.outcome,'$.values.members') m "
            "WHERE e.opening AND json_extract(m.value,'$.kind')='opening_money_fund') "
        )
        return source, parameters

    def investment_events(self):
        source, parameters = self.investment_source(current=True)
        source_ids = {
            row[0]
            for row in self.connection.execute(
                source + "SELECT DISTINCT json_extract(s.value,'$.source_calculation') "
                "FROM events e,"
                "json_each(e.outcome,'$.values.settlements') s "
                # Every direct source kind participates in investment selection.
                # Authenticate before filtering so damaged/missing headers
                # cannot silently remove actual settlements from the summary.
                "WHERE EXISTS(SELECT 1 FROM effects b WHERE b.event_id=e.event_id "
                "AND b.category IN ('bank','cash','platform'))",
                parameters,
            )
        }
        # The exact saved source named by a settlement can precede a valid
        # no-impact review. Authenticate its own input identity; do not claim
        # that it was the later independently adopted close head.
        self.snap.reads.verify_saved_input_identity(source_ids)
        self.snap.reads.verify_sql_outcomes(source_ids)
        query = source + (
            "SELECT printf('%012d:%s:confirmation',number,calculation_id) "
            "page_key,(SELECT f.subject_id FROM calculation ic JOIN fact_revision f "
            "ON f.id=ic.fact_id WHERE ic.id=investments.calculation_id) subject_id,"
            "number,sign,kind,fund_id,"
            "json_extract(outcome,'$.values.confirmation_date') "
            "actual_date,sign*json_extract(outcome,'$.values.cost_fen') cost_fen,"
            "sign*json_extract(outcome,'$.values.net_proceeds_fen') net_proceeds_fen,"
            "CASE WHEN kind='money_fund_redemption' THEN "
            "sign*coalesce(json_extract(outcome,'$.values.investment_income_fen'),0) END "
            "investment_income_fen,NULL settlement_fen,0 settlement FROM investments UNION ALL "
            "SELECT printf('%012d:%s:settlement:%06d',e.number,e.calculation_id,CAST(s.key AS "
            "INTEGER)),(SELECT f.subject_id FROM calculation ec JOIN fact_revision f "
            "ON f.id=ec.fact_id WHERE ec.id=e.calculation_id),e.number,e.sign,c.kind,"
            "json_extract(c.outcome,'$.values.fund_id'),json_extract(e.outcome,'$.values.actual_date'),NULL,NULL,NULL,"
            "e.sign*json_extract(s.value,'$.amount_fen'),1 FROM events "
            "e,json_each(e.outcome,'$.values.settlements') s "
            "CROSS JOIN calculation c ON c.id=json_extract(s.value,'$.source_calculation') "
            "WHERE c.kind IN ('money_fund_subscription','money_fund_redemption') AND "
            "EXISTS(SELECT 1 FROM effects b "
            "WHERE b.event_id=e.event_id AND b.category IN ('bank','cash','platform'))"
        )
        return query, parameters

    def investment_summary(self, *, page_request=None):
        source, parameters = self.investment_source()
        for row in self.connection.execute(
            source
            + "SELECT coalesce(l.fund_id,json_extract(e.outcome,'$.values.fund_id')) fund_id,"
            "sum(e.sign*e.amount) closing_cost_fen,"
            "sum(CASE WHEN e.period<? OR e.opening THEN e.sign*e.amount ELSE 0 END) "
            "opening_cost_fen,"
            "sum(CASE WHEN e.period=? AND NOT e.opening AND e.kind='money_fund_subscription' "
            "THEN e.sign*e.amount ELSE 0 END) subscription_cost_fen,"
            "sum(CASE WHEN e.period=? AND NOT e.opening AND e.kind='money_fund_redemption' THEN "
            "-e.sign*e.amount ELSE 0 END) redemption_cost_fen "
            "FROM effects e LEFT JOIN lots l USING(event_id,balance_key) "
            "WHERE e.category='short_term_investment' "
            "GROUP BY coalesce(l.fund_id,json_extract(e.outcome,'$.values.fund_id'))",
            [*parameters, self.snap.month, self.snap.month, self.snap.month],
        ):
            if row["fund_id"] is None:
                raise KernelError("dashboard_investment_source_missing", "投资余额缺少正式成本来源")
            self.base_product(row["fund_id"]).update(dict(row))
        unknown = [
            candidate["calculation_id"]
            for issue in self.issues
            for candidate in issue["candidates"]
            if candidate["kind"] == "opening_package"
        ]
        self.snap.reads.verify_saved_input_identity(unknown)
        self.snap.reads.verify_sql_outcomes(unknown)
        for row in self.connection.execute(
            "SELECT DISTINCT json_extract(m.value,'$.values.fund_id') fund_id FROM json_each(?) "
            "ids JOIN calculation c "
            "ON c.id=ids.value,json_each(c.outcome,'$.values.members') m WHERE "
            "json_extract(m.value,'$.kind')='opening_money_fund'",
            (canonical(unknown),),
        ):
            self.base_product(row["fund_id"]).update(opening_cost_fen=None, closing_cost_fen=None)
        source, parameters = self.investment_events()
        event_count = actual_payments = actual_receipts = 0
        summary_columns = (
            "fund_id",
            "investment_income_fen",
            "event_count",
            "actual_payments_fen",
            "actual_receipts_fen",
        )
        summary_select = (
            "SELECT fund_id,coalesce(sum(investment_income_fen),0) investment_income_fen,"
            "count(*) event_count,coalesce(sum(CASE WHEN "
            "kind='money_fund_subscription' THEN settlement_fen ELSE 0 END),0) "
            "actual_payments_fen,coalesce(sum(CASE WHEN kind='money_fund_redemption' "
            "THEN settlement_fen ELSE 0 END),0) actual_receipts_fen"
        )
        if page_request is None:
            summaries = self.connection.execute(
                summary_select + f" FROM ({source}) GROUP BY fund_id", parameters
            )
        else:
            summaries, rows, page = _sql_summary_page(
                self.connection,
                source,
                parameters,
                summary_select + " FROM source_rows GROUP BY fund_id",
                summary_columns,
                **page_request,
            )
            self.shared_pages["investment_events"] = rows, page
        for row in summaries:
            self.base_product(row["fund_id"])["investment_income_fen"] = row[
                "investment_income_fen"
            ]
            event_count += row["event_count"]
            actual_payments += row["actual_payments_fen"]
            actual_receipts += row["actual_receipts_fen"]
        totals = {
            key: _sum(self.product_rows.values(), key)
            for key in (
                "opening_cost_fen",
                "subscription_cost_fen",
                "redemption_cost_fen",
                "closing_cost_fen",
                "investment_income_fen",
            )
        }
        totals.update(
            event_count=event_count,
            actual_payments_fen=actual_payments,
            actual_receipts_fen=actual_receipts,
        )
        return totals

    def product_display(self, ident):
        profile = self.profile("asset", ident)
        return {
            "name": profile.get("display_name") or "未提供基金名称",
        }

    def investment_item(self, row):
        purchase = row["kind"] == "money_fund_subscription"
        label = (
            ("申购实际付款" if purchase else "赎回实际到账")
            if row["settlement"]
            else ("申购确认" if purchase else "赎回确认")
        )
        return {
            "id": row["page_key"],
            "subject_id": row["subject_id"],
            "date": row["actual_date"],
            "period": self.snap.period,
            "fund_id": row["fund_id"],
            "name": self.product_display(row["fund_id"])["name"],
            "type": ("更正原业务：" if row["sign"] < 0 else "") + label,
            **{
                key: row[key]
                for key in (
                    "cost_fen",
                    "net_proceeds_fen",
                    "investment_income_fen",
                    "settlement_fen",
                )
            },
        }


def funds(snap, *, sections=None, cursors=None, limit=20, filters=None, summary_only=False,
          select_first_account=False):
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("每页数量必须为 1 至 500")
    sections = set() if summary_only else SECTIONS if sections is None else set(sections)
    if sections - SECTIONS:
        raise ValueError("未知资金明细集合")
    cursors = cursors or {}
    filters = {} if filters is None else filters
    page_requests = {}
    if "movements" in sections and not select_first_account:
        where, values = "1=1", []
        if filters.get("movement_account_type"):
            category = next(
                (
                    key
                    for key, value in FUND_TYPES.items()
                    if value == filters["movement_account_type"]
                ),
                "",
            )
            where += " AND category=? AND balance_key=?"
            values.extend((category, filters.get("movement_account_id")))
        page_requests["movements"] = {
            "after": cursors.get("movements"),
            "limit": limit,
            "where": where,
            "filters": values,
        }
    if "statements" in sections:
        where, values = "1=1", []
        if filters.get("statement_account_id"):
            where += " AND account_id=?"
            values.append(filters["statement_account_id"])
        page_requests["statements"] = {
            "after": cursors.get("statements"),
            "limit": limit,
            "where": where,
            "filters": values,
        }
    if "investment_events" in sections:
        page_requests["investment_events"] = {
            "after": cursors.get("investment_events"),
            "limit": limit,
        }
    read = FundsRead(snap, amounts_only=summary_only)
    if "movements" in page_requests:
        movement_totals = read.account_summary(page_request=page_requests["movements"])
    elif select_first_account:
        movement_totals = read.account_summary(first_page_request={"after": None, "limit": limit})
    else:
        movement_totals = read.account_summary()
    if summary_only:
        # The owner brief consumes money amounts. account_summary has already
        # proved the full money scope and independent monetary opening states.
        accounts = [*read.account_rows.values(), *read.omitted_account_rows.values()]
        bank_accounts = [item for item in accounts if item["type"] == "bank"]
        return {
            "bank_calculation": {
                name: _sum(bank_accounts, name)
                for name in ("opening_fen", "inflow_fen", "outflow_fen")
            },
            "total_fen": _sum(accounts, "closing_fen"),
            "net_change_fen": _sum(accounts, "net_change_fen"),
            **{
                FUND_TYPES[category] + "_fen": _sum(
                    [item for item in accounts if item["type"] == FUND_TYPES[category]],
                    "closing_fen",
                )
                for category in FUND_TYPES
            },
            **{
                key: movement_totals[key]
                for key in ("inflow_fen", "outflow_fen", "internal_transfer_fen")
            },
        }
    account_keys = []
    if select_first_account or "accounts" in sections:
        snap.metadata.prime_profiles(
            "fund_account", {ident for _category, ident in read.account_rows}
        )
        account_keys = sorted(read.account_rows, key=lambda key: (
            pinyin_key(read.account_display(*key)["name"]), key,
        ))
    if select_first_account:
        # Selection uses the proved visible account set, including zero-activity
        # openings, and excludes identities retired by opening corrections.
        first = next(iter(account_keys), None)
        if first is not None:
            category, ident = first
            filters["movement_account_type"] = FUND_TYPES[category]
            filters["movement_account_id"] = ident
            where, values = "category=? AND balance_key=?", (category, ident)
        else:
            where, values = "0=1", ()
        if "movements" not in read.shared_pages:
            source, parameters = read.movements()
            # The company summary is already proved. Reuse the bounded SQL page
            # primitive with an empty summary instead of computing it a second time.
            _, rows, page = _sql_summary_page(
                read.connection, source, parameters,
                "SELECT NULL unused WHERE 0", ("unused",),
                after=None, limit=limit, where=where, filters=values,
                order_rows=read.movement_order,
            )
            read.shared_pages["movements"] = rows, page
    bank = (
        read.bank_summary(page_request=page_requests["statements"])
        if "statements" in page_requests
        else read.bank_summary()
    )
    if not summary_only:
        bank = {
            key: value
            for key, value in bank.items()
            if key
            not in {"matched_count", "unmatched_count", "needs_review_count", "unmatched_totals"}
        } | {
            "review_state": "pending"
            if any(
                item["reconciliation"]["state"] in {"attention", "pending"}
                for item in read.account_rows.values()
                if item["type"] == "bank"
            )
            else "complete"
        }
    # Detailed funds retain bank coverage, product cost and investment events.
    # The amount proof above includes investment-related money and opening states.
    investments = (
        (
            read.investment_summary(page_request=page_requests["investment_events"])
            if "investment_events" in page_requests
            else read.investment_summary()
        )
        if not summary_only
        else None
    )
    accounts = list(read.account_rows.values())
    complete_accounts = [*accounts, *read.omitted_account_rows.values()]
    complete_bank_accounts = [item for item in complete_accounts if item["type"] == "bank"]
    totals = {key: _sum(complete_accounts, key) for key in ("opening_fen", "net_change_fen")}
    data = (
        totals
        | movement_totals
        | {
            "total_fen": _sum(complete_accounts, "closing_fen"),
            "bank_opening_fen": _sum(complete_bank_accounts, "opening_fen"),
            "bank_inflow_fen": _sum(complete_bank_accounts, "inflow_fen"),
            "bank_outflow_fen": _sum(complete_bank_accounts, "outflow_fen"),
            **{
                FUND_TYPES[category] + "_fen": _sum(
                    [item for item in complete_accounts if item["type"] == FUND_TYPES[category]],
                    "closing_fen",
                )
                for category in FUND_TYPES
            },
            "account_count": len(accounts),
            "selected_movement_account": {
                "type": filters["movement_account_type"],
                "account_id": filters["movement_account_id"],
            } if filters.get("movement_account_type") else None,
            **{
                FUND_TYPES[category] + "_account_count": sum(
                    item["type"] == FUND_TYPES[category] for item in accounts
                )
                for category in FUND_TYPES
            },
            "attention_account_count": sum(
                item["negative_balance"]
                or item["closing_fen"] is None
                or item["reconciliation"]["state"] in {"attention", "pending"}
                for item in accounts
            ),
            **({"investments": investments} if not summary_only else {}),
            "bank_statement": bank,
            "collections": {},
        }
    )
    for section in sorted(sections):
        after = cursors.get(section)
        if section in {"accounts", "investment_products"}:
            if section == "investment_products":
                snap.metadata.prime_profiles("asset", read.product_rows)
            mapping = (
                {
                    category + ":" + ident: (category, ident)
                    for category, ident in account_keys
                }
                if section == "accounts"
                else {
                    ident: ident for ident in sorted(
                        read.product_rows,
                        key=lambda ident: (pinyin_key(read.product_display(ident)["name"]), ident),
                    )
                }
            )
            keys, page = page_keys(mapping, after, limit)
            items = (
                [read.account_item(mapping[key]) for key in keys]
                if section == "accounts"
                else [read.product_rows[key] | read.product_display(key) for key in keys]
            )
        else:
            if section == "movements":
                present = read.movement_item
            elif section == "statements":
                present = read.bank_item
            else:
                present = read.investment_item
            rows, page = read.shared_pages[section]
            if section == "movements":
                read.prepare_calculation_items(row["calculation_id"] for row in rows)
            elif section == "statements":
                read.prepare_bank_items(rows)
            items = [present(row) for row in rows]
        data["collections"][section] = {"items": items, "page": page}
    return data
