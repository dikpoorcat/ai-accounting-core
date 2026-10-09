"""Owner classifications of exact adopted obligations, shared by SQL and Python.

The accounting direction and stable obligation identity are never changed here.
Only three typed source families need a scalar from the adopted calculation;
neither current facts nor entity roles can supply that historical meaning.
"""

from __future__ import annotations

from .contracts import KernelError
from .types import canonical

SEMANTIC_SOURCE_KINDS = frozenset({
    "opening_obligation", "employee_advance", "reimbursement_acceptance",
})

# Ordered rules generate both consumers. A receivable fallback precedes all
# payable rules, so a deposit's refund and personal reimbursement stay distinct.
_RULES = (
    ("customer_receivables", (("category", "eq", "receivable"), ("account", "eq", "1122"))),
    ("supplier_advances", (("category", "eq", "receivable"), ("account", "eq", "1123"))),
    ("refundable_deposit_receivables", (
        ("category", "eq", "receivable"), ("source_kind", "eq", "opening_obligation"),
        ("semantic", "eq", "deposit_receivable"),
    )),
    ("refundable_deposit_receivables", (
        ("category", "eq", "receivable"), ("source_kind", "contains", "deposit"),
    )),
    ("other_receivables", (("category", "eq", "receivable"),)),
    ("payroll_payables", (("source_kind", "in", (
        "payroll", "payroll_bounded", "annual_bonus", "opening_payroll_payable",
    )),)),
    ("labor_payables", (("source_kind", "in", (
        "labor", "labor_accrual", "labor_project_cost",
    )),)),
    ("supplier_payables", (("account", "eq", "2202"),)),
    ("employee_payables", (("account", "eq", "224101"),)),
    ("employee_payables", (("source_kind", "in", (
        "reimbursed_asset", "reimbursed_asset_batch",
    )),)),
    ("employee_payables", (
        ("source_kind", "eq", "opening_obligation"),
        ("semantic", "eq", "employee_reimbursement"),
    )),
    ("employee_payables", (
        ("source_kind", "in", ("employee_advance", "reimbursement_acceptance")),
        ("semantic", "eq", "employee"),
    )),
)


def _invalid(ident, reason):
    raise KernelError(
        "content_integrity_failed", "待收待付的精确业务分类依据不匹配",
        component="obligation_classification", record_id=ident or "*", reason=reason,
    )


def validate_semantic(source_kind, category, account, semantic, *, ident=None):
    if source_kind == "opening_obligation":
        from .domains.opening import OBLIGATION_MAPPING

        if not isinstance(semantic, str) or semantic not in OBLIGATION_MAPPING:
            _invalid(ident, "opening_nature_missing_or_invalid")
        expected_account, normal, _cashflow, _expense = OBLIGATION_MAPPING[semantic]
        if (category, account) != (
            "receivable" if normal == "debit" else "payable", expected_account,
        ):
            _invalid(ident, "opening_nature_obligation_conflict")
    elif source_kind in {"employee_advance", "reimbursement_acceptance"}:
        if not isinstance(semantic, str) or semantic not in {"employee", "owner"}:
            _invalid(ident, "payer_kind_missing_or_invalid")
        if (category, account) != ("payable", "2241"):
            _invalid(ident, "payer_kind_obligation_conflict")


def obligation_category(category, account, source_kind, *, semantic=None):
    if category not in {"receivable", "payable"}:
        return "unknown"
    validate_semantic(source_kind, category, account, semantic)
    values = {
        "category": category, "account": account, "source_kind": source_kind or "",
        "semantic": semantic,
    }
    for label, conditions in _RULES:
        if all(
            values[field] == expected if operator == "eq"
            else values[field] in expected if operator == "in"
            else expected in values[field]
            for field, operator, expected in conditions
        ):
            return label
    return "other_payables"


