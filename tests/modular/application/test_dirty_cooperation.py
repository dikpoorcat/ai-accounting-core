"""Real monthly cooperation keeps clean formal heads and recalculates the suffix."""

from copy import deepcopy
from uuid import uuid4

from ._working_case import case as case


def results(case, agreement):
    current = case.get(agreement["business_id"])["businesses"][0]
    return {view["reference"]["result_slot"]: view for view in current["results"]}


def prepare(case):
    case.seed(expense=False)
    operator, project, bank = (str(uuid4()) for _ in range(3))
    agreement = {
        "business_id": str(uuid4()),
        "kind": "investment.cooperation_contract",
        "period": "2026-01",
        "evidence_ids": [case.source_id],
        "data": {
            "profile": "direct_joint_operation_cash_labor_v1",
            "project_id": project,
            "operator_id": operator,
            "company_initial_commitment_fen": "100",
            "company_revenue_expense_share": "0.5",
            "contract_source": {"source_id": case.source_id},
        },
    }
    funded = {
        "business_id": str(uuid4()),
        "kind": "investment.cooperation_funding",
        "period": "2026-01",
        "evidence_ids": [case.source_id],
        "data": {
            "contract_id": agreement["business_id"],
            "purpose": "initial",
            "amount_fen": "100",
            "funds_account_id": bank,
            "actual_on": "2026-01-03",
        },
    }
    february = project_expense(case, agreement, "2026-02")
    request = case.request(
        agreement,
        funded,
        february,
        objects=[
            {
                "object_id": operator,
                "kind": "person",
                "name": "合成经营者",
                "expected_seq": 0,
                "data": {},
            },
            {
                "object_id": project,
                "kind": "investment_product",
                "name": "合成共同经营",
                "expected_seq": 0,
                "data": {"category": "cooperation"},
            },
            {
                "object_id": bank,
                "kind": "bank_account",
                "name": "合成银行",
                "expected_seq": 0,
                "data": {
                    "bank_name": "合成银行",
                    "account_number": "synthetic-001",
                    "currency": "CNY",
                    "owner": "company",
                },
            },
        ],
    )
    outcome = case.app.accounting_apply(request)
    assert outcome["completion"] == "complete", outcome
    return agreement, february


def project_expense(case, agreement, period):
    value = case.expense("10", period)
    value["effective_on"] = period + "-05"
    value["data"].update(
        expense_class="service", cooperation_id=agreement["business_id"], project_total_fen="20"
    )
    return value


def test_late_expense_preserves_earlier_positions_and_early_correction_recalculates_suffix(
    case, monkeypatch
):
    agreement, february = prepare(case)
    investment = case.app.registry.owner("investment.cooperation_contract")
    extended = case.app.accounting_apply(case.request(case.expense("10", "2026-12")))
    assert extended["completion"] == "complete", extended
    before = results(case, agreement)
    assert set(before) == {"main", *(f"position:2026-{month:02d}" for month in range(1, 13))}
    evaluated, broad_reads = [], []
    evaluate, read_results = investment.evaluate_slot, case.app.ledger.get_results

    def evaluating(business_id, slot, context):
        evaluated.append((business_id, slot))
        return evaluate(business_id, slot, context)

    def reading(business_ids, context):
        broad_reads.extend(business_ids)
        return read_results(business_ids, context)

    monkeypatch.setattr(investment, "evaluate_slot", evaluating)
    december = project_expense(case, agreement, "2026-12")
    with monkeypatch.context() as patches:
        patches.setattr(case.app.ledger, "get_results", reading)
        changed = case.app.accounting_apply(case.request(december))
    assert changed["completion"] == "complete", changed
    assert evaluated == [(agreement["business_id"], "position:2026-12")]
    assert agreement["business_id"] not in broad_reads
    after = results(case, agreement)
    for slot in before.keys() - {"position:2026-12"}:
        assert after[slot] == before[slot]
    ending = after["position:2026-12"]["target"]["values"]
    assert ending["company_funded_fen"] == ending["held_fen"] == "100"
    assert ending["project_expense_fen"] == "40"
    assert ending["company_expense_fen"] == ending["company_trade_payable_fen"] == "20"
    assert ending["company_capital_fen"] == "80"

    evaluated.clear()
    corrected = deepcopy(february)
    corrected["data"].update(amount_fen="20", project_total_fen="40")
    request = case.request(corrected)
    request["mutations"][0].update(op="replace", expected_seq=1)
    replaced = case.app.accounting_apply(request)
    assert replaced["completion"] == "complete", replaced
    assert evaluated == [
        (agreement["business_id"], f"position:2026-{month:02d}") for month in range(2, 13)
    ]
    final = results(case, agreement)
    assert final["main"] == after["main"]
    assert final["position:2026-01"] == after["position:2026-01"]
    ending = final["position:2026-12"]["target"]["values"]
    assert ending["held_fen"] == "100"
    assert ending["project_expense_fen"] == "60"
    assert ending["company_expense_fen"] == ending["company_trade_payable_fen"] == "30"
    assert ending["company_capital_fen"] == "70"
