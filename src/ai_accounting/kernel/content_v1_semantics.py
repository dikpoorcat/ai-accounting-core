"""Retained read-only v1 semantics used to verify historical correction receipts.

Only scope/dependency declarations and the two opening-correction result checks
belong here. Ordinary business calculation and new writes use the current model.
"""

from __future__ import annotations

import json

from .contracts import KernelError, NeedsInformation
from .history_encoding_v1 import canonical, digest
from .history_types_v1 import BalanceEffect, Line, Outcome, Read

MATERIAL_CATEGORIES = ("transactions", "payroll", "bank", "tax", "assets", "financing")
OPENING_BINDING_KIND = "opening_identity_binding"
OPENING_BASIS_KIND = "opening_basis_correction"


def scopes(fact):
    period = str(fact.period)
    kind = fact.kind
    if kind == "opening_payroll_state":
        return (f"employee:{fact.employee_id}:year:{period[:4]}", f"opening:{fact.package_id}")
    if kind.startswith("opening_") and kind not in {
        "opening_package", "opening_identity_binding", "opening_basis_correction"
    }:
        result = (period, f"opening:{fact.package_id}")
        if kind == "opening_bank":
            return (*result, f"bank:{fact.bank_account_id}")
        if kind == "opening_cash":
            return (*result, f"cash:{fact.cash_account_id}")
        if kind == "opening_asset":
            return (*result, f"asset-source:{fact.asset_id}")
        if kind == "opening_money_fund":
            return (*result, f"money-fund:{fact.fund_id}")
        return result
    if kind == "opening_identity_binding":
        return (*fact.original_scopes, *fact.adopted_scopes,
                "opening-binding:" + fact.source_subject_id)
    return (period,)


def reads(fact):
    kind = fact.kind
    if kind == "opening_identity_binding":
        result = {
            Read("fact", fact.source_kind, "#" + fact.source_fact_id),
            Read("calculation", fact.source_kind, "#" + fact.source_calculation_id),
            Read("calculation", "opening_package", "#" + fact.package_calculation_id),
        }
        if fact.operation == "supersede":
            result.update({
                Read("fact", fact.source_kind, "#" + fact.replacement_source_fact_id),
                Read("calculation", fact.source_kind,
                     "#" + fact.replacement_source_calculation_id),
                Read("calculation", "opening_package",
                     "#" + fact.replacement_package_calculation_id),
            })
        return tuple(sorted(result))
    if kind == "opening_basis_correction":
        return tuple(sorted({
            read for member in fact.members for read in (
                Read("fact", "opening_identity_binding", "#" + member.binding_fact_id),
                Read("calculation", "opening_package",
                     "#" + member.package_calculation_id),
            )
        }))
    if kind.startswith("opening_") and kind != "opening_package":
        result = (Read("calculation", "opening_package", "@" + fact.package_id),)
        if kind == "opening_loan":
            result += (Read("fact", "loan_agreement", "@" + fact.agreement_id),)
        return result
    return ()


def assignment_data(source, assignments):
    data = source.fact.model_dump(mode="json")
    for assignment in assignments:
        path = assignment.path.replace("[", ".").replace("]", "").split(".")
        if path[0] in {"data", "fact"}:
            path.pop(0)
        node = data
        for part in path[:-1]:
            node = node[int(part)] if isinstance(node, list) else node[part]
        if isinstance(node, list):
            node[int(path[-1])] = assignment.entity_id
        else:
            node[path[-1]] = assignment.entity_id
    return type(source.fact).model_validate_json(canonical(data))


def entity_changes(before, after, declarations):
    """Compare released entity-reference paths in an adopted correction."""
    original = {
        item["path"]: item["entity_id"]
        for item in references_from_data(
            before.kind, before.model_dump(mode="json"), declarations
        )
        if item["reference_type"] == "entity"
    }
    destination = {
        item["path"]: item["entity_id"]
        for item in references_from_data(
            after.kind, after.model_dump(mode="json"), declarations
        )
        if item["reference_type"] == "entity"
    }
    return [
        dict(path=path, before=entity, after=destination[path])
        for path, entity in sorted(original.items())
        if path in destination and destination[path] != entity
    ]


