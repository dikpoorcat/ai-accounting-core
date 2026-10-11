"""Detached comparisons of actual quarterly submissions and complete report inputs.

Jobs and evidence are immutable sources.  Their bytes are read in the caller's
snapshot; calculators receive only the resulting detached dictionaries.
"""

from __future__ import annotations

import hashlib

from .contracts import KernelError, Read
from .types import YearMonth, canonical, digest

BASIS_CONTRACT = "quarterly-report-basis-v1"
SIGNATURE_CONTRACT = "quarterly-report-comparison-v1"
_CONTENT_FIELDS = (
    "company_id",
    "database_id",
    "organization",
    "period",
    "statements",
    "checks",
    "opening_source",
    "template",
    "rule",
    "report_basis",
)
_EVENT_FIELDS = (
    "event_type",
    "voucher_version_id",
    "voucher_id",
    "voucher_number",
    "voucher_calculation_id",
    "calculation_id",
    "fact_id",
    "kind",
    "calculation_period",
    "posting_period",
    "result_digest",
    "role",
    "direction",
    "reverses_voucher_version_id",
    "opening",
)


def report_dependency_reads(registry, end_period) -> tuple[Read, ...]:
    """Declare empty and populated ranges before loading comparison resources."""
    from .reports import REPORT_KINDS
    from .workflow import EXTERNAL_WORKFLOW_KINDS

    end = YearMonth(end_period)
    before = YearMonth.from_ordinal(end.ordinal + 1) if end.ordinal < 119987 else None
    accounting = {
        kind
        for kind in registry.evaluators
        if kind not in EXTERNAL_WORKFLOW_KINDS and registry.models[kind].lane == "accounting"
    }
    return (
        *(
            Read(source, kind, "*", before)
            for kind in sorted(accounting)
            for source in ("fact", "calculation")
        ),
        *(
            Read("fact", kind, "*", before)
            for kind in sorted(set(REPORT_KINDS))
            if kind in registry.models and kind not in accounting
        ),
    )


def _quarter(obligation):
    start, end = obligation.start_period, obligation.end_period
    year, month = int(end[:4]), int(end[5:])
    quarter = (month - 1) // 3 + 1
    if month != quarter * 3 or str(start) != f"{year:04d}-{quarter * 3 - 2:02d}":
        raise KernelError("report_basis_unestablished", "财报义务须明确覆盖完整季度")
    return year, quarter


def snapshot_basis(engine, reads, plan) -> dict:
    """Retain real accounting selections, including all balance carry-in sources.

    Selection proofs, close digests and projection roots identify storage paths;
    they are deliberately excluded so freezing unchanged inputs is equivalent.
    """
    from .business_queries import BusinessQueries

    if reads.store is not engine.store or not reads.connection.in_transaction:
        raise ValueError("report basis requires the caller's accounting snapshot")
    end = YearMonth(plan["period"]["quarter_end"][:7])
    kinds = {
        kind
        for kind in engine.store.registry.evaluators
        if engine.store.registry.models[kind].lane == "accounting"
    }
    selected = BusinessQueries(engine, reads=reads)._selected_accounting(
        reads.connection,
        None,
        str(end),
        kinds=kinds,
        include_lines=False,
    )["through_period"]
    if selected["unestablished_state_selections"]:
        raise KernelError("report_basis_unestablished", "报表采用的核算版本尚未建立")
    sources = [
        {field: event[field] for field in _EVENT_FIELDS if field in event}
        for event in (*selected["voucher_events"], *selected["state_results"])
    ]
    references = set(plan["report_fact_ids"])
    facts = reads.fact_versions(references)
    if set(facts) != references:
        raise KernelError("report_basis_unestablished", "报表依据事实版本缺失")
    rows = {
        row["id"]: row
        for row in reads.connection.execute(
            "SELECT id,digest FROM fact_revision WHERE id IN (SELECT value FROM json_each(?))",
            (canonical(sorted(references)),),
        )
    }
    report_facts = [
        {
            "subject_id": fact.subject_id,
            "fact_id": fact.id,
            "kind": fact.fact.kind,
            "period": str(fact.fact.period),
            "fact_digest": rows[fact.id]["digest"].hex(),
        }
        for fact in facts.values()
    ]
    return {
        "contract": BASIS_CONTRACT,
        "accounting_sources": sorted(sources, key=canonical),
        "report_facts": sorted(report_facts, key=canonical),
    }


