"""Company settings live in its business data directory, outside source code."""

import json
import re

from .company_notes import read_company_notes


def company_export_profile(organization) -> dict:
    profile = {
        "profile_id": "mybank-standard-v1",
        "salary": {"label": "薪资", "source": "posted_payroll_line.net_salary_fen"},
        "labor": {"label": "劳务", "source": "untaxed_labor_fen"},
        "reimbursement": {
            "label": "报销",
            "categories": ["回扣报销1", "回扣报销2", "发票报销"],
            "operation": "sum",
            "combined_kernel_payable": "use_remaining_balance_once_without_inventing_split",
        },
        "external_amounts_allowed": False,
        "settled_payables_included": False,
    }
    notes = read_company_notes(organization)
    blocks = re.findall(r"```mybank-export\s*\n(.*?)\n```", notes["content"], flags=re.DOTALL)
    if len(blocks) > 1:
        raise ValueError("MYBANK_PROFILE_DUPLICATE")
    if blocks:
        profile = json.loads(blocks[0])
    try:
        valid = (
            profile["salary"]["source"]
            in {
                "posted_payroll_line.net_salary_fen",
                "reported_salary_minus_actual_tax_and_employee_contributions",
            }
            and profile["labor"] == {"label": "劳务", "source": "untaxed_labor_fen"}
            and profile["salary"]["label"] == "薪资"
            and profile["reimbursement"]["label"] in {"报销", "报销款"}
            and profile["reimbursement"]["operation"] == "sum"
            and profile["external_amounts_allowed"] is False
            and profile["settled_payables_included"] is False
        )
        categories = profile["reimbursement"]["categories"]
        valid = valid and bool(categories) and len(categories) == len(set(categories))
        valid = valid and set(categories) <= {"回扣报销1", "回扣报销2", "发票报销"}
    except (KeyError, TypeError):
        valid = False
    if not valid:
        raise ValueError("MYBANK_PROFILE_REQUIRES_SUPPORTED_KERNEL_RULE")
    return {**profile, "company_notes_sha256": notes["sha256"]}
