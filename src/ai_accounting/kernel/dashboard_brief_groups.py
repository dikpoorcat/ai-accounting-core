"""Complete activity group summaries and bounded, exact member presentation."""

from __future__ import annotations

from collections import defaultdict

from .business_queries import business_amount_field, business_display_amount
from .contracts import KernelError
from .dashboard_matters import activity_matter
from .dashboard_reads import page_keys
from .dashboard_sort import (
    business_sort_metadata,
    calculation_headers,
    date_object_key,
    fact_sort_scalars,
)
from .types import YearMonth, canonical, checked, digest


def _group_title(kind, data, direction):
    from .dashboard import _name

    if kind in {"funding", "cash_funding", "platform_funding"}:
        return "股东投入" if data["funding_kind"] == "capital" else "借款到账"
    if kind in {"payment", "cash_payment", "platform_payment", "payroll_reserve_payment"}:
        return "收款" if direction == "inflow" else "付款"
    if kind == "advance":
        return "客户预收款" if data["side"] == "customer" else "供应商预付款"
    if kind == "bank_income":
        return {
            "bank_interest": "银行利息入账",
            "government_grant": "补助到账",
            "retained_verification_payment": "确认验证款收入",
            "bank_promotion_reward": "奖励到账",
        }.get(data["income_kind"], _name(kind))
    if kind == "asset_disposal":
        return "出售资产" if data["disposal_kind"] == "sale" else "报废资产"
    return _name(kind)


def _amount_inputs(snap, headers):
    """Prove exact sources, then read the common amount rule's narrow inputs."""
    from .report_open_contribution import verify_published_source_bindings

    identifiers = set(headers)
    fallback = verify_published_source_bindings(snap.reads, identifiers, _sql_outcomes=True)
    if fallback:
        snap.reads.verify_saved_input_identity(fallback)
    snap.reads.verify_sql_outcomes(identifiers)
    cached = snap.reads._verified_source_contents
    result, missing_by_kind = {}, defaultdict(set)
    for ident, header in headers.items():
        if ident in cached:
            result[ident] = cached[ident]
        else:
            missing_by_kind[header["kind"]].add(ident)
    for kind, missing in missing_by_kind.items():
        field = business_amount_field(kind)
        path = "$.values." + field
        for row in snap.connection.execute(
            "SELECT c.id,json_type(c.outcome,'$.values') values_type,"
            "json_type(c.outcome,?) amount_type,json_extract(c.outcome,?) amount,"
            "json_extract(c.outcome,'$.values.direction') direction,"
            "CASE WHEN c.kind='employee_advance' THEN "
            "json_type(c.outcome,'$.values.obligations') END obligation_type,"
            "CASE WHEN c.kind='employee_advance' THEN "
            "json_array_length(c.outcome,'$.values.obligations') END obligation_count,"
            "CASE WHEN c.kind='employee_advance' THEN "
            "json_type(c.outcome,'$.values.obligations[0].amount_fen') END obligation_amount_type,"
            "CASE WHEN c.kind='employee_advance' THEN "
            "json_extract(c.outcome,'$.values.obligations[0].amount_fen') END obligation_amount "
            "FROM json_each(?) ids CROSS JOIN calculation c ON c.id=ids.value",
            (path, path, canonical(sorted(missing))),
        ):
            if row["values_type"] != "object":
                raise KernelError("content_integrity_failed", "业务金额的保存结果格式不匹配")
            values = {"direction": row["direction"]}
            # A missing property falls back to the exact fact; explicit null and
            # non-integers remain unknown, matching business_display_amount.
            if row["amount_type"] is not None:
                values[field] = row["amount"] if row["amount_type"] == "integer" else None
            if kind == "employee_advance":
                if row["obligation_type"] not in (None, "array"):
                    raise KernelError("content_integrity_failed", "代付转债的保存义务格式不匹配")
                values["obligations"] = (
                    [
                        {
                            "amount_fen": (
                                row["obligation_amount"]
                                if row["obligation_amount_type"] == "integer"
                                else None
                            )
                        }
                    ]
                    if row["obligation_count"] == 1
                    else []
                )
            result[row["id"]] = {"values": values}
    return result


