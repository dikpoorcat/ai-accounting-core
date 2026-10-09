"""Exact adopted payment components shared by groups and scoped details."""

from __future__ import annotations

import json
from collections import defaultdict

from .contracts import KernelError
from .dashboard_matters import Matter, obligation_matter
from .domains.money import SETTLEMENT_PAYMENT_KINDS
from .query_semantics import resolve_calculation_relations
from .schema import table_name
from .text_sort import pinyin_key
from .types import YearMonth, canonical, checked, digest


def _invalid():
    raise KernelError("content_integrity_failed", "收付款业务分项的精确采用依据不一致")


def adopted_rows(snap):
    rows = snap.month_journal.verified_rows()
    if rows is None:
        query, parameters = snap.month_journal.sql()
        rows = list(snap.connection.execute(query, parameters))
        snap.reads.verify_selected_voucher_adoptions(rows, through_period=snap.month)
    return rows


def _records(snap, identifiers):
    """Read verified results without hydrating facts, evidence or ancestor graphs."""
    identifiers = set(identifiers)
    if not identifiers:
        return {}
    snap.reads.verify_sql_outcomes(identifiers)
    headers = {
        row["id"]: dict(row) for row in snap.connection.execute(
            "SELECT id,subject_id,fact_id,kind,period FROM calculation "
            "WHERE id IN (SELECT value FROM json_each(?))",
            (canonical(sorted(identifiers)),),
        )
    }
    if headers.keys() != identifiers:
        _invalid()
    cached = snap.reads._verified_source_contents
    outcomes = {ident: cached[ident] for ident in identifiers if ident in cached}
    missing = identifiers - outcomes.keys()
    if missing:
        fields = (
            "direction", "amount_fen", "settlements", "tax_transfers", "reserve_expense_fen",
            "creditor_kind", "payer_kind", "nature", "expense_class", "side", "obligations",
            "source_calculation_id", "superseded", "basis_values",
        )
        columns = ",".join(
            f"json_type(outcome,'$.values.{name}') {name}_type,"
            f"json_extract(outcome,'$.values.{name}') {name}" for name in fields
        )
        for row in snap.connection.execute(
            "SELECT id,json_type(outcome,'$.values') values_type," + columns
            + " ,CASE WHEN kind IN (SELECT value FROM json_each(?)) THEN "
            "json_extract(outcome,'$.lines') ELSE '[]' END lines FROM calculation "
            "WHERE id IN (SELECT value FROM json_each(?))",
            (canonical(SETTLEMENT_PAYMENT_KINDS), canonical(sorted(missing))),
        ):
            if row["values_type"] != "object":
                _invalid()
            values = {}
            for name in fields:
                shape = row[name + "_type"]
                if shape is not None:
                    values[name] = (json.loads(row[name]) if shape in {"array", "object"}
                                    else row[name])
            outcomes[row["id"]] = {
                "values": values,
                "lines": json.loads(row["lines"]) if row["lines"] is not None else [],
            }
    result = {}
    for ident, header in headers.items():
        outcome = outcomes[ident]
        if not isinstance(outcome.get("values"), dict):
            _invalid()
        values = outcome["values"]
        if "obligations" in values and (
            not isinstance(values["obligations"], list)
            or any(not isinstance(item, dict) for item in values["obligations"])
        ):
            _invalid()
        if header["kind"] == "opening_identity_binding":
            basis = values.get("basis_values")
            if not isinstance(basis, dict) or not isinstance(basis.get("obligations"), list):
                _invalid()
        result[ident] = {
            **header, "period": str(YearMonth.from_ordinal(header["period"])),
            "fact": {"id": header["fact_id"], "data": {}}, "outcome": outcome,
        }
    return result


def classification_values(snap, identifiers):
    return {ident: item["outcome"]["values"] for ident, item in _records(snap, identifiers).items()}


