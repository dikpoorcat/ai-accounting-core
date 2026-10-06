"""Small owner read projections; accounting decisions remain in the kernel.

These reads expose only explicit missing owner inputs. An unpaid obligation,
missing calculation or an unprocessed source is never a payment instruction.
"""

from __future__ import annotations

import json

from .payroll_confirmation import NO_CHANGE_KIND, PAYROLL_KINDS, PLAN_KINDS

MATERIAL_LABELS = {
    "bank": "银行资料",
    "payroll": "工资资料",
    "transactions": "业务资料",
    "tax": "税务资料",
    "assets": "资产资料",
    "financing": "融资资料",
}


def owner_tasks(snapshot):
    """Return explicit inputs for the selected open month, without full readiness.

    This is deliberately not a claim that the month is complete. Unchecked or
    conflicting sources remain the AI accountant's work. Closed content is not
    re-evaluated into a fresh owner instruction.
    """
    if snapshot.owner_month_state != "open":
        return []
    connection = snapshot.connection
    tasks = []
    for row in connection.execute(
        "SELECT m.* FROM material_revision m WHERE m.period=? AND m.id=("
        "SELECT max(n.id) FROM material_revision n "
        "WHERE n.period=m.period AND n.category=m.category) ORDER BY m.category",
        (snapshot.month,),
    ):
        if row["expected"] <= row["received"]:
            continue
        label = MATERIAL_LABELS.get(row["category"], "业务资料")
        tasks.append(
            {
                "key": f"materials:{row['category']}",
                "title": "补充已约定的资料",
                "object": label,
                "period": snapshot.period,
                "amount_fen": None,
                "amount_status": "not_applicable",
                "deadline": None,
                "impact": f"还缺 {row['expected'] - row['received']} 份已约定资料",
                "next_step": "把缺少的资料交给 AI 会计；已有资料请告知存放位置。",
                "subject_id": None,
            }
        )
    kinds = (*PAYROLL_KINDS, *PLAN_KINDS, NO_CHANGE_KIND)
    identifiers = [
        row[0]
        for row in connection.execute(
            "SELECT f.id FROM fact_revision f INDEXED BY fact_period "
            "JOIN fact_current c ON c.fact_id=f.id JOIN subject s ON s.id=c.subject_id "
            # Keep the month driver and one subject-ID lookup per current fact.
            # A bare IN lets SQLite seek (id,kind) once for every requested kind.
            "WHERE f.period=? AND (s.kind IN(SELECT value FROM json_each(?))) IS TRUE",
            (snapshot.month, json.dumps(kinds)),
        )
    ]
    if not identifiers:
        return tasks
    snapshot.reads.verify_fact_versions(identifiers)
    versions = snapshot.reads.fact_versions(identifiers).values()
    wages, plans, no_change = [], {}, False
    for version in versions:
        fact = version.fact
        if fact.kind in PAYROLL_KINDS:
            wages.append(version)
        elif fact.kind in PLAN_KINDS:
            plans.setdefault(fact.employee_id, []).append(version)
        elif fact.kind == NO_CHANGE_KIND:
            no_change = True
    for version in sorted(wages, key=lambda item: item.subject_id):
        fact = version.fact
        if plans.get(fact.employee_id) or no_change:
            # Existence is not validity. Official preparation/calculation checks
            # still decide whether an evidenced confirmation can be adopted.
            continue
        profile = snapshot.profile("employee", fact.employee_id)
        tasks.append(
            {
                "key": f"payroll_confirmation:{version.subject_id}",
                "title": "确认本月工资",
                "object": profile.get("display_name") or "员工工资",
                "period": snapshot.period,
                "amount_fen": None,
                "amount_status": "unknown",
                "deadline": None,
                "impact": "已登记工资尚缺负责人确认，AI 会计不能正式入账。",
                "next_step": "向 AI 会计确认本月工资方案；确实全员无变化时明确说明。",
                "subject_id": version.subject_id,
            }
        )
    return tasks


def obligation_view(item):
    from .settlement_freeze import obligation_category

    direction = item.get("category")
    if direction not in {"receivable", "payable"}:
        direction = "unknown"
    category = (
        "unknown" if direction == "unknown" else obligation_category(
            direction, item.get("account"), (item.get("source_business") or {}).get("kind")
        )
    )
    return {
        "key": item["key"],
        "name": item["name"],
        "direction": direction,
        "category_key": category,
        "source_period": item.get("source_period"),
        "source_amount_fen": item.get("source_amount_fen"),
        "paid_fen": item.get("paid_fen"),
        "other_settled_fen": item.get("other_settled_fen"),
        "remaining_fen": item.get("remaining_fen"),
        "settlement_status": item["settlement_status"],
    }


