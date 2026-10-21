"""Show only current explanations and real unresolved work, without an approval process."""

from ai_accounting_modular.contracts.public import PageQuery

from ._collections import prepare_model

_NEXT = {
    "provide_material": "提供这项业务的实际资料。",
    "clarify_reality": "说明实际发生的情况。",
    "external_payment": "按实际安排办理付款，并把结果交给会计AI。",
    "external_filing": "办理相关外部申报，并把结果交给会计AI。",
    "professional_check": "与会计AI核对疑问，必要时请专业人员协助检查。",
}


def _notes(reader, month, read):
    selected = reader.records.current_notes(("period:" + month,), read)
    note = selected[0] if selected else None
    if note is None:
        return {"status": "not_provided", "current": None, "latest": None, "supplements": []}
    if note["state"] != "current":
        return {"status": "stale", "current": None, "latest": None, "supplements": []}
    value = {
        "id": note["scope"] + ":" + str(note["seq"]),
        "text": note["text"],
        "status": "current",
    }
    return {"status": "current", "current": value, "latest": value, "supplements": []}


def prepare(reader, scope, read):
    month = scope["period_to"]
    issues, page = [], PageQuery(500)
    while True:
        result = reader.records.period_issues(
            read.start_period, month, read, limit=page.limit, cursor=page.cursor
        )
        issues.extend(result.items)
        if result.next_cursor is None:
            break
        page = PageQuery(500, result.next_cursor)
    risks, tasks = [], []
    for issue in issues:
        if issue["assignee"] == "ai":
            risks.append(
                {
                    "key": issue["issue_id"],
                    "title": issue["question"],
                    "impact": "会计AI继续核对本项事实及相关账务。",
                    "status": "ai_reviewing",
                }
            )
            continue
        tasks.append(
            {
                "key": issue["issue_id"],
                "title": issue["question"],
                "object": "本项业务" if issue.get("business_id") else "本公司",
                "period": issue.get("period"),
                "amount_fen": None,
                "amount_status": "unknown",
                "deadline": issue.get("due_on"),
                "impact": "会计AI需要这项实际信息或办理结果，才能继续完成相关工作。",
                "next_step": _NEXT[issue["action"]],
                "subject_id": issue.get("business_id"),
            }
        )
    return prepare_model(
        {
            "management_commentary_details": _notes(reader, month, read),
            "risks": risks,
            "owner_tasks": tasks,
        }
    )
