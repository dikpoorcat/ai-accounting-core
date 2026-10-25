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
    month_scope = "period:" + period
    plans = []
    for scope in repository.issue_scopes_for_period(period):
        current = records.current_issues(scope, uow)
        retained = tuple(
            issue
            for issue in current["issues"]
            if issue.get("period") != period
            and not (scope == month_scope and "period" not in issue)
        )
        plans.append(
            records.plan_change(
                RecordsChange(
                    unresolved=retained,
                    unresolved_scope=scope,
                    expected_scope_head=current["head"],
                ),
                RecordsContext(uow, command_id),
            )
        )
    changed = []
    for plan in plans:
        records.apply_current(plan, uow)
        if "work" in plan.changed_scopes:
            changed.append(plan.issue_scope["scope"])
    return tuple(changed)
