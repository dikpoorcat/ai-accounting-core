"""Pure construction and traversal of the one canonical posting representation."""

from ._wire_types import Line, PostingPlan


def append_journal_line(
    plan: PostingPlan,
    row: Line,
    *,
    journal_key: str = "main",
    summary: str | None = None,
) -> Line:
    """Append a domain-selected line, assigning its number within its journal."""
    journal = next((item for item in plan["journals"] if item["journal_key"] == journal_key), None)
    if journal is None:
        journal = {
            "journal_key": journal_key,
            "summary": row["summary"] if summary is None else summary,
            "lines": [],
        }
        plan["journals"].append(journal)
    row["line_no"] = len(journal["lines"]) + 1
    journal["lines"].append(row)
    return row


def posting_lines(plan: PostingPlan) -> list[Line]:
    """Read all lines for calculations that do not depend on voucher boundaries."""
    return [line for journal in plan["journals"] for line in journal["lines"]]
