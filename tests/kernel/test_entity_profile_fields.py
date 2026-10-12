"""One source per fund identity and kind-specific management facts."""

import json

import pytest
from pydantic import ValidationError
from test_fund_account_registration import book as book  # noqa: F401
from test_fund_account_registration import profile, register, snapshot

from ai_accounting.kernel.command_schema import command_models, validate_command
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.entities import display_profile, entity_profile_models
from ai_accounting.kernel.fund_accounts import require_fund_account

ENTITY_CASES = [
    ("person", None), ("organization", None), ("asset", None),
    ("project", None), ("fund_product", None),
    ("fund_account", "bank"), ("fund_account", "platform"), ("fund_account", "cash"),
]


def _registered_entity(book, kind, account_type):
    if kind == "fund_account":
        return register(book, account_type)
    _, entities, _ = book
    return entities.register_entity(
        kind, {"display_name": "合成原始对象"}, source="explicit synthetic fixture",
        request_id="register-" + kind,
    )


def _typed_update(book, entity_id, data, *, revision=1, request_id="typed-update"):
    engine, entities, _ = book
    payload = validate_command(command_models(engine.store.registry), "update_entity_profile", {
        "company_id": engine.store.company_id, "entity_id": entity_id, "data": data,
        "source": "explicit synthetic management update", "expected_revision": revision,
        "request_id": request_id,
    })
    # Normalization must not add a union branch's unprovided management fields.
    assert payload["data"] == data
    payload.pop("company_id")
    return entities.update_entity_profile(**payload)


def _latest_profile(engine, entity_id):
    with engine.store.connection(read_only=True) as connection:
        return json.loads(connection.execute(
            "SELECT content FROM entity_profile_revision WHERE entity_id=? "
            "ORDER BY revision DESC LIMIT 1", (entity_id,),
        ).fetchone()[0])


@pytest.mark.parametrize("kind,account_type", ENTITY_CASES)
def test_typed_management_updates_use_actual_entity_kind(book, kind, account_type):
    engine, _, _ = book
    saved = _registered_entity(book, kind, account_type)
    entity_id = saved["entity_id"]
    result = _typed_update(
        book, entity_id, {"display_name": "新的合成名称", "note": "明确管理备注"},
    )
    assert result["revision"] == 2
    current = _latest_profile(engine, entity_id)
    assert current["display_name"] == "新的合成名称" and current["note"] == "明确管理备注"
    if kind == "person":
        assert current["employment_state"] == "unknown"
    else:
        assert not any(field.startswith("employment_") for field in current)
    if kind == "fund_account":
        assert set(current) == {"display_name", "note", "active"}
        with engine.store.connection(read_only=True) as connection:
            assert require_fund_account(connection, entity_id) == saved["fund_account"]
            assert connection.execute(
                "SELECT count(*) FROM fund_account_identity_revision"
            ).fetchone()[0] == 0


@pytest.mark.parametrize("kind,account_type", ENTITY_CASES)
def test_typed_updates_reject_other_kind_and_removed_fields_atomically(book, kind, account_type):
    engine, _, _ = book
    saved = _registered_entity(book, kind, account_type)
    entity_id = saved["entity_id"]
    invalid = [
        {"purpose": "removed generic fact"},
        {"rights_description": "removed generic fact"},
        {"useful_life_basis": "removed generic fact"},
    ]
    invalid.extend([
        {"employment_start": "2026-01"}, {"employment_end": "2026-02"},
        {"employment_status": "active"}, {"employment_state": "regular"},
    ])
    if kind != "asset":
        invalid.append({"category_label": "asset-only category"})
    if kind != "fund_account":
        invalid.append({"active": False})
    else:
        invalid.extend([
            {"display_number": "copied-number"},
            {"external_identifiers": {"bank_account_number": "copied-number"}},
            {"bank_account_number": "copied-number"},
            {"account_identifier": "copied-platform-identifier"},
        ])
    before = snapshot(engine)
    for index, data in enumerate(invalid):
        with pytest.raises((KernelError, ValidationError)) as rejected:
            _typed_update(book, entity_id, data, request_id=f"invalid-typed-{index}")
        if isinstance(rejected.value, KernelError):
            assert rejected.value.code == "invalid_command"
        assert snapshot(engine) == before


def test_typed_person_metadata_patch_preserves_lifecycle_and_omitted_fields(book):
    engine, entities, _ = book
    registered = entities.register_employee(
        {"display_name": "合成人员", "display_number": "001", "effective_date": "2025-01"},
        source="明确入职资料", request_id="employee-registration",
    )
    entity_id = registered["entity_id"]
    entities.change_employee_employment(
        entity_id, {"action": "unpaid_leave", "effective_date": "2026-01"},
        source="明确停薪留职资料", expected_revision=1, request_id="unpaid-leave",
    )
    _typed_update(book, entity_id, {"note": "管理备注"}, revision=2)
    current = _latest_profile(engine, entity_id)
    assert current["display_name"] == "合成人员" and current["display_number"] == "001"
    assert current["note"] == "管理备注" and current["employment_start"] == "2025-01"
    assert current["employment_status"] == "active"
    assert current["employment_state"] == "unpaid_leave"


@pytest.mark.parametrize("kind", list(entity_profile_models()))
@pytest.mark.parametrize("field", ["purpose", "rights_description", "useful_life_basis"])
def test_removed_generic_entity_fields_are_rejected(kind, field):
    with pytest.raises(ValidationError):
        entity_profile_models()[kind].model_validate({field: "unsupported copied fact"})


