"""Complete object/matter groups with bounded, exact obligation members.

Whole-scope reads retain only verified amounts and formal source locators. Facts,
results, settlement histories and vouchers are not hydrated to make group totals.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import NamedTuple

from .contracts import KernelError
from .dashboard_matters import obligation_matter, owner_uses_standard_text
from .settlement_freeze import _DashboardSourceRecord
from .text_sort import pinyin_key
from .types import YearMonth, canonical, checked

CONFIGURATIONS = {
    "customer_receivables": ("待收客户款", "receivable"),
    "supplier_advances": ("待冲抵供应商预付款", "receivable"),
    "refundable_deposit_receivables": ("待收回保证金", "receivable"),
    "other_receivables": ("其他应收事项", "receivable"),
    "supplier_payables": ("待付供应商款", "payable"),
    "employee_payables": ("待付员工报销款", "payable"),
    "payroll_payables": ("待付工资与社保公积金", "payable"),
    "labor_payables": ("待付个人劳务", "payable"),
    "tax_payables": ("待缴税费", "payable"),
    "other_payables": ("其他应付事项", "payable"),
}


class _DisplayCohort(NamedTuple):
    """Shared presentation only; exact source facts and amounts stay separate."""

    group_key: str
    party_id: str | None
    missing_party: str
    description: str
    component: str | None
    category_key: str

    def __getitem__(self, key):
        return getattr(self, key) if isinstance(key, str) else tuple.__getitem__(self, key)

    def get(self, key, default=None):
        return getattr(self, key, default)


def _sum(values):
    total = 0
    for value in values:
        if value is None:
            return None
        total = checked(total + value)
    return total


def _status(members):
    """Keep a member's abnormal progress even if group amounts cancel out."""
    states = {member.get("settlement_status") if member else None for member in members}
    if None in states or "unestablished" in states or "unknown" in states:
        return "unestablished"
    if "checking" in states:
        return "checking"
    if "over_settled" in states:
        return "over_settled"
    if states == {"settled"}:
        return "settled"
    if "partial" in states or "settled" in states:
        return "partial"
    return "open"


def _source_headers(snap, sources):
    """Bind narrow consumed fact scalars to their exact sealed source identities.

    Reuse the established physical fact verifier's SQL hash fast path. Exceptional
    noncanonical/composite facts retain its strict raw decoder fallback.
    """
    from .query_reads import _exact_source_header_batch

    sources = list(sources)
    identifiers = {row.source_calculation_id
                   if isinstance(row, _DashboardSourceRecord)
                   else row["source_calculation_id"] for row in sources}
    if not identifiers:
        return {}
    if None in identifiers:
        raise KernelError("content_integrity_failed", "待收待付的精确业务来源缺失")
    with _exact_source_header_batch(snap.connection, identifiers, reads=snap.reads) as headers:
        if headers.keys() != identifiers or any(
            header["fact_subject"] is None or header["fact_kind"] is None
            for header in headers.values()
        ):
            raise KernelError("content_integrity_failed", "待收待付的精确业务来源缺失")
        for item in sources:
            if isinstance(item, _DashboardSourceRecord):
                header = headers[item.source_calculation_id]
                source_identity = item.source_subject_id, item.source_kind, item.source_fact_id
            else:
                header = headers[item["source_calculation_id"]]
                business = item.get("source_business") or {}
                source_identity = (
                    business.get("subject_id"), business.get("kind"), item.get("source_fact_id"),
                )
            if (
                not header["cs"]
                or not header["fs"]
                or (header["subject_id"], header["kind"], header["period"])
                != (header["fact_subject"], header["fact_kind"], header["fact_period"])
                or (header["subject_id"], header["kind"], header["fact_id"])
                != source_identity
            ):
                raise KernelError("content_integrity_failed", "待收待付的精确业务来源不匹配")
        snap.reads._verify_selected_fact_bodies(headers.values())
    return headers


def _locators(snap, sources):
    from .dashboard_sort import fact_identity_scalars

    headers = _source_headers(snap, sources)
    scalars = fact_identity_scalars(snap, headers.values()) if headers else {}
    return headers, scalars


