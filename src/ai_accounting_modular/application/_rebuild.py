"""Install final standard facts in one new-company accounting transaction."""

from collections import defaultdict
from dataclasses import replace
from uuid import uuid4

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.records.public import RecordsChange, RecordsContext, StandardBundle
from ai_accounting_modular.storage.public import company_metadata

from . import _execution, _notes
from ._submission import Submission


def rebuild(application, bundle, unit, *, prepared_sources=None):
    if not isinstance(bundle, StandardBundle):
        raise ContractError("invalid_input", "简化重建必须使用已核验的完整标准资料。")
    if unit.head != 0:
        raise ContractError("invalid_operation", "简化重建只用于新建独立公司库。")
    metadata = company_metadata(unit)
    identity = bundle.company_identity
    if any(
        getattr(metadata, key) != identity[key]
        for key in ("credit_code", "legal_name", "start_period", "accounting_standard", "currency")
    ):
        raise ContractError("conflict", "新库公司资料与标准资料中的公司不一致。")
    completed = False

    def require_complete():
        if not completed:
            raise ContractError("invalid_operation", "整公司重建未完整成功，不能提交部分资料。")

    unit.check_before_commit(require_complete)
    command_id = str(uuid4())
    submission = Submission(
        company_id=unit.company_id,
        command_id=command_id,
        mutations=tuple({"op": "create", "expected_seq": 0, "fact": fact} for fact in bundle.facts),
        objects=tuple({**obj, "expected_seq": 0} for obj in bundle.objects),
        sources=bundle.sources,
    )
    outcome = _execution.execute(
        application,
        submission,
        unit,
        evaluation_period_to=bundle.working_period_to,
        prepared_sources=prepared_sources,
    )
    heads = _notes.basis_heads(application, (note["scope"] for note in bundle.notes), unit)
    notes = tuple(
        {
            **note,
            "expected_seq": 0,
            **({"expected_basis_head": heads[note["scope"]]} if note["scope"] in heads else {}),
        }
        for note in bundle.notes
    )
    plan = application.records.plan_change(
        RecordsChange(notes=notes), RecordsContext(unit, command_id, note_basis_heads=heads)
    )
    application.records.apply_current(plan, unit)
    scopes = defaultdict(list)
    for issue in bundle.issues:
        scopes[issue["scope"]].append(issue)
    issue_scopes = []
    for scope, issues in sorted(scopes.items()):
        scope_plan = application.records.plan_change(
            RecordsChange(unresolved=tuple(issues), unresolved_scope=scope, expected_scope_head=0),
            RecordsContext(unit, command_id),
        )
        application.records.apply_current(scope_plan, unit)
        issue_scopes.append(scope_plan.issue_scope)
    completed = True
    return replace(
        outcome,
        notes=tuple(plan.notes),
        issue_scopes=tuple(issue_scopes),
        unresolved=bundle.issues,
        changed=outcome.changed or bool(plan.changed_scopes or issue_scopes),
    )
