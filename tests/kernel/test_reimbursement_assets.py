"""Personal funding is adopted by the company without inventing a cash movement."""

import itertools
import json

import pytest
from pydantic import ValidationError

from ai_accounting.kernel.contracts import Context, KernelError
from ai_accounting.kernel.domains.assets import ReimbursedAsset, ReimbursedAssetBatch
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.service import default_registry
from ai_accounting.kernel.storage import Store


@pytest.fixture
def book(tmp_path):
    engine = Engine(
        Store.create(tmp_path / "book.sqlite", default_registry(), "co", "911100000000000001", "db")
    )
    proof = engine.register_evidence(
        b"accepted expense and asset originals", "text/plain", "proof", request_id="proof"
    )["digest"]
    counter = itertools.count()

    def save(kind, subject, data, revision=0):
        return engine.save_fact(
            kind,
            subject,
            data,
            evidence=(proof,),
            expected_revision=revision,
            request_id=f"save-{next(counter)}",
        )

    def publish(*subjects):
        preview = engine.preview(list(subjects))
        return engine.confirm(
            list(subjects),
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id=f"publish-{next(counter)}",
        )

    return engine, save, publish


def asset(**changes):
    return {
        "period": "2026-02",
        "asset_type": "fixed",
        "cost_fen": 120000,
        "company_acceptance_confirmed": True,
        "creditors": [
            {"employee_id": "alice", "amount_fen": 80000},
            {"employee_id": "bob", "amount_fen": 40000},
        ],
        **changes,
    }


def activation(**changes):
    return {
        "period": "2026-02",
        "asset_id": "computer",
        "in_use_date": "2026-02-28",
        "useful_life_months": 12,
        "residual_fen": 0,
        "benefit_area": "administration",
        "rounding_policy": "floor_final_remainder",
        **changes,
    }


def pay(kind, source, name, recipient, amount, **changes):
    return {
        "period": "2026-03",
        "actual_date": "2026-03-03",
        "direction": "outflow",
        "bank_account_id": "bank",
        "counterparty_id": recipient,
        "amount_fen": amount,
        "allocations": [
            {"source_kind": kind, "source_id": source, "obligation": name, "amount_fen": amount}
        ],
        **changes,
    }


def result(engine, subject):
    with engine.store.connection(read_only=True) as connection:
        return json.loads(
            connection.execute(
                "SELECT c.outcome FROM calculation c JOIN calculation_current a "
                "ON a.calculation_id=c.id WHERE a.subject_id=?",
                (subject,),
            ).fetchone()[0]
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"cost_fen": 119999},
        {
            "creditors": [
                {"employee_id": "alice", "amount_fen": 80000},
                {"employee_id": "alice", "amount_fen": 40000},
            ]
        },
        {"company_acceptance_confirmed": False},
        {"acquisition_date": "2026-01-31"},
        {"cost_fen": 120000.0},
    ],
)
def test_claim_requires_exact_accepted_cost_and_company_month(changes):
    with pytest.raises(ValidationError):
        ReimbursedAsset.model_validate_json(json.dumps(asset(**changes)))


def test_month_precision_reuses_acceptance_without_inventing_personal_payment_day(book):
    engine, save, publish = book
    save("reimbursed_asset", "computer", asset())
    save("asset_activation", "activation", activation())
    publish("computer", "activation")
    adopted = result(engine, "computer")
    assert adopted["values"]["cost_fen"] == 120000
    assert {row["account"] for row in adopted["lines"]} == {"1604", "224101"}
    assert [
        (row["counterparty_id"], row["amount_fen"]) for row in adopted["values"]["obligations"]
    ] == [("alice", 80000), ("bob", 40000)]
    assert engine.overview("2026-02")["cashflow"] == []
    save("asset_consumption", "march", {"period": "2026-03", "asset_id": "computer"})
    save(
        "payment",
        "alice-reimbursement",
        pay("reimbursed_asset", "computer", "alice", "alice", 80000),
    )
    publish("march", "alice-reimbursement")
    assert result(engine, "march")["values"]["consumption_fen"] == 10000
    assert engine.overview("2026-03")["cashflow"] == [
        {"category": "asset_acquisition", "amount": -80000}
    ]
    save("payment", "wrong-person", pay("reimbursed_asset", "computer", "bob", "alice", 40000))
    with pytest.raises(KernelError):
        publish("wrong-person")
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT amount FROM balance WHERE balance_key='reimbursed_asset:computer:bob'"
            ).fetchone()[0]
            == 40000
        )


def test_known_company_day_prevents_activation_before_acquisition(book):
    _, save, publish = book
    save("reimbursed_asset", "computer", asset(acquisition_date="2026-02-28"))
    save("asset_activation", "activation", activation(in_use_date="2026-02-27"))
    with pytest.raises(KernelError, match="启用日期不能早于取得日期"):
        publish("computer", "activation")


