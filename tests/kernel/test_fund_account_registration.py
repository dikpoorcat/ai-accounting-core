"""Public fund registration requires sourced identity and an atomic subject binding."""

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from ai_accounting.kernel.command_schema import command_models, validate_command
from ai_accounting.kernel.contracts import NeedsInformation
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.fund_accounts import IDENTITY_FIELDS, require_fund_account
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.storage import Store


@pytest.fixture
def book(tmp_path):
    engine = Engine(
        Store.create(
            tmp_path / "funds.sqlite",
            production_bundle(),
            "synthetic-company",
            "911100000000000001",
            "synthetic-database",
        )
    )
    proof = engine.register_evidence(
        b"synthetic owner confirms exact managed fund identities",
        "text/plain",
        "fund-identities.txt",
        request_id="identity-evidence",
    )["digest"]
    return engine, Entities(engine), proof


def profile(account_type):
    return {
        "bank": {
            "display_name": "公司账户",
            "bank_name": "合成银行",
            "bank_account_number": "000123456789",
            "account_holder_name": "明确开户人",
        },
        "platform": {
            "display_name": "公司账户",
            "provider_name": "合成支付平台",
            "account_identifier": "synthetic@example.test",
            "account_holder_name": "明确开户人",
        },
        "cash": {"display_name": "有确认来源的现金账户"},
    }[account_type].copy()


def snapshot(engine):
    with engine.store.connection(read_only=True) as connection:
        return list(connection.iterdump())


def register(book, account_type, data=None, *, request_id="register", **changes):
    _, entities, proof = book
    arguments = dict(
        account_type=account_type,
        source="synthetic explicit owner confirmation",
        evidence_digest=proof,
        request_id=request_id,
    )
    arguments.update(changes)
    return entities.register_entity(
        "fund_account", profile(account_type) if data is None else data, **arguments
    )


def assert_issue(error, field):
    issues = error.value.response()["fact_issues"]
    issue = next(x for x in issues if x["field"] == field)
    assert issue["semantics"] == "accounting"
    assert issue["allowed_precision"] == ["exact"]
    assert issue["reusable_sources"]


def test_platform_identifier_preserves_meaningful_internal_spaces(book):
    first_data = profile("platform") | {"account_identifier": "synthetic user"}
    second_data = first_data | {"account_identifier": "syntheticuser"}
    first = register(book, "platform", first_data, request_id="first-space-id")
    second = register(book, "platform", second_data, request_id="second-space-id")
    assert first["fund_account"]["identity"]["account_identifier"] == "synthetic user"
    assert first["entity_id"] != second["entity_id"]


@pytest.mark.parametrize(
    "account_type,parent", [("bank", "1002"), ("platform", "1012"), ("cash", "1001")]
)
def test_registration_binds_sourced_identity_and_kernel_assigned_subject(
    book, account_type, parent
):
    engine, entities, proof = book
    result = register(book, account_type)
    with engine.store.connection(read_only=True) as connection:
        definition = require_fund_account(connection, result["entity_id"], account_type)
        assert definition["parent_account"] == parent
        assert definition["detail_code"] == parent + ".0001"
        assert definition["evidence_digest"] == proof
        assert definition["identity"] == {
            k: profile(account_type)[k] for k in IDENTITY_FIELDS[account_type]
        }
    assert result["fund_account"]["detail_code"] == definition["detail_code"]
    assert register(book, account_type) == result
    assert len(entities.find_entities(kind="fund_account")["items"]) == 1


@pytest.mark.parametrize("account_type", ["bank", "platform"])
def test_account_registration_does_not_require_optional_management_display_data(book, account_type):
    data = {key: value for key, value in profile(account_type).items() if key != "display_name"}
    result = register(book, account_type, data)
    assert result["fund_account"]["identity"] == data


@pytest.mark.parametrize(
    "account_type,field",
    [(kind, field) for kind, fields in IDENTITY_FIELDS.items() for field in fields],
)
def test_missing_identity_returns_specific_accounting_issue_without_partial_registration(
    book, account_type, field
):
    engine, _, _ = book
    data = profile(account_type)
    del data[field]
    before = snapshot(engine)
    with pytest.raises(NeedsInformation) as error:
        register(book, account_type, data)
    assert_issue(error, "data." + field)
    assert snapshot(engine) == before


@pytest.mark.parametrize("account_type", ["bank", "platform", "cash"])
@pytest.mark.parametrize("missing", ["source", "evidence_digest", "unregistered_evidence"])
def test_account_source_is_required_and_must_reference_preserved_evidence(
    book, account_type, missing
):
    engine, _, _ = book
    changes = (
        {"source": "   "}
        if missing == "source"
        else {
            "evidence_digest": "00" * 32 if missing == "unregistered_evidence" else None,
        }
    )
    before = snapshot(engine)
    with pytest.raises(NeedsInformation) as error:
        register(book, account_type, **changes)
    assert_issue(error, "source" if missing == "source" else "evidence_digest")
    assert snapshot(engine) == before


@pytest.mark.parametrize(
    "number",
    [123456789, 123.0, "123*789", "123＊789", "123…789", "1.234e8", "１２３４５", "123-789", "   "],
)
def test_bank_number_requires_complete_exact_digit_string(book, number):
    engine, _, _ = book
    data = profile("bank") | {"bank_account_number": number}
    before = snapshot(engine)
    with pytest.raises(NeedsInformation) as error:
        register(book, "bank", data)
    assert_issue(error, "data.bank_account_number")
    assert snapshot(engine) == before


