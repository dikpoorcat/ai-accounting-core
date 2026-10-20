"""Independent financing checks exercise only the assembled public application."""

from copy import deepcopy
from hashlib import sha256
from uuid import uuid4

from ._support import LENDER, OWNER, assemble, fact, get, payment, request, result, six


def test_unknown_overpayment_reclassification_day_cannot_change_zero_principal(tmp_path):
    app, company = assemble(tmp_path)
    _, contract, loan, interest, _, _ = six()
    paid = payment(loan, 1_100_000, day="2026-01-01")
    excess = fact(
        "operations.overpayment",
        {
            "source": {"business_id": loan["business_id"], "slot": "principal"},
            "amount_fen": "100000",
        },
    )

    actual = app.accounting_apply(request(company, excess, interest, paid, loan, contract))

    assert actual.get("completion") == "complete", (
        actual.get("error"),
        actual.get("message"),
        actual.get("fact_issues"),
    )
    assert result(app, company, interest)["target"]["values"]["interest_fen"] == "0"


def test_unknown_actual_personal_repayment_day_still_blocks_material_interest(tmp_path):
    app, company = assemble(tmp_path)
    _, contract, loan, interest, _, _ = six()
    accepted = fact(
        "operations.reimbursement",
        {
            "type": "acceptance",
            "payer_id": OWNER,
            "payer_kind": "owner",
            "sources": [
                {
                    "source": {"business_id": loan["business_id"], "slot": "principal"},
                    "amount_fen": "400000",
                    "recipient_id": LENDER,
                }
            ],
        },
    )

    actual = app.accounting_apply(request(company, accepted, interest, loan, contract))

    assert actual.get("error") == "needs_information", actual
    assert any(issue["code"] == "actual_payment_basis_required" for issue in actual["fact_issues"])
    assert get(app, company, loan["business_id"], accepted["business_id"])["businesses"] == []


def test_real_repayment_withdrawal_recalculates_both_months_without_reposting_drawdown(tmp_path):
    app, company = assemble(tmp_path)
    _, contract, loan, january, _, _ = six()
    february = deepcopy(january)
    february.update(business_id=str(uuid4()), period="2026-02")
    february["data"].update(start_on="2026-02-01", end_before="2026-03-01")
    january_paid = payment(loan, 500000, day="2026-01-16")
    february_paid = payment(loan, 250000, day="2026-02-15")

    actual = app.accounting_apply(
        request(company, february_paid, february, january_paid, january, loan, contract)
    )
    assert actual.get("completion") == "complete", actual
    assert result(app, company, january)["target"]["values"]["interest_fen"] == "2300"
    assert result(app, company, february)["target"]["values"]["interest_fen"] == "1050"
    drawdown_before = result(app, company, loan)

    actual = app.accounting_apply(
        {
            "contract": "ai-accounting-modular/1",
            "company_id": company,
            "command_id": str(uuid4()),
            "mutations": [
                {
                    "op": "withdraw",
                    "business_id": january_paid["business_id"],
                    "expected_seq": 1,
                }
            ],
        }
    )

    assert actual.get("completion") == "complete", actual
    assert result(app, company, january)["target"]["values"]["interest_fen"] == "3100"
    assert result(app, company, february)["target"]["values"]["interest_fen"] == "2450"
    assert result(app, company, loan) == drawdown_before


def test_missing_role_issue_bounds_hints_without_rejecting_full_source_set(tmp_path):
    app, company = assemble(tmp_path)
    sources = []
    for number in range(501):
        text = f"Synthetic financing source {number}."
        sources.append(
            {
                "source_id": str(uuid4()),
                "kind": "owner_statement",
                "text": text,
                "sha256": sha256(text.encode()).hexdigest(),
                "archive_month": "2026-01",
            }
        )
    first = request(company)
    first["sources"] = sources[:500]
    assert app.accounting_apply(first)["completion"] == "complete"
    _, contract, _, _, _, _ = six()
    contract["data"]["lender_id"] = OWNER  # A real person cannot be a licensed institution.
    contract["evidence_ids"] = [source["source_id"] for source in sources]
    submitted = request(company, contract)
    submitted["objects"] = []
    submitted["sources"] = sources[500:]

    actual = app.accounting_apply(submitted)

    assert actual.get("error") == "invalid_input", actual
    assert actual["fact_issues"][0]["code"] == "incompatible_object_kind"
    assert len(actual["fact_issues"][0]["reusable_sources"]) == 500
    assert get(app, company, contract["business_id"])["businesses"] == []

    contract["data"]["lender_id"] = LENDER
    submitted["mutations"][0]["fact"] = deepcopy(contract)
    actual = app.accounting_apply(submitted)
    assert actual.get("completion") == "complete", actual
    current = get(app, company, contract["business_id"])["businesses"][0]
    assert len(current["fact"]["evidence_ids"]) == 501
    assert len(current["results"][0]["basis"]["source_locations"]) == 501