def _batch_date_metadata(snap, rows, components):
    """Batch group keys contain a unique part, so no member order uses root parties."""
    by_basis = defaultdict(list)
    for row in rows:
        by_basis[row["basis_calculation_id"]].append(components.get(row["id"]))
    result = {}
    for ident, selections in by_basis.items():
        key = ("activity_payment_date", ident)
        if key in snap.semantic_cache and all(
            parts and all(part["is_batch"] for part in parts) for parts in selections
        ):
            result[ident] = {"date": snap.semantic_cache[key], "party": "", "identities": ()}
    return result


def activity_groups(snap):
    """Reuse the month's money proof, retaining only scalar group membership."""
    cached = getattr(snap, "_brief_activity_groups", None)
    if cached is not None:
        return cached
    journal = snap.month_journal
    rows = journal.authenticated_headers()
    ids = {row["basis_calculation_id"] for row in rows}
    headers = calculation_headers(snap, ids)
    components = snap.activity_components
    outcomes = _amount_inputs(snap, headers)
    metadata = _batch_date_metadata(snap, rows, components)
    displayed_headers = {
        ident: header for ident, header in headers.items() if ident not in metadata
    }
    fact_amounts = fact_sort_scalars(snap, displayed_headers.values())
    metadata.update(business_sort_metadata(
        snap, displayed_headers, headers=displayed_headers, scalars=fact_amounts,
    ))
    classifications, _ = snap.activity_classification
    members, summaries, by_version, sort_keys = {}, {}, {}, {}
    voucher_ids = defaultdict(set)
    ordered = sorted(
        rows,
        key=lambda row: date_object_key(
            metadata[row["basis_calculation_id"]],
            str(YearMonth.from_ordinal(row["period"])),
            row["id"],
        ),
    )
    for row in ordered:
        ident = row["basis_calculation_id"]
        header, full_display = headers[ident], metadata[ident]
        reversal = row["reverses_id"] is not None
        kind = header["kind"]
        values = outcomes[ident]["values"]
        data = fact_amounts.get(header["fact_id"], {})
        parts = components.get(row["id"])
        candidates = parts if parts is not None else [None]
        by_version[row["id"]] = {}
        for part in candidates:
            matter = (None if part else activity_matter(
                kind, data, direction=values.get("direction"),
            ))
            matter_key = part["matter_key"] if part else matter.key if matter else None
            classification = (part["group"] if part else "correction" if reversal
                              else matter.category if matter else classifications[ident, reversal])
            display = ({**full_display, "identities": part["identities"], "party": part["party"]}
                       if part else full_display)
            batch = bool(part and part["is_batch"])
            member_key = part["key"] if part else row["id"]
            key = digest([
                "business-matter-group/1", snap.store.company_id, snap.period,
                matter_key, display["identities"], reversal, values.get("direction"),
                member_key if batch or not display["identities"] or matter_key is None else None,
            ]).hex()
            by_version[row["id"]][member_key] = key
            members.setdefault(key, []).append({
                "row": row, "component": part, "key": member_key,
            })
            if part:
                amount, label = part["amount_fen"], part["amount_label"]
                title = part["title"]
            else:
                amount, label = business_display_amount({
                    "kind": kind, "fact": {"data": fact_amounts[header["fact_id"]]},
                    "outcome": outcomes[ident],
                })
                amount = -amount if reversal and amount is not None else amount
                title = ("冲正·" if reversal else "") + (
                    matter.title if matter else _group_title(kind, data, values.get("direction"))
                )
            if key not in summaries:
                summaries[key] = {
                    "key": key, "group_key": key, "kind": kind, "group": classification,
                    "party": display["party"], "title": title, "is_batch": batch,
                    "member_count": 0, "voucher_count": 0, "amount_fen": 0,
                    "amount_label": label, "state": "更正原业务" if reversal else "已入账",
                    "date_from": None, "date_to": None, "has_month_recognition": False,
                }
                sort_keys[key] = date_object_key(display, snap.period, key)
            group = summaries[key]
            group["member_count"] += 1
            group["amount_fen"] = (
                None if amount is None or group["amount_fen"] is None
                else checked(group["amount_fen"] + amount)
            )
            voucher_ids[key].add(row["id"])
            group["voucher_count"] = len(voucher_ids[key])
            actual = display["date"]
            if actual is None:
                group["has_month_recognition"] = True
            else:
                group["date_from"] = min(actual, group["date_from"] or actual)
                group["date_to"] = max(actual, group["date_to"] or actual)
    summaries = dict(sorted(summaries.items(), key=lambda item: sort_keys[item[0]]))
    snap._brief_activity_groups = summaries, members, by_version
    return snap._brief_activity_groups