def _fact_identity_tokens(scalars):
    """Intern complete, type-sensitive immutable inputs once per exact fact.

    Tokens are local to this group read. They share only display inputs; source
    components, parties, states and amounts retain their independent identity.
    Composite exceptions keep the original per-source interpretation.
    """
    from .dashboard_sort import IDENTITY_FACT_FIELDS

    tokens, inputs = {}, {}
    for fact_id, data in scalars.items():
        values = tuple(data.get(field) for field in IDENTITY_FACT_FIELDS)
        key = tuple(map(type, values)), values
        try:
            token = inputs.get(key)
            if token is None:
                token = len(inputs)
                inputs[key] = token
        except TypeError:
            token = None
        tokens[fact_id] = token
    return tokens


def _identity(snap, source, scalars, supplements=None, *, display_cache=None, group_cache=None,
              cohort_cache=None, fact_tokens=None):
    from .dashboard import _open_item_display

    if isinstance(source, _DashboardSourceRecord):
        kind, fact_id, name, party = (
            source.source_kind, source.source_fact_id, source.name, source.counterparty_id,
        )
        source_key, category, category_key = source.key, source.category, source.category_key
    else:
        kind = (source.get("source_business") or {}).get("kind", "")
        fact_id, name, party = (
            source.get("source_fact_id"), source.get("name"), source.get("counterparty_id"),
        )
        source_key, category, category_key = (
            source["key"], source.get("category"), source["category_key"],
        )
    data = scalars.get(fact_id, {})
    component = data.get("component") if kind == "opening_payroll_payable" else name
    # These are the scalar inputs consumed by the shared display/matter rules.
    # Dates, amounts and source versions do not identify an employee's matter;
    # each source keeps its own verified scalars outside the shared cohort.
    token = fact_tokens.get(fact_id) if fact_tokens is not None else None
    if token is not None:
        inputs = kind, component, party
        display_key = token, tuple(map(type, inputs)), inputs
    else:
        display_fields = ("employee_id", "person_id", "nature", "payer_kind", "creditor_kind",
                          "expense_class", "tax_kind", "side")
        inputs = (kind, component, party,
                  *(data.get(field) for field in display_fields))
        display_key = tuple(map(type, inputs)), inputs
    try:
        hash(display_key)
    except TypeError:
        display_key = None
    supplement = (supplements or {}).get(source_key)
    supplemental_party = supplement["party_id"] if supplement is not None else None
    cohort_inputs = (category, category_key, supplemental_party)
    cohort_key = (display_key, tuple(map(type, cohort_inputs)), cohort_inputs)
    try:
        hash(cohort_key)
    except TypeError:
        cohort_key = None
    if cohort_cache is not None and cohort_key is not None:
        shared = cohort_cache.get(cohort_key)
        if shared is not None:
            return shared
    cached = display_cache.get(display_key) if display_cache is not None else None
    if cached is None:
        party_id, missing, description = _open_item_display(
            kind, component, data, party,
        )
        matter = obligation_matter(kind, component, data=data)
        if display_cache is not None and display_key is not None:
            display_cache[display_key] = party_id, missing, description, matter
    else:
        party_id, missing, description, matter = cached
    if not party_id and supplement is not None:
        party_id = supplement["party_id"]
    if matter is not None:
        description = matter.title
    if (matter is not None and matter.open_category is not None
            and CONFIGURATIONS[matter.open_category][1] == category):
        category_key = matter.open_category
    # An obligation with no formal displayed object never borrows a display name
    # or the agency creditor of another employee's contribution as its identity.
    identity = party_id or source_key
    group_inputs = (
        snap.store.company_id, category, identity,
        matter.key if matter is not None else None,
        source_key if matter is None or not party_id else None,
    )
    # Unknown matters and missing formal objects keep their per-source key.
    # Only an explicit formal matter/object can share its presentation digest.
    group_key = (
        (tuple(map(type, group_inputs)), group_inputs)
        if matter is not None and party_id else None
    )
    try:
        hash(group_key)
    except TypeError:
        group_key = None
    key = group_cache.get(group_key) if group_cache is not None else None
    if key is None:
        key = hashlib.sha256(canonical(list(group_inputs)).encode("utf-8")).hexdigest()
        if group_cache is not None and group_key is not None:
            group_cache[group_key] = key
    cohort = _DisplayCohort(key, party_id, missing, description, component, category_key)
    if (cohort_cache is not None and cohort_key is not None
            and display_key is not None and matter is not None and party_id):
        cohort_cache[cohort_key] = cohort
    return cohort


