"""Hydrate selected complete formal details; selection never invents posting deltas."""

from ai_accounting_modular.contracts.public import (
    ReportContribution,
    format_fen,
    loads_json,
    validate_contract,
)

from ._values import fail


def report_contribution(row) -> ReportContribution:
    """Restore one saved known amount or the complete unestablished issue."""
    value = {key: row[key] for key in ("statement", "line", "detail_code")}
    amount, issue = row["amount_fen"], row["issue_json"]
    if (amount is None) == (issue is None):
        fail("storage_corrupt", "正式报表明细必须明确为已知金额或未知问题，不能混淆。")
    if issue is None:
        value["amount_fen"] = format_fen(amount)
    else:
        value.update(state="unestablished", issue=loads_json(issue))
    validate_contract("business-facts.schema.json", "ReportContribution", value)
    return value


def hydrate_report_contributions(repo, heads) -> dict[str, tuple[ReportContribution, ...]]:
    """Read one selected head batch, retaining unknowns and their exact issue.

    The caller selects current or frozen heads and already owns their precise
    source references. These are complete adopted details, not as-posted deltas.
    """
    grouped = {head["result_id"]: [] for head in heads}
    if grouped:
        rows = repo.children("result_report_contribution", tuple(grouped))
        for row in sorted(
            rows, key=lambda item: (item["result_id"], item["line"], item["detail_code"])
        ):
            grouped[row["result_id"]].append(report_contribution(row))
    return {result_id: tuple(values) for result_id, values in grouped.items()}
