"""Tiny native dashboard samples exercise authenticated derived storage."""

import copy
import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import date, timedelta

import pytest
from pydantic import ValidationError
from test_banking import book as book
from test_banking import opening

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_read_model import (
    DASHBOARD_READ_MODEL_DDL,
    ModelEpochs,
    ModelScope,
    PageBlock,
    PreparedModel,
    load_model,
    persist_model,
    prepare_model,
)
from ai_accounting.kernel.types import canonical, digest


@pytest.fixture
def database():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(
        """
        CREATE TABLE request(id TEXT PRIMARY KEY,digest BLOB NOT NULL,result TEXT NOT NULL) STRICT;
        CREATE TABLE audit(id INTEGER PRIMARY KEY,request_id TEXT NOT NULL,
          action TEXT NOT NULL,payload TEXT NOT NULL) STRICT;
    """
        + DASHBOARD_READ_MODEL_DDL
    )
    yield connection
    connection.close()


@pytest.fixture(scope="module")
def native(tmp_path_factory):
    engine, save, publish, _ = book.__wrapped__(tmp_path_factory.mktemp("read-model-native"))
    opening(save, publish)
    payload = Dashboard(engine).funds("2026-09")
    context = payload["read_context"]
    with engine.store.connection() as connection:
        epochs = engine.store.epochs(connection)
    scope = ModelScope(
        company_id=context["company_id"],
        database_id=context["database_id"],
        period="2026-09",
        epochs=ModelEpochs(**epochs),
        read_repair_revision=0,
        rule_build="synthetic-dashboard-build",
        as_of=context["as_of"],
    )
    overview = PageBlock("funds/default/overview/0", "dashboard_funds", payload)
    collection = payload["data"]["collections"]["accounts"]
    page = collection["page"]
    section = replace(
        overview,
        key="funds/default/accounts/0",
        keys=tuple((item["account_id"],) for item in collection["items"]),
        total_count=page["total_count"],
        filtered_count=page["filtered_count"],
        sources=(("synthetic-calc", "1" * 64, "synthetic-fact", "2" * 64),),
        voucher_version_ids=("synthetic-voucher",),
        item_sources=((("synthetic-calc", "1" * 64, "synthetic-fact", "2" * 64),),),
        item_voucher_version_ids=(("synthetic-voucher",),),
        fact_sources=(("synthetic-current-fact", "3" * 64),),
        item_fact_sources=((("synthetic-current-fact", "3" * 64),),),
    )
    return scope, [overview, section]


def anchor(connection, models, *, actor=False):
    request_id = models[0].descriptor["request_id"]
    result = {"status": "materialized", "dashboard_models": [model.descriptor for model in models]}
    payload = {"result": result, "actor": {"kind": "synthetic-owner"}} if actor else result
    audit = connection.execute(
        "INSERT INTO audit(request_id,action,payload) VALUES(?,?,?)",
        (request_id, "dashboard_read_model", canonical(payload)),
    )
    connection.execute(
        "INSERT INTO request VALUES(?,?,?)",
        (request_id, digest(["dashboard_read_model", result]), canonical(result)),
    )
    return audit.lastrowid


def produce(
    connection, scope, blocks, *, key="dashboard-read-model/first", parent=None, actor=False
):
    model = prepare_model(scope, blocks, request_id=key, parent_commit_id=parent)
    with connection:
        audit_id = anchor(connection, [model], actor=actor)
        persist_model(connection, model, audit_id=audit_id)
    return model


def assert_corrupt(operation):
    with pytest.raises(KernelError) as failure:
        operation()
    assert failure.value.code == "content_integrity_failed"


@pytest.mark.parametrize("actor", (False, True))
def test_native_responses_and_finite_source_sidecars_round_trip(database, native, actor):
    scope, blocks = native
    produce(database, scope, blocks, actor=actor)
    database.execute("BEGIN")
    loaded = load_model(database, scope)
    assert loaded.read_block(blocks[0].key) == blocks[0].payload
    assert loaded.read_page_block(blocks[1].key) == blocks[1]
    # A caller's mutable returned object never alters the saved block.
    returned = loaded.read_block(blocks[0].key)
    returned["data"]["bank_inflow_fen"] = 123
    assert loaded.read_block(blocks[0].key) == blocks[0].payload


