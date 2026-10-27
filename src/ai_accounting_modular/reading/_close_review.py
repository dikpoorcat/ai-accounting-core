"""Read the same complete month summary as the brief, without a closing permission."""

from ai_accounting_modular.contracts.public import ContractError, content_digest

from . import _brief_overview, _monthly


def digest(snapshot):
    return content_digest(
        {"contract": "readonly-month-review/1", "snapshot_version": snapshot.version}
    )


def review(snapshot):
    financial = snapshot.components["financial"]["financial"]
    funds = snapshot.components["funds"]["data"]
    amounts = {
        **{
            name: _brief_overview.amount(financial[source])
            for name, source in (
                ("month_revenue_fen", "revenue"),
                ("month_expense_fen", "expense"),
                ("month_result_fen", "net_profit"),
            )
        },
        "funds_total_fen": funds["total_fen"],
        "actual_receipts_fen": funds["inflow_fen"],
        "actual_payments_fen": funds["outflow_fen"],
    }
    if any(value is None for key, value in amounts.items() if key != "funds_total_fen"):
        return None
    context = snapshot.components["brief_context"]
    followups = {item["key"] for item in (*context["risks"], *context["owner_tasks"])}
    return {
        "period": snapshot.scope["period_to"],
        "amounts": amounts,
        "businesses": snapshot.components["brief_activity"]["review_businesses"],
        "materials": [],
        "status": "ai_reviewing"
        if followups
        or financial["issues"]
        or amounts["funds_total_fen"] is None
        or funds["attention_account_count"] > 0
        else "ready",
        "followup_count": len(followups),
    }


def request(snapshot):
    if snapshot.selected_period["status"] != "open" or review(snapshot) is None:
        return None
    return {"preview_digest": digest(snapshot)}


def detail(reader, scope, query):
    from ._brief import COMPONENTS

    normalized = _monthly.normalize(
        {
            "company_id": scope.company_id,
            "period_from": scope.period,
            "period_to": scope.period,
            "view": "as_posted",
        }
    )
    expected = query.get("preview_digest")
    with reader.storage.read_unit(scope.company_id) as read:
        month = normalized["period_to"]
        if month < read.start_period:
            raise ContractError("invalid_input", "不能核对建账前缺少逐月正式资料的月份。")
        response = {
            "schema_version": 2,
            "company_id": read.company_id,
            "database_id": read.database_id,
            "period": month,
            "state": "unprepared",
            "preview_digest": None,
            "close_digest": None,
            "reason": "本月简报的完整核对内容尚未准备好，请刷新简报后查看。",
            "covered_by": None,
            "owner_review": None,
        }
        closure = reader.ledger.period_closure(month, read)
        if closure.state == "closed":
            response.update(reason=None)
            if closure.closing_period == month:
                response.update(state="closed", preview_digest=expected, close_digest=closure.head)
            else:
                response.update(
                    state="covered",
                    covered_by={"period": closure.closing_period, "digest": closure.head},
                )
            return response
        try:
            snapshot = _monthly.ready_snapshot(reader, normalized, COMPONENTS, read)
        except _monthly._CorruptComponent:
            return response
        if snapshot is None:
            return response
        head = digest(snapshot)
        if expected is not None and expected != head:
            response.update(
                state="stale",
                preview_digest=expected,
                reason="本月核对内容已经变化，请刷新简报后查看。",
            )
            return response
        try:
            owner_review = review(snapshot)
        except _monthly._CorruptComponent:
            return response
        if owner_review is None:
            response.update(reason="本月仍有核对必需的金额未知，请会计AI继续核查。")
            return response
        response.update(
            state="prepared", preview_digest=head, owner_review=owner_review, reason=None
        )
        return response