def references_from_data(kind, data, declarations):
    """Apply the released paths without consulting current reference rules."""
    def at(value, segments, path=()):
        if not segments:
            if value is not None:
                yield ".".join(path), value
            return
        segment, *rest = segments
        if segment == "*":
            for index, child in enumerate(value or ()):
                yield from at(child, rest, (*path, str(index)))
        elif isinstance(value, dict) and segment in value:
            yield from at(value[segment], rest, (*path, segment))

    return [
        dict(declaration, path=path, entity_id=value)
        for declaration in declarations.get(kind, ())
        for path, value in at(data, declaration["path"].split("."))
    ]


def v1_calculate_opening_basis_correction(version, context):
    lines, effects, adopted = [], [], []
    for member in version.fact.members:
        binding = context.one("opening_identity_binding", "#" + member.binding_fact_id)
        packages = context.calculations("opening_package", "#" + member.package_calculation_id)
        if (
            len(packages) != 1
            or binding.subject_id != member.binding_subject_id
            or binding.fact.package_calculation_id != member.package_calculation_id
        ):
            raise NeedsInformation("members", "期初更正需要每项精确采用绑定")
        if (binding.fact.operation == "supersede") != (member.action == "supersede"):
            raise KernelError("opening_basis_correction", "期初更正动作与采用绑定不一致")
        sources = [
            m for m in packages[0].values["members"] if m["fact_id"] == binding.fact.source_fact_id
        ]
        if len(sources) != 1:
            raise KernelError("opening_basis_correction", "期初更正缺少原清单精确明细")
        sign = -1 if member.action == "supersede" else 1
        for line in sources[0]["opening_lines"]:
            lines.append(
                Line(
                    line["account"],
                    debit=line["credit"] if sign < 0 else line["debit"],
                    credit=line["debit"] if sign < 0 else line["credit"],
                )
            )
        effects.extend(
            BalanceEffect(e["key"], sign * e["amount"], e["category"])
            for e in sources[0]["balances"]
        )
        adopted.append(
            dict(
                binding_fact_id=binding.id,
                package_calculation_id=packages[0].id,
                action=member.action,
            )
        )
    if sum(x.debit for x in lines) != sum(x.credit for x in lines):
        raise NeedsInformation(
            "changes",
            "期初替代差额未平衡，需要明确同次纠正的配对原始明细，不能自动补权益",
            sources=tuple(m.binding_subject_id for m in version.fact.members),
        )
    return Outcome(tuple(lines), {"members": adopted, "obligations": []}, tuple(effects))


