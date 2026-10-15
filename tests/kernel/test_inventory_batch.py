"""Complete material declarations share one atomic, recoverable typed write."""

from __future__ import annotations

import sqlite3

import pytest
from draft_bundle_fixture import synthetic_draft_bundle
from test_local_service import service as service_fixture
from test_replay_close_range import replay_book as replay_book_fixture

from ai_accounting.kernel import dashboard_async
from ai_accounting.kernel.command_schema import command_models, validate_command
from ai_accounting.kernel.contracts import KernelError, NeedsInformation
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.maintenance import Maintenance
from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods
from ai_accounting.kernel.response_contracts import response_schemas, validate_response
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.types import YearMonth, digest

service = service_fixture
replay_book = replay_book_fixture


@pytest.fixture
def book(tmp_path):
    bundle = synthetic_draft_bundle(tmp_path / "contracts")
    engine = Engine(
        Store.create(
            tmp_path / "synthetic-company.sqlite",
            bundle,
            "synthetic-company",
            "91310000123456789A",
            "synthetic-database",
        )
    )
    proofs = [
        engine.register_evidence(
            content,
            "text/plain",
            name,
            request_id="proof-" + str(index),
        )["digest"]
        for index, (name, content) in enumerate(
            (
                ("owner.txt", b"Synthetic owner confirms each exact material declaration"),
                ("first.txt", b"Synthetic first original material"),
                ("second.txt", b"Synthetic second original material"),
            )
        )
    ]
    return engine, Periods(engine), proofs


def declaration(proof, *, period="2026-01", category="transactions", **changes):
    return {
        "period": period,
        "category": category,
        "evidence": [],
        "expected": 0,
        "no_business": True,
        "confirmation_evidence": proof,
    } | changes


def snapshot(engine):
    with engine.store.connection(read_only=True) as connection:
        return tuple(connection.iterdump())


def epochs(engine):
    with engine.store.connection(read_only=True) as connection:
        return engine.store.epochs(connection)


def test_batch_keeps_each_period_category_sources_and_one_epoch_and_queue(book, monkeypatch):
    engine, periods, (owner, first, second) = book
    before = epochs(engine)
    calls = []
    produce = dashboard_async.record_write

    def observed(*args, **kwargs):
        calls.append(kwargs)
        return produce(*args, **kwargs)

    monkeypatch.setattr(dashboard_async, "record_write", observed)
    inputs = [
        declaration(
            owner, category="bank", evidence=[second, first, first], expected=3, no_business=False
        ),
        declaration(owner, period="2026-02", category="tax"),
    ]
    result = periods.inventory_batch(inputs, request_id="two-complete-declarations")
    assert result["status"] == "saved" and len(result["results"]) == 2
    assert [(item["period"], item["category"], item["status"]) for item in result["results"]] == [
        ("2026-01", "bank", "saved"),
        ("2026-02", "tax", "saved"),
    ]
    assert len(calls) == 1
    assert epochs(engine) == before | {"material": before["material"] + 1}
    with engine.store.connection(read_only=True) as connection:
        actual = [
            dict(row)
            for row in connection.execute(
                "SELECT period,category,expected,received,no_business,evidence_digest "
                "FROM material_revision ORDER BY id"
            )
        ]
        assert actual == [
            {
                "period": YearMonth("2026-01").ordinal,
                "category": "bank",
                "expected": 3,
                "received": 2,
                "no_business": 0,
                "evidence_digest": bytes.fromhex(owner),
            },
            {
                "period": YearMonth("2026-02").ordinal,
                "category": "tax",
                "expected": 0,
                "received": 0,
                "no_business": 1,
                "evidence_digest": bytes.fromhex(owner),
            },
        ]
        assert [
            row[0].hex()
            for row in connection.execute(
                "SELECT evidence_digest FROM material_item WHERE inventory_id=? "
                "ORDER BY evidence_digest",
                (result["results"][0]["inventory_id"],),
            )
        ] == sorted([first, second])
        assert (
            connection.execute(
                "SELECT count(*) FROM audit WHERE request_id=? AND action='inventory_batch'",
                ("two-complete-declarations",),
            ).fetchone()[0]
            == 1
        )
    assert engine.request_result("two-complete-declarations")["result"] == result
    assert validate_response("inventory_batch", result) == result
    with engine.store.connection(read_only=True) as connection:
        # A count of missing originals remains a close blocker after batch registration.
        _, issues, _ = periods.completeness(
            connection,
            YearMonth("2026-01").ordinal,
            engine.store.registry,
        )
    assert any(item["field"] == "materials.bank" for item in issues)


