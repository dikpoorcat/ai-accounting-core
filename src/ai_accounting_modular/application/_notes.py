"""Monthly explanations follow accounting; business descriptions follow identity."""

from ai_accounting_modular.contracts.public import ContractError, validate_contract
from ai_accounting_modular.records.public import RecordsChange, RecordsContext


def basis_heads(application, scopes, read):
    result = {}
    for scope in sorted(set(scopes)):
        if scope == "company":
            continue
        if scope.startswith("business:"):
            validate_contract("core.schema.json", "BusinessNoteScope", scope)
            continue
        if not scope.startswith("period:"):
            raise ContractError("invalid_input", "说明范围须为公司、月份或具体业务。")
        period = scope.removeprefix("period:")
        validate_contract("core.schema.json", "Month", period)
        report = application.ledger.financial_snapshot(
            {
                "company_id": read.company_id,
                "period_from": period,
                "period_to": period,
                "view": "current",
            },
            read,
        )
        result[scope] = report["selection"]["selection_digest"]
    return result


def finish(application, submission, adoption, read, *, withdrawn_business_ids=()):
    scopes = {note["scope"] for note in submission.notes}
    supplied = scopes.copy()
    business_ids = {
        scope.removeprefix("business:") for scope in supplied if scope.startswith("business:")
    }
    if business_ids:
        businesses = read.repository("application").heads(business_ids)
        if any(
            identity not in businesses or businesses[identity]["state"] != "active"
            for identity in business_ids
        ):
            raise ContractError("not_found", "业务事项说明须关联本公司当前有效的业务。")
    if adoption.changed:
        first = max(
            read.start_period,
            min((ref["business_period"] for ref in adoption.results), default=read.start_period),
        )
        cursor = None
        while True:
            # An explicitly written later-month note may also depend on this
            # balance or year-to-date result, even before that work month opens.
            page = application.records.period_notes(first, "9999-12", read, cursor=cursor)
            scopes.update(note["scope"] for note in page.items if note["state"] == "current")
            cursor = page.next_cursor
            if cursor is None:
                break
    heads = basis_heads(application, scopes, read)
    plan = application.records.plan_change(
        RecordsChange(
            notes=tuple(submission.notes),
            unresolved=submission.unresolved,
            unresolved_scope=submission.unresolved_scope,
            expected_scope_head=submission.expected_scope_head,
        ),
        RecordsContext(read, submission.command_id, note_basis_heads=heads),
    )
    application.records.apply_current(plan, read)
    invalidated = application.records.invalidate_notes(
        {scope: head for scope, head in heads.items() if scope not in supplied},
        submission.command_id,
        read,
    )
    cleared = application.records.clear_notes(
        tuple("business:" + identity for identity in withdrawn_business_ids),
        submission.command_id,
        read,
    )
    return plan, tuple(sorted(set(invalidated) | set(cleared)))