def v1_calculate_opening_binding(version, context):
    fact = version.fact
    source = context.one(fact.source_kind, "#" + fact.source_fact_id)
    selected = context.calculations(fact.source_kind, "#" + fact.source_calculation_id)
    packages = context.calculations("opening_package", "#" + fact.package_calculation_id)
    if len(selected) != 1 or len(packages) != 1 or selected[0].fact_id != source.id:
        raise KernelError("opening_binding_source", "期初纠错缺少精确采用来源")
    member = [m for m in packages[0].values["members"] if m["fact_id"] == source.id]
    if len(member) != 1 or canonical(member[0]["values"]) != canonical(selected[0].values):
        raise KernelError("opening_binding_source", "期初明细与原总清单采用不一致")
    corrected = assignment_data(source, fact.assignments)
    before, after = source.fact.model_dump(mode="json"), corrected.model_dump(mode="json")
    values = json.loads(canonical(selected[0].values))
    common = {
        "source_kind": fact.source_kind,
        "source_subject_id": fact.source_subject_id,
        "source_fact_id": source.id,
        "source_calculation_id": selected[0].id,
        "package_calculation_id": packages[0].id,
        "source_opening_lines": member[0]["opening_lines"],
        "source_balances": member[0]["balances"],
    }
    if fact.operation == "supersede":
        target = context.one(fact.source_kind, "#" + fact.replacement_source_fact_id)
        target_calcs = context.calculations(
            fact.source_kind, "#" + fact.replacement_source_calculation_id
        )
        target_packages = context.calculations(
            "opening_package", "#" + fact.replacement_package_calculation_id
        )
        if (
            len(target_calcs) != 1
            or len(target_packages) != 1
            or target_calcs[0].fact_id != target.id
        ):
            raise KernelError("opening_binding_source", "保留期初缺少精确保存依据")
        targets = [m for m in target_packages[0].values["members"] if m["fact_id"] == target.id]
        if len(targets) != 1 or canonical(targets[0]["values"]) != canonical(
            target_calcs[0].values
        ):
            raise KernelError("opening_binding_source", "保留期初与原总清单采用不一致")
        target_fact = assignment_data(target, fact.assignments)
        target_values = json.loads(canonical(target_calcs[0].values))
        for assignment in fact.assignments:
            if "." not in assignment.path and "[" not in assignment.path:
                original = getattr(target.fact, assignment.path)
                target_values[assignment.path] = assignment.entity_id
                for obligation in target_values.get("obligations", ()):
                    if obligation.get("counterparty_id") == original:
                        obligation["counterparty_id"] = assignment.entity_id
        return Outcome(
            (),
            common
            | {
                "superseded": True,
                "replacement_source_subject_id": target.subject_id,
                "replacement_source_calculation_id": target_calcs[0].id,
                "basis_data": target_fact.model_dump(mode="json"),
                "basis_values": target_values,
                "obligations": [],
            },
        )
    effects = []
    if fact.source_kind in {"opening_bank", "opening_cash", "opening_asset"}:
        field = {
            "opening_bank": "bank_account_id",
            "opening_cash": "cash_account_id",
            "opening_asset": "asset_id",
        }[fact.source_kind]
        category = {"opening_bank": "bank", "opening_cash": "cash", "opening_asset": "asset"}[
            fact.source_kind
        ]
        amount = (
            values["cost_fen"] - values["accumulated_fen"]
            if category == "asset"
            else values["opening_fen"]
        )

        def key(value):
            return f"asset:{value}:carrying" if category == "asset" else value

        if before[field] != after[field] and amount:
            effects = [
                BalanceEffect(key(before[field]), -amount, category),
                BalanceEffect(key(after[field]), amount, category),
            ]
    for assignment in fact.assignments:
        if "." not in assignment.path and "[" not in assignment.path:
            original = before[assignment.path]
            values[assignment.path] = assignment.entity_id
            for obligation in values.get("obligations", ()):
                if obligation.get("counterparty_id") == original:
                    obligation["counterparty_id"] = assignment.entity_id
    return Outcome(
        (),
        {
            **common,
            "basis_data": after,
            "basis_values": values,
            "obligations": [],
        },
        tuple(effects),
    )


