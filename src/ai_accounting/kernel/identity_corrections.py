"""Evidence-backed identity changes, prepared together and published atomically.

This is a business-specific coordinator. It never accepts journal lines and does
not mutate a frozen opening package, a saved calculation, or a close manifest.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from .accounting import compatibility
from .asset_batch_models import MEMBER_KINDS, OWNER_KINDS
from .asset_batches import _AssetPreparation, _checked_members
from .contracts import (
    BalanceEffect,
    Context,
    Fact,
    FactVersion,
    KernelError,
    Line,
    NeedsInformation,
    Outcome,
    Read,
)
from .dependencies import fact_matches, scope_keys
from .engine import Engine
from .types import canonical, digest

IDENTITY_CORRECTION_DDL = """
CREATE TABLE identity_correction(id TEXT PRIMARY KEY,plan TEXT NOT NULL CHECK(json_valid(plan)),
 digest BLOB NOT NULL CHECK(length(digest)=32)) STRICT;
CREATE TABLE identity_correction_item(id TEXT PRIMARY KEY,
 correction_id TEXT NOT NULL REFERENCES identity_correction,
 subject_id TEXT NOT NULL REFERENCES subject,action TEXT NOT NULL
 CHECK(action IN('reassign','supersede','opening_binding')),
 before_fact_id TEXT NOT NULL REFERENCES fact_revision,
 after_fact_id TEXT REFERENCES fact_revision,
 replacement_subject_id TEXT REFERENCES subject,
 calculation_id TEXT REFERENCES calculation,
 UNIQUE(correction_id,subject_id)) STRICT;
CREATE INDEX identity_correction_subject ON identity_correction_item(subject_id,action);
CREATE INDEX identity_correction_calculation ON identity_correction_item(calculation_id);
CREATE TRIGGER identity_correction_immutable BEFORE UPDATE ON identity_correction
 BEGIN SELECT RAISE(ABORT,'immutable identity correction'); END;
CREATE TRIGGER identity_correction_retained BEFORE DELETE ON identity_correction
 BEGIN SELECT RAISE(ABORT,'retained identity correction'); END;
CREATE TRIGGER identity_correction_item_immutable BEFORE UPDATE ON identity_correction_item
 BEGIN SELECT RAISE(ABORT,'immutable identity correction item'); END;
CREATE TRIGGER identity_correction_item_retained BEFORE DELETE ON identity_correction_item
 BEGIN SELECT RAISE(ABORT,'retained identity correction item'); END;
