"""Observed wage-tax declarations and payment plans are distinct from withholding.

Neither fact records cash, settles a wage liability, or changes the adopted tax basis.
An explicitly retained difference stays payable until separate actual evidence
establishes its disposition.
"""

from typing import ClassVar, Literal

from pydantic import Field, model_validator

from .contracts import Context, Fact, KernelError, NeedsInformation, Outcome, Read
from .domains.payroll import PAYROLL_KINDS
from .remuneration_tax_basis import (
    RemunerationTaxBasis,
    _pool_allocation_data,
    remuneration_tax_period_scope,
)
from .reporting_fields import payroll_result_fields
from .types import ActualDate, NonNegativeFen, YearMonth, sum_fen


def declaration_scope(employee_id, period):
    return f"declared-wage-tax:{employee_id}:{period}"


def disbursement_scope(kind, subject_id):
    return f"wage-disbursement:{kind}:{subject_id}"


def _withholding_recorded(values, amount):
    basis = values.get("tax_basis")
    if basis is not None:
        return (
            basis.get("basis_type") == "actual_withholding"
            and basis.get("tax_amount_fen") == amount
        )
    # A frozen historical actual record remains an observed event, not an estimate.
    return values.get("actual_withholding", {}).get("withheld_tax_fen") == amount


def _tax_period(values, original_period):
    return YearMonth(values.get("tax_basis", {}).get("tax_period", str(original_period)))


def _tax_peer_reads(context, tax_period):
    reads = []
    for item in context.facts(RemunerationTaxBasis.kind, remuneration_tax_period_scope(tax_period)):
        if item.fact.source_kind not in PAYROLL_KINDS:
            continue
        reads.append(Read("fact", item.fact.source_kind, str(item.fact.period)))
        reads.append(Read("calculation", item.fact.source_kind, str(item.fact.period)))
        if item.fact.pooled_tax_proof is not None:
            reads.extend(
                Read("fact", allocation.source_kind, str(allocation.period))
                for allocation in item.fact.pooled_tax_proof.allocations
                if allocation.source_kind in PAYROLL_KINDS
            )
    return tuple(dict.fromkeys(reads))


def _tax_peer_exact_reads(context, tax_period):
    """Only historical originals absent from the current month batch need extra reads."""
    return tuple(
        dict.fromkeys(
            Read("fact", item.fact.source_kind, "#" + item.fact.source_fact_id)
            for item in context.facts(
                RemunerationTaxBasis.kind, remuneration_tax_period_scope(tax_period)
            )
            if item.fact.source_kind in PAYROLL_KINDS
            and not any(
                source.id == item.fact.source_fact_id
                for source in context.facts(item.fact.source_kind, str(item.fact.period))
            )
        )
    )