@pytest.mark.parametrize(
    "kind", ["organization", "fund_account", "asset", "project", "fund_product"]
)
@pytest.mark.parametrize(
    "field", ["employment_start", "employment_end", "employment_status", "employment_state"]
)
def test_employment_facts_belong_only_to_person_profiles(kind, field):
    value = "2026-01" if field in ("employment_start", "employment_end") else "unknown"
    with pytest.raises(ValidationError):
        entity_profile_models()[kind].model_validate({field: value})


@pytest.mark.parametrize(
    "kind", ["person", "organization", "fund_account", "project", "fund_product"]
)
def test_custom_asset_category_belongs_only_to_asset_profiles(kind):
    with pytest.raises(ValidationError):
        entity_profile_models()[kind].model_validate({"category_label": "软件资产"})


@pytest.mark.parametrize("kind", ["person", "organization", "asset", "project", "fund_product"])
def test_object_enable_flag_is_not_personnel_or_asset_accounting_status(kind):
    with pytest.raises(ValidationError):
        entity_profile_models()[kind].model_validate({"active": False})


@pytest.mark.parametrize("account_type", ["bank", "platform", "cash"])
@pytest.mark.parametrize(
    "copied",
    [
        {"display_number": "001234"},
        {"external_identifiers": {"bank_account_number": "001234"}},
        {"external_identifiers": {"account_identifier": "copied-platform"}},
    ],
)
def test_fund_registration_and_updates_reject_identity_copies(book, account_type, copied):
    engine, entities, _ = book
    copies = [copied]
    if account_type == "bank" and "bank_account_number" in copied.get("external_identifiers", {}):
        copies.append({"external_identifiers": {"bank_account": "000123456789"}})
    before = snapshot(engine)
    for fields in copies:
        with pytest.raises(ValidationError):
            register(book, account_type, profile(account_type) | fields)
        assert snapshot(engine) == before
    saved = register(book, account_type)
    before = snapshot(engine)
    for fields in copies:
        with pytest.raises(ValidationError):
            entities.update_entity_profile(
                saved["entity_id"],
                fields,
                source="synthetic management update",
                expected_revision=1,
                request_id="copied-account-field",
            )
        assert snapshot(engine) == before


@pytest.mark.parametrize(
    "account_type,field",
    [
        ("bank", "bank_account_number"),
        ("platform", "account_identifier"),
    ],
)
def test_account_display_and_search_reuse_only_formal_definition(book, account_type, field):
    engine, entities, proof = book
    data = {key: value for key, value in profile(account_type).items() if key != "display_name"}
    saved = register(book, account_type, data)
    found = entities.find_entities(kind="fund_account", query=data[field])["items"]
    assert len(found) == 1 and found[0]["match"] == "exact"
    assert found[0]["entity_id"] == saved["entity_id"]
    assert "display_number" not in found[0]["profile"]
    assert "external_identifiers" not in found[0]["profile"]
    displayed = display_profile(
        found[0]["profile"],
        "fund_account",
        fund_account=found[0]["fund_account"],
    )
    assert displayed["account_number"] == data[field]
    assert "display_number" not in displayed
    assert displayed["account_number_source"] == {
        "source_type": "fund_account_identity",
        "id": saved["fund_account"]["digest"],
        "entity_id": saved["entity_id"],
        "revision": None,
        "field": "identity." + field,
        "source": "synthetic explicit owner confirmation",
        "evidence_digest": proof,
    }
    with engine.store.connection(read_only=True) as connection:
        content = json.loads(
            connection.execute("SELECT content FROM entity_profile_revision").fetchone()[0]
        )
        assert content == {"display_name": None, "note": None, "active": True}


def test_cash_name_is_only_a_mutable_management_label(book):
    engine, entities, _ = book
    saved = register(book, "cash")
    original = saved["fund_account"]
    assert original["identity"] == {}
    assert original["identity_key"] == saved["entity_id"]
    renamed = entities.update_entity_profile(
        saved["entity_id"],
        {"display_name": "现金备用桶"},
        source="synthetic sourced cash label change",
        expected_revision=1,
        request_id="rename-cash",
    )
    assert renamed["revision"] == 2
    with engine.store.connection(read_only=True) as connection:
        assert require_fund_account(connection, saved["entity_id"]) == original
        assert (
            connection.execute("SELECT count(*) FROM fund_account_identity_revision").fetchone()[0]
            == 0
        )
    found = entities.find_entities(kind="fund_account", query="现金备用桶")["items"]
    assert len(found) == 1 and found[0]["entity_id"] == saved["entity_id"]
    displayed = display_profile(found[0]["profile"], "fund_account", fund_account=original)
    assert displayed["display_name"] == "现金备用桶" and displayed["account_number"] is None
    assert "account_number_source" not in displayed
    before = snapshot(engine)
    with pytest.raises(ValueError, match="update_entity_profile"):
        entities.correct_fund_account_identity(
            saved["entity_id"],
            account_type="cash",
            data={"display_name": "更名"},
            expected_identity_digest=original["digest"],
            recording_error_confirmed=True,
            source="synthetic unsupported identity correction",
            evidence_digest=original["evidence_digest"],
            request_id="cash-identity-change",
        )
    assert snapshot(engine) == before