def payment_components(snap):
    from .dashboard import _settlement_group
    from .report_open_contribution import verify_published_source_bindings

    rows = [row for row in adopted_rows(snap) if row["basis_kind"] in SETTLEMENT_PAYMENT_KINDS]
    identifiers = {row["basis_calculation_id"] for row in rows}
    if not identifiers:
        return {}
    fallback = verify_published_source_bindings(snap.reads, identifiers)
    if fallback:
        snap.reads.verify_saved_input_identity(fallback)
    # This proves the exact root dependency declaration, independently of each
    # referenced source's publication. It never substitutes current sources.
    snap.reads.verify_saved_input_identity(identifiers)
    roots = _records(snap, identifiers)
    snap.reads.prime_parents(identifiers)
    parents = {ident: set(snap.reads.parents(ident)) for ident in identifiers}
    source_ids, binding_ids, tax_ids = set(), set(), set()
    for root in roots.values():
        values = root["outcome"]["values"]
        frozen = values.get("settlements")
        if not isinstance(frozen, list) or any(not isinstance(item, dict) for item in frozen):
            _invalid()
        for item in frozen:
            pointer = item.get("source_calculation")
            if not isinstance(pointer, str) or not pointer:
                _invalid()
            source_ids.add(pointer)
            if item.get("binding_calculation_id") is not None:
                binding_ids.add(item["binding_calculation_id"])
        for item in values.get("tax_transfers", ()):
            if item.get("vat_fen"):
                tax_ids.add(item.get("source_calculation"))
    direct_ids = source_ids | binding_ids | tax_ids
    if any(not isinstance(ident, str) or not ident for ident in direct_ids):
        _invalid()
    fallback = verify_published_source_bindings(snap.reads, source_ids)
    if fallback:
        snap.reads.verify_saved_input_identity(fallback)
    # Bindings may be state results without an independent publication. The
    # saved root dependency proof anchors their exact input identity instead.
    if binding_ids | tax_ids:
        snap.reads.verify_saved_input_identity(binding_ids | tax_ids)
    sources = _records(snap, direct_ids)
    source_scalar_ids = {
        source["fact_id"] for source in sources.values()
        if source["kind"] in {"opening_payroll_payable", "opening_tax"}
    }
    from .dashboard_sort import fact_sort_scalars

    source_scalars = fact_sort_scalars(snap, [
        source for source in sources.values() if source["fact_id"] in source_scalar_ids
    ])
    facts = {}
    by_kind = defaultdict(set)
    for root in roots.values():
        by_kind[root["kind"]].add(root["fact_id"])
    for kind, fact_ids in by_kind.items():
        model = snap.store.registry.models[kind]
        fields = [name for name in ("amount_fen", "counterparty_id", "payment_method", "direction")
                  if name in model.model_fields]
        columns = ",".join(["revision_id", *(f'"{name}"' for name in fields)])
        for row in snap.connection.execute(
            f"SELECT {columns} FROM {table_name(kind)} "
            "WHERE revision_id IN (SELECT value FROM json_each(?))",
            (canonical(sorted(fact_ids)),),
        ):
            facts[row["revision_id"]] = {**dict(row), "allocations": []}
        allocation_fields = ["source_kind", "source_id", "obligation", "amount_fen"]
        from .schema import sequence_model

        child = sequence_model(model.model_fields["allocations"].annotation)
        if "recipient_id" in child.model_fields:
            allocation_fields.append("recipient_id")
        for row in snap.connection.execute(
            "SELECT revision_id,item_no," + ",".join(allocation_fields)
            + f" FROM {table_name(kind)}_allocations "
            "WHERE revision_id IN (SELECT value FROM json_each(?)) ORDER BY revision_id,item_no",
            (canonical(sorted(fact_ids)),),
        ):
            data = facts.get(row["revision_id"])
            if data is None or row["item_no"] != len(data["allocations"]):
                _invalid()
            data["allocations"].append({name: row[name] for name in allocation_fields})
    by_basis = {}
    for ident, root in roots.items():
        data = facts.get(root["fact_id"])
        if data is None:
            _invalid()
        root["fact"]["data"] = data
        values = root["outcome"]["values"]
        frozen = values.get("settlements")
        if (not isinstance(frozen, list) or len(frozen) != len(data["allocations"])
                or not frozen or values.get("direction") not in {"inflow", "outflow"}):
            _invalid()
        relation = resolve_calculation_relations(
            root, load_calculation=lambda key: sources.get(key),
            load_parents=lambda key: parents.get(key, ()), collect_ancestry=False,
        )
        if relation["issues"] or any(
            item["state"] != "resolved" for item in relation["settlements"]
        ):
            _invalid()
        grouped = {}
        total = 0
        for index, movement in enumerate(relation["settlements"]):
            source_id = movement["source_calculation_id"]
            source = sources.get(source_id)
            if source is None or source_id not in parents[ident]:
                _invalid()
            saved = frozen[index]
            binding_id = saved.get("binding_calculation_id")
            if binding_id is not None and binding_id not in parents[ident]:
                _invalid()
            source_values = source["outcome"]["values"]
            original_category = _settlement_group(
                source["kind"], creditor_kind=source_values.get("creditor_kind"),
                obligation_name=movement["obligation_name"], nature=source_values.get("nature"),
                payer_kind=source_values.get("payer_kind"),
            )
            source_component = movement["obligation_name"]
            source_period = source["period"]
            scalar = source_scalars.get(source["fact_id"], {})
            if source["kind"] == "opening_payroll_payable":
                source_period, source_component = scalar["payroll_period"], scalar["component"]
            matter = obligation_matter(
                source["kind"], source_component, values=source_values, data=scalar,
            )
            category = matter.category if matter is not None else original_category
            party = movement.get("recipient_id") or movement.get("creditor_id")
            # Unknown matters or objects never borrow another slot's identity.
            part_identity = (matter.key if matter is not None else None,
                             index if matter is None or not party else None)
            part = grouped.setdefault(part_identity, {
                "matter": matter,
                "source_category": category, "amount_fen": 0, "slots": [],
                "obligation_keys": set(), "source_calculation_ids": set(), "identities": set(),
                "source_periods": set(), "source_components": set(),
            })
            amount = movement["amount_fen"]
            if type(amount) is not int or amount <= 0:
                _invalid()
            total = checked(total + amount)
            part["amount_fen"] = checked(part["amount_fen"] + amount)
            part["slots"].append(index)
            part["obligation_keys"].add(movement["obligation_key"])
            part["source_calculation_ids"].add(source_id)
            part["source_periods"].add(source_period)
            part["source_components"].add(source_component)
            if party:
                part["identities"].add(party)
        if root["kind"] == "payroll_reserve_payment":
            reserve = values.get("reserve_expense_fen")
            if type(reserve) is not int or reserve <= 0:
                _invalid()
            grouped[("reserve-expense", None)] = {
                "matter": Matter("reserve-expense", "备用金费用支出", "expense_supplier"),
                "source_category": "expense_supplier", "amount_fen": reserve, "slots": [],
                "obligation_keys": set(), "source_calculation_ids": set(), "identities": set(),
                "source_periods": set(), "source_components": set(),
            }
            total = checked(total + reserve)
        if total != values.get("amount_fen") or total != data["amount_fen"]:
            _invalid()
        by_basis[ident] = grouped
    party_ids = {party for parts in by_basis.values() for part in parts.values()
                 for party in part["identities"]}
    snap.metadata.prime_profiles("employee", party_ids)
    snap.metadata.prime_profiles("counterparty", party_ids)
    result = {}
    for row in rows:
        ident = row["basis_calculation_id"]
        reversal = row["reverses_id"] is not None
        data = facts[roots[ident]["fact_id"]]
        direction = roots[ident]["outcome"]["values"]["direction"]
        items = []
        for part_identity, raw in by_basis[ident].items():
            category, matter = raw["source_category"], raw["matter"]
            identities = tuple(sorted(raw["identities"]))
            title = (matter.title if matter is not None and matter.key == "reserve-expense"
                     else matter.payment_title(direction) if matter is not None
                     else "收款" if direction == "inflow" else "付款")
            source_periods = tuple(sorted(raw["source_periods"]))
            description = title + (
                "（" + "、".join(source_periods) + "）" if source_periods else ""
            )
            items.append({
                **raw, "key": digest(["activity-part/2", snap.store.company_id, snap.period,
                                     row["id"], part_identity]).hex(),
                "matter_key": matter.key if matter is not None else None,
                "voucher_version_id": row["id"], "basis_calculation_id": ident,
                "group": "correction" if reversal else category,
                "amount_fen": -raw["amount_fen"] if reversal else raw["amount_fen"],
                "amount_label": "实际收付款", "slots": tuple(raw["slots"]),
                "obligation_keys": tuple(sorted(raw["obligation_keys"])),
                "source_calculation_ids": tuple(sorted(raw["source_calculation_ids"])),
                "identities": identities,
                "party": "、".join(sorted((snap.party(p) for p in identities), key=pinyin_key)),
                "title": ("冲正·" if reversal else "") + title,
                "description": ("冲正·" if reversal else "") + description,
                "source_periods": source_periods,
                "source_components": tuple(sorted(raw["source_components"])),
                "is_batch": data.get("payment_method") == "bank_batch"
                or roots[ident]["kind"] == "payroll_reserve_payment",
            })
        result[row["id"]] = items
    return result