def _declared_for_source(context, declaration, employee, tax_period, kind, subject, values):
    peers, original_people = [], {}
    for item in context.facts(RemunerationTaxBasis.kind, remuneration_tax_period_scope(tax_period)):
        if item.fact.source_kind not in PAYROLL_KINDS:
            continue
        originals = [
            original
            for original in context.facts(item.fact.source_kind, str(item.fact.period))
            if original.subject_id == item.fact.source_id
        ]
        adopted = [source for source in originals if source.id == item.fact.source_fact_id]
        if not adopted:
            adopted = context.facts(item.fact.source_kind, "#" + item.fact.source_fact_id)
        if (
            len(adopted) != 1
            or adopted[0].subject_id != item.fact.source_id
            or adopted[0].fact.period != item.fact.period
        ):
            raise NeedsInformation(
                "disbursement_tax_allocation",
                "需要税额采用的精确工资来源正文",
                sources=(item.fact.source_id, item.fact.source_fact_id),
                precision=("exact_fact_revision",),
            )
        original_people[(item.fact.source_kind, item.fact.source_id, item.fact.period)] = adopted[
            0
        ].fact.employee_id
        if adopted[0].fact.employee_id != employee:
            continue
        calculations = [
            calculation
            for calculation in context.calculations(item.fact.source_kind, str(item.fact.period))
            if calculation.subject_id == item.fact.source_id
            and calculation.values.get("tax_basis", {}).get("fact_id") == item.id
            and len(originals) == 1
            and calculation.fact_id == originals[0].id
        ]
        if len(originals) == 1 and originals[0].id == item.fact.source_fact_id:
            peers.append(item)
        elif len(calculations) == 1:
            peers.append(item)
        else:
            raise NeedsInformation(
                "disbursement_tax_allocation",
                "税额采用的原工资正文已变化，须先完成该来源的工资核对",
                sources=(item.fact.source_id, item.fact.source_fact_id),
                precision=("exact_fact_revision",),
            )
    if len(peers) <= 1 and not any(item.fact.pooled_tax_proof for item in peers):
        return declaration.fact.declared_tax_fen
    proofs = [item.fact.pooled_tax_proof for item in peers]
    proof = proofs[0]
    expected = {
        (allocation.source_kind, allocation.source_id, allocation.period)
        for allocation in (proof.allocations if proof is not None else ())
        if allocation.source_kind in PAYROLL_KINDS
        and (
            original_people[(allocation.source_kind, allocation.source_id, allocation.period)]
            == employee
            if (allocation.source_kind, allocation.source_id, allocation.period) in original_people
            else any(
                source.subject_id == allocation.source_id and source.fact.employee_id == employee
                for source in context.facts(allocation.source_kind, str(allocation.period))
            )
        )
    }
    if len(peers) == 1 and len(expected) <= 1:
        return declaration.fact.declared_tax_fen
    matching = [
        item for item in peers if item.fact.source_kind == kind and item.fact.source_id == subject
    ]
    if (
        proof is None
        or any(
            other is None or _pool_allocation_data(other) != _pool_allocation_data(proof)
            for other in proofs
        )
        or proof.evidence_digest not in declaration.evidence
        or len(matching) != 1
        or expected
        != {(item.fact.source_kind, item.fact.source_id, item.fact.period) for item in peers}
        or matching[0].id != values.get("tax_basis", {}).get("fact_id")
        or sum_fen(item.fact.tax_amount_fen for item in peers) != declaration.fact.declared_tax_fen
    ):
        raise NeedsInformation(
            "disbursement_tax_allocation",
            "同一人员税期涉及多笔工资，需要有申报原件依据的完整逐笔税额分配，不能重复采用汇总税额",
            sources=(declaration.id, *(item.id for item in peers)),
            precision=("integer_fen", "exact_source_allocation"),
        )
    return matching[0].fact.tax_amount_fen


class PayrollTaxDeclarationActual(Fact):
    kind: ClassVar[str] = "payroll_tax_declaration_actual"
    lane: ClassVar[str] = "management"
    immutable: ClassVar[bool] = True
    identity_fields: ClassVar[tuple[str, ...]] = ("employee_id", "tax_period", "income_category")
    employee_id: str = Field(min_length=1)
    tax_period: YearMonth
    income_category: Literal["wages"]
    declared_tax_fen: NonNegativeFen
    declaration_confirmed: Literal[True]
    declaration_date: ActualDate | None = Field(
        default=None,
        description="原件明确的实际申报日；未建立则为空，不以税期或记录月份补日",
    )

    def scopes(self):
        return (declaration_scope(self.employee_id, self.tax_period),)

    @model_validator(mode="after")
    def observed_period(self):
        if self.tax_period > self.period:
            raise ValueError("a declaration cannot cover a future unrecorded tax period")
        if self.declaration_date and self.declaration_date.period > self.period:
            raise ValueError("the declaration has not occurred in the recording period")
        return self


class PayrollDisbursementBasis(Fact):
    kind: ClassVar[str] = "payroll_disbursement_basis"
    lane: ClassVar[str] = "management"
    identity_fields: ClassVar[tuple[str, ...]] = ("payroll_kind", "payroll_id")
    payroll_kind: Literal["payroll", "payroll_bounded"]
    payroll_id: str = Field(min_length=1)
    employee_id: str = Field(min_length=1)
    tax_period: YearMonth
    declaration_id: str = Field(min_length=1)
    declaration_fact_id: str = Field(min_length=1)
    use_declared_tax_for_disbursement: Literal[True]

    def scopes(self):
        return (disbursement_scope(self.payroll_kind, self.payroll_id),)

    def reads(self):
        return (
            Read("fact", self.payroll_kind, "@" + self.payroll_id),
            Read("calculation", self.payroll_kind, "@" + self.payroll_id),
            Read("fact", PayrollTaxDeclarationActual.kind, "@" + self.declaration_id),
            Read(
                "fact",
                PayrollTaxDeclarationActual.kind,
                declaration_scope(self.employee_id, self.tax_period),
            ),
            Read("fact", self.kind, disbursement_scope(self.payroll_kind, self.payroll_id)),
            Read("fact", RemunerationTaxBasis.kind, remuneration_tax_period_scope(self.tax_period)),
        )

    def reads_after(self, subject_id, context):
        return _tax_peer_reads(context, self.tax_period)

    def reads_final(self, subject_id, context):
        reads = _tax_peer_exact_reads(context, self.tax_period)
        if len(reads) > 64:
            raise NeedsInformation(
                "disbursement_tax_allocation",
                "该税期历史来源超过一次核验的读取上限，须先核对来源范围",
                sources=(subject_id,),
                precision=("bounded_exact_fact_revisions",),
            )
        return reads


