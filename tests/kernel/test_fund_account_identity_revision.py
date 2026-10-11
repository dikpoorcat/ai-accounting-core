"""Sourced identity corrections retain immutable account subjects and adopted versions."""

import json
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
from jsonschema import Draft202012Validator
from test_fund_account_registration import assert_issue, profile, register, snapshot
from test_fund_account_registration import book as book  # noqa: F401
from test_local_service import service as service  # noqa: F401

from ai_accounting.kernel.command_schema import command_models, validate_command
from ai_accounting.kernel.contracts import KernelError, NeedsInformation
from ai_accounting.kernel.fund_accounts import historical_fund_account, require_fund_account


def correction(book, original, data, *, request_id="correction", **changes):
    _, entities, proof = book
    payload = dict(
        account_type=original["account_type"],
        data=data,
        expected_identity_digest=original["digest"],
        recording_error_confirmed=True,
        source="synthetic owner confirms the original identity recording error",
        evidence_digest=proof,
        request_id=request_id,
    )
    payload.update(changes)
    return entities.correct_fund_account_identity(original["entity_id"], **payload)


@pytest.mark.parametrize("account_type", ["bank", "platform", "cash"])
def test_identity_correction_appends_history_preserving_original_definition_and_subject(
    book, account_type
):
    engine, _, proof = book
    original = register(book, account_type)["fund_account"]
    with engine.store.connection(read_only=True) as connection:
        original_row = tuple(connection.execute("SELECT * FROM fund_account_definition").fetchone())
    data = profile(account_type)
    field = "display_name" if account_type == "cash" else "account_holder_name"
    data[field] = "原件明确纠正后的名称"
    result = correction(book, original, data)
    current = result["fund_account"]
    assert current["digest"] != original["digest"]
    assert current["identity"][field] == data[field]
    assert current["evidence_digest"] == proof
    for key in ("entity_id", "account_type", "parent_account", "detail_code", "sequence"):
        assert current[key] == original[key]
    with engine.store.connection(read_only=True) as connection:
        assert (
            tuple(connection.execute("SELECT * FROM fund_account_definition").fetchone())
            == original_row
        )
        assert require_fund_account(connection, original["entity_id"]) == current
        assert (
            historical_fund_account(connection, original["entity_id"], original["digest"])
            == original
        )
        assert (
            historical_fund_account(connection, original["entity_id"], current["digest"]) == current
        )
        assert (
            connection.execute("SELECT count(*) FROM fund_account_identity_revision").fetchone()[0]
            == 1
        )
        assert connection.execute("SELECT count(*) FROM entity_profile_revision").fetchone()[0] == 1
    before = snapshot(engine)
    assert correction(book, original, data) == result
    assert snapshot(engine) == before


def test_identity_correction_versions_each_source_and_rejects_stale_or_unknown_adoption(book):
    engine, _, _ = book
    original = register(book, "bank")["fund_account"]
    one = correction(book, original, profile("bank") | {"account_holder_name": "第一次正确户名"})[
        "fund_account"
    ]
    two = correction(
        book,
        one,
        profile("bank") | {"account_holder_name": "第二次有据纠错"},
        request_id="second-correction",
    )["fund_account"]
    before = snapshot(engine)
    with pytest.raises(KernelError) as error:
        correction(book, one, profile("bank"), request_id="stale-correction")
    assert error.value.code == "fund_account_identity_version_conflict"
    assert snapshot(engine) == before
    with engine.store.connection(read_only=True) as connection:
        for item in (original, one, two):
            assert historical_fund_account(connection, item["entity_id"], item["digest"]) == item
        with pytest.raises(KernelError) as error:
            historical_fund_account(connection, original["entity_id"], "ff" * 32)
        assert error.value.code == "content_integrity_failed"


