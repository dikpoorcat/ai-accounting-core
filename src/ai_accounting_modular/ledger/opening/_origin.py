"""One public meaning for a sourced new-company zero opening."""

from ai_accounting_modular.contracts.public import AdoptedBasis, ResultTarget, validate_contract


def is_zero_origin(target: ResultTarget, basis: AdoptedBasis) -> bool:
    validate_contract("business-facts.schema.json", "ResultTarget", target)
    validate_contract("business-facts.schema.json", "AdoptedBasis", basis)
    return (
        target["kind"] == "opening.package"
        and basis["kind"] == "opening.package"
        and target["result_slot"] == "main"
        and basis["result_slot"] == "main"
        and basis["input"]["basis"] == "newly_established"
        and target["values"]["debit_fen"] == "0"
        and target["values"]["credit_fen"] == "0"
    )
