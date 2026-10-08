"""Expand workflow references when asserting the unchanged business semantics."""

from copy import deepcopy


def expand_workflow(response):
    value = deepcopy(response)
    directory = value.pop("issues")

    def expand(container, name, ref_name, *, single=False):
        refs = container.pop(ref_name)
        container[name] = (
            None if refs is None else directory[refs]
        ) if single else [directory[ref] for ref in refs]

    sections = value["sections"]
    for area in sections["materials_and_accounting"]:
        expand(area["materials"], "issues", "issue_refs")
        expand(area["accounting"], "issues", "issue_refs")
        expand(area, "close_issues", "close_issue_refs")
    if sections["close"] is not None:
        expand(sections["close"], "issues", "issue_refs")
    for obligation in sections["external"]["obligations"]:
        expand(obligation, "basis_issues", "basis_issue_refs")
    settlement = sections["external"]["settlements"]
    if settlement is not None and "issue_refs" in settlement:
        expand(settlement, "issues", "issue_refs")
    for job in sections["files"]["jobs"]:
        expand(job, "result_issue", "result_issue_ref", single=True)
        expand(job, "contract_issues", "contract_issue_refs")
    mapping = sections["files"]["tax_import_mapping"]
    if mapping is not None:
        expand(mapping, "issues", "issue_refs")
    expand(value, "fact_issues", "fact_issue_refs")
    value["schema_version"] = 1
    return value
