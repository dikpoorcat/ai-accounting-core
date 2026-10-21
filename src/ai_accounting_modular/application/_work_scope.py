"""The application's saved work range, without loading documents or business bodies."""

from dataclasses import dataclass

from ai_accounting_modular.contracts.public import ContractError, ReadContext, validate_contract


@dataclass(frozen=True)
class WorkScope:
    start_period: str
    working_period_to: str
    has_formal_results: bool
    closed_through: str | None


def working_period(read: ReadContext) -> str:
    # The initial month needs no state row; execution atomically saves every advance.
    saved = read.repository("application").working_period_to()
    value = read.start_period if saved is None else saved
    try:
        validate_contract("core.schema.json", "Month", value)
        if value < read.start_period:
            raise ValueError
    except (ContractError, ValueError) as error:
        raise ContractError("storage_corrupt", "保存的工作末月不合法或早于建账月。") from error
    return value


def work_scope(application, read: ReadContext) -> WorkScope:
    """Current saved company metadata; leave the caller's accounting selection untouched."""
    return WorkScope(
        read.start_period,
        working_period(read),
        application.ledger.has_formal_results(read),
        application.ledger.closed_through(read),
    )
