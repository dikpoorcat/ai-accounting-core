"""Clear one month's current issues without changing notes or other months."""

from ai_accounting_modular.contracts.public import ContractError, UnitOfWork, validate_contract

from ._types import RecordsChange, RecordsContext


def clear(records, period, command_id, uow):
    validate_contract("core.schema.json", "Month", period)
    validate_contract("core.schema.json", "Id", command_id)
    if (
        not isinstance(uow, UnitOfWork)
        or uow.view != "current"
        or uow.as_of is not None
        or uow.next_head <= uow.head
    ):
        raise ContractError("invalid_operation", "月份待办只能在当前公司的正式写事务中清理。")
    repository = uow.repository("records")
    month_scopes = {"period:" + period, "work:" + period}
    plans = []
    for scope in repository.issue_scopes_for_period(period):
        current = records.current_issues(scope, uow)
        retained = []
        for issue in current["issues"]:
            related = {
                value
                for value in (issue.get("period"), issue["required_before_close"])
                if value is not None
            }
            if not related and scope in month_scopes:
                related = {period}
            if related != {period}:
                retained.append(issue)
        plans.append(
            records.plan_change(
                RecordsChange(
                    unresolved=tuple(retained),
                    unresolved_scope=scope,
                    expected_scope_head=current["head"],
                ),
                RecordsContext(uow, command_id),
            )
        )
    changed = set()
    for plan in plans:
        records.apply_current(plan, uow)
        changed.update(plan.issue_periods)
    return tuple(sorted(changed, key=lambda value: (value is not None, value)))