def test_same_key_normalizes_each_evidence_list_without_new_rows_or_production(book, monkeypatch):
    engine, periods, (owner, first, second) = book
    row = declaration(owner, evidence=[second, first, first], expected=2, no_business=False)
    first_result = periods.inventory_batch([row], request_id="stable-batch")
    before = snapshot(engine)
    calls = []
    produce = dashboard_async.record_write

    def observed(*args, **kwargs):
        calls.append(kwargs)
        return produce(*args, **kwargs)

    monkeypatch.setattr(dashboard_async, "record_write", observed)
    assert (
        periods.inventory_batch([row | {"evidence": [first, second]}], request_id="stable-batch")
        == first_result
    )
    assert not calls and snapshot(engine) == before
    with pytest.raises(KernelError) as error:
        periods.inventory_batch([row | {"expected": 3}], request_id="stable-batch")
    assert error.value.code == "idempotency_conflict"
    assert snapshot(engine) == before


def test_second_row_cannot_omit_any_earlier_received_original_and_first_row_rolls_back(book):
    engine, periods, (owner, first, second) = book
    periods.inventory(
        "2026-02",
        "bank",
        evidence=[first],
        expected=1,
        no_business=False,
        confirmation_evidence=owner,
        request_id="received-first",
    )
    periods.inventory(
        "2026-02",
        "bank",
        evidence=[first, second],
        expected=2,
        no_business=False,
        confirmation_evidence=owner,
        request_id="received-second",
    )
    before = snapshot(engine)
    with pytest.raises(KernelError) as error:
        periods.inventory_batch(
            [
                declaration(owner, period="2026-01", category="tax"),
                declaration(
                    owner,
                    period="2026-02",
                    category="bank",
                    evidence=[second],
                    expected=1,
                    no_business=False,
                ),
            ],
            request_id="must-not-hide-original",
        )
    assert error.value.code == "received_material_omitted"
    assert snapshot(engine) == before
    assert engine.request_result("must-not-hide-original")["status"] == "unknown"


@pytest.mark.parametrize("batch", [False, True])
def test_registration_preserves_corrected_inputs_without_reviving_old_mistake(book, batch):
    engine, periods, (owner, first, second) = book
    original = periods.inventory(
        **declaration(owner, category="tax", evidence=[first, second],
                      expected=2, no_business=False),
        request_id="before-maintenance-correction",
    )

    def correction(connection):
        # An authorized maintenance correction appends; it never edits the original.
        row = connection.execute(
            "INSERT INTO material_revision"
            "(period,category,expected,received,no_business,evidence_digest) "
            "VALUES(?,'tax',1,1,0,?) RETURNING id",
            (YearMonth("2026-01").ordinal, bytes.fromhex(owner)),
        ).fetchone()
        connection.execute(
            "INSERT INTO material_item VALUES(?,?)", (row[0], bytes.fromhex(second)),
        )
        return {"status": "corrected", "inventory_id": row[0]}

    engine._write(
        "maintenance-correction", digest(["correct", original["inventory_id"], first]),
        epochs(engine), ("material",), "development_inventory_correction", correction,
        _dashboard_periods=("2026-01",),
    )
    current = declaration(owner, category="tax", evidence=[second], expected=1, no_business=False)
    if batch:
        periods.inventory_batch([current], request_id="after-maintenance-correction")
    else:
        periods.inventory(**current, request_id="after-maintenance-correction")
    with engine.store.connection(read_only=True) as connection:
        assert {
            row[0].hex() for row in connection.execute(
                "SELECT evidence_digest FROM material_item WHERE inventory_id=?",
                (original["inventory_id"],),
            )
        } == {first, second}
        assert [
            row[0].hex() for row in connection.execute(
                "SELECT evidence_digest FROM material_item "
                "WHERE inventory_id=(SELECT max(id) FROM material_revision)",
            )
        ] == [second]

    before = snapshot(engine)
    with pytest.raises(KernelError) as error:
        missing = current | {"evidence": []}
        if batch:
            periods.inventory_batch([missing], request_id="cannot-omit-current-input")
        else:
            periods.inventory(**missing, request_id="cannot-omit-current-input")
    assert error.value.code == "received_material_omitted"
    assert snapshot(engine) == before