"""


class IdentityChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    subject_id: str = Field(min_length=1, max_length=200)
    expected_revision: int = Field(ge=1)
    action: Literal["reassign", "supersede"]
    data: dict | None = None
    replacement_subject_id: str | None = None


class EntityAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    path: str
    entity_id: str


class EntityResolution(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_entity_id: str
    target_entity_id: str


class OpeningIdentityBinding(Fact):
    kind: ClassVar[str] = "opening_identity_binding"
    registration_command: ClassVar[str] = "confirm_identity_correction"
    identity_fields: ClassVar[tuple[str, ...]] = (
        "source_kind",
        "source_subject_id",
        "source_fact_id",
        "source_calculation_id",
        "package_calculation_id",
        "period",
    )
    source_kind: str
    source_subject_id: str
    source_fact_id: str
    source_calculation_id: str
    package_calculation_id: str
    assignments: tuple[EntityAssignment, ...]
    adopted_scopes: tuple[str, ...]
    original_scopes: tuple[str, ...]
    operation: Literal["reassign", "supersede"] = "reassign"
    replacement_source_fact_id: str | None = None
    replacement_source_calculation_id: str | None = None
    replacement_package_calculation_id: str | None = None

    def scopes(self):
        return (
            *self.original_scopes,
            *self.adopted_scopes,
            "opening-binding:" + self.source_subject_id,
        )

    def reads(self):
        reads = (
            Read("fact", self.source_kind, "#" + self.source_fact_id),
            Read("calculation", self.source_kind, "#" + self.source_calculation_id),
            Read("calculation", "opening_package", "#" + self.package_calculation_id),
        )
        if self.operation == "supersede":
            reads += (
                Read("fact", self.source_kind, "#" + self.replacement_source_fact_id),
                Read("calculation", self.source_kind, "#" + self.replacement_source_calculation_id),
                Read(
                    "calculation", "opening_package", "#" + self.replacement_package_calculation_id
                ),
            )
        return tuple(sorted(set(reads)))


class OpeningBasisMember(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    binding_subject_id: str
    action: Literal["supersede", "restore"]
    binding_fact_id: str
    package_calculation_id: str


class OpeningBasisCorrection(Fact):
    kind: ClassVar[str] = "opening_basis_correction"
    registration_command: ClassVar[str] = "confirm_identity_correction"
    members: tuple[OpeningBasisMember, ...]

    def reads(self):
        return tuple(
            sorted(
                {
                    read
                    for m in self.members
                    for read in (
                        Read("fact", OpeningIdentityBinding.kind, "#" + m.binding_fact_id),
                        Read("calculation", "opening_package", "#" + m.package_calculation_id),
                    )
                }
            )
        )


def calculate_opening_basis_correction(version, context):
    lines, effects, adopted = [], [], []
    for member in version.fact.members:
        binding = context.one(OpeningIdentityBinding.kind, "#" + member.binding_fact_id)
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


def _set_path(data, path, value):
    parts = path.replace("[", ".").replace("]", "").split(".")
    if parts[0] in {"data", "fact"}:
        parts.pop(0)
    node = data
    for part in parts[:-1]:
        node = node[int(part)] if isinstance(node, list) else node[part]
    if isinstance(node, list):
        node[int(parts[-1])] = value
    else:
        node[parts[-1]] = value


def _assignment_data(source, assignments):
    data = source.fact.model_dump(mode="json")
    for item in assignments:
        _set_path(data, item.path, item.entity_id)
    return type(source.fact).model_validate_json(canonical(data))


def _entity_changes(before, after, subject_id):
    from .entity_references import references_for

    original = {
        r["path"]: r["entity_id"]
        for r in references_for(before, subject_id)
        if r["reference_type"] == "entity"
    }
    destination = {
        r["path"]: r["entity_id"]
        for r in references_for(after, subject_id)
        if r["reference_type"] == "entity"
    }
    return [
        dict(path=path, before=entity, after=destination[path])
        for path, entity in sorted(original.items())
        if path in destination and destination[path] != entity
    ]


def calculate_opening_binding(version, context):
    fact = version.fact
    source = context.one(fact.source_kind, "#" + fact.source_fact_id)
    selected = context.calculations(fact.source_kind, "#" + fact.source_calculation_id)
    packages = context.calculations("opening_package", "#" + fact.package_calculation_id)
    if len(selected) != 1 or len(packages) != 1 or selected[0].fact_id != source.id:
        raise KernelError("opening_binding_source", "期初纠错缺少精确采用来源")
    member = [m for m in packages[0].values["members"] if m["fact_id"] == source.id]
    if len(member) != 1 or canonical(member[0]["values"]) != canonical(selected[0].values):
        raise KernelError("opening_binding_source", "期初明细与原总清单采用不一致")
    corrected = _assignment_data(source, fact.assignments)
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
        target_fact = _assignment_data(target, fact.assignments)
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


def register(registry):
    registry.register(OpeningIdentityBinding, calculate_opening_binding)
    registry.register(OpeningBasisCorrection, calculate_opening_basis_correction)


def opening_binding_reads(key):
    return (
        Read("fact", OpeningIdentityBinding.kind, key),
        Read("calculation", OpeningIdentityBinding.kind, key),
    )


def opening_bindings(context, source_kind, key):
    """Resolve current bindings, recording both their declaration and adoption."""
    facts = [
        v
        for v in context.facts(OpeningIdentityBinding.kind, key)
        if v.fact.source_kind == source_kind
    ]
    calculations = context.calculations(OpeningIdentityBinding.kind, key)
    result = []
    for fact in facts:
        matches = [c for c in calculations if c.fact_id == fact.id]
        if len(matches) != 1:
            raise NeedsInformation("opening_identity_binding", "期初身份纠错尚未完整发布")
        result.append((fact, matches[0]))
    return result


def current_opening_binding(connection, source_subject_id):
    row = connection.execute(
        "SELECT c.* FROM calculation_current h JOIN calculation c ON c.id=h.calculation_id "
        "WHERE h.subject_id=?",
        ("opening-identity:" + source_subject_id,),
    ).fetchone()
    if row is None:
        return None
    outcome = json.loads(row["outcome"])
    if (
        digest(outcome) != row["digest"]
        or outcome["values"].get("source_subject_id") != source_subject_id
    ):
        raise KernelError("content_integrity_failed", "期初身份采用绑定损坏")
    return {"calculation_id": row["id"], "fact_id": row["fact_id"], **outcome["values"]}


def current_opening_bindings(connection, source_calculation_ids=None):
    """Exact original calculation -> current adopted identity; never used by frozen reads."""
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fact_opening_identity_binding'"
    ).fetchone():
        return {}
    restriction = ""
    parameters = ()
    if source_calculation_ids is not None:
        restriction = " AND b.source_calculation_id IN (SELECT value FROM json_each(?))"
        parameters = (canonical(sorted(source_calculation_ids)),)
    result = {}
    for row in connection.execute(
        "SELECT c.*,b.source_calculation_id FROM calculation_current h "
        "JOIN calculation c ON c.id=h.calculation_id JOIN fact_opening_identity_binding b "
        "ON b.revision_id=c.fact_id WHERE c.kind='opening_identity_binding'" + restriction,
        parameters,
    ):
        outcome = json.loads(row["outcome"])
        if (
            digest(outcome) != row["digest"]
            or outcome["values"].get("source_calculation_id") != row["source_calculation_id"]
        ):
            raise KernelError("content_integrity_failed", "期初身份采用绑定损坏")
        result[row["source_calculation_id"]] = {
            "calculation_id": row["id"],
            "fact_id": row["fact_id"],
            **outcome["values"],
        }
    return result


def superseded_subject_ids(connection):
    return {
        r[0]
        for r in connection.execute(
            "SELECT i.subject_id FROM identity_correction_item i WHERE i.action='supersede' "
            "AND i.rowid=(SELECT max(j.rowid) FROM identity_correction_item j "
            "WHERE j.subject_id=i.subject_id)"
        )
    }


class _OverlayStore:
    def __init__(self, store, changes, removed):
        self.base, self.changes, self.removed = store, changes, removed

    def __getattr__(self, key):
        return getattr(self.base, key)

    def current_fact(self, connection, subject_id):
        return self.changes.get(subject_id) or self.base.current_fact(connection, subject_id)

    def select_many(self, connection, reads, **kwargs):
        selected = self.base.select_many(connection, reads, **kwargs)
        for read in selected:
            if read.key.startswith("#"):
                if read.source == "fact":
                    replacements = [
                        v
                        for v in self.changes.values()
                        if v.id == read.key[1:] and (read.kind == "*" or read.kind == v.fact.kind)
                    ]
                    if replacements:
                        selected[read] = tuple(replacements)
                continue
            values = [
                v
                for v in selected[read]
                if v.subject_id not in self.removed
                and (read.source != "fact" or v.subject_id not in self.changes)
            ]
            if read.source == "fact":
                values.extend(
                    v
                    for v in self.changes.values()
                    if v.subject_id not in self.removed and fact_matches(read, v)
                )
            selected[read] = tuple(sorted(values, key=lambda v: v.subject_id))
        return selected


class _CorrectionPreparation(_AssetPreparation):
    _allows_asset_graph = True

    def __init__(self, engine, changes, removed, correction_id, previous):
        Engine.__init__(self, _OverlayStore(engine.store, changes, removed))
        self.removed, self.correction_id, self.previous = removed, correction_id, previous
        self.outcomes = {}

    def _explicit_roots(self, subjects):
        return set()

    def _selection_enabled(self, subject_id):
        return subject_id not in self.removed

    def _reads(self, version):
        if version.subject_id in self.removed:
            previous = self.previous.get(version.subject_id)
            return (Read("calculation", version.fact.kind, "#" + previous),) if previous else ()
        return super()._reads(version)

    def _snapshot(self, subjects):
        snapshot = Engine._snapshot(self, subjects)
        recovery_subjects = {
            value.subject_id
            for selection in snapshot[2].values()
            for value in selection
            if isinstance(value, FactVersion) and value.fact.kind == "overpayment"
        }
        if recovery_subjects - snapshot[1].keys():
            # A confirmed recovery right is a calculable accounting result, not
            # merely permission to let the historical payment exceed the debt.
            snapshot = Engine._snapshot(self, set(subjects) | recovery_subjects)
        for sid in tuple(snapshot[1]):
            if sid in self.removed and snapshot[1][sid].fact.kind in MEMBER_KINDS:
                del snapshot[1][sid]
        ids = {
            c.id
            for values in snapshot[2].values()
            for c in values
            if not isinstance(c, FactVersion)
        }
        with self.store.connection(read_only=True) as connection:
            for row in connection.execute(
                "SELECT subject_id,outcome FROM calculation "
                "WHERE id IN (SELECT value FROM json_each(?))",
                (canonical(sorted(ids)),),
            ):
                self.outcomes[row["subject_id"]] = json.loads(row["outcome"])
        return snapshot

    def _evaluate(self, version, context):
        if version.subject_id in self.removed:
            for read in self._reads(version):
                context.select(read)
            return asdict(
                Outcome(
                    (),
                    {
                        "identity_correction": self.correction_id,
                        "superseded": True,
                        "obligations": [],
                    },
                )
            )
        return super()._evaluate(version, context)


class IdentityCorrections:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    def _payroll_retention(self, connection, source, target, replacement, changes):
        """Choose the surviving business ID, never its financial facts."""
        from .domains.payroll import PAYROLL_KINDS

        if source.fact.kind not in PAYROLL_KINDS:
            return None
        if source.fact.period != target.period:
            raise KernelError("identity_payroll_period", "工资替代必须明确同一所属月的完整事实")
        candidates = []
        for row in connection.execute(
            "SELECT r.id FROM fact_current h JOIN fact_revision r ON r.id=h.fact_id "
            "JOIN subject s ON s.id=r.subject_id "
            "WHERE s.kind IN ('payroll','payroll_bounded') AND r.period=? ORDER BY s.id",
            (source.fact.period.ordinal,),
        ):
            version = self.store.fact(connection, row[0])
            if (
                version.fact.employee_id in {source.fact.employee_id, target.employee_id}
                or version.subject_id == replacement.subject_id
            ):
                candidates.append(
                    dict(
                        subject_id=version.subject_id,
                        fact_id=version.id,
                        employee_id=version.fact.employee_id,
                        kind=version.fact.kind,
                    )
                )
        existing = [c for c in candidates if c["employee_id"] == target.employee_id]
        chosen = min(existing or candidates, key=lambda c: c["subject_id"])["subject_id"]
        resolution = dict(
            period=str(source.fact.period),
            target_employee_id=target.employee_id,
            candidates=candidates,
            retained_subject_id=chosen,
            selection="target_existing_then_subject_id" if existing else "subject_id",
            amounts_from="explicit_complete_fact",
        )
        if replacement.subject_id != chosen:
            raise KernelError(
                "identity_payroll_retention",
                "同月工资优先保留目标对象已有业务；同优先级按业务编号稳定选择",
                payroll_resolution=resolution,
            )
        if not any(c.subject_id == chosen and c.data is not None for c in changes):
            raise NeedsInformation(
                "changes.data",
                "须明确保留工资的完整正确事实，编号选择不决定金额",
                sources=(chosen,),
            )
        return resolution

    def _prepare(self, *, changes, evidence, reason, posting_period=None, entity_resolution=None):
        from .entity_references import references_for, validate_entity_references

        if not changes or not evidence or not isinstance(reason, str) or not reason.strip():
            raise NeedsInformation("identity_correction", "需要明确变更、纠错依据与原因")
        typed = [IdentityChange.model_validate(item) for item in changes]
        if len({i.subject_id for i in typed}) != len(typed):
            raise KernelError("duplicate_subject", "纠错不能重复指定同一业务")
        evidence = tuple(sorted(set(evidence)))
        proposal = {
            "changes": [i.model_dump(mode="json") for i in typed],
            "evidence": list(evidence),
            "reason": reason,
            "posting_period": posting_period,
            "entity_resolution": entity_resolution,
        }
        versions, removed, old, previous, items = {}, set(), {}, {}, []
        reinstated = set()
        basis_members = []
        restored_asset_owners = {}
        roots = set()
        with self.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            epochs = self.store.epochs(connection)
            from .read_state import repair_revision

            read_revision = repair_revision(connection)
            for key in evidence:
                if not connection.execute(
                    "SELECT 1 FROM evidence WHERE digest=?", (bytes.fromhex(key),)
                ).fetchone():
                    raise NeedsInformation("evidence", "纠错依据尚未登记", sources=(key,))
            for change in typed:
                retired = change.subject_id in superseded_subject_ids(connection)
                if retired and change.action == "reassign":
                    latest = connection.execute(
                        "SELECT id FROM fact_revision WHERE subject_id=? "
                        "ORDER BY revision DESC LIMIT 1",
                        (change.subject_id,),
                    ).fetchone()
                    if not latest:
                        raise KernelError("unknown_fact", "待恢复的业务原事实不存在")
                    source = self.store.fact(connection, latest[0])
                    reinstated.add(change.subject_id)
                else:
                    source = self.store.current_fact(connection, change.subject_id)
                if change.expected_revision != source.revision:
                    raise KernelError("fact_version_conflict", "待纠错事实版本已变化")
                old[change.subject_id] = source
                if change.subject_id in reinstated and source.fact.kind == "asset_activation":
                    owners = {
                        r[0]
                        for r in connection.execute(
                            "SELECT o.subject_id FROM asset_batch_member m JOIN calculation o "
                            "ON o.id=m.owner_calculation_id WHERE m.member_fact_id=?",
                            (source.id,),
                        )
                    }
                    if len(owners) != 1:
                        raise KernelError(
                            "asset_member_owned", "恢复启用卡片需要唯一的原汇总拥有者"
                        )
                    owner_id = next(iter(owners))
                    restored_asset_owners.setdefault(owner_id, []).append(change.subject_id)
                    roots.add(owner_id)
                row = connection.execute(
                    "SELECT calculation_id FROM calculation_current WHERE subject_id=?",
                    (change.subject_id,),
                ).fetchone()
                previous[change.subject_id] = row[0] if row else None
            correction_id = (
                "ic_"
                + digest(
                    [
                        self.store.database_id,
                        proposal,
                        epochs,
                        sorted((k, v.id) for k, v in old.items()),
                    ]
                ).hex()
            )
            for change in typed:
                source = old[change.subject_id]
                item = {
                    "subject_id": change.subject_id,
                    "action": change.action,
                    "before_fact_id": source.id,
                    "after_fact_id": None,
                    "replacement_subject_id": change.replacement_subject_id,
                }
                if change.subject_id in reinstated:
                    item["reinstated"] = True
                if change.action == "supersede":
                    if getattr(source.fact, "actual_payment", False):
                        raise KernelError(
                            "identity_actual_funds",
                            "真实资金流水不能因身份纠错撤去，须明确重新分配或追回",
                        )
                    if (
                        change.data is not None
                        or not change.replacement_subject_id
                        or change.replacement_subject_id == change.subject_id
                    ):
                        raise KernelError(
                            "identity_supersession",
                            "替代必须指定另一笔明确保留的业务，不能修改原事实金额",
                        )
                    replacement = old.get(change.replacement_subject_id) or self.store.current_fact(
                        connection, change.replacement_subject_id
                    )
                    item["replacement_fact_id"] = replacement.id
                    if replacement.fact.kind != source.fact.kind:
                        raise KernelError("identity_supersession", "替代业务类型必须相同")
                    if any(
                        c.subject_id == change.replacement_subject_id and c.action == "supersede"
                        for c in typed
                    ):
                        raise KernelError("identity_supersession", "保留业务不能同时被替代")
                    replacement_change = next(
                        (c for c in typed if c.subject_id == replacement.subject_id), None
                    )
                    target = (
                        type(replacement.fact).model_validate_json(
                            canonical(replacement_change.data)
                        )
                        if replacement_change and replacement_change.data
                        else replacement.fact
                    )
                    payroll_retention = self._payroll_retention(
                        connection, source, target, replacement, typed
                    )
                    if payroll_retention is not None:
                        item["payroll_resolution"] = payroll_retention
                    item["entity_changes"] = _entity_changes(source.fact, target, source.subject_id)
                    if source.fact.kind.startswith("opening_"):
                        active_binding = current_opening_binding(connection, source.subject_id)
                        if active_binding and active_binding.get("superseded"):
                            raise KernelError(
                                "opening_identity_conflict", "该期初已停止采用，不能重复取代"
                            )
                        if active_binding:
                            item["entity_changes"] = _entity_changes(
                                type(source.fact).model_validate_json(
                                    canonical(active_binding["basis_data"])
                                ),
                                target,
                                source.subject_id,
                            )
                        target_binding = current_opening_binding(connection, replacement.subject_id)
                        if target_binding and target_binding.get("superseded"):
                            raise KernelError(
                                "opening_identity_conflict",
                                "保留期初已停止采用，须明确选择最终有效的保留基准",
                            )
                        target_assignments = ()
                        if target_binding:
                            item["retained_binding_fact_id"] = target_binding["fact_id"]
                            target = type(replacement.fact).model_validate_json(
                                canonical(target_binding["basis_data"])
                            )
                            target_assignments = self.store.fact(
                                connection, target_binding["fact_id"]
                            ).fact.assignments
                            item["entity_changes"] = _entity_changes(
                                type(source.fact).model_validate_json(
                                    canonical(active_binding["basis_data"])
                                )
                                if active_binding
                                else source.fact,
                                target,
                                source.subject_id,
                            )
                        version = self._opening_version(
                            connection,
                            source,
                            target,
                            target_assignments,
                            evidence,
                            replacement=replacement,
                        )
                        versions[version.subject_id] = version
                        item.update(action="opening_binding", after_fact_id=version.id)
                        basis_members.append(
                            OpeningBasisMember(
                                binding_subject_id=version.subject_id,
                                binding_fact_id=version.id,
                                package_calculation_id=version.fact.package_calculation_id,
                                action="supersede",
                            )
                        )
                        fact = target
                    else:
                        removed.add(change.subject_id)
                        versions[change.subject_id] = source
                else:
                    if change.data is None or change.replacement_subject_id is not None:
                        raise KernelError(
                            "identity_correction_shape", "身份重指须提供完整的已确认事实"
                        )
                    fact = type(source.fact).model_validate_json(canonical(change.data))
                    validate_entity_references(connection, fact, change.subject_id)
                    refs = [
                        r
                        for r in references_for(source.fact, change.subject_id)
                        if r["reference_type"] == "entity"
                    ]
                    newrefs = {
                        r["path"]: r
                        for r in references_for(fact, change.subject_id)
                        if r["reference_type"] == "entity"
                    }
                    assignments = [
                        EntityAssignment(path=r["path"], entity_id=newrefs[r["path"]]["entity_id"])
                        for r in refs
                        if r["path"] in newrefs
                        and r["entity_id"] != newrefs[r["path"]]["entity_id"]
                    ]
                    replacement_targets = {
                        i.replacement_subject_id for i in typed if i.action == "supersede"
                    }
                    replacements = {
                        i.subject_id: i.replacement_subject_id
                        for i in typed
                        if i.action == "supersede"
                    }
                    old_business = {
                        r["path"]: r["entity_id"]
                        for r in references_for(source.fact, change.subject_id)
                        if r["reference_type"] == "business"
                    }
                    new_business = {
                        r["path"]: r["entity_id"]
                        for r in references_for(fact, change.subject_id)
                        if r["reference_type"] == "business"
                    }
                    redirected = any(
                        replacements.get(value) == new_business.get(path)
                        and value != new_business.get(path)
                        for path, value in old_business.items()
                    )
                    adopted = (
                        current_opening_binding(connection, source.subject_id)
                        if source.fact.kind.startswith("opening_")
                        else None
                    )
                    changed_identity = (
                        canonical(adopted["basis_data"]) != canonical(fact.model_dump(mode="json"))
                        if adopted
                        else bool(assignments)
                    )
                    if (
                        not changed_identity
                        and change.subject_id not in replacement_targets
                        and change.subject_id not in reinstated
                        and not redirected
                    ):
                        raise KernelError(
                            "identity_correction_no_change", "该项没有明确的对象身份变更"
                        )
                    forbidden = [
                        name
                        for name in source.fact.identity_fields
                        if name not in {a.path for a in assignments}
                        and getattr(source.fact, name) != getattr(fact, name)
                    ]
                    if forbidden:
                        raise KernelError(
                            "identity_mismatch",
                            "身份纠错不能改变业务类型、所属期等稳定业务字段",
                            fields=forbidden,
                        )
                    if getattr(source.fact, "actual_payment", False):
                        for field in ("period", "actual_date", "amount_fen", "direction"):
                            if getattr(source.fact, field, None) != getattr(fact, field, None):
                                raise KernelError(
                                    "identity_actual_funds", "身份纠错保留真实金额、日期和收付方向"
                                )
                    elif (
                        source.fact.immutable or source.fact.kind == "payroll_withholding_actual"
                    ) and _assignment_data(source, assignments) != fact:
                        raise KernelError(
                            "identity_observed_fact",
                            "身份纠错保留原开票、实际扣税和真实业务金额日期",
                        )
                    is_opening = source.fact.kind.startswith("opening_")
                    frozen_opening = is_opening and (
                        adopted is not None
                        or connection.execute(
                            "SELECT 1 FROM period_close WHERE period>=? LIMIT 1",
                            (source.fact.period.ordinal,),
                        ).fetchone()
                        is not None
                    )
                    if is_opening and _assignment_data(source, assignments) != fact:
                        raise KernelError(
                            "closed_opening_immutable", "期初身份纠错不能更改任何经济字段"
                        )
                    if frozen_opening:
                        version = self._opening_version(
                            connection, source, fact, assignments, evidence
                        )
                        item["action"] = "opening_binding"
                        if adopted and adopted.get("superseded"):
                            basis_members.append(
                                OpeningBasisMember(
                                    binding_subject_id=version.subject_id,
                                    binding_fact_id=version.id,
                                    package_calculation_id=version.fact.package_calculation_id,
                                    action="restore",
                                )
                            )
                            item["reinstated"] = True
                    else:
                        if is_opening:
                            package = self.store.current_fact(connection, source.fact.package_id)
                            roots.add(package.subject_id)
                            roots.update(member.subject_id for member in package.fact.members)
                        ident = (
                            "f_ic_" + digest([correction_id, change.subject_id, change.data]).hex()
                        )
                        version = FactVersion(
                            ident, change.subject_id, source.revision + 1, fact, evidence
                        )
                    versions[version.subject_id] = version
                    item["after_fact_id"] = version.id
                    before_binding = (
                        type(source.fact).model_validate_json(canonical(adopted["basis_data"]))
                        if adopted
                        else source.fact
                    )
                    item["entity_changes"] = _entity_changes(
                        before_binding, fact, source.subject_id
                    )
                items.append(item)
                for candidate in (source, versions.get(change.subject_id)):
                    if candidate is None:
                        continue
                    scopes = scope_keys("fact", candidate.fact, candidate.subject_id)
                    roots |= self.engine._scope_consumers(
                        connection,
                        "fact",
                        candidate.fact.kind,
                        scopes,
                        candidate.fact.period.ordinal,
                    )
                    roots |= self.engine._scope_consumers(
                        connection,
                        "calculation",
                        candidate.fact.kind,
                        scopes,
                        candidate.fact.period.ordinal,
                    )
                if item["action"] == "opening_binding":
                    target_scopes = scope_keys("fact", fact, source.subject_id)
                    roots |= self.engine._scope_consumers(
                        connection,
                        "fact",
                        source.fact.kind,
                        target_scopes,
                        source.fact.period.ordinal,
                    )
                    roots |= self.engine._scope_consumers(
                        connection,
                        "calculation",
                        source.fact.kind,
                        target_scopes,
                        source.fact.period.ordinal,
                    )
                roots.add(change.subject_id)
            if basis_members:
                sid = "opening-correction:" + correction_id
                owner_fact = OpeningBasisCorrection(
                    period=versions[basis_members[0].binding_subject_id].fact.period,
                    members=tuple(basis_members),
                )
                versions[sid] = FactVersion(
                    "f_ob_" + digest([correction_id, owner_fact.model_dump(mode="json")]).hex(),
                    sid,
                    1,
                    owner_fact,
                    evidence,
                )
            roots |= set(versions)
            roots = self.engine._descendants(connection, roots)
            for sid in sorted(roots):
                candidate = versions.get(sid) or self.store.current_fact(connection, sid)
                if candidate.fact.kind != "asset_activation_batch":
                    continue
                retained = [m for m in candidate.fact.members if m.subject_id not in removed]
                restore_members = restored_asset_owners.get(sid, [])
                if len(retained) == len(candidate.fact.members) and not restore_members:
                    continue
                owner_data = candidate.fact.model_dump(mode="json") | {
                    "members": [m.model_dump(mode="json") for m in retained]
                    + [{"subject_id": mid} for mid in restore_members]
                }
                owner_fact = type(candidate.fact).model_validate_json(canonical(owner_data))
                versions[sid] = FactVersion(
                    "f_ic_" + digest([correction_id, sid, owner_data]).hex(),
                    sid,
                    candidate.revision + 1,
                    owner_fact,
                    evidence,
                )
            roots = {
                sid
                for sid in roots
                if (versions.get(sid) or self.store.current_fact(connection, sid)).fact.kind
                in self.store.registry.evaluators
                and not (sid in removed and versions[sid].fact.kind in MEMBER_KINDS)
            }
            if any(i["action"] == "opening_binding" for i in items):
                roots = {
                    sid
                    for sid in roots
                    if not (
                        versions.get(sid) or self.store.current_fact(connection, sid)
                    ).fact.kind.startswith("opening_")
                    or (versions.get(sid) or self.store.current_fact(connection, sid)).fact.kind
                    in {OpeningIdentityBinding.kind, OpeningBasisCorrection.kind}
                }
        prep = _CorrectionPreparation(self.engine, versions, removed, correction_id, previous)
        from .entities import validate_resolution

        with self.store.connection(read_only=True) as connection:
            validate_resolution(connection, items, entity_resolution)
        if roots:
            public, prepared = prep._prepare(sorted(roots), posting_period)
            if public["epochs"] != epochs:
                raise KernelError("preview_expired", "纠错范围读取期间事实已变化")
        else:
            public, prepared = (
                {
                    "subjects": [],
                    "epochs": epochs,
                    "posting_period": posting_period,
                    "results": [],
                    "company_id": self.store.company_id,
                    "database_id": self.store.database_id,
                    "checked_lanes": ["accounting", "management", "material"],
                },
                [],
            )
        public.update(
            read_repair_revision=read_revision,
            correction_id=correction_id,
            proposal=proposal,
            fact_changes=[
                {
                    "subject_id": v.subject_id,
                    "fact_id": v.id,
                    "revision": v.revision,
                    "kind": v.fact.kind,
                    "data": v.fact.model_dump(mode="json"),
                    "evidence": list(v.evidence),
                }
                for v in versions.values()
                if v.id not in {x.id for x in old.values()}
            ],
            items=items,
        )
        public["digest"] = digest({k: v for k, v in public.items() if k != "digest"}).hex()
        return public, prepared, versions

    def _opening_version(
        self, connection, source, fact, assignments, evidence, *, replacement=None
    ):
        row = connection.execute(
            "SELECT c.id FROM calculation c JOIN calculation_current h "
            "ON h.calculation_id=c.id WHERE c.fact_id=?",
            (source.id,),
        ).fetchone()
        if not row:
            raise KernelError("opening_binding_source", "期初来源尚未正式采用")
        package = connection.execute(
            "SELECT c.id FROM dependency_calculation d JOIN calculation c ON c.id=d.upstream_id "
            "WHERE d.calculation_id=? AND c.kind='opening_package'",
            (row[0],),
        ).fetchone()
        if not package:
            raise KernelError("opening_binding_source", "期初缺少精确总清单")
        sid = "opening-identity:" + source.subject_id
        head = connection.execute(
            "SELECT fact_id FROM fact_current WHERE subject_id=?", (sid,)
        ).fetchone()
        previous = self.store.fact(connection, head[0]) if head else None
        replacement_fields = {}
        if replacement is not None:
            target = connection.execute(
                "SELECT c.id FROM calculation_current h JOIN calculation c "
                "ON c.id=h.calculation_id WHERE c.fact_id=?",
                (replacement.id,),
            ).fetchone()
            target_package = (
                connection.execute(
                    "SELECT c.id FROM dependency_calculation d "
                    "JOIN calculation c ON c.id=d.upstream_id "
                    "WHERE d.calculation_id=? AND c.kind='opening_package'",
                    (target[0],),
                ).fetchone()
                if target
                else None
            )
            if not target_package:
                raise NeedsInformation(
                    "replacement_subject_id", "所保留期初尚未被完整总清单正式采用"
                )
            replacement_fields = dict(
                operation="supersede",
                replacement_source_fact_id=replacement.id,
                replacement_source_calculation_id=target[0],
                replacement_package_calculation_id=target_package[0],
            )
        binding = OpeningIdentityBinding(
            period=source.fact.period,
            source_kind=source.fact.kind,
            source_subject_id=source.subject_id,
            source_fact_id=source.id,
            source_calculation_id=row[0],
            package_calculation_id=package[0],
            assignments=tuple(assignments),
            adopted_scopes=tuple(sorted(fact.scopes_for(source.subject_id))),
            original_scopes=tuple(sorted(source.fact.scopes_for(source.subject_id))),
            **replacement_fields,
        )
        revision = previous.revision + 1 if previous else 1
        return FactVersion(
            "f_ib_" + digest([sid, revision, binding.model_dump(mode="json"), evidence]).hex(),
            sid,
            revision,
            binding,
            evidence,
        )

    def preview_identity_correction(
        self,
        *,
        changes: list[IdentityChange],
        evidence: list[str],
        reason: str,
        posting_period: str | None = None,
        entity_resolution: EntityResolution | None = None,
    ):
        kwargs = self._arguments(changes, evidence, reason, posting_period, entity_resolution)
        public, _, _ = self._prepare(**kwargs)
        return {"status": "preview", **public}

    @staticmethod
    def _arguments(changes, evidence, reason, posting_period, entity_resolution):
        return dict(
            changes=[IdentityChange.model_validate(c).model_dump(mode="json") for c in changes],
            evidence=evidence,
            reason=reason,
            posting_period=posting_period,
            entity_resolution=(
                EntityResolution.model_validate(entity_resolution).model_dump(mode="json")
                if entity_resolution is not None
                else None
            ),
        )

    def confirm_identity_correction(
        self,
        *,
        changes: list[IdentityChange],
        evidence: list[str],
        reason: str,
        preview_digest: str,
        epochs: dict,
        request_id: str,
        posting_period: str | None = None,
        entity_resolution: EntityResolution | None = None,
    ):
        kwargs = self._arguments(changes, evidence, reason, posting_period, entity_resolution)
        request_hash = digest(["identity_correction", kwargs, preview_digest, epochs])
        cached = self.engine._cached(request_id, request_hash)
        if cached is not None:
            return cached
        public, prepared, versions = self._prepare(**kwargs)
        if public["digest"] != preview_digest or public["epochs"] != epochs:
            raise KernelError("preview_expired", "纠错依据、事实或影响范围已变化")

        def operation(connection):
            from .entities import apply_resolution
            from .integrity import verify_prepared_sources, verify_publication
            from .projections import prepare_projection_check, verify_projection_change

            # BEGIN IMMEDIATE is already held. All read connections below see the
            # same committed source snapshot; no competing writer can advance it.
            public, prepared, versions = self._prepare(**kwargs)
            if public["digest"] != preview_digest or public["epochs"] != epochs:
                raise KernelError("preview_expired", "锁内复核的纠错范围或读取修复版本已变化")
            from .read_state import check_repair_revision

            check_repair_revision(connection, public["read_repair_revision"])

            for item in prepared:
                if item.compatibility_issue:
                    raise compatibility(item.calculation_id, "identity_comparison_unavailable")
            new = {
                v.id
                for v in versions.values()
                if not connection.execute(
                    "SELECT 1 FROM fact_revision WHERE id=?", (v.id,)
                ).fetchone()
            }
            verify_prepared_sources(self.engine, connection, prepared, new_fact_ids=new)
            self.engine._check_publication_projections(connection, prepared)
            projection = prepare_projection_check(
                connection, prepared, posting_period=kwargs.get("posting_period")
            )
            for version in versions.values():
                if version.id in new:
                    self.store.write_fact(
                        connection, version, digest(version.fact.model_dump(mode="json"))
                    )
            for item in prepared:
                self.engine._record_prepared(connection, item)
            for item in prepared:
                if (
                    item.version.fact.kind not in OWNER_KINDS
                    or connection.execute(
                        "SELECT 1 FROM calculation_seal WHERE calculation_id=?",
                        (item.calculation_id,),
                    ).fetchone()
                ):
                    continue
                for member in item.outcome["values"]["members"]:
                    connection.execute(
                        "INSERT INTO asset_batch_member VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            item.calculation_id,
                            member["position"],
                            member["asset_id"],
                            member["member_subject_id"],
                            member["member_fact_id"],
                            member["member_calculation_id"],
                            bytes.fromhex(member["result_digest"]),
                            canonical(member["summary"]),
                            member["line_start"],
                            member["line_count"],
                        ),
                    )
                _checked_members(connection, item.calculation_id, require_seals=False)
            results = []
            for item in prepared:
                if item.version.fact.kind in MEMBER_KINDS:
                    connection.execute(
                        "INSERT INTO calculation_seal VALUES(?) ON CONFLICT DO NOTHING",
                        (item.calculation_id,),
                    )
                    results.append(self.engine._finish_prepared(connection, item))
                else:
                    results.append(
                        self.engine._publish(connection, item, kwargs.get("posting_period"))
                    )
            connection.execute(
                "INSERT INTO identity_correction VALUES(?,?,?)",
                (public["correction_id"], canonical(public), digest(public)),
            )
            calcs = {item.version.subject_id: item.calculation_id for item in prepared}
            for index, item in enumerate(public["items"]):
                result_subject = (
                    "opening-identity:" + item["subject_id"]
                    if item["action"] == "opening_binding"
                    else item["subject_id"]
                )
                connection.execute(
                    "INSERT INTO identity_correction_item VALUES(?,?,?,?,?,?,?,?)",
                    (
                        public["correction_id"] + ":" + str(index),
                        public["correction_id"],
                        item["subject_id"],
                        item["action"],
                        item["before_fact_id"],
                        item["after_fact_id"],
                        item["replacement_subject_id"],
                        calcs.get(result_subject),
                    ),
                )
                if item["action"] == "supersede":
                    connection.execute(
                        "DELETE FROM fact_current WHERE subject_id=?", (item["subject_id"],)
                    )
                    if versions[item["subject_id"]].fact.kind in MEMBER_KINDS:
                        connection.execute(
                            "DELETE FROM calculation_current WHERE subject_id=?",
                            (item["subject_id"],),
                        )
                        connection.execute(
                            "DELETE FROM pending WHERE subject_id=?", (item["subject_id"],)
                        )
            apply_resolution(
                connection,
                public["correction_id"],
                public["items"],
                kwargs.get("entity_resolution"),
            )
            from .entity_references import rebuild_entity_references

            rebuild_entity_references(connection)
            from .discovery_indexes import sync_discovery_subjects

            sync_discovery_subjects(
                connection, {i["subject_id"] for i in public["items"]} | set(versions)
            )
            self.engine._sync_publication_projections(
                connection, [i.version.subject_id for i in prepared]
            )
            verify_publication(self.engine, connection, [i.calculation_id for i in prepared])
            verify_projection_change(connection, projection)
            verify_identity_corrections(self.engine, connection)
            return {
                "status": "corrected",
                "correction_id": public["correction_id"],
                "results": results,
                "digest": preview_digest,
            }

        return self.engine._write(
            request_id,
            request_hash,
            epochs,
            ("accounting", "management"),
            "identity_correction",
            operation,
            checked_lanes=("accounting", "management", "material"),
        )


def verify_identity_corrections(engine, connection):
    from .entity_references import references_for

    def fail(ident, message):
        raise KernelError(
            "content_integrity_failed",
            message,
            component="identity_correction",
            record_id=ident,
        )

    receipts = {r["id"]: r for r in connection.execute("SELECT * FROM identity_correction")}
    adopted_bindings, terminated, adopted_owners = set(), set(), set()
    for ident, row in receipts.items():
        plan = json.loads(row["plan"])
        if (
            digest(plan) != row["digest"]
            or plan.get("correction_id") != ident
            or digest({k: v for k, v in plan.items() if k != "digest"}).hex() != plan["digest"]
            or "ic_"
            + digest(
                [
                    engine.store.database_id,
                    plan["proposal"],
                    plan["epochs"],
                    sorted((i["subject_id"], i["before_fact_id"]) for i in plan["items"]),
                ]
            ).hex()
            != ident
        ):
            raise KernelError(
                "content_integrity_failed",
                "身份纠错依据摘要不一致",
                component="identity_correction",
                record_id=ident,
            )
        items = list(
            connection.execute(
                "SELECT * FROM identity_correction_item WHERE correction_id=? ORDER BY id", (ident,)
            )
        )
        if len(items) != len(plan["items"]):
            raise KernelError(
                "content_integrity_failed",
                "身份纠错明细缺失",
                component="identity_correction",
                record_id=ident,
            )
        expected = {i["subject_id"]: i for i in plan["items"]}
        changes = {i["subject_id"]: i for i in plan["proposal"]["changes"]}
        newfacts = {i["fact_id"]: i for i in plan["fact_changes"]}
        results = {i["subject_id"]: i for i in plan["results"]}
        for item in items:
            if item["subject_id"] not in expected or any(
                item[k] != expected[item["subject_id"]][k]
                for k in ("action", "before_fact_id", "after_fact_id", "replacement_subject_id")
            ):
                raise KernelError(
                    "content_integrity_failed",
                    "身份纠错明细与批准内容不一致",
                    component="identity_correction",
                    record_id=item["id"],
                )
            source = engine.store.fact(connection, item["before_fact_id"])
            proposal = changes[item["subject_id"]]
            if (
                source.subject_id != item["subject_id"]
                or source.revision != proposal["expected_revision"]
            ):
                fail(item["id"], "身份纠错原事实身份或版本不一致")
            result_subject = (
                "opening-identity:" + item["subject_id"]
                if item["action"] == "opening_binding"
                else item["subject_id"]
            )
            expected_result = results.get(result_subject)
            if item["calculation_id"] != (
                expected_result["calculation_id"] if expected_result else None
            ):
                fail(item["id"], "身份纠错采用计算与批准结果不一致")
            if expected_result:
                calc = connection.execute(
                    "SELECT * FROM calculation WHERE id=?", (item["calculation_id"],)
                ).fetchone()
                if (
                    not calc
                    or calc["subject_id"] != result_subject
                    or calc["fact_id"] != expected_result["fact_id"]
                    or calc["digest"].hex() != expected_result["result_digest"]
                    or digest(json.loads(calc["outcome"])) != calc["digest"]
                ):
                    fail(item["id"], "身份纠错采用结果内容不一致")
            if item["after_fact_id"]:
                after = engine.store.fact(connection, item["after_fact_id"])
                approved = newfacts.get(after.id)
                if not approved or (
                    after.subject_id != approved["subject_id"]
                    or after.revision != approved["revision"]
                    or canonical(after.fact.model_dump(mode="json")) != canonical(approved["data"])
                    or list(after.evidence) != approved["evidence"]
                ):
                    fail(item["id"], "纠错保存事实与批准事实不一致")
                if item["action"] == "reassign":
                    entity_changes = _entity_changes(source.fact, after.fact, source.subject_id)
                    if (
                        after.subject_id != source.subject_id
                        or after.revision != source.revision + 1
                        or canonical(after.fact.model_dump(mode="json"))
                        != canonical(
                            type(source.fact)
                            .model_validate_json(canonical(proposal["data"]))
                            .model_dump(mode="json")
                        )
                    ):
                        fail(item["id"], "重指事实并非批准的同一业务后续版本")
                else:
                    binding = after.fact
                    if (
                        binding.kind != OpeningIdentityBinding.kind
                        or binding.source_fact_id != source.id
                        or binding.source_subject_id != source.subject_id
                        or binding.source_kind != source.fact.kind
                    ):
                        fail(item["id"], "期初绑定与保存来源不一致")
                    allowed = {
                        r["path"]
                        for r in references_for(source.fact, source.subject_id)
                        if r["reference_type"] == "entity"
                    }
                    if len({a.path for a in binding.assignments}) != len(
                        binding.assignments
                    ) or any(a.path not in allowed for a in binding.assignments):
                        fail(item["id"], "期初绑定修改了对象身份以外的字段")
                    corrected = (
                        _assignment_data(
                            engine.store.fact(connection, binding.replacement_source_fact_id),
                            binding.assignments,
                        )
                        if binding.operation == "supersede"
                        else _assignment_data(source, binding.assignments)
                    )
                    previous_binding = connection.execute(
                        "SELECT id FROM fact_revision WHERE subject_id=? AND revision=?",
                        (after.subject_id, after.revision - 1),
                    ).fetchone()
                    prior = (
                        engine.store.fact(connection, previous_binding[0]).fact
                        if previous_binding
                        else None
                    )
                    old_basis = (
                        _assignment_data(
                            engine.store.fact(connection, prior.replacement_source_fact_id),
                            prior.assignments,
                        )
                        if prior and prior.operation == "supersede"
                        else _assignment_data(source, prior.assignments)
                        if prior
                        else source.fact
                    )
                    entity_changes = _entity_changes(old_basis, corrected, source.subject_id)
                    if (
                        canonical(corrected.model_dump(mode="json"))
                        != canonical(
                            _assignment_data(
                                engine.store.fact(
                                    connection, expected[item["subject_id"]]["replacement_fact_id"]
                                ),
                                binding.assignments,
                            ).model_dump(mode="json")
                            if proposal["action"] == "supersede"
                            else type(source.fact)
                            .model_validate_json(canonical(proposal["data"]))
                            .model_dump(mode="json")
                        )
                        or binding.original_scopes
                        != tuple(sorted(source.fact.scopes_for(source.subject_id)))
                        or binding.adopted_scopes
                        != tuple(sorted(corrected.scopes_for(source.subject_id)))
                    ):
                        fail(item["id"], "期初绑定目标或业务范围不一致")
                    retained_binding_id = expected[item["subject_id"]].get(
                        "retained_binding_fact_id"
                    )
                    if binding.operation == "supersede":
                        retained = (
                            engine.store.fact(connection, retained_binding_id).fact
                            if retained_binding_id
                            else None
                        )
                        if (
                            retained
                            and (
                                retained.source_fact_id != binding.replacement_source_fact_id
                                or retained.operation != "reassign"
                                or retained.assignments != binding.assignments
                            )
                        ) or (not retained and binding.assignments):
                            fail(item["id"], "保留期初的对象归属并非批准时的精确采用")
                    selection = engine.store.select_many(connection, binding.reads())
                    regenerated = asdict(calculate_opening_binding(after, Context(selection)))
                    if (
                        not expected_result
                        or digest(regenerated).hex() != expected_result["result_digest"]
                    ):
                        fail(item["id"], "期初身份绑定改变了原保存的经济依据")
                    adopted_bindings.add(after.id)
            else:
                replacement = engine.store.fact(
                    connection, expected[item["subject_id"]]["replacement_fact_id"]
                )
                if (
                    replacement.subject_id != item["replacement_subject_id"]
                    or replacement.fact.kind != source.fact.kind
                ):
                    fail(item["id"], "替代保留来源与批准业务不一致")
                target_proposal = changes.get(replacement.subject_id)
                destination = (
                    type(replacement.fact).model_validate_json(canonical(target_proposal["data"]))
                    if target_proposal and target_proposal["data"]
                    else replacement.fact
                )
                entity_changes = _entity_changes(source.fact, destination, source.subject_id)
            if entity_changes != expected[item["subject_id"]]["entity_changes"]:
                fail(item["id"], "身份归属变更与精确事实范围不一致")
            if item["action"] == "supersede" and item["calculation_id"]:
                if getattr(source.fact, "actual_payment", False) or source.fact.kind.startswith(
                    "opening_"
                ):
                    fail(item["id"], "身份纠错非法撤去了真实资金或原期初")
                terminated.add(item["calculation_id"])
                calc = connection.execute(
                    "SELECT outcome,subject_id FROM calculation WHERE id=?",
                    (item["calculation_id"],),
                ).fetchone()
                outcome = json.loads(calc["outcome"]) if calc else {}
                if (
                    not calc
                    or calc["subject_id"] != item["subject_id"]
                    or canonical(outcome)
                    != canonical(
                        asdict(
                            Outcome(
                                (),
                                {
                                    "identity_correction": ident,
                                    "superseded": True,
                                    "obligations": [],
                                },
                            )
                        )
                    )
                ):
                    raise KernelError(
                        "content_integrity_failed",
                        "身份替代结果不是明确的受控终止",
                        component="identity_correction",
                        record_id=item["id"],
                    )
        for approved in plan["fact_changes"]:
            if approved["kind"] != OpeningBasisCorrection.kind:
                continue
            owner = engine.store.fact(connection, approved["fact_id"])
            if owner.subject_id != "opening-correction:" + ident or canonical(
                owner.fact.model_dump(mode="json")
            ) != canonical(approved["data"]):
                fail(owner.id, "期初差额汇总与批准纠错不一致")
            expected_members = {
                (
                    "opening-identity:" + i["subject_id"],
                    i["after_fact_id"],
                    "supersede" if changes[i["subject_id"]]["action"] == "supersede" else "restore",
                )
                for i in plan["items"]
                if i["action"] == "opening_binding"
                and (changes[i["subject_id"]]["action"] == "supersede" or i.get("reinstated"))
            }
            if {
                (m.binding_subject_id, m.binding_fact_id, m.action) for m in owner.fact.members
            } != expected_members:
                fail(owner.id, "期初差额汇总遗漏或增加了批准明细")
            result = calculate_opening_basis_correction(
                owner, Context(engine.store.select_many(connection, owner.fact.reads()))
            )
            saved = results.get(owner.subject_id)
            if not saved or digest(asdict(result)).hex() != saved["result_digest"]:
                fail(owner.id, "期初差额并非原保存明细的配平结果")
            calc = connection.execute(
                "SELECT fact_id,digest FROM calculation WHERE id=?", (saved["calculation_id"],)
            ).fetchone()
            if (
                not calc
                or calc["fact_id"] != owner.id
                or calc["digest"].hex() != saved["result_digest"]
            ):
                fail(owner.id, "期初差额汇总未被精确发布")
            adopted_owners.add(owner.id)
    all_owners = (
        {r[0] for r in connection.execute("SELECT revision_id FROM fact_opening_basis_correction")}
        if OpeningBasisCorrection.kind in engine.store.registry.models
        else set()
    )
    if all_owners != adopted_owners:
        fail("opening_basis_correction", "期初差额汇总缺少完整纠错批准")
    all_bindings = (
        {r[0] for r in connection.execute("SELECT revision_id FROM fact_opening_identity_binding")}
        if OpeningIdentityBinding.kind in engine.store.registry.models
        else set()
    )
    all_terminal = {
        r[0]
        for r in connection.execute(
            "SELECT id FROM calculation WHERE json_extract(outcome,'$.values.superseded')=1 "
            "AND kind<>'opening_identity_binding'"
        )
    }
    if adopted_bindings != all_bindings or terminated != all_terminal:
        fail("identity_correction", "存在缺少完整纠错批准的绑定或替代结果")
    return {"corrections": len(receipts)}