def test_bank_promotion_reward_preserves_actual_receipt_and_requires_entitlement(book):
    engine, save, publish = book
    data = {
        "period": "2026-03",
        "actual_date": "2026-03-03",
        "bank_account_id": "bank",
        "amount_fen": 104,
        "income_kind": "bank_promotion_reward",
        "counterparty_id": "bank-provider",
    }
    save("bank_income", "reward-unknown", data)
    with pytest.raises(KernelError, match="无返还义务"):
        publish("reward-unknown")
    save("bank_income", "reward-confirmed", {**data, "entitlement_confirmed": True})
    publish("reward-confirmed")
    receipt = result(engine, "reward-confirmed")
    assert receipt["values"]["income_kind"] == "bank_promotion_reward"
    assert receipt["values"]["actual_date"] == "2026-03-03"
    assert [(line["account"], line["credit"]) for line in receipt["lines"]] == [
        ("1002", 0),
        ("6301", 104),
    ]


def accepted_batch(**changes):
    return {
        "period": "2026-02",
        "cost_fen": 150000,
        "company_acceptance_confirmed": True,
        "assets": [
            {"asset_id": "computer", "asset_type": "fixed", "cost_fen": 120000},
            {"asset_id": "chair", "asset_type": "fixed", "cost_fen": 30000},
        ],
        "creditors": [
            {"employee_id": "alice", "amount_fen": 90000},
            {"employee_id": "bob", "amount_fen": 60000},
        ],
        **changes,
    }


def batch_card(cost=120000, **changes):
    return asset(cost_fen=cost, creditors=[], acceptance_id="batch", **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"cost_fen": 150001},
        {"creditors": [{"employee_id": "alice", "amount_fen": 150001}]},
        {
            "creditors": [
                {"employee_id": "alice", "amount_fen": 90000},
                {"employee_id": "alice", "amount_fen": 60000},
            ]
        },
        {
            "assets": [
                {"asset_id": "computer", "asset_type": "fixed", "cost_fen": 120000},
                {"asset_id": "computer", "asset_type": "fixed", "cost_fen": 30000},
            ]
        },
        {"acquisition_date": "2026-03-01"},
    ],
)
def test_batch_requires_known_balanced_totals_without_person_asset_matrix(changes):
    with pytest.raises(ValidationError):
        ReimbursedAssetBatch.model_validate_json(json.dumps(accepted_batch(**changes)))


def test_batch_cards_do_not_duplicate_assets_creditors_or_actual_cash(book):
    engine, save, publish = book
    save("reimbursed_asset_batch", "batch", accepted_batch())
    save("reimbursed_asset", "computer", batch_card())
    save("reimbursed_asset", "chair", batch_card(30000))
    save("asset_activation", "activation", activation())
    save("asset_activation", "chair-use", activation(asset_id="chair"))
    publish("batch", "computer", "chair", "activation", "chair-use")
    assert result(engine, "computer")["lines"] == []
    assert result(engine, "chair")["balances"] == []
    assert result(engine, "computer")["values"]["obligations"] == []
    with engine.store.connection(read_only=True) as connection:
        balances = dict(connection.execute("SELECT balance_key, amount FROM balance"))
    assert balances["asset:computer:carrying"] == 120000
    assert balances["asset:chair:carrying"] == 30000
    assert balances["reimbursed_asset_batch:batch:alice"] == 90000
    assert balances["reimbursed_asset_batch:batch:bob"] == 60000
    assert engine.overview("2026-02")["cashflow"] == []
    save("payment", "alice-paid", pay("reimbursed_asset_batch", "batch", "alice", "alice", 90000))
    save("asset_consumption", "computer-march", {"period": "2026-03", "asset_id": "computer"})
    save("asset_consumption", "chair-march", {"period": "2026-03", "asset_id": "chair"})
    publish("alice-paid", "computer-march", "chair-march")
    assert engine.overview("2026-03")["cashflow"] == [
        {"category": "asset_acquisition", "amount": -90000}
    ]
    assert result(engine, "computer-march")["values"]["consumption_fen"] == 10000
    assert result(engine, "chair-march")["values"]["consumption_fen"] == 2500
    before = engine.overview("2026-03")
    engine.rebuild_projections(request_id="rebuild-batch")
    assert engine.overview("2026-03") == before


def test_batch_rejects_card_cost_mismatch_and_duplicate_adoption(book):
    _, save, publish = book
    save("reimbursed_asset_batch", "batch", accepted_batch())
    save("reimbursed_asset", "computer", batch_card(119999))
    with pytest.raises(KernelError, match="成本及取得期间"):
        publish("batch", "computer")
    save("reimbursed_asset", "computer", batch_card(), revision=1)
    publish("batch", "computer")
    save("reimbursed_asset_batch", "duplicate", accepted_batch())
    with pytest.raises(KernelError, match="重复验收"):
        publish("duplicate")


