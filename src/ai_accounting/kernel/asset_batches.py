"""Atomic, asset-specific card computation and batch posting.

The preview overlays typed facts in memory; it never opens a write transaction.
Only the owner publishes journal/balance effects. Frozen member rows are evidence
of adoption, never an alternate pointer to a shared publication.
"""

from __future__ import annotations

import json
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from .accounting import AccountingBook, compatibility
from .asset_batch_models import (
    LIFECYCLE_KINDS,
    MEMBER_KINDS,
    OWNER_KINDS,
    AssetActivationBatch,
    AssetConsumptionMonth,
)
from .contracts import FactVersion, KernelError, NeedsInformation, Read
from .domains.assets import AssetActivation, AssetConsumption
from .engine import Engine
from .types import YearMonth, canonical, digest, sum_fen


class ActivationBatchMemberInput(BaseModel):
    """Public wire shape for one card; journal fields are never accepted."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    subject_id: str = Field(min_length=1, max_length=200)
    data: AssetActivation
    expected_revision: int = Field(ge=0, strict=True)


ActivationBatchMembers = Annotated[list[ActivationBatchMemberInput], Field(max_length=5000)]


def frozen_members(connection, owner_calculation_id: str) -> list[dict]:
    """Return and validate exact frozen membership, including line attribution."""
    return _checked_members(connection, owner_calculation_id, require_seals=True)


def _checked_members(connection, owner_calculation_id, *, require_seals):
    owner = connection.execute(
        "SELECT * FROM calculation WHERE id=?", (owner_calculation_id,)
    ).fetchone()
    if owner is None or owner["kind"] not in OWNER_KINDS:
        raise KernelError("asset_batch_identity", "资产汇总计算不存在")
    if (
        require_seals
        and not connection.execute(
            "SELECT 1 FROM calculation_seal WHERE calculation_id=?", (owner_calculation_id,)
        ).fetchone()
    ):
        raise KernelError("asset_batch_unsealed", "资产汇总计算尚未封印")
    outcome = json.loads(owner["outcome"])
    if digest(outcome) != owner["digest"]:
        raise KernelError("asset_batch_digest", "资产汇总结果摘要不匹配")
    members, position, balances = [], 1, []
    for row in connection.execute(
        "SELECT * FROM asset_batch_member WHERE owner_calculation_id=? ORDER BY position",
        (owner_calculation_id,),
    ):
        member = dict(row)
        member.pop("owner_calculation_id")
        member["result_digest"] = member["result_digest"].hex()
        member["summary"] = json.loads(member["summary"])
        calculated = connection.execute(
            "SELECT * FROM calculation WHERE id=?", (member["member_calculation_id"],)
        ).fetchone()
        expected_kind = (
            "asset_activation" if owner["kind"] == "asset_activation_batch" else "asset_consumption"
        )
        if calculated is None or (
            calculated["kind"],
            calculated["subject_id"],
            calculated["fact_id"],
            calculated["period"],
        ) != (
            expected_kind,
            member["member_subject_id"],
            member["member_fact_id"],
            owner["period"],
        ):
            raise KernelError("asset_batch_identity", "资产汇总成员身份不匹配")
        if (
            require_seals
            and not connection.execute(
                "SELECT 1 FROM calculation_seal WHERE calculation_id=?", (calculated["id"],)
            ).fetchone()
        ):
            raise KernelError("asset_batch_unsealed", "资产汇总成员尚未封印")
        if connection.execute(
            "SELECT 1 FROM asset_batch_member m JOIN calculation c ON c.id=m.owner_calculation_id "
            "WHERE m.member_calculation_id=? AND c.subject_id<>? LIMIT 1",
            (calculated["id"], owner["subject_id"]),
        ).fetchone():
            raise KernelError("asset_member_owned", "同一成员计算不得被不同批次采用")
        result = json.loads(calculated["outcome"])
        count = len(result["lines"])
        if (
            member["position"] != len(members) + 1
            or digest(result).hex() != member["result_digest"]
            or calculated["digest"].hex() != member["result_digest"]
            or result["values"] != member["summary"]
            or result["values"].get("asset_id") != member["asset_id"]
            or member["line_count"] != count
            or member["line_start"] != (position if count else None)
            or outcome["lines"][position - 1 : position - 1 + count] != result["lines"]
        ):
            raise KernelError("asset_batch_digest", "资产汇总成员或分录范围不匹配")
        if not connection.execute(
            "SELECT 1 FROM dependency_calculation WHERE calculation_id=? AND upstream_id=?",
            (owner_calculation_id, calculated["id"]),
        ).fetchone():
            raise KernelError("asset_batch_identity", "资产汇总缺少精确成员依赖")
        member["kind"] = expected_kind
        members.append(member)
        balances.extend(result["balances"])
        position += count
    values = outcome["values"]
    if (
        type(values.get("member_count")) is not int
        or len(members) != values.get("member_count")
        or balances != outcome["balances"]
        or position - 1 != len(outcome["lines"])
        or members != values.get("members")
        or digest(members).hex() != values.get("membership_digest")
    ):
        raise KernelError("asset_batch_digest", "资产汇总完整成员清单不匹配")
    return members


def _matches(version, read):
    return (
        read.kind in ("*", version.fact.kind)
        and (not read.before_period or version.fact.period < read.before_period)
        and read.key
        in {
            "*",
            "@" + version.subject_id,
            str(version.fact.period),
            *version.fact.scopes_for(version.subject_id),
        }
    )


def required_asset_ids(period: YearMonth, selections) -> tuple[str, ...]:
    """One lifecycle selector for preparation and period readiness.

    Zero computations follow the existing useful-life interval; no perpetually
    recurring zero cards are invented before/after that accounting interval.
    """
    by_kind = {kind: list(selections.get(kind, ())) for kind in LIFECYCLE_KINDS}
    acquisitions = {
        v.subject_id: v for kind in ("asset", "reimbursed_asset") for v in by_kind[kind]
    }
    openings = {v.subject_id: v for v in by_kind["opening_asset"]}
    disposals = {v.fact.asset_id: v for v in by_kind["asset_disposal"]}
    activated = {}
    for version in by_kind["asset_activation"]:
        if version.fact.asset_id in activated:
            raise KernelError("duplicate_asset_activation", "同一资产不能存在多个当前启用身份")
        activated[version.fact.asset_id] = version
    result = []
    for asset_id in sorted(set(activated) | set(openings)):
        if asset_id in openings and asset_id in activated:
            raise KernelError("duplicate_asset_activation", "期初在用资产不得再次启用")
        if asset_id in disposals and disposals[asset_id].fact.period < period:
            continue
        opening = openings.get(asset_id)
        activation = opening or activated[asset_id]
        source = opening or acquisitions.get(asset_id)
        if source is None:
            raise NeedsInformation("asset_id", "需要已确认的资产取得来源", sources=(asset_id,))
        if opening and period < opening.fact.period:
            continue
        in_use = opening.fact.in_use_date.period if opening else activation.fact.period
        start = in_use.ordinal + (source.fact.asset_type == "fixed")
        if start <= period.ordinal < start + activation.fact.useful_life_months:
            result.append(asset_id)
    return tuple(result)


class _AssetPreparation(Engine):
    """Narrow snapshot adapter; all topological evaluation remains in Engine."""

    def __init__(self, engine, snapshot, outcomes):
        super().__init__(engine.store)
        self.snapshot, self.outcomes = snapshot, outcomes

    def _snapshot(self, subjects):
        return self.snapshot

    def _evaluate(self, version, context):
        if version.fact.kind not in OWNER_KINDS:
            result = super()._evaluate(version, context)
            self.outcomes[version.subject_id] = result
            return result
        for read in version.fact.reads():
            context.select(read)
        member_kind = (
            "asset_activation"
            if version.fact.kind == "asset_activation_batch"
            else "asset_consumption"
        )
        if member_kind == "asset_activation":
            subjects = [m.subject_id for m in version.fact.members]
            selected = [context.calculations(member_kind, "@" + sid) for sid in subjects]
            if any(len(items) != 1 for items in selected):
                raise NeedsInformation("asset_activation", "需要批次每张卡片的完整计算")
            calculations = [items[0] for items in selected]
        else:
            calculations = list(context.calculations(member_kind, str(version.fact.period)))
        lines, balances, members = [], [], []
        for calc in sorted(calculations, key=lambda c: (c.values["asset_id"], c.subject_id)):
            result = self.outcomes[calc.subject_id]
            if digest(result).hex() != calc.result_digest:
                raise KernelError("asset_member_result", "成员计算结果与批次采用版本不一致")
            count = len(result["lines"])
            members.append(
                {
                    "position": len(members) + 1,
                    "asset_id": calc.values["asset_id"],
                    "member_subject_id": calc.subject_id,
                    "member_fact_id": calc.fact_id,
                    "member_calculation_id": calc.id,
                    "result_digest": calc.result_digest,
                    "summary": result["values"],
                    "line_start": len(lines) + 1 if count else None,
                    "line_count": count,
                    "kind": member_kind,
                }
            )
            lines.extend(result["lines"])
            balances.extend(result["balances"])
        total = sum_fen(line["debit"] for line in lines)
        # Exercise signed-64-bit boundaries before the transaction starts.
        by_balance = {}
        for effect in balances:
            key = effect["category"], effect["key"]
            by_balance[key] = sum_fen((by_balance.get(key, 0), effect["amount"]))
        result = {
            "lines": lines,
            "balances": balances,
            "opening_lines": [],
            "opening": False,
            "explanation": [],
            "values": {
                "member_count": len(members),
                "positive_member_count": sum(bool(m["line_count"]) for m in members),
                "amount_fen": total,
                "members": members,
                ("cost_fen" if member_kind == "asset_activation" else "consumption_fen"): total,
                "membership_digest": digest(members).hex(),
            },
        }
        self.outcomes[version.subject_id] = result
        return result


class AssetBatches:
    def __init__(self, engine: Engine):
        self.engine, self.store = engine, engine.store

    def _version(self, connection, model, subject_id, data, evidence, expected_revision):
        # Reuse precisely the public typed fact validation, evidence and immutable
        # identity rules, but assign a reproducible private preview fact identity.
        self.engine._registration(
            False,
            model.kind,
            subject_id,
            data,
            evidence=evidence,
            expected_revision=expected_revision,
        )
        fact = model.model_validate_json(canonical(data))
        row = connection.execute("SELECT kind FROM subject WHERE id=?", (subject_id,)).fetchone()
        old = None
        if row:
            if row[0] != model.kind:
                raise KernelError("identity_mismatch", "业务身份不能改变类型")
            old = self.store.current_fact(connection, subject_id)
        revision = old.revision if old else 0
        if type(expected_revision) is not int or expected_revision != revision:
            raise KernelError("fact_version_conflict", "已确认事实版本发生变化")
        if old and any(
            getattr(fact, name) != getattr(old.fact, name) for name in model.identity_fields
        ):
            raise KernelError("immutable_fact", "稳定业务身份不能更改")
        evidence = tuple(sorted(set(evidence)))
        for key in evidence:
            if not connection.execute(
                "SELECT 1 FROM evidence WHERE digest=?", (bytes.fromhex(key),)
            ).fetchone():
                raise NeedsInformation("evidence", "确认依据不存在", sources=(key,))
        if old and fact == old.fact and evidence == old.evidence:
            return old
        ident = (
            "f_ab_"
            + digest(
                [
                    "asset-fact-v1",
                    self.store.database_id,
                    model.kind,
                    subject_id,
                    old.id if old else None,
                    revision + 1,
                    fact.model_dump(mode="json"),
                    evidence,
                ]
            ).hex()
        )
        return FactVersion(ident, subject_id, revision + 1, fact, evidence)

    def _prepare(
        self, mode, subject_id, period, members, evidence, expected_revision, correction_period
    ):
        month = YearMonth(period)
        with self.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            epochs = self.store.epochs(connection)
            changes = {}
            if mode == "activation":
                try:
                    typed_members = [
                        item
                        if isinstance(item, ActivationBatchMemberInput)
                        else ActivationBatchMemberInput.model_validate(item, strict=True)
                        for item in members
                    ]
                except (TypeError, ValueError) as exc:
                    raise KernelError(
                        "invalid_asset_member",
                        "启用成员仅接收身份、原有核算事实和预期版本",
                    ) from exc
                if len(typed_members) > 5000:
                    raise KernelError("invalid_asset_batch", "启用批次成员不得超过 5000 项")
                for member in typed_members:
                    version = self._version(
                        connection,
                        AssetActivation,
                        member.subject_id,
                        member.data.model_dump(mode="json"),
                        evidence,
                        member.expected_revision,
                    )
                    if version.fact.period != month:
                        raise KernelError("asset_batch_period", "每张启用卡片须属于批次核算月")
                    if version.subject_id in changes:
                        raise KernelError("duplicate_asset_member", "启用批次不能重复成员")
                    changes[version.subject_id] = version
                owner = self._version(
                    connection,
                    AssetActivationBatch,
                    subject_id,
                    {"period": period, "members": [{"subject_id": sid} for sid in sorted(changes)]},
                    evidence,
                    expected_revision,
                )
            else:
                subject_id = "asset-consumption-month:" + period
                owner = self._version(
                    connection,
                    AssetConsumptionMonth,
                    subject_id,
                    {"period": period},
                    evidence,
                    expected_revision,
                )
            changes[subject_id] = owner
            removed = set()
            pending_subjects = {
                r[0] for r in connection.execute("SELECT DISTINCT subject_id FROM pending")
            }
            queue, facts = [subject_id, *changes], {}

            def current(sid):
                return changes.get(sid) or self.store.current_fact(connection, sid)

            def select(read):
                rows = list(self.store.select(connection, read))
                if read.key.startswith("#"):
                    return tuple(rows)
                if read.source == "fact":
                    rows = [
                        v
                        for v in rows
                        if v.subject_id not in changes and v.subject_id not in removed
                    ]
                    rows.extend(
                        v
                        for v in changes.values()
                        if _matches(v, read) and v.subject_id not in removed
                    )
                    rows.sort(key=lambda v: v.subject_id)
                else:
                    rows = [v for v in rows if v.subject_id not in removed]
                return tuple(rows)

            # Membership changes invalidate all already-created affected owners,
            # including empty lifecycle scope consumers. Missing months stay missing.
            for version in list(changes.values()):
                oldrow = connection.execute(
                    "SELECT fact_id FROM fact_current WHERE subject_id=?", (version.subject_id,)
                ).fetchone()
                if oldrow and oldrow[0] == version.id:
                    continue
                scopes = {
                    *version.fact.scopes_for(version.subject_id),
                    "@" + version.subject_id,
                    str(version.fact.period),
                }
                earliest = version.fact.period.ordinal
                if oldrow:
                    old = self.store.fact(connection, oldrow[0])
                    scopes.update(old.fact.scopes_for(old.subject_id))
                    earliest = min(earliest, old.fact.period.ordinal)
                extra = set()
                for source in ("fact", "calculation"):
                    extra |= self.engine._scope_consumers(
                        connection, source, version.fact.kind, scopes, earliest
                    )
                queue.extend(self.engine._descendants(connection, extra))
            while queue:
                sid = queue.pop()
                if sid in facts:
                    continue
                version = current(sid)
                if version.fact.kind not in self.store.registry.evaluators:
                    continue
                facts[sid] = version
                if version.fact.kind == "asset_activation_batch":
                    member_ids = [m.subject_id for m in version.fact.members]
                    prior_rows = connection.execute(
                        "SELECT m.member_subject_id FROM calculation_current c "
                        "JOIN asset_batch_member m "
                        "ON m.owner_calculation_id=c.calculation_id WHERE c.subject_id=?",
                        (sid,),
                    ).fetchall()
                    missing = {r[0] for r in prior_rows} - set(member_ids)
                    if missing:
                        # Submitting the complete revised activation batch is the
                        # asset-specific withdrawal command.  Only the old owner is
                        # an internal dependant; real consumption/disposal or other
                        # current users must be corrected first.
                        placeholders = ",".join("?" for _ in missing)
                        consumers = {
                            row[0]
                            for row in connection.execute(
                                "SELECT a.subject_id FROM dependency_fact d "
                                "JOIN fact_revision f ON f.id=d.fact_id "
                                "JOIN calculation_current a ON a.calculation_id=d.calculation_id "
                                f"WHERE f.subject_id IN ({placeholders}) UNION "
                                "SELECT a.subject_id FROM dependency_calculation d "
                                "JOIN calculation c ON c.id=d.upstream_id "
                                "JOIN calculation_current a ON a.calculation_id=d.calculation_id "
                                f"WHERE c.subject_id IN ({placeholders})",
                                (*sorted(missing), *sorted(missing)),
                            )
                        }
                        consumers.discard(sid)
                        consumers.difference_update(missing)
                        for pending_row in connection.execute(
                            "SELECT DISTINCT p.subject_id,f.fact_id FROM pending p "
                            "JOIN fact_current f ON f.subject_id=p.subject_id "
                            "WHERE p.subject_id<>?",
                            (sid,),
                        ):
                            dependent = self.store.fact(connection, pending_row["fact_id"])
                            if any(
                                item.subject_id in missing
                                for read in dependent.fact.reads_for(dependent.subject_id)
                                for item in select(read)
                            ):
                                consumers.add(dependent.subject_id)
                        if consumers:
                            raise KernelError(
                                "has_dependents",
                                "启用成员仍有有效下游业务，须先更正下游后再撤回",
                                subjects=sorted(consumers),
                            )
                        removed.update(missing)
                    for member_sid in member_ids:
                        conflict = connection.execute(
                            "SELECT o.subject_id FROM asset_batch_member m "
                            "JOIN calculation_current o ON o.calculation_id=m.owner_calculation_id "
                            "WHERE m.member_subject_id=? AND o.subject_id<>?",
                            (member_sid, sid),
                        ).fetchone()
                        if conflict:
                            raise KernelError("asset_member_owned", "启用成员已属于另一个批次")
                    queue.extend(member_ids)
                elif version.fact.kind == "asset_consumption_month":
                    period_value = version.fact.period
                    until = YearMonth.from_ordinal(period_value.ordinal + 1)
                    lifecycle = {
                        kind: select(Read("fact", kind, "*", until)) for kind in LIFECYCLE_KINDS
                    }
                    asset_ids = required_asset_ids(period_value, lifecycle)
                    legacy = connection.execute(
                        "SELECT 1 FROM calculation c JOIN calculation_publication p "
                        "ON p.calculation_id=c.id "
                        "WHERE c.kind='asset_consumption' AND c.period=? LIMIT 1",
                        (period_value.ordinal,),
                    ).fetchone()
                    if legacy:
                        raise KernelError(
                            "legacy_asset_consumption_period",
                            "此月已有旧单卡凭证，不能重复生成月度汇总",
                        )
                    expected = set()
                    for asset_id in asset_ids:
                        candidates = select(
                            Read("fact", "asset_consumption", f"asset:{asset_id}:{period_value}")
                        )
                        if len(candidates) > 1:
                            raise KernelError("duplicate_consumption", "资产每月只能有一个摊折身份")
                        old = candidates[0] if candidates else None
                        member_sid = (
                            old.subject_id
                            if old
                            else "asset-consumption:"
                            + digest([self.store.database_id, asset_id, str(period_value)]).hex()
                        )
                        member = self._version(
                            connection,
                            AssetConsumption,
                            member_sid,
                            {"period": str(period_value), "asset_id": asset_id},
                            old.evidence if old else version.evidence,
                            old.revision if old else 0,
                        )
                        changes[member_sid] = member
                        expected.add(member_sid)
                        queue.append(member_sid)
                    for old in select(Read("fact", "asset_consumption", str(period_value))):
                        if old.subject_id not in expected:
                            removed.add(old.subject_id)
                elif version.fact.kind in MEMBER_KINDS:
                    if connection.execute(
                        "SELECT 1 FROM calculation c JOIN calculation_publication p "
                        "ON p.calculation_id=c.id "
                        "WHERE c.subject_id=? LIMIT 1",
                        (sid,),
                    ).fetchone():
                        raise KernelError(
                            "legacy_asset_member", "旧单卡发布结果保留历史，不能隐式收编或续写"
                        )
                for read in version.fact.reads_for(sid):
                    # A pending lifecycle fact can itself need publication (for
                    # example disposal after this month's consumption). It has
                    # no saved calculation edge yet, so include declared fact
                    # inputs as well as calculation inputs in the same graph.
                    if read.source in {"fact", "calculation"} and not read.key.startswith("#"):
                        queue.extend(
                            v.subject_id
                            for v in select(Read("fact", read.kind, read.key, read.before_period))
                            if v.subject_id in pending_subjects
                            and v.subject_id not in facts
                            and v.fact.kind in self.store.registry.evaluators
                        )
                # Existing dependants remain in one reviewed graph.
                queue.extend(self.engine._descendants(connection, {sid}) - set(facts))
            facts = {sid: v for sid, v in facts.items() if sid not in removed}
            reads = {r for v in facts.values() for r in v.fact.reads_for(v.subject_id)}
            selections = {r: select(r) for r in reads}
            pending = {r[0] for r in connection.execute("SELECT DISTINCT subject_id FROM pending")}
            closed = {r[0] for r in connection.execute("SELECT period FROM period_close")}
            previous, outcomes = {}, {}
            for sid in facts:
                row = connection.execute(
                    "SELECT c.* FROM calculation_current a "
                    "JOIN calculation c ON c.id=a.calculation_id WHERE a.subject_id=?",
                    (sid,),
                ).fetchone()
                previous[sid] = row["id"] if row else None
                if row:
                    outcomes[sid] = json.loads(row["outcome"])
            for values in selections.values():
                for value in values:
                    if not isinstance(value, FactVersion) and value.subject_id not in outcomes:
                        row = connection.execute(
                            "SELECT outcome FROM calculation WHERE id=?", (value.id,)
                        ).fetchone()
                        outcomes[value.subject_id] = json.loads(row[0])
            book = AccountingBook(self.store.registry)
            book.facts.update((v.id, v) for v in facts.values())
            book.facts.update(
                (v.id, v)
                for values in selections.values()
                for v in values
                if isinstance(v, FactVersion)
            )
            compare_ids = {cid for cid in previous.values() if cid}
            for version in facts.values():
                if version.fact.kind in self.store.registry.accounting_consumers:
                    selector = self.store.registry.accounting_consumers[version.fact.kind]
                    required = (
                        selector(version)
                        if selector
                        else version.fact.reads_for(version.subject_id)
                    )
                    compare_ids.update(
                        calc.id
                        for read in required
                        if read.source == "calculation"
                        for calc in selections[read]
                    )
            book.load(self.store, connection, compare_ids)
            changed = {
                sid: v
                for sid, v in changes.items()
                if not connection.execute(
                    "SELECT 1 FROM fact_revision WHERE id=?", (v.id,)
                ).fetchone()
            }
            snapshot = (epochs, facts, selections, pending, closed, previous, book)
            public, prepared = _AssetPreparation(self.engine, snapshot, outcomes)._prepare(
                list(facts), correction_period
            )
            public["fact_changes"] = [
                {
                    "subject_id": v.subject_id,
                    "fact_id": v.id,
                    "kind": v.fact.kind,
                    "revision": v.revision,
                    "data": v.fact.model_dump(mode="json"),
                    "evidence": list(v.evidence),
                }
                for v in sorted(changed.values(), key=lambda v: v.subject_id)
            ]
            public["removed_members"] = sorted(removed)
            public["digest"] = digest({k: v for k, v in public.items() if k != "digest"}).hex()
            return public, prepared, changed, removed

    def prepare_activation_batch(
        self,
        subject_id: str,
        period: str,
        members: ActivationBatchMembers,
        *,
        evidence: tuple[str, ...],
        expected_revision: int,
        correction_period: str | None = None,
    ):
        return {
            "status": "preview",
            **self._prepare(
                "activation",
                subject_id,
                period,
                members,
                evidence,
                expected_revision,
                correction_period,
            )[0],
        }

    def prepare_consumption_month(
        self,
        period: str,
        *,
        evidence: tuple[str, ...],
        expected_revision: int,
        correction_period: str | None = None,
    ):
        return {
            "status": "preview",
            **self._prepare(
                "consumption", "", period, None, evidence, expected_revision, correction_period
            )[0],
        }

    def _confirm(
        self,
        mode,
        subject_id,
        period,
        members,
        evidence,
        expected_revision,
        correction_period,
        preview_digest,
        epochs,
        request_id,
    ):
        request_hash = digest(
            [
                "asset_batch",
                mode,
                subject_id,
                period,
                members,
                evidence,
                expected_revision,
                correction_period,
                preview_digest,
                epochs,
            ]
        )
        cached = self.engine._cached(request_id, request_hash)
        if cached is not None:
            return cached
        public, prepared, changes, removed = self._prepare(
            mode, subject_id, period, members, evidence, expected_revision, correction_period
        )
        if public["digest"] != preview_digest or public["epochs"] != epochs:
            raise KernelError("preview_expired", "资产批次与已审阅预览不同")

        def operation(connection):
            from .integrity import verify_prepared_sources, verify_publication
            from .projections import prepare_projection_check, verify_projection_change

            for item in prepared:
                if item.compatibility_issue:
                    raise compatibility(item.calculation_id, "asset_comparison_unavailable")
            verify_prepared_sources(
                self.engine, connection, prepared,
                new_fact_ids={version.id for version in changes.values()},
            )
            projection_check = prepare_projection_check(
                connection, prepared, correction_period=correction_period
            )
            for version in sorted(changes.values(), key=lambda v: v.subject_id):
                self.store.write_fact(
                    connection, version, digest(version.fact.model_dump(mode="json"))
                )
                scopes = {
                    *version.fact.scopes_for(version.subject_id),
                    "@" + version.subject_id,
                    str(version.fact.period),
                }
                affected = self.engine._scope_consumers(
                    connection, "fact", version.fact.kind, scopes, version.fact.period.ordinal
                )
                affected.add(version.subject_id)
                affected = self.engine._descendants(connection, affected)
                connection.executemany(
                    "INSERT INTO pending VALUES(?,?) ON CONFLICT DO NOTHING",
                    [(sid, version.id) for sid in sorted(affected)],
                )
            for item in prepared:
                self.engine._record_prepared(connection, item)
            for item in prepared:
                if item.version.fact.kind not in OWNER_KINDS:
                    continue
                cid = item.calculation_id
                if connection.execute(
                    "SELECT 1 FROM calculation_seal WHERE calculation_id=?", (cid,)
                ).fetchone():
                    continue
                for member in item.outcome["values"]["members"]:
                    connection.execute(
                        "INSERT INTO asset_batch_member VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            cid,
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
                _checked_members(connection, cid, require_seals=False)
            for sid in sorted(removed):
                version = self.store.current_fact(connection, sid)
                activation = version.fact.kind == "asset_activation"
                connection.execute(
                    "INSERT INTO disposition(subject_id,cause_id,action,explanation) "
                    "VALUES(?,?,?,?)",
                    (
                        sid,
                        version.id,
                        "withdrawn" if activation else "asset_derived_removed",
                        "从已确认启用批次撤回误记成员"
                        if activation
                        else "完整生命周期重算后本月不再属于应计提成员",
                    ),
                )
                connection.execute("DELETE FROM calculation_current WHERE subject_id=?", (sid,))
                connection.execute("DELETE FROM pending WHERE subject_id=?", (sid,))
                if activation:
                    connection.execute("DELETE FROM fact_current WHERE subject_id=?", (sid,))
            results = []
            for item in prepared:
                if item.version.fact.kind in MEMBER_KINDS:
                    cid = item.calculation_id
                    if not connection.execute(
                        "SELECT 1 FROM calculation_seal WHERE calculation_id=?", (cid,)
                    ).fetchone():
                        connection.execute("INSERT INTO calculation_seal VALUES(?)", (cid,))
                    results.append(
                        {
                            **self.engine._finish_prepared(connection, item),
                            "publication_role": "asset_member",
                        }
                    )
                else:
                    results.append(self.engine._publish(connection, item, correction_period))
            verify_publication(
                self.engine, connection, [item.calculation_id for item in prepared]
            )
            verify_projection_change(connection, projection_check)
            return {"status": "published", "results": results, "digest": preview_digest}

        return self.engine._write(
            request_id,
            request_hash,
            epochs,
            ("accounting",),
            "publish_asset_batch",
            operation,
            checked_lanes=public["checked_lanes"],
        )

    def confirm_activation_batch(
        self,
        subject_id: str,
        period: str,
        members: ActivationBatchMembers,
        *,
        evidence: tuple[str, ...],
        expected_revision: int,
        preview_digest: str,
        epochs: dict,
        request_id: str,
        correction_period: str | None = None,
    ):
        return self._confirm(
            "activation",
            subject_id,
            period,
            members,
            evidence,
            expected_revision,
            correction_period,
            preview_digest,
            epochs,
            request_id,
        )

    def confirm_consumption_month(
        self,
        period: str,
        *,
        evidence: tuple[str, ...],
        expected_revision: int,
        preview_digest: str,
        epochs: dict,
        request_id: str,
        correction_period: str | None = None,
    ):
        return self._confirm(
            "consumption",
            "",
            period,
            None,
            evidence,
            expected_revision,
            correction_period,
            preview_digest,
            epochs,
            request_id,
        )