def v1_current_bindings(connection, rows, *, registry=None):
    from .entity_references import _expected_rows, references_from_data
    # Exact subject/path transitions, never a company-wide identity alias.
    # Before/after facts are immutable authority; the projection itself is not.
    if not rows:
        return rows
    source_ids = {row[0] for row in rows}
    subjects = {
        row["id"]: row["subject_id"]
        for row in connection.execute(
            "SELECT id,subject_id FROM fact_revision WHERE id IN(SELECT value FROM json_each(?))",
            (canonical(list(source_ids)),),
        )
    }
    # Follow only explicitly retained subjects. A later correction of the
    # retained business also applies to the discarded duplicate's provenance;
    # unrelated facts referencing the same object never join this chain.
    changes, visited, pending = [], set(), set(subjects.values())
    while pending:
        selected = list(
            connection.execute(
                "SELECT i.rowid AS sequence,i.*,c.plan,c.digest FROM identity_correction_item i "
                "JOIN identity_correction c ON c.id=i.correction_id "
                "WHERE i.subject_id IN(SELECT value FROM json_each(?)) ORDER BY i.rowid",
                (canonical(sorted(pending)),),
            )
        )
        visited.update(pending)
        pending = set()
        for record in selected:
            plan = json.loads(record["plan"])
            if digest(plan) != record["digest"]:
                raise KernelError("identity_correction_corrupt", "身份纠错依据校验失败")
            matches = [item for item in plan["items"] if item["subject_id"] == record["subject_id"]]
            if len(matches) != 1 or any(
                matches[0].get(key) != record[key]
                for key in ("action", "before_fact_id", "after_fact_id", "replacement_subject_id")
            ):
                raise KernelError("identity_correction_corrupt", "身份纠错范围与保存依据不一致")
            changes.append((record["sequence"], matches[0]))
            if record["replacement_subject_id"] and record["replacement_subject_id"] not in visited:
                pending.add(record["replacement_subject_id"])
    output = list(rows)
    owners = {(row[0], row[1]): [subjects[row[0]]] for row in rows}
    restored_ids = [change["after_fact_id"] for _, change in changes if change.get("reinstated")]
    restored = {}
    for row in _expected_rows(connection, restored_ids, registry=registry) if restored_ids else ():
        restored.setdefault(row[0], {})[row[1]] = row[2]
    opening_after_ids = {
        change["after_fact_id"] for _, change in changes if change["action"] == "opening_binding"
    }
    opening_after = {}
    if opening_after_ids:
        for saved in connection.execute(
            "SELECT c.fact_id,c.outcome,c.digest FROM identity_correction_item i "
            "JOIN calculation c ON c.id=i.calculation_id JOIN calculation_seal s "
            "ON s.calculation_id=c.id WHERE i.after_fact_id IN(SELECT value FROM json_each(?)) "
            "AND c.fact_id=i.after_fact_id",
            (canonical(sorted(opening_after_ids)),),
        ):
            from .stored_json_v1 import loads_unique

            try:
                outcome = loads_unique(saved["outcome"])
            except ValueError as exc:
                raise KernelError(
                    "identity_correction_corrupt", "期初纠错的精确采用结果已损坏"
                ) from exc
            if digest(outcome) != saved["digest"] or saved["fact_id"] in opening_after:
                raise KernelError(
                    "identity_correction_corrupt", "期初纠错的精确采用结果不唯一或已损坏"
                )
            values = outcome["values"]
            opening_after[saved["fact_id"]] = {
                ref["path"]: ref["entity_id"]
                for ref in references_from_data(
                    values["source_kind"], values["basis_data"], registry=registry
                )
                if ref["reference_type"] == "entity"
            }
        if opening_after.keys() != opening_after_ids:
            raise KernelError("identity_correction_corrupt", "期初纠错缺少精确采用结果")
    for _, change in sorted(changes, key=lambda item: item[0]):
        transitions = {item["path"]: item for item in change["entity_changes"]}
        updated = []
        for row in output:
            key = row[0], row[1]
            if change.get("reinstated") and change["subject_id"] in owners[key]:
                owners[key] = owners[key][: owners[key].index(change["subject_id"]) + 1]
                replacement = restored.get(change["after_fact_id"], {}).get(row[1])
                if replacement is not None:
                    row = (row[0], row[1], replacement, *row[3:])
            if owners[key][-1] == change["subject_id"]:
                if change["action"] == "opening_binding":
                    replacement = opening_after[change["after_fact_id"]].get(row[1])
                    if replacement is not None:
                        row = (row[0], row[1], replacement, *row[3:])
                else:
                    transition = transitions.get(row[1])
                    if transition and row[2] == transition["before"]:
                        row = (row[0], row[1], transition["after"], *row[3:])
                if change["replacement_subject_id"]:
                    owners[key].append(change["replacement_subject_id"])
            updated.append(row)
        output = updated
    return output
