"""Narrow display locators for globally ordered business pages.

These scalar candidates choose page identities, not accounting adoption or money.
The existing selected-page readers still authenticate facts, results and frozen
states before any business content is returned.
"""

from __future__ import annotations

from .contracts import KernelError
from .domains.money import SETTLEMENT_PAYMENT_KINDS
from .schema import sequence_model, table_name
from .text_sort import pinyin_key
from .types import YearMonth, canonical

DATES = (
    "actual_date",
    "acquisition_date",
    "in_use_date",
    "disposal_date",
    "income_date",
    "fulfillment_date",
    "recognition_date",
)
PARTIES = (
    "employee_id",
    "person_id",
    "counterparty_id",
    "customer_id",
    "supplier_id",
    "owner_id",
    "buyer_id",
    "lender_id",
    "payer_id",
    "beneficiary_id",
    "recipient_id",
    "authority_id",
)
CONTRIBUTIONS = frozenset(
    {"employee_social", "employer_social", "employee_housing", "employer_housing"}
)


def fact_sort_scalars(snapshot, headers):
    """Read only declared date/object fields, never full facts or evidence."""
    groups, result = {}, {}
    for row in headers:
        groups.setdefault(row["kind"], set()).add(row["fact_id"])
    wanted = (
        *DATES,
        *PARTIES,
        "payment_method",
        "payroll_period",
        "component",
        "nature",
        "payer_kind",
        "creditor_kind",
        "tax_kind",
        "side",
        "expense_class",
    )
    for kind, identifiers in groups.items():
        model = snapshot.store.registry.models[kind]
        fields = [name for name in wanted if name in model.model_fields]
        columns = ",".join(["revision_id", "period", *(f'"{name}"' for name in fields)])
        for row in snapshot.connection.execute(
            f"SELECT {columns} FROM {table_name(kind)} WHERE revision_id IN "
            "(SELECT value FROM json_each(?))",
            (canonical(sorted(identifiers)),),
        ):
            value = dict(row)
            value["period"] = str(YearMonth.from_ordinal(value["period"]))
            if type(value.get("payroll_period")) is int:
                value["payroll_period"] = str(YearMonth.from_ordinal(value["payroll_period"]))
            value["recipients"] = []
            result[row["revision_id"]] = value
        for field in ("allocations", "sources"):
            info = model.model_fields.get(field)
            child = sequence_model(info.annotation) if info is not None else None
            if child is None or "recipient_id" not in child.model_fields:
                continue
            columns = [
                name
                for name in ("recipient_id", "source_id", "source_kind", "obligation")
                if name in child.model_fields
            ]
            for row in snapshot.connection.execute(
                "SELECT revision_id," + ",".join(columns) + f" FROM {table_name(kind)}_{field} "
                "WHERE revision_id IN (SELECT value FROM json_each(?)) "
                "ORDER BY revision_id,item_no",
                (canonical(sorted(identifiers)),),
            ):
                result[row["revision_id"]]["recipients"].append(dict(row))
        if not identifiers <= result.keys():
            raise KernelError("content_integrity_failed", "清单排序的业务来源缺失")
    return result


def calculation_headers(snapshot, identifiers):
    identifiers = set(identifiers)
    if not identifiers:
        return {}
    rows = {
        row["id"]: dict(row)
        for row in snapshot.connection.execute(
            "SELECT c.id,c.subject_id,c.fact_id,c.kind,c.period FROM json_each(?) ids "
            "JOIN calculation c ON c.id=ids.value",
            (canonical(sorted(identifiers)),),
        )
    }
    if rows.keys() != identifiers:
        raise KernelError("content_integrity_failed", "清单排序的核算来源缺失")
    return rows


