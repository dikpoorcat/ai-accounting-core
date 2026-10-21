"""A fresh ledger keeps final facts and real horizon, not the old correction process."""

from copy import deepcopy
from uuid import uuid4

from ai_accounting_modular.host.public import create_offline_application
from ai_accounting_modular.ledger.public import LedgerQuery
from ai_accounting_modular.records.public import open_standard_bundle
from tests.modular.ledger._financial_snapshot_review_support import (
    BANK,
    PARTY_A,
    SOURCE,
    expense,
    fact,
    opening,
    payment,
)

from ._working_case import case as case
from .conftest import source
from .test_isolated_targets import read_target
from .test_isolated_targets import request as isolated_request
from .test_rebuild_standard import new_target, rebuild, unit


def test_closed_correction_becomes_one_final_fact_and_one_original_voucher(case, tmp_path):
    app = case.app
    fact = case.seed()
    original_business = fact["business_id"]
    for expected, amount, correction in ((1, "120000", None), (2, "150000", "2026-03")):
        if correction:
            assert case.close(case.check())["state"] == "closed"
        evidence = source(f"重新核实：一月维修实际费用为{amount}分。")
        final = deepcopy(fact)
        final["data"]["amount_fen"] = amount
        final["evidence_ids"] = [evidence["source_id"]]
        result = app.accounting_apply(
            {
                "contract": "ai-accounting-modular/1",
                "company_id": case.company,
                "command_id": str(uuid4()),
                "sources": [evidence],
                "mutations": [{"op": "replace", "expected_seq": expected, "fact": final}],
                **({"correction_period": correction} if correction else {}),
            }
        )
        assert result["completion"] == "complete", result
        fact = final
    with case.kernel.storage.read_unit(case.company) as read:
        old_results = app.ledger.get_results([original_business], read)
        old_vouchers = app.ledger.query_vouchers(LedgerQuery((original_business,)), read).items
        assert len(old_vouchers) == 3
        assert {v["role"] for v in old_vouchers} == {"original", "reversal", "replacement"}
        root = case.kernel.storage.company_metadata(read).company_root
    bundle = open_standard_bundle(root / "standard/current.json")
    assert bundle.working_period_to == "2026-03"
    assert len([item for item in bundle.facts if item["business_id"] == original_business]) == 1
    target = tmp_path / "simplified-target"
    company, _ = new_target(bundle, target)
    # Only the target's explicit unit is available to this call.
    app.storage = None
    assert app.repositories
    outcome = rebuild(app, bundle, target)
    assert all(head["seq"] == 1 for head in outcome.heads)
    with unit(target, owners=app.repositories) as read:
        assert app.ledger.closed_through(read) is None
        assert read.repository("application").working_period_to() == "2026-03"
        work = app.work_scope(read)
        assert work.has_formal_results and work.working_period_to == "2026-03"
        assert work.closed_through is None
        results = app.ledger.get_results([original_business], read)
        vouchers = app.ledger.query_vouchers(LedgerQuery((original_business,)), read).items
        assert len(vouchers) == 1
        assert vouchers[0]["role"] == "original"
        assert vouchers[0]["period"] == "2026-01"
        assert results[0]["reference"]["result_id"] != old_results[0]["reference"]["result_id"]
        assert results[0]["reference"]["generation"] == 1
        report = app.ledger.financial_snapshot(
            {
                "company_id": company,
                "period_from": "2026-01",
                "period_to": "2026-01",
                "view": "current",
            },
            read,
        )
        assert report["pre_tax"]["profit_before_tax"]["amount_fen"] == "-150000"
        assert report["balance_sheet"]["33"]["ending"]["amount_fen"] == "150000"