def _display_categories(value, identities):
    """Project navigation without rewriting the authenticated source or freeze."""
    records = isinstance(value["obligations"], tuple)
    obligations, categories = ([] if not records else value["obligations"]), {}
    measures = value.get("_category_measures")
    if measures is not None:
        measures = {key: dict(item) for key, item in measures.items()}
    for source in value["obligations"]:
        if records:
            source_key, original_key, remaining = (
                source.key, source.category_key, source.remaining_fen,
            )
        else:
            source_key, original_key, remaining = (
                source["key"], source["category_key"], source["remaining_fen"],
            )
        identity = identities.get(source_key)
        key = identity["category_key"] if identity is not None else original_key
        if not records:
            source = {**source, "category_key": key}
            obligations.append(source)
        if remaining == 0:
            continue
        if measures is not None:
            if key != original_key:
                for category_key, direction in ((original_key, -1), (key, 1)):
                    measure = measures.setdefault(
                        category_key, {"count": 0, "sum": 0, "unknown_count": 0},
                    )
                    measure["count"] += direction
                    measure["sum"] = checked(
                        measure["sum"] + direction * (remaining or 0)
                    )
                    measure["unknown_count"] += direction * (remaining is None)
            continue
        category = categories.setdefault(key, {"count": 0, "amount": 0})
        category["count"] += 1
        category["amount"] = _sum((category["amount"], remaining))
    if measures is not None:
        categories = {
            key: {"count": item["count"], "amount": None if item["unknown_count"] else item["sum"]}
            for key, item in measures.items() if item["count"]
        }
    return {**{key: item for key, item in value.items() if key != "_category_measures"},
            "obligations": obligations, "categories": categories}


def _record_data(snap):
    from .dashboard_party_supplements import pass_through_party_supplements
    from .dashboard_sort import _prime_party_metadata
    from .settlement_freeze import frozen_dashboard_group_sources
    from .settlement_projection import settlement_dashboard_open_sources

    historical = frozen_dashboard_group_sources(snap.connection, snap.period, reads=snap.reads)
    reader = (frozen_dashboard_group_sources if historical is not None
              else settlement_dashboard_open_sources)
    if historical is None:
        historical = reader(
            snap.connection, snap.period, reads=snap.reads, reuse_scope_sources=True,
        )
    sources = historical["obligations"]
    source_records = isinstance(sources, tuple)
    current = reader(
        snap.connection,
        snap.period,
        current=True,
        page_keys={row.key if source_records else row["key"] for row in sources},
        exact_sources=True,
        reads=snap.reads,
        **({"reuse_scope_sources": True} if reader is settlement_dashboard_open_sources else {}),
    )
    headers, scalars = _locators(snap, sources)
    fact_tokens = _fact_identity_tokens(scalars)
    supplements = pass_through_party_supplements(
        snap, (source for source in sources if not isinstance(source, _DashboardSourceRecord)
               or source.source_kind == "pass_through"),
    )
    display_cache, group_cache, cohort_cache = {}, {}, {}
    identities = {
        (row.key if source_records else row["key"]): _identity(snap, row, scalars, supplements,
                              display_cache=display_cache, group_cache=group_cache,
                              cohort_cache=cohort_cache, fact_tokens=fact_tokens)
        for row in sources
    }
    equal_records = source_records and current["obligations"] is sources
    historical = _display_categories(historical, identities)
    current = ({**current, "categories": historical["categories"]} if equal_records
               else _display_categories(current, identities))
    sources = historical["obligations"]
    parties = {value["party_id"] for value in identities.values() if value["party_id"]}
    _prime_party_metadata(snap, parties)
    grouped = defaultdict(list)
    for source in sources:
        source_key = source.key if source_records else source["key"]
        grouped[identities[source_key]["group_key"]].append(source)
    ordered = sorted(
        grouped,
        key=lambda key: (
            pinyin_key(
                snap.party(identities[grouped[key][0]["key"]]["party_id"])
                if identities[grouped[key][0]["key"]]["party_id"]
                else identities[grouped[key][0]["key"]]["missing_party"]
            ),
            pinyin_key(identities[grouped[key][0]["key"]]["description"]),
            key,
        ),
    )
    result = {
        "historical": historical,
        "current": current,
        "groups": grouped,
        "ordered": ordered,
        "identities": identities,
        "source_headers": headers,
        "source_scalars": scalars,
        "current_by_key": {
            row.key if source_records else row["key"]: row for row in current["obligations"]
        },
    }
    return result