def settlement_view(value):
    return {
        "cutoff_period": value["cutoff_period"],
        "status": value["status"],
        "checking": bool(value.get("issues"))
        or value.get("complete") is False
        or value["status"] == "partially_established",
        "obligations": [obligation_view(item) for item in value["obligations"]],
    }


def settlement_event_view(item):
    return {
        "id": item["id"],
        "subject_id": item["settlement_business"]["subject_id"],
        "source_subject_id": item["source_business"]["subject_id"],
        "posting_period": item["posting_period"],
        "direction": item["direction"],
        "signed_amount_fen": item["signed_amount_fen"],
        "relation_state": item["relation_state"],
        "kind": item["settlement_business"]["kind"],
        "name": item["obligation_name"],
        "mode": item["mode"],
    }


def business_profiles(snapshot, value):
    """Locate business objects through the adopted fact's explicit references."""
    from .entity_references import references_from_data

    subject_id = value["identity"]["subject_id"]
    selected = value["frozen_adoption"] or value["current_business_result"]
    if selected is not None:
        calculation = snapshot.calculation(selected["calculation_id"])
        fact = calculation["fact"]
    else:
        from .integrity import verify_sources

        fact_id = value["latest_source"]["id"]
        verify_sources(snapshot.engine, snapshot.connection, fact_ids=(fact_id,))
        fact = snapshot.fact(fact_id)
    references = references_from_data(fact["kind"], fact["data"], registry=snapshot.store.registry)
    ids = sorted({item["entity_id"] for item in references if item["reference_type"] == "entity"})
    kinds = {
        row["id"]: row["kind"]
        for row in snapshot.connection.execute(
            "SELECT id,kind FROM entity WHERE id IN(SELECT value FROM json_each(?))",
            (json.dumps(ids),),
        )
    }
    result = {
        "business": {"entity_id": subject_id, "values": snapshot.profile("business", subject_id)}
    }
    employees = {item["entity_id"] for item in references if item["role"] == "employee"}
    groups = {}
    for ident in ids:
        kind = kinds.get(ident)
        if kind in {"person", "organization"}:
            group, profile_kind = (
                ("employees" if ident in employees else "counterparties"),
                "counterparty",
            )
        elif kind == "fund_account":
            group, profile_kind = "fund_accounts", "fund_account"
        elif kind in {"asset", "project", "fund_product"}:
            group, profile_kind = "assets", "asset"
        else:
            continue
        groups.setdefault(group, set()).add((ident, profile_kind))
    for group, members in groups.items():
        profile_kind = next(iter(members))[1]
        snapshot.metadata.prime_profiles(profile_kind, {ident for ident, _ in members})
        result[group] = []
        for ident, profile_kind in sorted(members):
            profile = dict(snapshot.profile(profile_kind, ident))
            if profile_kind == "counterparty":
                party = snapshot.party_details(ident, exact_identity=True)
                if party.get("source"):
                    profile["display_name"] = party["name"]
                if group == "employees":
                    profile["display_number"] = snapshot.party_code_details(
                        ident, exact_identity=True
                    )[0]
            result[group].append({"entity_id": ident, "values": profile})
    return result


def business_view(value):
    latest, current, frozen = (
        value["latest_source"],
        value["current_business_result"],
        value["frozen_adoption"],
    )
    return {
        "identity": value["identity"],
        "period": value["period"],
        "as_of": value["as_of"],
        "latest_source": {"period": latest["period"], "deleted": latest["deleted"]},
        "closure": value["closure"],
        "current_business_result": None
        if current is None
        else {name: current[name] for name in ("amount_fen", "amount_label", "posting_period")},
        "frozen_adoption": None
        if frozen is None
        else {name: frozen[name] for name in ("amount_fen", "amount_label", "close_period")},
        "review": {"status": value["review"]["status"]},
        "settlements": settlement_view(value["settlements"]),
        "current_followups": {
            "settlements": settlement_view(value["current_followups"]["settlements"])
        },
        "display_profiles": _profile_view(value["display_profiles"]),
    }


def _profile_view(profiles):
    def project(profile):
        return {
            "entity_id": profile["entity_id"],
            "values": {
                name: profile["values"].get(name)
                for name in (
                    "display_name",
                    "display_number",
                    "purpose",
                    "note",
                    "employment_start",
                    "employment_end",
                    "employment_status",
                    "active",
                    "category_label",
                    "rights_description",
                )
            },
        }

    return {
        kind: project(profile) if kind == "business" else [project(item) for item in profile]
        for kind, profile in profiles.items()
    }