def calculate_basis(version, context: Context):
    fact = version.fact
    if not version.evidence:
        raise NeedsInformation(
            "disbursement_basis.evidence", "需要公司明确采用实际申报额代发的依据"
        )
    plans = context.facts(fact.kind, disbursement_scope(fact.payroll_kind, fact.payroll_id))
    if len(plans) != 1 or plans[0].id != version.id:
        raise KernelError("ambiguous_disbursement_basis", "同一工资来源只能有一份当前代发口径")
    wage_fact = context.one(fact.payroll_kind, "@" + fact.payroll_id)
    wages = context.calculations(fact.payroll_kind, "@" + fact.payroll_id)
    if len(wages) != 1 or wages[0].fact_id != wage_fact.id:
        raise NeedsInformation(
            "payroll_id", "需要该工资来源当前已发布的正式计算", sources=(fact.payroll_id,)
        )
    wage = wages[0]
    wage_fields = payroll_result_fields(wage.values)
    declaration = context.one(PayrollTaxDeclarationActual.kind, "@" + fact.declaration_id)
    declarations = context.facts(
        PayrollTaxDeclarationActual.kind, declaration_scope(fact.employee_id, fact.tax_period)
    )
    if len(declarations) != 1 or declarations[0].id != declaration.id:
        raise KernelError("ambiguous_tax_declaration", "该人员税期存在不唯一的实际工资申报明细")
    if declaration.id != fact.declaration_fact_id:
        raise KernelError("declaration_version_changed", "实际申报明细已变，需要重新确认采用版本")
    if not declaration.evidence or (
        declaration.fact.employee_id != fact.employee_id
        or declaration.fact.tax_period != fact.tax_period
        or wage.values.get("employee_id") != fact.employee_id
        or _tax_period(wage.values, wage.period) != fact.tax_period
        or wage.period > fact.period
    ):
        raise KernelError("disbursement_source_conflict", "申报明细与工资的人员、税期或依据不一致")
    net = [
        item
        for item in wage.values.get("obligations", ())
        if item["name"] == "net" and item["normal"] == "credit" and item["category"] == "payable"
    ]
    if len(net) != 1 or net[0]["amount_fen"] != wage_fields["net_fen"]:
        raise KernelError("invalid_export_obligation", "工资没有唯一明确的净薪应付款")
    declared_for_source = _declared_for_source(
        context,
        declaration,
        fact.employee_id,
        fact.tax_period,
        fact.payroll_kind,
        fact.payroll_id,
        wage.values,
    )
    difference = sum_fen((declared_for_source, -wage_fields["tax_fen"]))
    target = sum_fen((net[0]["amount_fen"], -difference))
    if target < 0 or target > net[0]["amount_fen"]:
        raise NeedsInformation(
            "disbursement_difference",
            "申报差额使拟代发净額为负或超过现有工资应付，需有依据的差额处置",
            sources=(fact.payroll_id, fact.declaration_id),
            precision=("integer_fen",),
        )
    return Outcome(
        (),
        {
            "employee_id": fact.employee_id,
            "tax_period": fact.tax_period,
            "payroll_kind": fact.payroll_kind,
            "payroll_id": fact.payroll_id,
            "payroll_fact_id": wage.fact_id,
            "payroll_calculation_id": wage.id,
            "payroll_result_digest": wage.result_digest,
            "declaration_id": declaration.subject_id,
            "declaration_fact_id": declaration.id,
            "declaration_evidence": list(declaration.evidence),
            "adopted_tax_fen": wage_fields["tax_fen"],
            "declared_tax_fen": declared_for_source,
            "declared_period_total_fen": declaration.fact.declared_tax_fen,
            "original_net_fen": net[0]["amount_fen"],
            "target_net_fen": target,
            "held_fen": difference,
            "withholding_recorded": _withholding_recorded(wage.values, declared_for_source),
        },
    )