@pytest.mark.parametrize("identifier", [123, "masked***", "hidden…", "   "])
def test_platform_identifier_must_be_complete_exact_string(book, identifier):
    with pytest.raises(NeedsInformation) as error:
        register(book, "platform", profile("platform") | {"account_identifier": identifier})
    assert_issue(error, "data.account_identifier")


@pytest.mark.parametrize("account_type", ["bank", "platform"])
def test_normalized_duplicate_reuses_existing_account_but_conflicting_holder_is_blocked(
    book, account_type
):
    engine, entities, _ = book
    original = register(book, account_type)
    data = profile(account_type)
    field = "bank_account_number" if account_type == "bank" else "account_identifier"
    data[field] = (
        "  " + data[field][:3] + " " + data[field][3:] + "  "
        if account_type == "bank"
        else "  " + data[field] + "  "
    )
    name_field = "bank_name" if account_type == "bank" else "provider_name"
    data[name_field] = "  " + data[name_field] + "  "
    data["account_holder_name"] = "  " + data["account_holder_name"] + "  "
    data["display_name"] = "另一个显示名称"
    duplicate = register(book, account_type, data, request_id="duplicate")
    assert duplicate["status"] == "existing"
    assert duplicate["entity_id"] == original["entity_id"]
    assert duplicate["fund_account"]["detail_code"] == original["fund_account"]["detail_code"]
    assert len(entities.find_entities(kind="fund_account")["items"]) == 1
    before = snapshot(engine)
    data["account_holder_name"] = "不同开户人"
    with pytest.raises(NeedsInformation) as error:
        register(book, account_type, data, request_id="conflict")
    assert_issue(error, "data.account_holder_name")
    assert snapshot(engine) == before


def test_same_display_name_distinct_real_accounts_receive_independent_stable_subjects(book):
    engine, entities, _ = book
    first = register(book, "bank", request_id="first")
    second = register(
        book, "bank", profile("bank") | {"bank_account_number": "000987654321"}, request_id="second"
    )
    assert first["entity_id"] != second["entity_id"]
    assert first["fund_account"]["detail_code"] == "1002.0001"
    assert second["fund_account"]["detail_code"] == "1002.0002"
    entities.update_entity_profile(
        first["entity_id"],
        {"display_name": "后来明确的名称"},
        source="synthetic rename confirmation",
        expected_revision=1,
        request_id="rename",
    )
    with engine.store.connection(read_only=True) as connection:
        assert require_fund_account(connection, first["entity_id"]) == first["fund_account"]
        assert require_fund_account(connection, second["entity_id"]) == second["fund_account"]
    before = snapshot(engine)
    with pytest.raises(ValidationError):
        entities.update_entity_profile(
            first["entity_id"],
            profile("bank") | {"bank_account_number": "000000001"},
            source="ordinary update",
            expected_revision=2,
            request_id="identity-change",
        )
    assert snapshot(engine) == before


def test_failure_after_account_and_subject_installation_rolls_back_entire_registration(book):
    engine, _, _ = book
    before = snapshot(engine)

    def fail(stage, connection):
        if stage == "fund_account_registered":
            assert connection.execute("SELECT count(*) FROM entity").fetchone()[0] == 1
            assert (
                connection.execute("SELECT count(*) FROM fund_account_definition").fetchone()[0]
                == 1
            )
            raise RuntimeError("synthetic account registration failure")

    engine.fault = fail
    with pytest.raises(RuntimeError, match="synthetic account registration failure"):
        register(book, "bank")
    assert snapshot(engine) == before
    engine.fault = lambda stage, connection: None
    assert register(book, "bank")["fund_account"]["detail_code"] == "1002.0001"


@pytest.mark.parametrize("account_type", ["bank", "platform", "cash"])
def test_command_schema_requires_typed_identity_and_evidence(book, account_type):
    engine, _, proof = book
    models = command_models(engine.store.registry)
    payload = dict(
        company_id="synthetic-company",
        kind="fund_account",
        account_type=account_type,
        data=profile(account_type),
        source="synthetic preserved identity",
        evidence_digest=proof,
        request_id="command",
    )
    schema = models["register_entity"].json_schema()
    Draft202012Validator(schema).validate(payload)
    validated = validate_command(models, "register_entity", payload)
    assert {k: validated["data"][k] for k in IDENTITY_FIELDS[account_type]} == {
        k: payload["data"][k] for k in IDENTITY_FIELDS[account_type]
    }
    assert list(
        Draft202012Validator(schema).iter_errors(
            payload | {"data": payload["data"] | {"detail_code": "1002.9999"}}
        )
    )
    for field in IDENTITY_FIELDS[account_type]:
        incomplete = payload | {"data": {k: v for k, v in payload["data"].items() if k != field}}
        assert list(Draft202012Validator(schema).iter_errors(incomplete))
        with pytest.raises(NeedsInformation) as error:
            validate_command(models, "register_entity", incomplete)
        assert_issue(error, "data." + field)
    for field in ("source", "evidence_digest"):
        incomplete = {k: v for k, v in payload.items() if k != field}
        assert list(Draft202012Validator(schema).iter_errors(incomplete))
        with pytest.raises(NeedsInformation) as error:
            validate_command(models, "register_entity", incomplete)
        assert_issue(error, field)
