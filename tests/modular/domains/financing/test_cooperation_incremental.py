"""Positions reuse adopted months while checking true historical open resources."""

from ai_accounting_modular.domains.financing.public import create_investment

from ._cooperation_support import (
    PROJECT,
    agency_payment,
    collection,
    command,
    contract,
    expense,
    funding,
    operator,
    policy,
    position,
    sale,
    submitted,
)
from ._support import COUNTERPARTY, OWNER, SOURCE, assemble, change, fact, get, payment


class _ObservedInvestment:
    def __init__(self):
        self.owner = create_investment()
        self.calls = []
        self.reads = []
        self.member_reads = []

    def __getattr__(self, name):
        return getattr(self.owner, name)

    def evaluate_slot(self, business_id, slot, context):
        self.calls.append((business_id, slot))
        reads = self.reads
        member_reads = self.member_reads

        class ObservedContext:
            def __getattr__(self, name):
                return getattr(context, name)

            def get_result(self, producer, result_slot):
                reads.append((slot, producer, result_slot))
                return context.get_result(producer, result_slot)

            def related_facts(self, producer, relation_kind, **kwargs):
                member_reads.append((slot, kwargs.get("period_from"), kwargs.get("period_to")))
                return context.related_facts(producer, relation_kind, **kwargs)

        return self.owner.evaluate_slot(business_id, slot, ObservedContext())

    def reset(self):
        self.calls.clear()
        self.reads.clear()
        self.member_reads.clear()


def _results(app, company, agreement):
    return {
        result["target"]["result_slot"]: result
        for result in get(app, company, agreement["business_id"])["businesses"][0]["results"]
    }


def test_contract_main_evaluates_without_reading_project_members_or_work_months():
    agreement = contract()

    class ContractContext:
        def get_fact(self, business_id):
            assert business_id == agreement["business_id"]
            return agreement

        def get_object(self, object_id):
            return {
                PROJECT: {"kind": "investment_product", "data": {"category": "cooperation"}},
                OWNER: {"kind": "person", "data": {}},
            }[object_id]

        def resolve_sources(self, ids):
            assert ids == [SOURCE]
            return [{"source_id": SOURCE}]

        def related_facts(self, *args, **kwargs):
            raise AssertionError("Contract main must not read project membership")

        @property
        def evaluation_period_to(self):
            raise AssertionError("Contract main must not read the work horizon")

    result = create_investment().evaluate_slot(agreement["business_id"], "main", ContractContext())
    assert result["target"]["kind"] == "investment.cooperation_contract"
    assert result["target"]["values"]["company_initial_commitment_fen"] == "100"
    assert result["lines"] == []


def test_december_addition_retains_january_to_november_and_does_not_replay_closed_sources(tmp_path):
    owner = _ObservedInvestment()
    app, company = assemble(tmp_path, investment=owner)
    agreement = contract()
    supplied = funding(agreement)
    charged = expense(agreement)
    paid = agency_payment(agreement, charged)
    december_capital = operator(agreement, 1, into=True, day="2026-12-01")
    actual = app.accounting_apply(
        submitted(company, december_capital, paid, charged, supplied, agreement)
    )
    assert actual["completion"] == "complete", actual
    before = _results(app, company, agreement)
    assert len(before) == 13
    assert not any(
        item["type"] == "cooperation_trade"
        for item in before["position:2026-11"]["basis"]["adopted_quantities"]
    )
    owner.reset()
    late = expense(agreement, day="2026-12-05")
    late["period"] = "2026-12"
    actual = app.accounting_apply(command(company, late))
    assert actual["completion"] == "complete", actual
    assert owner.calls == [(agreement["business_id"], "position:2026-12")]
    after = _results(app, company, agreement)
    for slot in before.keys() - {"position:2026-12"}:
        assert after[slot]["reference"] == before[slot]["reference"]
    december = position(app, company, agreement, "2026-12")
    assert december["project_expense_fen"] == "40"
    assert december["company_expense_fen"] == "20"
    assert december["project_paid_fen"] == "20"
    assert december["held_fen"] == "81"
    assert december["operator_due_fen"] == "9"
    assert december["company_capital_fen"] == "80"
    assert december["company_trade_payable_fen"] == "10"
    assert not {supplied["business_id"], charged["business_id"], paid["business_id"]}.intersection(
        producer for _, producer, _ in owner.reads
    )
    assert owner.member_reads == [("position:2026-12", "2026-12", "2026-12")]