@pytest.mark.parametrize("native_contract", (False, True))
@pytest.mark.parametrize("number", (
    "NaN", "Infinity", "-Infinity", "1e400", "-1e400", "1e999", "-1e999",
))
def test_decoded_block_rejects_nested_nonfinite_before_any_native_adapter(
    native, monkeypatch, native_contract, number,
):
    from ai_accounting.kernel import dashboard_read_model as model

    scope, blocks = native
    prepared = prepare_model(scope, blocks, request_id="nonfinite-block")
    root = json.loads(prepared.root_json)
    reference = model.BlockReference.model_validate_json(canonical(root["blocks"][0]))
    raw = dict(prepared.block_rows)[bytes.fromhex(reference.digest)]
    value = json.loads(raw)
    value["payload"]["historical_numeric"] = {"nested": ["NUMBER"]}
    raw = canonical(value).replace('"NUMBER"', number)

    def unexpected_native_adapter(*_args):
        raise AssertionError("nonfinite reached the native adapter")

    monkeypatch.setattr(model, "_checked_payload", unexpected_native_adapter)
    assert_corrupt(lambda: model._decode_block(
        raw, reference, scope, native_contract=native_contract,
    ))
    assert_corrupt(lambda: model._decode_block_value(
        model.loads_unique(raw), reference, scope, native_contract=native_contract,
    ))
    direct = model.loads_unique(raw)
    direct["payload"]["historical_numeric"]["nested"] = tuple(
        direct["payload"]["historical_numeric"]["nested"]
    )
    assert_corrupt(lambda: model._decode_block_value(
        direct, reference, scope, native_contract=native_contract,
    ))


def test_historical_decoded_block_preserves_finite_json_values(native):
    from ai_accounting.kernel import dashboard_read_model as model

    scope, blocks = native
    prepared = prepare_model(scope, blocks, request_id="finite-historical-block")
    reference = model.BlockReference.model_validate_json(
        canonical(json.loads(prepared.root_json)["blocks"][0])
    )
    value = model.loads_unique(dict(prepared.block_rows)[bytes.fromhex(reference.digest)])
    extra = {"nested": [0.0, -0.0, 1e300, 2**200, None, False, "保留"]}
    value["payload"]["historical_numeric"] = extra
    block = model._decode_block_value(value, reference, scope, native_contract=False)
    assert block.payload["historical_numeric"] == extra
    decoded = model._decode_block(canonical(value), reference, scope, native_contract=False)
    assert decoded.payload["historical_numeric"] == extra


def test_missing_model_is_absent_and_snapshot_is_required(database, native):
    scope, _ = native
    assert_corrupt(lambda: load_model(database, scope))
    database.execute("BEGIN")
    assert load_model(database, scope) is None


@pytest.mark.parametrize(
    "field,value",
    (
        ("read_repair_revision", 1),
        ("rule_build", "different-build"),
        ("as_of", None),
    ),
)
def test_authentic_stale_model_returns_none(database, native, field, value):
    scope, blocks = native
    if field == "as_of":
        value = (date.fromisoformat(scope.as_of) + timedelta(days=1)).isoformat()
    produce(database, scope, blocks)
    database.execute("BEGIN")
    expected = ModelScope.model_validate({**scope.model_dump(), field: value})
    assert load_model(database, expected) is None
    expected = scope.model_copy(
        update={
            "epochs": scope.epochs.model_copy(update={"management": scope.epochs.management + 1})
        }
    )
    assert load_model(database, expected) is None


