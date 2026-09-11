"""One publisher for every domain. Computation finishes before BEGIN IMMEDIATE."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from contextlib import nullcontext
from dataclasses import asdict, dataclass

from pydantic import ValidationError

from .build import calculator_build_id
from .contracts import Calculation, Context, FactVersion, KernelError, NeedsInformation
from .storage import Store
from .types import YearMonth, canonical, checked, digest, sum_fen

PROGRAM_VERSION = calculator_build_id()


def _reject_binary_numbers(value):
    if isinstance(value, float):
        raise KernelError("binary_float", "金额须使用整数分；税率等小数须使用十进制字符串")
    if isinstance(value, dict):
        for item in value.values():
            _reject_binary_numbers(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_binary_numbers(item)


@dataclass(frozen=True)
class Prepared:
    version: FactVersion
    calculation_id: str
    outcome: dict
    context: Context


class Engine:
    def __init__(self, store: Store, *, fault=None, commit_guard=None, audit_actor=None):
        self.store = store
        self.fault = fault or (lambda stage, connection: None)
        self.last_metrics = {}
        self.commit_guard = commit_guard or nullcontext
        self.audit_actor = audit_actor

    def _cached(self, key, request_hash):
        with self.store.connection(read_only=True) as connection:
            return self._replay(connection, key, request_hash)

    @staticmethod
    def _replay(connection, key, request_hash):
        row = connection.execute("SELECT digest,result FROM request WHERE id=?", (key,)).fetchone()
        if row:
            if row["digest"] != request_hash:
                raise KernelError("idempotency_conflict", "同一幂等键携带了不同的业务请求")
            return json.loads(row["result"])
        return None

    def _write(self, key, request_hash, expected, lanes, action, operation, *, checked_lanes=None):
        if not isinstance(key, str) or not key or len(key) > 200:
            raise KernelError(
                "invalid_request_id", "request id must be nonempty and at most 200 chars"
            )
        with self.commit_guard(), self.store.connection() as connection:
            count = 0

            def trace(sql):
                nonlocal count
                count += 1

            connection.set_trace_callback(trace)
            started = time.perf_counter()
            try:
                connection.execute("BEGIN IMMEDIATE")
                locked = time.perf_counter()
                replay = self._replay(connection, key, request_hash)
                if replay is not None:
                    connection.rollback()
                    return replay
                current = self.store.epochs(connection)
                check_lanes = lanes if checked_lanes is None else checked_lanes
                if expected is not None and any(current[k] != expected.get(k) for k in check_lanes):
                    raise KernelError(
                        "preview_expired", "事实或期间已变化，请重新预览", current=current
                    )
                self.fault("begin", connection)
                result = operation(connection)
                self.fault("published", connection)
                for lane in lanes:
                    connection.execute(f"UPDATE state SET {lane}={lane}+1 WHERE id=1")
                connection.execute(
                    "INSERT INTO audit(request_id,action,payload) VALUES(?,?,?)",
                    (
                        key,
                        action,
                        canonical(
                            {"result": result, "actor": self.audit_actor}
                            if self.audit_actor is not None
                            else result
                        ),
                    ),
                )
                connection.execute(
                    "INSERT INTO request VALUES(?,?,?)", (key, request_hash, canonical(result))
                )
                self.fault("commit", connection)
                connection.commit()
                self.last_metrics = {
                    "sql_statements": count,
                    "lock_seconds": time.perf_counter() - locked,
                    "write_seconds": time.perf_counter() - started,
                }
                return result
            except BaseException:
                # A deferred FK or I/O failure at COMMIT leaves SQLite in a transaction.
                connection.rollback()
                raise

    def register_evidence(self, content: bytes, media_type: str, name: str, *, request_id: str):
        if not isinstance(content, bytes):
            raise ValueError("evidence content must be bytes")
        if len(content) > 20 * 1024 * 1024:
            raise KernelError("evidence_too_large", "单份证据不能超过 20 MiB")
        hashed = hashlib.sha256(content).digest()
        request_hash = digest(["evidence", hashed.hex(), media_type, name])

        def operation(connection):
            connection.execute(
                "INSERT INTO evidence VALUES(?,?,?,?) ON CONFLICT(digest) DO NOTHING",
                (hashed, content, media_type, name),
            )
            return {"status": "registered", "digest": hashed.hex()}

        return self._write(request_id, request_hash, None, ("material",), "evidence", operation)

    def _registration(
        self,
        recording_correction,
        /,
        kind: str,
        subject_id: str,
        data: dict,
        *,
        evidence: tuple[str, ...],
        expected_revision: int,
    ):
        if kind not in self.store.registry.models:
            raise KernelError("unknown_fact_type", f"unknown business fact {kind}")
        if not subject_id or len(subject_id) > 200:
            raise KernelError("invalid_subject", "invalid stable business identity")
        _reject_binary_numbers(data)
        try:
            # JSON mode accepts explicit wire dates/Decimal strings, never float monetary values.
            model = self.store.registry.models[kind]
            fact = model.model_validate_json(canonical(data))
        except ValidationError as exc:
            missing = [e for e in exc.errors() if e["type"] == "missing"]
            if missing:
                issue = missing[0]
                raise NeedsInformation(
                    ".".join(map(str, issue["loc"])), "缺少必需核算事实", sources=(subject_id,)
                ) from exc
            raise KernelError(
                "invalid_fact",
                "业务事实校验失败",
                fact_issues=json.loads(exc.json(include_url=False)),
            ) from exc
        if not evidence:
            raise NeedsInformation("evidence", "已确认事实必须引用不可变依据")
        if any(len(bytes.fromhex(e)) != 32 for e in evidence):
            raise ValueError("evidence digest must be 32 bytes")
        payload = [
            kind,
            subject_id,
            fact.model_dump(mode="json"),
            sorted(set(evidence)),
            expected_revision,
        ]
        request_hash = digest(["save_fact", payload])

        def operation(connection):
            row = connection.execute(
                "SELECT s.kind,f.fact_id FROM subject s LEFT JOIN fact_current f "
                "ON f.subject_id=s.id WHERE s.id=?",
                (subject_id,),
            ).fetchone()
            old = (
                self.store.current_fact(connection, subject_id) if row and row["fact_id"] else None
            )
            if recording_correction and old is None:
                raise KernelError("no_record_to_amend", "录入纠错必须指向既有确认事实")
            if row and old is None:
                raise KernelError("withdrawn_subject", "已撤去身份保留审计；新业务须使用新身份")
            if row and row["kind"] != kind:
                raise KernelError(
                    "identity_mismatch", "stable identity cannot change business type"
                )
            immutable_changed = (
                old
                and fact.immutable
                and (
                    not fact.immutable_fields
                    or any(
                        getattr(fact, name) != getattr(old.fact, name)
                        for name in fact.immutable_fields
                    )
                )
            )
            if old and (
                (immutable_changed and not recording_correction)
                or any(
                    getattr(fact, name) != getattr(old.fact, name) for name in fact.identity_fields
                )
            ):
                raise KernelError("immutable_fact", "实际事实或稳定业务身份不能原地替换")
            revision = old.revision if old else 0
            if type(expected_revision) is not int or expected_revision != revision:
                raise KernelError("fact_version_conflict", "已确认事实版本发生变化")
            version = FactVersion(
                uuid.uuid4().hex, subject_id, revision + 1, fact, tuple(sorted(set(evidence)))
            )
            self.store.write_fact(connection, version, digest(fact.model_dump(mode="json")))
            scopes = set(fact.scopes_for(subject_id)) | {"@" + subject_id, str(fact.period)}
            scopes.update(claim.key for claim in fact.claims())
            if old:
                scopes.update(old.fact.scopes_for(old.subject_id))
                scopes.add(str(old.fact.period))
                scopes.update(claim.key for claim in old.fact.claims())
            affected = self._scope_consumers(
                connection,
                "fact",
                kind,
                scopes,
                min(fact.period.ordinal, old.fact.period.ordinal if old else 119988),
            )
            if kind in self.store.registry.evaluators:
                affected.add(subject_id)
            affected = self._descendants(connection, affected)
            connection.executemany(
                "INSERT INTO pending VALUES(?,?) ON CONFLICT DO NOTHING",
                [(sid, version.id) for sid in sorted(affected)],
            )
            return {
                "status": "confirmed",
                "subject_id": subject_id,
                "fact_id": version.id,
                "revision": version.revision,
                "pending": sorted(affected),
            }

        return request_hash, operation

    def _require_direct_registration(self, kind):
        model = self.store.registry.models.get(kind)
        if model is not None and model.registration_command:
            raise KernelError(
                "registration_command_required",
                "此类事实须通过核对来源的类型化命令生成",
                command=model.registration_command,
            )

    def save_fact(
        self,
        kind: str,
        subject_id: str,
        data: dict,
        *,
        evidence: tuple[str, ...],
        expected_revision: int,
        request_id: str,
    ):
        self._require_direct_registration(kind)
        request_hash, operation = self._registration(
            False, kind, subject_id, data, evidence=evidence, expected_revision=expected_revision
        )
        return self._write(
            request_id,
            request_hash,
            None,
            (self.store.registry.models[kind].lane,),
            "confirm_fact",
            operation,
        )

    def amend_fact(
        self,
        kind: str,
        subject_id: str,
        data: dict,
        *,
        evidence: tuple[str, ...],
        expected_revision: int,
        recording_error_confirmed: bool,
        request_id: str,
    ):
        """Explicitly correct recorded facts; preserve every original revision and evidence."""
        self._require_direct_registration(kind)
        if recording_error_confirmed is not True:
            raise NeedsInformation("recording_error_confirmed", "需要确认这是原记录的录入错误")
        request_hash, operation = self._registration(
            True, kind, subject_id, data, evidence=evidence, expected_revision=expected_revision
        )
        return self._write(
            request_id,
            digest(["amend_fact", request_hash.hex()]),
            None,
            (self.store.registry.models[kind].lane,),
            "recording_correction",
            operation,
        )

    def save_facts(self, facts: list[dict], *, request_id: str):
        """Confirm a typed source batch atomically, with one accounting epoch increment."""
        if not isinstance(facts, list) or not 1 <= len(facts) <= 5000:
            raise ValueError("a fact batch contains 1..5000 typed records")
        if len({item["subject_id"] for item in facts}) != len(facts):
            raise KernelError("duplicate_subject", "同一来源批次不能重复修改同一业务身份")
        for item in facts:
            self._require_direct_registration(item["kind"])
        registrations = [self._registration(False, **item) for item in facts]
        request_hash = digest(["confirm_facts", [hashed.hex() for hashed, _ in registrations]])

        def operation(connection):
            results = [save(connection) for _, save in registrations]
            return {"status": "confirmed", "results": results}

        return self._write(
            request_id,
            request_hash,
            None,
            tuple(sorted({self.store.registry.models[item["kind"]].lane for item in facts})),
            "confirm_facts",
            operation,
        )

    @staticmethod
    def _scope_consumers(connection, source, kind, scopes, period=0):
        result = set()
        for scope in set(scopes) | {"*"}:
            result.update(
                r[0]
                for r in connection.execute(
                    "SELECT c.subject_id FROM dependency_scope d JOIN calculation_current c "
                    "ON c.calculation_id=d.calculation_id WHERE d.source=? "
                    "AND d.kind IN (?,'*') AND d.scope_key=? "
                    "AND d.before_period>?",
                    (source, kind, scope, period),
                )
            )
        return result

    @staticmethod
    def _descendants(connection, subjects):
        found, queue = set(subjects), list(subjects)
        while queue:
            sid = queue.pop()
            rows = connection.execute(
                "SELECT c.subject_id FROM calculation_current a JOIN dependency_calculation d "
                "ON d.upstream_id=a.calculation_id JOIN calculation_current c "
                "ON c.calculation_id=d.calculation_id WHERE a.subject_id=?",
                (sid,),
            )
            for row in rows:
                if row[0] not in found:
                    found.add(row[0])
                    queue.append(row[0])
        return found

    def _snapshot(self, subjects):
        with self.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            epochs = self.store.epochs(connection)
            subjects = set(subjects)
            queue = list(subjects)
            facts = {}
            while queue:
                sid = queue.pop()
                version = self.store.current_fact(connection, sid)
                facts[sid] = version
                # A calculable fact is also a versioned input. Its fact-range
                # consumers may differ from consumers of its published result.
                extra = {
                    r[0]
                    for r in connection.execute(
                        "SELECT subject_id FROM pending WHERE cause_id=?", (version.id,)
                    )
                }
                if version.fact.kind in self.store.registry.evaluators:
                    extra |= self._scope_consumers(
                        connection,
                        "calculation",
                        version.fact.kind,
                        set(version.fact.scopes_for(sid)) | {"@" + sid, str(version.fact.period)},
                        version.fact.period.ordinal,
                    )
                extra |= self._descendants(connection, {sid})
                for new in extra - subjects:
                    subjects.add(new)
                    queue.append(new)
            facts = {
                sid: v for sid, v in facts.items() if v.fact.kind in self.store.registry.evaluators
            }
            reads = {
                read
                for version in facts.values()
                for read in version.fact.reads_for(version.subject_id)
            }
            selections = {
                read: self.store.select(connection, read) for read in sorted(reads, key=repr)
            }
            pending = {r[0] for r in connection.execute("SELECT DISTINCT subject_id FROM pending")}
            closed = {r[0] for r in connection.execute("SELECT period FROM period_close")}
            connection.commit()
        return epochs, facts, selections, pending, closed

    def _prepare(self, subjects, correction_period=None):
        epochs, facts, selections, pending, closed = self._snapshot(subjects)
        if not facts:
            raise KernelError("no_calculations", "没有可发布的业务计算")
        dependencies = {sid: set() for sid in facts}
        for sid, version in facts.items():
            if any(
                period.ordinal not in closed for period in version.fact.required_closed_periods()
            ):
                raise KernelError("awaiting_close", "该业务须等待所依据期间关账")
            for read in version.fact.reads_for(version.subject_id):
                if read.source != "calculation" or read.key.startswith("#"):
                    continue
                for other, upstream in facts.items():
                    if (
                        other != sid
                        and (upstream.fact.kind == read.kind or read.kind == "*")
                        and read.key
                        in (
                            *upstream.fact.scopes_for(other),
                            "@" + other,
                            str(upstream.fact.period),
                            "*",
                        )
                        and (not read.before_period or upstream.fact.period < read.before_period)
                    ):
                        dependencies[sid].add(other)
                for selected in selections[read]:
                    if selected.subject_id in pending and selected.subject_id not in facts:
                        raise KernelError(
                            "pending_upstream", "上游核算尚待更正", subject_id=selected.subject_id
                        )
        ordered = []
        remaining = set(facts)
        while remaining:
            ready = sorted(
                (sid for sid in remaining if not dependencies[sid] & remaining),
                key=lambda sid: (facts[sid].fact.period.ordinal, sid),
            )
            if not ready:
                raise KernelError("dependency_cycle", "计算依赖形成循环")
            ordered.extend(ready)
            remaining.difference_update(ready)
        prepared, overlays = [], {}
        for sid in ordered:
            version = facts[sid]
            selected = {}
            for read in version.fact.reads_for(version.subject_id):
                values = list(selections[read])
                if read.source == "calculation" and not read.key.startswith("#"):
                    values = [v for v in values if v.subject_id not in overlays]
                    for upstream_id, calc in overlays.items():
                        upstream = facts[upstream_id]
                        if (
                            (calc.kind == read.kind or read.kind == "*")
                            and read.key
                            in (
                                *upstream.fact.scopes_for(upstream_id),
                                "@" + upstream_id,
                                str(upstream.fact.period),
                                "*",
                            )
                            and (not read.before_period or calc.period < read.before_period)
                        ):
                            values.append(calc)
                    values.sort(key=lambda item: (item.period.ordinal, item.subject_id))
                selected[read] = tuple(values)
            context = Context(selected)
            outcome = asdict(self.store.registry.evaluators[version.fact.kind](version, context))
            calculated_hash = digest(
                {
                    "fact": version.id,
                    "outcome": outcome,
                    "reads": [asdict(r) for r in sorted(context.used, key=repr)],
                    "versions": sorted(context.versions),
                    "program": PROGRAM_VERSION,
                }
            )
            calc_id = "c_" + calculated_hash.hex()
            prepared.append(Prepared(version, calc_id, outcome, context))
            overlays[sid] = Calculation(
                calc_id,
                sid,
                version.fact.kind,
                version.fact.period,
                outcome["values"],
                version.id,
                digest(outcome).hex(),
            )
        posting = YearMonth(correction_period) if correction_period is not None else None
        if posting and posting.ordinal <= max(closed, default=-1):
            raise KernelError("closed_period", "冲正必须发布到开放期间")
        checked_lanes = {"accounting"}
        for item in prepared:
            checked_lanes.add(item.version.fact.lane)
            for read in item.context.used:
                if read.kind in self.store.registry.models:
                    checked_lanes.add(self.store.registry.models[read.kind].lane)
                for selected in item.context.selections[read]:
                    kind = (
                        selected.fact.kind if isinstance(selected, FactVersion) else selected.kind
                    )
                    checked_lanes.add(self.store.registry.models[kind].lane)
        public = {
            "subjects": sorted(facts),
            "epochs": epochs,
            "correction_period": correction_period,
            "results": [
                {
                    "subject_id": p.version.subject_id,
                    "calculation_id": p.calculation_id,
                    "kind": p.version.fact.kind,
                    "period": str(p.version.fact.period),
                    "fact_id": p.version.id,
                    "result_digest": digest(p.outcome).hex(),
                    "values": p.outcome["values"],
                    "balances": p.outcome["balances"],
                    "explanation": p.outcome["explanation"],
                    "lines": p.outcome["lines"],
                    "opening_lines": p.outcome["opening_lines"],
                    "opening": p.outcome["opening"],
                }
                for p in prepared
            ],
            "company_id": self.store.company_id,
            "database_id": self.store.database_id,
            "checked_lanes": sorted(checked_lanes),
        }
        public["digest"] = digest(
            {**public, "epochs": {lane: epochs[lane] for lane in checked_lanes}}
        ).hex()
        return public, prepared

    def preview(self, subjects: list[str], *, correction_period: str | None = None):
        return {"status": "preview", **self._prepare(subjects, correction_period)[0]}

    def confirm(
        self,
        subjects: list[str],
        *,
        preview_digest: str,
        epochs: dict,
        request_id: str,
        correction_period: str | None = None,
    ):
        request_hash = digest(
            ["publish", sorted(subjects), preview_digest, epochs, correction_period]
        )
        cached = self._cached(request_id, request_hash)
        if cached is not None:
            return cached
        public, prepared = self._prepare(subjects, correction_period)
        if public["digest"] != preview_digest or public["epochs"]["accounting"] != epochs.get(
            "accounting"
        ):
            raise KernelError("preview_expired", "计算结果与已审阅的预览不同")

        def operation(connection):
            results = [self._publish(connection, item, correction_period) for item in prepared]
            return {"status": "published", "results": results, "digest": preview_digest}

        lanes = tuple(sorted({item.version.fact.lane for item in prepared}))
        return self._write(
            request_id,
            request_hash,
            epochs,
            lanes,
            "publish",
            operation,
            checked_lanes=public["checked_lanes"],
        )

    def _publish(self, connection, prepared, correction_period):
        version, cid, outcome = prepared.version, prepared.calculation_id, prepared.outcome
        old = connection.execute(
            "SELECT c.* FROM calculation_current a JOIN calculation c ON c.id=a.calculation_id "
            "WHERE a.subject_id=?",
            (version.subject_id,),
        ).fetchone()
        old_outcome = json.loads(old["outcome"]) if old else None
        old_publication = (
            connection.execute(
                "SELECT * FROM calculation_publication WHERE calculation_id=?", (old["id"],)
            ).fetchone()
            if old
            else None
        )
        outcome_digest = digest(outcome)
        no_impact = (
            old is not None
            and old["digest"] == outcome_digest
            and old["period"] == version.fact.period.ordinal
        )
        if outcome.get("opening") and old is None:
            if (
                connection.execute("SELECT 1 FROM voucher LIMIT 1").fetchone()
                or connection.execute("SELECT 1 FROM period_close LIMIT 1").fetchone()
            ):
                raise KernelError(
                    "opening_after_accounting", "期初必须在首次正式业务入账和关账之前确认"
                )
            if connection.execute(
                "SELECT 1 FROM calculation_current a JOIN calculation c ON c.id=a.calculation_id "
                "WHERE json_extract(c.outcome,'$.opening')=1 LIMIT 1"
            ).fetchone():
                raise KernelError("opening_already_published", "公司已存在正式期初接续状态")
        if not no_impact and (outcome.get("opening") or (old_outcome or {}).get("opening")):
            earliest = min(
                version.fact.period.ordinal, old["period"] if old else version.fact.period.ordinal
            )
            if connection.execute(
                "SELECT 1 FROM period_close WHERE period>=?", (earliest,)
            ).fetchone():
                raise KernelError(
                    "closed_opening_immutable",
                    "已关账期初状态保持冻结，差异须在开放期通过类型化更正处理",
                )
        posting = version.fact.period.ordinal
        voucher_key = old_publication["voucher_id"] if old_publication else None
        if (
            old_publication
            and connection.execute(
                "SELECT 1 FROM period_close WHERE period>=?", (posting,)
            ).fetchone()
        ):
            posting = old_publication["posting_period"]
        is_new = not connection.execute("SELECT 1 FROM calculation WHERE id=?", (cid,)).fetchone()
        if is_new:
            connection.execute(
                "INSERT INTO calculation VALUES(?,?,?,?,?,?,?,?)",
                (
                    cid,
                    version.subject_id,
                    version.id,
                    version.fact.kind,
                    version.fact.period.ordinal,
                    canonical(outcome),
                    outcome_digest,
                    PROGRAM_VERSION,
                ),
            )
            connection.executemany(
                "INSERT INTO calculation_scope VALUES(?,?,?)",
                [
                    (cid, version.fact.kind, key)
                    for key in sorted(
                        set(version.fact.scopes_for(version.subject_id))
                        | {"@" + version.subject_id, str(version.fact.period)}
                    )
                ],
            )
            connection.executemany(
                "INSERT INTO dependency_scope VALUES(?,?,?,?,?)",
                [
                    (
                        cid,
                        r.source,
                        r.kind,
                        r.key,
                        r.before_period.ordinal if r.before_period else 119988,
                    )
                    for r in sorted(prepared.context.used, key=repr)
                ],
            )
            fact_ids = {version.id}
            calc_ids = set()
            for read in prepared.context.used:
                for item in prepared.context.selections[read]:
                    (fact_ids if read.source == "fact" else calc_ids).add(item.id)
            connection.executemany(
                "INSERT INTO dependency_fact VALUES(?,?)", [(cid, fid) for fid in sorted(fact_ids)]
            )
            connection.executemany(
                "INSERT INTO dependency_calculation VALUES(?,?)",
                [(cid, upstream) for upstream in sorted(calc_ids) if upstream != cid],
            )
        voucher_number = None
        if no_impact:
            # The accounting result is unchanged. Keep its sealed voucher, while
            # publishing the reviewed facts and dependencies as a new calculation.
            posting = old_publication["posting_period"]
            if voucher_key:
                number = connection.execute(
                    "SELECT number FROM voucher WHERE id=?", (voucher_key,)
                ).fetchone()
                voucher_number = number[0] if number else None
        elif old is None or old["id"] != cid:
            header = (
                connection.execute(
                    "SELECT v.*,s.number FROM voucher_version v JOIN "
                    "voucher_current a ON a.version_id=v.id "
                    "JOIN voucher s ON s.id=v.voucher_id WHERE "
                    "v.voucher_id=? AND v.reverses_id IS NULL",
                    (voucher_key,),
                ).fetchone()
                if old
                else None
            )
            if header:
                is_closed = connection.execute(
                    "SELECT 1 FROM period_close WHERE period>=?", (header["period"],)
                ).fetchone()
                if is_closed:
                    if correction_period is None:
                        raise KernelError(
                            "closed_correction_required", "已关账结果须指定开放期冲正"
                        )
                    posting = YearMonth(correction_period).ordinal
                    voucher_key = cid + ":replacement"
                    original = [
                        dict(r)
                        for r in connection.execute(
                            "SELECT account,debit,credit,cashflow FROM voucher_line "
                            "WHERE version_id=? ORDER BY line_no",
                            (header["id"],),
                        )
                    ]
                    reversed_lines = [
                        {**r, "debit": r["credit"], "credit": r["debit"]} for r in original
                    ]
                    self._journal(
                        connection,
                        cid + ":reverse",
                        cid,
                        posting,
                        reversed_lines,
                        reverses_id=header["id"],
                    )
                    if outcome["lines"]:
                        voucher_number = self._journal(
                            connection, cid + ":replacement", cid, posting, outcome["lines"]
                        )
                else:
                    voucher_key = header["voucher_id"]
                    original = [
                        dict(r)
                        for r in connection.execute(
                            "SELECT account,debit,credit,cashflow FROM voucher_line "
                            "WHERE version_id=?",
                            (header["id"],),
                        )
                    ]
                    self._journal_projection(connection, header["period"], original, -1)
                    if outcome["lines"]:
                        voucher_number = self._journal(
                            connection, header["voucher_id"], cid, posting, outcome["lines"]
                        )
                    else:
                        connection.execute(
                            "DELETE FROM voucher_current WHERE voucher_id=?",
                            (header["voucher_id"],),
                        )
            elif outcome["lines"]:
                if connection.execute(
                    "SELECT 1 FROM period_close WHERE period>=?", (posting,)
                ).fetchone():
                    if correction_period is None:
                        raise KernelError(
                            "closed_correction_required", "已关账结果须指定开放期冲正"
                        )
                    posting, voucher_key = (
                        YearMonth(correction_period).ordinal,
                        cid + ":replacement",
                    )
                voucher_key = voucher_key or "v:" + version.subject_id
                voucher_number = self._journal(
                    connection, voucher_key, cid, posting, outcome["lines"]
                )
            if old_outcome:
                self._opening_projection(
                    connection, old["period"], old_outcome.get("opening_lines", ()), -1
                )
                self._balance_projection(connection, old_outcome["balances"], -1)
            self._opening_projection(
                connection, version.fact.period.ordinal, outcome.get("opening_lines", ()), 1
            )
            self._balance_projection(connection, outcome["balances"], 1)
        if is_new:
            if voucher_key:
                self._reserve_voucher(connection, voucher_key)
            connection.execute(
                "INSERT INTO calculation_publication VALUES(?,?,?)", (cid, posting, voucher_key)
            )
            connection.execute("INSERT INTO calculation_seal VALUES(?)", (cid,))
        connection.execute(
            "INSERT INTO calculation_current VALUES(?,?) ON CONFLICT(subject_id) "
            "DO UPDATE SET calculation_id=excluded.calculation_id",
            (version.subject_id, cid),
        )
        causes = connection.execute(
            "SELECT cause_id FROM pending WHERE subject_id=?", (version.subject_id,)
        ).fetchall()
        connection.executemany(
            "INSERT INTO "
            "disposition(subject_id,cause_id,action,calculation_id,e"
            "xplanation) VALUES(?,?,?,?,?)",
            [
                (
                    version.subject_id,
                    row[0],
                    "review_no_impact" if no_impact else "recalculated",
                    cid,
                    "明确复核并发布当前事实的计算结果",
                )
                for row in causes
            ],
        )
        connection.execute("DELETE FROM pending WHERE subject_id=?", (version.subject_id,))
        self.fault("calculation", connection)
        return {
            "subject_id": version.subject_id,
            "calculation_id": cid,
            "voucher_number": voucher_number,
        }

    @staticmethod
    def _reserve_voucher(connection, voucher_id):
        row = connection.execute("SELECT number FROM voucher WHERE id=?", (voucher_id,)).fetchone()
        if row:
            number = row[0]
        else:
            number = connection.execute("SELECT next_number FROM state WHERE id=1").fetchone()[0]
            connection.execute("UPDATE state SET next_number=next_number+1 WHERE id=1")
            connection.execute("INSERT INTO voucher VALUES(?,?)", (voucher_id, number))
        return number

    def _journal(self, connection, voucher_id, calculation_id, period, lines, reverses_id=None):
        number = self._reserve_voucher(connection, voucher_id)
        version_id = "v_" + digest([voucher_id, calculation_id, period, reverses_id]).hex()
        total = sum_fen(line["debit"] for line in lines)
        connection.executemany(
            "INSERT INTO voucher_line VALUES(?,?,?,?,?,?)",
            [
                (
                    version_id,
                    i,
                    line["account"],
                    line["debit"],
                    line["credit"],
                    line.get("cashflow"),
                )
                for i, line in enumerate(lines, 1)
            ],
        )
        self.fault("lines", connection)
        connection.execute(
            "INSERT INTO voucher_version VALUES(?,?,?,?,?,?)",
            (version_id, voucher_id, calculation_id, period, reverses_id, total),
        )
        connection.execute(
            "INSERT INTO voucher_current VALUES(?,?) ON CONFLICT(voucher_id) "
            "DO UPDATE SET version_id=excluded.version_id",
            (voucher_id, version_id),
        )
        self._journal_projection(connection, period, lines, 1)
        return number

    @staticmethod
    def _journal_projection(connection, period, lines, sign):
        accounts, cashflows = {}, {}
        for line in lines:
            debit, credit = accounts.setdefault(line["account"], [0, 0])
            accounts[line["account"]] = [
                checked(debit + line["debit"]),
                checked(credit + line["credit"]),
            ]
            if line.get("cashflow"):
                category = line["cashflow"]
                cashflows[category] = checked(
                    cashflows.get(category, 0) + line["debit"] - line["credit"]
                )
        for account, (debit, credit) in accounts.items():
            previous = connection.execute(
                "SELECT debit,credit FROM monthly_account WHERE period=? AND account=?",
                (period, account),
            ).fetchone() or (0, 0)
            totals = (checked(previous[0] + sign * debit), checked(previous[1] + sign * credit))
            if totals == (0, 0):
                connection.execute(
                    "DELETE FROM monthly_account WHERE period=? AND account=?", (period, account)
                )
                continue
            connection.execute(
                "INSERT INTO monthly_account VALUES(?,?,?,?) ON CONFLICT(period,account) "
                "DO UPDATE SET debit=excluded.debit,credit=excluded.credit",
                (period, account, *totals),
            )
        for category, amount in cashflows.items():
            previous = connection.execute(
                "SELECT amount FROM monthly_cashflow WHERE period=? AND category=?",
                (period, category),
            ).fetchone()
            amount = checked((previous[0] if previous else 0) + sign * amount)
            if amount == 0:
                connection.execute(
                    "DELETE FROM monthly_cashflow WHERE period=? AND category=?", (period, category)
                )
                continue
            connection.execute(
                "INSERT INTO monthly_cashflow VALUES(?,?,?) ON CONFLICT(period,category) "
                "DO UPDATE SET amount=excluded.amount",
                (period, category, amount),
            )

    @staticmethod
    def _balance_projection(connection, balances, sign):
        for balance in balances:
            key, category = balance["key"], balance["category"]
            old = connection.execute(
                "SELECT amount FROM balance WHERE category=? AND balance_key=?", (category, key)
            ).fetchone()
            amount = checked((old[0] if old else 0) + sign * balance["amount"])
            if amount == 0:
                connection.execute(
                    "DELETE FROM balance WHERE category=? AND balance_key=?", (category, key)
                )
                continue
            connection.execute(
                "INSERT INTO balance VALUES(?,?,?) ON CONFLICT(category,balance_key) "
                "DO UPDATE SET amount=excluded.amount",
                (category, key, amount),
            )

    @staticmethod
    def _opening_projection(connection, period, lines, sign):
        for line in lines:
            row = connection.execute(
                "SELECT debit,credit FROM opening_account WHERE period=? AND account=?",
                (period, line["account"]),
            ).fetchone()
            debit = checked((row[0] if row else 0) + sign * line["debit"])
            credit = checked((row[1] if row else 0) + sign * line["credit"])
            if debit == credit == 0:
                connection.execute(
                    "DELETE FROM opening_account WHERE period=? AND account=?",
                    (period, line["account"]),
                )
            else:
                connection.execute(
                    "INSERT INTO opening_account VALUES(?,?,?,?) ON CONFLICT(period,account) "
                    "DO UPDATE SET debit=excluded.debit,credit=excluded.credit",
                    (period, line["account"], debit, credit),
                )

    def rebuild_projections(self, *, request_id: str):
        def operation(connection):
            for table in ("monthly_account", "monthly_cashflow", "balance", "opening_account"):
                connection.execute(f"DELETE FROM {table}")
            # SQLite sum(INTEGER) raises on overflow; total() or floating arithmetic is forbidden.
            connection.execute(
                "INSERT INTO monthly_account SELECT v.period,l.account,sum(l.debit),sum(l.credit) "
                "FROM voucher_current c JOIN voucher_version v ON v.id=c.version_id "
                "JOIN voucher_line l ON l.version_id=v.id GROUP BY v.period,l.account"
            )
            connection.execute(
                "INSERT INTO monthly_cashflow SELECT v.period,l.cashflow,sum(l.debit-l.credit) "
                "FROM voucher_current c JOIN voucher_version v ON v.id=c.version_id "
                "JOIN voucher_line l ON l.version_id=v.id WHERE l.cashflow IS NOT NULL "
                "GROUP BY v.period,l.cashflow HAVING sum(l.debit-l.credit)<>0"
            )
            balances = {}
            for row in connection.execute(
                "SELECT outcome,c.period FROM calculation_current a JOIN "
                "calculation c ON c.id=a.calculation_id"
            ):
                outcome = json.loads(row[0])
                self._opening_projection(connection, row[1], outcome.get("opening_lines", ()), 1)
                for effect in outcome["balances"]:
                    key = (effect["category"], effect["key"])
                    balances[key] = checked(balances.get(key, 0) + effect["amount"])
            connection.executemany(
                "INSERT INTO balance VALUES(?,?,?)",
                [(category, key, amount) for (category, key), amount in balances.items() if amount],
            )
            return {"status": "rebuilt"}

        return self._write(
            request_id, digest(["rebuild"]), None, (), "rebuild_projections", operation
        )

    def overview(self, period: str):
        month = YearMonth(period).ordinal
        with self.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            result = {
                "period": period,
                "epochs": self.store.epochs(connection),
                "opening_balances": [
                    dict(r)
                    for r in connection.execute(
                        "SELECT account,sum(debit) debit,sum(credit) credit FROM opening_account "
                        "WHERE period<=? GROUP BY account ORDER BY account",
                        (month,),
                    )
                ],
                "accounts": [
                    dict(r)
                    for r in connection.execute(
                        "SELECT account,debit,credit FROM monthly_account WHERE "
                        "period=? ORDER BY account",
                        (month,),
                    )
                ],
                "cashflow": [
                    dict(r)
                    for r in connection.execute(
                        "SELECT category,amount FROM monthly_cashflow WHERE "
                        "period=? ORDER BY category",
                        (month,),
                    )
                ],
                "pending": [
                    dict(r)
                    for r in connection.execute(
                        "SELECT p.subject_id,s.kind,count(*) causes FROM "
                        "pending p CROSS JOIN subject s "
                        "CROSS JOIN fact_current fc CROSS JOIN fact_revision f "
                        "WHERE s.id=p.subject_id AND fc.subject_id=p.subject_id "
                        "AND f.id=fc.fact_id "
                        "AND f.period=? GROUP BY p.subject_id ORDER BY p.subject_id LIMIT 50",
                        (month,),
                    )
                ],
            }
            connection.commit()
            return result

    def queue_backup(self, directory: str, *, request_id: str, rollover: bool = False):
        """Commit durable intent only; the resident worker performs file I/O afterward."""
        from pathlib import Path

        if not directory.strip() or type(rollover) is not bool:
            raise ValueError("invalid backup destination")
        payload = {"directory": str(Path(directory).expanduser().resolve()), "rollover": rollover}

        def operation(connection):
            job_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO jobs(id,kind,payload,status) VALUES(?,?,?,'pending')",
                (job_id, "portable_backup", canonical(payload)),
            )
            return {"status": "pending", "job_id": job_id}

        return self._write(request_id, digest(["backup", payload]), None, (), "backup", operation)

    def retry_job(self, job_id: str, *, request_id: str):
        """An explicit retry starts one new bounded attempt cycle for a failed job."""
        from .backup import _worker_lock

        def operation(connection):
            row = connection.execute(
                "SELECT status,attempts,last_error FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise KernelError("unknown_job", "后台任务不存在")
            if row["status"] != "failed":
                raise KernelError("job_not_failed", "仅失败的任务可以重新尝试")
            connection.execute(
                "UPDATE jobs SET status='pending',attempts=0,last_error=NULL,result=NULL "
                "WHERE id=?",
                (job_id,),
            )
            return {
                "status": "pending",
                "job_id": job_id,
                "previous_attempts": row["attempts"],
                "previous_error": row["last_error"],
            }

        with _worker_lock(self.store.path) as acquired:
            if not acquired:
                raise KernelError("job_running", "后台任务仍在执行，请稍后查看状态")
            return self._write(
                request_id, digest(["retry_job", job_id]), None, (), "retry_job", operation
            )

    def jobs(self, *, status: str | None = None, limit: int = 50, job_id: str | None = None):
        if status not in (None, "pending", "running", "succeeded", "failed"):
            raise ValueError("unknown job status")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("job limit must be 1..100")
        if job_id is not None and (not isinstance(job_id, str) or not 1 <= len(job_id) <= 200):
            raise ValueError("invalid job identity")
        with self.store.connection(read_only=True) as connection:
            rows = connection.execute(
                "SELECT id,kind,status,attempts,last_error,result FROM jobs "
                "WHERE (? IS NULL OR status=?) AND (? IS NULL OR id=?) "
                "ORDER BY rowid DESC LIMIT ?",
                (status, status, job_id, job_id, limit),
            )
            return [
                {**dict(row), "result": json.loads(row["result"]) if row["result"] else None}
                for row in rows
            ]

    def ledger(self, period: str, *, after_number: int = 0, limit: int = 100):
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("page limit must be 1..500")
        with self.store.connection(read_only=True) as connection:
            rows = connection.execute(
                "SELECT "
                "v.number,h.id,h.period,h.calculation_id,h.total,h.rever"
                "ses_id,c.kind FROM voucher v "
                "JOIN voucher_current a ON a.voucher_id=v.id JOIN "
                "voucher_version h ON h.id=a.version_id "
                "JOIN calculation c ON c.id=h.calculation_id "
                "WHERE h.period=? AND v.number>? ORDER BY v.number LIMIT ?",
                (YearMonth(period).ordinal, after_number, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def trace(self, calculation_id: str):
        with self.store.connection(read_only=True) as connection:
            row = connection.execute(
                "SELECT * FROM calculation WHERE id=?", (calculation_id,)
            ).fetchone()
            if not row:
                raise KernelError("unknown_calculation", "计算版本不存在")
            facts = [
                self.store.fact(connection, r[0])
                for r in connection.execute(
                    "SELECT fact_id FROM dependency_fact WHERE calculation_id=? ORDER BY fact_id",
                    (calculation_id,),
                )
            ]
            return {
                "calculation": {
                    **dict(row),
                    "digest": row["digest"].hex(),
                    "outcome": json.loads(row["outcome"]),
                },
                "facts": [
                    {
                        "id": f.id,
                        "subject_id": f.subject_id,
                        "revision": f.revision,
                        "kind": f.fact.kind,
                        "data": f.fact.model_dump(mode="json"),
                        "evidence": list(f.evidence),
                    }
                    for f in facts
                ],
                "upstream": [
                    r[0]
                    for r in connection.execute(
                        "SELECT upstream_id FROM dependency_calculation WHERE calculation_id=?",
                        (calculation_id,),
                    )
                ],
            }

    def preview_delete(self, subject_id: str, *, recording_error_evidence: str | None = None):
        with self.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            fact = self.store.current_fact(connection, subject_id)
            if fact.fact.immutable and recording_error_evidence is None:
                raise KernelError("immutable_fact", "实际资金或原始流水须通过明确的后续业务处理")
            if (
                recording_error_evidence is not None
                and not connection.execute(
                    "SELECT 1 FROM evidence WHERE digest=?",
                    (bytes.fromhex(recording_error_evidence),),
                ).fetchone()
            ):
                raise NeedsInformation("recording_error_evidence", "需要留存误记撤销的明确依据")
            row = connection.execute(
                "SELECT c.* FROM calculation_current a JOIN calculation c "
                "ON c.id=a.calculation_id WHERE a.subject_id=?",
                (subject_id,),
            ).fetchone()
            # Publication owns the posting month even when the calculation produced
            # no voucher or a later review reused an earlier voucher unchanged.
            closed = connection.execute(
                "SELECT 1 FROM calculation c JOIN calculation_publication p "
                "ON p.calculation_id=c.id WHERE c.subject_id=? "
                "AND p.posting_period<=(SELECT max(period) FROM period_close) LIMIT 1",
                (subject_id,),
            ).fetchone()
            if (
                closed
                or connection.execute(
                    "SELECT 1 FROM period_close WHERE period>=?", (fact.fact.period.ordinal,)
                ).fetchone()
            ):
                raise KernelError("closed_period", "已关账业务只能通过关联冲正更正")
            consumers = {
                r[0]
                for r in connection.execute(
                    "SELECT a.subject_id FROM dependency_fact d JOIN calculation_current a "
                    "ON a.calculation_id=d.calculation_id JOIN fact_revision f ON f.id=d.fact_id "
                    "WHERE f.subject_id=? UNION SELECT a.subject_id FROM dependency_calculation d "
                    "JOIN calculation_current a ON a.calculation_id=d.calculation_id "
                    "JOIN calculation c ON c.id=d.upstream_id WHERE c.subject_id=?",
                    (subject_id, subject_id),
                )
            }
            consumers.discard(subject_id)
            # Initial publications and replacements have no new calculation edges
            # yet. Inspect only pending subjects' CURRENT declared direct reads;
            # archived facts/calculations must never become permanent blockers.
            for candidate in connection.execute(
                "SELECT DISTINCT f.fact_id FROM pending p CROSS JOIN fact_current f "
                "WHERE f.subject_id=p.subject_id AND p.subject_id!=?",
                (subject_id,),
            ).fetchall():
                dependent = self.store.fact(connection, candidate[0])
                for read in dependent.fact.reads_for(dependent.subject_id):
                    selected_period = (
                        row["period"]
                        if read.source == "calculation" and row is not None
                        else fact.fact.period.ordinal
                    )
                    if (
                        read.key == "@" + subject_id
                        and read.kind in (fact.fact.kind, "*")
                        and (
                            read.before_period is None
                            or selected_period < read.before_period.ordinal
                        )
                    ):
                        consumers.add(dependent.subject_id)
                        break
            if consumers:
                raise KernelError(
                    "has_dependents", "仍有有效下游业务，不能撤去", subjects=sorted(consumers)
                )
            epochs = self.store.epochs(connection)
            result = {
                "subject_id": subject_id,
                "fact_id": fact.id,
                "calculation_id": row["id"] if row else None,
                "epochs": {"accounting": epochs["accounting"]},
                "recording_error_evidence": recording_error_evidence,
            }
            connection.commit()
        return {"status": "preview", **result, "digest": digest(result).hex()}

    def delete(
        self,
        subject_id: str,
        *,
        preview_digest: str,
        epochs: dict,
        request_id: str,
        recording_error_evidence: str | None = None,
    ):
        request_hash = digest(
            ["delete", subject_id, preview_digest, epochs, recording_error_evidence]
        )
        cached = self._cached(request_id, request_hash)
        if cached is not None:
            return cached
        preview = self.preview_delete(subject_id, recording_error_evidence=recording_error_evidence)
        if preview["digest"] != preview_digest:
            raise KernelError("preview_expired", "撤去业务预览已变化")

        def operation(connection):
            calc = preview["calculation_id"]
            if calc:
                for header in connection.execute(
                    "SELECT v.* FROM voucher_current a JOIN voucher_version v "
                    "ON v.id=a.version_id WHERE v.calculation_id=? OR v.voucher_id IN "
                    "(SELECT voucher_id FROM calculation_publication WHERE calculation_id=?)",
                    (calc, calc),
                ).fetchall():
                    lines = [
                        dict(r)
                        for r in connection.execute(
                            "SELECT account,debit,credit,cashflow FROM voucher_line "
                            "WHERE version_id=?",
                            (header["id"],),
                        )
                    ]
                    self._journal_projection(connection, header["period"], lines, -1)
                    connection.execute(
                        "DELETE FROM voucher_current WHERE voucher_id=?", (header["voucher_id"],)
                    )
                calculation = connection.execute(
                    "SELECT outcome,period FROM calculation WHERE id=?", (calc,)
                ).fetchone()
                outcome = json.loads(calculation[0])
                self._opening_projection(
                    connection, calculation[1], outcome.get("opening_lines", ()), -1
                )
                self._balance_projection(connection, outcome["balances"], -1)
                connection.execute(
                    "DELETE FROM calculation_current WHERE subject_id=?", (subject_id,)
                )
            connection.execute(
                "INSERT INTO disposition(subject_id,cause_id,action,calculation_id,explanation) "
                "VALUES(?,?,'withdrawn',?,'撤去无有效下游的开放期误记业务')",
                (subject_id, preview["fact_id"], calc),
            )
            connection.execute("DELETE FROM pending WHERE subject_id=?", (subject_id,))
            connection.execute("DELETE FROM fact_current WHERE subject_id=?", (subject_id,))
            return {
                "status": "withdrawn",
                "subject_id": subject_id,
                "retained_calculation_id": calc,
                "recording_error_evidence": recording_error_evidence,
            }

        return self._write(request_id, request_hash, epochs, ("accounting",), "withdraw", operation)