def test_old_unpaid_resource_cleared_outside_in_december_is_counted_in_that_month_once(tmp_path):
    owner = _ObservedInvestment()
    app, company = assemble(tmp_path, investment=owner)
    agreement = contract()
    charged = expense(agreement)
    horizon = operator(agreement, 1, into=True, day="2026-12-01")
    actual = app.accounting_apply(
        submitted(company, agreement, funding(agreement), charged, horizon)
    )
    assert actual["completion"] == "complete", actual
    before = _results(app, company, agreement)
    owner.reset()
    direct = payment(charged, 10, slot="primary", party=COUNTERPARTY, day="2026-12-05")
    actual = app.accounting_apply(command(company, direct))
    assert actual["completion"] == "complete", actual
    assert owner.calls == [(agreement["business_id"], "position:2026-12")]
    after = _results(app, company, agreement)
    for slot in before.keys() - {"position:2026-12"}:
        assert after[slot]["reference"] == before[slot]["reference"]
    november = position(app, company, agreement, "2026-11")
    december = position(app, company, agreement, "2026-12")
    assert november["company_trade_payable_fen"] == "10"
    assert november["company_outside_settled_out_fen"] == "0"
    assert december["company_trade_payable_fen"] == "0"
    assert december["company_outside_settled_out_fen"] == "10"
    assert december["project_paid_fen"] == december["company_paid_fen"] == "10"
    assert december["held_fen"] == "100"
    assert december["company_capital_fen"] == "100"
    assert not {charged["business_id"], direct["business_id"]}.intersection(
        producer for _, producer, _ in owner.reads
    )


def test_early_correction_recalculates_later_positions_and_keeps_actual_cash_fixed(tmp_path):
    owner = _ObservedInvestment()
    app, company = assemble(tmp_path, investment=owner)
    agreement = contract()
    charged = expense(agreement)
    paid = agency_payment(agreement, charged)
    horizon = operator(agreement, 1, into=True, day="2026-12-01")
    actual = app.accounting_apply(
        submitted(company, agreement, funding(agreement), charged, paid, horizon)
    )
    assert actual["completion"] == "complete", actual
    before = _results(app, company, agreement)
    owner.reset()
    charged["data"].update(project_total_fen="16", amount_fen="8")
    claim = fact(
        "operations.overpayment",
        {
            "source": {"business_id": charged["business_id"], "slot": "primary"},
            "amount_fen": "2",
            "project_total_fen": "4",
        },
        period="2026-12",
        actual="2026-12-07",
    )
    returned = fact(
        "investment.cooperation_collection",
        {
            "contract_id": agreement["business_id"],
            "source": {"business_id": claim["business_id"], "slot": "recovery"},
            "project_amount_fen": "4",
        },
        period="2026-12",
        actual="2026-12-08",
    )
    request = change(company, charged)
    request["mutations"].extend(command(company, returned, claim)["mutations"])
    actual = app.accounting_apply(request)
    assert actual["completion"] == "complete", actual
    assert [slot for business, slot in owner.calls if business == agreement["business_id"]] == [
        f"position:2026-{month:02d}" for month in range(1, 13)
    ]
    after = _results(app, company, agreement)
    assert after["main"]["reference"] == before["main"]["reference"]
    january = position(app, company, agreement)
    december = position(app, company, agreement, "2026-12")
    assert january["project_expense_fen"] == december["project_expense_fen"] == "16"
    assert january["company_expense_fen"] == december["company_expense_fen"] == "8"
    assert january["project_paid_fen"] == "20"
    assert january["company_trade_receivable_net_fen"] == "2"
    assert december["project_paid_fen"] == "16"
    assert december["held_fen"] == "85"
    assert december["operator_due_fen"] == "7"
    assert december["company_capital_fen"] == "92"
    assert december["company_trade_receivable_net_fen"] == "0"