@pytest.mark.parametrize("missing", ["evidence", "confirmation_evidence"])
def test_unknown_second_row_evidence_rolls_back_all_inventory_and_receipt(book, missing):
    engine, periods, (owner, _, _) = book
    row = declaration(owner, period="2026-02")
    row = row | (
        {"evidence": ["f" * 64], "expected": 1, "no_business": False}
        if missing == "evidence"
        else {"confirmation_evidence": "f" * 64}
    )
    before = snapshot(engine)
    with pytest.raises(sqlite3.IntegrityError):
        periods.inventory_batch([declaration(owner), row], request_id="unknown-original")
    assert snapshot(engine) == before
    assert engine.request_result("unknown-original")["status"] == "unknown"


def test_commit_failure_rolls_back_production_and_exact_original_retry_succeeds(book, monkeypatch):
    engine, periods, (owner, _, _) = book
    inputs = [declaration(owner), declaration(owner, period="2026-02", category="assets")]
    before = snapshot(engine)

    def fail(stage, connection):
        if stage == "commit":
            raise RuntimeError("synthetic failure after materialization before COMMIT")

    with monkeypatch.context() as patch:
        patch.setattr(engine, "fault", fail)
        with pytest.raises(RuntimeError, match="synthetic failure"):
            periods.inventory_batch(inputs, request_id="recover-after-commit-fault")
    assert snapshot(engine) == before
    assert engine.request_result("recover-after-commit-fault")["status"] == "unknown"
    result = periods.inventory_batch(inputs, request_id="recover-after-commit-fault")
    assert len(result["results"]) == 2
    assert engine.request_result("recover-after-commit-fault")["result"] == result


def test_duplicate_month_category_is_rejected_before_any_write(book):
    engine, periods, (owner, _, _) = book
    before = snapshot(engine)
    with pytest.raises(KernelError) as error:
        periods.inventory_batch([declaration(owner), declaration(owner)], request_id="duplicate")
    assert error.value.code == "duplicate_inventory"
    assert snapshot(engine) == before


def test_empty_materials_need_explicit_no_business_and_atomic_validation(book):
    engine, periods, (owner, _, _) = book
    before = snapshot(engine)
    with pytest.raises(NeedsInformation) as error:
        periods.inventory_batch(
            [
                declaration(owner),
                declaration(owner, period="2026-02", no_business=False),
            ],
            request_id="no-implicit-zero",
        )
    assert error.value.issues[0]["field"] == "no_business"
    assert snapshot(engine) == before


@pytest.fixture(scope="module")
def models():
    from ai_accounting.kernel.service import default_registry

    return command_models(default_registry())


@pytest.mark.parametrize(
    "change",
    [
        {"expected": True},
        {"expected": -1},
        {"expected": "0"},
        {"no_business": 1},
        {"period": "2026-13"},
        {"category": "misc"},
        {"extra": "forbidden"},
        {"confirmation_evidence": "not-a-digest"},
        {"evidence": ["not-a-digest"]},
    ],
)
def test_wire_rows_are_complete_strict_business_records(models, change):
    payload = {
        "company_id": "synthetic-company",
        "request_id": "strict-wire",
        "inventories": [declaration("a" * 64) | change],
    }
    with pytest.raises(KernelError) as error:
        validate_command(models, "inventory_batch", payload)
    assert error.value.code == "invalid_command"