def activity_group_page(snap, *, after=None, limit=20):
    summaries, _, _ = activity_groups(snap)
    selected, page = page_keys(summaries, after, limit)
    return {"items": [summaries[key] for key in selected], "page": page}


def activity_group_members(snap, group_key, *, after=None, limit=20):
    _, members, _ = activity_groups(snap)
    if group_key not in members:
        raise KernelError("dashboard_group_not_found", "所选月份没有这组业务，请刷新数据。")
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("每页数量必须为 1 至 500")
    cache = snap.semantic_cache.setdefault("activity_group_member_order", {})
    if group_key not in cache:
        entries = {entry["key"]: entry for entry in members[group_key]}
        keys = list(entries)
        cache[group_key] = (entries, keys, {key: index for index, key in enumerate(keys)})
    entries, keys, positions = cache[group_key]
    if after is not None and after not in positions:
        raise KernelError("dashboard_snapshot_changed", "分页位置已变化，请重新加载明细。")
    start = positions[after] + 1 if after is not None else 0
    selected = keys[start:start + limit]
    more = start + len(selected) < len(keys)
    page = {"total_count": len(keys), "filtered_count": len(keys),
            "returned_count": len(selected), "has_more": more,
            "next_cursor": selected[-1] if more else None}
    originals = {entries[key]["row"]["id"]: entries[key]["row"] for key in selected}
    hydrated = {row["id"]: row for row in snap.month_journal.hydrate(list(originals.values()))}
    from .dashboard_reads import JournalRow

    result = []
    for key in selected:
        entry = entries[key]
        row = JournalRow(snap, dict(hydrated[entry["row"]["id"]]))
        row["activity_component"] = entry["component"]
        result.append(row)
    return result, page



def vouchers_for_rows(snap, rows):
    """One page-local batch, shared by focused, member and voucher pages."""
    from .dashboard import _brief_prime_activity, _brief_prime_voucher_profiles
    from .fund_line_details import fund_line_details
    from .query_reads import _owns_unchanged_read_snapshot

    rows = list({row["id"]: row for row in rows}.values())
    # Page reads do not pass through the overview's whole-month money proof.
    # Prove the full frozen selection before authenticating the displayed lines.
    snap.month_journal._verify_frozen_headers()
    snap.month_journal.authenticated_selected_lines(rows)
    resolutions = _brief_prime_activity(snap, rows)
    _brief_prime_voucher_profiles(snap, rows)
    _prime_selected_voucher_groups(snap, rows)
    # Verify attribution once for the exact page. Physical lines already read
    # by its money proof can be reused, without claiming a complete source proof.
    fund_details = fund_line_details(
        snap.connection, [row["id"] for row in rows],
        _voucher_reads=(snap.reads if _owns_unchanged_read_snapshot(
            snap.reads, snap.connection,
        ) else None),
    )
    vouchers = [snap.owner_voucher(row, fund_details=fund_details) for row in rows]
    own = {
        row["basis"]["subject_id"]
        for row in rows
        if any(
            item.get("source_calculation_id") == row["basis"]["id"]
            and (item.get("source_business") or {}).get("subject_id") == row["basis"]["subject_id"]
            and isinstance(item.get("key"), str)
            and item["key"]
            for item in resolutions[row["basis"]["id"]].get("obligations", ())
        )
    }
    for item in vouchers:
        item["has_business_progress"] |= item["subject_id"] in own
    pending = {item["subject_id"] for item in vouchers if not item["has_business_progress"]}
    progress = (
        snap.queries.business_progress(snap.connection, snap.period, pending) if pending else {}
    )
    for item in vouchers:
        item["has_business_progress"] |= progress.get(item["subject_id"], False)
    return vouchers


