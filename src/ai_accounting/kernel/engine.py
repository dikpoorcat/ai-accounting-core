"""One publisher for every domain. Computation finishes before BEGIN IMMEDIATE."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from contextlib import nullcontext
from dataclasses import asdict, dataclass, replace

from pydantic import ValidationError

from .accounting import AccountingBook, compatibility
from .build import calculator_build_id
from .contracts import Calculation, Context, FactVersion, KernelError, NeedsInformation
from .dependencies import calculation_matches, checked_lanes, read_matches, scope_keys
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
    calculation_id: str | None
    outcome: dict | None
    context: Context
    previous_calculation_id: str | None
    accounting: dict | None
    impact: str
    compatibility_issue: dict | None = None
    publication: dict | None = None
    explicit: bool = True


class Engine:
    _allows_asset_graph = False

    def _reads(self, version):
        return version.fact.reads_for(version.subject_id)

    def _explicit_roots(self, subjects):
        return set(subjects)

    def _selection_enabled(self, subject_id):
        return True

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
                audit = connection.execute(
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
                from .read_indexes import sync_audit

                sync_audit(connection, audit.lastrowid)
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
        review=None,
        source_locations=(),
    ):
        from .duplicates import DuplicateReview, SourceLocation

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
        locations = tuple(SourceLocation.model_validate(item) for item in source_locations)
        review = (
            DuplicateReview.model_validate_json(canonical(review)) if review is not None else None
        )
        payload = [
            kind,
            subject_id,
            fact.model_dump(mode="json"),
            sorted(set(evidence)),
            expected_revision,
            [item.model_dump(mode="json") for item in locations],
            review.model_dump(mode="json") if review is not None else None,
        ]
        request_hash = digest(["save_fact", payload])

        def operation(
            connection,
            *,
            duplicate_prepared=None,
            fact_ids_by_subject=None,
        ):
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
            from .duplicates import DuplicateCandidates
            from .entity_references import references_for, validate_entity_references

            references = validate_entity_references(connection, fact, subject_id)
            if old:
                previous_entities = {
                    item["path"]: item["entity_id"]
                    for item in references_for(old.fact, subject_id)
                    if item["reference_type"] == "entity"
                }
                changed_entities = [
                    item["path"]
                    for item in references
                    if item["reference_type"] == "entity"
                    and item["path"] in previous_entities
                    and previous_entities[item["path"]] != item["entity_id"]
                ]
                if changed_entities:
                    raise KernelError(
                        "identity_correction_required",
                        "对象身份变化须先预览并确认身份纠错，不能通过普通事实修改绕过",
                        fields=sorted(changed_entities),
                    )
            duplicates = DuplicateCandidates(self.store)
            duplicate_prepared = duplicate_prepared or duplicates.prepare(
                connection,
                subject_id=subject_id,
                revision=revision + 1,
                fact=fact,
                evidence=evidence,
                source_locations=locations,
            )
            disposition = duplicates.require_review(duplicate_prepared, review)
            if disposition is not None and disposition.action == "reuse_existing":
                if old is not None:
                    raise KernelError(
                        "duplicate_reuse_requires_new_business",
                        "既有业务的录入纠错不能改为复用另一业务，请使用受控替代流程",
                    )
                checked = duplicates.record_check(
                    connection,
                    prepared=duplicate_prepared,
                    result_fact_id=None,
                    review=disposition,
                    fact_ids_by_subject=fact_ids_by_subject,
                )
                selected = self.store.fact(connection, checked["selected_fact_id"])
                return {
                    "status": "reused",
                    "subject_id": selected.subject_id,
                    "fact_id": selected.id,
                    "revision": selected.revision,
                    "requested_subject_id": subject_id,
                    "duplicate_check_id": checked["check_id"],
                    "pending": [],
                }
            version = FactVersion(
                uuid.uuid4().hex, subject_id, revision + 1, fact, tuple(sorted(set(evidence)))
            )
            self.store.write_fact(connection, version, digest(fact.model_dump(mode="json")))
            scopes = set(scope_keys("fact", fact, subject_id))
            if old:
                scopes.update(
                    row[0]
                    for row in connection.execute(
                        "SELECT scope_key FROM fact_scope WHERE fact_id=?", (old.id,)
                    )
                )
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
            checked = None
            if duplicates.eligible(kind):
                checked = duplicates.record_check(
                    connection,
                    prepared=duplicate_prepared,
                    result_fact_id=version.id,
                    review=disposition,
                    fact_ids_by_subject=fact_ids_by_subject,
                )
            return {
                "status": "confirmed",
                "subject_id": subject_id,
                "fact_id": version.id,
                "revision": version.revision,
                "pending": sorted(affected),
                **({"duplicate_check_id": checked["check_id"]} if checked is not None else {}),
            }

        operation.duplicate_proposal = {
            "subject_id": subject_id,
            "revision": expected_revision + 1,
            "fact": fact,
            "evidence": tuple(sorted(set(evidence))),
            "source_locations": locations,
        }
        operation.duplicate_review = review

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
        review=None,
        source_locations=(),
    ):
        self._require_direct_registration(kind)
        request_hash, operation = self._registration(
            False,
            kind,
            subject_id,
            data,
            evidence=evidence,
            expected_revision=expected_revision,
            review=review,
            source_locations=source_locations,
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
        review=None,
        source_locations=(),
    ):
        """Explicitly correct recorded facts; preserve every original revision and evidence."""
        self._require_direct_registration(kind)
        if recording_error_confirmed is not True:
            raise NeedsInformation("recording_error_confirmed", "需要确认这是原记录的录入错误")
        request_hash, operation = self._registration(
            True,
            kind,
            subject_id,
            data,
            evidence=evidence,
            expected_revision=expected_revision,
            review=review,
            source_locations=source_locations,
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
            from .duplicates import DuplicateCandidates

            duplicates = DuplicateCandidates(self.store)
            prepared = duplicates.prepare_batch(
                connection, [save.duplicate_proposal for _, save in registrations]
            )
            results = []
            fact_ids_by_subject = {}
            for (_, save), candidate in zip(registrations, prepared, strict=True):
                result = save(
                    connection,
                    duplicate_prepared=candidate,
                    fact_ids_by_subject=fact_ids_by_subject,
                )
                results.append(result)
                if result["status"] == "confirmed":
                    fact_ids_by_subject[save.duplicate_proposal["subject_id"]] = result["fact_id"]
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
                        scope_keys("calculation", version.fact, sid),
                        version.fact.period.ordinal,
                    )
                extra |= self._descendants(connection, {sid})
                for new in extra - subjects:
                    subjects.add(new)
                    queue.append(new)
            facts = {
                sid: v for sid, v in facts.items() if v.fact.kind in self.store.registry.evaluators
            }
            from .asset_batch_models import MEMBER_KINDS, OWNER_KINDS

            if not self._allows_asset_graph and any(
                v.fact.kind in MEMBER_KINDS | OWNER_KINDS for v in facts.values()
            ):
                raise KernelError(
                    "asset_batch_command_required", "包含资产卡片或汇总的变更须通过资产专用编排预览"
                )
            reads = {read for version in facts.values() for read in self._reads(version)}
            selections = self.store.select_many(connection, sorted(reads, key=repr))
            pending = {r[0] for r in connection.execute("SELECT DISTINCT subject_id FROM pending")}
            closed = {r[0] for r in connection.execute("SELECT period FROM period_close")}
            previous = {
                sid: row[0]
                if (
                    row := connection.execute(
                        "SELECT calculation_id FROM calculation_current WHERE subject_id=?", (sid,)
                    ).fetchone()
                )
                else None
                for sid in facts
            }
            accounting = AccountingBook(self.store.registry)
            accounting.facts.update((v.id, v) for v in facts.values())
            accounting.facts.update(
                (v.id, v)
                for rows in selections.values()
                for v in rows
                if isinstance(v, FactVersion)
            )
            comparison_ids = {cid for cid in previous.values() if cid is not None}
            for version in facts.values():
                if version.fact.kind in self.store.registry.accounting_consumers:
                    selector = self.store.registry.accounting_consumers[version.fact.kind]
                    required = selector(version) if selector is not None else self._reads(version)
                    comparison_ids.update(
                        calc.id
                        for read in required
                        if read.source == "calculation"
                        for calc in selections[read]
                    )
            accounting.load(self.store, connection, comparison_ids)
            from .publication import heads

            publications = heads(connection, facts)
            from .duplicates import DuplicateCandidates

            duplicate_subjects = self._explicit_roots(facts)
            if duplicate_subjects:
                DuplicateCandidates(self.store).require_publishable(connection, duplicate_subjects)
            connection.commit()
        return epochs, facts, selections, pending, closed, previous, accounting, publications

    def _evaluate(self, version, context):
        return asdict(self.store.registry.evaluators[version.fact.kind](version, context))

    def _prepare(self, subjects, posting_period=None):
        (epochs, facts, selections, pending, closed, previous, accounting, publications) = (
            self._snapshot(subjects)
        )
        if not facts:
            raise KernelError("no_calculations", "没有可发布的业务计算")
        dependencies = {sid: set() for sid in facts}
        for sid, version in facts.items():
            if any(
                period.ordinal not in closed for period in version.fact.required_closed_periods()
            ):
                raise KernelError("awaiting_close", "该业务须等待所依据期间关账")
            for read in self._reads(version):
                if read.source != "calculation" or read.key.startswith("#"):
                    continue
                for other, upstream in facts.items():
                    if other != sid and calculation_matches(
                        read,
                        Calculation(
                            "",
                            other,
                            upstream.fact.kind,
                            upstream.fact.period,
                            {},
                            upstream.id,
                        ),
                        upstream.fact,
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
        prepared, overlays, blocked = [], {}, set()
        for sid in ordered:
            version = facts[sid]
            selected = {}
            for read in self._reads(version):
                values = list(selections[read])
                if read.source == "calculation" and not read.key.startswith("#"):
                    values = [v for v in values if v.subject_id not in overlays]
                    for upstream_id, calc in overlays.items():
                        upstream = facts[upstream_id]
                        if self._selection_enabled(upstream_id) and calculation_matches(
                            read, calc, upstream.fact
                        ):
                            values.append(calc)
                    values.sort(key=lambda item: (item.period.ordinal, item.subject_id))
                selected[read] = tuple(values)
            context = Context(selected, accounting=accounting.signature)
            if dependencies[sid] & blocked:
                error = compatibility(previous[sid], "upstream_comparison_unavailable")
                prepared.append(
                    Prepared(
                        version,
                        None,
                        None,
                        context,
                        previous[sid],
                        None,
                        "compatibility_required",
                        error.response(),
                    )
                )
                blocked.add(sid)
                continue
            try:
                outcome = self._evaluate(version, context)
            except KernelError as exc:
                if exc.code != "accounting_compatibility_required":
                    raise
                prepared.append(
                    Prepared(
                        version,
                        None,
                        None,
                        context,
                        previous[sid],
                        None,
                        "compatibility_required",
                        exc.response(),
                    )
                )
                blocked.add(sid)
                continue
            trace = context.trace()
            calculated_hash = digest(
                {
                    "fact": version.id,
                    "outcome": outcome,
                    "reads": [asdict(read) for read, _items in trace.selections],
                    "versions": sorted(trace.versions),
                    "program": PROGRAM_VERSION,
                }
            )
            calc_id = "c_" + calculated_hash.hex()
            overlays[sid] = Calculation(
                calc_id,
                sid,
                version.fact.kind,
                version.fact.period,
                outcome["values"],
                version.id,
                digest(outcome).hex(),
            )
            signature, issue = None, None
            try:
                old_signature = accounting.signature(previous[sid]) if previous[sid] else None
                accounting.add(
                    overlays[sid],
                    version,
                    outcome,
                    (
                        value.id
                        for read, values in trace.selections
                        if read.source == "fact"
                        for value in values
                    ),
                    (
                        value.id
                        for read, values in trace.selections
                        if read.source == "calculation"
                        for value in values
                    ),
                )
                signature = accounting.signature(calc_id)
                impact = (
                    "initial"
                    if old_signature is None
                    else "review_no_impact"
                    if old_signature == signature
                    else "accounting_changed"
                )
            except KernelError as exc:
                if exc.code != "accounting_compatibility_required":
                    raise
                impact, issue = "compatibility_required", exc.response()
                blocked.add(sid)
            prepared.append(
                Prepared(
                    version,
                    calc_id,
                    outcome,
                    context,
                    previous[sid],
                    signature,
                    impact,
                    issue,
                )
            )
        from .asset_batch_models import MEMBER_KINDS, OWNER_KINDS
        from .publication import route

        requested = self._explicit_roots(subjects)
        has_changes = any(
            item.impact not in {"review_no_impact", "compatibility_required"}
            and item.version.fact.kind not in MEMBER_KINDS
            for item in prepared
        )
        decisions = {}
        for item in prepared:
            if item.version.fact.kind in MEMBER_KINDS or item.compatibility_issue:
                continue
            sid = item.version.subject_id
            decisions[sid] = route(
                item.version.fact.period.ordinal,
                publications.get(sid),
                max(closed, default=-1),
                posting_period,
                no_impact=item.impact == "review_no_impact",
                explicit=sid in requested
                and (item.impact != "review_no_impact" or not has_changes),
            )
        for item in prepared:
            if item.version.fact.kind not in OWNER_KINDS or item.outcome is None:
                continue
            for member in item.outcome["values"]["members"]:
                decisions[member["member_subject_id"]] = decisions[item.version.subject_id]
        prepared = [
            replace(
                item,
                publication=decisions.get(item.version.subject_id),
                explicit=item.version.subject_id in requested
                and (item.impact != "review_no_impact" or not has_changes),
            )
            for item in prepared
        ]
        checked = checked_lanes(
            self.store.registry,
            ((item.version, item.context.trace()) for item in prepared),
        )
        from .duplicates import DuplicateCandidates

        if any(DuplicateCandidates.eligible(item.version.fact.kind) for item in prepared):
            checked = tuple(sorted({*checked, "accounting", "material"}))
        public = {
            "subjects": sorted(facts),
            "epochs": epochs,
            "posting_period": posting_period,
            "results": [
                {
                    "subject_id": p.version.subject_id,
                    "calculation_id": p.calculation_id,
                    "kind": p.version.fact.kind,
                    "period": str(p.version.fact.period),
                    **(
                        {
                            "source_period": str(p.version.fact.period),
                            "posting_period": str(
                                YearMonth.from_ordinal(p.publication["posting_period"])
                            ),
                            "mode": p.publication["mode"],
                        }
                        if p.publication
                        else {}
                    ),
                    "fact_id": p.version.id,
                    "previous_calculation_id": p.previous_calculation_id,
                    "accounting": p.accounting,
                    "impact": p.impact,
                    **(
                        {
                            "result_digest": digest(p.outcome).hex(),
                            **p.outcome,
                        }
                        if p.outcome is not None
                        else {}
                    ),
                    **(
                        {"compatibility_issue": p.compatibility_issue}
                        if p.compatibility_issue
                        else {}
                    ),
                }
                for p in prepared
            ],
            "company_id": self.store.company_id,
            "database_id": self.store.database_id,
            "checked_lanes": list(checked),
        }
        public["digest"] = digest(
            {**public, "epochs": {lane: epochs[lane] for lane in checked}}
        ).hex()
        return public, prepared

    def preview(self, subjects: list[str], *, posting_period: str | None = None):
        return {"status": "preview", **self._prepare(subjects, posting_period)[0]}

    def confirm(
        self,
        subjects: list[str],
        *,
        preview_digest: str,
        epochs: dict,
        request_id: str,
        posting_period: str | None = None,
    ):
        request_hash = digest(["publish", sorted(subjects), preview_digest, epochs, posting_period])
        cached = self._cached(request_id, request_hash)
        if cached is not None:
            return cached
        public, prepared = self._prepare(subjects, posting_period)
        if public["digest"] != preview_digest or public["epochs"]["accounting"] != epochs.get(
            "accounting"
        ):
            raise KernelError("preview_expired", "计算结果与已审阅的预览不同")

        def operation(connection):
            # A comparison failure rejects the whole prepared graph, including
            # otherwise valid predecessors. It never becomes a partial publish.
            for item in prepared:
                if item.compatibility_issue is not None:
                    raise compatibility(
                        item.compatibility_issue.get("calculation_id")
                        or item.previous_calculation_id
                        or item.calculation_id,
                        item.compatibility_issue.get("reason", "comparison_unavailable"),
                    )
            from .duplicates import DuplicateCandidates
            from .integrity import verify_prepared_sources, verify_publication
            from .projections import prepare_projection_check, verify_projection_change

            duplicate_subjects = self._explicit_roots(item.version.subject_id for item in prepared)
            if duplicate_subjects:
                DuplicateCandidates(self.store).require_publishable(connection, duplicate_subjects)
            verify_prepared_sources(self, connection, prepared)
            self._check_publication_projections(connection, prepared)
            projection_check = prepare_projection_check(
                connection, prepared, posting_period=posting_period
            )
            results = [self._publish(connection, item, posting_period) for item in prepared]
            self._sync_publication_projections(
                connection, [item.version.subject_id for item in prepared]
            )
            verify_publication(self, connection, [item.calculation_id for item in prepared])
            verify_projection_change(connection, projection_check)
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

    def _record_prepared(self, connection, prepared):
        version, cid, outcome = prepared.version, prepared.calculation_id, prepared.outcome
        outcome_digest = digest(outcome)
        if connection.execute("SELECT 1 FROM calculation WHERE id=?", (cid,)).fetchone():
            return
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
                for key in sorted(scope_keys("calculation", version.fact, version.subject_id))
            ],
        )
        trace = prepared.context.trace()
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
                for r, _items in trace.selections
            ],
        )
        fact_ids = {version.id}
        calc_ids = set()
        for read, items in trace.selections:
            for item in items:
                (fact_ids if read.source == "fact" else calc_ids).add(item.id)
        connection.executemany(
            "INSERT INTO dependency_fact VALUES(?,?)", [(cid, fid) for fid in sorted(fact_ids)]
        )
        connection.executemany(
            "INSERT INTO dependency_calculation VALUES(?,?)",
            [(cid, upstream) for upstream in sorted(calc_ids) if upstream != cid],
        )

    def _publish(self, connection, prepared, posting_period):
        from .publication import append, head, route

        version, cid, outcome = prepared.version, prepared.calculation_id, prepared.outcome
        old = connection.execute(
            "SELECT c.* FROM calculation_current a JOIN calculation c ON c.id=a.calculation_id "
            "WHERE a.subject_id=?",
            (version.subject_id,),
        ).fetchone()
        old_outcome = json.loads(old["outcome"]) if old else None
        if (old["id"] if old else None) != prepared.previous_calculation_id:
            raise KernelError("preview_expired", "当前发布版本与已审阅预览不一致")
        previous = head(connection, version.subject_id)
        old_publication = previous if old else None
        no_impact = prepared.impact == "review_no_impact"
        closed = connection.execute("SELECT coalesce(max(period),-1) FROM period_close").fetchone()[
            0
        ]
        decision = route(
            version.fact.period.ordinal,
            previous,
            closed,
            posting_period,
            no_impact=no_impact,
            explicit=prepared.explicit,
        )
        if decision != prepared.publication:
            raise KernelError("preview_expired", "实际入账期间或发布关系已变化")
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
        posting = decision["posting_period"]
        voucher_key = old_publication["voucher_id"] if old_publication else None
        is_new = not connection.execute(
            "SELECT 1 FROM calculation_seal WHERE calculation_id=?", (cid,)
        ).fetchone()
        if is_new:
            self._record_prepared(connection, prepared)
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
            if (
                decision["mode"] == "open_replace"
                and old_publication
                and posting != old_publication["posting_period"]
            ):
                # A legitimate source-month correction moves the whole open
                # tranche, including the already-created baseline reversal.
                reversals = connection.execute(
                    "SELECT v.* FROM voucher_current h JOIN voucher_version v ON v.id=h.version_id "
                    "JOIN calculation c ON c.id=v.calculation_id WHERE c.subject_id=? "
                    "AND v.period=? AND v.reverses_id IS NOT NULL",
                    (version.subject_id, old_publication["posting_period"]),
                ).fetchall()
                for reversal in reversals:
                    lines = [
                        dict(row)
                        for row in connection.execute(
                            "SELECT account,debit,credit,cashflow FROM voucher_line "
                            "WHERE version_id=? ORDER BY line_no",
                            (reversal["id"],),
                        )
                    ]
                    self._journal_projection(connection, reversal["period"], lines, -1)
                    self._journal(
                        connection,
                        reversal["voucher_id"],
                        cid,
                        posting,
                        lines,
                        reverses_id=reversal["reverses_id"],
                    )
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
                    voucher_key = cid + ":replacement" if outcome["lines"] else None
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
                if decision["mode"] == "closed_correction":
                    voucher_key = cid + ":replacement"
                voucher_key = voucher_key or (
                    cid + ":replacement"
                    if old_publication
                    and old_publication["posting_period"] != version.fact.period.ordinal
                    else "v:" + version.subject_id
                )
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
            append(connection, version.subject_id, cid, decision, voucher_key)
            connection.execute("INSERT INTO calculation_seal VALUES(?)", (cid,))
        result = self._finish_prepared(connection, prepared, voucher_number)
        result.update(
            source_period=str(version.fact.period),
            posting_period=str(YearMonth.from_ordinal(posting)),
            mode=decision["mode"],
        )
        return result

    def _sync_publication_projections(self, connection, subjects):
        from .period_balances import sync_period_balances
        from .settlement_projection import sync_settlement_publications

        periods = sync_period_balances(connection, subjects)
        publications = [
            r[0]
            for r in connection.execute(
                "SELECT id FROM calculation_publication WHERE subject_id IN "
                "(SELECT value FROM json_each(?))",
                (canonical(sorted(subjects)),),
            )
        ]
        sync_settlement_publications(self, connection, publications, periods)

    def _check_publication_projections(self, connection, prepared=(), *, subjects=()):
        from .period_balances import verify_selected_balances
        from .settlement_projection import verify_settlement_periods

        subjects = {*subjects, *(item.version.subject_id for item in prepared)}
        periods = {
            r[0]
            for r in connection.execute(
                "SELECT DISTINCT posting_period FROM calculation_publication "
                "WHERE subject_id IN (SELECT value FROM json_each(?))",
                (canonical(sorted(subjects)),),
            )
        }
        periods.update(item.publication["posting_period"] for item in prepared if item.publication)
        verify_selected_balances(connection, 0, periods=periods)
        existing = {
            r[0]
            for r in connection.execute(
                "SELECT DISTINCT posting_period FROM calculation_publication "
                "WHERE posting_period IN (SELECT value FROM json_each(?))",
                (canonical(sorted(periods)),),
            )
        }
        verify_settlement_periods(connection, existing)

    def _finish_prepared(self, connection, prepared, voucher_number=None):
        version, cid = prepared.version, prepared.calculation_id
        no_impact = prepared.impact == "review_no_impact"
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
            "accounting": prepared.accounting,
            "impact": prepared.impact,
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
        from .maintenance import Maintenance

        return Maintenance(self).rebuild_projections(request_id=request_id)

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

    def trace(self, calculation_id: str | None = None, *, voucher_version_id: str | None = None):
        with self.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            voucher, related = None, []
            if voucher_version_id:
                version = connection.execute(
                    "SELECT v.*,n.number FROM voucher_version v JOIN voucher n "
                    "ON n.id=v.voucher_id WHERE v.id=?",
                    (voucher_version_id,),
                ).fetchone()
                if version is None:
                    raise KernelError("unknown_voucher", "凭证版本不存在")
                basis = version
                if version["reverses_id"]:
                    basis = connection.execute(
                        "SELECT * FROM voucher_version WHERE id=?", (version["reverses_id"],)
                    ).fetchone()
                    calculation_id = basis["calculation_id"]
                elif calculation_id and calculation_id != basis["calculation_id"]:
                    publication = connection.execute(
                        "SELECT 1 FROM calculation_publication WHERE calculation_id=? "
                        "AND voucher_id=? AND posting_period=?",
                        (calculation_id, version["voucher_id"], version["period"]),
                    ).fetchone()
                    if not publication:
                        raise KernelError("voucher_trace_mismatch", "该计算不属于所选凭证")
                else:
                    calculation_id = basis["calculation_id"]
                voucher = {
                    "id": version["id"],
                    "number": version["number"],
                    "period": str(YearMonth.from_ordinal(version["period"])),
                    "reverses_id": version["reverses_id"],
                    "total": version["total"],
                    "lines": [
                        dict(item)
                        for item in connection.execute(
                            "SELECT line_no,account,debit,credit FROM voucher_line "
                            "WHERE version_id=? ORDER BY line_no",
                            (version["id"],),
                        )
                    ],
                }
                # The reversal records the replacement calculation; its basis remains the original.
                correction = connection.execute(
                    "SELECT reverses_id FROM voucher_version WHERE calculation_id=? "
                    "AND reverses_id IS NOT NULL",
                    (version["calculation_id"],),
                ).fetchone()
                anchor_id = correction[0] if correction else basis["id"]
                groups = [(anchor_id, "current")]
                if (
                    anchor_id != version["id"]
                    and connection.execute(
                        "SELECT 1 FROM voucher_version WHERE reverses_id=? LIMIT 1",
                        (version["id"],),
                    ).fetchone()
                ):
                    groups.append((version["id"], "next"))
                for group_id, relation in groups:
                    anchor_number = connection.execute(
                        "SELECT n.number FROM voucher_version v JOIN voucher n "
                        "ON n.id=v.voucher_id WHERE v.id=?",
                        (group_id,),
                    ).fetchone()[0]
                    for item in connection.execute(
                        "SELECT v.id,v.reverses_id,v.calculation_id,n.number,v.period "
                        "FROM voucher_version v "
                        "JOIN voucher n ON n.id=v.voucher_id WHERE v.id=? OR v.reverses_id=? "
                        "OR v.calculation_id IN (SELECT calculation_id FROM voucher_version "
                        "WHERE reverses_id=?) ORDER BY v.period,n.number",
                        (group_id, group_id, group_id),
                    ):
                        if item["id"] == version["id"]:
                            continue
                        role = (
                            "original"
                            if item["id"] == group_id
                            else "reversal"
                            if item["reverses_id"]
                            else "replacement"
                        )
                        period = str(YearMonth.from_ordinal(item["period"]))
                        role_label = {
                            "original": "原凭证",
                            "reversal": "冲正凭证",
                            "replacement": "替换凭证",
                        }[role]
                        group_label = "本次更正" if relation == "current" else "后续更正"
                        related.append(
                            {
                                "id": item["id"],
                                "number": item["number"],
                                "period": period,
                                "role": role,
                                "correction_of_voucher_id": group_id,
                                "correction_of_number": anchor_number,
                                "correction_group": relation,
                                "label": (
                                    f"{role_label} {item['number']} · {period}"
                                    f"（原凭证 {anchor_number} 的{group_label}）"
                                ),
                            }
                        )
            if not calculation_id:
                raise ValueError("需要计算或凭证版本")
            row = connection.execute(
                "SELECT * FROM calculation WHERE id=?", (calculation_id,)
            ).fetchone()
            if not row:
                raise KernelError("unknown_calculation", "计算版本不存在")
            from .asset_batch_models import MEMBER_KINDS, OWNER_KINDS
            from .asset_batches import frozen_members

            def batch_header(owner):
                publication = connection.execute(
                    "SELECT p.posting_period,p.voucher_id,v.number "
                    "FROM calculation_publication p LEFT JOIN voucher v ON v.id=p.voucher_id "
                    "WHERE p.calculation_id=?",
                    (owner["id"],),
                ).fetchone()
                return {
                    "owner_calculation_id": owner["id"],
                    "owner_subject_id": owner["subject_id"],
                    "kind": owner["kind"],
                    "calculation_period": str(YearMonth.from_ordinal(owner["period"])),
                    "posting_period": (
                        str(YearMonth.from_ordinal(publication["posting_period"]))
                        if publication
                        else None
                    ),
                    "voucher_id": publication["voucher_id"] if publication else None,
                    "voucher_number": publication["number"] if publication else None,
                }

            asset_batch, asset_batch_owners = None, []
            if row["kind"] in OWNER_KINDS:
                asset_batch = {
                    **batch_header(row),
                    "members": frozen_members(connection, row["id"]),
                }
            elif row["kind"] in MEMBER_KINDS:
                for owner in connection.execute(
                    "SELECT DISTINCT c.* FROM asset_batch_member m "
                    "JOIN calculation c ON c.id=m.owner_calculation_id "
                    "WHERE m.member_calculation_id=? ORDER BY c.period,c.id",
                    (row["id"],),
                ):
                    member = next(
                        item
                        for item in frozen_members(connection, owner["id"])
                        if item["member_calculation_id"] == row["id"]
                    )
                    asset_batch_owners.append({**batch_header(owner), "member": member})
            facts = [
                self.store.fact(connection, r[0])
                for r in connection.execute(
                    "SELECT fact_id FROM dependency_fact WHERE calculation_id=? ORDER BY fact_id",
                    (calculation_id,),
                )
            ]
            from .dashboard import _name

            upstream_details = [
                {
                    "id": r["id"],
                    "label": f"{YearMonth.from_ordinal(r['period'])} · {_name(r['kind'])}",
                }
                for r in connection.execute(
                    "SELECT c.id,c.period,c.kind FROM dependency_calculation d "
                    "JOIN calculation c ON c.id=d.upstream_id WHERE d.calculation_id=? "
                    "ORDER BY c.period,c.kind,c.id",
                    (calculation_id,),
                )
            ]
            return {
                "voucher": voucher,
                "asset_batch": asset_batch,
                "asset_batch_owners": asset_batch_owners,
                "upstream_details": upstream_details,
                "related_vouchers": related,
                "evidence_details": self.store.evidence_metadata(
                    connection, [proof for fact in facts for proof in fact.evidence]
                ),
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

    def _delete_plan(self, connection, subject_id, recording_error_evidence):
        fact = self.store.current_fact(connection, subject_id)
        from .asset_batch_models import MEMBER_KINDS, OWNER_KINDS

        if fact.fact.kind in MEMBER_KINDS | OWNER_KINDS:
            raise KernelError(
                "asset_batch_command_required",
                "资产汇总及成员须通过资产专用撤回入口处理",
            )
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
        calculation = self.store.calculation(row) if row else None
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
            item[0]
            for item in connection.execute(
                "SELECT a.subject_id FROM dependency_fact d JOIN calculation_current a "
                "ON a.calculation_id=d.calculation_id JOIN fact_revision f ON f.id=d.fact_id "
                "WHERE f.subject_id=? UNION SELECT a.subject_id FROM dependency_calculation d "
                "JOIN calculation_current a ON a.calculation_id=d.calculation_id "
                "JOIN calculation c ON c.id=d.upstream_id WHERE c.subject_id=?",
                (subject_id, subject_id),
            )
        }
        consumers.discard(subject_id)
        fact_scopes = {
            item[0]
            for item in connection.execute(
                "SELECT scope_key FROM fact_scope WHERE fact_id=?", (fact.id,)
            )
        }
        calculation_scopes = (
            {
                item[0]
                for item in connection.execute(
                    "SELECT scope_key FROM calculation_scope WHERE calculation_id=?",
                    (calculation.id,),
                )
            }
            if calculation is not None
            else set()
        )
        # Initial publications and replacements have no new exact edges yet.
        # Match pending current facts against the same persisted source scopes
        # used by database selection; archived dependencies never block deletion.
        for candidate in connection.execute(
            "SELECT DISTINCT f.fact_id FROM pending p JOIN fact_current f "
            "ON f.subject_id=p.subject_id WHERE p.subject_id!=?",
            (subject_id,),
        ).fetchall():
            dependent = self.store.fact(connection, candidate[0])
            for read in dependent.fact.reads_for(dependent.subject_id):
                if read_matches(
                    read,
                    source="fact",
                    kind=fact.fact.kind,
                    ident=fact.id,
                    period=fact.fact.period,
                    scopes=fact_scopes,
                ) or (
                    calculation is not None
                    and read_matches(
                        read,
                        source="calculation",
                        kind=calculation.kind,
                        ident=calculation.id,
                        period=calculation.period,
                        scopes=calculation_scopes,
                    )
                ):
                    consumers.add(dependent.subject_id)
                    break
        if consumers:
            raise KernelError(
                "has_dependents", "仍有有效下游业务，不能撤去", subjects=sorted(consumers)
            )
        epochs = self.store.epochs(connection)
        checked = ("accounting", "management", "material")
        from .publication import head

        publication = head(connection, subject_id)
        plan = {
            "subject_id": subject_id,
            "kind": fact.fact.kind,
            "fact_id": fact.id,
            "calculation_id": calculation.id if calculation else None,
            "source_period": str(fact.fact.period),
            "posting_period": str(YearMonth.from_ordinal(publication["posting_period"]))
            if publication
            else str(fact.fact.period),
            "mode": "withdrawn",
            "epochs": epochs,
            "checked_lanes": list(checked),
            "recording_error_evidence": recording_error_evidence,
        }
        plan["digest"] = digest({**plan, "epochs": {lane: epochs[lane] for lane in checked}}).hex()
        return plan

    def preview_delete(self, subject_id: str, *, recording_error_evidence: str | None = None):
        with self.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            plan = self._delete_plan(connection, subject_id, recording_error_evidence)
            connection.commit()
        return {"status": "preview", **plan}

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
            locked = self._delete_plan(connection, subject_id, recording_error_evidence)
            if locked["digest"] != preview_digest:
                raise KernelError("preview_expired", "撤去业务预览已变化")
            calc = locked["calculation_id"]
            if calc:
                from .integrity import verify_publication

                verify_publication(self, connection, [calc])
                self._check_publication_projections(connection, subjects=[subject_id])
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
                removed = connection.execute(
                    "DELETE FROM calculation_current WHERE subject_id=? AND calculation_id=?",
                    (subject_id, calc),
                )
                if removed.rowcount != 1:
                    raise KernelError("preview_expired", "当前核算版本与已审阅预览不一致")
                from .publication import append, head

                previous = head(connection, subject_id)
                append(
                    connection,
                    subject_id,
                    None,
                    {
                        "previous_publication_id": previous["id"],
                        "mode": "withdrawn",
                        "posting_period": previous["posting_period"],
                        "baseline_calculation_id": previous["baseline_calculation_id"],
                    },
                    None,
                )
                self._sync_publication_projections(connection, [subject_id])
            connection.execute(
                "INSERT INTO disposition(subject_id,cause_id,action,calculation_id,explanation) "
                "VALUES(?,?,'withdrawn',?,'撤去无有效下游的开放期误记业务')",
                (subject_id, locked["fact_id"], calc),
            )
            connection.execute("DELETE FROM pending WHERE subject_id=?", (subject_id,))
            removed = connection.execute(
                "DELETE FROM fact_current WHERE subject_id=? AND fact_id=?",
                (subject_id, locked["fact_id"]),
            )
            if removed.rowcount != 1:
                raise KernelError("preview_expired", "当前事实版本与已审阅预览不一致")
            from .discovery_indexes import sync_discovery_subjects

            sync_discovery_subjects(connection, {subject_id})
            return {
                "status": "withdrawn",
                "subject_id": subject_id,
                "retained_calculation_id": calc,
                "recording_error_evidence": recording_error_evidence,
                "source_period": locked["source_period"],
                "posting_period": locked["posting_period"],
                "mode": "withdrawn",
            }

        lanes = {self.store.registry.models[preview["kind"]].lane}
        if preview["calculation_id"] is not None:
            lanes.add("accounting")
        return self._write(
            request_id,
            request_hash,
            epochs,
            tuple(sorted(lanes)),
            "withdraw",
            operation,
            checked_lanes=preview["checked_lanes"],
        )
