"""Retire explicitly selected unused sources and resume their current file cleanup."""

from ai_accounting_modular.contracts.public import ContractError, validate_contract
from ai_accounting_modular.records.public import SourceDiscardCleanup


def _protected_paths(application, read):
    if application.files is None:
        raise ContractError("unsupported", "当前实例未装配原件备份保护，不能清理原件。")
    plans = application.files.retained_input_plans("backup", read)
    return tuple(
        sorted(
            {
                item["relative_path"]
                for plan in plans
                for item in plan["inputs"]
                if item["source_root"] == "company"
            }
        )
    )


def finish(application, company_id, *, marker_ids=None, after_marker_id=None, limit=500):
    if marker_ids is None:
        with application.storage.read_unit(company_id) as read:
            if not application.records.has_source_discards(read):
                return SourceDiscardCleanup((), ())
    # This is deliberately a fresh writer. An uncommitted source deletion must
    # never authorize physical deletion, even when its marker already exists.
    with application.storage.write_unit(company_id) as unit:
        return application.records.finish_source_discards(
            unit,
            protected_paths=_protected_paths(application, unit),
            marker_ids=marker_ids,
            after_marker_id=after_marker_id,
            limit=limit,
        )


def discard(application, request):
    wanted = tuple(sorted(set(request["source_ids"])))
    with application.storage.write_unit(request["company_id"]) as unit:
        sources = application.records.resolve_sources(wanted, unit)
        paths = (
            _protected_paths(application, unit)
            if any(source["kind"] == "file" for source in sources)
            else ()
        )
        prepared = application.records.discard_sources(
            wanted,
            unit,
            retained_source_ids=application.ledger.referenced_source_ids(wanted, unit),
            protected_paths=paths,
        )
    pending, issues = False, set()
    for offset in range(0, len(prepared.marker_ids), 500):
        try:
            cleanup = finish(
                application,
                request["company_id"],
                marker_ids=prepared.marker_ids[offset : offset + 500],
            )
        except (ContractError, OSError) as error:
            # Registration has already committed. Preserve that truthful outcome
            # while the durable markers allow this same request to resume later.
            pending = True
            issues.add(
                (error.code, error.message)
                if isinstance(error, ContractError)
                else (
                    "source_cleanup_unavailable",
                    "来源登记已退出，原件暂时无法清理，可稍后重试。",
                )
            )
            break
        pending = pending or bool(cleanup.pending_marker_ids)
        issues.update((error.code, error.message) for error in cleanup.errors)
    return validate_contract(
        "core.schema.json",
        "SourceDiscardResult",
        {
            "company_id": request["company_id"],
            "discarded_source_ids": list(prepared.discarded_source_ids),
            "missing_source_ids": list(prepared.missing_source_ids),
            "file_cleanup": {
                "state": "pending" if pending else "complete",
                "issues": [{"code": code, "message": message} for code, message in sorted(issues)],
            },
        },
    )
