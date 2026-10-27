"""Issue changes notify the months that actually display their old or new contents."""

from copy import deepcopy
from uuid import uuid4

from ai_accounting_modular.reading import _monthly

from ._working_case import case as case

MONTHS = ("2026-01", "2026-02", "2026-03")
WORK = "period:2026-01"


def issue(case, question, period):
    value = {
        "issue_id": str(uuid4()),
        "scope": WORK,
        "evidence_ids": [case.source_id],
        "missing_fields": ["/data/amount_fen"],
        "question": question,
        "assignee": "ai",
        "action": "clarify_reality",
    }
    if period is not None:
        value["period"] = period
    return value


def replace_issues(case, *values):
    with case.kernel.storage.read_unit(case.company) as read:
        head = case.kernel.records.current_issues(WORK, read)["head"]
    result = case.app.accounting_apply(
        case.request(unresolved=list(values), unresolved_scope=WORK, expected_scope_head=head)
    )
    assert result["completion"] == "complete", result


def pages(case):
    return {
        month: _monthly.deliver(
            case.kernel.reading,
            {
                "company_id": case.company,
                "period_from": month,
                "period_to": month,
                "view": "as_posted",
            },
            (_monthly.BRIEF_CONTEXT,),
            lambda snapshot, read: dict(snapshot.components[_monthly.BRIEF_CONTEXT]),
        )
        for month in MONTHS
    }


def ready(case):
    pages(case)
    case.kernel.reading.run_pending(case.company)
    result = pages(case)
    assert all(page.state == "ready" for page in result.values())
    return result


def assert_pending(case, expected):
    actual = pages(case)
    assert {month for month, page in actual.items() if page.state == "preparing"} == set(expected)
    assert all(page.state in {"ready", "preparing"} for page in actual.values())


def test_issue_container_names_do_not_replace_actual_changed_periods(case):
    case.seed(expense=False)
    initial = ready(case)
    february = issue(case, "核对二月金额", "2026-02")
    replace_issues(case, february)
    assert_pending(case, MONTHS[1:])
    actual = ready(case)
    assert actual[MONTHS[0]].version == initial[MONTHS[0]].version
    assert [item["title"] for item in actual[MONTHS[1]].payload["risks"]] == ["核对二月金额"]

    march = issue(case, "核对三月金额", "2026-03")
    replace_issues(case, february, march)
    assert_pending(case, MONTHS[2:])
    ready(case)
    replace_issues(case, march)
    assert_pending(case, MONTHS[1:])
    actual = ready(case)
    assert actual[MONTHS[1]].payload["risks"] == []

    january = deepcopy(march)
    january.update(period="2026-01", question="核对移到一月的金额")
    replace_issues(case, january)
    assert_pending(case, MONTHS)
    actual = ready(case)
    assert all(
        [item["title"] for item in page.payload["risks"]] == [january["question"]]
        for page in actual.values()
    )
    replace_issues(case, january)
    assert_pending(case, ())


def test_cancel_and_clear_notify_using_removed_issue_periods(case):
    case.seed(expense=False)
    february = issue(case, "核对二月金额", "2026-02")
    global_issue = issue(case, "核对无期间事项", None)
    replace_issues(case, february, global_issue)
    ready(case)

    checked = case.app.accounting_cancel_month(
        {"company_id": case.company, "period": "2026-02", "action": "inspect"}
    )
    result = case.app.accounting_cancel_month(
        {
            "company_id": case.company,
            "period": "2026-02",
            "action": "cancel",
            "command_id": str(uuid4()),
            "expected_head": checked["head"],
        }
    )
    assert result["state"] == "cancelled"
    assert_pending(case, MONTHS[1:])
    actual = ready(case)
    assert all(
        [item["title"] for item in page.payload["risks"]] == [global_issue["question"]]
        for page in actual.values()
    )

    replace_issues(case)
    assert_pending(case, MONTHS)
    assert all(page.payload["risks"] == [] for page in ready(case).values())