@pytest.mark.parametrize("field", ("company_id", "database_id"))
def test_wrong_identity_rejects_even_authentic_model(database, native, field):
    scope, blocks = native
    produce(database, scope, blocks)
    database.execute("BEGIN")
    assert_corrupt(lambda: load_model(database, scope.model_copy(update={field: "other"})))


@pytest.mark.parametrize(
    "damage",
    (
        "missing_block",
        "wrong_digest",
        "missing_anchor",
        "wrong_request",
        "request_result",
        "request_digest",
        "empty_audit",
        "missing_locator",
        "wrong_period",
        "action",
    ),
)
def test_missing_or_damaged_content_and_anchor_reject(database, native, damage):
    scope, blocks = native
    prepared = produce(database, scope, blocks)
    block_digest = bytes.fromhex(json.loads(prepared.root_json)["blocks"][0]["digest"])
    with database:
        if damage == "missing_block":
            database.execute(
                "DELETE FROM dashboard_read_model_block WHERE digest=?", (block_digest,)
            )
        elif damage == "wrong_digest":
            database.execute(
                "UPDATE dashboard_read_model_block SET payload='{}' WHERE digest=?", (block_digest,)
            )
        elif damage == "missing_anchor":
            database.execute("UPDATE audit SET payload='{}'")
        elif damage == "wrong_request":
            database.execute("UPDATE audit SET request_id='wrong'")
        elif damage == "request_result":
            database.execute("UPDATE request SET result='{}'")
        elif damage == "request_digest":
            wrong_digest = bytes(32)
            assert len(wrong_digest) == 32
            assert database.execute("SELECT digest FROM request").fetchone()[0] != wrong_digest
            database.execute("UPDATE request SET digest=?", (wrong_digest,))
        elif damage == "empty_audit":
            # Equality and the request hash still authenticate this envelope;
            # an empty descriptor list cannot authorize a retained receipt.
            result = {"status": "materialized", "dashboard_models": []}
            database.execute("UPDATE audit SET payload=?", (canonical(result),))
            database.execute(
                "UPDATE request SET result=?,digest=?",
                (canonical(result), digest(["dashboard_read_model", result])),
            )
        elif damage == "missing_locator":
            database.execute("DELETE FROM dashboard_read_model_current")
        elif damage == "wrong_period":
            database.execute(
                "INSERT INTO dashboard_read_model_current VALUES('2026-08',?)",
                (bytes.fromhex(prepared.descriptor["commit_id"]),),
            )
        else:
            database.execute("UPDATE audit SET action='confirm'")
    database.execute("BEGIN")
    selected_scope = (
        scope.model_copy(update={"period": "2026-08"}) if damage == "wrong_period" else scope
    )
    if damage in ("missing_block", "wrong_digest"):
        loaded = load_model(database, selected_scope)
        assert_corrupt(lambda: loaded.read_block(blocks[0].key))
    else:
        assert_corrupt(lambda: load_model(database, selected_scope))


def test_damaged_root_cannot_hide_behind_stale_epochs(database, native):
    scope, blocks = native
    prepared = produce(database, scope, blocks)
    with database:
        database.execute(
            "UPDATE dashboard_read_model_block SET payload='{}' WHERE digest=?",
            (bytes.fromhex(prepared.descriptor["root_digest"]),),
        )
    database.execute("BEGIN")
    expected = scope.model_copy(update={"read_repair_revision": 1})
    assert_corrupt(lambda: load_model(database, expected))