def test_moving_february_source_to_december_removes_old_month_effect_without_dirtying_january(
    tmp_path,
):
    owner = _ObservedInvestment()
    app, company = assemble(tmp_path, investment=owner)
    agreement = contract()
    charged = expense(agreement, day="2026-02-05")
    charged["period"] = "2026-02"
    horizon = operator(agreement, 1, into=True, day="2026-12-01")
    actual = app.accounting_apply(
        submitted(company, agreement, funding(agreement), charged, horizon)
    )
    assert actual["completion"] == "complete", actual
    before = _results(app, company, agreement)
    owner.reset()
    charged.update(period="2026-12", effective_on="2026-12-05")
    actual = app.accounting_apply(change(company, charged))
    assert actual["completion"] == "complete", actual
    assert [slot for business, slot in owner.calls if business == agreement["business_id"]] == [
        f"position:2026-{month:02d}" for month in range(2, 13)
    ]
    after = _results(app, company, agreement)
    for slot in ("main", "position:2026-01"):
        assert after[slot]["reference"] == before[slot]["reference"]
    november = position(app, company, agreement, "2026-11")
    december = position(app, company, agreement, "2026-12")
    assert november["project_expense_fen"] == november["company_trade_payable_fen"] == "0"
    assert december["project_expense_fen"] == "20"
    assert december["company_trade_payable_fen"] == "10"


def test_zero_company_unsettled_status_carries_then_actual_project_cash_settles_it(tmp_path):
    owner = _ObservedInvestment()
    app, company = assemble(tmp_path, investment=owner, taxes=True)
    agreement, rule = contract(), policy()
    agreement["data"]["company_revenue_expense_share"] = "0.1"
    sold = sale(agreement, rule)
    sold["data"].update(gross_fen="0", project_total_fen="1")
    charged = expense(agreement, project="1", company="0")
    ended = fact(
        "investment.cooperation_exit",
        {"contract_id": agreement["business_id"]},
        actual="2026-01-10",
    )
    unrelated = fact(
        "operations.expense",
        {
            "creditor_id": COUNTERPARTY,
            "creditor_kind": "supplier",
            "amount_fen": "1",
            "expense_class": "service",
        },
        period="2026-12",
        actual="2026-12-05",
    )
    actual = app.accounting_apply(
        submitted(company, agreement, rule, sold, charged, ended, unrelated)
    )
    assert actual.get("completion") == "complete", actual
    before = _results(app, company, agreement)
    for month in range(1, 13):
        assert (
            position(app, company, agreement, f"2026-{month:02d}")["status"] == "operations_ended"
        )
    owner.reset()
    received = collection(agreement, sold, day="2026-09-05")
    received["data"]["project_amount_fen"] = "1"
    paid = agency_payment(agreement, charged, project="1", day="2026-09-06")
    paid["period"] = "2026-09"
    actual = app.accounting_apply(command(company, received, paid))
    assert actual["completion"] == "complete", actual
    assert [slot for business, slot in owner.calls if business == agreement["business_id"]] == [
        f"position:2026-{month:02d}" for month in range(9, 13)
    ]
    after = _results(app, company, agreement)
    for month in range(1, 9):
        slot = f"position:2026-{month:02d}"
        assert after[slot]["reference"] == before[slot]["reference"]
    for month in range(9, 13):
        current = position(app, company, agreement, f"2026-{month:02d}")
        assert current["status"] == "settled"
        assert current["project_collected_fen"] == current["project_paid_fen"] == "1"
        assert current["pool_fen"] == current["held_fen"] == "0"
    assert not any(
        slot > "position:2026-09" and producer in {sold["business_id"], charged["business_id"]}
        for slot, producer, _ in owner.reads
    )


def test_replacing_withdrawing_and_restoring_contract_replaces_complete_position_directory(
    tmp_path,
):
    app, company = assemble(tmp_path)
    agreement = contract()
    actual = app.accounting_apply(submitted(company, agreement))
    assert actual["completion"] == "complete", actual
    assert set(_results(app, company, agreement)) == {"main", "position:2026-01"}
    agreement["period"] = "2026-02"
    actual = app.accounting_apply(change(company, agreement))
    assert actual["completion"] == "complete", actual
    assert set(_results(app, company, agreement)) == {"main", "position:2026-02"}
    request = command(company)
    request["mutations"] = [
        {"op": "withdraw", "expected_seq": 2, "business_id": agreement["business_id"]}
    ]
    actual = app.accounting_apply(request)
    assert actual["completion"] == "complete", actual
    assert _results(app, company, agreement) == {}
    request = command(company, agreement)
    request["mutations"][0].update(op="restore", expected_seq=3)
    actual = app.accounting_apply(request)
    assert actual["completion"] == "complete", actual
    assert set(_results(app, company, agreement)) == {"main", "position:2026-02"}
