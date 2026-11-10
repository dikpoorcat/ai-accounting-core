"""Source-backed completion conditions, separate from deterministic accounting readiness."""

from dataclasses import dataclass

from ai_accounting_modular.contracts.public import (
    ContractError,
    YearMonth,
    canonical_json,
)


@dataclass(frozen=True)
class WorkCheck:
    basis: dict | None
    material_version: str
    note_versions: tuple
    source_ids: tuple[str, ...]
    issues: tuple


def issue(code, field, message, *, sources=(), business_id=None):
    result = {
        "code": code,
        "field": field,
        "meaning": "本次关闭范围所需的有效资料及实际办理条件",
        "message": message,
        "allowed_precision": ["source", "fen", "month"],
        "reusable_sources": [{"source_id": source} for source in sources],
        "blocking_scope": "period",
    }
    if business_id is not None:
        result["business_id"] = business_id
    return result


def _ordered(values):
    return [
        value for _, value in sorted({canonical_json(value): value for value in values}.items())
    ]


def normalize(basis):
    if basis is None:
        return None
    normalized = {
        "coverage": _ordered(
            {**part, "sources": _ordered(part["sources"])} for part in basis["coverage"]
        ),
        "additional_sources": _ordered(basis.get("additional_sources", ())),
        "note_refs": _ordered(basis.get("note_refs", ())),
    }
    if "early_close" in basis:
        early = basis["early_close"]
        normalized["early_close"] = {
            "pending_issue_ids": sorted(early["pending_issue_ids"]),
            "sources": _ordered(early["sources"]),
        }
    return normalized


def _sources(basis):
    if basis is None:
        return set()
    return {
        source["source_id"]
        for source in (
            *basis["additional_sources"],
            *basis.get("early_close", {}).get("sources", ()),
            *(source for part in basis["coverage"] for source in part["sources"]),
        )
    }


def _coverage(basis, actual_range):
    if basis is None:
        return False
    first = YearMonth(actual_range["period_from"]).ordinal
    last = YearMonth(actual_range["period_to"]).ordinal
    ranges = []
    for part in basis["coverage"]:
        start, end = (YearMonth(part[key]).ordinal for key in ("period_from", "period_to"))
        if start > end:
            raise ContractError("invalid_input", "资料覆盖范围的起始月不能晚于截止月。")
        ranges.append((start, end))
    for start, end in sorted(ranges):
        if start > first:
            break
        first = max(first, end + 1)
    return first > last


def inspect(application, actual_range, basis, expected_version, read):
    basis = normalize(basis)
    source_ids = _sources(basis)
    note_refs = basis["note_refs"] if basis is not None else ()
    requested = {reference["scope"]: reference["seq"] for reference in note_refs}
    if len(requested) != len(note_refs):
        raise ContractError("invalid_input", "同一办理说明只能采用一个当前版本。")
    notes = application.records.current_notes(tuple(sorted(requested)), read) if requested else ()
    if len(notes) != len(requested):
        raise ContractError("invalid_input", "采用的办理说明尚未登记，请修正说明引用。")
    for note in notes:
        if note["state"] != "current" or note["seq"] != requested[note["scope"]]:
            raise ContractError("snapshot_changed", "采用的办理说明已改变或失效，请复核该说明。")
        source_ids.update(note["evidence_ids"])
    materials = application.records.capture_close_materials(
        actual_range["period_from"],
        actual_range["period_to"],
        read,
        source_ids=tuple(sorted(source_ids)),
    )
    if expected_version is not None and expected_version != materials.version:
        raise ContractError(
            "snapshot_changed", "受核资料集合已变化，请核对变化部分后重新检查关账。"
        )
    issues = []
    if not _coverage(basis, actual_range):
        issues.append(
            issue(
                "materials_coverage_missing",
                "/basis/coverage",
                "尚未提供覆盖整个实际关闭范围的资料齐全依据；先核对已有资料，必要现实缺项才问老板。",
            )
        )
    if expected_version is None:
        issues.append(
            issue(
                "material_review_required",
                "/expected_material_version",
                "请核对本次范围、未归期和明确关联的资料集合，再采用返回的资料版本检查。",
            )
        )
    return WorkCheck(
        basis,
        materials.version,
        tuple(
            {"scope": note["scope"], "seq": note["seq"], "evidence_ids": note["evidence_ids"]}
            for note in sorted(notes, key=lambda value: value["scope"])
        ),
        tuple(sorted(source_ids)),
        tuple(issues),
    )


def completion_issues(items, actual_range, basis):
    first, last = actual_range["period_from"], actual_range["period_to"]
    required = {
        item["issue_id"]: (index, item)
        for index, item in enumerate(items)
        if item.get("required_before_close") is not None
        and first <= item["required_before_close"] <= last
    }
    excepted = set(basis.get("early_close", {}).get("pending_issue_ids", ())) if basis else set()
    problems = []
    if excepted - required.keys():
        problems.append(
            issue(
                "early_close_scope_changed",
                "/basis/early_close/pending_issue_ids",
                "先关账依据包含不属于本次关闭节点的未完成事项，请复核实际范围。",
            )
        )
    for issue_id, (index, item) in sorted(required.items()):
        if issue_id not in excepted:
            problems.append(
                issue(
                    "external_completion_pending",
                    f"/unresolved/{index}",
                    item["question"],
                    sources=item["evidence_ids"],
                    business_id=item.get("business_id"),
                )
            )
    return tuple(problems)