def _prime_party_metadata(snapshot, identifiers):
    identifiers = set(identifiers)
    snapshot.metadata.prime_profiles("employee", identifiers)
    snapshot.metadata.prime_profiles("counterparty", identifiers)
    unnamed = {
        ident
        for ident in identifiers
        if not any(
            profiles[kind].get(ident, {}).get("display_name")
            for profiles in (snapshot.profiles, snapshot.current_profiles)
            for kind in ("employee", "counterparty")
        )
    }
    if not unnamed:
        return
    snapshot.metadata.payee_records.prime(unnamed)
    snapshot.metadata.current_payees.prime(unnamed)
    without_payees = {
        ident
        for ident in unnamed
        if ident not in snapshot.payees and ident not in snapshot.current_payees
    }
    snapshot.metadata.tax_candidates.prime(without_payees)


def business_sort_metadata(snapshot, identifiers, *, funds=False):
    headers = calculation_headers(snapshot, identifiers)
    scalars = fact_sort_scalars(snapshot, headers.values())
    parties = {}
    for ident, header in headers.items():
        value = scalars[header["fact_id"]]
        fields = (
            ("counterparty_id", "owner_id", "lender_id", "payer_id", "recipient_id")
            if funds
            else PARTIES
        )
        parties[ident] = {value[field] for field in fields if value.get(field)}
        if value.get("payment_method") == "bank_batch":
            parties[ident].discard(value.get("counterparty_id"))
        parties[ident].update(
            row["recipient_id"] for row in value["recipients"] if row.get("recipient_id")
        )
        parties[ident].discard("payroll-group")
    # Actual single-payment recipients already determine the displayed objects.
    # Other candidates need ancestry only when they refer to a balance key.
    relation_ids = [
        ident
        for ident, header in headers.items()
        if not (
            header["kind"] in SETTLEMENT_PAYMENT_KINDS
            and scalars[header["fact_id"]].get("counterparty_id")
            and scalars[header["fact_id"]].get("payment_method") != "bank_batch"
        )
    ]
    relation_rows = (
        snapshot.connection.execute(
            "WITH RECURSIVE keys AS MATERIALIZED ("
            "SELECT ids.value root,json_extract(b.value,'$.key') obligation_key "
            "FROM json_each(?) ids JOIN calculation c ON c.id=ids.value,"
            "json_each(c.outcome,'$.balances') b), "
            "roots(id) AS (SELECT DISTINCT root FROM keys), "
            "ancestry(root,id) AS (SELECT id,id FROM roots UNION SELECT a.root,d.upstream_id "
            "FROM ancestry a JOIN dependency_calculation d ON d.calculation_id=a.id) "
            "SELECT DISTINCT a.root,c.id ancestor_id,c.subject_id,c.kind,"
            "json_extract(o.value,'$.name') component,"
            "json_extract(o.value,'$.counterparty_id') party FROM ancestry a "
            "JOIN calculation c ON c.id=a.id,json_each(c.outcome,'$.values.obligations') o "
            "JOIN keys k ON k.root=a.root AND k.obligation_key=json_extract(o.value,'$.key') "
            "WHERE json_extract(o.value,'$.counterparty_id') IS NOT NULL "
            "UNION SELECT DISTINCT a.root,a.id,NULL,NULL,NULL,NULL FROM ancestry a "
            "JOIN calculation c ON c.id=a.id "
            "WHERE c.kind NOT IN ('asset_activation','asset_consumption')",
            (canonical(sorted(relation_ids)),),
        ).fetchall()
        if relation_ids
        else ()
    )
    # These exact ancestor results supply identities used by the page and group
    # keys. Prove their original publication bindings before using JSON scalars.
    if relation_rows:
        from .report_open_contribution import verify_published_source_bindings

        used = {row["ancestor_id"] for row in relation_rows}
        fallback = verify_published_source_bindings(snapshot.reads, used)
        if fallback:
            snapshot.reads.verify_saved_input_identity(fallback)
        snapshot.reads.verify_sql_outcomes(used)
    for row in relation_rows:
        if row["party"] is None:
            continue
        value = scalars[headers[row["root"]]["fact_id"]]
        recipient = next(
            (
                item.get("recipient_id")
                for item in value["recipients"]
                if item.get("source_id") == row["subject_id"]
                and item.get("source_kind") == row["kind"]
                and item.get("obligation") == row["component"]
            ),
            None,
        )
        if not recipient:
            parties[row["root"]].add(row["party"])
    all_parties = set().union(*parties.values()) if parties else set()
    _prime_party_metadata(snapshot, all_parties)
    result = {}
    for ident, header in headers.items():
        value, identities = scalars[header["fact_id"]], tuple(sorted(parties[ident]))
        name = "、".join(sorted((snapshot.party(party) for party in identities), key=pinyin_key))
        actual = next(
            (
                value[field]
                for field in (("actual_date",) if funds else DATES)
                if isinstance(value.get(field), str) and len(value[field]) == 10
            ),
            None,
        )
        result[ident] = {"date": actual, "party": name, "identities": identities}
    return result


