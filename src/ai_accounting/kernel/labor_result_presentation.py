"""Present saved labor deduction semantics using the exact stored fact grammar."""

from .contracts import KernelError
from .types import canonical


def _invalid():
    raise KernelError("content_integrity_failed", "劳务扣税结果与保存的事实合同不一致",
                      component="labor", reason="invalid_withholding_basis")


def labor_withholding_presentation(
    values, model, *, fact_id, subject_id, period, content_version=None,
):
    from .domains.payroll import LaborRemuneration
    from .payroll_history import HistoricalLaborRemuneration
    from .remuneration_tax_basis import RemunerationTaxBasis

    confirmed_accrual = (
        model.kind in {"labor_accrual", "labor_project_cost"} and "tax_basis" in values
    )
    if content_version != 1 and (model is LaborRemuneration or confirmed_accrual):
        basis = values.get("tax_basis")
        if not isinstance(basis, dict) or "withholding_method" in values:
            _invalid()
        metadata = {"fact_id", "subject_id", "revision", "evidence", "cash_payment_recorded"}
        if set(basis) != metadata | RemunerationTaxBasis.model_fields.keys():
            _invalid()
        try:
            saved = RemunerationTaxBasis.model_validate_json(canonical({
                key: basis[key] for key in RemunerationTaxBasis.model_fields
            }))
        except (TypeError, ValueError):
            _invalid()
        if (
            saved.source_kind != model.kind or saved.source_id != subject_id
            or saved.source_fact_id != fact_id
            or saved.period != period
            or type(values.get("tax_fen")) is not int
            or saved.tax_amount_fen != values["tax_fen"]
            or basis["cash_payment_recorded"] is not False
            or type(basis["revision"]) is not int or basis["revision"] < 1
            or not all(isinstance(basis[key], str) and basis[key]
                       for key in ("fact_id", "subject_id"))
            or not isinstance(basis["evidence"], (list, tuple)) or not basis["evidence"]
            or not all(isinstance(item, str) and item for item in basis["evidence"])
        ):
            _invalid()
        return {
            "withholding_method": "confirmed_tax_basis",
            "withholding_label": {
                "final_statement": "按最终报酬表确认扣税金额",
                "actual_withholding": "按实际扣税记录确认金额",
            }[saved.basis_type],
        }
    if "tax_basis" in values:
        _invalid()
    labels = {
        "not_withheld_not_filed": "尚未记录扣缴及申报",
        "gross_paid_without_withholding": "已按毛额付款、未扣税",
        "net_after_withholding": "已确认净额及扣税义务",
    }
    method = values.get("withholding_method")
    allowed = (
        {"gross_paid_without_withholding", "net_after_withholding"}
        if model is HistoricalLaborRemuneration or content_version == 1 and model.kind == "labor"
        else {"not_withheld_not_filed"}
        if model.kind in {"labor_accrual", "labor_project_cost"} else set()
    )
    if not isinstance(method, str) or method not in allowed:
        _invalid()
    return {"withholding_method": method, "withholding_label": labels[method]}