@pytest.mark.parametrize(
    "field", ["period", "category", "evidence", "expected", "no_business", "confirmation_evidence"]
)
def test_wire_never_defaults_a_missing_inventory_fact(models, field):
    row = declaration("a" * 64)
    del row[field]
    with pytest.raises(KernelError) as error:
        validate_command(
            models,
            "inventory_batch",
            {
                "company_id": "synthetic-company",
                "request_id": "missing-field",
                "inventories": [row],
            },
        )
    assert error.value.code == "invalid_command"


def test_wire_batch_bounds_do_not_require_real_writes(models):
    rows = [
        declaration(
            "a" * 64,
            period=str(YearMonth.from_ordinal(YearMonth("2020-01").ordinal + offset)),
            category=category,
        )
        for offset in range(120)
        for category in MATERIAL_CATEGORIES
    ]
    payload = {"company_id": "synthetic-company", "request_id": "bounded", "inventories": rows}
    assert len(validate_command(models, "inventory_batch", payload)["inventories"]) == 720
    for invalid in ([], rows + [rows[-1]]):
        with pytest.raises(KernelError) as error:
            validate_command(models, "inventory_batch", payload | {"inventories": invalid})
        assert error.value.code == "invalid_command"


@pytest.mark.parametrize(
    "change",
    [
        {"inventory_id": True},
        {"inventory_id": "1"},
        {"inventory_id": 0},
        {"period": "2026-13"},
        {"category": "misc"},
        {"status": "pending"},
        {"extra": "forbidden"},
    ],
)
def test_batch_response_cannot_deliver_untyped_or_partial_item(change):
    result = {
        "status": "saved",
        "results": [
            {"period": "2026-01", "category": "bank", "status": "saved", "inventory_id": 1}
            | change,
        ],
    }
    with pytest.raises(KernelError) as error:
        validate_response("inventory_batch", result)
    assert error.value.code == "response_contract_mismatch"


