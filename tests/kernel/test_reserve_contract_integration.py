"""Reserve contracts use real object registration and the shared correction boundary."""

import pytest
from material_fixture import supporting_text

from ai_accounting.kernel.command_schema import command_models
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.discovery import Discovery
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.identity_corrections import IdentityCorrections
from ai_accounting.kernel.maintenance import Maintenance
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.storage import Store


@pytest.fixture
def book(tmp_path):
    engine = Engine(
        Store.create(
            tmp_path / "reserve.sqlite", production_bundle(), "co", "911100000000000001", "db"
        )
    )
    proof = engine.register_evidence(
        b"Synthetic actual receipt and recording correction",
        "text/plain",
        "explicit basis",
        request_id="proof",
    )["digest"]
    supporting_text(engine, proof)
    return engine, proof


def test_public_contracts_retire_capacity_and_keep_future_upgrade_contract(book):
    engine, _ = book
    schemas = engine.store.registry.schemas()
    retired = {
        "managed_reserve_scope",
        "managed_reserve_bank_expense",
        "managed_reserve_obligation_settlement",
        "platform_boundary_disposition",
    }
    assert not retired.intersection(schemas)
    assert {"managed_reserve_expense", "managed_reserve_refund"}.issubset(schemas)
    commands = command_models(engine.store.registry)
    assert "preview_managed_reserve_settlement" not in commands
    assert "confirm_managed_reserve_settlement" not in commands
    for kind in ("managed_reserve_expense", "managed_reserve_refund"):
        fields = schemas[kind]["properties"]
        assert not {"scope_id", "cost_claims", "source_expense_id", "account_type"}.intersection(
            fields
        )
        assert {
            "bank_account_id",
            "cash_account_id",
            "platform_account_id",
            "counterparty_id",
        }.issubset(fields)
        assert "counterparty_id" not in schemas[kind].get("required", ())
    with engine.store.connection(read_only=True) as connection:
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_schema")}
        assert not any(any(name.startswith("fact_" + kind) for kind in retired) for name in names)
        assert {"schema_meta", "schema_history"}.issubset(names)


@pytest.mark.parametrize("channel", ["bank", "cash", "platform"])
def test_accounts_must_be_registered_as_the_exact_funds_type(book, channel):
    engine, proof = book
    wrong_type = "cash" if channel != "cash" else "bank"
    account = Entities(engine).register_entity(
        "fund_account",
        {},
        source="synthetic account",
        account_type=wrong_type,
        request_id="account",
    )["entity_id"]
    data = {
        "period": "2026-01",
        "actual_date": "2026-01-03",
        "amount_fen": 100,
        f"{channel}_account_id": account,
        **({"movement_ids": ["explicit-row"]} if channel == "platform" else {}),
    }
    with pytest.raises(KernelError, match="资金账户类型"):
        engine.save_fact(
            "managed_reserve_refund",
            "receipt",
            data,
            evidence=(proof,),
            expected_revision=0,
            request_id="wrong-account",
        )
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute("SELECT count(*) FROM subject WHERE id='receipt'").fetchone()[0] == 0
        )


def test_cash_identity_correction_reassigns_current_funds_without_erasing_real_receipt(book):
    engine, proof = book
    entities = Entities(engine)
    accounts = [
        entities.register_entity(
            "fund_account",
            {},
            source="synthetic account",
            account_type="cash",
            request_id=f"account-{index}",
        )["entity_id"]
        for index in range(2)
    ]
    data = {
        "period": "2026-01",
        "actual_date": "2026-01-03",
        "amount_fen": 1900,
        "cash_account_id": accounts[0],
    }
    engine.save_fact(
        "managed_reserve_refund",
        "receipt",
        data,
        evidence=(proof,),
        expected_revision=0,
        request_id="save",
    )
    preview = engine.preview(["receipt"])
    engine.confirm(
        ["receipt"],
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="publish",
    )
    corrected = {**data, "cash_account_id": accounts[1]}
    with pytest.raises(KernelError) as ordinary:
        engine.amend_fact(
            "managed_reserve_refund",
            "receipt",
            corrected,
            evidence=(proof,),
            expected_revision=1,
            request_id="ordinary",
            recording_error_confirmed=True,
        )
    assert ordinary.value.code == "identity_correction_required"
    changes = [
        {"subject_id": "receipt", "expected_revision": 1, "action": "reassign", "data": corrected}
    ]
    correction = IdentityCorrections(engine)
    arguments = dict(
        changes=changes,
        evidence=(proof,),
        reason="确认收款记录引用了错误现金账户",
        entity_resolution={"source_entity_id": accounts[0], "target_entity_id": accounts[1]},
    )
    preview = correction.preview_identity_correction(**arguments)
    correction.confirm_identity_correction(
        **arguments,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="correct",
    )
    found = Discovery(engine).find_facts(entity_id=accounts[1], kind="managed_reserve_refund")
    assert found["items"][0]["revision"] == 2
    with engine.store.connection(read_only=True) as connection:
        balances = dict(connection.execute("SELECT balance_key,amount FROM balance"))
        assert balances.get(accounts[0], 0) == 0
        assert balances[accounts[1]] == 1900
        assert (
            connection.execute(
                "SELECT count(*) FROM fact_revision WHERE subject_id='receipt'"
            ).fetchone()[0]
            == 2
        )
    assert Maintenance(engine).verify_integrity()["status"] == "verified"


@pytest.mark.parametrize("kind", ["managed_reserve_expense", "managed_reserve_refund"])
def test_identity_correction_cannot_erase_actual_reserve_money(book, kind):
    engine, proof = book
    entities = Entities(engine)
    for index in range(2):
        account = entities.register_entity(
            "fund_account",
            {},
            source="synthetic account",
            account_type="cash",
            request_id=f"account-{index}",
        )["entity_id"]
        engine.save_fact(
            kind,
            f"money-{index}",
            {
                "period": "2026-01",
                "actual_date": f"2026-01-0{index + 1}",
                "amount_fen": 100 + index,
                "cash_account_id": account,
            },
            evidence=(proof,),
            expected_revision=0,
            request_id=f"save-{index}",
        )
    with engine.store.connection(read_only=True) as connection:
        before_count = connection.execute("SELECT count(*) FROM fact_revision").fetchone()[0]
    with pytest.raises(KernelError) as rejected:
        IdentityCorrections(engine).preview_identity_correction(
            changes=[
                {
                    "subject_id": "money-0",
                    "expected_revision": 1,
                    "action": "supersede",
                    "replacement_subject_id": "money-1",
                }
            ],
            evidence=(proof,),
            reason="纠错不能让真实收付款消失",
        )
    assert rejected.value.code == "identity_actual_funds"
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM identity_correction").fetchone()[0] == 0
        assert (
            connection.execute("SELECT count(*) FROM fact_revision").fetchone()[0] == before_count
        )