def _data(snap):
    if not hasattr(snap, "_open_group_data"):
        snap._open_group_data = _record_data(snap)
    return snap._open_group_data


def _page(keys, after, limit, *, positions=None):
    if limit < 1:
        raise ValueError("page limit must be positive")
    if after is not None and after not in (positions if positions is not None else keys):
        raise KernelError("dashboard_snapshot_changed", "分页位置已变化，请重新加载明细。")
    start = ((positions[after] if positions is not None else keys.index(after)) + 1
             if after is not None else 0)
    selected = keys[start : start + limit]
    more = start + len(selected) < len(keys)
    return selected, {
        "total_count": len(keys),
        "filtered_count": len(keys),
        "returned_count": len(selected),
        "has_more": more,
        "next_cursor": selected[-1] if more else None,
    }


def _totals(shared):
    result = {}
    for direction in ("receivable", "payable"):
        rows = [
            value
            for key, value in shared["categories"].items()
            if CONFIGURATIONS[key][1] == direction
        ]
        result[direction + "_count"] = sum(row["count"] for row in rows)
        result[direction + "_fen"] = _sum(row["amount"] for row in rows)
    return result | {
        "total_count": sum(row["count"] for row in shared["categories"].values()),
        "unestablished_count": 0,
        "complete": shared["complete"],
        "status": shared["status"],
        "issues": [dict(issue) for issue in shared["issues"]],
    }


def _group(snap, data, key):
    members = data["groups"][key]
    identity = data["identities"][members[0]["key"]]
    current = [data["current_by_key"].get(row["key"]) for row in members]
    amounts = {
        name: _sum(row[name] for row in members)
        for name in ("source_amount_fen", "paid_fen", "other_settled_fen", "remaining_fen")
    }
    remaining = amounts.pop("remaining_fen")
    current_remaining = _sum(row.get("remaining_fen") if row else None for row in current)
    return {
        "id": key,
        "group_key": key,
        "category_key": identity.get("category_key", members[0]["category_key"]),
        "party": snap.party_field(identity["party_id"], missing=identity["missing_party"])["party"],
        "description": identity["description"],
        "member_count": len(members),
        **amounts,
        "outstanding_fen": remaining,
        "current_outstanding_fen": current_remaining,
        "status": _status(members),
        "current_status": _status(current),
    }


def _verify_displayed_sources(snap, data, sources):
    """Prove exact result bytes for the sources used by returned amounts.

    Frozen totals retain their independent directory/root proof. A displayed
    group or member also binds its historical and current source bodies; a
    matching digest column alone cannot authenticate a changed result payload.
    Successful byte proof is reused only by this read snapshot.
    """
    identifiers, current_sources = set(), {}
    verified = data.setdefault("display_source_identities", set())
    pending = set()
    for source in sources:
        identifiers.add(source["source_calculation_id"])
        current = data["current_by_key"].get(source["key"])
        if current is not None:
            ident = current["source_calculation_id"]
            identifiers.add(ident)
            business = current.get("source_business") or {}
            identity = (
                ident, business.get("kind"), business.get("subject_id"), current["source_fact_id"],
            )
            if identity not in verified:
                header = data["source_headers"].get(ident)
                if header is None or (
                    header["kind"], header["subject_id"], header["fact_id"]
                ) != identity[1:]:
                    current_sources[identity] = current
                pending.add(identity)
    if current_sources:
        _source_headers(snap, current_sources.values())
    snap.reads.verify_sql_outcomes(identifiers)
    verified.update(pending)


def open_group_page(snap, *, after=None, limit=20, summary_only=False):
    data = _data(snap)
    selected, page = _page(data["ordered"], after, limit)
    if not summary_only:
        _verify_displayed_sources(
            snap, data, (source for key in selected for source in data["groups"][key]),
        )
    items = [] if summary_only else [_group(snap, data, key) for key in selected]
    category_groups = defaultdict(list)
    for key, members in data["groups"].items():
        identity = data["identities"][members[0]["key"]]
        category_groups[identity["category_key"]].append(key)
    categories = []
    for key, (label, direction) in CONFIGURATIONS.items():
        total = data["historical"]["categories"].get(key)
        if total is None:
            continue
        categories.append(
            {
                "key": key,
                "label": label,
                "direction": direction,
                "unit": "笔",
                "count": total["count"],
                "group_count": len(category_groups[key]),
                "loaded_count": sum(
                    item["member_count"] for item in items if item["category_key"] == key
                ),
                "outstanding_fen": total["amount"],
            }
        )
    return _totals(data["historical"]) | {
        "group_count": len(data["groups"]),
        "categories": categories,
        "current_outstanding": _totals(data["current"]),
        "collection": {"items": items, "page": page},
        "cutoff_period": data["historical"]["cutoff_period"],
        "current_cutoff_period": data["current"].get(
            "current_cutoff_period", data["current"]["cutoff_period"]
        ),
    }