def test_batch_missing_card_is_a_readiness_issue(book):
    from ai_accounting.kernel.domains.assets import required_reads, required_work
    from ai_accounting.kernel.types import YearMonth

    engine, save, publish = book
    save("reimbursed_asset_batch", "batch", accepted_batch())
    save("reimbursed_asset", "computer", batch_card())
    publish("batch", "computer")
    with engine.store.connection(read_only=True) as connection:
        context = Context(
            {
                read: engine.store.select(connection, read)
                for read in required_reads(YearMonth("2026-02"))
            }
        )
    issues = required_work(YearMonth("2026-02"), context)
    assert {item["asset_id"] for item in issues if item["field"] == "reimbursed_asset"} == {"chair"}


def test_opening_package_cannot_adopt_asset_already_in_accepted_batch(book):
    from ai_accounting.kernel.domains.opening import CATEGORIES

    engine, save, publish = book
    save("reimbursed_asset_batch", "batch", accepted_batch())
    publish("batch")
    save(
        "opening_asset",
        "computer",
        {
            "period": "2026-03",
            "package_id": "opening",
            "asset_type": "fixed",
            "cost_fen": 120000,
            "accumulated_fen": 0,
            "in_use_date": "2026-02-28",
            "useful_life_months": 12,
            "completed_months": 0,
            "residual_fen": 0,
            "benefit_area": "administration",
            "rounding_policy": "floor_final_remainder",
        },
    )
    save(
        "opening_equity",
        "equity",
        {
            "period": "2026-03",
            "package_id": "opening",
            "equity_kind": "retained_earnings",
            "balance_fen": 120000,
            "holder_or_basis_id": "source",
        },
    )
    counts = dict.fromkeys(CATEGORIES.values(), 0) | {"assets": 1, "equity": 1}
    save(
        "opening_package",
        "opening",
        {
            "period": "2026-03",
            "package_id": "opening",
            "counts": counts,
            "members": [
                {"kind": "opening_asset", "subject_id": "computer"},
                {"kind": "opening_equity", "subject_id": "equity"},
            ],
            "completeness_confirmed": True,
        },
    )
    with pytest.raises(KernelError) as error:
        publish("opening", "computer", "equity")
    assert error.value.code == "duplicate_asset_acceptance"
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT amount FROM balance WHERE balance_key='asset:computer:carrying'"
            ).fetchone()[0]
            == 120000
        )


def test_reimbursed_asset_uses_existing_disposal_and_projection_rebuild(book):
    engine, save, publish = book
    save("reimbursed_asset", "computer", asset())
    save("asset_activation", "activation", activation())
    save("asset_consumption", "march", {"period": "2026-03", "asset_id": "computer"})
    save(
        "asset_disposal",
        "scrap",
        {
            "period": "2026-03",
            "asset_id": "computer",
            "disposal_date": "2026-03-31",
            "disposal_kind": "scrap",
            "gross_proceeds_fen": 0,
        },
    )
    publish("computer", "activation", "march", "scrap")
    assert result(engine, "scrap")["values"]["carrying_fen"] == 0
    assert any(
        row["account"] == "571101" and row["debit"] == 110000
        for row in result(engine, "scrap")["lines"]
    )
    before = engine.overview("2026-03")
    engine.rebuild_projections(request_id="rebuild")
    after = engine.overview("2026-03")
    assert before["accounts"] == after["accounts"]
    assert before["cashflow"] == after["cashflow"]


def test_deposit_acceptance_separates_landlord_right_employee_debt_and_real_funds(book):
    engine, save, publish = book
    save(
        "reimbursed_deposit",
        "deposit",
        {
            "period": "2026-02",
            "counterparty_id": "landlord",
            "employee_id": "alice",
            "amount_fen": 30000,
            "company_acceptance_confirmed": True,
            "refund_right_confirmed": True,
        },
    )
    publish("deposit")
    assert engine.overview("2026-02")["cashflow"] == []
    save(
        "payment",
        "reimburse",
        pay("reimbursed_deposit", "deposit", "reimbursement", "alice", 30000),
    )
    save(
        "payment",
        "returned",
        pay(
            "reimbursed_deposit",
            "deposit",
            "refund",
            "landlord",
            30000,
            direction="inflow",
            actual_date="2026-03-08",
        ),
    )
    publish("reimburse", "returned")
    with engine.store.connection(read_only=True) as connection:
        rows = connection.execute(
            "SELECT amount FROM balance WHERE balance_key LIKE 'reimbursed_deposit:deposit:%'"
        ).fetchall()
    assert all(row[0] == 0 for row in rows)
