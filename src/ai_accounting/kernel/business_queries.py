"""Shared read-only business status and period readiness queries.

The query month is an accounting cut-off.  Current fact and publication heads
are reported separately so a later review never rewrites the selected history.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .contracts import KernelError, Read
from .display import _KINDS, Display
from .periods import Periods
from .provenance import recorded_times
from .query_reads import QueryReads, selected_voucher_sql
from .query_semantics import SETTLEMENT_SOURCE_SLOTS, resolve_calculation_relations
from .types import ActualDate, YearMonth, digest
from .workflow import NON_ACCOUNTING_CALCULATIONS, Workflow, _basis_state

_CHINA = timezone(timedelta(hours=8))
_PROFILE_FIELDS = (
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
    "useful_life_basis",
    "counterparty_id",
    "beneficiary_id",
    "handler_id",
)


def _today_china() -> str:
    return datetime.now(_CHINA).date().isoformat()


def _plain(value):
    if isinstance(value, bytes):
        return value.hex()
    if hasattr(value, "keys"):
        return {key: _plain(value[key]) for key in value.keys()}
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _missing_profile_value(field, value):
    return value is None or (field not in {"employment_start", "employment_end"} and value == "")


class BusinessQueries:
    def __init__(self, engine, *, reads=None):
        self.engine, self.store = engine, engine.store
        self.reads = reads

    def _reads(self, connection):
        if self.reads is None or self.reads.connection is not connection:
            self.reads = QueryReads(self.engine, connection)
        return self.reads

    def _fact(self, connection, fact_id):
        return dict(self._reads(connection).fact(fact_id))

    def _calculation(self, connection, calculation_id):
        return self._reads(connection).calculation(calculation_id)

    def _parents(self, connection, calculation_id):
        return self._reads(connection).parents(calculation_id)

    def _profiles(self, connection, subject_id, period):
        current_profiles = {kind: {} for kind in _KINDS}
        for row in connection.execute(
            "SELECT p.* FROM json_each(?) kinds JOIN display_profile_revision p "
            "ON p.kind=kinds.value AND p.entity_id=? WHERE p.revision=("
            "SELECT max(q.revision) FROM display_profile_revision q "
            "WHERE q.kind=p.kind AND q.entity_id=p.entity_id)",
            (json.dumps(_KINDS), subject_id),
        ):
            current_profiles[row["kind"]][subject_id] = Display._record(row)
        closes = self._reads(connection).close_rows(periods=[YearMonth(period).ordinal])
        closed = bool(closes)
        frozen_profiles = {kind: {} for kind in _KINDS} if closed else current_profiles
        if closed:
            identifiers = [
                item["id"]
                for item in json.loads(closes[0]["manifest"])
                .get("management_snapshot", {})
                .get("profiles", ())
            ]
            for row in connection.execute(
                "SELECT p.* FROM json_each(?) ids JOIN display_profile_revision p "
                "ON p.id=ids.value WHERE p.entity_id=? ORDER BY p.kind,p.entity_id",
                (json.dumps(identifiers), subject_id),
            ):
                frozen_profiles[row["kind"]][subject_id] = Display._record(row)
        references = set()
        result = {}
        for kind in frozen_profiles:
            frozen = frozen_profiles[kind].get(subject_id, {})
            current = current_profiles[kind].get(subject_id, {})
            if not frozen and not current:
                continue
            selected = {}
            sources = {}
            for field in _PROFILE_FIELDS:
                record = current if _missing_profile_value(field, frozen.get(field)) else frozen
                value = record.get(field)
                selected[field] = value
                if _missing_profile_value(field, value):
                    continue
                basis = (
                    "frozen"
                    if closed and record is frozen
                    else "current_supplement"
                    if closed
                    else "current"
                )
                reference = ("display_profile", str(record["id"]))
                references.add(reference)
                evidence = record.get("evidence_digest")
                sources[field] = {
                    "source_type": "display_profile",
                    "id": record["id"],
                    "revision": record.get("revision"),
                    "field": field,
                    "source": record.get("source"),
                    "evidence_digest": evidence,
                    "evidence": [evidence] if evidence else [],
                    "basis": basis,
                    "recorded_at": None,
                }
            result[kind] = {
                "entity_id": subject_id,
                "values": selected,
                "field_sources": sources,
            }
        times = recorded_times(connection, references)
        for profile in result.values():
            for source in profile["field_sources"].values():
                source["recorded_at"] = times.get((source["source_type"], str(source["id"])))
        return result

    def _voucher(self, connection, version_id, selection_source, selected_calculation_id=None):
        row = connection.execute(
            "SELECT v.*,n.number FROM voucher_version v JOIN voucher n ON n.id=v.voucher_id "
            "WHERE v.id=?",
            (version_id,),
        ).fetchone()
        if row is None:
            raise KernelError("selected_voucher_missing", "冻结期间引用的凭证版本不存在")
        calculation_id = selected_calculation_id or row["calculation_id"]
        if row["reverses_id"]:
            reversed_version = connection.execute(
                "SELECT calculation_id FROM voucher_version WHERE id=?",
                (row["reverses_id"],),
            ).fetchone()
            if reversed_version is None:
                raise KernelError(
                    "selected_voucher_missing",
                    "冲正凭证引用的原凭证版本不存在",
                )
            calculation_id = reversed_version["calculation_id"]
        calc = self._calculation(connection, calculation_id)
        replacement = (
            not row["reverses_id"]
            and connection.execute(
                "SELECT 1 FROM voucher_version WHERE calculation_id=? "
                "AND reverses_id IS NOT NULL LIMIT 1",
                (row["calculation_id"],),
            ).fetchone()
            is not None
        )
        role = "reversal" if row["reverses_id"] else "replacement" if replacement else "original"
        return {
            "event_type": "voucher",
            "voucher_version_id": row["id"],
            "voucher_id": row["voucher_id"],
            "voucher_number": row["number"],
            "voucher_calculation_id": row["calculation_id"],
            "calculation_id": calc["id"],
            "fact_id": calc["fact_id"],
            "kind": calc["kind"],
            "calculation_period": calc["period"],
            "posting_period": str(YearMonth.from_ordinal(row["period"])),
            "result_digest": calc["result_digest"],
            "role": role,
            "direction": -1 if role == "reversal" else 1,
            "reverses_voucher_version_id": row["reverses_id"],
            "selection_source": selection_source,
            "lines": [
                dict(item)
                for item in connection.execute(
                    "SELECT line_no,account,debit,credit,cashflow FROM voucher_line "
                    "WHERE version_id=? ORDER BY line_no",
                    (row["id"],),
                )
            ],
        }

    def _selected_accounting(
        self,
        connection,
        subject_id,
        period,
        *,
        current_heads=False,
        kinds=None,
        include_vouchers=True,
        include_lines=True,
        posting_period=None,
    ):
        """Select exact metadata first; payloads are loaded only by consumers.

        A kind or subject restriction narrows candidate closes.  Each candidate
        close still supplies its complete member graph for adoption proof.
        """
        cutoff = YearMonth(period)
        reads = self._reads(connection)
        subjects = (
            None
            if subject_id is None
            else ({subject_id} if isinstance(subject_id, str) else set(subject_id))
        )
        if kinds is not None:
            query = "SELECT id FROM subject WHERE kind IN (SELECT value FROM json_each(?))"
            parameters = [json.dumps(sorted(kinds))]
            if subjects is not None:
                query += " AND id IN (SELECT value FROM json_each(?))"
                parameters.append(json.dumps(sorted(subjects)))
            subjects = {row[0] for row in connection.execute(query, parameters)}
        proof_periods = (YearMonth(posting_period).ordinal,) if posting_period else None
        if proof_periods is None and subjects is not None:
            # A later manifest may repeat this subject only as a dependency.
            # State adoption is defined at its immutable publication period, so
            # those later manifests cannot contribute a state proof for it.
            proof_periods = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT p.posting_period FROM json_each(?) ids "
                    "JOIN calculation c ON c.subject_id=ids.value "
                    "JOIN calculation_publication p ON p.calculation_id=c.id "
                    "WHERE p.posting_period<=?",
                    (json.dumps(sorted(subjects)), cutoff.ordinal),
                )
            )
        if subjects == set():
            manifests = []
        else:
            manifests = [
                (row["period"], reads.close_manifest(row))
                for row in reads.close_rows(
                    subject_ids=subjects,
                    through_period=cutoff.ordinal,
                    periods=proof_periods,
                )
            ]
        events = []
        represented = set()
        # Even state-only reads require voucher root identity to avoid treating
        # an already represented calculation as an independent no-line state.
        sql, parameters = selected_voucher_sql(
            cutoff,
            current_heads=current_heads,
            subject_ids=subjects,
            posting_period=posting_period,
        )
        selected_rows = list(connection.execute(sql, parameters))
        frozen_vouchers = [
            [row["id"], row["close_period"]]
            for row in selected_rows
            if row["close_period"] is not None
        ]
        if frozen_vouchers:
            from .read_indexes import CLOSE_VOUCHERS, verify_close_references

            references = connection.execute(
                "SELECT r.* FROM json_each(?) ids JOIN close_reference r "
                "ON r.reference_id=json_extract(ids.value,'$[0]') "
                "AND r.close_period=json_extract(ids.value,'$[1]') "
                "WHERE r.path=?",
                (json.dumps(frozen_vouchers), CLOSE_VOUCHERS),
            ).fetchall()
            verify_close_references(connection, references)
        represented.update(row["basis_calculation_id"] for row in selected_rows)
        if include_vouchers:
            lines = reads.voucher_lines(row["id"] for row in selected_rows) if include_lines else {}
            for row in selected_rows:
                role = (
                    "reversal"
                    if row["reverses_id"]
                    else ("replacement" if row["replacement"] else "original")
                )
                events.append(
                    {
                        "event_type": "voucher",
                        "voucher_version_id": row["id"],
                        "voucher_id": row["voucher_id"],
                        "voucher_number": row["number"],
                        "voucher_calculation_id": row["voucher_calculation_id"],
                        "calculation_id": row["basis_calculation_id"],
                        "fact_id": row["basis_fact_id"],
                        "kind": row["basis_kind"],
                        "calculation_period": str(
                            YearMonth.from_ordinal(row["calculation_period"])
                        ),
                        "posting_period": str(YearMonth.from_ordinal(row["period"])),
                        "result_digest": row["result_digest"].hex(),
                        "role": role,
                        "direction": -1 if role == "reversal" else 1,
                        "reverses_voucher_version_id": row["reverses_id"],
                        "selection_source": row["selection_source"],
                        **({"lines": lines[row["id"]]} if include_lines else {}),
                    }
                )
        all_members = {
            ident
            for month, manifest in manifests
            if month not in reads.closed_accounting_contexts
            for ident in manifest.get("calculations", ())
        }
        reads.prime_parents(all_members)
        metadata = reads.metadata(all_members, state=False)
        closed_candidates, frozen_root_proofs = {}, {}
        for close_period, manifest in manifests:
            cached = reads.closed_accounting_contexts.get(close_period)
            if cached is not None:
                candidates, proofs = cached
                for candidate_subject in candidates if subjects is None else subjects:
                    identifiers = candidates.get(candidate_subject, set())
                    if identifiers:
                        closed_candidates[close_period, candidate_subject] = identifiers
                        frozen_root_proofs.update(
                            {
                                (close_period, ident): proofs[ident]
                                for ident in identifiers
                                if ident in proofs
                            }
                        )
                continue
            manifest_ids = set(manifest.get("calculations", ()))
            dependency_ids = {
                parent
                for ident in manifest_ids
                for parent in reads.parents(ident)
                if parent in manifest_ids
            }
            voucher_roots = {
                reference["calculation_id"]
                for reference in manifest.get("vouchers", ())
                if isinstance(reference, dict) and reference.get("calculation_id")
            }
            independent_proofs = {}
            for ident in manifest_ids:
                row = metadata[ident]
                if YearMonth(row["posting_period"]).ordinal != close_period:
                    continue
                if ident in voucher_roots:
                    independent_proofs[ident] = {"basis": "manifest_voucher_root"}
                elif (
                    ident not in dependency_ids and YearMonth(row["period"]).ordinal == close_period
                ):
                    independent_proofs[ident] = {"basis": "manifest_lineage_root"}
            # Domain adoption receives the complete independently proven graph;
            # page kind/subject filters must never remove its downstream proof.
            if any(metadata[ident]["kind"] == "opening_package" for ident in manifest_ids):
                from .opening_adoption import prove_opening_adoptions

                independent_proofs.update(
                    prove_opening_adoptions(
                        connection,
                        reads,
                        close_period=close_period,
                        manifest=manifest,
                        metadata=metadata,
                        independent_proofs=independent_proofs,
                    )
                )
            by_subject = {}
            for ident in manifest_ids:
                row = metadata[ident]
                if YearMonth(row["posting_period"]).ordinal == close_period:
                    by_subject.setdefault(row["subject_id"], set()).add(ident)
            reads.closed_accounting_contexts[close_period] = (by_subject, independent_proofs)
            for candidate_subject in by_subject if subjects is None else subjects:
                identifiers = by_subject.get(candidate_subject, set())
                if identifiers:
                    closed_candidates[close_period, candidate_subject] = identifiers
                    frozen_root_proofs.update(
                        {
                            (close_period, ident): independent_proofs[ident]
                            for ident in identifiers
                            if ident in independent_proofs
                        }
                    )
        query = (
            "SELECT c.id,c.subject_id FROM calculation_current a "
            "JOIN calculation c ON c.id=a.calculation_id "
            "JOIN calculation_publication p ON p.calculation_id=c.id "
            "WHERE p.posting_period<=?"
        )
        parameters = [cutoff.ordinal]
        if posting_period is not None:
            query += " AND p.posting_period=?"
            parameters.append(YearMonth(posting_period).ordinal)
        if not current_heads:
            query += " AND NOT EXISTS(SELECT 1 FROM period_close z WHERE z.period=p.posting_period)"
        if subjects is not None:
            query += " AND c.subject_id IN (SELECT value FROM json_each(?))"
            parameters.append(json.dumps(sorted(subjects)))
        currents = list(connection.execute(query, parameters))
        metadata.update(
            reads.metadata(
                {row["id"] for row in currents}
                | {ident for candidates in closed_candidates.values() for ident in candidates}
            )
        )
        current_subjects = {row["subject_id"] for row in currents}
        state_results = {}
        for row in currents:
            calc = metadata[row["id"]]
            if calc["kind"] not in NON_ACCOUNTING_CALCULATIONS and not calc["line_count"]:
                state_results[calc["id"]] = self._state_metadata(
                    calc, "current_publication", {"basis": "calculation_current"}
                )
        unresolved_states = []
        for (close_period, state_subject), identities in sorted(closed_candidates.items()):
            if current_heads and state_subject in current_subjects:
                continue
            candidates = [
                metadata[ident]
                for ident in sorted(identities)
                if metadata[ident]["kind"] not in NON_ACCOUNTING_CALCULATIONS
            ]
            proven = [
                calc
                for calc in candidates
                if (close_period, calc["id"]) in frozen_root_proofs
                and not calc["line_count"]
                and calc["id"] not in represented
            ]
            if len(proven) != 1:
                if any(
                    not calc["line_count"] and calc["id"] not in represented for calc in candidates
                ):
                    unresolved_states.append(
                        {
                            "event_type": "state_result_selection",
                            "status": "unestablished",
                            "reason": "manifest_state_adoption_not_proven",
                            "subject_id": state_subject,
                            "posting_period": str(YearMonth.from_ordinal(close_period)),
                            "selection_source": "close_manifest",
                            "candidates": [
                                {
                                    "calculation_id": calc["id"],
                                    "fact_id": calc["fact_id"],
                                    "kind": calc["kind"],
                                    "result_digest": calc["result_digest"],
                                    "has_journal_lines": bool(calc["line_count"]),
                                    "trace_only": True,
                                }
                                for calc in candidates
                            ],
                        }
                    )
                continue
            calc = proven[0]
            state_results[calc["id"]] = self._state_metadata(
                calc, "close_manifest", frozen_root_proofs[close_period, calc["id"]]
            )
        events.sort(
            key=lambda item: (
                item["posting_period"],
                item["voucher_number"],
                item["voucher_version_id"],
            )
        )
        states = sorted(
            state_results.values(),
            key=lambda item: (item["posting_period"], item["calculation_id"]),
        )
        unresolved_states.sort(key=lambda item: (item["posting_period"], item["subject_id"]))
        period_events = [
            item for item in (*events, *states) if item["posting_period"] == str(cutoff)
        ]
        period_events.sort(
            key=lambda item: (
                item["posting_period"],
                item.get("voucher_number", 0),
                item.get("calculation_id", ""),
            )
        )
        established = bool(events or states)
        status = (
            "partially_established"
            if established and unresolved_states
            else "established"
            if established
            else "unestablished"
            if unresolved_states
            else "not_established"
        )
        return {
            "cutoff_period": str(cutoff),
            "period_events": period_events,
            "through_period": {
                "status": status,
                "voucher_events": events,
                "state_results": states,
                "unestablished_state_selections": unresolved_states,
            },
        }

    @staticmethod
    def _state_metadata(calc, selection_source, selection_proof):
        return {
            "event_type": "state_result",
            "status": "established",
            "calculation_id": calc["id"],
            "fact_id": calc["fact_id"],
            "kind": calc["kind"],
            "calculation_period": calc["period"],
            "posting_period": calc["posting_period"],
            "result_digest": calc["result_digest"],
            "opening": bool(calc["opening"]),
            "selection_source": selection_source,
            "selection_proof": selection_proof,
            "vouchers": [],
        }

    @staticmethod
    def _state_result(calc, selection_source, selection_proof):
        return {
            "event_type": "state_result",
            "status": "established",
            "calculation_id": calc["id"],
            "fact_id": calc["fact_id"],
            "kind": calc["kind"],
            "calculation_period": calc["period"],
            "posting_period": calc["posting_period"],
            "result_digest": calc["result_digest"],
            "opening": bool(calc["outcome"].get("opening")),
            "selection_source": selection_source,
            "selection_proof": selection_proof,
            "vouchers": [],
        }

    def _current_publication(self, connection, subject_id):
        row = connection.execute(
            "SELECT c.id FROM calculation_current a JOIN calculation c ON c.id=a.calculation_id "
            "WHERE a.subject_id=?",
            (subject_id,),
        ).fetchone()
        if row is None:
            return None
        calc = self._calculation(connection, row["id"])
        vouchers = [
            self._voucher(connection, item["id"], "current_publication")
            for item in connection.execute(
                "SELECT v.id FROM voucher_version v WHERE v.calculation_id=? "
                "ORDER BY v.period,v.id",
                (calc["id"],),
            )
        ]
        active = connection.execute(
            "SELECT v.id FROM calculation_publication p JOIN voucher_current a "
            "ON a.voucher_id=p.voucher_id JOIN voucher_version v ON v.id=a.version_id "
            "WHERE p.calculation_id=?",
            (calc["id"],),
        ).fetchone()
        if active and active[0] not in {item["voucher_version_id"] for item in vouchers}:
            vouchers.append(
                self._voucher(
                    connection, active[0], "current_publication", selected_calculation_id=calc["id"]
                )
            )
        return {
            "status": "published",
            "knowledge": "current_knowledge",
            "calculation": calc,
            "publication": {
                "posting_period": calc["posting_period"],
                "voucher_id": calc["voucher_id"],
                "has_journal_lines": bool(calc["outcome"].get("lines")),
            },
            "voucher_versions": vouchers,
            "current_voucher_version_id": active[0] if active else None,
        }

    def _settlements(
        self,
        connection,
        subject_id: str | set[str] | None,
        selected,
        *,
        relation_selected=None,
        summary=False,
    ):
        target_subjects = (
            None
            if subject_id is None
            else {subject_id}
            if isinstance(subject_id, str)
            else set(subject_id)
        )
        reads = self._reads(connection)
        relation_selected = relation_selected or self._selected_accounting(
            connection,
            reads.related_subjects(target_subjects) if target_subjects is not None else None,
            selected["cutoff_period"],
            include_lines=False,
        )
        reads.prime_calculations(
            {
                item["calculation_id"]
                for item in (
                    *relation_selected["through_period"]["voucher_events"],
                    *relation_selected["through_period"]["state_results"],
                )
            }
        )
        selected_events = []
        for event in relation_selected["through_period"]["voucher_events"]:
            calculation_id = event["calculation_id"]
            selected_events.append((event, self._calculation(connection, calculation_id)))
        for state in relation_selected["through_period"]["state_results"]:
            calculation = self._calculation(connection, state["calculation_id"])
            selected_events.append((state | {"direction": 1}, calculation))
        if not selected_events:
            return {
                "cutoff_period": selected["cutoff_period"],
                "status": "not_established",
                "business": [],
                "obligations": [],
                "movements": [],
                "line_relations": [],
                "issues": [],
                **(
                    {"business_count": 0, "movement_count": 0, "line_relation_count": 0}
                    if summary
                    else {}
                ),
            }
        businesses, obligations, movements, line_relations, issues = [], {}, [], [], []
        related = False
        business_count = movement_count = line_relation_count = 0
        unresolved_movement = False
        for event, calculation in sorted(
            selected_events,
            key=lambda item: (
                item[0]["posting_period"],
                item[0].get("voucher_number", 0),
                item[0]["calculation_id"],
            ),
        ):
            resolution = reads.relations(calculation, resolver=resolve_calculation_relations)
            declared = reads.declared_subjects(calculation)
            direction = event["direction"]
            event_related = (
                target_subjects is None
                or calculation["subject_id"] in target_subjects
                or not target_subjects.isdisjoint(declared)
            )
            for obligation in resolution.get("obligations", ()):
                source_business = obligation.get("source_business") or {}
                if obligation.get("source_calculation_id") != calculation["id"] or (
                    target_subjects is not None
                    and source_business.get("subject_id") not in target_subjects
                ):
                    continue
                event_related = True
                key = obligation.get("key")
                if not isinstance(key, str) or not key:
                    continue
                item = obligations.setdefault(
                    key,
                    {
                        **obligation,
                        "source_amount_fen": 0,
                        "paid_fen": 0,
                        "other_settled_fen": 0,
                        "period_paid_fen": 0,
                        "period_other_settled_fen": 0,
                        "remaining_fen": 0,
                        "source_events": [],
                        **({"source_event_count": 0} if summary else {}),
                    },
                )
                amount = obligation.get("amount_fen")
                if type(amount) is int:
                    item["source_amount_fen"] += direction * amount
                else:
                    item["_uncertain_source"] = True
                if summary:
                    item["source_event_count"] += 1
                else:
                    item["source_events"].append(
                        {
                            "calculation_id": calculation["id"],
                            "fact_id": calculation["fact_id"],
                            "posting_period": event["posting_period"],
                            "voucher_version_id": event.get("voucher_version_id"),
                            "direction": direction,
                        }
                    )
            for movement in resolution.get("settlements", ()):
                source_business = movement.get("source_business") or {}
                settlement_business = movement.get("settlement_business") or {}
                declared_source = None
                references = reads.declared_sources(calculation)
                source_index = movement.get("index")
                # A conflicting frozen pointer does not erase the declared
                # source's affected slot; the resolver keeps its exact identity.
                if type(source_index) is int and 0 <= source_index < len(references):
                    declared_source = references[source_index].get("source_id")
                if target_subjects is not None and target_subjects.isdisjoint(
                    {
                        source_business.get("subject_id"),
                        settlement_business.get("subject_id"),
                        declared_source,
                    }
                ):
                    continue
                event_related = True
                signed = movement.get("amount_fen")
                record = {
                    **movement,
                    "posting_period": event["posting_period"],
                    "voucher_version_id": event.get("voucher_version_id"),
                    "direction": direction,
                    "signed_amount_fen": direction * signed if type(signed) is int else None,
                }
                movement_count += 1
                if not summary:
                    movements.append(record)
                key = movement.get("obligation_key")
                established = movement.get("state") == "resolved"
                if not established:
                    unresolved_movement = True
                if key in obligations:
                    if established and type(signed) is int:
                        field = (
                            "paid_fen" if movement.get("mode") == "payment" else "other_settled_fen"
                        )
                        obligations[key][field] += direction * signed
                        if event["posting_period"] == selected["cutoff_period"]:
                            obligations[key]["period_" + field] += direction * signed
                    else:
                        unresolved_movement = True
                        uncertainty = (
                            "_uncertain_payment"
                            if movement.get("mode") == "payment"
                            else "_uncertain_other"
                        )
                        obligations[key][uncertainty] = True
                        if event["posting_period"] == selected["cutoff_period"]:
                            obligations[key][
                                uncertainty.replace("_uncertain_", "_uncertain_period_")
                            ] = True
            for relation in resolution.get("line_relations", ()):
                source_business = relation.get("source_business") or {}
                if (
                    target_subjects is None
                    or calculation["subject_id"] in target_subjects
                    or source_business.get("subject_id") in target_subjects
                ):
                    line_relation_count += 1
                    if not summary:
                        line_relations.append(
                            {
                                **relation,
                                "posting_period": event["posting_period"],
                                "voucher_version_id": event.get("voucher_version_id"),
                                "direction": direction,
                            }
                        )
            if event_related:
                related = True
                business_count += 1
                if not summary:
                    businesses.append(
                        {
                            **(resolution.get("business") or {}),
                            "calculation_id": calculation["id"],
                            "posting_period": event["posting_period"],
                            "voucher_version_id": event.get("voucher_version_id"),
                            "direction": direction,
                        }
                    )
                issues.extend(resolution.get("issues", ()))
        if not related:
            return {
                "cutoff_period": selected["cutoff_period"],
                "status": "not_established",
                "business": [],
                "obligations": [],
                "movements": [],
                "line_relations": [],
                "issues": [],
                **(
                    {"business_count": 0, "movement_count": 0, "line_relation_count": 0}
                    if summary
                    else {}
                ),
            }
        for obligation in obligations.values():
            uncertain_source = obligation.pop("_uncertain_source", False)
            uncertain_payment = obligation.pop("_uncertain_payment", False)
            uncertain_other = obligation.pop("_uncertain_other", False)
            if uncertain_payment:
                obligation["paid_fen"] = None
            if uncertain_other:
                obligation["other_settled_fen"] = None
            if obligation.pop("_uncertain_period_payment", False):
                obligation["period_paid_fen"] = None
            if obligation.pop("_uncertain_period_other", False):
                obligation["period_other_settled_fen"] = None
            if uncertain_source:
                obligation["source_amount_fen"] = None
                unresolved_movement = True
            obligation["remaining_fen"] = (
                None
                if uncertain_source or uncertain_payment or uncertain_other
                else obligation["source_amount_fen"]
                - obligation["paid_fen"]
                - obligation["other_settled_fen"]
            )
            remaining = obligation["remaining_fen"]
            source = obligation["source_amount_fen"]
            obligation["settlement_status"] = (
                "unestablished"
                if remaining is None
                else "settled"
                if remaining == 0
                else "over_settled"
                if remaining < 0
                else "open"
                if remaining == source
                else "partial"
                if 0 < remaining < source
                else "unestablished"
            )
        return {
            "cutoff_period": selected["cutoff_period"],
            "status": "partially_established" if unresolved_movement else "established",
            "business": businesses,
            "obligations": [obligations[key] for key in sorted(obligations)],
            "movements": movements,
            "line_relations": line_relations,
            "issues": issues,
            **(
                {
                    "business_count": business_count,
                    "movement_count": movement_count,
                    "line_relation_count": line_relation_count,
                }
                if summary
                else {}
            ),
        }

    def settlement_summary(self, connection, period, *, current=False, subject_ids=None):
        """Page summary: select relevant raw declarations before hydrating outcomes."""
        return self.settlements(
            connection,
            period,
            current=current,
            subject_ids=subject_ids,
            summary=True,
        )

    def settlements(self, connection, period, *, subject_ids=None, current=False, summary=False):
        """Shared scoped reducer; source scope and relationship candidates are distinct."""
        reads = self._reads(connection)
        subjects = (
            None
            if subject_ids is None
            else {subject_ids}
            if isinstance(subject_ids, str)
            else set(subject_ids)
        )
        if subjects is None and summary:
            subjects = reads.settlement_subjects(period)
        if current:
            return self._current_settlement_followups(
                connection,
                period,
                subject_ids=subjects,
                summary=summary,
            )
        related = reads.related_subjects(subjects) if subjects is not None else None
        selected = self._selected_accounting(connection, related, period, include_lines=False)
        result = self._settlements(
            connection,
            subjects,
            selected,
            relation_selected=selected,
            summary=summary,
        )
        if summary:
            unknown = [
                item
                for item in selected["through_period"]["unestablished_state_selections"]
                if subjects is None or item["subject_id"] in subjects
            ]
            result["unestablished_state_selections"] = unknown
            result["complete"] = not unknown and result["status"] != "partially_established"
        return result

    def _current_settlement_selection(self, connection, period, *, subject_ids=None):
        """Keep the historical source scope while following later exact relations."""
        reads = self._reads(connection)
        if subject_ids is None:
            subject_ids = reads.settlement_subjects(period)
        scoped = self._selected_accounting(connection, subject_ids, period, include_lines=False)
        identities = {
            item["calculation_id"]
            for item in (
                *scoped["through_period"]["voucher_events"],
                *scoped["through_period"]["state_results"],
            )
        }
        subjects = {item["subject_id"] for item in reads.metadata(identities).values()}
        subjects.update(
            item["subject_id"]
            for item in scoped["through_period"]["unestablished_state_selections"]
        )
        related = reads.related_subjects(subjects)
        current_cutoff = YearMonth(period)
        if related:
            latest = connection.execute(
                "SELECT max(p.posting_period) FROM json_each(?) ids "
                "JOIN calculation_current a ON a.subject_id=ids.value "
                "JOIN calculation_publication p ON p.calculation_id=a.calculation_id",
                (json.dumps(sorted(related)),),
            ).fetchone()[0]
            if latest is not None:
                current_cutoff = max(current_cutoff, YearMonth.from_ordinal(latest))
        current = self._selected_accounting(
            connection,
            related,
            str(current_cutoff),
            current_heads=True,
            include_lines=False,
        )
        return subjects, current, str(current_cutoff)

    def _current_settlement_followups(self, connection, period, *, subject_ids=None, summary=False):
        subjects, current, current_cutoff = self._current_settlement_selection(
            connection, period, subject_ids=subject_ids
        )
        result = self._settlements(
            connection, subjects, current, relation_selected=current, summary=summary
        )
        if summary:
            unknown = [
                item
                for item in current["through_period"]["unestablished_state_selections"]
                if item["subject_id"] in subjects
            ]
            result["unestablished_state_selections"] = unknown
            result["complete"] = not unknown and result["status"] != "partially_established"
        return {
            **result,
            "scope_period": period,
            "current_cutoff_period": current_cutoff,
            "cutoff_semantics": "current_published_relations_independent_of_as_of",
        }

    def _external(
        self, connection, subject_id, period, as_of, period_readiness=None, *, summary=False
    ):
        from .workflow import SOURCES

        reads = self._reads(connection)
        related_obligations = {subject_id}
        related_completions = []
        completion_count = 0
        candidates = set()
        if "external_obligation" in self.store.registry.models:
            current = connection.execute(
                "SELECT c.kind,c.period FROM calculation_current a JOIN calculation c "
                "ON c.id=a.calculation_id WHERE a.subject_id=?",
                (subject_id,),
            ).fetchone()
            if current:
                kinds = [
                    key
                    for key, values in SOURCES.items()
                    if "*" in values or current["kind"] in values
                ]
                candidates.update(
                    row[0]
                    for row in connection.execute(
                        "SELECT a.subject_id FROM fact_external_obligation f "
                        "JOIN fact_current a ON a.fact_id=f.revision_id "
                        "WHERE f.start_period<=? AND f.end_period>=? "
                        "AND f.obligation_kind IN (SELECT value FROM json_each(?))",
                        (current["period"], current["period"], json.dumps(kinds)),
                    )
                )
            requested = [Read("fact", "external_obligation", "@" + ident) for ident in candidates]
            reads.prime_select(requested)
            versions = [version for read in requested for version in reads.select(read)]
            reads.prime_select(read for version in versions for read in version.fact.basis_reads())
            for version in versions:
                basis, _ = _basis_state(version.fact, reads.select)
                if any(item.subject_id == subject_id for item in basis):
                    related_obligations.add(version.subject_id)
        related_subjects = reads.related_subjects({subject_id})
        calculation_ids = {
            row[0]
            for row in connection.execute(
                "SELECT c.id FROM json_each(?) ids "
                "JOIN calculation_current a ON a.subject_id=ids.value "
                "JOIN calculation c ON c.id=a.calculation_id WHERE c.kind='external_completion'",
                (json.dumps(sorted(related_subjects)),),
            )
        }
        reads.prime_calculations(calculation_ids)
        for ident in sorted(calculation_ids):
            calc = self._calculation(connection, ident)
            values = calc["outcome"].get("values", {})
            references = [
                *values.get("accepted_calculations", ()),
                *values.get("reviewed_calculations", ()),
            ]
            if calc["subject_id"] == subject_id or any(
                item.get("subject_id") == subject_id for item in references
            ):
                completion_count += 1
                if not summary:
                    related_completions.append(
                        {
                            "subject_id": calc["subject_id"],
                            "calculation_id": calc["id"],
                            "fact_id": calc["fact_id"],
                            "obligation_id": values.get("obligation_id"),
                            "completion_status": values.get("completion_status"),
                            "completion_date": values.get("completion_date"),
                            "basis_current": values.get("basis_current"),
                            "accepted_calculations": values.get("accepted_calculations", []),
                            "reviewed_calculations": values.get("reviewed_calculations", []),
                        }
                    )
                if values.get("obligation_id"):
                    related_obligations.add(values["obligation_id"])
        obligations = Workflow(self.engine)._external_obligations(
            connection,
            period,
            as_of,
            reads=reads,
            obligation_ids=related_obligations,
            summary=summary,
        )
        if summary:
            return {
                **obligations,
                "as_of": as_of,
                "as_of_semantics": "current_knowledge",
                "completion_count": completion_count,
            }, obligations
        status = (
            "completed"
            if obligations
            and all(
                item["completion_status"] in {"completed", "not_applicable"} for item in obligations
            )
            else "followup_required"
            if obligations
            else "unestablished"
        )
        return {
            "status": status,
            "as_of": as_of,
            "as_of_semantics": "current_knowledge",
            "obligations": obligations,
            "completions": related_completions,
        }, {"obligations": obligations}

    def _file_jobs(
        self,
        connection,
        subject_id: str | None,
        period,
        *,
        summary=False,
        metadata_only=False,
        job_ids=None,
    ):
        reads = self._reads(connection)
        subjects = (
            None
            if subject_id is None
            else ({subject_id} if isinstance(subject_id, str) else set(subject_id))
        )
        rows = reads.job_rows(subject_id=subject_id, period=period)
        if job_ids is not None:
            selected_ids = set(job_ids)
            rows = [row for row in rows if row["id"] in selected_ids]
        plans, calculation_ids, fact_ids = {}, set(), set()
        for row in rows:
            if row["id"] not in reads.job_plans:
                try:
                    payload = json.loads(row["payload"])
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(payload, dict) or not isinstance(payload.get("plan"), dict):
                    continue
                reads.job_plans[row["id"]] = payload["plan"]
            plan = plans[row["id"]] = reads.job_plans[row["id"]]
            if row["kind"] == "payment_export" and isinstance(plan.get("rows"), list):
                for item in plan["rows"]:
                    if not isinstance(item, dict) or not isinstance(item.get("sources"), list):
                        continue
                    calculation_ids.update(
                        source["calculation_id"]
                        for source in item["sources"]
                        if isinstance(source, dict)
                        and isinstance(source.get("calculation_id"), str)
                    )
            field = "source_versions" if row["kind"] == "tax_import" else "report_fact_ids"
            if isinstance(plan.get(field), list):
                keys = {ident for ident in plan[field] if isinstance(ident, str)}
                fact_ids.update(keys)
                if row["kind"] == "tax_import":
                    calculation_ids.update(keys)
        calculations = (
            {
                row["id"]: row
                for row in connection.execute(
                    "SELECT c.id,c.subject_id,c.outcome FROM json_each(?) ids "
                    "JOIN calculation c ON c.id=ids.value",
                    (json.dumps(sorted(calculation_ids)),),
                )
            }
            if calculation_ids
            else {}
        )
        facts = (
            {
                row["id"]: row
                for row in connection.execute(
                    "SELECT f.id,f.subject_id FROM json_each(?) ids "
                    "JOIN fact_revision f ON f.id=ids.value",
                    (json.dumps(sorted(fact_ids)),),
                )
            }
            if fact_ids
            else {}
        )
        items = []
        total_count = issue_count = 0
        status_counts = {}
        for row in rows:
            if row["id"] not in plans:
                continue
            plan = plans[row["id"]]
            contract_issues = []

            def records(field, *, required=False, plan=plan, issues=contract_issues):
                if field not in plan:
                    if required:
                        issues.append(
                            {
                                "field": f"plan.{field}",
                                "message": "任务计划缺少必填数组",
                            }
                        )
                    return ()
                value = plan.get(field, ())
                if isinstance(value, list):
                    return value
                issues.append(
                    {
                        "field": f"plan.{field}",
                        "message": "任务计划字段不是数组",
                    }
                )
                return ()

            association, references = None, []
            if row["kind"] == "payment_export":
                for export_row in records("rows", required=True):
                    if not isinstance(export_row, dict):
                        contract_issues.append(
                            {"field": "plan.rows", "message": "任务计划行不是对象"}
                        )
                        continue
                    if "sources" not in export_row:
                        contract_issues.append(
                            {
                                "field": "plan.rows.sources",
                                "message": "任务计划行缺少精确来源数组",
                            }
                        )
                        continue
                    sources = export_row.get("sources", ())
                    if not isinstance(sources, list):
                        contract_issues.append(
                            {
                                "field": "plan.rows.sources",
                                "message": "任务计划来源不是数组",
                            }
                        )
                        continue
                    for source in sources:
                        if not isinstance(source, dict):
                            contract_issues.append(
                                {
                                    "field": "plan.rows.sources",
                                    "message": "任务计划来源不是对象",
                                }
                            )
                            continue
                        required_fields = ("subject_id", "calculation_id", "obligation")
                        if any(not isinstance(source.get(field), str) for field in required_fields):
                            contract_issues.append(
                                {
                                    "field": "plan.rows.sources",
                                    "message": "任务计划来源缺少精确业务、计算或义务键",
                                }
                            )
                            continue
                        calculation = calculations.get(source["calculation_id"])
                        if calculation is None or calculation["subject_id"] != source["subject_id"]:
                            contract_issues.append(
                                {
                                    "field": "plan.rows.sources.calculation_id",
                                    "message": "任务计划计算未精确绑定该业务身份",
                                }
                            )
                            continue
                        obligations = (
                            json.loads(calculation["outcome"])
                            .get("values", {})
                            .get("obligations", ())
                        )
                        if not any(
                            isinstance(item, dict) and item.get("key") == source["obligation"]
                            for item in obligations
                        ):
                            contract_issues.append(
                                {
                                    "field": "plan.rows.sources.obligation",
                                    "message": "任务计划义务键不属于精确冻结计算",
                                }
                            )
                            continue
                        if subjects is not None and source["subject_id"] in subjects:
                            references.append(source)
                if references:
                    association = "direct_source"
                elif subject_id is None and str(plan.get("period")) == period:
                    association = "period_scope"
            elif row["kind"] == "tax_import":
                for ident in records("source_versions", required=True):
                    if not isinstance(ident, str):
                        contract_issues.append(
                            {
                                "field": "plan.source_versions",
                                "message": "任务计划来源版本不是字符串",
                            }
                        )
                        continue
                    fact = facts.get(ident)
                    calc = calculations.get(ident)
                    if (
                        subject_id is not None
                        and (fact or calc)
                        and (fact or calc)["subject_id"] in subjects
                    ):
                        references.append(
                            {"id": ident, "source": "fact" if fact else "calculation"}
                        )
                if references:
                    association = "direct_source"
                elif subject_id is None and str(plan.get("period")) == period:
                    association = "period_scope"
            elif row["kind"] == "report_export":
                for ident in records("report_fact_ids", required=True):
                    if not isinstance(ident, str):
                        contract_issues.append(
                            {
                                "field": "plan.report_fact_ids",
                                "message": "报表事实版本不是字符串",
                            }
                        )
                        continue
                    fact = facts.get(ident)
                    if subjects is not None and fact and fact["subject_id"] in subjects:
                        references.append({"id": ident, "source": "fact"})
                if references:
                    association = "direct_source"
                else:
                    scoped = set()
                    for item in records("source_closes", required=True):
                        if not isinstance(item, dict):
                            contract_issues.append(
                                {
                                    "field": "plan.source_closes",
                                    "message": "报表闭期来源不是对象",
                                }
                            )
                            continue
                        if isinstance(item.get("period"), str):
                            scoped.add(item["period"])
                        else:
                            contract_issues.append(
                                {
                                    "field": "plan.source_closes.period",
                                    "message": "报表闭期来源缺少期间",
                                }
                            )
                    report_period = plan.get("period")
                    in_report_period = False
                    if isinstance(report_period, dict):
                        start = report_period.get("quarter_start")
                        end = report_period.get("quarter_end")
                        if isinstance(start, str) and isinstance(end, str):
                            try:
                                in_report_period = (
                                    YearMonth(start[:7]) <= YearMonth(period) <= YearMonth(end[:7])
                                )
                            except ValueError:
                                in_report_period = False
                    if in_report_period or period in scoped:
                        association = "period_scope"
            if association is None:
                continue
            if metadata_only:
                items.append(
                    {
                        "job_id": row["id"],
                        "status": row["status"],
                        "attempts": row["attempts"],
                        "last_error": row["last_error"],
                        "result_record": row["result"],
                        "association": association,
                    }
                )
                continue
            result, result_issue = None, None
            if row["result"]:
                try:
                    result = json.loads(row["result"])
                    if not isinstance(result, dict):
                        result = None
                        result_issue = {
                            "field": "result",
                            "message": "任务结果记录不是对象",
                        }
                except (TypeError, json.JSONDecodeError):
                    result_issue = {
                        "field": "result",
                        "message": "任务结果记录不是有效 JSON",
                    }
            elif row["status"] == "succeeded":
                result_issue = {
                    "field": "result",
                    "message": "成功任务缺少结果记录",
                }
            total_count += 1
            issue_count += bool(result_issue or contract_issues)
            status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1
            if summary:
                continue
            items.append(
                {
                    "job_id": row["id"],
                    "kind": row["kind"],
                    "status": row["status"],
                    "attempts": row["attempts"],
                    "last_error": row["last_error"],
                    "result": result,
                    **({"result_issue": result_issue} if result_issue else {}),
                    **({"contract_issues": contract_issues} if contract_issues else {}),
                    "association": association,
                    "references": references,
                    "period": _plain(plan.get("period")),
                    "verified_when_succeeded": (
                        row["status"] == "succeeded" and result_issue is None
                    ),
                    "current_file_availability": "not_checked",
                }
            )
        if summary:
            return {
                "total_count": total_count,
                "status_counts": status_counts,
                "issue_count": issue_count,
            }
        return items

    def business_status(self, subject_id: str, period: str, *, as_of: str | None = None):
        with self.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            return self._business_status(connection, subject_id, period, as_of=as_of)

    def _business_status(
        self,
        connection,
        subject_id: str,
        period: str,
        *,
        as_of: str | None = None,
        summary=False,
    ):
        """Full public contract inside a caller-owned company read transaction."""
        period, as_of = str(YearMonth(period)), str(ActualDate(as_of or _today_china()))
        subject = connection.execute(
            "SELECT id,kind FROM subject WHERE id=?", (subject_id,)
        ).fetchone()
        if subject is None:
            raise KernelError("unknown_subject", "稳定业务身份不存在")
        current_fact = connection.execute(
            "SELECT f.id FROM fact_current c JOIN fact_revision f ON f.id=c.fact_id "
            "WHERE c.subject_id=?",
            (subject_id,),
        ).fetchone()
        latest_row = (
            current_fact
            or connection.execute(
                "SELECT id FROM fact_revision WHERE subject_id=? ORDER BY revision DESC LIMIT 1",
                (subject_id,),
            ).fetchone()
        )
        latest = self._fact(connection, latest_row[0]) if latest_row else None
        if latest:
            latest["deleted"] = current_fact is None
            latest["knowledge"] = "current_knowledge"
            timestamp = recorded_times(connection, (("fact", latest["id"]),)).get(
                ("fact", latest["id"])
            )
            latest["recorded_at"] = timestamp
        current_publication = self._current_publication(connection, subject_id)
        selected = self._selected_accounting(
            connection, subject_id, period, include_lines=not summary
        )
        pending = [
            {"cause_fact_id": row["cause_id"]}
            for row in connection.execute(
                "SELECT cause_id FROM pending WHERE subject_id=? ORDER BY cause_id",
                (subject_id,),
            )
        ]
        dispositions = (
            []
            if summary
            else [
                _plain(row)
                for row in connection.execute(
                    "SELECT cause_id,action,calculation_id,explanation FROM disposition "
                    "WHERE subject_id=? ORDER BY id",
                    (subject_id,),
                )
            ]
        )
        evaluator = subject["kind"] in self.store.registry.evaluators
        matches = bool(
            latest
            and current_publication
            and current_publication["calculation"]["fact_id"] == latest["id"]
            and not pending
        )
        review_status = (
            "deleted"
            if current_fact is None
            else "not_required"
            if not evaluator
            else "current"
            if matches
            else "review_required"
            if current_publication
            else "unpublished"
        )
        external, _ = self._external(connection, subject_id, period, as_of, summary=summary)
        settlements = self._settlements(connection, subject_id, selected, summary=summary)
        trace_targets = []
        seen = set()
        for item in (
            *selected["through_period"]["voucher_events"],
            *selected["through_period"]["state_results"],
        ):
            target = {
                "calculation_id": item["calculation_id"],
                "voucher_version_id": item.get("voucher_version_id"),
            }
            key = (target["calculation_id"], target["voucher_version_id"])
            if key not in seen:
                trace_targets.append(target)
                seen.add(key)
        for selection in selected["through_period"]["unestablished_state_selections"]:
            for candidate in selection["candidates"]:
                key = (candidate["calculation_id"], None)
                if key not in seen:
                    trace_targets.append(
                        {
                            "calculation_id": candidate["calculation_id"],
                            "voucher_version_id": None,
                            "selection_status": "unestablished",
                        }
                    )
                    seen.add(key)
        result = {
            "identity": {
                "company_id": self.store.company_id,
                "database_id": self.store.database_id,
                "subject_id": subject_id,
                "kind": subject["kind"],
            },
            "period": period,
            "as_of": as_of,
            "latest_fact": latest,
            "current_publication": current_publication,
            "review": {
                "status": review_status,
                "latest_matches_publication": matches,
                "pending_causes": pending,
                "dispositions": dispositions,
            },
            "selected_accounting": selected,
            "settlements": settlements,
            "external": external,
            "file_jobs": self._file_jobs(connection, subject_id, period, summary=summary),
            "display_profiles": self._profiles(connection, subject_id, period),
            "trace_targets": trace_targets,
            "read_semantics": {
                "knowledge": "current_knowledge",
                "accounting": "frozen_close_or_current_published_at_period_end",
                "display": "frozen_with_current_supplements",
                "as_of": "external_deadlines_and_completion_only",
            },
        }
        if summary:
            result["projection"] = "summary"
            result["selected_accounting"] = self._selection_summary(selected)
            result["review"]["disposition_count"] = connection.execute(
                "SELECT count(*) FROM disposition WHERE subject_id=?",
                (subject_id,),
            ).fetchone()[0]
            result["review"]["dispositions"] = []
            result["trace_target_count"] = len(seen)
            result["external"] = self._external_summary(external)
            result["current_followups"] = {
                "settlements": self.settlement_summary(
                    connection, period, subject_ids={subject_id}, current=True
                )
            }
        return result

    @staticmethod
    def _selection_summary(selected):
        through = selected["through_period"]
        return {
            "cutoff_period": selected["cutoff_period"],
            "period_event_count": len(selected["period_events"]),
            "through_period": {
                "status": through["status"],
                "voucher_event_count": len(through["voucher_events"]),
                "state_result_count": len(through["state_results"]),
                "unestablished_state_selection_count": len(
                    through["unestablished_state_selections"]
                ),
                "state_results": through["state_results"],
                "unestablished_state_selections": through["unestablished_state_selections"],
            },
        }

    @staticmethod
    def _jobs_summary(items):
        counts = {}
        for item in items:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        return {
            "total_count": len(items),
            "status_counts": counts,
            "issue_count": sum(
                bool(item.get("result_issue") or item.get("contract_issues")) for item in items
            ),
        }

    @staticmethod
    def _external_summary(external):
        if "obligations" not in external:
            return external
        counts = {}
        for item in external["obligations"]:
            state = item["completion_status"]
            counts[state] = counts.get(state, 0) + 1
        return {
            key: value
            for key, value in external.items()
            if key not in {"obligations", "completions"}
        } | {
            "obligation_count": len(external["obligations"]),
            "completion_status_counts": counts,
        }

    @staticmethod
    def _collection_page(keys, after, limit):
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("每页数量必须为 1 至 500")
        start = 0
        if after is not None:
            try:
                start = keys.index(after) + 1
            except ValueError as exc:
                raise KernelError(
                    "dashboard_snapshot_changed", "分页位置已变化，请重新加载明细。"
                ) from exc
        selected = keys[start : start + limit]
        more = start + len(selected) < len(keys)
        return selected, {
            "total_count": len(keys),
            "filtered_count": len(keys),
            "returned_count": len(selected),
            "has_more": more,
            "next_cursor": selected[-1] if more else None,
        }

    def business_collection(
        self,
        connection,
        subject_id,
        period,
        *,
        section,
        after=None,
        limit=100,
        as_of=None,
        current=False,
    ):
        """Page exact source/event keys; the HTTP adapter binds the opaque cursor."""
        period = str(YearMonth(period))
        as_of = str(ActualDate(as_of or _today_china()))
        reads = self._reads(connection)
        subjects = (
            None
            if subject_id is None
            else ({subject_id} if isinstance(subject_id, str) else set(subject_id))
        )
        if section not in {"events", "settlement_events", "source_history", "file_jobs"}:
            raise ValueError("不支持的业务明细集合")
        if section == "source_history":
            query = "SELECT f.id,f.subject_id,f.revision FROM fact_revision f WHERE "
            if subjects is None:
                query += "f.period=?"
                parameters = [YearMonth(period).ordinal]
            else:
                query += "f.subject_id IN (SELECT value FROM json_each(?))"
                parameters = [json.dumps(sorted(subjects))]
            query += " ORDER BY f.subject_id,f.revision,f.id"
            records = list(connection.execute(query, parameters))
            keys, page = self._collection_page([row["id"] for row in records], after, limit)
            versions = reads.facts(keys)
            times = recorded_times(connection, (("fact", ident) for ident in keys))
            publications = {ident: [] for ident in keys}
            for row in connection.execute(
                "SELECT c.id,c.fact_id FROM json_each(?) ids "
                "CROSS JOIN calculation c INDEXED BY calculation_subject "
                "ON c.subject_id=ids.value "
                "JOIN calculation_publication p ON p.calculation_id=c.id "
                "WHERE c.fact_id IN (SELECT value FROM json_each(?)) ORDER BY c.id",
                (
                    json.dumps(sorted({versions[ident]["subject_id"] for ident in keys})),
                    json.dumps(keys),
                ),
            ):
                publications[row["fact_id"]].append(row["id"])
            items = [
                {
                    **versions[ident],
                    "recorded_at": times.get(("fact", ident)),
                    "trace_targets": [
                        {"calculation_id": calc, "voucher_version_id": None}
                        for calc in publications[ident]
                    ],
                }
                for ident in keys
            ]
            return {"items": items, "page": page}
        if section == "file_jobs":
            metadata = self._file_jobs(connection, subjects, period, metadata_only=True)
            keys, page = self._collection_page([item["job_id"] for item in metadata], after, limit)
            items = self._file_jobs(connection, subjects, period, job_ids=keys)
            page["collection_version"] = digest(metadata).hex()
            return {"items": items, "page": page}
        if current and section == "settlement_events":
            subjects, selected, cutoff = self._current_settlement_selection(
                connection, period, subject_ids=subjects
            )
            return {
                **self._settlement_collection(
                    connection, subjects, selected, after=after, limit=limit
                ),
                "scope_period": period,
                "current_cutoff_period": cutoff,
                "cutoff_semantics": "current_published_relations_independent_of_as_of",
            }
        if section == "settlement_events" and subjects is None:
            subjects = reads.settlement_subjects(period)
        related = (
            reads.related_subjects(subjects)
            if section == "settlement_events" and subjects is not None
            else subjects
        )
        selected = self._selected_accounting(
            connection,
            related,
            period,
            include_lines=False,
            posting_period=period if section == "events" and subjects is None else None,
        )
        if section == "settlement_events":
            return self._settlement_collection(
                connection, subjects, selected, after=after, limit=limit
            )
        through = selected["through_period"]
        events = [
            *through["voucher_events"],
            *through["state_results"],
            *through["unestablished_state_selections"],
        ]
        if subjects is None:
            events = [item for item in events if item["posting_period"] == period]
        events.sort(
            key=lambda item: (
                item["posting_period"],
                item.get("voucher_number", 0),
                item.get("calculation_id", item.get("subject_id", "")),
            )
        )
        indexed = {}
        for item in events:
            key = (
                "voucher:" + item["voucher_version_id"]
                if item.get("voucher_version_id")
                else "state:" + item["calculation_id"]
                if item.get("calculation_id")
                else "selection:" + item["posting_period"] + ":" + item["subject_id"]
            )
            indexed[key] = item
        keys, page = self._collection_page(list(indexed), after, limit)
        lines = reads.voucher_lines(
            indexed[key]["voucher_version_id"]
            for key in keys
            if indexed[key].get("voucher_version_id")
        )
        items = [
            {
                "id": key,
                **indexed[key],
                **(
                    {"lines": lines[indexed[key]["voucher_version_id"]]}
                    if indexed[key].get("voucher_version_id")
                    else {}
                ),
            }
            for key in keys
        ]
        return {"items": items, "page": page}

    def _settlement_collection(self, connection, subjects, selected, *, after, limit):
        """Select normalized declared slots, then run the same resolver on page calculations."""
        reads = self._reads(connection)
        events = [
            *selected["through_period"]["voucher_events"],
            *selected["through_period"]["state_results"],
        ]
        event_map = {}
        for item in events:
            key = item.get("voucher_version_id") or ("state:" + item["calculation_id"])
            event_map[key] = item
        metadata = reads.metadata({item["calculation_id"] for item in events}, state=False)
        # These are structural source slots, not an alternate settlement reducer.
        # Payment's resolver zips typed allocations with frozen settlement slots;
        # acceptance/offset resolvers enumerate their immutable typed declarations.
        slots = []
        for kind, (child, frozen, pointer) in SETTLEMENT_SOURCE_SLOTS.items():
            group = [
                [key, item["calculation_id"]]
                for key, item in event_map.items()
                if item["kind"] == kind
            ]
            if not group or kind not in self.store.registry.models:
                continue
            query = (
                "SELECT json_extract(e.value,'$[0]') AS event_id,t.item_no,"
                "source.subject_id AS frozen_source_subject_id,"
                "t.source_id AS declared_source_subject_id "
                "FROM json_each(?) e JOIN calculation c ON c.id=json_extract(e.value,'$[1]') "
                f"JOIN fact_{kind}_{child} t ON t.revision_id=c.fact_id "
                "LEFT JOIN calculation source ON source.id=json_extract(c.outcome,"
                f"'$.values.{frozen}['||t.item_no||'].{pointer}') WHERE 1=1"
            )
            if child == "allocations":
                query += " AND t.item_no<json_array_length(c.outcome,'$.values.settlements')"
            for row in connection.execute(query, (json.dumps(group),)):
                event = event_map[row["event_id"]]
                calc_subject = metadata[event["calculation_id"]]["subject_id"]
                if (
                    subjects is None
                    or row["frozen_source_subject_id"] in subjects
                    or row["declared_source_subject_id"] in subjects
                    or calc_subject in subjects
                ):
                    slots.append((row["event_id"], row["item_no"]))
        group = [
            [key, item["calculation_id"]]
            for key, item in event_map.items()
            if item["kind"] == "settlement"
        ]
        if group and "settlement" in self.store.registry.models:
            for row in connection.execute(
                "SELECT json_extract(e.value,'$[0]') AS event_id,n.value AS item_no,"
                "json_extract(CASE n.value WHEN 0 THEN f.first ELSE f.second END,"
                "'$.source_id') AS source_id "
                "FROM json_each(?) e JOIN calculation c ON c.id=json_extract(e.value,'$[1]') "
                "JOIN fact_settlement f ON f.revision_id=c.fact_id CROSS JOIN json_each('[0,1]') n",
                (json.dumps(group),),
            ):
                event = event_map[row["event_id"]]
                calc_subject = metadata[event["calculation_id"]]["subject_id"]
                if subjects is None or row["source_id"] in subjects or calc_subject in subjects:
                    slots.append((row["event_id"], row["item_no"]))
        slots.sort(
            key=lambda slot: (
                event_map[slot[0]]["posting_period"],
                event_map[slot[0]].get("voucher_number", 0),
                slot[0],
                slot[1],
            )
        )
        by_key = {json.dumps(slot, separators=(",", ":")): slot for slot in slots}
        keys, page = self._collection_page(list(by_key), after, limit)
        reads.prime_calculations({event_map[by_key[key][0]]["calculation_id"] for key in keys})
        items = []
        for key in keys:
            event_id, index = by_key[key]
            event = event_map[event_id]
            resolution = reads.relations(
                event["calculation_id"], resolver=resolve_calculation_relations
            )
            movement = resolution["settlements"][index]
            direction = event.get("direction", 1)
            amount = movement.get("amount_fen")
            items.append(
                {
                    "id": key,
                    **movement,
                    "posting_period": event["posting_period"],
                    "voucher_version_id": event.get("voucher_version_id"),
                    "direction": direction,
                    "signed_amount_fen": direction * amount if type(amount) is int else None,
                    "issues": resolution["issues"],
                }
            )
        return {"items": items, "page": page}

    def _period_readiness(
        self,
        connection,
        period: str,
        *,
        as_of: str | None = None,
        summary=False,
    ):
        """Compose readiness inside a caller-owned read snapshot."""
        period, as_of = str(YearMonth(period)), str(ActualDate(as_of or _today_china()))
        month = YearMonth(period).ordinal
        exact = connection.execute(
            "SELECT period,manifest,digest FROM period_close WHERE period=?", (month,)
        ).fetchone()
        later = connection.execute(
            "SELECT period,digest FROM period_close WHERE period>? ORDER BY period LIMIT 1",
            (month,),
        ).fetchone()
        periods = Periods(self.engine)
        if exact:
            manifest = json.loads(exact["manifest"])
            closure = {"state": "exact_close", "digest": exact["digest"].hex()}
            frozen = {
                "status": "ready",
                "source": "exact_period_manifest",
                **{
                    key: (
                        {"status": "recorded", "value": manifest[key]}
                        if key in manifest
                        else {"status": "not_recorded"}
                    )
                    for key in (
                        "readiness",
                        "inventories",
                        "material_coverage",
                        "previous_close_digest",
                    )
                },
            }
            readiness = None
            current = periods.collect_current_readiness(connection, period)
        elif later:
            closure = {
                "state": "sealed_by_later_close",
                "sealing_boundary": str(YearMonth.from_ordinal(later["period"])),
                "sealing_digest": later["digest"].hex(),
            }
            frozen = {
                "status": "unavailable",
                "reason": "no_exact_period_manifest",
            }
            readiness = None
            current = periods.collect_current_readiness(connection, period)
        else:
            closure = {"state": "open"}
            readiness = periods.check_readiness(connection, period)
            frozen = None
            current = (
                periods.collect_current_readiness(connection, period)
                if readiness.get("order_failure") is not None
                else readiness
            )
        if summary:
            external = Workflow(self.engine)._external_obligations(
                connection,
                period,
                as_of,
                reads=self._reads(connection),
                summary=True,
            )
            external["fact_issues"] = [] if exact else list(current["issues"])
            checked = (
                readiness
                if readiness is not None
                else (periods.check_readiness(connection, period) if not exact else None)
            )
            if checked and checked.get("order_failure"):
                failure = checked["order_failure"]
                external["fact_issues"].append(
                    {
                        "field": "period",
                        "code": failure["code"],
                        "message": failure["message"],
                        **failure["details"],
                    }
                )
        else:
            workflow = Workflow(self.engine)._query(
                connection,
                period,
                as_of=as_of,
                period_readiness=readiness if closure["state"] == "open" else None,
                reads=self._reads(connection),
            )
            external = {
                "status": (
                    "completed"
                    if workflow["obligations"]
                    and all(
                        item["completion_status"] in {"completed", "not_applicable"}
                        for item in workflow["obligations"]
                    )
                    else "followup_required"
                    if workflow["obligations"]
                    else "unestablished"
                ),
                "obligations": workflow["obligations"],
                "fact_issues": workflow["fact_issues"],
            }
        current_followups = {
            "knowledge": "current_knowledge",
            "affects_frozen_readiness": False,
            "materials": _plain(current["materials"]),
            "accounting": _plain(current["accounting"]),
            "close_requirements": _plain(current["close_requirements"]),
            "settlements": (
                self.settlement_summary(connection, period, current=True)
                if summary
                else self._current_settlement_followups(connection, period)
            ),
            "external": external,
            "file_jobs": self._file_jobs(connection, None, period, summary=summary),
        }
        result = {
            "company_id": self.store.company_id,
            "database_id": self.store.database_id,
            "period": period,
            "as_of": as_of,
            "as_of_semantics": "current_knowledge",
            "closure": closure,
            "frozen_readiness": frozen,
            "readiness": _plain(readiness) if readiness is not None else None,
            "current_followups": current_followups,
            "read_semantics": {
                "knowledge": "current_knowledge",
                "frozen_readiness": "exact_period_manifest_only",
                "current_followups": "never_changes_frozen_readiness",
            },
        }
        if summary:
            result["projection"] = "summary"
            result["current_followups"]["external"] = self._external_summary(
                current_followups["external"]
            )
        return result

    def period_readiness(self, period: str, *, as_of: str | None = None):
        with self.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            return self._period_readiness(connection, period, as_of=as_of)