def _member_vouchers(snap, headers):
    """Locate an adopted source's voucher, whose sealed owner may be older."""
    from .dashboard_reads import Journal
    from .query_reads import selected_voucher_sql

    publications = {}
    for row in snap.connection.execute(
        "SELECT p.* FROM json_each(?) ids JOIN calculation_publication p "
        "ON p.calculation_id=ids.value", (canonical(sorted(headers)),),
    ):
        if row["calculation_id"] in publications:
            raise KernelError("content_integrity_failed", "待收待付的精确业务发布不唯一")
        publications[row["calculation_id"]] = row
    if publications.keys() != headers.keys():
        raise KernelError("content_integrity_failed", "待收待付的精确业务发布缺失")
    snap.reads.verify_publication_records(publications.values())
    if any(publication["subject_id"] != headers[ident]["subject_id"]
           for ident, publication in publications.items()):
        raise KernelError("content_integrity_failed", "待收待付的精确业务发布不匹配")
    with_vouchers = {ident for ident, row in publications.items() if row["voucher_id"]}
    if not with_vouchers:
        return {}
    # The selector accepts physical version IDs. A reviewed publication can
    # retain a stable voucher whose physical version belongs to an older owner.
    # Keep exact publication IDs as the driver: reverses_id IS NULL covers most
    # of the journal and must not turn one member page into a history scan.
    version_ids = {
        row["id"] for row in snap.connection.execute(
            "SELECT v.id FROM json_each(?) ids CROSS JOIN calculation_publication p "
            "ON p.calculation_id=ids.value CROSS JOIN voucher_version v INDEXED BY voucher_period "
            "ON v.voucher_id=p.voucher_id AND v.period=p.posting_period "
            "WHERE v.reverses_id IS NULL", (canonical(sorted(with_vouchers)),),
        )
    }
    query, parameters = selected_voucher_sql(snap.period, voucher_ids=version_ids)
    selected = {}
    for row in snap.connection.execute(query, parameters):
        key = row["voucher_id"], row["period"]
        if row["reverses_id"] is not None or key in selected:
            raise KernelError("content_integrity_failed", "待收待付的精确凭证来源不唯一")
        selected[key] = row
    result, by_period = {}, defaultdict(list)
    for ident, publication in publications.items():
        if publication["voucher_id"] is None:
            continue
        row = selected.get((publication["voucher_id"], publication["posting_period"]))
        if row is None:
            raise KernelError("content_integrity_failed", "待收待付的精确凭证采用不匹配")
        result[ident] = row
        by_period[row["period"]].append(row)
    # Preserve the original physical duplicate rejection for each exact owner,
    # while other legitimate historical versions of its voucher stay separate.
    physical = defaultdict(list)
    for row in snap.connection.execute(
        "SELECT v.id,v.calculation_id,v.voucher_id,v.period FROM json_each(?) ids "
        "CROSS JOIN voucher_version v INDEXED BY voucher_calculation "
        "ON v.calculation_id=ids.value WHERE v.reverses_id IS NULL",
        (canonical(sorted({row["voucher_calculation_id"] for row in result.values()})),),
    ):
        physical[row["calculation_id"], row["voucher_id"], row["period"]].append(row["id"])
    for row in result.values():
        if physical[row["voucher_calculation_id"], row["voucher_id"], row["period"]] != [row["id"]]:
            raise KernelError("content_integrity_failed", "待收待付的精确凭证来源不唯一")
    frozen = {}
    for period, rows in by_period.items():
        journal = Journal(snap, month=period)
        snap.reads.verify_selected_voucher_adoptions(rows, through_period=snap.month)
        frozen.update(journal.authenticated_frozen_adoptions(rows))
    for ident, row in result.items():
        adopted = frozen.get(row["id"])
        adopted_id = (adopted["adopted_calculation_id"] if adopted is not None
                      else row["basis_calculation_id"])
        if adopted_id != ident or row["close_period"] is not None and adopted is None:
            raise KernelError("content_integrity_failed", "待收待付的精确凭证采用不匹配")
    return result


