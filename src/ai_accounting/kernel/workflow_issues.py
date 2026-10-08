"""The workflow's explicitly scoped issue locations and response-local references."""

from __future__ import annotations

import json

from .contracts import KernelError


def issue_locations(value):
    """Yield only the declared workflow locations, in the original work order.

    The boolean identifies a list; the final string identifies its issue type.
    This also preserves the deliberately omitted settlement issue list.
    """
    sections = value["sections"]
    for area in sections["materials_and_accounting"]:
        yield area["materials"], "issues", "issue_refs", True, "readiness"
        yield area["accounting"], "issues", "issue_refs", True, "readiness"
        yield area, "close_issues", "close_issue_refs", True, "readiness"
    if sections["close"] is not None:
        yield sections["close"], "issues", "issue_refs", True, "readiness"
    for obligation in sections["external"]["obligations"]:
        yield obligation, "basis_issues", "basis_issue_refs", True, "readiness"
    settlement = sections["external"]["settlements"]
    if settlement is not None and ("issues" in settlement or "issue_refs" in settlement):
        yield settlement, "issues", "issue_refs", True, "readiness"
    for job in sections["files"]["jobs"]:
        yield job, "result_issue", "result_issue_ref", False, "readiness"
        yield job, "contract_issues", "contract_issue_refs", True, "readiness"
    mapping = sections["files"]["tax_import_mapping"]
    if mapping is not None:
        yield mapping, "issues", "issue_refs", True, "tax_mapping"
    yield value, "fact_issues", "fact_issue_refs", True, "readiness"


def _check_issue_locations(value, locations):
    allowed = {(id(container), old) for container, old, *_ in locations}

    def visit(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if key == "issues" or key.endswith(("_issues", "_issue")):
                    if (id(item), key) not in allowed:
                        raise KernelError(
                            "response_contract_mismatch", "工作清单包含未声明的问题位置"
                        )
                    # An issue body is checked by its typed response contract.
                    continue
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)


def compact_workflow_issues(value):
    """Replace complete issue bodies with indices without changing business state."""
    locations = list(issue_locations(value))
    _check_issue_locations(value, locations)
    directory, indices = [], {}

    def reference(issue):
        # Full JSON equality preserves omitted/null fields and numeric value types.
        key = json.dumps(issue, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if key not in indices:
            indices[key] = len(directory)
            directory.append(issue)
        return indices[key]

    for container, old, new, multiple, _ in locations:
        original = container.pop(old)
        if multiple:
            if not isinstance(original, list):
                raise KernelError("response_contract_mismatch", "工作清单问题列表无效")
            container[new] = [reference(issue) for issue in original]
        else:
            container[new] = None if original is None else reference(original)
    value["schema_version"] = 2
    value["issues"] = directory
    # Direct readers still check reference bounds and each location's issue type.
    # Complete response validation remains at the public service boundary.
    from .response_contracts import _workflow_issue_targets

    try:
        return _workflow_issue_targets(value)
    except ValueError:
        raise KernelError(
            "response_contract_mismatch", "工作清单问题引用或正文类型无效"
        ) from None


def validate_issue_references(value, readiness_adapter, tax_mapping_adapter):
    """Validate every reference against its directory and its declared issue type."""
    directory = value["issues"]
    adapters = {"readiness": readiness_adapter, "tax_mapping": tax_mapping_adapter}
    checked = set()
    for container, _, key, multiple, kind in issue_locations(value):
        refs = container[key] if multiple else [container[key]]
        for ref in refs:
            if ref is None and not multiple:
                continue
            if type(ref) is not int or not 0 <= ref < len(directory):
                raise ValueError("workflow issue reference is outside this response")
            if (ref, kind) not in checked:
                adapters[kind].validate_python(directory[ref])
                checked.add((ref, kind))
    return value