def export_disbursement(store, connection, calculation, values, remaining):
    """Check only this selected wage; the export snapshots every adopted version."""
    if calculation["kind"] not in PAYROLL_KINDS:
        return remaining, None
    wage_fields = payroll_result_fields(values)
    subject = calculation["subject_id"]
    period = _tax_period(values, YearMonth.from_ordinal(calculation["period"]))
    declarations = store.select(
        connection,
        Read(
            "fact",
            PayrollTaxDeclarationActual.kind,
            declaration_scope(values["employee_id"], period),
        ),
    )
    scope = disbursement_scope(calculation["kind"], subject)
    plans = store.select(connection, Read("fact", PayrollDisbursementBasis.kind, scope))
    if len(declarations) > 1:
        raise KernelError(
            "ambiguous_tax_declaration", "相关人员税期的实际申报税额不唯一", sources=[subject]
        )
    if len(plans) > 1:
        raise KernelError(
            "ambiguous_disbursement_basis", "相关工资的代发口径不唯一", sources=[subject]
        )
    declared_for_source = None
    if declarations:
        read = Read("fact", RemunerationTaxBasis.kind, remuneration_tax_period_scope(period))
        context = Context({read: store.select(connection, read)})
        peer_reads = _tax_peer_reads(context, period)
        context = Context({read: context.select(read), **store.select_many(connection, peer_reads)})
        originals = _tax_peer_exact_reads(context, period)
        context = Context(
            {
                read: context.select(read),
                **{selection: context.select(selection) for selection in peer_reads},
                **store.select_many(connection, originals),
            }
        )
        declared_for_source = _declared_for_source(
            context,
            declarations[0],
            values["employee_id"],
            period,
            calculation["kind"],
            subject,
            values,
        )
    if not plans:
        if declarations and declared_for_source != wage_fields["tax_fen"]:
            raise NeedsInformation(
                "disbursement_basis",
                "该工资的实际申报税额与已采用扣税不同，需明确本人的代发差额口径",
                sources=(subject, declarations[0].subject_id),
            )
        return remaining, (
            {
                "declaration_fact_id": declarations[0].id,
                "declaration_id": declarations[0].subject_id,
                "declaration_evidence": list(declarations[0].evidence),
                "declared_tax_fen": declared_for_source,
                "declared_period_total_fen": declarations[0].fact.declared_tax_fen,
                "held_fen": 0,
                "withholding_recorded": _withholding_recorded(values, declared_for_source),
            }
            if declarations
            else None
        )
    plan = plans[0]
    calculations = store.select(
        connection, Read("calculation", plan.fact.kind, "@" + plan.subject_id)
    )
    if (
        len(calculations) != 1
        or calculations[0].fact_id != plan.id
        or connection.execute(
            "SELECT 1 FROM pending WHERE subject_id=?", (plan.subject_id,)
        ).fetchone()
    ):
        raise NeedsInformation(
            "disbursement_basis",
            "该工资的代发口径尚未发布或需要复核",
            sources=(subject, plan.subject_id),
        )
    adopted = calculations[0]
    data = adopted.values
    if (
        len(declarations) != 1
        or data["declaration_fact_id"] != declarations[0].id
        or data["payroll_calculation_id"] != calculation["id"]
    ):
        raise KernelError(
            "disbursement_basis_changed",
            "工资或实际申报版本已变化，需要重新复核代发",
            sources=[subject],
        )
    consumed = sum_fen((data["original_net_fen"], -remaining))
    amount = sum_fen((data["target_net_fen"], -consumed))
    if amount < 0:
        raise NeedsInformation(
            "disbursement_difference",
            "该工资已核销金额超过本次确认的代发目标，需明确差额处置",
            sources=(subject,),
        )
    return amount, dict(data) | {
        "declaration_evidence": list(data["declaration_evidence"]),
        "basis_fact_id": plan.id,
        "basis_calculation_id": adopted.id,
        "basis_id": plan.subject_id,
        "basis_evidence": list(plan.evidence),
        "settled_fen": consumed,
        "remaining_payable_fen": remaining,
        "proposed_payment_fen": amount,
    }


def register(registry):
    registry.register(PayrollTaxDeclarationActual)
    registry.register(PayrollDisbursementBasis, calculate_basis)