def _ordered_group_members(snap, data, group_key):
    """Reuse the native member order only inside this unchanged source snapshot."""
    cache = snap.semantic_cache.setdefault("open_group_member_order", {})
    if group_key in cache:
        return cache[group_key]
    members = data["groups"].get(group_key)
    if members is None:
        raise KernelError("dashboard_group_not_found", "这组待收待付已变化，请重新加载。")
    # Source month is obtained from its exact adopted calculation. Member
    # ordering never substitutes a management date for the accounting month.
    headers = data["source_headers"]
    ordered = sorted(
        members,
        key=lambda row: (
            headers[row["source_calculation_id"]]["period"],
            data["identities"][row["key"]]["component"] or "",
            row["key"],
        ),
    )
    keys = [row["key"] for row in ordered]
    cache[group_key] = (keys, {row["key"]: row for row in ordered},
                        {key: index for index, key in enumerate(keys)})
    return cache[group_key]


def open_group_members(snap, group_key, *, after=None, limit=20):
    from .dashboard import _contribution_identity, _open_item_display, _recognition
    from .dashboard_sort import CONTRIBUTIONS, fact_sort_scalars

    data = _data(snap)
    keys, by_key, positions = _ordered_group_members(snap, data, group_key)
    selected, page = _page(keys, after, limit, positions=positions)
    sources = [by_key[key] for key in selected]
    headers = data["source_headers"]
    _verify_displayed_sources(snap, data, sources)
    identities, current_by_key = data["identities"], data["current_by_key"]
    selected_headers = {
        source["source_calculation_id"]: headers[source["source_calculation_id"]]
        for source in sources
    }
    scalars = fact_sort_scalars(snap, selected_headers.values())
    subjects = {
        (source.get("source_business") or {}).get("subject_id")
        for source in sources
        if identities[source["key"]]["component"] not in CONTRIBUTIONS
        and not owner_uses_standard_text((source.get("source_business") or {}).get("kind"))
    }
    subjects.discard(None)
    if subjects:
        snap.metadata.prime_profiles("business", subjects)
        snap.management.prime(subjects)
    voucher_rows = _member_vouchers(snap, selected_headers)
    result = []
    for source in sources:
        identity = identities[source["key"]]
        business = source.get("source_business") or {}
        fact_data = scalars[source["source_fact_id"]]
        period = str(
            YearMonth.from_ordinal(selected_headers[source["source_calculation_id"]]["period"])
        )
        recognition = _recognition(fact_data, period)
        current = current_by_key.get(source["key"])
        voucher = voucher_rows.get(source["source_calculation_id"])
        _, _, description = _open_item_display(
            business.get("kind", ""),
            identity["component"],
            fact_data,
            source.get("counterparty_id"),
        )
        purpose = None
        if identity["component"] not in CONTRIBUTIONS:
            description = identity["description"]
        if identity["component"] not in CONTRIBUTIONS and not owner_uses_standard_text(
            business.get("kind")
        ):
            profile = snap.profile("business", business.get("subject_id"))
            supplied = dict.fromkeys(
                value.strip()
                for value in (
                    profile.get("purpose"),
                    profile.get("note"),
                )
                if isinstance(value, str) and value.strip()
            )
            purpose = "；".join(supplied) or None
        result.append(
            {
                "id": source["key"],
                "group_key": group_key,
                "category_key": identity["category_key"],
                "party": snap.party_field(identity["party_id"], missing=identity["missing_party"])[
                    "party"
                ],
                "description": description,
                "purpose": purpose,
                "status": source["settlement_status"],
                **{
                    name: source[name]
                    for name in ("source_amount_fen", "paid_fen", "other_settled_fen")
                },
                "outstanding_fen": source["remaining_fen"],
                "current_status": current.get("settlement_status") if current else None,
                "current_outstanding_fen": current.get("remaining_fen") if current else None,
                "subject_id": business.get("subject_id"),
                "source_period": period,
                "date": recognition["date"],
                "recognition": recognition,
                "voucher_version_id": voucher["id"] if voucher else None,
                "voucher_number": voucher["number"] if voucher else None,
                **_contribution_identity(
                    snap.store.company_id, business.get("kind"), fact_data, identity["component"]
                ),
            }
        )
    return {"items": result, "page": page}