def report_signature(plan) -> str:
    """Compare complete content and adopted inputs, never only displayed totals."""
    basis = plan.get("report_basis") if isinstance(plan, dict) else None
    if (
        not isinstance(basis, dict)
        or basis.get("contract") != BASIS_CONTRACT
        or not isinstance(basis.get("accounting_sources"), list)
        or not isinstance(basis.get("report_facts"), list)
        or any(field not in plan for field in _CONTENT_FIELDS)
    ):
        raise KernelError("report_basis_unestablished", "原报表缺少完整内容及采用依据")
    return digest(
        {
            "contract": SIGNATURE_CONTRACT,
            **{field: plan[field] for field in _CONTENT_FIELDS},
        }
    ).hex()


def report_content_signature(plan) -> str:
    """A fresh review can prove unchanged content after input-version changes."""
    report_signature(plan)
    return digest(
        {
            "contract": "quarterly-report-content-v1",
            **{field: plan[field] for field in _CONTENT_FIELDS if field != "report_basis"},
        }
    ).hex()


def _issue(reason, message="财报原件、导出任务或完整核对依据尚未建立"):
    return {"field": "report_snapshot", "reason": reason, "message": message}


def _evidence(reads, digests):
    cache = reads._report_snapshot_cache.setdefault("quarterly_report_evidence", {})
    missing = set(digests) - set(cache)
    for row in reads.connection.execute(
        "SELECT digest,content FROM evidence WHERE digest IN "
        "(SELECT unhex(value) FROM json_each(?))",
        (canonical(sorted(missing)),),
    ):
        content = bytes(row["content"])
        ident = row["digest"].hex()
        if hashlib.sha256(content).hexdigest() != ident:
            raise KernelError("content_integrity_failed", "报表证据字节与不可变摘要不符")
        cache[ident] = content
    return {ident: cache[ident] for ident in digests if ident in cache}


def _json_object(raw):
    from .stored_json import loads_unique

    value = loads_unique(raw)
    return value if isinstance(value, dict) else None


def _stored_job_objects(row):
    """Stored task corruption is a kernel failure, not missing business proof."""
    try:
        payload, result = _json_object(row["payload"]), _json_object(row["result"])
        plan = payload["plan"]
        if (
            not isinstance(plan, dict)
            or not isinstance(result, dict)
            or plan["digest"]
            != digest(
                {key: value for key, value in plan.items() if key not in {"digest", "epochs"}}
            ).hex()
        ):
            raise ValueError("invalid frozen report task")
    except (KeyError, TypeError, ValueError, UnicodeError) as error:
        raise KernelError("content_integrity_failed", "已保存的不可变报表任务内容损坏") from error
    return payload, result


