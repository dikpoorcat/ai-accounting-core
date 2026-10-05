"""Shared read-only business status and period readiness queries.

The query month is an accounting cut-off.  Current fact and publication heads
are reported separately so a later review never rewrites the selected history.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .contracts import KernelError, Read
from .diagnostics import job_error_message, public_job_code
from .display import _KINDS, Display
from .periods import Periods
from .provenance import recorded_times
from .query_reads import QueryReads, selected_voucher_sql, verify_current_publication_voucher_heads
from .query_semantics import (
    SETTLEMENT_SOURCE_SLOTS,
    project_settlement_followup,
    resolve_calculation_relations,
)
from .stored_json import verify_outcome_bytes
from .types import ActualDate, YearMonth, digest, is_sha256_hex
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
_PAYROLL_KINDS = {"payroll", "payroll_bounded"}
_PAYROLL_CONFIRMATION_KINDS = {
    "monthly_plan": {"payroll_plan_v2", "payroll_plan_bounded"},
    "explicit_no_change": {"payroll_no_change_v2"},
}


def business_display_amount(calculation):
    """Return the explicit owner-facing amount for one exact calculation version."""

    data = calculation["fact"]["data"]
    values = calculation["outcome"]["values"]
    spec = {
        "service_sale": ("gross_fen", "含税收入确认额"),
        "payroll": ("gross_fen", "税前工资"),
        "payroll_bounded": ("gross_fen", "税前工资"),
        "annual_bonus": ("gross_fen", "税前奖金"),
        "labor": ("gross_fen", "劳务确认毛额"),
        "labor_accrual": ("gross_fen", "劳务确认毛额"),
        "labor_project_cost": ("gross_fen", "资本化劳务确认毛额"),
        "asset": ("cost_fen", "已确认资产成本"),
        "reimbursed_asset": ("cost_fen", "已确认资产成本"),
        "reimbursed_asset_batch": ("cost_fen", "整批确认成本"),
        "asset_activation": ("cost_fen", "启用资产成本"),
        "asset_consumption": ("consumption_fen", "本期折旧摊销"),
        "asset_activation_batch": ("amount_fen", "本批启用资产成本"),
        "asset_consumption_month": ("amount_fen", "本月折旧摊销"),
        "asset_disposal": ("gross_proceeds_fen", "处置确认价款"),
        "loan_interest": ("interest_fen", "本期确认利息"),
        "loan_drawdown": ("principal_fen", "借款本金"),
        "project_release": ("released_fen", "转费用成本"),
        "money_fund_subscription": ("cost_fen", "申购确认成本"),
        "money_fund_redemption": ("net_proceeds_fen", "赎回结算额"),
        "income_tax_assessment": ("change_fen", "本期所得税确认额"),
        "platform_expense_confirmation": ("confirmed_amount_fen", "确认费用"),
        "managed_reserve_expense": ("amount_fen", "备用金实际支出"),
        "managed_reserve_refund": ("amount_fen", "备用金实际退款"),
        "managed_reserve_internal_movement": (
            "original_amount_fen",
            "备用金内部原行金额合计（不入公司账）",
        ),
    }
    field, label = spec.get(calculation["kind"], ("amount_fen", "业务确认金额"))
    amount = values.get(field, data.get(field))
    if calculation["kind"] == "employee_advance":
        obligations = values.get("obligations", ())
        amount = obligations[0]["amount_fen"] if len(obligations) == 1 else None
        label = "代付转债确认额"
    elif calculation["kind"] in {
        "payment",
        "cash_payment",
        "platform_payment",
        "payroll_reserve_payment",
    }:
        label = "实际收付款"
    elif calculation["kind"] in {"funding", "cash_funding", "platform_funding"}:
        label = "实际投入或借入金额"
    elif calculation["kind"] == "bank_platform_transfer":
        label = "内部划转金额"
    return (amount if type(amount) is int else None), label


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


def _file_job_period(value):
    """Project the stored report period into the labeled public read contract."""
    period = _plain(value)
    if (
        isinstance(period, dict)
        and type(period.get("year")) is int
        and type(period.get("quarter")) is int
        and 1 <= period["year"] <= 9999
        and 1 <= period["quarter"] <= 4
    ):
        period["label"] = f"{period['year']} 年第 {period['quarter']} 季度"
    return period


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

    def _external_obligation_ids_for_period(self, connection, period):
        """Return current external obligations whose declared interval covers the month."""
        if "external_obligation" not in self.store.registry.models:
            return set()
        month = YearMonth(period).ordinal
        return {
            row[0]
            for row in connection.execute(
                "SELECT a.subject_id FROM fact_external_obligation f "
                "JOIN fact_current a ON a.fact_id=f.revision_id "
                "WHERE f.start_period<=? AND f.end_period>=?",
                (month, month),
            )
        }

    def _fact(self, connection, fact_id):
        return dict(self._reads(connection).fact(fact_id))

    def _calculation(self, connection, calculation_id):
        return self._reads(connection).calculation(calculation_id)

    def _payroll_confirmation_source(self, connection, calculation):
        """Present the exact confirmation source retained by a published wage result."""

        if calculation["kind"] not in _PAYROLL_KINDS:
            return None
        values = calculation["outcome"].get("values")
        if not isinstance(values, dict):
            raise KernelError(
                "content_integrity_failed",
                "正式工资计算缺少有效结果内容",
                component="business_status",
                calculation_id=calculation["id"],
            )
        if (
            values.get("superseded") is True
            and isinstance(values.get("identity_correction"), str)
            and values.get("identity_correction")
            and values.get("obligations") == []
            and calculation["outcome"].get("lines") == []
            and calculation["outcome"].get("balances") == []
        ):
            return None
        confirmation = values.get("payroll_confirmation")
        if not isinstance(confirmation, dict):
            raise KernelError(
                "content_integrity_failed",
                "正式工资计算缺少工资确认来源",
                component="business_status",
                calculation_id=calculation["id"],
            )
        mode = confirmation.get("mode")
        fact_id = confirmation.get("confirmation_fact_id")
        subject_id = confirmation.get("confirmation_subject_id")
        revision = confirmation.get("confirmation_revision")
        if (
            mode not in _PAYROLL_CONFIRMATION_KINDS
            or not isinstance(fact_id, str)
            or not fact_id
            or not isinstance(subject_id, str)
            or not subject_id
            or type(revision) is not int
            or revision < 1
        ):
            raise KernelError(
                "content_integrity_failed",
                "正式工资计算的工资确认来源无效",
                component="business_status",
                calculation_id=calculation["id"],
            )
        try:
            source = self._fact(connection, fact_id)
        except (KeyError, ValueError):
            raise KernelError(
                "content_integrity_failed",
                "正式工资计算引用的确认事实不存在或无效",
                component="business_status",
                calculation_id=calculation["id"],
                confirmation_fact_id=fact_id,
            ) from None
        if (
            source["subject_id"] != subject_id
            or source["revision"] != revision
            or source["kind"] not in _PAYROLL_CONFIRMATION_KINDS[mode]
            or not source["evidence"]
        ):
            raise KernelError(
                "content_integrity_failed",
                "正式工资计算引用的确认事实与保存来源不匹配",
                component="business_status",
                calculation_id=calculation["id"],
                confirmation_fact_id=fact_id,
            )
        return {
            "mode": mode,
            "confirmation_fact_id": fact_id,
            "confirmation_subject_id": subject_id,
            "confirmation_revision": revision,
            "confirmation_kind": source["kind"],
            "evidence": list(source["evidence"]),
        }

    def _parents(self, connection, calculation_id):
        return self._reads(connection).parents(calculation_id)

    def _profiles(self, connection, subject_id, period, *, include_sources=True):
        current_profiles = {kind: {} for kind in _KINDS}
        for row in connection.execute(
            "SELECT p.* FROM json_each(?) kinds JOIN display_profile_revision p "
            "ON p.kind=kinds.value AND p.entity_id=? WHERE p.revision=("
            "SELECT max(q.revision) FROM display_profile_revision q "
            "WHERE q.kind=p.kind AND q.entity_id=p.entity_id)",
            (json.dumps(_KINDS), subject_id),
        ):
            current_profiles[row["kind"]][subject_id] = Display._record(row)
        closes = self._reads(connection).authoritative_close_rows(
            periods=[YearMonth(period).ordinal]
        )
        closed = bool(closes)
        frozen_profiles = {kind: {} for kind in _KINDS} if closed else current_profiles
        if closed:
            identifiers = [
                item["id"]
                for item in self._reads(connection)
                .close_section(closes[0], "management_snapshot")
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
                if not include_sources:
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
        times = recorded_times(connection, references) if include_sources else {}
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
        _owner_outcomes=None,
    ):
        """Select exact metadata first; payloads are loaded only by consumers.

        A kind or subject restriction narrows candidate closes and their verified
        accounting buckets. Unrelated historical material bodies are not read.
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
        from . import close_storage
        from .content_history_context import close_reader

        subjects_by_period = None
        if (
            subjects is not None and close_reader() is close_storage
            and getattr(self.store.registry, "content_version", None) != 1
        ):
            # Publication periods locate possible adoptions, including reviews
            # and withdrawn terminals. Independently include physical current
            # voucher owners: their original/reversal month need not be the
            # latest adopted calculation's publication month. The complete
            # reader still checks both authorities against the full subjects.
            subjects_by_period = {}
            month_clause = " AND p.posting_period=?" if posting_period else ""
            voucher_month_clause = " AND v.period=?" if posting_period else ""
            encoded_subjects = json.dumps(sorted(subjects))
            publication_parameters = (
                encoded_subjects, cutoff.ordinal,
                *((YearMonth(posting_period).ordinal,) if posting_period else ()),
            )
            for row in connection.execute(
                "SELECT p.posting_period,p.subject_id FROM json_each(?) requested "
                "CROSS JOIN calculation_publication p INDEXED BY publication_subject "
                "ON p.subject_id=requested.value WHERE p.posting_period<=?" + month_clause
                + " UNION SELECT v.period,c.subject_id FROM json_each(?) requested "
                "CROSS JOIN calculation c INDEXED BY calculation_subject "
                "ON c.subject_id=requested.value "
                "CROSS JOIN voucher_version v INDEXED BY voucher_calculation "
                "ON v.calculation_id=c.id "
                "CROSS JOIN voucher_current h ON h.version_id=v.id "
                "WHERE v.period<=?" + voucher_month_clause,
                publication_parameters * 2,
            ):
                subjects_by_period.setdefault(row[0], set()).add(row[1])
            if proof_periods is None:
                # A missing publication must not also remove its frozen month
                # from the proof. Scoped accounting uses the authenticated
                # subject filter to rule out all other requested subjects.
                proof_periods = tuple(row[0] for row in connection.execute(
                    "SELECT period FROM period_close WHERE period<=? ORDER BY period",
                    (cutoff.ordinal,),
                ))
        elif proof_periods is None and subjects is not None:
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
            selections = []
        else:
            # Current scoped readers independently locate frozen leaves;
            # released readers retain their original publication-period rule.
            selected_periods = proof_periods
            if selected_periods is None:
                selected_periods = tuple(
                    row[0]
                    for row in connection.execute(
                        "SELECT period FROM period_close WHERE period<=? ORDER BY period",
                        (cutoff.ordinal,),
                    )
                )
            close_rows = reads.authoritative_close_rows(
                periods=selected_periods, through_period=cutoff.ordinal
            )
            selections = []
            if subjects is not None:
                selected_slices = reads.close_accounting_many(
                    close_rows, subjects=subjects,
                    **(
                        {"subjects_by_period": {
                            row["period"]: subjects_by_period.get(row["period"], set())
                            for row in close_rows
                        }} if subjects_by_period is not None else {}
                    ),
                )
            else:
                selected_slices = (None for _ in close_rows)
            for row, selected_close in zip(close_rows, selected_slices, strict=True):
                if selected_close is not None:
                    adopted, vouchers = selected_close.adopted_results, selected_close.vouchers
                else:
                    adopted = reads.close_section(row, "adopted_results")
                    vouchers = reads.close_section(row, "vouchers")
                selections.append((row["period"], adopted, vouchers))
        adopted_by_period = {
            close_period: {item["calculation_id"]: item for item in adopted}
            for close_period, adopted, _ in selections
        }
        voucher_by_period = {
            close_period: {item["id"]: item for item in vouchers}
            for close_period, _, vouchers in selections
        }
        authoritative_vouchers = None
        if proof_periods is not None:
            authoritative_vouchers = {}
            for close_period, vouchers in voucher_by_period.items():
                for ident in vouchers:
                    authoritative_vouchers[ident] = min(
                        close_period, authoritative_vouchers.get(ident, close_period)
                    )
        events = []
        represented = set()
        # Even state-only reads require voucher root identity to avoid treating
        # an already represented calculation as an independent no-line state.
        sql, parameters = selected_voucher_sql(
            cutoff,
            current_heads=current_heads,
            subject_ids=subjects,
            posting_period=posting_period,
            authoritative_vouchers=authoritative_vouchers,
        )
        selected_rows = list(connection.execute(sql, parameters))
        if close_reader() is close_storage and {
            (row["close_period"], row["id"]) for row in selected_rows
            if row["close_period"] is not None
        } != {
            (close_period, ident) for close_period, vouchers in voucher_by_period.items()
            for ident in vouchers
        }:
            raise KernelError("content_integrity_failed", "所选冻结凭证集合与独立采用不一致")
        reads.verify_selected_voucher_adoptions(selected_rows, through_period=cutoff.ordinal)
        represented.update(row["basis_calculation_id"] for row in selected_rows)
        state_adoptions = [
            (close_period, adopted)
            for close_period, adopted_rows, _ in selections
            for adopted in adopted_rows
            if adopted["role"] != "journal_basis"
            and (subjects is None or adopted["subject_id"] in subjects)
        ]
        metadata = reads.metadata(
            {adopted["calculation_id"] for _, adopted in state_adoptions},
            _decoded_outcomes=_owner_outcomes,
        )
        if include_vouchers:
            voucher_adoptions = {
                voucher_by_period[row["close_period"]][row["id"]]["adopted_calculation_id"]
                for row in selected_rows
                if row["close_period"] is not None
                and row["id"] in voucher_by_period[row["close_period"]]
            }
            if _owner_outcomes is not None:
                # The preceding exact state metadata batch already checked
                # these bodies in this call; a voucher can adopt the same owner.
                voucher_adoptions.difference_update(metadata)
            reads.metadata(
                voucher_adoptions,
                state=_owner_outcomes is not None,
                _decoded_outcomes=_owner_outcomes,
            )
            lines = reads.voucher_lines(row["id"] for row in selected_rows) if include_lines else {}
            for row in selected_rows:
                calculation_id = row["basis_calculation_id"]
                fact_id = row["basis_fact_id"]
                kind = row["basis_kind"]
                calculation_period = row["calculation_period"]
                result_digest = row["result_digest"].hex()
                if row["close_period"] is not None:
                    frozen_voucher = voucher_by_period[row["close_period"]].get(row["id"])
                    if frozen_voucher is None:
                        raise KernelError(
                            "content_integrity_failed",
                            "关账凭证缺少直接采用依据",
                            component="close",
                            record_id=row["id"],
                            reason="voucher_adoption_missing",
                        )
                    adopted_calculation_id = frozen_voucher["adopted_calculation_id"]
                    adopted = adopted_by_period[row["close_period"]].get(adopted_calculation_id)
                    calc = reads.metadata({adopted_calculation_id}, state=False)[
                        adopted_calculation_id
                    ]
                    if adopted is None or any(
                        (
                            adopted["publication_id"] != calc["publication_id"],
                            adopted["subject_id"] != calc["subject_id"],
                            adopted["fact_id"] != calc["fact_id"],
                            adopted["source_period"] != calc["period"],
                            adopted["posting_period"] != calc["posting_period"],
                            adopted["result_digest"] != calc["result_digest"],
                            frozen_voucher["result_digest"] != calc["result_digest"],
                        )
                    ):
                        raise KernelError(
                            "content_integrity_failed",
                            "关账凭证的直接采用依据不匹配",
                            component="close",
                            record_id=row["id"],
                            reason="voucher_adoption_mismatch",
                        )
                    if row["reverses_id"] is None:
                        calculation_id = adopted_calculation_id
                        fact_id = calc["fact_id"]
                        kind = calc["kind"]
                        calculation_period = YearMonth(calc["period"]).ordinal
                        result_digest = calc["result_digest"]
                        represented.add(calculation_id)
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
                        "calculation_id": calculation_id,
                        "fact_id": fact_id,
                        "kind": kind,
                        "calculation_period": str(YearMonth.from_ordinal(calculation_period)),
                        "posting_period": str(YearMonth.from_ordinal(row["period"])),
                        "result_digest": result_digest,
                        "role": role,
                        "direction": -1 if role == "reversal" else 1,
                        "reverses_voucher_version_id": row["reverses_id"],
                        "selection_source": row["selection_source"],
                        **({"lines": lines[row["id"]]} if include_lines else {}),
                    }
                )
        closed_states = []
        for close_period, adopted in state_adoptions:
            ident = adopted["calculation_id"]
            calc = metadata[ident]
            if kinds is not None and calc["kind"] not in kinds:
                continue
            if any(
                (
                    adopted["publication_id"] != calc["publication_id"],
                    adopted["subject_id"] != calc["subject_id"],
                    adopted["fact_id"] != calc["fact_id"],
                    adopted["source_period"] != calc["period"],
                    adopted["posting_period"] != calc["posting_period"],
                    adopted["result_digest"] != calc["result_digest"],
                )
            ):
                raise KernelError(
                    "content_integrity_failed",
                    "关账直接采用的计算身份不匹配",
                    component="close",
                    record_id=ident,
                    reason="direct_adoption_mismatch",
                )
            if not calc["line_count"] and ident not in represented:
                closed_states.append(
                    self._state_metadata(
                        calc,
                        "close_manifest",
                        {
                            "basis": "direct_adoption",
                            "close_period": str(YearMonth.from_ordinal(close_period)),
                            "publication_id": adopted["publication_id"],
                            "role": adopted["role"],
                        },
                    )
                )
        if (close_reader() is close_storage
                and getattr(self.store.registry, "content_version", None) != 1):
            currents = self._current_accounting_heads(
                connection, subjects, cutoff=cutoff.ordinal, current_heads=current_heads,
                posting_period=YearMonth(posting_period).ordinal if posting_period else None,
            )
        else:
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
                query += (" AND NOT EXISTS(SELECT 1 FROM period_close z "
                          "WHERE z.period=p.posting_period)")
            if subjects is not None:
                query += " AND c.subject_id IN (SELECT value FROM json_each(?))"
                parameters.append(json.dumps(sorted(subjects)))
            currents = list(connection.execute(query, parameters))
        metadata.update(reads.metadata(
            {row["id"] for row in currents}, _decoded_outcomes=_owner_outcomes,
        ))
        state_results = {}
        for row in currents:
            calc = metadata[row["id"]]
            if calc["kind"] not in NON_ACCOUNTING_CALCULATIONS and not calc["line_count"]:
                state_results[calc["id"]] = self._state_metadata(
                    calc, "current_publication", {"basis": "calculation_current"}
                )
        for state in closed_states:
            state_results[state["calculation_id"]] = state
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
        status = "established" if established else "not_established"
        return {
            "cutoff_period": str(cutoff),
            "period_events": period_events,
            "through_period": {
                "status": status,
                "voucher_events": events,
                "state_results": states,
                "unestablished_state_selections": [],
            },
        }

    def _current_accounting_heads(
        self, connection, subjects=None, *, cutoff=None, posting_period=None,
        current_heads=True, include_members=False,
    ):
        """Locate current identities before missing publications can filter them.

        Closed snapshots consume their independent frozen heads. Existing later
        publications bound irrelevant current sources before body hydration;
        member calculations retain their separate exact owner adoption.
        """
        reads = self._reads(connection)
        if subjects is not None and not subjects:
            return []
        latest_close = connection.execute("SELECT max(period) FROM period_close").fetchone()[0]
        if (not current_heads and cutoff is not None and latest_close is not None
                and cutoff <= latest_close):
            return []
        query = (
            "SELECT a.subject_id requested_subject,a.calculation_id requested_id,"
            "c.id,c.subject_id,c.fact_id,c.kind,c.period,c.digest,f.subject_id fact_subject,"
            "f.period fact_period,s.kind fact_kind,p.id publication_id,p.posting_period,"
            "EXISTS(SELECT 1 FROM period_close z WHERE z.period=p.posting_period) closed "
            "FROM calculation_current a LEFT JOIN calculation c ON c.id=a.calculation_id "
            "LEFT JOIN fact_revision f ON f.id=c.fact_id LEFT JOIN subject s ON s.id=f.subject_id "
            "LEFT JOIN calculation_publication p ON p.calculation_id=a.calculation_id"
        )
        parameters = []
        if subjects is not None:
            query += " WHERE a.subject_id IN (SELECT value FROM json_each(?))"
            parameters.append(json.dumps(sorted(subjects)))
        candidates = list(connection.execute(query, parameters))
        from .content_history_context import publication_reader

        publications = {
            row["id"]: row for row in connection.execute(
                "SELECT p.* FROM json_each(?) ids "
                "JOIN calculation_publication p ON p.id=ids.value",
                (json.dumps(sorted({row["publication_id"] for row in candidates
                                    if row["publication_id"] is not None})),),
            )
        }
        terminal_query = (
            "SELECT p.*,a.calculation_id current_id,c.id calculation_exists "
            "FROM calculation_publication p "
            "LEFT JOIN calculation_current a ON a.subject_id=p.subject_id "
            "LEFT JOIN calculation c ON c.id=p.calculation_id "
            "WHERE NOT EXISTS(SELECT 1 FROM calculation_publication n "
            "WHERE n.previous_publication_id=p.id)"
        )
        terminal_parameters = []
        if subjects is not None:
            terminal_query += " AND p.subject_id IN (SELECT value FROM json_each(?))"
            terminal_parameters.append(json.dumps(sorted(subjects)))
        if cutoff is not None:
            terminal_query += " AND p.posting_period<=?"
            terminal_parameters.append(cutoff)
        if posting_period is not None:
            terminal_query += " AND p.posting_period=?"
            terminal_parameters.append(posting_period)
        if not current_heads:
            terminal_query += (" AND NOT EXISTS(SELECT 1 FROM period_close z "
                               "WHERE z.period=p.posting_period)")
        for terminal in connection.execute(terminal_query, terminal_parameters):
            publication_reader().verify_record(terminal)
            if terminal["mode"] == "withdrawn":
                if (terminal["calculation_id"] is not None or terminal["voucher_id"] is not None
                        or terminal["current_id"] is not None):
                    raise KernelError("content_integrity_failed", "撤去发布与当前业务头不一致")
            elif (terminal["calculation_exists"] is None
                  or terminal["current_id"] != terminal["calculation_id"]):
                raise KernelError("content_integrity_failed", "正式发布缺少精确当前核算头")
        result = []
        unpublished_members = {}
        for row in candidates:
            # A real later publication keeps this current source out of an
            # older query. Authenticate its period before using that boundary.
            if row["publication_id"] is not None:
                publication = publications[row["publication_id"]]
                publication_reader().verify_record(publication)
                if publication["subject_id"] != row["requested_subject"]:
                    raise KernelError("content_integrity_failed", "当前发布与请求业务身份不一致")
                if ((cutoff is not None and row["posting_period"] > cutoff)
                        or posting_period is not None and row["posting_period"] != posting_period
                        or not current_heads and row["closed"]):
                    continue
            if row["id"] is None or (
                row["subject_id"], row["kind"], row["period"]
            ) != (row["fact_subject"], row["fact_kind"], row["fact_period"]) or (
                row["requested_subject"] != row["subject_id"]
            ):
                raise KernelError("content_integrity_failed", "当前核算头缺失或来源身份不一致")
            if row["publication_id"] is None:
                # A genuinely future source is not consumed by this cutoff.
                if cutoff is not None and row["period"] > cutoff:
                    continue
                if row["kind"] not in {"asset_activation", "asset_consumption"}:
                    raise KernelError("content_integrity_failed", "当前核算头缺少正式发布")
                unpublished_members[row["id"]] = row
            else:
                result.append(row)
        verify_current_publication_voucher_heads(
            connection,
            (publications[row["publication_id"]] for row in result if not row["closed"]),
        )
        if unpublished_members:
            owners_by_member = {ident: set() for ident in unpublished_members}
            selected_members = set()
            selected_owners = set()
            selected_owner_members = {}
            open_owner_publications = {}
            for owner in connection.execute(
                "SELECT m.member_calculation_id,m.owner_calculation_id,"
                "c.id owner_exists,c.kind owner_kind,c.subject_id owner_subject,"
                "a.subject_id current_subject,p.*,"
                "EXISTS(SELECT 1 FROM period_close z "
                "WHERE z.period=p.posting_period) owner_closed "
                "FROM json_each(?) ids JOIN asset_batch_member m "
                "ON m.member_calculation_id=ids.value "
                "JOIN calculation_current a ON a.calculation_id=m.owner_calculation_id "
                "LEFT JOIN calculation c ON c.id=m.owner_calculation_id "
                "LEFT JOIN calculation_publication p ON p.calculation_id=m.owner_calculation_id",
                (json.dumps(sorted(unpublished_members)),),
            ):
                if (owner["owner_exists"] is None or owner["id"] is None
                        or owner["owner_kind"] not in {
                            "asset_activation_batch", "asset_consumption_month",
                        } or owner["owner_subject"] != owner["current_subject"]
                        or owner["subject_id"] != owner["current_subject"]):
                    raise KernelError("content_integrity_failed", "当前资产所有者缺少精确正式发布")
                publication_reader().verify_record(owner)
                ident = owner["member_calculation_id"]
                owners_by_member[ident].add(owner["owner_calculation_id"])
                if not (
                    cutoff is not None and owner["posting_period"] > cutoff
                    or posting_period is not None and owner["posting_period"] != posting_period
                    or not current_heads and owner["owner_closed"]
                ):
                    selected_members.add(ident)
                    selected_owners.add(owner["owner_calculation_id"])
                    selected_owner_members.setdefault(
                        (owner["owner_calculation_id"], owner["posting_period"]), set(),
                    ).add(ident)
                    if not owner["owner_closed"]:
                        open_owner_publications[owner["id"]] = owner
            if any(not owners for owners in owners_by_member.values()):
                raise KernelError("content_integrity_failed", "当前资产成员缺少精确所有者采用")
            verify_current_publication_voucher_heads(connection, open_owner_publications.values())
            # Card identity has already checked each complete frozen activation
            # directory. Reuse only exact current members of that same adopted
            # owner, after the independent current/publication guards above.
            # A reviewed current owner can differ from the selected voucher
            # basis; unmatched owners retain the ordinary complete-body guard.
            from . import asset_batches
            from .content_history_context import asset_membership_reader
            from .query_reads import _owns_current_selector_snapshot

            if (
                current_heads and not include_members and posting_period is None
                and cutoff is not None
                and _owns_current_selector_snapshot(reads, connection)
                and asset_membership_reader() is asset_batches
            ):
                cached_events = reads._report_snapshot_cache.get(
                    ("asset_activation_identity_events", cutoff),
                )
                selection = reads._report_snapshot_cache.get(
                    ("asset_owner_identity_selection", cutoff),
                )
                if cached_events is not None and selection is not None:
                    _, owners, membership, _ = selection
                    proofs = {
                        (
                            event["owner_calculation_id"],
                            YearMonth(event["adoption_period"]).ordinal,
                            event["subject_id"], event["calculation_id"],
                            event["fact_id"], event["kind"],
                            YearMonth(event["calculation_period"]).ordinal,
                            event["result_digest"],
                        )
                        for event in cached_events
                        if event["frozen_identity"] and event["direction"] == 1
                        and event["owner_calculation_id"] in membership
                        and owners[event["owner_calculation_id"]]["kind"]
                        == "asset_activation_batch"
                    }
                    for (owner_id, owner_period), member_ids in selected_owner_members.items():
                        current_rows = (unpublished_members[ident] for ident in member_ids)
                        if all((
                            owner_id, owner_period, row["subject_id"], row["id"],
                            row["fact_id"], row["kind"], row["period"], row["digest"].hex(),
                        ) in proofs for row in current_rows):
                            selected_owners.discard(owner_id)
            reads.asset_members_many(selected_owners)
            if include_members:
                result.extend(unpublished_members[ident] for ident in sorted(selected_members))
        return result

    def _selected_asset_members(
        self, connection, period, *, kinds=None, subjects=None, asset_ids=None, current_heads=False
    ):
        """Asset state is adopted by a selected owner, not independently posted.

        Keep these records outside voucher_events/state_results: their complete
        Outcomes are contributions already included in their owner's postings.
        """
        events = self._selected_asset_owner_events(
            connection,
            period,
            kinds=kinds,
            subjects=subjects,
            asset_ids=asset_ids,
            current_heads=current_heads,
        )
        kinds = {"asset_activation", "asset_consumption"} if kinds is None else set(kinds)
        subjects = {subjects} if isinstance(subjects, str) else subjects
        asset_ids = {asset_ids} if isinstance(asset_ids, str) else asset_ids
        reads = self._reads(connection)
        from .query_reads import _owns_current_selector_snapshot

        retained = reads._report_snapshot_cache.get(
            ("asset_owner_identity_selection", YearMonth(period).ordinal)
        ) if _owns_current_selector_snapshot(reads, connection) else None
        members_by_owner = reads.asset_members_many(
            (event["calculation_id"] for event in events),
            _decoded_owners=retained[3] if retained is not None else None,
        )
        member_ids = {
            member["member_calculation_id"]
            for members in members_by_owner.values()
            for member in members
        }
        metadata = reads.metadata(member_ids) if member_ids else {}
        result = []
        for event in events:
            owner_id = event["calculation_id"]
            members = members_by_owner[owner_id]
            for member in members:
                calc = metadata[member["member_calculation_id"]]
                if calc["kind"] not in kinds or (
                    subjects is not None and calc["subject_id"] not in subjects
                ) or (asset_ids is not None and member["asset_id"] not in asset_ids):
                    continue
                result.append(
                    {
                        "event_type": "asset_member",
                        "status": "established",
                        "calculation_id": calc["id"],
                        "subject_id": calc["subject_id"],
                        "fact_id": calc["fact_id"],
                        "kind": calc["kind"],
                        "calculation_period": calc["period"],
                        "posting_period": None,
                        "adoption_period": event["posting_period"],
                        "result_digest": calc["result_digest"],
                        "asset_id": member["asset_id"],
                        "owner_calculation_id": owner_id,
                        "voucher_version_id": event.get("voucher_version_id"),
                        "voucher_number": event.get("voucher_number"),
                        "direction": event.get("direction", 1),
                        "role": event.get("role", "state"),
                        "line_start": member["line_start"],
                        "line_count": member["line_count"],
                        "selection_source": event["selection_source"],
                        "selection_proof": {
                            "basis": "asset_batch_member",
                            "owner_calculation_id": owner_id,
                        },
                    }
                )
        return sorted(
            result,
            key=lambda item: (
                item["adoption_period"],
                item["voucher_number"] or 0,
                item["owner_calculation_id"],
                item["asset_id"],
            ),
        )

    def _selected_asset_owner_events(
        self, connection, period, *, kinds=None, subjects=None, asset_ids=None,
        current_heads=False, complete_owners=False,
        _owner_outcomes=None,
    ):
        member_kinds = {"asset_activation", "asset_consumption"}
        kinds = member_kinds if kinds is None else member_kinds & set(kinds)
        if not kinds or "asset_consumption_month" not in self.store.registry.models:
            return ()
        subjects = {subjects} if isinstance(subjects, str) else subjects
        asset_ids = {asset_ids} if isinstance(asset_ids, str) else asset_ids
        if asset_ids is not None and not asset_ids:
            return ()
        from . import close_storage
        from .content_history_context import close_reader

        reads = self._reads(connection)
        independent_scope = (
            reads._snapshot_active and connection.in_transaction
            and close_reader() is close_storage
            and getattr(self.store.registry, "content_version", None) != 1
        )
        if independent_scope:
            owner_kinds = {
                "asset_activation_batch"
                if kind == "asset_activation" else "asset_consumption_month"
                for kind in kinds
            }
            # A missing owner or member row must not erase a required history
            # before its independent current/frozen authority is inspected.
            query = (
                "SELECT id FROM subject WHERE kind IN (SELECT value FROM json_each(?)) "
                "UNION SELECT subject_id FROM calculation "
                "WHERE kind IN (SELECT value FROM json_each(?))"
            )
            parameters = [json.dumps(sorted(owner_kinds))] * 2
        elif complete_owners:
            query = (
                "SELECT id FROM subject WHERE kind IN "
                "('asset_activation_batch','asset_consumption_month')"
            )
            parameters = []
        else:
            query = (
                "SELECT DISTINCT owner.subject_id FROM asset_batch_member m "
                "JOIN calculation owner ON owner.id=m.owner_calculation_id "
                "JOIN subject member ON member.id=m.member_subject_id "
                "WHERE member.kind IN (SELECT value FROM json_each(?))"
            )
            parameters = [json.dumps(sorted(kinds))]
            if subjects is not None:
                query += " AND m.member_subject_id IN (SELECT value FROM json_each(?))"
                parameters.append(json.dumps(sorted(subjects)))
            if asset_ids is not None:
                query += " AND m.asset_id IN (SELECT value FROM json_each(?))"
                parameters.append(json.dumps(sorted(asset_ids)))
        owners = {row[0] for row in connection.execute(query, parameters)}
        if independent_scope:
            _, _, _, frozen_headers = self._frozen_asset_owner_sources(
                connection, YearMonth(period),
            )
            owners.update(
                header["source_subject"] for header in frozen_headers.values()
                if header["source_kind"] in owner_kinds
            )
        if not owners:
            return ()
        selected = self._selected_accounting(
            connection,
            owners,
            period,
            current_heads=current_heads,
            kinds=(
                None if independent_scope
                else {"asset_activation_batch", "asset_consumption_month"}
            ),
            include_lines=False,
            _owner_outcomes=_owner_outcomes,
        )["through_period"]
        return (*selected["voucher_events"], *selected["state_results"])

    def _frozen_asset_owner_sources(self, connection, cutoff):
        """Locate declared owners independently before narrowing mutable scope."""
        from . import asset_batches, close_storage, publication
        from .content_history_context import (
            asset_membership_reader,
            close_reader,
            publication_reader,
        )

        reads = self._reads(connection)
        reusable = (
            reads._snapshot_active and connection.in_transaction
            and getattr(self.store.registry, "content_version", None) != 1
            and close_reader() is close_storage
            and publication_reader() is publication
            and asset_membership_reader() is asset_batches
        )
        cached = getattr(reads, "_frozen_asset_owner_discovery", None) if reusable else None
        if cached is not None and cached[0] is reads._snapshot_token:
            if cutoff.ordinal in cached[1]:
                return cached[1][cutoff.ordinal]
        close_rows = reads.authoritative_close_rows(
            periods=(row[0] for row in connection.execute(
                "SELECT period FROM period_close WHERE period<=? ORDER BY period",
                (cutoff.ordinal,),
            )), through_period=cutoff.ordinal,
        )
        declarations, membership = {}, {}
        for row in close_rows:
            declared = {}
            for item in reads.close_section(row, "asset_batch_adoptions"):
                if (not isinstance(item, dict) or set(item) != {
                    "owner_calculation_id", "membership_digest",
                } or not isinstance(item["owner_calculation_id"], str)
                        or not is_sha256_hex(item["membership_digest"])
                        or item["owner_calculation_id"] in declared):
                    raise KernelError("content_integrity_failed", "冻结资产所有者目录不匹配")
                ident, expected = item["owner_calculation_id"], item["membership_digest"]
                if ident in membership and membership[ident] != expected:
                    raise KernelError("asset_batch_digest", "冻结资产成员摘要不一致")
                declared[ident] = membership[ident] = expected
            declarations[row["period"]] = declared
        frozen_headers = reads.calculation_identity_headers(membership)
        reads.verify_publication_records(frozen_headers.values())
        for header in frozen_headers.values():
            if (header["source_kind"] not in {
                "asset_activation_batch", "asset_consumption_month",
            } or not header["cs"] or not header["fs"]
                    or header["subject_id"] != header["source_subject"]):
                raise KernelError("content_integrity_failed", "冻结资产所有者精确来源不匹配")
        discovered = close_rows, declarations, membership, frozen_headers
        if reusable:
            # Both consumers still prove their own direct adoptions and member
            # directories. Discovery is neither a body nor membership proof;
            # publish it only after every frozen source succeeds in this token.
            if cached is None or cached[0] is not reads._snapshot_token:
                cached = reads._frozen_asset_owner_discovery = (reads._snapshot_token, {})
            cached[1][cutoff.ordinal] = discovered
        return discovered

    def _asset_owner_metadata_selection(self, connection, period):
        """Select owner events without interpreting independently frozen bodies.

        The complete small frozen owner list locates missing sources. Its
        membership digest authenticates each complete directory later; it is
        neither an outcome-content proof nor a negative member-index lookup.
        Open and otherwise unanchored owners retain strict outcome reads.
        """
        from . import asset_batches, close_storage, publication
        from .content_history_context import (
            asset_membership_reader,
            close_reader,
            publication_reader,
        )

        reads = self._reads(connection)
        if (
            not reads._snapshot_active or not connection.in_transaction
            or "asset_consumption_month" not in self.store.registry.models
            or getattr(self.store.registry, "content_version", None) == 1
            or close_reader() is not close_storage
            or publication_reader() is not publication
            or asset_membership_reader() is not asset_batches
        ):
            return None
        cutoff = YearMonth(period)
        selection_key = ("asset_owner_identity_selection", cutoff.ordinal)
        if selection_key in reads._report_snapshot_cache:
            return reads._report_snapshot_cache[selection_key]
        owner_kinds = {"asset_activation_batch", "asset_consumption_month"}
        close_rows, declarations, membership, frozen_headers = self._frozen_asset_owner_sources(
            connection, cutoff,
        )
        subjects = {header["source_subject"] for header in frozen_headers.values()}
        # Both live identity lanes survive a single missing subject/calculation;
        # the current/publication guard below authenticates their exact sources.
        subjects.update(row[0] for row in connection.execute(
            "SELECT id FROM subject WHERE kind IN (SELECT value FROM json_each(?)) "
            "UNION SELECT subject_id FROM calculation "
            "WHERE kind IN (SELECT value FROM json_each(?))",
            (json.dumps(sorted(owner_kinds)),) * 2,
        ))
        current = self._current_accounting_heads(
            connection, subjects, cutoff=cutoff.ordinal, current_heads=False,
        )
        selections = reads.close_asset_owner_accounting_many(close_rows, subjects=subjects)
        adopted_by_period, vouchers_by_period = {}, {}
        for selection in selections:
            adopted = {item["calculation_id"]: item for item in selection.adopted_results}
            owners = {
                ident for ident, item in adopted.items() if item["role"] == "asset_batch_owner"
            }
            if owners != declarations[selection.period].keys():
                raise KernelError("content_integrity_failed", "冻结资产所有者采用集合不匹配")
            adopted_by_period[selection.period] = adopted
            vouchers_by_period[selection.period] = {item["id"]: item for item in selection.vouchers}
        authoritative = {
            ident: close_period for close_period, vouchers in vouchers_by_period.items()
            for ident in vouchers
        }
        query, parameters = selected_voucher_sql(
            cutoff, subject_ids=subjects, authoritative_vouchers=authoritative,
        )
        selected = list(connection.execute(query, parameters))
        if {
            (row["close_period"], row["id"]) for row in selected
            if row["close_period"] is not None
        } != {
            (period, ident) for period, vouchers in vouchers_by_period.items() for ident in vouchers
        }:
            raise KernelError("content_integrity_failed", "冻结资产凭证集合与独立采用不一致")
        reads.verify_selected_voucher_adoptions(selected, through_period=cutoff.ordinal)
        owner_ids = set(membership) | {row["id"] for row in current}
        owner_ids.update(row["basis_calculation_id"] for row in selected)
        headers = frozen_headers | reads.calculation_identity_headers(owner_ids - membership.keys())
        reads.verify_publication_records(headers.values())
        for header in headers.values():
            if (header["source_kind"] not in owner_kinds or not header["cs"] or not header["fs"]
                    or header["subject_id"] != header["source_subject"]):
                raise KernelError("content_integrity_failed", "资产所有者精确来源不匹配")

        # Header agreement does not independently bind owner kind: both mutable
        # calculation/subject kinds could agree while selecting the wrong typed
        # fact table. Prove every precise owner fact before either identity
        # consumer excludes the other kind, including empty member directories.
        try:
            reads.verify_fact_versions(header["source_fact_id"] for header in headers.values())
        except KernelError as error:
            if error.code != "unknown_fact":
                raise
            raise KernelError(
                "content_integrity_failed", "已采用资产所有者的类型化事实来源缺失",
                component="asset_owner", reason="owner_typed_fact_missing",
            ) from error

        def adopted_header(item):
            header = headers[item["calculation_id"]]
            if any(item[field] != value for field, value in (
                ("publication_id", header["id"]),
                ("subject_id", header["source_subject"]),
                ("fact_id", header["source_fact_id"]),
                ("source_period", str(YearMonth.from_ordinal(header["source_period"]))),
                ("posting_period", str(YearMonth.from_ordinal(header["posting_period"]))),
                ("result_digest", header["source_digest"].hex()),
            )):
                raise KernelError("content_integrity_failed", "冻结资产采用身份不匹配")
            return header

        for close_period, declared in declarations.items():
            for ident in declared:
                adopted_header(adopted_by_period[close_period][ident])
        decoded = {}
        full = reads.metadata(owner_ids - membership.keys(), _decoded_outcomes=decoded)
        events, represented = [], set()
        for row in selected:
            ident = row["basis_calculation_id"]
            if row["close_period"] is not None:
                voucher = vouchers_by_period[row["close_period"]].get(row["id"])
                if voucher is None:
                    raise KernelError("content_integrity_failed", "冻结资产凭证采用缺失")
                item = adopted_by_period[row["close_period"]].get(voucher["adopted_calculation_id"])
                if item is None:
                    raise KernelError("content_integrity_failed", "冻结资产凭证直接依据缺失")
                header = adopted_header(item)
                if voucher["result_digest"] != header["source_digest"].hex():
                    raise KernelError("content_integrity_failed", "冻结资产凭证依据摘要不匹配")
                if row["reverses_id"] is None:
                    ident = item["calculation_id"]
            header = headers[ident]
            represented.add(ident)
            events.append({
                "calculation_id": ident, "kind": header["source_kind"],
                "calculation_period": str(YearMonth.from_ordinal(header["source_period"])),
                "posting_period": str(YearMonth.from_ordinal(row["period"])),
                "result_digest": header["source_digest"].hex(),
                "voucher_version_id": row["id"], "voucher_number": row["number"],
                "direction": -1 if row["reverses_id"] else 1,
            })
        states = {}
        for row in current:
            calc = full[row["id"]]
            if not calc["line_count"]:
                states[calc["id"]] = {
                    "calculation_id": calc["id"], "kind": calc["kind"],
                    "posting_period": calc["posting_period"],
                    "result_digest": calc["result_digest"],
                }
        for close_period, declared in declarations.items():
            for ident in declared:
                header = headers[ident]
                # A review can adopt a nonempty result while retaining a voucher
                # from another month. It is not an independent zero-line state.
                if ident not in represented and header["voucher_id"] is None:
                    states[ident] = {
                        "calculation_id": ident, "kind": header["source_kind"],
                        "posting_period": str(YearMonth.from_ordinal(close_period)),
                        "result_digest": header["source_digest"].hex(),
                    }
        owners = {
            ident: {"id": ident, "kind": header["source_kind"],
                    "period": header["source_period"], "digest": header["source_digest"]}
            for ident, header in headers.items()
        }
        return (*events, *states.values()), owners, membership, decoded

    def _selected_asset_activation_identities(self, connection, period):
        """Reuse frozen complete membership only for card identity and status.

        Owner kinds are authenticated before choosing activation directories.
        Every selected owner keeps its entire directory; open owners retain
        ordinary complete member/outcome reads. No amount or content proof is
        inferred from frozen identity evidence.
        """
        selection = self._asset_owner_metadata_selection(connection, period)
        if selection is None:
            return None
        reads = self._reads(connection)
        cutoff = YearMonth(period).ordinal
        identity_key = ("asset_activation_identity_events", cutoff)
        if identity_key in reads._report_snapshot_cache:
            return reads._report_snapshot_cache[identity_key]
        events, owners, membership, decoded = selection
        activation_events = tuple(
            event for event in events
            if owners[event["calculation_id"]]["kind"] == "asset_activation_batch"
        )
        frozen = tuple(event for event in activation_events
                       if event["calculation_id"] in membership)
        result = self._asset_member_identity_events(
            connection, period, asset_ids=None,
            selection=(frozen, owners, membership, decoded), include_sources=True,
        )
        for event in result:
            event["frozen_identity"] = True
        open_events = tuple(event for event in activation_events
                            if event["calculation_id"] not in membership)
        if open_events:
            members_by_owner = reads.asset_members_many(
                (event["calculation_id"] for event in open_events), _decoded_owners=decoded,
            )
            metadata = reads.metadata({
                member["member_calculation_id"]
                for members in members_by_owner.values() for member in members
            })
            for event in open_events:
                for member in members_by_owner[event["calculation_id"]]:
                    calc = metadata[member["member_calculation_id"]]
                    result.append({
                        "calculation_id": calc["id"], "subject_id": calc["subject_id"],
                        "fact_id": calc["fact_id"], "kind": calc["kind"],
                        "calculation_period": calc["period"],
                        "result_digest": calc["result_digest"],
                        "adoption_period": event["posting_period"],
                        "asset_id": member["asset_id"],
                        "owner_calculation_id": event["calculation_id"],
                        "voucher_version_id": event.get("voucher_version_id"),
                        "voucher_number": event.get("voucher_number"),
                        "direction": event.get("direction", 1), "frozen_identity": False,
                    })
        result.sort(key=lambda item: (
            item["adoption_period"], item["voucher_number"] or 0,
            item["owner_calculation_id"], item["asset_id"],
        ))
        # Publish only after all selected activation directories and open member
        # bodies succeed. Later card details reuse the same complete owner
        # selection, then perform their own required member/money checks.
        reads._report_snapshot_cache[("asset_owner_identity_selection", cutoff)] = selection
        reads._report_snapshot_cache[identity_key] = result
        return result

    def _selected_asset_member_heads(self, connection, period, *, asset_ids):
        """Locate card identities with each selected owner's complete membership.

        Closed directories bind to their independently saved membership digest;
        open owners are strictly decoded once. Unrelated member outcomes remain
        outside this identity-only consumer.
        """
        if not asset_ids:
            return []
        selection = self._asset_owner_metadata_selection(connection, period)
        if selection is not None:
            events, owners, membership, decoded = selection
            # This card-detail consumer only uses consumption timing/state.
            # Activation identities keep their separate complete card lane.
            selection = (
                tuple(event for event in events
                      if owners[event["calculation_id"]]["kind"] == "asset_consumption_month"),
                owners, membership, decoded,
            )
        return self._asset_member_identity_events(
            connection, period, asset_ids=asset_ids, selection=selection,
        )

    def _asset_member_identity_events(
        self, connection, period, *, asset_ids, selection, include_sources=False,
    ):
        """Check complete selected directories before filtering emitted identities."""
        from . import close_storage
        from .content_history_context import close_reader

        frozen_membership = {}
        # Keep the exact owner decoded by this one selection call. Released or
        # unowned readers retain their original complete source path.
        decoded_owners = (
            {} if close_reader() is close_storage
            and getattr(self.store.registry, "content_version", None) != 1 else None
        )
        if selection is not None:
            events, owners, frozen_membership, decoded_owners = selection
        else:
            events = self._selected_asset_owner_events(
                connection, period, asset_ids=asset_ids, complete_owners=True,
                _owner_outcomes=decoded_owners,
            )
        if not events:
            return []
        owner_ids = {event["calculation_id"] for event in events}
        retained = set(decoded_owners or ()) & owner_ids
        reads = self._reads(connection)
        if selection is None:
            metadata = reads.metadata(retained, state=False)
            owners = {
                ident: {"id": ident, "kind": calc["kind"],
                        "period": YearMonth(calc["period"]).ordinal,
                        "digest": bytes.fromhex(calc["result_digest"])}
                for ident, calc in metadata.items()
            }
            owners.update({
                row["id"]: row
                for row in connection.execute(
                    "SELECT id,kind,period,outcome,digest FROM calculation "
                    "WHERE id IN (SELECT value FROM json_each(?))",
                    (json.dumps(sorted(owner_ids - retained)),),
                )
            } if owner_ids - retained else {})
        members = {}
        for row in connection.execute(
            "SELECT m.*,c.kind,c.subject_id,c.fact_id,c.period,c.digest,c.id AS calc_id,"
            "f.id source_fact_id,f.subject_id fact_subject,f.period fact_period,"
            "s.id source_subject_id,s.kind fact_kind,"
            "EXISTS(SELECT 1 FROM calculation_seal s WHERE s.calculation_id=c.id) AS sealed,"
            "EXISTS(SELECT 1 FROM fact_seal fs WHERE fs.fact_id=f.id) AS fact_sealed,"
            "EXISTS(SELECT 1 FROM dependency_calculation d WHERE "
            "d.calculation_id=m.owner_calculation_id AND d.upstream_id=c.id) AS dependent,"
            "EXISTS(SELECT 1 FROM asset_batch_member other "
            "JOIN calculation other_owner ON other_owner.id=other.owner_calculation_id "
            "JOIN calculation owner ON owner.id=m.owner_calculation_id "
            "WHERE other.member_calculation_id=m.member_calculation_id "
            "AND other_owner.subject_id<>owner.subject_id) AS foreign_owned "
            "FROM asset_batch_member m LEFT JOIN calculation c "
            "ON c.id=m.member_calculation_id "
            "LEFT JOIN fact_revision f ON f.id=c.fact_id LEFT JOIN subject s ON s.id=f.subject_id "
            "WHERE m.owner_calculation_id IN (SELECT value FROM json_each(?)) "
            "ORDER BY m.owner_calculation_id,m.position",
            (json.dumps(sorted(owner_ids)),),
        ):
            owner_id = row["owner_calculation_id"]
            if (
                row["calc_id"] is None
                or row["source_fact_id"] is None or row["source_subject_id"] is None
                or row["kind"] not in {"asset_activation", "asset_consumption"}
                or (row["subject_id"], row["kind"], row["period"]) != (
                    row["fact_subject"], row["fact_kind"], row["fact_period"]
                )
                or row["subject_id"] != row["member_subject_id"]
                or row["fact_id"] != row["member_fact_id"]
                or row["digest"] != row["result_digest"]
                or not row["sealed"]
                or not row["fact_sealed"]
                or not row["dependent"]
                or row["foreign_owned"]
            ):
                raise KernelError("asset_batch_identity", "资产汇总成员身份不匹配")
            members.setdefault(owner_id, []).append(dict(row))
        result = []
        for event in events:
            owner_id = event["calculation_id"]
            owner = owners.get(owner_id)
            expected_kind = (
                "asset_activation"
                if event["kind"] == "asset_activation_batch"
                else "asset_consumption"
            )
            if owner is None or owner["kind"] not in {
                "asset_activation_batch", "asset_consumption_month"
            }:
                raise KernelError("asset_batch_identity", "资产汇总计算不存在")
            outcome = None if owner_id in frozen_membership else (
                decoded_owners[owner_id] if owner_id in retained else
                verify_outcome_bytes(owner["outcome"], owner["digest"], owner_id)
            )
            if event["result_digest"] != owner["digest"].hex():
                raise KernelError("asset_batch_digest", "资产汇总结果摘要不匹配")
            directory = []
            position = 1
            for member in members.get(owner_id, ()):
                if (
                    member["kind"] != expected_kind
                    or member["period"] != owner["period"]
                    or member["position"] != len(directory) + 1
                    or member["line_start"] != (
                        position if member["line_count"] else None
                    )
                ):
                    raise KernelError("asset_batch_identity", "资产汇总成员身份不匹配")
                directory.append(
                    {
                        "position": member["position"],
                        "asset_id": member["asset_id"],
                        "member_subject_id": member["member_subject_id"],
                        "member_fact_id": member["member_fact_id"],
                        "member_calculation_id": member["member_calculation_id"],
                        "result_digest": member["result_digest"].hex(),
                        "summary": json.loads(member["summary"]),
                        "line_start": member["line_start"],
                        "line_count": member["line_count"],
                        "kind": expected_kind,
                    }
                )
                position += member["line_count"]
                if asset_ids is not None and member["asset_id"] not in asset_ids:
                    continue
                result.append(
                    {
                        "calculation_id": member["member_calculation_id"],
                        "kind": member["kind"],
                        "calculation_period": str(YearMonth.from_ordinal(member["period"])),
                        "adoption_period": event["posting_period"],
                        "asset_id": member["asset_id"],
                        "owner_calculation_id": owner_id,
                        "voucher_version_id": event.get("voucher_version_id"),
                        "voucher_number": event.get("voucher_number"),
                        "direction": event.get("direction", 1),
                        **({
                            "subject_id": member["member_subject_id"],
                            "fact_id": member["member_fact_id"],
                            "result_digest": member["result_digest"].hex(),
                        } if include_sources else {}),
                    }
                )
            if owner_id in frozen_membership:
                if digest(directory).hex() != frozen_membership[owner_id]:
                    raise KernelError("asset_batch_digest", "冻结资产完整成员清单不匹配")
                continue
            values = outcome["values"]
            if (
                type(values.get("member_count")) is not int
                or values["member_count"] != len(directory)
                or values.get("members") != directory
                or values.get("membership_digest") != digest(directory).hex()
                or position - 1 != len(outcome["lines"])
            ):
                raise KernelError("asset_batch_digest", "资产汇总完整成员清单不匹配")
        return sorted(
            result,
            key=lambda item: (
                item["adoption_period"],
                item["voucher_number"] or 0,
                item["owner_calculation_id"],
                item["asset_id"],
            ),
        )

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

    def _current_publication(self, connection, subject_id, *, include_vouchers=True):
        from . import close_storage
        from .content_history_context import close_reader

        if (close_reader() is close_storage
                and getattr(self.store.registry, "content_version", None) != 1):
            candidates = self._current_accounting_heads(
                connection, {subject_id}, include_members=True,
            )
            row = candidates[0] if candidates else None
        else:
            row = connection.execute(
                "SELECT c.id FROM calculation_current a "
                "JOIN calculation c ON c.id=a.calculation_id "
                "WHERE a.subject_id=?", (subject_id,),
            ).fetchone()
        if row is None:
            return None
        calc = self._calculation(connection, row["id"])
        if calc.get("publication_role") == "asset_member":
            owners = connection.execute(
                "SELECT m.owner_calculation_id FROM asset_batch_member m "
                "JOIN calculation_current a ON a.calculation_id=m.owner_calculation_id "
                "WHERE m.member_calculation_id=? ORDER BY m.owner_calculation_id",
                (calc["id"],),
            ).fetchall()
            for owner in owners:
                self._reads(connection).asset_members(owner[0])
            if not owners:
                return None
            return {
                "status": "adopted",
                "knowledge": "current_knowledge",
                "calculation": calc,
                "publication": {
                    "role": "asset_member",
                    "posting_period": None,
                    "voucher_id": None,
                    "has_journal_lines": False,
                    "owner_calculation_ids": [owner[0] for owner in owners],
                },
                "voucher_versions": [],
                "current_voucher_version_id": None,
            }
        vouchers = [
            self._voucher(connection, item["id"], "current_publication")
            for item in connection.execute(
                "SELECT v.id FROM voucher_version v WHERE v.calculation_id=? "
                "ORDER BY v.period,v.id",
                (calc["id"],),
            )
        ] if include_vouchers else []
        active = connection.execute(
            "SELECT v.id FROM calculation_publication p JOIN voucher_current a "
            "ON a.voucher_id=p.voucher_id JOIN voucher_version v ON v.id=a.version_id "
            "WHERE p.calculation_id=?",
            (calc["id"],),
        ).fetchone()
        if (
            include_vouchers and active
            and active[0] not in {item["voucher_version_id"] for item in vouchers}
        ):
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
        relation_ids = {
            item["calculation_id"]
            for item in (
                *relation_selected["through_period"]["voucher_events"],
                *relation_selected["through_period"]["state_results"],
            )
        }
        reads.prime_calculations(relation_ids)
        resolved_relations = reads.relations_many(
            relation_ids, resolver=resolve_calculation_relations
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
            resolution = resolved_relations[calculation["id"]]
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

    def settlement_summary(
        self, connection, period, *, current=False, subject_ids=None, include_history_counts=True
    ):
        """Read the normalized projection; detail hydration stays in settlements()."""
        from .settlement_projection import settlement_summary

        subjects = (
            None
            if subject_ids is None
            else {subject_ids}
            if isinstance(subject_ids, str)
            else set(subject_ids)
        )
        return settlement_summary(
            connection,
            period,
            current=current,
            subject_ids=subjects,
            reads=self._reads(connection),
            include_history_counts=include_history_counts,
        )

    def settlements(self, connection, period, *, subject_ids=None, current=False, summary=False):
        """Shared scoped reducer; source scope and relationship candidates are distinct."""
        if summary:
            return self.settlement_summary(
                connection, period, current=current, subject_ids=subject_ids
            )
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
        subjects = {item["subject_id"] for item in reads.metadata(identities, state=False).values()}
        subjects.update(
            item["subject_id"]
            for item in scoped["through_period"]["unestablished_state_selections"]
        )
        related = reads.related_subjects(subjects)
        current_cutoff = YearMonth(period)
        if related:
            from . import close_storage
            from .content_history_context import close_reader

            if (close_reader() is close_storage
                    and getattr(self.store.registry, "content_version", None) != 1):
                heads = self._current_accounting_heads(connection, related)
                latest = max((row["posting_period"] for row in heads), default=None)
            else:
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
        from .workflow import OBLIGATION_DEFINITIONS

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
                    for key, definition in OBLIGATION_DEFINITIONS.items()
                    if "*" in definition.basis_kinds or current["kind"] in definition.basis_kinds
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
                "JOIN calculation c ON c.id=a.calculation_id "
                "JOIN subject s ON s.id=a.subject_id WHERE s.kind='external_completion'",
                (json.dumps(sorted(related_subjects)),),
            )
        }
        reads.prime_calculations(calculation_ids)
        for ident in sorted(calculation_ids):
            calc = self._calculation(connection, ident)
            values = calc["outcome"].get("values", {})
            references = [
                *values.get("source_facts", ()),
                *values.get("accepted_calculations", ()),
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
                            "source_facts": values.get("source_facts", []),
                            "adopted_evidence_digests": values.get("adopted_evidence_digests", []),
                            "previous_completion_fact_id": values.get(
                                "previous_completion_fact_id"
                            ),
                            "accepted_calculations": values.get("accepted_calculations", []),
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
                item["actual_completion_status"] in {"completed", "not_applicable"}
                for item in obligations
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
        include_result=True,
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
        calculations = {}
        if calculation_ids:
            for row in connection.execute(
                "SELECT c.id,c.subject_id,c.outcome,c.digest FROM json_each(?) ids "
                "JOIN calculation c ON c.id=ids.value",
                (json.dumps(sorted(calculation_ids)),),
            ):
                calculations[row["id"]] = {
                    "id": row["id"],
                    "subject_id": row["subject_id"],
                    "outcome": verify_outcome_bytes(row["outcome"], row["digest"], row["id"]),
                }
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
                        obligations = calculation["outcome"].get("values", {}).get(
                            "obligations", ()
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
            error_code = public_job_code(row["error_code"]) if row["status"] == "failed" else None
            if metadata_only:
                items.append(
                    {
                        "job_id": row["id"],
                        "status": row["status"],
                        "attempts": row["attempts"],
                        "error_code": error_code,
                        "error_message": job_error_message(error_code),
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
                    "error_code": error_code,
                    "error_message": job_error_message(error_code),
                    **({"result": result} if include_result else {}),
                    **({"result_issue": result_issue} if result_issue else {}),
                    **({"contract_issues": contract_issues} if contract_issues else {}),
                    "association": association,
                    "references": references,
                    "period": _file_job_period(plan.get("period")),
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
        include_payroll_confirmation=False,
        owner_projection=False,
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
            timestamp = (
                None if owner_projection else recorded_times(
                    connection, (("fact", latest["id"]),)
                ).get(("fact", latest["id"]))
            )
            latest["recorded_at"] = timestamp
        current_publication = self._current_publication(
            connection, subject_id, include_vouchers=not owner_projection
        )
        if current_publication is not None and (not summary or include_payroll_confirmation):
            confirmation = self._payroll_confirmation_source(
                connection, current_publication["calculation"]
            )
            if confirmation is not None:
                current_publication["payroll_confirmation"] = confirmation
        selected = self._selected_accounting(
            connection, subject_id, period, include_lines=not summary
        )
        exact_close = self._reads(connection).authoritative_close_rows(
            periods=[YearMonth(period).ordinal]
        )
        later_close = connection.execute(
            "SELECT period,digest FROM period_close WHERE period>? ORDER BY period LIMIT 1",
            (YearMonth(period).ordinal,),
        ).fetchone()
        if later_close is not None:
            later_close = self._reads(connection).authoritative_close_rows(
                periods=[later_close["period"]]
            )[0]
        if exact_close:
            close_row = exact_close[0]
            reads = self._reads(connection)
            card_adoptions = reads.close_section(close_row, "asset_card_adoptions")
            batch_adoptions = reads.close_section(close_row, "asset_batch_adoptions")
            owner_ids = {item["acceptance_calculation_id"] for item in card_adoptions} | {
                item["owner_calculation_id"] for item in batch_adoptions
            }
            owner_metadata = reads.metadata(owner_ids, state=False)
            close_selection = reads.close_accounting(
                close_row,
                subjects={subject_id, *(item["subject_id"] for item in owner_metadata.values())},
            )
            adopted_results = close_selection.adopted_results
            closure = {
                "state": "exact_close",
                "close_period": period,
                "digest": close_row["digest"].hex(),
            }
            frozen_entry = next(
                (item for item in adopted_results if item["subject_id"] == subject_id),
                None,
            )
            frozen_adoption = (
                {
                    "close_period": period,
                    "publication_id": frozen_entry["publication_id"],
                    "calculation_id": frozen_entry["calculation_id"],
                    "result_digest": frozen_entry["result_digest"],
                    "role": frozen_entry["role"],
                    "selection_proof": {
                        "basis": "direct_adoption",
                        "close_period": period,
                        "publication_id": frozen_entry["publication_id"],
                        "role": frozen_entry["role"],
                    },
                }
                if frozen_entry is not None
                else None
            )
            card_metadata = self._reads(connection).metadata(
                {item["calculation_id"] for item in card_adoptions},
                state=False,
            )
            if frozen_adoption is None or any(
                card_metadata[item["calculation_id"]]["subject_id"] == subject_id
                for item in card_adoptions
            ):
                direct_by_calculation = {item["calculation_id"]: item for item in adopted_results}
                card = next(
                    (
                        item
                        for item in card_adoptions
                        if card_metadata[item["calculation_id"]]["subject_id"] == subject_id
                    ),
                    None,
                )
                if card is not None:
                    owner = direct_by_calculation.get(card["acceptance_calculation_id"])
                    card_calc = self._reads(connection).metadata(
                        {card["calculation_id"]}, state=False
                    )[card["calculation_id"]]
                    if (
                        owner is None
                        or card_calc["subject_id"] != subject_id
                        or card_calc["result_digest"] != card["result_digest"]
                        or owner["result_digest"] != card["acceptance_result_digest"]
                    ):
                        raise KernelError(
                            "content_integrity_failed",
                            "资产卡片的关账采用依据不匹配",
                            component="close",
                            record_id=card["calculation_id"],
                            reason="asset_card_adoption_mismatch",
                        )
                    frozen_adoption = {
                        "close_period": period,
                        "publication_id": owner["publication_id"],
                        "calculation_id": card["calculation_id"],
                        "result_digest": card["result_digest"],
                        "role": "asset_card_member",
                        "selection_proof": {
                            "basis": "asset_card_adoption",
                            "owner_calculation_id": owner["calculation_id"],
                            "owner_publication_id": owner["publication_id"],
                        },
                    }
                else:
                    for adoption in batch_adoptions:
                        owner = direct_by_calculation.get(adoption["owner_calculation_id"])
                        if owner is None:
                            continue
                        member = next(
                            (
                                item
                                for item in self._reads(connection).asset_members(
                                    owner["calculation_id"]
                                )
                                if item["member_subject_id"] == subject_id
                            ),
                            None,
                        )
                        if member is None:
                            continue
                        frozen_adoption = {
                            "close_period": period,
                            "publication_id": owner["publication_id"],
                            "calculation_id": member["member_calculation_id"],
                            "result_digest": member["result_digest"],
                            "role": "asset_batch_member",
                            "selection_proof": {
                                "basis": "asset_batch_member",
                                "owner_calculation_id": owner["calculation_id"],
                                "owner_publication_id": owner["publication_id"],
                                "membership_digest": adoption["membership_digest"],
                            },
                        }
                        break
        elif later_close:
            closure = {
                "state": "covered_by_later_close",
                "close_period": str(YearMonth.from_ordinal(later_close["period"])),
                "digest": later_close["digest"].hex(),
            }
            frozen_adoption = None
        else:
            closure = {"state": "open"}
            frozen_adoption = None
        if frozen_adoption is not None and (not summary or include_payroll_confirmation):
            confirmation = self._payroll_confirmation_source(
                connection,
                self._calculation(connection, frozen_adoption["calculation_id"]),
            )
            if confirmation is not None:
                frozen_adoption["payroll_confirmation"] = confirmation
        asset_members = self._selected_asset_members(
            connection, period, kinds={subject["kind"]}, subjects={subject_id}
        )
        if asset_members:
            selected["through_period"]["asset_member_results"] = asset_members
            if selected["through_period"]["status"] == "not_established":
                selected["through_period"]["status"] = "established"
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
        external = None
        if not owner_projection:
            external, _ = self._external(connection, subject_id, period, as_of, summary=summary)
        settlements = self._settlements(connection, subject_id, selected, summary=summary)
        if summary:
            for field in ("business", "movements", "line_relations"):
                settlements.pop(field, None)
        trace_targets = []
        seen = set()
        for item in (() if owner_projection else (
            *selected["through_period"]["voucher_events"],
            *selected["through_period"]["state_results"],
            *asset_members,
        )):
            target = {
                "calculation_id": item.get("owner_calculation_id", item["calculation_id"]),
                "voucher_version_id": item.get("voucher_version_id"),
            }
            key = (target["calculation_id"], target["voucher_version_id"])
            if key not in seen:
                trace_targets.append(target)
                seen.add(key)
        selections = (
            () if owner_projection
            else selected["through_period"]["unestablished_state_selections"]
        )
        for selection in selections:
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
        presented_frozen_adoption = frozen_adoption
        if summary and frozen_adoption is not None:
            frozen_calculation = self._calculation(connection, frozen_adoption["calculation_id"])
            frozen_calculation["fact"] = self._fact(connection, frozen_calculation["fact_id"])
            frozen_amount, frozen_amount_label = business_display_amount(frozen_calculation)
            presented_frozen_adoption = {
                **frozen_adoption,
                "amount_fen": frozen_amount,
                "amount_label": frozen_amount_label,
            }
        result = {
            "identity": {
                "company_id": self.store.company_id,
                "database_id": self.store.database_id,
                "subject_id": subject_id,
                "kind": subject["kind"],
            },
            "period": period,
            "as_of": as_of,
            **(
                {"latest_source": self._fact_summary(latest)}
                if summary
                else {"latest_fact": latest}
            ),
            "closure": (
                {
                    "state": "exact_close",
                    "digest": closure["digest"],
                }
                if summary and closure["state"] == "exact_close"
                else {
                    "state": "covered_by_later_close",
                    "sealing_boundary": closure["close_period"],
                    "sealing_digest": closure["digest"],
                }
                if summary and closure["state"] == "covered_by_later_close"
                else closure
            ),
            "as_posted": {
                "cutoff_period": selected["cutoff_period"],
                **selected["through_period"],
            },
            "current_business_result": (
                self._publication_summary(connection, current_publication)
                if summary
                else current_publication
            ),
            "frozen_adoption": presented_frozen_adoption,
            "review": {
                "status": review_status,
                "latest_matches_publication": matches,
                "pending_causes": pending,
                "dispositions": dispositions,
            },
            "settlements": settlements,
            "external": external,
            "file_jobs": None if owner_projection else self._file_jobs(
                connection, subject_id, period, summary=summary
            ),
            "display_profiles": {} if owner_projection else self._profiles(
                connection, subject_id, period, include_sources=not owner_projection
            ),
            "trace_targets": trace_targets,
            "read_semantics": {
                "knowledge": "current_knowledge",
                "accounting": "as_posted",
                "business_basis": (
                    "frozen_adoption" if frozen_adoption is not None else "current_known"
                ),
                "display": "frozen_with_current_supplements",
                "as_of": "external_deadlines_and_completion_only",
            },
        }
        if summary:
            selected_calculation_id = (
                frozen_adoption["calculation_id"]
                if frozen_adoption is not None
                else current_publication["calculation"]["id"]
                if current_publication is not None
                else None
            )
            if selected_calculation_id is None or owner_projection:
                result["adopted_basis"] = None
            else:
                from .close_review import business_adopted_basis

                result["adopted_basis"] = {
                    "basis": (
                        "frozen_adoption" if frozen_adoption is not None else "current_publication"
                    ),
                    "calculation_ids": [selected_calculation_id],
                    **business_adopted_basis(connection, self.engine, [selected_calculation_id]),
                }
            current_settlements = self.settlement_summary(
                connection, period, subject_ids={subject_id}, current=True
            )
            for field in ("business", "movements", "line_relations"):
                current_settlements.pop(field, None)
            result["projection"] = "summary"
            result["review"]["disposition_count"] = 0 if owner_projection else connection.execute(
                "SELECT count(*) FROM disposition WHERE subject_id=?", (subject_id,)
            ).fetchone()[0]
            result["review"]["dispositions"] = []
            result["trace_target_count"] = len(seen)
            result["external"] = None if owner_projection else self._external_summary(external)
            result["current_followups"] = {"settlements": current_settlements}
        if owner_projection:
            # The owner consumes business amounts and real settlement progress.
            # Duplicate candidates, correction plans and all historical entity
            # references remain in the full CLI/MCP business-status contract.
            return result
        from .duplicates import DuplicateCandidates
        from .entity_references import verify_hits

        result["duplicate_checks"] = DuplicateCandidates(self.store).business_detail(
            connection, subject_id, summary=summary
        )
        if summary:
            result["duplicate_checks"]["checks"] = [
                {
                    key: item[key]
                    for key in (
                        "check_id",
                        "action",
                        "proposed_subject_id",
                        "result_fact_id",
                        "selected_fact_id",
                        "candidate_digest",
                        "explanation",
                        "created_at",
                    )
                    if key in item
                }
                for item in result["duplicate_checks"]["checks"]
            ]
        corrections = list(
            connection.execute(
                "SELECT c.id,c.plan,c.digest,i.action,i.before_fact_id,i.after_fact_id,"
                "i.replacement_subject_id FROM identity_correction_item i "
                "JOIN identity_correction c ON c.id=i.correction_id "
                "WHERE i.subject_id=? OR i.replacement_subject_id=? ORDER BY i.rowid",
                (subject_id, subject_id),
            )
        )
        result["identity_corrections"] = [
            {
                **{
                    key: row[key]
                    for key in (
                        "id",
                        "action",
                        "before_fact_id",
                        "after_fact_id",
                        "replacement_subject_id",
                    )
                },
                "digest": row["digest"].hex(),
                **({"plan": json.loads(row["plan"])} if not summary else {}),
            }
            for row in corrections
        ]
        facts = list(
            connection.execute(
                "SELECT id fact_id FROM fact_revision WHERE subject_id=?", (subject_id,)
            )
        )
        verify_hits(
            connection,
            facts,
            identity_match="current",
            registry=self.store.registry,
        )
        result["entity_references"] = [
            dict(row)
            for row in connection.execute(
                "SELECT r.fact_id,r.path,r.entity_id recorded_entity_id,"
                "c.entity_id current_entity_id,r.role FROM entity_reference_recorded r "
                "JOIN entity_reference_current c ON c.fact_id=r.fact_id AND c.path=r.path "
                "JOIN fact_revision f ON f.id=r.fact_id WHERE f.subject_id=? "
                "ORDER BY f.revision,r.path",
                (subject_id,),
            )
        ]
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
                **(
                    {"asset_member_results": through["asset_member_results"]}
                    if "asset_member_results" in through
                    else {}
                ),
                "unestablished_state_selections": through["unestablished_state_selections"],
            },
        }

    @staticmethod
    def _fact_summary(fact):
        """Expose immutable source identity without leaking kind-specific fact bags."""

        return {
            key: fact[key]
            for key in (
                "id",
                "subject_id",
                "revision",
                "kind",
                "period",
                "evidence",
                "deleted",
                "knowledge",
                "recorded_at",
            )
            if key in fact
        }

    def _publication_summary(self, connection, publication):
        """Project the adopted result identity; facts and outcome JSON stay internal."""

        if publication is None:
            return None
        calculation = dict(publication["calculation"])
        calculation["fact"] = self._fact(connection, calculation["fact_id"])
        amount, amount_label = business_display_amount(calculation)
        result = {
            "status": publication["status"],
            "knowledge": publication["knowledge"],
            "calculation_id": calculation["id"],
            "subject_id": calculation["subject_id"],
            "kind": calculation["kind"],
            "fact_id": calculation["fact_id"],
            "result_digest": calculation["result_digest"],
            "posting_period": calculation["posting_period"],
            "publication_id": calculation["publication_id"],
            "voucher_id": calculation.get("voucher_id"),
            "has_journal_lines": publication["publication"]["has_journal_lines"],
            "current_voucher_version_id": publication.get("current_voucher_version_id"),
            "amount_fen": amount,
            "amount_label": amount_label,
        }
        if publication.get("payroll_confirmation") is not None:
            result["payroll_confirmation"] = publication["payroll_confirmation"]
        return result

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
        actual_counts, review_counts = {}, {}
        for item in external["obligations"]:
            actual = item["actual_completion_status"]
            review = item["basis_review_status"]
            actual_counts[actual] = actual_counts.get(actual, 0) + 1
            review_counts[review] = review_counts.get(review, 0) + 1
        return {
            key: value
            for key, value in external.items()
            if key not in {"obligations", "completions"}
        } | {
            "obligation_count": len(external["obligations"]),
            "actual_completion_status_counts": actual_counts,
            "basis_review_status_counts": review_counts,
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
            if type(limit) is not int or not 1 <= limit <= 500:
                raise ValueError("每页数量必须为 1 至 500")
            scope = " FROM fact_revision f WHERE "
            if subjects is None:
                scope += "f.period=?"
                parameters = [YearMonth(period).ordinal]
            else:
                scope += "f.subject_id IN (SELECT value FROM json_each(?))"
                parameters = [json.dumps(sorted(subjects))]
            total = connection.execute("SELECT count(*)" + scope, parameters).fetchone()[0]
            cursor = None
            if after is not None:
                cursor = connection.execute(
                    "SELECT f.subject_id,f.revision,f.id" + scope + " AND f.id=?",
                    (*parameters, after),
                ).fetchone()
                if cursor is None:
                    raise KernelError(
                        "dashboard_snapshot_changed", "分页位置已变化，请重新加载明细。"
                    )
            records = list(
                connection.execute(
                    "SELECT f.id" + scope
                    + (
                        " AND (f.subject_id,f.revision,f.id)>(?,?,?)" if cursor is not None else ""
                    )
                    + " ORDER BY f.subject_id,f.revision,f.id LIMIT ?",
                    (*parameters, *(tuple(cursor) if cursor is not None else ()), limit + 1),
                )
            )
            more = len(records) > limit
            keys = [row["id"] for row in records[:limit]]
            page = {
                "total_count": total,
                "filtered_count": total,
                "returned_count": len(keys),
                "has_more": more,
                "next_cursor": keys[-1] if more else None,
            }
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
                    **self._fact_summary(versions[ident]),
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
            items = self._file_jobs(
                connection, subjects, period, job_ids=keys, include_result=False
            )
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
            reads.verify_sql_outcomes(item[1] for item in group)
            reads.fact_versions({metadata[item[1]]["fact_id"] for item in group})
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
            reads.fact_versions({metadata[item[1]]["fact_id"] for item in group})
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
            relation_state = movement.get("state")
            items.append(
                {
                    "id": key,
                    **{name: value for name, value in movement.items() if name != "state"},
                    "relation_state": relation_state,
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
        _inspection_cache=None,
        _allow_frozen_materials=False,
        _checked_open=None,
    ):
        """Compose readiness inside a caller-owned read snapshot."""
        from .tax_import import assess_tax_import_mapping

        period, as_of = str(YearMonth(period)), str(ActualDate(as_of or _today_china()))
        month = YearMonth(period).ordinal
        reads = self._reads(connection)
        exact_rows = (
            reads.authoritative_close_rows(periods=[month])
            if summary
            else reads.close_rows(periods=[month])
        )
        exact = exact_rows[0] if exact_rows else None
        later = connection.execute(
            "SELECT period,digest FROM period_close WHERE period>? ORDER BY period LIMIT 1",
            (month,),
        ).fetchone()
        periods = Periods(self.engine)
        if _checked_open is not None and (
            exact is not None
            or later is not None
            or _checked_open["period"] != period
            or _checked_open["order_failure"] is not None
        ):
            raise ValueError("prepared close readiness must belong to this open period")
        if exact:
            required_frozen = (
                "readiness",
                "inventories",
                "material_coverage",
                "previous_close_digest",
            )
            closure = {"state": "exact_close", "digest": exact["digest"].hex()}
            if summary:
                # Page projection declares the verified storage commitments;
                # the complete read entry below still returns exact contents.
                header = reads.close_header(exact)
                if not {"material", "management"} <= header.root["subroots"].keys():
                    raise KernelError("content_integrity_failed", "冻结关账依据缺失")
                frozen = {
                    "status": "ready",
                    "source": "exact_period_manifest",
                    **{key: {"status": "recorded"} for key in required_frozen},
                }
            else:
                manifest = reads.close_manifest(exact)
                frozen = {
                    "status": "ready",
                    "source": "exact_period_manifest",
                    **{
                        key: {"status": "recorded", "value": manifest[key]}
                        for key in required_frozen
                    },
                }
            readiness = None
            current = periods.collect_current_readiness(
                connection,
                period,
                _inspection_cache=_inspection_cache,
                _allow_frozen_materials=_allow_frozen_materials,
                _query_reads=reads,
            )
        elif later:
            later = (
                reads.authoritative_close_rows(periods=[later["period"]])[0]
                if summary
                else reads.close_rows(periods=[later["period"]])[0]
            )
            closure = {
                "state": "covered_by_later_close",
                "sealing_boundary": str(YearMonth.from_ordinal(later["period"])),
                "sealing_digest": later["digest"].hex(),
            }
            frozen = {
                "status": "unavailable",
                "reason": "no_exact_period_manifest",
            }
            readiness = None
            current = periods.collect_current_readiness(
                connection,
                period,
                _inspection_cache=_inspection_cache,
                _allow_frozen_materials=_allow_frozen_materials,
                _query_reads=reads,
            )
        else:
            closure = {"state": "open"}
            readiness = (
                _checked_open
                if _checked_open is not None
                else periods.check_readiness(
                    connection,
                    period,
                    _inspection_cache=_inspection_cache,
                    _allow_frozen_materials=_allow_frozen_materials,
                    _query_reads=reads,
                )
            )
            frozen = None
            current = (
                periods.collect_current_readiness(
                    connection,
                    period,
                    _inspection_cache=_inspection_cache,
                    _allow_frozen_materials=_allow_frozen_materials,
                    _query_reads=reads,
                )
                if readiness.get("order_failure") is not None
                else readiness
            )
        external_obligation_ids = self._external_obligation_ids_for_period(connection, period)
        obligations_query = Workflow(self.engine)
        if summary:
            external = obligations_query._external_obligations(
                connection,
                period,
                as_of,
                reads=self._reads(connection),
                obligation_ids=external_obligation_ids,
                summary=True,
            )
            external.update(
                scope_period=period,
                scope_semantics="obligation_interval_includes_selected_period",
            )
            external["fact_issues"] = [] if exact else list(current["issues"])
            checked = (
                readiness
                if readiness is not None
                else (
                    periods.check_readiness(
                        connection,
                        period,
                        _inspection_cache=_inspection_cache,
                        _allow_frozen_materials=_allow_frozen_materials,
                        _query_reads=reads,
                    )
                    if not exact
                    else None
                )
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
            obligations = obligations_query._external_obligations(
                connection,
                period,
                as_of,
                reads=self._reads(connection),
                obligation_ids=external_obligation_ids,
            )
            period_issues = [] if exact else list(current["issues"])
            if readiness and readiness.get("order_failure"):
                failure = readiness["order_failure"]
                period_issues.append(
                    {
                        "field": "period",
                        "code": failure["code"],
                        "message": failure["message"],
                        **failure["details"],
                    }
                )
            external = {
                "status": (
                    "completed"
                    if obligations
                    and all(
                        item["actual_completion_status"] in {"completed", "not_applicable"}
                        for item in obligations
                    )
                    else "followup_required"
                    if obligations
                    else "unestablished"
                ),
                "obligations": obligations,
                "fact_issues": period_issues,
                "scope_period": period,
                "scope_semantics": "obligation_interval_includes_selected_period",
            }
        materials = current["materials"]
        if summary:
            from .materials import MaterialReadSummary

            coverage = materials["coverage"]
            # The page consumes the verified coverage identity and counts, not
            # a second recursively copied payload of every historical row.
            materials = {
                "status": materials["status"],
                "issues": materials["issues"],
                "inventories": materials["inventories"],
                "coverage": {
                    "coverage_digest": (
                        coverage.coverage_digest
                        if isinstance(coverage, MaterialReadSummary)
                        else coverage["coverage_digest"]
                    )
                },
            }
        if summary:
            from .settlement_projection import settlement_followup_summary

            settlements_followup = settlement_followup_summary(
                connection, period, current=True, reads=self._reads(connection)
            )
        else:
            settlements_followup = project_settlement_followup(
                self.settlement_summary(connection, period, current=True)
            )
        current_followups = {
            "knowledge": "current_knowledge",
            "affects_frozen_readiness": False,
            "materials": _plain(materials),
            "accounting": _plain(current["accounting"]),
            "close_requirements": _plain(current["close_requirements"]),
            "settlements": settlements_followup,
            "external": external,
            "file_jobs": self._file_jobs(
                connection, None, period, summary=summary, include_result=False
            ),
            "tax_import_mapping": assess_tax_import_mapping(
                self.store,
                connection,
                YearMonth(period),
                reads=reads if reads._snapshot_active else None,
            ),
        }
        result = {
            "schema_version": 1,
            "company_id": self.store.company_id,
            "database_id": self.store.database_id,
            "period": period,
            "as_of": as_of,
            "as_of_semantics": "current_knowledge",
            "closure": closure,
            "frozen_readiness": frozen,
            "readiness": (
                _plain({key: readiness[key] for key in ("period", "order_failure", "issues")})
                if summary and readiness is not None
                else _plain(readiness)
                if readiness is not None
                else None
            ),
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