def test_batch_response_rejects_empty_results_and_has_discoverable_strict_schema():
    with pytest.raises(KernelError):
        validate_response("inventory_batch", {"status": "saved", "results": []})
    schema = response_schemas()["inventory_batch"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["results"]["minItems"] == 1
    assert schema["properties"]["results"]["maxItems"] == 720


def test_original_inventory_keeps_its_hash_and_normalized_idempotency(book):
    engine, periods, (owner, first, second) = book
    first_result = periods.inventory(
        "2026-01",
        "bank",
        evidence=[second, first, first],
        expected=2,
        no_business=False,
        confirmation_evidence=owner,
        request_id="original-inventory",
    )
    before = snapshot(engine)
    assert (
        periods.inventory(
            "2026-01",
            "bank",
            evidence=[first, second],
            expected=2,
            no_business=False,
            confirmation_evidence=owner,
            request_id="original-inventory",
        )
        == first_result
    )
    assert snapshot(engine) == before
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute(
            "SELECT digest FROM request WHERE id='original-inventory'"
        ).fetchone()[0] == digest(
            [
                "inventory",
                ["2026-01", "bank", sorted([first, second]), 2, False, owner],
            ]
        )


def test_authenticated_dispatch_selected_contract_and_company_local_receipt(service):
    app, company = service
    try:
        selected = app.dispatch(
            "schema",
            {
                "view": "selected",
                "commands": ["inventory_batch"],
                "response_types": ["inventory_batch"],
            },
        )
        assert selected["commands"] == ["inventory_batch"]
        assert set(selected["command_schemas"]) == {"inventory_batch"}
        assert set(selected["response_schemas"]) == {"inventory_batch"}
        assert selected["response_schemas"]["inventory_batch"]["additionalProperties"] is False
        proof = app.engine(company).register_evidence(
            b"Synthetic authenticated inventory confirmation",
            "text/plain",
            "confirmation.txt",
            request_id="authenticated-confirmation",
        )["digest"]
        payload = {
            "company_id": company,
            "request_id": "authenticated-inventory-batch",
            "inventories": [declaration(proof), declaration(proof, period="2026-02")],
        }
        first = app.dispatch("inventory_batch", payload)
        before = snapshot(app.engine(company))
        assert validate_response("inventory_batch", first) == first
        assert app.dispatch("inventory_batch", payload) == first
        assert snapshot(app.engine(company)) == before
        receipt = app.dispatch(
            "request_result",
            {
                "company_id": company,
                "submitted_request_id": payload["request_id"],
            },
        )
        assert receipt["status"] == "committed" and receipt["result"] == first
        assert receipt["company_id"] == company
        assert receipt["database_id"] == app.engine(company).store.database_id
        other = app.dispatch(
            "create_company",
            {
                "taxpayer_id": "91310000123456789B",
                "name": "Second synthetic company",
            },
        )["id"]
        before_other = snapshot(app.engine(other))
        with pytest.raises(sqlite3.IntegrityError):
            app.dispatch("inventory_batch", payload | {"company_id": other})
        assert snapshot(app.engine(other)) == before_other
        assert (
            app.dispatch(
                "request_result",
                {
                    "company_id": other,
                    "submitted_request_id": payload["request_id"],
                },
            )["status"]
            == "unknown"
        )
        assert snapshot(app.engine(company)) == before
    finally:
        app.close()


def test_new_build_preserves_formally_adopted_opening_through_inventory_and_readiness(
    replay_book,
    monkeypatch,
):
    from ai_accounting.kernel import engine as engine_module

    engine = replay_book.app.engine(replay_book.company)
    periods = Periods(engine)

    def adopted_history():
        with engine.store.connection(read_only=True) as connection:
            return {
                table: tuple(tuple(row) for row in connection.execute("SELECT * FROM " + table))
                for table in (
                    "calculation",
                    "calculation_current",
                    "calculation_publication",
                    "voucher",
                    "voucher_version",
                    "voucher_current",
                    "period_close",
                )
            }

    prior_history = adopted_history()
    first_review = periods.preview_close("2026-01", owner_confirmation=replay_book.proof)
    first_manifest = first_review["manifest"]
    assert first_manifest["opening_calculation_id"] is not None
    assert replay_book.closes() == 0
    monkeypatch.setattr(engine_module, "PROGRAM_VERSION", "synthetic-next-inventory-api-build")
    assert Maintenance(engine).verify_integrity()["status"] == "verified"
    reviewed = periods.preview_close("2026-01", owner_confirmation=replay_book.proof)
    assert (
        reviewed["manifest"]["opening_calculation_id"] == first_manifest["opening_calculation_id"]
    )
    assert reviewed["manifest"]["adopted_results"] == first_manifest["adopted_results"]
    assert reviewed["manifest"]["readiness"] == first_manifest["readiness"]
    assert adopted_history() == prior_history
    saved = periods.inventory_batch(
        [
            declaration(replay_book.proof, category="tax"),
            declaration(replay_book.proof, period="2026-02", category="bank"),
        ],
        request_id="new-build-inventory-only",
    )
    assert saved["status"] == "saved"
    assert adopted_history() == prior_history
    assert Maintenance(engine).verify_integrity()["status"] == "verified"
    final_review = periods.preview_close("2026-01", owner_confirmation=replay_book.proof)
    assert (
        final_review["manifest"]["opening_calculation_id"]
        == first_manifest["opening_calculation_id"]
    )
    assert final_review["manifest"]["adopted_results"] == first_manifest["adopted_results"]
    assert final_review["manifest"]["readiness"] == first_manifest["readiness"]