def date_object_key(
    metadata,
    period,
    identity,
    *,
    month_confirmation=True,
    internal=False,
    missing_party="无需往来对象",
):
    actual = metadata["date"]
    if actual:
        date = (0, actual[:7], 0, actual)
    elif month_confirmation and period:
        date = (0, period, 1, "")
    else:
        date = (1, "", 0, "")
    party = metadata["party"] or ("公司账户内部划转" if internal else missing_party)
    return (*date, pinyin_key(party), metadata["identities"], identity)


def open_item_order(snapshot, rows):
    """Object/matter identities deliberately exclude undisplayed payroll months."""
    from .dashboard import LABOR_KINDS, PAYROLL_KINDS, _contribution_identity, _open_item_display
    from .dashboard_matters import obligation_matter
    from .dashboard_party_supplements import pass_through_party_supplements

    rows = list(rows)
    supplements = pass_through_party_supplements(
        snapshot,
        [
            {
                **row,
                "key": row["obligation_key"],
                "name": row.get("component"),
                "source_business": {
                    "kind": row.get("source_kind"),
                    "subject_id": row.get("source_subject_id"),
                },
            }
            for row in rows
        ],
    )
    remuneration = [
        row
        for row in rows
        if row.get("source_kind")
        in PAYROLL_KINDS | LABOR_KINDS | {"opening_payroll_payable", "labor_project_cost"}
        and row.get("source_fact_id")
    ]
    scalars = fact_sort_scalars(
        snapshot,
        ({"kind": row["source_kind"], "fact_id": row["source_fact_id"]} for row in remuneration),
    )
    keys, all_parties = {}, set()
    for row in rows:
        data = scalars.get(row.get("source_fact_id"), {})
        component = (
            data.get("component")
            if row.get("source_kind") == "opening_payroll_payable"
            else row.get("component")
        )
        party, missing_party, matter = _open_item_display(
            row.get("source_kind"),
            component,
            data,
            row.get("counterparty_id"),
        )
        if party is None and row["obligation_key"] in supplements:
            party = supplements[row["obligation_key"]]["party_id"]
        all_parties.update((party,) if party else ())
        group = _contribution_identity(
            snapshot.store.company_id, row.get("source_kind"), data, component
        )
        group_key = group["contribution_group_key"]
        if group_key:
            formal = obligation_matter(row.get("source_kind"), component, data=data)
            matter = formal.title if formal is not None else matter
        keys[row["obligation_key"]] = (
            party,
            matter,
            group_key or row["obligation_key"],
            missing_party,
        )
    _prime_party_metadata(snapshot, all_parties)
    return sorted(
        keys,
        key=lambda key: (
            pinyin_key(snapshot.party(keys[key][0]) if keys[key][0] else keys[key][3]),
            pinyin_key(keys[key][1]),
            keys[key][0] or "",
            keys[key][2],
            key,
        ),
    )