def _report_binding(engine, reads, evidence_digests, obligation):
    """Authenticate an actual workbook and manifest against one retained job."""
    year, quarter = _quarter(obligation)
    contents = _evidence(reads, evidence_digests)
    found = []
    for ident, raw in contents.items():
        try:
            manifest = _json_object(raw)
        except (ValueError, TypeError, UnicodeError):
            continue
        if not manifest or set(manifest) != {"job_id", "report_digest", "sha256", "plan"}:
            continue
        try:
            row = reads.connection.execute(
                "SELECT kind,status,payload,result FROM jobs WHERE id=?",
                (manifest["job_id"],),
            ).fetchone()
            if row is None or row["kind"] != "report_export" or row["status"] != "succeeded":
                continue
            payload, result = _stored_job_objects(row)
            plan = manifest["plan"]
            if (
                not payload
                or not result
                or payload.get("plan") != plan
                or plan["company_id"] != engine.store.company_id
                or plan["database_id"] != engine.store.database_id
                or plan["period"]["year"] != year
                or plan["period"]["quarter"] != quarter
                or plan["period"]["quarter_start"][:7] != str(obligation.start_period)
                or plan["period"]["quarter_end"][:7] != str(obligation.end_period)
                or plan["status"] != "ready"
                or plan["fact_issues"]
                or manifest["report_digest"] != plan["digest"]
                or result.get("report_digest") != plan["digest"]
                or result.get("sha256") != manifest["sha256"]
                or plan["digest"]
                != digest(
                    {key: value for key, value in plan.items() if key not in {"digest", "epochs"}}
                ).hex()
                or manifest["sha256"] not in contents
            ):
                continue
            # The original XLSX must be separately retained, not just a screenshot
            # or a JSON document claiming that an export took place.
            workbook = contents[manifest["sha256"]]
            if not workbook.startswith(b"PK"):
                continue
            signature = report_signature(plan)
            found.append(
                {
                    "job_id": manifest["job_id"],
                    "manifest_evidence_digest": ident,
                    "workbook_evidence_digest": manifest["sha256"],
                    "report_digest": plan["digest"],
                    "report_signature": signature,
                    "content_signature": report_content_signature(plan),
                }
            )
        except KernelError as error:
            if error.code != "report_basis_unestablished":
                raise
            continue
        except (KeyError, TypeError, ValueError, UnicodeError):
            continue
    if len(found) != 1:
        raise KernelError("report_basis_unestablished", "需要唯一的原报表及对应成功导出任务")
    return found[0]


def _completion_binding(engine, reads, obligation, completion):
    adopted = tuple(completion.fact.adopted_evidence_digests)
    if not adopted or not set(adopted) <= set(completion.evidence):
        raise KernelError("report_basis_unestablished", "实际提交原件未被办理事实采用")
    receipt = set(completion.evidence) - set(adopted)
    if not receipt or len(_evidence(reads, receipt)) != len(receipt):
        raise KernelError("report_basis_unestablished", "原报表须另外保存实际办理回执")
    return _report_binding(engine, reads, adopted, obligation)


def _current_report(engine, reads, obligation):
    from .reports import Reports

    year, quarter = _quarter(obligation)
    key = ("quarterly_report_comparison", year, quarter)
    cache = reads._report_snapshot_cache
    if key not in cache:
        plan = Reports(engine)._export_plan(year, quarter, reads=reads)
        cache[key] = plan
    return cache[key]


def _prepare_one(engine, reads, version):
    review = version.fact
    result = {
        "comparison_result": "unestablished",
        "submitted_report_binding": None,
        "reviewed_report_signature": None,
        "current_report_source": None,
        "current_report_basis": None,
        "current_calculations": [],
        "basis_issues": [],
    }
    try:
        completion = reads.fact_version(review.completion_fact_id)
        obligation = reads.fact_version(review.obligation_fact_id).fact
        if (
            completion.subject_id != review.completion_id
            or completion.fact.obligation_id != review.obligation_id
            or completion.fact.obligation_fact_id != review.obligation_fact_id
            or obligation.obligation_kind != "quarterly_financial_report"
            or completion.fact.obligation_kind != obligation.obligation_kind
            or completion.fact.start_period != obligation.start_period
            or completion.fact.end_period != obligation.end_period
        ):
            raise KernelError("report_basis_unestablished", "原报表与精确办理事实的义务范围不符")
        result["current_calculations"] = sorted(
            (
                {"subject_id": item.subject_id, "calculation_id": item.id}
                for read in report_dependency_reads(engine.store.registry, obligation.end_period)
                if read.source == "calculation"
                for item in reads.select(read)
            ),
            key=lambda item: item["subject_id"],
        )
        original = _completion_binding(engine, reads, obligation, completion)
        reviewed = _report_binding(engine, reads, version.evidence, obligation)
        current = _current_report(engine, reads, obligation)
        result["current_report_source"] = current["source"]
        result["submitted_report_binding"] = original
        result["reviewed_report_signature"] = reviewed["report_signature"]
        if current["status"] != "ready" or current["fact_issues"]:
            result["comparison_result"] = "outdated"
            result["basis_issues"] = list(current["fact_issues"])
            return result
        signature = report_signature(current)
        result.update(
            submitted_report_binding=original,
            reviewed_report_signature=reviewed["report_signature"],
            current_report_basis=current["report_basis"],
        )
        if reviewed["report_signature"] != signature:
            result["comparison_result"] = "outdated"
            result["basis_issues"] = [
                _issue("review_report_changed", "本次复核原件已不代表当前完整报表")
            ]
        else:
            result["comparison_result"] = (
                "matched"
                if original["content_signature"] == report_content_signature(current)
                else "different"
            )
    except KernelError as error:
        if error.code != "report_basis_unestablished":
            raise
        result["basis_issues"] = [_issue(error.code)]
    return result