@pytest.mark.parametrize(
    "field", ("company_id", "database_id", "epochs", "read_repair_revision", "rule_build", "as_of")
)
def test_rehashed_receipt_cannot_forge_original_audit_scope(database, native, field):
    scope, blocks = native
    prepared = produce(database, scope, blocks)
    value = prepared.descriptor
    replacement = {
        "company_id": "fake",
        "database_id": "fake",
        "epochs": {"accounting": 987, "material": 0, "management": 0},
        "read_repair_revision": 98,
        "rule_build": "fake",
        "as_of": (date.fromisoformat(scope.as_of) + timedelta(days=1)).isoformat(),
    }
    value["scope"][field] = replacement[field]
    # Even disabling the immutable row trigger and recomputing its local ID
    # cannot manufacture the original producer's independently anchored receipt.
    value["commit_id"] = digest(
        {key: item for key, item in value.items() if key != "commit_id"}
    ).hex()
    with database:
        database.execute("BEGIN")
        database.execute("PRAGMA defer_foreign_keys=ON")
        database.execute("DROP TRIGGER immutable_dashboard_read_model_commit_update")
        database.execute(
            "UPDATE dashboard_read_model_commit SET id=?,descriptor=?",
            (bytes.fromhex(value["commit_id"]), canonical(value)),
        )
        database.execute(
            "UPDATE dashboard_read_model_current SET commit_id=?",
            (bytes.fromhex(value["commit_id"]),),
        )
    database.execute("BEGIN")
    assert_corrupt(lambda: load_model(database, scope))


@pytest.mark.parametrize("amount", (True, 1.5, "1", 2**63, -(2**63) - 1))
def test_native_money_never_accepts_bool_float_string_or_overflow(native, amount):
    scope, blocks = native
    payload = copy.deepcopy(blocks[0].payload)
    payload["data"]["bank_inflow_fen"] = amount
    assert_corrupt(
        lambda: prepare_model(scope, [replace(blocks[0], payload=payload)], request_id="bad-money")
    )


def test_strict_boolean_and_duplicate_directories_reject(native):
    scope, blocks = native
    payload = copy.deepcopy(blocks[0].payload)
    payload["data"]["collections"]["accounts"]["page"]["has_more"] = 1
    assert_corrupt(
        lambda: prepare_model(scope, [replace(blocks[0], payload=payload)], request_id="bad-bool")
    )
    with pytest.raises(ValidationError):
        prepare_model(scope, [blocks[0], blocks[0]], request_id="duplicate-block")
    duplicated = replace(blocks[1], keys=(blocks[1].keys[0], blocks[1].keys[0]))
    assert_corrupt(lambda: prepare_model(scope, [duplicated], request_id="duplicate-key"))
    assert_corrupt(
        lambda: prepare_model(
            scope,
            [replace(blocks[1], sources=blocks[1].sources * 2)],
            request_id="duplicate-source",
        )
    )
    with pytest.raises(ValidationError):
        ModelScope.model_validate({**scope.model_dump(), "read_repair_revision": True})


def test_collection_count_mismatch_rejects(native):
    scope, blocks = native
    wrong = replace(blocks[1], keys=())
    assert_corrupt(lambda: prepare_model(scope, [wrong], request_id="missing-member"))


def test_receipts_are_immutable_but_derived_blobs_can_be_repaired(database, native):
    scope, blocks = native
    prepared = produce(database, scope, blocks)
    with pytest.raises(sqlite3.IntegrityError):
        database.execute("UPDATE dashboard_read_model_commit SET request_id='rewrite'")
    database.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        database.execute("DELETE FROM dashboard_read_model_commit")
    database.rollback()
    blob_digest, blob = prepared.block_rows[0]
    with database:
        database.execute(
            "UPDATE dashboard_read_model_block SET payload='{}' WHERE digest=?", (blob_digest,)
        )
        database.execute(
            "UPDATE dashboard_read_model_block SET payload=? WHERE digest=?", (blob, blob_digest)
        )
    database.execute("BEGIN")
    assert load_model(database, scope).read_block(blocks[0].key) == blocks[0].payload


def test_derived_locator_cannot_roll_back_to_old_authentic_receipt(database, native):
    scope, blocks = native
    first = produce(database, scope, blocks)
    produce(
        database,
        scope,
        blocks,
        key="dashboard-read-model/second",
        parent=first.descriptor["commit_id"],
    )
    with database:
        database.execute(
            "UPDATE dashboard_read_model_current SET commit_id=?",
            (bytes.fromhex(first.descriptor["commit_id"]),),
        )
    database.execute("BEGIN")
    assert_corrupt(lambda: load_model(database, scope))