def _prime_selected_voucher_groups(snap, rows):
    """Classify only the exact returned vouchers, including older open sources."""
    from .dashboard import ACTUAL_PAYMENT_KINDS, _group, _settlement_source
    from .report_open_contribution import verify_published_source_bindings

    source_ids, slots = set(), {}
    for row in rows:
        calc = row["basis"]
        if calc["kind"] in ACTUAL_PAYMENT_KINDS and row["sign"] > 0:
            values = calc["outcome"]["values"]
            settlements = values.get("settlements", [])
            if not isinstance(settlements, list) or any(
                not isinstance(item, dict)
                or not isinstance(item.get("source_calculation"), str)
                or not item["source_calculation"]
                for item in settlements
            ):
                raise KernelError("content_integrity_failed", "付款核销缺少精确业务来源")
            slots[row["id"]] = settlements
            source_ids.update(item["source_calculation"] for item in settlements)
    fallback = verify_published_source_bindings(snap.reads, source_ids, _sql_outcomes=True)
    if fallback:
        snap.reads.verify_saved_input_identity(fallback)
    snap.reads.verify_sql_outcomes(source_ids)
    for row in rows:
        calc = row["basis"]
        values, settlement_sources = calc["outcome"]["values"], []
        for slot in slots.get(row["id"], ()):
            source = snap.calculation(slot["source_calculation"])
            source_values = source["outcome"]["values"]
            obligations = source_values.get("obligations", [])
            if source["kind"] in {"reimbursed_deposit", "pass_through"}:
                if not isinstance(obligations, list) or any(
                    not isinstance(item, dict) for item in obligations
                ):
                    raise KernelError("content_integrity_failed", "付款业务来源内容不一致")
                names = [
                    item.get("name")
                    for item in obligations
                    if item.get("key") == slot.get("obligation")
                ]
            else:
                names = []
            settlement_sources.extend(
                _settlement_source(
                    source["kind"],
                    creditor_kind=source_values.get("creditor_kind"),
                    obligation_name=name,
                    nature=source_values.get("nature"),
                    payer_kind=source_values.get("payer_kind"),
                )
                for name in names or [None]
            )
        row["_owner_activity_group"] = _group(
            calc["kind"],
            row["sign"] < 0,
            creditor_kind=values.get("creditor_kind"),
            settlement_sources=settlement_sources,
            direction=values.get("direction"),
            nature=values.get("nature"),
            payer_kind=values.get("payer_kind"),
        )


def exact_open_voucher_rows(snap, items):
    """Locate each obligation's adopted voucher at its own posting period."""
    from .query_reads import selected_voucher_sql

    ordered_versions = list(
        dict.fromkeys(
            item["voucher_version_id"] for item in items if item.get("voucher_version_id")
        )
    )
    versions = set(ordered_versions)
    if not versions:
        return []
    found = {}
    for row in snap.connection.execute(
        "SELECT id,period FROM voucher_version WHERE id IN (SELECT value FROM json_each(?))",
        (canonical(sorted(versions)),),
    ):
        found.setdefault(row["period"], set()).add(row["id"])
    result = []
    for period, ids in found.items():
        journal = type(snap.month_journal)(snap, month=period)
        # Retain the independent source-scope proof, then drive the exact
        # voucher identities. An outer ID predicate on a month selector can
        # still scan every unrelated original in that posting month.
        snap.reads.verify_open_voucher_scope(snap.month, posting_period=period)
        query, parameters = selected_voucher_sql(
            snap.period, voucher_ids=ids,
            posting_period=str(YearMonth.from_ordinal(period)),
        )
        selected = list(
            snap.connection.execute(query, parameters)
        )
        if {row["id"] for row in selected} != ids:
            raise KernelError("content_integrity_failed", "款项采用的精确凭证版本不匹配")
        result.extend(journal.hydrate(selected))
    if {row["id"] for row in result} != versions:
        raise KernelError("content_integrity_failed", "款项采用的精确凭证缺失")
    by_version = {row["id"]: row for row in result}
    return [by_version[ident] for ident in ordered_versions]