def test_concurrent_identity_corrections_with_same_expected_digest_commit_only_one(book):
    engine, _, _ = book
    original = register(book, "bank")["fund_account"]

    def compete(number):
        try:
            correction(
                book,
                original,
                profile("bank") | {"account_holder_name": f"明确开户人-{number}"},
                request_id=f"concurrent-correction-{number}",
            )
            return "corrected"
        except KernelError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(compete, (1, 2)))
    assert sorted(results) == ["corrected", "fund_account_identity_version_conflict"]
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute("SELECT count(*) FROM fund_account_identity_revision").fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM audit WHERE action='correct_fund_account_identity'"
            ).fetchone()[0]
            == 1
        )


def test_correction_duplicate_checks_current_identity_and_releases_old_recording_error(book):
    engine, _, _ = book
    original = register(book, "bank", request_id="first")["fund_account"]
    second_data = profile("bank") | {"bank_account_number": "000987654321"}
    second = register(book, "bank", second_data, request_id="second")["fund_account"]
    before = snapshot(engine)
    with pytest.raises(NeedsInformation) as error:
        correction(book, original, second_data)
    assert_issue(error, "data.bank_account_number")
    assert snapshot(engine) == before
    current = correction(book, original, profile("bank") | {"bank_account_number": "000333333333"})[
        "fund_account"
    ]
    reused_old_number = register(book, "bank", profile("bank"), request_id="correct-real-owner")
    assert reused_old_number["entity_id"] not in (original["entity_id"], second["entity_id"])
    assert reused_old_number["fund_account"]["detail_code"] == "1002.0003"
    with engine.store.connection(read_only=True) as connection:
        assert (
            historical_fund_account(connection, original["entity_id"], original["digest"])
            == original
        )
        assert require_fund_account(connection, current["entity_id"]) == current


@pytest.mark.parametrize(
    "field,value", [("source", "   "), ("evidence_digest", None), ("evidence_digest", "00" * 32)]
)
def test_identity_correction_requires_registered_source_and_is_atomic(book, field, value):
    engine, _, _ = book
    original = register(book, "bank")["fund_account"]
    before = snapshot(engine)
    with pytest.raises(NeedsInformation) as error:
        correction(
            book, original, profile("bank") | {"account_holder_name": "明确更正"}, **{field: value}
        )
    assert_issue(error, field)
    assert snapshot(engine) == before


def test_identity_correction_fault_rolls_back_revision_audit_and_request(book):
    engine, _, _ = book
    original = register(book, "bank")["fund_account"]
    before = snapshot(engine)

    def fail(stage, connection):
        if stage == "fund_account_identity_corrected":
            assert (
                connection.execute(
                    "SELECT count(*) FROM fund_account_identity_revision"
                ).fetchone()[0]
                == 1
            )
            raise RuntimeError("synthetic identity correction interruption")

    engine.fault = fail
    with pytest.raises(RuntimeError, match="synthetic identity correction interruption"):
        correction(book, original, profile("bank") | {"account_holder_name": "明确更正"})
    assert snapshot(engine) == before


def test_duplicate_normalization_does_not_mislabel_bank_spelling_as_holder_conflict(book):
    first = register(book, "bank", profile("bank") | {"bank_name": "Synthetic BANK"})
    duplicate = register(
        book,
        "bank",
        profile("bank") | {"bank_name": "  synthetic bank "},
        request_id="alternate-spelling",
    )
    assert duplicate["entity_id"] == first["entity_id"]


@pytest.mark.parametrize("account_type", ["bank", "platform", "cash"])
def test_identity_correction_command_requires_typed_profile_and_explicit_confirmation(
    book, account_type
):
    engine, _, proof = book
    original = register(book, account_type)["fund_account"]
    models = command_models(engine.store.registry)
    payload = dict(
        company_id="synthetic-company",
        entity_id=original["entity_id"],
        account_type=account_type,
        data=profile(account_type),
        expected_identity_digest=original["digest"],
        recording_error_confirmed=True,
        source="synthetic confirmation",
        evidence_digest=proof,
        request_id="typed-correction",
    )
    schema = models["correct_fund_account_identity"].json_schema()
    Draft202012Validator(schema).validate(payload)
    validated = validate_command(models, "correct_fund_account_identity", payload)
    assert validated["recording_error_confirmed"] is True
    with pytest.raises(KernelError) as error:
        validate_command(
            models, "correct_fund_account_identity", payload | {"recording_error_confirmed": False}
        )
    assert error.value.code == "invalid_command"
    with pytest.raises(NeedsInformation):
        validate_command(models, "correct_fund_account_identity", payload | {"data": {}})
    assert list(Draft202012Validator(schema).iter_errors(payload | {"detail_code": "1002.9999"}))