def test_failed_late_model_rolls_back_entire_production_batch(database, native):
    scope, blocks = native
    first = prepare_model(scope, blocks, request_id="dashboard-read-model/batch")
    second = prepare_model(
        scope,
        blocks,
        request_id="dashboard-read-model/batch",
        parent_commit_id=first.descriptor["commit_id"],
    )
    with pytest.raises(KernelError):
        with database:
            audit_id = anchor(database, [first])
            persist_model(database, first, audit_id=audit_id)
            # A late producer failure after the first model has been persisted
            # rolls back its receipt, blobs, locator, audit and request too.
            persist_model(database, second, audit_id=audit_id)
    for table in (
        "dashboard_read_model_commit",
        "dashboard_read_model_block",
        "dashboard_read_model_current",
        "audit",
        "request",
    ):
        assert database.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_no_cross_request_or_same_snapshot_success_memo(database, native):
    scope, blocks = native
    prepared = produce(database, scope, blocks)
    database.execute("BEGIN")
    loaded = load_model(database, scope)
    loaded.read_block(blocks[0].key)
    block_digest = bytes.fromhex(json.loads(prepared.root_json)["blocks"][0]["digest"])
    database.execute(
        "UPDATE dashboard_read_model_block SET payload='{}' WHERE digest=?", (block_digest,)
    )
    assert_corrupt(lambda: loaded.read_block(blocks[0].key))


def test_duplicate_json_fields_reject_even_with_matching_local_hash(database, native):
    scope, blocks = native
    prepared = prepare_model(scope, blocks, request_id="dashboard-read-model/duplicate-json")
    root = json.loads(prepared.root_json)
    original_digest = bytes.fromhex(root["blocks"][0]["digest"])
    rows = dict(prepared.block_rows)
    envelope = json.loads(rows.pop(original_digest))
    raw = canonical(envelope)
    # JSON1 and Python disagree about duplicate values, so hashing alone is not
    # sufficient even when all metadata was recomputed by a faulty producer.
    raw = raw[:-1] + ',"kind":"dashboard_funds"}'
    block_digest = hashlib.sha256(raw.encode()).digest()
    root["blocks"][0]["digest"] = block_digest.hex()
    root_raw = canonical(root)
    root_digest = digest(root)
    rows.pop(bytes.fromhex(prepared.descriptor["root_digest"]))
    rows[block_digest], rows[root_digest] = raw, root_raw
    descriptor = prepared.descriptor
    descriptor["root_digest"] = root_digest.hex()
    descriptor["commit_id"] = digest(
        {key: value for key, value in descriptor.items() if key != "commit_id"}
    ).hex()
    bad = PreparedModel(canonical(descriptor), root_raw, tuple(rows.items()))
    with pytest.raises(KernelError):
        with database:
            audit_id = anchor(database, [bad])
            persist_model(database, bad, audit_id=audit_id)
    assert database.execute("SELECT count(*) FROM dashboard_read_model_commit").fetchone()[0] == 0


def test_incomplete_collection_directory_rejects_before_production(native):
    scope, blocks = native
    section = replace(blocks[1], key="funds/default/accounts/1")
    with pytest.raises(ValidationError):
        prepare_model(scope, [blocks[0], section], request_id="missing-ordinal-zero")
    section = replace(blocks[1], total_count=2, filtered_count=2)
    payload = copy.deepcopy(section.payload)
    payload["data"]["collections"]["accounts"]["page"]["total_count"] = 2
    payload["data"]["collections"]["accounts"]["page"]["filtered_count"] = 2
    with pytest.raises(ValidationError):
        prepare_model(
            scope,
            [blocks[0], replace(section, payload=payload)],
            request_id="missing-second-member",
        )


