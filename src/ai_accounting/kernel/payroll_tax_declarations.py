"""Observed wage-tax declarations and payment plans are distinct from withholding.

Neither fact records cash, settles a wage liability, or changes calculated tax.
An explicitly retained difference stays payable until separate actual evidence
establishes its disposition.
"""

from typing import ClassVar, Literal

from pydantic import Field, model_validator

from .contracts import Context, Fact, KernelError, NeedsInformation, Outcome, Read
from .domains.payroll import PAYROLL_KINDS
from .types import ActualDate, NonNegativeFen, YearMonth, sum_fen


def declaration_scope(employee_id, period):
    return f"declared-wage-tax:{employee_id}:{period}"


def disbursement_scope(kind, subject_id):
    return f"wage-disbursement:{kind}:{subject_id}"


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
        )


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
        or wage.period != fact.tax_period
        or wage.period > fact.period
    ):
        raise KernelError("disbursement_source_conflict", "申报明细与工资的人员、税期或依据不一致")
    net = [
        item
        for item in wage.values.get("obligations", ())
        if item["name"] == "net" and item["normal"] == "credit" and item["category"] == "payable"
    ]
    if len(net) != 1 or net[0]["amount_fen"] != wage.values["net_fen"]:
        raise KernelError("invalid_export_obligation", "工资没有唯一明确的净薪应付款")
    difference = sum_fen((declaration.fact.declared_tax_fen, -wage.values["tax_fen"]))
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
            "calculated_tax_fen": wage.values.get("calculated_tax_fen", wage.values["tax_fen"]),
            "declared_tax_fen": declaration.fact.declared_tax_fen,
            "original_net_fen": net[0]["amount_fen"],
            "target_net_fen": target,
            "held_fen": difference,
            "withholding_recorded": (
                wage.values.get("actual_withholding", {}).get("withheld_tax_fen")
                == declaration.fact.declared_tax_fen
            ),
        },
    )


def export_disbursement(store, connection, calculation, values, remaining):
    """Check only this selected wage; the export snapshots every adopted version."""
    if calculation["kind"] not in PAYROLL_KINDS:
        return remaining, None
    subject = calculation["subject_id"]
    period = YearMonth.from_ordinal(calculation["period"])
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
    if not plans:
        if declarations and declarations[0].fact.declared_tax_fen != values["tax_fen"]:
            raise NeedsInformation(
                "disbursement_basis",
                "该工资的实际申报税额与计算不同，需明确本人的代发差额口径",
                sources=(subject, declarations[0].subject_id),
            )
        return remaining, (
            {
                "declaration_fact_id": declarations[0].id,
                "declaration_id": declarations[0].subject_id,
                "declaration_evidence": list(declarations[0].evidence),
                "declared_tax_fen": declarations[0].fact.declared_tax_fen,
                "held_fen": 0,
                "withholding_recorded": (
                    values.get("actual_withholding", {}).get("withheld_tax_fen")
                    == declarations[0].fact.declared_tax_fen
                ),
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
