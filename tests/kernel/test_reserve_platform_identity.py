"""Platform reserve money keeps one original row through identity correction."""

from material_fixture import supporting_text

from ai_accounting.kernel.contracts import Read
from ai_accounting.kernel.domains.platforms import movement_scope
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.identity_corrections import IdentityCorrections
from ai_accounting.kernel.maintenance import Maintenance
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.storage import Store


def test_platform_account_correction_moves_reserve_money_and_keeps_one_consumer(tmp_path):
    engine = Engine(
        Store.create(
            tmp_path / "reserve-platform.sqlite",
            production_bundle(),
            "co",
            "911100000000000001",
            "db",
        )
    )
    proof = engine.register_evidence(
        b"Synthetic platform row and account identity correction",
        "text/plain",
        "explicit basis",
        request_id="proof",
    )["digest"]
    supporting_text(engine, proof)
    accounts = [
        Entities(engine).register_entity(
            "fund_account",
            {},
            source="synthetic account",
            account_type="platform",
            request_id=f"account-{index}",
        )["entity_id"]
        for index in range(2)
    ]
    movement = {
        "period": "2026-01",
        "platform_account_id": accounts[0],
        "actual_date": "2026-01-03",
        "direction": "outflow",
        "amount_fen": 1900,
        "source_evidence_digest": proof,
        "source_location": "platform.csv!2",
        "transaction_reference": "synthetic-platform-row",
    }
    reserve = {
        "period": "2026-01",
        "actual_date": "2026-01-03",
        "platform_account_id": accounts[0],
        "movement_ids": ["movement"],
        "amount_fen": 1900,
    }
    for kind, subject, data in (
        ("platform_movement", "movement", movement),
        ("managed_reserve_expense", "reserve", reserve),
    ):
        engine.save_fact(
            kind,
            subject,
            data,
            evidence=(proof,),
            expected_revision=0,
            request_id=f"save-{subject}",
        )
        preview = engine.preview([subject])
        engine.confirm(
            [subject],
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id=f"publish-{subject}",
        )

    correction = IdentityCorrections(engine)
    arguments = {
        "changes": [
            {
                "subject_id": "movement",
                "expected_revision": 1,
                "action": "reassign",
                "data": movement | {"platform_account_id": accounts[1]},
            },
            {
                "subject_id": "reserve",
                "expected_revision": 1,
                "action": "reassign",
                "data": reserve | {"platform_account_id": accounts[1]},
            },
        ],
        "evidence": (proof,),
        "reason": "confirmed platform account identity",
        "entity_resolution": {
            "source_entity_id": accounts[0],
            "target_entity_id": accounts[1],
        },
    }
    preview = correction.preview_identity_correction(**arguments)
    correction.confirm_identity_correction(
        **arguments,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="correct",
    )

    with engine.store.connection(read_only=True) as connection:
        balances = dict(connection.execute("SELECT balance_key,amount FROM balance"))
        assert balances.get(accounts[0], 0) == 0
        assert balances[accounts[1]] == -1900
        consumers = engine.store.select(
            connection,
            Read("fact", "*", movement_scope("movement")),
        )
        claims = [
            claim
            for item in consumers
            for claim in item.fact.claims()
            if claim.key == movement_scope("movement")
        ]
        assert [item.subject_id for item in consumers] == ["reserve"]
        assert [claim.amount for claim in claims] == [1]
        assert engine.store.current_fact(connection, "movement").revision == 2
        assert engine.store.current_fact(connection, "reserve").revision == 2
    assert Maintenance(engine).verify_integrity()["status"] == "verified"