def test_per_item_binding_directory_is_precise_and_complete(native):
    scope, blocks = native
    assert_corrupt(
        lambda: prepare_model(
            scope, [replace(blocks[1], item_sources=())], request_id="missing-item-binding"
        )
    )
    assert_corrupt(
        lambda: prepare_model(
            scope, [replace(blocks[1], item_sources=((),))], request_id="hidden-item-source"
        )
    )
    assert_corrupt(
        lambda: prepare_model(
            scope, [replace(blocks[1], item_voucher_version_ids=((),))], request_id="hidden-voucher"
        )
    )


def binding_metadata(item_count=2):
    sources = [
        ["calculation-a", "1" * 64, "fact-a", "2" * 64],
        ["calculation-b", "3" * 64, "fact-b", "4" * 64],
    ]
    facts = [["fact-c", "5" * 64], ["fact-d", "6" * 64]]
    vouchers = ["voucher-a", "voucher-b"]
    return {
        "keys": [[f"item-{index}"] for index in range(item_count)],
        "sources": sources,
        "fact_sources": facts,
        "voucher_version_ids": vouchers,
        "item_sources": [copy.deepcopy(sources) for _ in range(item_count)],
        "item_fact_sources": [copy.deepcopy(facts) for _ in range(item_count)],
        "item_voucher_version_ids": [list(vouchers) for _ in range(item_count)],
    }


@pytest.mark.parametrize("item_count", (1, 500))
def test_binding_format_work_depends_on_unique_top_sources_not_item_reuse(monkeypatch, item_count):
    from ai_accounting.kernel import dashboard_read_model as model

    metadata = binding_metadata(item_count)
    original_check, original_digest = model._checked_metadata, model._is_digest
    checks, digests = [], []

    def counted_check(value):
        checks.append(value)
        return original_check(value)

    def counted_digest(value):
        digests.append(value)
        return original_digest(value)

    monkeypatch.setattr(model, "_checked_metadata", counted_check)
    monkeypatch.setattr(model, "_is_digest", counted_digest)
    assert model._checked_metadata(metadata) is metadata
    assert len(checks) == 1
    assert digests == [str(index) * 64 for index in range(1, 7)]
    # Every item may legitimately reuse the same complete source bindings.
    assert len(metadata["item_sources"]) == item_count


@pytest.mark.parametrize(
    "directory", ("item_sources", "item_fact_sources", "item_voucher_version_ids"),
)
@pytest.mark.parametrize(
    "defect",
    (
        "directory_null", "directory_tuple", "missing_item", "extra_item",
        "item_null", "item_tuple", "duplicate", "unknown", "missing_union",
        "null_value", "wrong_type", "empty_value",
    ),
)
def test_item_binding_shape_membership_and_complete_union_reject(directory, defect):
    from ai_accounting.kernel.dashboard_read_model import _checked_metadata

    metadata = binding_metadata()
    entries = metadata[directory]
    if defect == "directory_null":
        metadata[directory] = None
    elif defect == "directory_tuple":
        metadata[directory] = tuple(entries)
    elif defect == "missing_item":
        entries.pop()
    elif defect == "extra_item":
        entries.append([])
    elif defect == "item_null":
        entries[0] = None
    elif defect == "item_tuple":
        entries[0] = tuple(entries[0])
    elif defect == "duplicate":
        entries[0].append(copy.deepcopy(entries[0][0]))
    elif defect == "unknown":
        if directory == "item_voucher_version_ids":
            entries[0][0] = "unlisted-voucher"
        else:
            entries[0][0][0] = "unlisted-source"
    elif defect == "missing_union":
        for bindings in entries:
            bindings.pop()
    elif defect == "null_value":
        entries[0][0] = None
    elif defect == "wrong_type":
        entries[0][0] = {"unhashable": []}
    elif defect == "empty_value":
        entries[0][0] = "" if directory == "item_voucher_version_ids" else []
    else:
        raise AssertionError(defect)
    assert_corrupt(lambda: _checked_metadata(metadata))