@pytest.mark.parametrize("transport", ["cli", "mcp"])
def test_identity_correction_contract_is_discovered_and_dispatched_by_cli_and_mcp(
    service,
    transport,
    tmp_path,
    monkeypatch,
    capsys,
):
    from ai_accounting.kernel import cli, mcp

    app, company = service
    engine = app.engine(company)
    proof = engine.register_evidence(
        b"synthetic owner explicitly confirms bank name, number and corrected holder",
        "text/plain",
        "bank-owner-confirmation",
        request_id="public-proof",
    )["digest"]
    original = app.dispatch(
        "register_entity",
        dict(
            company_id=company,
            kind="fund_account",
            account_type="bank",
            data=profile("bank"),
            source="synthetic bank record",
            evidence_digest=proof,
            request_id="public-registration",
        ),
    )["fund_account"]
    payload = dict(
        company_id=company,
        entity_id=original["entity_id"],
        account_type="bank",
        data=profile("bank") | {"account_holder_name": "有据更正开户名"},
        expected_identity_digest=original["digest"],
        recording_error_confirmed=True,
        source="synthetic owner confirms recording error",
        evidence_digest=proof,
        request_id="public-correction",
    )
    selected = app.dispatch(
        "schema", dict(view="selected", commands=["correct_fund_account_identity"])
    )
    assert selected["commands"] == ["correct_fund_account_identity"]
    Draft202012Validator(selected["command_schemas"]["correct_fund_account_identity"]).validate(
        payload
    )

    class InProcessServiceClient:
        def __init__(self, root):
            assert root == tmp_path

        def dispatch(self, command, data):
            return app.dispatch(command, data)

    if transport == "cli":
        request = tmp_path / "identity-request.json"
        request.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(cli, "ServiceClient", InProcessServiceClient)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "finance-local",
                "--root",
                str(tmp_path),
                "call",
                "correct_fund_account_identity",
                "--input",
                str(request),
            ],
        )
        cli.main()
        result = json.loads(capsys.readouterr().out)
    else:
        registered = {}

        class InProcessMCP:
            def __init__(self, *args, **kwargs):
                pass

            def tool(self):
                def keep(function):
                    registered[function.__name__] = function
                    return function

                return keep

            def run(self, **kwargs):
                assert kwargs == {"transport": "stdio"}

        monkeypatch.setattr(mcp, "FastMCP", InProcessMCP)
        monkeypatch.setattr(mcp, "ServiceClient", InProcessServiceClient)
        mcp.serve(tmp_path)
        discovered = registered["finance_local_schema"](
            view="selected",
            commands=["correct_fund_account_identity"],
        )
        assert discovered["command_schemas"] == selected["command_schemas"]
        result = registered["finance_local_command"]("correct_fund_account_identity", payload)
    assert result["status"] == "corrected"
    assert result["fund_account"]["detail_code"] == original["detail_code"]
    assert (
        result["fund_account"]["identity"]["account_holder_name"]
        == payload["data"]["account_holder_name"]
    )
    with engine.store.connection(read_only=True) as connection:
        audit = connection.execute(
            "SELECT action,payload FROM audit WHERE request_id='public-correction'"
        ).fetchone()
        assert audit["action"] == "correct_fund_account_identity"
        recorded = json.loads(audit["payload"])
        assert recorded["result"] == result
        assert recorded["actor"]["owner_id"]
        assert app._test_session_token not in audit["payload"]