def prepare_review_snapshots(engine, connection, versions) -> dict:
    """Capture external resources before pure calculators run outside SQLite."""
    from .query_reads import QueryReads

    if not connection.in_transaction:
        raise ValueError("review preparation requires the caller's transaction")
    versions = tuple(versions)
    selected = tuple(
        version
        for version in versions
        if version.fact.kind == "external_basis_review"
        and version.fact.obligation_kind == "quarterly_financial_report"
    )
    if not selected:
        return {}
    # Attach to the transaction already owned by Engine.  Opening another
    # connection here would detach job/evidence proof from calculator inputs.
    with QueryReads.attached_snapshot(engine, connection) as reads:
        return {version.id: _prepare_one(engine, reads, version) for version in selected}


def review_snapshot_state(engine, reads, obligation, completion, calc) -> dict:
    """Recheck a published review against today's complete report in one snapshot."""
    from .query_reads import QueryReads, _owns_current_selector_snapshot

    if not _owns_current_selector_snapshot(reads, reads.connection):
        with QueryReads.attached_snapshot(engine, reads.connection) as attached:
            return _review_snapshot_state(engine, attached, obligation, completion, calc)
    return _review_snapshot_state(engine, reads, obligation, completion, calc)


def _review_snapshot_state(engine, reads, obligation, completion, calc):
    result = {
        "review_status": "unestablished",
        "basis_issues": [],
        "current_report_signature": None,
    }
    try:
        original = _completion_binding(engine, reads, obligation, completion)
        saved = calc.values.get("submitted_report_binding")
        if not saved or dict(saved) != original or not calc.values.get("reviewed_report_signature"):
            raise KernelError("report_basis_unestablished", "已发布核对未保留完整原报表绑定")
        current = _current_report(engine, reads, obligation)
        if current["status"] != "ready" or current["fact_issues"]:
            result["review_status"] = "outdated"
            result["basis_issues"] = list(current["fact_issues"])
            return result
        signature = report_signature(current)
        result["current_report_signature"] = signature
        if calc.values["reviewed_report_signature"] != signature:
            result["review_status"] = "outdated"
            result["basis_issues"] = [
                _issue("review_report_changed", "完整报表内容或采用依据已变化，须重新复核")
            ]
        else:
            proven = (
                "matched"
                if original["content_signature"] == report_content_signature(current)
                else "difference_identified"
            )
            if calc.values.get("review_result") == proven:
                result["review_status"] = "reviewed" if proven == "matched" else proven
            else:
                result["basis_issues"] = [_issue("review_result_conflict")]
    except KernelError as error:
        if error.code != "report_basis_unestablished":
            raise
        result["basis_issues"] = [_issue(error.code)]
    return result