def obligation_category_sql(*, semantic):
    """Render the same ordered rules for normalized, already verified rows."""
    columns = {
        "category": "category", "account": "account",
        "source_kind": "coalesce(source_kind,'')", "semantic": semantic,
    }

    def literal(value):
        return "'" + value.replace("'", "''") + "'"

    parts = []
    for label, conditions in _RULES:
        predicates = []
        for field, operator, expected in conditions:
            column = columns[field]
            if operator == "eq":
                predicates.append(f"{column}={literal(expected)}")
            elif operator == "in":
                predicates.append(f"{column} IN ({','.join(map(literal, expected))})")
            else:
                predicates.append(f"instr({column},{literal(expected)})>0")
        parts.append(f"WHEN {' AND '.join(predicates)} THEN {literal(label)}")
    return (
        "CASE WHEN category IS NULL OR category NOT IN ('receivable','payable') "
        "THEN 'unknown' " + " ".join(parts) + " ELSE 'other_payables' END"
    )


def adopted_source_semantics(connection, obligations, *, reads=None):
    """Verify and retain only the scalar meaning of exact referenced sources.

    The publication/frozen consumer proves adoption before calling this helper.
    Result bytes and the calculation/fact binding are checked before JSON1 reads
    the selected scalar. Missing, conflicting and unsupported meanings fail.
    """
    selected = [
        item for item in obligations
        if (item.get("source_business") or {}).get("kind") in SEMANTIC_SOURCE_KINDS
    ]
    if not selected:
        return {}
    identifiers = {item.get("source_calculation_id") for item in selected}
    if None in identifiers or any(not isinstance(ident, str) for ident in identifiers):
        _invalid(None, "adopted_source_missing")
    if reads is None:
        from .stored_json import verify_sql_outcomes

        verify_sql_outcomes(connection, identifiers)
    else:
        reads.verify_sql_outcomes(identifiers)
    actual = {
        row["id"]: row
        for row in connection.execute(
            "SELECT c.id,c.subject_id,c.kind,c.fact_id,c.period,"
            "f.subject_id fact_subject,f.period fact_period,s.kind fact_kind,"
            "EXISTS(SELECT 1 FROM calculation_seal z WHERE z.calculation_id=c.id) cs,"
            "EXISTS(SELECT 1 FROM fact_seal z WHERE z.fact_id=c.fact_id) fs,"
            "CASE WHEN c.kind='opening_obligation' THEN "
            "json_extract(c.outcome,'$.values.nature') ELSE "
            "json_extract(c.outcome,'$.values.payer_kind') END semantic "
            "FROM json_each(?) ids JOIN calculation c ON c.id=ids.value "
            "LEFT JOIN fact_revision f ON f.id=c.fact_id "
            "LEFT JOIN subject s ON s.id=f.subject_id",
            (canonical(sorted(identifiers)),),
        )
    }
    if actual.keys() != identifiers:
        _invalid(None, "adopted_source_missing")
    result = {}
    for item in selected:
        ident = item["source_calculation_id"]
        source = actual[ident]
        business = item["source_business"]
        if (
            not source["cs"] or not source["fs"]
            or (source["subject_id"], source["kind"], source["period"])
            != (source["fact_subject"], source["fact_kind"], source["fact_period"])
            or (source["subject_id"], source["kind"], source["fact_id"])
            != (business.get("subject_id"), business.get("kind"), item.get("source_fact_id"))
            or item.get("name") != "primary"
            or item.get("key") != f"{source['kind']}:{source['subject_id']}:primary"
        ):
            _invalid(ident, "adopted_source_identity_conflict")
        semantic = source["semantic"]
        validate_semantic(
            source["kind"], item.get("category"), item.get("account"), semantic, ident=ident,
        )
        result[ident] = semantic
    return result


def classify_obligations(connection, obligations, *, reads=None):
    obligations = list(obligations)
    semantics = adopted_source_semantics(connection, obligations, reads=reads)
    return [
        {**item, "category_key": obligation_category(
            item.get("category"), item.get("account"),
            (item.get("source_business") or {}).get("kind"),
            semantic=semantics.get(item.get("source_calculation_id")),
        )}
        for item in obligations
    ]
