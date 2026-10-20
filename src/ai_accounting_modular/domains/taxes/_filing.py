"""Only supported, explicitly adopted forms receive known filing fields."""

from ai_accounting_modular.contracts.public import parse_rate

from ._common import amount, issue

_ACTUAL_ROWS = {
    **{
        f"row{number}_fen": f"row_{number}_fen"
        for number in (1, 2, 3, 9, 10, 11, 15, 16, 17, 18, 19)
    },
    "prepaid_vat_fen": "row_21_fen",
    "vat_due_fen": "row_22_fen",
    "urban_due_fen": "row_23_fen",
    "education_due_fen": "row_24_fen",
    "local_education_due_fen": "row_25_fen",
}


def filing(fact, metrics, sales, policy, calculated_fields=None, actual=None):
    problems = []
    profile = fact["data"].get("filing_profile")
    if profile is None:
        problems.append(
            issue(
                fact,
                "/data/filing_profile",
                "缺少实际适用表式资料，税额仍按已明确来源采用。",
                blocking_scope="none",
            )
        )
    elif profile["form_scope"] != "nonpilot":
        metrics.update(
            filing_state="unsupported_form",
            filing_issues=[
                issue(
                    fact,
                    "/data/filing_profile/form_scope",
                    "当前未实现该试点或其他表式。",
                    code="unsupported_form",
                    blocking_scope="none",
                )
            ],
        )
        return
    if policy is None:
        problems.append(
            issue(
                fact,
                "/data/vat_policy_id",
                "尚未明确采用的form_version，不用算法字段冒充实际申报。",
                blocking_scope="none",
            )
        )
        metrics.update(filing_state="partial", filing_issues=problems)
        return
    if any(
        row["filing_bucket"] != "standard" or parse_rate(row["nominal_rate"]) != parse_rate("0.03")
        for row in sales
    ):
        metrics.update(
            filing_state="unsupported_form",
            filing_issues=[
                issue(
                    fact,
                    "/data/filing_profile",
                    "此类税销售尚未接入对应申报表行，税额采用不受影响。",
                    code="unsupported_form",
                    blocking_scope="none",
                )
            ],
        )
        return
    fields = {"form_version": policy["form_version"]}
    if actual is None:
        fields.update(calculated_fields or {})
        if any(
            row["invoice_kind"] == "unestablished" and amount(row["net_sales_fen"]) for row in sales
        ):
            fields.pop("row_2_fen", None)
            fields.pop("row_3_fen", None)
            problems.append(
                issue(
                    fact,
                    "/data/filing_profile",
                    "发票类别尚未明确，第2/3栏保留未知。",
                    blocking_scope="none",
                )
            )
        problems.append(
            issue(
                fact,
                "/data/settlement",
                "本期预缴和应补退没有实际来源，第21—25栏保留未知。",
                blocking_scope="none",
            )
        )
    else:
        fields["row_20_fen"] = actual["assessed_vat_fen"]
        fields["row_11_fen"] = "0"
        fields["row_19_fen"] = "0"
        for source, target in _ACTUAL_ROWS.items():
            if source in actual:
                fields[target] = actual[source]
        for source, target in _ACTUAL_ROWS.items():
            if source not in actual and source not in ("row11_fen", "row19_fen"):
                problems.append(
                    issue(
                        fact,
                        "/data/actual_declaration/" + source,
                        f"实际来源没有明确{target}，不拿计算参考补值。",
                        blocking_scope="none",
                    )
                )
        if "row1_fen" in actual and sum(
            amount(actual[field]) for field in ("row2_fen", "row3_fen") if field in actual
        ) > amount(actual["row1_fen"]):
            problems.append(
                issue(
                    fact,
                    "/data/actual_declaration/row1_fen",
                    "实际开票栏超过应税净销售栏；当前普通表式无法说明差额或跨期开票关系，"
                    "税额仍可按有源实际金额采用，申报明细不能标为完整。",
                    code="unsupported_form",
                    blocking_scope="none",
                )
            )
    metrics["filing_fields"] = fields
    metrics.update(filing_state="partial" if problems else "ready", filing_issues=problems)
