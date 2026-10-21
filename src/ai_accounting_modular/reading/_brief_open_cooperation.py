"""The complete small cooperation provider, shared by full and partial Open captures."""

from dataclasses import replace

from ai_accounting_modular.contracts.public import ReadInputs

from . import _brief_open_positions as positions
from ._brief_open_sources import batches


def capture(reader, scope, read, current_cutoff):
    selected = reader.investment.cooperation_positions(scope, read)
    contracts = tuple(
        sorted(
            {
                item.contract_id
                for item in selected.positions
                if item.operator_due_fen or item.company_due_fen
            }
        )
    )
    current_scope = {
        "company_id": scope["company_id"],
        "period_from": current_cutoff,
        "period_to": current_cutoff,
        "view": "current",
    }
    current, dependencies = [], selected.inputs
    for batch in batches(contracts):
        actual = reader.investment.cooperation_positions(current_scope, read, contract_ids=batch)
        current.extend(actual.positions)
        dependencies = dependencies.merge(
            replace(
                actual.inputs,
                business_ids=(),
                current_business_ids=tuple(
                    sorted(
                        set(actual.inputs.business_ids) | set(actual.inputs.current_business_ids)
                    )
                ),
            )
        )
    extras = positions.project(
        selected.positions,
        current,
        company_id=scope["company_id"],
        cutoff_period=scope["period_to"],
        identity=reader.identity,
        read=read,
    )
    objects = {
        identity
        for item in selected.positions
        if item.operator_due_fen or item.company_due_fen
        for identity in (item.operator_id, item.project_id)
    }
    return extras, dependencies.merge(
        ReadInputs(current_business_ids=contracts, object_ids=tuple(sorted(objects)))
    )