def test_closed_paid_expense_with_later_real_recovery_rebuilds_only_final_facts(
    case, tmp_path, monkeypatch
):
    cost = expense("rebuild-paid-cost", "100")
    paid = payment("rebuild-paid-money", cost, "100")
    evidence = source(
        "合成资料：期初银行200分，1月费用和实付100分；2月核实最终80分，追收20分并实际退款。"
    )
    evidence["source_id"] = SOURCE
    initial = case.app.accounting_apply(
        case.request(
            opening(cash="200"),
            cost,
            paid,
            sources=[evidence],
            objects=[
                {
                    "object_id": object_id,
                    "kind": kind,
                    "name": "合成对象",
                    "expected_seq": 0,
                    "data": data,
                }
                for object_id, kind, data in (
                    (PARTY_A, "organization", {}),
                    (
                        BANK,
                        "bank_account",
                        {
                            "bank_name": "合成银行",
                            "account_number": "rebuild-paid",
                            "currency": "CNY",
                            "owner": "company",
                        },
                    ),
                )
            ],
        )
    )
    assert initial["completion"] == "complete", initial
    with case.kernel.storage.read_unit(case.company) as read:
        movement = case.kernel.ledger.fund_movements(BANK, "2026-01", "2026-01", read)[0]
    row_id = str(uuid4())
    statement = fact(
        "rebuild-paid-statement",
        "money.bank_statement",
        {
            "funds_account_id": BANK,
            "opening_fen": "200",
            "closing_fen": "100",
            "rows": [
                {
                    "row_id": row_id,
                    "source": {"source_id": SOURCE},
                    "actual_on": "2026-01-21",
                    "direction": "outflow",
                    "amount_fen": "100",
                }
            ],
        },
    )
    reconciliation = fact(
        "rebuild-paid-reconciliation",
        "money.reconciliation",
        {
            "statement_id": statement["business_id"],
            "matches": [
                {
                    "statement_row_ids": [row_id],
                    "fund_movement_ids": [movement.value["movement_id"]],
                }
            ],
        },
    )
    assert (
        case.app.accounting_apply(case.request(statement, reconciliation))["completion"]
        == "complete"
    )
    assert case.close(case.check())["state"] == "closed"
    final = expense("rebuild-paid-cost", "80")
    claim = fact(
        "rebuild-paid-claim",
        "operations.overpayment",
        {
            "source": {"business_id": cost["business_id"], "slot": "primary"},
            "counterparty_id": PARTY_A,
            "amount_fen": "20",
        },
        "2026-02",
    )
    correction = case.request(claim, correction_period="2026-02")
    correction["mutations"].append({"op": "replace", "expected_seq": 1, "fact": final})
    assert case.app.accounting_apply(correction)["completion"] == "complete"
    received = payment(
        "rebuild-paid-refund", claim, "20", on="2026-02-20", slot="recovery", incoming=True
    )
    assert case.app.accounting_apply(case.request(received))["completion"] == "complete"
    with case.kernel.storage.read_unit(case.company, view="as_posted", as_of="2026-01") as read:
        old = case.kernel.ledger.get_results([cost["business_id"]], read)[0]
        assert old["target"]["values"]["amount_fen"] == "100"
    with case.kernel.storage.read_unit(case.company) as read:
        metadata = case.kernel.storage.company_metadata(read)
        assert case.kernel.ledger.query_vouchers(LedgerQuery(), read).total == 6
    bundle = open_standard_bundle(metadata.company_root / "standard/current.json")
    expected = {item["business_id"]: item for item in bundle.facts}
    assert expected[cost["business_id"]] == final
    assert expected[paid["business_id"]] == paid
    assert expected[claim["business_id"]] == claim
    assert expected[received["business_id"]] == received
    assert bundle.working_period_to == "2026-02"
    offline = create_offline_application()
    # The source is stopped; both operations must use only the standard set.
    case.kernel.close()

    def stopped_source_read(*args, **kwargs):
        raise AssertionError("读取了旧公司数据库")

    monkeypatch.setattr(case.kernel.storage, "read_unit", stopped_source_read)
    target = tmp_path / "final-paid-ledger"
    checked = offline.accounting_isolated(
        isolated_request("rebuild", "check", bundle.manifest_path, target)
    )
    assert checked["state"] == "checked", checked
    assert not target.exists()
    created = offline.accounting_isolated(
        isolated_request("rebuild", "run", bundle.manifest_path, target)
    )
    assert created["state"] == "created", created
    with read_target(offline, target) as read:
        assert offline.ledger.closed_through(read) is None
        all_vouchers = offline.ledger.query_vouchers(LedgerQuery(), read)
        assert all_vouchers.total == 4
        assert {item["role"] for item in all_vouchers.items} == {"original"}
        current = offline.get_businesses(
            {"company_id": read.company_id, "business_ids": list(expected), "include": ["facts"]},
            read,
        )
        assert {
            item["fact"]["business_id"]: item["fact"] for item in current["businesses"]
        } == expected
        vouchers = offline.ledger.query_vouchers(LedgerQuery((cost["business_id"],)), read).items
        assert len(vouchers) == 1 and vouchers[0]["role"] == "original"
        assert vouchers[0]["period"] == "2026-01"
        assert [(line["debit_fen"], line["credit_fen"]) for line in vouchers[0]["lines"]] == [
            ("80", "0"),
            ("0", "80"),
        ]
        movements = offline.ledger.fund_movements(BANK, "2026-01", "2026-02", read)
        assert sorted(
            (item.value["direction"], item.value["amount_fen"]) for item in movements
        ) == [("inflow", "20"), ("outflow", "100")]
        balances = offline.ledger.resolve(
            [
                {"business_id": cost["business_id"], "slot": "primary"},
                {"business_id": claim["business_id"], "slot": "recovery"},
            ],
            "obligation",
            read,
        )
        assert [item["balance"]["remaining_fen"] for item in balances] == ["0", "0"]