@pytest.mark.parametrize("item_count", (1, 500))
@pytest.mark.parametrize(
    "directory", ("item_sources", "item_fact_sources", "item_voucher_version_ids"),
)
def test_every_item_binding_is_checked_including_last_growing_item(directory, item_count):
    from ai_accounting.kernel.dashboard_read_model import _checked_metadata

    metadata = binding_metadata(item_count)
    last = metadata[directory][-1]
    if directory == "item_voucher_version_ids":
        last[-1] = "unlisted-last-voucher"
    else:
        last[-1][-1] = "0" * 64
    assert_corrupt(lambda: _checked_metadata(metadata))


@pytest.mark.parametrize("directory,width", (("item_sources", 4), ("item_fact_sources", 2)))
@pytest.mark.parametrize("defect", ("short", "long", "tuple", "hybrid", "null", "bool"))
def test_item_source_rows_require_exact_original_tuple(directory, width, defect):
    from ai_accounting.kernel.dashboard_read_model import _checked_metadata

    metadata = binding_metadata()
    row = metadata[directory][0][0]
    if defect == "short":
        row.pop()
    elif defect == "long":
        row.append("extra")
    elif defect == "tuple":
        metadata[directory][0][0] = tuple(row)
    elif defect == "hybrid":
        row[-1] = metadata[directory][0][1][-1]
    elif defect == "null":
        row[width - 1] = None
    elif defect == "bool":
        row[0] = True
    assert_corrupt(lambda: _checked_metadata(metadata))


def test_empty_overview_keeps_valid_top_bindings_without_item_union():
    from ai_accounting.kernel.dashboard_read_model import _checked_metadata

    metadata = binding_metadata(0)
    assert _checked_metadata(metadata) is metadata
    # The overview exception does not bypass top-level digest validation.
    metadata["sources"][0][1] = "F" * 64
    assert_corrupt(lambda: _checked_metadata(metadata))


@pytest.mark.parametrize("top_subclass", (False, True))
@pytest.mark.parametrize("item_subclass", (False, True))
def test_fact_digest_preserves_str_subclass_acceptance(top_subclass, item_subclass):
    from ai_accounting.kernel.dashboard_read_model import _checked_metadata

    class Digest(str):
        pass

    metadata = binding_metadata()
    if top_subclass:
        metadata["fact_sources"][0][1] = Digest("5" * 64)
    if item_subclass:
        metadata["item_fact_sources"][0][0][1] = Digest("5" * 64)
    assert _checked_metadata(metadata) is metadata


@pytest.mark.parametrize(
    "directory", ("item_sources", "item_fact_sources", "item_voucher_version_ids"),
)
def test_item_identifiers_still_reject_str_subclasses(directory):
    from ai_accounting.kernel.dashboard_read_model import _checked_metadata

    class Identifier(str):
        pass

    metadata = binding_metadata()
    if directory == "item_voucher_version_ids":
        metadata[directory][0][0] = Identifier(metadata[directory][0][0])
    else:
        metadata[directory][0][0][0] = Identifier(metadata[directory][0][0][0])
    assert_corrupt(lambda: _checked_metadata(metadata))


def test_authenticated_root_directory_is_deeply_immutable(database, native):
    scope, blocks = native
    produce(database, scope, blocks)
    database.execute("BEGIN")
    root = load_model(database, scope).root
    assert isinstance(root.blocks, tuple)
    assert isinstance(root.blocks[1].keys, tuple)
    with pytest.raises(ValidationError):
        root.blocks[1].item_count = 0


def test_new_production_cannot_silently_replace_a_corrupt_parent_receipt(database, native):
    scope, blocks = native
    first = produce(database, scope, blocks)
    with database:
        database.execute("UPDATE audit SET payload='{}'")
    assert_corrupt(
        lambda: produce(
            database,
            scope,
            blocks,
            key="dashboard-read-model/second",
            parent=first.descriptor["commit_id"],
        )
    )
    assert database.execute("SELECT count(*) FROM dashboard_read_model_commit").fetchone()[0] == 1
    assert database.execute("SELECT count(*) FROM request").fetchone()[0] == 1
